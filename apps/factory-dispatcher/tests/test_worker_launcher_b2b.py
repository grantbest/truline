"""PARTB-B2b: the launcher's remaining subcommands -- seed, vseed, materialize, export,
venv, pip, purge and probe. Same idiom as test_worker_launcher.py (B2a): the launcher
runs directly as the test's own uid, never through sudo, never as a real
`_factoryworker`; `_sandbox_probes_available()` gates every test that actually runs
`sandbox-exec`, because this suite can itself run nested inside the dispatcher's own
deny-write sandbox. No test here executes sudo, the real reap, or claude.

Superseded review notes fb154028/6785d1a5/28bdab8a and this re-spec (PR 1175,
AC-1..AC-13) replace PR 1174's shape: seed/vseed separation is structural (distinct
`_cmd_seed`/`_cmd_vseed` wrappers over one `_seed_vseed_core`), export's patch and
design text are top-level envelope fields rather than nested inside `stdout`, every
git spawn is hardened (AC-11), and every payload path/ref is confined to W or one
fixed root before any spawn (AC-12).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handoff  # noqa: E402
import worker_launcher as wl  # noqa: E402

OAUTH = "oauth-token-fixture-aaaaaaaaaaaa"


def _sandbox_probes_available() -> tuple[bool, str]:
    """Copied from test_worker_launcher.py's (and test_containment.py's) idiom."""
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="worker-launcher-b2b-probe-", dir="/tmp")
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


def _permissive_profile(path: Path) -> Path:
    path.write_text("(version 1)\n(allow default)\n")
    return path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [wl.GIT_PATH, "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "a")


class _FakeRunResult:
    """For `subprocess.run` fakes. `_probe_run` calls PROBE_RUNNER with text=True, so
    its real stdout/stderr are always str; seed/vseed/export call plain subprocess.run
    (no text=True) and only inspect .returncode/.stderr on failure, so bytes there is
    fine too -- this default (str) matches the more restrictive probe caller."""

    def __init__(self, returncode: int = 0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _seed_payload(tmp_path: Path, bundle: Path, profile: Path, **extra) -> dict:
    payload = {
        "bundle": str(bundle), "base_ref": "main", "tree": "wtree",
        "remote_url": "https://example.invalid/repo.git",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
    }
    payload.update(extra)
    return payload


def _touch_bundle(tmp_path: Path, name: str = "x.bundle") -> Path:
    bundle = tmp_path / name
    bundle.write_bytes(b"")
    return bundle


# ---------------------------------------------------------------------------
# AC-1: seed and vseed from a bundle, and their structural separation.
# ---------------------------------------------------------------------------


def test_seed_refuses_when_repo_already_exists(tmp_path):
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    envelope = wl._cmd_seed(_seed_payload(tmp_path, bundle, profile), tmp_path)
    assert envelope["status"] == "refused"


def test_seed_refuses_unknown_tree(tmp_path):
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, tree="not-a-tree")
    envelope = wl._cmd_seed(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_seed_refuses_a_payload_that_carries_a_patch(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("seed spawned a process despite carrying a patch")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, patch=str(tmp_path / "vtree" / "handoff.patch"))

    envelope = wl._cmd_seed(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_main_seed_refuses_a_payload_that_carries_a_patch(tmp_path):
    """Same AC-1 refusal, driven through `main()` (argv + stdin/stdout), matching the
    AC's own `main(['seed', W])` phrasing."""
    import io

    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, patch=str(tmp_path / "vtree" / "handoff.patch"))

    stdin = io.BytesIO(json.dumps(payload).encode("utf-8"))
    stdout = io.StringIO()
    rc = wl.main(["seed", str(tmp_path)], stdin, stdout)

    assert rc == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["status"] == "refused"


def test_vseed_of_vtree_refuses_with_no_patch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or _FakeRunResult())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    profile = _permissive_profile(tmp_path / "verify.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, tree="vtree")

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert calls == []


def test_vseed_of_ptree_refuses_with_a_patch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or _FakeRunResult())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    profile = _permissive_profile(tmp_path / "ptree.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(
        tmp_path, bundle, profile, tree="ptree", patch=str(tmp_path / "vtree" / "handoff.patch")
    )

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert calls == []


def test_vseed_of_wtree_refuses(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)) or _FakeRunResult())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    profile = _permissive_profile(tmp_path / "wtree.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, tree="wtree")

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert calls == []


def test_vseed_of_ptree_with_no_patch_proceeds_to_the_clone(monkeypatch, tmp_path):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "ptree.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, tree="ptree")

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    # check-ref-format, clone, checkout, remote set-url, fetch -- no apply step.
    assert len(captured) == 5
    assert captured[0] == [wl.GIT_PATH, "check-ref-format", "--branch", "main"]


def test_seed_composes_sandboxed_git_argv_without_executing(monkeypatch, tmp_path):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    repo = tmp_path / "wtree" / "repo"
    payload = _seed_payload(tmp_path, bundle, profile)

    envelope = wl._cmd_seed(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert len(captured) == 5
    assert captured[0] == [wl.GIT_PATH, "check-ref-format", "--branch", "main"]
    assert captured[1] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile),
        wl.GIT_PATH, "clone", "--quiet", "--no-checkout", "--", str(bundle), str(repo),
    ]
    assert captured[2] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), wl.GIT_PATH, "-C", str(repo), "checkout",
        "--quiet", "main", "--",
    ]
    assert captured[3] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), wl.GIT_PATH, "-C", str(repo), "remote",
        "set-url", "origin", "--", "https://example.invalid/repo.git",
    ]
    assert captured[4] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), wl.GIT_PATH, "-C", str(repo), "fetch",
        "--quiet", "--end-of-options", str(bundle),
        "refs/remotes/origin/main:refs/remotes/origin/main",
    ]


def test_vseed_composes_an_additional_apply_step(monkeypatch, tmp_path):
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "verify.sb")
    bundle = _touch_bundle(tmp_path)
    repo = tmp_path / "vtree" / "repo"
    patch = tmp_path / "vtree" / "handoff.patch"
    patch.parent.mkdir(parents=True)
    patch.write_bytes(b"")
    payload = _seed_payload(tmp_path, bundle, profile, tree="vtree", patch=str(patch))

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert len(captured) == 6
    assert captured[5] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), wl.GIT_PATH, "-C", str(repo), "apply",
        "--index", "--binary", "--", str(patch),
    ]


def test_vseed_refuses_when_patch_apply_fails(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        if "apply" in argv:
            return _FakeRunResult(returncode=1, stdout=b"", stderr=b"patch does not apply")
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "verify.sb")
    bundle = _touch_bundle(tmp_path)
    patch = tmp_path / "vtree" / "handoff.patch"
    patch.parent.mkdir(parents=True)
    patch.write_bytes(b"")
    payload = _seed_payload(tmp_path, bundle, profile, tree="vtree", patch=str(patch))

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert "patch does not apply" in envelope["stderr"]


