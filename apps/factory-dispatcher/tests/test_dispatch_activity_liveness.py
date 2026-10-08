"""Pure unit tests for dispatch activity liveness bounds."""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import workflow_core  # noqa: E402


def _install_temporal_stubs(monkeypatch):
    temporalio = ModuleType("temporalio")
    activity_mod = ModuleType("temporalio.activity")
    client_mod = ModuleType("temporalio.client")
    common_mod = ModuleType("temporalio.common")
    exceptions_mod = ModuleType("temporalio.exceptions")
    workflow_mod = ModuleType("temporalio.workflow")

    def decorator(obj=None, *_args, **_kwargs):
        if callable(obj):
            return obj

        def apply(obj):
            return obj

        return apply

    class RetryPolicy:
        def __init__(self, maximum_attempts=None):
            self.maximum_attempts = maximum_attempts

    class ApplicationError(RuntimeError):
        def __init__(self, message, *, type=None):
            self.type = type
            super().__init__(message)

    async def execute_activity(name, payload, **kwargs):
        execute_activity.calls.append((name, payload, kwargs))
        return {"status": "no_task", "exit_code": 0}

    execute_activity.calls = []

    activity_mod.defn = decorator
    activity_mod.heartbeat = lambda _details: None
    activity_mod.in_activity = lambda: False
    client_mod.Client = object
    client_mod.ScheduleState = object
    client_mod.ScheduleUpdate = object
    common_mod.RetryPolicy = RetryPolicy
    exceptions_mod.ApplicationError = ApplicationError
    workflow_mod.defn = decorator
    workflow_mod.run = decorator
    workflow_mod.execute_activity = execute_activity
    workflow_mod.info = lambda: SimpleNamespace(attempt=1)

    temporalio.activity = activity_mod
    temporalio.client = client_mod
    temporalio.common = common_mod
    temporalio.exceptions = exceptions_mod
    temporalio.workflow = workflow_mod

    for name, module in {
        "temporalio": temporalio,
        "temporalio.activity": activity_mod,
        "temporalio.client": client_mod,
        "temporalio.common": common_mod,
        "temporalio.exceptions": exceptions_mod,
        "temporalio.workflow": workflow_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return workflow_mod


def _fresh_import(monkeypatch, module_name: str):
    previous_module = sys.modules.pop(module_name, None)
    parent_name, _, child_name = module_name.rpartition(".")
    parent_module = sys.modules.get(parent_name) if parent_name else None
    had_parent_attr = bool(parent_module and hasattr(parent_module, child_name))
    previous_parent_attr = (
        getattr(parent_module, child_name) if had_parent_attr else None
    )

    module = importlib.import_module(module_name)

    if previous_module is None:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    else:
        monkeypatch.setitem(sys.modules, module_name, previous_module)
    if parent_module:
        if had_parent_attr:
            monkeypatch.setattr(parent_module, child_name, previous_parent_attr)
        else:
            monkeypatch.delattr(parent_module, child_name, raising=False)
    return module


def test_heartbeat_timeout_is_derived_from_the_heartbeat_cadence_not_the_work_budget():
    """2026-09-03: the old shared 45-minute heartbeat_timeout was sized off
    DEFAULT_STUCK_THRESHOLD_MINUTES (a work-budget number) and applied to
    every activity, including ones that never heartbeated. A tunnel flap
    during preflight then went undetected for the full 45 minutes. The bound
    must instead be a small multiple of how often a heartbeating activity
    actually calls heartbeat(), so a dead connection is missed for at most a
    couple of cadence periods -- tens of seconds, not the work budget."""
    assert workflow_core.DISPATCH_HEARTBEAT_TIMEOUT == (
        workflow_core.DISPATCH_HEARTBEAT_INTERVAL * 3
    )
    assert workflow_core.DISPATCH_HEARTBEAT_INTERVAL <= timedelta(seconds=30)
    assert workflow_core.DISPATCH_HEARTBEAT_TIMEOUT < timedelta(minutes=5)


def test_only_activities_that_actually_heartbeat_declare_a_heartbeat_timeout():
    """A heartbeat_timeout on a step that never calls heartbeat() is not a
    liveness check -- Temporal has nothing to reset its clock, so the step
    would simply fail if it ever ran a little slow. Only steps wrapped in the
    heartbeat-subprocess pattern (activities.dispatch_steps) may declare one;
    every other step must get None and stay bounded by start_to_close alone."""
    for step in ("isolate", "preflight", "run", "verify", "propose"):
        assert workflow_core.heartbeat_timeout_for_step(step) == (
            workflow_core.DISPATCH_HEARTBEAT_TIMEOUT
        )
    for step in (
        "reconcile",
        "claim",
        "contain",
        "scope",
        "smoke",
        "record_failure",
        "cleanup",
        "some-unknown-step",
    ):
        assert workflow_core.heartbeat_timeout_for_step(step) is None


def test_workflow_declares_per_step_heartbeat_and_start_to_close_timeouts(
    monkeypatch,
):
    """2026-09-11 wedge fix: `run`'s 24h start_to_close_timeout must stay
    exactly what it was (a heartbeating step's own genuine work budget is
    unchanged by this bead) while every non-heartbeating step now gets a
    start_to_close_timeout proportionate to its own work instead of
    inheriting that same 24h bound by default."""
    workflow_mod = _install_temporal_stubs(monkeypatch)
    dispatch_task = _fresh_import(monkeypatch, "workflows.dispatch_task")

    calls: list[tuple[str, dict]] = []

    async def execute_activity(name, payload, **kwargs):
        calls.append((name, kwargs))
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "propose":
            return {"status": "review", "exit_code": 0, "task_id": "task-1"}
        return {**payload, name: True}

    workflow_mod.execute_activity = execute_activity

    result = asyncio.run(dispatch_task.DispatchTaskWorkflow().run({}))

    assert result["status"] == "review"
    seen_names = [name for name, _kwargs in calls]
    for step in ("isolate", "preflight", "run", "verify", "propose"):
        assert step in seen_names

    for name, kwargs in calls:
        assert kwargs["heartbeat_timeout"] == workflow_core.heartbeat_timeout_for_step(
            name
        )
        assert kwargs["start_to_close_timeout"] == (
            workflow_core.start_to_close_timeout_for_step(name)
        )

    heartbeating = {
        name: kwargs
        for name, kwargs in calls
        if name in {"isolate", "preflight", "run", "verify", "propose"}
    }
    assert {kwargs["heartbeat_timeout"] for kwargs in heartbeating.values()} == {
        workflow_core.DISPATCH_HEARTBEAT_TIMEOUT
    }
    # Unchanged from before this bead: the five heartbeating steps still get
    # `run`'s own 24h start_to_close budget, not the new short bound.
    assert {kwargs["start_to_close_timeout"] for kwargs in heartbeating.values()} == {
        dispatch_task.ACTIVITY_START_TO_CLOSE_TIMEOUT
    }
    for kwargs in heartbeating.values():
        assert kwargs["start_to_close_timeout"] > timedelta(hours=23)

    non_heartbeating = {
        name: kwargs
        for name, kwargs in calls
        if name not in {"isolate", "preflight", "run", "verify", "propose"}
    }
    # record_failure is never called on this all-success path -- everything
    # ELSE this workflow can call outside the five heartbeating steps is here.
    assert set(non_heartbeating) == {"claim", "contain", "scope", "smoke", "reconcile", "cleanup"}
    assert set(non_heartbeating) < workflow_core.NON_HEARTBEATING_DISPATCH_STEPS
    assert {kwargs["heartbeat_timeout"] for kwargs in non_heartbeating.values()} == {None}
    for name, kwargs in non_heartbeating.items():
        assert kwargs["start_to_close_timeout"] < dispatch_task.ACTIVITY_START_TO_CLOSE_TIMEOUT
        assert kwargs["start_to_close_timeout"] <= workflow_core.RECONCILE_STEP_TIMEOUT


def test_lost_worker_attempt_is_retryable_within_declared_heartbeat_bound(monkeypatch):
    _install_temporal_stubs(monkeypatch)
    dispatch_task = _fresh_import(monkeypatch, "workflows.dispatch_task")

    started = datetime(2026, 9, 3, 15, 30, 10, tzinfo=timezone.utc)
    last_heartbeat = started + timedelta(seconds=20)
    worker_died = started + timedelta(seconds=40)

    retry_eligible_at = last_heartbeat + workflow_core.DISPATCH_HEARTBEAT_TIMEOUT
    old_start_to_close_eligible_at = (
        started + dispatch_task.ACTIVITY_START_TO_CLOSE_TIMEOUT
    )

    assert retry_eligible_at - worker_died <= workflow_core.DISPATCH_HEARTBEAT_TIMEOUT
    assert retry_eligible_at < old_start_to_close_eligible_at
    assert old_start_to_close_eligible_at - retry_eligible_at > timedelta(hours=23)
    # The incident this bound fixes: with the old shared 45-minute value, a
    # connection lost at worker_died was not noticed until 45 minutes after
    # the last heartbeat. The new bound notices it in under two minutes.
    assert retry_eligible_at - worker_died < timedelta(minutes=2)


def test_lost_short_step_frees_the_workflow_at_its_own_bound_not_24h(monkeypatch):
    """Literal simulated-lost-step demonstration (2026-09-11 wedge): `contain`
    is started and never completes -- the connection was lost before the
    worker ever answered, so nothing will ever set the event it awaits. The
    stubbed execute_activity below honors the real start_to_close_timeout it
    is handed and raises the same Temporal-shaped exception a real
    start_to_close timeout produces, the way Temporal's own server-side
    enforcement does -- so this drives the REAL DispatchTaskWorkflow/
    run_dispatch_attempt code and proves the whole sequence becomes runnable
    again (completes, gracefully, as an environmental fault) at the
    workflow's declared per-step bound, not 24 hours later. A test that only
    asserted the constants changed would not catch a wiring mistake that left
    the mechanism itself unreachable; this one would."""
    workflow_mod = _install_temporal_stubs(monkeypatch)
    # A real timeout, scaled down so the test runs in milliseconds rather than
    # minutes -- the mechanism under test (asyncio.wait_for preempting a
    # coroutine that never completes, at the exact timeout it is given) does
    # not depend on the constant's real-world magnitude.
    monkeypatch.setattr(workflow_core, "DISPATCH_SHORT_STEP_TIMEOUT", timedelta(seconds=0.05))
    dispatch_task = _fresh_import(monkeypatch, "workflows.dispatch_task")

    async def execute_activity(name, payload, *, start_to_close_timeout, **_kwargs):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **payload,
                "worker_result": {
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "contain":
            # Started, and never completes: nothing ever sets this event, so
            # only the timeout Temporal itself would enforce can end the wait.
            try:
                await asyncio.wait_for(
                    asyncio.Event().wait(), timeout=start_to_close_timeout.total_seconds()
                )
            except asyncio.TimeoutError as exc:
                cause = RuntimeError("Timeout")
                cause.type = SimpleNamespace(name="START_TO_CLOSE")
                cause.__cause__ = exc
                wrapped = RuntimeError("Activity task failed (contain)")
                wrapped.__cause__ = cause
                raise wrapped  # noqa: B904 - __cause__ set explicitly above
            raise AssertionError("the hung activity must never complete on its own")
        return {**payload, name: True}

    workflow_mod.execute_activity = execute_activity

    started = time.monotonic()
    result = asyncio.run(dispatch_task.DispatchTaskWorkflow().run({}))
    elapsed = time.monotonic() - started

    # Regained control at the declared per-step bound (tens of milliseconds
    # here, scaled from the real 10-minute production value), nowhere near
    # the 24h `run` needs: the workflow completed gracefully instead of
    # hanging for a day, and did not spend the bead's retry budget on it.
    assert elapsed < 5.0
    assert result["status"] == "environmental_fault"
    assert workflow_core.start_to_close_timeout_for_step("contain") == timedelta(seconds=0.05)
    assert dispatch_task.ACTIVITY_START_TO_CLOSE_TIMEOUT == timedelta(hours=24)


def test_long_running_healthy_worker_is_not_cancelled_by_heartbeat_bound(monkeypatch):
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    release_worker = threading.Event()
    heartbeats = []
    state = {
        "task": {"id": "task-1"},
        "worker": {"name": "codex", "argv": ["codex", "exec"]},
        "prompt": "do the work",
        "clone": ".",
        "budget": dispatch.DEFAULT_BUDGET_MINUTES + 10,
    }

    def fake_run_worker(_prompt, _clone, _budget, _argv, *_a, **_k):
        release_worker.wait(timeout=2)
        return dispatch.WorkerResult(
            exit_code=0,
            stdout="ok",
            stderr=None,
            duration_s=(
                workflow_core.DISPATCH_HEARTBEAT_TIMEOUT.total_seconds() * 2
            ),
            timed_out=False,
        )

    def fake_heartbeat(details):
        heartbeats.append(dict(details))
        if len(heartbeats) == 4:
            release_worker.set()

    result = dispatch_steps._run_worker_with_heartbeat(
        state,
        run_worker=fake_run_worker,
        heartbeat=fake_heartbeat,
        heartbeat_interval=0.001,
    )

    assert result.exit_code == 0
    assert result.duration_s > (
        workflow_core.DISPATCH_HEARTBEAT_TIMEOUT.total_seconds()
    )
    assert len(heartbeats) >= 4
    assert heartbeats[-1] == {
        "step": "run",
        "task_id": "task-1",
        "worker": "codex",
        "budget_minutes": dispatch.DEFAULT_BUDGET_MINUTES + 10,
    }


def test_isolate_heartbeats_while_clone_and_bootstrap_run(monkeypatch):
    """dispatch.make_clone can run a multi-hundred-second subprocess (git
    clone with a 300s timeout). isolate_activity must keep Temporal liveness
    fresh the same way run_activity already does for the worker subprocess."""
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    release = threading.Event()
    heartbeats = []

    def fake_make_clone(_cfg, _clone):
        release.wait(timeout=2)

    def fake_heartbeat(details):
        heartbeats.append(dict(details))
        if len(heartbeats) == 4:
            release.set()

    dispatch_steps._make_clone_with_heartbeat(
        SimpleNamespace(),
        Path("/does/not/matter"),
        {"task": {"id": "task-42"}},
        make_clone=fake_make_clone,
        heartbeat=fake_heartbeat,
        heartbeat_interval=0.001,
    )

    assert len(heartbeats) >= 4
    assert heartbeats[-1] == {"step": "isolate", "task_id": "task-42"}


def test_preflight_heartbeats_while_declared_verification_runs(monkeypatch):
    """dispatch.verify_declared_commands can bootstrap a venv and run several
    declared commands, each individually capped at 20 minutes -- genuinely
    long. preflight_activity must heartbeat through it."""
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    release = threading.Event()
    heartbeats = []
    sentinel = object()

    def fake_verify(_clone, _commands):
        release.wait(timeout=2)
        return sentinel

    def fake_heartbeat(details):
        heartbeats.append(dict(details))
        if len(heartbeats) == 4:
            release.set()

    result = dispatch_steps._verify_declared_commands_with_heartbeat(
        Path("."),
        ("pytest -q",),
        {"task": {"id": "task-42"}},
        step="preflight",
        verify_declared_commands=fake_verify,
        heartbeat=fake_heartbeat,
        heartbeat_interval=0.001,
    )

    assert result is sentinel
    assert len(heartbeats) >= 4
    assert heartbeats[-1] == {"step": "preflight", "task_id": "task-42"}


def test_verify_heartbeats_while_declared_verification_runs(monkeypatch):
    """Same wrapper as preflight, reused by verify_activity after the worker
    has run -- must heartbeat identically regardless of which step calls it."""
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    release = threading.Event()
    heartbeats = []
    sentinel = object()

    def fake_verify(_clone, _commands):
        release.wait(timeout=2)
        return sentinel

    def fake_heartbeat(details):
        heartbeats.append(dict(details))
        if len(heartbeats) == 4:
            release.set()

    result = dispatch_steps._verify_declared_commands_with_heartbeat(
        Path("."),
        ("pytest -q",),
        {"task": {"id": "task-42"}},
        step="verify",
        verify_declared_commands=fake_verify,
        heartbeat=fake_heartbeat,
        heartbeat_interval=0.001,
    )

    assert result is sentinel
    assert len(heartbeats) >= 4
    assert heartbeats[-1] == {"step": "verify", "task_id": "task-42"}


def test_propose_heartbeats_while_opening_the_pull_request(monkeypatch):
    """dispatch.open_pull_request pushes (up to a 300s subprocess timeout)
    and calls `gh pr create` (up to 180s) -- a real multi-minute network and
    subprocess call. propose_activity must heartbeat through it."""
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    release = threading.Event()
    heartbeats = []

    def fake_open_pull_request(*_args, **_kwargs):
        release.wait(timeout=2)
        return "https://example.invalid/pr/1"

    def fake_heartbeat(details):
        heartbeats.append(dict(details))
        if len(heartbeats) == 4:
            release.set()

    result = dispatch_steps._open_pull_request_with_heartbeat(
        SimpleNamespace(),
        Path("."),
        {"id": "task-42"},
        "factory/lane-title-task42",
        "worker stdout",
        object(),
        object(),
        {"task": {"id": "task-42"}},
        open_pull_request=fake_open_pull_request,
        heartbeat=fake_heartbeat,
        heartbeat_interval=0.001,
    )

    assert result == "https://example.invalid/pr/1"
    assert len(heartbeats) >= 4
    assert heartbeats[-1] == {"step": "propose", "task_id": "task-42"}
