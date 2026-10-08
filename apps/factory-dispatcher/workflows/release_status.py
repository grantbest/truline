"""Temporal workflow for the nightly release status report.

Direct clone of workflows/verdict_staleness.py: one activity, one attempt, read-only with
respect to release lifecycle. The activity (activities/release_status.py) measures every release
not in state released and writes arch.observation beads; it has no call available to it that
patches an arch.release bead's state, so this workflow cannot advance, close, or otherwise
change a release's lifecycle -- that remains an operator act.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class ReleaseStatusWorkflow:
    """Durable owner for one read-only release-status report pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "report_release_status",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
