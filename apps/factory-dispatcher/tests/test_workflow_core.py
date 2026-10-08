"""In-process tests for the dispatcher workflow sequence."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow_core


def test_dispatch_sequence_success_path_runs_documented_steps():
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "propose":
            return {"status": "review", "exit_code": 0, "task_id": "task-1"}
        return {**state, name: True}

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result == {"status": "review", "exit_code": 0, "task_id": "task-1"}
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "contain",
        "scope",
        "smoke",
        "verify",
        "propose",
        "cleanup",
    ]


def test_dispatch_sequence_records_failure_and_skips_later_steps():
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 1,
                    "stdout": "pytest failed",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "contain":
            raise RuntimeError("worker failed")
        return state

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert "worker failed" in result["message"]
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "contain",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_raises_ordinary_failure_for_temporal_retry_before_bound():
    calls: list[str] = []
    recorded: dict[str, object] = {}

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 1,
                    "stdout": "pytest failed",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "contain":
            raise RuntimeError("worker failed")
        if name == "record_failure":
            recorded.update(state)
            return {"status": "failure_recorded", "task_id": "task-1"}
        return state

    with pytest.raises(workflow_core.DispatchAttemptFailed):
        asyncio.run(
            workflow_core.run_dispatch_sequence(
                {},
                execute,
                workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3),
            )
        )

    assert recorded["retry_attempt"] == 1
    assert recorded["retry_maximum_attempts"] == 3
    assert "retry_exhausted" not in recorded
    assert recorded["failure_class"] == "work"
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "contain",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_does_not_derive_bead_exhaustion_from_workflow_attempt():
    """The Temporal workflow's own attempt count belongs to whichever bead
    that attempt happens to claim, not necessarily this one -- a retried
    workflow re-runs claim and may pick a different pending bead each time
    (tests/test_claim_atomicity.py). Reaching the workflow's own attempt
    bound must not be handed to record_failure as an exhaustion verdict for
    THIS bead; fail_task derives that solely from the bead's own recorded
    failure notes."""
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 1,
                    "stdout": "pytest failed",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "contain":
            raise RuntimeError("worker failed")
        if name == "record_failure":
            recorded.update(state)
            return {"status": "failure_recorded", "task_id": "task-1"}
        return state

    with pytest.raises(workflow_core.DispatchAttemptFailed):
        asyncio.run(
            workflow_core.run_dispatch_sequence(
                {},
                execute,
                workflow_core.DispatchRetryContext(attempt=3, maximum_attempts=3),
            )
        )

    assert recorded["retry_attempt"] == 3
    assert recorded["retry_maximum_attempts"] == 3
    assert "retry_exhausted" not in recorded
    assert recorded["failure_class"] == "work"


def test_dispatch_sequence_returns_capacity_status_when_failure_recorder_detects_backpressure():
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise RuntimeError("capacity backpressure: retry_at=Aug 7th 10:43 PM")
        if name == "record_failure":
            return {"status": "capacity_backpressure_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "capacity_backpressure"
    assert result["exit_code"] == 1
    assert "retry_at=Aug 7th 10:43 PM" in result["message"]
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_returns_environmental_status_without_temporal_retry():
    calls: list[str] = []
    recorded: dict[str, object] = {}

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "preflight":
            raise RuntimeError(
                "Declared verification command `pytest -q` failed before the worker ran."
            )
        if name == "record_failure":
            recorded.update(state)
            return {"status": "environmental_fault_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {},
            execute,
            workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3),
        )
    )

    assert result["status"] == "environmental_fault"
    assert result["exit_code"] == 1
    assert "failed before the worker ran" in result["message"]
    assert recorded["failure_class"] == "environment"
    assert "retry_exhausted" not in recorded
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_ends_retry_chain_for_already_satisfied_claim():
    """2026-08-19 dev.task 07a99270: a worker that verified its defect was
    already fixed, changed nothing, and said so plainly must not be retried
    as an ordinary work failure — Temporal should complete this attempt
    without raising, so no further attempt spends budget rediscovering it."""
    calls: list[str] = []
    recorded: dict[str, object] = {}

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 0,
                    "stdout": "No files were modified: acceptance criteria already met.",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "contain":
            raise RuntimeError(
                f"{workflow_core.retry_policy.ALREADY_SATISFIED_WORK_MARKER} worker "
                "made no changes and its output declared the task already "
                "satisfied\nWorker output (tail): acceptance criteria already met."
            )
        if name == "record_failure":
            recorded.update(state)
            return {"status": "already_satisfied_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {},
            execute,
            workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3),
        )
    )

    assert result["status"] == "already_satisfied"
    assert result["exit_code"] == 1
    assert "acceptance criteria already met" in result["message"]
    assert recorded["failure_class"] == "already_satisfied"
    assert "retry_exhausted" not in recorded
    assert "worker_result" in recorded
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "contain",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_does_not_retry_capacity_backpressure_under_policy():
    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise RuntimeError("capacity backpressure: retry_at=Aug 7th 10:43 PM")
        if name == "record_failure":
            return {"status": "capacity_backpressure_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {},
            execute,
            workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3),
        )
    )

    assert result["status"] == "capacity_backpressure"
    assert "retry_at=Aug 7th 10:43 PM" in result["message"]


def test_dispatch_sequence_missing_executable_does_not_spend_retry_budget():
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise FileNotFoundError(2, "No such file or directory", "codex")
        if name == "record_failure":
            recorded.update(state)
            return {"status": "environmental_fault_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {},
            execute,
            workflow_core.DispatchRetryContext(attempt=3, maximum_attempts=3),
        )
    )

    assert result["status"] == "environmental_fault"
    assert recorded["failure_class"] == "environment"
    assert "retry_exhausted" not in recorded
    assert "worker_result" not in recorded


def test_dispatch_sequence_verification_could_not_start_keeps_retry_budget():
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 0,
                    "stdout": "done",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "verify":
            raise RuntimeError(
                "declared verification did not pass:\n"
                "Ran: (none)\n"
                "Failed: (none)\n"
                "Could not start: `pytest -q` (command not found)"
            )
        if name == "record_failure":
            recorded.update(state)
            return {"status": "environmental_fault_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {},
            execute,
            workflow_core.DispatchRetryContext(attempt=3, maximum_attempts=3),
        )
    )

    assert result["status"] == "environmental_fault"
    assert recorded["failure_class"] == "environment"
    assert "retry_exhausted" not in recorded
    assert "worker_result" not in recorded


def test_reconcile_runs_before_the_claim_on_a_quiet_pass():
    # A task sits in review exactly when the queue is otherwise empty, so this
    # is the pass on which reconciliation matters most.
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        return {"status": "no_task", "exit_code": 0}

    asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert calls == ["reconcile", "claim"]


def test_reconcile_failure_does_not_stop_the_pass_and_is_reported():
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        if name == "reconcile":
            raise RuntimeError("substrate unreachable")
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "propose":
            return {"status": "review", "exit_code": 0, "task_id": "task-1"}
        return {**state, name: True}

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "review"
    assert result["exit_code"] == 0
    assert result["reconcile_error"] == "substrate unreachable"
    assert "isolate" in calls and "propose" in calls


def test_reconcile_failure_is_reported_on_a_pass_that_found_no_work():
    async def execute(name, state):
        if name == "reconcile":
            raise RuntimeError("gh rate limited")
        return {"status": "no_task", "exit_code": 0}

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "no_task"
    assert result["reconcile_error"] == "gh rate limited"


def test_reconcile_receives_the_request_not_the_claim_state():
    seen: dict[str, dict] = {}

    async def execute(name, state):
        seen[name] = state
        return {"status": "no_task", "exit_code": 0}

    asyncio.run(workflow_core.run_dispatch_sequence({"dry_run": True}, execute))

    assert seen["reconcile"] == {"dry_run": True}


def _wrapped(outer: str, inner: BaseException) -> Exception:
    """Build the shape Temporal raises: a generic wrapper over the real cause."""
    error = RuntimeError(outer)
    error.__cause__ = inner
    return error


def test_describe_failure_recovers_the_cause_temporal_hides():
    # Temporal's ActivityError stringifies to exactly this, and every real
    # dispatcher failure arrived at the bead looking like it.
    exc = _wrapped("Activity task failed", RuntimeError("declared verification did not pass"))

    assert workflow_core.describe_failure(exc) == (
        "Activity task failed: declared verification did not pass"
    )


def test_describe_failure_keeps_the_whole_chain_in_order():
    exc = _wrapped(
        "Activity task failed",
        _wrapped("worker exited 1", RuntimeError("pytest-asyncio is not installed")),
    )

    assert workflow_core.describe_failure(exc) == (
        "Activity task failed: worker exited 1: pytest-asyncio is not installed"
    )


def test_describe_failure_does_not_repeat_a_message_the_wrapper_copied():
    exc = _wrapped("worker changed nothing", RuntimeError("worker changed nothing"))

    assert workflow_core.describe_failure(exc) == "worker changed nothing"


def test_describe_failure_terminates_on_a_cyclic_cause():
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert workflow_core.describe_failure(first) == "first: second"


def test_describe_failure_names_the_type_when_there_is_no_message():
    assert workflow_core.describe_failure(TimeoutError()) == "TimeoutError"


def test_dispatch_sequence_records_the_cause_not_the_wrapper():
    recorded: dict[str, str] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise _wrapped(
                "Activity task failed",
                RuntimeError("scope guard rejected .github/workflows/lint.yml"),
            )
        if name == "record_failure":
            recorded["reason"] = state["failure_reason"]
            return state
        return state

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert "scope guard rejected .github/workflows/lint.yml" in recorded["reason"]
    assert "scope guard rejected .github/workflows/lint.yml" in result["message"]


def test_dispatch_sequence_detects_capacity_backpressure_through_the_wrapper():
    # The capacity marker arrives on the cause, so a reason built from the
    # wrapper alone would report a plain failure and burn an attempt.
    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise _wrapped(
                "Activity task failed",
                RuntimeError("capacity backpressure: retry_at=Aug 7th 10:43 PM"),
            )
        if name == "record_failure":
            return {"status": "capacity_backpressure_recorded", "task_id": "task-1"}
        return state

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "capacity_backpressure"
    assert "retry_at=Aug 7th 10:43 PM" in result["message"]


def test_dispatch_sequence_returns_unclaimed_result_without_cleanup():
    calls: list[str] = []

    async def execute(name, state):
        calls.append(name)
        return {"status": "no_task", "exit_code": 0}

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result == {"status": "no_task", "exit_code": 0}
    assert calls == ["reconcile", "claim"]


def _heartbeat_timeout_death(step: str) -> Exception:
    """Build the shape Temporal raises when heartbeat_timeout fires: an
    ActivityError wrapping temporalio.exceptions.TimeoutError(type=HEARTBEAT).
    Duck-typed (a bare object with a `.name` attribute standing in for the
    real TimeoutType enum member) so this test carries no temporalio import,
    matching workflow_core's own dependency-free design."""
    timeout_type = SimpleNamespace(name="HEARTBEAT")
    cause = RuntimeError("Timeout")
    cause.type = timeout_type
    return _wrapped(f"Activity task failed ({step})", cause)


