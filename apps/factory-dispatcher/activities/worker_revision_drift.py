"""Scheduled worker-revision-drift check -- the missing sixth schedule.

`worker_revision.describe_worker_revision_drift` and `schedule_status.py` already compute and
render whether the running worker's revision is drift from main. Neither ever ran except when an
operator typed the command themselves: a worker could run twelve hours on a checkout eleven
commits behind main and nothing would notice. This activity is what makes that evaluation happen
on a schedule (`workflows/worker_revision_drift.py`, `schedule_runtime.py`), the same way EA-model
divergence and release status already do.

Landing follows `activities/ea_observation.py::land_findings`'s already-established idiom for this
exact shape (PRIN-014: an unattended intake is idempotent) rather than the append-per-day idiom
`activities/release_status.py`/`activities/staleness_report.py` use: one standing `arch.observation`
bead, found by a fixed `content.ref`, updated in place while the condition is open (`drifted` or
`unknown`), closed (`state: "resolved"`) when it clears, and reopened without a second bead if it
recurs. See `.factory/design.md` for why `arch.observation` -- not a new bead type -- is the right
carrier, and why this needed its own store class rather than reusing
`activities/ea_observation.py`'s (that one has no `context` param on `update_observation`; the
mutable drift detail here -- revision, commits behind, uptime -- has to live in `context`, since
`ArchObservationContent` is closed and does not carry those fields).

2026-09-02: landing the observation used to be the whole activity -- read-only, non-remediating,
with `scripts/factory-redeploy.py` the only path that could actually move the checkout. A worker
could (and did) sit behind nine merged PRs for the better part of an hour with this schedule
faithfully reporting `drifted: true` every fifteen minutes and nothing acting on it. Landing the
observation still happens first and unconditionally (the durable record of what was observed must
not depend on what the response then does), but a `drifted` status now also goes through
`worker_checkout_drift_response.respond_to_worker_revision_drift`: advance the checkout and recycle
the worker, defer if a dispatch run is in flight, or halt dispatch by pausing
`config.DISPATCH_SCHEDULE_ID` (the same mechanism `activities/dispatch_steps.py` already uses for
capacity backpressure) when advancing itself is not safe. See `.factory/design.md` and
`worker_checkout_drift_response.py`'s own module docstring for the full design.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, NamedTuple, Protocol

import httpx
from temporalio import activity
from temporalio.client import Client, ScheduleState, ScheduleUpdate

import dispatch
import failure_diagnosis
import schedule_runtime
import worker_checkout
import worker_checkout_drift_response as drift_response
import worker_revision
from config import config

logger = logging.getLogger(__name__)

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

CREATED_BY = "factory-dispatcher/worker-revision-drift"
OBSERVATION_KIND = "worker_revision_drift"
#: This mechanism's own label in `schedule_runtime.NON_DISPATCH_SCHEDULE_ENV_DEFAULTS` --
#: `_factory_activity_in_flight_async`'s unreadable-workflow-id carve-out below is scoped to
#: exactly this label, not to "worker_revision_drift" typed twice in two files.
OWN_SCHEDULE_LABEL = "worker_revision_drift"
#: Fixed, not dated: this is the idempotency key for the one standing record a single-tenant host
#: ever needs (worker_revision.py's own docstring: "single-tenant factory host").
OBSERVATION_REF = "obs.worker-revision-drift"
HTTP_TIMEOUT_S = 30.0

DRIFTED = "drifted"
UNKNOWN = "unknown"
CLEAR = "clear"

#: Not a real Kubernetes object -- ArchObservedWorkload requires a non-blank
#: cluster/namespace/kind/name, and the worker this reports on is a launchd process, not a
#: cluster workload. This is a stable, descriptive identity for that process, not a claim that
#: one exists in any cluster inventory.
WORKLOAD = {
    "cluster": "factory",
    "namespace": "factory-dispatcher",
    "kind": "Process",
    "name": "worker",
}


class WorkerRevisionDriftStore(Protocol):
    """Deliberately three methods: this activity never lists, links, or transitions anything
    beyond the one standing record it owns."""

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


class SubstrateWorkerRevisionDriftStore:
    """Narrow client for the one bead this activity owns, matching
    `activities/ea_observation.py::SubstrateEAObserverStore`'s shape -- one tested consumer,
    three methods, nothing more."""

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


def default_store() -> WorkerRevisionDriftStore:
    return SubstrateWorkerRevisionDriftStore()


#: `pause_dispatch_schedule`'s `temporal` param wants exactly these two names -- matching
#: `activities/dispatch_steps.py::TEMPORAL_PAUSE_TYPES`, defined independently rather than
#: imported from that module so this file's Temporal wiring does not depend on another
#: activity module's internals.
_TEMPORAL_PAUSE_TYPES = SimpleNamespace(
    ScheduleState=ScheduleState,
    ScheduleUpdate=ScheduleUpdate,
)

def __getattr__(name: str) -> Any:
    """Resolve `WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX` lazily (PEP 562), not as an
    import-time alias of `drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX`.

    Owned by `worker_checkout_drift_response.py`, not redeclared here: that module's
    `respond_to_cleared_drift` needs the exact same text to recognize a pause as its own before
    resuming it (f51e057c F2), so there is exactly one place this string can drift -- this
    module still exposes it under its own name (`tests/test_worker_revision_drift_schedule.py`
    reads `wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX`), just not via an eager module-level
    assignment.

    An eager `WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX = drift_response....` here previously
    detonated the documented standalone-import cycle: importing `worker_checkout_drift_response`
    first re-enters this module through `dispatch` -> `activities` -> `activities.worker_revision_drift`
    while `drift_response` (that same `worker_checkout_drift_response` module, still mid-import)
    has not yet reached the line defining the prefix, raising `AttributeError: partially
    initialized module`. Deferring the lookup to first access -- always after every import in the
    chain has finished -- avoids that ordering dependency entirely.
    """
    if name == "WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX":
        return drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def _connect_temporal() -> Any:
    return await Client.connect(config.TEMPORAL_URL, namespace=config.TEMPORAL_NAMESPACE)


class FactoryActivityInFlight(NamedTuple):
    """Whether some Temporal-scheduled factory activity is currently running, and which.

    Mirrors `activities.dispatch_steps.DispatchRunLockResult`'s shape for the same reason:
    `__bool__` defers to `in_flight` so a caller that only wants the yes/no
    (`worker_checkout_drift_response.respond_to_worker_revision_drift`'s `if
    factory_activity_in_flight():`) keeps working exactly as it did when this was a plain bool,
    while a caller that must say what it is waiting on (PRIN-008: a refusal must name its
    cause -- the acceptance criterion this exists for) reads `.description`.
    """

    in_flight: bool
    description: str = ""
    #: True when the in-flight action this found belongs to `config.DISPATCH_SCHEDULE_ID` --
    #: dev.finding 639a20c5 AC-4: `_escalate_persistent_deferral` reads this (via
    #: `worker_checkout_drift_response.DriftResponse.blocked_by_dispatch`) to decide whether
    #: pausing dispatch can actually help. Pausing it for a reconciler-blocked streak drains
    #: dispatch capacity for no benefit, since the reconciler runs regardless of dispatch's pause
    #: state -- 83 of the 97 drift runs replayed from the 2026-09-30/10-01 incident were blocked
    #: by a reconciler, not dispatch, which pausing dispatch harder could never clear.
    dispatch: bool = False

    def __bool__(self) -> bool:
        return self.in_flight


def _current_activity_workflow_id() -> str | None:
    """This activity's own `workflow_id` when running inside a real Temporal activity context,
    else `None`.

    Mirrors `activities.dispatch_steps._current_workflow_run_identity`'s exact guard for the
    identical "is there a real context to ask" question: `activity.in_activity()` is False in
    every direct-call unit test (no worker ever established a context), so those tests get
    `None` and the self-exclusion below is simply never triggered -- matching production
    exactly, since a direct call has no "self" workflow to exclude.
    """
    if not activity.in_activity():
        return None
    return activity.info().workflow_id


async def _factory_activity_in_flight_async() -> FactoryActivityInFlight:
    client = await _connect_temporal()
    self_workflow_id = _current_activity_workflow_id()
    # config.DISPATCH_SCHEDULE_ID first (the schedule this mechanism used to check
    # exclusively), then every non-dispatch schedule this worker registers, from the one
    # registry `worker.py`'s own registration calls also read
    # (`schedule_runtime.NON_DISPATCH_SCHEDULE_ENV_DEFAULTS`) -- so a thirteenth schedule
    # added there is checked here automatically, never only in one of the two places.
    #
    # That registry includes this very mechanism's own schedule ("worker_revision_drift"),
    # because `worker.py` registers it exactly like every other reconciler. Left unfiltered,
    # this activity would see ITS OWN currently-running workflow as a running action of the
    # worker_revision_drift schedule, defer, and never advance a drifted checkout at all --
    # see the self_workflow_id filter below.
    schedule_ids = {"dispatch": config.DISPATCH_SCHEDULE_ID}
    schedule_ids.update(schedule_runtime.non_dispatch_schedule_ids())
    for label, schedule_id in schedule_ids.items():
        status = await schedule_runtime.describe_factory_schedule_status(
            client, schedule_id=schedule_id
        )
        for workflow in status.in_flight:
            if self_workflow_id is not None and workflow.workflow_id == self_workflow_id:
                # This evaluation's own in-flight run, not some OTHER activity that could
                # race the checkout mutation this guard exists to protect. Excluding by
                # workflow_id (rather than dropping the whole "worker_revision_drift" label
                # from the walk) still catches a genuine second, concurrent run of this same
                # mechanism, which the pre-fix code -- and a label-based exclusion -- would
                # both have missed.
                continue
            if (
                self_workflow_id is not None
                and label == OWN_SCHEDULE_LABEL
                and workflow.workflow_id == schedule_runtime.UNKNOWN_WORKFLOW_ID
            ):
                # Release-gate finding on PR #897: Temporal can describe a running scheduled
                # action with no readable workflow_id at all (surfaces from
                # `schedule_runtime._in_flight_workflow` as UNKNOWN_WORKFLOW_ID), and that
                # sentinel can never equal a real self_workflow_id -- the equality check above
                # would then never recognize this evaluation's own run, defer forever, and
                # re-open the exact permanent dispatch pause (#894) the check above exists to
                # prevent. Identity by workflow_id is impossible here because there is no
                # workflow_id to compare; ON THIS MECHANISM'S OWN SCHEDULE LABEL ONLY, an
                # unreadable id is treated as self rather than as a foreign activity. This is
                # narrower than exempting the whole schedule: a genuinely concurrent second run
                # whose id IS readable still falls through to the equality check above and is
                # still caught.
                continue
            return FactoryActivityInFlight(
                True,
                f"schedule {schedule_id!r} ({label}) has an in-flight workflow "
                f"(workflow_id={workflow.workflow_id})",
                dispatch=(schedule_id == config.DISPATCH_SCHEDULE_ID),
            )
    return FactoryActivityInFlight(False)


def default_factory_activity_in_flight() -> FactoryActivityInFlight:
    """True (naming which one) when ANY schedule this worker registers -- dispatch or one of
    the eleven reconcilers -- reports a currently-running action OTHER than this evaluation
    itself.

    2026-09-16 (OPS-99 follow-up): this used to check only `config.DISPATCH_SCHEDULE_ID`,
    which was a sufficient exclusivity guarantee purely by accident of arithmetic when
    `worker.ACTIVITY_EXECUTOR_CONCURRENCY` was 1 -- nothing else could be in flight if dispatch
    was not, because there was exactly one activity slot for all twelve schedules. OPS-99
    raised that constant to 12 precisely so the eleven 15-minute reconcilers stop starving
    behind an in-flight ~80-minute dispatch `run`; the reconcilers correctly began running
    concurrently with dispatch, but this guard's "is dispatch running" question silently
    stopped being the right question to ask. `worker_checkout.advance()` mutates the shared
    checkout on disk (`git fetch`/`checkout --detach`/`branch -f`) and the drift response that
    calls it then kills the worker process; a reconciler reading that checkout underneath
    either of those is exactly the race concurrency 1 made structurally impossible and
    concurrency 12 makes routine. See `.factory/design.md` for the full survey.

    2026-09-16, second pass (release-gate DO-NOT-MERGE on PR #894): the first broadened guard
    walked `schedule_runtime.non_dispatch_schedule_ids()`, which correctly includes this
    mechanism's OWN schedule ("worker_revision_drift") -- `worker.py` registers it exactly
    like every other reconciler, and the registry must not lie about what is actually
    registered. But nothing then excluded this evaluation's own currently-running workflow
    from that schedule's `running_actions`, so the activity always observed itself in flight,
    always deferred, and after `MAX_CONSECUTIVE_DEFERRALS` escalated to a dispatch pause that
    nothing could ever clear (the remedy that would clear it was the thing being blocked).
    `_current_activity_workflow_id` + the per-workflow filter above is the fix: exclude this
    run's own workflow_id specifically, not its whole schedule, so a second, genuinely
    concurrent run of this same mechanism (if one ever existed) would still be caught.
    """
    return asyncio.run(_factory_activity_in_flight_async())


async def _halt_dispatch_async(note: str) -> None:
    client = await _connect_temporal()
    await schedule_runtime.pause_dispatch_schedule(
        client,
        _TEMPORAL_PAUSE_TYPES,
        schedule_id=config.DISPATCH_SCHEDULE_ID,
        note=f"{drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; {note}",
    )


def default_halt_dispatch(note: str) -> None:
    """Pause the dispatch schedule -- the platform's declared alert for "dispatch stopped"."""
    asyncio.run(_halt_dispatch_async(note))


async def _dispatch_schedule_pause_state_async() -> tuple[bool, str]:
    client = await _connect_temporal()
    return await schedule_runtime.dispatch_schedule_pause_state(
        client, schedule_id=config.DISPATCH_SCHEDULE_ID
    )


def default_dispatch_schedule_pause_state() -> tuple[bool, str]:
    """`(paused, note)` for `config.DISPATCH_SCHEDULE_ID` -- matching
    `activities/capacity_pause_response.py::default_dispatch_schedule_pause_state` exactly, so
    `respond_to_cleared_drift` can tell its own pause apart from any other."""
    return asyncio.run(_dispatch_schedule_pause_state_async())


async def _resume_dispatch_async(note: str) -> None:
    client = await _connect_temporal()
    await schedule_runtime.resume_dispatch_schedule(
        client,
        _TEMPORAL_PAUSE_TYPES,
        schedule_id=config.DISPATCH_SCHEDULE_ID,
        note=note,
        logger=logger,
    )


def default_resume_dispatch(note: str) -> None:
    """Resume `config.DISPATCH_SCHEDULE_ID` -- the platform's declared "dispatch is running
    again" mechanism, mirroring `activities/capacity_pause_response.py`'s resume call."""
    asyncio.run(_resume_dispatch_async(note))


def default_supervisor() -> drift_response.WorkerSupervisor:
    import launchd_agent

    return launchd_agent.LaunchdWorkerSupervisor()


def default_source_repo_root(checkout_root: Path) -> Path:
    return worker_checkout.declared_source_repo_root(checkout_root)


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _condition_for(status: worker_revision.WorkerRevisionStatus) -> str:
    """`unknown` is a distinct third value, never folded into `clear` -- an indeterminate
    revision must never be recorded as though it were confirmed current."""
    if status.error:
        return UNKNOWN
    if status.drifted:
        return DRIFTED
    return CLEAR


def _running_for_seconds(
    status: worker_revision.WorkerRevisionStatus, *, observed_at: datetime
) -> float | None:
    if not status.worker_started_at:
        return None
    text = status.worker_started_at
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        started_at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (observed_at - started_at).total_seconds())


def _content(observed_at_iso: str) -> dict[str, Any]:
    return {
        "ref": OBSERVATION_REF,
        "observed_at": observed_at_iso,
        "workload": dict(WORKLOAD),
        "source_class": "observed",
    }


def _context(
    status: worker_revision.WorkerRevisionStatus,
    condition: str,
    *,
    now: datetime,
    observed_at_iso: str,
    first_observed_at: str,
    consecutive_deferrals: int = 0,
) -> dict[str, Any]:
    return {
        "observation_kind": OBSERVATION_KIND,
        "condition": condition,
        "worker_revision": status.worker_revision,
        "commits_behind": status.commits_behind,
        "is_ancestor": status.is_ancestor,
        "worker_started_at": status.worker_started_at,
        "running_for_seconds": _running_for_seconds(status, observed_at=now),
        "main_ref": status.main_ref,
        "main_revision": status.main_revision,
        "error": status.error,
        "first_observed_at": first_observed_at,
        "last_observed_at": observed_at_iso,
        #: Carried forward from the prior evaluation of the same open condition (reset to 0 on a
        #: fresh occurrence, same as `first_observed_at`), then advanced by
        #: `_escalate_persistent_deferral` once the response for *this* run is known. Landing
        #: happens before the response is computed (see `report_worker_revision_drift_activity`),
        #: so this write only carries the count forward; it never itself increments it.
        "consecutive_deferrals": consecutive_deferrals,
    }


def land_worker_revision_drift(
    store: WorkerRevisionDriftStore,
    status: worker_revision.WorkerRevisionStatus,
    *,
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Idempotently land `status` as the one standing worker-revision-drift observation.

    Update in place while the condition stays open (drifted or unknown) -- two evaluations of an
    unchanged condition patch the same bead, they never create a second. Close (`state:
    "resolved"`) the moment it clears, and reopen without minting a new bead if it recurs later.
    """
    now = now_fn()
    observed_at_iso = _iso(now)
    condition = _condition_for(status)
    existing = store.find_observation(OBSERVATION_REF)
    existing_open = bool(existing) and existing.get("state") == "active"

    if condition == CLEAR:
        if existing_open:
            store.update_observation(existing["id"], state="resolved")
            return {"status": "reported", "condition": CLEAR, "action": "closed"}
        return {"status": "reported", "condition": CLEAR, "action": "none"}

    existing_context = (existing.get("context") or {}) if existing_open else {}
    first_observed_at = existing_context.get("first_observed_at") or observed_at_iso
    consecutive_deferrals = int(existing_context.get("consecutive_deferrals") or 0)
    content = _content(observed_at_iso)
    context = _context(
        status,
        condition,
        now=now,
        observed_at_iso=observed_at_iso,
        first_observed_at=first_observed_at,
        consecutive_deferrals=consecutive_deferrals,
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


#: 2026-09-13 (f51e057c): factory_activity_in_flight() gates the remedy, and the dispatch queue is
#: effectively never idle -- the same condition was deferred 15 consecutive times, 20:15Z-22:45Z,
#: with nothing ever escalating. `respond_to_worker_revision_drift`'s DEFERRED choice is correct
#: in isolation (never race a running task); the bound below is what stops that correct-in-isolation
#: choice from repeating forever unannounced. 3 is the implementer's choice: at this schedule's
#: 15-minute cadence that is 45-60 minutes of deferral before escalating, long enough to absorb one
#: ordinary busy stretch of the dispatch queue without ever again letting a 2.5-hour silent streak
#: like 2026-09-13's happen unannounced.
MAX_CONSECUTIVE_DEFERRALS = 3


def _escalate_persistent_deferral(
    store: WorkerRevisionDriftStore,
    bead_id: str,
    response: drift_response.DriftResponse,
    status: worker_revision.WorkerRevisionStatus,
) -> drift_response.DriftResponse:
    """Turn a `DEFERRED` outcome into `ESCALATED` once the same condition has been deferred too
    many consecutive times, rather than letting `respond_to_worker_revision_drift` (which has no
    memory of prior runs) return `DEFERRED` again forever (PRIN-008: a problem must announce
    itself, not just repeat its own unremarkable-looking refusal on a schedule).

    The streak lives in the standing observation bead's context (`consecutive_deferrals`),
    carried forward by `_context`/`land_worker_revision_drift` and advanced here once the
    response for this run is known. Reset to 0 the moment the response is anything other than
    `DEFERRED` -- an advance, a restart, or a halt all mean the condition is being acted on, not
    silently deferred.
    """
    bead = store.find_observation(OBSERVATION_REF)
    context = dict((bead.get("context") or {}) if bead else {})
    prior = int(context.get("consecutive_deferrals") or 0)
    new_count = prior + 1 if response.action == drift_response.DEFERRED else 0

    if new_count != prior:
        context["consecutive_deferrals"] = new_count
        store.update_observation(bead_id, context=context)

    if response.action == drift_response.DEFERRED and new_count > MAX_CONSECUTIVE_DEFERRALS:
        # dev.finding 639a20c5 AC-4: read the cause from structured state
        # (`DriftResponse.blocked_by_dispatch`, set by `respond_to_worker_revision_drift` from
        # the in-flight result that produced THIS crossing deferral), never from `response.detail`
        # prose. Pausing dispatch only helps when dispatch is the blocker -- 83 of the 97 drift
        # runs replayed from the 2026-09-30/10-01 incident were blocked by a 15-minute
        # reconciler, not dispatch, which pausing dispatch harder could never clear.
        dispatch_paused = bool(response.blocked_by_dispatch)
        outcome = (
            "pausing the dispatch schedule"
            if dispatch_paused
            else "leaving the dispatch schedule running -- the blocker was not dispatch"
        )
        note = (
            f"{response.detail} This is the {new_count}th consecutive deferral of the same "
            f"worker-revision-drift condition (bound: {MAX_CONSECUTIVE_DEFERRALS}); escalating "
            f"through the declared alert policy ({outcome}) rather than deferring silently "
            "forever."
        )
        if dispatch_paused:
            default_halt_dispatch(note)
        # PRIN-008: a problem must announce itself, not just repeat a halt nobody is told about
        # (f51e057c F1) -- pausing the schedule is the halt, not the announcement. Announced
        # either way: a streak left unpaused must say so just as loudly as one that paused.
        # `revision=status.worker_revision` keeps the alert's fingerprint stable across every
        # consecutive escalation of this same open condition (f51e057c F-D) -- the counter in
        # `note` above changes every tick, the revision does not until a worker restart
        # actually rewrites the record.
        failure_diagnosis.announce_worker_revision_drift_escalated(
            note, revision=status.worker_revision, dispatch_paused=dispatch_paused
        )
        return drift_response.DriftResponse(drift_response.ESCALATED, note)

    return response


def _maybe_resume_drift_pause() -> None:
    """Resume the drift-pause once the condition it exists to enforce has actually cleared.

    2026-09-13 (f51e057c F2): `_escalate_persistent_deferral` pauses dispatch through
    `schedule_runtime.pause_dispatch_schedule`. Pausing stops any dispatch run from being in
    flight, so the very next evaluation of this activity typically resolves the drift cleanly
    (RESTARTED or ADVANCED) -- or the condition is simply found clear -- but nothing ever resumed
    the schedule: a merely-busy queue could otherwise permanently stop the factory. Only a pause
    carrying this mechanism's own prefix (`drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX`)
    is ever touched -- see `drift_response.respond_to_cleared_drift`.

    Best-effort, matching every other alert/schedule side effect in this module: a resume check
    that cannot run (no Temporal reachable, config missing) must not crash a report that already
    landed and already resolved the underlying drift.
    """
    try:
        response = drift_response.respond_to_cleared_drift(
            dispatch_schedule_pause_state=default_dispatch_schedule_pause_state,
            resume_schedule=default_resume_dispatch,
        )
        if response.outcome == drift_response.PAUSE_RESUMED:
            logger.warning("worker-revision-drift: resumed dispatch schedule: %s", response.detail)
            # f51e057c F-E: an escalation that announces itself but never announces its own
            # resolution leaves an operator who saw the escalation alert with no signal that
            # dispatch is running again, short of checking by hand -- mirrors
            # `activities.capacity_pause_response.default_announce_resume`'s pairing of
            # resume_schedule with announce_resume for the sibling capacity pause.
            failure_diagnosis.announce_worker_revision_drift_escalated_resolved(response.detail)
    except Exception:  # noqa: BLE001 - a resume check must never crash a report that already landed.
        logger.exception(
            "could not check/resume the worker-revision-drift dispatch pause; the schedule's "
            "current state is unaffected"
        )


def _maybe_announce_unknown_while_paused(status: worker_revision.WorkerRevisionStatus) -> None:
    """PRIN-008: a pause this mechanism set must not sit silently once the condition it exists
    to enforce becomes unresolved (`unknown`) -- dev.finding 9a10f2aa.

    `report_worker_revision_drift_activity` resumes this mechanism's own pause in exactly two
    cases -- a drifted status resolved ADVANCED/RESTARTED, or the condition found CLEAR.
    `unknown` reaches neither branch and must never be made to: an indeterminate revision is never
    folded into clean (PRIN-015 fail closed; see `_condition_for` above). This never resumes --
    it only checks, read-only, whether the schedule is still paused with THIS mechanism's own
    note prefix (`drift_response.is_our_pause_note`, the same discrimination
    `_maybe_resume_drift_pause` relies on for the resume path) and, if so, announces that the
    pause cannot resolve on its own right now. An operator's manual pause, or the sibling
    capacity-backpressure pause, stays silent here exactly as it does for resume.

    Best-effort, matching every other schedule/alert side effect in this module: a pause-state
    check that cannot run must not crash a report that already landed.
    """
    try:
        paused, note = default_dispatch_schedule_pause_state()
    except Exception:  # noqa: BLE001 - a pause-state check must never crash a report that landed.
        logger.exception(
            "could not check the dispatch schedule pause state while worker-revision-drift is "
            "unknown; no alert raised this tick"
        )
        return
    if not paused or not drift_response.is_our_pause_note(note):
        return
    failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(note, error=status.error)


#: dev.finding 639a20c5 AC-3: this is the only caller that opts into the bounded wait in
#: `worker_checkout_drift_response.respond_to_worker_revision_drift`. 300s comfortably covers the
#: measured <=19s reconciler runtime with margin to spare, and stays >=120s below this activity's
#: `ACTIVITY_START_TO_CLOSE_TIMEOUT` (workflows/worker_revision_drift.py) -- pinned by
#: test_worker_revision_drift_schedule.py so a future change to either constant has to keep that
#: relationship deliberate.
WORKER_REVISION_DRIFT_WAIT_SECONDS = 300
#: How often the wait above re-checks `factory_activity_in_flight` while it waits.
WORKER_REVISION_DRIFT_POLL_INTERVAL_SECONDS = 15


def respond_to_drifted_status(status: worker_revision.WorkerRevisionStatus) -> drift_response.DriftResponse:
    """Wire real collaborators into `respond_to_worker_revision_drift` for a drifted status.

    Only called when `status.drifted` -- see that function's own docstring for why an
    `unknown` status is left untouched here.
    """
    checkout_root = worker_checkout.default_checkout_root()
    return drift_response.respond_to_worker_revision_drift(
        status,
        checkout_root=checkout_root,
        source_repo_root=default_source_repo_root(checkout_root),
        factory_activity_in_flight=default_factory_activity_in_flight,
        supervisor=default_supervisor(),
        halt_dispatch=default_halt_dispatch,
        wait_seconds=WORKER_REVISION_DRIFT_WAIT_SECONDS,
        poll_interval_seconds=WORKER_REVISION_DRIFT_POLL_INTERVAL_SECONDS,
    )


#: dev.finding 5170b3f9a: the three, and only three, outcomes `bring_base_ref_current` may
#: return -- mirrors every other closed outcome vocabulary in this area (`DRIFTED`/`UNKNOWN`/
#: `CLEAR` above, `drift_response`'s five-outcome set) rather than open-ended text a caller
#: would have to pattern-match prose to branch on.
SYNC_SKIPPED = "sync_skipped"
SYNC_CURRENT = "sync_current"
SYNC_REFUSED = "sync_refused"


@dataclass(frozen=True)
class BaseRefSyncResult:
    """What `bring_base_ref_current` did this tick. `revision` is set only on `SYNC_CURRENT`
    (the checkout's own local `main`, post fast-forward); `detail` carries the in-flight
    description or the refusal text on the other two outcomes, and is empty on `SYNC_CURRENT`."""

    outcome: str
    detail: str = ""
    revision: str | None = None


def bring_base_ref_current(
    *,
    factory_activity_in_flight: Callable[[], Any],
    ensure_base_ref_current: Callable[..., "dispatch.BaseRefStatus"],
    config_factory: Callable[[], "dispatch.Config"],
) -> BaseRefSyncResult:
    """Bring the worker checkout's local `main` current with GitHub before the drift tick
    compares against it -- the fix for dev.finding 5170b3f9a: before this, local `main` was
    moved only by a dispatch (`make_clone` -> `ensure_base_ref_current`) or a hand
    `scripts/factory-redeploy.py`, so an idle factory (an empty queue, or every claim refused)
    never picked up a merge until a person intervened.

    Checks whether anything else is in flight FIRST, for two reasons named in `.factory/design.md`:
    (1) `ensure_base_ref_current`'s own `_record_concurrent_clone_defer` counts consecutive
    refusals toward OPS-60's deadlock alert, and a drift tick observing a long-running dispatch
    must not feed that counter on the dispatch run's behalf by calling in anyway; (2) while
    something is in flight, local `main` is exactly as current as that run's own
    `ensure_base_ref_current` call already left it, so skipping buys nothing and only risks the
    exact race `_other_clone_in_flight` exists to prevent.

    PRIN-015 (fail closed): `SYNC_REFUSED` is a distinct outcome from `SYNC_SKIPPED`, never
    folded into it -- a refusal means the sync RAN and found the base ref stale, wrong, or
    blocked by a concurrent clone, which `report_worker_revision_drift_activity` must report as
    `unknown`, never silently re-use whatever the last successful comparison said. A sync that
    never ran at all (something in flight, or the in-flight check itself raised) is `SYNC_SKIPPED`
    instead: nothing was learned either way, so the existing unforced comparison against local
    `main` is left to proceed exactly as it did before this function existed.

    **Accepted cost** (named, not hidden): a `SYNC_REFUSED` caused by a concurrent clone still
    counts toward the OPS-60 streak via `ensure_base_ref_current`'s own bookkeeping, exactly as a
    dispatch tick's refusal does -- this function is a second caller of that same gate, not a new
    failure mode. And an attended `dispatch.py --task` run is visible to `_other_clone_in_flight`
    (a workdir-owner-marker check) but not to the Temporal `factory_activity_in_flight` check this
    function runs first, so an attended run longer than two drift ticks will trip the OPS-60
    "wedged" alert even though nothing is actually wedged -- deduplicated for 24h, and out of this
    bead's scope to fix.
    """
    try:
        in_flight = factory_activity_in_flight()
    except Exception as exc:  # noqa: BLE001 - a sync that cannot be proven safe is not run.
        logger.warning(
            "worker-revision-drift: skipping base-ref sync -- could not determine whether "
            "another factory activity is in flight: %s",
            exc,
        )
        return BaseRefSyncResult(SYNC_SKIPPED, detail=str(exc))

    if in_flight:
        description = getattr(in_flight, "description", "") or "a factory activity"
        return BaseRefSyncResult(SYNC_SKIPPED, detail=description)

    cfg = config_factory()
    try:
        status = ensure_base_ref_current(cfg, force_tracking_refresh=True)
    except (dispatch.DispatchEnvironmentError, dispatch.DispatchError) as exc:
        return BaseRefSyncResult(SYNC_REFUSED, detail=str(exc))
    return BaseRefSyncResult(SYNC_CURRENT, revision=status.local_rev)


def default_bring_base_ref_current() -> BaseRefSyncResult:
    """The one seam `report_worker_revision_drift_activity` calls -- wires the real Temporal
    in-flight check, the real `dispatch.ensure_base_ref_current` (forced), and the real
    household `dispatch.Config.from_env`. No other production code calls `bring_base_ref_current`
    directly."""
    return bring_base_ref_current(
        factory_activity_in_flight=default_factory_activity_in_flight,
        ensure_base_ref_current=dispatch.ensure_base_ref_current,
        config_factory=dispatch.Config.from_env,
    )


def _base_ref_for_sync_refusal() -> str:
    """The `main_ref` to name on a `SYNC_REFUSED` tick's `could_not_determine` status --
    `cfg.base_ref` when a `Config` can be built (the same one `default_bring_base_ref_current`
    itself built to run the sync that just refused), else `worker_revision.DEFAULT_MAIN_REF`.
    `BaseRefSyncResult` deliberately carries no `Config`/`base_ref` field of its own (AC-2's
    three-field shape), so this re-resolves it the same cheap, side-effect-free way rather than
    widening that result."""
    try:
        return dispatch.Config.from_env().base_ref
    except dispatch.MissingConfigError:
        return worker_revision.DEFAULT_MAIN_REF


@activity.defn(name="report_worker_revision_drift")
def report_worker_revision_drift_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    sync_result = default_bring_base_ref_current()
    store = default_store()

    if sync_result.outcome == SYNC_REFUSED:
        # AC-3: a refused sync is never clear -- describe_worker_revision_drift is not even
        # called, so there is no chance of it coincidentally reading the local main a refused
        # sync left untouched as though that were a confirmed comparison (the 22:07Z/22:22Z
        # defect this bead exists to close).
        status = worker_revision.WorkerRevisionStatus.could_not_determine(
            _base_ref_for_sync_refusal(), sync_result.detail
        )
    else:
        status = worker_revision.describe_worker_revision_drift()

    result = land_worker_revision_drift(store, status)
    result["base_ref_sync"] = {
        "outcome": sync_result.outcome,
        "detail": sync_result.detail,
        "revision": sync_result.revision,
    }

    if status.drifted:
        response = respond_to_drifted_status(status)
        response = _escalate_persistent_deferral(store, result["bead_id"], response, status)
        result["response_action"] = response.action
        result["response_detail"] = response.detail
        if response.action in (drift_response.ADVANCED, drift_response.RESTARTED):
            _maybe_resume_drift_pause()
    elif result["condition"] == CLEAR:
        _maybe_resume_drift_pause()
    elif result["condition"] == UNKNOWN:
        _maybe_announce_unknown_while_paused(status)

    return result


ACTIVITIES = [report_worker_revision_drift_activity]
