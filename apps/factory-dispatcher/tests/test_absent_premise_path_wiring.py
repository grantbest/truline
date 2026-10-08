"""AC-1/AC-3/AC-6 (dev.finding 0d1e952b's carried-forward wiring gap, PR #913):
``guards.absent_premise_paths`` existed and was tested but had no caller, so a
spec naming a path absent from the base ref was still dispatched silently.

This drives the real ``activities.dispatch_steps.isolate_activity`` end to
end against a real git fixture -- an actual ``git clone`` of an actual base
ref, no mocked git plumbing for the part under test -- so a wiring that
silently computes nothing and one that works cannot look identical (the
parent bead's own AC-6). Both directions are exercised: a bead naming an
absent premise path gets a note; a bead naming only present ones gets none.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import guards  # noqa: E402
from activities import dispatch_steps  # noqa: E402
from test_dispatch import FakeSubstrate, git, task_with_id  # noqa: E402
from _clone_fixtures import recorded  # noqa: E402


def _build_base_repo(tmp_path: Path) -> Path:
    """A real, committed base ref: ``apps/mcp-hub/keep.py`` exists on it;
    ``apps/mcp-hub/tests/test_audit_shape.py`` deliberately does not -- the
    exact OPS-141 shape ``guards.absent_premise_paths``'s own tests pin."""
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    (repo / "README.md").write_text("root\n")
    mcp_dir = repo / "apps" / "mcp-hub"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "keep.py").write_text("# present on the base ref\n")
    git(repo, "add", "README.md", "apps/mcp-hub/keep.py")
    git(repo, "commit", "-m", "base ref")
    return repo


def _isolate(tmp_path: Path, monkeypatch, repo: Path, bead: dict, sub: FakeSubstrate) -> dict:
    workdir_root = tmp_path / "work"
    workdir_root.mkdir()
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(workdir_root))

    def real_clone(cfg: dispatch.Config, dest: Path) -> None:
        # The exact shape dispatch.make_clone uses, minus ensure_base_ref_current
        # (network/host-checkout plumbing this fixture has no use for) -- a
        # real `git clone` of the real base ref, so tracked_paths reads a real
        # ref, not a stub.
        git(
            tmp_path,
            "clone",
            "--branch",
            cfg.base_ref,
            "--single-branch",
            str(cfg.repo_root),
            str(dest),
        )
        recorded(dest)

    monkeypatch.setattr(dispatch, "make_clone", real_clone)
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    cfg = dispatch.Config(repo_root=repo, base_ref="main")
    state = {
        "cfg": dispatch_steps._cfg_to_state(cfg),
        "task": bead,
        "worker": {"name": "claude"},
    }
    return dispatch_steps.isolate_activity(state)


