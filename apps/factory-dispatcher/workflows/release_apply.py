"""Temporal workflow for the in-cluster release-charter applier.

Direct clone of workflows/ea_apply.py: one activity, one attempt. The idempotency this must
carry -- the same charter content applied twice writes nothing -- lives in
scripts/release-load.py's own `reconcile()` (a bead is patched only when its content differs from
the charter on disk), which activities/release_apply.py calls unchanged rather than
reimplementing. Temporal holds run-state only (Pillar 10); the reconcile's own creates/updates/
unchanged/errors summary is the record, not something a Temporal query would be needed to see.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class ReleaseApplyWorkflow:
    """Durable owner for one release-charter reconcile pass."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            "apply_release_charters",
            request or {},
            start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
            retry_policy=NO_ACTIVITY_RETRY,
        )
