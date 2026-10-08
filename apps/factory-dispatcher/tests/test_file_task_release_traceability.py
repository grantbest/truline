"""Newly filed work must name the release it serves, or say why not.

Cloned from test_file_task_traceability.py one field over (2026-08-26's
release-traceability task): a spec carries ``release_ref`` or a written
``release_ref_waived``, and filing refuses when it carries neither. Unlike
requirement traceability, this is resolution, not shape — a release_ref is
checked against the substrate, not a local registry file, so every test here
supplies a fake substrate rather than calling ``build_content`` directly.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

DISPATCHER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISPATCHER))

import dispatch  # noqa: E402
import file_task  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_pristine_verification(monkeypatch):
    """Keep these tests hermetic — see test_file_task.py's identical fixture."""
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(()),
    )


def _spec(**overrides):
    spec = {
        "lane": "code-health",
        "title": "a task",
        "intent": "why it exists",
        "acceptance": ["THE thing SHALL happen."],
        "scope": {"paths": ["apps/factory-dispatcher/"]},
        "risk_class": "behavioral",
        # These fixtures are about release traceability, not requirement
        # traceability — carry the waiver for the latter so it never fires.
        "requirement_refs_waived": "test fixture; exercises release traceability only",
        # This scope is a boundary scope (C-4) -- these fixtures are about
        # release traceability, not C-4, so they carry declarations here.
        "principal": "test fixture; exercises unrelated behaviour",
        "reaches": ["none"],
    }
    spec.update(overrides)
    return spec


def _release(ref="R26.01", state="in_flight", outcomes=("O-1", "O-2")):
    return {
        "id": f"release-{ref}",
        "state": state,
        "content": {"ref": ref, "outcomes": [{"id": o} for o in outcomes]},
    }


class FakeSubstrate:
    """No network, no database — just enough of Substrate for main()'s wiring."""

    def __init__(self, releases=None, tasks=None):
        self._releases = releases or []
        self._tasks = tasks or []
        self.posted = None
        self.links = []
        self.notes = []
        self.patches = []

    def list_tasks(self, state=None, limit=200):
        return list(self._tasks)

    def list_notes(self, parent_id, limit=500):
        return []

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

    def _request(self, method, path, **kwargs):
        self.posted = kwargs.get("json")
        return {"id": "new-bead-id"}

    def create_task(self, content, created_by, *, trust_tier="user"):
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "task",
                "state": "pending",
                "trust_tier": trust_tier,
                "created_by": created_by,
                "content": content,
            },
        )


def _file(tmp_path, **overrides):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(**overrides)))
    return spec_path


# --- the refusal --------------------------------------------------------------


def test_filing_without_a_release_ref_or_waiver_is_refused(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01", state="in_flight")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "no release_ref" in message
    assert "release_ref_waived" in message
    assert fake.posted is None


def test_the_refusal_names_the_releases_currently_open(tmp_path, monkeypatch):
    fake = FakeSubstrate(
        releases=[
            _release("R26.01", state="in_flight"),
            _release("R26.02", state="planned"),
            _release("R25.09", state="closing"),
            _release("R25.08", state="released"),
        ]
    )
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "R26.01" in message
    assert "R26.02" in message
    # closing/released are not open — naming them would tell the author to
    # cite a release that can no longer accept new work.
    assert "R25.09" not in message
    assert "R25.08" not in message


# --- the unresolvable ref -------------------------------------------------------


def test_a_release_ref_the_substrate_does_not_hold_is_refused(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R99.99")

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "R99.99" in message
    assert "does not hold" in message
    assert fake.posted is None
    assert fake.links == []


def test_a_malformed_release_ref_is_refused(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="sprint-33")

    with pytest.raises(SystemExit):
        file_task.main([str(spec_path)])

    assert fake.posted is None


# --- the undeclared outcome ------------------------------------------------------


def test_an_undeclared_outcome_is_refused_naming_the_declared_ones(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01", outcomes=("O-1", "O-2"))])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R26.01/O-9")

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "O-9" in message
    assert "O-1" in message
    assert "O-2" in message
    assert fake.posted is None
    assert fake.links == []


# --- the waiver is explicit, reasoned, and recorded ----------------------------


def test_file_task_release_traceability_a_waiver_with_a_reason_is_accepted_and_reaches_the_bead(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref_waived="no release yet; triage queue")

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted["content"]["release_ref_waived"] == "no release yet; triage queue"
    assert "outcome_ref" not in fake.posted["content"]
    assert fake.links == []


def test_a_blank_waiver_is_refused_exactly_like_a_missing_ref(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref_waived="   ")

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    assert "no release_ref" in str(exc.value)


# --- a resolvable ref: exactly one edge, never the release id in content -------


def test_a_resolvable_release_ref_writes_exactly_one_delivers_edge(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R26.01")

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted is not None
    assert "release_ref" not in fake.posted["content"]
    assert "outcome_ref" not in fake.posted["content"]
    assert len(fake.links) == 1
    source_id, target_id, link_type, created_by = fake.links[0]
    assert source_id == "new-bead-id"
    assert target_id == "release-R26.01"
    assert link_type == "delivers"


def test_an_outcome_qualified_ref_persists_only_the_bare_outcome_id(tmp_path, monkeypatch):
    fake = FakeSubstrate(releases=[_release("R26.01", outcomes=("O-1", "O-2"))])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R26.01/O-2")

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted["content"]["outcome_ref"] == "O-2"
    assert "release_ref" not in fake.posted["content"]
    assert len(fake.links) == 1
    assert fake.links[0][1] == "release-R26.01"


# --- substrate unreachable: refuse, never file unbound -------------------------


def test_substrate_unreachable_while_listing_open_releases_refuses(tmp_path, monkeypatch):
    class Broken(FakeSubstrate):
        def list_beads(self, namespace, type, state=None, limit=200):
            raise RuntimeError("connection refused")

    fake = Broken()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit):
        file_task.main([str(spec_path)])

    assert fake.posted is None


def test_substrate_unreachable_while_resolving_a_ref_refuses(tmp_path, monkeypatch):
    class Broken(FakeSubstrate):
        def find_bead(self, namespace, type, content_ref):
            raise RuntimeError("timeout")

    fake = Broken(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R26.01")

    with pytest.raises(SystemExit):
        file_task.main([str(spec_path)])

    assert fake.posted is None


def test_edge_write_failure_after_posting_names_the_bead_and_the_fix(tmp_path, monkeypatch):
    """The bead exists (POST already succeeded) but the edge did not — this
    can only be reported loudly, never silently, per the acceptance
    criterion that a task must never land silently unbound."""

    class FailingLink(FakeSubstrate):
        def add_link(self, source_id, target_id, link_type, created_by):
            raise RuntimeError("503 service unavailable")

    fake = FailingLink(releases=[_release("R26.01")])
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path, release_ref="R26.01")

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "new-bead-id" in message
    assert "--bind-release" in message
    assert "R26.01" in message
