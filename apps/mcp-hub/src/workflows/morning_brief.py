import json
import logging
from datetime import timedelta
from typing import Callable, Dict, Any, Literal, Optional, TypeVar
from temporalio import workflow, activity
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)
T = TypeVar("T")

with workflow.unsafe.imports_passed_through():
    from src.tools.context import get_current_context
    from src.tools.homelab import get_platform_status, get_grafana_alerts
    from src.tools.google import get_calendar_events, get_upcoming_events
    from src.tools.finance import (
        get_bills_due,
        get_budget_status,
        get_recent_transactions,
        substrate_beads_url,
        substrate_headers,
    )
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.tools.litellm_client import LIFEOPS_DEFAULT_MODEL, chat_completion
    from src.tools.notify import (
        AlertSeverity,
        alert_content_fingerprint,
        finance_alert_policy,
        post_discord_raise_on_error,
        send_alert,
    )


def _current_workflow_id() -> Optional[str]:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _alert_policy():
    return finance_alert_policy(post=post_discord_raise_on_error)


async def _send_discord_notification(content: str) -> bool:
    return await send_alert(
        _alert_policy(),
        "morning_brief.daily",
        alert_content_fingerprint(content),
        content,
        severity=AlertSeverity.INFORMATIONAL,
    )


def _safe_data_call(label: str, fn: Callable[[], T], fallback: T) -> T:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - optional brief context should degrade
        logger.warning(
            "Morning brief data source %s failed; continuing with fallback (%s).",
            label,
            type(exc).__name__,
        )
        return fallback


# Activities
@activity.defn
async def gather_data_activity(location: Literal["office", "home"]) -> Dict[str, Any]:
    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()

    # Yesterday is a DAY query, not a month query. This used to call
    # get_transactions(month="current"), which windows to
    # [first-of-this-month, first-of-next-month) — so on the 1st of any month
    # yesterday falls in the *previous* month and was filtered out before the
    # brief ever saw it. Observed 2026-08-01: ten transactions posted 07-31
    # (including $261.63 at Caputo's and a $4,737.97 payroll deposit) while the
    # brief reported "No recorded spending from yesterday", and the anomaly
    # detector flagged one of those same transactions 90 minutes later.
    #
    # get_recent_transactions orders by the bank's posted_date and pages
    # properly, so it also removes the second bug the old call carried: a bare
    # limit=200 over a whole month, when Chase alone posts ~200/month.
    #
    # Transfers are excluded because this feeds a *spending* summary — moving
    # $300 between our own accounts is not spend (LO-CAT-006).
    recent_transactions = await get_recent_transactions(
        limit=2000, include_transfers=False
    )
    yesterday_spending = [
        t for t in recent_transactions
        if t["content"].get("posted_date") == yesterday
    ]


    spending_summary = {}
    for t in yesterday_spending:
        cat = t["content"].get("our_category") or "unreconciled"
        amount = t["content"].get("amount", 0)
        spending_summary[cat] = spending_summary.get(cat, 0) + amount

    data = {
        "context": _safe_data_call("context", get_current_context, {}),
        "today_events": _safe_data_call("calendar_today", get_calendar_events, []),
        "upcoming": _safe_data_call(
            "calendar_upcoming",
            lambda: get_upcoming_events(days_ahead=3),
            [],
        ),
        "platform_status": _safe_data_call("platform_status", get_platform_status, "unknown"),
        "grafana_alerts": _safe_data_call("grafana_alerts", get_grafana_alerts, []),
        "bills_due": await get_bills_due(days_ahead=7),
        "budget_status": await get_budget_status(),
        "yesterday_spending": spending_summary,
        "location": location
    }
    if location == "office":
        data["notes"] = ["Parking automation placeholder: Elgin train station payment status."]
    return data

