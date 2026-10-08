"""Weekly financial-insight workflow.

Runs every Sunday at 23:00 America/Chicago. Pulls the last 7 days of
transactions + active budgets from Substrate, asks the LLM to play
"Truline CFO" over the aggregated category-vs-budget JSON, and writes
the result back as a `finance.insight` bead with transaction-id
provenance in the bead's top-level `provenance.parent_ids` field.

Auth: every Substrate call goes through the packaged substrate_client
(src.tools.finance.create_bead/query_beads, M12; X-API-Key required).
LiteLLM calls keep the existing Authorization-bearer pattern used in
morning_brief and bank_sync.
"""

import logging
import os
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

# The finance tool module imports httpx/urllib at module load, which trips
# Temporal's workflow sandbox. Activities are deterministic-free, so pass
# this through for the workflow validation pass only.
with workflow.unsafe.imports_passed_through():
    from src.tools.finance import (
        FINANCE_NAMESPACE,
        create_bead,
        get_recurring_candidates,
        query_beads,
    )
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.tools.litellm_client import LIFEOPS_DEFAULT_MODEL, chat_completion
    from src.tools.provenance import build_provenance

logger = logging.getLogger(__name__)


def _current_workflow_id() -> Optional[str]:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _finance_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the current insight window.

    Use the beginning of the calendar day seven days ago so weekly insight
    runs include all transactions from that day, not only transactions newer
    than the exact runtime timestamp.
    """
    since_date = (now - timedelta(days=7)).date()
    since = datetime.combine(since_date, time.min)
    if now.tzinfo is not None:
        since = since.replace(tzinfo=now.tzinfo)
    return since, now


def _build_insight_payload(narrative: str, summary: Dict[str, Any], model: str) -> Dict[str, Any]:
    transaction_ids = summary.get("transaction_ids", [])
    return {
        "namespace": FINANCE_NAMESPACE,
        "type": "insight",
        "state": "active",
        "content": {
            "summary": narrative,
            "narrative": narrative,
            "window": summary["window"],
            "by_category": summary["by_category"],
            "totals": summary["totals"],
            "model": model,
            "transaction_ids": transaction_ids,
        },
        "provenance": build_provenance(
            worker="financial-insights-workflow",
            model=model,
            prompt_ref="financial-insights/synthesize",
        ),
        "trust_tier": "system",
        "created_by": "financial-insights-workflow",
    }


# --- Activities ---


@activity.defn
async def summarize_merchant_insights_activity() -> str:
    """Phase 8 Architect: pre-calculate merchant stats for the "Fast-Path" UI.

    Aggregates transactions from the last 12 months, computes YTD totals,
    averages, and frequencies for all merchants. Persists as a
    ``finance.summary`` bead.

    Returns the bead ID.
    """
    from datetime import timezone

    # 1. Reuse recurring candidate logic (12 months lookback)
    candidates = await get_recurring_candidates(
        months=12,
        min_occurrences=2,
        use_semantic_merge=True,
    )

    insights = {}
    for c in candidates:
        merchant_key = c["merchant"]
        insights[merchant_key] = {
            "display_name": c["display_name"],
            "avg_amount": c["avg_amount"],
            "total_ytd": c["total_amount"],
            "occurrences": c["occurrences"],
            "last_paid": c["last_seen"],
            "frequency": _deterministic_frequency_from_cluster(c),
        }

    content = {
        "merchant_insights": insights,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    bead = await create_bead(
        "summary",
        content,
        "active",
        "financial-insights/summarize_merchants",
        trust_tier="system",
    )
    return bead["id"]


def _deterministic_frequency_from_cluster(cluster: Dict[str, Any]) -> str:
    """Heuristic for merchant frequency based on charge gaps."""
    first_seen = cluster.get("first_seen")
    last_seen = cluster.get("last_seen")
    occurrences = int(cluster.get("occurrences") or 0)
    if not first_seen or not last_seen or occurrences < 2:
        return "unknown"
    try:
        first = datetime.fromisoformat(str(first_seen)[:10])
        last = datetime.fromisoformat(str(last_seen)[:10])
    except ValueError:
        return "unknown"
    span_days = max((last - first).days, 1)
    avg_gap = span_days / max(occurrences - 1, 1)
    if 24 <= avg_gap <= 38:
        return "monthly"
    if 6 <= avg_gap <= 10:
        return "weekly"
    if 75 <= avg_gap <= 105:
        return "quarterly"
    if 330 <= avg_gap <= 400:
        return "annual"
    return "unknown"


@activity.defn
async def aggregate_finance_data_activity() -> Dict[str, Any]:
    """Pull last-7-day transactions + active budgets from Substrate and
    summarize into category totals vs. budget targets.

    Returns a dict shaped for the LLM:
        {
          "window": {"since": ISO, "until": ISO},
          "by_category": {
            "groceries": {"spent": 412.10, "budget": 500.0, "remaining": 87.9, "tx_count": 9},
            ...
          },
          "totals": {"spent": ..., "budget": ..., "remaining": ...},
          "transaction_ids": [...]   # for provenance
        }
    """
    now = datetime.now()
    since, until = _finance_window(now)

    # created_after is a GET /beads filter the packaged substrate_client's
    # list_beads does not take (OPS-190 gave it list_beads/search, not a
    # generic passthrough GET) -- the named carve-out (AC-1), routed through
    # finance.query_beads, the one shared helper every such read goes
    # through rather than a per-file copy of the request.
    tx_beads = await query_beads(
        {"type": "transaction", "created_after": since.isoformat(), "limit": 1000}
    )
    # Exclude Plaid-removed beads from the CFO digest's spend math.
    transactions = [b for b in tx_beads if b.get("state") != "removed"]

    budgets = await query_beads({"type": "budget", "state": "active", "limit": 100})

    # Index budgets by category for fast lookup.
    budget_by_cat: Dict[str, float] = {}
    for b in budgets:
        cat = b.get("content", {}).get("category")
        amt = b.get("content", {}).get("amount")
        if cat and amt is not None:
            budget_by_cat[cat] = float(amt)

    # Aggregate transactions by category. Use the LLM-assigned category
    # (`our_category`) — Plaid's `personal_finance_category` is not stored
    # per Phase 3 decision §Categorization.
    by_category: Dict[str, Dict[str, Any]] = {}
    transaction_ids: List[str] = []
    total_spent = 0.0

    for t in transactions:
        bead_id = t.get("id")
        if bead_id:
            transaction_ids.append(bead_id)
        content = t.get("content", {})
        cat = content.get("our_category") or "unreconciled"
        amt = float(content.get("amount", 0) or 0)
        # Plaid returns positive amounts for outflows on most account types;
        # match that convention without re-signing here.
        bucket = by_category.setdefault(
            cat, {"spent": 0.0, "budget": budget_by_cat.get(cat, 0.0), "tx_count": 0}
        )
        bucket["spent"] += amt
        bucket["tx_count"] += 1
        total_spent += amt

    # Fill in remaining + ensure every active budget appears even with zero spend.
    for cat, budgeted in budget_by_cat.items():
        bucket = by_category.setdefault(
            cat, {"spent": 0.0, "budget": budgeted, "tx_count": 0}
        )
        bucket["remaining"] = round(bucket["budget"] - bucket["spent"], 2)
        bucket["spent"] = round(bucket["spent"], 2)

    for cat, bucket in by_category.items():
        # Categories with no active budget still need a remaining field
        # (negative = unbudgeted spend).
        if "remaining" not in bucket:
            bucket["remaining"] = round(bucket["budget"] - bucket["spent"], 2)
            bucket["spent"] = round(bucket["spent"], 2)

    total_budget = round(sum(budget_by_cat.values()), 2)
    totals = {
        "spent": round(total_spent, 2),
        "budget": total_budget,
        "remaining": round(total_budget - total_spent, 2),
    }

    return {
        "window": {"since": since.date().isoformat(), "until": until.date().isoformat()},
        "by_category": by_category,
        "totals": totals,
        "transaction_ids": transaction_ids,
    }


@activity.defn
async def synthesize_insight_activity(summary: Dict[str, Any]) -> str:
    """Call LiteLLM with a 'Truline CFO' persona over the aggregated summary.

    Returns the narrative string. Model is configurable via INSIGHT_MODEL
    env var; default is LIFEOPS_DEFAULT_MODEL (claude-haiku).
    """
    import json
    import time

    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    model = os.environ.get("INSIGHT_MODEL", LIFEOPS_DEFAULT_MODEL)

    # Trim the summary for the prompt — drop transaction_ids (not useful
    # for the LLM, kept for the provenance bead).
    llm_view = {
        "window": summary["window"],
        "by_category": summary["by_category"],
        "totals": summary["totals"],
    }

    prompt = f"""You are the Truline CFO — a sharp, friendly financial advisor for the Best family.
