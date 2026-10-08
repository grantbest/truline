"""Scheduled GitHub Actions workflow-run health check.

`build-mcp-hub.yml` failed on every run for eight days and nobody noticed: the pipeline reported
failure correctly every time, but nothing polled `gh run list` or the Actions API, so a red build
was a fact that existed only on a web page somebody had to think to open. `workflow_run_health.py`
is the checker; this activity is what makes it runnable as a Temporal activity, registered as a
second leg of the existing 15-minute `ClusterHealthWorkflow`/cluster-health schedule
(`workflows/cluster_health.py`) rather than a schedule of its own -- see that module's docstring
for why. This module follows `activities/cluster_health.py`'s idiom -- a thin driver over the
checker's own public functions (`collect_workflow_runs_snapshot`, `failing_workflow_findings`,
`observed_workflow_names`, `notify_findings`) that reimplements none of the detection logic, so
the CLI entry point and the scheduled run can never independently drift on what counts as a
finding.

The injectable parameters below default to `None`, not directly to the `workflow_run_health`
functions themselves, and are resolved from that module inside the function body instead. Binding
them as literal defaults evaluates `workflow_run_health.<name>` at `def`-time -- this module's own
import time -- and `workflow_run_health.py` imports `failure_diagnosis` -> `worker_revision` ->
`dispatch` -> `activities/__init__` -> back to this module, a cycle that (whenever the top-level
`workflow_run_health` module is the first thing imported in a process, e.g. `tests/
test_workflow_run_health.py` importing it directly before anything pulls in `activities`) reaches
this file while `workflow_run_health` is still mid-initialization and hasn't defined those
functions yet, raising `AttributeError: partially initialized module ... has no attribute ...`.
Resolving inside the function body defers the lookup until both modules have fully finished
importing, which is always true by the time an activity is actually invoked.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from temporalio import activity


def _repo_from_env() -> str:
    return os.environ["FACTORY_REPO"]


def run_workflow_run_health_check(
    *,
    repo_fn: Callable[[], str] = _repo_from_env,
    collect: Callable[[str], list[dict[str, Any]]] | None = None,
    find: Callable[[list[dict[str, Any]]], list[Any]] | None = None,
    observe: Callable[[list[dict[str, Any]]], set[str]] | None = None,
    notify: Callable[..., list[Any]] | None = None,
) -> dict[str, Any]:
    """Run the checker end-to-end and report what it found.

    `collect` raises `workflow_run_health.MissingGhRunOutputError` when `gh` itself could not
    answer -- deliberately left uncaught here as far as this activity's own outcome goes, so that
    failure still becomes a failed Temporal activity attempt (and a failed scheduled drain, per
    `schedule_runtime.describe_factory_schedule_status`) rather than a quiet, successful run that
    reports zero findings because it never actually looked. Before re-raising, it posts a deduped
    "checker cannot reach GitHub" notice through `workflow_run_health.announce_unreachable` --
    deliberately not through the injected `notify` above: that parameter is for this run's own
    findings, and an unreachable check produced none.
    """
    import workflow_run_health

    collect = collect or workflow_run_health.collect_workflow_runs_snapshot
    find = find or workflow_run_health.failing_workflow_findings
    observe = observe or workflow_run_health.observed_workflow_names
    notify = notify or workflow_run_health.notify_findings

    repo = repo_fn()
    try:
        runs = collect(repo)
    except workflow_run_health.MissingGhRunOutputError as exc:
        workflow_run_health.announce_unreachable(exc)
        raise
    findings = find(runs)
    notify(findings, observed_workflow_names=observe(runs))
    return {
        "status": "reported",
        "repo": repo,
        "run_count": len(runs),
        "finding_count": len(findings),
        "failing_workflows": sorted(finding.workflow_name for finding in findings),
    }


@activity.defn(name="report_workflow_run_health")
def report_workflow_run_health_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_workflow_run_health_check()


ACTIVITIES = [report_workflow_run_health_activity]
