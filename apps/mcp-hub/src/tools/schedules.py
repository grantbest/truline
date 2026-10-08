"""Temporal schedule operations for the API pod (Plaid plan PR 9 / W8).

The worker registers bank-sync schedules at boot (and PR 7's Reloader
restarts it on token changes, which re-reconciles). What was missing:

  * sync-now — trigger an institution's BankSyncWorkflow on demand
    (the Console page's "Sync now" button) without waiting for 03:00.
  * reconcile — force schedule registration without a worker restart.
  * visibility — per-institution schedule state (paused, last/next run,
    or MISSING entirely) so drift is a status, not a surprise.

The API pod runs the same image as the worker, so temporalio and the
worker's schedule definitions are importable; the Temporal client is
created lazily and cached so pods that never touch these endpoints
never open the connection.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.service import RPCError, RPCStatusCode

from tools.plaid import list_linked_institutions

logger = logging.getLogger(__name__)

_client: Optional[Client] = None
_client_lock = asyncio.Lock()

BANK_SYNC_SCHEDULE_PREFIX = "bank-sync-"


async def get_temporal_client() -> Client:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            address = os.environ.get(
                "TEMPORAL_ADDRESS", "temporal.platform-core.svc.cluster.local:7233"
            )
            namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
            logger.info("Connecting Temporal client: %s ns=%s", address, namespace)
            _client = await Client.connect(address, namespace=namespace)
    return _client


def _task_queue() -> str:
    return os.environ.get("TEMPORAL_TASK_QUEUE", "morning-brief")


def schedules_enabled() -> bool:
    """Whether this environment may register recurring schedules.

    Mirrors ``temporal_worker.temporal_schedules_enabled`` — deliberately
    duplicated rather than imported, because importing temporal_worker pulls
    in every workflow module (see ``reconcile_schedules`` below).
    """
    return os.environ.get("ENABLE_TEMPORAL_SCHEDULES", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def bank_sync_workflow_retry_policy() -> RetryPolicy:
    """Retry direct/manual bank-sync starts the same way schedules do."""
    return RetryPolicy(
        initial_interval=timedelta(minutes=10),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(hours=2),
        maximum_attempts=4,
    )


def _is_not_found(exc: Exception) -> bool:
    return (
        isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND
    ) or "not found" in str(exc).lower()


async def trigger_bank_sync(institution_slug: str) -> Dict[str, Any]:
    """Run an institution's bank sync now.

    Preferred path: trigger the existing ``bank-sync-{slug}`` schedule,
    so the run uses exactly the schedule's workflow configuration and
    shows up in the schedule's recent-actions history. If the schedule
    doesn't exist yet (token added but worker not yet restarted), fall
    back to starting BankSyncWorkflow directly — by workflow-type name,
    so this module never imports the workflow code.
    """
    client = await get_temporal_client()
    schedule_id = f"{BANK_SYNC_SCHEDULE_PREFIX}{institution_slug}"
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.trigger()
        return {"institution": institution_slug, "via": "schedule", "id": schedule_id}
    except Exception as exc:  # noqa: BLE001 — NOT_FOUND falls through to direct start
        if not _is_not_found(exc):
            raise

    workflow_id = (
        f"{schedule_id}-manual-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    await client.start_workflow(
        "BankSyncWorkflow",
        institution_slug,
        id=workflow_id,
        task_queue=_task_queue(),
        execution_timeout=timedelta(hours=2),
        retry_policy=bank_sync_workflow_retry_policy(),
    )
    logger.info(
        "No %s schedule; started BankSyncWorkflow directly as %s",
        schedule_id, workflow_id,
    )
    return {"institution": institution_slug, "via": "workflow", "id": workflow_id}


async def run_scenario(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the SDD Phase 3 scenario engine on demand and return its result.

    The console can't reach Temporal directly, so the ``POST /finance/scenario``
    endpoint calls this to start ``FinanceScenarioRunnerWorkflow`` and **wait**
    for its projection (``execute_workflow``). The math is cheap, so the
    synchronous round-trip hands the UI an immediate baseline-vs-scenario series
    to render; the workflow still persists a ``finance.projection_scenario``
    bead so Substrate stays the source of truth. Started by workflow-type name
    so this module never imports the workflow code.
    """
    client = await get_temporal_client()
    workflow_id = (
        "finance-scenario-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    result = await client.execute_workflow(
        "FinanceScenarioRunnerWorkflow",
        payload,
        id=workflow_id,
        task_queue=_task_queue(),
        execution_timeout=timedelta(minutes=3),
    )
    logger.info("Scenario workflow %s completed.", workflow_id)
    return result


async def reconcile_schedules() -> Dict[str, Any]:
    """Re-run the worker's idempotent schedule registration on demand.

    Imported lazily: src.temporal_worker pulls in every workflow module,
    which the API pod shouldn't pay for at import time just to serve
    health checks.

    Gated on ENABLE_TEMPORAL_SCHEDULES. The worker checks the same flag at
    boot, but this endpoint didn't — so one call against the DEV API pod
    (or the ``reconcile_schedules`` MCP tool, which any agent can reach)
    re-created the full recurring set in the dev Temporal namespace behind
    a worker that was configured never to create it. That is how dev ended
    up running 14 live schedules — including sandbox-Plaid bank syncs whose
    stale-token alerts were indistinguishable from prod's (2026-08-01).
    """
    if not schedules_enabled():
        logger.warning(
            "reconcile_schedules refused: ENABLE_TEMPORAL_SCHEDULES is off in this environment."
        )
        return {
            "reconciled": False,
            "reason": (
                "ENABLE_TEMPORAL_SCHEDULES is not enabled in this environment; "
                "recurring schedules are registered by the prod worker only."
            ),
            "institutions": list_linked_institutions(),
        }

    from src.temporal_worker import ensure_schedules

    client = await get_temporal_client()
    await ensure_schedules(client)
    return {
        "reconciled": True,
        "institutions": list_linked_institutions(),
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


async def _describe_bank_sync(client: Client, institution_slug: str) -> Dict[str, Any]:
    schedule_id = f"{BANK_SYNC_SCHEDULE_PREFIX}{institution_slug}"
    handle = client.get_schedule_handle(schedule_id)
    try:
        desc = await handle.describe()
    except Exception as exc:  # noqa: BLE001
        if _is_not_found(exc):
            # Token exists but no schedule — worker hasn't reconciled yet.
            return {
                "institution": institution_slug,
                "schedule_id": schedule_id,
                "exists": False,
            }
        raise

    recent = desc.info.recent_actions or []
    last = recent[-1] if recent else None
    next_times = desc.info.next_action_times or []
    return {
        "institution": institution_slug,
        "schedule_id": schedule_id,
        "exists": True,
        "paused": desc.schedule.state.paused,
        "note": desc.schedule.state.note,
        "num_actions": desc.info.num_actions,
        "last_run_at": _iso(last.started_at if last else None),
        "next_run_at": _iso(next_times[0] if next_times else None),
    }


async def get_bank_sync_schedules() -> Dict[str, Any]:
    """Schedule state for every linked institution (Console + ops view)."""
    client = await get_temporal_client()
    slugs = list_linked_institutions()
    described = await asyncio.gather(*(_describe_bank_sync(client, s) for s in slugs))
    return {
        "task_queue": _task_queue(),
        "schedules": list(described),
        "missing": [d["institution"] for d in described if not d["exists"]],
    }