def _start_to_close_timeout_death(step: str) -> Exception:
    """Build the shape Temporal raises when a short step's start_to_close_timeout
    fires (2026-09-11 wedge fix): an ActivityError wrapping
    temporalio.exceptions.TimeoutError(type=START_TO_CLOSE), the same
    duck-typed shape _heartbeat_timeout_death builds for HEARTBEAT."""
    timeout_type = SimpleNamespace(name="START_TO_CLOSE")
    cause = RuntimeError("Timeout")
    cause.type = timeout_type
    return _wrapped(f"Activity task failed ({step})", cause)


def test_dispatch_sequence_classifies_heartbeat_timeout_environmental_before_worker_result_exists():
    """The common case (isolate/preflight/run all die before a worker_result
    exists): already worked before this change, via worker_result_present
    alone. Kept as a regression guard."""
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "preflight":
            raise _heartbeat_timeout_death("preflight")
        if name == "record_failure":
            recorded.update(state)
            status = (
                "environmental_fault_recorded"
                if state.get("failure_class") == "environment"
                else "failure_recorded"
            )
            return {"status": status, "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {}, execute, workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3)
        )
    )

    assert result["status"] == "environmental_fault"
    assert recorded["failure_class"] == "environment"


def test_dispatch_sequence_classifies_heartbeat_timeout_environmental_after_worker_result_exists():
    """The risky case: verify and propose both run after `run` already put a
    worker_result on the state, so classify_dispatch_failure's usual
    worker_result_present heuristic alone would call this a work failure and
    spend the bead's retry budget on what was actually a dropped connection
    (2026-09-03 tunnel-flap incident). The heartbeat-timeout signal must
    override that heuristic instead of losing to it."""
    calls: list[str] = []
    recorded: dict[str, object] = {}

    async def execute(name, state):
        calls.append(name)
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "verify":
            raise _heartbeat_timeout_death("verify")
        if name == "record_failure":
            recorded.update(state)
            status = (
                "environmental_fault_recorded"
                if state.get("failure_class") == "environment"
                else "failure_recorded"
            )
            return {"status": status, "task_id": "task-1"}
        return state

    result = asyncio.run(
        workflow_core.run_dispatch_sequence(
            {}, execute, workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3)
        )
    )

    # No DispatchAttemptFailed escaped asyncio.run: the misclassification
    # this test guards against would have raised one here instead of
    # returning, since a work failure with a retry_context left burns an
    # attempt rather than completing the workflow run.
    assert result["status"] == "environmental_fault"
    assert recorded["failure_class"] == "environment"
    assert "worker_result" not in recorded
    assert calls == [
        "reconcile",
        "claim",
        "isolate",
        "preflight",
        "run",
        "contain",
        "scope",
        "smoke",
        "verify",
        "record_failure",
        "cleanup",
    ]


