"""PARTB-B2a: worker_launcher.py's process core. The launcher runs directly as the
test's own uid here -- never through sudo, never as a real `_factoryworker`. Tests that
execute `sandbox-exec` for real are gated by `_sandbox_probes_available()` (the same idiom
test_containment.py uses), because this suite can itself run nested inside the
dispatcher's own deny-write sandbox, where a second `sandbox_apply` always fails with
rc=71 regardless of the inner profile. `reap`'s real SIGKILL-broadcast default is never
invoked by any test here -- see test_reap_real_default_never_invoked_when_guard_fails and
the module-level REAP_KILL/REAP_STAT_UID seams it exercises.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_identity  # noqa: E402
import worker_launcher as wl  # noqa: E402

# Fixture credential values: >=16 chars, distinct from any real value.
OAUTH = "oauth-token-fixture-aaaaaaaaaaaa"
READ_KEY = "read-key-fixture-bbbbbbbbbbbbbbbb"


def _parse_env_output(text: str) -> dict[str, str]:
    """Parses `/usr/bin/env`'s NAME=VALUE-per-line stdout. None of this suite's fixture
    values contain '=' or a newline, so splitting on the first '=' per line is exact."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        name, _, value = line.partition("=")
        env[name] = value
    return env


def _sandbox_probes_available() -> tuple[bool, str]:
    """Copied from tests/test_containment.py's idiom: whether this process can write
    directly under /tmp and apply a sandbox-exec profile of its own -- both denied when
    this suite itself runs nested inside the dispatcher's own deny-write sandbox."""
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="worker-launcher-probe-", dir="/tmp")
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


def _run_main(argv: list[str], payload: dict) -> tuple[dict, str]:
    """Drives main() in-process exactly as AC-1 requires, then proves exactly one JSON
    value sits on stdout (raw_decode plus asserting nothing trails it)."""
    stdin = io.BytesIO(json.dumps(payload).encode("utf-8"))
    stdout = io.StringIO()
    rc = wl.main(argv, stdin, stdout)
    assert rc == 0
    raw = stdout.getvalue()
    decoder = json.JSONDecoder()
    envelope, idx = decoder.raw_decode(raw)
    assert raw[idx:].strip() == "", f"trailing data after the envelope: {raw[idx:]!r}"
    return envelope, raw


# ---------------------------------------------------------------------------
# AC-1: the module contract.
# ---------------------------------------------------------------------------


def test_module_docstring_names_design_and_invariant_l():
    assert wl.__doc__ is not None
    assert "design" in wl.__doc__.lower()
    assert "Invariant L" in wl.__doc__


def test_subcommand_set_is_exactly_this_halfs_four():
    assert {"selfcheck", "reap", "run", "exec"} <= set(wl.SUBCOMMAND_PROFILE)


def test_subcommand_set_is_exactly_both_halves():
    # B2b (PARTB-B2b) extends B2a's four with the git/venv/pip/purge/probe set.
    assert set(wl.SUBCOMMAND_PROFILE) == {
        "selfcheck", "reap", "run", "exec",
        "seed", "vseed", "materialize", "export", "venv", "pip", "purge", "probe",
    }


def test_main_single_envelope_selfcheck(tmp_path):
    envelope, _ = _run_main(["selfcheck", str(tmp_path)], {"tmp_root": str(tmp_path)})
    assert envelope["status"] == "ok"
    assert set(envelope) == {"status", "child_rc", "stdout", "stderr", "stdout_truncated_result"}


def test_main_single_envelope_unknown_subcommand(tmp_path):
    envelope, _ = _run_main(["bogus", str(tmp_path)], {})
    assert envelope["status"] == "refused"
    assert "bogus" in envelope["stderr"]


def test_main_single_envelope_reap_refusal(tmp_path):
    envelope, _ = _run_main(["reap", str(tmp_path)], {"worker_uid": -1})
    assert envelope["status"] == "refused"


def test_main_single_envelope_run_refusal_missing_fields(tmp_path):
    envelope, _ = _run_main(["run", str(tmp_path)], {})
    assert envelope["status"] == "refused"


