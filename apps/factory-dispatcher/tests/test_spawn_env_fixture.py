"""AC-5 (SEC-a0166920-2): no child spawned by this lane receives the write key.

One case per function that contains a spawn site -- the population named by
`spawn_scan.find_spawn_calls` (the AST-visible set, "list A") plus the two spawn
sites that scan is structurally blind to: `TunnelKeeper._spawn_link`'s injected
`Popen` default, and `merge_on_verdict.run_merge_script`, which spawns through
`dispatch.run`. `subprocess.run`/`subprocess.Popen` are replaced as attributes of
the shared `subprocess` module -- every call site in this lane resolves them at
call time via a plain `import subprocess`, so one patch covers all of them.

Every case lives once, as an entry in ``CASES`` below: a (case_key, function)
pair where ``function`` performs the one action that case needs. The
per-function tests below call their own entry directly, so a failure names the
right case. `test_every_spawn_bearing_function_has_a_case` and
`test_dispatcher_own_environment_is_never_mutated` do not restate that
population by hand -- they iterate ``CASES`` and RUN it themselves, so deleting
an entry (or a regression that pops a name out of ``os.environ``) is something
those two tests discover by actually exercising the shrunken/broken set, not
by comparing against a list that could silently go stale.

The decision-D third exception (a `worker_launcher.py` `run` subcommand) is
decided from that file's EXISTENCE, never a flag: see
`test_worker_launcher_third_exception_decided_by_file_existence`. It is inert
today because the file does not exist in this checkout.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_health  # noqa: E402
import containment  # noqa: E402
import deployed_revision  # noqa: E402
import dispatch  # noqa: E402
import handoff  # noqa: E402
import launchd_agent  # noqa: E402
import probe_console_surface  # noqa: E402
import process_env  # noqa: E402
import scanner  # noqa: E402
import tunnel_keeper  # noqa: E402
import worker_identity  # noqa: E402
import workflow_run_health  # noqa: E402

if (_DISPATCHER_ROOT / "worker_launcher.py").exists():
    import worker_launcher  # noqa: E402
from activities import ea_observation  # noqa: E402
from activities import merge_on_verdict as mov  # noqa: E402
from spawn_scan import find_spawn_calls  # noqa: E402

W = "write-key-fixture-aaaaaaaaaaaaaaaa"
R = "read-key-fixture-bbbbbbbbbbbbbbbbb"
OAUTH = "oauth-token-fixture-cccccccccccccc"
DISCORD = "https://discord.example.invalid/webhook-fixture-dddddddddddddd"

CaseKey = tuple[str, str]
CaseFunc = Callable[["SpawnRecorder", Path, pytest.MonkeyPatch], None]

# AC-5's exactly-two exceptions, plus the third (decision D), added below only
# when worker_launcher.py exists -- never restated as a standing hand list.
_ALLOWED_CREDENTIALS: dict[CaseKey, set[str]] = {
    ("dispatch.py", "run_worker"): {"CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY"},
    ("activities/merge_on_verdict.py", "run_merge_script"): {"DISCORD_WEBHOOK_URL"},
}

_WORKER_LAUNCHER_PATH = _DISPATCHER_ROOT / "worker_launcher.py"
_WORKER_LAUNCHER_RUN_CASE: CaseKey = ("worker_launcher.py", "run")

if _WORKER_LAUNCHER_PATH.exists():
    # Decision D's third exception, decided from the file's existence: once
    # worker_launcher.py lands, its `run` subcommand's child env is allowed
    # the same two names run_worker's is. test_worker_launcher_third_exception
    # _decided_by_file_existence below fails loudly until a CASES entry for
    # _WORKER_LAUNCHER_RUN_CASE actually exercises it.
    _ALLOWED_CREDENTIALS[_WORKER_LAUNCHER_RUN_CASE] = {
        "CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY",
    }
    # AC-11: git's own GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> env-var convention
    # names the git config KEY, which process_env.is_credential_name's generic
    # KEY|TOKEN|SECRET|PASSWORD pattern cannot tell apart from a credential name by
    # spelling alone -- their VALUES (core.hooksPath, core.fsmonitor, gc.auto,
    # maintenance.auto) are never sensitive. Every launcher git spawn sets exactly
    # these four names via _git_env, never the payload's own, so allow-listing them
    # here does not relax the no-leak check for anything a payload could supply.
    _GIT_CONFIG_KEY_NAMES = {f"GIT_CONFIG_KEY_{i}" for i in range(4)}
    _ALLOWED_CREDENTIALS[("worker_launcher.py", "_seed_vseed_core")] = _GIT_CONFIG_KEY_NAMES
    _ALLOWED_CREDENTIALS[("worker_launcher.py", "_cmd_export")] = _GIT_CONFIG_KEY_NAMES

# The two spawn sites invisible to spawn_scan's AST walk (per its own docstring
# and tests/test_spawn_inventory.py): TunnelKeeper's injected Popen default, and
# run_merge_script, which spawns through the local dispatch.run wrapper rather
# than a literal subprocess.* call.
_EXTRA_CASES = {
    ("tunnel_keeper.py", "TunnelKeeper._spawn_link"),
    ("activities/merge_on_verdict.py", "run_merge_script"),
}

_RUN_ONLY_KWARGS = {"input", "capture_output", "timeout", "check"}


def _popen_signature() -> inspect.Signature:
    return inspect.signature(subprocess.Popen)


def _assert_valid_popen_like_call(args, kwargs, *, strip_run_only: bool) -> None:
    filtered = {k: v for k, v in kwargs.items() if not (strip_run_only and k in _RUN_ONLY_KWARGS)}
    _popen_signature().bind(*args, **filtered)


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakePopen:
    def __init__(self):
        self.pid = 99999
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class SpawnRecorder:
    def __init__(self):
        self.calls: list[dict] = []

    def _record(self, args, kwargs):
        argv = args[0] if args else kwargs.get("args")
        env = kwargs.get("env")
        env = dict(os.environ) if env is None else dict(env)
        self.calls.append({"argv": list(argv), "env": env})

    def fake_run(self, *args, **kwargs):
        _assert_valid_popen_like_call(args, kwargs, strip_run_only=True)
        self._record(args, kwargs)
        return FakeCompletedProcess(returncode=0, stdout="[]", stderr="")

    def fake_popen(self, *args, **kwargs):
        _assert_valid_popen_like_call(args, kwargs, strip_run_only=False)
        self._record(args, kwargs)
        return FakePopen()


@pytest.fixture
def recorder(monkeypatch):
    rec = SpawnRecorder()
    monkeypatch.setattr(subprocess, "run", rec.fake_run)
    monkeypatch.setattr(subprocess, "Popen", rec.fake_popen)
    return rec


@pytest.fixture
def credential_fixture(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", W)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", R)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD)
    monkeypatch.setenv("ALIAS_OF_W", W)
    monkeypatch.setenv("AUTH_HEADER", "Bearer " + W)
    return {"W": W, "R": R, "OAUTH": OAUTH, "DISCORD": DISCORD}


def _assert_no_leak(case_key, env: dict[str, str]) -> None:
    allowed = _ALLOWED_CREDENTIALS.get(case_key, set())
    for name, value in env.items():
        if process_env.is_credential_name(name):
            assert name in allowed, f"{case_key}: unexpected credential name {name!r} in env"
    values = list(env.values())
    assert all(W not in v for v in values), f"{case_key}: write key leaked into env"
    if "DISCORD_WEBHOOK_URL" not in allowed:
        assert all(DISCORD not in v for v in values), f"{case_key}: discord webhook leaked into env"


# ---------------------------------------------------------------------------
# One case function per function containing a spawn site. Each performs ONLY
# the action; assertions live in the per-function test that also owns it.
# These are the single source of truth CASES below points at -- there is no
# second, independently maintained list of "what AC-5 covers".
# ---------------------------------------------------------------------------


def _case_ea_observation_current_kubectl_context(recorder, tmp_path, monkeypatch):
    monkeypatch.delenv(ea_observation.DEFAULT_CLUSTER_ENV, raising=False)
    ea_observation._current_kubectl_context()


def _case_cluster_health_kubectl_json(recorder, tmp_path, monkeypatch):
    cluster_health._kubectl_json("pods")


def _case_deployed_revision_default_git_runner(recorder, tmp_path, monkeypatch):
    deployed_revision._default_git_runner(["git", "--version"], cwd=tmp_path, check=False)


def _case_deployed_revision_default_kubectl_health_probe(recorder, tmp_path, monkeypatch):
    deployed_revision.default_kubectl_health_probe("ns", "deploy")


def _case_dispatch_run(recorder, tmp_path, monkeypatch):
    dispatch.run(["git", "--version"], check=False)


def _case_dispatch_run_worker(recorder, tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda *_a, **_k: "/usr/bin/true")
    dispatch.run_worker("prompt", tmp_path, 1, ("codex", "exec"))


def _case_dispatch_run_verification_shell(recorder, tmp_path, monkeypatch):
    env = dispatch._verification_env(tmp_path)
    dispatch.run_verification_shell(tmp_path, "true", env, timeout=5)


def _case_handoff_run_git(recorder, tmp_path, monkeypatch):
    handoff._run_git(tmp_path, ["status", "--short"])


def _case_launchd_agent_validate_worker_dependencies(recorder, tmp_path, monkeypatch):
    config = launchd_agent.LaunchdConfig()
    try:
        launchd_agent.validate_worker_dependencies(config, values=dict(os.environ))
    except launchd_agent.ConfigError:
        pass


def _case_launchd_agent_run_launchctl(recorder, tmp_path, monkeypatch):
    launchd_agent.run_launchctl(["list"], check=False)


def _case_launchd_agent_default_kubeconfig_has_context(recorder, tmp_path, monkeypatch):
    launchd_agent.default_kubeconfig_has_context()


def _case_launchd_agent_launchctl_print(recorder, tmp_path, monkeypatch):
    launchd_agent.launchctl_print("gui/501/some.label")


def _case_probe_console_surface_git_revision(recorder, tmp_path, monkeypatch):
    probe_console_surface.git_revision(tmp_path)


def _case_scanner_last_changed(recorder, tmp_path, monkeypatch):
    scanner.last_changed(("a.py",), repo_root=tmp_path)


def _case_scanner_last_changed_revision(recorder, tmp_path, monkeypatch):
    scanner.last_changed_revision(("a.py",), repo_root=tmp_path)


def _case_scanner_revision_exists(recorder, tmp_path, monkeypatch):
    scanner.revision_exists("deadbeef", repo_root=tmp_path)


def _case_scanner_is_ancestor(recorder, tmp_path, monkeypatch):
    scanner.is_ancestor("a", "b", repo_root=tmp_path)


def _case_scanner_git_mtime(recorder, tmp_path, monkeypatch):
    scanner.git_mtime(tmp_path / "a.py", repo_root=tmp_path)


def _case_scanner_merged_commit_subjects(recorder, tmp_path, monkeypatch):
    scanner.merged_commit_subjects(repo_root=tmp_path)


def _case_scanner_merged_main_tasks_basenames(recorder, tmp_path, monkeypatch):
    scanner.merged_main_tasks_basenames(repo_root=tmp_path)


def _case_workflow_run_health_gh_workflow_names(recorder, tmp_path, monkeypatch):
    workflow_run_health._gh_workflow_names(repo="owner/repo")


def _case_workflow_run_health_gh_default_branch(recorder, tmp_path, monkeypatch):
    workflow_run_health._gh_default_branch(repo="owner/repo")


def _case_workflow_run_health_gh_run_list_json(recorder, tmp_path, monkeypatch):
    workflow_run_health._gh_run_list_json(repo="owner/repo", workflow="lint.yml", limit=5, branch="main")


def _case_tunnel_keeper_spawn_link(recorder, tmp_path, monkeypatch):
    link = launchd_agent.TunnelConfig(
        name="t1", kubectl_args=("get", "pods"), local_address="127.0.0.1:9999"
    )
    keeper = tunnel_keeper.TunnelKeeper(
        links=(link,), spawn=recorder.fake_popen, notifier=lambda _n: None
    )
    keeper._spawn_link(link)


def _case_merge_on_verdict_run_merge_script(recorder, tmp_path, monkeypatch):
    cfg = dispatch.Config(repo="grant/test-repo", remote="origin", base_ref="main", repo_root=tmp_path)
    mov.run_merge_script(7, cfg)


def _case_worker_identity_default_acl_runner(recorder, tmp_path, monkeypatch):
    worker_identity._default_acl_runner(("chmod", "+a", "_factoryworker allow search", str(tmp_path)))


def _case_worker_identity_run_launch_call(recorder, tmp_path, monkeypatch):
    cfg = worker_identity.WorkerIdentityConfig(user="_factoryworker")
    call = worker_identity.build_launch_call(cfg, "run", tmp_path, b"{}", 1.0)
    worker_identity.run_launch_call(call)


def _case_worker_launcher_run(recorder, tmp_path, monkeypatch):
    # worker_launcher.run's own Popen call is exercised directly (not through main()),
    # so the recorder sees the child env even though the fake Popen it gets back lacks
    # a real communicate() -- the resulting AttributeError is swallowed here, same as
    # launchd_agent's ConfigError above, because only the recorded call matters to AC-5.
    repo = tmp_path / "wtree" / "repo"
    repo.mkdir(parents=True)
    profile = tmp_path / "worker.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "argv": ["/usr/bin/true"],
        "cwd": str(repo),
        "env": {
            "CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
            "SUBSTRATE_API_KEY": R,
            "SUBSTRATE_URL": "https://substrate.example.invalid",
        },
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    try:
        worker_launcher.run(payload, tmp_path)
    except Exception:
        pass


def _case_worker_launcher_exec(recorder, tmp_path, monkeypatch):
    repo = tmp_path / "vtree" / "repo"
    repo.mkdir(parents=True)
    profile = tmp_path / "verify.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "argv": ["/usr/bin/true"],
        "cwd": str(repo),
        "env": {"PATH": "/usr/bin:/bin"},
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    try:
        worker_launcher._cmd_exec(payload, tmp_path)
    except Exception:
        pass


#: A tree name unused by _case_worker_launcher_run ("wtree") and _exec ("vtree"), and
#: every mkdir below uses exist_ok=True: these four cases run back-to-back sharing one
#: tmp_path in test_every_spawn_bearing_function_has_a_case, and must not collide.
_B2B_CASE_TREE = "ptree"


def _case_worker_launcher_seed(recorder, tmp_path, monkeypatch):
    # PARTB-B2b: `_seed_vseed_core` implements both the `seed` and `vseed`
    # subcommands' git steps. subprocess.run's FakeCompletedProcess always reports
    # rc=0, so every git step here "succeeds" without ever touching a real bundle or
    # repo on disk. The bundle must resolve under W (AC-12), so it is created there.
    tree_dir = tmp_path / _B2B_CASE_TREE
    tree_dir.mkdir(parents=True, exist_ok=True)
    bundle = tmp_path / "repo.bundle"
    bundle.write_bytes(b"")
    profile = tmp_path / "worker.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "bundle": str(bundle),
        "base_ref": "main",
        "tree": _B2B_CASE_TREE,
        "remote_url": "https://example.invalid/repo.git",
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    worker_launcher._cmd_seed(payload, tmp_path)


def _case_worker_launcher_materialize(recorder, tmp_path, monkeypatch):
    scripts = tmp_path / _B2B_CASE_TREE / "repo" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "materialize_agents.py").write_text("")
    profile = tmp_path / "worker.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "tree": _B2B_CASE_TREE,
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    try:
        worker_launcher._cmd_materialize(payload, tmp_path)
    except Exception:
        pass


def _case_worker_launcher_export(recorder, tmp_path, monkeypatch):
    repo = tmp_path / _B2B_CASE_TREE / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "worker.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "tree": _B2B_CASE_TREE,
        "base_ref": "main",
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    try:
        worker_launcher._cmd_export(payload, tmp_path)
    except Exception:
        pass


def _case_worker_launcher_venv(recorder, tmp_path, monkeypatch):
    # PARTB-B2b: `_cmd_venv` implements both the `venv` and `pip` subcommands; this
    # case drives it as `venv` would be dispatched (kind bound, never read from payload).
    (tmp_path / _B2B_CASE_TREE).mkdir(parents=True, exist_ok=True)
    profile = tmp_path / "worker.sb"
    profile.write_text("(version 1)\n(allow default)\n")
    payload = {
        "tree": _B2B_CASE_TREE,
        "profile": str(profile),
        "profile_owner_uid": os.getuid(),
        "deadline_s": 5.0,
    }
    try:
        worker_launcher._cmd_venv(payload, tmp_path, kind="venv")
    except Exception:
        pass


# The population AC-5 covers: list A (spawn_scan's AST-visible calls) plus the
# two AST-blind sites. This list IS the population -- `CASES`'s own keys are
# what `test_every_spawn_bearing_function_has_a_case` runs and records, so a
# case deleted here is a case missing there, not a list that could disagree
# with it.
CASES: list[tuple[CaseKey, CaseFunc]] = [
    (("activities/ea_observation.py", "_current_kubectl_context"), _case_ea_observation_current_kubectl_context),
    (("cluster_health.py", "_kubectl_json"), _case_cluster_health_kubectl_json),
    (("deployed_revision.py", "_default_git_runner"), _case_deployed_revision_default_git_runner),
    (("deployed_revision.py", "default_kubectl_health_probe"), _case_deployed_revision_default_kubectl_health_probe),
    (("dispatch.py", "run"), _case_dispatch_run),
    (("dispatch.py", "run_worker"), _case_dispatch_run_worker),
    (("dispatch.py", "run_verification_shell"), _case_dispatch_run_verification_shell),
    (("handoff.py", "_run_git"), _case_handoff_run_git),
    (("launchd_agent.py", "validate_worker_dependencies"), _case_launchd_agent_validate_worker_dependencies),
    (("launchd_agent.py", "run_launchctl"), _case_launchd_agent_run_launchctl),
    (("launchd_agent.py", "default_kubeconfig_has_context"), _case_launchd_agent_default_kubeconfig_has_context),
    (("launchd_agent.py", "launchctl_print"), _case_launchd_agent_launchctl_print),
    (("probe_console_surface.py", "git_revision"), _case_probe_console_surface_git_revision),
    (("scanner.py", "last_changed"), _case_scanner_last_changed),
    (("scanner.py", "last_changed_revision"), _case_scanner_last_changed_revision),
    (("scanner.py", "revision_exists"), _case_scanner_revision_exists),
    (("scanner.py", "is_ancestor"), _case_scanner_is_ancestor),
    (("scanner.py", "git_mtime"), _case_scanner_git_mtime),
    (("scanner.py", "merged_commit_subjects"), _case_scanner_merged_commit_subjects),
    (("scanner.py", "merged_main_tasks_basenames"), _case_scanner_merged_main_tasks_basenames),
    (("workflow_run_health.py", "_gh_workflow_names"), _case_workflow_run_health_gh_workflow_names),
    (("workflow_run_health.py", "_gh_default_branch"), _case_workflow_run_health_gh_default_branch),
    (("workflow_run_health.py", "_gh_run_list_json"), _case_workflow_run_health_gh_run_list_json),
    (("tunnel_keeper.py", "TunnelKeeper._spawn_link"), _case_tunnel_keeper_spawn_link),
    (("activities/merge_on_verdict.py", "run_merge_script"), _case_merge_on_verdict_run_merge_script),
    (("worker_identity.py", "_default_acl_runner"), _case_worker_identity_default_acl_runner),
    (("worker_identity.py", "run_launch_call"), _case_worker_identity_run_launch_call),
]

if _WORKER_LAUNCHER_PATH.exists():
    # Decided from the file's existence, same as the _ALLOWED_CREDENTIALS entry above:
    # these two are the spawn-bearing functions worker_launcher.py adds in this bead.
    CASES.append((("worker_launcher.py", "run"), _case_worker_launcher_run))
    # spawn_scan records the actual enclosing `def` name, not the subcommand string --
    # the exec subcommand is implemented by a function named `_cmd_exec`.
    CASES.append((("worker_launcher.py", "_cmd_exec"), _case_worker_launcher_exec))
    # PARTB-B2b: `_seed_vseed_core` implements both `seed` and `vseed`'s git steps
    # (the AC-1 separation checks live in their own thin `_cmd_seed`/`_cmd_vseed`
    # wrappers, which spawn nothing themselves); `_cmd_venv` implements both `venv`
    # and `pip` -- one CASES entry per implementing function, same as above.
    CASES.append((("worker_launcher.py", "_seed_vseed_core"), _case_worker_launcher_seed))
    CASES.append((("worker_launcher.py", "_cmd_materialize"), _case_worker_launcher_materialize))
    CASES.append((("worker_launcher.py", "_cmd_export"), _case_worker_launcher_export))
    CASES.append((("worker_launcher.py", "_cmd_venv"), _case_worker_launcher_venv))

_CASE_FUNCS: dict[CaseKey, CaseFunc] = dict(CASES)


# ---------------------------------------------------------------------------
# Per-function tests. Each drives its own CASES entry (never a second,
# divergent copy of the call) and checks what only it needs to check.
# ---------------------------------------------------------------------------


def test_ea_observation_current_kubectl_context(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("activities/ea_observation.py", "_current_kubectl_context")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_cluster_health_kubectl_json(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("cluster_health.py", "_kubectl_json")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_deployed_revision_default_git_runner(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("deployed_revision.py", "_default_git_runner")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_deployed_revision_default_kubectl_health_probe(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("deployed_revision.py", "default_kubectl_health_probe")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_dispatch_run(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("dispatch.py", "run")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_dispatch_pid_start_time(recorder, credential_fixture):
    # Not part of the AC-5 population: _pid_start_time's `ps` call goes
    # through dispatch.run (the "dispatch.py run" case above), so spawn_scan's
    # AST walk does not see a spawn call inside _pid_start_time itself. Kept
    # as its own test for coverage of this call site's leak behaviour.
    dispatch._pid_start_time(os.getpid())
    assert len(recorder.calls) == 1
    _assert_no_leak(("dispatch.py", "_pid_start_time"), recorder.calls[-1]["env"])


def test_dispatch_run_worker(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("dispatch.py", "run_worker")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, env)
    assert env["SUBSTRATE_API_KEY"] == R
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH


def test_dispatch_interpreter_version(recorder, credential_fixture):
    # Not part of the AC-5 population, for the same reason as
    # test_dispatch_pid_start_time above: interpreter_version shares
    # dispatch.run rather than calling subprocess directly.
    dispatch.interpreter_version(sys.executable)
    assert len(recorder.calls) == 1
    _assert_no_leak(("dispatch.py", "interpreter_version"), recorder.calls[-1]["env"])


def test_dispatch_run_verification_shell(recorder, credential_fixture, monkeypatch, tmp_path):
    """AC-1: run_verification_shell is one of the exempt sites -- an explicit
    env is honoured as given. Checking only that no credential leaks (the
    original shape of this test) cannot tell "honoured as given" apart from
    "happens to be credential-free some other way"; it also must equal what
    the base's own builder (_verification_env, plus the verify_tmp_env
    additions run_verification_shell itself contributes) returns.
    """
    case_key = ("dispatch.py", "run_verification_shell")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    actual_env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, actual_env)

    verify_tmp = tmp_path.resolve().parent / containment.VERIFY_TMPDIR_NAME
    expected_env = {**dispatch._verification_env(tmp_path), **containment.verify_tmp_env(verify_tmp)}
    assert actual_env == expected_env, (
        "run_verification_shell's child env must equal _verification_env(clone) "
        "plus verify_tmp_env, not merely be credential-free"
    )


def test_handoff_run_git(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("handoff.py", "_run_git")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_launchd_agent_validate_worker_dependencies(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("launchd_agent.py", "validate_worker_dependencies")
    # The subprocess call under test (the import smoke-check) always runs
    # first; validate_worker_executable_dependencies, called only after it
    # succeeds, is a second, unrelated check this case does not exercise.
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_launchd_agent_run_launchctl(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("launchd_agent.py", "run_launchctl")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_launchd_agent_default_kubeconfig_has_context(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("launchd_agent.py", "default_kubeconfig_has_context")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_launchd_agent_launchctl_print(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("launchd_agent.py", "launchctl_print")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_probe_console_surface_git_revision(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("probe_console_surface.py", "git_revision")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_last_changed(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "last_changed")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_last_changed_revision(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "last_changed_revision")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_revision_exists(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "revision_exists")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_is_ancestor(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "is_ancestor")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_git_mtime(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "git_mtime")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_scanner_merged_commit_subjects(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "merged_commit_subjects")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) >= 1
    for call in recorder.calls:
        _assert_no_leak(case_key, call["env"])


def test_scanner_merged_main_tasks_basenames(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("scanner.py", "merged_main_tasks_basenames")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) >= 1
    for call in recorder.calls:
        _assert_no_leak(case_key, call["env"])


def test_workflow_run_health_gh_workflow_names(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("workflow_run_health.py", "_gh_workflow_names")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_workflow_run_health_gh_default_branch(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("workflow_run_health.py", "_gh_default_branch")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_workflow_run_health_gh_run_list_json(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("workflow_run_health.py", "_gh_run_list_json")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_tunnel_keeper_spawn_link(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("tunnel_keeper.py", "TunnelKeeper._spawn_link")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


def test_merge_on_verdict_run_merge_script(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("activities/merge_on_verdict.py", "run_merge_script")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, env)
    assert env["DISCORD_WEBHOOK_URL"] == DISCORD


def test_worker_identity_default_acl_runner(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_identity.py", "_default_acl_runner")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, env)
    assert env == {"PATH": worker_identity.MINIMAL_PATH}


def test_worker_identity_run_launch_call(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_identity.py", "run_launch_call")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, env)
    assert env == {"PATH": worker_identity.MINIMAL_PATH}


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_run(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_launcher.py", "run")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    env = recorder.calls[-1]["env"]
    _assert_no_leak(case_key, env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
    assert env["SUBSTRATE_API_KEY"] == R


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_exec(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_launcher.py", "_cmd_exec")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_seed(recorder, credential_fixture, monkeypatch, tmp_path):
    # PARTB-B2b: seed and vseed share this one implementing function; no credential
    # ever reaches git (design §2.1: seed/vseed carry no credential).
    case_key = ("worker_launcher.py", "_seed_vseed_core")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    # check-ref-format, clone, checkout, remote set-url, fetch (no patch: this case
    # drives `seed`, never `vseed`).
    assert len(recorder.calls) == 5
    for call in recorder.calls:
        _assert_no_leak(case_key, call["env"])


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_materialize(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_launcher.py", "_cmd_materialize")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_export(recorder, credential_fixture, monkeypatch, tmp_path):
    case_key = ("worker_launcher.py", "_cmd_export")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    # check-ref-format, add, diff.
    assert len(recorder.calls) == 3
    for call in recorder.calls:
        _assert_no_leak(case_key, call["env"])


@pytest.mark.skipif(
    not _WORKER_LAUNCHER_PATH.exists(), reason="worker_launcher.py does not exist yet"
)
def test_worker_launcher_venv(recorder, credential_fixture, monkeypatch, tmp_path):
    # PARTB-B2b: venv and pip share this one implementing function; no credential
    # ever reaches it (design §2.1: venv/pip carry no credential).
    case_key = ("worker_launcher.py", "_cmd_venv")
    _CASE_FUNCS[case_key](recorder, tmp_path, monkeypatch)
    assert len(recorder.calls) == 1
    _assert_no_leak(case_key, recorder.calls[-1]["env"])


# ---------------------------------------------------------------------------
# AC-6: nothing any spawn-site case does may mutate the dispatcher's own
# os.environ. This test DRIVES every CASES entry itself (not just the four
# names the fixture happened to set) and snapshots the whole environment,
# not a chosen subset -- a test that only reads back what it set stayed green
# while scanner.last_changed popped a name out of os.environ (the gate's
# finding on #1094). Proved manually (not committed) by adding
# `os.environ.pop("DISCORD_WEBHOOK_URL", None)` inside
# `_case_scanner_last_changed` above and re-running this test: it goes red,
# reporting the missing key; removing the pop again turns it green.
#
# This loop also asserts `_assert_no_leak` on every call each case records
# (the #1097 gate's required fix). Without this, the no-leak check lived
# only in the per-function tests above, so deleting one of them (the gate's
# own example: `test_scanner_is_ancestor`) silently dropped the only check
# that site's env was ever asked to pass -- this loop and the meta-assertion
# below already run every case regardless, so they are where the check
# belongs to survive a deleted per-function test. Proved manually (not
# committed) by deleting `test_scanner_is_ancestor` above AND changing
# `scanner.is_ancestor` to pass `process_env.child_env(needs=
# ("SUBSTRATE_API_KEY",))`: this test (and the meta-assertion below) go red
# on the write key leaking into `is_ancestor`'s recorded env; reverting both
# changes turns the suite green again at the same count as before.
# ---------------------------------------------------------------------------


def test_dispatcher_own_environment_is_never_mutated(recorder, credential_fixture, monkeypatch, tmp_path):
    # Guarantee the ea_observation case's own delenv (test scaffolding for
    # ITS precondition, not a thing we're asserting about) is a true no-op
    # before the snapshot below, regardless of the ambient environment.
    monkeypatch.delenv(ea_observation.DEFAULT_CLUSTER_ENV, raising=False)
    before = dict(os.environ)

    for case_key, func in CASES:
        calls_before = len(recorder.calls)
        func(recorder, tmp_path, monkeypatch)
        for call in recorder.calls[calls_before:]:
            _assert_no_leak(case_key, call["env"])

    assert os.environ == before, (
        "a spawn-site case mutated the dispatcher's own os.environ: "
        f"added={set(os.environ) - set(before)} "
        f"removed={set(before) - set(os.environ)} "
        f"changed={[k for k in before if k in os.environ and os.environ[k] != before[k]]}"
    )


# ---------------------------------------------------------------------------
# Meta-assertion: every function with a spawn site has exactly one case, and
# the "exercised" set is what this test actually RAN, not a hand-written
# list a deleted case could leave stale. Proved manually (not committed) by
# deleting one CASES entry above (for example the scanner.is_ancestor line)
# and re-running this test: `missing` then names that function and the test
# goes red; restoring the entry turns it green again.
#
# Also asserts `_assert_no_leak` on every call each case records -- see the
# comment above `test_dispatcher_own_environment_is_never_mutated` for why:
# this loop runs every case regardless of whether its per-function test
# still exists, so it is one of the two places the no-leak check must live
# to survive a deleted per-function test.
# ---------------------------------------------------------------------------


def test_every_spawn_bearing_function_has_a_case(recorder, credential_fixture, monkeypatch, tmp_path):
    discovered = {(call.path, call.function) for call in find_spawn_calls(_DISPATCHER_ROOT)}
    discovered |= _EXTRA_CASES

    exercised: set[CaseKey] = set()
    for case_key, func in CASES:
        calls_before = len(recorder.calls)
        func(recorder, tmp_path, monkeypatch)
        for call in recorder.calls[calls_before:]:
            _assert_no_leak(case_key, call["env"])
        exercised.add(case_key)

    missing = discovered - exercised
    extra = exercised - discovered
    assert not missing, f"spawn-bearing functions with no AC-5 case: {sorted(missing)}"
    assert not extra, f"AC-5 cases naming a function with no spawn call: {sorted(extra)}"


def test_worker_launcher_third_exception_decided_by_file_existence():
    """Decision D's third AC-5 exception (worker_launcher.py's `run`
    subcommand, scoped by decision D / the part-B design) is decided from
    that file's EXISTENCE, never a flag or env var. If it exists, its `run`
    case must be exercised in CASES above; if it does not, the exception is
    inert and this test does nothing.

    Proved manually (not committed) in a scratch copy of this checkout: with
    a temporary worker_launcher.py containing a `run` function that spawns
    (and no matching CASES entry), this test fails, naming the missing case;
    with worker_launcher.py removed again, this test is inert (skipped).
    """
    if not _WORKER_LAUNCHER_PATH.exists():
        pytest.skip(
            "worker_launcher.py does not exist in this checkout; the third "
            "AC-5 exception is inert"
        )
    assert _WORKER_LAUNCHER_RUN_CASE in _CASE_FUNCS, (
        "worker_launcher.py exists but no AC-5 case exercises its `run` "
        "subcommand's child env"
    )
