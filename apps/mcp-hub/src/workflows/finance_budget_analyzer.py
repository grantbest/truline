"""SDD Phase 3 — Intelligent Budget Auto-Adjuster (Component 2).

Stop "broken window" budgets: a cap nobody hits anymore stops meaning
anything. Once a month we compare each category's 90-day rolling average spend
to its active budget cap, and when reality has drifted >15% from the cap (over
*or* under) we surface a one-click recommendation to re-base it.

What it does each run (monthly cron, 1st of the month):
  1. ``fetch_budget_analysis_inputs_activity`` — pull active ``finance.budget``
     beads + the last 90 days of spend (``finance.transaction`` +
     ``finance.expense``). Heavy I/O stays in the activity.
  2. (pure) :func:`compute_recommendations` — per category, average the last
     90 days into a monthly-equivalent figure and compare to the cap.
  3. ``emit_budget_recommendations_activity`` — upsert one
     ``finance.budget_recommendation`` bead (state=pending) per drifting
     category, keyed on a stable ``fingerprint`` so monthly re-runs refresh.

Only *monthly* budgets are evaluated — the cap is a monthly number and the
90-day average is converted to a monthly equivalent (÷3). A thin-history guard
(``MIN_SAMPLE_TXNS``) keeps a category with one or two stray charges from
re-basing the budget off noise, mirroring the anomaly detector's min-history
rule.

``budget_recommendation`` is a free-form, single-token type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), matching the Phase 1/2 precedent — NOT the
brief's dotted ``budget.recommendation``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import (
        create_bead,
        get_expenses,
        patch_bead,
        query_beads,
    )


# --- Tunables -------------------------------------------------------------

# Rolling window for the average. The brief specifies a 90-day average.
WINDOW_DAYS = 90

# Months the window spans, used to convert the 90-day total to a monthly cap.
WINDOW_MONTHS = WINDOW_DAYS / 30.0

# Re-base only when the monthly average deviates from the cap by more than this
# fraction, in either direction. The brief specifies 15%.
DEVIATION_THRESHOLD = 0.15

# Need at least this many charges in the window to trust the average — a
# category with one stray transaction shouldn't move the budget.
MIN_SAMPLE_TXNS = 3

# Round suggested caps to a tidy figure (nearest $10).
CAP_ROUNDING = 10

_BUDGET_TYPE = "budget"
_RECOMMENDATION_TYPE = "budget_recommendation"
_TXN_PAGE_SIZE = 1000
_MAX_TXN_PAGES = 100


# --- Pure helpers (deterministic; safe to unit test directly) -------------

def _parse_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _round_cap(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(round(value / CAP_ROUNDING) * CAP_ROUNDING) or float(CAP_ROUNDING)


def _spend_by_category(
    transactions: List[Dict[str, Any]],
    expenses: List[Dict[str, Any]],
    *,
    window_start: date,
    window_end: date,
) -> Dict[str, Dict[str, float]]:
    """Sum spend per category inside [window_start, window_end), returning
    ``{category: {"total": float, "count": int}}``.

    Transfers, Plaid-removed beads, and non-positive amounts (refunds/inflows)
    are excluded — only real outflows feed the average.
    """
    totals: Dict[str, Dict[str, float]] = {}

    def add(cat: Optional[str], amount: Any, when: Optional[date]) -> None:
        if not cat or when is None:
            return
        if not (window_start <= when < window_end):
            return
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return
        if amt <= 0:
            return
        bucket = totals.setdefault(cat, {"total": 0.0, "count": 0})
        bucket["total"] += amt
        bucket["count"] += 1

    for tx in transactions:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("is_transfer"):
            continue
        when = _parse_date(content.get("posted_date") or content.get("date"))
        add(content.get("our_category") or content.get("category"), content.get("amount"), when)

    for e in expenses:
        content = e.get("content") or {}
        when = _parse_date(content.get("date"))
        add(content.get("category"), content.get("amount"), when)

    return totals


def compute_recommendations(
    budgets: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    expenses: List[Dict[str, Any]],
    *,
    today: date,
    window_days: int = WINDOW_DAYS,
    threshold: float = DEVIATION_THRESHOLD,
    min_samples: int = MIN_SAMPLE_TXNS,
) -> List[Dict[str, Any]]:
    """Return budget-recommendation dicts for monthly budgets whose 90-day
    monthly-average spend deviates from the cap by more than ``threshold``.

    Pure and deterministic. ``budgets`` are active ``finance.budget`` beads;
    only ``period == "monthly"`` budgets are evaluated. Categories with fewer
    than ``min_samples`` charges in the window are skipped (thin-history guard).
    """
    window_start = today - timedelta(days=window_days)
    window_months = window_days / 30.0
    spend = _spend_by_category(
        transactions, expenses, window_start=window_start, window_end=today
    )

    recs: List[Dict[str, Any]] = []
    for bead in budgets:
        content = bead.get("content") or {}
        if (content.get("period") or "monthly") != "monthly":
            continue
        category = content.get("category")
        cap = content.get("amount")
        if not category or cap is None or cap <= 0:
            continue

        bucket = spend.get(category)
        if not bucket or bucket["count"] < min_samples:
            continue

        monthly_avg = bucket["total"] / window_months
        deviation = (monthly_avg - cap) / cap
        if abs(deviation) <= threshold:
            continue

        suggested = _round_cap(monthly_avg)
        if suggested == cap:
            continue

        recs.append(
            {
                "category": category,
                "budget_id": bead.get("id"),
                "current_cap": round(float(cap), 2),
                "suggested_cap": suggested,
                "monthly_average": round(monthly_avg, 2),
                "deviation_pct": round(deviation * 100, 1),
                "direction": "increase" if deviation > 0 else "decrease",
                "window_days": window_days,
                "sample_size": int(bucket["count"]),
                "reason": f"90-day average is ${monthly_avg:,.0f}/mo vs ${float(cap):,.0f} cap",
                "fingerprint": f"budget_rec:{category}",
            }
        )
    return recs


# --- Activities -----------------------------------------------------------

async def _fetch_all_transactions() -> List[Dict[str, Any]]:
    """Page through every ``finance.transaction`` bead (offset pagination)."""
    results: List[Dict[str, Any]] = []
    for page in range(_MAX_TXN_PAGES):
        chunk = await query_beads(
            {"type": "transaction", "limit": _TXN_PAGE_SIZE, "offset": page * _TXN_PAGE_SIZE}
        )
        results.extend(chunk)
        if len(chunk) < _TXN_PAGE_SIZE:
            break
    return results


@activity.defn
async def fetch_budget_analysis_inputs_activity() -> List[Dict[str, Any]]:
    """Fetch active budgets + 90 days of spend, run the pure analyzer, return
    the (small) recommendation list. Heavy data stays in the activity."""
    budgets = await query_beads({"type": _BUDGET_TYPE, "state": "active", "limit": 200})
    transactions = await _fetch_all_transactions()
    # get_expenses windows by month; pull the last 4 months to cover 90 days.
    expenses: List[Dict[str, Any]] = []
    seen: set = set()
    for month in ("current", "last"):
        for e in await get_expenses(month=month, limit=2000):
            eid = e.get("id")
            if eid not in seen:
                seen.add(eid)
                expenses.append(e)

    today = datetime.now(timezone.utc).date()
    recs = compute_recommendations(budgets, transactions, expenses, today=today)
    logger.info(
        "Budget analyzer: %d budgets, %d txns -> %d recommendation(s).",
        len(budgets), len(transactions), len(recs),
    )
    return recs


@activity.defn
async def emit_budget_recommendations_activity(recs: List[Dict[str, Any]]) -> int:
    """Upsert one ``finance.budget_recommendation`` bead (state=pending) per
    recommendation, keyed on ``content.fingerprint`` (one per category) so
    monthly re-runs refresh in place. Returns the count written/refreshed."""
    if not recs:
        return 0

    existing = await query_beads(
        {"type": _RECOMMENDATION_TYPE, "state": "pending", "limit": 1000}
    )
    pending_by_fp: Dict[str, Dict[str, Any]] = {}
    for b in existing:
        fp = (b.get("content") or {}).get("fingerprint")
        if fp and fp not in pending_by_fp:
            pending_by_fp[fp] = b

    written = 0
    for rec in recs:
        content = {**rec, "detected_at": datetime.now(timezone.utc).isoformat()}
        match = pending_by_fp.get(rec.get("fingerprint"))
        if match is not None:
            await patch_bead(match["id"], content=content, created_by="budget-analyzer/refresh")
        else:
            await create_bead(
                _RECOMMENDATION_TYPE, content, state="pending", created_by="budget-analyzer/emit"
            )
        written += 1

    logger.info("Emitted/refreshed %d budget_recommendation beads.", written)
    return written


# --- Workflow -------------------------------------------------------------

@workflow.defn
class FinanceBudgetAnalyzerWorkflow:
    """Monthly budget re-basing scan. Scheduled by
    ``temporal_worker.ensure_schedules`` for the 1st at 06:30 America/Chicago."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        recs = await workflow.execute_activity(
            fetch_budget_analysis_inputs_activity,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        emitted = await workflow.execute_activity(
            emit_budget_recommendations_activity,
            recs,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        return {
            "recommendations": len(recs),
            "beads_emitted": emitted,
            "increases": sum(1 for r in recs if r.get("direction") == "increase"),
            "decreases": sum(1 for r in recs if r.get("direction") == "decrease"),
        }