def test_main_single_envelope_exec_refusal_missing_fields(tmp_path):
    envelope, _ = _run_main(["exec", str(tmp_path)], {})
    assert envelope["status"] == "refused"


def test_main_malformed_payload_is_refused_not_raised(tmp_path):
    stdin = io.BytesIO(b"not json at all")
    stdout = io.StringIO()
    rc = wl.main(["selfcheck", str(tmp_path)], stdin, stdout)
    assert rc == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["status"] == "refused"


def test_main_wrong_argv_shape_is_refused():
    stdin = io.BytesIO(b"{}")
    stdout = io.StringIO()
    rc = wl.main(["selfcheck"], stdin, stdout)
    assert rc == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["status"] == "refused"


# ---------------------------------------------------------------------------
# AC-10: nothing imports worker_launcher except its own tests.
# ---------------------------------------------------------------------------


def test_nothing_imports_worker_launcher_except_tests():
    dispatcher_dir = Path(wl.__file__).resolve().parent
    for path in dispatcher_dir.rglob("*.py"):
        rel = path.relative_to(dispatcher_dir)
        if rel.parts[0] == "tests" or path.name == "worker_launcher.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "worker_launcher" for a in node.names), (
                    f"{rel} imports worker_launcher"
                )
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "worker_launcher", f"{rel} imports worker_launcher"


# ---------------------------------------------------------------------------
# AC-8 (and AC-2's static half): Invariant L, parsed with ast.
# ---------------------------------------------------------------------------

_PROCESS_STARTER_SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}


def _is_os_process_starter(attr: str) -> bool:
    return (
        attr in ("system", "popen")
        or attr.startswith("exec")
        or attr.startswith("spawn")
        or attr.startswith("posix_spawn")
    )


def _invariant_l_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    stack: list[str] = []
    function_to_subcommand = {v: k for k, v in wl.SUBCOMMAND_FUNCTION.items()}

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node: ast.Name) -> None:
            if node.id in ("environ", "getenv"):
                violations.append(f"bare name {node.id!r} read at line {node.lineno}")
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr in ("environ", "getenv"):
                violations.append(f"attribute .{node.attr} read at line {node.lineno}")
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            is_process_starter = False
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module, attr = func.value.id, func.attr
                if module == "subprocess" and attr in _PROCESS_STARTER_SUBPROCESS_ATTRS:
                    is_process_starter = True
                elif module == "os" and _is_os_process_starter(attr):
                    is_process_starter = True
            if is_process_starter:
                enclosing = stack[-1] if stack else "<module>"
                subcommand = function_to_subcommand.get(enclosing)
                if subcommand is None:
                    violations.append(
                        f"process-starting call at line {node.lineno} inside "
                        f"{enclosing!r}, which no subcommand registers"
                    )
                else:
                    category = wl.SUBCOMMAND_PROFILE.get(subcommand)
                    if category == "UNSANDBOXED":
                        first_arg = node.args[0] if node.args else None
                        fixed = (
                            isinstance(first_arg, ast.Constant)
                            and isinstance(first_arg.value, str)
                            and first_arg.value in wl.UNSANDBOXED_FIXED_BINARIES
                        )
                        if not fixed:
                            violations.append(
                                f"UNSANDBOXED function {enclosing!r} spawns with a "
                                f"non-fixed argv at line {node.lineno}"
                            )
                    elif category not in ("worker", "verification", "pip"):
                        violations.append(
                            f"{enclosing!r} (subcommand {subcommand!r}) maps to "
                            f"unrecognised category {category!r}"
                        )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_invariant_l_static_scan():
    source = Path(wl.__file__).read_text(encoding="utf-8")
    violations = _invariant_l_violations(source)
    assert violations == [], "\n".join(violations)


def test_unsandboxed_subcommands_set_is_exactly_the_design_four():
    assert wl.UNSANDBOXED_SUBCOMMANDS == frozenset({"selfcheck", "probe", "reap", "purge"})


