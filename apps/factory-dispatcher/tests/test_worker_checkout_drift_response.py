"""Tests for turning a detected worker-revision-drift into action (2026-09-02).

The 15-minute drift schedule detected the checkout sitting nine merged PRs behind main and did
nothing about it -- the observation bead updated, the same refusal repeated every quarter hour,
until a person noticed and advanced the checkout by hand. `worker_checkout_drift_response.py` is
the missing action half: given a drift condition already computed elsewhere, it must always
choose exactly one of advance-and-recycle, defer, or halt -- never a fourth "log it and move on".

No Temporal server, no launchd, no substrate, no network, no real git repository: `advance`, the
in-flight check, and the supervisor are all injected fakes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_checkout  # noqa: E402
import worker_checkout_drift_response as drift_response  # noqa: E402
from worker_revision import CheckoutFreshness, WorkerRevisionStatus  # noqa: E402

MAIN_REF = "main"
OLD_REVISION = "aaaaaaa"
MAIN_REVISION = "ccccccc"
NEW_REVISION = "ddddddd"

CHECKOUT_ROOT = Path("/factory/worker-checkout")
SOURCE_REPO_ROOT = Path("/operator/gastown")
RECORD_PATH = Path("/factory/state/worker-revision.json")


def checkout_current() -> CheckoutFreshness:
    """The checkout itself is already at main's tip -- only a recorded-revision drift remains."""
    return CheckoutFreshness(
        revision=MAIN_REVISION,
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=0,
    )


def checkout_stale(*, commits_behind: int = 9) -> CheckoutFreshness:
    """The checkout itself is a valid but outdated ancestor of main."""
    return CheckoutFreshness(
        revision=OLD_REVISION,
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=commits_behind,
    )


class FakeSupervisor:
    def __init__(self) -> None:
        self.recycled = 0

    def recycle(self) -> None:
        self.recycled += 1


def drifted_status(*, is_ancestor: bool = True, commits_behind: int | None = 9) -> WorkerRevisionStatus:
    return WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-09-02T13:24:00Z",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=is_ancestor,
        commits_behind=commits_behind,
    )


def clean_status() -> WorkerRevisionStatus:
    return WorkerRevisionStatus(
        worker_revision=MAIN_REVISION,
        worker_started_at="2026-09-02T13:24:00Z",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=0,
    )


def unknown_status() -> WorkerRevisionStatus:
    return WorkerRevisionStatus.could_not_determine(MAIN_REF, "no worker revision record found")


