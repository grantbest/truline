#!/usr/bin/env python3
"""Supervise the factory's kubectl port-forward tunnel chain.

Until 2026-09-04 the tunnel (substrate, and separately Temporal) was owned by launchd
LaunchAgents (`launchd_agent.py install-tunnel`, PQ-3). Those were stood down because macOS
Local Network privacy denies LAN access to a process launchd starts in the background -- true
even in the `gui/<uid>` Aqua-session domain `install-tunnel` already used: TCC resolves the
"responsible process" for a Local-Network-gated connection by walking the parent chain, and a
launchd-started job has launchd, not an already-permitted foreground app, as that ancestor. No
launchd domain this codebase can select changes which process TCC charges the request to -- see
`.factory/design.md` and `docs/runbooks/factory-tunnel-keeper.md`.

The replacement is this module. An operator starts it once, attended, exactly the way the raw
`kubectl port-forward` used to be started (`nohup ... &`) -- except now there is one supervised
process instead of N unsupervised ones:

    nohup python3 apps/factory-dispatcher/tunnel_keeper.py run \\
        --link substrate-prod > ~/.factory-dispatcher/logs/tunnel-keeper.nohup.log 2>&1 &

`TunnelKeeper` spawns each link's `kubectl port-forward` as its own child (reusing
`launchd_agent.TunnelConfig` -- the same shape `install-tunnel` already validated, so there is
exactly one definition of "what a tunnel link is"), restarts a dead or unreachable link with
backoff, and after every check writes a heartbeat file so a *separate* process
(`schedule_status.py`, or this module's own `check-heartbeat` subcommand) can answer "is the
keeper alive" without an RPC into it -- the same design `worker_revision.py` uses for the
worker's own revision record.

Restart is immediate (bounded only by backoff), because a dead tunnel helps nobody while an
alert threshold elapses. Alerting is threshold+dedup'd, modelled on (not imported from)
`worker.TemporalTunnelAlerter` -- that class's alert copy is hardcoded to "Temporal tunnel",
which would misname a substrate-link failure. Alerts post through `cluster_health.notify` /
`cluster_health.poster_from_env`, the same declared Discord path `schedule_status.py` already
uses, so a tunnel-keeper fault does not need a fourth bespoke webhook client remembered.

`check-heartbeat` is the reboot-surviving half: reading a local file and posting to a public
Discord webhook needs no LAN access, so -- unlike the kubectl process itself -- this check *can*
run unattended under launchd (`launchd_agent.py install-keeper-watchdog`). It cannot restart the
tunnel (nothing running in that context could reach the cluster to do so), but it can and does
say, in the declared alert path, that a human is required and exactly what command to run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import cluster_health
import process_env
from launchd_agent import (
    TUNNEL_DEFAULTS,
    TunnelArgError,
    TunnelConfig,
    temporal_address_reachable,
    tunnel_program_arguments,
)
from worker_revision import STATE_DIR_ENV

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATE_FILE_NAME = "tunnel-keeper.json"
WATCHDOG_STATE_FILE_NAME = "tunnel-keeper-watchdog.json"

DEFAULT_CHECK_INTERVAL_SECONDS = 10
DEFAULT_ALERT_THRESHOLD_SECONDS = 60
DEFAULT_ALERT_DEDUP_SECONDS = 30 * 60
DEFAULT_RESTART_BACKOFF_SECONDS: tuple[int, ...] = (2, 5, 15, 30, 60)
DEFAULT_HEARTBEAT_STALE_MULTIPLE = 3

CHECK_INTERVAL_ENV = "FACTORY_TUNNEL_KEEPER_CHECK_INTERVAL_SECONDS"
ALERT_THRESHOLD_ENV = "FACTORY_TUNNEL_KEEPER_ALERT_THRESHOLD_SECONDS"
ALERT_DEDUP_ENV = "FACTORY_TUNNEL_KEEPER_ALERT_DEDUP_SECONDS"

#: The exact operator recovery command, stated once so every alert and every doc quotes the
#: same thing (README.md and docs/runbooks/factory-tunnel-keeper.md both point back here).
RECOVERY_COMMAND = (
    "nohup python3 apps/factory-dispatcher/tunnel_keeper.py run --link substrate-prod "
    "> ~/.factory-dispatcher/logs/tunnel-keeper.nohup.log 2>&1 &"
)

#: Where the single-instance lock lives. flock releases automatically when the
#: holder dies, so a stale lock from a crashed keeper never wedges a restart --
#: only a genuinely live keeper holds it (release-gate blocking finding on
#: #670/#671: the alert's own recovery command, run beside a live keeper,
#: spawned dueling port-forwards).
DEFAULT_LOCK_PATH = Path.home() / ".factory-dispatcher" / "tunnel-keeper.lock"


class KeeperAlreadyRunning(SystemExit):
    """Raised (as a loud nonzero exit) when another live keeper holds the lock."""

    def __init__(self, holder_pid: str):
        super().__init__(
            f"another tunnel keeper is already running (pid {holder_pid}); refusing to "
            "start a second -- two keepers fight over the same ports and clobber the "
            "heartbeat. Stop it deliberately first (kill <pid>; it terminates its own "
            "kubectl children on SIGTERM)."
        )
        self.holder_pid = holder_pid


def acquire_single_instance_lock(lock_path: Path = DEFAULT_LOCK_PATH):
    """Take the keeper's flock or raise KeeperAlreadyRunning naming the holder.

    Returns the open file object; the caller keeps it referenced for the
    process lifetime -- closing it (or dying) releases the lock, which is the
    stale-lock story: a dead keeper's lock is already free.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "unknown"
        handle.close()
        raise KeeperAlreadyRunning(holder) from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


