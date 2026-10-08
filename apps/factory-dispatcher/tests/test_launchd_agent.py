"""Tests for the Air launchd supervisor installer."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launchd_agent  # noqa: E402
import worker_checkout  # noqa: E402
import worker_identity as wi  # noqa: E402


REQUIRED_ENV = {
    "SUBSTRATE_URL": "http://127.0.0.1:18001",
    "SUBSTRATE_API_KEY": "super-secret-api-key",
    "TEMPORAL_URL": "127.0.0.1:7233",
    "FACTORY_REPO": "example/repo",
    "FACTORY_REMOTE": "git@github.com:example/repo.git",
    "FACTORY_DEPLOYED_REVISION_NAMESPACE": "example-ns",
    "FACTORY_DEPLOYED_REVISION_DEPLOYMENT": "example-app",
}


def write_env_file(path: Path, values: dict[str, str] = REQUIRED_ENV) -> None:
    path.write_text(
        "\n".join(f"export {name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )


def write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def stub_import_dependency_check_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(
        launchd_agent.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )


def test_default_repo_root_is_the_factorys_own_checkout_not_the_installers_tree(
    monkeypatch,
):
    # 2026-08-25: the worker must load from a checkout the factory controls
    # by default, not from wherever launchd_agent.py itself happens to be
    # checked out (the operator's shared working tree).
    monkeypatch.delenv(worker_checkout.CHECKOUT_DIR_ENV, raising=False)

    config = launchd_agent.LaunchdConfig()

    assert config.repo_root == worker_checkout.default_checkout_root()
    assert config.repo_root != Path(__file__).resolve().parents[2]


def test_repo_root_cli_default_is_the_factory_controlled_checkout(monkeypatch):
    monkeypatch.delenv(worker_checkout.CHECKOUT_DIR_ENV, raising=False)

    parser = launchd_agent.build_parser()
    args = parser.parse_args(["install"])

    assert args.repo_root == worker_checkout.default_checkout_root()


def test_render_plist_sets_supervision_logs_and_no_secret(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(env_file)
    log_dir = tmp_path / "logs"

    plist_text = launchd_agent.render_plist(
        launchd_agent.LaunchdConfig(
            repo_root=tmp_path / "repo",
            env_file=env_file,
            python=Path("/usr/bin/python3"),
            log_dir=log_dir,
        )
    )
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["Label"] == launchd_agent.LABEL
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == str(log_dir / "factory-dispatcher-worker.out.log")
    assert plist["StandardErrorPath"] == str(log_dir / "factory-dispatcher-worker.err.log")
    assert "super-secret-api-key" not in plist_text
    assert str(env_file) in plist_text


def test_render_plist_runs_the_launcher_instead_of_sourcing_the_env_file(tmp_path):
    """dev.finding a0166920: `set -a; . <env file>; set +a` before `exec` put every
    credential the env file names into the worker's exec-time environment, which any
    same-uid process can read through KERN_PROCARGS2 / /proc/<pid>/environ. The
    rendered command must run through process_env.py instead, which loads the file
    after exec."""
    env_file = tmp_path / "worker.env"
    write_env_file(env_file)
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        log_dir=tmp_path / "logs",
    )

    plist_text = launchd_agent.render_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))
    shell_command = plist["ProgramArguments"][2]

    assert shell_command == launchd_agent.shell_command(config)
    assert "apps/factory-dispatcher/process_env.py" in shell_command
    assert f"--env-file {env_file}" in shell_command
    assert shell_command.endswith("apps/factory-dispatcher/worker.py")
    for token in ("set -a", "set +a", "; . ", "source "):
        assert token not in shell_command


def test_shell_command_execs_worker_directly_when_there_is_no_env_file(tmp_path):
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=None,
        python=Path("/usr/bin/python3"),
    )

    command = launchd_agent.shell_command(config)

    assert "process_env.py" not in command
    assert command.endswith("apps/factory-dispatcher/worker.py")
    for token in ("set -a", "set +a", "; . ", "source "):
        assert token not in command


def test_config_preserves_symlinked_python_while_resolving_other_paths(tmp_path):
    repo_real = tmp_path / "repo-real"
    repo_real.mkdir()
    repo_link = tmp_path / "repo-link"
    repo_link.symlink_to(repo_real, target_is_directory=True)

    env_real = tmp_path / "env-real"
    write_env_file(env_real)
    env_link = tmp_path / "env-link"
    env_link.symlink_to(env_real)

    log_real = tmp_path / "logs-real"
    log_real.mkdir()
    log_link = tmp_path / "logs-link"
    log_link.symlink_to(log_real, target_is_directory=True)

    python_target = tmp_path / "homebrew" / "python3.14"
    python_target.parent.mkdir()
    python_target.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(python_target)

    config = launchd_agent.config_from_args(
        SimpleNamespace(
            repo_root=repo_link,
            env_file=env_link,
            python=venv_python,
            log_dir=log_link,
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )

    assert config.python == venv_python
    assert config.python != venv_python.resolve()
    assert config.repo_root == repo_link.resolve()
    assert config.env_file == env_link.resolve()
    assert config.log_dir == log_link.resolve()


def test_rendered_plist_invokes_symlinked_python_path(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_DISPATCHER_ENV_FILE", raising=False)
    python_target = tmp_path / "homebrew" / "python3.14"
    python_target.parent.mkdir()
    python_target.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(python_target)

    config = launchd_agent.config_from_args(
        SimpleNamespace(
            repo_root=tmp_path / "repo",
            env_file=None,
            python=venv_python,
            log_dir=tmp_path / "logs",
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )
    plist_text = launchd_agent.render_plist(config)

    assert str(venv_python) in plist_text
    assert str(venv_python.resolve()) not in plist_text


def test_install_is_idempotent_and_leaves_one_loaded_agent(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(env_file)
    loaded: set[str] = set()
    launchctl_calls: list[tuple[str, ...]] = []

    def fake_launchctl(args, check=True):
        launchctl_calls.append(tuple(args))
        if args[0] == "bootout":
            loaded.discard(str(tmp_path / "LaunchAgents" / f"{launchd_agent.LABEL}.plist"))
        elif args[0] == "bootstrap":
            loaded.add(args[2])

    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )

    first = launchd_agent.install(
        config,
        environ={},
        launchctl=fake_launchctl,
        temporal_reachable=lambda _address: True,
        dependency_checker=lambda _config, _values: None,
    )
    second = launchd_agent.install(
        config,
        environ={},
        launchctl=fake_launchctl,
        temporal_reachable=lambda _address: True,
        dependency_checker=lambda _config, _values: None,
    )

    assert first == second == config.plist_path
    assert loaded == {str(config.plist_path)}
    assert config.log_dir.is_dir()
    assert config.plist_path.exists()
    assert [call[0] for call in launchctl_calls] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]


def test_install_refuses_missing_required_configuration(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(
        env_file,
        {
            "SUBSTRATE_URL": "http://127.0.0.1:18001",
            "TEMPORAL_URL": "127.0.0.1:7233",
        },
    )
    launchctl_calls = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    with pytest.raises(launchd_agent.ConfigError, match="SUBSTRATE_API_KEY"):
        launchd_agent.install(
            config,
            environ={},
            launchctl=lambda *args, **kwargs: launchctl_calls.append(args),
            temporal_reachable=lambda _address: True,
            dependency_checker=lambda _config, _values: None,
        )

    assert launchctl_calls == []
    assert not config.plist_path.exists()


def test_install_refuses_when_temporal_tunnel_is_not_reachable(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(env_file)
    launchctl_calls = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    with pytest.raises(launchd_agent.ConfigError, match="Temporal address"):
        launchd_agent.install(
            config,
            environ={},
            launchctl=lambda *args, **kwargs: launchctl_calls.append(args),
            temporal_reachable=lambda _address: False,
            dependency_checker=lambda _config, _values: None,
        )

    assert launchctl_calls == []
    assert not config.plist_path.exists()


def test_install_refuses_pythonpath_in_env_file(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(env_file, {**REQUIRED_ENV, "PYTHONPATH": "/tmp/stale-site-packages"})
    launchctl_calls = []
    dependency_checks = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    with pytest.raises(launchd_agent.ConfigError, match="PYTHONPATH"):
        launchd_agent.install(
            config,
            environ={},
            launchctl=lambda *args, **kwargs: launchctl_calls.append(args),
            temporal_reachable=lambda _address: True,
            dependency_checker=lambda _config, _values: dependency_checks.append(_config),
        )

    assert launchctl_calls == []
    assert dependency_checks == []
    assert not config.plist_path.exists()


def test_install_refuses_python_that_cannot_import_worker_dependency(tmp_path):
    env_file = tmp_path / "worker.env"
    write_env_file(env_file)
    launchctl_calls = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=tmp_path / "venv" / "bin" / "python",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    def missing_temporalio(_config, _values):
        raise launchd_agent.ConfigError(
            "Python interpreter cannot import worker dependency 'temporalio': "
            f"{config.python}"
        )

    with pytest.raises(launchd_agent.ConfigError, match="temporalio") as excinfo:
        launchd_agent.install(
            config,
            environ={},
            launchctl=lambda *args, **kwargs: launchctl_calls.append(args),
            temporal_reachable=lambda _address: True,
            dependency_checker=missing_temporalio,
        )

    assert str(config.python) in str(excinfo.value)
    assert launchctl_calls == []
    assert not config.plist_path.exists()


def test_dependency_check_names_interpreter_and_missing_import_without_pythonpath(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs["env"]
        return SimpleNamespace(returncode=1, stderr="temporalio\n", stdout="")

    monkeypatch.setenv("PYTHONPATH", "/tmp/stale-site-packages")
    monkeypatch.setattr(launchd_agent.subprocess, "run", fake_run)
    config = launchd_agent.LaunchdConfig(python=tmp_path / "venv" / "bin" / "python")

    with pytest.raises(launchd_agent.ConfigError, match="temporalio") as excinfo:
        launchd_agent.validate_worker_dependencies(config)

    assert seen["args"][0] == str(config.python)
    assert "PYTHONPATH" not in seen["env"]
    assert str(config.python) in str(excinfo.value)


def test_install_refuses_when_required_executable_is_missing_from_configured_path(
    tmp_path, monkeypatch
):
    stub_import_dependency_check_succeeds(monkeypatch)
    env_file = tmp_path / "worker.env"
    bin_dir = tmp_path / "worker-bin"
    bin_dir.mkdir()
    write_env_file(env_file, {**REQUIRED_ENV, "PATH": str(bin_dir)})
    launchctl_calls = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    with pytest.raises(launchd_agent.ConfigError) as excinfo:
        launchd_agent.install(
            config,
            environ={"PATH": str(tmp_path / "ambient-bin")},
            launchctl=lambda *args, **kwargs: launchctl_calls.append(args),
            temporal_reachable=lambda _address: True,
        )

    message = str(excinfo.value)
    import dispatch
    assert dispatch.required_worker_executables()[0] in message
    assert f"PATH searched: {bin_dir}" in message
    assert str(tmp_path / "ambient-bin") not in message
    assert launchctl_calls == []
    assert not config.plist_path.exists()


def test_install_accepts_when_every_required_executable_resolves(
    tmp_path, monkeypatch
):
    stub_import_dependency_check_succeeds(monkeypatch)
    env_file = tmp_path / "worker.env"
    bin_dir = tmp_path / "worker-bin"
    bin_dir.mkdir()
    import dispatch
    for name in dispatch.required_worker_executables():
        write_executable(bin_dir / name)
    write_env_file(env_file, {**REQUIRED_ENV, "PATH": str(bin_dir)})
    launchctl_calls = []
    config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=env_file,
        python=Path("/usr/bin/python3"),
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    launchd_agent.install(
        config,
        environ={"PATH": ""},
        launchctl=lambda args, check=True: launchctl_calls.append(tuple(args)),
        temporal_reachable=lambda _address: True,
    )

    assert [call[0] for call in launchctl_calls] == ["bootout", "bootstrap"]
    assert config.plist_path.exists()


def test_dependency_check_reads_env_file_path_instead_of_ambient_path(tmp_path):
    env_file = tmp_path / "worker.env"
    ambient_bin = tmp_path / "ambient-bin"
    env_bin = tmp_path / "env-bin"
    ambient_bin.mkdir()
    env_bin.mkdir()
    write_executable(env_bin / "env-only-tool")
    write_env_file(env_file, {**REQUIRED_ENV, "PATH": str(env_bin)})
    config = launchd_agent.LaunchdConfig(env_file=env_file)
    values = launchd_agent.effective_environment(
        config,
        environ={"PATH": str(ambient_bin)},
    )

    launchd_agent.validate_worker_executable_dependencies(
        values,
        required=("env-only-tool",),
    )

    with pytest.raises(launchd_agent.ConfigError) as excinfo:
        launchd_agent.validate_worker_executable_dependencies(
            {"PATH": str(ambient_bin)},
            required=("env-only-tool",),
        )

    assert f"PATH searched: {ambient_bin}" in str(excinfo.value)


# ---------------------------------------------------------------------------
# kickstart_worker / LaunchdWorkerSupervisor -- recycling the worker after
# worker_checkout.advance() moves the checkout (2026-09-02).
# ---------------------------------------------------------------------------


def test_kickstart_worker_kickstarts_the_worker_label_under_its_gui_domain():
    calls = []
    config = launchd_agent.LaunchdConfig(gui_domain="gui/501")

    launchd_agent.kickstart_worker(config, launchctl=lambda args, check: calls.append((args, check)))

    assert calls == [
        (["kickstart", "-k", f"gui/501/{launchd_agent.LABEL}"], True)
    ]


def test_launchd_worker_supervisor_recycle_kickstarts_through_the_configured_launchctl():
    calls = []
    config = launchd_agent.LaunchdConfig(gui_domain="gui/501")
    supervisor = launchd_agent.LaunchdWorkerSupervisor(
        config, launchctl=lambda args, check: calls.append((args, check))
    )

    supervisor.recycle()

    assert calls == [
        (["kickstart", "-k", f"gui/501/{launchd_agent.LABEL}"], True)
    ]


# ---------------------------------------------------------------------------
# Part B, B3 (docs/plans/2026-09-26-design-part-b-worker-runs-as-separate-user.md
# §8 row B3): install-worker-identity, the dispatcher-side hash check and re-copy, and
# probe-worker-identity. No test here touches a real account, a real sudo, or a real
# /etc path (decision record D7).
# ---------------------------------------------------------------------------


# --- AC-1: golden sudoers and PAM text, printed only --------------------------


def test_render_worker_sudoers_text_is_byte_for_byte_the_design_rule():
    text = launchd_agent.render_worker_sudoers_text(
        "_factoryworker", "/Users/Shared/factory/bin/factory-worker-launch", "someoperator"
    )

    assert text == (
        "# /etc/sudoers.d/factory-worker   root:wheel 0440, installed with visudo\n"
        "Defaults>_factoryworker pam_service=factory-worker, !pam_setcred, !log_input, "
        "!log_output\n"
        "someoperator ALL=(_factoryworker) NOPASSWD: "
        "/Users/Shared/factory/bin/factory-worker-launch\n"
    )


def test_render_worker_sudoers_text_never_grants_root_or_the_worker_user_as_runas():
    text = launchd_agent.render_worker_sudoers_text("_factoryworker", "/x", "someoperator")

    assert "ALL=(_factoryworker)" in text
    assert "ALL=(root)" not in text
    assert "ALL=(someoperator)" not in text


def test_render_worker_sudoers_text_uses_the_given_operator_not_a_compiled_in_default():
    text_one = launchd_agent.render_worker_sudoers_text("_factoryworker", "/x", "operator-one")
    text_two = launchd_agent.render_worker_sudoers_text("_factoryworker", "/x", "operator-two")

    assert "operator-one ALL=(_factoryworker)" in text_one
    assert "operator-two ALL=(_factoryworker)" in text_two


def test_render_worker_sudoers_text_disables_sudo_io_logging():
    text = launchd_agent.render_worker_sudoers_text("_factoryworker", "/x", "someoperator")

    assert "!log_input" in text
    assert "!log_output" in text


def test_require_operator_account_refuses_when_unset():
    with pytest.raises(launchd_agent.ConfigError, match=launchd_agent.OPERATOR_ACCOUNT_ENV):
        launchd_agent._require_operator_account({})


def test_require_operator_account_returns_the_configured_value():
    assert (
        launchd_agent._require_operator_account({launchd_agent.OPERATOR_ACCOUNT_ENV: "someoperator"})
        == "someoperator"
    )


def test_install_worker_identity_refuses_when_no_operator_is_configured(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    monkeypatch.delenv(launchd_agent.OPERATOR_ACCOUNT_ENV, raising=False)

    with pytest.raises(launchd_agent.ConfigError, match=launchd_agent.OPERATOR_ACCOUNT_ENV):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root,
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


def test_render_worker_pam_text_is_byte_for_byte_the_design_service():
    text = launchd_agent.render_worker_pam_text()

    assert text == (
        "# /etc/pam.d/factory-worker   root:wheel 0644\n"
        "auth       required   pam_deny.so\n"
        "account    required   pam_permit.so\n"
        "password   required   pam_deny.so\n"
        "session    required   pam_launchd.so\n"
    )


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not on PATH")
def test_rendered_sudoers_text_parses_with_visudo(tmp_path):
    text = launchd_agent.render_worker_sudoers_text(
        "_factoryworker", "/Users/Shared/factory/bin/factory-worker-launch", "someoperator"
    )
    fragment = tmp_path / "factory-worker"
    fragment.write_text(text, encoding="utf-8")

    result = subprocess.run(
        ["visudo", "-c", "-f", str(fragment)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _write_fake_claude(tmp_path: Path) -> Path:
    claude_bin = tmp_path / "claude-real-binary"
    if not claude_bin.exists():
        claude_bin.write_bytes(b"#!/bin/sh\necho fake-claude\n")
        claude_bin.chmod(0o755)
    return claude_bin


def _mkdir_mode(path: Path, mode: int) -> Path:
    """Creates ``path`` and chmods it to exactly ``mode``, independent of the ambient
    umask -- install_worker_identity's --bin and --runs-root mode checks (item 6) refuse
    a bin that is group/world-writable and a runs-root carrying any group or other bit,
    so every test exercising the installer needs dirs created at a mode that passes
    those checks rather than whatever a bare .mkdir() happens to get from umask."""
    path.mkdir()
    path.chmod(mode)
    return path


def test_install_worker_identity_prints_both_texts_under_labelled_headings(tmp_path, capsys):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
    )

    out = capsys.readouterr().out
    assert "/etc/sudoers.d/factory-worker" in out
    assert "sudo visudo -f /etc/sudoers.d/factory-worker" in out
    assert "/etc/pam.d/factory-worker" in out
    assert "sudo install -m 0644 -o root -g wheel" in out
    assert "pam_launchd.so" in out


def test_install_worker_identity_never_reads_or_writes_etc(tmp_path, monkeypatch):
    """AC-1: nothing under /etc is read or written by any code path in this bead, proved
    end to end by making every open of an /etc path a hard failure."""
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    claude_source = _write_fake_claude(tmp_path)

    real_open = open

    def guarded_open(file, *args, **kwargs):
        path_str = os.fspath(file) if hasattr(file, "__fspath__") else str(file)
        if path_str.startswith("/etc"):
            raise AssertionError(f"install-worker-identity opened {file!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setattr("io.open", guarded_open)

    real_chmod = os.chmod

    def guarded_chmod(path, *args, **kwargs):
        if str(path).startswith("/etc"):
            raise AssertionError(f"install-worker-identity chmod'd {path!r}")
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(launchd_agent.os, "chmod", guarded_chmod)

    real_os_open = os.open

    def guarded_os_open(path, *args, **kwargs):
        if str(path).startswith("/etc"):
            raise AssertionError(f"install-worker-identity os.open'd {path!r}")
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(launchd_agent.os, "open", guarded_os_open)

    result = launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(claude_source),
    )

    assert len(result.files) == 3


# --- AC-2: install copies and records ------------------------------------------


def test_install_worker_identity_copies_launcher_process_env_and_claude_with_hashes(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    claude_source = tmp_path / "claude-real"
    claude_source.write_bytes(b"fake claude binary bytes")
    claude_source.chmod(0o755)

    result = launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(claude_source),
    )

    checkout_dir = Path(launchd_agent.__file__).resolve().parent
    launcher_dest = bin_dir / "factory-worker-launch"
    process_env_dest = bin_dir / "process_env.py"
    claude_dest = bin_dir / "claude"
    worker_user_file = runs_root / ".worker-user"

    assert launcher_dest.read_bytes() == (checkout_dir / "worker_launcher.py").read_bytes()
    assert process_env_dest.read_bytes() == (checkout_dir / "process_env.py").read_bytes()
    assert claude_dest.read_bytes() == claude_source.read_bytes()

    assert stat.S_IMODE(launcher_dest.stat().st_mode) == 0o755
    assert stat.S_IMODE(process_env_dest.stat().st_mode) == 0o644
    assert stat.S_IMODE(claude_dest.stat().st_mode) == 0o755

    assert worker_user_file.read_text(encoding="utf-8") == "_factoryworker"
    assert stat.S_IMODE(worker_user_file.stat().st_mode) == 0o600

    hashes_by_path = {str(f.path): f.sha256 for f in result.files}
    assert hashes_by_path[str(launcher_dest)] == hashlib.sha256(launcher_dest.read_bytes()).hexdigest()
    assert hashes_by_path[str(process_env_dest)] == hashlib.sha256(process_env_dest.read_bytes()).hexdigest()
    assert hashes_by_path[str(claude_dest)] == hashlib.sha256(claude_source.read_bytes()).hexdigest()
    assert result.worker_user_file == worker_user_file


def test_install_worker_identity_resolves_a_symlinked_claude_to_its_real_target(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    real_target = tmp_path / "real-claude-macho"
    real_target.write_bytes(b"the actual mach-o bytes")
    real_target.chmod(0o755)
    symlink_path = tmp_path / "claude-symlink"
    symlink_path.symlink_to(real_target)

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(symlink_path),
    )

    assert (bin_dir / "claude").read_bytes() == real_target.read_bytes()
    assert not (bin_dir / "claude").is_symlink()


def test_install_worker_identity_replaces_destinations_atomically_changing_inode(tmp_path):
    """gate finding: rewriting claude's inode in place SIGKILLs an already-running copy
    (repro'd rc 137). A re-install must swap the inode via os.replace(), not truncate
    the existing file -- proved here by the inode actually changing across a re-run."""
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    claude_source = _write_fake_claude(tmp_path)

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(claude_source),
    )
    claude_dest = bin_dir / "claude"
    launcher_dest = bin_dir / "factory-worker-launch"
    first_claude_inode = claude_dest.stat().st_ino
    first_launcher_inode = launcher_dest.stat().st_ino

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(claude_source),
    )

    assert claude_dest.stat().st_ino != first_claude_inode
    assert launcher_dest.stat().st_ino != first_launcher_inode


def test_install_worker_identity_recopy_leaves_other_hardlinks_untouched(tmp_path):
    """Today's in-place write would mutate every hardlink to the old file; os.replace()
    only repoints bin_dir's own directory entry, leaving a separately-held hardlink's
    content exactly as it was."""
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    claude_source = _write_fake_claude(tmp_path)

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(claude_source),
    )
    claude_dest = bin_dir / "claude"
    hardlink = tmp_path / "claude-hardlink"
    os.link(claude_dest, hardlink)
    original_bytes = hardlink.read_bytes()

    new_claude_source = tmp_path / "claude-real-v2"
    new_claude_source.write_bytes(b"a brand new claude binary, totally different bytes")
    new_claude_source.chmod(0o755)

    launchd_agent.install_worker_identity(
        "_factoryworker", bin_dir, runs_root, operator="someoperator",
        claude_resolver=lambda _name: str(new_claude_source),
    )

    assert hardlink.read_bytes() == original_bytes
    assert claude_dest.read_bytes() == new_claude_source.read_bytes()


def test_install_worker_identity_refuses_when_bin_dir_is_not_owned_by_effective_uid(
    tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    real_euid = os.geteuid()
    monkeypatch.setattr(launchd_agent.os, "geteuid", lambda: real_euid + 1)

    with pytest.raises(launchd_agent.ConfigError, match="--bin"):
        launchd_agent.install_worker_identity("_factoryworker", bin_dir, runs_root)

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


def test_install_worker_identity_refuses_when_runs_root_is_not_a_directory(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runs_root = tmp_path / "runs-does-not-exist"

    with pytest.raises(launchd_agent.ConfigError, match="--runs-root"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root,
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []


def test_install_worker_identity_refuses_when_a_destination_is_a_symlink(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("x")
    (bin_dir / "factory-worker-launch").symlink_to(elsewhere)

    with pytest.raises(launchd_agent.ConfigError, match="symlink"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert not (runs_root / ".worker-user").exists()
    assert not (bin_dir / "process_env.py").exists()
    assert not (bin_dir / "claude").exists()


def test_install_worker_identity_refuses_when_claude_is_not_on_path(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)

    with pytest.raises(launchd_agent.ConfigError, match="claude"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: None,
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


def test_install_worker_identity_refuses_when_a_destination_is_a_directory(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    (bin_dir / "process_env.py").mkdir()

    with pytest.raises(launchd_agent.ConfigError, match="directory"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert not (runs_root / ".worker-user").exists()
    assert not (bin_dir / "factory-worker-launch").exists()
    assert not (bin_dir / "claude").exists()


def test_install_worker_identity_refuses_when_a_destination_is_a_fifo(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    os.mkfifo(bin_dir / "claude")

    with pytest.raises(launchd_agent.ConfigError, match="FIFO"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert not (runs_root / ".worker-user").exists()
    assert not (bin_dir / "factory-worker-launch").exists()
    assert not (bin_dir / "process_env.py").exists()


def test_install_worker_identity_refuses_a_group_or_world_writable_bin_dir(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    bin_dir.mkdir()
    bin_dir.chmod(0o777)
    _mkdir_mode(runs_root, 0o700)

    with pytest.raises(launchd_agent.ConfigError, match="--bin"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


def test_install_worker_identity_refuses_a_runs_root_with_any_group_or_other_bits(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    runs_root.mkdir()
    runs_root.chmod(0o755)

    with pytest.raises(launchd_agent.ConfigError, match="--runs-root"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


@pytest.mark.parametrize(
    "bad_user", ["root", "ALL", "User", "_factory worker", "-leadingdash", "a" * 33]
)
def test_install_worker_identity_refuses_invalid_user_names(tmp_path, bad_user):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)

    with pytest.raises(launchd_agent.ConfigError, match="--user"):
        launchd_agent.install_worker_identity(
            bad_user, bin_dir, runs_root, operator="someoperator",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


def test_install_worker_identity_refuses_an_operator_name_with_a_newline_and_writes_nothing(
    tmp_path, capsys
):
    """gate finding: an operator string containing a newline and a sudoers fragment used
    to survive straight into the rendered/printed rule (`ALL=(ALL) NOPASSWD: ALL`).
    Validating --operator against the same account-name pattern as --user refuses it
    before anything is rendered or printed."""
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)
    hostile_operator = "someoperator\nALL=(ALL) NOPASSWD: ALL"

    with pytest.raises(launchd_agent.ConfigError, match="--operator"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator=hostile_operator,
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []
    out = capsys.readouterr().out
    assert "ALL=(ALL)" not in out
    assert out == ""


def test_install_worker_identity_refuses_when_user_equals_operator(tmp_path):
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    _mkdir_mode(bin_dir, 0o755)
    _mkdir_mode(runs_root, 0o700)

    with pytest.raises(launchd_agent.ConfigError, match="same account"):
        launchd_agent.install_worker_identity(
            "_factoryworker", bin_dir, runs_root, operator="_factoryworker",
            claude_resolver=lambda _name: str(_write_fake_claude(tmp_path)),
        )

    assert list(bin_dir.iterdir()) == []
    assert list(runs_root.iterdir()) == []


# --- AC-3: the hash check and re-copy -------------------------------------------


def test_check_installed_worker_identity_reports_match_for_both_files(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher_bytes = b"launcher content"
    process_env_bytes = b"process env content"
    (bin_dir / "factory-worker-launch").write_bytes(launcher_bytes)
    (bin_dir / "process_env.py").write_bytes(process_env_bytes)
    launcher_hash = hashlib.sha256(launcher_bytes).hexdigest()
    process_env_hash = hashlib.sha256(process_env_bytes).hexdigest()

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, (launcher_hash, process_env_hash)
    )

    assert result["worker_launcher.py"] == launchd_agent.FileCheckResult("match")
    assert result["process_env.py"] == launchd_agent.FileCheckResult("match")


def test_check_installed_worker_identity_recopies_a_mismatched_file(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "factory-worker-launch").write_bytes(b"stale content")
    (bin_dir / "process_env.py").write_bytes(b"ok content")
    good_process_env_hash = hashlib.sha256(b"ok content").hexdigest()
    fresh_hash = hashlib.sha256(b"fresh content").hexdigest()

    copied = {}

    def fake_copy(name, dest):
        copied[name] = dest
        dest.write_bytes(b"fresh content")

    ops = launchd_agent.HashCheckOps(
        read=lambda p: p.read_bytes() if p.exists() else None,
        stat=lambda p: p.lstat(),
        copy=fake_copy,
    )

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, (fresh_hash, good_process_env_hash), ops=ops,
    )

    assert result["worker_launcher.py"] == launchd_agent.FileCheckResult("recopied")
    assert result["process_env.py"] == launchd_agent.FileCheckResult("match")
    assert copied == {"worker_launcher.py": bin_dir / "factory-worker-launch"}
    assert (bin_dir / "factory-worker-launch").read_bytes() == b"fresh content"


def test_check_installed_worker_identity_recopies_a_missing_file(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "process_env.py").write_bytes(b"ok")
    good_hash = hashlib.sha256(b"ok").hexdigest()
    recreated_hash = hashlib.sha256(b"recreated").hexdigest()

    def fake_copy(name, dest):
        dest.write_bytes(b"recreated")

    ops = launchd_agent.HashCheckOps(
        read=lambda p: p.read_bytes() if p.exists() else None,
        stat=lambda p: p.lstat(),
        copy=fake_copy,
    )

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, (recreated_hash, good_hash), ops=ops,
    )

    assert result["worker_launcher.py"] == launchd_agent.FileCheckResult("recopied")


def test_check_installed_worker_identity_reports_failed_when_recopy_raises(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "process_env.py").write_bytes(b"ok")
    good_hash = hashlib.sha256(b"ok").hexdigest()

    def failing_copy(name, dest):
        raise OSError("disk full")

    ops = launchd_agent.HashCheckOps(
        read=lambda p: p.read_bytes() if p.exists() else None,
        stat=lambda p: p.lstat(),
        copy=failing_copy,
    )

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, ("deadbeef" * 8, good_hash), ops=ops,
    )

    assert result["worker_launcher.py"].outcome == "failed"
    assert "disk full" in result["worker_launcher.py"].reason


def test_check_installed_worker_identity_refuses_when_recopy_still_mismatches(tmp_path):
    """The re-copy itself can succeed (no OSError) and still produce the wrong bytes --
    e.g. a stale or truncated source. That must be reported failed, never reported ok;
    dropping the post-copy re-check would make this test the one thing to catch it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "factory-worker-launch").write_bytes(b"stale content")
    (bin_dir / "process_env.py").write_bytes(b"ok content")
    good_process_env_hash = hashlib.sha256(b"ok content").hexdigest()
    expected_launcher_hash = hashlib.sha256(b"expected content").hexdigest()

    def fake_copy(name, dest):
        dest.write_bytes(b"still the wrong content")

    ops = launchd_agent.HashCheckOps(
        read=lambda p: p.read_bytes() if p.exists() else None,
        stat=lambda p: p.lstat(),
        copy=fake_copy,
    )

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, (expected_launcher_hash, good_process_env_hash), ops=ops,
    )

    assert result["worker_launcher.py"].outcome == "failed"
    assert "still does not match" in result["worker_launcher.py"].reason
    assert result["process_env.py"] == launchd_agent.FileCheckResult("match")


