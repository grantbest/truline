"""Temporal workflow for the knowledge ingestion engine (F-DCE-4).

Files one extraction ``dev.task`` per registered, unfiled knowledge source,
then lands each merged extraction task's candidates as ``proposed``
``arch.principle`` beads. All decision logic lives in
``activities.knowledge_ingestion`` (plain Python, unit-testable without a
Temporal server); this module only sequences the two activities.

Neither activity returns takeaway or candidate text, so Temporal's workflow
history for a run of this workflow carries only ids, counts, and short
structural reasons — never doctrine content.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class KnowledgeIngestionWorkflow:
    """Durable owner for one knowledge-ingestion pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = request or {}
        filing = await workflow.execute_activity(
            "file_knowledge_extraction_tasks",
            request,
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
        landing = await workflow.execute_activity(
            "land_knowledge_principles",
            request,
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
        return {"filing": filing, "landing": landing}