def test_dispatch_sequence_lost_short_step_does_not_spend_the_bead_retry_budget():
    """2026-09-11 wedge fix: contain/scope/smoke run after `run` already put a
    worker_result on the state (like verify/propose above), but unlike
    verify/propose they never heartbeat -- they are three of the seven steps
    this bead bounds with a short start_to_close_timeout instead. A start-to-
    close timeout death on one of THEM must classify environmental too, and
    must do so regardless of which workflow attempt this is: the
    workflow-level retry (DISPATCH_RETRY_POLICY) is the only thing that may
    ever count a bead's attempts (DispatchRetryContext's own docstring), so a
    step-level recovery here must never make that count depend on attempt
    number. Checked at attempt 1 and attempt 3 to pin that."""
    for step in ("contain", "scope", "smoke"):
        for attempt in (1, 3):
            recorded: dict[str, object] = {}

            async def execute(name, state, _step=step, _recorded=recorded):
                if name == "claim":
                    return {"status": "claimed", "task": {"id": "task-1"}}
                if name == "run":
                    return {
                        **state,
                        "worker_result": {
                            "exit_code": 0,
                            "stdout": "ok",
                            "stderr": "",
                            "duration_s": 1.0,
                            "timed_out": False,
                        },
                    }
                if name == _step:
                    raise _start_to_close_timeout_death(_step)
                if name == "record_failure":
                    _recorded.clear()
                    _recorded.update(state)
                    status = (
                        "environmental_fault_recorded"
                        if state.get("failure_class") == "environment"
                        else "failure_recorded"
                    )
                    return {"status": status, "task_id": "task-1"}
                return state

            result = asyncio.run(
                workflow_core.run_dispatch_sequence(
                    {},
                    execute,
                    workflow_core.DispatchRetryContext(attempt=attempt, maximum_attempts=3),
                )
            )

            # No DispatchAttemptFailed escaped asyncio.run: a work-failure
            # misclassification here would have raised one instead of
            # returning, spending a workflow-level retry on a lost connection.
            assert result["status"] == "environmental_fault", (step, attempt)
            assert recorded["failure_class"] == "environment", (step, attempt)
            assert "worker_result" not in recorded, (step, attempt)


