"""In-flight schedule status must distinguish busy from wedged.

These tests exercise the operator entry point without Temporal. The schedule
description is represented by the same value objects `schedule_status.py`
receives from `schedule_runtime`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import schedule_status  # noqa: E402
from schedule_runtime import FactoryScheduleStatus, InFlightWorkflow  # noqa: E402

NOW = datetime(2026, 8, 13, 17, 44, tzinfo=timezone.utc)
SCHEDULE_ID = "factory-dispatcher-dev"


def status_with_in_flight(*workflows):
    return FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=tuple(workflows),
        recent=(),
    )


def test_old_in_flight_execution_reports_age_and_wedged_status():
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-14-00-00",
            run_id="run-wedged",
            started_at="2026-08-13T14:00:00Z",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "in_flight_workflows: 1" in rendered
    assert "in_flight_wedged: true" in rendered
    assert "in_flight_wedged_workflows: 1" in rendered
    assert (
        f"in_flight_wedged_threshold_minutes: "
        f"{dispatch.DEFAULT_STUCK_THRESHOLD_MINUTES}"
    ) in rendered
    assert "WEDGED IN-FLIGHT WORKFLOW: 1 execution over 45m threshold." in rendered
    assert "workflow_id=factory-dispatcher-2026-08-13-14-00-00" in rendered
    assert "running_for=3h 44m" in rendered
    assert "wedged=true" in rendered


def test_recent_in_flight_execution_reports_age_without_wedged_status():
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-17-42-30",
            run_id="run-recent",
            started_at="2026-08-13T17:42:30Z",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "in_flight_workflows: 1" in rendered
    assert "in_flight_wedged: false" in rendered
    assert "in_flight_wedged_workflows: 0" in rendered
    assert "WEDGED IN-FLIGHT WORKFLOW" not in rendered
    assert "running_for=1m" in rendered
    assert "wedged=false" in rendered


def test_no_in_flight_output_is_unchanged_from_runtime_renderer():
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=True,
        in_flight=(),
        recent=(),
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert rendered == schedule_status.render_factory_schedule_status(
        status,
        namespace="dev",
    )
    assert "in_flight_wedged" not in rendered


def test_in_flight_wedged_threshold_uses_dispatch_stuck_threshold():
    assert (
        schedule_status.DEFAULT_STUCK_THRESHOLD_MINUTES
        == dispatch.DEFAULT_STUCK_THRESHOLD_MINUTES
    )


def test_in_flight_execution_with_unresolvable_age_reports_cannot_determine_not_wedged():
    """A workflow whose age cannot be measured (no started_at or scheduled_at)
    must not be asserted as a measured over-threshold wedge -- that is a false
    alarm wearing a measurement's clothes. It gets its own honest,
    still-prominent cannot-determine line instead, and is never counted or
    printed as wedged=true (a fresh workflow reads as fresh, not stuck) nor as
    wedged=false (which would silently read as healthy, the 2026-08-25 hole:
    OPS-15, #524).
    """
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-24-11-45-00",
            run_id="run-unresolvable",
            started_at="",
            scheduled_at="",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "in_flight_wedged: false" in rendered
    assert "in_flight_wedged_workflows: 0" in rendered
    assert "in_flight_cannot_determine_workflows: 1" in rendered
    assert "WEDGED IN-FLIGHT WORKFLOW" not in rendered
    assert "CANNOT DETERMINE AGE: 1 in-flight execution" in rendered
    assert "factory-dispatcher-2026-08-24-11-45-00" in rendered
    assert "running_for=unknown" in rendered
    assert "wedged=cannot-determine" in rendered
    assert "wedged=true" not in rendered


def test_fresh_workflow_with_no_timestamps_yet_is_cannot_determine_not_wedged():
    """A brand-new workflow execution simply may not have started_at or
    scheduled_at populated yet. That must read as cannot-determine, never as
    a wedge -- this is the concrete "fresh workflow" scenario the
    cannot-determine state exists for.
    """
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-17-44-00",
            run_id="run-fresh-unknown",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "in_flight_wedged: false" in rendered
    assert "in_flight_wedged_workflows: 0" in rendered
    assert "in_flight_cannot_determine_workflows: 1" in rendered
    assert "WEDGED IN-FLIGHT WORKFLOW" not in rendered
    assert "CANNOT DETERMINE AGE" in rendered


def test_unparseable_timestamp_is_named_as_present_not_claimed_absent():
    """The cannot-determine line must name the actual failure: a started_at
    that is present but unparseable is not "no started_at" -- claiming a
    field is absent while printing its value two lines below would be the
    same asserted-non-measurement this line exists to end.
    """
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-24-11-45-00",
            run_id="run-garbage-timestamp",
            started_at="not-a-time",
            scheduled_at="",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "in_flight_cannot_determine_workflows: 1" in rendered
    assert "timestamp present but unparseable: 'not-a-time'" in rendered
    assert "no started_at or scheduled_at" not in rendered
    assert "wedged=cannot-determine" in rendered


def test_announce_wedged_dispatch_workflows_announces_each_wedged_workflow_once():
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-14-00-00",
            run_id="run-wedged",
            started_at="2026-08-13T14:00:00Z",
        )
    )
    calls: list[tuple[str, int, int]] = []

    def fake_announce(workflow_id, running_for_minutes, threshold_minutes):
        calls.append((workflow_id, running_for_minutes, threshold_minutes))
        return True

    announced = schedule_status.announce_wedged_dispatch_workflows(
        status, now=NOW, announce=fake_announce
    )

    assert announced == ["factory-dispatcher-2026-08-13-14-00-00"]
    assert calls == [
        (
            "factory-dispatcher-2026-08-13-14-00-00",
            224,  # 3h44m, floored to minutes
            dispatch.DEFAULT_STUCK_THRESHOLD_MINUTES,
        )
    ]


def test_announce_wedged_dispatch_workflows_does_not_announce_healthy_or_undetermined():
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-17-42-30",
            run_id="run-recent",
            started_at="2026-08-13T17:42:30Z",
        ),
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-24-11-45-00",
            run_id="run-unresolvable",
            started_at="",
            scheduled_at="",
        ),
    )
    calls: list[tuple[str, int, int]] = []

    def fake_announce(workflow_id, running_for_minutes, threshold_minutes):
        calls.append((workflow_id, running_for_minutes, threshold_minutes))
        return True

    announced = schedule_status.announce_wedged_dispatch_workflows(
        status, now=NOW, announce=fake_announce
    )

    assert announced == []
    assert calls == []


def test_announce_wedged_dispatch_workflows_defaults_to_the_real_declared_alert(monkeypatch):
    """No injected announce: the real failure_diagnosis.announce_schedule_wedged
    is what fires in production, not a bespoke send."""
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-14-00-00",
            run_id="run-wedged",
            started_at="2026-08-13T14:00:00Z",
        )
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        schedule_status.failure_diagnosis,
        "announce_schedule_wedged",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    announced = schedule_status.announce_wedged_dispatch_workflows(status, now=NOW)

    assert announced == ["factory-dispatcher-2026-08-13-14-00-00"]
    assert len(calls) == 1


def test_absent_timestamps_are_named_as_absent():
    status = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-24-11-45-00",
            run_id="run-no-timestamps",
            started_at="",
            scheduled_at="",
        )
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        now=NOW,
    )

    assert "no started_at or scheduled_at" in rendered
    assert "unparseable" not in rendered

def test_main_actually_calls_the_wedge_announcer(monkeypatch):
    """Release-gate F2 (#896): pin the announcer's ONLY production call site.

    `announce_wedged_dispatch_workflows` had three tests and its alert id resolves in the real
    inventory -- but nothing pinned that production ever calls it. Deleting the
    `asyncio.to_thread(announce_wedged_dispatch_workflows, ...)` block from `_main` left the whole
    suite green (1869 passed), which means the one line that makes this bead's alerting real was
    unguarded. A function that exists, is tested, is registered, and is never called is the shape
    this repository keeps finding.

    This drives `_main` with every external collaborator stubbed -- no Temporal, no network -- and
    asserts the announcer is invoked exactly once with the status `_main` computed.
    """
    import asyncio
    import types

    wedged = status_with_in_flight(
        InFlightWorkflow(
            workflow_id="factory-dispatcher-2026-08-13-14-00-00",
            run_id="run-wedged",
            started_at="2026-08-13T14:00:00Z",
        )
    )

    fake_client = object()

    class _FakeClient:
        @staticmethod
        async def connect(_address, namespace=None):
            return fake_client

    monkeypatch.setitem(
        sys.modules,
        "temporalio.client",
        types.SimpleNamespace(Client=_FakeClient),
    )

    async def fake_describe(_client, *, schedule_id):
        return wedged

    monkeypatch.setattr(schedule_status, "describe_factory_schedule_status", fake_describe)

    async def fake_pause_state(_client, *, schedule_id):
        return False, ""

    monkeypatch.setattr(schedule_status, "dispatch_schedule_pause_state", fake_pause_state)
    for name in (
        "describe_waiting_queue",
        "describe_worker_revision_drift",
        "describe_base_ref_status",
        "describe_tunnel_keeper_status",
    ):
        # Not None: _main now reads `.error` off each of these (building
        # IdleInputs for describe_idle_reason) before ever reaching
        # render_schedule_status, which is what this test actually exercises.
        monkeypatch.setattr(schedule_status, name, lambda *a, **k: types.SimpleNamespace(error="stub"))
    monkeypatch.setattr(schedule_status, "render_schedule_status", lambda *a, **k: "")
    monkeypatch.setattr(schedule_status, "factory_health_notifications", lambda *a, **k: [])
    monkeypatch.setattr(schedule_status.cluster_health, "notify", lambda *a, **k: None)
    monkeypatch.setattr(schedule_status.cluster_health, "poster_from_env", lambda *a, **k: None)

    calls = []

    def fake_announce(status, **kwargs):
        calls.append(status)
        return ["factory-dispatcher-2026-08-13-14-00-00"]

    monkeypatch.setattr(schedule_status, "announce_wedged_dispatch_workflows", fake_announce)

    asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
        )
    )

    assert len(calls) == 1, "_main must call the wedge announcer exactly once"
    assert calls[0] is wedged, "the announcer must receive the status _main actually measured"

