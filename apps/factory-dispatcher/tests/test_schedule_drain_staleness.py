"""A schedule firing into an unreachable Temporal must not read as quiet.

On 2026-08-25 the schedule kept firing for 13h23m while the worker's tunnel
was down. `drains_failing` stayed false because no drain had *failed* — the
one workflow that started never finished, so nothing terminal was ever
recorded. This is a second, independent signal: how long since anything
last succeeded, compared against the schedule's own interval, so a schedule
firing into a dead Temporal is distinguishable from a genuinely quiet queue
with nothing to do.

These tests require no Temporal server, no substrate and no network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
from schedule_runtime import DrainOutcome, FactoryScheduleStatus, InFlightWorkflow  # noqa: E402

SCHEDULE_ID = "factory-dispatcher-dev"
INTERVAL_SECONDS = 15 * 60  # matches config.DEFAULT_DISPATCH_INTERVAL_SECONDS


def test_no_successful_drain_for_multiple_intervals_is_reported_stale():
    # The 2026-08-25 shape: one workflow scheduled at 11:45Z, still running
    # (SKIP overlap swallows every firing behind it), nothing has ever
    # completed, and it is now 01:08Z the next calendar day.
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

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert "drains_stale: true" in rendered
    assert "SCHEDULE STALE" in rendered


def test_a_recently_succeeded_schedule_is_not_stale():
    now = datetime(2026, 8, 14, 13, 20, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=(
            DrainOutcome(
                workflow_id="drain-1",
                scheduled_at="2026-08-14T13:15:00Z",
                status="COMPLETED",
            ),
        ),
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert "drains_stale: false" in rendered
    assert "SCHEDULE STALE" not in rendered


def test_staleness_is_not_rendered_when_interval_seconds_is_not_supplied():
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=True,
        in_flight=(),
        recent=(),
    )

    rendered = schedule_status.render_schedule_status(status, namespace="dev")

    assert "drains_stale" not in rendered


def test_no_history_at_all_cannot_be_judged_stale_but_is_not_hidden_as_healthy():
    # Nothing to compute a reference point from: not a false positive, but
    # the existing "No recent drain outcomes are available" language already
    # covers "this is not the same as healthy" for that case.
    now = datetime(2026, 8, 14, 13, 20, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=(),
    )

    rendered = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )

    assert "drains_stale: false" in rendered
    assert "drains_stale_since: unknown" in rendered


def test_stale_drain_multiple_is_configurable():
    now = datetime(2026, 8, 14, 14, 20, tzinfo=timezone.utc)
    status = FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=(
            DrainOutcome(
                workflow_id="drain-1",
                scheduled_at="2026-08-14T13:15:00Z",
                status="COMPLETED",
            ),
        ),
    )
    # last success was 65 minutes ago == 4.33x the 15m interval.

    default_multiple = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        interval_seconds=INTERVAL_SECONDS,
        now=now,
    )
    assert "drains_stale: true" in default_multiple

    generous_multiple = schedule_status.render_schedule_status(
        status,
        namespace="dev",
        interval_seconds=INTERVAL_SECONDS,
        stale_drain_multiple=10,
        now=now,
    )
    assert "drains_stale: false" in generous_multiple
