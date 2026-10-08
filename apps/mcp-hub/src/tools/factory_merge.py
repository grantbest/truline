"""Starts and reads back the merge-by-verdict Temporal workflow (R26.09/O-2).

The gateway carries no GitHub credential (grep GITHUB_TOKEN/GH_TOKEN/gh over
the mcp-hub image and its k8s base finds none) and must not read GitHub or
merge itself: ``MergeOnVerdictWorkflow`` and its activity
(``apps/factory-dispatcher/workflows`` and ``activities/merge_on_verdict.py``)
run on the factory-dispatcher worker, which already holds ``gh auth``, and do
that work. This module only starts that workflow and reads its recorded
disposition back, over a SECOND Temporal client connection this app opens for
exactly this purpose.

Deliberately NOT ``tools/automations.py``'s client: that module's namespace is
mcp-hub's own automations namespace (``AUTOMATIONS_TEMPORAL_NAMESPACE``,
default ``automations``) -- no factory-dispatcher worker polls that task
queue. This module points at the dispatcher's own namespace/task queue
instead (``FACTORY_TEMPORAL_NAMESPACE``/``FACTORY_TASK_QUEUE``, defaulting to
the live values ``apps/factory-dispatcher/dispatch.py``'s ``TEMPORAL_NAMESPACE``
and ``worker.py``'s ``TASK_QUEUE`` already use), reading ``TEMPORAL_ADDRESS``
the same way ``tools/automations.py`` and ``tools/schedules.py`` both do. See
.factory/design.md for the full record.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, Optional

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

WORKFLOW_TYPE = "MergeOnVerdictWorkflow"

_client: Optional[Client] = None
_client_lock = asyncio.Lock()


class NotFound(RuntimeError):
    """No such merge-on-verdict workflow execution."""


def _is_not_found(exc: Exception) -> bool:
    return (
        isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND
    ) or "not found" in str(exc).lower()


async def get_factory_dispatcher_client() -> Client:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            address = os.environ.get(
                "TEMPORAL_ADDRESS", "temporal.platform-core.svc.cluster.local:7233"
            )
            namespace = os.environ.get("FACTORY_TEMPORAL_NAMESPACE", "dev")
            _client = await Client.connect(address, namespace=namespace)
    return _client


def _task_queue() -> str:
    return os.environ.get("FACTORY_TASK_QUEUE", "factory-dispatcher-dev")


async def start_merge_on_verdict(number: int, requested_by: str, scope: str) -> Dict[str, Any]:
    """Start ``MergeOnVerdictWorkflow`` on the dispatcher's worker.

    Never merges or reads GitHub here -- that happens in the workflow's own
    activity, on the worker that holds the ``gh`` credential. Returns
    immediately with the started workflow id, so the caller (the gateway
    route) can answer 202 without waiting on ``gh``, ``merge-pr.sh``, or its
    post-merge health check.
    """
    client = await get_factory_dispatcher_client()
    workflow_id = f"merge-on-verdict-{number}-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        WORKFLOW_TYPE,
        {"pr_number": number, "requested_by": requested_by, "scope": scope},
        id=workflow_id,
        task_queue=_task_queue(),
    )
    return {"workflow_id": handle.id, "run_id": handle.run_id}


async def get_merge_disposition(workflow_id: str) -> Dict[str, Any]:
    """Read back a started workflow's recorded disposition via its query
    handler -- never blocks on the workflow completing, and never re-reads
    GitHub or the store itself."""
    client = await get_factory_dispatcher_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query("disposition")
    except Exception as exc:  # noqa: BLE001 -- NOT_FOUND maps to a 404 below; anything else propagates
        if _is_not_found(exc):
            raise NotFound(f"no merge-on-verdict workflow found for id {workflow_id!r}") from exc
        raise
