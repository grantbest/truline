"""Temporal workflow for the nightly requirement verdict staleness report."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class VerdictStalenessReportWorkflow:
    """Durable owner for one read-only staleness report pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "report_verdict_staleness",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
