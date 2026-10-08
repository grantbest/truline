"""Capacity-backpressure tests for dispatcher activities."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


USAGE_LIMIT_OUTPUT = (
    "worker exited 1\n"
    "You've hit your usage limit. Please try again at Aug 7th 10:43 PM."
)


def task(**content):
    base = {
        "lane": "code-health",
        "title": "t",
    }
    base.update(content)
    return {"id": "task-1", "content": base}


def prior_failure_note(n: int) -> dict:
    return {
        "content": {"kind": "status", "body": f"Run failed: attempt {n} boom"},
        "created_at": f"2026-08-0{n}T00:00:00Z",
    }


class FakeSubstrate:
    def __init__(self, existing_notes=None):
        self.notes = []
        self.patches = []
        self.states = []
        self.existing_notes = list(existing_notes or [])

    def add_note(
        self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
    ):
        args = (parent_id, kind, body, created_by)
        kwargs = dict(extra)
        if provenance is not None:
            kwargs["provenance"] = provenance
        self.notes.append((args, kwargs))

    def list_notes(self, _task_id):
        return list(self.existing_notes)

    def patch_content(self, bead_id, content, created_by):
        self.patches.append((bead_id, content, created_by))

    def set_state(self, bead_id, state, created_by):
        self.states.append((bead_id, state, created_by))


def failed_state(output: str):
    return {
        "task": task(),
        "worker": {"name": "codex"},
        "run_started": time.time() - 4,
        "failure_reason": f"worker exited 1: {output}",
        "worker_result": {
            "exit_code": 1,
            "stdout": output,
            "duration_s": 4.0,
            "timed_out": False,
        },
    }


def changed_nothing_state(tmp_path: Path, output: str):
    cfg = dispatch.Config(repo_root=tmp_path)
    clone = tmp_path / "clone"
    clone.mkdir()
    return {
        "cfg": dispatch_steps._cfg_to_state(cfg),
        "task": task(max_attempts=5),
        "worker": {"name": "codex", "argv": ["codex", "exec"]},
        "run_started": time.time() - 4,
        "prompt": "do the task",
        "budget": 20,
        "workdir": str(tmp_path),
        "clone": str(clone),
        "before": {"head": "head-before", "status": ""},
        "worker_result": {
            "exit_code": 0,
            "stdout": output,
            "duration_s": 4.0,
            "timed_out": False,
        },
    }


def environmental_state(reason: str):
    return {
        "task": task(),
        "worker": {"name": "codex"},
        "run_started": time.time() - 4,
        "failure_reason": reason,
    }


def test_preflight_activity_failure_is_environmental(monkeypatch, tmp_path):
    state = {
        "clone": str(tmp_path),
        "task": task(verification={"commands": ["pytest -q"]}),
    }
    report = dispatch.VerificationReport(
        (
            dispatch.VerificationCommandResult(
                command="pytest -q",
                outcome="failed",
                exit_code=1,
                output="failed before work",
                duration_s=0.1,
            ),
        ),
        bootstrap_note="stub bootstrap",
    )
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: report)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.preflight_activity(state)

    assert "pytest -q" in str(excinfo.value)
    assert "failed before the worker ran" in str(excinfo.value)
    assert "failure preceded the work" in str(excinfo.value)


def test_record_failure_environmental_fault_leaves_attempts_unchanged(monkeypatch):
    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(
        environmental_state(
            "Declared verification command `pytest -q` failed before the worker ran. "
            "The failure preceded the work."
        )
    )

    assert result["status"] == "environmental_fault_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    note = sub.notes[-1][0][2]
    assert "Environmental fault" in note
    assert "pytest -q" in note
    assert "failed before the worker ran" in note
    assert "without incrementing attempts" in note
    assert "Run failed:" not in note


def test_record_failure_pauses_schedule_and_leaves_attempts_unchanged(monkeypatch):
    sub = FakeSubstrate()
    pause_notes = []

    async def fake_pause(note: str) -> None:
        pause_notes.append(note)

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps,
        "pause_dispatch_schedule_for_capacity_failure",
        fake_pause,
    )

    result = dispatch_steps.record_failure_activity(failed_state(USAGE_LIMIT_OUTPUT))

    assert result["status"] == "capacity_backpressure_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    assert len(pause_notes) == 1
    assert "usage limit exhaustion" in pause_notes[0]
    assert "retry_at=Aug 7th 10:43 PM" in pause_notes[0]
    note = sub.notes[-1][0][2]
    assert "Capacity backpressure" in note
    assert "without incrementing attempts" in note
    assert "Temporal schedule paused" in note
    assert "retry_at=Aug 7th 10:43 PM" in note


def test_record_failure_pausing_schedule_raises_the_declared_capacity_alert(monkeypatch):
    """Capacity backpressure pausing the queue must announce itself, not
    just leave a bead note on whichever task happened to be running."""
    import failure_diagnosis

    sub = FakeSubstrate()

    async def fake_pause(note: str) -> None:
        pass

    announced: list[tuple] = []
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps, "pause_dispatch_schedule_for_capacity_failure", fake_pause
    )
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_capacity_pause",
        lambda bead_id, cause, schedule_status, **_k: announced.append(
            (bead_id, cause, schedule_status)
        )
        or True,
    )

    result = dispatch_steps.record_failure_activity(failed_state(USAGE_LIMIT_OUTPUT))

    assert result["status"] == "capacity_backpressure_recorded"
    assert len(announced) == 1
    bead_id, cause, schedule_status = announced[0]
    assert bead_id == "task-1"
    assert "usage limit exhaustion" in cause
    assert "Temporal schedule paused" in schedule_status


def test_record_failure_pauses_schedule_for_provider_capacity_stderr(monkeypatch):
    sub = FakeSubstrate()
    pause_notes = []

    async def fake_pause(note: str) -> None:
        pause_notes.append(note)

    state = failed_state("agent transcript before wrapper failure")
    state["worker_result"]["stderr"] = (
        "Error: You've hit your usage limit. Please try again at Aug 7th 10:43 PM."
    )

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps,
        "pause_dispatch_schedule_for_capacity_failure",
        fake_pause,
    )

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] == "capacity_backpressure_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    assert len(pause_notes) == 1
    assert "usage limit exhaustion" in pause_notes[0]
    assert "retry_at=Aug 7th 10:43 PM" in pause_notes[0]


def test_record_failure_pauses_schedule_for_claude_session_limit_exhaustion(monkeypatch):
    """dev.finding 69016c99: six retry attempts were burned on a claude CLI
    "session limit" exhaustion because it never reached this path -- the CLI's
    JSON-graded prose lands in WorkerResult.stdout, not stderr, and the
    classifier only ever consulted stdout when stderr was completely absent.
    Driven through `_grade_json_worker_result` (the real builder `run_worker`
    uses for this worker) and the real `record_failure_activity` call chain,
    faking only `pause_dispatch_schedule_for_capacity_failure` -- the seam
    this file's own tests above already use -- never `Client.connect`."""
    sub = FakeSubstrate()
    pause_notes = []

    async def fake_pause(note: str) -> None:
        pause_notes.append(note)

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps,
        "pause_dispatch_schedule_for_capacity_failure",
        fake_pause,
    )

    doc = {
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "You've hit your session limit · resets 7:30pm (America/Chicago)",
    }
    proc = subprocess.CompletedProcess(
        args=["claude", "exec"], returncode=0, stdout=json.dumps(doc), stderr=""
    )
    worker_result = dispatch._grade_json_worker_result(proc, 4.0)
    # Sanity on the shape: the CLI's prose is only in stdout, the grading
    # message occupies stderr -- this is exactly what the old code missed.
    assert worker_result.stderr == "error_during_execution"

    state = failed_state(worker_result.stdout)
    state["worker_result"] = dispatch_steps._worker_result_to_state(worker_result)

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] == "capacity_backpressure_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    assert len(pause_notes) == 1
    assert "usage limit exhaustion" in pause_notes[0]
    assert "retry_at=7:30pm (America/Chicago)" in pause_notes[0]
    note = sub.notes[-1][0][2]
    assert "Capacity backpressure" in note
    assert "Failure class: capacity" in note
    assert "Failure class: work" not in note


