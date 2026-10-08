"""OPS-75: `principles_sync check-view` — mechanized (PRIN-016, PRIN-015).

`docs/architecture/principles.md`'s own PRIN-016 entry records the defect this closes:
`scripts/principles_sync.py check-view` exits 1 correctly on drift between the registry
file and the `arch.principle` beads it is a view of, but ran on no schedule and no CI job
— the file drifted before (PRIN-014 present in the file, missing from the store) with
nothing reporting it. This rides the doctrine-staleness nightly
(`workflows/doctrine_staleness.py`) rather than mint a second schedule: both this and the
staleness report need exactly the same substrate credentials the worker environment
already holds (`config.REQUIRED_CONFIG`'s `SUBSTRATE_URL`/`SUBSTRATE_API_KEY`).

Three conditions, kept structurally distinct (PRIN-015 — a control that cannot evaluate
its question fails closed):

- ``clean``: check-view's own diff is empty.
- ``diverged``: check-view names the file/bead ids and fields that disagree. Raises
  ``factory_dispatcher.doctrine_registry_drift``, naming the diverging ids and which side
  led each disagreement (present-only-in-file / present-only-in-beads / a field mismatch)
  as a structured field, not just prose in the diff lines.
- ``failed_to_evaluate``: the substrate could not be read (network/HTTP failure) or the
  registry file could not be read or parsed. Raises
  ``factory_dispatcher.doctrine_registry_view_unreachable`` instead of silently reporting
  ``clean`` — the four PRIN-015 repairs that principle cites are all instances of a control
  mistaking "could not tell" for "healthy", and this would be a fifth.

One standing ``arch.observation`` (``obs.principles-check-view``), landed on *every* run —
including a clean one, so "ran and found nothing" stays distinguishable from "never ran" —
following `activities/worker_revision_drift.py`'s idiom (PRIN-014: an unattended intake is
idempotent) for the bead itself: updated in place while a divergence or a read failure
persists, resolved the run it clears.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from temporalio import activity

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import principles_sync  # noqa: E402

_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)
from tools import notify  # noqa: E402

import failure_diagnosis  # noqa: E402

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

CREATED_BY = "factory-dispatcher/doctrine-registry-view"
OBSERVATION_KIND = "doctrine_registry_view"
#: Fixed, not dated: one standing record for the one registry file this platform has.
OBSERVATION_REF = "obs.principles-check-view"
HTTP_TIMEOUT_S = 30.0

REGISTRY_PATH = _REPO_ROOT / "docs" / "architecture" / "principles.md"

CLEAN = "clean"
DIVERGED = "diverged"
FAILED_TO_EVALUATE = "failed_to_evaluate"

DIVERGENCE_ALERT_ID = "factory_dispatcher.doctrine_registry_drift"
UNREACHABLE_ALERT_ID = "factory_dispatcher.doctrine_registry_view_unreachable"

#: Not a real Kubernetes object, matching worker_revision_drift.py's own WORKLOAD comment:
#: this is a stable, descriptive identity for the file this observation is about.
WORKLOAD = {
    "cluster": "repository",
    "namespace": "doctrine",
    "kind": "ArchPrincipleRegistry",
    "name": "docs/architecture/principles.md",
}


class DoctrineRegistryViewStore(Protocol):
    """Deliberately narrow: the one `arch.principle` read `check_view` needs, plus the
    three methods this activity needs to land its one standing observation."""

    def list_principles(self) -> list[dict[str, Any]]: ...

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


class SubstrateDoctrineRegistryViewStore:
    """Narrow client for this activity's one read and one standing bead.

    Not `principles_sync.Substrate`: that class's parent
    (`scripts/substrate_client.Substrate`) calls `sys.exit(...)` at construction when
    credentials are absent, which is right for a CLI and wrong for a Temporal activity that
    must turn "cannot evaluate" into a declared alert, never a process exit (PRIN-015). Not
    `activities.doctrine_staleness.SubstratePrincipleObservationStore` either: its
    `update_observation` takes positional `content`/`context` and has no way to just resolve
    without also passing both, where this activity's "clean run still updates the observed
    timestamp" requirement needs the keyword-based, individually-omittable shape
    `activities/worker_revision_drift.py`'s store already established.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here.
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

    def list_principles(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "principle", "limit": 1000},
        )

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "observation", "content_ref": ref, "limit": 1},
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


def default_store() -> DoctrineRegistryViewStore:
    return SubstrateDoctrineRegistryViewStore()


# ---------------------------------------------------------------------------
# evaluate — never raises; "cannot tell" is its own condition (PRIN-015)
# ---------------------------------------------------------------------------


def _diff_summary(diffs: list[str]) -> dict[str, Any]:
    """Classify `principles_sync.diff_view`'s lines into which side leads.

    A typed field (PRIN-003), not prose a reader must open: `file` when every
    disagreement is an id present only in the file (someone edited the file without
    pushing), `beads` when every disagreement is an id present only in the beads (a push
    landed but the file was never regenerated/committed), `mixed` otherwise — including
    any field-level mismatch, which names two present values without saying which is
    newer.
    """
    file_only: list[str] = []
    beads_only: list[str] = []
    field_mismatch: list[str] = []
    for line in diffs:
        entry_id = line.split(":", 1)[0].split(" ")[0]
        if line.endswith("present in the file, missing from the beads"):
            file_only.append(entry_id)
        elif line.endswith("present in the beads, missing from the file"):
            beads_only.append(entry_id)
        else:
            field_mismatch.append(entry_id)

    if file_only and not beads_only and not field_mismatch:
        leader = "file"
    elif beads_only and not file_only and not field_mismatch:
        leader = "beads"
    else:
        leader = "mixed"

    diverging_ids = sorted(set(file_only) | set(beads_only) | set(field_mismatch))
    return {
        "diverging_ids": diverging_ids,
        "leader": leader,
        "file_only_ids": sorted(set(file_only)),
        "beads_only_ids": sorted(set(beads_only)),
        "field_mismatch_ids": sorted(set(field_mismatch)),
    }


