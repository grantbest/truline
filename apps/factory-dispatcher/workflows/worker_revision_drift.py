"""Temporal workflow for the scheduled worker-revision-drift check.

Direct clone of workflows/verdict_staleness.py's shape: one activity, one attempt. The activity
(activities/worker_revision_drift.py) both lands the observation and, since 2026-09-02, responds
to a drifted checkout -- advance-and-recycle, defer, or halt dispatch -- via
worker_checkout_drift_response.py. See that module's docstring and .factory/design.md.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class WorkerRevisionDriftWorkflow:
    """Durable owner for one read-only worker-revision-drift evaluation pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "report_worker_revision_drift",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
