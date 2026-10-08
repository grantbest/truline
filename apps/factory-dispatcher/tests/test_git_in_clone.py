"""dev.finding 79db3113 part b: every dispatcher git call whose cwd is a
worker-written clone goes through one builder, ``dispatch.git_in_clone``,
that disables hooks/fsmonitor/gc/maintenance and neutralises global/system
git config -- and an AST ratchet over a named allowlist makes that mechanical
rather than a convention someone can forget at the next call site.

No network, no substrate: every repo here is built fresh in a tmp dir.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402
from _clone_fixtures import recorded  # noqa: E402


# ---------------------------------------------------------------------------
# AC-1: the builder's own argv/env shape, and the check hook
# ---------------------------------------------------------------------------


def test_git_in_clone_builds_unchanged_argv_and_the_hardened_env(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dispatch, "run", fake_run)
    # clone is never actually created on disk here -- dispatch.run is wholly
    # faked, so this is the Path.cwd()-shape stub case (dev.finding 79db3113
    # part c1, AC-3): recording a fictional path is not possible, so the
    # hook itself is stubbed instead.
    monkeypatch.setattr(dispatch, "check_clone_git_control", lambda clone: None)
    clone = tmp_path / "clone"

    dispatch.git_in_clone(clone, ["status", "--porcelain"], check=False)

    assert captured["cmd"] == [dispatch.GIT, "status", "--porcelain"]
    assert captured["kwargs"]["cwd"] == clone
    assert captured["kwargs"]["check"] is False
    assert "needs" not in captured["kwargs"]  # popped and applied to child_env, not forwarded

    env = captured["kwargs"]["env"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"
    assert env["GIT_CONFIG_VALUE_1"] == "false"
    assert env["GIT_CONFIG_KEY_2"] == "gc.auto"
    assert env["GIT_CONFIG_VALUE_2"] == "0"
    assert env["GIT_CONFIG_KEY_3"] == "maintenance.auto"
    assert env["GIT_CONFIG_VALUE_3"] == "false"
    for name, value in dispatch.FACTORY_GIT_IDENTITY.items():
        assert env[name] == value


def test_git_in_clone_calls_the_check_hook_exactly_once_per_call(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(dispatch, "check_clone_git_control", lambda clone: seen.append(clone))
    monkeypatch.setattr(
        dispatch, "run", lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    clone = tmp_path / "clone"

    dispatch.git_in_clone(clone, ["status"])
    assert seen == [clone]

    dispatch.git_in_clone(clone, ["rev-parse", "HEAD"])
    assert seen == [clone, clone]


def test_check_clone_git_control_fails_closed_with_no_record():
    """Live (dev.finding 79db3113 part c2): a path with no recorded baseline
    at all is refused, not waved through."""
    with pytest.raises(dispatch.CloneGitControlTampered, match="no git-control record"):
        dispatch.check_clone_git_control(Path("/nonexistent"))


# ---------------------------------------------------------------------------
# dev.finding 79db3113 part c2: the hook is live -- check_clone_git_control
# now calls verify_clone_git_control for real, so a builder-routed call on a
# tampered clone must be refused before any git subprocess starts.
# ---------------------------------------------------------------------------


def _recorded_clone(tmp_path: Path) -> Path:
    """A real, on-disk, remote-less clone with a recorded git-control
    baseline -- enough for a direct git_in_clone call to succeed or refuse
    for real, no stubbing of the hook itself."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("hello\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "init")
    recorded(repo)
    return repo


def test_live_hook_refuses_a_tampered_clone_before_any_subprocess_starts(
    tmp_path, monkeypatch
):
    clone = _recorded_clone(tmp_path)
    config = clone / ".git" / "config"
    config.write_text(config.read_text() + "\n[test]\n\tplanted = 1\n")

    def fail_if_called(cmd, **_kwargs):
        raise AssertionError(f"git subprocess started despite tamper: {cmd!r}")

    monkeypatch.setattr(dispatch, "run", fail_if_called)

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch.git_in_clone(clone, ["status", "--porcelain"])

    assert str(config) in str(excinfo.value)


def test_live_hook_lets_an_untampered_clone_proceed(tmp_path):
    clone = _recorded_clone(tmp_path)

    result = dispatch.git_in_clone(clone, ["status", "--porcelain"])

    assert result.returncode == 0


def test_live_hook_catches_tamper_introduced_between_two_builder_calls(tmp_path):
    clone = _recorded_clone(tmp_path)

    first = dispatch.git_in_clone(clone, ["status", "--porcelain"])
    assert first.returncode == 0

    (clone / ".git" / "hooks" / "planted-after-first-call").write_text("x\n")

    with pytest.raises(dispatch.CloneGitControlTampered):
        dispatch.git_in_clone(clone, ["status", "--porcelain"])


def test_live_hook_refuses_a_clone_with_no_recorded_baseline(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "main")
    _git(clone, "commit", "--allow-empty", "-q", "-m", "init")
    # Deliberately never calls recorded(clone) -- fail closed, AC-1.

    with pytest.raises(dispatch.CloneGitControlTampered, match="no git-control record"):
        dispatch.git_in_clone(clone, ["status", "--porcelain"])


# ---------------------------------------------------------------------------
# dispatch.git_subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ([dispatch.GIT, "push", "-q"], "push"),
        ([dispatch.GIT, "-c", "commit.gpgsign=false", "commit", "-q"], "commit"),
        ([dispatch.GIT, "worktree", "add", "--detach"], "worktree"),
        ([dispatch.GIT, "ls-remote", "--heads"], "ls-remote"),
    ],
)
def test_git_subcommand_reads_the_first_non_flag_token(cmd, expected):
    assert dispatch.git_subcommand(cmd) == expected


def test_git_subcommand_raises_when_nothing_but_flags_follow():
    with pytest.raises(ValueError):
        dispatch.git_subcommand([dispatch.GIT, "-c", "x=y"])


# ---------------------------------------------------------------------------
# AC-2: an AST ratchet over a named allowlist
# ---------------------------------------------------------------------------

#: (enclosing function, git subcommand) pairs permitted to call run()/
#: dispatch.run() directly with a GIT argv and a cwd that isn't cfg.repo_root.
ALLOWED_CLONE_GIT_CALLS = frozenset(
    {
        # Always invoked with cwd=cfg.repo_root at fingerprint_tree's own call
        # sites (the second AST test, below, is what ratchets that).
        ("fingerprint_tree", "rev-parse"),
        ("fingerprint_tree", "status"),
        # No cwd at all: the clone doesn't exist yet.
        ("make_clone", "clone"),
        # cwd=dest, but before any worker content exists.
        ("make_clone", "remote"),
    }
)


def _is_git_name(node: ast.AST, *, dotted: bool) -> bool:
    if dotted:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "GIT"
            and isinstance(node.value, ast.Name)
            and node.value.id == "dispatch"
        )
    return isinstance(node, ast.Name) and node.id == "GIT"


def _is_run_call(func: ast.AST, *, dotted: bool) -> bool:
    if dotted:
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "dispatch"
        )
    return isinstance(func, ast.Name) and func.id == "run"


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _subcommand_of(elements: list[ast.AST]) -> str | None:
    """``dispatch.git_subcommand``'s own reading, applied to the literal
    strings in an argv list's AST elements up to and including the first
    resolved subcommand token (``elements[0]`` is GIT itself) -- every real
    call site resolves it from a literal before any dynamic argument, so this
    never needs to look past that point. A dynamic element reached first is
    an unrecognised shape, not a subcommand to guess at."""
    literals = ["GIT"]
    skip_next = False
    for element in elements[1:]:
        if skip_next:
            skip_next = False
            literals.append("")
            continue
        text = _literal_str(element)
        if text is None:
            return None
        literals.append(text)
        if text in ("-c", "-C"):
            skip_next = True
        elif not text.startswith("-"):
            break
    try:
        return dispatch.git_subcommand(literals)
    except ValueError:
        return None


def _cwd_is_repo_root(keywords: list[ast.keyword]) -> bool:
    for kw in keywords:
        if kw.arg == "cwd":
            return isinstance(kw.value, ast.Attribute) and kw.value.attr == "repo_root"
    return False


class _CloneGitCallVisitor(ast.NodeVisitor):
    """Collects every (enclosing function, subcommand) for a run()/
    dispatch.run() call whose argv starts with GIT and whose cwd is not
    (verifiably, by this static read) cfg.repo_root -- a call that must go
    through git_in_clone instead, unless it is in ALLOWED_CLONE_GIT_CALLS."""

    def __init__(self, *, dotted: bool):
        self.dotted = dotted
        self.stack: list[str] = []
        self.list_vars: list[dict[str, list[ast.AST]]] = [{}]
        self.found: set[tuple[str | None, str]] = set()

    def _visit_function(self, node):
        self.stack.append(node.name)
        self.list_vars.append({})
        self.generic_visit(node)
        self.list_vars.pop()
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.List)
        ):
            self.list_vars[-1][node.targets[0].id] = node.value.elts
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        enclosing = self.stack[-1] if self.stack else None
        if enclosing == "git_in_clone":
            return  # the builder's own internal call -- exempt structurally
        if not _is_run_call(node.func, dotted=self.dotted) or not node.args:
            return
        argv = node.args[0]
        if isinstance(argv, ast.List):
            elements = argv.elts
        elif isinstance(argv, ast.Name):
            elements = next(
                (scope[argv.id] for scope in reversed(self.list_vars) if argv.id in scope),
                None,
            )
            if elements is None:
                return
        else:
            return
        if not elements or not _is_git_name(elements[0], dotted=self.dotted):
            return
        if _cwd_is_repo_root(node.keywords):
            return
        subcommand = _subcommand_of(elements)
        if subcommand is None:
            raise AssertionError(
                f"could not read a git subcommand for the call in {enclosing!r} "
                f"at line {node.lineno} -- new shape the ratchet doesn't understand yet"
            )
        self.found.add((enclosing, subcommand))


def _clone_git_call_sites(path: Path, *, dotted: bool) -> set[tuple[str | None, str]]:
    visitor = _CloneGitCallVisitor(dotted=dotted)
    visitor.visit(ast.parse(path.read_text()))
    return visitor.found


def test_every_dispatcher_clone_git_call_goes_through_the_builder():
    found = _clone_git_call_sites(Path(dispatch.__file__), dotted=False)
    found |= _clone_git_call_sites(Path(dispatch_steps.__file__), dotted=True)

    violations = found - ALLOWED_CLONE_GIT_CALLS
    assert not violations, (
        f"these call sites invoke run()/dispatch.run() directly with a clone "
        f"cwd instead of going through git_in_clone: {sorted(violations)}"
    )

    unused = ALLOWED_CLONE_GIT_CALLS - found
    assert not unused, (
        f"these allowlist entries no longer match any call site and must be "
        f"removed: {sorted(unused)}"
    )


# ---------------------------------------------------------------------------
# AC-3: fingerprint_tree is only ever called on cfg.repo_root
# ---------------------------------------------------------------------------


def _fingerprint_tree_violations(path: Path, *, dotted: bool) -> list[str]:
    tree = ast.parse(path.read_text())
    violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            self.generic_visit(node)
            func = node.func
            is_match = (
                isinstance(func, ast.Attribute)
                and func.attr == "fingerprint_tree"
                and isinstance(func.value, ast.Name)
                and func.value.id == "dispatch"
                if dotted
                else isinstance(func, ast.Name) and func.id == "fingerprint_tree"
            )
            if not is_match:
                return
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Attribute) and arg.attr == "repo_root"):
                violations.append(f"{path.name}:{node.lineno}")

    _Visitor().visit(tree)
    return violations


def test_fingerprint_tree_is_called_only_on_repo_root():
    activities_dir = Path(dispatch_steps.__file__).parent
    violations = _fingerprint_tree_violations(Path(dispatch.__file__), dotted=False)
    for activity_file in sorted(activities_dir.glob("*.py")):
        violations += _fingerprint_tree_violations(activity_file, dotted=True)

    assert not violations, (
        f"fingerprint_tree() called with a non-repo_root argument at: {violations} "
        "-- use dispatch.fingerprint_clone for a worker-written clone"
    )


# ---------------------------------------------------------------------------
# AC-4: hooks do not fire, and global/system config is ignored
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [dispatch.GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo_with_hooks(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("hello\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "init")

    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch '{repo}/pre-commit-fired'\n")
    (hooks / "pre-commit").chmod(0o755)
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch '{repo}/post-checkout-fired'\n")
    (hooks / "post-checkout").chmod(0o755)
    recorded(repo)
    return repo


def test_builder_routed_calls_do_not_fire_hooks(tmp_path):
    repo = _init_repo_with_hooks(tmp_path)

    dispatch.git_in_clone(repo, ["checkout", "-b", "feature"])
    (repo / "file.txt").write_text("changed\n")
    dispatch.git_in_clone(repo, ["add", "-A"])
    dispatch.git_in_clone(repo, ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "change"])

    assert not (repo / "post-checkout-fired").exists()
    assert not (repo / "pre-commit-fired").exists()


def test_plain_run_through_the_same_fixture_does_fire_hooks(tmp_path):
    """Control for the test above: the fixture's hooks are real and would
    fire through a plain, un-hardened call -- it is git_in_clone's env, not
    an inert fixture, that suppresses them."""
    repo = _init_repo_with_hooks(tmp_path)

    dispatch.run([dispatch.GIT, "checkout", "-b", "feature"], cwd=repo)
    (repo / "file.txt").write_text("changed\n")
    dispatch.run([dispatch.GIT, "add", "-A"], cwd=repo)
    dispatch.run([dispatch.GIT, "commit", "-q", "-m", "change"], cwd=repo)

    assert (repo / "post-checkout-fired").exists()
    assert (repo / "pre-commit-fired").exists()


def test_global_config_is_ignored_but_repo_config_is_still_read(tmp_path, monkeypatch):
    """The residual part c closes, documented: a global-only setting never
    reaches a builder-routed call, but the clone's OWN .git/config --
    including a url.insteadOf, the exact mechanism the 2026-09-30 gate probe
    used to show hooksPath alone is not enough -- still does. The marker key
    is deliberately not one of git_in_clone's own hardening overrides
    (core.hooksPath/fsmonitor, gc.auto, maintenance.auto), or its own forced
    value would confound "global is ignored" with "global is ignored AND
    happens to agree with the override"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    # This test's own point is that .git/config DOES change between these
    # git_in_clone calls (a url.insteadOf planted mid-test) and must still be
    # read -- the opposite of what the fingerprint hook polices, so the hook
    # is stubbed rather than given a baseline it is designed to outgrow
    # (dev.finding 79db3113 part c1, AC-3).
    monkeypatch.setattr(dispatch, "check_clone_git_control", lambda clone: None)

    fake_global = tmp_path / "fake-global-gitconfig"
    fake_global.write_text("[test]\n\tglobalmarker = from-global\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_global))

    # Control: a plain run() honors GIT_CONFIG_GLOBAL from the ambient env.
    control = dispatch.run([dispatch.GIT, "config", "--get", "test.globalmarker"], cwd=repo)
    assert control.stdout.strip() == "from-global"

    # Through the builder, the same ambient GIT_CONFIG_GLOBAL never reaches git.
    neutralised = dispatch.git_in_clone(
        repo, ["config", "--get", "test.globalmarker"], check=False
    )
    assert neutralised.returncode != 0
    assert neutralised.stdout.strip() == ""

    # Repo-level config -- url.insteadOf among it -- is still read.
    _git(repo, "config", "url.https://repo-rewritten.example/.insteadOf", "https://github.com/")
    repo_read = dispatch.git_in_clone(
        repo, ["config", "--get", "url.https://repo-rewritten.example/.insteadOf"]
    )
    assert repo_read.stdout.strip() == "https://github.com/"
