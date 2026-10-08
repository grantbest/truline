"""Content/provenance validation above the store (R26.06).

NAMESPACE_TYPE_SCHEMAS and BeadProvenance (apps/substrate/src/schemas.py) used
to be enforceable only by attempting a write and inspecting the substrate's
422/409 — there was no way for the dispatcher to ask "would create_task/add_note
accept this" without calling one of them. ``validate_task_content`` and
``validate_note_content`` answer that question against the exact
``DevTaskContent``/``DevNoteContent``/``BeadProvenance`` models any
``BeadStore`` implementation is ultimately checked against, imported (not
copied) the same way test_design_note_provenance.py's fake substrate already
does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "substrate" / "src"))

from beadstore import validate_note_content, validate_task_content  # noqa: E402
from schemas import validate_bead_content  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def _valid_task_content(**overrides) -> dict:
    content = {
        "lane": "bug-triage",
        "title": "t",
        "intent": "i",
        "context_refs": [],
        "acceptance": ["WHEN x THE y SHALL z"],
        "verification": {"commands": ["true"]},
        "scope": {"paths": ["x"], "forbidden_paths": [".github/workflows/**"]},
        "risk_class": "structural",
        "budget": {"max_agent_minutes": 1, "max_usd": 1.0, "max_tokens": 1},
    }
    content.update(overrides)
    return content


def test_validate_task_content_accepts_a_well_formed_task():
    validate_task_content(_valid_task_content(), created_by="human")


def test_validate_task_content_rejects_what_the_live_schema_rejects():
    """AC3: same schema objects, not a second transcription of them."""
    bad = {"lane": "not-a-real-lane"}
    with pytest.raises(ValidationError) as from_beadstore:
        validate_task_content(bad, created_by="human")
    with pytest.raises(ValidationError) as from_schemas:
        validate_bead_content("dev", "task", bad)
    assert from_beadstore.value.errors() == from_schemas.value.errors()


def test_validate_task_content_rejects_an_agent_author_with_no_provenance():
    """The dev.task 20025031 incident this repo already carries a scar for
    (test_design_note_provenance.py): an agent-authored write with no
    provenance record is exactly what this must catch before create_task is
    ever called, not after the store 422s."""
    with pytest.raises(ValidationError):
        validate_task_content(_valid_task_content(), created_by="claude", provenance={})
    validate_task_content(
        _valid_task_content(),
        created_by="claude",
        provenance={
            "worker": "claude",
            "model": "m",
            "prompt_ref": "p",
            "tokens": None,
            "cost_usd": None,
            "duration_s": 1.0,
        },
    )


def test_validate_note_content_accepts_a_well_formed_note():
    validate_note_content({"kind": "comment", "body": "hi"}, created_by="human")


def test_validate_note_content_rejects_a_question_missing_its_blocking_semantics():
    """DevNoteContent's cross-field rule: ``blocking`` only makes sense on a
    ``question`` note. This is deliberately a rule ``validate_bead_content``
    enforces via a ``model_validator``, not a plain missing-field check, to
    prove the composed helper reaches the real cross-field validation."""
    with pytest.raises(ValidationError):
        validate_note_content({"kind": "comment", "body": "hi", "blocking": True}, created_by="human")
