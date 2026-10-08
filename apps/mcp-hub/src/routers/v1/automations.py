"""Automations capability: manual triggers for the automations worker.

Deliberately narrow — one endpoint per automation, no generic "run any
workflow" surface. The scope model is per capability, and the endpoint
docstrings double as the MCP tool descriptions remote agents read.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter

from access_auth import check_scope
from tools.automations import trigger_pay_train_parking

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/pay_train_parking/trigger",
    summary="Pay today's train parking",
    operation_id="trigger_pay_train_parking",
)
async def automations_trigger_pay_train_parking() -> Dict[str, Any]:
    """Trigger today's Metra train-parking payment automation.

    Runs the same Temporal workflow as the weekday schedule, so every
    kill-switch stays in effect (Home Assistant skip boolean, dry-run mode,
    already-paid-today guard). Returns the Temporal identifier for auditing.
    """
    check_scope("automations.trigger")
    return await trigger_pay_train_parking()
