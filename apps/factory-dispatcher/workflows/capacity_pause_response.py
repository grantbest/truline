"""Temporal workflow for the scheduled capacity-pause resume probe (OPS-66).

Direct clone of workflows/worker_revision_drift.py's shape: one activity, one attempt. The
activity (activities/capacity_pause_response.py) reads whether the dispatch schedule is paused
and, if it is, decides RESUMED, STILL_EXHAUSTED, or NOT_OURS via
capacity_pause_response.respond_to_capacity_pause. See that module's docstring and
.factory/design.md for the full design.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

#: Comfortably above PROBE_BUDGET_MINUTES (2) plus containment/connection overhead -- matches
#: workflows/worker_revision_drift.py's own timeout rather than inventing a tighter one.
ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class CapacityPauseResumeWorkflow:
    """Durable owner for one capacity-pause resume probe pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "probe_capacity_pause_resume",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
