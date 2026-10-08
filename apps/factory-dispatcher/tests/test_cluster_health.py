"""Cluster health notifications must not be another quiet-by-construction gap.

Fixtures are shaped like the real `kubectl get ... -o json` / `kubectl get
applications.argoproj.io -o json` output captured during the 2026-08-23
undeployability incident: a wedged ReplicaSet, a failed backup CronJob (with
its owned Job), a Degraded ArgoCD Application, and the post-remediation
healthy state. This test module fails against today's repo because
`cluster_health` does not exist yet — that absence is the bug.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cluster_health  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cluster_health"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class RecordingPoster:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.posted: list[str] = []

    def __call__(self, content: str) -> None:
        self.posted.append(content)
        if self.exc is not None:
            raise self.exc


# ---------------------------------------------------------------------------
# Signal 1: pods wedged in ImagePullBackOff / ErrImagePull
# ---------------------------------------------------------------------------


def test_wedged_replicaset_pods_produce_a_notification_past_grace():
    pods = _load("wedged_pods.json")
    now = datetime(2026, 8, 23, 10, 0, 0, tzinfo=timezone.utc)

    notifications = cluster_health.pod_image_pull_notifications(
        pods, now=now, grace_seconds=cluster_health.IMAGE_PULL_GRACE_SECONDS_DEFAULT
    )

    sources = {n.source for n in notifications}
    assert sources == {"image-pull"}
    assert len(notifications) == 2  # substrate (ImagePullBackOff) + mcp-hub (ErrImagePull)

    substrate = next(n for n in notifications if "substrate" in n.title)
    assert "platform-substrate-prod/substrate" in substrate.title
    assert "sha256:0000000000000000000000000000000000000000000000000000000000000000" in substrate.detail
    assert "reason=ImagePullBackOff" in substrate.detail
    # startTime 2026-08-21T21:14:03Z -> now 2026-08-23T10:00:00Z is ~1d12h
    assert "1d" in substrate.detail

    mcp_hub = next(n for n in notifications if "mcp-hub" in n.title)
    assert "reason=ErrImagePull" in mcp_hub.detail


def test_healthy_running_pod_in_the_same_fixture_produces_nothing():
    pods = _load("wedged_pods.json")
    now = datetime(2026, 8, 23, 10, 0, 0, tzinfo=timezone.utc)

    notifications = cluster_health.pod_image_pull_notifications(pods, now=now)

    assert all("lifeops-console" not in n.title for n in notifications)


def test_image_pull_failure_inside_grace_period_produces_nothing():
    pods = _load("wedged_pods.json")
    # Barely after the pods were created — well inside the default 10-minute grace.
    now = datetime(2026, 8, 21, 21, 15, 0, tzinfo=timezone.utc)

    notifications = cluster_health.pod_image_pull_notifications(pods, now=now)

    assert notifications == []


# ---------------------------------------------------------------------------
# Signal 2: backup / restore-drill CronJob without a successful run
# ---------------------------------------------------------------------------


def test_failed_backup_cronjob_produces_a_notification_for_each_backup_job():
    fixture = _load("failed_backup_cronjob.json")

    notifications = cluster_health.cronjob_failure_notifications(
        fixture["cronjobs"], fixture["jobs"]
    )

    names = {n.title for n in notifications}
    assert any("pg-backup" in title for title in names)
    assert any("pg-restore-drill" in title for title in names)
    # The unrelated, non-backup-named CronJob must not page even though its
    # own lastScheduleTime/lastSuccessfulTime also disagree.
    assert not any("grafana-report-export" in title for title in names)

    pg_backup = next(n for n in notifications if "pg-backup" in n.title)
    assert "BackoffLimitExceeded" in pg_backup.detail

    pg_restore_drill = next(n for n in notifications if "pg-restore-drill" in n.title)
    assert "no Job found for the most recently scheduled run" in pg_restore_drill.detail
    assert "(never)" in pg_restore_drill.detail


def test_backup_cronjob_with_recent_success_produces_nothing():
    fixture = _load("healthy_post_remediation.json")

    notifications = cluster_health.cronjob_failure_notifications(
        fixture["cronjobs"], fixture["jobs"]
    )

    assert notifications == []


# ---------------------------------------------------------------------------
# Signal 3: Degraded ArgoCD Application
# ---------------------------------------------------------------------------


def test_degraded_application_produces_a_notification():
    applications = _load("degraded_application.json")

    notifications = cluster_health.degraded_application_notifications(applications)

    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.source == "argocd-application"
    assert "argocd/truline-apps" in notification.title
    assert "one or more objects failed to apply" in notification.detail
    # The healthy sibling Application in the same fixture must not page.
    assert "truline-gaming" not in notification.detail


def test_healthy_application_produces_nothing():
    fixture = _load("healthy_post_remediation.json")

    notifications = cluster_health.degraded_application_notifications(
        fixture["applications"]
    )

    assert notifications == []


# ---------------------------------------------------------------------------
# The whole incident, and its remediation, through run_checks + notify
# ---------------------------------------------------------------------------


def test_a_healthy_cluster_produces_no_notification_and_no_post():
    fixture = _load("healthy_post_remediation.json")
    poster = RecordingPoster()
    now = datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc)

    notifications = cluster_health.run_checks(
        pods=fixture["pods"],
        cronjobs=fixture["cronjobs"],
        jobs=fixture["jobs"],
        applications=fixture["applications"],
        now=now,
    )
    cluster_health.notify(notifications, poster)

    assert notifications == []
    assert poster.posted == []


def test_notify_builds_and_posts_one_payload_without_contacting_the_network():
    poster = RecordingPoster()
    notifications = [
        cluster_health.Notification(
            severity="urgent",
            source="image-pull",
            title="platform-substrate-prod/substrate cannot pull its image",
            detail="pod=platform-substrate-prod/substrate-abc reason=ImagePullBackOff",
        ),
    ]

    cluster_health.notify(notifications, poster)

    assert len(poster.posted) == 1
    payload = poster.posted[0]
    assert "severity=urgent service=cluster-health" in payload
    assert "platform-substrate-prod/substrate cannot pull its image" in payload
    assert "ImagePullBackOff" in payload


def test_notify_with_no_poster_configured_logs_and_does_not_raise(caplog):
    notifications = [
        cluster_health.Notification(
            severity="urgent", source="image-pull", title="x", detail="y"
        )
    ]
    caplog.set_level(logging.WARNING, logger=cluster_health.logger.name)

    cluster_health.notify(notifications, None)

    assert "webhook post skipped" in caplog.text


def test_cluster_health_webhook_failure_is_logged_without_secret_and_swallowed(caplog):
    secret = "https://discord.example.test/api/webhooks/cluster-health-secret"
    poster = RecordingPoster(RuntimeError(f"failed to reach {secret}"))
    notifications = [
        cluster_health.Notification(
            severity="urgent", source="image-pull", title="x", detail="y"
        )
    ]
    caplog.set_level(logging.WARNING, logger=cluster_health.logger.name)

    cluster_health.notify(notifications, poster)  # must not raise

    assert "Cluster health Discord webhook post failed" in caplog.text
    assert secret not in caplog.text


def test_cluster_health_discord_webhook_post_sets_user_agent_and_does_not_log_url(monkeypatch, caplog):
    secret_url = "https://discord.example.test/api/webhooks/super-secret-token"
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        raise HTTPError(secret_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(cluster_health.urllib.request, "urlopen", fake_urlopen)
    poster = cluster_health.DiscordWebhookPoster(secret_url)
    notifications = [
        cluster_health.Notification(
            severity="urgent", source="image-pull", title="x", detail="y"
        )
    ]
    caplog.set_level(logging.WARNING, logger=cluster_health.logger.name)

    cluster_health.notify(notifications, poster)

    headers = dict(seen["request"].header_items())
    assert headers["User-agent"] == cluster_health.DISCORD_WEBHOOK_USER_AGENT
    assert seen["timeout"] == cluster_health.DISCORD_WEBHOOK_TIMEOUT_SECONDS
    assert secret_url not in caplog.text


def test_poster_from_env_is_none_when_webhook_url_unset():
    assert cluster_health.poster_from_env({}) is None


def test_poster_from_env_builds_poster_from_configured_url():
    poster = cluster_health.poster_from_env(
        {cluster_health.DISCORD_WEBHOOK_URL_ENV: "https://discord.example.test/webhook"}
    )
    assert isinstance(poster, cluster_health.DiscordWebhookPoster)


def test_grace_seconds_is_configured_from_env():
    assert (
        cluster_health.grace_seconds_from_env(
            {cluster_health.IMAGE_PULL_GRACE_SECONDS_ENV: "120"}
        )
        == 120
    )
    assert (
        cluster_health.grace_seconds_from_env({})
        == cluster_health.IMAGE_PULL_GRACE_SECONDS_DEFAULT
    )


# ---------------------------------------------------------------------------
# Cross-run dedup (Amendment 30 gate finding on #672): a persisting finding
# must post at most once per declared re-alert window, and a finding that
# resolves and later recurs must post again immediately.
#
# tests/conftest.py's autouse `_no_test_writes_to_the_real_home` fixture
# fakes $HOME for every test in this module, so `cluster_health`'s default
# alert-state path (under `Path.home()`) is a fresh, isolated file per test
# with no extra plumbing needed here.
# ---------------------------------------------------------------------------


def _finding(detail_suffix: str = "") -> cluster_health.Notification:
    return cluster_health.Notification(
        severity="urgent",
        source="cronjob",
        title="factory/pg-restore-drill has no successful run for its last schedule",
        detail=f"cronjob=factory/pg-restore-drill last_scheduled=2026-08-16T00:00:00Z{detail_suffix}",
        key="cronjob:factory/pg-restore-drill",
        reason="no-job",
    )


def test_persisting_finding_posts_at_most_once_per_window():
    poster = RecordingPoster()
    finding = _finding()

    for _ in range(3):
        gated = cluster_health.notify_new_findings([finding], poster)

    assert len(poster.posted) == 1
    assert gated == []  # the third (and second) run found nothing new to post


def test_first_occurrence_of_a_new_finding_still_posts_immediately():
    poster = RecordingPoster()
    finding = _finding()

    gated = cluster_health.notify_new_findings([finding], poster)

    assert gated == [finding]
    assert len(poster.posted) == 1
    assert poster.posted[0] == cluster_health.build_payload([finding])


def test_a_healthy_run_posts_nothing_through_the_dedup_path():
    poster = RecordingPoster()

    gated = cluster_health.notify_new_findings([], poster)

    assert gated == []
    assert poster.posted == []


def test_finding_that_resolves_and_recurs_posts_again_immediately():
    """The pg-restore-drill shape: an incident that resolves and later recurs
    identically must not inherit the earlier suppression window."""
    poster = RecordingPoster()
    finding = _finding()

    first = cluster_health.notify_new_findings([finding], poster)
    resolved = cluster_health.notify_new_findings([], poster)  # a healthy run in between
    recurred = cluster_health.notify_new_findings([finding], poster)

    assert first == [finding]
    assert resolved == []
    assert recurred == [finding]
    assert len(poster.posted) == 2


def test_failed_delivery_does_not_silence_the_next_run():
    """#674: dedup state used to be recorded as soon as the noop `post` in
    `finding_alert_policy` returned True — before the real, batched Discord
    post that follows even ran. A finding whose first post failed at Discord
    was silenced for a full re-alert window despite never being delivered.
    A failed post must revert that optimistic record so the very next run
    still treats the finding as new."""
    failing_poster = RecordingPoster(RuntimeError("discord unreachable"))
    finding = _finding()

    first = cluster_health.notify_new_findings([finding], failing_poster)

    assert first == [finding]  # dedup gated it through: it was new information
    assert len(failing_poster.posted) == 1  # delivery was attempted
    doc = cluster_health._read_alert_state_doc(cluster_health._alert_state_path())
    assert doc == {}  # but the failed attempt must not be recorded as delivered

    working_poster = RecordingPoster()
    second = cluster_health.notify_new_findings([finding], working_poster)

    assert second == [finding]  # not silenced by the earlier failed attempt
    assert len(working_poster.posted) == 1

    third = cluster_health.notify_new_findings([finding], working_poster)
    assert third == []  # this time delivery succeeded, so it dedups normally


def test_cluster_health_unrelated_finding_does_not_suppress_or_get_suppressed_by_another():
    poster = RecordingPoster()
    cronjob_finding = _finding()
    image_pull_finding = cluster_health.Notification(
        severity="urgent",
        source="image-pull",
        title="platform-substrate-prod/substrate cannot pull its image",
        detail="pod=platform-substrate-prod/substrate-abc reason=ImagePullBackOff",
        key="image-pull:platform-substrate-prod/substrate/substrate",
        reason="ImagePullBackOff:sha256:0",
    )

    cluster_health.notify_new_findings([cronjob_finding], poster)
    gated = cluster_health.notify_new_findings([cronjob_finding, image_pull_finding], poster)

    assert gated == [image_pull_finding]


def test_dedup_failure_fails_open_and_still_posts_the_finding(monkeypatch, caplog):
    """A broken dedup path must never be the reason an urgent finding goes unposted."""
    poster = RecordingPoster()
    finding = _finding()

    def broken_policy():
        raise RuntimeError("state file is corrupt")

    monkeypatch.setattr(cluster_health, "finding_alert_policy", broken_policy)
    caplog.set_level(logging.WARNING, logger=cluster_health.logger.name)

    gated = cluster_health.notify_new_findings([finding], poster)

    assert gated == [finding]
    assert len(poster.posted) == 1
    assert "dedup failed" in caplog.text


def test_notify_unreachable_posts_a_deduped_notice_bounded_by_the_same_window():
    poster = RecordingPoster()
    error = cluster_health.MissingKubectlOutputError("kubectl: command not found")

    first = cluster_health.notify_unreachable(error, poster)
    second = cluster_health.notify_unreachable(error, poster)
    third = cluster_health.notify_unreachable(error, poster)

    assert (first, second, third) == (True, False, False)
    assert len(poster.posted) == 1
    assert "cannot see the cluster" in poster.posted[0]


def test_notify_unreachable_failed_delivery_reverts_so_the_next_check_retries():
    error = cluster_health.MissingKubectlOutputError("kubectl: command not found")
    failing_poster = RecordingPoster(RuntimeError("discord unreachable"))

    first = cluster_health.notify_unreachable(error, failing_poster)
    assert first is False  # delivery failed
    doc = cluster_health._read_alert_state_doc(cluster_health._alert_state_path())
    assert doc == {}  # the failed attempt must not be recorded as delivered

    working_poster = RecordingPoster()
    second = cluster_health.notify_unreachable(error, working_poster)
    assert second is True
    assert len(working_poster.posted) == 1


def test_reachable_run_clears_the_unreachable_notices_window_for_the_next_outage():
    poster = RecordingPoster()
    error = cluster_health.MissingKubectlOutputError("kubectl: command not found")

    cluster_health.notify_unreachable(error, poster)
    cluster_health.notify_new_findings([], poster)  # the cluster answers again
    posted_again = cluster_health.notify_unreachable(error, poster)

    assert posted_again is True
    assert len(poster.posted) == 2


def test_skipped_resource_kinds_names_a_partial_outage_by_resource_kind():
    snapshot = cluster_health.ClusterSnapshot(
        pods=None,
        cronjobs=None,
        jobs=None,
        applications={"items": []},
    )

    assert cluster_health.skipped_resource_kinds(snapshot) == ["pods", "cronjobs", "jobs"]


def test_skipped_resource_kinds_is_empty_when_every_kind_answered():
    fixture = _load("healthy_post_remediation.json")
    snapshot = cluster_health.ClusterSnapshot(
        pods=fixture["pods"],
        cronjobs=fixture["cronjobs"],
        jobs=fixture["jobs"],
        applications=fixture["applications"],
    )

    assert cluster_health.skipped_resource_kinds(snapshot) == []
