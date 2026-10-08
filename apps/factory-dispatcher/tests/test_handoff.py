"""dev.finding 79db3113, design §5: handoff.guard_tree() and handoff.apply()
are the only functions permitted to run git against W/repo as grantbest in
split mode. Every repo here is built fresh in a tmp dir with the real git
binary (the same idiom tests/test_git_in_clone.py already uses) -- no
network, no substrate, no live sudo (decision record 2026-09-13 D7).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import handoff  # noqa: E402
import process_env  # noqa: E402

_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Factory Test",
    "GIT_AUTHOR_EMAIL": "factory@example.test",
    "GIT_COMMITTER_NAME": "Factory Test",
    "GIT_COMMITTER_EMAIL": "factory@example.test",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess:
    """Runs with raw bytes stdout/stderr -- needed for the ``diff --binary``
    patch bytes this suite feeds straight into ``handoff.apply``."""
    return subprocess.run(
        [dispatch.GIT, *args],
        cwd=cwd,
        input=input,
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_IDENTITY_ENV},
    )


def git_text(cwd: Path, *args: str, input: str | None = None) -> str:
    encoded = input.encode() if input is not None else None
    return git(cwd, *args, input=encoded).stdout.decode().strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _commit_file(repo: Path, rel: str, content: str, *, mode: int | None = None) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if mode is not None:
        target.chmod(mode)
    git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", f"add {rel}")


def _hash_tree(root: Path) -> str:
    """A recursive, order-independent hash of every path under `root`."""
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        hasher.update(rel.encode())
        if path.is_symlink():
            hasher.update(b"symlink:" + os.readlink(path).encode())
        elif path.is_file():
            hasher.update(b"file:" + path.read_bytes())
        elif path.is_dir():
            hasher.update(b"dir:")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# AC-1: guard_tree
# ---------------------------------------------------------------------------


def test_guard_tree_passes_a_clean_tree(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")

    assert handoff.guard_tree(repo) is None


def test_guard_tree_refuses_a_symlink(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")
    (repo / "linky").symlink_to("f.txt")
    git(repo, "add", "linky")
    git(repo, "commit", "-q", "-m", "add symlink")

    with pytest.raises(handoff.HandoffRefused, match="linky.*120000"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_a_submodule_gitlink(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")
    head = git_text(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},fakesubmodule")

    with pytest.raises(handoff.HandoffRefused, match="fakesubmodule.*160000"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_a_case_folded_dot_git_component(tmp_path):
    """git's own verify_path refuses writing a real a/.GIT/x path even via
    plumbing (confirmed: ``git update-index --add --cacheinfo`` raises
    "Invalid path" even with core.protectHFS/protectNTFS disabled), so this
    builds the entry at the OBJECT level (mktree/commit-tree/update-ref,
    which never calls verify_path) and points HEAD at it -- the same trick
    the belt-and-braces rationale in this module's docstring exists for:
    guard_tree must refuse what a crafted history can carry even though git
    itself refuses to ever check such a path out.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")

    blob = git_text(repo, "hash-object", "-w", "--stdin", input="evil\n")
    inner = git_text(repo, "mktree", input=f"100644 blob {blob}\tx\n")
    dotgit_tree = git_text(repo, "mktree", input=f"040000 tree {inner}\t.GIT\n")
    f_blob = git_text(repo, "rev-parse", "HEAD:f.txt")
    root_tree = git_text(
        repo, "mktree", input=f"040000 tree {dotgit_tree}\ta\n100644 blob {f_blob}\tf.txt\n",
    )
    parent = git_text(repo, "rev-parse", "HEAD")
    commit = git_text(repo, "commit-tree", root_tree, "-p", parent, "-m", "bad tree")
    branch = git_text(repo, "symbolic-ref", "--short", "HEAD")
    git(repo, "update-ref", f"refs/heads/{branch}", commit)

    with pytest.raises(handoff.HandoffRefused, match=r"a/\.GIT/x.*\.git.*\.factory"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_a_case_folded_dot_git_root_component_with_non_ascii_sibling(tmp_path):
    """The outer loop's confirmed bypass (handoff.py:109-110 on PR #1168
    head e1a08775): without ``-z``, git quotes the WHOLE path the moment any
    one byte in it needs quoting (core.quotePath), wrapping it in a pair of
    double quotes. When the refused component sits FIRST in the path, that
    stray leading quote becomes part of it: a tree rooted at ``.GIT/<a
    non-ASCII byte>`` is rendered by ``git ls-tree -r HEAD`` (no ``-z``) as
    the single quoted string ``".GIT/\\303\\251"``, so splitting on ``/``
    yields a first component of ``".GIT`` -- which case-folds to ``".git``,
    not ``.git``, and the old component check silently let it through. This
    builds the same crafted-object-level tree the sibling ``a/.GIT/x`` test
    above does (verify_path refuses a real checkout of such a path, so this
    goes in at the object level) but with ``.GIT`` itself at repo ROOT, the
    shape that actually exploited the quoting bug."""
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")

    blob = git_text(repo, "hash-object", "-w", "--stdin", input="evil\n")
    inner = git_text(repo, "mktree", input=f"100644 blob {blob}\té\n")
    f_blob = git_text(repo, "rev-parse", "HEAD:f.txt")
    root_tree = git_text(
        repo, "mktree", input=f"040000 tree {inner}\t.GIT\n100644 blob {f_blob}\tf.txt\n",
    )
    parent = git_text(repo, "rev-parse", "HEAD")
    commit = git_text(repo, "commit-tree", root_tree, "-p", parent, "-m", "bad tree")
    branch = git_text(repo, "symbolic-ref", "--short", "HEAD")
    git(repo, "update-ref", f"refs/heads/{branch}", commit)

    with pytest.raises(handoff.HandoffRefused, match=r"\.GIT.*\.git.*\.factory"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_an_index_entry_with_a_non_ascii_sibling_under_dot_factory(tmp_path):
    """Same quoting bypass as the ``.GIT`` test above, but for a staged
    (not yet committed) entry picked up by ``git ls-files -s``: without
    ``-z``, a path like ``.factory/é`` is quoted whole as ``".factory/
    \\303\\251"``, corrupting the leading ``.factory`` component."""
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")
    (repo / ".factory").mkdir()
    (repo / ".factory" / "é").write_text("evil\n")
    git(repo, "add", ".factory/é")

    with pytest.raises(handoff.HandoffRefused, match=r"\.factory"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_an_index_entry_with_a_double_quote_in_its_name(tmp_path):
    """Same bypass again, triggered by a literal double-quote character
    instead of a non-ASCII byte -- core.quotePath quotes on either."""
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")
    (repo / ".factory").mkdir()
    (repo / ".factory" / 'x"q').write_text("evil\n")
    git(repo, "add", '.factory/x"q')

    with pytest.raises(handoff.HandoffRefused, match=r"\.factory"):
        handoff.guard_tree(repo)


def test_guard_tree_refuses_a_case_folded_dot_factory_component(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "movable.txt", "hello\n")
    (repo / ".Factory").mkdir()
    git(repo, "mv", "movable.txt", ".Factory/x")
    git(repo, "commit", "-q", "-m", "move into .Factory")

    with pytest.raises(handoff.HandoffRefused, match=r"\.Factory/x"):
        handoff.guard_tree(repo)


def test_guard_tree_git_invocation_shape(monkeypatch, tmp_path):
    """AC-1: git runs with -c core.hooksPath=/dev/null -c core.fsmonitor=false
    and env from process_env.child_env."""
    repo = _init_repo(tmp_path / "repo")
    _commit_file(repo, "f.txt", "hello\n")
    captured = []
    real_run = subprocess.run

    def recording_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)

    handoff.guard_tree(repo)

    assert len(captured) == 2
    expected_env = process_env.child_env()
    for cmd, kwargs in captured:
        assert cmd[:5] == [
            dispatch.GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        ]
        assert kwargs["cwd"] == repo
        assert kwargs["env"] == expected_env


# ---------------------------------------------------------------------------
# AC-2: apply
# ---------------------------------------------------------------------------


def _make_w(tmp_path: Path, source_repo: Path) -> Path:
    w = tmp_path / "W"
    repo = w / "repo"
    git(tmp_path, "clone", "-q", str(source_repo), str(repo))
    return w


def test_apply_a_clean_patch_updates_the_index(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", tmp_path / "failed")
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "a\nb\nc\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    (repo / "f.txt").write_text("a\nB\nc\n")
    patch = git(repo, "diff", "--binary").stdout
    git(repo, "checkout", "--", "f.txt")

    result = handoff.apply(w, patch, "task-clean")

    assert result.repo == repo
    assert (repo / "f.txt").read_text() == "a\nB\nc\n"
    assert git_text(repo, "status", "--short") == "M  f.txt"


def test_apply_refuses_a_dot_git_hooks_patch_and_leaves_dot_git_untouched(tmp_path, monkeypatch):
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    before_hash = _hash_tree(repo / ".git")

    patch = (
        "diff --git a/.git/hooks/pre-commit b/.git/hooks/pre-commit\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/.git/hooks/pre-commit\n"
        "@@ -0,0 +1 @@\n"
        "+evil\n"
    ).encode()

    with pytest.raises(handoff.HandoffRefused) as excinfo:
        handoff.apply(w, patch, "task-hook")

    assert _hash_tree(repo / ".git") == before_hash
    saved = excinfo.value.saved_patch_path
    assert saved is not None
    assert saved.read_bytes() == patch
    assert "task-hook" in saved.name


def test_apply_refuses_a_rename_into_dot_factory(tmp_path, monkeypatch):
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "movable.txt", "hello\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    (repo / ".Factory").mkdir()
    git(repo, "mv", "movable.txt", ".Factory/x")
    patch = git(repo, "diff", "--cached", "-M", "--binary").stdout
    git(repo, "reset", "--hard", "HEAD")

    with pytest.raises(handoff.HandoffRefused, match=r"\.Factory/x") as excinfo:
        handoff.apply(w, patch, "task-factory-rename")

    assert git_text(repo, "status", "--short") == ""
    assert excinfo.value.saved_patch_path.read_bytes() == patch


def test_apply_refuses_a_copy_whose_source_is_a_symlink(tmp_path, monkeypatch):
    """The --summary line for a copy carries no mode field at all when the
    mode does not change -- git apply --index happily writes the new
    symlink, so this is caught only by the SECOND guard_tree call, after the
    real apply already ran (AC-6: skipping the second guard_tree must turn
    this red)."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")
    (source / "linksrc").symlink_to("f.txt")
    git(source, "add", "linksrc")
    git(source, "commit", "-q", "-m", "add symlink")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    (repo / "linkdst").symlink_to("f.txt")
    git(repo, "add", "linkdst")
    patch = git(repo, "diff", "--cached", "-C", "--find-copies-harder", "--binary").stdout
    git(repo, "reset", "--hard", "HEAD")

    with pytest.raises(handoff.HandoffRefused, match="120000"):
        handoff.apply(w, patch, "task-symlink-copy")


def test_apply_refuses_a_rename_of_an_existing_symlink(tmp_path, monkeypatch):
    """A second real-git scenario for the same gap as the copy case above:
    a rename of an already-tracked symlink also carries no mode line."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")
    (source / "linky").symlink_to("f.txt")
    git(source, "add", "linky")
    git(source, "commit", "-q", "-m", "add symlink")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    git(repo, "mv", "linky", "linky2")
    patch = git(repo, "diff", "--cached", "-M", "--binary").stdout
    git(repo, "reset", "--hard", "HEAD")

    with pytest.raises(handoff.HandoffRefused, match="120000"):
        handoff.apply(w, patch, "task-symlink-rename")


def test_apply_refuses_a_patch_over_the_16mib_bound_before_any_git_call(tmp_path, monkeypatch):
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    git_calls = []
    real_run = subprocess.run

    def recording_run(cmd, **kwargs):
        git_calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)

    w = tmp_path / "W"
    (w / "repo").mkdir(parents=True)
    oversized = b"x" * (handoff.MAX_PATCH_BYTES + 1)

    with pytest.raises(handoff.HandoffRefused, match="16777217"):
        handoff.apply(w, oversized, "task-oversized")

    assert git_calls == []
    saved = list(dest_dir.glob("task-oversized-*"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == oversized


def test_apply_saves_every_refused_patch_as_data(tmp_path, monkeypatch):
    """AC-6 mutation: not saving a refused patch must turn this red."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")
    w = _make_w(tmp_path, source)

    garbage_patch = b"not a valid patch at all\n"

    with pytest.raises(handoff.HandoffRefused):
        handoff.apply(w, garbage_patch, "task-garbage")

    saved = list(dest_dir.glob("task-garbage-*"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == garbage_patch


def test_apply_refuses_a_plain_create_under_dot_factory(tmp_path, monkeypatch):
    """Review item 2: the create-mode pre-check parsed `path` out of
    `git apply --summary`'s "create mode" line and checked its MODE but
    never its path component -- so a plain (non-rename, non-copy) create
    under .factory passed the pre-check outright and only the slower,
    already-dirtying path (real apply, then the second guard_tree) would
    have caught it. Asserts the index and work tree are fully untouched,
    which only holds if the pre-check itself refuses before any real
    `git apply --index` runs."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"

    (repo / ".factory").mkdir()
    (repo / ".factory" / "x").write_text("evil\n")
    git(repo, "add", ".factory/x")
    patch = git(repo, "diff", "--cached", "--binary").stdout
    git(repo, "reset", "-q")
    (repo / ".factory" / "x").unlink()
    (repo / ".factory").rmdir()

    before_hash = _hash_tree(repo)

    with pytest.raises(handoff.HandoffRefused, match=r"\.factory/x"):
        handoff.apply(w, patch, "task-factory-create")

    assert git_text(repo, "status", "--porcelain") == ""
    assert _hash_tree(repo) == before_hash


def test_apply_refuses_a_plain_create_under_dot_factory_with_a_non_ascii_name(tmp_path, monkeypatch):
    """Same gap as above, with a non-ASCII filename: `git apply --numstat -z
    --summary` never quotes its own output (unlike `git ls-files`/`git
    ls-tree` without -z), so this was never a quoting bug for apply() --
    it is purely that the create-mode pre-check skipped the component
    check entirely."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "f.txt", "hello\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"

    (repo / ".factory").mkdir()
    (repo / ".factory" / "é").write_text("evil\n")
    git(repo, "add", ".factory/é")
    patch = git(repo, "diff", "--cached", "--binary").stdout
    git(repo, "reset", "-q")
    (repo / ".factory" / "é").unlink()
    (repo / ".factory").rmdir()

    before_hash = _hash_tree(repo)

    with pytest.raises(handoff.HandoffRefused, match=r"\.factory"):
        handoff.apply(w, patch, "task-factory-create-unicode")

    assert git_text(repo, "status", "--porcelain") == ""
    assert _hash_tree(repo) == before_hash


def test_apply_refuses_a_new_symlink_before_any_real_apply(tmp_path, monkeypatch):
    """Review item 3 / AC-6 mutation: deleting the create-mode pre-check at
    handoff.py:161 must turn this red. A patch creating a fresh 120000
    entry (not via rename/copy, which --summary never gives a mode line
    for -- see the copy/rename symlink tests above) is refused by that
    pre-check BEFORE `git apply --index` ever runs, so the index and work
    tree are untouched. Without the pre-check, the real apply would
    succeed and only the second guard_tree() would catch it -- by which
    point the tree is already dirty, which is exactly what this test's
    hash comparison would expose."""
    dest_dir = tmp_path / "failed"
    monkeypatch.setattr(dispatch, "FAILURE_PATCH_DIR", dest_dir)
    source = _init_repo(tmp_path / "source")
    _commit_file(source, "target.txt", "hello\n")

    w = _make_w(tmp_path, source)
    repo = w / "repo"
    (repo / "newlink").symlink_to("target.txt")
    git(repo, "add", "newlink")
    patch = git(repo, "diff", "--cached", "--binary").stdout
    git(repo, "reset", "-q")
    (repo / "newlink").unlink()

    before_hash = _hash_tree(repo)

    with pytest.raises(handoff.HandoffRefused, match="120000"):
        handoff.apply(w, patch, "task-new-symlink")

    assert git_text(repo, "status", "--porcelain") == ""
    assert _hash_tree(repo) == before_hash
