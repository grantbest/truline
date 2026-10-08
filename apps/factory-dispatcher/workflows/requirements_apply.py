"""Temporal workflow for the in-cluster requirements-registry applier (PC-ASR-002/AC-2)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class RequirementsApplyWorkflow:
    """Durable owner for one requirements-registry reconcile pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "apply_requirement_registries",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
