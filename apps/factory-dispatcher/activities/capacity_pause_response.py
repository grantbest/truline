"""Scheduled capacity-pause resume probe -- the missing resume half (OPS-66).

`dispatch_steps.py` already pauses the dispatch schedule with `schedule_runtime.
CAPACITY_PAUSE_NOTE_PREFIX` and raises a declared alert the moment capacity backpressure
exhausts the usage window. Nothing ever resumed it except a person, on their own schedule,
guessing at whether the window had reset: the live schedule note as of this writing was written
by hand ("Unpaused by outer loop at the Operator's direction 2026-09-03: probing whether the usage
window reset"). This activity is what makes the resume evaluation happen on a schedule
(`workflows/capacity_pause_response.py`, `schedule_runtime.py`), the same way worker-revision
drift does for the checkout.

Two concerns are split here, deliberately. Whether the dispatch schedule is even paused, and by
whom, is read first (`schedule_runtime.dispatch_schedule_pause_state`) -- an unpaused schedule has
nothing to probe, which is a wiring-level no-op, not one of `capacity_pause_response.py`'s three
declared outcomes. Only a paused schedule's note reaches
`capacity_pause_response.respond_to_capacity_pause`, which is the one place that decides RESUMED,
STILL_EXHAUSTED, or NOT_OURS -- see that module's docstring and `.factory/design.md` for why
"demonstrably reset" means a real, minimal live-worker attempt classified through the exact
detection path (`dispatch.detect_worker_capacity_failure`) production dispatch already uses,
never a parsed guess at the worker's own free-text `retry_at` clock.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from temporalio import activity
from temporalio.client import Client, ScheduleState, ScheduleUpdate

import capacity_pause_response as pause_response
import dispatch
import failure_diagnosis
import schedule_runtime
from config import config

_LOG = logging.getLogger("factory-dispatcher.capacity-pause-response")

#: `resume_dispatch_schedule`'s `temporal` param wants exactly these two names -- matching
#: `activities/dispatch_steps.py::TEMPORAL_PAUSE_TYPES` and
#: `activities/worker_revision_drift.py::_TEMPORAL_PAUSE_TYPES`, defined independently rather than
#: imported so this file's Temporal wiring does not depend on another activity module's internals.
_TEMPORAL_PAUSE_TYPES = SimpleNamespace(
    ScheduleState=ScheduleState,
    ScheduleUpdate=ScheduleUpdate,
)

#: A trivial, read-only prompt: the probe exists to observe whether the worker CLI still reports
#: capacity exhaustion, never to have it do task work. No file access, no commands -- a disposable
#: temp directory backs the invocation and is removed unconditionally afterward.
PROBE_PROMPT = (
    "Reply with exactly the single word OK. Do not read or write any files, run any commands, "
    "or take any other action."
)
#: Small and fixed, not the task-sized budget dispatch itself uses -- a probe attempt bounds its
#: own cost regardless of how long a real dispatch attempt is allowed to run.
PROBE_BUDGET_MINUTES = 2


async def _connect_temporal() -> Any:
    return await Client.connect(config.TEMPORAL_URL, namespace=config.TEMPORAL_NAMESPACE)


def default_capacity_window_probe() -> pause_response.CapacityProbeResult:
    """Attempt a real, minimal invocation of the default worker and classify the result.

    Reuses `WORKER_REGISTRY[dispatch.DEFAULT_WORKER]`'s own argv/containment/extra_env -- the
    same shape production dispatch invokes -- against a disposable temp workdir, and reads the
    outcome through `dispatch.detect_worker_capacity_failure`, the one place "capacity failure"
    is defined for this codebase. Only a run that completed cleanly with no capacity failure
    detected counts as a confirmed reset; anything else (a recognized capacity failure, a
    timeout, an unrecognized non-zero exit, or the invocation failing to run at all) reports
    `reset=False` or `reset=None` -- never treated as a guess toward `RESUMED` by
    `respond_to_capacity_pause`.
    """
    worker = dispatch.WORKER_REGISTRY[dispatch.DEFAULT_WORKER]
    workdir = Path(tempfile.mkdtemp(prefix="factory-capacity-probe-"))
    try:
        result = dispatch.run_worker(
            PROBE_PROMPT,
            workdir,
            PROBE_BUDGET_MINUTES,
            worker.argv,
            worker.containment_allow,
            worker.extra_env,
            worker.grade_json_result,
        )
    except dispatch.DispatchEnvironmentError as exc:
        return pause_response.CapacityProbeResult(
            reset=None, evidence=f"probe invocation could not run: {exc}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    capacity = dispatch.detect_worker_capacity_failure(result)
    if capacity:
        return pause_response.CapacityProbeResult(
            reset=False, evidence=f"live probe still reports {capacity.describe()}"
        )
    if result.timed_out:
        return pause_response.CapacityProbeResult(
            reset=None,
            evidence=(
                f"probe invocation timed out after {PROBE_BUDGET_MINUTES} minute(s) without a "
                "recognized capacity failure; cannot confirm the window reset from this"
            ),
        )
    if result.exit_code != 0:
        return pause_response.CapacityProbeResult(
            reset=None,
            evidence=(
                f"probe invocation exited {result.exit_code} without a recognized capacity "
                "failure; cannot confirm the window reset from this"
            ),
        )
    return pause_response.CapacityProbeResult(
        reset=True,
        evidence=(
            f"live probe invocation of {dispatch.DEFAULT_WORKER!r} completed successfully with "
            "no capacity failure detected"
        ),
    )


async def _resume_dispatch_schedule_async(note: str) -> None:
    client = await _connect_temporal()
    await schedule_runtime.resume_dispatch_schedule(
        client,
        _TEMPORAL_PAUSE_TYPES,
        schedule_id=config.DISPATCH_SCHEDULE_ID,
        note=note,
        logger=_LOG,
    )


def default_resume_schedule(note: str) -> None:
    """Resume `config.DISPATCH_SCHEDULE_ID` -- the platform's declared "dispatch is running
    again" mechanism, mirroring `activities/dispatch_steps.py`'s pause call exactly."""
    asyncio.run(_resume_dispatch_schedule_async(note))


def default_announce_resume(note: str) -> None:
    failure_diagnosis.announce_capacity_resume(note)


async def _dispatch_schedule_pause_state_async() -> tuple[bool, str]:
    client = await _connect_temporal()
    return await schedule_runtime.dispatch_schedule_pause_state(
        client, schedule_id=config.DISPATCH_SCHEDULE_ID
    )


def default_dispatch_schedule_pause_state() -> tuple[bool, str]:
    return asyncio.run(_dispatch_schedule_pause_state_async())


@activity.defn(name="probe_capacity_pause_resume")
def probe_capacity_pause_resume_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    paused, note = default_dispatch_schedule_pause_state()
    if not paused:
        return {
            "outcome": "schedule_not_paused",
            "detail": (
                f"{config.DISPATCH_SCHEDULE_ID} is not paused; nothing for the capacity-resume "
                "probe to evaluate."
            ),
        }

    response = pause_response.respond_to_capacity_pause(
        note,
        probe=default_capacity_window_probe,
        resume_schedule=default_resume_schedule,
        announce_resume=default_announce_resume,
    )
    return {"outcome": response.outcome, "detail": response.detail}


ACTIVITIES = [probe_capacity_pause_resume_activity]
