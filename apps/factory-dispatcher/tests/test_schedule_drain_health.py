"""A failed drain must be visible to the operator check.

`schedule_status.py` answered "is the factory quiet?" with the schedule's
paused flag and its in-flight executions. On 2026-08-06 that answered "not
quiet, the schedule can start workflows" — technically true, and it was the
workflows that were dying. The check could not tell a working factory from a
dead one, which is why the outage took three hours to find.

These tests require no Temporal server, no substrate and no network: the
schedule description is a fixture and the execution status is injected.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_runtime  # noqa: E402

SCHEDULE_ID = "factory-dispatcher-dev"


class FakeScheduleHandle:
    def __init__(self, paused, recent_actions):
        self._paused = paused
        self._recent_actions = recent_actions

    async def describe(self):
        return SimpleNamespace(
            schedule=SimpleNamespace(state=SimpleNamespace(paused=self._paused)),
            info=SimpleNamespace(
                running_actions=(),
                recent_actions=self._recent_actions,
            ),
        )


class FakeClient:
    def __init__(self, paused=False, recent_actions=()):
        self.handle = FakeScheduleHandle(paused, tuple(recent_actions))

    def get_schedule_handle(self, schedule_id):
        return self.handle


def action(workflow_id, scheduled_at, first_execution_run_id=""):
    return SimpleNamespace(
        workflow_id=workflow_id,
        first_execution_run_id=first_execution_run_id,
        scheduled_at=scheduled_at,
    )


def statuses(mapping):
    """Injected lookup: workflow id -> execution status."""

    async def lookup(_client, workflow_id, _run_id):
        return mapping[workflow_id]

    return lookup


def statuses_by_execution(mapping):
    """Injected lookup: (workflow id, run id) -> execution status."""

    async def lookup(_client, workflow_id, run_id):
        return mapping[(workflow_id, run_id)]

    return lookup


def describe(client, lookup):
    return asyncio.run(
        schedule_runtime.describe_factory_schedule_status(
            client,
            schedule_id=SCHEDULE_ID,
            status_lookup=lookup,
        )
    )


# The 2026-08-06 signature: five firings, all dead, schedule unpaused.
FAILING_HISTORY = [
    action("factory-dispatcher-2026-08-06-21-30-00", "2026-08-06T21:30:00Z"),
    action("factory-dispatcher-2026-08-06-21-45-00", "2026-08-06T21:45:00Z"),
    action("factory-dispatcher-2026-08-06-22-00-00", "2026-08-06T22:00:00Z"),
    action("factory-dispatcher-2026-08-06-22-15-00", "2026-08-06T22:15:00Z"),
    action("factory-dispatcher-2026-08-07-00-30-00", "2026-08-07T00:30:00Z"),
]
ALL_FAILED = statuses({a.workflow_id: "FAILED" for a in FAILING_HISTORY})


def test_status_reports_failures_when_every_recent_drain_died():
    status = describe(FakeClient(recent_actions=FAILING_HISTORY), ALL_FAILED)

    assert status.recent_failures == 5
    assert status.consecutive_failures == 5
    assert status.drains_failing is True
    assert status.last_success_at == ""


def test_render_does_not_report_health_when_every_recent_drain_died():
    status = describe(FakeClient(recent_actions=FAILING_HISTORY), ALL_FAILED)

    rendered = schedule_runtime.render_factory_schedule_status(status, namespace="dev")

    assert "FACTORY IS FAILING, NOT IDLE" in rendered
    assert "consecutive_failures: 5" in rendered
    assert "drains_failing: true" in rendered
    # The line that read as reassurance during the outage must no longer
    # stand on its own as the last word.
    assert "Recent drains are completing" not in rendered
    assert "factory-dispatcher-2026-08-07-00-30-00" in rendered


def test_the_failure_verdict_is_the_last_thing_the_operator_reads():
    status = describe(FakeClient(recent_actions=FAILING_HISTORY), ALL_FAILED)

    rendered = schedule_runtime.render_factory_schedule_status(status, namespace="dev")
    body = rendered[rendered.index("FACTORY IS FAILING, NOT IDLE") :]

    # The last line is read as the verdict. During the outage it was
    # "schedule is unpaused and can start future workflows", which is true and
    # was taken as reassurance. Nothing reassuring may follow the failure.
    assert "schedule is unpaused and can start future workflows" not in body


def test_healthy_history_reports_no_failure_so_the_check_cannot_cry_wolf():
    history = [
        action("drain-1", "2026-08-08T10:00:00Z"),
        action("drain-2", "2026-08-08T10:15:00Z"),
        action("drain-3", "2026-08-08T10:30:00Z"),
    ]
    status = describe(
        FakeClient(recent_actions=history),
        statuses({a.workflow_id: "COMPLETED" for a in history}),
    )
    rendered = schedule_runtime.render_factory_schedule_status(status, namespace="dev")

    assert status.recent_failures == 0
    assert status.drains_failing is False
    assert status.last_success_at == "2026-08-08T10:30:00Z"
    assert "FACTORY IS FAILING" not in rendered
    assert "Recent drains are completing" in rendered


def test_an_old_failure_since_recovered_is_not_an_outage():
    history = [
        action("drain-1", "2026-08-08T10:00:00Z"),
        action("drain-2", "2026-08-08T10:15:00Z"),
        action("drain-3", "2026-08-08T10:30:00Z"),
    ]
    status = describe(
        FakeClient(recent_actions=history),
        statuses({"drain-1": "FAILED", "drain-2": "COMPLETED", "drain-3": "COMPLETED"}),
    )

    # One failure is in the history and is counted, but the factory recovered.
    # Reporting this as an outage is how an operator learns to ignore it.
    assert status.recent_failures == 1
    assert status.consecutive_failures == 0
    assert status.drains_failing is False


def test_retry_that_failed_first_run_and_completed_final_run_is_not_a_failing_drain():
    history = [
        action(
            "factory-dispatcher-2026-08-14-13-00-00",
            "2026-08-14T13:00:00Z",
        ),
        action(
            "factory-dispatcher-2026-08-14-13-15-00",
            "2026-08-14T13:15:00Z",
            first_execution_run_id="01a00069-failed-first-run",
        )
    ]

    status = describe(
        FakeClient(recent_actions=history),
        statuses_by_execution(
            {
                (
                    "factory-dispatcher-2026-08-14-13-15-00",
                    "01a00069-failed-first-run",
                ): "FAILED",
                ("factory-dispatcher-2026-08-14-13-00-00", ""): "COMPLETED",
                ("factory-dispatcher-2026-08-14-13-15-00", ""): "COMPLETED",
            }
        ),
    )

    assert status.recent_failures == 0
    assert status.consecutive_failures == 0
    assert status.drains_failing is False
    assert status.last_success_at == "2026-08-14T13:15:00Z"


def test_workflow_whose_final_run_failed_is_still_counted_as_a_failing_drain():
    history = [
        action(
            "factory-dispatcher-2026-08-14-13-15-00",
            "2026-08-14T13:15:00Z",
            first_execution_run_id="01a00069-first-run",
        )
    ]

    status = describe(
        FakeClient(recent_actions=history),
        statuses_by_execution(
            {
                ("factory-dispatcher-2026-08-14-13-15-00", "01a00069-first-run"): (
                    "FAILED"
                ),
                ("factory-dispatcher-2026-08-14-13-15-00", ""): "FAILED",
            }
        ),
    )

    assert status.recent_failures == 1
    assert status.consecutive_failures == 1
    assert status.drains_failing is True
    assert status.last_success_at == ""


def test_failures_after_a_success_report_the_last_good_drain():
    history = [
        action("drain-1", "2026-08-08T10:00:00Z"),
        action("drain-2", "2026-08-08T10:15:00Z"),
        action("drain-3", "2026-08-08T10:30:00Z"),
    ]
    status = describe(
        FakeClient(recent_actions=history),
        statuses({"drain-1": "COMPLETED", "drain-2": "FAILED", "drain-3": "FAILED"}),
    )
    rendered = schedule_runtime.render_factory_schedule_status(status, namespace="dev")

    assert status.consecutive_failures == 2
    assert status.last_success_at == "2026-08-08T10:00:00Z"
    assert "last_success: 2026-08-08T10:00:00Z" in rendered


def test_a_running_drain_is_neither_a_failure_nor_a_success():
    history = [action("drain-1", "2026-08-08T10:00:00Z")]
    status = describe(FakeClient(recent_actions=history), statuses({"drain-1": "RUNNING"}))

    assert status.recent_failures == 0
    assert status.drains_failing is False
    assert status.last_success_at == ""


def test_no_recent_history_is_reported_as_unknown_not_as_healthy():
    status = describe(FakeClient(recent_actions=()), statuses({}))
    rendered = schedule_runtime.render_factory_schedule_status(status, namespace="dev")

    assert status.recent == ()
    assert "No recent drain outcomes are available" in rendered
    assert "This is not the same as healthy" in rendered


def test_an_undeterminable_execution_is_counted_as_a_failure():
    history = [action("drain-1", "2026-08-08T10:00:00Z")]

    async def lookup_fails(_client, _workflow_id, _run_id):
        return ""

    status = describe(FakeClient(recent_actions=history), lookup_fails)

    assert status.recent_failures == 1
    assert status.consecutive_failures == 1
    assert status.drains_failing is True


def test_quiet_case_exit_semantics_are_unchanged():
    # The existing contract: genuinely_quiet means paused with nothing in
    # flight, and schedule_status exits 0 on it. Drain health is reported
    # alongside, and does not redefine it.
    paused = describe(FakeClient(paused=True, recent_actions=()), statuses({}))
    assert paused.genuinely_quiet is True

    still_quiet = describe(
        FakeClient(paused=True, recent_actions=FAILING_HISTORY), ALL_FAILED
    )
    assert still_quiet.genuinely_quiet is True
    assert still_quiet.drains_failing is True