def test_vseed_apply_failure_stderr_is_fit_to_the_exec_envelope_cap(monkeypatch, tmp_path):
    """Review item 4 (PR 1178 gate): the gate measured a 6,300,274-byte vseed envelope
    against EXEC_ENVELOPE_CAP_BYTES of 2,101,248 -- git's own stderr on a failing apply
    was never fitted. 6 MiB of apply stderr must still produce a serialized envelope
    within EXEC_ENVELOPE_CAP_BYTES.

    Item 2(a) (PR 1180 gate): the fake stderr ends with a known marker line; the
    fitted envelope's stderr must still carry that marker (tail-kept, not
    head-kept) and the "seed git command" prefix. Mutation: changing
    _cap_stream's raw[-cap:] to raw[:cap] turns this red."""
    tail_marker = b"\nLAST-LINE\n"
    big_stderr = b"e" * (6 * 1024 * 1024 - len(tail_marker)) + tail_marker

    def fake_run(argv, **kwargs):
        if "apply" in argv:
            return _FakeRunResult(returncode=1, stdout=b"", stderr=big_stderr)
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "verify.sb")
    bundle = _touch_bundle(tmp_path)
    patch = tmp_path / "vtree" / "handoff.patch"
    patch.parent.mkdir(parents=True)
    patch.write_bytes(b"")
    payload = _seed_payload(tmp_path, bundle, profile, tree="vtree", patch=str(patch))

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    serialized = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= wl.EXEC_ENVELOPE_CAP_BYTES
    assert envelope["stderr"].startswith("seed git command")
    assert envelope["stderr"].endswith("\nLAST-LINE\n")


