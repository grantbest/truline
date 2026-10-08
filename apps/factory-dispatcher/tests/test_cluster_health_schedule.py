"""Tests for the scheduled cluster health check (OPS-8).

#509 shipped `cluster_health.py` and it worked -- run by hand on 2026-08-24 it caught
pg-restore-drill silently failing since 2026-08-16. Nothing scheduled it: a grep for
`cluster_health` across workflows/, activities/, and schedule_runtime.py turned up only the
module itself. This module asserts the two things that fail against today's repo before this
change lands: that a durable Temporal schedule exists and names the checker
(`test_cluster_health_schedule_is_registered_with_the_worker_and_names_the_checker`), and that a
scheduled run posts exactly the notifications the checker itself produces -- nothing more, nothing
less, and nothing when the cluster is healthy.

No Temporal server, no substrate, no network: schedule registration is tested against the same
in-memory `FakeClient`/`FAKE_TEMPORAL` stand-in `test_schedule_runtime.py` already uses, and the
activity is tested by injecting a fake `collect` and a recording poster -- both fixtures below are
the same JSON fixtures `test_cluster_health.py` already exercises directly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cluster_health  # noqa: E402
import schedule_runtime  # noqa: E402
from activities import cluster_health as activity  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cluster_health"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class RecordingPoster:
    def __init__(self) -> None:
        self.posted: list[str] = []

    def __call__(self, content: str) -> None:
        self.posted.append(content)


def _snapshot(fixture: dict) -> cluster_health.ClusterSnapshot:
    return cluster_health.ClusterSnapshot(
        pods=fixture.get("pods"),
        cronjobs=fixture.get("cronjobs"),
        jobs=fixture.get("jobs"),
        applications=fixture.get("applications"),
    )


# -- the schedule exists and names the checker -----------------------------------------------


def test_cluster_health_schedule_is_registered_with_the_worker_and_names_the_checker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.cluster_health import ClusterHealthWorkflow

    definition = temporal_workflow._Definition.from_class(ClusterHealthWorkflow)
    assert definition.name == "ClusterHealthWorkflow"
    assert "ClusterHealthWorkflow" in inspect.getsource(worker.build_worker)
    assert "register_cluster_health_schedule" in inspect.getsource(worker.main)


def test_cluster_health_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_cluster_health_activity" in names


def test_cluster_health_schedule_id_names_the_checker():
    assert "cluster-health" in schedule_runtime.CLUSTER_HEALTH_SCHEDULE_ID
    assert "cluster-health" in schedule_runtime.CLUSTER_HEALTH_WORKFLOW_ID_PREFIX


# -- cadence is declared once, and the schedule matches that declaration ----------------------


# -- a run posts exactly what the checker produced -------------------------------------------


def test_a_healthy_cluster_snapshot_posts_nothing():
    snapshot = _snapshot(_load("healthy_post_remediation.json"))
    poster = RecordingPoster()

    result = activity.run_cluster_health_check(
        now_fn=lambda: datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc),
        collect=lambda: snapshot,
        poster_fn=lambda: poster,
    )

    assert result["notification_count"] == 0
    assert poster.posted == []


def test_a_failed_backup_cronjob_snapshot_posts_exactly_the_checkers_own_notifications():
    fixture = _load("failed_backup_cronjob.json")
    snapshot = _snapshot(fixture)
    poster = RecordingPoster()
    now = datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc)

    result = activity.run_cluster_health_check(
        now_fn=lambda: now,
        collect=lambda: snapshot,
        poster_fn=lambda: poster,
    )

    expected = cluster_health.run_checks(
        pods=fixture.get("pods"),
        cronjobs=fixture.get("cronjobs"),
        jobs=fixture.get("jobs"),
        applications=fixture.get("applications"),
        now=now,
    )
    assert result["notification_count"] == len(expected) > 0
    assert len(poster.posted) == 1
    assert poster.posted[0] == cluster_health.build_payload(expected)


def test_the_activity_never_reimplements_a_check_it_calls_run_checks_and_notify_exactly_once():
    """Non-duplication: the scheduled path must drive #509's own functions, not new logic."""
    fixture = _load("degraded_application.json")
    snapshot = cluster_health.ClusterSnapshot(
        pods=None, cronjobs=None, jobs=None, applications=fixture
    )
    calls = {"run_checks": 0, "notify": 0}
    sentinel_notifications = [
        cluster_health.Notification(
            severity="urgent", source="argocd-application", title="x", detail="y"
        )
    ]

    def fake_run_checks(**kwargs):
        calls["run_checks"] += 1
        assert kwargs["applications"] is fixture
        assert kwargs["pods"] is None
        return sentinel_notifications

    def fake_notify(notifications, poster):
        calls["notify"] += 1
        assert notifications is sentinel_notifications

    result = activity.run_cluster_health_check(
        collect=lambda: snapshot,
        run_checks=fake_run_checks,
        notify=fake_notify,
        poster_fn=lambda: None,
    )

    assert calls == {"run_checks": 1, "notify": 1}
    assert result["notification_count"] == 1
    assert result["sources"] == ["argocd-application"]


