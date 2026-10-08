"""Schedule `deployed_revision.describe_deployed_revision_drift` -- the deploy-side half of
OPS-108's AC-5 deferral.

`worker_revision_drift.py` already answers "is the running worker current"; nothing answered
the same question for a *deployed app*. mcp-hub's build failed for nine days (2026-09-03 to
2026-09-12) while the pod reported 1/1 Running, Available, zero restarts, serving a tree missing
two modules that had landed on main. `deployed_revision.py` is the read + comparison; this module
is the schedule, the standing record, and the declared alert -- following
`spec_record_reconcile.py`'s idiom (`evaluate` never raises, `land` writes one standing
`arch.observation` every run so "ran and found nothing" is distinguishable from "never ran",
`announce` raises through `ALERT_INVENTORY` rather than a log line) rather than
`worker_revision_drift.py`'s (that module never alerts at all -- it only lands + remediates,
which is the wrong shape here since there is no safe automated remediation for a deployed pod).

Deliberately imports `deployed_revision` (a leaf module with no first-party dependencies) rather
than `worker_revision`/`dispatch` directly at module scope, and is registered in
`activities/__init__.py` *after* `dispatch_steps`/`worker_revision_drift` -- see
`deployed_revision.py`'s own module docstring and `.factory/design.md` for the circular-import
this avoids when this module is the first thing a fresh interpreter imports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from temporalio import activity

import deployed_revision

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)
from tools import notify  # noqa: E402

import failure_diagnosis  # noqa: E402

CREATED_BY = "factory-dispatcher/deployed-revision-drift"
OBSERVATION_KIND = "deployed_revision_drift"
#: Fixed, not dated: one standing record for the one workload this check watches today.
OBSERVATION_REF = "obs.deployed-revision-drift"
HTTP_TIMEOUT_S = 30.0

#: No compiled-in namespace/deployment default (F4): absent, this check is simply not
#: configured for this host -- PRIN-015's credless posture, not a failure to alert on.
NAMESPACE_ENV = "FACTORY_DEPLOYED_REVISION_NAMESPACE"
DEPLOYMENT_ENV = "FACTORY_DEPLOYED_REVISION_DEPLOYMENT"

DRIFTED = "drifted"
CLEAR = "clear"
UNREACHABLE = "unreachable"
SHA_UNKNOWN = "sha_unknown"

DRIFT_ALERT_ID = "factory_dispatcher.deployed_revision_drift"
UNREACHABLE_ALERT_ID = "factory_dispatcher.deployed_revision_check_unreachable"


class DeployedRevisionDriftStore(Protocol):
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


class SubstrateDeployedRevisionDriftStore:
    """Narrow client for the one bead this activity owns, matching
    `spec_record_reconcile.SubstrateSpecRecordStore`'s shape."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

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


# ---------------------------------------------------------------------------
# credential / configuration posture (PRIN-015)
# ---------------------------------------------------------------------------


def resolve_credentials() -> tuple[str, str] | None:
    """``(url, key)`` when both are configured, else ``None`` -- the credless half of
    PRIN-015, matching `spec_record_reconcile.resolve_credentials` exactly."""
    url = os.environ.get("SUBSTRATE_URL")
    key = os.environ.get("SUBSTRATE_API_KEY")
    if not url or not key:
        return None
    return url, key


def resolve_target(environ: Any = None) -> tuple[str, str] | None:
    """``(namespace, deployment)`` when both are configured, else ``None``.

    Absent means this check is not configured for this host, not that it failed to run --
    the same posture as `resolve_credentials` for SUBSTRATE_URL/SUBSTRATE_API_KEY, applied
    to the kubectl target (F4: no compiled-in namespace/deployment default).
    """
    values = os.environ if environ is None else environ
    namespace = values.get(NAMESPACE_ENV)
    deployment = values.get(DEPLOYMENT_ENV)
    if not namespace or not deployment:
        return None
    return namespace, deployment


# ---------------------------------------------------------------------------
# describe -- reads env-resolved target/remote, calls the pure comparison
# ---------------------------------------------------------------------------


def describe(*, remote: str | None = None) -> deployed_revision.DeployedRevisionStatus | None:
    """Describe drift for the env-configured target, or ``None`` if not configured here.

    ``remote`` defaults to ``os.environ["FACTORY_REMOTE"]`` (required config for every
    factory-dispatcher host -- config.REQUIRED_CONFIG) but accepts an explicit override so a
    caller (and a test) can prove the value is threaded through rather than re-read.
    """
    target = resolve_target()
    if target is None:
        return None
    namespace, deployment = target
    return deployed_revision.describe_deployed_revision_drift(
        namespace=namespace,
        deployment=deployment,
        remote=remote if remote is not None else os.environ["FACTORY_REMOTE"],
    )