Notifier = Callable[[list["cluster_health.Notification"]], None]
Reachable = Callable[[str], bool]
Spawn = Callable[..., "subprocess.Popen[bytes]"]
Clock = Callable[[], float]
WallClock = Callable[[], datetime]


def default_state_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the keeper's heartbeat lives -- same state directory the worker's own revision
    record uses (`worker_revision.default_state_path`), so both live under one operator-known
    root (`~/.factory-dispatcher/` or `$FACTORY_DISPATCHER_STATE_DIR`)."""
    values = os.environ if environ is None else environ
    base = values.get(STATE_DIR_ENV)
    directory = Path(base) if base else Path.home() / ".factory-dispatcher"
    return directory / STATE_FILE_NAME


def default_watchdog_state_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    base = values.get(STATE_DIR_ENV)
    directory = Path(base) if base else Path.home() / ".factory-dispatcher"
    return directory / WATCHDOG_STATE_FILE_NAME


def default_notifier(environ: Mapping[str, str] | None = None) -> Notifier:
    poster = cluster_health.poster_from_env(environ)
    return lambda notifications: cluster_health.notify(notifications, poster)


def parse_link_spec(value: str) -> TunnelConfig:
    """Parse `name` or `name:namespace:resource:local_port:remote_port` into a `TunnelConfig`.

    Mirrors `launchd_agent.tunnel_config_from_args`'s refusal to guess: a bare name only
    resolves when `TUNNEL_DEFAULTS` has every field on file (today, only `substrate-prod`).
    Everything else -- `temporal` included, whose namespace/resource are not written down
    anywhere in this repo -- must be spelled out in full.
    """
    parts = value.split(":")
    name = parts[0]
    if name not in TUNNEL_DEFAULTS:
        raise TunnelArgError(
            f"Unknown tunnel name {name!r} in --link {value!r}. Known names: "
            f"{', '.join(sorted(TUNNEL_DEFAULTS))}. Use "
            "name:namespace:resource:local_port:remote_port to supervise a tunnel with no "
            "on-file default."
        )
    defaults = TUNNEL_DEFAULTS[name]
    if len(parts) == 1:
        namespace, resource = defaults["namespace"], defaults["resource"]
        local_port, remote_port = defaults["local_port"], defaults["remote_port"]
    elif len(parts) == 5:
        _, namespace, resource, local_port_raw, remote_port_raw = parts
        try:
            local_port = int(local_port_raw)
            remote_port = int(remote_port_raw)
        except ValueError as exc:
            raise TunnelArgError(
                f"--link {value!r}: local_port and remote_port must be integers"
            ) from exc
    else:
        raise TunnelArgError(
            f"--link {value!r} is not `name` or "
            "`name:namespace:resource:local_port:remote_port`."
        )
    if namespace is None or resource is None:
        raise TunnelArgError(
            f"Refusing to supervise the {name!r} tunnel: no on-file default namespace/resource. "
            f"Pass --link {name}:<namespace>:<resource>:<local_port>:<remote_port> explicitly."
        )
    return TunnelConfig(
        name=name,
        kubectl_args=(
            "-n",
            str(namespace),
            "port-forward",
            str(resource),
            f"{local_port}:{remote_port}",
        ),
        local_address=f"127.0.0.1:{local_port}",
    )


def _with_overrides(link: TunnelConfig, *, kubectl: str, kubeconfig: str | None) -> TunnelConfig:
    return TunnelConfig(
        name=link.name,
        kubectl_args=link.kubectl_args,
        local_address=link.local_address,
        kubectl=kubectl,
        kubeconfig=kubeconfig,
    )


@dataclass
class _LinkRuntime:
    config: TunnelConfig
    process: "subprocess.Popen[bytes] | None" = None
    last_reachable: bool = False
    down_since: float | None = None  # monotonic seconds -- duration math only
    down_since_wall: str | None = None  # ISO 8601 -- what the heartbeat file reports
    alerted_down: bool = False
    last_alert_at: float | None = None  # monotonic seconds
    restart_attempts: int = 0
    next_restart_at: float = 0.0  # monotonic seconds
    unmanaged: bool = False  # port answers but the process is not ours -- defer, never fight


def _format_duration(seconds: float) -> str:
    return cluster_health.format_duration(timedelta(seconds=max(0.0, seconds)))


class TunnelKeeper:
    """Restart-on-death, liveness, and alerting for a fixed set of tunnel links."""

    def __init__(
        self,
        links: Sequence[TunnelConfig],
        *,
        notifier: Notifier | None = None,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = lambda: datetime.now(timezone.utc),
        reachable: Reachable = temporal_address_reachable,
        spawn: Spawn = subprocess.Popen,
        check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS,
        alert_threshold_seconds: int = DEFAULT_ALERT_THRESHOLD_SECONDS,
        alert_dedup_seconds: int = DEFAULT_ALERT_DEDUP_SECONDS,
        restart_backoff_seconds: Sequence[int] = DEFAULT_RESTART_BACKOFF_SECONDS,
        state_path: Path | None = None,
        log_dir: Path | None = None,
        lock_path: Path | None = None,
    ) -> None:
        if not links:
            raise ValueError("TunnelKeeper needs at least one link to supervise")
        self._runtimes = [_LinkRuntime(config=link) for link in links]
        self._notifier = notifier if notifier is not None else default_notifier()
        self._clock = clock
        self._wall_clock = wall_clock
        self._reachable = reachable
        self._spawn = spawn
        self._check_interval_seconds = check_interval_seconds
        self._alert_threshold_seconds = alert_threshold_seconds
        self._alert_dedup_seconds = alert_dedup_seconds
        self._restart_backoff_seconds = tuple(restart_backoff_seconds)
        self._state_path = state_path if state_path is not None else default_state_path()
        self._log_dir = log_dir
        self._lock_path = lock_path if lock_path is not None else DEFAULT_LOCK_PATH
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    def run(self) -> None:
        """Loop until `request_stop()` (a signal handler, or a test) ends it."""
        lock = acquire_single_instance_lock(self._lock_path)
        self._install_signal_handlers()
        try:
            while not self._stopping:
                self.tick()
                time.sleep(self._check_interval_seconds)
        finally:
            self._terminate_all()
            lock.close()

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, frame: Any) -> None:
            logger.info("tunnel keeper received signal %s; stopping", signum)
            self.request_stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _handle)

    def tick(self) -> None:
        """One supervision pass: check every link, restart or alert, then heartbeat.

        The unit this class's tests drive directly -- `run()` is just this in a sleep loop.
        """
        for runtime in self._runtimes:
            self._ensure_link(runtime)
        self._write_heartbeat()

    def _ensure_link(self, runtime: _LinkRuntime) -> None:
        process_alive = runtime.process is not None and runtime.process.poll() is None
        if not process_alive:
            exit_code = runtime.process.poll() if runtime.process is not None else None
            if self._safe_reachable(runtime.config.local_address):
                # The port answers but the process is not ours: an operator's
                # own port-forward, or an orphan from a previous keeper.
                # Traffic flows -- respawning here loses the fight every 15s
                # forever and reports the tunnel down while it is actually up
                # (release-gate blocking finding on #670/#671). Defer and say
                # so; resume managing only when the port actually goes dark.
                if not runtime.unmanaged:
                    logger.warning(
                        "tunnel %s: port %s answers but the serving process is "
                        "not this keeper's -- deferring to it, not respawning",
                        runtime.config.name,
                        runtime.config.local_address,
                    )
                runtime.unmanaged = True
                runtime.last_reachable = True
                self._observe_up(runtime)
                return
            runtime.unmanaged = False
            runtime.last_reachable = False
            self._observe_down(runtime, reason=self._death_reason(runtime, exit_code))
            self._maybe_restart(runtime)
            return
        runtime.unmanaged = False
        healthy = self._safe_reachable(runtime.config.local_address)
        runtime.last_reachable = healthy
        if healthy:
            self._observe_up(runtime)
        else:
            self._observe_down(runtime, reason="process running but port not reachable")

    def _death_reason(self, runtime: _LinkRuntime, exit_code: int | None) -> str:
        if runtime.process is None:
            return "not yet started"
        return f"process exited with code {exit_code}"

    def _safe_reachable(self, address: str) -> bool:
        try:
            return self._reachable(address)
        except Exception:  # noqa: BLE001 - a broken health check reads "down", never crashes the keeper.
            logger.exception("tunnel keeper reachability check raised; treating as down")
            return False

    def _observe_down(self, runtime: _LinkRuntime, *, reason: str) -> None:
        now = self._clock()
        if runtime.down_since is None:
            runtime.down_since = now
            runtime.down_since_wall = self._wall_clock().isoformat()
            runtime.alerted_down = False
        if runtime.alerted_down:
            return
        duration = now - runtime.down_since
        if duration < self._alert_threshold_seconds:
            return
        if self._inside_dedup_window(runtime, now):
            return
        runtime.alerted_down = True
        runtime.last_alert_at = now
        self._post(
            severity="urgent",
            title=f"tunnel link '{runtime.config.name}' is down",
            detail=(
                f"reason={reason} duration={_format_duration(duration)} "
                f"restart_attempts={runtime.restart_attempts}"
            ),
        )

    def _observe_up(self, runtime: _LinkRuntime) -> None:
        was_down = runtime.down_since is not None
        was_alerted = runtime.alerted_down
        now = self._clock()
        duration = (now - runtime.down_since) if was_down and runtime.down_since is not None else 0.0
        runtime.down_since = None
        runtime.down_since_wall = None
        runtime.alerted_down = False
        runtime.restart_attempts = 0
        if was_down and was_alerted:
            self._post(
                severity="info",
                title=f"tunnel link '{runtime.config.name}' recovered",
                detail=f"was down for {_format_duration(duration)}",
            )

    def _inside_dedup_window(self, runtime: _LinkRuntime, now: float) -> bool:
        if runtime.last_alert_at is None:
            return False
        return now - runtime.last_alert_at < self._alert_dedup_seconds

    def _post(self, *, severity: str, title: str, detail: str) -> None:
        try:
            self._notifier(
                [
                    cluster_health.Notification(
                        severity=severity,
                        source="tunnel-keeper",
                        title=title,
                        detail=detail,
                    )
                ]
            )
        except Exception:  # noqa: BLE001 - alerting must never crash the supervision loop.
            logger.exception("tunnel keeper alert failed to post; alert swallowed")

    def _maybe_restart(self, runtime: _LinkRuntime) -> None:
        now = self._clock()
        if now < runtime.next_restart_at:
            return
        backoff_index = min(runtime.restart_attempts, len(self._restart_backoff_seconds) - 1)
        runtime.next_restart_at = now + self._restart_backoff_seconds[backoff_index]
        runtime.restart_attempts += 1
        try:
            runtime.process = self._spawn_link(runtime.config)
            logger.info(
                "tunnel keeper (re)started link %s (attempt %d)",
                runtime.config.name,
                runtime.restart_attempts,
            )
        except Exception:  # noqa: BLE001 - a failed spawn is reported next tick, not raised here.
            logger.exception("tunnel keeper failed to spawn link %s", runtime.config.name)
            runtime.process = None

    def _spawn_link(self, link: TunnelConfig) -> "subprocess.Popen[bytes]":
        argv = list(tunnel_program_arguments(link))
        stdout: Any = subprocess.DEVNULL
        stderr: Any = subprocess.DEVNULL
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stdout = (self._log_dir / f"tunnel-keeper-{link.name}.out.log").open("ab")
            stderr = (self._log_dir / f"tunnel-keeper-{link.name}.err.log").open("ab")
        return self._spawn(
            argv,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            env=process_env.child_env(),
        )

    def _terminate_all(self) -> None:
        for runtime in self._runtimes:
            if runtime.process is not None and runtime.process.poll() is None:
                runtime.process.terminate()
        for runtime in self._runtimes:
            if runtime.process is None:
                continue
            try:
                runtime.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # A hung kubectl survives terminate and then port-conflicts
                # its replacement (release-gate advisory) -- escalate.
                runtime.process.kill()

    def _write_heartbeat(self) -> None:
        payload = {
            "heartbeat_at": self._wall_clock().isoformat(),
            "pid": os.getpid(),
            "links": {
                runtime.config.name: {
                    "reachable": runtime.last_reachable,
                    "process_alive": (
                        runtime.process is not None and runtime.process.poll() is None
                    ),
                    "down_since": runtime.down_since_wall,
                    "unmanaged": runtime.unmanaged,
                    "restart_attempts": runtime.restart_attempts,
                }
                for runtime in self._runtimes
            },
        }
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(path)


@dataclass(frozen=True)
class TunnelKeeperHeartbeat:
    heartbeat_at: datetime
    pid: int | None
    links: dict[str, dict[str, Any]]


def read_heartbeat(path: Path | None = None) -> TunnelKeeperHeartbeat | None:
    """The keeper's most recently written heartbeat, or None if there is none to read.

    Best-effort, matching `worker_revision.read_worker_revision_record`: any parse failure
    reads as "no heartbeat," never as a crash in the caller doing the asking.
    """
    state_path = path if path is not None else default_state_path()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return TunnelKeeperHeartbeat(
            heartbeat_at=_parse_iso(payload["heartbeat_at"]),
            pid=payload.get("pid"),
            links=payload.get("links", {}),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def default_pid_is_running(pid: int) -> bool:
    """Same signal-0 existence probe `worker_revision._default_pid_is_running` uses.

    Public (not underscore-prefixed): `schedule_status.describe_tunnel_keeper_status` reads
    this too, to report whether the keeper's recorded pid is still alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