def evaluate_registry_view(
    store: DoctrineRegistryViewStore, registry_path: Path = REGISTRY_PATH
) -> dict[str, Any]:
    """Read the file and the beads and compare them. Never raises: reading the file, or
    reaching the substrate, failing for any reason becomes `condition: failed_to_evaluate`
    -- never `clean`, and never an uncaught exception out of a Temporal activity that would
    otherwise report nothing at all."""
    try:
        text = registry_path.read_text()
        ok, diffs = principles_sync.check_view(store, text)
    except Exception as exc:  # noqa: BLE001 - PRIN-015: cannot evaluate is its own condition
        return {
            "condition": FAILED_TO_EVALUATE,
            "error_type": type(exc).__name__,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if ok:
        return {"condition": CLEAN}
    return {"condition": DIVERGED, "diffs": diffs, **_diff_summary(diffs)}


# ---------------------------------------------------------------------------
# land — the one standing observation, updated on every run
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def land_check_view(
    store: DoctrineRegistryViewStore,
    evaluation: dict[str, Any],
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Idempotently land `evaluation` as the one standing check-view observation.

    Unlike `worker_revision_drift.land_worker_revision_drift`, a clean run still writes:
    the acceptance criterion is that the observation trail *knows the check ran*, not only
    that it knows the check once failed. `state` still tracks whether something needs
    attention (`active` for diverged/failed-to-evaluate, `resolved` for clean).
    """
    observed_at = _iso(now_fn())
    condition = evaluation["condition"]
    # "factory-dispatcher/doctrine-registry-view" is enrolled in
    # apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["observed"] (#800, frozenset
    # entry at line 243 on the tree #800 merged) -- declaring "observed" here depends on
    # that enrolment being DEPLOYED, not just merged (unenrolled, this would be a 409). See
    # .factory/design.md.
    content = {
        "ref": OBSERVATION_REF,
        "source_class": "observed",
        "observed_at": observed_at,
        "workload": dict(WORKLOAD),
    }
    context = {
        "observation_kind": OBSERVATION_KIND,
        "condition": condition,
        "checked_at": observed_at,
        "diverging_ids": evaluation.get("diverging_ids", []),
        "leader": evaluation.get("leader"),
        "diffs": evaluation.get("diffs", []),
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


async def _announce_registry_drift_async(
    diverging_ids: list[str], leader: str, diffs: list[str], policy: "notify.AlertPolicy"
) -> bool:
    ids_text = ", ".join(diverging_ids)
    content = (
        f"docs/architecture/principles.md has drifted from the arch.principle beads it "
        f"is a view of: {ids_text} ({leader} leads). " + "; ".join(diffs)
    )
    fingerprint = notify.alert_content_fingerprint("|".join(sorted(diffs)))
    return await notify.send_alert(
        policy,
        DIVERGENCE_ALERT_ID,
        fingerprint,
        content,
        template_values={"diverging_ids": ids_text, "leader": leader},
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_registry_drift(
    diverging_ids: list[str],
    leader: str,
    diffs: list[str],
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a diverged check-view run.

    Best-effort, matching every other alert path in this app: alerting must never crash a
    nightly report whose observation already landed.
    """
    try:
        return asyncio.run(
            _announce_registry_drift_async(
                diverging_ids, leader, diffs, policy or failure_diagnosis.dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce doctrine registry drift (%s); the standing observation "
            "is unaffected",
            diverging_ids,
        )
        return False


async def _announce_registry_view_unreachable_async(
    error: str, policy: "notify.AlertPolicy"
) -> bool:
    content = (
        "principles_sync check-view could not evaluate "
        "docs/architecture/principles.md against the substrate beads: " + error
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


def announce_registry_view_unreachable(
    error: str, *, policy: "notify.AlertPolicy | None" = None
) -> bool:
    """Raise the declared alert when check-view could not evaluate at all (PRIN-015).

    Best-effort, matching `announce_registry_drift`: alerting must never crash a nightly
    report whose observation already landed.
    """
    try:
        return asyncio.run(
            _announce_registry_view_unreachable_async(
                error, policy or failure_diagnosis.dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce doctrine registry view unreachable (%s); the standing "
            "observation is unaffected",
            error,
        )
        return False


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


@activity.defn(name="check_principles_registry_view")
def check_principles_registry_view_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    store = default_store()
    evaluation = evaluate_registry_view(store)

    # Announce before landing: the alert path is webhook-only, while landing reads and
    # writes the same substrate whose unreachability the unreachable alert exists to
    # report -- landing first would crash the activity in exactly the outage the alert
    # is for (PRIN-015), and the alert would never fire.
    if evaluation["condition"] == DIVERGED:
        announce_registry_drift(
            evaluation["diverging_ids"], evaluation["leader"], evaluation["diffs"]
        )
    elif evaluation["condition"] == FAILED_TO_EVALUATE:
        announce_registry_view_unreachable(evaluation["error"])

    try:
        return land_check_view(store, evaluation)
    except Exception as exc:  # noqa: BLE001 - a trail that cannot land must not fail silent
        announce_registry_view_unreachable(
            f"check-view evaluated ({evaluation['condition']}) but landing the standing "
            f"observation failed: {exc!r}"
        )
        raise


ACTIVITIES = [check_principles_registry_view_activity]
