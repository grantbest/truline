"""schedule_status.py must answer 'is the tunnel dead' from the keeper's own heartbeat file.

The launchd LaunchAgent that used to own the kubectl port-forward tunnel was stood down
2026-09-04 (macOS Local Network privacy). Its supervised replacement, `tunnel_keeper.py`, writes
a heartbeat file no other process reads unless something asks -- these tests pin that
`schedule_status.py` is that one surface: a live keeper renders quietly, a stale or missing
heartbeat renders a named fault and produces exactly the kind of urgent notification every other
factory-level fault in this module already produces.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
import tunnel_keeper  # noqa: E402
from schedule_runtime import FactoryScheduleStatus  # noqa: E402
from schedule_status import TunnelKeeperLiveStatus  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
SCHEDULE_ID = "factory-dispatcher-dev"
INTERVAL_SECONDS = 15 * 60


def quiet_status() -> FactoryScheduleStatus:
    return FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=False, in_flight=(), recent=())


def write_heartbeat(path: Path, *, heartbeat_at: datetime, pid: int, links: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"heartbeat_at": heartbeat_at.isoformat(), "pid": pid, "links": links}),
        encoding="utf-8",
    )


def alive_links() -> dict:
    return {"substrate-prod": {"reachable": True, "process_alive": True, "down_since": None, "restart_attempts": 0}}


# --- describe_tunnel_keeper_status -------------------------------------------


def test_describe_reports_could_not_determine_with_no_heartbeat_file(tmp_path):
    status = schedule_status.describe_tunnel_keeper_status(state_path=tmp_path / "missing.json")
    assert status.error
    assert status.heartbeat_at is None


def test_describe_reads_a_fresh_heartbeat(tmp_path):
    path = tmp_path / "tunnel-keeper.json"
    write_heartbeat(path, heartbeat_at=NOW, pid=os.getpid(), links=alive_links())

    status = schedule_status.describe_tunnel_keeper_status(state_path=path)
    assert status.error == ""
    assert status.heartbeat_at == NOW.isoformat()
    assert status.pid_running is True
    assert status.links["substrate-prod"]["reachable"] is True


# --- rendering ----------------------------------------------------------------


def test_render_is_quiet_for_a_fresh_alive_keeper(tmp_path):
    path = tmp_path / "tunnel-keeper.json"
    write_heartbeat(path, heartbeat_at=NOW, pid=os.getpid(), links=alive_links())
    status = schedule_status.describe_tunnel_keeper_status(state_path=path)

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        tunnel_keeper_status=status,
        now=NOW,
    )

    assert "tunnel_keeper_stale: false" in rendered
    assert "TUNNEL KEEPER STALE" not in rendered
    assert "tunnel_keeper_link_substrate-prod_reachable: true" in rendered


def test_render_names_a_stale_keeper_and_the_recovery_command(tmp_path):
    stale_at = NOW - timedelta(hours=1)  # far enough in the past to exceed the default threshold
    path = tmp_path / "tunnel-keeper.json"
    write_heartbeat(path, heartbeat_at=stale_at, pid=999999, links=alive_links())
    status = schedule_status.describe_tunnel_keeper_status(state_path=path)

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        tunnel_keeper_status=status,
        now=NOW,
    )

    assert "tunnel_keeper_stale: true" in rendered
    assert "TUNNEL KEEPER STALE OR NOT RUNNING" in rendered
    assert tunnel_keeper.RECOVERY_COMMAND in rendered


def test_render_names_an_unreachable_link_even_when_the_keeper_itself_is_fresh(tmp_path):
    path = tmp_path / "tunnel-keeper.json"
    write_heartbeat(
        path,
        heartbeat_at=NOW,
        pid=os.getpid(),
        links={
            "substrate-prod": {
                "reachable": False,
                "process_alive": True,
                "down_since": NOW.isoformat(),
                "restart_attempts": 2,
            }
        },
    )
    status = schedule_status.describe_tunnel_keeper_status(state_path=path)

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        tunnel_keeper_status=status,
        now=NOW,
    )

    assert "tunnel_keeper_stale: false" in rendered
    assert "TUNNEL LINK DOWN: substrate-prod" in rendered


def test_render_reports_could_not_determine_for_a_host_with_no_keeper_ever_started(tmp_path):
    status = schedule_status.describe_tunnel_keeper_status(state_path=tmp_path / "missing.json")

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        tunnel_keeper_status=status,
        now=NOW,
    )

    assert "tunnel_keeper_status: could-not-determine" in rendered


# --- notifications -------------------------------------------------------------


def test_stale_keeper_produces_one_urgent_notification():
    stale_status = TunnelKeeperLiveStatus(
        heartbeat_at=(NOW - timedelta(hours=1)).isoformat(),
        pid=999999,
        pid_running=False,
        links=alive_links(),
    )

    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        tunnel_keeper_status=stale_status,
        now=NOW,
    )

    assert len(notifications) == 1
    assert notifications[0].source == "tunnel-keeper-stale"
    assert notifications[0].severity == "urgent"


def test_fresh_keeper_produces_no_tunnel_notification():
    fresh_status = TunnelKeeperLiveStatus(
        heartbeat_at=NOW.isoformat(),
        pid=os.getpid(),
        pid_running=True,
        links=alive_links(),
    )

    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        tunnel_keeper_status=fresh_status,
        now=NOW,
    )

    assert notifications == []


def test_could_not_determine_status_is_treated_as_a_fault_not_silence():
    unknown_status = TunnelKeeperLiveStatus.could_not_determine("no heartbeat file found")

    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        tunnel_keeper_status=unknown_status,
        now=NOW,
    )

    assert len(notifications) == 1
    assert notifications[0].source == "tunnel-keeper-stale"


def test_no_tunnel_keeper_status_at_all_produces_no_notification_and_no_crash():
    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        now=NOW,
    )
    assert notifications == []
