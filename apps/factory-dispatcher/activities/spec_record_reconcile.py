"""Schedule `scanner.scan_spec_record`/`scan_ahead_of_gate_filings` on the credentialed
nightly (PRIN-005: reuse the reconciler that already exists rather than write a second one).

`scanner.py` already reconciles `tasks/` against `dev.task` beads with six labelled kinds,
including `queued-spec-merged-no-bead` -- the OPS-86/OPS-82 dropped-filing shape -- and
`reconcile_spec_record` already knows how to act on it. Nothing schedules either (the OPS-14
class, again). This activity is the schedule, not a second reconciler: every read below is
`scanner.scan_spec_record` / `scanner.scan_ahead_of_gate_filings`, called exactly as
`scanner.py`'s own CLI calls them.

Rides the doctrine-staleness nightly (`workflows/doctrine_staleness.py`) as a third leg,
following the OPS-75 precedent (`activities/doctrine_registry_view.py`) for the PRIN-015 shape:
`evaluate()` never raises (a read failure becomes `condition: failed_to_evaluate`, never
`clean`), `land()` writes one standing `arch.observation` every run so "ran and found nothing"
stays distinguishable from "never ran", and `announce()` raises a declared alert
(`ALERT_INVENTORY` in `apps/mcp-hub/src/tools/notify.py`) rather than a bare exception.

Credential posture is a third state `doctrine_registry_view.py` does not need (it assumes a
Temporal worker is always credentialed): PRIN-015 requires distinguishing a credless host
(`SUBSTRATE_URL`/`SUBSTRATE_API_KEY` simply unset -- not this check's turn to run, skip without
naming which credential is missing) from a credentialed host that cannot reach the substrate
(a real failure, and `failed_to_evaluate` must violate rather than silently pass).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from temporalio import activity

import scanner

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)
from tools import notify  # noqa: E402

import failure_diagnosis  # noqa: E402

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

CREATED_BY = "factory-dispatcher/spec-record-reconcile"
# "factory-dispatcher/spec-record-reconcile" is enrolled in
# apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119,
# #822) -- declaring "derived" here depends on that enrolment being DEPLOYED,
# not just merged (unenrolled, this would be a 409). See .factory/design.md.
SOURCE_CLASS = "derived"
OBSERVATION_KIND = "spec_record_reconcile"
#: Fixed, not dated: one standing record for the one reconcile question this platform has.
OBSERVATION_REF = "obs.spec-record-reconcile"
HTTP_TIMEOUT_S = 30.0

CLEAN = "clean"
DIVERGED = "diverged"
FAILED_TO_EVALUATE = "failed_to_evaluate"
SKIPPED_CREDLESS = "skipped_credless"

DRIFT_ALERT_ID = "factory_dispatcher.spec_record_drift"
UNREACHABLE_ALERT_ID = "factory_dispatcher.spec_record_check_unreachable"

#: Not a real Kubernetes object, matching worker_revision_drift.py's own WORKLOAD comment: a
#: stable, descriptive identity for the population this observation is about.
WORKLOAD = {
    "cluster": "repository",
    "namespace": "factory-dispatcher",
    "kind": "SpecRecord",
    "name": "apps/factory-dispatcher/tasks",
}


class SpecRecordStore(Protocol):
    def list_tasks(self) -> list[dict[str, Any]]: ...

    def find_observation(self, ref: str) -> dict[str, Any] | None: ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]: ...


class SubstrateSpecRecordStore:
    """Narrow client for this activity's one read and one standing bead.

    Takes credentials explicitly rather than reading the environment itself
    (unlike ``SubstrateDoctrineRegistryViewStore``): construction must never be
    the thing that decides credless-vs-unreachable, so that decision is made
    once, by ``resolve_credentials``, before a store is ever built.
    """

    def __init__(self, base_url: str, api_key: str):
        # Header construction lives in substrate_client (the one Python
        # substrate client, M6) rather than duplicated here. Both arguments
        # are always non-empty by the time this is constructed (resolve_credentials()
        # guarantees it), so the shared client's env-fallback branches never fire.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={"namespace": "dev", "type": "task", "limit": 5000},
        )

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={
                "namespace": "arch",
                "type": "observation",
                "content_ref": ref,
                "limit": 1,
            },
        )
        return found[0] if found else None

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"created_by": CREATED_BY}
        if content is not None:
            body["content"] = content
        if context is not None:
            body["context"] = context
        if state is not None:
            body["state"] = state
        return self._request("PATCH", f"/beads/{bead_id}", json=body)


# ---------------------------------------------------------------------------
# credential posture (PRIN-015 both halves)
# ---------------------------------------------------------------------------


def resolve_credentials() -> tuple[str, str] | None:
    """``(url, key)`` when both are configured, else ``None``.

    The credless half of PRIN-015: a host with no ``SUBSTRATE_URL``/
    ``SUBSTRATE_API_KEY`` at all has nothing to name -- it is simply not this
    check's turn to run, not a failure. A host WITH both that then cannot
    reach the substrate is a different, real condition
    (``FAILED_TO_EVALUATE``) and must not be conflated with this one.
    """
    url = os.environ.get("SUBSTRATE_URL")
    key = os.environ.get("SUBSTRATE_API_KEY")
    if not url or not key:
        return None
    return url, key


# ---------------------------------------------------------------------------
# evaluate — never raises; "cannot tell" is its own condition (PRIN-015)
# ---------------------------------------------------------------------------


def evaluate_spec_record(
    store: SpecRecordStore,
    queue_dirs: tuple[Path, ...] = scanner.SPEC_QUEUE_DIRS,
    ahead_of_gate_main_basenames_fn: Callable[
        [], frozenset[str] | None
    ] = scanner.merged_main_tasks_basenames,
) -> dict[str, Any]:
    """Run the existing reconciler's two directions and combine them.

    Never raises: a store-read failure, or ``main`` being unresolvable for
    the ahead-of-gate mirror check, both become ``condition:
    failed_to_evaluate`` -- never ``clean``.
    """
    try:
        issues: list[scanner.SpecRecordIssue] = []
        for tasks_dir in queue_dirs:
            issues.extend(scanner.scan_spec_record(store, tasks_dir=tasks_dir))
        tasks = store.list_tasks()
        ahead_of_gate = scanner.scan_ahead_of_gate_filings(
            tasks, main_basenames_fn=ahead_of_gate_main_basenames_fn
        )
    except Exception as exc:  # noqa: BLE001 - PRIN-015: cannot evaluate is its own condition
        return {
            "condition": FAILED_TO_EVALUATE,
            "error_type": type(exc).__name__,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if ahead_of_gate is None:
        return {
            "condition": FAILED_TO_EVALUATE,
            "error_type": "MainUnresolvable",
            "error": (
                "git could not resolve main, so the ahead-of-gate mirror check cannot "
                "evaluate -- reporting cannot-evaluate rather than risk calling a spec "
                "filed only on a worker branch 'filed'"
            ),
        }
    # done-spec-no-bead predates dev.task beads as the record, not drift an
    # operator has to act on (scanner.render_report's own split).
    real_issues = tuple(i for i in issues if i.kind != "done-spec-no-bead")
    if not real_issues and not ahead_of_gate:
        return {"condition": CLEAN}
    return {
        "condition": DIVERGED,
        "issues": real_issues,
        "ahead_of_gate": ahead_of_gate,
    }


# ---------------------------------------------------------------------------
# land — the one standing observation, updated on every run
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _issue_summary(issue: scanner.SpecRecordIssue) -> dict[str, Any]:
    return {
        "kind": issue.kind,
        "path": issue.display_path,
        "title": issue.title,
        "bead_id": issue.bead_id,
        "bead_state": issue.bead_state,
        "hold_note_category": issue.hold_note_category,
    }


def land_spec_record_check(
    store: SpecRecordStore,
    evaluation: dict[str, Any],
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Idempotently land `evaluation` as the one standing spec-record observation.

    A clean run still writes: the acceptance criterion is that the trail
    *knows the check ran*, not only that it once found drift. `state` tracks
    whether something needs attention (`active` for diverged/
    failed_to_evaluate, `resolved` for clean).
    """
    observed_at = _iso(now_fn())
    condition = evaluation["condition"]
    content = {
        "ref": OBSERVATION_REF,
        "source_class": SOURCE_CLASS,
        "observed_at": observed_at,
        "workload": dict(WORKLOAD),
    }
    issues = evaluation.get("issues") or ()
    ahead_of_gate = evaluation.get("ahead_of_gate") or ()
    context = {
        "observation_kind": OBSERVATION_KIND,
        "condition": condition,
        "checked_at": observed_at,
        "issues": [_issue_summary(i) for i in issues],
        "ahead_of_gate": [_issue_summary(i) for i in ahead_of_gate],
        "error": evaluation.get("error"),
    }
    state = "resolved" if condition == CLEAN else "active"

    existing = store.find_observation(OBSERVATION_REF)
    if existing is None:
        bead = store.create_observation(
            {
                "namespace": "arch",
                "type": "observation",
                "state": state,
                "trust_tier": "system",
                "created_by": CREATED_BY,
                "content": content,
                "context": context,
            }
        )
        return {"status": "reported", "condition": condition, "action": "created", "bead_id": bead.get("id")}

    reopen_or_close = state if existing.get("state") != state else None
    store.update_observation(
        existing["id"], content=content, context=context, state=reopen_or_close
    )
    return {
        "status": "reported",
        "condition": condition,
        "action": "updated",
        "bead_id": existing.get("id"),
    }