You review the past week's spending and produce ONE short briefing (5 sentences max).

Your tone: direct, numeric, no fluff. Imagine a CFO updating the household principal in a 1:1.

DATA (last 7 days, USD):
{json.dumps(llm_view, indent=2)}

YOUR BRIEFING MUST CONTAIN:
1. Anomalies — any category where spend is >150% of pace (a monthly budget should pace at ~25%/week; flag categories materially over that), or any unusually large single-week swing.
2. Wins — categories meaningfully under pace.
3. ONE actionable savings tip — concrete, specific to the data above (not generic advice like "make coffee at home" unless dining is the standout).

OUTPUT FORMAT: plain prose, no markdown headers, no bullet lists. 5 sentences or fewer.
"""

    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"} if LITELLM_API_KEY else {}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    started = time.monotonic()
    result = await chat_completion(payload, headers=headers, timeout=60.0)
    body = result.body
    latency_ms = (time.monotonic() - started) * 1000.0

    # Cost capture — context carries only routing tags (window dates, category
    # count). by_category / totals stay out: redact_context would strip them
    # anyway, but the contract is "callers pass tags, not data".
    await log_llm_cost(
        model=model,
        usage=body.get("usage"),
        agent="financial-insights/synthesize",
        context={
            "window_since": summary["window"]["since"],
            "window_until": summary["window"]["until"],
            "category_count": len(summary.get("by_category", {})),
        },
        workflow_id=_current_workflow_id(),
        latency_ms=latency_ms,
        prompt_hash_value=prompt_hash(prompt),
        cost_usd=result.cost_usd,
    )

    return body["choices"][0]["message"]["content"].strip()


@activity.defn
async def persist_insight_activity(
    narrative: str, summary: Dict[str, Any]
) -> Optional[str]:
    """Write the narrative + summary + provenance as a finance.insight bead.

    Returns the new bead id.
    """
    model = os.environ.get("INSIGHT_MODEL", LIFEOPS_DEFAULT_MODEL)
    payload = _build_insight_payload(narrative, summary, model)

    bead = await create_bead(
        payload["type"],
        payload["content"],
        payload["state"],
        payload["created_by"],
        trust_tier=payload["trust_tier"],
        provenance=payload["provenance"],
    )
    return bead.get("id")


# --- Workflow ---

@workflow.defn
class FinancialInsightsWorkflow:
    """Weekly CFO digest. Scheduled by temporal_worker.ensure_schedules
    to fire every Sunday 23:00 America/Chicago."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        # Phase 4.1: maximum_interval >= 60s lets retries straddle the
        # Gemini free-tier per-minute quota reset. 90s gives headroom for
        # rate-limit windows that drift past the minute boundary.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=90),
            maximum_attempts=3,
        )

        summary = await workflow.execute_activity(
            aggregate_finance_data_activity,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry_policy,
        )

        # Phase 8 Architect: pre-calculate merchant stats for the "Fast-Path" UI.
        # This is non-blocking for the CFO digest but essential for the Console.
        merchant_summary_id: Optional[str] = None
        try:
            merchant_summary_id = await workflow.execute_activity(
                summarize_merchant_insights_activity,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("summarize_merchant_insights_activity failed: %s", exc)

        narrative = await workflow.execute_activity(
            synthesize_insight_activity,
            summary,
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=retry_policy,
        )

        bead_id = await workflow.execute_activity(
            persist_insight_activity,
            args=[narrative, summary],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        return {
            "bead_id": bead_id,
            "merchant_summary_id": merchant_summary_id,
            "transactions_analyzed": len(summary.get("transaction_ids", [])),
            "categories": list(summary.get("by_category", {}).keys()),
        }
