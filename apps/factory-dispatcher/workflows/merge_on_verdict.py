"""Temporal workflow for one merge-by-verdict request (R26.09/O-2).

Started by the gateway (``apps/mcp-hub/src/tools/factory_merge.py``), which
holds no GitHub credential and never merges itself. This workflow's one
activity (``activities/merge_on_verdict.py``, on the worker that holds `gh
auth`) does the real work; this workflow's own job is to make the outcome
queryable without the caller ever blocking on it -- a phone request gets a
202 with the workflow id immediately, and reads the disposition back later
through :meth:`disposition`.

``self._disposition`` is set BEFORE re-raising an activity failure, not only
on success, so a query always answers something meaningful: ``{"status":
"running"}`` while in flight, ``{"status": "done", ...}`` merging in the
activity's own result dict (which already carries its own ``disposition``/
``reason`` fields) once it returns, or ``{"status": "failed", "error": ...}``
if the activity itself raised (a `gh`/environment fault the activity's own
try/except didn't already turn into a refusal).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=15)
# Never auto-retried: a retry after a merge-pr.sh call that partially
# succeeded (merged, but the activity died before returning) must not risk a
# second `gh pr merge` attempt run unsupervised. Same NO_ACTIVITY_RETRY shape
# workflows/cluster_health.py uses for its own single-attempt legs.
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class MergeOnVerdictWorkflow:
    def __init__(self) -> None:
        self._disposition: dict[str, Any] = {"status": "running"}

    @workflow.query
    def disposition(self) -> dict[str, Any]:
        return self._disposition

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await workflow.execute_activity(
                "run_merge_on_verdict",
                request,
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            self._disposition = {"status": "failed", "error": str(exc)[:500]}
            raise
        self._disposition = {"status": "done", **result}
        return self._disposition