def test_selfcheck_and_reap_never_call_subprocess(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("an UNSANDBOXED subcommand called subprocess.Popen")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    wl._cmd_selfcheck({"tmp_root": str(tmp_path)}, tmp_path)
    wl._cmd_reap({"worker_uid": -1}, tmp_path)  # guard fails before any spawn could occur


# ---------------------------------------------------------------------------
# AC-7: selfcheck.
# ---------------------------------------------------------------------------


def test_selfcheck_creates_absent_dir_at_0700(tmp_path):
    envelope = wl._cmd_selfcheck({"tmp_root": str(tmp_path)}, tmp_path)
    assert envelope["status"] == "ok"
    created = Path(envelope["stdout"])
    assert created == tmp_path / f"claude-{os.geteuid()}"
    st = created.stat()
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.geteuid()


def test_selfcheck_succeeds_on_existing_correct_dir(tmp_path):
    path = tmp_path / f"claude-{os.geteuid()}"
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    marker = path / "marker"
    marker.write_text("kept")

    envelope = wl._cmd_selfcheck({"tmp_root": str(tmp_path)}, tmp_path)

    assert envelope["status"] == "ok"
    assert marker.read_text() == "kept"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_selfcheck_refuses_foreign_owner_without_touching_it(tmp_path, monkeypatch):
    # No real chown (the test has no privilege for that): report a patched effective
    # uid whose expected path -- claude-999999 -- is pre-created but really owned by
    # this test process's own (different) real uid, so the ownership check fails the
    # same way it would against a genuinely foreign path.
    monkeypatch.setattr(os, "geteuid", lambda: 999999)
    path = tmp_path / "claude-999999"
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    marker = path / "marker"
    marker.write_text("untouched")

    envelope = wl._cmd_selfcheck({"tmp_root": str(tmp_path)}, tmp_path)

    assert envelope["status"] == "refused"
    assert marker.read_text() == "untouched"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_selfcheck_refuses_wrong_mode_without_chmodding(tmp_path):
    path = tmp_path / f"claude-{os.geteuid()}"
    path.mkdir(mode=0o755)
    os.chmod(path, 0o755)

    envelope = wl._cmd_selfcheck({"tmp_root": str(tmp_path)}, tmp_path)

    assert envelope["status"] == "refused"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


# ---------------------------------------------------------------------------
# AC-6: reap -- guarded, injected, and the real default proven unreachable.
# ---------------------------------------------------------------------------


class _RecordingKiller:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))


def test_reap_calls_killer_only_when_all_three_guards_pass(tmp_path, monkeypatch):
    killer = _RecordingKiller()
    monkeypatch.setattr(wl, "REAP_KILL", killer)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(wl, "REAP_STAT_UID", lambda path: 999)  # W owned by someone else

    envelope = wl._cmd_reap({"worker_uid": 501}, tmp_path)

    assert envelope["status"] == "ok"
    assert killer.calls == [(-1, signal.SIGKILL)]


def test_reap_refuses_on_worker_uid_mismatch(tmp_path, monkeypatch):
    killer = _RecordingKiller()
    monkeypatch.setattr(wl, "REAP_KILL", killer)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(wl, "REAP_STAT_UID", lambda path: 999)

    envelope = wl._cmd_reap({"worker_uid": 502}, tmp_path)

    assert envelope["status"] == "refused"
    assert killer.calls == []


def test_reap_refuses_below_minimum_uid(tmp_path, monkeypatch):
    killer = _RecordingKiller()
    monkeypatch.setattr(wl, "REAP_KILL", killer)
    monkeypatch.setattr(os, "geteuid", lambda: 10)
    monkeypatch.setattr(wl, "REAP_STAT_UID", lambda path: 999)

    envelope = wl._cmd_reap({"worker_uid": 10}, tmp_path)

    assert envelope["status"] == "refused"
    assert killer.calls == []


