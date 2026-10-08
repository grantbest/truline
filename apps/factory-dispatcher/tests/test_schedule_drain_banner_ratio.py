"""The drain banner's severity wording must match what was actually measured.

Measured 2026-09-17: nine of the last ten drains succeeded, one failed (bead
5bb23acc's own WORK-class failure), and the banner still said "FACTORY IS
FAILING, NOT IDLE ... every attempt is dying" -- a sentence that is only true
when *every* recent drain failed. That is the 2026-08-06 outage signature
(five-for-five, every firing dead within seconds), and repeating that
sentence for an ordinary single failure teaches operators to discount it for
the case it exists to catch.

`DrainOutcome` carries no failure class (WORK vs INFRA), so the fix works
only with what `FactoryScheduleStatus` already computes: `recent`,
`recent_failures`, `consecutive_failures`. These tests drive constructed
`FactoryScheduleStatus`/`DrainOutcome` values directly -- no Temporal, no
substrate, no network -- across every boundary: zero failures, a lone
trailing failure, a lone non-trailing (already-recovered) failure, a partial
consecutive run short of the window, all-of-N failures (both N=10 and the
N=1 edge), and the empty-history case, whose message is unrelated to this
change and must survive unchanged.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
from schedule_runtime import (  # noqa: E402
    DrainOutcome,
    FactoryScheduleStatus,
    render_factory_schedule_status,
)
from schedule_status import WaitingQueueStatus  # noqa: E402

SCHEDULE_ID = "factory-dispatcher-dev"
NOW = datetime(2026, 9, 17, 23, 21, tzinfo=timezone.utc)

OUTAGE_SENTENCE_FRAGMENT = "FACTORY IS FAILING, NOT IDLE"
RATIO_FRAGMENT = "scheduled drain(s) failed"


def outcome(workflow_id: str, status: str) -> DrainOutcome:
    return DrainOutcome(workflow_id=workflow_id, scheduled_at="", status=status)


def status_with(recent: list[DrainOutcome]) -> FactoryScheduleStatus:
    return FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=tuple(recent),
    )


def render(status: FactoryScheduleStatus) -> str:
    return render_factory_schedule_status(status, namespace="dev")


# --- Known-good control: nothing failed, no banner at all. ---------------


def test_control_zero_failures_produces_no_drain_banner():
    status = status_with([outcome(f"drain-{i}", "COMPLETED") for i in range(10)])

    rendered = render(status)

    assert status.recent_failures == 0
    assert status.consecutive_failures == 0
    assert status.drains_failing is False
    assert OUTAGE_SENTENCE_FRAGMENT not in rendered
    assert RATIO_FRAGMENT not in rendered


# --- The measured shape: one of ten, the ordinary case. -------------------


def test_one_of_ten_trailing_failure_states_the_ratio_not_the_outage_sentence():
    recent = [outcome(f"drain-{i}", "COMPLETED") for i in range(9)]
    recent.append(outcome("drain-9", "FAILED"))
    status = status_with(recent)

    rendered = render(status)

    assert status.recent_failures == 1
    assert status.consecutive_failures == 1
    assert status.drains_failing is True
    assert "1 of the last 10 scheduled drain(s) failed (1 most recent in a row)" in rendered
    assert OUTAGE_SENTENCE_FRAGMENT not in rendered
    assert "every attempt is dying" not in rendered


def test_a_non_trailing_single_failure_does_not_reach_the_banner_at_all():
    # drains_failing is trailing-only (unchanged): a failure already followed
    # by a success does not even produce a ratio sentence.
    recent = [
        outcome("drain-0", "COMPLETED"),
        outcome("drain-1", "FAILED"),
        outcome("drain-2", "COMPLETED"),
    ]
    status = status_with(recent)

    rendered = render(status)

    assert status.recent_failures == 1
    assert status.consecutive_failures == 0
    assert status.drains_failing is False
    assert RATIO_FRAGMENT not in rendered
    assert OUTAGE_SENTENCE_FRAGMENT not in rendered


def test_partial_consecutive_run_short_of_the_window_states_the_ratio():
    recent = [outcome(f"drain-{i}", "COMPLETED") for i in range(7)]
    recent += [outcome(f"drain-{i}", "FAILED") for i in range(7, 10)]
    status = status_with(recent)

    rendered = render(status)

    assert status.recent_failures == 3
    assert status.consecutive_failures == 3
    assert len(status.recent) == 10
    assert "3 of the last 10 scheduled drain(s) failed (3 most recent in a row)" in rendered
    assert OUTAGE_SENTENCE_FRAGMENT not in rendered


# --- The all-failing case must stay loud, in the same urgent register. ----


def test_all_of_n_failures_still_fires_the_loud_outage_sentence():
    recent = [outcome(f"drain-{i}", "FAILED") for i in range(5)]
    status = status_with(recent)

    rendered = render(status)

    assert status.recent_failures == 5
    assert status.consecutive_failures == 5
    assert status.drains_failing is True
    expected = (
        "FACTORY IS FAILING, NOT IDLE: the last 5 scheduled drain(s) failed. "
        "A quiet board here means every attempt is dying, not that there is "
        "no work."
    )
    assert expected in rendered


def test_all_of_n_with_a_single_drain_is_still_the_outage_sentence():
    # The N=1 edge: one drain observed, it failed, so consecutive == len(recent)
    # == 1. There is no non-failing evidence in the window at all, which is
    # the outage shape, not the "mostly healthy" shape.
    status = status_with([outcome("drain-0", "FAILED")])

    rendered = render(status)

    assert status.consecutive_failures == 1
    assert len(status.recent) == 1
    assert "FACTORY IS FAILING, NOT IDLE: the last 1 scheduled drain(s) failed" in rendered


# --- The empty-history message is a different case and must not move. -----


def test_empty_recent_history_message_is_preserved_unchanged():
    status = status_with([])

    rendered = render(status)

    assert status.recent == ()
    assert (
        "No recent drain outcomes are available. This is not the same as "
        "healthy: the schedule may never have fired, or the executions may "
        "have aged out of retention." in rendered
    )
    assert OUTAGE_SENTENCE_FRAGMENT not in rendered
    assert RATIO_FRAGMENT not in rendered


# --- AC-3 / AC-4: drains_failing's meaning and the insert-position lookup. -


def test_drains_failing_boolean_meaning_is_unchanged_trailing_only():
    # Kept deliberately as "at least one trailing failure", independent of
    # the severity wording change above -- see the property's docstring.
    all_failed_but_short_window = status_with([outcome("drain-0", "FAILED")])
    partial = status_with(
        [outcome(f"drain-{i}", "COMPLETED") for i in range(9)]
        + [outcome("drain-9", "FAILED")]
    )

    assert all_failed_but_short_window.drains_failing is True
    assert partial.drains_failing is True


def test_operator_diagnostic_insert_lands_after_drains_failing_not_at_the_end():
    """schedule_status._operator_diagnostic_insert_index must find a real
    index, not silently fall back to end-of-list. The ratio wording added
    here left `drains_failing: <bool>`'s own line untouched, so the lookup
    still matches -- this pins that so a future reword of that specific line
    is the thing that has to notice and update the lookup, not discover it
    by insert position silently degrading.
    """
    recent = [outcome(f"drain-{i}", "COMPLETED") for i in range(9)]
    recent.append(outcome("drain-9", "FAILED"))
    status = status_with(recent)
    queue = WaitingQueueStatus(
        claimable_count=2,
        oldest_waiting_age=None,
        non_claimable_pending_count=0,
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        queue_status=queue,
        now=NOW,
    )

    lines = rendered.splitlines()
    drains_failing_index = lines.index("drains_failing: true")
    queue_index = lines.index("waiting_claimable_tasks: 2")

    assert queue_index == drains_failing_index + 1
    # If the lookup had fallen back to len(lines), the queue line would be
    # the very last line, after the ratio sentence and "Failed firings:".
    assert lines[-1] != "waiting_claimable_tasks: 2"
    assert lines.index("Failed firings:") > queue_index


def test_insert_index_helper_finds_the_real_index_directly():
    status = status_with([outcome("drain-0", "FAILED")])
    lines = render(status).splitlines()

    index = schedule_status._operator_diagnostic_insert_index(lines, status)

    assert lines[index - 1] == "drains_failing: true"
    assert index != len(lines)


def test_insert_index_helper_falls_back_only_when_the_line_is_truly_absent():
    status = status_with([outcome("drain-0", "FAILED")])

    index = schedule_status._operator_diagnostic_insert_index([], status)

    assert index == 0


# --- GATE FINDING 1/2 (#932): the near-total end of the window ------------
#
# The cases above stop at 3 of 10. That gap is what let a categorical denial of
# the outage ship unnoticed: a total outage climbs 1/10 -> 9/10 before it ever
# reaches 10/10, so every ratio short of the window is a possible outage in
# progress, and the branch that renders them must not rule one out.


def _failing_tail(failed: int, total: int) -> list[DrainOutcome]:
    """`consecutive_failures` walks `reversed(recent)`, so the failures go LAST.

    Spelled out because building this list the other way round yields
    `consecutive_failures == 0`, no banner at all, and a test that passes while
    measuring nothing -- which is exactly how the first reproduction of this
    finding went wrong.
    """
    return [outcome(f"ok-{i}", "COMPLETED") for i in range(total - failed)] + [
        outcome(f"bad-{i}", "FAILED") for i in range(failed)
    ]


def test_nine_of_ten_failing_does_not_deny_the_outage():
    status = status_with(_failing_tail(9, 10))
    assert status.consecutive_failures == 9, "fixture built backwards"

    rendered = render(status)

    assert "9 of the last 10 scheduled drain(s) failed" in rendered
    # The banner may not rule out the very shape it exists to announce.
    assert "outage shape" not in rendered
    # Nor may the reassurance return in any casing -- the guard in
    # test_schedule_drain_health.py pins one exact string, and a differently
    # cased copy slipped past it once already.
    assert "recent drains are completing" not in rendered.lower()


def test_seven_of_ten_failing_does_not_deny_the_outage_either():
    status = status_with(_failing_tail(7, 10))
    assert status.consecutive_failures == 7, "fixture built backwards"

    rendered = render(status)

    assert "7 of the last 10 scheduled drain(s) failed" in rendered
    assert "outage shape" not in rendered
    assert "recent drains are completing" not in rendered.lower()


def test_the_all_failing_case_is_still_the_loud_one():
    """The control for the two above: softening every ratio uniformly would
    satisfy them both and destroy the banner (AC-2)."""
    status = status_with(_failing_tail(10, 10))

    rendered = render(status)

    assert "FACTORY IS FAILING, NOT IDLE" in rendered
    assert "every attempt is dying" in rendered
