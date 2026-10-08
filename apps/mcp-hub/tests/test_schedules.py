"""Tests for tools.schedules — sync-now, reconcile, and schedule status (PR 9)."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.service import RPCError, RPCStatusCode

from tools import schedules as sched


def _not_found_error() -> RPCError:
    return RPCError("schedule not found", RPCStatusCode.NOT_FOUND, b"")


class _FakeScheduleHandle:
    def __init__(self, *, trigger_exc=None, describe_result=None, describe_exc=None):
        self.trigger_exc = trigger_exc
        self.describe_result = describe_result
        self.describe_exc = describe_exc
        self.triggered = 0

    async def trigger(self):
        if self.trigger_exc:
            raise self.trigger_exc
        self.triggered += 1

    async def describe(self):
        if self.describe_exc:
            raise self.describe_exc
        return self.describe_result


class _FakeClient:
    def __init__(self, handles):
        self.handles = handles
        self.started = []

    def get_schedule_handle(self, schedule_id):
        return self.handles[schedule_id]

    async def start_workflow(self, workflow, *args, **kwargs):
        self.started.append({"workflow": workflow, "args": args, **kwargs})
        return SimpleNamespace(id=kwargs.get("id"))


@pytest.fixture
def temporal(monkeypatch):
    def install(client):
        async def fake_get_client():
            return client
        monkeypatch.setattr(sched, "get_temporal_client", fake_get_client)
        return client
    return install


async def test_sync_now_triggers_existing_schedule(temporal):
    handle = _FakeScheduleHandle()
    client = temporal(_FakeClient({"bank-sync-chase": handle}))

    result = await sched.trigger_bank_sync("chase")

    assert handle.triggered == 1
    assert client.started == []
    assert result == {"institution": "chase", "via": "schedule", "id": "bank-sync-chase"}


async def test_sync_now_falls_back_to_direct_start(temporal, monkeypatch):
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "test-queue")
    handle = _FakeScheduleHandle(trigger_exc=_not_found_error())
    client = temporal(_FakeClient({"bank-sync-chase": handle}))

    result = await sched.trigger_bank_sync("chase")

    assert result["via"] == "workflow"
    assert result["id"].startswith("bank-sync-chase-manual-")
    (started,) = client.started
    assert started["workflow"] == "BankSyncWorkflow"
    assert started["args"] == ("chase",)
    assert started["task_queue"] == "test-queue"
    assert started["execution_timeout"].total_seconds() == 7200
    assert started["retry_policy"].maximum_attempts == 4
    assert started["retry_policy"].initial_interval.total_seconds() == 600


async def test_sync_now_reraises_non_not_found(temporal):
    handle = _FakeScheduleHandle(
        trigger_exc=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    )
    temporal(_FakeClient({"bank-sync-chase": handle}))

    with pytest.raises(RPCError):
        await sched.trigger_bank_sync("chase")


def _describe(paused=False, last_started=None, next_run=None, num_actions=3):
    recent = []
    if last_started:
        recent = [SimpleNamespace(scheduled_at=last_started, started_at=last_started)]
    return SimpleNamespace(
        schedule=SimpleNamespace(state=SimpleNamespace(paused=paused, note=None)),
        info=SimpleNamespace(
            recent_actions=recent,
            next_action_times=[next_run] if next_run else [],
            num_actions=num_actions,
        ),
    )


async def test_schedule_status_reports_existing_and_missing(temporal, monkeypatch):
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "t1")
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_AMEX", "t2")
    last = datetime(2026, 6, 12, 3, 0, tzinfo=timezone.utc)
    nxt = datetime(2026, 6, 13, 3, 0, tzinfo=timezone.utc)
    temporal(_FakeClient({
        "bank-sync-amex": _FakeScheduleHandle(describe_exc=_not_found_error()),
        "bank-sync-chase": _FakeScheduleHandle(
            describe_result=_describe(last_started=last, next_run=nxt)
        ),
    }))

    result = await sched.get_bank_sync_schedules()

    assert result["missing"] == ["amex"]
    by_slug = {s["institution"]: s for s in result["schedules"]}
    assert by_slug["amex"]["exists"] is False
    chase = by_slug["chase"]
    assert chase["exists"] is True
    assert chase["paused"] is False
    assert chase["last_run_at"] == last.isoformat()
    assert chase["next_run_at"] == nxt.isoformat()


async def test_reconcile_calls_worker_registration(temporal, monkeypatch):
    client = temporal(_FakeClient({}))
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "t1")

    calls = []

    async def fake_ensure(c):
        calls.append(c)

    import src.temporal_worker as tw
    monkeypatch.setattr(tw, "ensure_schedules", fake_ensure)

    result = await sched.reconcile_schedules()

    assert calls == [client]
    assert result["reconciled"] is True
    assert "chase" in result["institutions"]


async def test_reconcile_refuses_when_schedules_disabled(temporal, monkeypatch):
    """The API pod must obey the same gate as the worker.

    ENABLE_TEMPORAL_SCHEDULES=false stops the worker registering schedules at
    boot, but this endpoint (and the reconcile_schedules MCP tool) used to
    call ensure_schedules unconditionally — so one request against the DEV
    pod re-created the full recurring set in the dev Temporal namespace. That
    is how dev ended up running 14 live schedules, including sandbox-Plaid
    bank syncs whose re-auth alerts were indistinguishable from prod's.
    """
    temporal(_FakeClient({}))
    monkeypatch.setenv("ENABLE_TEMPORAL_SCHEDULES", "false")
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "t1")

    calls = []

    async def fake_ensure(c):
        calls.append(c)

    import src.temporal_worker as tw
    monkeypatch.setattr(tw, "ensure_schedules", fake_ensure)

    result = await sched.reconcile_schedules()

    assert calls == []
    assert result["reconciled"] is False
    assert "ENABLE_TEMPORAL_SCHEDULES" in result["reason"]


def test_reconcile_gate_matches_worker_gate(monkeypatch):
    """schedules.schedules_enabled duplicates the worker's flag parsing
    (importing temporal_worker would pull in every workflow module). Drift
    between the two re-opens the dev-schedule hole, so pin them together."""
    import src.temporal_worker as tw

    for value in ("true", "TRUE", "1", "yes", "on", "false", "0", "no", "off", ""):
        monkeypatch.setenv("ENABLE_TEMPORAL_SCHEDULES", value)
        assert sched.schedules_enabled() == tw.temporal_schedules_enabled(), value

    monkeypatch.delenv("ENABLE_TEMPORAL_SCHEDULES", raising=False)
    assert sched.schedules_enabled() == tw.temporal_schedules_enabled()


async def test_worker_bank_sync_schedule_uses_local_time_and_workflow_retry(monkeypatch):
    for key in ("PLAID_ACCESS_TOKEN_AMEX", "PLAID_ACCESS_TOKEN_CHASE", "PLAID_ACCESS_TOKEN_CITI", "PLAID_ACCESS_TOKEN_SOFI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "t1")
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "test-queue")

    import src.temporal_worker as tw

    captured = {}

    async def fake_reconcile_schedule(client, schedule_id, schedule):
        captured[schedule_id] = schedule

    monkeypatch.setattr(tw, "reconcile_schedule", fake_reconcile_schedule)

    await tw.ensure_schedules(SimpleNamespace())

    schedule = captured["bank-sync-chase"]
    assert schedule.spec.time_zone_name == "America/Chicago"
    # Evening only. The 2026-07-13/14 zero-write regression was a 03:00 run
    # reading an empty page before banks finished posting; four runs a day was
    # the first fix and 21:00 is the one of them that was always going to find
    # the day's transactions. Four-a-day cost $50 of consumption billing in a
    # month (2026-08-29).
    assert [r.start for r in schedule.spec.calendars[0].hour] == [21]
    # chase is the primary checking feed and the only one read mid-week.
    assert [r.start for r in schedule.spec.calendars[0].day_of_week] == [2, 4, 6]
    assert schedule.action.task_queue == "test-queue"
    assert schedule.action.execution_timeout.total_seconds() == 7200
    assert schedule.action.retry_policy.maximum_attempts == 4
    assert schedule.action.retry_policy.initial_interval.total_seconds() == 600


@pytest.mark.asyncio
async def test_worker_bank_sync_credit_institutions_run_weekly_on_sunday(monkeypatch):
    """Credit and secondary depository feeds are read for balances and due
    dates, which move monthly. Four runs a day charged for that as if it were
    the checking account (2026-08-29, $50 of consumption billing)."""
    for key in ("PLAID_ACCESS_TOKEN_AMEX", "PLAID_ACCESS_TOKEN_CHASE",
                "PLAID_ACCESS_TOKEN_CITI", "PLAID_ACCESS_TOKEN_SOFI",
                "PLAID_ACCESS_TOKEN_DISCOVER"):
        monkeypatch.delenv(key, raising=False)
    for key in ("PLAID_ACCESS_TOKEN_AMEX", "PLAID_ACCESS_TOKEN_CITI",
                "PLAID_ACCESS_TOKEN_DISCOVER", "PLAID_ACCESS_TOKEN_SOFI"):
        monkeypatch.setenv(key, "t")
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "test-queue")

    import src.temporal_worker as tw

    captured = {}

    async def fake_reconcile_schedule(client, schedule_id, schedule):
        captured[schedule_id] = schedule

    monkeypatch.setattr(tw, "reconcile_schedule", fake_reconcile_schedule)
    await tw.ensure_schedules(SimpleNamespace())

    for slug in ("amex", "citi", "discover", "sofi"):
        cal = captured[f"bank-sync-{slug}"].spec.calendars[0]
        assert [r.start for r in cal.day_of_week] == [0], slug
        assert [r.start for r in cal.hour] == [21], slug
    # Four institutions share Sunday 21:00, so the stagger that stopped the
    # 2026-07-18 decrypt herd is still doing work.
    minutes = {captured[f"bank-sync-{s}"].spec.calendars[0].minute[0].start
               for s in ("amex", "citi", "discover", "sofi")}
    assert len(minutes) == 4


@pytest.mark.asyncio
async def test_worker_refuses_to_schedule_an_unclassified_institution(monkeypatch):
    """A default cadence is how five institutions ended up on four runs a day.
    An institution nobody classified gets no schedule, and reports itself as
    missing, rather than quietly costing money."""
    for key in list(os.environ):
        if key.startswith("PLAID_ACCESS_TOKEN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAID_ACCESS_TOKEN_NEWBANK", "t")
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "test-queue")

    import src.temporal_worker as tw

    captured = {}

    async def fake_reconcile_schedule(client, schedule_id, schedule):
        captured[schedule_id] = schedule

    monkeypatch.setattr(tw, "reconcile_schedule", fake_reconcile_schedule)
    await tw.ensure_schedules(SimpleNamespace())

    assert "bank-sync-newbank" not in captured


# --- reconcile_schedule: pause/note must survive re-registration ---
#
# A worker restart re-runs ensure_schedules -> reconcile_schedule for every
# schedule, unconditionally, on every boot. The prod Deployment restarts on
# every secret rotation (stakater reloader), so a schedule an operator paused
# from the Temporal UI must still be paused after the next Plaid credential
# rotation. These tests drive src.temporal_worker.reconcile_schedule directly
# against a fake client/handle — no Temporal server, no substrate, no network.


class _FakeReconcileHandle:
    """Fake Temporal ScheduleHandle exercising reconcile_schedule's real update path.

    `update` mirrors the real ScheduleHandle: it builds a ScheduleUpdateInput
    from the current description and hands it to the caller's updater, the
    same way reconcile_schedule reads `update_input.description.schedule.state`
    at update time rather than trusting a value fetched earlier.
    """

    def __init__(self, *, describe_result=None, describe_exc=None):
        self.describe_result = describe_result
        self.describe_exc = describe_exc
        self.updated_schedule = None

    async def describe(self):
        if self.describe_exc:
            raise self.describe_exc
        return self.describe_result

    async def update(self, updater):
        update_input = SimpleNamespace(description=self.describe_result)
        result = updater(update_input)
        self.updated_schedule = result.schedule


class _FakeReconcileClient:
    def __init__(self, handle):
        self._handle = handle
        self.created = None

    def get_schedule_handle(self, schedule_id):
        return self._handle

    async def create_schedule(self, schedule_id, schedule):
        self.created = (schedule_id, schedule)


def _schedule(hour: int, *, task_queue: str = "test-queue", state: ScheduleState = None) -> Schedule:
    kwargs = {} if state is None else {"state": state}
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "SomeWorkflow",
            id="some-schedule",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(start=hour)])]
        ),
        **kwargs,
    )


async def test_reconcile_schedule_preserves_pause_and_note_across_reregistration():
    """Re-registering a paused schedule must not silently resume it."""
    existing = _schedule(
        hour=21,
        state=ScheduleState(paused=True, note="paused during Plaid incident"),
    )
    handle = _FakeReconcileHandle(
        describe_result=SimpleNamespace(schedule=existing)
    )
    client = _FakeReconcileClient(handle)

    import src.temporal_worker as tw

    desired = _schedule(hour=21)
    await tw.reconcile_schedule(client, "bank-sync-chase", desired)

    assert handle.updated_schedule is not None
    assert handle.updated_schedule.state.paused is True
    assert handle.updated_schedule.state.note == "paused during Plaid incident"


async def test_reconcile_schedule_corrects_drift_without_pausing():
    """Pause preservation must not become drift preservation."""
    # drifted: someone hand-edited the hour in the UI
    existing = _schedule(hour=3, state=ScheduleState(paused=False, note=None))
    handle = _FakeReconcileHandle(
        describe_result=SimpleNamespace(schedule=existing)
    )
    client = _FakeReconcileClient(handle)

    import src.temporal_worker as tw

    desired = _schedule(hour=21)  # the correct, desired definition
    await tw.reconcile_schedule(client, "bank-sync-chase", desired)

    assert handle.updated_schedule is not None
    assert handle.updated_schedule.state.paused is False
    assert [r.start for r in handle.updated_schedule.spec.calendars[0].hour] == [21]


async def test_reconcile_schedule_creates_when_missing():
    handle = _FakeReconcileHandle(describe_exc=_not_found_error())
    client = _FakeReconcileClient(handle)

    import src.temporal_worker as tw

    desired = _schedule(hour=21)
    await tw.reconcile_schedule(client, "bank-sync-chase", desired)

    assert handle.updated_schedule is None
    assert client.created == ("bank-sync-chase", desired)
