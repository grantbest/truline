"""The A30 design note carries provenance, and its rejection never kills a run.

dev.task 20025031: the architect persona's design note posted with
``dispatch.CREATED_BY`` (an agent author) and no provenance record, so the
substrate 422'd the write on its first live call and the raised
``SubstrateError`` killed the dispatch. The tests here hold both halves of
the fix, against a double that rejects exactly what schemas.py rejects — a
double that accepts more than the live contract is a second implementation,
and that is precisely how the bare call shipped green.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "apps" / "substrate" / "src"))

import dispatch  # noqa: E402
import substrate  # noqa: E402
from schemas import BeadProvenance, _created_by_is_agent  # noqa: E402

from test_dispatch_activity_liveness import (  # noqa: E402
    _fresh_import,
    _install_temporal_stubs,
)


class ValidatingFakeSubstrate:
    """Rejects agent-authored notes the way the live schema does.

    The validators are imported from apps/substrate/src/schemas.py rather
    than re-implemented, so this double cannot drift looser than the server.
    """

    def __init__(self, fail_with: Exception | None = None):
        self.notes: list[dict] = []
        self._fail_with = fail_with

    def add_note(
        self,
        parent_id,
        kind,
        body,
        created_by,
        trust_tier="system",
        provenance=None,
        **extra,
    ):
        if self._fail_with is not None:
            raise self._fail_with
        if _created_by_is_agent(created_by) and not provenance:
            raise substrate.SubstrateError(
                422,
                '{"detail":[{"type":"value_error","loc":["body"],"msg":"Value '
                'error, agent-authored beads require a complete provenance record"}]}',
            )
        if provenance:
            BeadProvenance.model_validate(provenance)
        self.notes.append(
            {
                "parent_id": parent_id,
                "kind": kind,
                "body": body,
                "created_by": created_by,
                "provenance": provenance,
            }
        )
        return {"id": "note-1"}


def test_the_double_rejects_the_pre_fix_call_shape():
    """The exact call the dispatcher made before the fix must fail here.

    If this test ever passes with a bare add_note, the double has drifted
    looser than the substrate and every test above it proves nothing.
    """
    fake = ValidatingFakeSubstrate()
    with pytest.raises(substrate.SubstrateError) as excinfo:
        fake.add_note(
            "task-1",
            "comment",
            "Architect/SME design judgment (Amendment 30):\n\njudgment",
            dispatch.CREATED_BY,
        )
    assert excinfo.value.status == 422
    assert "provenance" in excinfo.value.body


def _persona_state(clone: Path) -> dict:
    return {
        "task": {"id": "task-1"},
        "worker": {
            "name": "claude",
            "argv": ["claude", "-p"],
            "uses_personas": True,
        },
        "prompt": "do the work",
        "clone": str(clone),
        "budget": dispatch.DEFAULT_BUDGET_MINUTES,
    }


def _run_with(monkeypatch, tmp_path, fake, design_body="# Design\n\nkeep it small"):
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: fake)

    # A persona-using worker materialises its agents from the clone itself
    # (Amendment 30 PR-5), so the synthetic clone carries the real ones.
    shutil.copytree(REPO / "docs" / "agents", tmp_path / "docs" / "agents")

    design = tmp_path / ".factory" / "design.md"
    design.parent.mkdir(parents=True)
    design.write_text(design_body)

    release = threading.Event()
    release.set()

    def fake_run_worker(_prompt, _clone, _budget, _argv, *_a, **_k):
        return dispatch.WorkerResult(
            exit_code=0,
            stdout="ok",
            stderr=None,
            duration_s=42.5,
            timed_out=False,
        )

    return dispatch_steps._run_worker_with_heartbeat(
        _persona_state(tmp_path),
        run_worker=fake_run_worker,
        heartbeat=lambda _details: None,
        heartbeat_interval=0.001,
    )


def test_design_note_posts_with_a_complete_provenance_record(monkeypatch, tmp_path):
    fake = ValidatingFakeSubstrate()
    result = _run_with(monkeypatch, tmp_path, fake)

    assert result.exit_code == 0
    assert len(fake.notes) == 1
    note = fake.notes[0]
    assert note["parent_id"] == "task-1"
    assert note["body"].startswith("Architect/SME design judgment (Amendment 30):")
    # The record itself must satisfy the live schema, name the worker that
    # authored the judgment, and point at the originating dev.task run.
    record = BeadProvenance.model_validate(note["provenance"])
    assert record.worker == "claude"
    assert record.prompt_ref == "dev.task/task-1"
    assert record.duration_s == 42.5


def test_a_rejected_design_note_is_logged_and_does_not_kill_the_run(
    monkeypatch, tmp_path, caplog
):
    rejection = substrate.SubstrateError(422, "synthetic rejection detail")
    fake = ValidatingFakeSubstrate(fail_with=rejection)

    with caplog.at_level(logging.WARNING, logger="factory-dispatcher.dispatch-steps"):
        result = _run_with(
            monkeypatch, tmp_path, fake, design_body="First judgment line\nmore"
        )

    # The run completed normally: the comment is lost, the work is not.
    assert result.exit_code == 0
    assert fake.notes == []
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "task-1" in joined
    assert "synthetic rejection detail" in joined
    assert "First judgment line" in joined


def test_a_worker_without_personas_posts_no_design_note(monkeypatch, tmp_path):
    fake = ValidatingFakeSubstrate()
    _install_temporal_stubs(monkeypatch)
    dispatch_steps = _fresh_import(monkeypatch, "activities.dispatch_steps")
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: fake)

    design = tmp_path / ".factory" / "design.md"
    design.parent.mkdir(parents=True)
    design.write_text("orphan judgment")

    state = _persona_state(tmp_path)
    state["worker"]["uses_personas"] = False

    def fake_run_worker(_prompt, _clone, _budget, _argv, *_a, **_k):
        return dispatch.WorkerResult(
            exit_code=0, stdout="ok", stderr=None, duration_s=1.0, timed_out=False
        )

    result = dispatch_steps._run_worker_with_heartbeat(
        state,
        run_worker=fake_run_worker,
        heartbeat=lambda _details: None,
        heartbeat_interval=0.001,
    )
    assert result.exit_code == 0
    assert fake.notes == []
