"""The tunnel keeper watchdog is the one piece of tunnel supervision launchd CAN own.

The kubectl port-forward tunnel itself cannot be launchd-managed: macOS Local Network privacy
denies LAN access to a process launchd starts in the background, in every domain this codebase
can select (including `gui/<uid>`, the domain `install-tunnel` already used before it was stood
down 2026-09-04). The watchdog does no LAN work -- it reads a local heartbeat file and, at most,
posts to a public Discord webhook -- so it is exactly the half of this problem launchd can
honestly promise to keep running across a reboot. These tests pin the plist it renders (RunAtLoad
+ StartInterval, not KeepAlive: a one-shot check, not a daemon) and that install/uninstall behave
like the existing tunnel agent's (idempotent, kickstarts, unloads and removes cleanly).
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launchd_agent  # noqa: E402


def test_render_keeper_watchdog_plist_is_runatload_not_keepalive(tmp_path):
    config = launchd_agent.KeeperWatchdogConfig(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        start_interval_seconds=180,
    )

    plist_text = launchd_agent.render_keeper_watchdog_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["Label"] == "com.gastown.factory-dispatcher-tunnel-keeper-watchdog"
    assert plist["RunAtLoad"] is True
    assert plist["StartInterval"] == 180
    assert "KeepAlive" not in plist
    assert plist["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    shell_command = plist["ProgramArguments"][2]
    assert str(config.repo_root) in shell_command
    assert "tunnel_keeper.py check-heartbeat" in shell_command
    assert str(config.python) in shell_command
    assert plist["StandardOutPath"] == str(
        tmp_path / "logs" / "factory-dispatcher-tunnel-keeper-watchdog.out.log"
    )
    assert plist["StandardErrorPath"] == str(
        tmp_path / "logs" / "factory-dispatcher-tunnel-keeper-watchdog.err.log"
    )


def test_keeper_watchdog_shell_command_passes_through_max_heartbeat_age(tmp_path):
    config = launchd_agent.KeeperWatchdogConfig(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        max_heartbeat_age_seconds=45,
    )

    command = launchd_agent.keeper_watchdog_shell_command(config)
    assert "check-heartbeat --max-age-seconds 45" in command


def test_keeper_watchdog_shell_command_omits_flag_when_not_set(tmp_path):
    config = launchd_agent.KeeperWatchdogConfig(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    command = launchd_agent.keeper_watchdog_shell_command(config)
    assert "--max-age-seconds" not in command
    assert command.endswith("tunnel_keeper.py check-heartbeat")


def test_install_keeper_watchdog_is_idempotent_and_kickstarts(tmp_path):
    config = launchd_agent.KeeperWatchdogConfig(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )
    loaded: set[str] = set()
    launchctl_calls: list[tuple[str, ...]] = []

    def fake_launchctl(args, check=True):
        launchctl_calls.append(tuple(args))
        if args[0] == "bootout":
            loaded.discard(str(config.plist_path))
        elif args[0] == "bootstrap":
            loaded.add(args[2])

    first = launchd_agent.install_keeper_watchdog(config, launchctl=fake_launchctl)
    second = launchd_agent.install_keeper_watchdog(config, launchctl=fake_launchctl)

    assert first == second == config.plist_path
    assert loaded == {str(config.plist_path)}
    assert config.plist_path.exists()
    assert [call[0] for call in launchctl_calls] == [
        "bootout",
        "bootstrap",
        "kickstart",
        "bootout",
        "bootstrap",
        "kickstart",
    ]
    assert launchctl_calls[2] == (
        "kickstart",
        "-k",
        "gui/501/com.gastown.factory-dispatcher-tunnel-keeper-watchdog",
    )


def test_uninstall_keeper_watchdog_unloads_and_removes_plist(tmp_path):
    config = launchd_agent.KeeperWatchdogConfig(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )
    launchd_agent.install_keeper_watchdog(
        config, launchctl=lambda args, check=True: None
    )
    assert config.plist_path.exists()

    launchctl_calls: list[tuple[str, ...]] = []
    path = launchd_agent.uninstall_keeper_watchdog(
        config, launchctl=lambda args, check=True: launchctl_calls.append(tuple(args))
    )

    assert path == config.plist_path
    assert not config.plist_path.exists()
    assert launchctl_calls == [("bootout", "gui/501", str(config.plist_path))]


def test_keeper_watchdog_config_from_args_reflects_every_cli_flag(tmp_path):
    import argparse

    args = argparse.Namespace(
        repo_root=tmp_path / "repo",
        python=tmp_path / "venv" / "bin" / "python",
        max_heartbeat_age_seconds=90,
        start_interval_seconds=120,
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    config = launchd_agent.keeper_watchdog_config_from_args(args)

    assert config.repo_root == (tmp_path / "repo").resolve()
    assert config.max_heartbeat_age_seconds == 90
    assert config.start_interval_seconds == 120
    assert config.log_dir == (tmp_path / "logs").resolve()
    assert config.launch_agents_dir == (tmp_path / "LaunchAgents").resolve()


def test_main_wires_install_and_uninstall_keeper_watchdog_subcommands(monkeypatch, tmp_path):
    """main() dispatches to the right function with a config built from argv -- the CLI
    wiring, not the launchctl side effects the direct-call tests above already cover."""
    calls: list[str] = []
    monkeypatch.setattr(
        launchd_agent, "install_keeper_watchdog", lambda config: calls.append("install") or Path("x")
    )
    monkeypatch.setattr(
        launchd_agent, "uninstall_keeper_watchdog", lambda config: calls.append("uninstall") or Path("x")
    )

    repo_root = tmp_path / "repo"
    python = tmp_path / "venv" / "bin" / "python"
    common_args = [
        "--repo-root", str(repo_root),
        "--python", str(python),
        "--log-dir", str(tmp_path / "logs"),
        "--launch-agents-dir", str(tmp_path / "LaunchAgents"),
    ]

    assert launchd_agent.main(["install-keeper-watchdog", *common_args]) == 0
    assert launchd_agent.main(["uninstall-keeper-watchdog", *common_args]) == 0
    assert calls == ["install", "uninstall"]


def test_watchdog_shell_command_runs_through_the_launcher_for_the_poster(tmp_path):
    """launchd provides no environment: without the process_env.py launcher loading
    the env file, the alert poster finds no DISCORD_WEBHOOK_URL and every
    human-required alert silently becomes a log line (release-gate blocking finding
    on #670/#671). Sourcing the file before exec (the old shape) would instead put
    it in the watchdog's exec-time environment (dev.finding a0166920)."""
    import launchd_agent

    env_file = tmp_path / "env"
    python = tmp_path / "venv" / "bin" / "python"
    config = launchd_agent.KeeperWatchdogConfig(python=python, env_file=env_file)
    command = launchd_agent.keeper_watchdog_shell_command(config)

    launcher_command = (
        f"exec {python} apps/factory-dispatcher/process_env.py "
        f"--env-file {env_file} apps/factory-dispatcher/tunnel_keeper.py check-heartbeat"
    )
    assert launcher_command in command
    assert "set -a" not in command
    assert "set +a" not in command
    assert command.index(str(python)) < command.index("check-heartbeat")
