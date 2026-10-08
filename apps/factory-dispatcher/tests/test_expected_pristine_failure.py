"""Expected pristine verification failures for reproduction-test tasks."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def task(*, expect_pristine_failure: bool | None = None) -> dict:
    verification: dict[str, object] = {"commands": ["python -m pytest tests/ -q"]}
    if expect_pristine_failure is not None:
        verification["expect_pristine_failure"] = expect_pristine_failure
    return {
        "id": "task-1",
        "content": {
            "lane": "bug-triage",
            "title": "reproduction test",
            "attempts": 0,
            "verification": verification,
        },
    }


def state(tmp_path: Path, bead: dict) -> dict:
    return {
        "clone": str(tmp_path),
        "task": bead,
        "worker": {"name": "codex"},
        "run_started": time.time() - 1,
        # verify_activity reads this (contain_activity normally supplies it);
        # empty here since none of these fixtures materialize real changed
        # files in the clone, so no lint command would be appended anyway.
        "paths": [],
    }


def report(*results: dispatch.VerificationCommandResult) -> dispatch.VerificationReport:
    return dispatch.VerificationReport(results, bootstrap_note="stub bootstrap")


def passed(command: str) -> dispatch.VerificationCommandResult:
    return dispatch.VerificationCommandResult(
        command=command,
        outcome="passed",
        exit_code=0,
        output="",
        duration_s=0.1,
    )


def failed(command: str) -> dispatch.VerificationCommandResult:
    return dispatch.VerificationCommandResult(
        command=command,
        outcome="failed",
        exit_code=1,
        output="assertion failed",
        duration_s=0.1,
    )


def could_not_start(command: str) -> dispatch.VerificationCommandResult:
    return dispatch.VerificationCommandResult(
        command=command,
        outcome="could_not_start",
        exit_code=127,
        output="/bin/sh: missing-checker: command not found",
        duration_s=0.1,
        reason="command not found",
    )


class FakeSubstrate:
    def __init__(self) -> None:
        self.notes: list[tuple[tuple, dict]] = []
        self.patches: list[tuple] = []
        self.states: list[tuple] = []

    def add_note(
        self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
    ) -> None:
        args = (parent_id, kind, body, created_by)
        kwargs = dict(extra)
        if provenance is not None:
            kwargs["provenance"] = provenance
        self.notes.append((args, kwargs))

    def patch_content(self, bead_id, content, created_by) -> None:
        self.patches.append((bead_id, content, created_by))

    def set_state(self, bead_id, state, created_by) -> None:
        self.states.append((bead_id, state, created_by))


def test_expected_pristine_failure_proceeds_to_worker(monkeypatch, tmp_path):
    bead = task(expect_pristine_failure=True)
    pristine = report(failed("python -m pytest tests/ -q"))
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: pristine)

    result = dispatch_steps.preflight_activity(state(tmp_path, bead))

    assert result["pristine_verification_expected_failure"] is True
    assert result["pristine_verification_report"]["commands"][0]["outcome"] == "failed"


def test_expected_pristine_pass_is_recorded_as_already_satisfied_without_attempt(
    monkeypatch,
    tmp_path,
):
    bead = task(expect_pristine_failure=True)
    pristine = report(passed("python -m pytest tests/ -q"))
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: pristine)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.preflight_activity(state(tmp_path, bead))

    assert dispatch.PRISTINE_ALREADY_SATISFIED_MARKER in str(excinfo.value)

    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    result = dispatch_steps.record_failure_activity(
        {
            **state(tmp_path, bead),
            "failure_reason": str(excinfo.value),
            "failure_class": "environment",
        }
    )

    assert result["status"] == "already_satisfied_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "failed", dispatch.CREATED_BY)]
    note = sub.notes[-1][0][2]
    # Same wording as the CLI path now (dispatch.record_pristine_already_
    # satisfied) — converged from a dispatch_steps-local duplicate that used
    # to phrase the identical event differently.
    assert "Already-satisfied claim:" in note
    assert "No worker was dispatched" in note
    assert "without spending an attempt" in note


def test_expected_pristine_failure_does_not_change_post_work_failure(
    monkeypatch,
    tmp_path,
):
    bead = task(expect_pristine_failure=True)
    post_work = report(failed("python -m pytest tests/ -q"))
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: post_work)

    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch_steps.verify_activity(state(tmp_path, bead))

    assert "declared verification did not pass" in str(excinfo.value)
    assert "Failed: `python -m pytest tests/ -q` (exit 1)" in str(excinfo.value)


def test_pristine_failure_without_expectation_stays_environmental(
    monkeypatch,
    tmp_path,
):
    bead = task()
    pristine = report(failed("python -m pytest tests/ -q"))
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: pristine)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.preflight_activity(state(tmp_path, bead))

    assert "failed before the worker ran" in str(excinfo.value)
    assert "failure preceded the work" in str(excinfo.value)


def test_expected_pristine_failure_still_treats_startup_failure_as_environmental(
    monkeypatch,
    tmp_path,
):
    bead = task(expect_pristine_failure=True)
    pristine = report(could_not_start("missing-checker --strict"))
    monkeypatch.setattr(dispatch, "verify_declared_commands", lambda *_args: pristine)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.preflight_activity(state(tmp_path, bead))

    assert "failed before the worker ran" in str(excinfo.value)
    assert "Could not start: `missing-checker --strict`" in str(excinfo.value)
