#!/usr/bin/env python3
"""Install the factory Temporal worker as a launchd agent on Air."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

import containment
import dispatch
import process_env
import worker_identity
from config import (
    REQUIRED_WORKER_IMPORTS,
    missing_required_config,
    missing_required_worker_executables,
    missing_worker_executables_diagnosis,
)
from worker_checkout import default_checkout_root

LABEL = "com.gastown.factory-dispatcher-worker"
TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "launchd"
    / "com.gastown.factory-dispatcher-worker.plist.template"
)

TUNNEL_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "launchd"
    / "com.gastown.factory-dispatcher-tunnel.plist.template"
)

# The kubectl port-forward tunnel itself cannot be launchd-managed (see
# `TunnelConfig`/`install_tunnel` docstrings and docs/runbooks/factory-tunnel-keeper.md): macOS
# Local Network privacy denies LAN access to a process launchd starts in the background, in
# every domain this codebase can select. `tunnel_keeper.py` supervises the tunnel instead, from
# an attended session -- but the watchdog below, which does no LAN work, CAN survive under
# launchd, and is the reboot-surviving half of supervision this host can honestly promise.
KEEPER_WATCHDOG_LABEL = "com.gastown.factory-dispatcher-tunnel-keeper-watchdog"
KEEPER_WATCHDOG_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "launchd"
    / "com.gastown.factory-dispatcher-tunnel-keeper-watchdog.plist.template"
)
DEFAULT_KEEPER_WATCHDOG_START_INTERVAL_SECONDS = 5 * 60

# The only kubectl port-forward target that is on the record anywhere in this
# repo (README.md: "kubectl -n platform-substrate-prod port-forward svc/substrate
# 18001:8000"). Temporal's namespace/resource are deliberately absent: they are
# not written down anywhere, and guessing one would let `install-tunnel --name
# temporal` succeed against a plausible-but-wrong target instead of refusing.
TUNNEL_DEFAULTS: dict[str, dict[str, object]] = {
    "substrate-prod": {
        "namespace": "platform-substrate-prod",
        "resource": "svc/substrate",
        "local_port": 18001,
        "remote_port": 8000,
    },
    "temporal": {
        "namespace": None,
        "resource": None,
        "local_port": 7233,
        "remote_port": 7233,
    },
}


class ConfigError(RuntimeError):
    """The agent cannot be installed without operator action."""


class LaunchctlError(RuntimeError):
    """launchctl failed in a way the installer could not recover from."""


@dataclass(frozen=True)
class LaunchdConfig:
    # Defaults to the factory's own controlled checkout
    # (worker_checkout.default_checkout_root), not the installer's own
    # working tree: the worker must run from a checkout the factory
    # advances deliberately (worker_checkout.py advance), not from whatever
    # branch an operator's shared working tree has checked out (2026-08-25).
    repo_root: Path = field(default_factory=default_checkout_root)
    env_file: Path | None = None
    python: Path = field(default_factory=lambda: Path(sys.executable))
    log_dir: Path = field(
        default_factory=lambda: Path.home() / ".factory-dispatcher" / "logs"
    )
    launch_agents_dir: Path = field(
        default_factory=lambda: Path.home() / "Library" / "LaunchAgents"
    )
    gui_domain: str = field(default_factory=lambda: f"gui/{os.getuid()}")

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{LABEL}.plist"

    @property
    def stdout_path(self) -> Path:
        return self.log_dir / "factory-dispatcher-worker.out.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_dir / "factory-dispatcher-worker.err.log"


LaunchctlRunner = Callable[[Sequence[str], bool], None]
TemporalReachability = Callable[[str], bool]
DependencyChecker = Callable[[LaunchdConfig, Mapping[str, str]], None]
LaunchctlPrintRunner = Callable[[str], str]
TunnelDefaultContextChecker = Callable[[], bool]


@dataclass(frozen=True)
class TunnelConfig:
    """A single kubectl port-forward tunnel supervised as its own launchd agent."""

    name: str
    kubectl_args: tuple[str, ...]
    local_address: str
    kubectl: str = "kubectl"
    kubeconfig: str | None = None
    log_dir: Path = field(
        default_factory=lambda: Path.home() / ".factory-dispatcher" / "logs"
    )
    launch_agents_dir: Path = field(
        default_factory=lambda: Path.home() / "Library" / "LaunchAgents"
    )
    gui_domain: str = field(default_factory=lambda: f"gui/{os.getuid()}")

    @property
    def label(self) -> str:
        return f"com.gastown.factory-dispatcher-tunnel-{self.name}"

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.label}.plist"

    @property
    def stdout_path(self) -> Path:
        return self.log_dir / f"factory-dispatcher-tunnel-{self.name}.out.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_dir / f"factory-dispatcher-tunnel-{self.name}.err.log"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse the shell-compatible KEY=value subset used by the worker env file.

    Delegates to process_env.parse_env_file (the one implementation of this grammar;
    see dev.finding a0166920), translating its ProcessEnvError into this module's
    ConfigError so every existing caller and test keeps seeing the same exception type.
    """
    try:
        return process_env.parse_env_file(path)
    except process_env.ProcessEnvError as exc:
        raise ConfigError(str(exc)) from exc


