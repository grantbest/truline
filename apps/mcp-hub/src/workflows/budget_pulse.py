"""Daily proactive budgeting workflow — Phase 4.2.

Runs every weekday 08:30 America/Chicago. Compares each active
``finance.budget`` against month-to-date transaction spend. For every
category whose utilisation is ``> 80%``, persists a ``finance.alert``
bead, then asks the LLM to format a single Discord nudge over the
combined alert set.

Auth: every Substrate request flows through ``substrate_headers``
(Amendment 7 — X-API-Key required). LiteLLM keeps the bearer pattern.

No-alert short-circuit: when nothing is over 80% the workflow returns
without writing alert beads or sending a Discord message.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import create_bead, get_budget_status
    from src.tools.bill_guardian import get_bill_guardian_summary
    from src.tools.notify import (
        AlertSeverity,
        alert_content_fingerprint,
        finance_alert_policy,
        post_discord_raise_on_error,
        send_alert,
    )
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.tools.litellm_client import LIFEOPS_DEFAULT_MODEL, chat_completion
    from src.tools.provenance import build_provenance


logger = logging.getLogger(__name__)

ALERT_THRESHOLD = 0.80
ACCOUNT_REFERENCE_RE = re.compile(
    r"(?i)\b(account|acct|card|mask|ending(?:\s+in)?)\b([\s:#*\-]*)(\*{2,}\d{2,}|x{2,}\d{2,}|\d{2,})"
)


def _current_workflow_id() -> Optional[str]:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _alert_policy():
    return finance_alert_policy(post=post_discord_raise_on_error)


async def _send_discord_notification(message: str) -> bool:
    return await send_alert(
        _alert_policy(),
        "budget_pulse.finance_nudge",
        alert_content_fingerprint(message),
        message,
        severity=AlertSeverity.ACTIONABLE,
    )


def _month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def redact_account_references(message: str) -> str:
    """Defense-in-depth for Discord output.

    The prompt only receives category-level budget data, but this keeps an
    unexpected LLM account/card reference from reaching Discord in plaintext.
    """
    return ACCOUNT_REFERENCE_RE.sub(lambda m: f"{m.group(1)} [redacted]", message)


# --- Activities ---


@activity.defn
async def audit_bills_activity() -> Dict[str, Any]:
    """Phase 8.5: audit pending bills for upcoming/overdue status.

    Returns the Bill Guardian summary (upcoming + overdue lists).
    """
    return await get_bill_guardian_summary()


@activity.defn
async def analyze_budget_status_activity() -> Dict[str, Any]:
    """Return categories whose month-to-date spend > 80% of budget.

    Reuses the tool-side ``get_budget_status`` so the proactive pulse
    and the on-demand MCP tool stay in lockstep on the comparison logic.
    Categories without an active budget, and the synthetic ``total``
    row, are skipped — alerts are per-category by definition.
    """
    now = datetime.now()
    status = await get_budget_status(month="current")

    alerts: List[Dict[str, Any]] = []
    for category, row in status.items():
        if category == "total":
            continue
        budget = float(row.get("budget", 0) or 0)
        if budget <= 0:
            continue
        spent = float(row.get("spent", 0) or 0)
        utilization = spent / budget
        if utilization > ALERT_THRESHOLD:
            alerts.append(
                {
                    "category": category,
                    "budget_amount": round(budget, 2),
                    "spent_amount": round(spent, 2),
                    "remaining_amount": round(budget - spent, 2),
                    "utilization_pct": round(utilization * 100, 1),
                }
            )

    alerts.sort(key=lambda a: a["utilization_pct"], reverse=True)

    return {
        "month": _month_key(now),
        "as_of_date": now.date().isoformat(),
        "threshold": ALERT_THRESHOLD,
        "alerts": alerts,
    }


@activity.defn
async def persist_budget_alerts_activity(report: Dict[str, Any]) -> List[str]:
    """Write one ``finance.alert`` bead per over-threshold category.

    Returns the list of created bead ids (used downstream for
    notification provenance).
    """
    alerts = report.get("alerts", [])
    if not alerts:
        return []

    month = report["month"]
    as_of = report["as_of_date"]
    threshold = report["threshold"]
    bead_ids: List[str] = []
    provenance = build_provenance(
        worker="budget-pulse-workflow",
        model=None,
        prompt_ref="budget-pulse/persist",
    )

    for alert in alerts:
        content = {
            "category": alert["category"],
            "budget_amount": alert["budget_amount"],
            "spent_amount": alert["spent_amount"],
            "remaining_amount": alert["remaining_amount"],
            "utilization_pct": alert["utilization_pct"],
            "month": month,
            "as_of_date": as_of,
            "threshold_breached": threshold,
        }
        bead = await create_bead(
            "alert",
            content,
            "active",
            "budget-pulse-workflow",
            trust_tier="system",
            provenance=provenance,
        )
        bead_id = bead.get("id")
        if bead_id:
            bead_ids.append(bead_id)

    return bead_ids


@activity.defn
async def notify_budget_alerts_activity(report: Dict[str, Any]) -> Optional[str]:
    """Format the combined alert set as a Discord nudge and post it.

    Records a ``platform.cost`` bead for the LLM call regardless of
    whether the Discord webhook is configured. Returns the formatted
    message (or ``None`` when there's nothing to send).
    """
    import json
    import os
    import time

    from src.untrusted import wrap_untrusted_short

    alerts = [
        {**a, "category": wrap_untrusted_short(a.get("category"))}
        for a in report.get("alerts", [])
    ]
    upcoming = [
        {**b, "vendor": wrap_untrusted_short(b.get("vendor"))}
        for b in report.get("bills_upcoming", [])
    ]
    overdue = [
        {**b, "vendor": wrap_untrusted_short(b.get("vendor"))}
        for b in report.get("bills_overdue", [])
    ]

    if not alerts and not upcoming and not overdue:
        return None

    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    model = os.environ.get("BUDGET_PULSE_MODEL", LIFEOPS_DEFAULT_MODEL)

    prompt = f"""You are the Truline CFO sending a short Discord nudge.
Month: {report['month']}. As-of: {report['as_of_date']}.

BUDGET ALERTS (categories over {int(report.get('threshold', 0.8) * 100)}% of monthly budget):
{json.dumps(alerts, indent=2)}

BILL GUARDIAN:
- Upcoming (3d): {json.dumps(upcoming, indent=2)}
- Overdue: {json.dumps(overdue, indent=2)}

Write ONE Discord message (plain text, no markdown headers).
- Start the message with: "⚠️ Finance Nudge:".
- For budget alerts: "- {{pct}}% of {{Category}} budget (${{spent}}/${{budget}})."
- For overdue bills: "🔴 OVERDUE: {{vendor}} (${{amount}}) due {{date}}."
- For upcoming bills: "📅 Due Soon: {{vendor}} (${{amount}}) on {{date}}."
- Capitalize category names and vendors.
- Keep numbers to 0–2 decimals.
- No closing remark. Maximum 10 lines total."""

    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"} if LITELLM_API_KEY else {}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    started = time.monotonic()
    result = await chat_completion(payload, headers=headers, timeout=60.0)
    body = result.body
    latency_ms = (time.monotonic() - started) * 1000.0

    message = redact_account_references(body["choices"][0]["message"]["content"].strip())

    await log_llm_cost(
        model=model,
        usage=body.get("usage"),
        agent="budget-pulse/notify",
        context={
            "month": report["month"],
            "as_of_date": report["as_of_date"],
            "alert_count": len(alerts),
            "bill_upcoming_count": len(upcoming),
            "bill_overdue_count": len(overdue),
        },
        workflow_id=_current_workflow_id(),
        latency_ms=latency_ms,
        prompt_hash_value=prompt_hash(prompt),
        cost_usd=result.cost_usd,
    )

    # HTTP failures still reach Temporal retries through the policy's post
    # adapter; state failures fail open inside AlertPolicy.
    await _send_discord_notification(message)

    return message


# --- Workflow ---


@workflow.defn
class DailyBudgetPulseWorkflow:
    """Daily proactive budgeting pulse. Scheduled by
    ``temporal_worker.ensure_schedules`` for 08:30 America/Chicago
    Mon-Fri."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        # Phase 4.1: maximum_interval >= 60s straddles the Gemini
        # free-tier per-minute quota reset. 90s adds headroom.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=90),
            maximum_attempts=3,
        )

        # 1. Budget analysis
        report = await workflow.execute_activity(
            analyze_budget_status_activity,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # 2. Bill Guardian audit (Phase 8.5)
        bill_report = await workflow.execute_activity(
            audit_bills_activity,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # Merge bill findings into the combined report for notification
        report["bills_upcoming"] = bill_report.get("upcoming", [])
        report["bills_overdue"] = bill_report.get("overdue", [])

        alert_count = len(report.get("alerts", []))
        bill_issue_count = len(report["bills_upcoming"]) + len(report["bills_overdue"])

        if alert_count == 0 and bill_issue_count == 0:
            return {
                "alert_count": 0,
                "bill_issue_count": 0,
                "alert_bead_ids": [],
                "notified": False,
                "month": report.get("month"),
            }

        # 3. Persist budget alerts (if any)
        alert_bead_ids = []
        if alert_count > 0:
            alert_bead_ids = await workflow.execute_activity(
                persist_budget_alerts_activity,
                report,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

        # 4. Notify Discord (combined budget + bills)
        await workflow.execute_activity(
            notify_budget_alerts_activity,
            report,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=retry_policy,
        )

        return {
            "alert_count": alert_count,
            "bill_issue_count": bill_issue_count,
            "alert_bead_ids": alert_bead_ids,
            "notified": True,
            "month": report.get("month"),
        }
