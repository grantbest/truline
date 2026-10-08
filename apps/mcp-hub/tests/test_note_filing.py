"""``tools.note_filing`` files a dev.note bead by calling the
factory-dispatcher's own ``substrate.Substrate.add_note`` -- never a
re-derived write path (PRIN-005, see ``.factory/design.md``). No real
substrate, no clone, no network: the store here is a small in-memory fake
implementing only ``add_note``, the one method ``file_dev_note`` calls
(apps/mcp-hub/tests is watched by the same hand-rolled-double discipline
apps/factory-dispatcher/tests' ratchet enforces there -- a double covers
what its own test file calls, nothing more).
"""

from __future__ import annotations

import pytest

from tools import note_filing


class FakeStore:
    def __init__(self) -> None:
        self.notes: list[dict] = []

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
        content = {"kind": kind, "body": body}
        content.update({k: v for k, v in extra.items() if v is not None})
        note = {
            "id": f"note-{len(self.notes)}",
            "parent_id": parent_id,
            "state": "active",
            "trust_tier": trust_tier,
            "created_by": created_by,
            "content": content,
        }
        self.notes.append(note)
        return note


class FakeRefusingStore:
    """Raises a SubstrateError-shaped object -- duck-typed on ``.status``
    and ``.body``, the exact shape
    apps/factory-dispatcher/substrate.py::SubstrateError has -- so
    file_dev_note's mapping to FilingRefused can be proven without
    depending on that module being importable (this test never sets
    FACTORY_DISPATCHER_ROOT)."""

    class _Refused(RuntimeError):
        def __init__(self, status, body):
            super().__init__(f"substrate {status}: {body}")
            self.status = status
            self.body = body

    def add_note(self, *args, **kwargs):
        raise self._Refused(409, "open_questions: answers_ref does not resolve to an open question")


def test_forwards_kind_body_and_fields_to_add_note():
    store = FakeStore()
    bead = note_filing.file_dev_note(
        "task-1",
        "answer",
        "the fix ships in v2",
        "agent-dev",
        "service",
        fields={"answers_ref": "note-9", "releases_work": True},
        store=store,
    )

    assert bead["id"] == store.notes[0]["id"]
    assert store.notes[0]["parent_id"] == "task-1"
    assert store.notes[0]["content"]["kind"] == "answer"
    assert store.notes[0]["content"]["answers_ref"] == "note-9"
    assert store.notes[0]["content"]["releases_work"] is True


def test_trust_tier_is_derived_from_client_type_human():
    store = FakeStore()
    note_filing.file_dev_note("task-1", "comment", "hi", "operator@example.org", "human", store=store)

    assert store.notes[0]["trust_tier"] == "user"


def test_trust_tier_is_derived_from_client_type_non_human():
    store = FakeStore()
    note_filing.file_dev_note("task-1", "comment", "status update", "agent-dev", "service", store=store)

    assert store.notes[0]["trust_tier"] == "system"


def test_created_by_is_the_real_client_identity_not_a_constant():
    store = FakeStore()
    note_filing.file_dev_note("task-1", "comment", "hi", "agent-review", "service", store=store)

    assert store.notes[0]["created_by"] == "agent-review"
    assert store.notes[0]["created_by"] != "lifeops-console/factory-board"


def test_fields_omitted_from_the_caller_are_never_invented():
    store = FakeStore()
    note_filing.file_dev_note("task-1", "comment", "hi", "agent-dev", "service", store=store)

    assert "releases_work" not in store.notes[0]["content"]
    assert "answers_ref" not in store.notes[0]["content"]
    assert "verdict" not in store.notes[0]["content"]


def test_releases_work_false_is_preserved_not_dropped():
    store = FakeStore()
    note_filing.file_dev_note(
        "task-1",
        "answer",
        "hold for now",
        "agent-dev",
        "service",
        fields={"answers_ref": "note-9", "releases_work": False},
        store=store,
    )

    assert store.notes[0]["content"]["releases_work"] is False


def test_unset_env_var_raises_plainly(monkeypatch):
    monkeypatch.delenv(note_filing.FACTORY_DISPATCHER_ROOT_ENV, raising=False)

    with pytest.raises(note_filing.Unavailable):
        note_filing.file_dev_note("task-1", "comment", "hi", "agent-dev", "service")


def test_store_refusal_is_translated_to_filing_refused_with_the_stores_status_and_body():
    store = FakeRefusingStore()

    with pytest.raises(note_filing.FilingRefused) as excinfo:
        note_filing.file_dev_note(
            "task-1",
            "answer",
            "shipping now",
            "agent-dev",
            "service",
            fields={"answers_ref": "note-9", "releases_work": True},
            store=store,
        )

    assert excinfo.value.status == 409
    assert "open_questions" in excinfo.value.body
