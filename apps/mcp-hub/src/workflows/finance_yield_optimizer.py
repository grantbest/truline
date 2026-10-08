"""SDD Phase 3 — Yield vs APR Optimizer (Component 1).

Maximize capital efficiency by flagging idle cash that should be deployed
against high-interest debt. Every dollar sitting in a zero-yield checking
account while a credit card carries a 22% APR balance is a guaranteed,
risk-free loss the household can stop.

Scope note (asset-yield is stubbed, deliberately)
--------------------------------------------------
The brief framed this as "compare liability APRs against *asset yields*". We
build only the half the data supports today: the **cash-drag → paydown** case.
Plaid's ``/liabilities/get`` gives us a real ``apr`` on credit/loan account
beads (see ``bank_sync._liability_from_*``), but it reports **no APY** for
deposit/savings accounts, so there is no asset-yield signal to compare against.
Suggesting "move cash to a higher-yield savings account" would require a data
source we don't have — same situation as the Phase 2 card-expiry stub. When an
``apy`` field lands on ``finance.account`` content, add an asset-rotation
detector here; until then a zero-yield checking buffer is the only asset side
we can reason about honestly.

What it does each run (weekly cron):
  1. ``fetch_optimizer_inputs_activity`` — pull ``finance.account`` beads and
     the household monthly burn (run-rate). Heavy I/O stays in the activity.
  2. (pure) :func:`analyze_allocation` — find zero-yield checking cash above a
     one-month-burn buffer, then greedily allocate the excess against the
     highest-APR liabilities.
  3. ``emit_allocation_insights_activity`` — upsert one
     ``finance.insight_allocation`` bead (state=pending) per paydown
     suggestion, keyed on a stable ``fingerprint`` so weekly re-runs refresh
     rather than pile up.

``insight_allocation`` is a free-form, single-token type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), matching the Phase 1/2 precedent
(``audit_discrepancy`` / ``review_anomaly``) — NOT the brief's dotted
``insight.allocation``. Substrate stores ``type`` as a flat string and the
console queries it flat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import (
        calculate_run_rate,
        create_bead,
        get_account_balances,
        patch_bead,
        query_beads,
    )


# --- Tunables -------------------------------------------------------------

# Account types Plaid classifies as liabilities (carry an APR we can pay down).
# Aligned with the console's WealthDashboard LIABILITY_ACCOUNT_TYPES.
_LIABILITY_TYPES = ("credit", "loan")

# Deposit subtypes we treat as zero-yield "idle cash". Savings/money-market are
# excluded — they may earn interest, and we have no APY to prove they're idle.
_ZERO_YIELD_SUBTYPES = ("checking",)

# Keep the household's monthly burn liquid before deploying anything. The brief
# specifies a 1-month buffer.
BUFFER_MONTHS = 1.0

# Don't generate noise: only suggest a paydown when the deployable excess clears
# this floor and the move saves at least this much interest per year.
MIN_EXCESS_CASH = 500.0
MIN_ANNUAL_SAVINGS = 25.0

_INSIGHT_TYPE = "insight_allocation"


# --- Pure helpers (deterministic; safe to unit test directly) -------------

def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_balance(content: Dict[str, Any]) -> float:
    return _f(content.get("current_balance")) or 0.0


def _is_liability(content: Dict[str, Any]) -> bool:
    return content.get("type") in _LIABILITY_TYPES or bool(content.get("liabilities"))


def _is_zero_yield_cash(content: Dict[str, Any]) -> bool:
    if _is_liability(content):
        return False
    if content.get("type") != "depository":
        return False
    subtype = str(content.get("subtype") or "").lower()
    return subtype in _ZERO_YIELD_SUBTYPES


def analyze_allocation(
    accounts: List[Dict[str, Any]],
    *,
    monthly_burn: float,
    buffer_months: float = BUFFER_MONTHS,
    min_excess: float = MIN_EXCESS_CASH,
    min_annual_savings: float = MIN_ANNUAL_SAVINGS,
) -> List[Dict[str, Any]]:
    """Return paydown suggestions: deploy idle checking cash against the
    highest-APR liabilities.

    Pure and deterministic — no clock, no I/O. ``accounts`` are
    ``finance.account`` beads. The deployable pool is (total zero-yield
    checking balance − ``buffer_months`` × ``monthly_burn``); it is sourced
    (for display) from the single largest checking account and allocated
    greedily across liabilities by descending APR. Each liability that receives
    a paydown becomes one suggestion. Returns ``[]`` when there's nothing worth
    surfacing.
    """
    cash_accounts: List[Dict[str, Any]] = []
    liabilities: List[Dict[str, Any]] = []
    for bead in accounts:
        if bead.get("state") == "removed":
            continue
        content = bead.get("content") or {}
        if _is_zero_yield_cash(content):
            cash_accounts.append(bead)
        elif _is_liability(content):
            liabilities.append(bead)

    total_cash = sum(_account_balance(b.get("content") or {}) for b in cash_accounts)
    buffer = max(0.0, buffer_months * max(0.0, monthly_burn))
    deployable = round(total_cash - buffer, 2)
    if deployable < min_excess:
        return []

    # Source account (for display): the largest single checking balance.
    source = max(
        cash_accounts,
        key=lambda b: _account_balance(b.get("content") or {}),
        default=None,
    )
    if source is None:
        return []
    source_content = source.get("content") or {}

    # Rank liabilities by APR desc; only those with a known APR and a balance.
    payable: List[Dict[str, Any]] = []
    for bead in liabilities:
        content = bead.get("content") or {}
        apr = _f((content.get("liabilities") or {}).get("apr"))
        balance = _account_balance(content)
        if apr is None or apr <= 0 or balance <= 0:
            continue
        payable.append({"bead": bead, "apr": apr, "balance": balance})
    payable.sort(key=lambda x: x["apr"], reverse=True)

    suggestions: List[Dict[str, Any]] = []
    remaining = deployable
    for item in payable:
        if remaining < min_excess:
            break
        content = item["bead"].get("content") or {}
        amount = round(min(remaining, item["balance"]), 2)
        annual_savings = round(amount * item["apr"] / 100.0, 2)
        if annual_savings < min_annual_savings:
            continue
        target_id = item["bead"].get("id")
        suggestions.append(
            {
                "action": "paydown",
                "source_account_id": source.get("id"),
                "source_account_name": source_content.get("name"),
                "target_account_id": target_id,
                "target_account_name": content.get("name"),
                "target_apr": round(item["apr"], 2),
                "amount": amount,
                "projected_annual_savings": annual_savings,
                "idle_cash": deployable,
                "buffer_retained": round(buffer, 2),
                "fingerprint": f"paydown:{source.get('id')}:{target_id}",
            }
        )
        remaining = round(remaining - amount, 2)

    return suggestions


# --- Activities -----------------------------------------------------------

@activity.defn
async def fetch_optimizer_inputs_activity() -> List[Dict[str, Any]]:
    """Fetch account beads + household monthly burn, run the pure allocator,
    and return the (small) suggestion list. Heavy data stays in the activity."""
    accounts = await get_account_balances()
    try:
        run_rate = await calculate_run_rate(months=3)
        monthly_burn = float(run_rate.get("monthly_run_rate") or 0.0)
    except Exception:  # noqa: BLE001 — a missing run-rate shouldn't kill the optimizer
        logger.warning("yield-optimizer: run-rate unavailable; using zero buffer.", exc_info=True)
        monthly_burn = 0.0

    suggestions = analyze_allocation(accounts, monthly_burn=monthly_burn)
    logger.info(
        "Yield optimizer: %d accounts, monthly_burn=%.2f -> %d paydown suggestion(s).",
        len(accounts), monthly_burn, len(suggestions),
    )
    return suggestions


@activity.defn
async def emit_allocation_insights_activity(suggestions: List[Dict[str, Any]]) -> int:
    """Upsert one ``finance.insight_allocation`` bead (state=pending) per
    suggestion, keyed on ``content.fingerprint`` so weekly re-runs refresh in
    place rather than accrete duplicates. Returns the count written/refreshed."""
    if not suggestions:
        return 0

    existing = await query_beads({"type": _INSIGHT_TYPE, "state": "pending", "limit": 1000})
    pending_by_fp: Dict[str, Dict[str, Any]] = {}
    for b in existing:
        fp = (b.get("content") or {}).get("fingerprint")
        if fp and fp not in pending_by_fp:
            pending_by_fp[fp] = b

    written = 0
    for suggestion in suggestions:
        content = {**suggestion, "detected_at": datetime.now(timezone.utc).isoformat()}
        match = pending_by_fp.get(suggestion.get("fingerprint"))
        if match is not None:
            await patch_bead(match["id"], content=content, created_by="yield-optimizer/refresh")
        else:
            await create_bead(
                _INSIGHT_TYPE, content, state="pending", created_by="yield-optimizer/emit"
            )
        written += 1

    logger.info("Emitted/refreshed %d insight_allocation beads.", written)
    return written


# --- Workflow -------------------------------------------------------------

@workflow.defn
class FinanceYieldOptimizerWorkflow:
    """Weekly capital-efficiency scan. Scheduled by
    ``temporal_worker.ensure_schedules`` for Monday 06:00 America/Chicago."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        suggestions = await workflow.execute_activity(
            fetch_optimizer_inputs_activity,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        emitted = await workflow.execute_activity(
            emit_allocation_insights_activity,
            suggestions,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        return {
            "suggestions": len(suggestions),
            "beads_emitted": emitted,
            "total_annual_savings": round(
                sum(s.get("projected_annual_savings") or 0.0 for s in suggestions), 2
            ),
        }