def test_dispatch_sequence_start_to_close_timeout_on_run_itself_is_not_swept_into_environmental():
    """A start_to_close timeout on `run` -- unlike contain/scope/smoke -- is a
    genuine worker-verdict window (24h, unchanged by this bead) and is not one
    of workflow_core.NON_HEARTBEATING_DISPATCH_STEPS. It must keep whatever
    classification classify_dispatch_failure would otherwise give it, not be
    misclassified environmental just because the exception shape matches."""
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            raise _start_to_close_timeout_death("run")
        if name == "record_failure":
            recorded.update(state)
            status = (
                "environmental_fault_recorded"
                if state.get("failure_class") == "environment"
                else "failure_recorded"
            )
            return {"status": status, "task_id": "task-1"}
        return state

    try:
        asyncio.run(
            workflow_core.run_dispatch_sequence(
                {}, execute, workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3)
            )
        )
        raised = False
    except workflow_core.DispatchAttemptFailed:
        raised = True

    # `run` has no worker_result yet when it dies, so worker_result_present
    # alone already classifies this environmental via the pre-existing rule
    # (not the new short-step override) -- this pins that NON_HEARTBEATING_
    # DISPATCH_STEPS membership is what gates the new override, not "any
    # START_TO_CLOSE-shaped exception."
    assert not raised
    assert recorded["failure_class"] == "environment"