def test_check_installed_worker_identity_refuses_to_recopy_over_a_symlinked_destination(
    tmp_path,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "elsewhere"
    target.write_bytes(b"x")
    (bin_dir / "factory-worker-launch").symlink_to(target)
    (bin_dir / "process_env.py").write_bytes(b"ok")
    good_hash = hashlib.sha256(b"ok").hexdigest()

    copy_calls = []
    ops = launchd_agent.HashCheckOps(
        read=lambda p: p.read_bytes() if p.exists() else None,
        stat=lambda p: p.lstat(),
        copy=lambda name, dest: copy_calls.append((name, dest)),
    )

    result = launchd_agent.check_installed_worker_identity(
        bin_dir, ("deadbeef" * 8, good_hash), ops=ops,
    )

    assert result["worker_launcher.py"].outcome == "failed"
    assert "symlink" in result["worker_launcher.py"].reason
    assert copy_calls == []


def test_check_installed_worker_identity_defaults_hashes_to_worker_identitys_own(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkout_dir = Path(launchd_agent.__file__).resolve().parent
    (bin_dir / "factory-worker-launch").write_bytes(
        (checkout_dir / "worker_launcher.py").read_bytes()
    )
    (bin_dir / "process_env.py").write_bytes((checkout_dir / "process_env.py").read_bytes())

    result = launchd_agent.check_installed_worker_identity(bin_dir)

    assert result["worker_launcher.py"] == launchd_agent.FileCheckResult("match")
    assert result["process_env.py"] == launchd_agent.FileCheckResult("match")


# --- AC-4: probe and selfcheck through the launcher -----------------------------


class _FakeLaunchRunner:
    def __init__(self, stdout: bytes, stderr: bytes = b""):
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list = []

    def __call__(self, call: wi.LaunchCall) -> subprocess.CompletedProcess:
        self.calls.append(call)
        return subprocess.CompletedProcess(
            args=call.argv, returncode=0, stdout=self.stdout, stderr=self.stderr
        )


def test_build_selfcheck_launch_call_golden_argv_and_env():
    cfg = wi.WorkerIdentityConfig(user="_factoryworker")

    call = launchd_agent.build_selfcheck_launch_call(cfg)

    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "selfcheck",
        "/Users/Shared/factory-runs",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert call.input == b"{}"


def test_build_probe_launch_call_golden_argv_and_env():
    cfg = wi.WorkerIdentityConfig(user="_factoryworker")

    call = launchd_agent.build_probe_launch_call(cfg, fields={})

    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "probe",
        "/Users/Shared/factory-runs",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert call.input == b"{}"


def test_run_selfcheck_probe_calls_the_runner_exactly_once_and_prints_the_envelope(capsys):
    cfg = wi.WorkerIdentityConfig(user="_factoryworker")
    envelope = {
        "status": "ok", "child_rc": None, "stdout": "/tmp/claude-850", "stderr": "",
        "stdout_truncated_result": False,
    }
    runner = _FakeLaunchRunner(json.dumps(envelope).encode("utf-8"))

    result = launchd_agent.run_selfcheck_probe(cfg, runner=runner)

    assert result == envelope
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "selfcheck",
        "/Users/Shared/factory-runs",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert json.loads(call.input) == {}
    out = capsys.readouterr().out
    assert "probe-worker-identity --selfcheck" in out
    assert "/tmp/claude-850" in out
    assert "Closure precondition for dev.finding 75b50b58" in out


def test_run_worker_probe_calls_the_runner_exactly_once_and_labels_items(capsys):
    cfg = wi.WorkerIdentityConfig(user="_factoryworker")
    observations = {
        "a": {
            "managername": {"argv": [], "rc": 0, "stdout": "StandardIO\n", "stderr": ""},
            "windowserver_lookup": 1102,
        },
        "i": {"com.apple.pasteboard.1": 1100},
    }
    envelope = {
        "status": "ok", "child_rc": None, "stdout": json.dumps(observations), "stderr": "",
        "stdout_truncated_result": False,
    }
    runner = _FakeLaunchRunner(json.dumps(envelope).encode("utf-8"))

    result = launchd_agent.run_worker_probe(cfg, runner=runner)

    assert result == envelope
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv == (
        "/usr/bin/sudo", "-n", "-u", "_factoryworker",
        "/Users/Shared/factory/bin/factory-worker-launch", "probe",
        "/Users/Shared/factory-runs",
    )
    assert call.env == {"PATH": wi.MINIMAL_PATH}
    assert set(json.loads(call.input).keys()) == {"paths", "mach_names", "target_pid"}
    out = capsys.readouterr().out
    assert "(a) Which session" in out
    assert "(i) bootstrap_look_up" in out
    assert "Closure precondition for dev.finding 75b50b58" in out


def test_probe_item_labels_say_what_was_actually_measured_not_what_was_intended():
    """(e) sends no claude payload, so the authenticated call never runs; (g)/(h) run
    unsandboxed and (h)'s plist is empty -- none of that is "inside the worker profile"
    yet, and the labels must say so rather than claiming the design's intended
    measurement."""
    assert "NOT RUN" in launchd_agent.PROBE_ITEM_LABELS["e"]
    assert "NOT MEASURED IN THE WORKER PROFILE" in launchd_agent.PROBE_ITEM_LABELS["g"]
    assert "NOT MEASURED IN THE WORKER PROFILE" in launchd_agent.PROBE_ITEM_LABELS["h"]
    assert "inside the worker profile" not in launchd_agent.PROBE_ITEM_LABELS["g"]
    assert "inside the worker profile" not in launchd_agent.PROBE_ITEM_LABELS["h"]


def test_main_install_worker_identity_writes_every_file_through_the_cli(tmp_path, monkeypatch):
    """AC-1: install-worker-identity was never driven through main() at all. Runs the
    real CLI end to end (argparse, PATH-based claude resolution, the env var the
    installer reads for --operator) against tmp_path targets and asserts every file
    main() is supposed to have written, with its mode -- swapping --bin/--runs-root in
    main(), or deleting the install call, each leave this test unable to find what it
    expects."""
    bin_dir = tmp_path / "bin"
    runs_root = tmp_path / "runs"
    bin_dir.mkdir()
    bin_dir.chmod(0o755)
    runs_root.mkdir()
    runs_root.chmod(0o700)
    fake_path_bin = tmp_path / "fake-path-bin"
    fake_path_bin.mkdir()

    import dispatch

    claude_name = dispatch.WORKER_REGISTRY[dispatch.DEFAULT_WORKER].argv[0]
    fake_claude = fake_path_bin / claude_name
    fake_claude.write_bytes(b"#!/bin/sh\necho fake-claude\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_path_bin))
    monkeypatch.setenv(launchd_agent.OPERATOR_ACCOUNT_ENV, "someoperator")

    rc = launchd_agent.main(
        [
            "install-worker-identity",
            "--user", "_factoryworker",
            "--bin", str(bin_dir),
            "--runs-root", str(runs_root),
        ]
    )

    assert rc == 0
    checkout_dir = Path(launchd_agent.__file__).resolve().parent
    launcher_dest = bin_dir / "factory-worker-launch"
    process_env_dest = bin_dir / "process_env.py"
    claude_dest = bin_dir / "claude"
    worker_user_file = runs_root / ".worker-user"

    assert launcher_dest.read_bytes() == (checkout_dir / "worker_launcher.py").read_bytes()
    assert stat.S_IMODE(launcher_dest.stat().st_mode) == 0o755
    assert process_env_dest.read_bytes() == (checkout_dir / "process_env.py").read_bytes()
    assert stat.S_IMODE(process_env_dest.stat().st_mode) == 0o644
    assert claude_dest.read_bytes() == fake_claude.read_bytes()
    assert stat.S_IMODE(claude_dest.stat().st_mode) == 0o755
    assert worker_user_file.read_text(encoding="utf-8") == "_factoryworker"
    assert stat.S_IMODE(worker_user_file.stat().st_mode) == 0o600


def test_main_probe_worker_identity_selfcheck_dispatches_with_the_right_config(monkeypatch):
    captured = {}

    def fake_run_selfcheck_probe(cfg, **_kwargs):
        captured["cfg"] = cfg
        return {"status": "ok"}

    monkeypatch.setattr(launchd_agent, "run_selfcheck_probe", fake_run_selfcheck_probe)

    rc = launchd_agent.main(["probe-worker-identity", "--user", "_factoryworker", "--selfcheck"])

    assert rc == 0
    assert captured["cfg"].user == "_factoryworker"


def test_main_probe_worker_identity_without_selfcheck_runs_the_full_probe(monkeypatch):
    captured = {}

    def fake_run_worker_probe(cfg, **_kwargs):
        captured["cfg"] = cfg
        return {"status": "ok"}

    monkeypatch.setattr(launchd_agent, "run_worker_probe", fake_run_worker_probe)

    rc = launchd_agent.main(["probe-worker-identity", "--user", "_factoryworker"])

    assert rc == 0
    assert captured["cfg"].user == "_factoryworker"
