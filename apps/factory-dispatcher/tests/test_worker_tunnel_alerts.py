"""Temporal tunnel alerts must not be another log-only outage signal."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePoster:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.alerts: list[worker.TunnelAlert] = []

    async def __call__(self, alert: worker.TunnelAlert) -> None:
        self.alerts.append(alert)
        if self.exc is not None:
            raise self.exc


def run(coro):
    return asyncio.run(coro)


def severities(poster: FakePoster) -> list[str]:
    return [alert.severity for alert in poster.alerts]


def test_tunnel_loss_posts_one_urgent_alert_after_default_threshold():
    clock = FakeClock()
    poster = FakePoster()
    alerter = worker.TemporalTunnelAlerter(
        "127.0.0.1:7233",
        poster=poster,
        clock=clock,
    )

    run(alerter.observe(False))
    clock.advance(worker.TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS - 1)
    run(alerter.observe(False))
    assert poster.alerts == []

    clock.advance(1)
    run(alerter.observe(False))
    run(alerter.observe(False))

    assert severities(poster) == ["urgent"]
    alert = poster.alerts[0]
    assert alert.tunnel == "127.0.0.1:7233"
    assert alert.duration_seconds == worker.TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS


def test_alert_threshold_dedup_and_webhook_are_configured_from_env(monkeypatch):
    created = {}

    class StubPoster:
        def __init__(self, webhook_url: str):
            created["webhook_url"] = webhook_url

        async def __call__(self, alert: worker.TunnelAlert) -> None:
            raise AssertionError("this test does not post")

    monkeypatch.setattr(worker, "DiscordWebhookPoster", StubPoster)

    alerter = worker.temporal_tunnel_alerter_from_env(
        "127.0.0.1:7233",
        {
            worker.DISCORD_WEBHOOK_URL_ENV: "https://discord.example.test/webhook",
            worker.TEMPORAL_TUNNEL_ALERT_THRESHOLD_ENV: "5",
            worker.TEMPORAL_TUNNEL_ALERT_DEDUP_ENV: "7",
        },
    )

    assert created["webhook_url"] == "https://discord.example.test/webhook"
    assert alerter._threshold_seconds == 5
    assert alerter._dedup_window_seconds == 7


def test_recovery_posts_one_info_followup_only_after_an_urgent_alert():
    clock = FakeClock()
    poster = FakePoster()
    alerter = worker.TemporalTunnelAlerter(
        "127.0.0.1:7233",
        poster=poster,
        clock=clock,
        threshold_seconds=60,
    )

    run(alerter.observe(False))
    clock.advance(30)
    run(alerter.observe(True))
    assert poster.alerts == []

    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))
    clock.advance(15)
    run(alerter.observe(True))
    run(alerter.observe(True))

    assert severities(poster) == ["urgent", "info"]
    assert poster.alerts[1].duration_seconds == 75


def test_flapping_inside_dedup_window_does_not_post_more_urgent_alerts():
    clock = FakeClock()
    poster = FakePoster()
    alerter = worker.TemporalTunnelAlerter(
        "127.0.0.1:7233",
        poster=poster,
        clock=clock,
        threshold_seconds=60,
        dedup_window_seconds=1800,
    )

    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))
    clock.advance(10)
    run(alerter.observe(True))

    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))
    clock.advance(10)
    run(alerter.observe(True))

    assert severities(poster) == ["urgent", "info"]

    clock.advance(1800)
    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))

    assert severities(poster) == ["urgent", "info", "urgent"]


def test_worker_tunnel_alerts_webhook_failure_is_logged_without_secret_and_swallowed(caplog):
    secret = "https://discord.example.test/api/webhooks/secret-token"
    clock = FakeClock()
    poster = FakePoster(RuntimeError(f"failed to reach {secret}"))
    alerter = worker.TemporalTunnelAlerter(
        "127.0.0.1:7233",
        poster=poster,
        clock=clock,
        threshold_seconds=60,
    )

    caplog.set_level(logging.WARNING, logger=worker.logger.name)

    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))

    assert severities(poster) == ["urgent"]
    assert "Temporal tunnel Discord webhook post failed" in caplog.text
    assert secret not in caplog.text


def test_worker_tunnel_alerts_discord_webhook_post_sets_user_agent_and_does_not_log_url(
    monkeypatch,
    caplog,
):
    secret_url = "https://discord.example.test/api/webhooks/super-secret-token"
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        raise HTTPError(
            secret_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    clock = FakeClock()
    alerter = worker.TemporalTunnelAlerter(
        "127.0.0.1:7233",
        poster=worker.DiscordWebhookPoster(secret_url),
        clock=clock,
        threshold_seconds=60,
    )
    caplog.set_level(logging.WARNING, logger=worker.logger.name)

    run(alerter.observe(False))
    clock.advance(60)
    run(alerter.observe(False))

    headers = dict(seen["request"].header_items())
    assert headers["User-agent"] == worker.DISCORD_WEBHOOK_USER_AGENT
    assert seen["timeout"] == worker.DISCORD_WEBHOOK_TIMEOUT_SECONDS
    assert secret_url not in caplog.text