@activity.defn
async def synthesize_brief_activity(data: Dict[str, Any]) -> str:
    import os
    import time
    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    location = data.get("location", "home")
    from src.untrusted import wrap_untrusted, wrap_untrusted_short

    # Wrap fields in place to preserve all conditional/unknown keys (e.g. notes)
    if "today_events" in data:
        data["today_events"] = [
            {**ev, "title": wrap_untrusted(ev.get("title")), "description": wrap_untrusted(ev.get("description"))}
            for ev in data["today_events"]
        ]
    if "upcoming" in data:
        data["upcoming"] = [
            {**ev, "title": wrap_untrusted(ev.get("title")), "description": wrap_untrusted(ev.get("description"))}
            for ev in data["upcoming"]
        ]
    if "grafana_alerts" in data:
        data["grafana_alerts"] = [
            {**alert, "title": wrap_untrusted(alert.get("title")), "message": wrap_untrusted(alert.get("message"))}
            for alert in data["grafana_alerts"]
        ]
    if "bills_due" in data:
        data["bills_due"] = [
            {**bill, "vendor": wrap_untrusted_short(bill.get("vendor"))}
            for bill in data["bills_due"]
        ]

    model = LIFEOPS_DEFAULT_MODEL
    prompt = f"""
    You are the Truline Personal OS. Generate a concise, location-aware morning briefing for the Operator.

    CURRENT LOCATION: {location}

    DATA:
    {json.dumps(data, indent=2)}

    GUIDELINES:
    - Keep it friendly but efficient.
    - If LOCATION is 'office': Lead with commute/parking context (Chicago). Mention weather for the drive.
    - If LOCATION is 'home': Lead with family/weekend context (Elgin).
    - Focus on today's schedule.
    - Mention cluster health briefly.
    - Finance: Mention any bills due in the next 7 days. If any budget category is over-spent, mention it.
    - Yesterday's Spending: Briefly summarize yesterday's spending from 'yesterday_spending' (e.g., "Yesterday you spent $X across Y categories, mostly on groceries").
    """

    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }

    started = time.monotonic()
    result = await chat_completion(payload, headers=headers, timeout=60.0)
    body = result.body
    latency_ms = (time.monotonic() - started) * 1000.0

    content = body["choices"][0]["message"]["content"]

    # Record spend best-effort — never let a Substrate hiccup block the brief.
    await log_llm_cost(
        model=model,
        usage=body.get("usage"),
        agent="morning-brief/synthesize",
        context={"location": location},
        workflow_id=_current_workflow_id(),
        latency_ms=latency_ms,
        prompt_hash_value=prompt_hash(prompt),
        cost_usd=result.cost_usd,
    )

    return content

@activity.defn
async def send_to_discord_activity(content: str) -> None:
    # HTTP failures still reach Temporal retries through the policy's post
    # adapter; a missing webhook just logs (matches previous silent-return behavior).
    await _send_discord_notification(content)

@activity.defn
async def save_to_substrate_activity(content: str) -> None:
    import httpx
    payload = {
        "namespace": "personal",
        "type": "digest",
        "state": "active",
        "content": {"text": content},
        "trust_tier": "system",
        "created_by": "daily-brief-workflow"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(substrate_beads_url(), json=payload, headers=substrate_headers(), timeout=10.0)
        resp.raise_for_status()

# Workflow
@workflow.defn
class MorningBriefWorkflow:
    @workflow.run
    async def run(self, location: Literal["office", "home"]) -> str:
        # Phase 4.1: maximum_interval >= 60s lets retries straddle the
        # Gemini free-tier per-minute quota reset. 90s gives headroom for
        # rate-limit windows that drift past the minute boundary.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=90),
            maximum_attempts=3,
        )

        # 1. Gather Data
        data = await workflow.execute_activity(
            gather_data_activity,
            location,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # 2. Synthesize Brief
        brief = await workflow.execute_activity(
            synthesize_brief_activity,
            data,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # 3. Post to Discord
        await workflow.execute_activity(
            send_to_discord_activity,
            brief,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policy,
        )

        # 4. Save Digest
        await workflow.execute_activity(
            save_to_substrate_activity,
            brief,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policy,
        )

        return brief
