"""Temporal workflow for the scheduled cluster health check (OPS-8) and, riding the same
15-minute schedule, the GitHub Actions workflow-run health check (`workflow_run_health.py`):
both are read-only "a failure was reported correctly and nobody read it" checks with no shared
credential or state dependency, so this workflow runs them in sequence rather than each needing
its own schedule and worker wiring -- the same reasoning `workflows/doctrine_staleness.py` used to
fold `check_deployed_revision_drift` into its own nightly rather than giving it a fourth schedule,
applied here once more rather than reinvented. Declaring a new Temporal Schedule also trips
`scripts/readme_conformance.py`'s machinery-section check, which requires every schedule
`schedule_runtime.py` declares to be named in `docs/architecture/README.md` -- out of scope for
this bead; riding an existing, already-documented schedule avoids that edit entirely.

Each leg runs -- raising its own declared alerts -- even when the other fails (the same shape
`DoctrineStalenessReportWorkflow` uses for its four legs): a kubectl outage failing this workflow
on its first await must not silence the GitHub Actions leg's own unreachable alert in exactly the
outage it exists to report, and vice versa. The first failure stays visible: it is re-raised once
both legs have had their turn, so a failed attempt still surfaces as a failed scheduled drain (see
activities/cluster_health.py, activities/workflow_run_health.py) rather than being swallowed by a
retry that quietly succeeds on the next try -- neither leg carries its own activity retry policy.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class ClusterHealthWorkflow:
    """Durable owner for one read-only cluster-health check pass and one read-only GitHub
    Actions workflow-run health check pass -- both ride the same 15-minute schedule."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        results: dict[str, Any] = {}
        first_error: ActivityError | None = None

        try:
            results["cluster_health"] = await workflow.execute_activity(
                "report_cluster_health",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = exc

        try:
            results["workflow_run_health"] = await workflow.execute_activity(
                "report_workflow_run_health",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = first_error or exc

        if first_error is not None:
            raise first_error
        return results
