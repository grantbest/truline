"""Deterministic dispatch workflow sequence.

This module has no Temporal dependency so tests can exercise the workflow
ordering with an in-process harness. The Temporal workflow adapts
``execute_activity`` to the ``ActivityExecutor`` callable below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import retry_policy

ActivityExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

# A dead Temporal connection must be noticed in the time it takes to miss a
# couple of heartbeats, not at a work-sized timeout. 2026-09-03: the tunnel
# (a kubectl port-forward over Tailscale) flapped for 45 minutes while a
# preflight activity sat dead and undetected, because only the worker-run
# step heartbeated and every activity shared a 45-minute heartbeat_timeout
# sized off the work budget (DEFAULT_STUCK_THRESHOLD_MINUTES), not off any
# heartbeat cadence. Sized in tens of seconds instead, for every activity
# that actually heartbeats (see HEARTBEATING_DISPATCH_STEPS below).
DISPATCH_HEARTBEAT_INTERVAL = timedelta(seconds=20)
DISPATCH_HEARTBEAT_TIMEOUT = DISPATCH_HEARTBEAT_INTERVAL * 3

# Steps whose blocking work is wrapped in the heartbeat-subprocess pattern
# (activities.dispatch_steps._run_blocking_with_heartbeat /
# _run_worker_with_heartbeat) and can therefore promise a heartbeat every
# DISPATCH_HEARTBEAT_INTERVAL. Every other step -- the rest of DISPATCH_STEPS
# below, plus reconcile/record_failure/cleanup, executed outside it -- sends
# no heartbeats at all, so declaring a heartbeat_timeout for one of them
# would just make it fail if it ever ran a little slow, since Temporal has
# nothing to reset the clock. Those steps get no heartbeat_timeout; see
# NON_HEARTBEATING_DISPATCH_STEPS below for how they are bounded instead.
HEARTBEATING_DISPATCH_STEPS = frozenset({"isolate", "preflight", "run", "verify", "propose"})


def heartbeat_timeout_for_step(step: str) -> timedelta | None:
    """Per-activity heartbeat_timeout: tens of seconds where it heartbeats, else none."""
    return DISPATCH_HEARTBEAT_TIMEOUT if step in HEARTBEATING_DISPATCH_STEPS else None


class DispatchAttemptFailed(RuntimeError):
    """An ordinary dispatch failure that Temporal should retry under policy."""


@dataclass(frozen=True)
class DispatchRetryContext:
    """Current Temporal workflow retry attempt and its declared bound.

    This is the WORKFLOW's attempt count, carried for observability only. A
    retried workflow re-runs ``claim`` and may pick a different pending bead
    than the attempt before it (tests/test_claim_atomicity.py), so this
    number does not describe how many times the bead a given attempt happens
    to claim has itself failed. It must never be used to decide whether THAT
    bead is retry-exhausted -- see dispatch.fail_task, which derives
    exhaustion solely from the bead's own recorded failure notes.
    """

    attempt: int
    maximum_attempts: int = retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS


def describe_failure(exc: BaseException) -> str:
    """Flatten an exception chain into a reason that names what went wrong.

    Temporal reports an activity failure as ``ActivityError("Activity task
    failed")`` and carries the message the activity actually raised on
    ``__cause__``. ``str(exc)`` alone therefore records *that* a step failed and
    never *why* — which is what a retry reads back and what a human triages
    from. Walking ``__cause__`` recovers the real reason without importing
    Temporal, keeping this module's in-process testability.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text and text not in parts:
            parts.append(text)
        current = current.__cause__
    return ": ".join(parts) or type(exc).__name__


def _is_timeout_of_type(exc: BaseException, type_name: str) -> bool:
    """Whether an activity died because a Temporal timeout of this type fired.

    Temporal reports this as ``ActivityError`` with a chained
    ``temporalio.exceptions.TimeoutError(type=TimeoutType.<type_name>)``. The
    wrapped error's message is server-generated free text ("Timeout") and not
    safe to match on; ``type`` is the documented, stable signal (verified
    against the installed temporalio>=1.28.0,<2 package rather than assumed).
    Duck-typed instead of imported so this module keeps no temporalio
    dependency (see module docstring) and stays testable without it installed.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        timeout_type_name = getattr(getattr(current, "type", None), "name", None)
        if isinstance(timeout_type_name, str) and type_name in timeout_type_name.upper():
            return True
        current = current.__cause__
    return False


def _is_heartbeat_timeout_death(exc: BaseException) -> bool:
    """Whether an activity died because Temporal's heartbeat_timeout fired."""
    return _is_timeout_of_type(exc, "HEARTBEAT")


def _is_lost_short_step_death(exc: BaseException, step: str) -> bool:
    """Whether ``step`` died from its own short start_to_close_timeout firing.

    Only meaningful for the seven steps NON_HEARTBEATING_DISPATCH_STEPS bounds
    below (2026-09-11 wedge fix): a start_to_close timeout on one of THOSE
    steps means it never got the chance to say why -- Temporal killed it for
    going silent, the same "not a worker verdict" shape a heartbeat_timeout
    death already is for the five heartbeating steps. A start_to_close timeout
    on any other step (chiefly `run`, whose 24h budget is a genuine worker
    verdict window) must not be swept into this -- that would misclassify a
    worker that legitimately ran too long as an infrastructure death instead
    of the work failure it actually is.
    """
    return step in NON_HEARTBEATING_DISPATCH_STEPS and _is_timeout_of_type(exc, "START_TO_CLOSE")


DISPATCH_STEPS = (
    "claim",
    "isolate",
    "preflight",
    "run",
    "contain",
    "scope",
    "smoke",
    "verify",
    "propose",
)

#: The three activities run outside the claim..propose sequence (workflow_core
#: itself calls them by name below), not part of DISPATCH_STEPS -- reconcile
#: runs before the claim, record_failure/cleanup run out of run_dispatch_attempt's
#: except/finally, not its main loop.
NON_SEQUENCE_DISPATCH_STEPS = ("reconcile", "record_failure", "cleanup")

# Amendment (2026-09-16, this bead): OPS-57 gave every activity NOT in
# HEARTBEATING_DISPATCH_STEPS no bound but the 24h ACTIVITY_START_TO_CLOSE_TIMEOUT
# sized for `run`, reasoning that these steps are all a few seconds of
# git/HTTP work and Temporal has no heartbeat clock to reset for them anyway.
# That was correct as far as it went, but it means a step that is LOST -- not
# failed, lost, as claim was for 24h on 2026-09-11 when the tunnel dropped
# before the worker ever ran it -- is undetectable for a full day and never
# retries, wedging the whole queue behind ScheduleOverlapPolicy.SKIP. The
# fix is not a heartbeat (nothing in these steps calls activity.heartbeat, and
# adding one with no corresponding heartbeat is exactly the "fails if it runs
# a little slow" failure OPS-57 was avoiding) -- it is a start_to_close bound
# proportionate to what each step actually does, derived from DISPATCH_STEPS
# and NON_SEQUENCE_DISPATCH_STEPS rather than hand-listed, so a step added to
# either later cannot silently inherit the 24h bound by omission.
ALL_DISPATCH_ACTIVITY_NAMES = DISPATCH_STEPS + NON_SEQUENCE_DISPATCH_STEPS
NON_HEARTBEATING_DISPATCH_STEPS = frozenset(ALL_DISPATCH_ACTIVITY_NAMES) - HEARTBEATING_DISPATCH_STEPS

#: claim/contain/scope/smoke/record_failure/cleanup are each one task's worth
#: of git/HTTP calls -- fixed cost, not scaling with anything. Sized generously
#: (~60x the documented "a few seconds").
#:
#: CORRECTED 2026-09-16 AT THE PR #896 GATE (F4). An earlier version of this
#: comment said 10 minutes stays "comfortably under schedule_status.py's
#: 45-minute wedge alarm". THAT ARITHMETIC IS WRONG, and the next reader would
#: have re-derived it wrongly too: this bound is per ACTIVITY ATTEMPT, and
#: DISPATCH_RETRY_POLICY retries the workflow up to 3 times. A sustained tunnel
#: outage losing reconcile (20 min) then claim (10 min) costs ~30 min per
#: attempt, so ~90 minutes across three -- DOUBLE the alarm, not under it.
#: The bound is still right and is ~16x better than the 24h it replaces; what
#: was wrong was the claim that it pre-empts the alarm. It does not, and it does
#: not need to: the alarm and the bound are independent controls.
DISPATCH_SHORT_STEP_TIMEOUT = timedelta(minutes=10)

#: reconcile alone scales with the size of the review queue -- one GitHub API
#: round trip per task in `review` (dispatch.reconcile_review_tasks) -- so it
#: is not the same fixed-cost shape as the other six and gets its own,
#: longer bound rather than forcing a loop-shaped cost under their tight one.
RECONCILE_STEP_TIMEOUT = timedelta(minutes=20)

_SHORT_STEP_TIMEOUT_OVERRIDES: dict[str, timedelta] = {"reconcile": RECONCILE_STEP_TIMEOUT}

#: Unchanged from before this amendment -- `run`'s own genuine work budget.
ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(hours=24)


def start_to_close_timeout_for_step(step: str) -> timedelta:
    """Per-activity start_to_close_timeout: proportionate to the step's own
    declared work, not the 24h ACTIVITY_START_TO_CLOSE_TIMEOUT `run` needs."""
    if step in NON_HEARTBEATING_DISPATCH_STEPS:
        return _SHORT_STEP_TIMEOUT_OVERRIDES.get(step, DISPATCH_SHORT_STEP_TIMEOUT)
    return ACTIVITY_START_TO_CLOSE_TIMEOUT


async def run_dispatch_attempt(
    request: dict[str, Any],
    execute_activity: ActivityExecutor,
    retry_context: DispatchRetryContext | None = None,
) -> dict[str, Any]:
    """Run one claim-through-propose dispatch pass.

    This is the part of one dispatcher pass that a CLI run
    (``dispatch.dispatch_once``) and a scheduled (Temporal) run
    (``workflows.dispatch_task.DispatchTaskWorkflow``) must execute
    identically: claim, the rest of ``DISPATCH_STEPS``, failure
    classification/recording, and cleanup. Both entry points call this
    function directly so there is exactly one place that sequence lives —
    reconciliation is deliberately not part of it; see
    ``run_dispatch_sequence``, the scheduled drain's wrapper around this.
    """
    state = await execute_activity("claim", dict(request))
    if state.get("status") != "claimed":
        return state

    cleanup_state: dict[str, Any] | None = state
    try:
        for step in DISPATCH_STEPS[1:]:
            state = await execute_activity(step, state)
            cleanup_state = state
        return state
    except Exception as exc:  # noqa: BLE001 - preserve dispatcher failure path
        message = describe_failure(exc)
        failure_state = dict(cleanup_state or state)
        classification = retry_policy.classify_dispatch_failure(
            message,
            worker_result_present="worker_result" in failure_state,
            infrastructure_timeout=(
                _is_heartbeat_timeout_death(exc) or _is_lost_short_step_death(exc, step)
            ),
        )
        if classification == retry_policy.ENVIRONMENT_FAILURE:
            failure_state.pop("worker_result", None)
        failure_state["failure_reason"] = message
        failure_state["failure_class"] = classification.name
        if retry_context is not None:
            failure_state["retry_attempt"] = retry_context.attempt
            failure_state["retry_maximum_attempts"] = retry_context.maximum_attempts
        recorded_failure: dict[str, Any] | None = None
        try:
            recorded_failure = await execute_activity("record_failure", failure_state)
        except Exception as record_exc:  # noqa: BLE001 - report both failures
            message = f"{message}; failure recording failed: {describe_failure(record_exc)}"
        if recorded_failure and recorded_failure.get("status") == "capacity_backpressure_recorded":
            return {
                "status": "capacity_backpressure",
                "exit_code": 1,
                "message": message,
                "task_id": state.get("task", {}).get("id"),
            }
        if recorded_failure and recorded_failure.get("status") == "environmental_fault_recorded":
            return {
                "status": "environmental_fault",
                "exit_code": 1,
                "message": message,
                "task_id": state.get("task", {}).get("id"),
            }
        if recorded_failure and recorded_failure.get("status") == "stale_base_ref_recorded":
            return {
                "status": "stale_base_ref",
                "exit_code": 1,
                "message": message,
                "task_id": state.get("task", {}).get("id"),
            }
        if not classification.consumes_retry:
            status = (
                "environmental_fault"
                if classification == retry_policy.ENVIRONMENT_FAILURE
                else classification.name
            )
            return {
                "status": status,
                "exit_code": 1,
                "message": message,
                "task_id": state.get("task", {}).get("id"),
            }
        if retry_context is not None:
            raise DispatchAttemptFailed(message) from exc
        return {
            "status": "failed",
            "exit_code": 1,
            "message": message,
            "task_id": state.get("task", {}).get("id"),
        }
    finally:
        if cleanup_state is not None:
            try:
                await execute_activity("cleanup", cleanup_state)
            except Exception:
                pass


async def run_dispatch_sequence(
    request: dict[str, Any],
    execute_activity: ActivityExecutor,
    retry_context: DispatchRetryContext | None = None,
) -> dict[str, Any]:
    """Run one scheduled-drain pass: best-effort reconcile, then the attempt.

    Reconciliation runs before the claim and outside the dispatch sequence. A
    task sits in review exactly when the queue is otherwise quiet, so a
    reconcile placed after a successful claim would never run on the passes
    that need it. It is bookkeeping: a failure here is reported on the result
    but must not stop the pass from doing work. It is also not one of the
    nine claim/isolate/.../propose steps a CLI dispatch and a scheduled
    dispatch must run identically — both entry points already share the one
    ``reconcile_review_tasks`` implementation directly (the CLI via
    ``--reconcile-review``, here via the ``reconcile`` activity) — so
    ``run_dispatch_attempt``, the part a non-Temporal caller drives on its
    own, does not include it.
    """
    reconcile_error: str | None = None
    try:
        await execute_activity("reconcile", dict(request))
    except Exception as exc:  # noqa: BLE001 - reconciliation must not block dispatch
        reconcile_error = describe_failure(exc)

    result = await run_dispatch_attempt(request, execute_activity, retry_context)
    if reconcile_error is None:
        return result
    return {**result, "reconcile_error": reconcile_error}
