"""Direct tests for git_control.py's fingerprint/compare/record logic
(dev.finding 79db3113 part c1, AC-5). These import git_control directly --
never through dispatch, and never against a clone made by
dispatch.make_clone -- so the module's own stdlib-only contract is exercised
on its own terms. test_clone_git_control.py covers the same ground through
dispatch.verify_clone_git_control, against a real clone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import git_control  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    hooks = git_dir / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit.sample").write_text("#!/bin/sh\necho sample\n")
    info = git_dir / "info"
    info.mkdir()
    (info / "exclude").write_text("*.pyc\n")
    return repo


def test_compute_git_control_fingerprint_on_a_fixture_repo(tmp_path):
    repo = _make_repo(tmp_path)

    fingerprint = git_control.compute_git_control_fingerprint(repo)

    assert fingerprint["git_entry_kind"] == "dir"
    assert fingerprint["commondir_present"] is False
    assert fingerprint["config"] is not None
    paths = {entry["path"] for entry in fingerprint["manifest"]}
    assert "hooks/pre-commit.sample" in paths
    assert "info/exclude" in paths


def test_compare_returns_none_on_an_unchanged_tree(tmp_path):
    repo = _make_repo(tmp_path)
    baseline = git_control.compute_git_control_fingerprint(repo)
    current = git_control.compute_git_control_fingerprint(repo)

    assert git_control.compare_git_control_fingerprint(baseline, current, repo) is None


def test_compare_names_the_first_changed_path_on_a_changed_tree(tmp_path):
    repo = _make_repo(tmp_path)
    baseline = git_control.compute_git_control_fingerprint(repo)
    (repo / ".git" / "info" / "exclude").write_text("*.pyc\n*.pyo\n")
    current = git_control.compute_git_control_fingerprint(repo)

    message = git_control.compare_git_control_fingerprint(baseline, current, repo)

    assert message is not None
    assert str(repo / ".git" / "info" / "exclude") in message


def test_record_read_write_round_trip(tmp_path):
    repo = _make_repo(tmp_path)
    fingerprint = git_control.compute_git_control_fingerprint(repo)

    assert git_control.record_exists(repo) is False

    git_control.write_record(repo, fingerprint)

    assert git_control.record_exists(repo) is True
    assert git_control.read_record(repo) == fingerprint


def test_commondir_present_detects_a_dangling_symlink(tmp_path):
    """Path.exists() follows a symlink and reports False when the target is
    missing -- exactly the tamper this field exists to catch. The real
    field is lexists-based (git_control.py's compute_git_control_fingerprint)
    and must report True regardless of whether the target resolves."""
    repo = _make_repo(tmp_path)
    (repo / ".git" / "commondir").symlink_to(repo / ".git" / "nonexistent-target")

    fingerprint = git_control.compute_git_control_fingerprint(repo)

    assert fingerprint["commondir_present"] is True