def test_reap_refuses_when_w_is_owned_by_the_effective_uid(tmp_path, monkeypatch):
    """What a real test run looks like: W is owned by the test's own uid, so a crafted
    payload claiming worker_uid == that uid must still be refused."""
    killer = _RecordingKiller()
    monkeypatch.setattr(wl, "REAP_KILL", killer)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(wl, "REAP_STAT_UID", lambda path: 501)

    envelope = wl._cmd_reap({"worker_uid": 501}, tmp_path)

    assert envelope["status"] == "refused"
    assert killer.calls == []


def test_reap_real_default_never_invoked_when_guard_fails(tmp_path, monkeypatch):
    """Uses the REAL REAP_KILL/REAP_STAT_UID defaults (no fake injected) and
    monkeypatches the real os.kill to blow up if ever called with -1 -- proving the
    guard-fail path never reaches it, through the actual default seam rather than a
    substitute."""

    def _must_not_be_called(pid, sig):
        raise AssertionError(f"the real os.kill was invoked: ({pid}, {sig})")

    monkeypatch.setattr(os, "kill", _must_not_be_called)
    # worker_uid deliberately does not match this process's real effective uid.
    envelope = wl._cmd_reap({"worker_uid": -1}, tmp_path)
    assert envelope["status"] == "refused"


# ---------------------------------------------------------------------------
# AC-2: the environment is the payload, never os.environ; exec admits no credential.
# ---------------------------------------------------------------------------