# -- the checker's own failure must not read as a quiet, healthy run --------------------------


def test_kubectl_being_unreachable_fails_the_activity_rather_than_reporting_zero_findings():
    def broken_collect():
        raise cluster_health.MissingKubectlOutputError("kubectl: command not found")

    calls = {"notify": 0}

    def unreachable_notify(notifications, poster):
        calls["notify"] += 1

    try:
        activity.run_cluster_health_check(collect=broken_collect, notify=unreachable_notify)
        raised = False
    except cluster_health.MissingKubectlOutputError:
        raised = True

    assert raised, "a checker that never ran must fail the activity, not report a clean run"
    assert calls["notify"] == 0


def test_collect_kubectl_snapshot_raises_only_when_every_resource_type_is_unavailable():
    def all_missing(*_args):
        return None

    try:
        cluster_health.collect_kubectl_snapshot(runner=all_missing)
        raised = False
    except cluster_health.MissingKubectlOutputError:
        raised = True
    assert raised


def test_collect_kubectl_snapshot_does_not_raise_when_only_one_resource_type_is_absent():
    def argocd_crd_missing(*args):
        return None if args[0] == "applications.argoproj.io" else {"items": []}

    snapshot = cluster_health.collect_kubectl_snapshot(runner=argocd_crd_missing)

    assert snapshot.pods == {"items": []}
    assert snapshot.cronjobs == {"items": []}
    assert snapshot.jobs == {"items": []}
    assert snapshot.applications is None


# -- kubectl unreachable: a proactive, deduped notice through a path that -------------------
# -- doesn't need the LAN kubectl needs, on top of the activity still failing ----------------


def test_kubectl_unreachable_posts_a_deduped_notice_and_still_fails_the_activity_every_time():
    """tests/conftest.py fakes $HOME for every test, so cluster_health's alert-state file is
    fresh and isolated here -- three consecutive scheduled ticks against the same standing
    outage must fail the activity every time (drain health) but post to Discord only once
    (the re-alert window), through a path independent of the LAN/tunnel kubectl needs."""

    def broken_collect():
        raise cluster_health.MissingKubectlOutputError("kubectl: command not found")

    poster = RecordingPoster()
    raised_count = 0

    for _ in range(3):
        try:
            activity.run_cluster_health_check(collect=broken_collect, poster_fn=lambda: poster)
        except cluster_health.MissingKubectlOutputError:
            raised_count += 1

    assert raised_count == 3
    assert len(poster.posted) == 1
    assert "cannot see the cluster" in poster.posted[0]


def test_kubectl_unreachable_never_calls_the_findings_notify_path():
    """The unreachable notice is not a `run_checks` finding -- the activity's own `notify`
    parameter (for this run's findings) must never fire when there were none to compute."""

    def broken_collect():
        raise cluster_health.MissingKubectlOutputError("kubectl: command not found")

    calls = {"notify": 0}

    def counting_notify(notifications, poster):
        calls["notify"] += 1

    try:
        activity.run_cluster_health_check(collect=broken_collect, notify=counting_notify)
    except cluster_health.MissingKubectlOutputError:
        pass

    assert calls["notify"] == 0


# -- partial outage: the result names which resource kinds were skipped ---------------------


def test_a_three_of_four_down_run_reports_the_three_by_name():
    snapshot = cluster_health.ClusterSnapshot(
        pods=None,
        cronjobs=None,
        jobs=None,
        applications=_load("degraded_application.json"),
    )
    poster = RecordingPoster()

    result = activity.run_cluster_health_check(
        now_fn=lambda: datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc),
        collect=lambda: snapshot,
        poster_fn=lambda: poster,
    )

    assert result["skipped_resource_kinds"] == ["pods", "cronjobs", "jobs"]


def test_a_fully_healthy_run_reports_no_skipped_resource_kinds():
    snapshot = _snapshot(_load("healthy_post_remediation.json"))
    poster = RecordingPoster()

    result = activity.run_cluster_health_check(
        now_fn=lambda: datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc),
        collect=lambda: snapshot,
        poster_fn=lambda: poster,
    )

    assert result["skipped_resource_kinds"] == []
