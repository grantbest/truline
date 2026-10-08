"""Stale-base-ref handling on the Temporal activity path.

`dispatch_once` (the CLI/sync path) has the raised BaseRefStaleError
directly and can catch it by type. `activities/dispatch_steps.py`'s
`record_failure_activity` cannot: a Temporal ActivityError loses the
original exception type crossing the workflow boundary (see
`_authentication_failure_from_state`'s docstring for the established
pattern this mirrors), so a stale base ref must be re-detected from the
flattened failure text via `dispatch.is_stale_base_ref_reason`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import workflow_core  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def task(**content):
    base = {
        "lane": "code-health",
        "title": "t",
    }
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


def stale_base_ref_state():
    status = dispatch.BaseRefStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        local_only=0,
        upstream_only=4,
    )
    reason = str(dispatch.BaseRefStaleError(status))
    return {
        "task": task(),
        "worker": {"name": "codex"},
        "run_started": time.time() - 1,
        "failure_reason": reason,
    }


def test_record_failure_recognizes_a_stale_base_ref_from_flattened_text(monkeypatch):
    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(stale_base_ref_state())

    assert result["status"] == "stale_base_ref_recorded"
    assert sub.patches == []
    assert sub.states == [("task-1", "pending", dispatch.CREATED_BY)]
    note = sub.notes[-1][0][2]
    assert note.startswith(dispatch.STALE_BASE_REF_NOTE_PREFIX)
    assert "Environmental fault:" not in note
    assert "refused to clone" in note
    assert "a" * 40 in note
    assert "b" * 40 in note
    assert "not counted toward the environmental-fault bound" in note


def test_record_failure_does_not_misclassify_an_ordinary_environmental_fault(monkeypatch):
    sub = FakeSubstrate()
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    result = dispatch_steps.record_failure_activity(
        {
            "task": task(),
            "worker": {"name": "codex"},
            "run_started": time.time() - 1,
            "failure_reason": (
                "Declared verification command `pytest -q` failed before the "
                "worker ran. The failure preceded the work."
            ),
        }
    )

    assert result["status"] == "environmental_fault_recorded"
    note = sub.notes[-1][0][2]
    assert note.startswith(dispatch.ENVIRONMENTAL_FAULT_NOTE_PREFIX)


# ---------------------------------------------------------------------------
# workflow_core.run_dispatch_sequence -- the "stale_base_ref_recorded" activity
# result surfaces as a distinct terminal status, not lumped into
# "environmental_fault".
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_workflow_maps_stale_base_ref_recorded_to_a_distinct_terminal_status():
    async def execute_activity(name, payload):
        if name == "claim":
            return {"status": "claimed", "task": {"id": "task-1"}}
        if name == "isolate":
            raise RuntimeError("base ref is behind its tracking remote; refused to clone: ...")
        if name == "record_failure":
            return {"status": "stale_base_ref_recorded", "exit_code": 0, "task_id": "task-1"}
        if name == "cleanup":
            return {}
        raise AssertionError(f"unexpected activity {name}")

    result = run(workflow_core.run_dispatch_sequence({}, execute_activity))

    assert result["status"] == "stale_base_ref"
    assert result["exit_code"] == 1
    assert result["task_id"] == "task-1"