def test_absent_context_ref_surfaces_a_note_on_isolate(tmp_path, monkeypatch):
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-absent",
        context_refs=["apps/mcp-hub/tests/test_audit_shape.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead)

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert len(sub.notes) == 1
    args, kwargs = sub.notes[0]
    assert args[0] == "task-absent"
    assert args[1] == "status"
    assert args[2].startswith(guards.ABSENT_PREMISE_NOTE_PREFIX)
    assert "apps/mcp-hub/tests/test_audit_shape.py" in args[2]
    assert kwargs["provenance"]["prompt_ref"] == "dev.task/task-absent"


def test_all_named_paths_present_surfaces_nothing(tmp_path, monkeypatch):
    """The other direction (AC-6): remove the absent path and the surface is
    clean -- a wiring that always fires and one that never fires would both
    pass the first test alone, so this one has to hold too."""
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-present",
        context_refs=["apps/mcp-hub/keep.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead)

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert sub.notes == []


def test_absent_premise_path_does_not_block_dispatch(tmp_path, monkeypatch):
    """AC-2 carried forward: the finding is advisory. isolate_activity must
    still return normally -- the claimed state moving on to preflight/run --
    with an absent premise path present."""
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-absent-2",
        context_refs=["apps/mcp-hub/tests/test_audit_shape.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead)

    result = _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert Path(result["clone"]).exists()
    assert result["verified_revision"]
    assert len(sub.notes) == 1


def _build_base_repo_with_space_path(tmp_path: Path, *, include: bool) -> Path:
    """A real, committed base ref that either does or does not carry
    ``apps/space dir/f.py`` -- a tracked path containing a space, the exact
    shape ``dispatch.tracked_paths``'s pre-fix whitespace ``.split()`` used
    to shred into two listing entries (dev.finding F1)."""
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    (repo / "README.md").write_text("root\n")
    git(repo, "add", "README.md")
    if include:
        space_dir = repo / "apps" / "space dir"
        space_dir.mkdir(parents=True)
        (space_dir / "f.py").write_text("# present on the base ref\n")
        git(repo, "add", "apps/space dir/f.py")
    git(repo, "commit", "-m", "base ref")
    return repo


def test_scope_path_with_a_space_present_surfaces_nothing_end_to_end(tmp_path, monkeypatch):
    """AC-3, proven end to end and not just at the ``tracked_paths`` helper:
    a bead whose ``scope.paths`` names a path containing a space, with that
    exact path committed on the cloned ref, must not trip the advisory.
    Pre-fix, ``dispatch.tracked_paths`` reported ``'apps/space'`` and
    ``'dir/f.py'`` as two separate entries -- neither equal to nor a prefix
    match for ``'apps/space dir/f.py'`` -- so this exact scenario used to
    wrongly surface a note claiming a present path was absent."""
    repo = _build_base_repo_with_space_path(tmp_path, include=True)
    bead = task_with_id(
        "task-space-present",
        context_refs=["apps/space dir/f.py"],
        scope={"paths": ["apps/space dir/f.py"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead)

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert sub.notes == []


def test_scope_path_with_a_space_absent_still_surfaces_a_note_end_to_end(tmp_path, monkeypatch):
    """The other direction (AC-3): remove the space-containing path from the
    base ref and the advisory note IS emitted. Without both directions, a
    helper that always returns nothing and one that actually works look
    identical -- the exact defect this bead's parent (#926) existed to fix,
    one layer down."""
    repo = _build_base_repo_with_space_path(tmp_path, include=False)
    bead = task_with_id(
        "task-space-absent",
        context_refs=["apps/space dir/f.py"],
        scope={"paths": ["apps/space dir/f.py"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead)

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert len(sub.notes) == 1
    args, _kwargs = sub.notes[0]
    assert args[2].startswith(guards.ABSENT_PREMISE_NOTE_PREFIX)
    assert "apps/space dir/f.py" in args[2]


def test_repeat_isolate_over_the_same_finding_does_not_duplicate_the_note(
    tmp_path, monkeypatch
):
    """AC-5: ``isolate_activity`` runs once per dispatch attempt, so a bead
    stuck retrying the same absent premise path used to accrue one identical
    advisory note per attempt (dev.finding F5). A second isolate over a bead
    that already carries the exact note a first isolate would write must add
    no second one -- simulated here by pre-seeding the store's notes with
    the same body the first isolate call is proven (by the sibling test
    below) to write, rather than re-running isolate twice against a FakeSubstrate
    double whose ``add_note`` does not feed back into ``list_notes`` (it
    isn't meant to; that plumbing is the live substrate's job, not this
    double's)."""
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-repeat",
        context_refs=["apps/mcp-hub/tests/test_audit_shape.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )
    already_recorded_body = guards.render_absent_premise_note(
        ("apps/mcp-hub/tests/test_audit_shape.py",)
    )
    sub = FakeSubstrate(
        bead,
        notes={
            "task-repeat": [
                {
                    "id": "note-from-a-prior-attempt",
                    "parent_id": "task-repeat",
                    "content": {"kind": "status", "body": already_recorded_body},
                }
            ]
        },
    )

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert sub.notes == []


def test_first_isolate_still_writes_the_note_with_no_prior_note_present(
    tmp_path, monkeypatch
):
    """The idempotency check's other direction (AC-5): a dedupe that always
    skips the write and one that actually compares bodies would both pass
    the "no duplicate" test above alone. With ``list_notes`` returning
    nothing for this bead, the first isolate call must still write the note
    -- this is the same assertion as
    ``test_absent_context_ref_surfaces_a_note_on_isolate`` above, restated
    here so the two idempotency-relevant tests sit side by side."""
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-first-time",
        context_refs=["apps/mcp-hub/tests/test_audit_shape.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )
    sub = FakeSubstrate(bead, notes={"task-first-time": []})

    _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert len(sub.notes) == 1
    args, _kwargs = sub.notes[0]
    assert args[2] == guards.render_absent_premise_note(
        ("apps/mcp-hub/tests/test_audit_shape.py",)
    )


def test_note_write_failure_does_not_fail_isolate(tmp_path, monkeypatch):
    """The advisory note write itself must not be able to fail the dispatch
    attempt: a store outage while recording a nice-to-have finding must not
    spend a retry on a problem the operator never asked this check to gate."""
    repo = _build_base_repo(tmp_path)
    bead = task_with_id(
        "task-store-down",
        context_refs=["apps/mcp-hub/tests/test_audit_shape.py"],
        scope={"paths": ["apps/mcp-hub/"], "forbidden_paths": []},
    )

    class ExplodingSubstrate(FakeSubstrate):
        def add_note(self, *args, **kwargs):
            raise RuntimeError("substrate unreachable")

    sub = ExplodingSubstrate(bead)

    result = _isolate(tmp_path, monkeypatch, repo, bead, sub)

    assert Path(result["clone"]).exists()
    assert result["verified_revision"]