# ---------------------------------------------------------------------------
# land -- the one standing observation, updated on every run
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _condition_for(status: deployed_revision.DeployedRevisionStatus) -> str:
    if status.cannot_determine_reason == "unreachable":
        return UNREACHABLE
    if status.cannot_determine_reason == "sha_unknown":
        return SHA_UNKNOWN
    if status.drifted:
        return DRIFTED
    return CLEAR


def _workload(status: deployed_revision.DeployedRevisionStatus) -> dict[str, str]:
    return {
        "cluster": "prod",
        "namespace": status.namespace,
        "kind": "Deployment",
        "name": status.deployment,
    }


def _content(status: deployed_revision.DeployedRevisionStatus, observed_at_iso: str) -> dict[str, Any]:
    # "factory-dispatcher/deployed-revision-drift" is enrolled in
    # apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["observed"] (#800, frozenset
    # entry at line 248 on the tree #800 merged) -- declaring "observed" here depends on
    # that enrolment being DEPLOYED, not just merged (unenrolled, this would be a 409). See
    # .factory/design.md.
    return {
        "ref": OBSERVATION_REF,
        "source_class": "observed",
        "observed_at": observed_at_iso,
        "workload": _workload(status),
    }


def _context(
    status: deployed_revision.DeployedRevisionStatus,
    condition: str,
    *,
    observed_at_iso: str,
    first_observed_at: str,
) -> dict[str, Any]:
    return {
        "observation_kind": OBSERVATION_KIND,
        "condition": condition,
        "namespace": status.namespace,
        "deployment": status.deployment,
        "deployed_sha": status.deployed_sha,
        "tracking_ref": status.tracking_ref,
        "tracking_revision": status.tracking_revision,
        "is_ancestor": status.is_ancestor,
        "pathspec_commits": status.pathspec_commits,
        "error": status.error,
        "first_observed_at": first_observed_at,
        "last_observed_at": observed_at_iso,
    }


