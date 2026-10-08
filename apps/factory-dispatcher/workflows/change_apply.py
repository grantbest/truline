"""Temporal workflow for the `arch.change` merged-PR reconciler.

Direct clone of `workflows/release_apply.py`: one activity, one attempt. The idempotency this
must carry -- the same merged-PR range reconciled twice writes nothing new -- lives in
`activities/change_apply.py`'s own watermark and per-PR `find_change` check, which this workflow
calls unchanged rather than reimplementing. Temporal holds run-state only (Pillar 10).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class ChangeApplyWorkflow:
    """Durable owner for one merged-PR-to-arch.change reconcile pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "apply_merged_pr_changes",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
