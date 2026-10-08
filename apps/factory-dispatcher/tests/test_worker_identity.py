"""PARTB-B1: worker_identity.py's pure core. No test here touches a real account, a real
sudo, or a real chmod ACL grant -- AC-6's single real-subprocess test proves only the
dispatcher-side timeout wiring, through a fixture shell script run as the test's own uid.
Cross-uid facts are owed to the attended probe and the canaries (design §10), never to a test.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import containment
import dispatch
import file_task
import process_env
import worker_identity as wi

DISPATCHER_DIR = Path(__file__).resolve().parents[1]

#: Fixture credential values: >=16 chars (the carrier floor), distinct.
WK = "wk-write-key-0123456789abcdef"
RK = "rk-read-key-fedcba9876543210"


# --- AC-1: config, or None ---------------------------------------------------


def test_load_config_returns_none_when_unset():
    assert wi.load_config({}) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_load_config_raises_on_blank(blank):
    with pytest.raises(ValueError):
        wi.load_config({"FACTORY_WORKER_USER": blank})


def test_load_config_returns_values_when_set():
    cfg = wi.load_config({"FACTORY_WORKER_USER": "_factoryworker"})
    assert cfg.user == "_factoryworker"
    assert cfg.runs_root == Path("/Users/Shared/factory-runs")
    assert cfg.launcher_path == Path("/Users/Shared/factory/bin/factory-worker-launch")
    assert cfg.process_env_path == Path("/Users/Shared/factory/bin/process_env.py")


def test_launcher_constant_matches_sudoers_rule():
    assert wi.LAUNCHER_PATH == "/Users/Shared/factory/bin/factory-worker-launch"


# --- AC-2: the golden launch call --------------------------------------------


def _cfg():
    return wi.WorkerIdentityConfig(user="_factoryworker")


def test_golden_launch_call_run():
    call = wi.build_launch_call(_cfg(), "run", Path("/Users/Shared/factory-runs/w1"), b"{}", 60.0)
    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "run",
        "/Users/Shared/factory-runs/w1",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert call.input == b"{}"
    assert call.timeout == 60.0 + 120.0


def test_golden_launch_call_exec():
    call = wi.build_launch_call(_cfg(), "exec", Path("/w2"), b'{"a":1}', 10.0)
    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "exec", "/w2",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert call.input == b'{"a":1}'
    assert call.timeout == 10.0 + 120.0


def test_build_launch_call_rejects_unknown_subcommand():
    with pytest.raises(ValueError):
        wi.build_launch_call(_cfg(), "nope", Path("/w"), b"{}", 1.0)


def test_build_launch_call_seams_are_overridable_without_touching_constants():
    call = wi.build_launch_call(
        _cfg(), "run", Path("/w"), b"{}", 1.0, sudo_path="/tmp/shim", margin_s=5.0
    )
    assert call.argv[0] == "/tmp/shim"
    assert call.timeout == 6.0
    # constants themselves are untouched
    assert wi.SUDO_PATH == "/usr/bin/sudo"
    assert wi.LAUNCH_TIMEOUT_MARGIN_S == 120.0


# --- AC-3/AC-4: payloads -----------------------------------------------------


def _base_env(**extra):
    env = {
        "SUBSTRATE_API_KEY": WK,
        "SUBSTRATE_READ_API_KEY": RK,
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-value",
        "SUBSTRATE_URL": "https://substrate.example.test",
    }
    env.update(extra)
    return env


def test_run_payload_keys_allowlist_is_exactly_the_four():
    assert wi.RUN_PAYLOAD_KEYS == (
        "CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY", "SUBSTRATE_URL", "DISABLE_AUTOUPDATER",
    )


def test_run_payload_has_exactly_the_four_keys_and_rk():
    extra_env = dispatch.WORKER_REGISTRY["claude"].extra_env
    payload = wi.build_run_payload(_base_env(), extra_env)
    assert set(payload) == set(wi.RUN_PAYLOAD_KEYS)
    assert payload["SUBSTRATE_API_KEY"] == RK
    assert payload["DISABLE_AUTOUPDATER"] == "1"  # from the real extra_env, dispatch.py:563


def test_run_payload_drops_non_allowlisted_keys():
    extra_env = dispatch.WORKER_REGISTRY["claude"].extra_env
    base_env = _base_env(PATH="/usr/bin:/bin", HOME="/Users/someone", SOME_FLAG="1")
    payload = wi.build_run_payload(base_env, extra_env)
    assert set(payload) == set(wi.RUN_PAYLOAD_KEYS)


@pytest.mark.parametrize(
    "planted",
    [
        {"SOME_OTHER_VAR": WK},
        {"PADDED_VAR": f"  {WK}  "},
        {"URL_VAR": f"https://example.test/{WK}/path"},
    ],
)
def test_write_key_never_reaches_the_run_payload_under_any_disguise(planted):
    extra_env = dispatch.WORKER_REGISTRY["claude"].extra_env
    payload = wi.build_run_payload(_base_env(**planted), extra_env)
    payload_bytes = json.dumps(payload).encode()
    assert WK not in payload_bytes.decode()
    call = wi.build_launch_call(_cfg(), "run", Path("/w"), payload_bytes, 60.0)
    assert WK not in call.input.decode()
    assert all(WK not in part for part in call.argv)


def test_run_payload_refusal_is_identical_to_the_non_split_paths_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", WK)
    monkeypatch.delenv("SUBSTRATE_READ_API_KEY", raising=False)
    executable = shutil.which("true") or sys.executable

    with pytest.raises(dispatch.DispatchEnvironmentError) as non_split:
        dispatch.run_worker("prompt", tmp_path / "clone", 1, (executable,))

    recorded: list = []
    with pytest.raises(dispatch.DispatchEnvironmentError) as split:
        wi.build_run_payload(dict(os.environ), ())
        recorded.append("unreachable")  # build_run_payload raises before this

    assert str(split.value) == str(non_split.value)
    assert recorded == []  # the injected recorder saw nothing: no launch call was built


def test_build_payload_refuses_a_credential_named_field():
    with pytest.raises(ValueError):
        wi.build_payload("exec", {"SOME_TOKEN": "x"})


def test_build_payload_passes_through_benign_fields_for_every_non_run_subcommand():
    for subcommand in sorted(wi.SUBCOMMANDS - {"run"}):
        assert wi.build_payload(subcommand, {"command": "echo hi"}) == {"command": "echo hi"}


def test_build_payload_refuses_run():
    with pytest.raises(ValueError):
        wi.build_payload("run", {})


# --- AC-5: the exit mapping ---------------------------------------------------


def _envelope(status, child_rc=None, stdout="", stderr="", truncated=False):
    doc = {"status": status, "stdout": stdout, "stderr": stderr}
    if child_rc is not None:
        doc["child_rc"] = child_rc
    if truncated:
        doc["stdout_truncated_result"] = True
    return json.dumps(doc).encode()


def test_cap_constants_match_design_3_3():
    assert wi.EXEC_ENVELOPE_CAP_BYTES == 2 * 1024 * 1024 + 4096
    assert wi.RUN_ENVELOPE_CAP_BYTES == 16 * 1024 * 1024 + 1024 * 1024 + 4096


# row (a): envelope status "deadline"
def test_row_a_deadline_run():
    env = _envelope("deadline", stdout="partial", stderr="oops")
    result = wi.map_exit("run", env, b"", None, False, 1.5)
    assert isinstance(result, dispatch.WorkerResult)
    assert result.exit_code == -1 and result.timed_out is True
    assert result.stdout == "partial" and result.stderr == "oops"


def test_row_a_deadline_exec():
    env = _envelope("deadline", stdout="p", stderr="e")
    result = wi.map_exit("exec", env, b"", None, False, 2.0, command="cmd", timeout_s=30)
    assert isinstance(result, dispatch.VerificationCommandResult)
    assert result.outcome == "failed" and result.reason == "timed out after 30s"


# row (b): the dispatcher's own timeout fired
def test_row_b_dispatcher_timeout_run():
    result = wi.map_exit("run", b"partial-out", b"partial-err", None, True, 9.0)
    assert isinstance(result, dispatch.WorkerResult)
    assert result.exit_code == -1 and result.timed_out is True
    assert result.stdout == "partial-out" and result.stderr == "partial-err"


def test_row_b_dispatcher_timeout_exec():
    result = wi.map_exit(
        "exec", b"", b"", None, True, 9.0, command="cmd", timeout_s=12
    )
    assert result.outcome == "failed" and result.reason == "timed out after 12s"


# row (c): no envelope / malformed / over cap -- always charged, run exit_code never 0
def test_row_c_no_envelope_keeps_nonzero_exit_code_run():
    result = wi.map_exit("run", b"not json", b"", 5, False, 1.0)
    assert result.exit_code == 5 and result.timed_out is False


def test_row_c_zero_exit_code_becomes_one_for_run():
    result = wi.map_exit("run", b"not json", b"", 0, False, 1.0)
    assert result.exit_code == 1


def test_row_c_exit_code_124_without_envelope_is_not_a_timeout():
    result = wi.map_exit("run", b"garbage", b"", 124, False, 1.0)
    assert result.timed_out is False
    assert result.exit_code == 124


def test_row_c_exit_code_125_without_envelope_is_not_a_timeout():
    result = wi.map_exit("run", b"garbage", b"", 125, False, 1.0)
    assert result.timed_out is False


def test_row_c_malformed_json_exec():
    result = wi.map_exit(
        "exec", b"{not json", b"", 3, False, 1.0, command="cmd", timeout_s=5
    )
    assert result.outcome == "failed" and result.exit_code == 3


def test_row_c_missing_required_field_is_malformed():
    bad = json.dumps({"status": "ok", "stdout": "x"}).encode()  # no child_rc
    result = wi.map_exit("run", bad, b"", 2, False, 1.0)
    assert result.exit_code == 2  # charged, not graded as "ok"


def test_row_c_over_cap_envelope(monkeypatch):
    monkeypatch.setattr(wi, "EXEC_ENVELOPE_CAP_BYTES", 10)
    env = _envelope("ok", child_rc=0, stdout="this is definitely over ten bytes")
    result = wi.map_exit(
        "exec", env, b"", 1, False, 1.0, command="cmd", timeout_s=5
    )
    assert result.outcome == "failed" and result.exit_code == 1


# row (d): envelope status "refused"
def test_row_d_refused_run_raises_uncharged():
    env = _envelope("refused", stdout="", stderr="no capacity")
    with pytest.raises(dispatch.DispatchEnvironmentError, match="no capacity"):
        wi.map_exit("run", env, b"", None, False, 1.0)


def test_row_d_refused_exec_is_could_not_start():
    env = _envelope("refused", stdout="", stderr="no capacity")
    result = wi.map_exit(
        "exec", env, b"", None, False, 1.0, command="cmd", timeout_s=5
    )
    assert result.outcome == "could_not_start"


# row (e): sudo's own stderr
def test_row_e_sudo_prefix_run_raises():
    with pytest.raises(dispatch.DispatchEnvironmentError, match="sudo: a password is required"):
        wi.map_exit("run", b"", b"sudo: a password is required", 1, False, 1.0)


def test_row_e_sudo_prefix_exec_is_could_not_start():
    result = wi.map_exit(
        "exec", b"", b"sudo: a password is required", 1, False, 1.0,
        command="cmd", timeout_s=5,
    )
    assert result.outcome == "could_not_start"


# row (f): envelope status "ok"
def test_row_f_ok_run_plain_grading():
    env = _envelope("ok", child_rc=7, stdout="hello", stderr="oops")
    result = wi.map_exit("run", env, b"", 0, False, 3.0, grade_json_result=False)
    assert result.exit_code == 7 and result.stdout == "hellooops" and result.stderr == "oops"


def test_row_f_ok_run_graded_json_success():
    doc = {
        "is_error": False, "total_cost_usd": 1.25, "result": "done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    env = _envelope("ok", child_rc=0, stdout=json.dumps(doc), stderr="")
    result = wi.map_exit("run", env, b"", 0, False, 3.0, grade_json_result=True)
    assert result.exit_code == 0 and result.stdout == "done"
    assert result.cost_usd == 1.25 and result.tokens == 15


def test_row_f_ok_run_graded_json_malformed_is_charged():
    env = _envelope("ok", child_rc=0, stdout="not json", stderr="")
    result = wi.map_exit("run", env, b"", 0, False, 3.0, grade_json_result=True)
    assert result.exit_code == 1  # never 0: the process exited 0 but emitted no result JSON


def test_row_f_truncated_result_is_a_charged_failure():
    env = _envelope("ok", child_rc=0, stdout="partial resu", truncated=True)
    result = wi.map_exit("run", env, b"", 0, False, 3.0)
    assert result.exit_code != 0 and result.timed_out is False


def test_row_f_ok_exec_passed():
    env = _envelope("ok", child_rc=0, stdout="all good")
    result = wi.map_exit("exec", env, b"", 0, False, 1.0, command="cmd", timeout_s=5)
    assert result.outcome == "passed" and result.exit_code == 0


def test_row_f_ok_exec_failed():
    env = _envelope("ok", child_rc=3, stdout="bad")
    result = wi.map_exit("exec", env, b"", 3, False, 1.0, command="cmd", timeout_s=5)
    assert result.outcome == "failed" and result.exit_code == 3


def test_row_f_ok_exec_could_not_start_via_126():
    env = _envelope("ok", child_rc=126, stderr="not found")
    result = wi.map_exit("exec", env, b"", 126, False, 1.0, command="cmd", timeout_s=5)
    assert result.outcome == "could_not_start" and result.reason == "not found"


def test_row_f_ok_exec_could_not_start_via_sandbox_exec_stderr():
    env = _envelope("ok", child_rc=65, stderr="sandbox-exec: failed to exec")
    result = wi.map_exit("exec", env, b"", 65, False, 1.0, command="cmd", timeout_s=5)
    assert result.outcome == "could_not_start"


def test_map_exit_rejects_unknown_kind():
    with pytest.raises(ValueError):
        wi.map_exit("bogus", b"", b"", 0, False, 1.0)


def test_map_exit_exec_requires_command_and_timeout():
    with pytest.raises(ValueError):
        wi.map_exit("exec", b"", b"", 0, False, 1.0)


# --- AC-6: a real timeout through the seams -----------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="posix shell script")
def test_real_timeout_through_run_launch_call_fires_dispatcher_side(tmp_path):
    script = tmp_path / "sleepy.sh"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)

    cfg = wi.WorkerIdentityConfig(user="nobody")
    call = wi.build_launch_call(
        cfg, "exec", tmp_path, b"{}", deadline_s=0.2, sudo_path=str(script), margin_s=1.0
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        wi.run_launch_call(call)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0
    stdout = excinfo.value.stdout or b""
    stderr = excinfo.value.stderr or b""
    result = wi.map_exit(
        "exec", stdout, stderr, None, True, elapsed, command="cmd", timeout_s=call.timeout
    )
    assert result.outcome == "failed"
    assert result.reason == f"timed out after {call.timeout:.0f}s"


# --- AC-7: the injectable ACL runner ------------------------------------------


@pytest.mark.parametrize("permissions", wi.ALLOWED_ACL_PERMISSIONS)
def test_run_acl_add_exact_argv(permissions, tmp_path):
    recorded = []
    path = tmp_path / "ptree"
    wi.run_acl_add(
        "_factoryworker", permissions, path,
        runner=lambda argv: recorded.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    assert recorded == [("chmod", "+a", f"_factoryworker allow {permissions}", str(path))]


@pytest.mark.parametrize("permissions", wi.ALLOWED_ACL_PERMISSIONS)
def test_run_acl_remove_exact_argv(permissions, tmp_path):
    recorded = []
    path = tmp_path / "ptree"
    wi.run_acl_remove(
        "_factoryworker", permissions, path,
        runner=lambda argv: recorded.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    assert recorded == [("chmod", "-a", f"_factoryworker allow {permissions}", str(path))]


def test_acl_rejects_a_permission_set_outside_the_design(tmp_path):
    with pytest.raises(ValueError):
        wi.run_acl_add("_factoryworker", "execute", tmp_path, runner=lambda argv: None)


# --- AC-8: import-time hashes --------------------------------------------------


def test_sha256_or_none_on_a_real_tmp_file(tmp_path):
    path = tmp_path / "f.py"
    path.write_bytes(b"hello")
    assert wi.sha256_or_none(path) == hashlib.sha256(b"hello").hexdigest()


def test_sha256_or_none_missing_file(tmp_path):
    assert wi.sha256_or_none(tmp_path / "missing.py") is None


def test_compare_hash_match():
    data = b"content"
    source_hash = hashlib.sha256(data).hexdigest()
    assert wi.compare_hash(source_hash, data) == "match"


def test_compare_hash_mismatch():
    source_hash = hashlib.sha256(b"one").hexdigest()
    assert wi.compare_hash(source_hash, b"two") == "mismatch"


def test_compare_hash_missing_installed_copy():
    source_hash = hashlib.sha256(b"one").hexdigest()
    assert wi.compare_hash(source_hash, None) == "missing"


def test_compare_hash_missing_source():
    assert wi.compare_hash(None, b"anything") == "missing"


def test_worker_launcher_source_hash_is_recorded_now_that_b2a_exists():
    # PARTB-B2a added worker_launcher.py; the hash recorded at this module's own import
    # time must equal the sha256 of that file's current bytes on disk.
    launcher_path = DISPATCHER_DIR / "worker_launcher.py"
    assert launcher_path.exists()
    assert wi.WORKER_LAUNCHER_SOURCE_HASH == hashlib.sha256(launcher_path.read_bytes()).hexdigest()


def test_process_env_source_hash_is_recorded():
    assert wi.PROCESS_ENV_SOURCE_HASH is not None


def test_source_hashes_returns_recorded_values_without_reread(monkeypatch):
    def _boom(self, *a, **k):
        raise AssertionError("source_hashes must not re-read any file")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert wi.source_hashes() == (wi.WORKER_LAUNCHER_SOURCE_HASH, wi.PROCESS_ENV_SOURCE_HASH)


def test_compare_installed_hashes_reports_process_env_match():
    real_bytes = (DISPATCHER_DIR / "process_env.py").read_bytes()
    report = wi.compare_installed_hashes(None, real_bytes)
    assert report == {"worker_launcher.py": "missing", "process_env.py": "match"}


# --- AC-9/AC-10: boundary + no caller yet (besides PARTB-B3's launchd_agent.py) -----


def test_worker_identity_is_on_boundary_paths():
    assert file_task._first_boundary_intersection(
        ["apps/factory-dispatcher/worker_identity.py"]
    )


#: PARTB-B3 (launchd_agent.py's install-worker-identity/probe-worker-identity) is the
#: first and, until B4/B5 land, only non-test caller: design §8 row B3 builds directly on
#: B1's config/launch-call/payload/hash-comparison functions. dispatch_steps.py and the
#: split run path stay untouched (B4/B5), so the exception list below names exactly one
#: file, never a glob.
_MODULES_ALLOWED_TO_IMPORT_WORKER_IDENTITY = ("launchd_agent.py",)


def test_no_non_test_module_other_than_launchd_agent_imports_worker_identity_yet():
    offenders = []
    for path in sorted(DISPATCHER_DIR.rglob("*.py")):
        rel = path.relative_to(DISPATCHER_DIR)
        if rel.parts[0] == "tests" or path.name.startswith("test_"):
            continue
        if path.name == "worker_identity.py":
            continue
        if path.name in _MODULES_ALLOWED_TO_IMPORT_WORKER_IDENTITY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "worker_identity" for alias in node.names
            ):
                offenders.append(str(rel))
            if isinstance(node, ast.ImportFrom) and node.module == "worker_identity":
                offenders.append(str(rel))
    assert offenders == []


def test_containment_dispatch_process_env_are_unchanged_imports():
    # Sanity: this module imports them, never the reverse (no cycle).
    assert "worker_identity" not in dir(containment)
    assert "worker_identity" not in dir(dispatch)
    assert "worker_identity" not in dir(process_env)