def respond(
    status,
    *,
    in_flight: bool = False,
    advance=None,
    supervisor=None,
    halt_notes=None,
    checkout_freshness=None,
    factory_activity_in_flight=None,
    wait_seconds: float = 0,
    poll_interval_seconds: float = 0,
    sleep=None,
    clock=None,
):
    supervisor = supervisor if supervisor is not None else FakeSupervisor()
    notes = halt_notes if halt_notes is not None else []
    kwargs = dict(
        checkout_root=CHECKOUT_ROOT,
        source_repo_root=SOURCE_REPO_ROOT,
        factory_activity_in_flight=factory_activity_in_flight or (lambda: in_flight),
        supervisor=supervisor,
        halt_dispatch=notes.append,
        state_path=RECORD_PATH,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if sleep is not None:
        kwargs["sleep"] = sleep
    if clock is not None:
        kwargs["clock"] = clock
    if advance is not None:
        kwargs["advance"] = advance
    if checkout_freshness is not None:
        kwargs["check_checkout_freshness"] = lambda **_kw: checkout_freshness
    return drift_response.respond_to_worker_revision_drift(status, **kwargs), supervisor, notes


# ---------------------------------------------------------------------------
# AC1: advance-or-halt, never silent continuation.
# ---------------------------------------------------------------------------


def test_a_clean_status_takes_no_action_and_touches_no_collaborator():
    def unreachable_advance(**kwargs):
        raise AssertionError("advance must not be called for a status that is not drifted")

    result, supervisor, notes = respond(
        clean_status(), advance=unreachable_advance, supervisor=FakeSupervisor()
    )

    assert result.action == drift_response.NONE
    assert supervisor.recycled == 0
    assert notes == []


def test_a_drifted_status_advances_the_checkout_and_recycles_the_worker():
    calls = []

    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        calls.append((checkout_root, source_repo_root, main_ref))
        return NEW_REVISION

    result, supervisor, notes = respond(drifted_status(), advance=fake_advance)

    assert result.action == drift_response.ADVANCED
    assert result.revision == NEW_REVISION
    assert calls == [(CHECKOUT_ROOT, SOURCE_REPO_ROOT, MAIN_REF)]
    assert supervisor.recycled == 1
    assert notes == []


def test_a_diverged_status_is_still_advanced_since_advance_defaults_to_mains_own_tip():
    # is_ancestor=False (a checkout that diverged entirely, not merely stale) is still
    # `drifted`, and advance()'s default target (main's tip) fixes it exactly the same way.
    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        return NEW_REVISION

    result, supervisor, notes = respond(
        drifted_status(is_ancestor=False, commits_behind=None), advance=fake_advance
    )

    assert result.action == drift_response.ADVANCED
    assert supervisor.recycled == 1


def test_an_unknown_status_takes_no_action():
    # There is no confirmed revision to act on; describe_worker_revision_drift's own contract
    # is that unknown must never be treated as though it were safe -- nor, symmetrically, as
    # a confirmed fault to halt on.
    def unreachable_advance(**kwargs):
        raise AssertionError("advance must not be called for an unknown status")

    result, supervisor, notes = respond(
        unknown_status(), advance=unreachable_advance, supervisor=FakeSupervisor()
    )

    assert result.action == drift_response.NONE
    assert supervisor.recycled == 0
    assert notes == []


def test_a_failed_advance_halts_dispatch_with_a_declared_note_rather_than_continuing_silently():
    def failing_advance(*, checkout_root, source_repo_root, main_ref):
        raise worker_checkout.WorkerCheckoutError("not an ancestor of main")

    result, supervisor, notes = respond(drifted_status(), advance=failing_advance)

    assert result.action == drift_response.HALTED
    assert len(notes) == 1
    assert "not an ancestor of main" in notes[0]
    assert supervisor.recycled == 0  # never recycle a worker onto a checkout that didn't move


def test_response_action_is_always_one_of_the_four_declared_outcomes():
    for status in (clean_status(), unknown_status(), drifted_status()):
        result, _, _ = respond(
            status,
            advance=lambda **kwargs: NEW_REVISION,
        )
        assert result.action in {
            drift_response.NONE,
            drift_response.ADVANCED,
            drift_response.DEFERRED,
            drift_response.HALTED,
        }


# ---------------------------------------------------------------------------
# AC2: an in-flight factory activity defers the advance rather than being raced.
# ---------------------------------------------------------------------------


def test_defers_when_a_dispatch_run_is_in_flight():
    def unreachable_advance(**kwargs):
        raise AssertionError("advance must not run while a dispatch run is in flight")

    result, supervisor, notes = respond(
        drifted_status(), in_flight=True, advance=unreachable_advance
    )

    assert result.action == drift_response.DEFERRED
    assert supervisor.recycled == 0
    assert notes == []


def test_defer_is_not_reported_as_a_halt_or_silently_dropped():
    result, _, notes = respond(drifted_status(), in_flight=True)

    assert result.action != drift_response.HALTED
    assert result.action != drift_response.NONE
    assert result.detail  # a human-readable reason is always present
    assert notes == []  # deferral is not a dispatch halt; nothing paused


def test_factory_activity_in_flight_check_is_only_consulted_when_a_status_is_drifted():
    calls = []

    def counting_check():
        calls.append(1)
        return False

    respond(clean_status(), in_flight=False)  # baseline sanity, uses lambda not counting_check

    drift_response.respond_to_worker_revision_drift(
        clean_status(),
        checkout_root=CHECKOUT_ROOT,
        source_repo_root=SOURCE_REPO_ROOT,
        factory_activity_in_flight=counting_check,
        supervisor=FakeSupervisor(),
        halt_dispatch=lambda note: None,
    )

    assert calls == []


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-3: a bounded wait before deferring. `wait_seconds`/`poll_interval_seconds`
# default to 0 (no wait at all -- the behaviour proven above), so this is purely additive for the
# one caller (`activities.worker_revision_drift.respond_to_drifted_status`) that opts in.
# ---------------------------------------------------------------------------


def _controlled_sleep_and_clock():
    """A fake clock that only ever advances when `sleep` is called -- deterministic, and fast:
    no test here waits in real time."""
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    def clock() -> float:
        return elapsed[0]

    return sleep, clock


def test_bounded_wait_proceeds_once_the_blocker_clears_within_the_window():
    sleep, clock = _controlled_sleep_and_clock()
    in_flight_results = iter([True, True, False])  # two polls still blocked, then clear

    def in_flight():
        return next(in_flight_results)

    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        return NEW_REVISION

    result, supervisor, notes = respond(
        drifted_status(),
        factory_activity_in_flight=in_flight,
        advance=fake_advance,
        checkout_freshness=checkout_stale(),
        wait_seconds=300,
        poll_interval_seconds=15,
        sleep=sleep,
        clock=clock,
    )

    assert result.action == drift_response.ADVANCED
    assert result.action != drift_response.DEFERRED
    assert supervisor.recycled == 1
    assert notes == []
    assert clock() == 30.0  # two 15s polls before the blocker cleared


def test_bounded_wait_defers_when_still_in_flight_at_the_end_of_the_window():
    sleep, clock = _controlled_sleep_and_clock()

    def always_in_flight():
        return True

    result, supervisor, notes = respond(
        drifted_status(),
        factory_activity_in_flight=always_in_flight,
        checkout_freshness=checkout_stale(),
        wait_seconds=300,
        poll_interval_seconds=15,
        sleep=sleep,
        clock=clock,
    )

    assert result.action == drift_response.DEFERRED
    assert supervisor.recycled == 0
    assert notes == []
    assert clock() >= 300.0  # the full window was spent polling


def test_bounded_wait_does_not_sleep_at_all_when_nothing_is_in_flight():
    sleep_calls = []

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        return NEW_REVISION

    result, supervisor, notes = respond(
        drifted_status(),
        in_flight=False,
        advance=fake_advance,
        checkout_freshness=checkout_stale(),
        wait_seconds=300,
        poll_interval_seconds=15,
        sleep=recording_sleep,
        clock=lambda: 0.0,
    )

    assert result.action == drift_response.ADVANCED
    assert sleep_calls == []


def test_a_plain_bool_in_flight_check_leaves_blocked_by_dispatch_unset():
    # Every pre-existing caller/test injects a plain bool, not a richer FactoryActivityInFlight --
    # it cannot say which schedule blocked it, so the deferral must not claim a cause either way.
    result, _, _ = respond(drifted_status(), in_flight=True)

    assert result.action == drift_response.DEFERRED
    assert result.blocked_by_dispatch is None


# ---------------------------------------------------------------------------
# f51e057c AC1: the message names the object that actually drifted -- the
# recorded revision, not the checkout -- when the checkout is not the one behind.
# ---------------------------------------------------------------------------


def test_a_recorded_revision_drift_with_a_current_checkout_does_not_claim_the_checkout_is_behind():
    # FACT AT FILING (2026-09-13): the checkout was verifiably current (HEAD, local main and
    # origin/main all a17b2f7a) while the recorded revision was 9 commits behind, and the
    # deferred response nonetheless read "worker checkout is behind main (a17b2f7a...)" -- naming
    # the very revision the checkout was already AT as though the checkout were behind it.
    result, _, _ = respond(
        drifted_status(), in_flight=True, checkout_freshness=checkout_current()
    )

    assert "checkout is behind" not in result.detail
    assert "checkout" not in result.detail or "already" in result.detail
    # The object that actually drifted -- the recorded revision -- is named explicitly.
    assert OLD_REVISION in result.detail
    assert str(RECORD_PATH) in result.detail


def test_a_checkout_that_is_genuinely_behind_still_names_the_checkout_and_its_own_revision():
    # The inverse case: when the checkout really is the thing that's behind, the message may say
    # so -- but must cite the checkout's own (stale) revision, not main's tip mislabeled as the
    # thing that's behind (the second half of the same defect: the old message's parenthetical
    # was `status.main_revision`, main's own tip, not the outdated revision it was "behind").
    result, _, _ = respond(
        drifted_status(), in_flight=True, checkout_freshness=checkout_stale(commits_behind=9)
    )

    assert "worker checkout" in result.detail
    assert OLD_REVISION in result.detail  # the checkout's own stale revision, not main's tip


# ---------------------------------------------------------------------------
# f51e057c AC2: a remedy that cannot clear the condition it detected must never be proposed --
# an already-current checkout is restarted (worker restart), never "advanced" (a no-op).
# ---------------------------------------------------------------------------


def test_a_current_checkout_with_only_the_record_behind_restarts_rather_than_advances():
    def unreachable_advance(**kwargs):
        raise AssertionError(
            "advance must not be called: the checkout is already current, so advancing it "
            "cannot clear a condition that lives entirely in worker-revision.json"
        )

    result, supervisor, notes = respond(
        drifted_status(),
        in_flight=False,
        advance=unreachable_advance,
        checkout_freshness=checkout_current(),
    )

    assert result.action == drift_response.RESTARTED
    assert result.action != drift_response.ADVANCED
    assert supervisor.recycled == 1  # the restart itself -- what rewrites worker-revision.json
    assert notes == []


def test_a_genuinely_stale_checkout_is_still_advanced_not_merely_restarted():
    calls = []

    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        calls.append((checkout_root, source_repo_root, main_ref))
        return NEW_REVISION

    result, supervisor, notes = respond(
        drifted_status(),
        in_flight=False,
        advance=fake_advance,
        checkout_freshness=checkout_stale(),
    )

    assert result.action == drift_response.ADVANCED
    assert calls == [(CHECKOUT_ROOT, SOURCE_REPO_ROOT, MAIN_REF)]
    assert supervisor.recycled == 1
    assert notes == []


def test_a_diverged_checkout_is_advanced_rather_than_restarted():
    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        return NEW_REVISION

    diverged = CheckoutFreshness(
        revision="zzzzzzz",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=False,
    )

    result, supervisor, notes = respond(
        drifted_status(), in_flight=False, advance=fake_advance, checkout_freshness=diverged
    )

    assert result.action == drift_response.ADVANCED
    assert supervisor.recycled == 1


# ---------------------------------------------------------------------------
# f51e057c F2: a drift-escalation pause must not be a one-way factory stop -- once the condition
# it exists to enforce has cleared, the schedule is resumed automatically, but only a pause this
# mechanism itself made is ever touched. Mirrors `capacity_pause_response.py`'s NOT_OURS guard.
# ---------------------------------------------------------------------------

OUR_PAUSE_NOTE = f"{drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"


def resume(*, paused, note):
    resumed = []
    result = drift_response.respond_to_cleared_drift(
        dispatch_schedule_pause_state=lambda: (paused, note),
        resume_schedule=resumed.append,
    )
    return result, resumed


def test_a_schedule_paused_by_this_mechanism_is_resumed():
    result, resumed = resume(paused=True, note=OUR_PAUSE_NOTE)

    assert result.outcome == drift_response.PAUSE_RESUMED
    assert len(resumed) == 1
    assert OUR_PAUSE_NOTE in resumed[0]


def test_an_unpaused_schedule_is_left_alone():
    result, resumed = resume(paused=False, note="")

    assert result.outcome == drift_response.PAUSE_NOT_PAUSED
    assert resumed == []


def test_a_manually_paused_schedule_is_not_ours_and_is_left_alone():
    result, resumed = resume(paused=True, note="paused by the Operator for maintenance")

    assert result.outcome == drift_response.PAUSE_NOT_OURS
    assert resumed == []


def test_a_capacity_paused_schedule_is_not_ours_and_is_left_alone():
    # The sibling capacity-backpressure pause uses its own, differently-prefixed note
    # (schedule_runtime.CAPACITY_PAUSE_NOTE_PREFIX) -- this mechanism must never resume it.
    result, resumed = resume(
        paused=True, note="factory dispatcher paused: capacity backpressure; exhausted"
    )

    assert result.outcome == drift_response.PAUSE_NOT_OURS
    assert resumed == []


# ---------------------------------------------------------------------------
# dev.finding 9a10f2aa: `is_our_pause_note` is the one place the pause-note prefix comparison is
# written, shared by `respond_to_cleared_drift` above and the unknown-while-paused announcement
# in `activities/worker_revision_drift.py` -- both need the same "is this pause ours" answer, and
# only one of them may act on it (resume).
# ---------------------------------------------------------------------------


def test_is_our_pause_note_recognizes_our_own_prefix():
    assert drift_response.is_our_pause_note(OUR_PAUSE_NOTE) is True


def test_is_our_pause_note_rejects_a_manual_pause():
    assert drift_response.is_our_pause_note("paused by the Operator for maintenance") is False


def test_is_our_pause_note_rejects_the_capacity_mechanisms_pause():
    assert drift_response.is_our_pause_note(
        "factory dispatcher paused: capacity backpressure; exhausted"
    ) is False


def test_is_our_pause_note_rejects_an_empty_note():
    assert drift_response.is_our_pause_note("") is False


# ---------------------------------------------------------------------------
# standalone importability (2026-09-14, f51e057c F-A)
# ---------------------------------------------------------------------------
#
# This test file's own `sys.path.insert` above already has `worker_checkout_drift_response`
# loaded before these tests run, so an in-process import or `importlib.reload` cannot reproduce
# the cycle a *fresh* interpreter hits: `worker_checkout_drift_response` -> `worker_checkout` ->
# `dispatch` -> (mid-module) `activities` -> `activities.worker_revision_drift` ->
# `worker_checkout_drift_response` again, re-entering it while its own body is still mid-import.
# A prior attempt's eager `WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX =
# drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX` alias in
# `activities/worker_revision_drift.py` raised `AttributeError: partially initialized module`
# the moment that reentrant import ran -- only a real subprocess proves it is gone.


def _run_standalone_import(module_name: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


def test_worker_checkout_drift_response_is_importable_standalone():
    result = _run_standalone_import("worker_checkout_drift_response")
    assert result.returncode == 0, result.stderr


def test_activities_worker_revision_drift_is_importable_standalone():
    """The same cycle, entered from the other side."""
    result = _run_standalone_import("activities.worker_revision_drift")
    assert result.returncode == 0, result.stderr


def test_worker_checkout_is_importable_standalone():
    result = _run_standalone_import("worker_checkout")
    assert result.returncode == 0, result.stderr
