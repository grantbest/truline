"""Authentication-classification tests for dispatcher activities.

2026-08-21 finding: contain_activity (activities/dispatch_steps.py) called
dispatch.raise_for_worker_failure without the clone path it already held, so
the authentication branch (#452/#465) was silently skipped in the only path
production dispatches through since Amendment 24. Every prior authentication
test drove dispatch.dispatch_once, which classifies by exception *type* and
never exercised contain_activity's missing argument -- the tests were green
while six attempts burned across two beads. These tests drive the actual
Temporal activity path instead: contain_activity, then record_failure_activity,
exactly as workflow_core.run_dispatch_sequence chains them in production.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


# Live 2026-08-19 11:32:41Z, bead a3c2ad3b (verbatim, per #452/#465): a
# subscription OAuth token expired mid-run.
OAUTH_EXPIRED_ERROR_OUTPUT = (
    "worker exited 1\n"
    "Failed to authenticate. API Error: 401 OAuth access token has expired. "
    "Re-authenticate to continue."
)


def task(**content):
    base = {"lane": "code-health", "title": "t"}
    base.update(content)
    return {"id": "task-1", "content": base}


class FakeSubstrate:
    def __init__(self):
        self.notes = []
        self.patches = []
        self.states = []

    def add_note(
        self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
    ):
        args = (parent_id, kind, body, created_by)
        kwargs = dict(extra)
        if provenance is not None:
            kwargs["provenance"] = provenance
        self.notes.append((args, kwargs))

    def list_notes(self, _task_id):
        return []

    def patch_content(self, bead_id, content, created_by):
        self.patches.append((bead_id, content, created_by))

    def set_state(self, bead_id, state, created_by):
        self.states.append((bead_id, state, created_by))


def _contained_state(tmp_path: Path, output: str) -> dict:
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
            "exit_code": 1,
            "stdout": output,
            "duration_s": 2.0,
            "timed_out": False,
        },
    }


def test_contain_activity_carries_clone_into_raise_for_worker_failure(monkeypatch, tmp_path):
    """The call contain_activity makes must be the 3-arg form: the auth
    branch is only reachable when clone is carried through."""
    state = _contained_state(tmp_path, OAUTH_EXPIRED_ERROR_OUTPUT)
    monkeypatch.setattr(dispatch, "changed_paths", lambda _clone: [])

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.contain_activity(state)

    assert "authenticate" in str(excinfo.value)


def test_contain_activity_authentication_failure_with_diff_stays_work_classified(
    monkeypatch, tmp_path
):
    """#452/#465 guard, proven in the activity path (not dispatch_once): the
    same output with a workspace diff present stays work-classified."""
    state = _contained_state(tmp_path, OAUTH_EXPIRED_ERROR_OUTPUT)
    monkeypatch.setattr(
        dispatch,
        "changed_paths",
        lambda _clone: ["apps/factory-dispatcher/dispatch.py"],
    )

    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch_steps.contain_activity(state)

    assert not isinstance(excinfo.value, dispatch.DispatchEnvironmentError)
    assert "worker exited 1" in str(excinfo.value)


def test_worker_oauth_expiry_failure_through_the_activity_path_burns_no_attempt(
    monkeypatch, tmp_path
):
    """The verbatim 2026-08-19 OAuth-expiry output, driven through the real
    Temporal activity chain: contain_activity raises, and the state
    workflow_core.run_dispatch_sequence would hand to record_failure (the
    last successfully-returned step's state plus the failure reason) must
    still classify as an environment failure with no attempt burned."""
    state = _contained_state(tmp_path, OAUTH_EXPIRED_ERROR_OUTPUT)
    monkeypatch.setattr(dispatch, "changed_paths", lambda _clone: [])

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch_steps.contain_activity(state)

    # contain_activity never returns on this path, so workflow_core's
    # cleanup_state stays at what "run" returned (state, unchanged) plus the
    # failure reason it recovers from the raised exception.
    failure_state = {**state, "failure_reason": str(excinfo.value)}

    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(failure_state)

    assert result["status"] == "environmental_fault_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    note = sub.notes[-1][0][2]
    assert "Environmental fault" in note
    assert "authenticate" in note
    assert "Failure class: environment" in note
    assert "without incrementing attempts" in note


def test_record_failure_activity_reclassifies_authentication_failure_even_without_clone_fix(
    monkeypatch, tmp_path
):
    """record_failure_activity re-derives the authentication verdict from
    worker_result + clone directly, independent of whatever exception type or
    message text the failing step produced - the defense that keeps
    classification correct even if a future step regresses contain_activity's
    3-arg call."""
    state = _contained_state(tmp_path, OAUTH_EXPIRED_ERROR_OUTPUT)
    state["failure_reason"] = f"worker exited 1: {OAUTH_EXPIRED_ERROR_OUTPUT}"
    monkeypatch.setattr(dispatch, "changed_paths", lambda _clone: [])

    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] == "environmental_fault_recorded"
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]


def test_record_failure_activity_treats_a_tampered_clone_as_containment_not_authentication(
    monkeypatch, tmp_path
):
    """dev.finding 79db3113 part c2, AC-3: _authentication_failure_from_state
    calls dispatch.changed_paths to confirm no workspace diff exists before
    downgrading a 401 to an environment failure. If that call raises
    CloneGitControlTampered instead of returning a path list, this must
    answer False -- the run failed for containment, not authentication --
    and fall through to ordinary failure recording rather than being
    recorded as an environmental fault."""
    state = _contained_state(tmp_path, OAUTH_EXPIRED_ERROR_OUTPUT)
    state["failure_reason"] = f"worker exited 1: {OAUTH_EXPIRED_ERROR_OUTPUT}"

    def tampered(_clone):
        raise dispatch.CloneGitControlTampered(f"{_clone}: .git/config changed")

    monkeypatch.setattr(dispatch, "changed_paths", tampered)
    monkeypatch.setattr(dispatch, "save_failure_patch", lambda _clone, _task_id: None)

    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(state)

    assert result["status"] != "environmental_fault_recorded"
    assert result["status"] == "failure_recorded"