def _base_payload(cwd: Path, profile: Path, **extra) -> dict:
    payload = {
        "argv": ["/usr/bin/true"],
        "cwd": str(cwd),
        "env": {},
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    payload.update(extra)
    return payload


def test_exec_refuses_credential_named_env_before_any_process_starts(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("exec spawned a process despite a credential-named env key")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile, env={"SUBSTRATE_API_KEY": READ_KEY})

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "refused"
    assert "SUBSTRATE_API_KEY" in envelope["stderr"]


#: subprocess.Popen(env=...) replaces the child's environment block wholesale (execve's
#: envp, not a merge with the parent's os.environ) -- so nothing beyond what the
#: launcher itself builds should ever reach the child. The child is `/usr/bin/env`
#: itself (not a Python interpreter): a freshly spawned CPython mutates its OWN
#: os.environ at startup (PEP 538/540 locale coercion can set LC_CTYPE; a framework
#: build linking CoreFoundation can have __CF_USER_TEXT_ENCODING added by the loader
#: before main() runs) and then dumps that already-mutated environ right back out, so
#: a Python dumper is not a trustworthy witness of what the launcher itself passed as
#: envp. `/usr/bin/env` with no args has no startup of its own: it prints exactly the
#: envp execve gave it, one NAME=VALUE per line, nothing added.
_RUN_CHILD_ENV_EXPECTED_KEYS = {
    "CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY", "SUBSTRATE_URL", "DISABLE_AUTOUPDATER",
    "HOME", "TMPDIR", "PATH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
}


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_run_child_env_holds_exactly_the_allowed_credentials(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(
        repo, profile,
        argv=["/usr/bin/env"],
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
            "SUBSTRATE_API_KEY": READ_KEY,
            "SUBSTRATE_URL": "https://substrate.example.invalid",
            "DISABLE_AUTOUPDATER": "1",
            "NOT_ALLOWED": "should-not-appear",
        },
    )

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    child_env = _parse_env_output(envelope["stdout"])
    assert set(child_env) == _RUN_CHILD_ENV_EXPECTED_KEYS
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
    assert child_env["SUBSTRATE_API_KEY"] == READ_KEY
    assert child_env["SUBSTRATE_URL"] == "https://substrate.example.invalid"
    assert child_env["DISABLE_AUTOUPDATER"] == "1"
    assert "NOT_ALLOWED" not in child_env
    assert child_env["HOME"] == str(tmp_path / "wtree" / ".tmp" / "home")
    assert child_env["GIT_AUTHOR_EMAIL"] == wl.GIT_IDENTITY_EMAIL


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_exec_child_env_holds_only_the_payloads_non_credential_entries(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(
        repo, profile,
        argv=["/usr/bin/env"],
        env={"SOME_SETTING": "value"},
    )

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    child_env = _parse_env_output(envelope["stdout"])
    expected_keys = {
        "SOME_SETTING", "HOME", "TMPDIR", "PATH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    }
    assert set(child_env) == expected_keys
    assert child_env["SOME_SETTING"] == "value"
    assert child_env["HOME"] == str(tmp_path / "vtree" / ".verify-tmp" / "home")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY"):
        assert name not in child_env


# ---------------------------------------------------------------------------
# AC-3: profiles are paths; every sandboxed child goes through one.
# ---------------------------------------------------------------------------


def test_run_refuses_absent_profile(tmp_path):
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    payload = _base_payload(repo, tmp_path / "does-not-exist.sb")
    envelope = wl.run(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_run_refuses_profile_that_is_a_directory(tmp_path):
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    not_a_file = tmp_path / "profile-dir"
    not_a_file.mkdir()
    payload = _base_payload(repo, not_a_file)
    envelope = wl.run(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_run_refuses_profile_with_wrong_owner_uid(tmp_path):
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(repo, profile, profile_owner_uid=os.getuid() + 1)
    envelope = wl.run(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_exec_refuses_missing_profile_owner_uid(tmp_path):
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile)
    del payload["profile_owner_uid"]
    envelope = wl._cmd_exec(payload, tmp_path)
    assert envelope["status"] == "refused"


def test_run_refuses_cwd_outside_w(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("run spawned a process despite a cwd outside W")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    outside = tmp_path.parent / "elsewhere" / "wtree" / "repo"
    outside.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(outside, profile)

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_run_refuses_cwd_one_level_off_w(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("run spawned a process despite a wrong-shaped cwd")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    wrong_shape = tmp_path / "wtree"  # missing the trailing /repo
    wrong_shape.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(wrong_shape, profile)

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_exec_refuses_cwd_outside_w(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("exec spawned a process despite a cwd outside W")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    outside = tmp_path.parent / "elsewhere" / "vtree" / "repo"
    outside.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(outside, profile)

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_exec_refuses_cwd_under_wtree_which_is_a_run_tree_not_an_exec_tree(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("exec spawned a process despite a run-shaped cwd")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    run_shaped = tmp_path / "wtree" / "repo"
    run_shaped.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(run_shaped, profile)

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "refused"


def test_exec_accepts_either_ptree_or_vtree_cwd_shape(tmp_path, monkeypatch):
    for tree in ("ptree", "vtree"):
        repo = tmp_path / tree / "repo"
        repo.mkdir(parents=True)
        profile = _permissive_profile(tmp_path / f"{tree}.sb")

        def fake_popen(argv, **kwargs):
            return _FakeCompletedChild(argv, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        payload = _base_payload(repo, profile)

        envelope = wl._cmd_exec(payload, tmp_path)

        assert envelope["status"] == "ok", (tree, envelope)


class _FakeCompletedChild:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0

    def communicate(self, timeout=None):
        return b"", b""


def test_run_composes_sandboxed_argv_without_executing_it(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeCompletedChild(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(repo, profile, argv=["/bin/echo", "hi"])

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "ok"
    assert captured["argv"] == [wl.SANDBOX_EXEC_PATH, "-f", str(profile), "/bin/echo", "hi"]
    assert captured["kwargs"]["start_new_session"] is True


def test_exec_composes_sandboxed_argv_without_executing_it(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return _FakeCompletedChild(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile, argv=["/bin/echo", "hi"])

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "ok"
    assert captured["argv"] == [wl.SANDBOX_EXEC_PATH, "-f", str(profile), "/bin/echo", "hi"]


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_exec_actually_runs_under_sandbox_exec(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile, argv=[sys.executable, "-c", "print('hi')"])

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["child_rc"] == 0
    assert envelope["stdout"].strip() == "hi"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_exec_actually_runs_under_sandbox_exec_records_spawned_argv0(tmp_path, monkeypatch):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile, argv=[sys.executable, "-c", "print('hi')"])

    captured = {}
    real_popen = subprocess.Popen

    def recording_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert captured["argv"][0] == wl.SANDBOX_EXEC_PATH


# ---------------------------------------------------------------------------
# AC-4: the envelope and the caps.
# ---------------------------------------------------------------------------


def test_cap_stream_passes_through_when_under_cap():
    assert wl._cap_stream(b"short", 100) == "short"


def test_cap_stream_keeps_tail_with_exact_prefix_count():
    raw = b"0123456789ABCDEFGHIJ"  # 20 bytes
    text = wl._cap_stream(raw, 10)
    assert text == "[... 10 bytes truncated ...]ABCDEFGHIJ"


def test_cap_stream_replaces_invalid_utf8():
    raw = b"\xff\xfehello"
    text = wl._cap_stream(raw, 100)
    assert "�" in text
    assert "hello" in text


def test_cap_stream_invalid_utf8_in_the_kept_tail_still_decodes():
    raw = b"X" * 5 + b"\xff\xfe" + b"Y" * 5
    text = wl._cap_stream(raw, 7)  # tail = last 7 bytes: one 'X', the bad pair, 'YYYYY'
    assert text.startswith("[... 5 bytes truncated ...]")
    assert "�" in text
    assert text.endswith("YYYYY")


def test_stdout_truncated_result_true_when_kept_tail_has_no_newline():
    raw = b"z" * 20
    assert wl._stdout_truncated_result(raw, 10) is True


def test_stdout_truncated_result_true_despite_one_trailing_newline():
    # The regression this guards: a single trailing '\n' (the common case for
    # line-oriented output) must not hide that the content before it is itself
    # over the cap -- the old implementation looked at raw[-cap:], which always
    # contains that trailing '\n' and so always read as "not truncated".
    raw = b"x" * 20 + b"\n"
    assert wl._stdout_truncated_result(raw, 10) is True


def test_stdout_truncated_result_false_newline_separated_final_line_under_cap():
    raw = b"x" * 5 + b"\n" + b"y" * 5
    assert wl._stdout_truncated_result(raw, 8) is False


def test_stdout_truncated_result_false_when_kept_tail_has_a_newline():
    raw = b"x" * 5 + b"\n" + b"y" * 5  # 11 bytes; last 8 bytes include the '\n'
    assert wl._stdout_truncated_result(raw, 8) is False


def test_stdout_truncated_result_false_when_under_cap():
    assert wl._stdout_truncated_result(b"short", 100) is False


def test_cap_constants_pinned_values():
    assert wl.EXEC_STREAM_CAP_BYTES == 1 * 1024 * 1024
    assert wl.RUN_STDOUT_CAP_BYTES == 16 * 1024 * 1024
    assert wl.RUN_STDERR_CAP_BYTES == 1 * 1024 * 1024
    assert wl.ENVELOPE_CAP_SLACK_BYTES == 4 * 1024


def test_envelope_cap_constants_equal_worker_identitys():
    assert wl.EXEC_ENVELOPE_CAP_BYTES == worker_identity.EXEC_ENVELOPE_CAP_BYTES
    assert wl.RUN_ENVELOPE_CAP_BYTES == worker_identity.RUN_ENVELOPE_CAP_BYTES


def _fake_popen_with_output(stdout_bytes: bytes, stderr_bytes: bytes = b""):
    def fake_popen(argv, **kwargs):
        child = _FakeCompletedChild(argv, **kwargs)
        child.communicate = lambda timeout=None: (stdout_bytes, stderr_bytes)
        return child

    return fake_popen


#: AC-4/round-2 item 2: a linear subtraction of the serialized overage, sized for a
#: 1:1 raw-to-serialized byte ratio, overshoots straight past zero on a 3x-inflating
#: byte pattern (every byte -> one 3-byte U+FFFD replacement) and collapses the tail
#: to nothing. The fix (binary search in _max_fitting_cap) must keep most of it.
_FIT_NONCOLLAPSE_FLOOR_BYTES = 64 * 1024


def test_exec_envelope_fits_the_cap_with_1mib_of_0xff_on_stdout(tmp_path, monkeypatch):
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile)
    big = b"\xff" * wl.EXEC_STREAM_CAP_BYTES  # each byte decodes to a 3-byte U+FFFD
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(big, b""))

    envelope = wl._cmd_exec(payload, tmp_path)
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    parsed = worker_identity._parse_envelope(raw, worker_identity.EXEC_ENVELOPE_CAP_BYTES)
    assert parsed is not None, f"envelope of {len(raw)} bytes exceeds the exec cap"
    assert envelope["stdout"] != ""
    assert len(raw) >= wl.EXEC_ENVELOPE_CAP_BYTES - _FIT_NONCOLLAPSE_FLOOR_BYTES


def test_exec_envelope_fits_the_cap_with_1mib_of_0x01_on_stdout(tmp_path, monkeypatch):
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile)
    big = b"\x01" * wl.EXEC_STREAM_CAP_BYTES
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(big, b""))

    envelope = wl._cmd_exec(payload, tmp_path)
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    parsed = worker_identity._parse_envelope(raw, worker_identity.EXEC_ENVELOPE_CAP_BYTES)
    assert parsed is not None, f"envelope of {len(raw)} bytes exceeds the exec cap"
    assert envelope["stdout"] != ""
    assert len(raw) >= wl.EXEC_ENVELOPE_CAP_BYTES - _FIT_NONCOLLAPSE_FLOOR_BYTES


def test_exec_envelope_fits_the_cap_with_over_1mib_of_plain_lines_on_both_streams(
    tmp_path, monkeypatch
):
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    payload = _base_payload(repo, profile)
    line = b"x" * 79 + b"\n"
    lines = line * ((wl.EXEC_STREAM_CAP_BYTES // len(line)) + 100)  # > 1 MiB
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(lines, lines))

    envelope = wl._cmd_exec(payload, tmp_path)
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    parsed = worker_identity._parse_envelope(raw, worker_identity.EXEC_ENVELOPE_CAP_BYTES)
    assert parsed is not None, f"envelope of {len(raw)} bytes exceeds the exec cap"


def test_run_envelope_fits_the_cap_with_over_16mib_of_json_lines(tmp_path, monkeypatch):
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(repo, profile)
    line = (json.dumps({"ok": True, "pad": "x" * 60}) + "\n").encode("utf-8")
    lines = line * ((wl.RUN_STDOUT_CAP_BYTES // len(line)) + 100)  # > 16 MiB
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(lines, b""))

    envelope = wl.run(payload, tmp_path)
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    parsed = worker_identity._parse_envelope(raw, worker_identity.RUN_ENVELOPE_CAP_BYTES)
    assert parsed is not None, f"envelope of {len(raw)} bytes exceeds the run cap"


def test_run_stdout_truncated_result_true_when_fitting_shrinks_the_cap(tmp_path, monkeypatch):
    """Round-2 item 3: a 15 MiB final line of '"' characters is under the nominal 16 MiB
    stdout cap raw, so the OLD check (final line vs. RUN_STDOUT_CAP_BYTES) read False.
    But each '"' doubles under JSON escaping, so the serialized stdout alone blows past
    the run envelope's budget and _fit_envelope shrinks stdout's cap well below 15 MiB
    -- stdout_truncated_result must track THAT shrunk cap, not the nominal one."""
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(repo, profile)
    final_line = b'"' * (15 * 1024 * 1024) + b"\n"
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(final_line, b""))

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["stdout_truncated_result"] is True


def test_run_stdout_truncated_result_false_for_small_result_line_after_big_filler(
    tmp_path, monkeypatch
):
    """The mirror case: well over 16 MiB of earlier filler is truncated from the front
    (tail-keep), but the small final result line survives whole -- stdout_truncated_result
    must read False, since claude's result JSON on that final line was not cut."""
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "worker.sb")
    payload = _base_payload(repo, profile)
    filler = b"x" * (wl.RUN_STDOUT_CAP_BYTES + 100)
    result_line = b'{"ok": true}'
    stdout_bytes = filler + b"\n" + result_line + b"\n"
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_with_output(stdout_bytes, b""))

    envelope = wl.run(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    assert envelope["stdout_truncated_result"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_exec_child_driven_cap_keeps_exact_tail_and_prefix(tmp_path):
    available, reason = _sandbox_probes_available()
    if not available:
        pytest.skip(f"sandbox-exec not usable in this process: {reason}")

    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = _permissive_profile(tmp_path / "verify.sb")
    total = wl.EXEC_STREAM_CAP_BYTES + 1000
    script = f"import sys\nsys.stdout.buffer.write(b'A' * {total})\n"
    payload = _base_payload(repo, profile, argv=[sys.executable, "-c", script])

    envelope = wl._cmd_exec(payload, tmp_path)

    assert envelope["status"] == "ok", envelope
    expected_truncated = total - wl.EXEC_STREAM_CAP_BYTES
    assert envelope["stdout"] == (
        f"[... {expected_truncated} bytes truncated ...]" + "A" * wl.EXEC_STREAM_CAP_BYTES
    )


# ---------------------------------------------------------------------------
# AC-5: the deadline kills the whole process group.
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_run_deadline_kills_child_and_grandchild(tmp_path, monkeypatch):
    # Bypasses the real profile/sandbox-exec composition entirely -- the deadline/kill
    # logic is generic to any spawned process group, and this way the test needs
    # neither macOS nor a working sandbox-exec to prove it. Driven with /bin/sh rather
    # than `sys.executable -c <script>`: the interpreter needs more from its own
    # startup environment (PYTHONHOME, LD_LIBRARY_PATH, ...) than the launcher's
    # payload-only child env guarantees, and on at least one CI runner that made the
    # interpreter exit before reaching time.sleep -- the child finished inside the
    # deadline and the envelope read "ok", never "deadline". /bin/sh needs nothing
    # beyond PATH, which the launcher always sets.
    monkeypatch.setattr(wl, "_check_profile", lambda payload: None)
    monkeypatch.setattr(wl, "_sandboxed_argv", lambda payload, argv: list(argv))

    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    pid_marker = tmp_path / "child.pid"
    grandchild_marker = tmp_path / "grandchild.pid"
    script = (
        f"echo $$ > {shlex.quote(str(pid_marker))}; "
        "sleep 300 & "
        f"echo $! > {shlex.quote(str(grandchild_marker))}; "
        "wait"
    )
    payload = _base_payload(
        repo, tmp_path / "unused.sb", argv=["/bin/sh", "-c", script], deadline_s=2.0
    )

    started = time.monotonic()
    envelope = wl.run(payload, tmp_path)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0
    assert envelope["status"] == "deadline"
    assert envelope["child_rc"] is None

    # The child really started (and really spawned its grandchild) -- a child that
    # exited before writing its marker would leave these files absent, turning a
    # silent early-exit into a loud assertion failure instead of a false "deadline".
    assert pid_marker.exists(), "child never started: no pid marker was written"
    assert grandchild_marker.exists(), "child never spawned its grandchild"
    child_pid = int(pid_marker.read_text())
    grandchild_pid = int(grandchild_marker.read_text())
    assert not _pid_alive(child_pid)
    assert not _pid_alive(grandchild_pid)


def test_run_exit_code_is_always_zero_even_on_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "_check_profile", lambda payload: None)
    monkeypatch.setattr(wl, "_sandboxed_argv", lambda payload, argv: list(argv))
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    pid_marker = tmp_path / "child.pid"
    script = f"echo $$ > {shlex.quote(str(pid_marker))}; sleep 300"
    payload = _base_payload(
        repo, tmp_path / "unused.sb", argv=["/bin/sh", "-c", script], deadline_s=0.5,
    )

    stdin = io.BytesIO(json.dumps(payload).encode("utf-8"))
    stdout = io.StringIO()

    rc = wl.main(["run", str(tmp_path)], stdin, stdout)

    assert rc == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["status"] == "deadline"
    assert pid_marker.exists(), "child never started: no pid marker was written"
