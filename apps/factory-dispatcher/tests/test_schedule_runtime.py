"""Tests for unattended dispatcher schedule registration."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_runtime  # noqa: E402
from config import DEFAULT_DISPATCH_INTERVAL_SECONDS, Config  # noqa: E402
from retry_policy import DISPATCH_RETRY_MAXIMUM_ATTEMPTS  # noqa: E402


class FakeOverlapPolicy:
    SKIP = "SKIP"
    ALLOW_ALL = "ALLOW_ALL"


@dataclass
class FakeAction:
    workflow: object
    arg: object = None
    id: str = ""
    task_queue: str = ""
    retry_policy: object = None

    def __post_init__(self):
        self.args = [self.arg]


@dataclass
class FakeInterval:
    every: timedelta
    offset: timedelta | None = None


@dataclass
class FakeSpec:
    intervals: list[FakeInterval]


@dataclass
class FakePolicy:
    overlap: object


@dataclass
class FakeState:
    note: str | None = None
    paused: bool = False


@dataclass
class FakeSchedule:
    action: FakeAction
    spec: FakeSpec
    policy: FakePolicy
    state: FakeState


@dataclass
class FakeUpdate:
    schedule: FakeSchedule


class MissingSchedule(Exception):
    pass


class FakeHandle:
    def __init__(self, client):
        self.client = client
        self.updates = []

    async def describe(self):
        if self.client.schedule is None:
            raise MissingSchedule("not found")
        return SimpleNamespace(
            schedule=self.client.schedule,
            info=SimpleNamespace(running_actions=self.client.running_actions),
        )

    async def update(self, updater):
        update_input = SimpleNamespace(
            description=SimpleNamespace(schedule=self.client.schedule)
        )
        update = updater(update_input)
        self.updates.append(update)
        self.client.schedule = update.schedule


class FakeClient:
    def __init__(self, schedule=None, running_actions=()):
        self.schedule = schedule
        self.running_actions = tuple(running_actions)
        self.created = []
        self.handle = FakeHandle(self)

    def get_schedule_handle(self, schedule_id):
        self.schedule_id = schedule_id
        return self.handle

    async def create_schedule(self, schedule_id, schedule):
        self.created.append((schedule_id, schedule))
        self.schedule = schedule


FAKE_TEMPORAL = SimpleNamespace(
    Schedule=FakeSchedule,
    ScheduleActionStartWorkflow=FakeAction,
    ScheduleIntervalSpec=FakeInterval,
    ScheduleOverlapPolicy=FakeOverlapPolicy,
    SchedulePolicy=FakePolicy,
    ScheduleSpec=FakeSpec,
    ScheduleState=FakeState,
    ScheduleUpdate=FakeUpdate,
)


async def fake_workflow(_request=None):
    return {"status": "ok"}


def build_schedule(interval=900, overlap=FakeOverlapPolicy.SKIP, paused=False):
    schedule = schedule_runtime.build_dispatch_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        interval_seconds=interval,
        task_queue="factory-dispatcher-dev",
        paused=paused,
    )
    schedule.policy.overlap = overlap
    return schedule


def test_build_dispatch_schedule_sets_interval_and_explicit_skip_overlap():
    schedule = build_schedule(interval=DEFAULT_DISPATCH_INTERVAL_SECONDS)

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=DEFAULT_DISPATCH_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.DISPATCH_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-dispatcher-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_build_dispatch_schedule_declares_workflow_retry_policy():
    retry = SimpleNamespace(maximum_attempts=DISPATCH_RETRY_MAXIMUM_ATTEMPTS)

    schedule = schedule_runtime.build_dispatch_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        interval_seconds=DEFAULT_DISPATCH_INTERVAL_SECONDS,
        task_queue="factory-dispatcher-dev",
        retry_policy=retry,
    )

    assert schedule.action.retry_policy is retry
    assert schedule.action.retry_policy.maximum_attempts == DISPATCH_RETRY_MAXIMUM_ATTEMPTS


def test_build_staleness_schedule_is_nightly_and_skips_overlap():
    schedule = schedule_runtime.build_staleness_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.STALENESS_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(days=1)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.STALENESS_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-verdict-staleness-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_creates_schedule_when_absent_and_does_not_duplicate_on_restart():
    logger = logging.getLogger("test-register-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-dispatcher-dev",
            interval_seconds=900,
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-dispatcher-dev",
            interval_seconds=900,
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_staleness_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-staleness-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_staleness_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-verdict-staleness-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_staleness_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-verdict-staleness-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_build_ea_apply_schedule_matches_dispatch_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_ea_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(seconds=DEFAULT_DISPATCH_INTERVAL_SECONDS)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.EA_APPLY_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-ea-apply-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_ea_apply_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-ea-apply-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_ea_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-ea-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_ea_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-ea-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_updates_staleness_literal_template_workflow_id_in_place():
    logger = logging.getLogger("test-register-staleness-template-update")
    existing = schedule_runtime.build_staleness_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=True,
    )
    existing.action.id = (
        "factory-verdict-staleness-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_staleness_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-verdict-staleness-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.STALENESS_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    assert client.schedule.state.paused is True


def test_build_ea_observation_schedule_is_nightly_and_skips_overlap():
    schedule = schedule_runtime.build_ea_observation_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_OBSERVATION_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(days=1)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.EA_OBSERVATION_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-ea-observation-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_ea_observation_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-ea-observation-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_ea_observation_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-ea-observation-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_ea_observation_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-ea-observation-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_updates_existing_schedule_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-update")
    existing = build_schedule(interval=60, overlap=FakeOverlapPolicy.ALLOW_ALL, paused=True)
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-dispatcher-dev",
            interval_seconds=900,
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.spec.intervals[0].every == timedelta(seconds=900)
    assert client.schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert client.schedule.state.paused is True


def test_register_updates_literal_template_workflow_id_in_place():
    logger = logging.getLogger("test-register-template-update")
    existing = build_schedule(interval=900, paused=True)
    existing.action.id = (
        "factory-dispatcher-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-dispatcher-dev",
            interval_seconds=900,
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.DISPATCH_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    assert client.schedule.state.paused is True


def test_pause_dispatch_schedule_sets_paused_note_and_preserves_spec():
    existing = build_schedule(interval=900, paused=False)
    client = FakeClient(existing)
    note = schedule_runtime.capacity_pause_note(
        "worker reported usage limit exhaustion; retry_at=Aug 7th 10:43 PM"
    )

    asyncio.run(
        schedule_runtime.pause_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            schedule_id="factory-dispatcher-dev",
            note=note,
        )
    )

    assert len(client.handle.updates) == 1
    assert client.schedule.state.paused is True
    assert client.schedule.state.note == note
    assert "usage limit exhaustion" in client.schedule.state.note
    assert "Aug 7th 10:43 PM" in client.schedule.state.note
    assert client.schedule.spec.intervals[0].every == timedelta(seconds=900)
    assert client.schedule.policy.overlap is FakeOverlapPolicy.SKIP


def test_factory_schedule_status_reports_paused_quiet_against_fake_client():
    client = FakeClient(build_schedule(paused=True))

    status = asyncio.run(
        schedule_runtime.describe_factory_schedule_status(
            client,
            schedule_id="factory-dispatcher-dev",
        )
    )
    rendered = schedule_runtime.render_factory_schedule_status(
        status,
        namespace="dev",
    )

    assert status.genuinely_quiet is True
    assert "paused: true" in rendered
    assert "in_flight_workflows: 0" in rendered
    assert "genuinely_quiet: true" in rendered


def test_factory_schedule_status_says_pause_does_not_stop_in_flight_workflows():
    running = SimpleNamespace(
        workflow_id="factory-dispatcher-2026-08-06-02-21-00",
        first_execution_run_id="run-abc",
    )
    client = FakeClient(build_schedule(paused=True), running_actions=[running])

    status = asyncio.run(
        schedule_runtime.describe_factory_schedule_status(
            client,
            schedule_id="factory-dispatcher-dev",
        )
    )
    rendered = schedule_runtime.render_factory_schedule_status(
        status,
        namespace="dev",
    )

    assert status.genuinely_quiet is False
    assert "paused: true" in rendered
    assert "in_flight_workflows: 1" in rendered
    assert "PAUSE DOES NOT STOP RUNNING WORKFLOWS" in rendered
    assert "workflow_id=factory-dispatcher-2026-08-06-02-21-00" in rendered
    assert "run_id=run-abc" in rendered
    assert "temporal workflow terminate" in rendered


def test_factory_schedule_status_reports_unpaused_as_not_quiet():
    client = FakeClient(build_schedule(paused=False))

    status = asyncio.run(
        schedule_runtime.describe_factory_schedule_status(
            client,
            schedule_id="factory-dispatcher-dev",
        )
    )
    rendered = schedule_runtime.render_factory_schedule_status(
        status,
        namespace="dev",
    )

    assert status.genuinely_quiet is False
    assert "paused: false" in rendered
    assert "in_flight_workflows: 0" in rendered
    assert "Factory is not quiet: schedule is unpaused" in rendered


def test_log_startup_mode_names_interval_overlap_and_paused_state(caplog):
    registration = schedule_runtime.ScheduleRegistration(
        schedule_id="factory-dispatcher-dev",
        interval_seconds=900,
        overlap_policy=FakeOverlapPolicy.SKIP,
        paused=True,
        action="DispatchTaskWorkflow",
        task_queue="factory-dispatcher-dev",
        operation="unchanged",
    )

    with caplog.at_level(logging.WARNING):
        schedule_runtime.log_startup_mode(
            logging.getLogger("test-log-startup"),
            namespace="dev",
            registration=registration,
        )

    message = caplog.messages[0]
    assert "interval=900s" in message
    assert "overlap=SKIP" in message
    assert "paused=True" in message


def test_build_release_apply_schedule_matches_ea_apply_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_release_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.RELEASE_APPLY_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-release-apply-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_release_apply_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-release-apply-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_release_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_release_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_release_apply_schedule_updates_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-release-apply-update")
    existing = schedule_runtime.build_release_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        interval_seconds=60,
        paused=True,
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_release_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True


def test_build_release_status_schedule_is_nightly_and_skips_overlap():
    schedule = schedule_runtime.build_release_status_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.RELEASE_STATUS_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(days=1)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.RELEASE_STATUS_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-release-status-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_release_status_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-release-status-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_release_status_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-status-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_release_status_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-status-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_release_status_schedule_updates_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-release-status-update")
    existing = schedule_runtime.build_release_status_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=True,
    )
    existing.action.id = (
        "factory-release-status-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_release_status_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-release-status-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.RELEASE_STATUS_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True


def test_build_worker_revision_drift_schedule_matches_dispatch_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_worker_revision_drift_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.WORKER_REVISION_DRIFT_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-worker-revision-drift-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_worker_revision_drift_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-worker-revision-drift-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_worker_revision_drift_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-worker-revision-drift-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_worker_revision_drift_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-worker-revision-drift-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_worker_revision_drift_schedule_updates_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-worker-revision-drift-update")
    existing = schedule_runtime.build_worker_revision_drift_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=True,
    )
    existing.action.id = (
        "factory-worker-revision-drift-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_worker_revision_drift_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-worker-revision-drift-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.WORKER_REVISION_DRIFT_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-1/AC-2: the drift schedule runs off the other 15-minute reconcilers'
# shared tick, and that offset actually reaches an already-registered schedule.
# ---------------------------------------------------------------------------


def test_worker_revision_drift_schedule_carries_the_offset():
    schedule = schedule_runtime.build_worker_revision_drift_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS == 7 * 60
    assert schedule.spec.intervals[0].offset == timedelta(
        seconds=schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS
    )


def test_every_other_built_schedule_carries_no_offset():
    other_builders = [
        lambda: schedule_runtime.build_dispatch_schedule(
            FAKE_TEMPORAL, fake_workflow, interval_seconds=900, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_staleness_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_ea_apply_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_ea_observation_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_release_apply_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_requirements_apply_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_release_status_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_doctrine_staleness_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_capacity_resume_probe_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_cluster_health_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
        lambda: schedule_runtime.build_change_apply_schedule(
            FAKE_TEMPORAL, fake_workflow, task_queue="factory-dispatcher-dev"
        ),
    ]

    for build in other_builders:
        schedule = build()
        assert schedule.spec.intervals[0].offset is None


def test_register_worker_revision_drift_schedule_applies_the_offset_to_an_existing_schedule_without_one():
    """An already-registered drift schedule from before this offset existed -- same interval, no
    offset -- must be updated in place on the next worker start, never logged as 'already up to
    date' (the trap: a desired-schedule change with no corresponding `schedule_needs_update`
    comparison is silently never applied)."""
    logger = logging.getLogger("test-register-worker-revision-drift-offset-backfill")
    existing = FakeSchedule(
        action=FakeAction(
            workflow=fake_workflow,
            id=schedule_runtime.WORKER_REVISION_DRIFT_WORKFLOW_ID_PREFIX,
            task_queue="factory-dispatcher-dev",
        ),
        spec=FakeSpec(intervals=[FakeInterval(every=timedelta(seconds=900))]),
        policy=FakePolicy(overlap=FakeOverlapPolicy.SKIP),
        state=FakeState(note=schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_NOTE, paused=False),
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_worker_revision_drift_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-worker-revision-drift-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert len(client.handle.updates) == 1
    assert client.schedule.spec.intervals[0].offset == timedelta(
        seconds=schedule_runtime.WORKER_REVISION_DRIFT_SCHEDULE_OFFSET_SECONDS
    )


def test_register_worker_revision_drift_schedule_is_unchanged_when_the_offset_already_matches():
    logger = logging.getLogger("test-register-worker-revision-drift-offset-noop")
    existing = schedule_runtime.build_worker_revision_drift_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_worker_revision_drift_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-worker-revision-drift-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "unchanged"
    assert client.handle.updates == []


def test_dispatch_interval_default_and_env_override(monkeypatch):
    monkeypatch.delenv("FACTORY_DISPATCH_INTERVAL_SECONDS", raising=False)
    assert Config().DISPATCH_SCHEDULE_INTERVAL_SECONDS == 900

    monkeypatch.setenv("FACTORY_DISPATCH_INTERVAL_SECONDS", "1200")
    assert Config().DISPATCH_SCHEDULE_INTERVAL_SECONDS == 1200


# ---------------------------------------------------------------------------
# OPS-66: resuming a capacity pause, and the schedule that drives the probe.
# ---------------------------------------------------------------------------


def test_resume_dispatch_schedule_clears_paused_and_sets_the_resume_note_and_preserves_spec():
    existing = build_schedule(interval=900, paused=True)
    existing.state.note = schedule_runtime.capacity_pause_note("worker reported usage limit exhaustion")
    client = FakeClient(existing)
    note = "factory dispatcher resumed: capacity-pause resume responder; live probe succeeded"

    asyncio.run(
        schedule_runtime.resume_dispatch_schedule(
            client,
            FAKE_TEMPORAL,
            schedule_id="factory-dispatcher-dev",
            note=note,
        )
    )

    assert len(client.handle.updates) == 1
    assert client.schedule.state.paused is False
    assert client.schedule.state.note == note
    assert client.schedule.spec.intervals[0].every == timedelta(seconds=900)
    assert client.schedule.policy.overlap is FakeOverlapPolicy.SKIP


def test_dispatch_schedule_pause_state_reports_paused_and_note():
    existing = build_schedule(paused=True)
    existing.state.note = schedule_runtime.capacity_pause_note("worker reported usage limit exhaustion")
    client = FakeClient(existing)

    paused, note = asyncio.run(
        schedule_runtime.dispatch_schedule_pause_state(
            client, schedule_id="factory-dispatcher-dev"
        )
    )

    assert paused is True
    assert "worker reported usage limit exhaustion" in note


def test_dispatch_schedule_pause_state_reports_unpaused_with_its_note():
    client = FakeClient(build_schedule(paused=False))

    paused, note = asyncio.run(
        schedule_runtime.dispatch_schedule_pause_state(
            client, schedule_id="factory-dispatcher-dev"
        )
    )

    assert paused is False
    assert note == schedule_runtime.SCHEDULE_NOTE


def test_dispatch_schedule_pause_state_reports_empty_note_when_none_is_set():
    existing = build_schedule(paused=False)
    existing.state.note = None
    client = FakeClient(existing)

    paused, note = asyncio.run(
        schedule_runtime.dispatch_schedule_pause_state(
            client, schedule_id="factory-dispatcher-dev"
        )
    )

    assert paused is False
    assert note == ""


def test_build_capacity_resume_probe_schedule_matches_dispatch_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_capacity_resume_probe_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.CAPACITY_RESUME_PROBE_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.CAPACITY_RESUME_PROBE_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-capacity-resume-probe-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_capacity_resume_probe_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-capacity-resume-probe-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_capacity_resume_probe_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-capacity-resume-probe-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_capacity_resume_probe_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-capacity-resume-probe-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_capacity_resume_probe_schedule_never_inherits_the_dispatch_schedules_pause():
    # This schedule must keep probing through the exact capacity-pause condition it exists to
    # detect -- registering it never carries over another schedule's paused state, since each
    # schedule_id is independent; a fresh existing schedule for THIS id starts unpaused.
    logger = logging.getLogger("test-register-capacity-resume-probe-update")
    existing = schedule_runtime.build_capacity_resume_probe_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=False,
    )
    existing.action.id = (
        "factory-capacity-resume-probe-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_capacity_resume_probe_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-capacity-resume-probe-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.CAPACITY_RESUME_PROBE_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    assert client.schedule.state.paused is False


# -- OPS-8: the cluster health checker's schedule -------------------------------------------------


def test_build_cluster_health_schedule_matches_dispatch_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_cluster_health_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.CLUSTER_HEALTH_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.CLUSTER_HEALTH_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-cluster-health-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_cluster_health_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-cluster-health-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_cluster_health_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-cluster-health-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_cluster_health_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-cluster-health-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_cluster_health_schedule_updates_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-cluster-health-update")
    existing = schedule_runtime.build_cluster_health_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=True,
    )
    existing.action.id = (
        "factory-cluster-health-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_cluster_health_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-cluster-health-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.action.id == schedule_runtime.CLUSTER_HEALTH_WORKFLOW_ID_PREFIX
    assert "{{" not in client.schedule.action.id
    assert "}}" not in client.schedule.action.id
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True


def test_build_doctrine_staleness_schedule_is_nightly_and_skips_overlap():
    schedule = schedule_runtime.build_doctrine_staleness_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.DOCTRINE_STALENESS_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(days=1)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.DOCTRINE_STALENESS_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-doctrine-staleness-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_doctrine_staleness_schedule_uses_create_or_update_pattern():
    logger = logging.getLogger("test-register-doctrine-staleness-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_doctrine_staleness_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-doctrine-staleness-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_doctrine_staleness_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-doctrine-staleness-nightly",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_doctrine_staleness_workflow_is_registered_on_the_worker():
    """REL-4's deferred defect, closed: the activity was registered while the
    workflow was absent from the worker's list, so nothing could ever start
    it. The worker's declared workflows must carry the schedule's target."""
    import worker as worker_module

    source = inspect.getsource(worker_module.build_worker)
    assert "DoctrineStalenessReportWorkflow" in source


def test_build_requirements_apply_schedule_is_15m_and_skips_overlap():
    schedule = schedule_runtime.build_requirements_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.REQUIREMENTS_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(minutes=15)
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.REQUIREMENTS_APPLY_WORKFLOW_ID_PREFIX
    assert "{{" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_requirements_apply_schedule_uses_create_or_update_pattern():
    logger = logging.getLogger("test-register-requirements-apply-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_requirements_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-requirements-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_requirements_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-requirements-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_requirements_apply_schedule_preserves_operator_pause():
    logger = logging.getLogger("test-register-requirements-apply-pause")
    existing = schedule_runtime.build_requirements_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        paused=True,
    )
    existing.action.id = (
        "factory-requirements-apply-{{ScheduledTime | date:'2006-01-02-15-04-05'}}"
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_requirements_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-requirements-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True


def test_build_change_apply_schedule_matches_ea_apply_cadence_and_skips_overlap():
    schedule = schedule_runtime.build_change_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
    )

    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.CHANGE_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.EA_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    assert schedule.policy.overlap is FakeOverlapPolicy.SKIP
    assert schedule.action.id == schedule_runtime.CHANGE_APPLY_WORKFLOW_ID_PREFIX
    assert schedule.action.id.startswith("factory-change-apply-")
    assert "{{" not in schedule.action.id
    assert "}}" not in schedule.action.id
    assert schedule.action.args == [{}]


def test_register_change_apply_schedule_uses_same_create_or_update_pattern():
    logger = logging.getLogger("test-register-change-apply-create")
    client = FakeClient()

    first = asyncio.run(
        schedule_runtime.register_change_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-change-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )
    second = asyncio.run(
        schedule_runtime.register_change_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-change-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert first.operation == "created"
    assert second.operation == "unchanged"
    assert len(client.created) == 1
    assert client.handle.updates == []


def test_register_change_apply_schedule_updates_in_place_and_preserves_pause():
    logger = logging.getLogger("test-register-change-apply-update")
    existing = schedule_runtime.build_change_apply_schedule(
        FAKE_TEMPORAL,
        fake_workflow,
        task_queue="factory-dispatcher-dev",
        interval_seconds=60,
        paused=True,
    )
    client = FakeClient(existing)

    result = asyncio.run(
        schedule_runtime.register_change_apply_schedule(
            client,
            FAKE_TEMPORAL,
            fake_workflow,
            schedule_id="factory-change-apply-15m",
            task_queue="factory-dispatcher-dev",
            logger=logger,
        )
    )

    assert result.operation == "updated"
    assert client.created == []
    assert len(client.handle.updates) == 1
    assert client.schedule.spec.intervals[0].every == timedelta(
        seconds=schedule_runtime.CHANGE_APPLY_SCHEDULE_INTERVAL_SECONDS
    )
    # PC-EXE-003: an operator-set pause must survive a registration re-run.
    assert client.schedule.state.paused is True
