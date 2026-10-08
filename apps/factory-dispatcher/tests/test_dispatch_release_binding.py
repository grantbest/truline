"""dispatch.py's release-resolution primitives and the --bind-release verb.

A dev.task binds to arch.release by a ``delivers`` edge, never by a string in
its own content — apps/substrate/src/schemas.py's ArchReleaseContent docstring
says why: "everything in this release" has to be one indexed query. The
resolution logic here (``resolve_release_ref``) is the one function both
file_task.py (at filing — see test_file_task_release_traceability.py) and
--bind-release (for work already in flight) call, so the two paths can never
validate a different shape.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402


def _release(ref="R26.01", state="in_flight", outcomes=("O-1", "O-2")):
    return {
        "id": f"release-{ref}",
        "state": state,
        "content": {"ref": ref, "outcomes": [{"id": o} for o in outcomes]},
    }


def _task(task_id="t1", state="pending", **content):
    base = {"title": "t"}
    base.update(content)
    return {"id": task_id, "state": state, "content": base}


class FakeSubstrate:
    def __init__(self, tasks=None, releases=None):
        self._tasks = tasks or []
        self._releases = releases or []
        self.links = []
        self.notes = []
        self.patches = []

    def list_tasks(self, state=None, limit=200):
        return list(self._tasks)

    def list_beads(self, namespace, type, state=None, limit=200):
        if namespace == "arch" and type == "release":
            return list(self._releases)
        return []

    def find_bead(self, namespace, type, content_ref):
        if namespace == "arch" and type == "release":
            for release in self._releases:
                if (release.get("content") or {}).get("ref") == content_ref:
                    return release
        return None

    def add_link(self, source_id, target_id, link_type, created_by):
        self.links.append((source_id, target_id, link_type, created_by))
        return {"id": f"link-{len(self.links)}"}

    def add_note(self, parent_id, kind, body, created_by, **extra):
        self.notes.append((parent_id, kind, body, created_by, extra))
        return {}

    def patch_content(self, bead_id, content, created_by):
        self.patches.append((bead_id, content, created_by))
        return {}


class _Conflict(RuntimeError):
    status = 409


# --- parse_release_ref ----------------------------------------------------------


def test_parse_release_ref_splits_release_and_outcome():
    assert dispatch.parse_release_ref("R26.01/O-2") == ("R26.01", "O-2")


def test_parse_release_ref_accepts_a_bare_release():
    assert dispatch.parse_release_ref("R26.01") == ("R26.01", None)


def test_parse_release_ref_rejects_a_malformed_ref():
    with pytest.raises(ValueError):
        dispatch.parse_release_ref("sprint-33")


def test_parse_release_ref_rejects_a_bare_sprint_label():
    """R1/R2.2 are already in use as gitops-resilience sprint labels — a
    release ref that could be read as one of those would make every
    cross-reference ambiguous (ArchReleaseContent's own validator)."""
    with pytest.raises(ValueError):
        dispatch.parse_release_ref("R1")


# --- resolve_release_ref ---------------------------------------------------------


def test_resolve_release_ref_returns_the_release_bead_id():
    sub = FakeSubstrate(releases=[_release("R26.01")])

    binding = dispatch.resolve_release_ref("R26.01", sub)

    assert binding.release_id == "release-R26.01"
    assert binding.release_ref == "R26.01"
    assert binding.outcome_id is None


def test_resolve_release_ref_resolves_the_outcome_too():
    sub = FakeSubstrate(releases=[_release("R26.01", outcomes=("O-1", "O-2"))])

    binding = dispatch.resolve_release_ref("R26.01/O-2", sub)

    assert binding.outcome_id == "O-2"


def test_resolve_release_ref_refuses_a_release_the_substrate_does_not_hold():
    sub = FakeSubstrate(releases=[])

    with pytest.raises(dispatch.ReleaseResolutionError) as exc:
        dispatch.resolve_release_ref("R99.99", sub)

    assert "R99.99" in str(exc.value)


def test_resolve_release_ref_refuses_an_outcome_the_charter_does_not_declare():
    sub = FakeSubstrate(releases=[_release("R26.01", outcomes=("O-1", "O-2"))])

    with pytest.raises(dispatch.ReleaseResolutionError) as exc:
        dispatch.resolve_release_ref("R26.01/O-9", sub)

    message = str(exc.value)
    assert "O-9" in message
    assert "O-1" in message and "O-2" in message


def test_resolve_release_ref_names_open_releases_when_the_ref_is_unknown():
    sub = FakeSubstrate(
        releases=[
            _release("R26.01", state="in_flight"),
            _release("R25.09", state="released"),
        ]
    )

    with pytest.raises(dispatch.ReleaseResolutionError) as exc:
        dispatch.resolve_release_ref("R99.99", sub)

    message = str(exc.value)
    assert "R26.01" in message
    assert "R25.09" not in message


def test_resolve_release_ref_refuses_when_the_substrate_is_unreachable():
    class Broken(FakeSubstrate):
        def find_bead(self, namespace, type, content_ref):
            raise RuntimeError("connection refused")

    with pytest.raises(dispatch.ReleaseResolutionError):
        dispatch.resolve_release_ref("R26.01", Broken())


# --- list_open_release_refs -------------------------------------------------------


def test_list_open_release_refs_excludes_closed_states():
    sub = FakeSubstrate(
        releases=[
            _release("R26.01", state="planned"),
            _release("R26.02", state="in_flight"),
            _release("R26.03", state="closing"),
            _release("R25.09", state="released"),
            _release("R25.08", state="abandoned"),
        ]
    )

    assert dispatch.list_open_release_refs(sub) == ["R26.01", "R26.02"]


# --- bind_task_to_release ----------------------------------------------------------


def test_bind_task_to_release_writes_the_edge_and_a_status_note():
    sub = FakeSubstrate(tasks=[_task("t1")], releases=[_release("R26.01")])

    rc = dispatch.bind_task_to_release(sub, "t1", "R26.01")

    assert rc == 0
    assert sub.links == [
        ("t1", "release-R26.01", "delivers", dispatch.operator_actor())
    ]
    assert len(sub.notes) == 1
    parent_id, kind, body, created_by, extra = sub.notes[0]
    assert parent_id == "t1"
    assert kind == "status"
    assert "R26.01" in body
    assert extra["provenance"]["model"] == "none"


def test_bind_task_to_release_persists_the_bare_outcome_ref():
    sub = FakeSubstrate(
        tasks=[_task("t1")],
        releases=[_release("R26.01", outcomes=("O-1", "O-2"))],
    )

    dispatch.bind_task_to_release(sub, "t1", "R26.01/O-2")

    assert sub.patches[-1][1]["outcome_ref"] == "O-2"


def test_bind_task_to_release_without_an_outcome_patches_nothing():
    sub = FakeSubstrate(tasks=[_task("t1")], releases=[_release("R26.01")])

    dispatch.bind_task_to_release(sub, "t1", "R26.01")

    assert sub.patches == []


def test_bind_task_to_release_refuses_an_unresolvable_ref(capsys):
    sub = FakeSubstrate(tasks=[_task("t1")], releases=[])

    rc = dispatch.bind_task_to_release(sub, "t1", "R99.99")

    assert rc == 1
    assert sub.links == []
    assert sub.notes == []
    assert "refused" in capsys.readouterr().out


def test_bind_task_to_release_reports_an_unknown_bead(capsys):
    sub = FakeSubstrate(tasks=[], releases=[_release("R26.01")])

    rc = dispatch.bind_task_to_release(sub, "missing", "R26.01")

    assert rc == 1
    assert "not_found" in capsys.readouterr().out


def test_bind_task_to_release_dry_run_writes_nothing():
    sub = FakeSubstrate(tasks=[_task("t1")], releases=[_release("R26.01")])

    rc = dispatch.bind_task_to_release(sub, "t1", "R26.01", dry_run=True)

    assert rc == 0
    assert sub.links == []
    assert sub.notes == []
    assert sub.patches == []


def test_bind_task_to_release_treats_a_duplicate_edge_as_already_bound():
    """A 409 (already-linked) is success, not failure — the same idempotency
    record_applies_links already relies on for the applies edge."""

    class AlreadyLinked(FakeSubstrate):
        def add_link(self, source_id, target_id, link_type, created_by):
            raise _Conflict("duplicate")

    sub = AlreadyLinked(tasks=[_task("t1")], releases=[_release("R26.01")])

    rc = dispatch.bind_task_to_release(sub, "t1", "R26.01")

    assert rc == 0
    assert len(sub.notes) == 1


def test_bind_task_to_release_reports_a_non_conflict_link_failure():
    class Broken(FakeSubstrate):
        def add_link(self, source_id, target_id, link_type, created_by):
            raise RuntimeError("503 service unavailable")

    sub = Broken(tasks=[_task("t1")], releases=[_release("R26.01")])

    rc = dispatch.bind_task_to_release(sub, "t1", "R26.01")

    assert rc == 1
    assert sub.notes == []
