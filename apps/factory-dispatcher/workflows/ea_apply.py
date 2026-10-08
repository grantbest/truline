"""Temporal workflow for the in-cluster EA-model applier (PC-ASR-007/AC-3).

Mirrors workflows/doctrine_staleness.py: one activity, one attempt. Temporal holds run-state
only (Pillar 10) -- the applier's own activity (activities/ea_apply.py) records the applied
revision, the change summary, and any failure as a standing substrate bead before this workflow
run ends, success or failure; nothing here is the record someone would need to query Temporal to
see. A reconcile failure propagates so the workflow run itself also shows failed, but that is
belt-and-suspenders next to the bead the activity already wrote.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class EaApplyWorkflow:
    """Durable owner for one EA-model reconcile pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "apply_ea_model",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
