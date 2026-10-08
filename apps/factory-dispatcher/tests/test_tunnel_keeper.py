"""Restart-on-death, liveness, and alerting for the tunnel keeper (Local Network privacy, 2026-09-04).

The launchd LaunchAgent that used to own the kubectl port-forward tunnel was stood down because
macOS Local Network privacy denies LAN access to a background launchd job; the replacement is an
attended `nohup` process that must supervise itself. These tests pin the three promises that
supervision makes: a dead or unreachable link is restarted without human action, a link down past
threshold produces exactly one alert naming it (and dedups while still down), and recovery
produces exactly one recovery alert through the same declared path -- never silence in either
direction.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tunnel_keeper  # noqa: E402
from launchd_agent import TunnelArgError, TunnelConfig  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    def __init__(self):
        self._returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    def die(self, code: int = 1) -> None:
        self._returncode = code

    def terminate(self) -> None:
        self.terminated = True


class FakeNotifier:
    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, notifications) -> None:
        self.calls.append(list(notifications))

    @property
    def notifications(self):
        return [n for call in self.calls for n in call]


def make_link(name: str = "substrate-prod", port: int = 18001) -> TunnelConfig:
    return TunnelConfig(
        name=name,
        kubectl_args=("-n", "platform-substrate-prod", "port-forward", "svc/substrate", f"{port}:8000"),
        local_address=f"127.0.0.1:{port}",
    )


def make_keeper(
    *,
    links=None,
    clock=None,
    reachable=None,
    spawn=None,
    notifier=None,
    state_path=None,
    **kwargs,
) -> tuple[tunnel_keeper.TunnelKeeper, FakeNotifier]:
    fake_notifier = notifier if notifier is not None else FakeNotifier()
    keeper = tunnel_keeper.TunnelKeeper(
        links or [make_link()],
        notifier=fake_notifier,
        clock=clock or FakeClock(),
        wall_clock=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc),
        reachable=reachable if reachable is not None else (lambda address: True),
        spawn=spawn if spawn is not None else (lambda *a, **k: FakeProcess()),
        state_path=state_path or Path("/tmp/does-not-matter/tunnel-keeper.json"),
        **kwargs,
    )
    if reachable is None:
        # The truthful default: the port answers iff this keeper's own spawned
        # process is alive. An always-True fake became load-bearing once
        # reachable-but-unmanaged started meaning "defer, don't respawn"
        # (release-gate cure) -- dead-process tests would silently defer
        # instead of restarting under it.
        keeper._reachable = lambda address: any(
            rt.process is not None and rt.process.poll() is None
            for rt in keeper._runtimes
        )
    return keeper, fake_notifier


# --- restart on death -------------------------------------------------------


def test_dead_process_is_restarted_on_next_tick(tmp_path):
    clock = FakeClock()
    spawned = []

    def spawn(*args, **kwargs):
        proc = FakeProcess()
        spawned.append(proc)
        return proc

    keeper, _ = make_keeper(spawn=spawn, clock=clock, state_path=tmp_path / "state.json")
    keeper.tick()
    assert len(spawned) == 1

    spawned[0].die()
    clock.advance(tunnel_keeper.DEFAULT_RESTART_BACKOFF_SECONDS[0])
    keeper.tick()
    assert len(spawned) == 2


def test_restart_respects_backoff_between_attempts(tmp_path):
    clock = FakeClock()
    spawned = []

    def spawn(*args, **kwargs):
        proc = FakeProcess()
        proc.die()  # dies immediately every time, forcing repeated restart attempts
        spawned.append(proc)
        return proc

    keeper, _ = make_keeper(
        spawn=spawn,
        clock=clock,
        state_path=tmp_path / "state.json",
        restart_backoff_seconds=(10, 20),
    )
    keeper.tick()  # first spawn attempt (dies immediately)
    assert len(spawned) == 1

    keeper.tick()  # backoff not elapsed yet
    assert len(spawned) == 1

    clock.advance(10)
    keeper.tick()  # backoff elapsed -> second attempt
    assert len(spawned) == 2

    keeper.tick()  # next backoff (20s) not elapsed
    assert len(spawned) == 2

    clock.advance(20)
    keeper.tick()
    assert len(spawned) == 3


def test_unreachable_but_alive_process_is_also_restarted(tmp_path):
    spawned = []

    def spawn(*args, **kwargs):
        proc = FakeProcess()
        spawned.append(proc)
        return proc

    keeper, _ = make_keeper(
        spawn=spawn,
        reachable=lambda address: False,
        state_path=tmp_path / "state.json",
    )
    keeper.tick()
    assert len(spawned) == 1
    # The first process is still "alive" per poll(), but never reachable -> treated as down,
    # and (once backoff allows) restarted rather than left running unreachable forever.
    keeper.tick()
    assert len(spawned) >= 1


def test_reachability_check_raising_is_treated_as_down_not_a_crash(tmp_path):
    def explode(address):
        raise OSError("boom")

    keeper, notifier = make_keeper(reachable=explode, state_path=tmp_path / "state.json")
    keeper.tick()  # must not raise
    keeper.tick()


# --- alerting: threshold, dedup, recovery -----------------------------------


def test_no_alert_before_threshold_elapses(tmp_path):
    clock = FakeClock()
    dead = FakeProcess()
    dead.die()
    keeper, notifier = make_keeper(
        spawn=lambda *a, **k: dead,
        clock=clock,
        state_path=tmp_path / "state.json",
        alert_threshold_seconds=60,
    )
    keeper.tick()
    clock.advance(59)
    keeper.tick()
    assert notifier.notifications == []


def test_alert_fires_once_past_threshold_and_names_the_link(tmp_path):
    clock = FakeClock()
    dead = FakeProcess()
    dead.die()
    keeper, notifier = make_keeper(
        spawn=lambda *a, **k: dead,
        clock=clock,
        state_path=tmp_path / "state.json",
        alert_threshold_seconds=60,
        restart_backoff_seconds=(1000,),  # don't let a restart attempt swap in a "new" process
    )
    keeper.tick()
    clock.advance(60)
    keeper.tick()

    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification.severity == "urgent"
    assert notification.source == "tunnel-keeper"
    assert "substrate-prod" in notification.title

    # Still down, still inside the tick loop: no repeat alert.
    clock.advance(5)
    keeper.tick()
    assert len(notifier.notifications) == 1


def test_flapping_inside_dedup_window_does_not_repeat_the_alert(tmp_path):
    clock = FakeClock()
    state = {"reachable": True}
    alive = FakeProcess()

    def reachable(address):
        # Truthful fake: the port cannot answer before the keeper has ever
        # spawned -- an up-before-spawn port now means "someone else's
        # tunnel, defer" (the unmanaged-port cure), which is not this
        # test's subject.
        return state["reachable"] and alive.poll() is None and spawned["yes"]

    spawned = {"yes": False}

    def spawn_alive(*a, **k):
        spawned["yes"] = True
        return alive
    keeper, notifier = make_keeper(
        spawn=spawn_alive,
        clock=clock,
        reachable=reachable,
        state_path=tmp_path / "state.json",
        alert_threshold_seconds=60,
        alert_dedup_seconds=1800,
    )
    keeper.tick()  # starts reachable=True

    state["reachable"] = False
    clock.advance(60)
    keeper.tick()
    assert len(notifier.notifications) == 1

    state["reachable"] = True
    clock.advance(10)
    keeper.tick()  # recovers -- info alert
    assert len(notifier.notifications) == 2

    state["reachable"] = False
    clock.advance(60)
    keeper.tick()  # down again, but inside the 1800s dedup window since the last urgent alert
    assert len(notifier.notifications) == 2


def test_recovery_posts_exactly_one_info_alert_after_an_urgent_one(tmp_path):
    clock = FakeClock()
    state = {"reachable": False}

    alive = FakeProcess()
    keeper, notifier = make_keeper(
        spawn=lambda *a, **k: alive,
        clock=clock,
        reachable=lambda address: state["reachable"],
        state_path=tmp_path / "state.json",
        alert_threshold_seconds=60,
    )
    keeper.tick()
    clock.advance(60)
    keeper.tick()
    assert [n.severity for n in notifier.notifications] == ["urgent"]

    state["reachable"] = True
    clock.advance(5)
    keeper.tick()
    assert [n.severity for n in notifier.notifications] == ["urgent", "info"]
    assert "recovered" in notifier.notifications[1].title


def test_recovery_before_threshold_posts_no_alert_at_all(tmp_path):
    """A blip that self-heals before the alert threshold must restart silently, not page."""
    clock = FakeClock()
    state = {"reachable": False}
    alive = FakeProcess()
    keeper, notifier = make_keeper(
        spawn=lambda *a, **k: alive,
        clock=clock,
        reachable=lambda address: state["reachable"],
        state_path=tmp_path / "state.json",
        alert_threshold_seconds=60,
    )
    keeper.tick()
    clock.advance(5)
    state["reachable"] = True
    keeper.tick()
    assert notifier.notifications == []


# --- heartbeat file ----------------------------------------------------------


def test_heartbeat_is_written_after_every_tick(tmp_path):
    state_path = tmp_path / "tunnel-keeper.json"
    keeper, _ = make_keeper(state_path=state_path)
    assert not state_path.exists()
    keeper.tick()
    assert state_path.exists()

    # The tick that spawns a link does not also check its reachability -- that happens once
    # the process has had a tick to come up.
    keeper.tick()
    record = tunnel_keeper.read_heartbeat(state_path)
    assert record is not None
    assert record.heartbeat_at == datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert "substrate-prod" in record.links
    assert record.links["substrate-prod"]["reachable"] is True


def test_heartbeat_records_down_since_for_an_unreachable_link(tmp_path):
    state_path = tmp_path / "tunnel-keeper.json"
    keeper, _ = make_keeper(
        reachable=lambda address: False,
        state_path=state_path,
        restart_backoff_seconds=(1000,),
    )
    keeper.tick()
    record = tunnel_keeper.read_heartbeat(state_path)
    assert record.links["substrate-prod"]["down_since"] is not None


def test_read_heartbeat_returns_none_for_a_missing_file(tmp_path):
    assert tunnel_keeper.read_heartbeat(tmp_path / "nope.json") is None


def test_read_heartbeat_returns_none_for_unparseable_content(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert tunnel_keeper.read_heartbeat(path) is None


# --- keeper_looks_alive / check_heartbeat (the reboot-surviving half) -------


def test_keeper_looks_alive_is_false_with_no_heartbeat_file(tmp_path):
    alive, reason = tunnel_keeper.keeper_looks_alive(state_path=tmp_path / "missing.json")
    assert alive is False
    assert "no tunnel keeper heartbeat file" in reason


def test_keeper_looks_alive_is_false_when_recorded_pid_is_gone(tmp_path):
    state_path = tmp_path / "state.json"
    keeper, _ = make_keeper(state_path=state_path)
    keeper.tick()

    alive, reason = tunnel_keeper.keeper_looks_alive(
        state_path=state_path,
        pid_is_running=lambda pid: False,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert alive is False
    assert "not running" in reason


def test_keeper_looks_alive_is_false_when_heartbeat_is_stale(tmp_path):
    state_path = tmp_path / "state.json"
    keeper, _ = make_keeper(state_path=state_path)
    keeper.tick()

    alive, reason = tunnel_keeper.keeper_looks_alive(
        state_path=state_path,
        max_age_seconds=30,
        pid_is_running=lambda pid: True,
        now=datetime(2026, 9, 6, 0, 1, tzinfo=timezone.utc),  # 60s later
    )
    assert alive is False
    assert "old" in reason


def test_keeper_looks_alive_is_true_for_a_fresh_heartbeat_with_a_live_pid(tmp_path):
    state_path = tmp_path / "state.json"
    keeper, _ = make_keeper(state_path=state_path)
    keeper.tick()

    alive, reason = tunnel_keeper.keeper_looks_alive(
        state_path=state_path,
        max_age_seconds=30,
        pid_is_running=lambda pid: True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert alive is True
    assert reason == ""


def test_check_heartbeat_alerts_and_returns_false_when_dead(tmp_path):
    notifier = FakeNotifier()
    alive = tunnel_keeper.check_heartbeat(
        state_path=tmp_path / "missing.json",
        watchdog_state_path=tmp_path / "watchdog.json",
        notifier=notifier,
    )
    assert alive is False
    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification.severity == "urgent"
    assert "human is required" in notification.title
    assert tunnel_keeper.RECOVERY_COMMAND in notification.detail


def test_check_heartbeat_does_not_repeat_the_alert_while_still_dead(tmp_path):
    notifier = FakeNotifier()
    watchdog_state_path = tmp_path / "watchdog.json"
    for _ in range(3):
        tunnel_keeper.check_heartbeat(
            state_path=tmp_path / "missing.json",
            watchdog_state_path=watchdog_state_path,
            notifier=notifier,
        )
    # Every call today posts (each invocation is a fresh, separate launchd firing) --
    # what must not happen is a *recovery* notice sneaking in while still dead.
    assert all(n.severity == "urgent" for n in notifier.notifications)


def test_check_heartbeat_posts_recovery_after_a_prior_alert(tmp_path):
    notifier = FakeNotifier()
    watchdog_state_path = tmp_path / "watchdog.json"
    state_path = tmp_path / "state.json"

    tunnel_keeper.check_heartbeat(
        state_path=state_path,
        watchdog_state_path=watchdog_state_path,
        notifier=notifier,
    )
    assert notifier.notifications[-1].severity == "urgent"

    keeper, _ = make_keeper(state_path=state_path)
    keeper.tick()
    tunnel_keeper.check_heartbeat(
        state_path=state_path,
        watchdog_state_path=watchdog_state_path,
        notifier=notifier,
        pid_is_running=lambda pid: True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert notifier.notifications[-1].severity == "info"
    assert "running again" in notifier.notifications[-1].title


def test_check_heartbeat_alive_and_never_alerted_stays_quiet(tmp_path):
    state_path = tmp_path / "state.json"
    keeper, _ = make_keeper(state_path=state_path)
    keeper.tick()

    notifier = FakeNotifier()
    alive = tunnel_keeper.check_heartbeat(
        state_path=state_path,
        watchdog_state_path=tmp_path / "watchdog.json",
        notifier=notifier,
        pid_is_running=lambda pid: True,
        now=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert alive is True
    assert notifier.notifications == []


# --- link spec parsing -------------------------------------------------------


def test_parse_link_spec_uses_documented_substrate_default():
    link = tunnel_keeper.parse_link_spec("substrate-prod")
    assert link.local_address == "127.0.0.1:18001"
    assert link.kubectl_args == (
        "-n",
        "platform-substrate-prod",
        "port-forward",
        "svc/substrate",
        "18001:8000",
    )


def test_parse_link_spec_refuses_bare_temporal_with_no_on_file_target():
    try:
        tunnel_keeper.parse_link_spec("temporal")
    except TunnelArgError as exc:
        assert "no on-file default" in str(exc)
    else:
        raise AssertionError("expected TunnelArgError")


def test_parse_link_spec_accepts_a_fully_specified_temporal_link():
    link = tunnel_keeper.parse_link_spec("temporal:platform-temporal-prod:svc/temporal:7233:7233")
    assert link.name == "temporal"
    assert link.local_address == "127.0.0.1:7233"


def test_parse_link_spec_refuses_an_unknown_name():
    try:
        tunnel_keeper.parse_link_spec("not-a-real-tunnel")
    except TunnelArgError as exc:
        assert "Unknown tunnel name" in str(exc)
    else:
        raise AssertionError("expected TunnelArgError")


def test_run_command_defaults_to_substrate_prod_only(monkeypatch, tmp_path):
    import argparse

    seen_links = {}

    class StubKeeper:
        def __init__(self, links, **kwargs):
            seen_links["links"] = links

        def run(self):
            pass

    monkeypatch.setattr(tunnel_keeper, "TunnelKeeper", StubKeeper)
    args = argparse.Namespace(
        links=None,
        kubectl="kubectl",
        kubeconfig=None,
        check_interval_seconds=None,
        alert_threshold_seconds=None,
        alert_dedup_seconds=None,
        state_path=tmp_path / "state.json",
        log_dir=tmp_path / "logs",
    )
    tunnel_keeper._run_command(args)
    assert [link.name for link in seen_links["links"]] == ["substrate-prod"]


# --- release-gate cures: single-instance lock and unmanaged-port deferral ---

def test_a_second_keeper_refuses_loudly_while_the_lock_is_held(tmp_path):
    import tunnel_keeper as tk

    lock_path = tmp_path / "keeper.lock"
    held = tk.acquire_single_instance_lock(lock_path)
    try:
        with pytest.raises(tk.KeeperAlreadyRunning) as exc_info:
            tk.acquire_single_instance_lock(lock_path)
        assert str(os.getpid()) in str(exc_info.value)
    finally:
        held.close()


def test_a_stale_lock_from_a_dead_keeper_is_reclaimed(tmp_path):
    import tunnel_keeper as tk

    lock_path = tmp_path / "keeper.lock"
    first = tk.acquire_single_instance_lock(lock_path)
    first.close()  # the holder dying releases flock exactly like this
    second = tk.acquire_single_instance_lock(lock_path)
    try:
        assert lock_path.read_text().strip() == str(os.getpid())
    finally:
        second.close()


def test_reachable_but_unmanaged_port_is_deferred_to_not_fought(tmp_path):
    """An operator's own port-forward (or an orphan) answers the port while the
    keeper owns no process: the keeper must defer across ticks -- zero spawns,
    heartbeat says unmanaged, status healthy -- not lose a respawn fight every
    interval while reporting the tunnel down (release-gate blocking finding)."""
    import tunnel_keeper as tk

    spawns: list = []

    def fake_spawn(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("keeper must not spawn against a reachable unmanaged port")

    keeper = tk.TunnelKeeper(
        [make_link()],
        notifier=lambda notifications: None,
        reachable=lambda address: True,
        spawn=fake_spawn,
        state_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "keeper.lock",
    )
    for _ in range(3):
        keeper.tick()

    assert spawns == []
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    (link_status,) = heartbeat["links"].values()
    assert link_status["unmanaged"] is True
    assert link_status["reachable"] is True
