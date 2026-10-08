"""dev.finding 79db3113 part c1: the fingerprint, the record, and the real
comparison function (``verify_clone_git_control``) this bead adds.

``check_clone_git_control`` -- the hook ``git_in_clone`` already calls before
every git invocation -- stays a no-op through this bead (part c2 rewires it
with one line). Every test here drives the real functions DIRECTLY, never
through the hook, exactly as the intent asks.

No network, no substrate, no reference to any live bead -- every repo here
is built fresh in a tmp dir, the same shape tests/test_dispatch.py:481 uses.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import containment  # noqa: E402
import dispatch  # noqa: E402
import guards  # noqa: E402
import retry_policy  # noqa: E402

from _clone_fixtures import recorded  # noqa: E402

_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Factory Test",
    "GIT_AUTHOR_EMAIL": "factory@example.test",
    "GIT_COMMITTER_NAME": "Factory Test",
    "GIT_COMMITTER_EMAIL": "factory@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [dispatch.GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY_ENV},
    )


def _make_clone(tmp_path: Path) -> Path:
    """A real clone made through ``dispatch.make_clone`` against a local
    bare origin -- the fixture shape tests/test_dispatch.py:481 already
    uses. ``make_clone`` itself records the git-control baseline as its
    last statement, so every clone this helper returns already carries one.
    """
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text("one\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")

    clone = tmp_path / "clone"
    dispatch.make_clone(
        dispatch.Config(repo_root=repo, remote="git@example.invalid:repo.git", base_ref="main"),
        clone,
    )
    return clone


# ---------------------------------------------------------------------------
# AC-2: verify_clone_git_control, driven directly.
# ---------------------------------------------------------------------------


def test_unchanged_clone_passes(tmp_path):
    clone = _make_clone(tmp_path)

    assert dispatch.verify_clone_git_control(clone) is None


def test_changed_config_is_refused_naming_the_path(tmp_path):
    clone = _make_clone(tmp_path)
    config = clone / ".git" / "config"
    config.write_text(config.read_text() + "\n[test]\n\tplanted = 1\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert str(config) in str(excinfo.value)


def test_new_hook_is_refused_naming_the_path(tmp_path):
    clone = _make_clone(tmp_path)
    planted = clone / ".git" / "hooks" / "post-checkout"
    planted.write_text("#!/bin/sh\necho planted\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert str(planted) in str(excinfo.value)


def test_changed_hook_mode_is_refused_naming_the_path(tmp_path):
    clone = _make_clone(tmp_path)
    hooks_dir = clone / ".git" / "hooks"
    existing = next(p for p in hooks_dir.iterdir() if p.is_file())
    current_mode = existing.stat().st_mode
    os.chmod(existing, current_mode ^ 0o100)

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert str(existing) in str(excinfo.value)


def test_new_info_file_is_refused_naming_the_path(tmp_path):
    clone = _make_clone(tmp_path)
    planted = clone / ".git" / "info" / "planted.txt"
    planted.write_text("planted\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert str(planted) in str(excinfo.value)


def test_config_replaced_by_symlink_is_refused_naming_the_path(tmp_path):
    clone = _make_clone(tmp_path)
    config = clone / ".git" / "config"
    original = config.read_bytes()
    config.unlink()
    target = tmp_path / "evil-config"
    target.write_bytes(original)
    config.symlink_to(target)

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert str(config) in str(excinfo.value)


def test_git_entry_replaced_by_gitdir_file_is_refused(tmp_path):
    clone = _make_clone(tmp_path)
    git_dir = clone / ".git"
    moved = tmp_path / "git-dir-moved"
    shutil.move(str(git_dir), str(moved))
    git_dir.write_text(f"gitdir: {moved}\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert ".git entry replaced" in str(excinfo.value)


def test_commondir_file_created_is_refused(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".git" / "commondir").write_text("../somewhere/.git\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert ".git/commondir present" in str(excinfo.value)


def test_dangling_commondir_symlink_is_refused(tmp_path):
    """A commondir that is a SYMLINK to a target that does not exist must
    still be caught: ``Path.exists()`` follows the link and reports False
    for a dangling target, which would silently pass this check. The real
    field uses ``os.path.lexists``, which reports the link itself."""
    clone = _make_clone(tmp_path)
    (clone / ".git" / "commondir").symlink_to(clone / ".git" / "nonexistent-target")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert ".git/commondir present" in str(excinfo.value)


def test_symlink_in_hooks_is_recorded_as_a_symlink_not_dereferenced(tmp_path):
    """The manifest hashes a symlink's own ``readlink()`` target string, not
    the bytes of whatever it points at -- so changing the TARGET FILE's
    content (never the symlink itself) must not trip the check. A buggy
    implementation that instead reads through the link would see the
    target's new bytes and raise."""
    clone = _make_clone(tmp_path)
    target = tmp_path / "hook-target.sh"
    target.write_text("#!/bin/sh\necho original\n")
    link = clone / ".git" / "hooks" / "linked-hook"
    link.symlink_to(target)
    record_path = dispatch._git_control_record_path(clone)
    record_path.unlink()
    dispatch.record_clone_git_control(clone)

    target.write_text("#!/bin/sh\necho changed\n")

    assert dispatch.verify_clone_git_control(clone) is None


