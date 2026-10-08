"""Retry policy constants for factory dispatch attempts.

The retry budget is a dispatcher execution policy. Keep the bound here so
Temporal configuration, guards, and terminal-state recording cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

DISPATCH_RETRY_MAXIMUM_ATTEMPTS = 3

#: Threshold for dispatch.py's consecutive-environmental-fault circuit breaker
#: (queue_order.trailing_environmental_fault_streak / dispatch.pick_task). An
#: environmental fault never spends the retry budget (ENVIRONMENT_FAILURE
#: below) - correct for a transient fault, like an auth outage, that will not
#: recur. It has no bound at all, which is wrong for a PERMANENT one: a bad
#: declared-verification command reclaims the same bead every drain tick
#: forever, spending no budget while starving every other pending task behind
#: it (OPS-6, 2026-08-23: dev.task 05262e55 dispatched 12 times in under three
#: hours before anything caught it). This bounds the SAME fault recurring on
#: CONSECUTIVE dispatches only - it is not a second retry cap. A transient
#: fault that does not repeat, or a real work failure landing in between two
#: occurrences of an intermittent one, resets the count to zero. R26.12 B3
#: (PC-EXE-007): a latched bead is not stuck forever - it lifts on a later
#: successful run's "Worker finished in" status note, on a recorded requeue
#: (guards.requeue_barrier_at), or on an operator forcing an out-of-band
#: attempt with `dispatch.py --task <bead-id>`.
CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT = 3

#: Threshold for dispatch.py's consecutive-concurrent-clone-defer breaker
#: (dispatch._record_concurrent_clone_defer / ensure_base_ref_current). A single
#: deferral because another dispatch clone is in flight is not a person's problem --
#: it clears on its own once that clone finishes. It stops being safe to stay silent
#: about once the SAME cause recurs this many cycles in a row: that shape is exactly
#: what a marker unable to distinguish "this run is live" from "the daemon that once
#: ran it is live" produces forever (OPS-60, 2026-09-04: 188 consecutive refusals, no
#: alert, found only because a person asked why nothing was being picked up).
CONSECUTIVE_CONCURRENT_CLONE_DEFER_LIMIT = 3


def retry_exhausted(attempt: int) -> bool:
    """Whether a 1-based dispatch attempt has spent the retry policy."""
    return int(attempt) >= DISPATCH_RETRY_MAXIMUM_ATTEMPTS


@dataclass(frozen=True)
class DispatchFailureClass:
    """Failure class that owns both retry spending and operator wording."""

    name: str
    consumes_retry: bool


WORK_FAILURE = DispatchFailureClass("work", True)
ENVIRONMENT_FAILURE = DispatchFailureClass("environment", False)
CAPACITY_FAILURE = DispatchFailureClass("capacity", False)
# A worker that changed nothing AND said why in terms that claim the task is
# already done is not the same failure as a worker that changed nothing and
# said nothing (dev.task 07a99270, 2026-08-19): the claim ends this run's
# retry chain for operator disposition (--bind-pr / --requeue / reject)
# instead of burning attempts re-discovering the same conclusion.
ALREADY_SATISFIED_FAILURE = DispatchFailureClass("already_satisfied", False)

#: Marker dispatch.changed_nothing_failure_reason prefixes onto the reason
#: text when the worker's own output claims the task is already satisfied.
#: Matched here rather than re-detected, so the claim is judged once.
ALREADY_SATISFIED_WORK_MARKER = "already-satisfied work:"


def _verification_could_not_start_only(reason: str) -> bool:
    return "Could not start:" in reason and "Failed: (none)" in reason


def classify_dispatch_failure(
    reason: str,
    *,
    worker_result_present: bool,
    infrastructure_timeout: bool = False,
) -> DispatchFailureClass:
    """Classify a dispatch failure once for recording and retry policy.

    A worker verdict can only exist once the worker process produced a result.
    Failures before that point, and declared verification that could not start,
    are environment failures. Declared verification that ran and failed remains
    a work failure.

    ``infrastructure_timeout`` means Temporal killed the activity for going
    silent -- heartbeat-silent (a heartbeating step's connection dropped), or
    start_to_close-silent -- neither is a worker verdict.

    NARROWED 2026-09-16 AT THE PR #896 GATE (F3). An earlier draft of this
    sentence said the start_to_close half covers "one of the seven
    non-heartbeating steps ... lost before it ever ran or answered". Seven steps
    are BOUNDED by DISPATCH_SHORT_STEP_TIMEOUT, but only THREE reach this
    classification: contain, scope and smoke, and all three run AFTER ``run``.
    The other four never arrive here -- ``claim`` raises before
    run_dispatch_attempt's try block and escapes DispatchTaskWorkflow uncaught,
    and reconcile/record_failure/cleanup have their own handlers. ``claim`` --
    the step that actually wedged on 2026-09-11 -- is freed by its BOUND plus
    workflow-level retry exhaustion, not by this classification. Do not read
    this override as covering it.
    neither is a worker verdict. That is an infrastructure death regardless of
    which step it hit or whether a ``worker_result`` already existed from an
    earlier step (e.g. a dead connection during ``verify``, ``propose``,
    ``contain``, ``scope``, or ``smoke``, all of which run after ``run``
    already produced one): it must never be charged to the work it happens to
    interrupt, so it is checked first and overrides every other rule below.
    """
    if infrastructure_timeout:
        return ENVIRONMENT_FAILURE
    if "capacity backpressure:" in reason:
        return CAPACITY_FAILURE
    if ALREADY_SATISFIED_WORK_MARKER in reason:
        return ALREADY_SATISFIED_FAILURE
    if not worker_result_present:
        return ENVIRONMENT_FAILURE
    if _verification_could_not_start_only(reason):
        return ENVIRONMENT_FAILURE
    return WORK_FAILURE