def land_deployed_revision_drift(
    store: DeployedRevisionDriftStore,
    status: deployed_revision.DeployedRevisionStatus,
    *,
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Idempotently land `status` as the one standing deployed-revision-drift observation.

    Update in place while the condition stays open (drifted, unreachable, or sha_unknown) --
    two evaluations of an unchanged condition patch the same bead, never create a second.
    Close (`state: "resolved"`) the moment it clears, reopen without minting a new bead if it
    recurs later. Matches `spec_record_reconcile.land_spec_record_check`'s idiom exactly.
    """
    now = now_fn()
    observed_at_iso = _iso(now)
    condition = _condition_for(status)
    content = _content(status, observed_at_iso)

    existing = store.find_observation(OBSERVATION_REF)
    if condition == CLEAR:
        if existing is not None and existing.get("state") == "active":
            store.update_observation(existing["id"], state="resolved")
            return {"status": "reported", "condition": CLEAR, "action": "closed"}
        return {"status": "reported", "condition": CLEAR, "action": "none"}

    first_observed_at = (
        ((existing.get("context") or {}).get("first_observed_at") or observed_at_iso)
        if existing is not None and existing.get("state") == "active"
        else observed_at_iso
    )
    context = _context(
        status, condition, observed_at_iso=observed_at_iso, first_observed_at=first_observed_at
    )

    if existing is None:
        bead = store.create_observation(
            {
                "namespace": "arch",
                "type": "observation",
                "state": "active",
                "trust_tier": "system",
                "created_by": CREATED_BY,
                "content": content,
                "context": context,
            }
        )
        return {
            "status": "reported",
            "condition": condition,
            "action": "created",
            "bead_id": bead.get("id"),
        }

    reopen_state = "active" if existing.get("state") != "active" else None
    store.update_observation(existing["id"], content=content, context=context, state=reopen_state)
    return {
        "status": "reported",
        "condition": condition,
        "action": "updated",
        "bead_id": existing.get("id"),
    }


# ---------------------------------------------------------------------------
# alert -- declared ALERT_INVENTORY entries, dispatcher's own alert-policy state
# ---------------------------------------------------------------------------


def _drift_fingerprint(status: deployed_revision.DeployedRevisionStatus) -> str:
    # Deliberately excludes tracking_revision (F6): main's tip moves every merge, so
    # including it would mint a "new" fingerprint -- and a fresh repost -- every night a
    # drifted pod stays exactly as drifted as the night before.
    return notify.alert_content_fingerprint(f"{status.deployed_sha}:{status.is_ancestor}")


async def _announce_drift_async(
    status: deployed_revision.DeployedRevisionStatus, policy: "notify.AlertPolicy"
) -> bool:
    content = (
        f"{status.namespace}/{status.deployment} is running {status.deployed_sha}, which "
        f"has {status.pathspec_commits or 0} unreleased apps/mcp-hub commit(s) ahead of it "
        f"on {status.tracking_ref} ({status.tracking_revision})."
        if status.is_ancestor
        else (
            f"{status.namespace}/{status.deployment} is running {status.deployed_sha}, which "
            f"is not an ancestor of {status.tracking_ref} ({status.tracking_revision}) at all."
        )
    )
    return await notify.send_alert(
        policy,
        DRIFT_ALERT_ID,
        _drift_fingerprint(status),
        content,
        template_values={
            "namespace": status.namespace,
            "deployment": status.deployment,
            "deployed_sha": status.deployed_sha,
        },
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_drift(
    status: deployed_revision.DeployedRevisionStatus, *, policy: "notify.AlertPolicy | None" = None
) -> bool:
    """Raise the declared alert for a drifted deployed revision.

    Best-effort, matching every other alert path in this app: alerting must never crash a
    nightly report whose observation already landed.
    """
    try:
        return asyncio.run(
            _announce_drift_async(status, policy or failure_diagnosis.dev_task_alert_policy())
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce deployed revision drift for %s/%s; the standing observation "
            "is unaffected",
            status.namespace,
            status.deployment,
        )
        return False


async def _announce_unreachable_async(
    status: deployed_revision.DeployedRevisionStatus, policy: "notify.AlertPolicy"
) -> bool:
    content = (
        f"the nightly deployed-revision-drift check could not evaluate "
        f"{status.namespace}/{status.deployment}: {status.error}"
    )
    return await notify.send_alert(
        policy,
        UNREACHABLE_ALERT_ID,
        notify.alert_content_fingerprint(status.error),
        content,
        template_values={
            "namespace": status.namespace,
            "deployment": status.deployment,
            "error": status.error,
        },
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_unreachable(
    status: deployed_revision.DeployedRevisionStatus, *, policy: "notify.AlertPolicy | None" = None
) -> bool:
    """Raise the declared alert when the check could not evaluate at all (PRIN-015).

    Only for `cannot_determine_reason == "unreachable"` -- a `sha_unknown` status is a
    distinct, expected, temporary state (see deployed_revision.py's module docstring) and is
    landed but never alerted, so it does not repost nightly for every image built before the
    build workflow is wired with a real build-arg.
    """
    try:
        return asyncio.run(
            _announce_unreachable_async(
                status, policy or failure_diagnosis.dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce deployed revision check unreachable for %s/%s (%s); the "
            "standing observation is unaffected",
            status.namespace,
            status.deployment,
            status.error,
        )
        return False


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


@activity.defn(name="check_deployed_revision_drift")
def check_deployed_revision_drift_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    if resolve_target() is None:
        return {"status": "skipped", "condition": "skipped_not_configured"}
    credentials = resolve_credentials()
    if credentials is None:
        return {"status": "skipped", "condition": "skipped_credless"}

    status = describe()
    assert status is not None  # resolve_target() above already confirmed configuration

    if status.cannot_determine_reason == "unreachable":
        announce_unreachable(status)
    elif status.drifted:
        announce_drift(status)

    store = SubstrateDeployedRevisionDriftStore(*credentials)
    try:
        return land_deployed_revision_drift(store, status)
    except Exception as exc:  # noqa: BLE001 - a trail that cannot land must not fail silent
        announce_unreachable(
            deployed_revision.DeployedRevisionStatus.unreachable(
                namespace=status.namespace,
                deployment=status.deployment,
                error=(
                    f"deployed-revision-drift check evaluated "
                    f"({_condition_for(status)}) but landing the standing observation "
                    f"failed: {exc!r}"
                ),
            )
        )
        raise


ACTIVITIES = [check_deployed_revision_drift_activity]