def test_record_failure_does_not_pause_schedule_for_ordinary_worker_failure(monkeypatch):
    sub = FakeSubstrate()
    pause_notes = []

    async def fake_pause(note: str) -> None:
        pause_notes.append(note)

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps,
        "pause_dispatch_schedule_for_capacity_failure",
        fake_pause,
    )

    result = dispatch_steps.record_failure_activity(
        failed_state("pytest failed: assertion error")
    )

    assert result["status"] == "failure_recorded"
    assert pause_notes == []
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    assert "Run failed: worker exited 1" in sub.notes[-1][0][2]


def test_record_failure_terminal_state_derives_from_the_beads_own_failure_notes(monkeypatch):
    """Exhaustion comes from THIS bead's own recorded 'Run failed:' notes, not
    from anything stamped in externally: a bead with two prior failures of its
    own is exhausted on its third."""
    sub = FakeSubstrate(existing_notes=[prior_failure_note(1), prior_failure_note(2)])

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(
        failed_state("pytest failed: assertion error")
    )

    assert result["status"] == "failure_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "failed", dispatch.CREATED_BY)]


def test_record_failure_does_not_fail_a_bead_with_no_failures_of_its_own(monkeypatch):
    """A bead with zero recorded failures of its own must not be marked
    terminally failed on its first failure, regardless of what a Temporal
    workflow's retry-attempt count happens to be (dev.task: cross-bead
    retry misattribution)."""
    sub = FakeSubstrate()

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(
        failed_state("pytest failed: assertion error")
    )

    assert result["status"] == "failure_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]


