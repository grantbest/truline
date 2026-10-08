"""``tools.task_filing`` files a dev.task bead by calling ``file_task.file_spec``
from the real factory-dispatcher checkout -- never a re-derived filing path
(PRIN-005, see ``.factory/design.md``). No real substrate, no clone, no
network: every store here is a small in-memory fake, and the containment
tests prove the dispatcher's own clone/verify functions are never invoked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER_DIR = REPO_ROOT / "apps" / "factory-dispatcher"
if str(DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(DISPATCHER_DIR))

import dispatch  # noqa: E402
import file_task  # noqa: E402

from tools import task_filing  # noqa: E402


def _spec(**overrides):
    spec = {
        "lane": "code-health",
        "title": "t",
        "intent": "i",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        "requirement_refs_waived": "test fixture; exercises unrelated behaviour",
        "release_ref_waived": "test fixture; exercises unrelated behaviour",
    }
    spec.update(overrides)
    return {k: v for k, v in spec.items() if v is not None}


class FakeStore:
    """A BeadStore-shaped double -- no substrate, no network."""

    def __init__(self, tasks=None):
        self._tasks = tasks or []
        self.created: list[dict] = []
        self.notes: list[dict] = []
        self.links: list[tuple] = []

    def list_tasks(self, state=None, limit=200):
        return list(self._tasks)

    def list_notes(self, parent_id, limit=500):
        return []

    def list_beads(self, namespace, type, **params):
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return []

    def find_bead(self, namespace, type, content_ref):
        return None

    def create_task(self, content, created_by, *, trust_tier="user"):
        bead = {
            "id": f"bead-{len(self.created)}",
            "state": "pending",
            "created_by": created_by,
            "content": content,
        }
        self.created.append(bead)
        return bead

    def add_note(self, parent_id, kind, body, created_by, provenance=None, **extra):
        note = {"parent_id": parent_id, "kind": kind, "body": body, "created_by": created_by}
        self.notes.append(note)
        return note

    def add_link(self, source_id, target_id, link_type, created_by):
        self.links.append((source_id, target_id, link_type, created_by))
        return {}

    def patch_content(self, bead_id, content, created_by):
        raise AssertionError("patch_content should not be called by file_dev_task")


# ---------------------------------------------------------------------------
# import order (task_filing's own module docstring, "Discovery 1")
# ---------------------------------------------------------------------------
#
# Subprocess-isolated, matching test_container_startup_import.py's own
# reasoning: an in-process attempt could pass by accident from another test
# having already imported (and cached in sys.modules) file_task/dispatch
# earlier in the same session.


def _run_import_probe(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=DISPATCHER_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_importing_file_task_alone_reproduces_the_documented_cycle():
    """Pins the bug task_filing.py's docstring exists to route around: a bare
    ``import file_task`` (no ``dispatch`` first) hits the circular import."""
    result = _run_import_probe("import file_task")
    assert result.returncode != 0
    # Python reports one cycle under two different messages depending on
    # WHERE in the cycle the re-entrant import lands, and this suite has now
    # seen both. At this PR's base (160e812f) it was "partially initialized
    # module 'file_task'"; on the merged tree of 2026-09-17 the same cycle
    # surfaces from scanner.py's `from file_task import ...` as "cannot
    # import name 'CLOSED_TASK_STATES' from 'file_task'". Nothing about the
    # cycle changed -- only which name the partially-executed module was
    # asked for first. VERIFIED on unmodified `main` at 0783a8d5, with this
    # PR's own changes absent, so this is not a symptom this PR introduced.
    # Pinning one message pins the accident, not the bug, so assert on what
    # actually matters: the bare import fails and the traceback is the
    # file_task <-> dispatch cycle.
    assert (
        "partially initialized module 'file_task'" in result.stderr
        or "cannot import name" in result.stderr
    ), result.stderr
    assert "file_task" in result.stderr and "dispatch" in result.stderr


def test_importing_dispatch_before_file_task_avoids_the_cycle():
    """The fix task_filing._load_file_task relies on: reversing the two
    imports load-bearing lines makes both succeed."""
    result = _run_import_probe("import dispatch; import file_task; print('OK')")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_created_by_is_the_real_client_identity_not_a_constant():
    store = FakeStore()
    bead = task_filing.file_dev_task(_spec(title="provenance probe"), "agent-dev", store=store)

    assert bead["created_by"] == "agent-dev"
    assert store.created[0]["created_by"] == "agent-dev"
    assert store.created[0]["created_by"] != file_task.CREATED_BY


# ---------------------------------------------------------------------------
# containment (AC-2) -- the point of Option A
# ---------------------------------------------------------------------------


def test_http_filing_never_invokes_pristine_verification_at_all(monkeypatch):
    """AC-3 (#909/#935 gate): the gateway property is that
    file_task._check_pristine_verification -- the mechanism that clones a
    repo and runs a client-supplied command -- is never invoked from the HTTP
    filing path, independent of what value run_pristine_verification carries.
    A double that only records whether it was called is the point: asserting
    on the argument's value would still pass if the flag stopped being
    honoured, which is exactly the property this test must not miss.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        file_task, "_check_pristine_verification", lambda content: calls.append(content)
    )

    store = FakeStore()
    bead = task_filing.file_dev_task(_spec(title="never invoked probe"), "agent-dev", store=store)

    assert bead["id"] == store.created[0]["id"]
    assert calls == []


