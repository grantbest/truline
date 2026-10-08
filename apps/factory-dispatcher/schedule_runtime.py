"""Temporal Schedule registration for unattended factory dispatch."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping

DISPATCH_WORKFLOW_ID_PREFIX = "factory-dispatcher-"
STALENESS_WORKFLOW_ID_PREFIX = "factory-verdict-staleness-"
EA_APPLY_WORKFLOW_ID_PREFIX = "factory-ea-apply-"
SCHEDULE_NOTE = "factory dispatcher unattended drain"
STALENESS_SCHEDULE_NOTE = "factory dispatcher nightly verdict staleness report"
EA_APPLY_SCHEDULE_NOTE = "factory dispatcher EA model applier (PC-ASR-007/AC-3)"
CAPACITY_PAUSE_NOTE_PREFIX = "factory dispatcher paused: capacity backpressure"
STALENESS_SCHEDULE_ID = "factory-verdict-staleness-nightly"
STALENESS_SCHEDULE_INTERVAL_SECONDS = 24 * 60 * 60
EA_APPLY_SCHEDULE_ID = "factory-ea-apply-15m"
#: Same cadence as the dispatcher's own drain (config.DEFAULT_DISPATCH_INTERVAL_SECONDS) --
#: a model change merged to main is reconciled about as promptly as a task is dispatched.
EA_APPLY_SCHEDULE_INTERVAL_SECONDS = 15 * 60
EA_OBSERVATION_WORKFLOW_ID_PREFIX = "factory-ea-observation-"
EA_OBSERVATION_SCHEDULE_NOTE = "factory dispatcher nightly EA live-model observation"
EA_OBSERVATION_SCHEDULE_ID = "factory-ea-observation-nightly"
EA_OBSERVATION_SCHEDULE_INTERVAL_SECONDS = 24 * 60 * 60
RELEASE_APPLY_WORKFLOW_ID_PREFIX = "factory-release-apply-"
RELEASE_APPLY_SCHEDULE_NOTE = "factory dispatcher release charter applier"
RELEASE_APPLY_SCHEDULE_ID = "factory-release-apply-15m"
#: Same cadence as the EA-model applier -- a release charter merged to main is
#: reconciled about as promptly as an EA model change is.
RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS
REQUIREMENTS_APPLY_WORKFLOW_ID_PREFIX = "factory-requirements-apply-"
REQUIREMENTS_APPLY_SCHEDULE_NOTE = (
    "factory dispatcher requirements-registry applier (PC-ASR-002/AC-2)"
)
REQUIREMENTS_APPLY_SCHEDULE_ID = "factory-requirements-apply-15m"
#: Same cadence as the EA-model and release-charter appliers -- a verdict merged to main
#: (a re-measured criterion, a new baseline) should reach release-status and the console
#: about as promptly as a charter change does; the PRIN-014 revision gate in the activity
#: makes the ~96 idle ticks a day one git-log and one observation lookup each.
REQUIREMENTS_APPLY_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS
RELEASE_STATUS_WORKFLOW_ID_PREFIX = "factory-release-status-"
RELEASE_STATUS_SCHEDULE_NOTE = "factory dispatcher nightly release status report"
RELEASE_STATUS_SCHEDULE_ID = "factory-release-status-nightly"
RELEASE_STATUS_SCHEDULE_INTERVAL_SECONDS = STALENESS_SCHEDULE_INTERVAL_SECONDS
WORKER_REVISION_DRIFT_WORKFLOW_ID_PREFIX = "factory-worker-revision-drift-"
WORKER_REVISION_DRIFT_SCHEDULE_NOTE = "factory dispatcher worker revision drift check"
WORKER_REVISION_DRIFT_SCHEDULE_ID = "factory-worker-revision-drift-15m"
#: Same cadence as the EA-model applier, not the nightly report family -- a worker running stale
#: code is a condition worth catching about as promptly as the dispatcher's own drain, not once a
#: day. The 2026-08-29 incident this schedule exists for ran undetected for 12h10m; a nightly check
#: would still have missed most of that window.
WORKER_REVISION_DRIFT_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS
#: dev.finding 639a20c5: every other 15-minute reconciler in this file fires on the exact same
#: tick (no offset), so the drift check's own in-flight guard
#: (`activities.worker_revision_drift.default_factory_activity_in_flight`) almost always finds a
#: same-tick sibling running and defers -- 83 of 97 deferrals in the 2026-09-30/10-01 incident
#: were blocked by a reconciler, not dispatch, and nothing about pausing dispatch harder could
#: ever clear that. Offsetting ONLY this schedule 7 minutes into its own 15-minute period gives it
#: a tick no other reconciler shares, without touching any other schedule's cadence. The exact
#: value is not load-bearing -- it only needs to land reliably off :00/:15/:30/:45 -- so the
#: midpoint of the period was chosen for comfortable clearance either side of scheduler jitter.
WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS = 7 * 60
DOCTRINE_STALENESS_WORKFLOW_ID_PREFIX = "factory-doctrine-staleness-"
DOCTRINE_STALENESS_SCHEDULE_NOTE = (
    "factory dispatcher nightly doctrine principle staleness report (F-DCE-5, PRIN-011)"
)
DOCTRINE_STALENESS_SCHEDULE_ID = "factory-doctrine-staleness-nightly"
DOCTRINE_STALENESS_SCHEDULE_INTERVAL_SECONDS = STALENESS_SCHEDULE_INTERVAL_SECONDS
CAPACITY_RESUME_PROBE_WORKFLOW_ID_PREFIX = "factory-capacity-resume-probe-"
CAPACITY_RESUME_PROBE_SCHEDULE_NOTE = "factory dispatcher capacity-pause resume prober"
CAPACITY_RESUME_PROBE_SCHEDULE_ID = "factory-capacity-resume-probe-15m"
#: Same cadence as the EA-model applier and the worker-revision-drift check, not a nightly
#: family: OPS-66's own framing is "every hour between window reset and a human noticing is
#: idle capacity paid for", so this schedule is never itself a target of capacity-pause backoff
#: and runs unconditionally at worker boot -- it must keep probing through the exact condition
#: it exists to detect.
CAPACITY_RESUME_PROBE_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS
CLUSTER_HEALTH_WORKFLOW_ID_PREFIX = "factory-cluster-health-"
CLUSTER_HEALTH_SCHEDULE_NOTE = "factory dispatcher cluster health check (OPS-8)"
CLUSTER_HEALTH_SCHEDULE_ID = "factory-cluster-health-15m"
#: Same cadence as the EA-model applier and the worker-revision-drift check, not the nightly
#: report family: the pod image-pull grace period this checker enforces defaults to ten minutes
#: (cluster_health.IMAGE_PULL_GRACE_SECONDS_DEFAULT), and the incident this schedule exists for --
#: pg-restore-drill silently failing since 2026-08-16, found by hand eight days later -- is
#: exactly the shape a once-daily check leaves open for up to 24h longer than it has to.
CLUSTER_HEALTH_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS
CHANGE_APPLY_WORKFLOW_ID_PREFIX = "factory-change-apply-"
CHANGE_APPLY_SCHEDULE_NOTE = "factory dispatcher arch.change merged-PR reconciler (R26.05/O-1)"
CHANGE_APPLY_SCHEDULE_ID = "factory-change-apply-15m"
#: Same short-cycle cadence as the EA-model and release-charter appliers -- itsm-target-state.md
#: §2 calls this reconciler "short-cycle", in the same family as arch.release's own 15-minute
#: idempotent reconciler.
CHANGE_APPLY_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS

#: Every NON-dispatch schedule this worker registers, in one place: label -> (env override
#: name, default schedule id). `worker.py`'s own registration calls in `main()` resolve every
#: non-dispatch schedule id from `non_dispatch_schedule_ids()` below, which reads this same
#: dict -- so there is exactly one list of "what this worker registers", not two that can drift
#: apart. That single list is also what
#: `activities/worker_revision_drift.py::default_factory_activity_in_flight` walks to decide
#: whether it is safe to advance the shared worker checkout: OPS-99 raised
#: `worker.ACTIVITY_EXECUTOR_CONCURRENCY` from 1 to 12 so these schedules can run concurrently
#: with each other and with dispatch, and a checkout-mutating guard that only ever asked "is
#: dispatch running" stopped being sufficient the moment that happened. Adding a twelfth
#: non-dispatch schedule is one line here; both the registration and the guard pick it up
#: without a second edit. (The dispatch schedule itself is not in this dict -- it is
#: `config.DISPATCH_SCHEDULE_ID`, resolved independently, since it predates this registry and
#: is not "non-dispatch".)
NON_DISPATCH_SCHEDULE_ENV_DEFAULTS: dict[str, tuple[str, str]] = {
    "staleness": ("FACTORY_STALENESS_SCHEDULE_ID", STALENESS_SCHEDULE_ID),
    "ea_apply": ("FACTORY_EA_APPLY_SCHEDULE_ID", EA_APPLY_SCHEDULE_ID),
    "ea_observation": ("FACTORY_EA_OBSERVATION_SCHEDULE_ID", EA_OBSERVATION_SCHEDULE_ID),
    "release_apply": ("FACTORY_RELEASE_APPLY_SCHEDULE_ID", RELEASE_APPLY_SCHEDULE_ID),
    "release_status": ("FACTORY_RELEASE_STATUS_SCHEDULE_ID", RELEASE_STATUS_SCHEDULE_ID),
    "requirements_apply": (
        "FACTORY_REQUIREMENTS_APPLY_SCHEDULE_ID",
        REQUIREMENTS_APPLY_SCHEDULE_ID,
    ),
    "doctrine_staleness": (
        "FACTORY_DOCTRINE_STALENESS_SCHEDULE_ID",
        DOCTRINE_STALENESS_SCHEDULE_ID,
    ),
    "worker_revision_drift": (
        "FACTORY_WORKER_REVISION_DRIFT_SCHEDULE_ID",
        WORKER_REVISION_DRIFT_SCHEDULE_ID,
    ),
    "capacity_resume_probe": (
        "FACTORY_CAPACITY_RESUME_PROBE_SCHEDULE_ID",
        CAPACITY_RESUME_PROBE_SCHEDULE_ID,
    ),
    "cluster_health": ("FACTORY_CLUSTER_HEALTH_SCHEDULE_ID", CLUSTER_HEALTH_SCHEDULE_ID),
    "change_apply": ("FACTORY_CHANGE_APPLY_SCHEDULE_ID", CHANGE_APPLY_SCHEDULE_ID),
}


def non_dispatch_schedule_ids(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """{label: schedule_id} for every non-dispatch schedule this worker registers.

    Resolved against env overrides exactly as `worker.py`'s registration calls resolve them
    (`os.environ.get(env_name, default)`) -- from the one dict both read
    (`NON_DISPATCH_SCHEDULE_ENV_DEFAULTS`), so this can never silently fall out of sync with
    what `worker.main()` actually registers.
    """
    values = os.environ if environ is None else environ
    return {
        label: values.get(env_name, default)
        for label, (env_name, default) in NON_DISPATCH_SCHEDULE_ENV_DEFAULTS.items()
    }


@dataclass(frozen=True)
class ScheduleRegistration:
    schedule_id: str
    interval_seconds: int
    overlap_policy: Any
    paused: bool
    action: str
    task_queue: str
    operation: str


@dataclass(frozen=True)
class InFlightWorkflow:
    workflow_id: str
    run_id: str
    scheduled_at: str = ""
    started_at: str = ""


#: Workflow execution statuses that mean the drain did not complete its work.
#: RUNNING and CONTINUED_AS_NEW are neither failures nor successes — a drain
#: still in progress must not be counted as either. Unknown is different: a
#: scheduled drain with no determinable terminal outcome is not evidence of
#: health, so it is reported as failed.
FAILED_STATUSES = frozenset({"FAILED", "TIMED_OUT", "TERMINATED", "CANCELED"})
SUCCEEDED_STATUSES = frozenset({"COMPLETED"})
IN_PROGRESS_STATUSES = frozenset({"RUNNING", "CONTINUED_AS_NEW"})


@dataclass(frozen=True)
class DrainOutcome:
    """How one scheduled firing actually ended.

    The schedule itself does not know this. `ScheduleActionResult` records
    that a workflow was *started*, not whether it worked — which is precisely
    why the 2026-08-06 outage was invisible from the schedule alone.
    """

    workflow_id: str
    scheduled_at: str = ""
    status: str = ""

    @property
    def failed(self) -> bool:
        status = self.status.upper()
        return status in FAILED_STATUSES or status not in (
            SUCCEEDED_STATUSES | IN_PROGRESS_STATUSES
        )

    @property
    def succeeded(self) -> bool:
        return self.status.upper() in SUCCEEDED_STATUSES


@dataclass(frozen=True)
class FactoryScheduleStatus:
    schedule_id: str
    paused: bool
    in_flight: tuple[InFlightWorkflow, ...]
    #: Most recent firing last, matching Temporal's own ordering.
    recent: tuple[DrainOutcome, ...] = ()

    @property
    def genuinely_quiet(self) -> bool:
        # Unchanged, and deliberately so: this drives the exit code, and
        # "quiet" has always meant paused with nothing in flight. Whether the
        # drains that did run worked is a different question, answered below.
        return self.paused and not self.in_flight

    @property
    def recent_failures(self) -> int:
        return sum(1 for outcome in self.recent if outcome.failed)

    @property
    def consecutive_failures(self) -> int:
        """Failures at the end of recent history, newest first, until one isn't.

        Trailing rather than total, so a single failure a week ago followed by
        healthy drains does not read as an outage. The 2026-08-06 signature is
        five in a row.
        """
        count = 0
        for outcome in reversed(self.recent):
            if not outcome.failed:
                break
            count += 1
        return count

    @property
    def last_success_at(self) -> str:
        for outcome in reversed(self.recent):
            if outcome.succeeded:
                return outcome.scheduled_at or "(no scheduled time recorded)"
        return ""

    @property
    def drains_failing(self) -> bool:
        # Deliberately kept as "at least one trailing failure", not "every
        # recent drain failed": three call sites read this boolean only to
        # decide whether a trailing failure is worth mentioning at all
        # (schedule_runtime.py's "Recent drains are completing" gate and
        # _drain_lines' early-return, and schedule_status.py's rendered-line
        # lookup for its insert position). None of them need the *severity*
        # distinction between one bad drain and a total outage -- that
        # distinction is made separately, inside _drain_lines, from
        # consecutive_failures and len(recent) directly.
        return self.consecutive_failures > 0


def build_dispatch_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    interval_seconds: int,
    task_queue: str,
    paused: bool = False,
    note: str | None = SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that starts one dispatcher workflow pass."""
    if interval_seconds <= 0:
        raise ValueError("dispatch schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=DISPATCH_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


def build_staleness_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    paused: bool = False,
    note: str | None = STALENESS_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the nightly Temporal Schedule for verdict staleness reporting."""
    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=STALENESS_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=STALENESS_SCHEDULE_INTERVAL_SECONDS),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


def _interval_seconds(schedule: Any) -> int | None:
    intervals = list(getattr(getattr(schedule, "spec", None), "intervals", []) or [])
    if len(intervals) != 1:
        return None
    every = getattr(intervals[0], "every", None)
    if every is None:
        return None
    return int(every.total_seconds())


def _interval_offset_seconds(schedule: Any) -> int:
    """0 for no offset (the ordinary case for every schedule but the worker-revision-drift one),
    never `None` -- so an offset-less existing schedule and a `desired` built with no offset at
    all compare equal, and only a genuine mismatch (e.g. an existing drift schedule registered
    before `WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS` existed) is ever flagged."""
    intervals = list(getattr(getattr(schedule, "spec", None), "intervals", []) or [])
    if len(intervals) != 1:
        return 0
    offset = getattr(intervals[0], "offset", None)
    return int(offset.total_seconds()) if offset is not None else 0


def _action_summary(schedule: Any) -> tuple[Any, Any, Any, Any]:
    action = getattr(schedule, "action", None)
    return (
        getattr(action, "workflow", None),
        getattr(action, "id", None),
        getattr(action, "task_queue", None),
        getattr(action, "retry_policy", None),
    )


def schedule_needs_update(existing: Any, desired: Any) -> bool:
    """Return true when the durable schedule should be updated in place."""
    return (
        _interval_seconds(existing) != _interval_seconds(desired)
        or _interval_offset_seconds(existing) != _interval_offset_seconds(desired)
        or getattr(getattr(existing, "policy", None), "overlap", None)
        != getattr(getattr(desired, "policy", None), "overlap", None)
        or _action_summary(existing) != _action_summary(desired)
    )


async def register_dispatch_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str,
    interval_seconds: int,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the dispatcher schedule without duplicating it."""
    desired = build_dispatch_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or SCHEDULE_NOTE
    desired = build_dispatch_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_dispatch_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


async def register_staleness_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = STALENESS_SCHEDULE_ID,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the nightly staleness schedule without duplicating it."""
    desired = build_staleness_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or STALENESS_SCHEDULE_NOTE
    desired = build_staleness_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_staleness_schedule(
                temporal,
                workflow_run,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_ea_apply_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = EA_APPLY_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = EA_APPLY_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the in-cluster EA-model applier."""
    if interval_seconds <= 0:
        raise ValueError("EA apply schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=EA_APPLY_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_ea_apply_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = EA_APPLY_SCHEDULE_ID,
    interval_seconds: int = EA_APPLY_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the EA-apply schedule without duplicating it."""
    desired = build_ea_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or EA_APPLY_SCHEDULE_NOTE
    desired = build_ea_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_ea_apply_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_ea_observation_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    paused: bool = False,
    note: str | None = EA_OBSERVATION_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the nightly Temporal Schedule for the EA live-model observation run."""
    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=EA_OBSERVATION_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=EA_OBSERVATION_SCHEDULE_INTERVAL_SECONDS),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_ea_observation_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = EA_OBSERVATION_SCHEDULE_ID,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the nightly EA observation schedule without duplicating it."""
    desired = build_ea_observation_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or EA_OBSERVATION_SCHEDULE_NOTE
    desired = build_ea_observation_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_ea_observation_schedule(
                temporal,
                workflow_run,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_release_apply_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = RELEASE_APPLY_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the in-cluster release-charter applier."""
    if interval_seconds <= 0:
        raise ValueError("release apply schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=RELEASE_APPLY_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_release_apply_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = RELEASE_APPLY_SCHEDULE_ID,
    interval_seconds: int = RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the release-apply schedule without duplicating it."""
    desired = build_release_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or RELEASE_APPLY_SCHEDULE_NOTE
    desired = build_release_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_release_apply_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_requirements_apply_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = REQUIREMENTS_APPLY_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = REQUIREMENTS_APPLY_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the in-cluster requirements-registry applier."""
    if interval_seconds <= 0:
        raise ValueError("requirements apply schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=REQUIREMENTS_APPLY_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_requirements_apply_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = REQUIREMENTS_APPLY_SCHEDULE_ID,
    interval_seconds: int = REQUIREMENTS_APPLY_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the requirements-apply schedule without duplicating it."""
    desired = build_requirements_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or REQUIREMENTS_APPLY_SCHEDULE_NOTE
    desired = build_requirements_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_requirements_apply_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_release_status_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    paused: bool = False,
    note: str | None = RELEASE_STATUS_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the nightly Temporal Schedule for the release status report."""
    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=RELEASE_STATUS_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=RELEASE_STATUS_SCHEDULE_INTERVAL_SECONDS),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_release_status_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = RELEASE_STATUS_SCHEDULE_ID,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the nightly release-status schedule without duplicating it."""
    desired = build_release_status_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or RELEASE_STATUS_SCHEDULE_NOTE
    desired = build_release_status_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_release_status_schedule(
                temporal,
                workflow_run,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_doctrine_staleness_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    paused: bool = False,
    note: str | None = DOCTRINE_STALENESS_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the nightly Temporal Schedule for the doctrine staleness report."""
    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=DOCTRINE_STALENESS_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=DOCTRINE_STALENESS_SCHEDULE_INTERVAL_SECONDS),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_doctrine_staleness_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = DOCTRINE_STALENESS_SCHEDULE_ID,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the nightly doctrine-staleness schedule without duplicating it."""
    desired = build_doctrine_staleness_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or DOCTRINE_STALENESS_SCHEDULE_NOTE
    desired = build_doctrine_staleness_schedule(
        temporal,
        workflow_run,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_doctrine_staleness_schedule(
                temporal,
                workflow_run,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_worker_revision_drift_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = WORKER_REVISION_DRIFT_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = WORKER_REVISION_DRIFT_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the worker-revision-drift check.

    The one schedule in this file whose `ScheduleIntervalSpec` carries an `offset` -- see
    `WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS`'s own docstring for why.
    """
    if interval_seconds <= 0:
        raise ValueError("worker revision drift schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=WORKER_REVISION_DRIFT_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                    offset=timedelta(seconds=WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_worker_revision_drift_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = WORKER_REVISION_DRIFT_SCHEDULE_ID,
    interval_seconds: int = WORKER_REVISION_DRIFT_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the worker-revision-drift schedule without duplicating it."""
    desired = build_worker_revision_drift_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or WORKER_REVISION_DRIFT_SCHEDULE_NOTE
    desired = build_worker_revision_drift_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_worker_revision_drift_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_capacity_resume_probe_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = CAPACITY_RESUME_PROBE_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = CAPACITY_RESUME_PROBE_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the capacity-pause resume probe (OPS-66)."""
    if interval_seconds <= 0:
        raise ValueError("capacity resume probe schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=CAPACITY_RESUME_PROBE_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_capacity_resume_probe_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = CAPACITY_RESUME_PROBE_SCHEDULE_ID,
    interval_seconds: int = CAPACITY_RESUME_PROBE_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the capacity-resume-probe schedule without duplicating it.

    Deliberately never pauses itself in response to capacity backpressure -- it is the one
    schedule that must keep running through the exact condition it exists to detect.
    """
    desired = build_capacity_resume_probe_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or CAPACITY_RESUME_PROBE_SCHEDULE_NOTE
    desired = build_capacity_resume_probe_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_capacity_resume_probe_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_cluster_health_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = CLUSTER_HEALTH_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = CLUSTER_HEALTH_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the cluster health check (OPS-8)."""
    if interval_seconds <= 0:
        raise ValueError("cluster health schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=CLUSTER_HEALTH_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_cluster_health_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = CLUSTER_HEALTH_SCHEDULE_ID,
    interval_seconds: int = CLUSTER_HEALTH_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the cluster-health schedule without duplicating it."""
    desired = build_cluster_health_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or CLUSTER_HEALTH_SCHEDULE_NOTE
    desired = build_cluster_health_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_cluster_health_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def build_change_apply_schedule(
    temporal: Any,
    workflow_run: Any,
    *,
    task_queue: str,
    interval_seconds: int = CHANGE_APPLY_SCHEDULE_INTERVAL_SECONDS,
    paused: bool = False,
    note: str | None = CHANGE_APPLY_SCHEDULE_NOTE,
    retry_policy: Any = None,
) -> Any:
    """Build the Temporal Schedule that drives the arch.change merged-PR reconciler."""
    if interval_seconds <= 0:
        raise ValueError("change apply schedule interval must be positive")

    return temporal.Schedule(
        action=temporal.ScheduleActionStartWorkflow(
            workflow_run,
            {},
            id=CHANGE_APPLY_WORKFLOW_ID_PREFIX,
            task_queue=task_queue,
            retry_policy=retry_policy,
        ),
        spec=temporal.ScheduleSpec(
            intervals=[
                temporal.ScheduleIntervalSpec(
                    every=timedelta(seconds=interval_seconds),
                )
            ],
        ),
        policy=temporal.SchedulePolicy(overlap=temporal.ScheduleOverlapPolicy.SKIP),
        state=temporal.ScheduleState(note=note, paused=paused),
    )


async def register_change_apply_schedule(
    client: Any,
    temporal: Any,
    workflow_run: Any,
    *,
    schedule_id: str = CHANGE_APPLY_SCHEDULE_ID,
    interval_seconds: int = CHANGE_APPLY_SCHEDULE_INTERVAL_SECONDS,
    task_queue: str,
    logger: logging.Logger,
    retry_policy: Any = None,
) -> ScheduleRegistration:
    """Create or update the change-apply schedule without duplicating it."""
    desired = build_change_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        retry_policy=retry_policy,
    )
    handle = client.get_schedule_handle(schedule_id)

    try:
        description = await handle.describe()
    except Exception:
        logger.info("Creating Temporal Schedule %s.", schedule_id)
        await client.create_schedule(schedule_id, desired)
        return _registration(schedule_id, desired, "created")

    existing = description.schedule
    state = getattr(existing, "state", None)
    paused = bool(getattr(state, "paused", False))
    note = getattr(state, "note", None) or CHANGE_APPLY_SCHEDULE_NOTE
    desired = build_change_apply_schedule(
        temporal,
        workflow_run,
        interval_seconds=interval_seconds,
        task_queue=task_queue,
        paused=paused,
        note=note,
        retry_policy=retry_policy,
    )

    if schedule_needs_update(existing, desired):
        logger.info("Updating Temporal Schedule %s in place.", schedule_id)

        def updater(update_input: Any) -> Any:
            current = update_input.description.schedule
            current_state = getattr(current, "state", None)
            updated = build_change_apply_schedule(
                temporal,
                workflow_run,
                interval_seconds=interval_seconds,
                task_queue=task_queue,
                paused=bool(getattr(current_state, "paused", paused)),
                note=getattr(current_state, "note", None) or note,
                retry_policy=retry_policy,
            )
            return temporal.ScheduleUpdate(schedule=updated)

        await handle.update(updater)
        return _registration(schedule_id, desired, "updated")

    logger.info("Temporal Schedule %s already up to date.", schedule_id)
    return _registration(schedule_id, desired, "unchanged")


def capacity_pause_note(reason: str) -> str:
    return f"{CAPACITY_PAUSE_NOTE_PREFIX}; {reason}"


async def _set_dispatch_schedule_pause(
    client: Any,
    temporal: Any,
    *,
    schedule_id: str,
    note: str,
    paused: bool,
    logger: logging.Logger | None = None,
) -> None:
    """Pause or resume the durable dispatcher schedule, preserving its action/spec.

    Shared by `pause_dispatch_schedule` and `resume_dispatch_schedule`, which are otherwise the
    same read-modify-write dance with `paused` flipped -- one implementation, so a future change
    to how a Schedule's state is safely replaced (the `replace`-then-fallback below exists because
    some Schedule stand-ins are frozen dataclasses and some are not) only has to be made once.
    """
    handle = client.get_schedule_handle(schedule_id)

    def updater(update_input: Any) -> Any:
        current = update_input.description.schedule
        state = temporal.ScheduleState(note=note, paused=paused)
        try:
            updated = replace(current, state=state)
        except TypeError:
            current.state = state
            updated = current
        return temporal.ScheduleUpdate(schedule=updated)

    await handle.update(updater)
    if logger:
        verb = "Paused" if paused else "Resumed"
        logger.warning("%s Temporal Schedule %s: %s", verb, schedule_id, note)


async def pause_dispatch_schedule(
    client: Any,
    temporal: Any,
    *,
    schedule_id: str,
    note: str,
    logger: logging.Logger | None = None,
) -> None:
    """Pause the durable dispatcher schedule, preserving the schedule action/spec."""
    await _set_dispatch_schedule_pause(
        client, temporal, schedule_id=schedule_id, note=note, paused=True, logger=logger
    )


async def resume_dispatch_schedule(
    client: Any,
    temporal: Any,
    *,
    schedule_id: str,
    note: str,
    logger: logging.Logger | None = None,
) -> None:
    """Resume a paused durable dispatcher schedule, preserving the schedule action/spec.

    The mirror image of `pause_dispatch_schedule` -- see `capacity_pause_response.py` for the
    decision logic that decides when a resume is warranted, and
    `activities/capacity_pause_response.py` for the real Temporal wiring around it.
    """
    await _set_dispatch_schedule_pause(
        client, temporal, schedule_id=schedule_id, note=note, paused=False, logger=logger
    )


async def dispatch_schedule_pause_state(client: Any, *, schedule_id: str) -> tuple[bool, str]:
    """Return `(paused, note)` for `schedule_id` -- nothing else.

    The capacity-resume prober only needs these two facts, not the in-flight/drain-history
    machinery `describe_factory_schedule_status` pays for on every call.
    """
    description = await client.get_schedule_handle(schedule_id).describe()
    state = getattr(getattr(description, "schedule", None), "state", None)
    paused = bool(getattr(state, "paused", False))
    note = str(getattr(state, "note", None) or "")
    return paused, note


async def workflow_execution_status(
    client: Any,
    workflow_id: str,
    run_id: str | None = None,
) -> str:
    """Ask Temporal how a workflow ended; "" when it cannot say.

    `run_id` is optional by design. Scheduled actions record the first run that
    started, but a Temporal RetryPolicy can create a later run under the same
    workflow id. Omitting run_id asks Temporal for the current/latest run, which
    is the action outcome operators care about.
    """
    try:
        handle = client.get_workflow_handle(workflow_id, run_id=run_id or None)
        description = await handle.describe()
    except Exception:
        # Unknown stays unknown here; DrainOutcome treats it as a failure
        # because silence is not evidence that a scheduled drain completed.
        return ""
    status = getattr(description, "status", None)
    return str(getattr(status, "name", status) or "")


async def describe_factory_schedule_status(
    client: Any,
    *,
    schedule_id: str,
    status_lookup: Any = workflow_execution_status,
) -> FactoryScheduleStatus:
    """Return pause state, running workflows, and how recent drains ended.

    `status_lookup` is injected so this is testable without a Temporal server:
    the schedule description says only that a workflow was started, so the
    outcome has to be read from the execution itself.
    """
    description = await client.get_schedule_handle(schedule_id).describe()
    paused = bool(
        getattr(getattr(getattr(description, "schedule", None), "state", None), "paused", False)
    )
    info = getattr(description, "info", None)
    running_actions = getattr(info, "running_actions", ()) or ()
    recent_actions = getattr(info, "recent_actions", ()) or ()

    recent = []
    for action in recent_actions:
        workflow = _in_flight_workflow(action)
        recent.append(
            DrainOutcome(
                workflow_id=workflow.workflow_id,
                scheduled_at=_time_attr(action, "scheduled_at"),
                status=await status_lookup(client, workflow.workflow_id, ""),
            )
        )

    return FactoryScheduleStatus(
        schedule_id=schedule_id,
        paused=paused,
        in_flight=tuple(_in_flight_workflow(action) for action in running_actions),
        recent=tuple(recent),
    )


def render_factory_schedule_status(
    status: FactoryScheduleStatus,
    *,
    namespace: str,
    address: str = "$TEMPORAL_URL",
    worker_command: str = "python worker.py",
) -> str:
    """Render operator-facing schedule status without implying pause means stop."""
    lines = [
        "Factory schedule status",
        f"schedule_id: {status.schedule_id}",
        f"namespace: {namespace}",
        f"paused: {str(status.paused).lower()}",
        f"in_flight_workflows: {len(status.in_flight)}",
        f"genuinely_quiet: {str(status.genuinely_quiet).lower()}",
        f"recent_drains: {len(status.recent)}",
        f"recent_failures: {status.recent_failures}",
        f"consecutive_failures: {status.consecutive_failures}",
        f"last_success: {status.last_success_at or '(none in recent history)'}",
        f"drains_failing: {str(status.drains_failing).lower()}",
    ]

    if status.in_flight:
        lines.extend(
            [
                "",
                (
                    "PAUSE DOES NOT STOP RUNNING WORKFLOWS: pause governs future "
                    "schedule firings only. These executions can still claim work "
                    "when a worker starts:"
                ),
            ]
        )
        for workflow in status.in_flight:
            detail = f"- workflow_id={workflow.workflow_id}"
            if workflow.run_id:
                detail += f" run_id={workflow.run_id}"
            if workflow.scheduled_at:
                detail += f" scheduled_at={workflow.scheduled_at}"
            if workflow.started_at:
                detail += f" started_at={workflow.started_at}"
            lines.append(detail)
        lines.extend(
            [
                "",
                (
                    "Drain by leaving the worker running until this status reports "
                    "in_flight_workflows: 0:"
                ),
                f"  {worker_command}",
                "",
                "Terminate in-flight executions with:",
            ]
        )
        for workflow in status.in_flight:
            command = (
                f'temporal workflow terminate --address "{address}" --namespace {namespace} '
                f'--workflow-id "{workflow.workflow_id}"'
            )
            if workflow.run_id:
                command += f' --run-id "{workflow.run_id}"'
            command += ' --reason "stop factory dispatcher in-flight execution"'
            lines.append(f"  {command}")
    elif status.paused:
        lines.extend(
            [
                "",
                (
                    "Factory is genuinely quiet: schedule is paused and no "
                    "in-flight workflows were reported."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Factory is not quiet: schedule is unpaused and can start future workflows.",
            ]
        )
        # An unpaused schedule says the factory CAN start work, never that the
        # work succeeds. On 2026-08-06 that distinction was the whole outage.
        if not status.recent:
            lines.append(
                "Whether those workflows succeed is not known from the schedule "
                "alone, and no recent drain outcomes were available."
            )
        elif not status.drains_failing:
            lines.append(
                f"Recent drains are completing (last success {status.last_success_at})."
            )

    # Last, deliberately. The operator reads the final line as the verdict, and
    # on 2026-08-06 the final line was "schedule is unpaused and can start
    # future workflows" while every firing was dying in four seconds.
    lines.extend(_drain_lines(status))

    return "\n".join(lines)


def _drain_lines(status: FactoryScheduleStatus) -> list[str]:
    """Render the drain history, which is what pause state cannot tell you."""
    if not status.recent:
        return [
            "",
            (
                "No recent drain outcomes are available. This is not the same as "
                "healthy: the schedule may never have fired, or the executions "
                "may have aged out of retention."
            ),
        ]

    if not status.drains_failing:
        return []

    # "Every attempt is dying" is only true when every drain in the observed
    # window failed -- that is the 2026-08-06 outage signature (five-for-five)
    # this banner exists to catch. A trailing failure inside an otherwise
    # healthy window is the ordinary case the retry machinery handles, and
    # saying the outage sentence for it teaches operators to discount the
    # sentence for the case that matters. drains_failing itself is unchanged
    # (see its docstring): it still just gates "mention this at all".
    all_recent_failed = status.consecutive_failures == len(status.recent)

    if all_recent_failed:
        headline = (
            f"FACTORY IS FAILING, NOT IDLE: the last {status.consecutive_failures} "
            f"scheduled drain(s) failed. A quiet board here means every attempt is "
            f"dying, not that there is no work."
        )
    else:
        # GATE FINDING 1 (#932): NO CATEGORICAL DENIAL OF THE OUTAGE HERE.
        # This branch is reached at EVERY ratio short of the whole window, so a
        # total outage passes through it nine times on its way up: at a 15-minute
        # interval (config.DEFAULT_DISPATCH_INTERVAL_SECONDS) over a 10-deep
        # window, a clause saying "this is not the 2026-08-06 outage shape" would
        # deny a real outage for its first ~2h15m -- nine firings of the banner
        # arguing against the thing it exists to announce.
        #
        # It would also have quietly undone a prior remediation:
        # tests/test_schedule_drain_health.py asserts that the reassurance
        # "Recent drains are completing" cannot stand during an outage, and a
        # differently-cased copy of that meaning inside the headline slips past
        # that guard twice -- the substring differs, and the guard's fixture is
        # all-failing, where this branch never executes.
        #
        # The ratio alone is what AC-1 asked for, and it is accurate at every
        # value. Severity is left to the reader, who can see 9 of 10.
        headline = (
            f"{status.recent_failures} of the last {len(status.recent)} scheduled "
            f"drain(s) failed ({status.consecutive_failures} most recent in a row)."
        )

    lines = [
        "",
        headline,
        (
            f"last_success: {status.last_success_at}"
            if status.last_success_at
            else "No drain succeeded in the recent history available."
        ),
        "",
        "Failed firings:",
    ]
    for outcome in status.recent:
        if not outcome.failed:
            continue
        detail = f"- workflow_id={outcome.workflow_id} status={outcome.status}"
        if outcome.scheduled_at:
            detail += f" scheduled_at={outcome.scheduled_at}"
        lines.append(detail)
    lines.extend(
        [
            "",
            (
                "Read the failure with: temporal workflow show --workflow-id "
                "<id>. A worker started without required configuration fails "
                "every firing in seconds while still registering as a poller."
            ),
        ]
    )
    return lines


def log_startup_mode(
    logger: logging.Logger,
    *,
    namespace: str,
    registration: ScheduleRegistration,
) -> None:
    """Log the schedule mode at warning level so paused/stale state is visible."""
    logger.warning(
        "FACTORY DISPATCHER SCHEDULE: namespace=%s schedule_id=%s interval=%ss "
        "overlap=%s paused=%s action=%s task_queue=%s operation=%s",
        namespace,
        registration.schedule_id,
        registration.interval_seconds,
        _policy_name(registration.overlap_policy),
        registration.paused,
        registration.action,
        registration.task_queue,
        registration.operation,
    )


def _registration(schedule_id: str, schedule: Any, operation: str) -> ScheduleRegistration:
    action = getattr(schedule, "action", None)
    return ScheduleRegistration(
        schedule_id=schedule_id,
        interval_seconds=_interval_seconds(schedule) or 0,
        overlap_policy=getattr(getattr(schedule, "policy", None), "overlap", None),
        paused=bool(getattr(getattr(schedule, "state", None), "paused", False)),
        action=str(getattr(action, "workflow", "")),
        task_queue=str(getattr(action, "task_queue", "")),
        operation=operation,
    )


def _policy_name(policy: Any) -> str:
    return str(getattr(policy, "name", policy))


#: What `_in_flight_workflow` reports when Temporal describes a running scheduled action with no
#: readable `workflow_id` on either the action or its nested action detail. Named so a caller that
#: must tell "this activity's own run" apart from "some other activity" -- see
#: `activities/worker_revision_drift.py::_factory_activity_in_flight_async` -- can recognize the
#: sentinel by symbol rather than a hand-typed copy of the literal that could silently drift from
#: this one.
UNKNOWN_WORKFLOW_ID = "(unknown workflow id)"


def _in_flight_workflow(action: Any) -> InFlightWorkflow:
    action_detail = getattr(action, "action", action)
    workflow_id = (
        _string_attr(action, "workflow_id")
        or _string_attr(action_detail, "workflow_id")
        or UNKNOWN_WORKFLOW_ID
    )
    run_id = (
        _string_attr(action, "first_execution_run_id")
        or _string_attr(action_detail, "first_execution_run_id")
        or _string_attr(action, "run_id")
        or _string_attr(action_detail, "run_id")
        or _string_attr(action, "workflow_run_id")
        or _string_attr(action_detail, "workflow_run_id")
    )
    return InFlightWorkflow(
        workflow_id=workflow_id,
        run_id=run_id,
        scheduled_at=_time_attr(action, "scheduled_at"),
        started_at=_time_attr(action, "started_at"),
    )


def _string_attr(value: Any, name: str) -> str:
    attr = getattr(value, name, "")
    return str(attr) if attr else ""


def _time_attr(value: Any, name: str) -> str:
    attr = getattr(value, name, "")
    if not attr:
        return ""
    isoformat = getattr(attr, "isoformat", None)
    return isoformat() if callable(isoformat) else str(attr)