def _make_bundle_repo(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    _init_repo(src)
    (src / "file.txt").write_text("hello\n")
    _git(src, "add", "file.txt")
    _git(src, "commit", "-q", "-m", "init")
    _git(src, "update-ref", "refs/remotes/origin/main", "main")
    bundle = tmp_path / "repo.bundle"
    _git(src, "bundle", "create", str(bundle), "HEAD", "refs/heads/main", "refs/remotes/origin/main")
    head_sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    return bundle, head_sha


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_seed_clones_bundle_checks_out_base_ref_and_sets_remote(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")
    bundle, head_sha = _make_bundle_repo(tmp_path)
    w_path = tmp_path / "W"
    w_path.mkdir()
    bundle_in_w = w_path / "repo.bundle"
    shutil.copy(bundle, bundle_in_w)
    profile = _permissive_profile(w_path / "worker.sb")
    payload = _seed_payload(w_path, bundle_in_w, profile, remote_url="https://example.invalid/real-repo.git")

    envelope = wl._cmd_seed(payload, w_path)

    assert envelope["status"] == "ok", envelope
    repo = w_path / "wtree" / "repo"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_sha
    assert (repo / "file.txt").read_text() == "hello\n"
    assert _git(repo, "remote", "get-url", "origin").stdout.strip() == (
        "https://example.invalid/real-repo.git"
    )
    assert _git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip() == head_sha


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_vseed_applies_patch_after_seeding(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")
    bundle, _head_sha = _make_bundle_repo(tmp_path)

    patch_src = tmp_path / "patchsrc"
    subprocess.run([wl.GIT_PATH, "clone", "-q", str(bundle), str(patch_src)], check=True)
    (patch_src / "file.txt").write_text("hello\nchanged\n")
    _git(patch_src, "add", "-A")
    patch_bytes = subprocess.run(
        [wl.GIT_PATH, "-C", str(patch_src), "diff", "--cached", "--binary"],
        capture_output=True, check=True,
    ).stdout

    w_path = tmp_path / "W"
    (w_path / "vtree").mkdir(parents=True)
    bundle_in_w = w_path / "repo.bundle"
    shutil.copy(bundle, bundle_in_w)
    patch_path = w_path / "vtree" / "handoff.patch"
    patch_path.write_bytes(patch_bytes)
    profile = _permissive_profile(w_path / "verify.sb")
    payload = _seed_payload(
        w_path, bundle_in_w, profile, tree="vtree",
        remote_url="https://example.invalid/real-repo.git", patch=str(patch_path),
    )

    envelope = wl._cmd_vseed(payload, w_path)

    assert envelope["status"] == "ok", envelope
    repo = w_path / "vtree" / "repo"
    assert (repo / "file.txt").read_text() == "hello\nchanged\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_vseed_refuses_when_real_patch_apply_fails(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")
    bundle, _head_sha = _make_bundle_repo(tmp_path)
    w_path = tmp_path / "W"
    (w_path / "vtree").mkdir(parents=True)
    bundle_in_w = w_path / "repo.bundle"
    shutil.copy(bundle, bundle_in_w)
    bad_patch = w_path / "vtree" / "handoff.patch"
    bad_patch.write_text("this is not a valid patch\n")
    profile = _permissive_profile(w_path / "verify.sb")
    payload = _seed_payload(
        w_path, bundle_in_w, profile, tree="vtree",
        remote_url="https://example.invalid/real-repo.git", patch=str(bad_patch),
    )

    envelope = wl._cmd_vseed(payload, w_path)

    assert envelope["status"] == "refused"


# ---------------------------------------------------------------------------
# AC-11: every launcher git call is hardened.
# ---------------------------------------------------------------------------


def test_git_config_count_pins_equal_dispatchs_own():
    # dispatch.py:823-829 (git_in_clone) pins exactly these four, restated here
    # (not imported -- Invariant L's stdlib-plus-process_env-only rule).
    dispatch_pins = {
        "core.hooksPath": "/dev/null", "core.fsmonitor": "false",
        "gc.auto": "0", "maintenance.auto": "false",
    }
    assert dict(wl.GIT_CONFIG_COUNT_PINS) == dispatch_pins


def test_git_env_drops_every_payload_git_star_key():
    env = wl._git_env({
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil'",
        "GIT_DIR": "/tmp/evil.git",
        "GIT_EXEC_PATH": "/tmp/evil-bin",
        "GIT_SSH_COMMAND": "/tmp/evil-ssh",
        "SAFE": "1",
    })
    assert not any(name.startswith("GIT_") and name not in (
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT",
        *(f"GIT_CONFIG_KEY_{i}" for i in range(4)), *(f"GIT_CONFIG_VALUE_{i}" for i in range(4)),
    ) for name in env)
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["SAFE"] == "1"


def test_seed_env_carries_no_payload_git_star_key(monkeypatch, tmp_path):
    captured_envs = []

    def fake_run(argv, env=None, **kwargs):
        captured_envs.append(dict(env or {}))
        return _FakeRunResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(
        tmp_path, bundle, profile,
        env={"GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil'"},
    )

    envelope = wl._cmd_seed(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    for env in captured_envs:
        assert "GIT_CONFIG_PARAMETERS" not in env


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_seed_never_honours_a_payload_git_config_parameters_hooks_path(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")
    bundle, _head_sha = _make_bundle_repo(tmp_path)
    w_path = tmp_path / "W"
    w_path.mkdir()
    bundle_in_w = w_path / "repo.bundle"
    shutil.copy(bundle, bundle_in_w)
    profile = _permissive_profile(w_path / "worker.sb")

    hooks_dir = tmp_path / "evil-hooks"
    hooks_dir.mkdir()
    marker = tmp_path / "hook-ran.marker"
    hook = hooks_dir / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)

    payload = _seed_payload(
        w_path, bundle_in_w, profile,
        remote_url="https://example.invalid/real-repo.git",
        env={"GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks_dir}'"},
    )

    envelope = wl._cmd_seed(payload, w_path)

    assert envelope["status"] == "ok", envelope
    assert not marker.exists(), "a payload GIT_CONFIG_PARAMETERS hooksPath fired a hook"


def test_seed_never_honours_a_payload_git_config_parameters_hooks_path_on_every_platform(
    monkeypatch, tmp_path
):
    """Review item 6 (PR 1178 gate): the only existing hook-marker test is darwin-only
    (it runs through sandbox-exec), so Linux CI never exercised AC-11's env hardening.
    This drops the sandbox-exec wrapper (bare git only) so it runs everywhere.
    Mutation: _git_env keeping GIT_CONFIG_PARAMETERS turns this red in Linux CI."""
    monkeypatch.setattr(wl, "_sandboxed_argv", lambda payload, argv: list(argv))
    bundle, _head_sha = _make_bundle_repo(tmp_path)
    w_path = tmp_path / "W"
    w_path.mkdir()
    bundle_in_w = w_path / "repo.bundle"
    shutil.copy(bundle, bundle_in_w)
    profile = _permissive_profile(w_path / "worker.sb")  # unused: sandboxing is bypassed

    hooks_dir = tmp_path / "evil-hooks-cross-platform"
    hooks_dir.mkdir()
    marker = tmp_path / "hook-ran-cross-platform.marker"
    hook = hooks_dir / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)

    payload = _seed_payload(
        w_path, bundle_in_w, profile,
        remote_url="https://example.invalid/real-repo.git",
        env={"GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks_dir}'"},
    )

    envelope = wl._cmd_seed(payload, w_path)

    assert envelope["status"] == "ok", envelope
    assert not marker.exists(), "a payload GIT_CONFIG_PARAMETERS hooksPath fired a hook"


# ---------------------------------------------------------------------------
# AC-12: payload paths and refs are confined.
# ---------------------------------------------------------------------------


def _no_spawn_recorder(monkeypatch) -> list:
    """Review item 5 (PR 1178 gate): every AC-12 confinement test must prove the
    refusal happened before any spawn, not merely that the final status came out
    'refused' for some other, unrelated reason (e.g. real git failing on an empty
    bundle) -- which is what let a removed confinement check go unnoticed."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    return calls


def test_seed_refuses_a_bundle_outside_w(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    outside = tmp_path.parent / "outside-bundle.bundle"
    outside.write_bytes(b"")
    try:
        profile = _permissive_profile(tmp_path / "worker.sb")
        payload = _seed_payload(tmp_path, outside, profile)
        envelope = wl._cmd_seed(payload, tmp_path)
        assert envelope["status"] == "refused"
        assert calls == []
    finally:
        outside.unlink(missing_ok=True)


def test_seed_refuses_a_bundle_that_is_a_symlink_out_of_w(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    outside = tmp_path.parent / "outside-bundle-2.bundle"
    outside.write_bytes(b"")
    try:
        link = tmp_path / "link.bundle"
        link.symlink_to(outside)
        profile = _permissive_profile(tmp_path / "worker.sb")
        payload = _seed_payload(tmp_path, link, profile)
        envelope = wl._cmd_seed(payload, tmp_path)
        assert envelope["status"] == "refused"
        assert calls == []
    finally:
        outside.unlink(missing_ok=True)


def test_vseed_refuses_a_patch_path_other_than_the_fixed_one(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    profile = _permissive_profile(tmp_path / "verify.sb")
    bundle = _touch_bundle(tmp_path)
    (tmp_path / "vtree").mkdir(parents=True)
    other_patch = tmp_path / "vtree" / "not-handoff.patch"
    other_patch.write_bytes(b"")
    payload = _seed_payload(tmp_path, bundle, profile, tree="vtree", patch=str(other_patch))

    envelope = wl._cmd_vseed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert calls == []


def test_vseed_refuses_a_patch_path_that_is_a_symlink_out_of_w(monkeypatch, tmp_path):
    """Review item 5 (PR 1178 gate): the patch-symlink case. The payload's `patch`
    resolves (through a symlink) outside W, at a path other than the one fixed
    location -- refused on the literal-path mismatch, before any spawn."""
    calls = _no_spawn_recorder(monkeypatch)
    outside = tmp_path.parent / "outside-patch.patch"
    outside.write_bytes(b"")
    try:
        profile = _permissive_profile(tmp_path / "verify.sb")
        bundle = _touch_bundle(tmp_path)
        (tmp_path / "vtree").mkdir(parents=True)
        link = tmp_path / "vtree" / "patch-link"
        link.symlink_to(outside)
        payload = _seed_payload(tmp_path, bundle, profile, tree="vtree", patch=str(link))

        envelope = wl._cmd_vseed(payload, tmp_path)

        assert envelope["status"] == "refused"
        assert calls == []
    finally:
        outside.unlink(missing_ok=True)


def test_seed_refuses_a_profile_outside_w_and_the_fixed_root(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    outside_profile = tmp_path.parent / "outside.sb"
    _permissive_profile(outside_profile)
    try:
        bundle = _touch_bundle(tmp_path)
        payload = _seed_payload(tmp_path, bundle, outside_profile)
        envelope = wl._cmd_seed(payload, tmp_path)
        assert envelope["status"] == "refused"
        assert calls == []
    finally:
        outside_profile.unlink(missing_ok=True)


def test_seed_refuses_a_profile_failing_check_profile(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, profile_owner_uid=os.getuid() + 1)
    envelope = wl._cmd_seed(payload, tmp_path)
    assert envelope["status"] == "refused"
    assert calls == []


def test_seed_refuses_a_tree_of_path_traversal(monkeypatch, tmp_path):
    calls = _no_spawn_recorder(monkeypatch)
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, tree="../x")
    envelope = wl._cmd_seed(payload, tmp_path)
    assert envelope["status"] == "refused"
    assert calls == []


@pytest.mark.parametrize("base_ref", ["--orphan=x", "-x"])
def test_seed_refuses_dash_leading_base_refs_before_any_spawn(monkeypatch, tmp_path, base_ref):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    payload = _seed_payload(tmp_path, bundle, profile, base_ref=base_ref)

    envelope = wl._cmd_seed(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert calls == []


def test_seed_refuses_a_base_ref_that_fails_check_ref_format(tmp_path):
    profile = _permissive_profile(tmp_path / "worker.sb")
    bundle = _touch_bundle(tmp_path)
    # "not..a..valid..ref" has no leading dash, so it reaches the real
    # `git check-ref-format --branch` call (not mocked here) and fails there.
    payload = _seed_payload(tmp_path, bundle, profile, base_ref="..bad..ref..")
    envelope = wl._cmd_seed(payload, tmp_path)
    assert envelope["status"] == "refused"


def _no_probe_runner_recorder(monkeypatch) -> list:
    """Review item 5's PROBE_RUNNER half: a probe confinement test must prove no probe
    item ever ran (never the real /bin/launchctl or /usr/bin/security), not merely
    that the top-level status came out 'refused'."""
    calls: list = []
    monkeypatch.setattr(wl, "PROBE_RUNNER", lambda *a, **k: calls.append(a) or _FakeRunResult())
    return calls


def test_probe_refuses_a_bootstrap_plist_outside_w_and_the_fixed_root(monkeypatch, tmp_path):
    calls = _no_probe_runner_recorder(monkeypatch)
    payload = {"paths": [], "mach_names": [], "bootstrap_plist": "/etc/not-allowed.plist"}
    envelope = wl._cmd_probe(payload, tmp_path)
    assert envelope["status"] == "refused"
    assert calls == []


def test_probe_refuses_a_bootstrap_plist_that_is_a_symlink_out_of_w(monkeypatch, tmp_path):
    """Review item 5 (PR 1178 gate): the plist-symlink case, mirroring the bundle- and
    patch-symlink cases above: the plist resolves, through a symlink inside W, to a
    target outside W and outside FIXED_TRUSTED_ROOT."""
    calls = _no_probe_runner_recorder(monkeypatch)
    outside = tmp_path.parent / "outside.plist"
    outside.write_text("<plist/>")
    try:
        link = tmp_path / "plist-link.plist"
        link.symlink_to(outside)
        payload = {"paths": [], "mach_names": [], "bootstrap_plist": str(link)}

        envelope = wl._cmd_probe(payload, tmp_path)

        assert envelope["status"] == "refused"
        assert calls == []
    finally:
        outside.unlink(missing_ok=True)


def test_probe_refuses_a_worker_profile_failing_check_profile(monkeypatch, tmp_path):
    calls = _no_probe_runner_recorder(monkeypatch)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "paths": [], "mach_names": [], "worker_profile": str(profile),
        "worker_profile_owner_uid": os.getuid() + 1,
    }
    envelope = wl._cmd_probe(payload, tmp_path)
    assert envelope["status"] == "refused"
    assert calls == []


# ---------------------------------------------------------------------------
# AC-2: export carries only a patch and data.
# ---------------------------------------------------------------------------


def test_max_patch_bytes_equals_handoffs():
    assert wl.MAX_PATCH_BYTES == handoff.MAX_PATCH_BYTES


def test_export_envelope_cap_fits_a_16mib_patch():
    patch_b64 = base64.b64encode(b"\xff" * wl.MAX_PATCH_BYTES).decode("ascii")
    envelope = {
        "status": "ok", "child_rc": 0, "stdout": "", "stderr": "",
        "stdout_truncated_result": False,
        "patch_b64": patch_b64, "design_text": "x" * wl.DESIGN_TEXT_CAP_BYTES,
    }
    serialized = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= wl.EXPORT_ENVELOPE_CAP_BYTES


def test_export_composes_the_required_git_config_and_flags(monkeypatch, tmp_path):
    captured = []

    def fake_run(argv, env=None, **kwargs):
        captured.append((list(argv), dict(env or {})))
        return _FakeRunResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert len(captured) == 3
    ref_check_argv, ref_check_env = captured[0]
    add_argv, add_env = captured[1]
    diff_argv, diff_env = captured[2]
    assert ref_check_argv == [wl.GIT_PATH, "check-ref-format", "--branch", "main"]
    assert "-c" in add_argv and "core.hooksPath=/dev/null" in add_argv
    assert "core.fsmonitor=false" in add_argv
    assert "core.attributesFile=/dev/null" in add_argv
    assert add_argv[-3:] == [
        ".", ":(exclude,icase,glob)**/.factory", ":(exclude,icase,glob)**/.factory/**",
    ]
    assert "--no-ext-diff" in diff_argv
    assert "--no-textconv" in diff_argv
    assert "--src-prefix=a/" in diff_argv
    assert "--dst-prefix=b/" in diff_argv
    assert "--end-of-options" in diff_argv
    end_idx = diff_argv.index("--end-of-options")
    assert diff_argv[end_idx + 1] == "main"
    assert diff_argv[-2:] == [
        ":(exclude,icase,glob)**/.factory", ":(exclude,icase,glob)**/.factory/**",
    ]
    assert add_env["GIT_ATTR_NOSYSTEM"] == "1"
    assert diff_env["GIT_ATTR_NOSYSTEM"] == "1"
    assert ref_check_env["GIT_ATTR_NOSYSTEM"] == "1"


def test_export_refuses_when_repo_is_absent(tmp_path):
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
    }
    envelope = wl._cmd_export(payload, tmp_path)
    assert envelope["status"] == "refused"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_export_patch_covers_worker_changes_and_excludes_factory_and_ignored(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "wtree" / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # a worker commit
    (repo / "tracked.txt").write_text("committed change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "worker change")
    # a staged-but-uncommitted change
    (repo / "staged.txt").write_text("staged content\n")
    _git(repo, "add", "staged.txt")
    # an unstaged binary file
    (repo / "binary.bin").write_bytes(b"\x00\x01\x02\xff")
    # a file under .factory other than design.md, committed; plus design.md itself
    (repo / ".factory").mkdir()
    (repo / ".factory" / "other.txt").write_text("must never travel")
    (repo / ".factory" / "design.md").write_text("# design note\n")
    _git(repo, "add", ".factory/other.txt")
    _git(repo, "commit", "-q", "-m", "factory file committed by the worker")
    # a gitignored file
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "add gitignore")
    (repo / "ignored.txt").write_text("must never travel either\n")

    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": base_sha,
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 30.0,
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["stdout"] == ""
    patch_text = base64.b64decode(envelope["patch_b64"]).decode("utf-8", errors="replace")

    assert "committed change" in patch_text
    assert "staged.txt" in patch_text
    assert "binary.bin" in patch_text
    assert ".factory" not in patch_text
    # Review item 1 (PR 1178 gate): the committed .gitignore line "+ignored.txt"
    # legitimately appears in the patch -- assert on the ignored file's own content
    # and its path header instead, so `git add -A -f` (which would make the ignored
    # file itself travel) turns this red.
    assert "must never travel either" not in patch_text
    assert "b/ignored.txt" not in patch_text
    assert envelope["design_text"] == "# design note\n"


def test_export_excludes_case_folded_factory_components_at_any_depth(monkeypatch, tmp_path):
    """Review items 2 and 7 (PR 1178 gate). Item 2: the old version of this test was
    darwin-only AND skipped itself on case-insensitive filesystems (APFS folds
    `.factory`/`.Factory` to the same directory), so it ran nowhere; rewritten to
    commit only `.Factory/y.txt` with no `.factory` directory present at that level,
    which sidesteps the collision instead of skipping around it, and to run on Linux
    and darwin by monkeypatching `_sandboxed_argv` to drop the sandbox-exec wrapper so
    the real (unsandboxed) git runs add/diff directly. Item 7: also covers a
    case-folded `.factory` nested below the repo root, in two distinct directories
    (again to avoid any case-insensitive-filesystem collision between the two).
    Mutation: removing `icase` from, or dropping the `**/` glob prefix on, either
    pathspec must turn this red.
    """
    monkeypatch.setattr(wl, "_sandboxed_argv", lambda payload, argv: list(argv))

    repo = tmp_path / "wtree" / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / ".Factory").mkdir()
    (repo / ".Factory" / "y.txt").write_text("top-level-upper-marker\n")
    (repo / "subA" / ".factory").mkdir(parents=True)
    (repo / "subA" / ".factory" / "z.txt").write_text("nested-lower-marker\n")
    (repo / "subB" / ".Factory").mkdir(parents=True)
    (repo / "subB" / ".Factory" / "w.txt").write_text("nested-upper-marker\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "factory variants at various depths")

    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": base_sha,
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 30.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    patch_text = base64.b64decode(envelope["patch_b64"]).decode("utf-8", errors="replace")
    assert "top-level-upper-marker" not in patch_text
    assert "nested-lower-marker" not in patch_text
    assert "nested-upper-marker" not in patch_text
    assert ".factory" not in patch_text
    assert ".Factory" not in patch_text


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_export_never_runs_an_external_diff_or_textconv_driver(tmp_path):
    """AC-9 mutation sensitivity: 'export without --no-ext-diff' and 'a hook executed
    during export'. A local (test-only, never worker-committed) repo config binds an
    external diff driver and a binary textconv filter to a marker-writing command;
    --no-ext-diff/--no-textconv (removed, this test goes red) must keep both unrun."""
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "wtree" / "repo"
    _init_repo(repo)
    marker = tmp_path / "driver-ran.marker"
    (repo / ".gitattributes").write_text("*.bin diff=markerdriver\n")
    _git(repo, "config", "diff.external", f"/bin/sh -c 'echo ran > {marker}'")
    _git(repo, "config", "diff.markerdriver.textconv", f"/bin/sh -c 'echo ran > {marker}'")
    (repo / "tracked.bin").write_bytes(b"\x00base")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tracked.bin").write_bytes(b"\x00changed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": base_sha,
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 30.0,
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert not marker.exists(), "export ran an external diff or textconv driver"


def test_export_refuses_when_design_md_is_a_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory").mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("do not leak")
    (repo / ".factory" / "design.md").symlink_to(outside)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert "do not leak" not in json.dumps(envelope)


def test_export_refuses_when_design_md_is_a_directory(monkeypatch, tmp_path):
    """Review item 3 (PR 1178 gate): is_file() used to return False for a directory,
    so the block was skipped entirely and export returned ok with design_text ""."""
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory" / "design.md").mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_export_refuses_when_design_md_is_a_fifo_and_does_not_hang(monkeypatch, tmp_path):
    """Review item 3 (PR 1178 gate): same is_file() gap as the directory case above,
    plus the opened fd must be O_NONBLOCK so a reader-only open of a FIFO with no
    writer returns immediately instead of hanging the whole export.

    Item 3 (PR 1180 gate): removing O_NONBLOCK makes the open() itself block
    forever (a reader-only open of a FIFO with no writer waits for one), which
    hung this test's own process for the gate's full 600s timeout rather than
    failing. Run the call in a daemon thread with a bounded join instead, so a
    regression here fails fast; release the FIFO afterward with a non-blocking
    writer so the (by-then-leaked) thread can still unblock and exit instead of
    leaking a reader open on the FIFO for the rest of the test run."""
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory").mkdir(parents=True)
    fifo_path = repo / ".factory" / "design.md"
    os.mkfifo(str(fifo_path))
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    result: dict = {}

    def _call():
        result["envelope"] = wl._cmd_export(payload, tmp_path)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout=10)
    hung = thread.is_alive()
    if hung:
        # Unblock the stuck open() so the leaked daemon thread can still finish
        # instead of holding a reader open on the FIFO for the rest of the suite.
        writer_fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer_fd)
        thread.join(timeout=10)

    assert not hung, "export hung opening a FIFO design.md without O_NONBLOCK"
    assert result["envelope"]["status"] == "refused"


def test_export_refuses_when_design_md_exceeds_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "design.md").write_bytes(b"x" * (wl.DESIGN_TEXT_CAP_BYTES + 1))
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_export_design_md_at_the_cap_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory").mkdir(parents=True)
    content = b"x" * wl.DESIGN_TEXT_CAP_BYTES
    (repo / ".factory" / "design.md").write_bytes(content)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["design_text"] == content.decode("ascii")


def test_export_refuses_a_non_utf8_design_md(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "design.md").write_bytes(b"\xff\xfe not utf-8")
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_export_a_15mib_patch_fits_the_envelope_and_round_trips(monkeypatch, tmp_path):
    big = os.urandom(15 * 1024 * 1024)

    def fake_run(argv, env=None, **kw):
        if "diff" in argv:
            return _FakeRunResult(returncode=0, stdout=big, stderr=b"")
        return _FakeRunResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    serialized = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= wl.EXPORT_ENVELOPE_CAP_BYTES
    assert base64.b64decode(envelope["patch_b64"]) == big


def test_export_refuses_a_patch_over_max_patch_bytes(monkeypatch, tmp_path):
    big = b"\xff" * (wl.MAX_PATCH_BYTES + 1)

    def fake_run(argv, env=None, **kw):
        if "diff" in argv:
            return _FakeRunResult(returncode=0, stdout=big, stderr=b"")
        return _FakeRunResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert "too large" in envelope["stderr"]


def test_export_refuses_an_over_cap_serialized_envelope(monkeypatch, tmp_path):
    monkeypatch.setattr(wl, "EXPORT_ENVELOPE_CAP_BYTES", 10)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env=None, **kw: _FakeRunResult(returncode=0, stdout=b"diff", stderr=b"")
    )
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "base_ref": "main",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_export(payload, tmp_path)

    assert envelope["status"] == "refused"


# ---------------------------------------------------------------------------
# AC-3: materialize runs the repo's own script under -I in the worker profile.
# ---------------------------------------------------------------------------


def test_materialize_composes_sandboxed_argv_and_cwd(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs

        class _Child:
            pid = 4242
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        return _Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    repo = tmp_path / "wtree" / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "materialize_agents.py").write_text("")
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0, "env": {},
    }

    envelope = wl._cmd_materialize(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert captured["argv"] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile),
        sys.executable, "-I", str(repo / "scripts" / "materialize_agents.py"), str(repo),
    ]
    assert captured["kwargs"]["cwd"] == str(repo)


def test_materialize_refuses_when_repo_is_absent(tmp_path):
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    envelope = wl._cmd_materialize(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_materialize_refuses_a_profile_outside_w(tmp_path):
    outside_profile = tmp_path.parent / "outside-materialize.sb"
    _permissive_profile(outside_profile)
    try:
        repo = tmp_path / "wtree" / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "materialize_agents.py").write_text("")
        payload = {
            "tree": "wtree", "profile": str(outside_profile), "profile_owner_uid": os.getuid(),
            "deadline_s": 5.0,
        }
        envelope = wl._cmd_materialize(payload, tmp_path)
        assert envelope["status"] == "refused"
    finally:
        outside_profile.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_materialize_runs_the_repos_own_script_with_no_cwd_on_sys_path(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "wtree" / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    marker = repo / "marker.txt"
    script = (
        "import sys\n"
        f"with open({str(marker)!r}, 'w') as f:\n"
        "    f.write('ran')\n"
        f"assert {str(scripts)!r} not in sys.path, sys.path\n"
        f"assert '' not in sys.path, sys.path\n"
    )
    (scripts / "materialize_agents.py").write_text(script)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "tree": "wtree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 30.0, "env": {},
    }

    envelope = wl._cmd_materialize(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["child_rc"] == 0, envelope
    assert marker.read_text() == "ran"


# ---------------------------------------------------------------------------
# AC-4: venv and pip.
# ---------------------------------------------------------------------------


def test_venv_composes_sandboxed_argv_and_cwd(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs

        class _Child:
            pid = 1
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        return _Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0, "env": {},
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")

    assert envelope["status"] == "ok", envelope
    assert captured["argv"] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), sys.executable, "-I", "-m", "venv",
        ".verification-venv",
    ]
    assert captured["kwargs"]["cwd"] == str(tree_dir)


def test_pip_composes_sandboxed_argv_using_the_trees_own_venv(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs

        class _Child:
            pid = 1
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        return _Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "args": ["-r", "requirements.txt"],
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
        "env": {},
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="pip")

    assert envelope["status"] == "ok", envelope
    venv_python = tree_dir / ".verification-venv" / "bin" / "python"
    assert captured["argv"] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(profile), str(venv_python), "-m", "pip", "install",
        "-r", "requirements.txt",
    ]


def test_venv_refuses_when_tree_is_absent(tmp_path):
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")
    assert envelope["status"] == "refused"


def test_pip_refuses_when_tree_is_absent(tmp_path):
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "args": ["pkg"],
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
    }
    envelope = wl._cmd_venv(payload, tmp_path, kind="pip")
    assert envelope["status"] == "refused"


def test_venv_refuses_a_payload_carrying_args_before_any_spawn(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("venv spawned a process despite carrying args")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "args": ["pkg"],
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")

    assert envelope["status"] == "refused"


def test_pip_refuses_a_payload_with_no_args_before_any_spawn(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("pip spawned a process despite carrying no args")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree",
        "profile": str(profile), "profile_owner_uid": os.getuid(), "deadline_s": 5.0,
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="pip")

    assert envelope["status"] == "refused"


def _fake_popen_capturing(captured: dict):
    def fake_popen(argv, **kwargs):
        captured["kwargs"] = kwargs

        class _Child:
            pid = 1
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        return _Child()

    return fake_popen


def test_venv_env_strips_disallowed_pip_and_python_keys(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_capturing(captured))
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
        "env": {
            "PIP_INDEX_URL": "https://evil.invalid/simple",
            "PIP_CONFIG_FILE": "/tmp/evil.cfg",
            "PYTHONPATH": "/evil",
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil'",
            "SAFE": "1",
        },
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")

    assert envelope["status"] == "ok", envelope
    env = captured["kwargs"]["env"]
    assert "PIP_INDEX_URL" not in env
    assert "PIP_CONFIG_FILE" not in env
    assert "PYTHONPATH" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env.get("SAFE") == "1"


def test_venv_env_keeps_allowlisted_pip_env_keys(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_capturing(captured))
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
        "env": {"PIP_INDEX_URL": "https://internal.invalid/simple"},
        "pip_env": ["PIP_INDEX_URL"],
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")

    assert envelope["status"] == "ok", envelope
    env = captured["kwargs"]["env"]
    assert env.get("PIP_INDEX_URL") == "https://internal.invalid/simple"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_venv_creates_a_real_venv(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")
    tree_dir = tmp_path / "ptree"
    tree_dir.mkdir()
    profile = _permissive_profile(tmp_path / "pip.sb")
    payload = {
        "tree": "ptree", "profile": str(profile), "profile_owner_uid": os.getuid(),
        "deadline_s": 60.0, "env": {},
    }

    envelope = wl._cmd_venv(payload, tmp_path, kind="venv")

    assert envelope["status"] == "ok", envelope
    assert envelope["child_rc"] == 0, envelope
    assert (tree_dir / ".verification-venv" / "bin" / "python").exists()


# ---------------------------------------------------------------------------
# AC-5: purge clears flags, restores write bits, and reports the remainder.
# ---------------------------------------------------------------------------


def test_purge_removes_entries_it_owns(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("y")

    envelope = wl._cmd_purge({}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert not tmp_path.exists()


def test_purge_with_keep_root_empties_contents_but_keeps_the_directory(tmp_path):
    (tmp_path / "a.txt").write_text("x")

    envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert tmp_path.is_dir()
    assert list(tmp_path.iterdir()) == []


def test_purge_skips_an_entry_owned_by_another_uid(monkeypatch, tmp_path):
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("mine? no.")
    real_lstat = os.lstat

    def fake_lstat(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if str(path) == str(foreign):
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, st.st_gid,
                 st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
            )
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)

    envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert foreign.exists()
    body = json.loads(envelope["stdout"])
    assert str(foreign) in body["remaining"]


@pytest.mark.skipif(sys.platform != "darwin", reason="chflags/uchg is macOS-only")
def test_purge_clears_uchg_and_removes_the_file(tmp_path):
    target = tmp_path / "locked.txt"
    target.write_text("locked")
    os.chflags(str(target), stat.UF_IMMUTABLE)

    envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert not target.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="chmod 0555 enforcement is macOS-only here")
def test_purge_removes_a_file_inside_a_0555_directory(tmp_path):
    locked_dir = tmp_path / "locked_dir"
    locked_dir.mkdir()
    (locked_dir / "inside.txt").write_text("inside")
    os.chmod(str(locked_dir), 0o555)

    envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert not locked_dir.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="chflags/uchg is macOS-only")
def test_purge_removes_a_file_inside_a_uchg_directory(tmp_path):
    locked_dir = tmp_path / "uchg_dir"
    locked_dir.mkdir()
    (locked_dir / "inside.txt").write_text("inside")
    os.chflags(str(locked_dir), stat.UF_IMMUTABLE)

    envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert not locked_dir.exists()


def test_purge_never_follows_a_symlink_out_of_w(tmp_path):
    outside = tmp_path.parent / "purge-outside"
    outside.mkdir(exist_ok=True)
    marker = outside / "marker.txt"
    marker.write_text("do not touch")
    try:
        link = tmp_path / "escape"
        link.symlink_to(outside)

        envelope = wl._cmd_purge({"keep_root": True}, tmp_path)

        assert envelope["status"] == "ok", envelope
        assert not link.exists() and not link.is_symlink()
        assert marker.read_text() == "do not touch"
        assert outside.is_dir()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_purge_bounded_by_timeout_reports_remaining(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")

    times = iter([0.0] + [1000.0] * 20)

    def fake_monotonic():
        return next(times, 1000.0)

    monkeypatch.setattr(wl.time, "monotonic", fake_monotonic)

    envelope = wl._cmd_purge({"timeout_s": 1.0}, tmp_path)

    assert envelope["status"] == "ok", envelope
    body = json.loads(envelope["stdout"])
    assert body["remaining_count"] >= 1


# ---------------------------------------------------------------------------
# AC-6: probe measures, the operator decides.
# ---------------------------------------------------------------------------


def test_probe_issues_the_runbooks_items_through_the_injected_seams(monkeypatch, tmp_path):
    calls = []

    def fake_runner(argv, env=None, **kwargs):
        calls.append(list(argv))
        return _FakeRunResult(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(wl, "PROBE_RUNNER", fake_runner)
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"errno": 22})

    worker_profile = _permissive_profile(tmp_path / "worker.sb")
    payload = {
        "paths": [str(tmp_path / "nope")],
        "mach_names": ["com.apple.foo", "com.apple.bar"],
        "submit_label": "com.test.probe",
        "bootstrap_plist": str(tmp_path / "plist.xml"),
        "bootstrap_domain": "user/850",
        "worker_profile": str(worker_profile),
        "worker_profile_owner_uid": os.getuid(),
        "target_pid": 4242,
    }

    envelope = wl._cmd_probe(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    observations = json.loads(envelope["stdout"])

    assert calls[0] == [wl.LAUNCHCTL_PATH, "managername"]
    assert calls[1] == [wl.SECURITY_PATH, "find-generic-password", "-s", "Claude Code-credentials"]
    assert calls[2] == [wl.GIT_PATH, "--version"]
    assert calls[3] == [sys.executable, "-I", "-c", "import ssl"]
    assert calls[4] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(worker_profile), wl.LAUNCHCTL_PATH, "submit", "-l",
        "com.test.probe", "--", "/usr/bin/true",
    ]
    assert calls[5] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(worker_profile), wl.LAUNCHCTL_PATH, "remove", "com.test.probe",
    ]
    assert calls[6] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(worker_profile), wl.LAUNCHCTL_PATH, "bootstrap", "user/850",
        str(tmp_path / "plist.xml"),
    ]
    assert calls[7] == [
        wl.SANDBOX_EXEC_PATH, "-f", str(worker_profile), wl.LAUNCHCTL_PATH, "bootout", "user/850",
        str(tmp_path / "plist.xml"),
    ]
    assert len(calls) == 8

    assert observations["a"]["windowserver_lookup"] == 1102
    assert observations["c"] == {"errno": 22}
    assert observations["i"] == {"com.apple.foo": 1102, "com.apple.bar": 1102}
    assert observations["e"] is None
    assert observations["b"][str(tmp_path / "nope")]["errno"] is not None


def test_probe_never_issues_claude_without_run_claude_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(wl, "PROBE_RUNNER", lambda *a, **k: _FakeRunResult())
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True, "size": 0})

    def _boom(payload, w_path):
        raise AssertionError("claude was run despite run_claude_probe being unset")

    monkeypatch.setattr(wl, "run", _boom)

    envelope = wl._cmd_probe({"paths": [], "mach_names": []}, tmp_path)

    assert envelope["status"] == "ok", envelope
    observations = json.loads(envelope["stdout"])
    assert observations["e"] is None


def test_probe_never_issues_claude_version_or_run_without_run_claude_probe(monkeypatch, tmp_path):
    """AC-6 (amended 2026-10-04): even when claude_version_payload/claude_run_payload
    are present, `run_claude_probe` unset must still gate both out."""
    recorded_runner_calls = []
    monkeypatch.setattr(
        wl, "PROBE_RUNNER",
        lambda argv, **k: recorded_runner_calls.append(list(argv)) or _FakeRunResult(),
    )
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True, "size": 0})

    def _boom(payload, w_path):
        raise AssertionError("claude was run despite run_claude_probe being unset")

    monkeypatch.setattr(wl, "run", _boom)

    payload = {
        "paths": [], "mach_names": [],
        "claude_version_payload": {"argv": ["claude", "--version"]},
        "claude_run_payload": {"argv": ["claude", "-p", "hi"]},
    }

    envelope = wl._cmd_probe(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    observations = json.loads(envelope["stdout"])
    assert observations["e"] is None
    assert "e_version" not in observations
    assert not any("-p" in c for c in recorded_runner_calls)


def test_probe_runs_claude_through_the_worker_profile_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(wl, "PROBE_RUNNER", lambda *a, **k: _FakeRunResult())
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True, "size": 0})

    captured_payloads = []

    def fake_run(payload, w_path):
        captured_payloads.append(payload)
        return {
            "status": "ok", "child_rc": 0, "stdout": "is_error: false", "stderr": "",
            "stdout_truncated_result": False,
        }

    monkeypatch.setattr(wl, "run", fake_run)

    payload = {
        "paths": [], "mach_names": [], "run_claude_probe": True,
        "claude_version_payload": {"argv": ["claude", "--version"]},
        "claude_run_payload": {"argv": ["claude", "-p", "hi"]},
    }

    envelope = wl._cmd_probe(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    observations = json.loads(envelope["stdout"])
    assert observations["e"]["status"] == "ok"
    assert observations["e_version"]["status"] == "ok"
    assert len(captured_payloads) == 2
    assert captured_payloads[0]["argv"] == ["claude", "--version"]
    assert captured_payloads[1]["argv"] == ["claude", "-p", "hi"]


def test_procargs2_mib_is_1_49_pid():
    """Review item 8 (PR 1178 gate): the sysctl mib [1, 49, pid] was never asserted --
    factored out into its own pure function to make that possible."""
    assert wl._procargs2_mib(4242) == [1, 49, 4242]


def test_probe_sysctl_seam_receives_the_payload_target_pid(monkeypatch, tmp_path):
    """Review item 8: the pid reaches the ctypes seam (PROBE_SYSCTL_PROCARGS2)."""
    captured = []
    monkeypatch.setattr(wl, "PROBE_RUNNER", lambda *a, **k: _FakeRunResult())
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: captured.append(pid) or {"ok": True})

    envelope = wl._cmd_probe({"paths": [], "mach_names": [], "target_pid": 4242}, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert captured == [4242]


def test_probe_bootstrap_look_up_receives_exactly_the_payload_mach_names(monkeypatch, tmp_path):
    """Review item 8: the names passed to the bootstrap_look_up seam equal the
    payload's mach_names.

    Item 2(b) (PR 1180 gate): assert the FULL recorded list, not just its tail --
    item (a)'s own fixed lookup (com.apple.windowserver.active) must appear first,
    exactly once. Mutation: changing that literal turns this red."""
    captured = []
    monkeypatch.setattr(
        wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: captured.append(name) or 1102
    )
    monkeypatch.setattr(wl, "PROBE_RUNNER", lambda *a, **k: _FakeRunResult())
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True})

    payload = {"paths": [], "mach_names": ["com.apple.foo", "com.apple.bar"]}
    envelope = wl._cmd_probe(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    # item (a) also looks up the windowserver name once, first; item (i) looks up
    # exactly the payload's own mach_names, in order, last.
    assert captured == ["com.apple.windowserver.active", "com.apple.foo", "com.apple.bar"]


def test_probe_git_version_routes_through_git_env(monkeypatch, tmp_path):
    """Review item 9 (PR 1178 gate): probe's `git --version` used to bypass _git_env
    entirely. Mutation: reverting to the plain process_env-only env turns this red."""
    captured_envs = []

    def fake_runner(argv, env=None, **kwargs):
        if argv[:2] == [wl.GIT_PATH, "--version"]:
            captured_envs.append(dict(env or {}))
        return _FakeRunResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wl, "PROBE_RUNNER", fake_runner)
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True})

    payload = {
        "paths": [], "mach_names": [],
        "env": {"GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil'"},
    }
    envelope = wl._cmd_probe(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert len(captured_envs) == 1
    assert "GIT_CONFIG_PARAMETERS" not in captured_envs[0]
    assert captured_envs[0]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured_envs[0]["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_probe_item_streams_are_tail_capped_and_envelope_still_parses_as_json(monkeypatch, tmp_path):
    """Review item 4 (PR 1178 gate): the gate measured a 25 MB probe envelope. 3 MiB of
    probe-runner stdout must still give a serialized envelope within
    EXEC_ENVELOPE_CAP_BYTES, and the probe's stdout must still parse as JSON."""
    big = "x" * (3 * 1024 * 1024)
    monkeypatch.setattr(
        wl, "PROBE_RUNNER", lambda *a, **k: _FakeRunResult(returncode=0, stdout=big, stderr="")
    )
    monkeypatch.setattr(wl, "PROBE_BOOTSTRAP_LOOK_UP", lambda name: 1102)
    monkeypatch.setattr(wl, "PROBE_SYSCTL_PROCARGS2", lambda pid: {"ok": True})

    envelope = wl._cmd_probe({"paths": [], "mach_names": []}, tmp_path)

    assert envelope["status"] == "ok", envelope
    serialized = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= wl.EXEC_ENVELOPE_CAP_BYTES
    observations = json.loads(envelope["stdout"])
    assert isinstance(observations, dict)


# ---------------------------------------------------------------------------
# AC-7: Invariant L's table is extended, not bypassed.
# ---------------------------------------------------------------------------

_B2B_SUBCOMMANDS = {"seed", "vseed", "materialize", "export", "venv", "pip", "purge", "probe"}


def test_every_b2b_subcommand_is_registered_in_every_table():
    assert _B2B_SUBCOMMANDS <= set(wl.SUBCOMMAND_PROFILE)
    assert _B2B_SUBCOMMANDS <= set(wl.SUBCOMMAND_FUNCTION)
    assert _B2B_SUBCOMMANDS <= set(wl._HANDLERS)


def test_b2b_subcommands_use_the_designs_profile_assignment():
    assert wl.SUBCOMMAND_PROFILE["seed"] == "worker"
    assert wl.SUBCOMMAND_PROFILE["vseed"] == "verification"
    assert wl.SUBCOMMAND_PROFILE["materialize"] == "worker"
    assert wl.SUBCOMMAND_PROFILE["export"] == "worker"
    assert wl.SUBCOMMAND_PROFILE["venv"] == "pip"
    assert wl.SUBCOMMAND_PROFILE["pip"] == "pip"
    assert wl.SUBCOMMAND_PROFILE["purge"] == "UNSANDBOXED"
    assert wl.SUBCOMMAND_PROFILE["probe"] == "UNSANDBOXED"


def test_unsandboxed_fixed_binaries_are_exactly_the_module_constant():
    assert set(wl.UNSANDBOXED_FIXED_BINARIES) == {wl.LAUNCHCTL_PATH, wl.SECURITY_PATH, wl.GIT_PATH}


def test_probe_runner_default_is_a_bare_reference_never_a_call():
    # Invisible to the Invariant L ast scan and to spawn_scan.py, the same shape as
    # tunnel_keeper.TunnelKeeper's injected Popen default.
    assert wl.PROBE_RUNNER is subprocess.run


def test_seed_and_vseed_are_distinct_handler_objects_selected_at_table_definition():
    # AC-1 SEPARATION: dispatch never reads the payload to decide seed vs. vseed.
    assert wl._HANDLERS["seed"] is wl._cmd_seed
    assert wl._HANDLERS["vseed"] is wl._cmd_vseed
    assert wl._HANDLERS["seed"] is not wl._HANDLERS["vseed"]


def test_venv_and_pip_handlers_bind_kind_via_functools_partial():
    # AC-4 SEPARATION: `kind` is fixed at table-definition time, not read from payload.
    assert wl._HANDLERS["venv"].func is wl._cmd_venv
    assert wl._HANDLERS["venv"].keywords == {"kind": "venv"}
    assert wl._HANDLERS["pip"].func is wl._cmd_venv
    assert wl._HANDLERS["pip"].keywords == {"kind": "pip"}