def test_start_to_close_on_a_heartbeating_step_after_run_stays_a_work_failure():
    """Release-gate F1 (#896): the step-gate is what stops a real failure being laundered.

    `_is_lost_short_step_death` requires `step in NON_HEARTBEATING_DISPATCH_STEPS`. Without that
    clause, ANY start_to_close-shaped death becomes an infrastructure death, and
    `infrastructure_timeout` "is checked first and overrides every other rule" in
    retry_policy.classify_dispatch_failure -- so a genuine work failure would be recorded as
    environment, never charged to the attempt budget.

    `verify` is the discriminating case, and the sibling test at
    ...start_to_close_timeout_on_run_itself... is NOT: `run` dies before any worker_result exists,
    so the pre-existing worker_result_present rule already returns `environment` there and the
    assertion passes with or without the gate. `verify` runs AFTER `run` has produced a
    worker_result, and it heartbeats (`_verify_declared_commands_with_heartbeat`), so a
    start_to_close death on it is a real 24h verdict window expiring on the work -- WORK, not
    infrastructure.

    Shown failing by deleting `step in NON_HEARTBEATING_DISPATCH_STEPS and` from
    workflow_core._is_lost_short_step_death: the whole suite (1869 passed) stayed green before this
    test existed, which is why it exists.
    """
    recorded: dict[str, object] = {}

    async def execute(name, state):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "run":
            return {
                **state,
                "worker_result": {
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "duration_s": 1.0,
                    "timed_out": False,
                },
            }
        if name == "verify":
            raise _start_to_close_timeout_death("verify")
        if name == "record_failure":
            recorded.clear()
            recorded.update(state)
            status = (
                "environmental_fault_recorded"
                if state.get("failure_class") == "environment"
                else "failure_recorded"
            )
            return {"status": status, "task_id": "task-1"}
        return state

    assert "verify" not in workflow_core.NON_HEARTBEATING_DISPATCH_STEPS
    assert "verify" in workflow_core.HEARTBEATING_DISPATCH_STEPS

    try:
        asyncio.run(
            workflow_core.run_dispatch_sequence(
                {}, execute, workflow_core.DispatchRetryContext(attempt=1, maximum_attempts=3)
            )
        )
        raised = False
    except workflow_core.DispatchAttemptFailed:
        raised = True

    assert recorded["failure_class"] == "work", (
        "a start_to_close death on a heartbeating step that already produced a worker_result is "
        "the work's own verdict window expiring -- classifying it as infrastructure would make it "
        "consume no retry and re-dispatch on a real failure"
    )
    assert raised, "a work failure must raise DispatchAttemptFailed so Temporal charges the attempt"


