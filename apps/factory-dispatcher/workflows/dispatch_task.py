"""Temporal workflow for one factory dispatcher pass."""

from __future__ import annotations

from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

import retry_policy
from workflow_core import (
    ACTIVITY_START_TO_CLOSE_TIMEOUT,  # noqa: F401 - re-exported, `run`'s own 24h budget
    DispatchAttemptFailed,
    DispatchRetryContext,
    heartbeat_timeout_for_step,
    run_dispatch_sequence,
    start_to_close_timeout_for_step,
)

# NO_ACTIVITY_RETRY is deliberate, not an oversight: DISPATCH_RETRY_POLICY
# below is the only thing that counts a bead's attempts (see
# DispatchRetryContext's docstring in workflow_core.py) -- an activity-level
# retry here would double-count against it. What changed in this amendment is
# how long an activity may run before Temporal calls it dead
# (start_to_close_timeout_for_step) and how that death is classified
# (workflow_core._is_lost_short_step_death), never whether the activity call
# itself retries.
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)
DISPATCH_RETRY_POLICY = RetryPolicy(
    maximum_attempts=retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS,
)


@workflow.defn
class DispatchTaskWorkflow:
    """Durable owner for the dispatcher step sequence."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        async def execute(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            return await workflow.execute_activity(
                name,
                payload,
                start_to_close_timeout=start_to_close_timeout_for_step(name),
                heartbeat_timeout=heartbeat_timeout_for_step(name),
                retry_policy=NO_ACTIVITY_RETRY,
            )

        retry_context = DispatchRetryContext(
            attempt=workflow.info().attempt,
            maximum_attempts=DISPATCH_RETRY_POLICY.maximum_attempts,
        )
        try:
            return await run_dispatch_sequence(request or {}, execute, retry_context)
        except DispatchAttemptFailed as exc:
            raise ApplicationError(str(exc), type="DispatchAttemptFailed") from exc