def test_record_failure_ignores_capacity_words_in_agent_transcript(monkeypatch):
    sub = FakeSubstrate()
    pause_notes = []

    async def fake_pause(note: str) -> None:
        pause_notes.append(note)

    state = failed_state(
        "Investigating rate-limit handling.\n"
        "A fixture contains this provider sentence:\n"
        "+You've hit your usage limit. Please try again at Aug 7th 10:43 PM.\n"
        "pytest failed: assertion error"
    )
    state["worker_result"]["stderr"] = "Error: declared verification failed"

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(
        dispatch_steps,
        "pause_dispatch_schedule_for_capacity_failure",
        fake_pause,
    )

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] == "failure_recorded"
    assert pause_notes == []
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    assert "Run failed: worker exited 1" in sub.notes[-1][0][2]


def test_contain_activity_changed_nothing_raises_worker_stdout(monkeypatch, tmp_path):
    explanation = "I inspected the task and found no change to make."
    state = changed_nothing_state(tmp_path, explanation)
    monkeypatch.setattr(dispatch, "changed_paths", lambda _clone: [])

    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch_steps.contain_activity(state)

    assert "worker changed nothing" in str(excinfo.value)
    assert explanation in str(excinfo.value)


def test_contain_activity_names_the_review_note_when_baseline_was_applied(
    monkeypatch, tmp_path
):
    explanation = "No files were modified; the acceptance criteria were already met."
    state = changed_nothing_state(tmp_path, explanation)
    state["preserved_baseline_refs"] = ["#7"]
    monkeypatch.setattr(dispatch, "changed_paths", lambda _clone: [])

    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch_steps.contain_activity(state)

    assert "preserved baseline" in str(excinfo.value)
    assert "not this attempt's own work" in str(excinfo.value)


def test_record_failure_activity_notes_changed_nothing_stdout(monkeypatch, tmp_path):
    explanation = "I inspected the task and found no change to make."
    state = changed_nothing_state(tmp_path, explanation)
    state["failure_reason"] = dispatch.changed_nothing_failure_reason(
        dispatch.WorkerResult(
            exit_code=0,
            stdout=explanation,
            duration_s=4.0,
            timed_out=False,
        )
    )
    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] == "failure_recorded"
    note = sub.notes[-1][0][2]
    assert "Run failed: worker changed nothing" in note
    assert explanation in note