def test_change_after_one_successful_check_is_still_caught(tmp_path):
    clone = _make_clone(tmp_path)

    assert dispatch.verify_clone_git_control(clone) is None

    (clone / ".git" / "hooks" / "planted-after-first-check").write_text("x\n")

    with pytest.raises(dispatch.CloneGitControlTampered):
        dispatch.verify_clone_git_control(clone)


def test_clone_with_its_record_deleted_is_refused(tmp_path):
    clone = _make_clone(tmp_path)
    record_path = dispatch._git_control_record_path(clone)
    record_path.unlink()

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)

    assert "no git-control record" in str(excinfo.value)


def test_tampered_error_classifies_as_work_failure_and_carries_no_retry_markers(tmp_path):
    assert issubclass(dispatch.CloneGitControlTampered, dispatch.DispatchError)
    assert not issubclass(dispatch.CloneGitControlTampered, dispatch.DispatchEnvironmentError)

    # The actual message verify_clone_git_control raises, not a hand-built
    # stand-in -- a mutation that slips a marker string into that real
    # raise must turn this test red too.
    clone = _make_clone(tmp_path)
    git_dir = clone / ".git"
    moved = tmp_path / "git-dir-moved"
    shutil.move(str(git_dir), str(moved))
    git_dir.write_text(f"gitdir: {moved}\n")
    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.verify_clone_git_control(clone)
    reason = str(excinfo.value)

    assert "capacity backpressure:" not in reason
    assert retry_policy.ALREADY_SATISFIED_WORK_MARKER not in reason
    assert "Could not start:" not in reason

    classification = retry_policy.classify_dispatch_failure(reason, worker_result_present=True)

    assert classification is retry_policy.WORK_FAILURE


# ---------------------------------------------------------------------------
# AC-1: record_clone_git_control, overwrite refusal, and the write-boundary
# proof.
# ---------------------------------------------------------------------------


def test_record_clone_git_control_refuses_to_overwrite_an_existing_record(tmp_path):
    clone = _make_clone(tmp_path)

    with pytest.raises(dispatch.DispatchError):
        dispatch.record_clone_git_control(clone)


def test_make_clone_records_before_anything_else_runs_git_in_the_clone(tmp_path):
    clone = _make_clone(tmp_path)

    record_path = dispatch._git_control_record_path(clone)
    assert record_path.exists()
    assert dispatch.verify_clone_git_control(clone) is None