def test_non_heartbeating_dispatch_steps_is_derived_not_hand_listed():
    """The seven steps this bead bounds -- claim, contain, scope, smoke,
    reconcile, record_failure, cleanup -- must come from DISPATCH_STEPS and
    NON_SEQUENCE_DISPATCH_STEPS minus HEARTBEATING_DISPATCH_STEPS, so a step
    added to either collection later cannot silently inherit the 24h bound by
    omission."""
    assert workflow_core.NON_HEARTBEATING_DISPATCH_STEPS == {
        "claim",
        "contain",
        "scope",
        "smoke",
        "reconcile",
        "record_failure",
        "cleanup",
    }
    assert workflow_core.NON_HEARTBEATING_DISPATCH_STEPS == (
        frozenset(
            workflow_core.DISPATCH_STEPS + workflow_core.NON_SEQUENCE_DISPATCH_STEPS
        )
        - workflow_core.HEARTBEATING_DISPATCH_STEPS
    )


def test_start_to_close_timeout_for_step_bounds_the_seven_short_steps_well_under_24h():
    for step in workflow_core.NON_HEARTBEATING_DISPATCH_STEPS:
        bound = workflow_core.start_to_close_timeout_for_step(step)
        assert bound < workflow_core.ACTIVITY_START_TO_CLOSE_TIMEOUT
        # Comfortably under schedule_status.py's 45-minute wedge alarm, so
        # the bound itself resolves most wedges before that alarm ever fires.
        assert bound <= workflow_core.RECONCILE_STEP_TIMEOUT

    for step in workflow_core.HEARTBEATING_DISPATCH_STEPS:
        assert (
            workflow_core.start_to_close_timeout_for_step(step)
            == workflow_core.ACTIVITY_START_TO_CLOSE_TIMEOUT
        )


def test_lost_short_step_start_to_close_bound_is_minutes_not_the_24h_run_budget():
    """Timeline demonstration, mirroring
    test_dispatch_activity_liveness.test_lost_worker_attempt_is_retryable_within_declared_heartbeat_bound:
    a `contain` activity lost the moment it is dispatched (the 2026-09-11
    shape -- the connection dropped before the worker ever ran it, so nothing
    will ever answer) becomes eligible to retry when its OWN bound elapses,
    not 24 hours later."""
    from datetime import datetime, timedelta, timezone

    lost_at = datetime(2026, 9, 11, 13, 0, 0, tzinfo=timezone.utc)
    new_bound = workflow_core.start_to_close_timeout_for_step("contain")
    old_bound = workflow_core.ACTIVITY_START_TO_CLOSE_TIMEOUT

    retry_eligible_at = lost_at + new_bound
    old_retry_eligible_at = lost_at + old_bound

    assert retry_eligible_at - lost_at < timedelta(minutes=15)
    assert retry_eligible_at < old_retry_eligible_at
    assert old_retry_eligible_at - retry_eligible_at > timedelta(hours=23)