# ---------------------------------------------------------------------------
# alert — the standing per-subject dedup policy, shared with every other
# factory-dispatcher alert family (failure_diagnosis.dev_task_alert_policy)
# ---------------------------------------------------------------------------


async def _announce_drift_async(
    issues: tuple[scanner.SpecRecordIssue, ...],
    ahead_of_gate: tuple[scanner.SpecRecordIssue, ...],
    policy: "notify.AlertPolicy",
) -> bool:
    all_issues = tuple(issues) + tuple(ahead_of_gate)
    kinds = ", ".join(sorted({i.kind for i in all_issues}))
    content = (
        f"apps/factory-dispatcher/tasks/ has drifted from dev.task bead state "
        f"({len(all_issues)} issue(s): {kinds}): "
        + "; ".join(f"{i.kind} {i.display_path}" for i in all_issues)
    )
    fingerprint = notify.alert_content_fingerprint(
        "|".join(sorted(f"{i.kind}:{i.display_path}" for i in all_issues))
    )
    return await notify.send_alert(
        policy,
        DRIFT_ALERT_ID,
        fingerprint,
        content,
        template_values={"issue_count": len(all_issues)},
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_drift(
    issues: tuple[scanner.SpecRecordIssue, ...],
    ahead_of_gate: tuple[scanner.SpecRecordIssue, ...],
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a diverged spec-record check.

    Best-effort, matching every other alert path in this app: alerting must
    never crash a nightly report whose observation already landed.
    """
    try:
        return asyncio.run(
            _announce_drift_async(
                issues, ahead_of_gate, policy or failure_diagnosis.dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce spec record drift (%d issue(s)); the standing "
            "observation is unaffected",
            len(issues) + len(ahead_of_gate),
        )
        return False


async def _announce_unreachable_async(error: str, policy: "notify.AlertPolicy") -> bool:
    content = (
        "the nightly spec-record reconcile could not evaluate "
        "apps/factory-dispatcher/tasks/ against dev.task bead state: " + error
    )
    fingerprint = notify.alert_content_fingerprint(error)
    return await notify.send_alert(
        policy,
        UNREACHABLE_ALERT_ID,
        fingerprint,
        content,
        template_values={"error": error},
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_unreachable(error: str, *, policy: "notify.AlertPolicy | None" = None) -> bool:
    """Raise the declared alert when the check could not evaluate at all (PRIN-015).

    Best-effort, matching `announce_drift`: alerting must never crash a
    nightly report whose observation already landed.
    """
    try:
        return asyncio.run(
            _announce_unreachable_async(
                error, policy or failure_diagnosis.dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce spec record check unreachable (%s); the standing "
            "observation is unaffected",
            error,
        )
        return False


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


@activity.defn(name="check_spec_record")
def check_spec_record_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    credentials = resolve_credentials()
    if credentials is None:
        return {"status": "skipped", "condition": SKIPPED_CREDLESS}

    store = SubstrateSpecRecordStore(*credentials)
    evaluation = evaluate_spec_record(store)

    # Announce before landing: the alert path is webhook-only, while landing
    # reads and writes the same substrate whose unreachability the
    # unreachable alert exists to report -- landing first would crash the
    # activity in exactly the outage the alert is for (PRIN-015), and the
    # alert would never fire.
    if evaluation["condition"] == DIVERGED:
        announce_drift(evaluation["issues"], evaluation["ahead_of_gate"])
    elif evaluation["condition"] == FAILED_TO_EVALUATE:
        announce_unreachable(evaluation["error"])

    try:
        return land_spec_record_check(store, evaluation)
    except Exception as exc:  # noqa: BLE001 - a trail that cannot land must not fail silent
        announce_unreachable(
            f"spec-record check evaluated ({evaluation['condition']}) but landing the "
            f"standing observation failed: {exc!r}"
        )
        raise


ACTIVITIES = [check_spec_record_activity]
