"""Wedge/staleness faults must notify through cluster_health.py's own path.

Amendment 30: a fault detected and not surfaced through the platform's one
notification path is a fault nobody sees. `schedule_status.py` must reuse
cluster_health.py's Notification/build_payload/notify/poster_from_env rather
than growing a second webhook implementation. These tests build payloads
directly and never construct a poster, so no network call is reachable from
them even accidentally.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cluster_health  # noqa: E402
import schedule_status  # noqa: E402
from schedule_runtime import DrainOutcome, FactoryScheduleStatus, InFlightWorkflow  # noqa: E402

SCHEDULE_ID = "factory-dispatcher-dev"
INTERVAL_SECONDS = 15 * 60


def test_wedged_workflow_with_age_measured_from_scheduled_at_produces_a_notification():
    """Renamed from ..._with_unresolvable_age_...: this fixture's age IS
    measured -- from scheduled_at -- so it exercises the ordinary wedge
    notification, not the cannot-determine state (which deliberately does
    not notify; see the pin test below)."""
    now = datetime(2026, 8, 25, 1, 8, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(
            InFlightWorkflow(
                workflow_id="factory-dispatcher-2026-08-24-11-45-00",
                run_id="run-wedged",
                scheduled_at="2026-08-24T11:45:00Z",
            ),
        ),
        recent=(),
    )

    notifications = schedule_status.factory_health_notifications(
        status,
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert len(notifications) >= 1
    sources = {n.source for n in notifications}
    assert "schedule-wedged" in sources

    # The payload must be constructible with no poster in reach — proves the
    # notification path never has to touch the network to be verified.
    payload = cluster_health.build_payload(notifications)
    assert "schedule-wedged" in payload
    assert "factory-dispatcher-2026-08-24-11-45-00" in payload


def test_stale_drains_with_no_in_flight_workflow_also_notify():
    now = datetime(2026, 8, 25, 1, 8, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=(
            DrainOutcome(
                workflow_id="drain-1",
                scheduled_at="2026-08-24T11:45:00Z",
                status="COMPLETED",
            ),
        ),
    )

    notifications = schedule_status.factory_health_notifications(
        status,
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    sources = {n.source for n in notifications}
    assert "schedule-stale" in sources

    payload = cluster_health.build_payload(notifications)
    assert "schedule-stale" in payload


def test_recovered_healthy_state_produces_no_notification():
    # Fixture of the recovered state: schedule paused, nothing in flight, and
    # the most recent drain completed well inside the interval.
    now = datetime(2026, 8, 25, 1, 30, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=True,
        in_flight=(),
        recent=(
            DrainOutcome(
                workflow_id="drain-recovered",
                scheduled_at="2026-08-25T01:15:00Z",
                status="COMPLETED",
            ),
        ),
    )

    notifications = schedule_status.factory_health_notifications(
        status,
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert notifications == []


def test_a_healthy_recent_in_flight_workflow_produces_no_wedge_notification():
    now = datetime(2026, 8, 25, 1, 30, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(
            InFlightWorkflow(
                workflow_id="factory-dispatcher-2026-08-25-01-29-00",
                run_id="run-fresh",
                started_at="2026-08-25T01:29:00Z",
            ),
        ),
        recent=(
            DrainOutcome(
                workflow_id="drain-1",
                scheduled_at="2026-08-25T01:15:00Z",
                status="COMPLETED",
            ),
        ),
    )

    notifications = schedule_status.factory_health_notifications(
        status,
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert notifications == []


def test_cannot_determine_age_deliberately_produces_no_wedge_notification():
    """Pins the OPS-79 choice: an unmeasurable age raises NO schedule-wedged
    notification. The pre-OPS-79 behavior notified `schedule-wedged` for an
    age never measured -- the false alarm the bead exists to end, live-firing
    on every fresh workflow. The silence is bounded, not absolute: the
    cannot-determine state stays loud in the rendered report (its own
    all-caps line + counter), and a long silent outage is backstopped by the
    schedule-stale detector, which notifies on the drained-schedule shape
    regardless of workflow ages. If a distinct notification source for
    unknown ages is ever wanted, it must be its own honestly-named source
    (e.g. schedule-age-unknown), never schedule-wedged.
    """
    now = datetime(2026, 8, 25, 1, 8, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(
            InFlightWorkflow(
                workflow_id="factory-dispatcher-2026-08-24-11-45-00",
                run_id="run-no-timestamps",
                started_at="",
                scheduled_at="",
            ),
        ),
        recent=(
            DrainOutcome(
                workflow_id="factory-dispatcher-2026-08-25-01-00-00",
                scheduled_at="2026-08-25T01:00:00Z",
                status="COMPLETED",
            ),
        ),
    )

    notifications = schedule_status.factory_health_notifications(
        status,
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert not any(n.source == "schedule-wedged" for n in notifications)
