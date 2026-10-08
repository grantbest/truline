"""Turn worker-revision-drift detection into action: advance-or-restart-or-halt, never silent
continuation.

2026-09-02: the fifteen-minute worker-revision-drift schedule
(`activities/worker_revision_drift.py`) detected the checkout sitting nine merged PRs behind
main and landed that fact on an `arch.observation` bead -- and then nothing else happened.
`dispatch.ensure_worker_checkout_is_current` refuses any *individual* dispatch attempt from a
stale checkout, so nothing was silently dispatched from the stale tree either, but a refusal
repeated every fifteen minutes with no one advancing the checkout is still a factory that sits
idle until a human runs `worker_checkout.py advance` by hand. Detection without action is not an
outcome.

2026-09-13 (f51e057c): `status` here is `worker_revision.WorkerRevisionStatus` -- the worker's
*recorded* revision (`worker-revision.json`, frozen at process start) compared against main, not
the checkout's own git HEAD. Those are different objects, and this module used to conflate them:
every message named "the checkout" regardless of which one had actually drifted, and the one
remedy it knew -- `worker_checkout.advance`, which moves the checkout -- is a no-op when the
checkout is already current and only the record is stale (the ordinary case once a checkout has
already been advanced but the worker restarted since). Only a worker restart rewrites the record.
This module now checks the checkout's own freshness (`worker_revision.check_checkout_freshness`)
before choosing a remedy, so the message and the action both name whichever object actually
needs to move.

Given a drift condition already computed elsewhere (`worker_revision.describe_worker_revision_drift`),
`respond_to_worker_revision_drift` decides and executes exactly one of four outcomes -- never a
fifth "log it and move on":

  * ADVANCED -- the checkout itself is confirmed behind (or diverged from) main, or its freshness
    could not be confirmed at all (treated conservatively, same as behind): no dispatch run is in
    flight, so the checkout is moved with the existing, one documented mechanism
    (`worker_checkout.advance`) and the worker is recycled through its supervisor so it loads what
    `advance` just checked out.
  * RESTARTED -- the checkout is already current; only the worker's recorded revision is behind.
    Advancing an already-current checkout would be a no-op, so this does not call `advance()` at
    all -- it only recycles the worker through its supervisor, which is what makes
    `record_worker_start` write a fresh record.
  * DEFERRED -- some factory activity is in flight (dispatch itself, or one of the eleven
    15-minute reconcilers `worker.py` also schedules onto the same shared executor pool since
    OPS-99 widened it past one slot); moving the checkout, or restarting the worker, underneath
    it would let a running task finish against a tree (or a process) that changed out from under
    it, so nothing is touched until the run ends. Not a failure: the next scheduled evaluation
    tries again. A caller that lets this repeat unbounded across many consecutive evaluations of
    the *same* condition must escalate rather than keep deferring silently forever -- see
    `activities/worker_revision_drift.py`'s consecutive-deferral bound; this function has no
    memory of prior calls, so it cannot make that judgment itself.
  * HALTED -- advancing itself is unsafe or fails (including the self-referential invocation
    `worker_checkout.advance` now refuses on its own). Dispatch is halted through the supplied
    `halt_dispatch` callable rather than left to keep refusing silently, fifteen minutes at a
    time.

Every collaborator this needs -- whether any factory activity is in flight, how fresh the
checkout is, how the worker is recycled, how dispatch is halted, and `advance` itself -- is
injected, so the decision logic here is testable with no Temporal server, no launchd, no
substrate and no network.
Production wiring (real Temporal query, real `launchctl kickstart`, real schedule pause) lives in
`activities/worker_revision_drift.py`, the one place that already runs on the schedule and
already has the `WorkerRevisionStatus` this needs. See `.factory/design.md`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import worker_checkout
import worker_revision

#: The only five outcomes `respond_to_worker_revision_drift` may return -- deliberately not an
#: open-ended string, so a caller (and a test) can assert "one of these five" rather than trust
#: free text never to grow a silent sixth meaning. `ESCALATED` is never returned by this
#: function itself (it has no memory of prior calls) -- it is the value the activity layer
#: substitutes for a `DEFERRED` outcome once the same condition has been deferred too many
#: consecutive times; declared here anyway so both modules share one vocabulary.
NONE = "none"
ADVANCED = "advanced"
RESTARTED = "restarted"
DEFERRED = "deferred"
HALTED = "halted"
ESCALATED = "escalated"

#: Distinguishes a drift-halt pause from `dispatch_steps.py`'s capacity-backpressure pause in
#: the schedule's own note field -- both use `schedule_runtime.pause_dispatch_schedule`, the
#: platform's one declared "dispatch is deliberately not running" alert, but an operator
#: reading the paused note should not have to guess which of the two paused it. Also what makes
#: a paused schedule's cause identifiable enough to resume automatically once the underlying
#: drift clears -- see `respond_to_cleared_drift` below, the mirror of
#: `capacity_pause_response.py`'s `NOT_OURS` guard for the sibling capacity pause.
WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX = (
    "factory dispatcher paused: worker checkout drifted from main and could not be advanced "
    "safely"
)


def is_our_pause_note(note: str) -> bool:
    """True if `note` carries this mechanism's own pause-note prefix.

    The one place this prefix comparison is written -- shared by `respond_to_cleared_drift`
    below (which resumes a pause carrying this prefix once the drift clears) and
    `activities/worker_revision_drift.py`'s unknown-while-paused announcement (which must
    recognize the same prefix but never resume on `unknown` -- PRIN-015 fail closed, unknown is
    never folded into clear).
    """
    return (note or "").startswith(WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX)


class WorkerSupervisor(Protocol):
    """Recycles the worker process so it loads what `advance` just checked out.

    Deliberately one method: this module has no other reason to reach the worker's
    supervisor, and a wider surface would let a future caller start reaching for
    capabilities (stop, install, reconfigure) this response was never meant to need.
    """

    def recycle(self) -> None: ...


#: Returns either a plain bool or a richer truthy/falsy result (a NamedTuple like
#: `activities.worker_revision_drift.FactoryActivityInFlight`, which carries a `.description`
#: naming what is in flight) -- `respond_to_worker_revision_drift` reads it with `bool(...)`
#: and an optional `getattr(..., "description", "")`, so either shape works.
FactoryActivityInFlightCheck = Callable[[], Any]
HaltDispatch = Callable[[str], None]
AdvanceFn = Callable[..., str]
#: Quoted forward ref, not `worker_revision.CheckoutFreshness` bare: `Callable[...]` is a plain
#: expression `from __future__ import annotations` does not defer, so an unquoted reference here
#: is evaluated the moment this line runs. On the standalone-import path (this module first),
#: `import worker_checkout` below re-enters `dispatch` -> `activities` -> back to this very module
#: while it is still partially initialized -- `worker_revision` itself is unaffected by that cycle
#: and is always fully loaded by the time this line runs, but a quoted `ForwardRef` costs nothing
#: and keeps this line from ever depending on that ordering holding.
CheckCheckoutFreshnessFn = Callable[..., "worker_revision.CheckoutFreshness"]
#: Real signatures are `time.sleep`/`time.monotonic` -- injected so a test can drive the
#: bounded-wait loop below without a real wait. Resolved from the `time` module at call time
#: (never bound as a literal default value), so `monkeypatch.setattr(time, "sleep", ...)` /
#: `(..., "monotonic", ...)` reaches it even though this module only imports `time` itself.
SleepFn = Callable[[float], None]
ClockFn = Callable[[], float]


@dataclass(frozen=True)
class DriftResponse:
    action: str
    detail: str
    #: Set only when `action == ADVANCED` -- the revision `advance()` actually checked out.
    revision: str | None = None
    #: Set only on a `DEFERRED` response, from the in-flight result that caused it
    #: (`getattr(in_flight, "dispatch", None)`) -- `True` when the blocking schedule was
    #: dispatch itself, `False` when it was named and was not dispatch, `None` when the caller's
    #: in-flight check is a plain bool and cannot say which schedule it saw (every existing test
    #: still using that shape). `activities.worker_revision_drift._escalate_persistent_deferral`
    #: reads this directly to decide whether pausing dispatch can help -- PRIN-008, the cause
    #: travels as structured state, never as prose a reader has to parse back out of `detail`.
    blocked_by_dispatch: bool | None = None


def _checkout_shape(
    *,
    status: worker_revision.WorkerRevisionStatus,
    checkout_root: Path,
    record_path: Path,
    check_checkout_freshness: CheckCheckoutFreshnessFn,
) -> tuple[bool, str]:
    """Which object actually needs to move, and the sentence describing it.

    Returns `(checkout_needs_advance, remedy_detail)`. `checkout_needs_advance` is True when the
    checkout itself is confirmed behind or diverged from main, *or* when that could not be
    confirmed at all -- an unconfirmed checkout is treated the same as a stale one (advance is
    the conservative default: idempotent when the checkout turns out to already be current,
    unlike doing nothing when it turns out not to be). It is False only when the checkout is
    affirmatively confirmed current, which is the one case a restart -- not an advance -- clears
    the condition.
    """
    freshness = check_checkout_freshness(repo_root=checkout_root, main_ref=status.main_ref)

    if freshness.error:
        return True, (
            f"worker's recorded revision ({status.worker_revision}, recorded in {record_path}) "
            f"is behind {status.main_ref} ({status.main_revision}); could not confirm whether "
            f"the checkout at {checkout_root} is also behind ({freshness.error}), so treating "
            "it as though it needs advancing"
        )

    if freshness.is_ancestor is False:
        return True, (
            f"worker checkout at {checkout_root} is at {freshness.revision}, which has "
            f"diverged from {status.main_ref} ({status.main_revision}) entirely"
        )

    if freshness.stale:
        return True, (
            f"worker checkout at {checkout_root} is at {freshness.revision}, "
            f"{freshness.commits_behind} commit(s) behind {status.main_ref} "
            f"({status.main_revision})"
        )

    return False, (
        f"worker's recorded revision ({status.worker_revision}, recorded in {record_path}) is "
        f"behind {status.main_ref} ({status.main_revision}), though the checkout at "
        f"{checkout_root} is already at {freshness.revision}"
    )


def respond_to_worker_revision_drift(
    status: worker_revision.WorkerRevisionStatus,
    *,
    checkout_root: Path,
    source_repo_root: Path,
    factory_activity_in_flight: FactoryActivityInFlightCheck,
    supervisor: WorkerSupervisor,
    halt_dispatch: HaltDispatch,
    advance: AdvanceFn | None = None,
    check_checkout_freshness: CheckCheckoutFreshnessFn | None = None,
    state_path: Path | None = None,
    wait_seconds: float = 0,
    poll_interval_seconds: float = 0,
    sleep: SleepFn | None = None,
    clock: ClockFn | None = None,
) -> DriftResponse:
    """Advance-or-restart-or-halt on a drifted `status`; never continue in silence.

    2026-10-01 (dev.finding 639a20c5): `wait_seconds`/`poll_interval_seconds` default to 0 --
    every existing caller and test is unaffected. With `wait_seconds > 0`, a blocking in-flight
    result is not fatal on the first check: `factory_activity_in_flight` is re-polled every
    `poll_interval_seconds` until `wait_seconds` elapses, proceeding the moment it comes back
    clear and deferring only if it is still blocked when the window ends. This exists because the
    one caller that opts in (`activities.worker_revision_drift.respond_to_drifted_status`) runs on
    the same 15-minute tick as every other reconciler by default; the schedule-level offset
    (`schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS`) makes a same-tick collision
    rare, this bounded wait makes it survivable when it still happens.

    `status.drifted` covers both shapes `describe_worker_revision_drift` reports for the
    *recorded* revision: one that is a valid but outdated ancestor of main, and one that has
    diverged from it entirely. Either can coincide with a checkout that is itself still stale, or
    with one that has already been advanced and is simply waiting on a worker restart to record
    it -- `_checkout_shape` tells the two apart so the remedy actually clears the condition
    (2026-09-13, f51e057c). An `unknown` status (`status.error` set, `drifted` False by
    construction -- see `WorkerRevisionStatus.drifted`) is left alone here: there is no confirmed
    revision to act on, and `describe_worker_revision_drift`'s own docstring is explicit that
    unknown must never be treated as though it were clean.
    """
    # Resolved lazily, not as an import-time default: worker_checkout imports
    # dispatch, dispatch imports workflow_core, and the activity layer imports
    # this module -- an import-time worker_checkout.advance default detonates
    # that cycle with a partially-initialized module (found by the EA
    # conformance job on #637, dormant since the path-filtered jobs skipped
    # scripts-adjacent changes).
    if advance is None:
        advance = worker_checkout.advance
    if check_checkout_freshness is None:
        check_checkout_freshness = worker_revision.check_checkout_freshness
    if not status.drifted:
        return DriftResponse(
            NONE,
            f"worker's recorded revision is not drifted from {status.main_ref}; no action taken.",
        )

    record_path = state_path if state_path is not None else worker_revision.default_state_path()
    checkout_needs_advance, remedy_detail = _checkout_shape(
        status=status,
        checkout_root=checkout_root,
        record_path=record_path,
        check_checkout_freshness=check_checkout_freshness,
    )

    in_flight = factory_activity_in_flight()
    if in_flight and wait_seconds > 0:
        if sleep is None:
            sleep = time.sleep
        if clock is None:
            clock = time.monotonic
        deadline = clock() + wait_seconds
        while in_flight and clock() < deadline:
            sleep(poll_interval_seconds)
            in_flight = factory_activity_in_flight()

    if in_flight:
        action_word = "advancing the checkout" if checkout_needs_advance else "restarting the worker"
        # `.description` is populated by the real, production check
        # (`activities.worker_revision_drift.default_factory_activity_in_flight`), which names
        # the specific schedule/workflow it found running -- PRIN-008, a refusal must name its
        # cause. Falls back to the old wording for callers (and every existing test) that still
        # inject a plain bool.
        what = getattr(in_flight, "description", "") or "a dispatch run"
        return DriftResponse(
            DEFERRED,
            f"{remedy_detail}, but {what} is in flight; deferring {action_word} until "
            "it ends rather than acting underneath a running task.",
            blocked_by_dispatch=getattr(in_flight, "dispatch", None),
        )

    if not checkout_needs_advance:
        supervisor.recycle()
        return DriftResponse(
            RESTARTED,
            f"{remedy_detail}; restarted the worker through its supervisor to record a fresh "
            "revision rather than advancing an already-current checkout.",
        )

    try:
        revision = advance(
            checkout_root=checkout_root,
            source_repo_root=source_repo_root,
            main_ref=status.main_ref,
        )
    except worker_checkout.WorkerCheckoutError as exc:
        note = f"{remedy_detail}; advancing it to fix that failed: {exc}"
        halt_dispatch(note)
        return DriftResponse(HALTED, note)

    supervisor.recycle()
    return DriftResponse(
        ADVANCED,
        f"advanced worker checkout at {checkout_root} to {revision} and recycled the worker "
        "through its supervisor.",
        revision=revision,
    )


#: The only three outcomes `respond_to_cleared_drift` may return -- deliberately not an
#: open-ended string, matching `capacity_pause_response.py`'s three-outcome vocabulary
#: (NOT_OURS/STILL_EXHAUSTED/RESUMED) for the sibling capacity pause.
PAUSE_NOT_PAUSED = "not_paused"
PAUSE_NOT_OURS = "not_ours"
PAUSE_RESUMED = "resumed"

DispatchSchedulePauseState = Callable[[], tuple[bool, str]]
ResumeDispatchSchedule = Callable[[str], None]


@dataclass(frozen=True)
class DriftPauseResumeResponse:
    outcome: str
    detail: str


def respond_to_cleared_drift(
    *,
    dispatch_schedule_pause_state: DispatchSchedulePauseState,
    resume_schedule: ResumeDispatchSchedule,
) -> DriftPauseResumeResponse:
    """Resume the dispatch schedule once a worker-revision-drift pause's own condition has
    cleared -- never touch a pause this mechanism did not create.

    2026-09-13 (f51e057c F2): the activity layer pauses dispatch (via
    `WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX`) once the same drift condition has been deferred
    past the declared bound. Pausing stops any dispatch run from being in flight, so the very
    next evaluation of `respond_to_worker_revision_drift` typically resolves cleanly -- RESTARTED
    or ADVANCED -- but nothing ever resumed the schedule: a merely-busy queue could otherwise
    permanently stop the factory. Mirrors `capacity_pause_response.respond_to_capacity_pause`'s
    `NOT_OURS` guard exactly: only a pause note carrying this mechanism's own prefix is ever
    resumed here, never an operator's own manual pause or no pause at all.
    """
    paused, note = dispatch_schedule_pause_state()
    if not paused:
        return DriftPauseResumeResponse(
            PAUSE_NOT_PAUSED, "dispatch schedule is not paused; nothing to resume."
        )
    if not is_our_pause_note(note):
        return DriftPauseResumeResponse(
            PAUSE_NOT_OURS,
            "schedule pause note does not carry the worker-revision-drift prefix "
            f"({WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX!r}); this is not this mechanism's pause "
            f"to resume. note={note!r}",
        )
    resume_note = (
        "factory dispatcher resumed: worker-revision-drift condition cleared "
        f"(was paused: {note})"
    )
    resume_schedule(resume_note)
    return DriftPauseResumeResponse(PAUSE_RESUMED, resume_note)
