"""Tests for the Air launchd supervisor installer's kubectl tunnel agents (PQ-3)."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launchd_agent  # noqa: E402


def tunnel_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        tunnel_name="substrate-prod",
        namespace=None,
        resource=None,
        local_port=None,
        remote_port=None,
        kubectl="kubectl",
        kubeconfig=None,
        log_dir=Path("/tmp/does-not-matter/logs"),
        launch_agents_dir=Path("/tmp/does-not-matter/LaunchAgents"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_tunnel_config_from_args_uses_documented_substrate_default(tmp_path):
    config = launchd_agent.tunnel_config_from_args(
        tunnel_args(
            tunnel_name="substrate-prod",
            log_dir=tmp_path / "logs",
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )

    assert config.name == "substrate-prod"
    assert config.kubectl_args == (
        "-n",
        "platform-substrate-prod",
        "port-forward",
        "svc/substrate",
        "18001:8000",
    )
    assert config.local_address == "127.0.0.1:18001"
    assert config.label == "com.gastown.factory-dispatcher-tunnel-substrate-prod"


def test_tunnel_config_from_args_refuses_temporal_without_namespace_and_resource(tmp_path):
    with pytest.raises(launchd_agent.TunnelArgError, match="--namespace and --resource"):
        launchd_agent.tunnel_config_from_args(
            tunnel_args(
                tunnel_name="temporal",
                log_dir=tmp_path / "logs",
                launch_agents_dir=tmp_path / "LaunchAgents",
            )
        )


def test_tunnel_config_from_args_accepts_explicit_temporal_target(tmp_path):
    config = launchd_agent.tunnel_config_from_args(
        tunnel_args(
            tunnel_name="temporal",
            namespace="platform-temporal-prod",
            resource="svc/temporal-frontend",
            log_dir=tmp_path / "logs",
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )

    assert config.kubectl_args == (
        "-n",
        "platform-temporal-prod",
        "port-forward",
        "svc/temporal-frontend",
        "7233:7233",
    )
    assert config.local_address == "127.0.0.1:7233"
    assert config.label == "com.gastown.factory-dispatcher-tunnel-temporal"


def test_tunnel_config_from_args_overrides_ports(tmp_path):
    config = launchd_agent.tunnel_config_from_args(
        tunnel_args(
            tunnel_name="substrate-prod",
            local_port=28001,
            remote_port=9000,
            log_dir=tmp_path / "logs",
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )

    assert config.kubectl_args[-1] == "28001:9000"
    assert config.local_address == "127.0.0.1:28001"


def test_render_tunnel_plist_program_arguments_and_keep_alive_policy(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    plist_text = launchd_agent.render_tunnel_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["Label"] == "com.gastown.factory-dispatcher-tunnel-substrate-prod"
    assert plist["ProgramArguments"] == [
        "kubectl",
        "-n",
        "platform-substrate-prod",
        "port-forward",
        "svc/substrate",
        "18001:8000",
    ]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardOutPath"] == str(
        tmp_path / "logs" / "factory-dispatcher-tunnel-substrate-prod.out.log"
    )
    assert plist["StandardErrorPath"] == str(
        tmp_path / "logs" / "factory-dispatcher-tunnel-substrate-prod.err.log"
    )


def test_render_tunnel_plist_for_temporal_uses_temporal_label_and_args(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="temporal",
        kubectl_args=("-n", "platform-temporal-prod", "port-forward", "svc/temporal-frontend", "7233:7233"),
        local_address="127.0.0.1:7233",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    plist_text = launchd_agent.render_tunnel_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["Label"] == "com.gastown.factory-dispatcher-tunnel-temporal"
    assert plist["ProgramArguments"] == [
        "kubectl",
        "-n",
        "platform-temporal-prod",
        "port-forward",
        "svc/temporal-frontend",
        "7233:7233",
    ]
    assert plist["KeepAlive"] is True


def test_install_tunnel_is_idempotent_kicks_start_and_leaves_one_loaded_agent(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
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

    first = launchd_agent.install_tunnel(
        config, launchctl=fake_launchctl, default_context_checker=lambda: True
    )
    second = launchd_agent.install_tunnel(
        config, launchctl=fake_launchctl, default_context_checker=lambda: True
    )

    assert first == second == config.plist_path
    assert loaded == {str(config.plist_path)}
    assert config.log_dir.is_dir()
    assert config.plist_path.exists()
    assert [call[0] for call in launchctl_calls] == [
        "bootout",
        "bootstrap",
        "kickstart",
        "bootout",
        "bootstrap",
        "kickstart",
    ]
    assert launchctl_calls[2] == ("kickstart", "-k", "gui/501/com.gastown.factory-dispatcher-tunnel-substrate-prod")


def test_uninstall_tunnel_unloads_and_removes_plist(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )
    launchctl_calls: list[tuple[str, ...]] = []
    launchd_agent.install_tunnel(
        config,
        launchctl=lambda args, check=True: launchctl_calls.append(tuple(args)),
        default_context_checker=lambda: True,
    )
    assert config.plist_path.exists()

    launchctl_calls.clear()
    path = launchd_agent.uninstall_tunnel(
        config, launchctl=lambda args, check=True: launchctl_calls.append(tuple(args))
    )

    assert path == config.plist_path
    assert not config.plist_path.exists()
    assert launchctl_calls == [("bootout", "gui/501", str(config.plist_path))]


def test_parse_launchctl_state_reads_state_line():
    output = "\n".join(
        [
            "com.gastown.factory-dispatcher-tunnel-substrate-prod = {",
            "\tactive count = 1",
            "\tstate = running",
            "}",
        ]
    )
    assert launchd_agent.parse_launchctl_state(output) == "running"


def test_parse_launchctl_state_returns_none_when_absent():
    assert launchd_agent.parse_launchctl_state("no state line here") is None


def test_verify_tunnel_running_requires_both_state_and_reachability(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    def running_print(_target):
        return "state = running"

    def waiting_print(_target):
        return "state = waiting"

    assert launchd_agent.verify_tunnel_running(
        config, print_runner=running_print, reachable=lambda _address: True
    )
    assert not launchd_agent.verify_tunnel_running(
        config, print_runner=running_print, reachable=lambda _address: False
    )
    assert not launchd_agent.verify_tunnel_running(
        config, print_runner=waiting_print, reachable=lambda _address: True
    )
    assert not launchd_agent.verify_tunnel_running(
        config, print_runner=lambda _target: "", reachable=lambda _address: True
    )


def test_tunnel_config_from_args_carries_explicit_kubeconfig(tmp_path):
    config = launchd_agent.tunnel_config_from_args(
        tunnel_args(
            tunnel_name="substrate-prod",
            kubeconfig="/Users/op/.kube/config-cluster-a",
            log_dir=tmp_path / "logs",
            launch_agents_dir=tmp_path / "LaunchAgents",
        )
    )
    assert config.kubeconfig == "/Users/op/.kube/config-cluster-a"


def test_render_tunnel_plist_invokes_kubectl_with_explicit_kubeconfig_flag(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="temporal",
        kubectl_args=("-n", "platform-temporal-prod", "port-forward", "svc/temporal-frontend", "7233:7233"),
        local_address="127.0.0.1:7233",
        kubeconfig="/Users/op/.kube/config-cluster-a",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    plist_text = launchd_agent.render_tunnel_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["ProgramArguments"] == [
        "kubectl",
        "--kubeconfig",
        "/Users/op/.kube/config-cluster-a",
        "-n",
        "platform-temporal-prod",
        "port-forward",
        "svc/temporal-frontend",
        "7233:7233",
    ]


def test_render_tunnel_plist_omits_kubeconfig_flag_when_none_given(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    plist_text = launchd_agent.render_tunnel_plist(config)
    plist = plistlib.loads(plist_text.encode("utf-8"))

    assert plist["ProgramArguments"] == [
        "kubectl",
        "-n",
        "platform-substrate-prod",
        "port-forward",
        "svc/substrate",
        "18001:8000",
    ]


def test_install_tunnel_refuses_without_kubeconfig_when_no_default_context(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="temporal",
        kubectl_args=("-n", "platform-temporal-prod", "port-forward", "svc/temporal-frontend", "7233:7233"),
        local_address="127.0.0.1:7233",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    with pytest.raises(launchd_agent.ConfigError, match="--kubeconfig") as excinfo:
        launchd_agent.install_tunnel(
            config,
            launchctl=lambda args, check=True: pytest.fail("launchctl must not run"),
            default_context_checker=lambda: False,
        )
    assert "default context" in str(excinfo.value)
    assert not config.plist_path.exists()


def test_install_tunnel_succeeds_without_kubeconfig_when_default_context_exists(tmp_path):
    config = launchd_agent.TunnelConfig(
        name="temporal",
        kubectl_args=("-n", "platform-temporal-prod", "port-forward", "svc/temporal-frontend", "7233:7233"),
        local_address="127.0.0.1:7233",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )

    launchd_agent.install_tunnel(
        config,
        launchctl=lambda args, check=True: None,
        default_context_checker=lambda: True,
    )

    plist = plistlib.loads(config.plist_path.read_bytes())
    assert "--kubeconfig" not in plist["ProgramArguments"]


def test_verify_tunnel_reconstructs_config_from_installed_plist_without_flags(tmp_path):
    installed = launchd_agent.TunnelConfig(
        name="temporal",
        kubectl_args=("-n", "platform-temporal-prod", "port-forward", "svc/temporal-frontend", "7233:7233"),
        local_address="127.0.0.1:7233",
        kubeconfig="/Users/op/.kube/config-cluster-a",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
        gui_domain="gui/501",
    )
    launchd_agent.install_tunnel(
        installed,
        launchctl=lambda args, check=True: None,
        default_context_checker=lambda: False,
    )

    reconstructed = launchd_agent.tunnel_config_from_installed_plist(
        "temporal",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert reconstructed is not None
    assert reconstructed.kubectl_args == installed.kubectl_args
    assert reconstructed.local_address == installed.local_address
    assert reconstructed.kubeconfig == installed.kubeconfig
    assert reconstructed.label == installed.label

    def running_print(_target):
        return "state = running"

    assert launchd_agent.verify_tunnel_running(
        reconstructed, print_runner=running_print, reachable=lambda _address: True
    )


def test_verify_tunnel_reports_not_installed_when_plist_missing(tmp_path):
    reconstructed = launchd_agent.tunnel_config_from_installed_plist(
        "temporal",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )
    assert reconstructed is None


def test_install_tunnel_does_not_touch_worker_label_or_plist_path(tmp_path):
    worker_config = launchd_agent.LaunchdConfig(
        repo_root=tmp_path / "repo",
        env_file=None,
        python=Path("/usr/bin/python3"),
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )
    tunnel_config = launchd_agent.TunnelConfig(
        name="substrate-prod",
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", "18001:8000"),
        local_address="127.0.0.1:18001",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "LaunchAgents",
    )

    assert worker_config.plist_path != tunnel_config.plist_path
    assert launchd_agent.LABEL not in tunnel_config.label
    assert tunnel_config.label != launchd_agent.LABEL
