"""SDD Phase 4 — Rule commit (mcp-hub side).

The Substrate dry-run is read-only. Committing a rule (a) persists it as a
``finance.rule`` bead and (b) starts the background retroaction workflow. Both
need things Substrate doesn't have (a Temporal client; the finance bead
helpers live here), so the ``POST /finance/rules/commit`` endpoint routes to
this module — the same split the Phase 3 scenario engine uses.

Unlike ``run_scenario`` (which awaits its result), retroaction can touch
thousands of beads, so we **start** the workflow fire-and-forget and return
immediately with the rule bead id + workflow id for the console to track.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from tools.finance import create_bead
from tools.schedules import _task_queue, get_temporal_client

logger = logging.getLogger(__name__)

_RULE_TYPE = "rule"


async def commit_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a categorization rule and start its retroaction workflow.

    ``rule`` is the same ``{field, operator, value, target_category}`` payload
    the dry-run backtests. We save it as an ``active`` ``finance.rule`` bead,
    then start :class:`FinanceRuleApplyWorkflow` by type name (so this module
    never imports workflow code) and return without awaiting it.
    """
    bead = await create_bead(
        _RULE_TYPE,
        {**rule, "created_at": datetime.now(timezone.utc).isoformat()},
        state="active",
        created_by="rule-sandbox/commit",
    )
    rule_bead_id = bead.get("id")

    client = await get_temporal_client()
    workflow_id = (
        "finance-rule-apply-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    await client.start_workflow(
        "FinanceRuleApplyWorkflow",
        {"rule": rule, "rule_bead_id": rule_bead_id},
        id=workflow_id,
        task_queue=_task_queue(),
        execution_timeout=timedelta(minutes=15),
    )
    logger.info("Committed rule %s; started retroaction %s", rule_bead_id, workflow_id)
    return {
        "rule_bead_id": rule_bead_id,
        "workflow_id": workflow_id,
        "status": "applying",
    }
