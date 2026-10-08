"""Tests for the scheduled capacity-pause resume probe's wiring (OPS-66, 2026-09-06).

`capacity_pause_response.py` (tested directly in test_capacity_pause_response.py) is the pure
decision logic. This file covers the production wiring around it:
`activities/capacity_pause_response.py`'s activity, and its registration with the Temporal
worker -- mirroring test_worker_revision_drift_schedule.py's own wiring-test shape for the drift
check this task is the resume-half counterpart to.

No Temporal server, no substrate, no network, no real worker invocation: every collaborator the
activity would otherwise reach for is monkeypatched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import capacity_pause_response as acpr  # noqa: E402
import capacity_pause_response as pause_response  # noqa: E402
import schedule_runtime  # noqa: E402

OUR_NOTE = schedule_runtime.capacity_pause_note("worker reported usage limit exhaustion")
MANUAL_NOTE = "paused by the Operator for maintenance"


# ---------------------------------------------------------------------------
# wiring: registered with the Temporal worker
# ---------------------------------------------------------------------------


def test_capacity_pause_resume_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.capacity_pause_response import CapacityPauseResumeWorkflow

    definition = temporal_workflow._Definition.from_class(CapacityPauseResumeWorkflow)
    assert definition.name == "CapacityPauseResumeWorkflow"
    assert "CapacityPauseResumeWorkflow" in inspect.getsource(worker.build_worker)


def test_capacity_pause_resume_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "probe_capacity_pause_resume_activity" in names


# ---------------------------------------------------------------------------
# activity: short-circuits when there is nothing to probe
# ---------------------------------------------------------------------------


def test_activity_reports_a_no_op_when_the_schedule_is_not_paused(monkeypatch):
    monkeypatch.setattr(acpr, "default_dispatch_schedule_pause_state", lambda: (False, ""))

    def unreachable_probe():
        raise AssertionError("must not probe when the schedule is not paused")

    monkeypatch.setattr(acpr, "default_capacity_window_probe", unreachable_probe)
    monkeypatch.setattr(
        acpr,
        "default_resume_schedule",
        lambda note: (_ for _ in ()).throw(AssertionError("must not resume")),
    )
    monkeypatch.setattr(
        acpr,
        "default_announce_resume",
        lambda note: (_ for _ in ()).throw(AssertionError("must not announce")),
    )

    result = acpr.probe_capacity_pause_resume_activity()

    assert result["outcome"] == "schedule_not_paused"
    assert result["outcome"] not in {
        pause_response.RESUMED,
        pause_response.STILL_EXHAUSTED,
        pause_response.NOT_OURS,
    }


# ---------------------------------------------------------------------------
# activity: delegates the three-outcome decision to the pure responder
# ---------------------------------------------------------------------------


def test_activity_reports_not_ours_for_a_manual_pause_and_touches_no_collaborator(monkeypatch):
    monkeypatch.setattr(acpr, "default_dispatch_schedule_pause_state", lambda: (True, MANUAL_NOTE))

    def unreachable_probe():
        raise AssertionError("must not probe a pause this mechanism does not own")

    monkeypatch.setattr(acpr, "default_capacity_window_probe", unreachable_probe)
    monkeypatch.setattr(
        acpr,
        "default_resume_schedule",
        lambda note: (_ for _ in ()).throw(AssertionError("must not resume")),
    )
    monkeypatch.setattr(
        acpr,
        "default_announce_resume",
        lambda note: (_ for _ in ()).throw(AssertionError("must not announce")),
    )

    result = acpr.probe_capacity_pause_resume_activity()

    assert result["outcome"] == pause_response.NOT_OURS


def test_activity_resumes_and_announces_when_the_probe_confirms_reset(monkeypatch):
    monkeypatch.setattr(acpr, "default_dispatch_schedule_pause_state", lambda: (True, OUR_NOTE))
    monkeypatch.setattr(
        acpr,
        "default_capacity_window_probe",
        lambda: pause_response.CapacityProbeResult(
            reset=True, evidence="live probe completed with no capacity failure detected"
        ),
    )
    resumed = []
    announced = []
    monkeypatch.setattr(acpr, "default_resume_schedule", resumed.append)
    monkeypatch.setattr(acpr, "default_announce_resume", announced.append)

    result = acpr.probe_capacity_pause_resume_activity()

    assert result["outcome"] == pause_response.RESUMED
    assert len(resumed) == 1
    assert resumed == announced  # the schedule note and the alert agree on what happened


def test_activity_leaves_the_schedule_paused_when_the_probe_finds_it_still_exhausted(monkeypatch):
    monkeypatch.setattr(acpr, "default_dispatch_schedule_pause_state", lambda: (True, OUR_NOTE))
    monkeypatch.setattr(
        acpr,
        "default_capacity_window_probe",
        lambda: pause_response.CapacityProbeResult(
            reset=False, evidence="live probe still reports usage limit exhaustion"
        ),
    )
    monkeypatch.setattr(
        acpr,
        "default_resume_schedule",
        lambda note: (_ for _ in ()).throw(AssertionError("must not resume")),
    )
    monkeypatch.setattr(
        acpr,
        "default_announce_resume",
        lambda note: (_ for _ in ()).throw(AssertionError("must not announce")),
    )

    result = acpr.probe_capacity_pause_resume_activity()

    assert result["outcome"] == pause_response.STILL_EXHAUSTED
    assert "usage limit exhaustion" in result["detail"]


def test_activity_fails_closed_when_the_probe_cannot_evaluate(monkeypatch):
    monkeypatch.setattr(acpr, "default_dispatch_schedule_pause_state", lambda: (True, OUR_NOTE))
    monkeypatch.setattr(
        acpr,
        "default_capacity_window_probe",
        lambda: pause_response.CapacityProbeResult(
            reset=None, evidence="probe invocation could not run: executable not found"
        ),
    )
    monkeypatch.setattr(
        acpr,
        "default_resume_schedule",
        lambda note: (_ for _ in ()).throw(AssertionError("must not resume on an unevaluable probe")),
    )
    monkeypatch.setattr(
        acpr,
        "default_announce_resume",
        lambda note: (_ for _ in ()).throw(AssertionError("must not announce")),
    )

    result = acpr.probe_capacity_pause_resume_activity()

    assert result["outcome"] == pause_response.STILL_EXHAUSTED
    assert "could not evaluate" in result["detail"]