def test_never_executes_or_clones_client_supplied_verification_commands(tmp_path, monkeypatch):
    sentinel = tmp_path / "SHOULD_NOT_EXIST"
    spec = _spec(
        title="containment probe",
        verification={
            "commands": [f"python3 -c \"open({str(sentinel)!r}, 'w').close()\""]
        },
    )

    def _boom_verify(*_a, **_k):
        raise AssertionError("dispatch.verify_pristine_commands must never run over HTTP")

    def _boom_clone(*_a, **_k):
        raise AssertionError("dispatch.make_clone must never run over HTTP")

    monkeypatch.setattr(dispatch, "verify_pristine_commands", _boom_verify)
    monkeypatch.setattr(dispatch, "make_clone", _boom_clone)

    store = FakeStore()
    bead = task_filing.file_dev_task(spec, "agent-dev", store=store)

    assert bead["id"] == store.created[0]["id"]
    assert not sentinel.exists(), "the sentinel command ran -- containment is broken"


# ---------------------------------------------------------------------------
# refusal parity with the CLI (PC-FAC-001/AC-5, reached from this surface)
# ---------------------------------------------------------------------------


def _duplicate_spec_identity_case():
    spec = _spec(title="already filed")
    tasks = [{"id": "existing-1", "state": "pending", "content": {"title": "already filed"}}]
    return spec, tasks


def _unresolvable_release_ref_case():
    spec = _spec(title="bad release", release_ref="R99.99", release_ref_waived=None)
    return spec, []


def _no_requirement_refs_and_no_waiver_case():
    spec = _spec(title="no reqs", requirement_refs_waived=None)
    return spec, []


REFUSAL_CASES = {
    "duplicate_spec_identity": _duplicate_spec_identity_case,
    "unresolvable_release_ref": _unresolvable_release_ref_case,
    "no_requirement_refs_and_no_waiver": _no_requirement_refs_and_no_waiver_case,
}


@pytest.mark.parametrize("case_name", sorted(REFUSAL_CASES))
def test_http_refusal_text_matches_cli_refusal_text(case_name, tmp_path, monkeypatch):
    spec, tasks = REFUSAL_CASES[case_name]()

    cli_store = FakeStore(tasks=list(tasks))
    monkeypatch.setattr(file_task, "Substrate", lambda: cli_store)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(SystemExit) as cli_exc:
        file_task.main([str(spec_path)])
    cli_message = str(cli_exc.value)

    http_store = FakeStore(tasks=list(tasks))
    with pytest.raises(task_filing.FilingRefused) as http_exc:
        task_filing.file_dev_task(spec, "agent-dev", store=http_store)
    http_message = str(http_exc.value)

    assert http_message == cli_message
    assert cli_store.created == []
    assert http_store.created == []


# ---------------------------------------------------------------------------
# misconfiguration is loud, not degraded (unlike tools.factory_status)
# ---------------------------------------------------------------------------


def test_unset_env_var_raises_plainly(monkeypatch):
    monkeypatch.delenv(task_filing.FACTORY_DISPATCHER_ROOT_ENV, raising=False)
    with pytest.raises(task_filing.Unavailable):
        task_filing.file_dev_task(_spec(), "agent-dev", store=FakeStore())