PidChecker = Callable[[int], bool]


def keeper_looks_alive(
    *,
    state_path: Path | None = None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
    pid_is_running: PidChecker = default_pid_is_running,
) -> tuple[bool, str]:
    """(alive, reason) -- reason is only meaningful when alive is False."""
    path = state_path if state_path is not None else default_state_path()
    now_value = now if now is not None else datetime.now(timezone.utc)
    threshold = (
        max_age_seconds
        if max_age_seconds is not None
        else DEFAULT_CHECK_INTERVAL_SECONDS * DEFAULT_HEARTBEAT_STALE_MULTIPLE
    )

    record = read_heartbeat(path)
    if record is None:
        return False, f"no tunnel keeper heartbeat file found at {path}"
    if record.pid is not None and not pid_is_running(record.pid):
        return False, f"heartbeat names pid {record.pid}, which is not running"
    age_seconds = (now_value - record.heartbeat_at).total_seconds()
    if age_seconds > threshold:
        return False, (
            f"heartbeat is {_format_duration(age_seconds)} old, past the "
            f"{_format_duration(threshold)} threshold"
        )
    return True, ""


def _read_watchdog_previously_alerted(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return bool(payload.get("alerted"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _write_watchdog_alerted(path: Path, alerted: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps({"alerted": alerted}), encoding="utf-8")
    tmp_path.replace(path)


def check_heartbeat(
    *,
    state_path: Path | None = None,
    watchdog_state_path: Path | None = None,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
    notifier: Notifier | None = None,
    pid_is_running: PidChecker = default_pid_is_running,
) -> bool:
    """Read the keeper's heartbeat; alert (urgent-absent or info-recovered) as the state changes.

    This is the half of supervision that *can* run unattended under launchd
    (`launchd_agent.py install-keeper-watchdog`): it touches only a local file and, when it
    alerts, an outbound HTTPS POST to Discord -- neither is gated by the Local Network privacy
    denial that keeps the tunnel itself off launchd. It cannot restart the tunnel; it can and
    does say a human must, and names the exact command.
    """
    notifier = notifier if notifier is not None else default_notifier()
    watchdog_path = (
        watchdog_state_path if watchdog_state_path is not None else default_watchdog_state_path()
    )
    previously_alerted = _read_watchdog_previously_alerted(watchdog_path)

    alive, reason = keeper_looks_alive(
        state_path=state_path,
        max_age_seconds=max_age_seconds,
        now=now,
        pid_is_running=pid_is_running,
    )
    if not alive:
        notifier(
            [
                cluster_health.Notification(
                    severity="urgent",
                    source="tunnel-keeper-watchdog",
                    title="factory tunnel keeper is not running -- a human is required",
                    detail=(
                        f"{reason}. Local Network privacy denies a launchd-managed process the "
                        "LAN access kubectl needs, so this cannot restart itself. Start it from "
                        f"an interactive session: {RECOVERY_COMMAND}"
                    ),
                )
            ]
        )
        _write_watchdog_alerted(watchdog_path, True)
        return False

    if previously_alerted:
        notifier(
            [
                cluster_health.Notification(
                    severity="info",
                    source="tunnel-keeper-watchdog",
                    title="factory tunnel keeper is running again",
                    detail="heartbeat is fresh and its recorded pid is running.",
                )
            ]
        )
    _write_watchdog_alerted(watchdog_path, False)
    return True


def _positive_int_from_env(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of seconds") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Supervise the configured tunnel links until stopped."
    )
    run_parser.add_argument(
        "--link",
        dest="links",
        action="append",
        default=None,
        help=(
            "name or name:namespace:resource:local_port:remote_port. Repeatable. Defaults to "
            "substrate-prod alone (the only on-file default); temporal has no on-file target "
            "and must be given explicitly."
        ),
    )
    run_parser.add_argument("--kubeconfig", default=None)
    run_parser.add_argument("--kubectl", default="kubectl")
    run_parser.add_argument("--check-interval-seconds", type=int, default=None)
    run_parser.add_argument("--alert-threshold-seconds", type=int, default=None)
    run_parser.add_argument("--alert-dedup-seconds", type=int, default=None)
    run_parser.add_argument("--state-path", type=Path, default=None)
    run_parser.add_argument(
        "--log-dir", type=Path, default=Path.home() / ".factory-dispatcher" / "logs"
    )

    heartbeat_parser = subparsers.add_parser(
        "check-heartbeat",
        help="Read the keeper's heartbeat; alert and exit 1 if it looks dead.",
    )
    heartbeat_parser.add_argument("--state-path", type=Path, default=None)
    heartbeat_parser.add_argument("--watchdog-state-path", type=Path, default=None)
    heartbeat_parser.add_argument("--max-age-seconds", type=int, default=None)

    return parser


def _run_command(args: argparse.Namespace) -> int:
    link_specs = args.links or ["substrate-prod"]
    links = [
        _with_overrides(parse_link_spec(spec), kubectl=args.kubectl, kubeconfig=args.kubeconfig)
        for spec in link_specs
    ]
    values = os.environ
    keeper = TunnelKeeper(
        links,
        check_interval_seconds=(
            args.check_interval_seconds
            if args.check_interval_seconds is not None
            else _positive_int_from_env(values, CHECK_INTERVAL_ENV, DEFAULT_CHECK_INTERVAL_SECONDS)
        ),
        alert_threshold_seconds=(
            args.alert_threshold_seconds
            if args.alert_threshold_seconds is not None
            else _positive_int_from_env(values, ALERT_THRESHOLD_ENV, DEFAULT_ALERT_THRESHOLD_SECONDS)
        ),
        alert_dedup_seconds=(
            args.alert_dedup_seconds
            if args.alert_dedup_seconds is not None
            else _positive_int_from_env(values, ALERT_DEDUP_ENV, DEFAULT_ALERT_DEDUP_SECONDS)
        ),
        state_path=args.state_path,
        log_dir=args.log_dir,
    )
    logger.info(
        "tunnel keeper starting: links=%s", ", ".join(link.name for link in links)
    )
    keeper.run()
    return 0


def _check_heartbeat_command(args: argparse.Namespace) -> int:
    alive = check_heartbeat(
        state_path=args.state_path,
        watchdog_state_path=args.watchdog_state_path,
        max_age_seconds=args.max_age_seconds,
    )
    print("tunnel keeper: ALIVE" if alive else "tunnel keeper: NOT RUNNING")
    return 0 if alive else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run_command(args)
        if args.command == "check-heartbeat":
            return _check_heartbeat_command(args)
        parser.error(f"unknown command {args.command}")  # pragma: no cover - argparse prevents this.
        return 2
    except TunnelArgError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