def test_git_control_record_path_is_outside_both_profiles_write_allows(tmp_path):
    """AC-1: the record path must not be matched by either sandbox profile's
    write-allow list under the COMPONENT-BOUNDARY rule (``p == allow or
    p.startswith(allow + "/")``) -- never a bare string-prefix test, which a
    record path sharing the clone path's string prefix could pass by
    accident even while sitting outside the clone.

    Checked against the LIVE rendered text: ``prepare_containment`` patches
    a ``.tmp`` write-allow into ``render_profile``'s output after the fact,
    and every real call to ``render_verification_profile`` passes
    ``extra_write=(containment.verification_venv_path(clone),)``. A record
    path under either of those -- which a bare ``render_profile(clone)`` /
    ``render_verification_profile(clone)`` call (no extras) would miss --
    must still be caught here."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    clone = workdir / "clone"
    clone.mkdir()
    (clone / ".git").mkdir()

    resolved_clone = clone.resolve()
    record_path = str(dispatch._git_control_record_path(resolved_clone))

    def matches(allow: str) -> bool:
        return record_path == allow or record_path.startswith(allow + "/")

    worker_profile_path, _ = containment.prepare_containment(clone)
    worker_profile_text = worker_profile_path.read_text()
    verify_profile_text = containment.render_verification_profile(
        clone, extra_write=(containment.verification_venv_path(clone),)
    )

    for profile_text in (worker_profile_text, verify_profile_text):
        allows = re.findall(r'\(subpath "([^"]+)"\)', profile_text)
        literals = re.findall(r'\(literal "([^"]+)"\)', profile_text)
        assert not any(matches(allow) for allow in allows)
        assert not any(matches(literal) for literal in literals)


# ---------------------------------------------------------------------------
# AC-4: a push through open_pull_request's path does not trip the
# fingerprint; pushing WITH -u would have (and is refused).
# ---------------------------------------------------------------------------


def test_push_through_open_pull_request_does_not_trip_the_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)

    bare = tmp_path / "cfg-remote.git"
    git(tmp_path, "init", "--bare", "-q", str(bare))

    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", "-b", "main", str(clone))
    git(clone, "config", "user.email", "factory@example.test")
    git(clone, "config", "user.name", "Factory Test")
    (clone / "README.md").write_text("one\n")
    git(clone, "add", "README.md")
    git(clone, "commit", "-q", "-m", "initial")
    git(clone, "remote", "add", "origin", str(tmp_path / "not-cfg-remote.git"))
    (clone / "new.txt").write_text("worker's change\n")

    recorded(clone)

    real_run = dispatch.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            if cmd[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "https://example.test/pr/1\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(dispatch, "run", fake_run)

    cfg = dispatch.Config(repo="example/repo", remote=str(bare), repo_root=clone, base_ref="main")
    branch = "factory/task-1"
    dispatch.open_pull_request(
        cfg,
        clone,
        {"id": "task-1", "content": {}},
        branch,
        "worker report",
        guards.ScopeVerdict(),
        dispatch.VerificationReport((), bootstrap_note="stub bootstrap"),
    )

    # The push itself -- no -u, an explicit refspec to cfg.remote -- writes
    # nothing into the fingerprinted paths.
    assert dispatch.verify_clone_git_control(clone) is None

    # Control: pushing the SAME branch WITH -u writes branch.<branch>.* into
    # .git/config, pinning part a's no-"-u" decision with a real refusal.
    before = (clone / ".git" / "config").read_text()
    git(clone, "push", "-q", "-u", str(bare), branch)
    after = (clone / ".git" / "config").read_text()

    assert before != after
    assert f'branch "{branch}"' in after

    with pytest.raises(dispatch.CloneGitControlTampered):
        dispatch.verify_clone_git_control(clone)


# ---------------------------------------------------------------------------
# AC-4 (part c2): the same push, now proven through the LIVE hook -- a
# builder-routed call, not a direct verify_clone_git_control call.
# ---------------------------------------------------------------------------


def test_builder_call_after_a_push_through_open_pull_request_is_not_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)

    bare = tmp_path / "cfg-remote.git"
    git(tmp_path, "init", "--bare", "-q", str(bare))

    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", "-b", "main", str(clone))
    git(clone, "config", "user.email", "factory@example.test")
    git(clone, "config", "user.name", "Factory Test")
    (clone / "README.md").write_text("one\n")
    git(clone, "add", "README.md")
    git(clone, "commit", "-q", "-m", "initial")
    git(clone, "remote", "add", "origin", str(tmp_path / "not-cfg-remote.git"))
    (clone / "new.txt").write_text("worker's change\n")

    recorded(clone)

    real_run = dispatch.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            if cmd[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "https://example.test/pr/1\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(dispatch, "run", fake_run)

    cfg = dispatch.Config(repo="example/repo", remote=str(bare), repo_root=clone, base_ref="main")
    branch = "factory/task-1"
    dispatch.open_pull_request(
        cfg,
        clone,
        {"id": "task-1", "content": {}},
        branch,
        "worker report",
        guards.ScopeVerdict(),
        dispatch.VerificationReport((), bootstrap_note="stub bootstrap"),
    )

    # The live hook, not a direct verify_clone_git_control call: a further
    # builder-routed call in the same clone must proceed, not refuse.
    result = dispatch.git_in_clone(clone, ["status", "--porcelain"])
    assert result.returncode == 0
