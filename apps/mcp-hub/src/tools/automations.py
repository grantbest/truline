"""Manual triggers for the automations worker's Temporal workflows.

The automations app (apps/automations) runs in its own Temporal namespace
with its own worker and task queue; mcp-hub starts runs by schedule id or
workflow-type name only, so it never imports that code. Same lazy-client
pattern as tools/schedules.py, but a separate connection: TEMPORAL_NAMESPACE
is mcp-hub's own namespace, so the automations client gets its own env vars.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

logger = logging.getLogger(__name__)

_client: Optional[Client] = None
_client_lock = asyncio.Lock()

PARKING_SCHEDULE_ID = "pay-train-parking-daily"
PARKING_WORKFLOW_TYPE = "PayTrainParkingWorkflow"


async def get_automations_client() -> Client:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            address = os.environ.get(
                "TEMPORAL_ADDRESS", "temporal.platform-core.svc.cluster.local:7233"
            )
            namespace = os.environ.get("AUTOMATIONS_TEMPORAL_NAMESPACE", "automations")
            logger.info("Connecting automations Temporal client: %s ns=%s", address, namespace)
            _client = await Client.connect(address, namespace=namespace)
    return _client


def _task_queue() -> str:
    return os.environ.get("AUTOMATIONS_TASK_QUEUE", "automations-queue")


def _is_not_found(exc: Exception) -> bool:
    return (
        isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND
    ) or "not found" in str(exc).lower()


async def trigger_pay_train_parking() -> Dict[str, Any]:
    """Trigger today's train-parking payment run.

    Preferred path: trigger the worker-registered schedule, which reuses the
    schedule's date-stamped workflow id (pay-train-parking-YYYY-MM-DD), so a
    same-day re-trigger lands on the same id and the workflow's own guards
    (HA skip boolean, PARKMOBILE_DRY_RUN, already-paid-today) decide what
    actually happens. Fallback when the schedule doesn't exist (worker not
    yet booted): start the workflow directly by type name.
    """
    client = await get_automations_client()
    handle = client.get_schedule_handle(PARKING_SCHEDULE_ID)
    try:
        await handle.trigger()
        return {"via": "schedule", "id": PARKING_SCHEDULE_ID}
    except Exception as exc:  # noqa: BLE001 — NOT_FOUND falls through to direct start
        if not _is_not_found(exc):
            raise

    workflow_id = (
        f"pay-train-parking-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-manual"
    )
    await client.start_workflow(
        PARKING_WORKFLOW_TYPE,
        id=workflow_id,
        task_queue=_task_queue(),
        execution_timeout=timedelta(minutes=30),
    )
    logger.info(
        "No %s schedule; started %s directly as %s",
        PARKING_SCHEDULE_ID, PARKING_WORKFLOW_TYPE, workflow_id,
    )
    return {"via": "workflow", "id": workflow_id}
