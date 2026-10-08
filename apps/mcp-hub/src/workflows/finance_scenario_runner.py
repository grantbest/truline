"""SDD Phase 3 — Cash Flow Counterfactuals / Scenario Engine (Component 3).

Dry-run a big financial decision before making it: "if I buy a $5k car with a
$600/mo payment, where's my liquid wealth in 12 months vs. if I don't?" We
project a baseline 12-month liquid-wealth trajectory from the household's
recent net cash flow, apply the scenario's upfront + recurring deltas, and
persist a ``finance.projection_scenario`` bead the console renders as a
baseline-vs-scenario chart.

Trigger model (on-demand, synchronous)
--------------------------------------
Unlike the other Phase 3 workflows this one is NOT scheduled — it runs when the
user submits a scenario in the console. The console can't reach Temporal
directly (browser → same-origin nginx → mcp-hub), so a thin mcp-hub endpoint
(``POST /finance/scenario``) starts this workflow **and waits for the result**
(``client.execute_workflow``). The math is cheap, so a synchronous round-trip
gives the UI an immediate projection to render — and the bead is still
persisted so Substrate stays the source of truth and past scenarios are
revisitable. This mirrors the existing ``/finance/connections/{slug}/sync``
seam, which already starts workflows from an endpoint.

Baseline realism
----------------
The brief framed the baseline as "90-day burn rate vs liquid assets". A pure
burn-down ignores income and would show every household going bankrupt, so we
derive a **net monthly cash flow** (inflows − outflows) from the last 90 days
of transactions instead — that captures paychecks too. ``monthly_burn`` (the
existing run-rate) is still surfaced for context.

``projection_scenario`` is a free-form, single-token type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), matching the Phase 1/2 precedent — NOT the
brief's dotted ``projection.scenario``. Persisted in state ``complete`` (a
finished artifact, not a queue item).
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
        calculate_run_rate,
        create_bead,
        get_account_balances,
        query_beads,
    )


# --- Tunables -------------------------------------------------------------

HORIZON_MONTHS = 12
NET_FLOW_WINDOW_DAYS = 90

# Liquid = depository accounts (cash you can actually move). Investment/credit
# balances are excluded from the spendable runway.
_LIQUID_TYPES = ("depository",)

_PROJECTION_TYPE = "projection_scenario"
_TXN_PAGE_SIZE = 1000
_MAX_TXN_PAGES = 100


# --- Pure helpers (deterministic; safe to unit test directly) -------------

def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def month_labels(start: date, months: int) -> List[str]:
    """Return ``months`` ``YYYY-MM`` labels for the months *following* the
    month of ``start`` (the projection horizon)."""
    labels: List[str] = []
    y, m = start.year, start.month
    for _ in range(months):
        m += 1
        if m > 12:
            m = 1
            y += 1
        labels.append(f"{y:04d}-{m:02d}")
    return labels


def liquid_assets(accounts: List[Dict[str, Any]]) -> float:
    """Sum spendable (depository) balances across active account beads."""
    total = 0.0
    for bead in accounts:
        if bead.get("state") == "removed":
            continue
        content = bead.get("content") or {}
        if content.get("type") not in _LIQUID_TYPES:
            continue
        total += _f(content.get("current_balance")) or 0.0
    return round(total, 2)


def net_monthly_flow(
    transactions: List[Dict[str, Any]],
    *,
    today: date,
    window_days: int = NET_FLOW_WINDOW_DAYS,
) -> float:
    """Average monthly net cash flow (inflows − outflows) over the trailing
    window. Plaid convention: outflows positive, inflows negative — so net flow
    is ``-sum(amount) / months``. Transfers and removed beads are excluded."""
    window_start = today - timedelta(days=window_days)
    signed_total = 0.0
    for tx in transactions:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("is_transfer"):
            continue
        when = _parse_date(content.get("posted_date") or content.get("date"))
        if when is None or not (window_start <= when < today):
            continue
        amt = _f(content.get("amount"))
        if amt is None:
            continue
        signed_total += amt
    months = window_days / 30.0
    # signed_total is net *spend* (outflow-positive); flip sign for net flow.
    return round(-signed_total / months, 2)


def project_wealth(
    starting_liquid: float,
    net_flow: float,
    months: int,
    *,
    upfront: float = 0.0,
    monthly_delta: float = 0.0,
) -> List[float]:
    """Month-by-month liquid-wealth series. ``upfront`` is applied once at t0;
    ``net_flow + monthly_delta`` accrues each month."""
    series: List[float] = []
    balance = starting_liquid + upfront
    step = net_flow + monthly_delta
    for _ in range(months):
        balance += step
        series.append(round(balance, 2))
    return series


def build_projection(
    *,
    scenario_name: str,
    starting_liquid: float,
    net_flow: float,
    monthly_burn: float,
    monthly_impact: float,
    upfront_impact: float,
    today: date,
    months: int = HORIZON_MONTHS,
) -> Dict[str, Any]:
    """Assemble the full projection_scenario content payload (baseline vs
    scenario series + summary). Pure — the workflow persists what this returns."""
    labels = month_labels(today, months)
    baseline = project_wealth(starting_liquid, net_flow, months)
    scenario = project_wealth(
        starting_liquid,
        net_flow,
        months,
        upfront=upfront_impact,
        monthly_delta=monthly_impact,
    )
    ending_baseline = baseline[-1] if baseline else starting_liquid
    ending_scenario = scenario[-1] if scenario else starting_liquid
    return {
        "name": scenario_name,
        "horizon_months": months,
        "starting_liquid": round(starting_liquid, 2),
        "net_monthly_flow": round(net_flow, 2),
        "monthly_burn": round(monthly_burn, 2),
        "monthly_impact": round(monthly_impact, 2),
        "upfront_impact": round(upfront_impact, 2),
        "months": labels,
        "baseline_wealth": baseline,
        "scenario_wealth": scenario,
        "ending_baseline": round(ending_baseline, 2),
        "ending_scenario": round(ending_scenario, 2),
        "delta_ending": round(ending_scenario - ending_baseline, 2),
    }


# --- Activities -----------------------------------------------------------

async def _fetch_all_transactions() -> List[Dict[str, Any]]:
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
async def run_scenario_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compute baseline + scenario projections, persist the
    ``finance.projection_scenario`` bead, and return the projection content
    (with ``bead_id``). All heavy I/O + date math stays here so the workflow
    body has no nondeterminism."""
    scenario_name = str(payload.get("scenario_name") or "Scenario").strip() or "Scenario"
    monthly_impact = _f(payload.get("monthly_impact")) or 0.0
    upfront_impact = _f(payload.get("upfront_impact")) or 0.0

    accounts = await get_account_balances()
    transactions = await _fetch_all_transactions()
    today = datetime.now(timezone.utc).date()

    starting = liquid_assets(accounts)
    flow = net_monthly_flow(transactions, today=today)
    try:
        run_rate = await calculate_run_rate(months=3)
        burn = float(run_rate.get("monthly_run_rate") or 0.0)
    except Exception:  # noqa: BLE001 — burn is context-only; don't fail the projection
        logger.warning("scenario-runner: run-rate unavailable.", exc_info=True)
        burn = 0.0

    projection = build_projection(
        scenario_name=scenario_name,
        starting_liquid=starting,
        net_flow=flow,
        monthly_burn=burn,
        monthly_impact=monthly_impact,
        upfront_impact=upfront_impact,
        today=today,
    )

    bead = await create_bead(
        _PROJECTION_TYPE,
        {**projection, "computed_at": datetime.now(timezone.utc).isoformat()},
        state="complete",
        created_by="scenario-runner/emit",
    )
    projection["bead_id"] = bead.get("id")
    logger.info(
        "Scenario '%s': liquid=%.2f net_flow=%.2f -> baseline_end=%.2f scenario_end=%.2f",
        scenario_name, starting, flow,
        projection["ending_baseline"], projection["ending_scenario"],
    )
    return projection


# --- Workflow -------------------------------------------------------------

@workflow.defn
class FinanceScenarioRunnerWorkflow:
    """On-demand scenario projection. Started (and awaited) by the mcp-hub
    ``POST /finance/scenario`` endpoint; not scheduled."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=3,
        )
        return await workflow.execute_activity(
            run_scenario_activity,
            payload,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )
