"""Amendment 30 PR-6: every worker runs inside the dispatcher-owned boundary."""
import inspect
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import containment
import dispatch
import substrate
import worker_revision

# Fixture substrate keys shared by the AC-1/AC-2/AC-4 tests below. Each is
# >= 16 characters so containment._carries_write_key's substring-alias check
# (the length floor that keeps a short fixture from matching unrelated
# strings by accident) actually exercises the substring branch, not just the
# exact-match one.
WRITE_KEY_FIXTURE = "write-key-fixture-0123456789ab"
READ_KEY_FIXTURE = "read-key-fixture-9876543210ba"


def _sandbox_probes_available() -> tuple[bool, str]:
    """Whether this process can (a) write directly under /tmp and (b) apply
    a sandbox-exec profile of its own - both of which these OS-level probes
    need. A dev.task worker running this very suite is itself dispatched
    inside the dispatcher's own deny-write profile (this module, PR-6),
    which denies (a) outright - only the workspace and a private tmpdir are
    allowed - and denies (b) universally once nested: rc=71 "sandbox_apply:
    Operation not permitted" regardless of the inner profile's content, the
    same wall the retired "codex" WorkerEntry hit (dispatch.py), generalised
    from one worker's own sandbox to any second sandbox_apply at all.
    Checked at runtime rather than assumed from ``sys.platform``, so these
    probes skip with a clear reason in that shape of environment and still
    run for real wherever they are not nested.
    """
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="containment-probe-", dir="/tmp")
    except PermissionError as exc:
        return False, f"cannot write directly under /tmp: {exc}"
    finally:
        if probe_dir:
            shutil.rmtree(probe_dir, ignore_errors=True)
    probe = subprocess.run(
        ["sandbox-exec", "-f", "/dev/stdin", "/usr/bin/true"],
        input="(version 1)\n(allow default)\n",
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return False, (probe.stderr or "").strip()
    return True, ""


def test_profile_substitutes_workspace_and_extras(tmp_path):
    text = containment.render_profile(tmp_path, ("~/.codex",))
    assert str(tmp_path) in text
    assert "@WORKSPACE@" not in text
    assert "{EXTRA_ALLOWS}" not in text
    assert ".codex" in text and "~" not in text.split(".codex")[0].split()[-1]


def test_prepare_writes_profile_and_tmp_beside_workspace(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    profile, tmpdir = containment.prepare_containment(ws)
    resolved_parent = ws.resolve().parent
    assert profile.parent == resolved_parent and profile.is_file()
    assert tmpdir.parent == resolved_parent and tmpdir.is_dir()
    assert not (ws / containment.PROFILE_NAME).exists()
    assert str(ws.resolve()) in profile.read_text()
    assert str(tmpdir) in profile.read_text()


def test_profile_paths_are_resolved_through_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    text = containment.render_profile(link / "ws")
    assert str(real) in text or str((link / "ws").resolve()) in text
    assert "/link/" not in text


def test_contained_argv_wraps_with_sandbox_exec(tmp_path):
    argv = containment.contained_argv(("codex", "exec"), tmp_path / "p.sb")
    assert argv[0] == "sandbox-exec"
    assert argv[1] == "-f"
    assert argv[-2:] == ("codex", "exec")


def test_run_worker_invokes_wrapped_argv_with_private_tmpdir(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    # The pre-check resolves the worker binary on the real PATH; CI runners
    # do not carry codex, so resolve it to a stand-in for this unit test.
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    # Distinct fixture keys: dev.finding c48a3827's fail-closed env build
    # (containment.worker_environment) refuses to run at all without them.
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)
    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"), ("~/.codex",))
    assert seen["cmd"][0] == "sandbox-exec"
    assert seen["cmd"][-3:] == ["codex", "exec", "prompt"]
    assert seen["env"]["TMPDIR"] == str(Path(tmp_path).resolve().parent / containment.TMPDIR_NAME)
    profile_text = (Path(tmp_path).resolve().parent / containment.PROFILE_NAME).read_text()
    assert ".codex" in profile_text


def test_worker_scratch_env_var_matches_what_run_worker_sets(tmp_path, monkeypatch):
    """dev.task 4f24656a: the location the worker is told about
    (dispatch.WORKER_SCRATCH_ENV_VAR, quoted into the assembled prompt by
    _worker_scratch_instruction) and the location run_worker actually points
    at must be the same named constant, or the two can silently drift apart."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)
    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))
    assert dispatch.WORKER_SCRATCH_ENV_VAR == "TMPDIR"
    expected = str(Path(tmp_path).resolve().parent / containment.TMPDIR_NAME)
    assert seen["env"][dispatch.WORKER_SCRATCH_ENV_VAR] == expected


def test_claude_worker_scratchpad_root_is_allowed_and_boundary_stays_tight(tmp_path):
    worker = dispatch.WORKER_REGISTRY["claude"]
    scratchpad_root = dispatch.CLAUDE_HARNESS_SCRATCHPAD_ROOT
    assert scratchpad_root in worker.containment_allow

    text = containment.render_profile(tmp_path, worker.containment_allow)
    resolved_root = str(Path(scratchpad_root).resolve())
    assert resolved_root in text
    # Tight boundary: the allowance is the UID-scoped root, never the shared
    # /tmp (or its resolved /private/tmp) it lives under.
    assert '(subpath "/tmp")' not in text
    assert '(subpath "/private/tmp")' not in text


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_os_blocks_writes_outside_workspace_and_allows_inside(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    profile, _ = containment.prepare_containment(workspace)
    blocked = subprocess.run(
        ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(outside)],
        capture_output=True,
    )
    allowed = subprocess.run(
        ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(workspace / "in.txt")],
        capture_output=True,
    )
    assert blocked.returncode != 0 and not outside.exists()
    assert allowed.returncode == 0 and (workspace / "in.txt").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_unresolved_var_workspace_writes_are_allowed(tmp_path):
    """Regression for the first canary: mkdtemp can return /var/... (a symlink
    into /private); the profile must allow the RESOLVED path or every write
    inside the workspace is denied."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    # The alias is constructed, never read off the environment. This test's
    # precondition has blocked the factory twice in one day (2026-08-18):
    # first when the dispatcher handed the suite an already-resolved TMPDIR
    # (baseline check of dev.task 20025031), then when the launchd worker
    # handed it no TMPDIR at all, so mkdtemp fell back to /tmp and the
    # /private/var strip never fired. On macOS /tmp is itself a symlink into
    # /private and mkdtemp returns dir= unresolved, so anchoring there makes
    # the unresolved alias constructible in every environment.
    workdir = Path(tempfile.mkdtemp(prefix="containment-regress-", dir="/tmp"))
    assert workdir != workdir.resolve(), "precondition: tmp path must be an unresolved alias"
    ws = workdir / "repo"
    ws.mkdir()
    profile, tmpdir = containment.prepare_containment(ws)
    inside = subprocess.run(
        ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(ws / "in.txt")],
        capture_output=True,
    )
    tmpwrite = subprocess.run(
        ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(tmpdir / "t.txt")],
        capture_output=True,
    )
    assert inside.returncode == 0, inside.stderr
    assert tmpwrite.returncode == 0, tmpwrite.stderr
    assert (ws / "in.txt").exists() and (tmpdir / "t.txt").exists()


# ---------------------------------------------------------------------------
# dev.finding 79db3113 part a (AC-1): both profiles deny WRITING the
# resolved workspace/cwd's .git entry, .git/hooks, .git/config, .git/info
# and .git/commondir -- placed AFTER the write-allow block so the deny wins
# (seatbelt: the later rule takes precedence).
# ---------------------------------------------------------------------------


def _expected_git_write_deny_lines(resolved: Path) -> list[str]:
    git_dir = resolved / ".git"
    return [
        f'(deny file-write* (literal "{git_dir}"))',
        f'(deny file-write* (subpath "{git_dir / "hooks"}"))',
        f'(deny file-write* (literal "{git_dir / "config"}"))',
        f'(deny file-write* (subpath "{git_dir / "info"}"))',
        f'(deny file-write* (literal "{git_dir / "commondir"}"))',
    ]


def test_worker_profile_denies_the_five_git_control_write_paths_after_the_allows(
    tmp_path, monkeypatch
):
    """Five denies, each present with the workspace's RESOLVED .git path,
    placed after the write-allow block, with every pre-existing rule --
    including #1103's credential-directory read denies -- still present
    byte-for-byte."""
    monkeypatch.delenv("KUBECONFIG", raising=False)
    ws = tmp_path / "repo"
    ws.mkdir()
    text = containment.render_profile(ws)
    resolved = ws.resolve()

    allow_idx = text.index("(allow file-write*")
    for line in _expected_git_write_deny_lines(resolved):
        assert text.count(line) == 1, line
        assert text.index(line) > allow_idx, f"{line} must come after the write-allow block"

    # Old rules survive byte-for-byte.
    assert f'  (subpath "{resolved}")' in text
    assert f'(deny file-read* (subpath "{(Path.home() / ".ssh").resolve()}"))' in text
    assert '(deny network-outbound (remote tcp "*:22"))' in text
    assert "(deny job-creation)" in text


def test_verification_profile_denies_the_five_git_control_write_paths_after_the_allows(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    clone = tmp_path / "repo"
    clone.mkdir()
    text = containment.render_verification_profile(clone)
    resolved = clone.resolve()

    allow_idx = text.index("(allow file-write*")
    for line in _expected_git_write_deny_lines(resolved):
        assert text.count(line) == 1, line
        assert text.index(line) > allow_idx, f"{line} must come after the write-allow block"

    assert f'  (subpath "{resolved}")' in text
    assert f'(deny file-read* (subpath "{(Path.home() / ".ssh").resolve()}"))' in text
    assert '(deny network-outbound (remote tcp "*:22"))' in text
    assert "(deny job-creation)" in text


def _probe_git_containment_under_profile(profile: Path, ws: Path) -> None:
    """Shared by the worker- and verification-profile darwin probes below:
    the five git-control paths are refused, an ordinary file stays
    writable, and `git add`/`commit` (with an explicit -c identity, since
    .git/config is now denied)/`status`/`diff` all still work."""
    git_dir = ws.resolve() / ".git"

    def sandboxed(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sandbox-exec", "-f", str(profile), *argv],
            cwd=str(ws), capture_output=True, text=True,
        )

    assert sandboxed(["/usr/bin/touch", str(git_dir / "hooks" / "pre-commit")]).returncode != 0
    assert sandboxed(["/bin/sh", "-c", f'echo x >> "{git_dir / "config"}"']).returncode != 0
    assert sandboxed(["/usr/bin/touch", str(git_dir / "info" / "exclude")]).returncode != 0
    assert sandboxed(["/usr/bin/touch", str(git_dir / "commondir")]).returncode != 0
    assert sandboxed([dispatch.GIT, "config", "x.y", "z"]).returncode != 0

    ordinary = sandboxed(["/usr/bin/touch", str(ws / "ordinary.txt")])
    assert ordinary.returncode == 0, ordinary.stderr
    assert (ws / "ordinary.txt").exists()

    add = sandboxed([dispatch.GIT, "add", "ordinary.txt"])
    assert add.returncode == 0, add.stderr

    commit = sandboxed(
        [dispatch.GIT, "-c", "user.name=t", "-c", "user.email=t@example.test",
         "commit", "-q", "-m", "second"]
    )
    assert commit.returncode == 0, commit.stderr

    status = sandboxed([dispatch.GIT, "status"])
    assert status.returncode == 0, status.stderr
    diff = sandboxed([dispatch.GIT, "diff", "HEAD~1"])
    assert diff.returncode == 0, diff.stderr

    # Destructive, so run last: a sandbox that failed to deny this would
    # otherwise corrupt the fixture for any assertion after it.
    mv = sandboxed(["/bin/mv", str(git_dir), str(ws.resolve() / "git2")])
    assert mv.returncode != 0


def _init_git_fixture_repo(ws: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}
    subprocess.run([dispatch.GIT, "init", "-q", "-b", "main", str(ws)], check=True, capture_output=True)
    (ws / "f.txt").write_text("one\n")
    subprocess.run([dispatch.GIT, "-C", str(ws), "add", "f.txt"], check=True, capture_output=True, env=env)
    subprocess.run(
        [dispatch.GIT, "-C", str(ws), "commit", "-q", "-m", "init"],
        check=True, capture_output=True, env=env,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_worker_profile_sandbox_denies_git_control_writes_but_allows_ordinary_git(tmp_path):
    """dev.finding 79db3113 part a, AC-2: a real sandbox-exec run over a
    fixture git repository under the WORKER profile. The worker cannot
    observe this probe itself (it skips when nested); it is the outer
    loop's proof, run unsandboxed on darwin, exactly as #1103's
    test_git_add_inside_a_linked_worktree_succeeds_under_the_verification_profile
    (tests/test_containment.py:408 at filing) is."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    ws = tmp_path / "repo"
    ws.mkdir()
    _init_git_fixture_repo(ws)
    profile, _ = containment.prepare_containment(ws)

    _probe_git_containment_under_profile(profile, ws)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_verification_profile_sandbox_denies_git_control_writes_but_allows_ordinary_git(tmp_path):
    """Same proof as above, under the VERIFICATION profile."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    ws = tmp_path / "repo"
    ws.mkdir()
    _init_git_fixture_repo(ws)
    profile, _ = containment.prepare_verification_containment(ws)

    _probe_git_containment_under_profile(profile, ws)


# ---------------------------------------------------------------------------
# dev.finding 6c19f60f: declared verification gets the same OS write
# boundary, its own private tmp, an outside venv, and a state-dir read deny.
# ---------------------------------------------------------------------------


def _write_allow_entries(profile_text: str) -> set[str]:
    """Every ``(subpath ...)``/``(literal ...)`` inside the ``(allow
    file-write* ...)`` block ONLY -- a ``(deny file-read* ...)`` line is not
    a write entry and must never be counted (AC-1), and (dev.finding
    79db3113 part a) neither are the ``(deny file-write* ...)`` git-control
    denies now rendered right after this block -- the split below stops at
    the first of those."""
    after = profile_text.split("(allow file-write*", 1)[1]
    allow_block = after.split("\n(deny file-write*", 1)[0]
    return set(re.findall(r'\((?:subpath|literal) "([^"]+)"\)', allow_block))


def _read_deny_entries(profile_text: str) -> list[tuple[str, str]]:
    return re.findall(r'\(deny file-read\* \((subpath|literal) "([^"]+)"\)\)', profile_text)


def _entries_for(entries: list[tuple[str, str]], *paths: str) -> list[tuple[str, str]]:
    """Narrows a deny-entry list down to just ``paths`` -- AC-3 (dev.findings
    019909c0, 75b50b58) widened the file-read deny set every profile carries
    to include CREDENTIAL_DIRS, KUBECONFIG's targets and their symlinked
    escapes, so a predecessor's 'this is the WHOLE set' assertion is
    narrowed to 'these are the entries for this path', per path, rather
    than re-asserting the whole set."""
    wanted = set(paths)
    return [entry for entry in entries if entry[1] in wanted]


def test_verification_profile_exact_write_set_for_a_plain_clone(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    clone = tmp_path / "repo"
    clone.mkdir()
    text = containment.render_verification_profile(clone)
    resolved = clone.resolve()
    verify_tmp = resolved.parent / containment.VERIFY_TMPDIR_NAME
    assert _write_allow_entries(text) == {str(resolved), str(verify_tmp), "/dev", "/dev/null"}
    # SEC-6c19f60f's original AC-1 assertion here was `"(deny network*)" not
    # in text` -- true when this profile denied no network at all. AC-3
    # (dev.findings 75b50b58, dd648709) reverses that: the profile now
    # denies outbound ssh and every loopback port but the substrate
    # tunnel's. The replacement pins the reversal -- exactly those rules,
    # in order -- rather than re-asserting the premise AC-3 overturned.
    import launchd_agent

    substrate_port = launchd_agent.TUNNEL_DEFAULTS["substrate-prod"]["local_port"]
    assert _network_rule_entries(text) == [
        '(deny network-outbound (remote tcp "*:22"))',
        '(deny network-outbound (remote tcp "localhost:*"))',
        f'(allow network-outbound (remote tcp "localhost:{substrate_port}"))',
    ]


def _git_rev_parse(worktree: Path, *args: str) -> str:
    return subprocess.run(
        [dispatch.GIT, "-C", str(worktree), "rev-parse", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_verification_profile_exact_write_set_for_a_linked_worktree(tmp_path):
    """The expected set is computed INDEPENDENTLY of
    ``linked_worktree_write_extras`` -- via `git rev-parse --git-dir` /
    `--git-common-dir`, per dev.finding 6c19f60f's review -- so a helper bug
    (for example, dropping the common object store, or the helper silently
    adding an unrelated entry such as $HOME) fails this test instead of
    passing it vacuously. The profile itself is still rendered through the
    real helper, matching what run_verification_shell does at its call
    site."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([dispatch.GIT, "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "f.txt").write_text("one\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}
    subprocess.run([dispatch.GIT, "-C", str(repo), "add", "f.txt"], check=True, capture_output=True, env=env)
    subprocess.run(
        [dispatch.GIT, "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True, capture_output=True, env=env,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        [dispatch.GIT, "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )

    # Independently derived expectation -- not via containment.py at all.
    git_dir = Path(_git_rev_parse(worktree, "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    expected_admin_dir = git_dir.resolve(strict=True)
    common_dir = Path(_git_rev_parse(worktree, "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = worktree / common_dir
    expected_common_objects = (common_dir.resolve(strict=True) / "objects")

    # Rendered through the SAME helper run_verification_shell calls, so
    # production and test still exercise the identical code path.
    extras = containment.linked_worktree_write_extras(worktree)
    assert extras, "a linked worktree must produce at least the admin dir and object store"
    text = containment.render_verification_profile(worktree, extras)

    resolved = worktree.resolve()
    verify_tmp = resolved.parent / containment.VERIFY_TMPDIR_NAME
    expected = {
        str(resolved), str(verify_tmp), "/dev", "/dev/null",
        str(expected_admin_dir), str(expected_common_objects),
    }
    assert _write_allow_entries(text) == expected


def test_verification_profile_write_set_excludes_venv_parent_home_and_scratchpad(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    clone = tmp_path / "run" / "repo"
    clone.mkdir(parents=True)

    text = containment.render_verification_profile(clone)
    entries = {Path(e) for e in _write_allow_entries(text) if e not in ("/dev", "/dev/null")}

    venv = containment.verification_venv_path(clone)
    parent = clone.resolve().parent
    home = Path(tmp_path / "home").resolve()
    scratchpad = Path(dispatch.CLAUDE_HARNESS_SCRATCHPAD_ROOT).resolve()

    assert venv not in entries
    assert parent not in entries
    assert home not in entries
    assert scratchpad not in entries
    assert not any(entry == venv or venv in entry.parents for entry in entries)


def test_verification_profile_state_dir_read_deny_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    clone = tmp_path / "repo"
    clone.mkdir()

    text = containment.render_verification_profile(clone)

    expected = str((tmp_path / "home" / ".factory-dispatcher").resolve())
    # Narrowed per AC-3 (dev.findings 019909c0, 75b50b58): the file-read
    # deny set now also carries CREDENTIAL_DIRS -- this asserts only the
    # state-directory entry, by path, not the whole set.
    assert _entries_for(_read_deny_entries(text), expected) == [("subpath", expected)]


def test_verification_profile_state_dir_read_deny_with_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    override = tmp_path / "elsewhere" / "state"
    override.mkdir(parents=True)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(override))
    clone = tmp_path / "repo"
    clone.mkdir()

    text = containment.render_verification_profile(clone)

    home_dir = str((tmp_path / "home" / ".factory-dispatcher").resolve())
    override_dir = str(override.resolve())
    assert set(_entries_for(_read_deny_entries(text), home_dir, override_dir)) == {
        ("subpath", home_dir),
        ("subpath", override_dir),
    }


def test_prepare_verification_containment_refuses_a_cwd_under_the_state_dir(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    state_dir = home / ".factory-dispatcher"
    cwd = state_dir / "some" / "path"
    cwd.mkdir(parents=True)

    with pytest.raises(ValueError, match=re.escape(str(state_dir.resolve()))):
        containment.prepare_verification_containment(cwd)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_git_add_inside_a_linked_worktree_succeeds_under_the_verification_profile(tmp_path):
    """dev.finding 6c19f60f AC-1: the admin dir alone still gets EPERM on
    `git add` (it writes loose objects into the COMMON object store) -- this
    failed all three prior attempts at this bead. The worker cannot observe
    this probe itself (it skips when nested); it is the outer loop's proof."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}
    subprocess.run([dispatch.GIT, "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run([dispatch.GIT, "-C", str(repo), "add", "f.txt"], check=True, capture_output=True, env=env)
    subprocess.run(
        [dispatch.GIT, "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True, capture_output=True, env=env,
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        [dispatch.GIT, "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, capture_output=True,
    )
    (worktree / "g.txt").write_text("two\n")

    extras = containment.linked_worktree_write_extras(worktree)
    profile, _ = containment.prepare_verification_containment(worktree, extras)

    result = subprocess.run(
        ["sandbox-exec", "-f", str(profile), dispatch.GIT, "add", "g.txt"],
        cwd=str(worktree), capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr


# --- AC-2: fail-closed classification of sandbox-exec vs. command failures ---


def _fake_completed(returncode: int, stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["sandbox-exec"], returncode, "", stderr)


def test_run_verification_shell_wraps_argv_with_sandbox_exec(tmp_path, monkeypatch):
    clone = tmp_path / "repo"
    clone.mkdir()
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _fake_completed(0, "")

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    dispatch.run_verification_shell(clone, "true", {}, 5)

    assert seen["cmd"][0] == "sandbox-exec"
    assert seen["cmd"][1] == "-f"
    assert tuple(seen["cmd"][-3:]) == ("/bin/sh", "-c", "true")


@pytest.mark.parametrize(
    "returncode,stderr",
    [
        (65, "sandbox-exec: /tmp/x/.verify-containment.sb: 3: syntax error near unexpected token"),
        (65, "sandbox-exec: /tmp/x/.verify-containment.sb: No such file or directory"),
        (71, "sandbox-exec: sandbox_apply: Operation not permitted"),
    ],
)
def test_sandbox_exec_own_failure_is_could_not_start_never_failed(
    tmp_path, monkeypatch, returncode, stderr
):
    clone = tmp_path / "repo"
    clone.mkdir()
    monkeypatch.setattr(
        dispatch.subprocess, "run", lambda *_a, **_k: _fake_completed(returncode, stderr)
    )
    result = dispatch.run_verification_shell(clone, "true", {}, 5)
    assert result.outcome == "could_not_start"


@pytest.mark.parametrize("returncode", [65, 71])
def test_command_exit_65_or_71_without_sandbox_prefix_is_failed(tmp_path, monkeypatch, returncode):
    """A test suite's own exit 65/71 must not be laundered into could_not_start
    just because it shares a number with a sandbox-exec failure mode."""
    clone = tmp_path / "repo"
    clone.mkdir()
    monkeypatch.setattr(
        dispatch.subprocess,
        "run",
        lambda *_a, **_k: _fake_completed(returncode, "AssertionError: something the command itself raised"),
    )
    result = dispatch.run_verification_shell(clone, "true", {}, 5)
    assert result.outcome == "failed"


# --- AC-3: the venv lives outside the clone, immune to a planted package ---


def test_venv_creation_uses_isolated_mode_with_cwd_at_the_clones_parent(tmp_path):
    """Control (OLD shape, must still plant the marker -- a Python whose
    import rules changed would otherwise pass this test vacuously) vs. fix
    (dispatcher's actual invocation, must NOT plant it)."""
    clone = tmp_path / "repo"
    clone.mkdir()
    venv_pkg = clone / "venv"
    venv_pkg.mkdir()
    marker = tmp_path / "marker.txt"
    (venv_pkg / "__init__.py").write_text("")
    (venv_pkg / "__main__.py").write_text(f"open({str(marker)!r}, 'w').write('planted')\n")
    python = sys.executable

    old_target = tmp_path / "old-venv"
    subprocess.run(
        [python, "-m", "venv", str(old_target)], cwd=str(clone), capture_output=True, text=True
    )
    assert marker.exists(), "control precondition: the planted venv package must shadow stdlib venv"
    marker.unlink()

    new_target = containment.verification_venv_path(clone)
    result = subprocess.run(
        [python, "-I", "-m", "venv", str(new_target)],
        cwd=str(clone.resolve().parent), capture_output=True, text=True,
    )
    assert not marker.exists(), result.stderr
    assert (new_target / "bin" / "python").exists()


def test_bootstrap_skips_creation_only_when_the_outside_venv_exists(tmp_path, monkeypatch):
    """A worker-planted <clone>/venv must never suppress creation of the
    real, outside venv."""
    clone = tmp_path / "repo"
    (clone / "venv" / "bin").mkdir(parents=True)
    (clone / "venv" / "bin" / "python").write_text("")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dispatch, "run", fake_run)
    monkeypatch.setattr(dispatch, "verification_dependency_args", lambda _clone: [])
    monkeypatch.setattr(dispatch, "resolve_verification_python", lambda: sys.executable)

    dispatch.bootstrap_verification_env(clone)

    venv_calls = [c for c in calls if "venv" in c and "-m" in c]
    assert venv_calls, "bootstrap did not attempt to create the outside venv"
    assert "-I" in venv_calls[0]


# --- AC-4: the bootstrap's own pip install runs contained ---


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_hostile_pep517_backend_cannot_write_outside_the_contained_set(tmp_path, monkeypatch):
    """A worker-edited local app's pyproject.toml can name an in-tree PEP 517
    build backend (`backend-path`). That backend's code now runs inside the
    verification profile during the bootstrap's pip install -- it should get
    EPERM on anything outside {clone, the outside venv, .verify-tmp}, never a
    real write."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    escaped = tmp_path / "escaped.txt"
    run_dir = tmp_path / "run"
    clone = run_dir / "repo"
    app_dir = clone / "apps" / "evil"
    backend_dir = app_dir / "_backend"
    backend_dir.mkdir(parents=True)
    (app_dir / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "evilbackend"\n'
        'backend-path = ["_backend"]\n'
    )
    errno_path = clone / "errno.txt"
    (backend_dir / "evilbackend.py").write_text(
        "import errno as _errno\n"
        f"_escaped = {str(escaped)!r}\n"
        f"_errno_path = {str(errno_path)!r}\n"
        "def _try():\n"
        "    try:\n"
        "        open(_escaped, 'w').write('escaped')\n"
        "    except OSError as exc:\n"
        "        open(_errno_path, 'w').write(str(exc.errno))\n"
        "def get_requires_for_build_wheel(config_settings=None):\n"
        "    _try()\n"
        "    return []\n"
        "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
        "    _try()\n"
        "    raise SystemExit(1)\n"
    )
    monkeypatch.setattr(dispatch, "verification_dependency_args", lambda _clone: ["--no-index", "apps/evil"])
    monkeypatch.setattr(dispatch, "resolve_verification_python", lambda: sys.executable)

    try:
        dispatch.bootstrap_verification_env(clone)
    except Exception:
        pass  # the install is expected to fail; that failure is not the assertion

    assert not escaped.exists()
    assert errno_path.exists(), "the backend never ran at all"
    assert errno_path.read_text().strip() == "1"  # EPERM


# --- AC-5: a planted conftest.py is denied everything outside the set ---


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_planted_conftest_is_denied_writes_and_the_state_dir_read(tmp_path, monkeypatch):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    home = tmp_path  # the suite's own conftest already sets HOME = tmp_path
    monkeypatch.setenv("HOME", str(home))
    state_dir = home / ".factory-dispatcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = "SECRET-MARKER-6c19f60f"
    (state_dir / "env").write_text(marker)

    clone = tmp_path / "run" / "repo"
    clone.mkdir(parents=True)
    escaped = tmp_path / "escaped.txt"
    venv_dir = containment.verification_venv_path(clone)
    venv_dir.mkdir(parents=True)
    env_probe_path = str(state_dir / "env")
    (clone / "conftest.py").write_text(
        "import os\n"
        f"_escaped = {str(escaped)!r}\n"
        f"_venv_probe = {str(venv_dir / 'probe.txt')!r}\n"
        f"_env_probe = {env_probe_path!r}\n"
        "def _errno_of(fn):\n"
        "    try:\n"
        "        fn()\n"
        "        return 0\n"
        "    except OSError as exc:\n"
        "        return exc.errno\n"
        "escaped_errno = _errno_of(lambda: open(_escaped, 'w').write('x'))\n"
        "venv_errno = _errno_of(lambda: open(_venv_probe, 'w').write('x'))\n"
        "read_errno = _errno_of(lambda: open(_env_probe).read())\n"
        "with open(os.path.join(os.path.dirname(__file__), 'recorded.txt'), 'w') as fh:\n"
        "    fh.write(f'{escaped_errno},{venv_errno},{read_errno}')\n"
    )
    (clone / "test_trivial.py").write_text("def test_ok():\n    assert True\n")

    extras = containment.linked_worktree_write_extras(clone)
    profile, verify_tmp = containment.prepare_verification_containment(clone, extras)
    command = f"{sys.executable} -m pytest test_trivial.py -q"
    argv = containment.contained_argv(("/bin/sh", "-c", command), profile)
    result = subprocess.run(
        argv, cwd=str(clone), capture_output=True, text=True,
        env={**os.environ, "TMPDIR": str(verify_tmp)},
    )

    assert not escaped.exists()
    recorded = (clone / "recorded.txt").read_text()
    escaped_errno, venv_errno, read_errno = (int(v) for v in recorded.split(","))
    assert escaped_errno == 1  # EPERM
    assert venv_errno == 1  # EPERM: the venv is writable only by the contained pip install
    assert read_errno == 1  # EPERM: the state-dir read deny
    assert marker not in recorded
    assert marker not in result.stdout + result.stderr


# --- AC-6: a planted venv/bin/python is never consulted ---


def test_planted_venv_bin_python_is_never_used(tmp_path, monkeypatch):
    """Platform-independent: the OS boundary itself is proven by the real
    sandbox tests above (AC-5); this only proves the PATH/existence-check
    wiring never looks at <clone>/venv."""
    if sys.platform != "darwin":
        monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)
    else:
        available, reason = _sandbox_probes_available()
        if not available:
            monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)

    # Linux CI test seam only, never a production concern: actions/setup-python's
    # Ubuntu builds are `--enable-shared`, so the interpreter needs
    # LD_LIBRARY_PATH to find libpython3.NN.so.1.0 at exec time. The scrubbed
    # verification environment omits it deliberately -- this dispatcher's own
    # host is macOS, whose interpreter is not built this way, and no declared
    # command legitimately needs it -- so it is passed through here only, for
    # this test's own venv-created interpreter, rather than added to
    # VERIFICATION_ENV_ALLOWLIST. What this test proves (PATH/existence-check
    # wiring, not environment content) is unaffected either way.
    ld_library_path = os.environ.get("LD_LIBRARY_PATH")
    if ld_library_path:
        real_verification_env = dispatch._verification_env
        monkeypatch.setattr(
            dispatch,
            "_verification_env",
            lambda clone: {**real_verification_env(clone), "LD_LIBRARY_PATH": ld_library_path},
        )

    clone = tmp_path / "repo"
    planted_bin = clone / "venv" / "bin"
    planted_bin.mkdir(parents=True)
    marker = tmp_path / "planted-ran.txt"
    (planted_bin / "python").write_text(f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
    os.chmod(planted_bin / "python", 0o755)

    outside = containment.verification_venv_path(clone)
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(outside)],
        cwd=str(clone.resolve().parent), check=True, capture_output=True,
    )

    result = dispatch.run_verification_command(clone, 'python -c "import sys; print(sys.prefix)"')

    assert not marker.exists()
    assert result.outcome == "passed", result.output
    assert Path(result.output.strip()).resolve() == outside.resolve()


# ---------------------------------------------------------------------------
# dev.finding c48a3827: the dev.task worker is handed the read-only substrate
# key, never the write key, and cannot read the dispatcher's state directory.
# ---------------------------------------------------------------------------


def test_state_dir_env_name_matches_worker_revision():
    """containment.py reads the name literally rather than importing
    worker_revision (an import cycle: worker_revision imports dispatch,
    which imports containment) -- this pins the two literals equal."""
    assert containment.STATE_DIR_ENV == worker_revision.STATE_DIR_ENV


# --- AC-1: worker_environment strips the write key, installs the read key ---


def test_worker_environment_installs_the_read_key_when_distinct():
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": READ_KEY_FIXTURE,
    }
    env, reason = containment.worker_environment(base_env)
    assert reason is None
    assert env["SUBSTRATE_API_KEY"] == READ_KEY_FIXTURE
    assert WRITE_KEY_FIXTURE not in env.values()


def test_worker_environment_strips_every_alias_of_the_write_key():
    """W under an ambient name, under a name with surrounding whitespace,
    under a name as a substring (a header/URL embedding it), and re-added by
    a WorkerEntry's extra_env under the protected name itself -- none of it
    survives, because the check runs AFTER the additions are merged in."""
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": READ_KEY_FIXTURE,
        "AMBIENT_COPY": WRITE_KEY_FIXTURE,
        "WHITESPACE_COPY": f"  {WRITE_KEY_FIXTURE}  ",
        "SUBSTRING_ALIAS": f"Bearer {WRITE_KEY_FIXTURE}",
    }
    env, reason = containment.worker_environment(
        base_env, additions=(("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE),)
    )
    assert reason is None
    for name in ("AMBIENT_COPY", "WHITESPACE_COPY", "SUBSTRING_ALIAS"):
        assert name not in env
    assert WRITE_KEY_FIXTURE not in env.values()
    assert not any(WRITE_KEY_FIXTURE in value for value in env.values())


def test_worker_environment_refuses_when_read_key_unset():
    base_env = {"PATH": "/usr/bin", "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE}
    env, reason = containment.worker_environment(base_env)
    assert "SUBSTRATE_API_KEY" not in env
    assert "SUBSTRATE_READ_API_KEY" not in env
    assert WRITE_KEY_FIXTURE not in env.values()
    assert reason is not None
    assert "SUBSTRATE_READ_API_KEY" in reason
    assert (
        "set SUBSTRATE_READ_API_KEY (distinct from SUBSTRATE_API_KEY) in "
        "the worker's launchd env file and restart the worker"
    ) in reason
    assert WRITE_KEY_FIXTURE not in reason


def test_worker_environment_refuses_when_read_key_blank():
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": "   ",
    }
    env, reason = containment.worker_environment(base_env)
    assert "SUBSTRATE_API_KEY" not in env
    assert "SUBSTRATE_READ_API_KEY" not in env
    assert reason is not None
    assert WRITE_KEY_FIXTURE not in reason


def test_worker_environment_unset_and_blank_read_key_give_the_identical_reason():
    unset_env = {"PATH": "/usr/bin", "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE}
    blank_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": "",
    }
    _, unset_reason = containment.worker_environment(unset_env)
    _, blank_reason = containment.worker_environment(blank_env)
    assert unset_reason == blank_reason


def test_worker_environment_refuses_when_read_key_equals_write_key():
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": WRITE_KEY_FIXTURE,
    }
    env, reason = containment.worker_environment(base_env)
    assert "SUBSTRATE_API_KEY" not in env
    assert "SUBSTRATE_READ_API_KEY" not in env
    assert WRITE_KEY_FIXTURE not in env.values()
    assert reason is not None
    assert "SUBSTRATE_READ_API_KEY" in reason
    assert WRITE_KEY_FIXTURE not in reason


def test_worker_environment_passes_through_unrelated_entries_and_scratch_var():
    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/worker",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-value-unrelated",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": READ_KEY_FIXTURE,
    }
    env, reason = containment.worker_environment(base_env, (("TMPDIR", "/tmp/scratch"),))
    assert reason is None
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/Users/worker"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token-value-unrelated"
    assert env["TMPDIR"] == "/tmp/scratch"


# ---------------------------------------------------------------------------
# dev.findings 019909c0, 75b50b58: the worker gets no KUBECONFIG or
# SSH_AUTH_SOCK, and the host's named credential directories are denied.
# ---------------------------------------------------------------------------


def test_worker_environment_strips_kubeconfig_and_ssh_auth_sock():
    """AC-1(a): both names are absent from the output outright, regardless
    of value -- the worker never pushes (no use for the ssh agent) and
    never reads the cluster (every kubectl call runs in the dispatcher)."""
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": READ_KEY_FIXTURE,
        "KUBECONFIG": "/Users/operator/.kube/config",
        "SSH_AUTH_SOCK": "/var/run/example-agent.sock",
    }
    env, reason = containment.worker_environment(base_env)
    assert reason is None
    assert "KUBECONFIG" not in env
    assert "SSH_AUTH_SOCK" not in env


def test_worker_environment_strips_kubeconfig_and_ssh_auth_sock_from_extra_env():
    """AC-1(b): extra_env (a WorkerEntry's own additions, merged in before
    the strip) cannot restore either name -- the check runs AFTER the
    merge, same ordering guarantee as the write-key alias check above."""
    base_env = {
        "PATH": "/usr/bin",
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": READ_KEY_FIXTURE,
    }
    env, reason = containment.worker_environment(
        base_env,
        additions=(
            ("KUBECONFIG", "/example/kc"),
            ("SSH_AUTH_SOCK", "/example/agent.sock"),
        ),
    )
    assert reason is None
    assert "KUBECONFIG" not in env
    assert "SSH_AUTH_SOCK" not in env


def test_run_worker_strips_kubeconfig_and_ssh_auth_sock_from_the_dispatchers_own_env(
    tmp_path, monkeypatch
):
    """AC-1(d): through dispatch.run_worker with a recording double, the
    wrapped worker's env carries neither name, and the dispatcher's own
    os.environ is untouched afterwards -- run_worker copies, never mutates."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)
    monkeypatch.setenv("KUBECONFIG", "/Users/operator/.kube/config")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/var/run/example-agent.sock")

    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert "KUBECONFIG" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert os.environ["KUBECONFIG"] == "/Users/operator/.kube/config"
    assert os.environ["SSH_AUTH_SOCK"] == "/var/run/example-agent.sock"


def test_dispatcher_keeps_kubeconfig_and_ssh_auth_sock_while_worker_and_verification_lose_them(
    tmp_path, monkeypatch
):
    """AC-4: the routes this bead closes are the WORKER's and declared
    VERIFICATION's, never the dispatcher's own process -- every cluster
    read (activities/ea_observation.py among them), `git push` and `gh`
    call runs in the dispatcher, outside either sandbox, and keeps its own
    configuration."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from activities import ea_observation

    monkeypatch.setenv("KUBECONFIG", "/Users/operator/.kube/config")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/var/run/example-agent.sock")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)

    monkeypatch.setattr(
        dispatch.subprocess,
        "run",
        lambda *_a, **_k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")

    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))
    dispatch._verification_env(tmp_path)

    assert os.environ["KUBECONFIG"] == "/Users/operator/.kube/config"
    assert os.environ["SSH_AUTH_SOCK"] == "/var/run/example-agent.sock"
    ea_observation._require_kubeconfig()  # must not raise


# --- AC-2: run_worker uses worker_environment and fails closed ---


def test_run_worker_hands_the_wrapped_worker_the_read_key(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)

    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert env["SUBSTRATE_API_KEY"] == READ_KEY_FIXTURE
    assert WRITE_KEY_FIXTURE not in env.values()


def test_run_worker_refuses_when_read_key_unset(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dispatch.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.delenv("SUBSTRATE_READ_API_KEY", raising=False)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert "SUBSTRATE_READ_API_KEY" in str(excinfo.value)
    assert (
        "set SUBSTRATE_READ_API_KEY (distinct from SUBSTRATE_API_KEY) in "
        "the worker's launchd env file and restart the worker"
    ) in str(excinfo.value)
    assert calls == []


def test_run_worker_refuses_when_read_key_equals_write_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dispatch.subprocess, "run", lambda *a, **k: calls.append((a, k))
    )
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", WRITE_KEY_FIXTURE)

    with pytest.raises(dispatch.DispatchEnvironmentError) as excinfo:
        dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert "SUBSTRATE_READ_API_KEY" in str(excinfo.value)
    assert calls == []


def test_run_worker_receives_only_the_oauth_token_and_the_read_key(tmp_path, monkeypatch):
    """AC-3 (SEC-a0166920-2): run_worker builds the worker's base environment
    from process_env.child_env(needs=(...)) instead of raw os.environ, so a
    credential this child does not need -- DISCORD_WEBHOOK_URL, the merge
    script's credential -- never reaches containment.worker_environment, and
    therefore never reaches the worker's exec environment either."""
    oauth_token = "oauth-token-fixture-0123456789ab"
    discord_webhook = "https://discord.example.invalid/webhook-fixture-0123456789"
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", oauth_token)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", discord_webhook)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")

    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    env = captured["env"]
    assert env["SUBSTRATE_API_KEY"] == READ_KEY_FIXTURE
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == oauth_token
    assert "SUBSTRATE_READ_API_KEY" not in env
    assert "DISCORD_WEBHOOK_URL" not in env
    values = list(env.values())
    assert all(WRITE_KEY_FIXTURE not in v for v in values)
    assert all(discord_webhook not in v for v in values)


def test_run_worker_never_mutates_the_dispatchers_own_environment(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)

    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert os.environ["SUBSTRATE_API_KEY"] == WRITE_KEY_FIXTURE
    assert os.environ["SUBSTRATE_READ_API_KEY"] == READ_KEY_FIXTURE


# --- AC-4: the dispatcher's own store client keeps the write key ---


def test_dispatcher_store_client_keeps_the_write_key_while_worker_gets_the_read_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY_FIXTURE)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY_FIXTURE)

    captured_worker = {}

    def fake_run(cmd, **kwargs):
        captured_worker["env"] = kwargs.get("env") or {}

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))

    assert captured_worker["env"]["SUBSTRATE_API_KEY"] == READ_KEY_FIXTURE

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "note-1"}

    captured_request = {}

    # Bound against the real httpx.request signature so this double rejects
    # what the live dependency rejects: a caller-passed keyword httpx.request
    # does not take (for example ``bogus_kw=1``) raises TypeError here too,
    # not just in production (CLAUDE.md: "a test double that accepts more
    # than the live contract is a second implementation").
    _real_httpx_request_sig = inspect.signature(httpx.request)

    def fake_httpx_request(*args, **kwargs):
        bound = _real_httpx_request_sig.bind(*args, **kwargs)
        captured_request["headers"] = bound.arguments.get("headers")
        return FakeResponse()

    monkeypatch.setattr(substrate.httpx, "request", fake_httpx_request)

    sub = substrate.Substrate(base_url="http://substrate.test")
    sub.add_note("task-1", "status", "done", "factory-dispatcher")

    assert captured_request["headers"]["X-API-Key"] == WRITE_KEY_FIXTURE


# --- AC-5: the worker profile denies reading the state directory ---


def test_worker_profile_state_dir_read_deny_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    text = containment.render_profile(tmp_path / "ws")

    expected = str((tmp_path / "home" / ".factory-dispatcher").resolve())
    entries = _read_deny_entries(text)
    # Narrowed per AC-3 (dev.findings 019909c0, 75b50b58): the file-read
    # deny set now also carries CREDENTIAL_DIRS -- this asserts only the
    # state-directory entry, by path, not the whole set.
    assert _entries_for(entries, expected) == [("subpath", expected)]
    # Placement: the deny comes after (allow default), before (deny file-write*).
    assert text.index("(allow default)") < text.index(f'(subpath "{expected}")')
    assert text.index(f'(subpath "{expected}")') < text.index("(deny file-write*)")


def test_worker_profile_state_dir_read_deny_with_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    override = tmp_path / "elsewhere" / "state"
    override.mkdir(parents=True)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(override))

    text = containment.render_profile(tmp_path / "ws")

    home_dir = str((tmp_path / "home" / ".factory-dispatcher").resolve())
    override_dir = str(override.resolve())
    assert set(_entries_for(_read_deny_entries(text), home_dir, override_dir)) == {
        ("subpath", home_dir),
        ("subpath", override_dir),
    }


def test_worker_profile_state_dir_read_deny_collapses_when_override_equals_default(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    state_dir = home / ".factory-dispatcher"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(state_dir))

    text = containment.render_profile(tmp_path / "ws")

    expected = str(state_dir.resolve())
    assert _entries_for(_read_deny_entries(text), expected) == [("subpath", expected)]


def test_worker_profile_state_dir_deny_is_never_a_literal(tmp_path, monkeypatch):
    """A literal deny on one file inside the state directory is exactly the
    #859 gate's F1 finding: a sibling file (or nested file) escapes it. Only
    a subpath form covers the whole directory. Narrowed per AC-3 (dev.
    findings 019909c0, 75b50b58): CREDENTIAL_DIRS, KUBECONFIG's targets and
    their symlinked escapes legitimately use the literal form now, so this
    checks only the state directory's own entry, never the whole text."""
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    text = containment.render_profile(Path("/tmp/some-workspace"))
    state_dir = str((Path.home() / ".factory-dispatcher").resolve())
    assert f'(deny file-read* (literal "{state_dir}"))' not in text


def test_prepare_containment_refuses_a_workspace_under_the_default_state_dir(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    state_dir = home / ".factory-dispatcher"
    workspace = state_dir / "some" / "path"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match=re.escape(str(state_dir.resolve()))):
        containment.prepare_containment(workspace)


def test_prepare_containment_refuses_a_workspace_under_the_overridden_state_dir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    override = tmp_path / "elsewhere" / "state"
    workspace = override / "some" / "path"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(override))

    with pytest.raises(ValueError, match=re.escape(str(override.resolve()))):
        containment.prepare_containment(workspace)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_worker_profile_denies_reading_anywhere_under_the_state_dir(tmp_path, monkeypatch):
    """#859's gate finding F1: a literal deny on one file left a SIBLING file
    in the same directory readable. Proves the class, not just the one path:
    the env file itself, a sibling, and a nested file are all denied, while a
    read inside the workspace still succeeds."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    home = tmp_path  # the suite's own conftest already sets HOME = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    state_dir = home / ".factory-dispatcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "env").write_text("SUBSTRATE_API_KEY=super-secret\n")
    (state_dir / "env.bak-test").write_text("SUBSTRATE_API_KEY=super-secret\n")
    (state_dir / "sub").mkdir()
    (state_dir / "sub" / "file").write_text("nested-secret\n")

    workspace = tmp_path / "run" / "repo"
    workspace.mkdir(parents=True)
    (workspace / "readable.txt").write_text("not secret\n")
    profile, _ = containment.prepare_containment(workspace)

    for target in (state_dir / "env", state_dir / "env.bak-test", state_dir / "sub" / "file"):
        result = subprocess.run(
            ["sandbox-exec", "-f", str(profile), "/bin/cat", str(target)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, f"{target} was readable: {result.stdout!r}"
        assert "Operation not permitted" in result.stderr

    inside = subprocess.run(
        ["sandbox-exec", "-f", str(profile), "/bin/cat", str(workspace / "readable.txt")],
        capture_output=True, text=True,
    )
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout == "not secret\n"


# ---------------------------------------------------------------------------
# AC-3: both profiles render CREDENTIAL_DIRS, KUBECONFIG's targets, their
# symlinked escapes, the port-22/loopback network rules, and the mach/job
# rules from one helper (dev.findings 019909c0, 75b50b58, dd648709).
# ---------------------------------------------------------------------------


def _render_profile_pair(tmp_path: Path) -> tuple[str, str]:
    """Worker profile text and verification profile text, rendered through
    the real production entry points, for throwaway targets under
    tmp_path -- AC-3's read-deny/network/mach assertions apply identically
    to both, since both render from the same containment.py helpers."""
    ws = tmp_path / "worker-ws"
    ws.mkdir(exist_ok=True)
    cwd = tmp_path / "verify-cwd"
    cwd.mkdir(exist_ok=True)
    return containment.render_profile(ws), containment.render_verification_profile(cwd)


def _network_rule_entries(text: str) -> list[str]:
    return re.findall(r'\((?:allow|deny) network-outbound[^\n]*\)', text)


def _expected_state_and_credential_entries(home: Path) -> set[tuple[str, str]]:
    state_dir = str((home / ".factory-dispatcher").resolve())
    cred_dirs = {str((home / rel).resolve()) for rel in containment.CREDENTIAL_DIRS}
    return {("subpath", state_dir)} | {("subpath", d) for d in cred_dirs}


def test_credential_dirs_exact_read_deny_set_and_ordered_network_rules(
    tmp_path, monkeypatch
):
    """AC-3(a): with KUBECONFIG removed and no symlinks, the file-read deny
    set over EITHER profile is exactly the state directory plus
    CREDENTIAL_DIRS -- nothing more, nothing less -- and the network rules
    are exactly the three AC-3(iv)/(v) lines, in order, with the allowed
    port read from launchd_agent.TUNNEL_DEFAULTS rather than hand-listed."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)

    import launchd_agent

    substrate_port = launchd_agent.TUNNEL_DEFAULTS["substrate-prod"]["local_port"]
    temporal_port = launchd_agent.TUNNEL_DEFAULTS["temporal"]["local_port"]
    expected_entries = _expected_state_and_credential_entries(tmp_path / "home")

    for text in _render_profile_pair(tmp_path):
        assert set(_read_deny_entries(text)) == expected_entries
        network = _network_rule_entries(text)
        assert network == [
            '(deny network-outbound (remote tcp "*:22"))',
            '(deny network-outbound (remote tcp "localhost:*"))',
            f'(allow network-outbound (remote tcp "localhost:{substrate_port}"))',
        ]
        assert str(temporal_port) not in "".join(network)


def test_kubeconfig_entry_outside_credential_dirs_gets_a_literal_deny(
    tmp_path, monkeypatch
):
    """AC-3(b): KUBECONFIG = <tmp>/.kube/a + pathsep + <tmp>/elsewhere/b (b
    exists) -- the exact set plus exactly one literal deny for b resolved,
    and NOTHING for a, since a already falls under the .kube subpath deny."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    b = tmp_path / "elsewhere" / "b"
    b.parent.mkdir(parents=True)
    b.write_text("kubeconfig-b\n")
    a = tmp_path / "home" / ".kube" / "a"
    monkeypatch.setenv("KUBECONFIG", f"{a}{os.pathsep}{b}")

    expected = _expected_state_and_credential_entries(tmp_path / "home")
    expected.add(("literal", str(b.resolve())))

    for text in _render_profile_pair(tmp_path):
        assert set(_read_deny_entries(text)) == expected


def test_kubeconfig_symlink_target_gets_a_literal_deny(tmp_path, monkeypatch):
    """AC-3(c): <tmp>/.kube/config is a symlink to <tmp>/outside/real (a
    file) -- the exact set plus a literal deny for the resolved target."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    kube_dir = tmp_path / "home" / ".kube"
    kube_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real"
    real.write_text("real kubeconfig\n")
    (kube_dir / "config").symlink_to(real)

    expected = _expected_state_and_credential_entries(tmp_path / "home")
    expected.add(("literal", str(real.resolve())))

    for text in _render_profile_pair(tmp_path):
        assert set(_read_deny_entries(text)) == expected


def test_symlinked_directory_inside_credential_dir_gets_a_subpath_deny(
    tmp_path, monkeypatch
):
    """AC-3(d): <tmp>/.ssh/keys is a symlink to a directory outside -- the
    exact set plus a subpath deny for it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True)
    outside_keys = tmp_path / "outside-keys"
    outside_keys.mkdir()
    (ssh_dir / "keys").symlink_to(outside_keys)

    expected = _expected_state_and_credential_entries(tmp_path / "home")
    expected.add(("subpath", str(outside_keys.resolve())))

    for text in _render_profile_pair(tmp_path):
        assert set(_read_deny_entries(text)) == expected


def test_state_dir_denies_still_present_alongside_credential_dirs(tmp_path, monkeypatch):
    """AC-3(e): the state-directory denies OPS-122/SEC-6c19f60f assert are
    still present with the same paths, override included."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    override = tmp_path / "elsewhere" / "state"
    override.mkdir(parents=True)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(override))
    monkeypatch.delenv("KUBECONFIG", raising=False)

    home_dir = str((tmp_path / "home" / ".factory-dispatcher").resolve())
    override_dir = str(override.resolve())

    for text in _render_profile_pair(tmp_path):
        entries = set(_read_deny_entries(text))
        assert {("subpath", home_dir), ("subpath", override_dir)} <= entries


def test_prepare_containment_refuses_a_workspace_under_a_credential_dir(
    tmp_path, monkeypatch
):
    """AC-3(f): a ValueError for a workspace under <tmp>/.kube."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    kube_dir = tmp_path / "home" / ".kube"
    workspace = kube_dir / "some" / "path"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match=re.escape(str(kube_dir.resolve()))):
        containment.prepare_containment(workspace)


def test_prepare_verification_containment_refuses_a_cwd_under_a_credential_dir(
    tmp_path, monkeypatch
):
    """AC-3(f): a ValueError for a verification cwd under <tmp>/.ssh."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    ssh_dir = tmp_path / "home" / ".ssh"
    cwd = ssh_dir / "some" / "path"
    cwd.mkdir(parents=True)

    with pytest.raises(ValueError, match=re.escape(str(ssh_dir.resolve()))):
        containment.prepare_verification_containment(cwd)


# --- AC-3(vi): mach-lookup/job-creation rules, measured on this host ---


def test_mach_deny_lists_exactly_the_measured_services(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)

    for text in _render_profile_pair(tmp_path):
        mach_lines = re.findall(r"\(deny mach-lookup[^\n]*\)", text)
        assert len(mach_lines) == 1
        for name in containment.MACH_DENY:
            assert f'(global-name "{name}")' in mach_lines[0]
        assert text.count("(deny job-creation)") == 1
        assert "(allow job-creation)" not in text


def test_mach_deny_exact_service_names_are_pinned():
    """Review item 5: pins MACH_DENY's exact measured names directly,
    independent of a loop over the tuple itself -- a loop over
    containment.MACH_DENY (as test_mach_deny_lists_exactly_the_measured_services
    does) stays green even if an entry is quietly dropped or renamed from the
    module, because the test would then just assert over the smaller set.
    This hardcodes the three names the module docstring's bootstrap_look_up
    measurement records, so removing or renaming one goes red."""
    assert containment.MACH_DENY == (
        "com.apple.pasteboard.1",
        "com.apple.coreservices.appleevents",
        "com.apple.coreservices.launchservicesd",
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="bootstrap_look_up is macOS-only")
def test_mach_deny_services_are_actually_reachable_unsandboxed_on_this_host():
    """Cites the measurement MACH_DENY's docstring records: each service in
    MACH_DENY must answer 0 (reachable) via bootstrap_look_up when looked up
    unsandboxed, or denying it would not be a measured deny.

    Guarded by _sandbox_probes_available() even though this test never calls
    sandbox-exec itself: when THIS PROCESS is itself the dev.task worker (or
    declared verification), it is already running wrapped in the very
    profile under test, whose (deny mach-lookup ...) rule denies these same
    lookups to itself -- bootstrap_look_up would then answer 1100
    (sandbox-denied), not 0, failing a test that is supposed to measure the
    unsandboxed baseline. _sandbox_probes_available()'s own /tmp-write check
    detects that same nested-sandbox context (a release-gate finding on PR
    1095: this test failed with no guard, 1 failed / 2416 passed, run inside
    the profiles this PR ships)."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    import ctypes

    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    bootstrap_port = ctypes.c_uint32.in_dll(libsystem, "bootstrap_port")

    def look_up(name: str) -> int:
        port = ctypes.c_uint32(0)
        return libsystem.bootstrap_look_up(bootstrap_port, name.encode(), ctypes.byref(port))

    for name in containment.MACH_DENY:
        assert look_up(name) == 0, f"{name} did not answer reachable (0) on this host"


# --- AC-3 darwin probes: the OS actually enforces the new deny lines ---


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_both_profiles_deny_reading_every_credential_path_and_kubeconfig_target(
    tmp_path, monkeypatch
):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    home = tmp_path  # the suite's own conftest already sets HOME = tmp_path
    (home / ".kube" / "sub").mkdir(parents=True, exist_ok=True)
    (home / ".kube" / "config").write_text("kube-config\n")
    (home / ".kube" / "sub" / "file").write_text("nested\n")
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_fixture").write_text("private-key\n")
    (home / ".config" / "gh").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("gh-token\n")
    (home / ".config" / "gcloud").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "gcloud" / "f").write_text("gcloud-cred\n")
    (home / ".config" / "sops").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "sops" / "f").write_text("sops-key\n")

    outside = home / "outside"
    outside.mkdir()
    kc = outside / "kc"
    kc.write_text("kubeconfig-outside\n")
    monkeypatch.setenv("KUBECONFIG", str(kc))

    outside2 = home / "outside2"
    outside2.mkdir()
    real = outside2 / "real"
    real.write_text("linked-kube-config\n")
    (home / ".kube" / "link").symlink_to(real)

    denied_paths = [
        home / ".kube" / "config",
        home / ".kube" / "sub" / "file",
        home / ".ssh" / "id_fixture",
        home / ".config" / "gh" / "hosts.yml",
        home / ".config" / "gcloud" / "f",
        home / ".config" / "sops" / "f",
        kc,
        real,
    ]

    workspace = tmp_path / "run" / "repo"
    workspace.mkdir(parents=True)
    (workspace / "readable.txt").write_text("not secret\n")
    worker_profile, _ = containment.prepare_containment(workspace)

    verify_cwd = tmp_path / "verify" / "repo"
    verify_cwd.mkdir(parents=True)
    (verify_cwd / "readable.txt").write_text("not secret\n")
    verify_profile, _ = containment.prepare_verification_containment(verify_cwd)

    for profile, inside_file in (
        (worker_profile, workspace / "readable.txt"),
        (verify_profile, verify_cwd / "readable.txt"),
    ):
        for target in denied_paths:
            result = subprocess.run(
                ["sandbox-exec", "-f", str(profile), "/bin/cat", str(target)],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, (
                f"{target} was readable under {profile}: {result.stdout!r}"
            )
            assert "Operation not permitted" in result.stderr

        allowed = subprocess.run(
            ["sandbox-exec", "-f", str(profile), "/bin/cat", str(inside_file)],
            capture_output=True, text=True,
        )
        assert allowed.returncode == 0, allowed.stderr
        assert allowed.stdout == "not secret\n"


_NETWORK_PROBE_SCRIPT = """\
import socket


def attempt(host, port):
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect((host, port))
        return "connected"
    except PermissionError:
        return "eperm"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError as exc:
        return f"other:{{exc.errno}}"
    finally:
        s.close()


print("leg1", attempt("127.0.0.1", 22))
print("leg2", attempt("192.0.2.1", 22))
print("leg3", attempt("192.0.2.1", 2222))
print("leg4a", attempt("127.0.0.1", {port_p}))
print("leg4b", attempt("::1", {port_p}))
print("leg5a", attempt("127.0.0.1", {port_q}))
print("leg5b", attempt("::1", {port_q}))
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_network_rules_scope_to_port_22_and_every_loopback_port_but_the_tunnel(
    tmp_path, monkeypatch
):
    """AC-3(vii)/darwin probe: the five loopback/port-22 legs, using only
    listeners this test opens itself -- never the real Temporal/substrate
    ports or any other real service. Probed under BOTH the worker profile
    and the declared-verification profile, since both render their network
    rules from the same containment._network_rule_lines helper."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    import launchd_agent

    def _free_port() -> int:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def _open_listener_pair(port: int) -> list[socket.socket]:
        # Fresh sockets per profile pass -- see the module-level note above
        # the probe script. A connect() that lands in the accept backlog
        # without an accept() stays there for the socket's lifetime; reusing
        # one long-lived pair of listen(1) sockets across both passes let
        # the worker pass's unaccepted connection occupy the only backlog
        # slot, so the verification pass's connect() to the same port had
        # nowhere to land and timed out instead of succeeding (release gate
        # on PR 1102: "fails EVERY time unsandboxed on darwin"). Opening new
        # listeners immediately before each pass and closing them
        # immediately after removes the shared state instead of just
        # widening the backlog around it.
        opened = []
        s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s4.bind(("127.0.0.1", port))
        s4.listen(5)
        opened.append(s4)
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s6.bind(("::1", port))
        s6.listen(5)
        opened.append(s6)
        return opened

    port_p = _free_port()
    port_q = _free_port()

    monkeypatch.setitem(
        launchd_agent.TUNNEL_DEFAULTS["substrate-prod"], "local_port", port_p
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker_profile, _ = containment.prepare_containment(workspace)

    verify_cwd = tmp_path / "verify-cwd"
    verify_cwd.mkdir()
    verify_profile, _ = containment.prepare_verification_containment(verify_cwd)

    script = tmp_path / "net_probe.py"
    script.write_text(
        _NETWORK_PROBE_SCRIPT.format(port_p=port_p, port_q=port_q)
    )

    # Both profiles render the same network rules from the same helper
    # (containment._network_rule_lines) -- the spec requires this probe
    # to prove that for both, not just the worker profile (release gate
    # on PR 1095: this test "must also render and probe BOTH profiles,
    # as the spec says, not only the worker profile").
    for name, profile in (("worker", worker_profile), ("verification", verify_profile)):
        sockets = _open_listener_pair(port_p) + _open_listener_pair(port_q)
        try:
            result = subprocess.run(
                ["sandbox-exec", "-f", str(profile), sys.executable, str(script)],
                capture_output=True, text=True, timeout=15,
            )
        finally:
            for s in sockets:
                s.close()
        legs = dict(
            line.split(" ", 1) for line in result.stdout.strip().splitlines()
        )
        context = f"[{name}] " + result.stdout + result.stderr
        assert legs.get("leg1") == "eperm", context
        assert legs.get("leg2") == "eperm", context
        assert legs.get("leg3") != "eperm", context
        assert legs.get("leg4a") == "connected", context
        assert legs.get("leg4b") == "connected", context
        assert legs.get("leg5a") == "eperm", context
        assert legs.get("leg5b") == "eperm", context


@pytest.mark.skipif(sys.platform != "darwin", reason="launchctl/sandbox-exec are macOS-only")
def test_job_creation_is_denied_or_the_result_is_reported(tmp_path, monkeypatch):
    """AC-3(vi): whether `(deny job-creation)` actually stops `launchctl
    submit` from a sandboxed process on this macOS release is UNTESTED by
    design -- this reports the result under both the worker and
    verification profiles, PLUS a control run under a bare `(allow
    default)` profile carrying no job-creation deny at all. Without the
    control leg, a submit failure under the two real profiles cannot be
    attributed to `(deny job-creation)` specifically -- sandbox-exec is
    independently known to interfere with launchd submission even under
    `(allow default)` (release gate on PR 1095, item 3). Removes any job it
    submitted in a finally block so a submit that succeeds leaves nothing
    behind in the user's launchd domain."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker_profile, _ = containment.prepare_containment(workspace)

    verify_cwd = tmp_path / "verify-cwd"
    verify_cwd.mkdir()
    verify_profile, _ = containment.prepare_verification_containment(verify_cwd)

    bare_profile = tmp_path / "bare-allow-default.sb"
    bare_profile.write_text("(version 1)\n(allow default)\n")

    labels: list[str] = []
    try:
        for name, profile in (
            ("worker", worker_profile),
            ("verification", verify_profile),
            ("control (bare allow-default, no job-creation deny)", bare_profile),
        ):
            label = f"containment-probe-{os.getpid()}-{re.sub(r'[^a-zA-Z0-9]+', '-', name)}"
            labels.append(label)
            result = subprocess.run(
                [
                    "sandbox-exec", "-f", str(profile),
                    "launchctl", "submit", "-l", label, "--", "/usr/bin/true",
                ],
                capture_output=True, text=True,
            )
            print(f"job-creation probe ({name}): rc={result.returncode} stderr={result.stderr!r}")
    finally:
        for label in labels:
            subprocess.run(
                ["launchctl", "remove", label], capture_output=True, text=True
            )


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_keep_condition_legs_succeed_under_both_profiles(tmp_path, monkeypatch):
    """AC-3(vi)'s keep-condition: `/usr/bin/git --version`, `python3 -I -c
    'import ssl'` and a getaddrinfo resolution must all still succeed under
    both the worker and verification profiles, or the mach-lookup/
    job-creation rules would have to be dropped. The `launchctl submit`
    control leg is covered by test_job_creation_is_denied_or_the_result_is_reported;
    the authenticated `claude -p` leg cannot run here at all -- nested
    sandboxes are refused (_sandbox_probes_available), so it is run
    unsandboxed by the gate and, after merge, by the outer loop's attended
    canary. This test does not claim either of those two legs."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox probes unavailable in this execution environment: {reason}")

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    worker_profile, _ = containment.prepare_containment(workspace)

    verify_cwd = tmp_path / "verify-cwd"
    verify_cwd.mkdir()
    verify_profile, _ = containment.prepare_verification_containment(verify_cwd)

    getaddrinfo_script = tmp_path / "getaddrinfo_probe.py"
    getaddrinfo_script.write_text(
        "import socket\n"
        "socket.getaddrinfo('example.com', 443)\n"
        "print('resolved')\n"
    )

    for name, profile in (("worker", worker_profile), ("verification", verify_profile)):
        git_result = subprocess.run(
            ["sandbox-exec", "-f", str(profile), "/usr/bin/git", "--version"],
            capture_output=True, text=True,
        )
        assert git_result.returncode == 0, f"[{name}] git --version: {git_result.stderr}"

        ssl_result = subprocess.run(
            ["sandbox-exec", "-f", str(profile), sys.executable, "-I", "-c", "import ssl"],
            capture_output=True, text=True,
        )
        assert ssl_result.returncode == 0, f"[{name}] import ssl: {ssl_result.stderr}"

        dns_result = subprocess.run(
            ["sandbox-exec", "-f", str(profile), sys.executable, str(getaddrinfo_script)],
            capture_output=True, text=True, timeout=15,
        )
        assert dns_result.returncode == 0, f"[{name}] getaddrinfo: {dns_result.stderr}"
        assert "resolved" in dns_result.stdout, f"[{name}] getaddrinfo: {dns_result.stdout!r}"
