"""Scheduled cluster health check -- OPS-8's missing schedule.

#509 shipped `cluster_health.py`: kubectl-driven detection of wedged pods, CronJobs with no
successful run for their last schedule, and Degraded ArgoCD Applications, posting findings to the
house Discord webhook. Nothing ever called it except a human running it by hand -- run that way on
2026-08-24 it caught a real open incident (pg-restore-drill silently failing since 2026-08-16) that
had gone unnoticed for over a week. This activity and `workflows/cluster_health.py` are what put it
on the same durable Temporal schedule (`schedule_runtime.py`) the dispatcher's other unattended
checks already run on, instead of a capability that reads as shipped but only fires when someone
remembers it exists. See `.factory/design.md`.

This activity is deliberately a thin driver over `cluster_health.py`'s own public functions
(`collect_kubectl_snapshot`, `run_checks`, `notify`) -- it reimplements none of the pod/CronJob/
ArgoCD detection logic, so the CLI entry point (`python cluster_health.py`) and the scheduled run
can never independently drift on what counts as a finding. Unlike the other scheduled checks in
this package, it writes nothing to substrate: `cluster_health.py` was designed as a stateless,
notify-only checker (see its own module docstring), and this wiring preserves that -- the schedule
is the only new moving part, not a new bead type or a new database write.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from temporalio import activity

import cluster_health


def run_cluster_health_check(
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    collect: Callable[[], cluster_health.ClusterSnapshot] = cluster_health.collect_kubectl_snapshot,
    run_checks: Callable[..., list[cluster_health.Notification]] = cluster_health.run_checks,
    notify: Callable[
        [list[cluster_health.Notification], Any], Any
    ] = cluster_health.notify_new_findings,
    grace_seconds_fn: Callable[[], int] = cluster_health.grace_seconds_from_env,
    poster_fn: Callable[[], Any] = cluster_health.poster_from_env,
) -> dict[str, Any]:
    """Run #509's checker end-to-end and report what it found.

    `collect` raises `cluster_health.MissingKubectlOutputError` when kubectl itself could not
    answer for any tracked resource type -- deliberately left uncaught here as far as this
    activity's own outcome goes, so that failure still becomes a failed Temporal activity attempt
    (and a failed scheduled drain, per `schedule_runtime.describe_factory_schedule_status`) rather
    than a quiet, successful run that reports zero notifications because it never actually looked.
    Before re-raising, it posts a deduped "checker cannot see the cluster" notice through
    `cluster_health.notify_unreachable` -- a path independent of the LAN/tunnel kubectl needs (see
    .factory/design.md) -- deliberately not through the injected `notify` above: that parameter is
    for this run's own findings, and an unreachable cluster produced none.

    `notify` defaults to `cluster_health.notify_new_findings`, which posts only findings that are
    new information since the last run (cross-run dedup, Amendment 30 gate finding on #672) --
    `notification_count`/`sources` below still report the full set `run_checks` found, regardless
    of how much of that set a dedup window suppressed from Discord this run, so a drain-health
    reader always sees the true current state.
    """
    now = now_fn()
    poster = poster_fn()
    try:
        snapshot = collect()
    except cluster_health.MissingKubectlOutputError as exc:
        cluster_health.notify_unreachable(exc, poster)
        raise
    notifications = run_checks(
        pods=snapshot.pods,
        cronjobs=snapshot.cronjobs,
        jobs=snapshot.jobs,
        applications=snapshot.applications,
        now=now,
        grace_seconds=grace_seconds_fn(),
    )
    notify(notifications, poster)
    return {
        "status": "reported",
        "notification_count": len(notifications),
        "sources": sorted({n.source for n in notifications}),
        "skipped_resource_kinds": cluster_health.skipped_resource_kinds(snapshot),
    }


@activity.defn(name="report_cluster_health")
def report_cluster_health_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_cluster_health_check()


ACTIVITIES = [report_cluster_health_activity]
