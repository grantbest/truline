"""Tests for turning a probed capacity pause into action (OPS-66, 2026-09-06).

The capacity-pause schedule note reading "Unpaused by outer loop at the Operator's direction
2026-09-03: probing whether the usage window reset" was a person guessing at a clock.
`capacity_pause_response.py` is the missing action half: given the schedule's current pause note
and a way to probe whether the usage window has demonstrably reset, it must always choose exactly
one of resume, wait, or leave-an-operator's-pause-alone -- never a fourth "log it and move on".

No Temporal server, no substrate, no network, no real worker invocation: the probe, the resume,
and the announce are all injected fakes, matching `test_worker_checkout_drift_response.py`'s
approach for the drift-response half this mirrors.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capacity_pause_response as pause_response  # noqa: E402
import schedule_runtime  # noqa: E402

OUR_NOTE = schedule_runtime.capacity_pause_note("worker reported usage limit exhaustion")
MANUAL_NOTE = "paused by the Operator for maintenance"


def respond(
    note,
    *,
    probe,
    resumed=None,
    announced=None,
):
    resumed = resumed if resumed is not None else []
    announced = announced if announced is not None else []
    return (
        pause_response.respond_to_capacity_pause(
            note,
            probe=probe,
            resume_schedule=resumed.append,
            announce_resume=announced.append,
        ),
        resumed,
        announced,
    )


def unreachable_probe():
    raise AssertionError("probe must not be called for a pause this mechanism does not own")


# ---------------------------------------------------------------------------
# AC3: an operator's own pause (or no pause at all) is never touched.
# ---------------------------------------------------------------------------


def test_a_manually_paused_note_is_not_ours_and_touches_no_collaborator():
    result, resumed, announced = respond(MANUAL_NOTE, probe=unreachable_probe)

    assert result.outcome == pause_response.NOT_OURS
    assert resumed == []
    assert announced == []


def test_a_manual_pause_stays_not_ours_across_repeated_probes():
    for _ in range(3):
        result, resumed, announced = respond(MANUAL_NOTE, probe=unreachable_probe)
        assert result.outcome == pause_response.NOT_OURS
        assert resumed == []
        assert announced == []


def test_an_empty_note_is_not_ours():
    result, resumed, announced = respond("", probe=unreachable_probe)

    assert result.outcome == pause_response.NOT_OURS
    assert resumed == []
    assert announced == []


# ---------------------------------------------------------------------------
# AC1/AC2: our pause, probe confirms reset -> resume, announce, name the evidence.
# ---------------------------------------------------------------------------


def test_our_pause_with_a_confirmed_reset_resumes_and_announces_with_the_evidence():
    def confirmed_reset_probe():
        return pause_response.CapacityProbeResult(
            reset=True, evidence="live probe completed with no capacity failure detected"
        )

    result, resumed, announced = respond(OUR_NOTE, probe=confirmed_reset_probe)

    assert result.outcome == pause_response.RESUMED
    assert "capacity-pause resume responder" in result.detail
    assert "live probe completed with no capacity failure detected" in result.detail
    assert resumed == [result.detail]
    assert announced == [result.detail]


# ---------------------------------------------------------------------------
# AC2/AC4: our pause, probe finds it still exhausted, or cannot tell -- STILL_EXHAUSTED
# either way, and the inability is stated rather than a guess resolved toward resuming.
# ---------------------------------------------------------------------------


def test_our_pause_with_a_confirmed_still_exhausted_probe_stays_paused():
    def still_exhausted_probe():
        return pause_response.CapacityProbeResult(
            reset=False, evidence="live probe still reports usage limit exhaustion"
        )

    result, resumed, announced = respond(OUR_NOTE, probe=still_exhausted_probe)

    assert result.outcome == pause_response.STILL_EXHAUSTED
    assert "usage limit exhaustion" in result.detail
    assert resumed == []
    assert announced == []


def test_a_probe_that_cannot_evaluate_reports_still_exhausted_with_the_inability_stated():
    def unevaluable_probe():
        return pause_response.CapacityProbeResult(
            reset=None, evidence="probe invocation raised a timeout after 2 minutes"
        )

    result, resumed, announced = respond(OUR_NOTE, probe=unevaluable_probe)

    assert result.outcome == pause_response.STILL_EXHAUSTED
    assert "could not evaluate" in result.detail
    assert "probe invocation raised a timeout after 2 minutes" in result.detail
    assert resumed == []  # never resumed on an unevaluable guess
    assert announced == []


def test_could_not_evaluate_and_confirmed_still_exhausted_are_distinguishable_in_the_detail():
    def unevaluable_probe():
        return pause_response.CapacityProbeResult(reset=None, evidence="reason A")

    def still_exhausted_probe():
        return pause_response.CapacityProbeResult(reset=False, evidence="reason B")

    cannot_evaluate, _, _ = respond(OUR_NOTE, probe=unevaluable_probe)
    still_exhausted, _, _ = respond(OUR_NOTE, probe=still_exhausted_probe)

    assert cannot_evaluate.outcome == still_exhausted.outcome == pause_response.STILL_EXHAUSTED
    assert cannot_evaluate.detail != still_exhausted.detail
    assert "could not evaluate" in cannot_evaluate.detail
    assert "could not evaluate" not in still_exhausted.detail


# ---------------------------------------------------------------------------
# The outcome is always one of the three declared values.
# ---------------------------------------------------------------------------


def test_response_outcome_is_always_one_of_the_three_declared_outcomes():
    def reset_probe():
        return pause_response.CapacityProbeResult(reset=True, evidence="reset")

    def exhausted_probe():
        return pause_response.CapacityProbeResult(reset=False, evidence="exhausted")

    def unknown_probe():
        return pause_response.CapacityProbeResult(reset=None, evidence="unknown")

    for note, probe in (
        (MANUAL_NOTE, unreachable_probe),
        (OUR_NOTE, reset_probe),
        (OUR_NOTE, exhausted_probe),
        (OUR_NOTE, unknown_probe),
    ):
        result, _, _ = respond(note, probe=probe)
        assert result.outcome in {
            pause_response.NOT_OURS,
            pause_response.STILL_EXHAUSTED,
            pause_response.RESUMED,
        }
        assert result.detail  # a human-readable reason is always present