def effective_environment(
    config: LaunchdConfig,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = dict(os.environ if environ is None else environ)
    if config.env_file is not None:
        values.update(parse_env_file(config.env_file))
    return values


def validate_required_config(values: Mapping[str, str]) -> None:
    missing = missing_required_config(values)
    if missing:
        names = ", ".join(missing)
        raise ConfigError(
            "Refusing to install the factory worker LaunchAgent. Missing required "
            f"configuration: {names}. Provide these values in the process environment "
            "or in --env-file; the installer will not write secrets into the plist."
        )


def validate_env_file_has_no_pythonpath(config: LaunchdConfig) -> None:
    if config.env_file is None:
        return
    values = parse_env_file(config.env_file)
    if "PYTHONPATH" not in values:
        return
    raise ConfigError(
        "Refusing to install the factory worker LaunchAgent. Env file "
        f"{config.env_file} sets PYTHONPATH. Remove PYTHONPATH from the env file; "
        "--python must point at an interpreter with the worker requirements installed."
    )


def temporal_host_port(address: str) -> tuple[str, int]:
    parsed = urlparse(address if "://" in address else f"//{address}")
    if not parsed.hostname:
        raise ConfigError(f"TEMPORAL_URL does not contain a host: {address!r}")
    try:
        port = parsed.port or 7233
    except ValueError as exc:
        raise ConfigError(f"TEMPORAL_URL has an invalid port: {address!r}") from exc
    return parsed.hostname, port


def temporal_address_reachable(address: str, timeout_seconds: float = 2.0) -> bool:
    host, port = temporal_host_port(address)
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def validate_temporal_reachable(
    values: Mapping[str, str],
    reachable: TemporalReachability = temporal_address_reachable,
) -> None:
    address = values["TEMPORAL_URL"]
    if not reachable(address):
        raise ConfigError(
            "Refusing to install the factory worker LaunchAgent. Temporal address "
            f"{address!r} is not reachable. Start and verify the kubectl port-forward "
            "first; installing now would put launchd into a Client.connect crash-loop."
        )


def validate_worker_executable_dependencies(
    values: Mapping[str, str],
    *,
    required: Sequence[str] | None = None,
) -> None:
    if required is None:
        from dispatch import required_worker_executables

        required = required_worker_executables()
    missing = missing_required_worker_executables(values, required=required)
    if not missing:
        return
    raise ConfigError(
        "Refusing to install the factory worker LaunchAgent. "
        f"{missing_worker_executables_diagnosis(missing, values)}. "
        "Install the missing command-line tool or set PATH in the launchd env file "
        "to the directories the worker should search."
    )


def validate_worker_dependencies(
    config: LaunchdConfig,
    values: Mapping[str, str] | None = None,
) -> None:
    script = """
import importlib
import sys

for module_name in sys.argv[1:]:
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        print(exc.name or module_name, file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"{module_name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
"""
    env = process_env.child_env()
    env.pop("PYTHONPATH", None)
    try:
        proc = subprocess.run(
            [str(config.python), "-c", script, *REQUIRED_WORKER_IMPORTS],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise ConfigError(
            "Refusing to install the factory worker LaunchAgent. Could not run "
            f"Python interpreter {config.python}: {exc}"
        ) from exc
    if proc.returncode == 0:
        validate_worker_executable_dependencies(
            effective_environment(config) if values is None else values
        )
        return
    missing_import = (proc.stderr or proc.stdout or "unknown import").strip()
    raise ConfigError(
        "Refusing to install the factory worker LaunchAgent. Python interpreter "
        f"{config.python} cannot import worker dependency {missing_import!r}. "
        "Install the worker requirements into that interpreter or pass --python "
        "pointing at the virtualenv interpreter."
    )


def shell_command(config: LaunchdConfig) -> str:
    """Render the worker's ProgramArguments shell command.

    Never sources the env file (dev.finding a0166920: `set -a; . <file>; set +a`
    before `exec` puts every credential the file names into the worker's exec-time
    environment, which any same-uid process can read through KERN_PROCARGS2 /
    /proc/<pid>/environ). Instead the launcher, process_env.py, loads the env file
    itself AFTER exec.
    """
    parts = [f"cd {shlex.quote(str(config.repo_root))}"]
    if config.env_file is not None:
        parts.append(
            "exec "
            f"{shlex.quote(str(config.python))} "
            "apps/factory-dispatcher/process_env.py "
            f"--env-file {shlex.quote(str(config.env_file))} "
            "apps/factory-dispatcher/worker.py"
        )
    else:
        parts.append(
            "exec "
            f"{shlex.quote(str(config.python))} "
            "apps/factory-dispatcher/worker.py"
        )
    return "; ".join(parts)


def render_plist(config: LaunchdConfig) -> str:
    template = Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    rendered = template.substitute(
        LABEL=html.escape(LABEL, quote=True),
        SHELL_COMMAND=html.escape(shell_command(config), quote=True),
        STDOUT_PATH=html.escape(str(config.stdout_path), quote=True),
        STDERR_PATH=html.escape(str(config.stderr_path), quote=True),
    )
    plistlib.loads(rendered.encode("utf-8"))
    return rendered


def run_launchctl(args: Sequence[str], check: bool = True) -> None:
    proc = subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        env=process_env.child_env(),
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise LaunchctlError(
            f"launchctl {' '.join(args)} failed with exit {proc.returncode}: {detail}"
        )


def kickstart_worker(
    config: LaunchdConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> None:
    """Restart the running worker so it loads whatever `worker_checkout.advance()` just checked out.

    `kickstart -k`, not `bootout`+`bootstrap`: the plist and the checkout path it names are
    unchanged -- only the process needs to reload -- the same primitive
    `scripts/factory-redeploy.py`'s own kickstart step already uses after a manual advance.
    """
    launchctl(["kickstart", "-k", f"{config.gui_domain}/{LABEL}"], True)


class LaunchdWorkerSupervisor:
    """`worker_checkout_drift_response.WorkerSupervisor` backed by the real launchd agent.

    The one place `worker_checkout_drift_response`'s injected `supervisor.recycle()` becomes
    an actual `launchctl kickstart` -- see `activities/worker_revision_drift.py`, which wires
    this in for the scheduled drift check. Tests of the decision logic use a fake instead;
    this class itself needs no test beyond `kickstart_worker`'s own, since it is a one-line
    pass-through.
    """

    def __init__(
        self,
        config: LaunchdConfig | None = None,
        *,
        launchctl: LaunchctlRunner = run_launchctl,
    ) -> None:
        self._config = config or LaunchdConfig()
        self._launchctl = launchctl

    def recycle(self) -> None:
        kickstart_worker(self._config, launchctl=self._launchctl)


def install(
    config: LaunchdConfig,
    *,
    environ: Mapping[str, str] | None = None,
    launchctl: LaunchctlRunner = run_launchctl,
    temporal_reachable: TemporalReachability = temporal_address_reachable,
    dependency_checker: DependencyChecker = validate_worker_dependencies,
) -> Path:
    values = effective_environment(config, environ)
    validate_required_config(values)
    validate_env_file_has_no_pythonpath(config)
    validate_temporal_reachable(values, temporal_reachable)
    dependency_checker(config, values)

    plist_text = render_plist(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.plist_path.write_text(plist_text, encoding="utf-8")

    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    launchctl(["bootstrap", config.gui_domain, str(config.plist_path)], True)
    return config.plist_path


def uninstall(
    config: LaunchdConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    config.plist_path.unlink(missing_ok=True)
    return config.plist_path


def tunnel_program_arguments(config: TunnelConfig) -> tuple[str, ...]:
    if config.kubeconfig is not None:
        return (config.kubectl, "--kubeconfig", config.kubeconfig, *config.kubectl_args)
    return (config.kubectl, *config.kubectl_args)


def render_tunnel_plist(config: TunnelConfig) -> str:
    template = Template(TUNNEL_TEMPLATE_PATH.read_text(encoding="utf-8"))
    arguments_xml = "\n".join(
        f"    <string>{html.escape(argument, quote=True)}</string>"
        for argument in tunnel_program_arguments(config)
    )
    rendered = template.substitute(
        LABEL=html.escape(config.label, quote=True),
        PROGRAM_ARGUMENTS_XML=arguments_xml,
        STDOUT_PATH=html.escape(str(config.stdout_path), quote=True),
        STDERR_PATH=html.escape(str(config.stderr_path), quote=True),
    )
    plistlib.loads(rendered.encode("utf-8"))
    return rendered


def default_kubeconfig_has_context() -> bool:
    """True when `kubectl config current-context` resolves, i.e. ambient kubectl auth works.

    The factory host deliberately has no default context (this deployment's cluster
    configs live in separate, explicitly-named kubeconfig files instead), so this is
    False there unless the operator has set one - which is exactly the case
    install_tunnel must detect before writing a plist that would otherwise crash-loop
    under KeepAlive.
    """
    proc = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        env=process_env.child_env(),
    )
    return proc.returncode == 0


def validate_tunnel_kubeconfig(
    config: TunnelConfig,
    *,
    default_context_checker: TunnelDefaultContextChecker = default_kubeconfig_has_context,
) -> None:
    """Refuse before writing a plist that would spawn into a silent KeepAlive crash-loop.

    A launchd-spawned kubectl has no shell and no operator-made wrapper script to pin
    --kubeconfig for it (PRIN-008: fail loud at install time, not by restarting every
    ThrottleInterval seconds forever).
    """
    if config.kubeconfig is not None:
        return
    if default_context_checker():
        return
    raise ConfigError(
        f"Refusing to install the {config.name!r} tunnel LaunchAgent. No --kubeconfig "
        "was given and this host has no default kubectl context "
        "(`kubectl config current-context` failed). Pass --kubeconfig PATH pointing at "
        "the cluster's kubeconfig file, or set a default context first with "
        "`kubectl config use-context <name>`."
    )


def install_tunnel(
    config: TunnelConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
    default_context_checker: TunnelDefaultContextChecker = default_kubeconfig_has_context,
) -> Path:
    validate_tunnel_kubeconfig(config, default_context_checker=default_context_checker)

    plist_text = render_tunnel_plist(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.plist_path.write_text(plist_text, encoding="utf-8")

    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    launchctl(["bootstrap", config.gui_domain, str(config.plist_path)], True)
    launchctl(["kickstart", "-k", f"{config.gui_domain}/{config.label}"], True)
    return config.plist_path


def uninstall_tunnel(
    config: TunnelConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    config.plist_path.unlink(missing_ok=True)
    return config.plist_path


@dataclass(frozen=True)
class KeeperWatchdogConfig:
    """The reboot-surviving half of tunnel supervision: a periodic, LAN-free liveness check.

    Deliberately carries no kubeconfig, namespace, resource, or port -- unlike `TunnelConfig`,
    this LaunchAgent never touches the cluster. It shells out to
    `tunnel_keeper.py check-heartbeat`, which reads a local file and, at most, posts to a public
    Discord webhook.
    """

    repo_root: Path = field(default_factory=default_checkout_root)
    python: Path = field(default_factory=lambda: Path(sys.executable))
    #: launchd inherits no shell environment, so without sourcing this file the
    #: watchdog's alert poster finds no DISCORD_WEBHOOK_URL and every
    #: "a human is required" alert silently downgrades to a log line -- the
    #: exact incident class this supervision exists to end (release-gate
    #: blocking finding on both #670 and #671).
    env_file: Path | None = field(
        default_factory=lambda: Path.home() / ".factory-dispatcher" / "env"
    )
    log_dir: Path = field(
        default_factory=lambda: Path.home() / ".factory-dispatcher" / "logs"
    )
    launch_agents_dir: Path = field(
        default_factory=lambda: Path.home() / "Library" / "LaunchAgents"
    )
    gui_domain: str = field(default_factory=lambda: f"gui/{os.getuid()}")
    max_heartbeat_age_seconds: int | None = None
    start_interval_seconds: int = DEFAULT_KEEPER_WATCHDOG_START_INTERVAL_SECONDS

    @property
    def label(self) -> str:
        return KEEPER_WATCHDOG_LABEL

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{KEEPER_WATCHDOG_LABEL}.plist"

    @property
    def stdout_path(self) -> Path:
        return self.log_dir / "factory-dispatcher-tunnel-keeper-watchdog.out.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_dir / "factory-dispatcher-tunnel-keeper-watchdog.err.log"


def keeper_watchdog_shell_command(config: KeeperWatchdogConfig) -> str:
    """Render the keeper watchdog's ProgramArguments shell command.

    Same rationale as shell_command() above: launchd provides no environment, and the
    poster needs DISCORD_WEBHOOK_URL to be loud -- but the env file is loaded by the
    process_env.py launcher AFTER exec, never sourced into the exec-time environment.
    """
    tail = "apps/factory-dispatcher/tunnel_keeper.py check-heartbeat"
    if config.max_heartbeat_age_seconds is not None:
        tail += f" --max-age-seconds {config.max_heartbeat_age_seconds}"
    if config.env_file is not None:
        command = (
            f"exec {shlex.quote(str(config.python))} "
            "apps/factory-dispatcher/process_env.py "
            f"--env-file {shlex.quote(str(config.env_file))} "
            f"{tail}"
        )
    else:
        command = f"exec {shlex.quote(str(config.python))} {tail}"
    parts = [f"cd {shlex.quote(str(config.repo_root))}", command]
    return "; ".join(parts)


def render_keeper_watchdog_plist(config: KeeperWatchdogConfig) -> str:
    template = Template(KEEPER_WATCHDOG_TEMPLATE_PATH.read_text(encoding="utf-8"))
    rendered = template.substitute(
        LABEL=html.escape(config.label, quote=True),
        SHELL_COMMAND=html.escape(keeper_watchdog_shell_command(config), quote=True),
        START_INTERVAL_SECONDS=str(int(config.start_interval_seconds)),
        STDOUT_PATH=html.escape(str(config.stdout_path), quote=True),
        STDERR_PATH=html.escape(str(config.stderr_path), quote=True),
    )
    plistlib.loads(rendered.encode("utf-8"))
    return rendered


def install_keeper_watchdog(
    config: KeeperWatchdogConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    plist_text = render_keeper_watchdog_plist(config)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    config.plist_path.write_text(plist_text, encoding="utf-8")

    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    launchctl(["bootstrap", config.gui_domain, str(config.plist_path)], True)
    launchctl(["kickstart", "-k", f"{config.gui_domain}/{config.label}"], True)
    return config.plist_path


def uninstall_keeper_watchdog(
    config: KeeperWatchdogConfig,
    *,
    launchctl: LaunchctlRunner = run_launchctl,
) -> Path:
    launchctl(["bootout", config.gui_domain, str(config.plist_path)], False)
    config.plist_path.unlink(missing_ok=True)
    return config.plist_path


def launchctl_print(target: str) -> str:
    proc = subprocess.run(
        ["launchctl", "print", target],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        env=process_env.child_env(),
    )
    return proc.stdout


def parse_launchctl_state(output: str) -> str | None:
    """The value of launchctl print's `state = ...` line, or None if absent."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            return stripped[len("state = ") :].strip()
    return None


def verify_tunnel_running(
    config: TunnelConfig,
    *,
    print_runner: LaunchctlPrintRunner = launchctl_print,
    reachable: TemporalReachability = temporal_address_reachable,
) -> bool:
    """True only when launchd reports the job running AND the port answers.

    `state = running` alone is not proof: a kubectl process can report running
    moments before it exits, or before the forward has finished its handshake.
    Requiring both signals is what makes this verification RUNNING rather than
    merely installed.
    """
    state = parse_launchctl_state(print_runner(f"{config.gui_domain}/{config.label}"))
    return state == "running" and reachable(config.local_address)


class TunnelArgError(ConfigError):
    """The tunnel CLI was not given enough information to build a target."""


def tunnel_config_from_args(args: argparse.Namespace) -> TunnelConfig:
    defaults = TUNNEL_DEFAULTS[args.tunnel_name]
    namespace = args.namespace or defaults["namespace"]
    resource = args.resource or defaults["resource"]
    if namespace is None or resource is None:
        raise TunnelArgError(
            f"Refusing to build the {args.tunnel_name!r} tunnel LaunchAgent. "
            "--namespace and --resource are required for this tunnel; there is "
            "no on-file kubectl target for it. Provide the same values you would "
            "pass to `kubectl -n <namespace> port-forward <resource> <ports>`."
        )
    local_port = args.local_port or defaults["local_port"]
    remote_port = args.remote_port or defaults["remote_port"]
    return TunnelConfig(
        name=args.tunnel_name,
        kubectl_args=(
            "-n",
            str(namespace),
            "port-forward",
            str(resource),
            f"{local_port}:{remote_port}",
        ),
        local_address=f"127.0.0.1:{local_port}",
        kubectl=args.kubectl,
        kubeconfig=args.kubeconfig,
        log_dir=args.log_dir.expanduser().resolve(),
        launch_agents_dir=args.launch_agents_dir.expanduser().resolve(),
    )


def tunnel_config_from_installed_plist(
    name: str,
    *,
    log_dir: Path,
    launch_agents_dir: Path,
) -> TunnelConfig | None:
    """Reconstruct a TunnelConfig from the plist install-tunnel actually wrote.

    verify-tunnel must confirm what was installed, not what the operator remembers passing
    at install time; the rendered plist is the only durable record of the installed target,
    so this reads it back instead of requiring --namespace/--resource/--ports again. Returns
    None when no plist exists at the expected path, distinguishing "not installed" from
    "installed but not running".
    """
    label = f"com.gastown.factory-dispatcher-tunnel-{name}"
    plist_path = launch_agents_dir / f"{label}.plist"
    if not plist_path.exists():
        return None
    plist = plistlib.loads(plist_path.read_bytes())
    arguments = list(plist.get("ProgramArguments", []))
    if not arguments:
        raise ConfigError(f"Installed plist {plist_path} has no ProgramArguments to verify.")
    kubectl, *rest = arguments
    kubeconfig = None
    if "--kubeconfig" in rest:
        index = rest.index("--kubeconfig")
        kubeconfig = rest[index + 1]
        rest = rest[:index] + rest[index + 2 :]
    if not rest:
        raise ConfigError(f"Installed plist {plist_path} has no port-forward target to verify.")
    ports = rest[-1]
    local_port = ports.split(":", 1)[0]
    return TunnelConfig(
        name=name,
        kubectl_args=tuple(rest),
        local_address=f"127.0.0.1:{local_port}",
        kubectl=kubectl,
        kubeconfig=kubeconfig,
        log_dir=log_dir,
        launch_agents_dir=launch_agents_dir,
    )


def absolute_path_without_resolving(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path(os.path.abspath(expanded))


def config_from_args(args: argparse.Namespace) -> LaunchdConfig:
    env_file = args.env_file
    if env_file is None and os.environ.get("FACTORY_DISPATCHER_ENV_FILE"):
        env_file = Path(os.environ["FACTORY_DISPATCHER_ENV_FILE"])
    return LaunchdConfig(
        repo_root=args.repo_root.resolve(),
        env_file=env_file.expanduser().resolve() if env_file is not None else None,
        python=absolute_path_without_resolving(args.python),
        log_dir=args.log_dir.expanduser().resolve(),
        launch_agents_dir=args.launch_agents_dir.expanduser().resolve(),
    )


# ---------------------------------------------------------------------------
# Part B, B3 (design docs/plans/2026-09-26-design-part-b-worker-runs-as-separate-user.md
# §8 row B3, predecessor B2): install-worker-identity, the dispatcher-side hash check and
# re-copy, and probe-worker-identity. Nothing here runs anything as the worker user itself
# -- it copies files the operator account already owns, and builds the sudo call B1
# (worker_identity.py) already exposes, handing it to an injected runner. No new
# process-spawning call is added in this module: the one sudo spawn stays owned by
# worker_identity.run_launch_call (B1), which already carries its own
# tests/test_spawn_env_fixture.py CASES entry.
#
# R2603-5 (scripts/check-repo-invariants.py's no-household-identifiers rule) forbids a
# compiled-in operator account name in this file, so unlike the design note's hardcoded
# literal, the operator account the sudoers rule names is read from
# OPERATOR_ACCOUNT_ENV (or passed explicitly) -- an environment variable this module
# refuses to render the sudoers text without, never a hardcoded default.
# ---------------------------------------------------------------------------

# --- AC-1: the golden sudoers and PAM texts, printed only ---------------------

#: The exact paths the operator installs these at by hand (design §3.1/§3.2); never read
#: or written by any code path in this bead.
SUDOERS_INSTALL_PATH = "/etc/sudoers.d/factory-worker"
PAM_INSTALL_PATH = "/etc/pam.d/factory-worker"

#: R2603-5: the macOS account allowed to invoke the launcher via sudo (design §3.1's
#: hardcoded operator name) is configuration, not a compiled-in default.
OPERATOR_ACCOUNT_ENV = "FACTORY_OPERATOR_ACCOUNT"


def _require_operator_account(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    operator = values.get(OPERATOR_ACCOUNT_ENV)
    if not operator:
        raise ConfigError(
            "Refusing to render the worker-identity sudoers rule: "
            f"{OPERATOR_ACCOUNT_ENV} is not set. R2603-5 forbids a compiled-in operator "
            "account name in the platform core; pass --operator or set this in the "
            "environment before running install-worker-identity."
        )
    return operator


def render_worker_sudoers_text(user: str, launcher_path: str, operator: str) -> str:
    """design §3.1's rule, byte for byte: a lower-privilege runas, never root and never
    the operator account itself, with sudo's own I/O logging turned off so the stdin
    credential payload never reaches sudo's log."""
    return (
        f"# {SUDOERS_INSTALL_PATH}   root:wheel 0440, installed with visudo\n"
        f"Defaults>{user} pam_service=factory-worker, !pam_setcred, !log_input, "
        "!log_output\n"
        f"{operator} ALL=({user}) NOPASSWD: {launcher_path}\n"
    )


def render_worker_pam_text() -> str:
    """design §3.2's PAM service -- the only mechanism found that moves a sudo-started
    process out of the operator account's gui/501 Mach bootstrap namespace. Whether it
    actually does so on this sudo/PAM version is unverified until the attended probe's
    item (a)."""
    return (
        f"# {PAM_INSTALL_PATH}   root:wheel 0644\n"
        "auth       required   pam_deny.so\n"
        "account    required   pam_permit.so\n"
        "password   required   pam_deny.so\n"
        "session    required   pam_launchd.so\n"
    )


def print_worker_identity_texts(
    user: str, launcher_path: str, operator: str, stdout=None
) -> None:
    """Prints (never writes) the sudoers and PAM texts, under headings naming where the
    operator installs each and with which command. No code path reachable from here
    touches /etc."""
    stdout = sys.stdout if stdout is None else stdout
    print(f"--- {SUDOERS_INSTALL_PATH} ---", file=stdout)
    print(f"# The operator installs this with: sudo visudo -f {SUDOERS_INSTALL_PATH}", file=stdout)
    print("# paste the text below, save, then run: sudo visudo -c", file=stdout)
    print(render_worker_sudoers_text(user, launcher_path, operator), file=stdout, end="")
    print(file=stdout)
    print(f"--- {PAM_INSTALL_PATH} ---", file=stdout)
    print(
        "# The operator installs this with: sudo install -m 0644 -o root -g wheel <file> "
        f"{PAM_INSTALL_PATH}",
        file=stdout,
    )
    print(render_worker_pam_text(), file=stdout, end="")


# --- AC-2: install copies and records -----------------------------------------

#: Where this checkout's own copies of the launcher and process_env.py live -- the running
#: checkout `install-worker-identity` copies FROM (design §10 step 10).
_CHECKOUT_DIR = Path(__file__).resolve().parent

LAUNCHER_INSTALLED_NAME = "factory-worker-launch"
PROCESS_ENV_INSTALLED_NAME = "process_env.py"
CLAUDE_INSTALLED_NAME = "claude"
WORKER_USER_FILE_NAME = ".worker-user"

#: (source file in this checkout, installed name, installed mode). Shared by
#: install_worker_identity and check_installed_worker_identity's default recopy.
WORKER_IDENTITY_SOURCE_FILES: tuple[tuple[str, str, int], ...] = (
    ("worker_launcher.py", LAUNCHER_INSTALLED_NAME, 0o755),
    ("process_env.py", PROCESS_ENV_INSTALLED_NAME, 0o644),
)
_SOURCE_FILE_MODES: dict[str, int] = {
    source_name: mode for source_name, _dest_name, mode in WORKER_IDENTITY_SOURCE_FILES
}


@dataclass(frozen=True)
class InstalledFile:
    path: Path
    mode: int
    sha256: str


@dataclass(frozen=True)
class WorkerIdentityInstallResult:
    user: str
    files: tuple[InstalledFile, ...]
    worker_user_file: Path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_owned_directory(path: Path, flag: str) -> None:
    if not path.is_dir():
        raise ConfigError(
            f"Refusing to install worker identity artifacts: {flag} {path} is not a "
            "directory."
        )
    if path.stat().st_uid != os.geteuid():
        raise ConfigError(
            f"Refusing to install worker identity artifacts: {flag} {path} is not owned "
            f"by the effective uid {os.geteuid()}."
        )


def _require_safe_destination(path: Path) -> None:
    """Refuse any existing destination that is not a plain regular file: a symlink, a
    directory, a FIFO, or anything else os.replace() could be pointed at unsafely. A
    pre-existing regular file (the ordinary re-install case) is left for the atomic
    write to replace."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return
    if stat.S_ISREG(mode):
        return
    if stat.S_ISLNK(mode):
        kind = "a symlink"
    elif stat.S_ISDIR(mode):
        kind = "a directory"
    elif stat.S_ISFIFO(mode):
        kind = "a FIFO"
    else:
        kind = "not a regular file"
    raise ConfigError(
        f"Refusing to install worker identity artifacts: {path} already exists as "
        f"{kind}."
    )


def _require_bin_dir_not_writable_by_others(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"Refusing to install worker identity artifacts: --bin {path} is group- or "
            f"world-writable (mode {oct(mode)}); the launcher is imported from this "
            "directory."
        )


def _require_runs_root_has_no_group_or_other_bits(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"Refusing to install worker identity artifacts: --runs-root {path} grants "
            f"group or other permissions (mode {oct(mode)})."
        )


#: design docs don't give an account-name grammar; this is the macOS short-name shape
#: (lowercase, digits, underscore, hyphen, <=32 chars, never starting with a digit or
#: hyphen). "ALL" is already excluded by the lowercase-only pattern; "root" matches it
#: lexically and is refused separately below.
ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _validate_account_name(name: str, flag: str) -> None:
    if not ACCOUNT_NAME_PATTERN.match(name):
        raise ConfigError(
            f"Refusing to install worker identity artifacts: {flag} {name!r} is not a "
            f"valid macOS short account name (must match {ACCOUNT_NAME_PATTERN.pattern!r})."
        )
    if name == "root":
        raise ConfigError(
            f"Refusing to install worker identity artifacts: {flag} may not be 'root'."
        )


def _atomic_write_bytes(dest: Path, data: bytes, mode: int) -> None:
    """Writes ``data`` to ``dest`` without ever rewriting an existing file's inode in
    place: rewriting an executed Mach-O under a running process SIGKILLs it on its next
    exec. Creates a sibling temp file with O_CREAT|O_EXCL|O_NOFOLLOW at ``mode`` (via
    os.fchmod on the fd, so umask cannot weaken it), fsyncs it, then os.replace()s it
    onto ``dest`` -- the replace swaps the directory entry's inode rather than mutating
    the old one."""
    tmp_path = dest.parent / f".{dest.name}.{os.urandom(8).hex()}.tmp"
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(dest))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def install_worker_identity(
    user: str,
    bin_dir: Path,
    runs_root: Path,
    *,
    operator: str | None = None,
    claude_resolver: Callable[[str], str | None] = shutil.which,
    stdout=None,
) -> WorkerIdentityInstallResult:
    """design §10 step 10: copies the launcher, process_env.py and the current claude
    binary into an operator-owned bin, writes ``<runs_root>/.worker-user``, and PRINTS
    the sudoers and PAM texts -- it never writes under /etc. Every check runs before any
    write, so a refusal (AC-2) leaves the filesystem exactly as it found it. ``operator``
    defaults to ``OPERATOR_ACCOUNT_ENV`` (R2603-5: never a compiled-in default)."""
    stdout = sys.stdout if stdout is None else stdout
    _require_owned_directory(bin_dir, "--bin")
    _require_owned_directory(runs_root, "--runs-root")
    _require_bin_dir_not_writable_by_others(bin_dir)
    _require_runs_root_has_no_group_or_other_bits(runs_root)
    operator_account = operator if operator is not None else _require_operator_account()
    _validate_account_name(user, "--user")
    _validate_account_name(operator_account, "--operator")
    if user == operator_account:
        raise ConfigError(
            "Refusing to install worker identity artifacts: --user and --operator must "
            f"not name the same account ({user!r})."
        )

    claude_name = dispatch.WORKER_REGISTRY[dispatch.DEFAULT_WORKER].argv[0]
    claude_source = claude_resolver(claude_name)
    if claude_source is None:
        raise ConfigError(
            f"Refusing to install worker identity artifacts: {claude_name!r} was not "
            "found on PATH."
        )
    # This host's `claude` is a symlink into ~/.local/share; the sudo rule names a path
    # under the worker uid's reach, which must be the real binary, not a symlink only
    # the operator's own uid can resolve.
    claude_real = Path(os.path.realpath(claude_source))
    if not claude_real.is_file():
        raise ConfigError(
            f"Refusing to install worker identity artifacts: resolved claude binary "
            f"{claude_real} is not a regular file."
        )

    worker_user_path = runs_root / WORKER_USER_FILE_NAME
    destinations = [bin_dir / dest_name for _src, dest_name, _mode in WORKER_IDENTITY_SOURCE_FILES]
    destinations.append(bin_dir / CLAUDE_INSTALLED_NAME)
    destinations.append(worker_user_path)
    for destination in destinations:
        _require_safe_destination(destination)

    installed: list[InstalledFile] = []
    for source_name, dest_name, mode in WORKER_IDENTITY_SOURCE_FILES:
        data = (_CHECKOUT_DIR / source_name).read_bytes()
        dest = bin_dir / dest_name
        _atomic_write_bytes(dest, data, mode)
        installed.append(InstalledFile(dest, mode, _sha256_bytes(data)))

    claude_data = claude_real.read_bytes()
    claude_dest = bin_dir / CLAUDE_INSTALLED_NAME
    _atomic_write_bytes(claude_dest, claude_data, 0o755)
    installed.append(InstalledFile(claude_dest, 0o755, _sha256_bytes(claude_data)))

    _atomic_write_bytes(worker_user_path, user.encode("utf-8"), 0o600)

    print(f"Installed worker identity artifacts for user {user!r}:", file=stdout)
    for entry in installed:
        print(f"  {entry.path} (mode {oct(entry.mode)}, sha256 {entry.sha256})", file=stdout)
    print(f"  {worker_user_path} (mode 0o600): {user}", file=stdout)
    print(file=stdout)
    print_worker_identity_texts(
        user, str(worker_identity.LAUNCHER_PATH), operator_account, stdout
    )

    return WorkerIdentityInstallResult(
        user=user, files=tuple(installed), worker_user_file=worker_user_path
    )


# --- AC-3: the dispatcher-side hash check and re-copy -------------------------


def _default_read_installed_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _default_stat_installed(path: Path) -> os.stat_result:
    return os.lstat(path)


def _default_recopy_installed(source_name: str, dest: Path) -> None:
    data = (_CHECKOUT_DIR / source_name).read_bytes()
    _atomic_write_bytes(dest, data, _SOURCE_FILE_MODES[source_name])


@dataclass(frozen=True)
class HashCheckOps:
    read: Callable[[Path], bytes | None] = _default_read_installed_bytes
    stat: Callable[[Path], os.stat_result] = _default_stat_installed
    copy: Callable[[str, Path], None] = _default_recopy_installed


@dataclass(frozen=True)
class FileCheckResult:
    outcome: str  # "match" | "recopied" | "failed"
    reason: str | None = None


def check_installed_worker_identity(
    bin_dir: Path,
    hashes: tuple[str | None, str | None] | None = None,
    *,
    ops: HashCheckOps | None = None,
) -> dict[str, FileCheckResult]:
    """design §3.4: the dispatcher-side hash check and automatic re-copy. Pure apart from
    ``ops`` -- ``bin`` is operator-owned and the sudoers rule names a path, not a hash, so
    a re-copy needs no admin. Compares against ``hashes`` (defaulting to
    ``worker_identity.source_hashes()``, B1's import-time measurement) via B1's own
    ``compare_hash``, never a second, independent comparator.

    The claim-side decision this feeds -- pausing the dispatch schedule in the
    capacity-pause shape (``_pause_schedule_for_capacity_failure``,
    ``activities/dispatch_steps.py:1582``) when a file comes back ``"failed"`` -- is B4b's;
    this function only reports per-file outcomes for that caller to act on.
    """
    ops = ops or HashCheckOps()
    if hashes is None:
        hashes = worker_identity.source_hashes()
    launcher_hash, process_env_hash = hashes

    results: dict[str, FileCheckResult] = {}
    for source_name, dest_name, expected_hash in (
        ("worker_launcher.py", LAUNCHER_INSTALLED_NAME, launcher_hash),
        ("process_env.py", PROCESS_ENV_INSTALLED_NAME, process_env_hash),
    ):
        dest_path = bin_dir / dest_name
        comparison = worker_identity.compare_hash(expected_hash, ops.read(dest_path))
        if comparison == "match":
            results[source_name] = FileCheckResult("match")
            continue

        try:
            dest_stat = ops.stat(dest_path)
        except OSError:
            dest_stat = None
        if dest_stat is not None and stat.S_ISLNK(dest_stat.st_mode):
            results[source_name] = FileCheckResult(
                "failed", f"{dest_path} exists as a symlink; refusing to overwrite"
            )
            continue

        try:
            ops.copy(source_name, dest_path)
        except OSError as exc:
            results[source_name] = FileCheckResult("failed", f"recopy failed: {exc}")
            continue

        if worker_identity.compare_hash(expected_hash, ops.read(dest_path)) == "match":
            results[source_name] = FileCheckResult("recopied")
        else:
            results[source_name] = FileCheckResult(
                "failed", "hash still does not match after recopy"
            )
    return results


# --- AC-4: probe and selfcheck through the launcher ---------------------------

SELFCHECK_DEADLINE_S = 30.0
PROBE_DEADLINE_S = 60.0

#: design §10 step 13's own lettering and what each item measures. A label only -- this
#: command decides nothing.
PROBE_ITEM_LABELS: dict[str, str] = {
    "a": "(a) Which session: launchctl managername must not print Aqua; "
    "bootstrap_look_up(com.apple.windowserver.active) must return 1102",
    "b": "(b) EACCES on credential paths (env file, ~/.kube, ~/.ssh, /tmp/claude-501)",
    "c": "(c) EINVAL from the ctypes sysctl mib [1, 49, <worker.py pid>]",
    "d": '(d) security find-generic-password for "Claude Code-credentials" must fail',
    "e_version": "(e) claude --version",
    "e": "(e) NOT RUN: this bead sends no claude payload, so the authenticated "
    "claude -p call is not exercised",
    "f": "(f) git --version and python3.12 -I -c 'import ssl'",
    "g": "(g) launchctl submit of a no-op job -- NOT MEASURED IN THE WORKER PROFILE: "
    "this bead runs it unsandboxed",
    "h": "(h) launchctl bootstrap user/850 of a no-op plist -- NOT MEASURED IN THE "
    "WORKER PROFILE: this bead runs it unsandboxed with an empty plist",
    "i": "(i) bootstrap_look_up of each mach name: must return 1100 or 1102",
}
PROBE_ITEM_ORDER: tuple[str, ...] = ("a", "b", "c", "d", "e_version", "e", "f", "g", "h", "i")

#: design §10 step 13's closure precondition for dev.finding 75b50b58, verbatim. Printed
#: for the operator to read and act on; never evaluated or branched on here.
CLOSURE_PRECONDITION_TEXT = (
    "Closure precondition for dev.finding 75b50b58 (design docs/plans/"
    "2026-09-26-design-part-b-worker-runs-as-separate-user.md §10, step 13): (a) "
    "passes, (g) and (h) created no job in gui/501, and (i) returns only 1100 or 1102. "
    "If any fails, closure is blocked. This command decides nothing -- record the result "
    "by hand, and file a finding if it fails."
)


def default_probe_fields() -> dict[str, object]:
    """Real credential paths and mach-lookup names for an attended probe run. No test
    relies on these specific values: the golden-argv/env tests only cover what
    ``build_launch_call`` itself produces, which does not depend on payload content."""
    home = Path.home()
    return {
        "paths": [str(home / ".ssh"), str(home / ".kube"), f"/tmp/claude-{os.getuid()}"],
        "mach_names": [*containment.MACH_DENY, "com.apple.windowserver.active"],
        "target_pid": os.getpid(),
    }


def build_selfcheck_launch_call(cfg: worker_identity.WorkerIdentityConfig) -> worker_identity.LaunchCall:
    payload = worker_identity.build_payload("selfcheck")
    return worker_identity.build_launch_call(
        cfg, "selfcheck", cfg.runs_root, json.dumps(payload).encode("utf-8"), SELFCHECK_DEADLINE_S,
    )


def build_probe_launch_call(
    cfg: worker_identity.WorkerIdentityConfig, *, fields: Mapping[str, object] | None = None
) -> worker_identity.LaunchCall:
    payload = worker_identity.build_payload(
        "probe", fields=fields if fields is not None else default_probe_fields()
    )
    return worker_identity.build_launch_call(
        cfg, "probe", cfg.runs_root, json.dumps(payload).encode("utf-8"), PROBE_DEADLINE_S,
    )


def _parse_envelope_bytes(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unparseable", "raw": raw.decode("utf-8", "replace")}


def _print_envelope(label: str, envelope: dict, stdout) -> None:
    print(f"=== {label} ===", file=stdout)
    print(json.dumps(envelope, indent=2), file=stdout)


def _print_probe_observations(envelope: dict, stdout) -> None:
    if envelope.get("status") != "ok":
        return
    try:
        observations = json.loads(envelope.get("stdout") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(observations, dict):
        return
    print(file=stdout)
    print("--- observations (design §10 step 13) ---", file=stdout)
    for key in PROBE_ITEM_ORDER:
        if key not in observations:
            continue
        print(f"{PROBE_ITEM_LABELS[key]}:", file=stdout)
        print(json.dumps(observations[key], indent=2), file=stdout)


def run_selfcheck_probe(
    cfg: worker_identity.WorkerIdentityConfig,
    *,
    runner: Callable[[worker_identity.LaunchCall], subprocess.CompletedProcess] = (
        worker_identity.run_launch_call
    ),
    stdout=None,
) -> dict:
    """design §10 step 12: ``probe-worker-identity --selfcheck``. Decides nothing -- the
    attended operator reads the printed envelope."""
    stdout = sys.stdout if stdout is None else stdout
    call = build_selfcheck_launch_call(cfg)
    proc = runner(call)
    envelope = _parse_envelope_bytes(proc.stdout)
    _print_envelope("probe-worker-identity --selfcheck", envelope, stdout)
    print(file=stdout)
    print(CLOSURE_PRECONDITION_TEXT, file=stdout)
    return envelope


def run_worker_probe(
    cfg: worker_identity.WorkerIdentityConfig,
    *,
    runner: Callable[[worker_identity.LaunchCall], subprocess.CompletedProcess] = (
        worker_identity.run_launch_call
    ),
    stdout=None,
) -> dict:
    """design §10 step 13: ``probe-worker-identity``, attended. Decides nothing -- every
    item is printed, labelled, for the operator to read and record."""
    stdout = sys.stdout if stdout is None else stdout
    call = build_probe_launch_call(cfg)
    proc = runner(call)
    envelope = _parse_envelope_bytes(proc.stdout)
    _print_envelope("probe-worker-identity", envelope, stdout)
    _print_probe_observations(envelope, stdout)
    print(file=stdout)
    print(CLOSURE_PRECONDITION_TEXT, file=stdout)
    return envelope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install or remove the Air launchd agent for the factory Temporal worker."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--repo-root",
            type=Path,
            default=default_checkout_root(),
            help=(
                "Checkout the worker should run from -- the factory's own "
                "controlled checkout (see worker_checkout.py advance), not "
                "the operator's working tree."
            ),
        )
        subparser.add_argument(
            "--env-file",
            type=Path,
            default=None,
            help=(
                "Shell-compatible env file containing SUBSTRATE_URL, "
                "SUBSTRATE_API_KEY and TEMPORAL_URL. The installer reads but "
                "does not create this file."
            ),
        )
        subparser.add_argument(
            "--python",
            type=Path,
            default=Path(sys.executable),
            help="Python interpreter used to start worker.py.",
        )
        subparser.add_argument(
            "--log-dir",
            type=Path,
            default=Path.home() / ".factory-dispatcher" / "logs",
            help="Directory for worker stdout/stderr logs.",
        )
        subparser.add_argument(
            "--launch-agents-dir",
            type=Path,
            default=Path.home() / "Library" / "LaunchAgents",
            help="LaunchAgents directory to write into.",
        )

    add_common(subparsers.add_parser("install", help="Render, load and start the agent."))
    add_common(subparsers.add_parser("uninstall", help="Unload and remove the agent."))

    def add_tunnel_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--name",
            dest="tunnel_name",
            choices=sorted(TUNNEL_DEFAULTS),
            required=True,
            help="Which tunnel to supervise.",
        )
        subparser.add_argument(
            "--namespace",
            default=None,
            help=(
                "kubectl -n namespace for the port-forward. Has a documented "
                "default for substrate-prod; required for temporal, which has "
                "no on-file target."
            ),
        )
        subparser.add_argument(
            "--resource",
            default=None,
            help="kubectl port-forward resource, e.g. svc/substrate.",
        )
        subparser.add_argument(
            "--local-port",
            type=int,
            default=None,
            help="Local port the tunnel listens on.",
        )
        subparser.add_argument(
            "--remote-port",
            type=int,
            default=None,
            help="Remote port the tunnel forwards to.",
        )
        subparser.add_argument(
            "--kubectl",
            default="kubectl",
            help="kubectl executable to run.",
        )
        subparser.add_argument(
            "--kubeconfig",
            default=None,
            help=(
                "Path to the kubeconfig kubectl should use for this tunnel. Required "
                "for install-tunnel unless the host has a default kubectl context "
                "(`kubectl config current-context` succeeds). Rendered into "
                "ProgramArguments as an explicit --kubeconfig flag; a kubeconfig path "
                "is not a secret."
            ),
        )
        subparser.add_argument(
            "--log-dir",
            type=Path,
            default=Path.home() / ".factory-dispatcher" / "logs",
            help="Directory for tunnel stdout/stderr logs.",
        )
        subparser.add_argument(
            "--launch-agents-dir",
            type=Path,
            default=Path.home() / "Library" / "LaunchAgents",
            help="LaunchAgents directory to write into.",
        )

    add_tunnel_common(
        subparsers.add_parser("install-tunnel", help="Render, load, start and verify a tunnel agent.")
    )
    add_tunnel_common(
        subparsers.add_parser("uninstall-tunnel", help="Unload and remove a tunnel agent.")
    )
    add_tunnel_common(
        subparsers.add_parser("verify-tunnel", help="Report whether a tunnel agent is RUNNING.")
    )

    def add_keeper_watchdog_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--repo-root",
            type=Path,
            default=default_checkout_root(),
            help="Checkout the watchdog should cd into before running tunnel_keeper.py.",
        )
        subparser.add_argument(
            "--python",
            type=Path,
            default=Path(sys.executable),
            help="Python interpreter used to run tunnel_keeper.py check-heartbeat.",
        )
        subparser.add_argument(
            "--max-heartbeat-age-seconds",
            type=int,
            default=None,
            help="Passed through as tunnel_keeper.py check-heartbeat --max-age-seconds.",
        )
        subparser.add_argument(
            "--start-interval-seconds",
            type=int,
            default=DEFAULT_KEEPER_WATCHDOG_START_INTERVAL_SECONDS,
            help="How often launchd re-runs the check while the session stays logged in.",
        )
        subparser.add_argument(
            "--log-dir",
            type=Path,
            default=Path.home() / ".factory-dispatcher" / "logs",
            help="Directory for watchdog stdout/stderr logs.",
        )
        subparser.add_argument(
            "--launch-agents-dir",
            type=Path,
            default=Path.home() / "Library" / "LaunchAgents",
            help="LaunchAgents directory to write into.",
        )

    add_keeper_watchdog_common(
        subparsers.add_parser(
            "install-keeper-watchdog",
            help=(
                "Render, load and start the LAN-free watchdog that alerts when the tunnel "
                "keeper looks dead."
            ),
        )
    )
    add_keeper_watchdog_common(
        subparsers.add_parser(
            "uninstall-keeper-watchdog", help="Unload and remove the keeper watchdog agent."
        )
    )

    installer = subparsers.add_parser(
        "install-worker-identity",
        help=(
            "Copy the launcher, process_env.py and the claude binary into an "
            "operator-owned bin, write <runs-root>/.worker-user, and PRINT the sudoers "
            "and PAM texts (never installs them -- that is the operator's, by hand)."
        ),
    )
    installer.add_argument(
        "--user", required=True, help="The worker account the sudoers rule runs as, e.g. _factoryworker."
    )
    installer.add_argument(
        "--bin",
        dest="bin_dir",
        type=Path,
        default=Path(worker_identity.LAUNCHER_PATH).parent,
        help="Directory to copy the launcher, process_env.py and claude into.",
    )
    installer.add_argument(
        "--runs-root",
        type=Path,
        default=Path(worker_identity.RUNS_ROOT),
        help="Directory to write .worker-user into.",
    )
    installer.add_argument(
        "--operator",
        default=None,
        help=(
            "The macOS account the sudoers rule grants (R2603-5: no compiled-in "
            f"default); falls back to ${OPERATOR_ACCOUNT_ENV} when omitted."
        ),
    )

    prober = subparsers.add_parser(
        "probe-worker-identity",
        help="Run selfcheck or the attended runbook probe through the launcher, as the worker user.",
    )
    prober.add_argument(
        "--user", required=True, help="The worker account the sudoers rule runs as, e.g. _factoryworker."
    )
    prober.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run selfcheck (design §10 step 12) instead of the full attended probe (step 13).",
    )

    return parser


def keeper_watchdog_config_from_args(args: argparse.Namespace) -> KeeperWatchdogConfig:
    return KeeperWatchdogConfig(
        repo_root=args.repo_root.resolve(),
        python=absolute_path_without_resolving(args.python),
        log_dir=args.log_dir.expanduser().resolve(),
        launch_agents_dir=args.launch_agents_dir.expanduser().resolve(),
        max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
        start_interval_seconds=args.start_interval_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            config = config_from_args(args)
            path = install(config)
            print(f"Installed and loaded {LABEL}: {path}")
        elif args.command == "uninstall":
            config = config_from_args(args)
            path = uninstall(config)
            print(f"Unloaded and removed {LABEL}: {path}")
        elif args.command == "install-tunnel":
            tunnel_config = tunnel_config_from_args(args)
            install_tunnel(tunnel_config)
            running = verify_tunnel_running(tunnel_config)
            print(f"{tunnel_config.label}: {'RUNNING' if running else 'NOT YET RUNNING'}")
            if not running:
                return 1
        elif args.command == "uninstall-tunnel":
            tunnel_config = tunnel_config_from_args(args)
            path = uninstall_tunnel(tunnel_config)
            print(f"Unloaded and removed {tunnel_config.label}: {path}")
        elif args.command == "verify-tunnel":
            tunnel_config = tunnel_config_from_installed_plist(
                args.tunnel_name,
                log_dir=args.log_dir.expanduser().resolve(),
                launch_agents_dir=args.launch_agents_dir.expanduser().resolve(),
            )
            if tunnel_config is None:
                label = f"com.gastown.factory-dispatcher-tunnel-{args.tunnel_name}"
                print(f"{label}: NOT INSTALLED")
                return 1
            running = verify_tunnel_running(tunnel_config)
            print(f"{tunnel_config.label}: {'RUNNING' if running else 'NOT RUNNING'}")
            if not running:
                return 1
        elif args.command == "install-keeper-watchdog":
            watchdog_config = keeper_watchdog_config_from_args(args)
            path = install_keeper_watchdog(watchdog_config)
            print(f"Installed and loaded {watchdog_config.label}: {path}")
        elif args.command == "uninstall-keeper-watchdog":
            watchdog_config = keeper_watchdog_config_from_args(args)
            path = uninstall_keeper_watchdog(watchdog_config)
            print(f"Unloaded and removed {watchdog_config.label}: {path}")
        elif args.command == "install-worker-identity":
            install_worker_identity(
                args.user, args.bin_dir, args.runs_root, operator=args.operator
            )
        elif args.command == "probe-worker-identity":
            cfg = worker_identity.WorkerIdentityConfig(user=args.user)
            if args.selfcheck:
                run_selfcheck_probe(cfg)
            else:
                run_worker_probe(cfg)
        else:  # pragma: no cover - argparse prevents this.
            parser.error(f"unknown command {args.command}")
    except (ConfigError, LaunchctlError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
