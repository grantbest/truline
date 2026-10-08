import html
import json
import logging
import os
from typing import Optional, List, Dict, Any, Literal
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from tools.finance import register_plaid_item
from tools.infisical import (
    write_configured as infisical_write_configured,
    write_secret as infisical_write_secret,
)
from tools.connections import get_connections_status
from tools.schedules import (
    get_bank_sync_schedules,
    reconcile_schedules,
    run_scenario,
    trigger_bank_sync,
)
from tools.rules import commit_rule
from tools.plaid import create_link_token, exchange_public_token
from tools.finance import (
    log_expense,
    get_expenses,
    log_bill,
    mark_bill_paid,
    get_bills_due,
    set_budget,
    get_budget_status,
    get_category_history,
    get_recent_transactions,
    get_transactions,
    get_account_balances,
    get_unreconciled_transactions,
    list_subscriptions,
    get_bill_ledger,
    calculate_run_rate,
)
from workflows.finance_reconciliation import get_ledger_variance
from routers.v1.types import FinanceCategory, MonthSelector
from access_auth import check_scope

logger = logging.getLogger(__name__)

# Plaid sunset the Development environment in 2024 — sandbox/production only.
_PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


def _plaid_host() -> str:
    return _PLAID_HOSTS.get(os.environ.get("PLAID_ENV", "sandbox"), _PLAID_HOSTS["sandbox"])


def _plaid_secret() -> Optional[str]:
    env = os.environ.get("PLAID_ENV", "sandbox")
    return os.environ.get("PLAID_SANDBOX_SECRET") if env == "sandbox" else os.environ.get("PLAID_SECRET")


def _validate_institution_slug(institution_slug: str) -> None:
    if not institution_slug.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid institution_slug")


router = APIRouter()

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class LogExpenseRequest(BaseModel):
    amount: float = Field(..., gt=0)
    category: FinanceCategory = Field(
        ...,
        description="Pick the closest match. Coffee/restaurants → 'dining'. Supermarket → 'groceries'.",
    )
    description: str
    vendor: Optional[str] = None
    paid_with: Optional[str] = None
    date: Optional[str] = Field(
        None,
        description="ISO date (YYYY-MM-DD). Defaults to today.",
    )


@router.post("/log_expense", summary="Log an expense", operation_id="log_expense")
async def finance_log_expense(req: LogExpenseRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    return await log_expense(
        amount=req.amount,
        category=req.category,
        description=req.description,
        vendor=req.vendor,
        paid_with=req.paid_with,
        date=req.date,
    )


class GetExpensesRequest(BaseModel):
    month: MonthSelector = Field(
        "current",
        description="'current', 'last', or a specific month as 'YYYY-MM'.",
    )
    category: Optional[FinanceCategory] = None
    limit: int = 100


@router.post("/get_expenses", summary="List expenses for a month", operation_id="get_expenses")
async def finance_get_expenses(req: GetExpensesRequest) -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_expenses(month=req.month, category=req.category, limit=req.limit)


class LogBillRequest(BaseModel):
    amount: float = Field(..., gt=0)
    vendor: str
    due_date: str = Field(..., description="ISO date (YYYY-MM-DD)")
    category: FinanceCategory = "utilities"
    recurring: bool = False
    frequency: Optional[Literal["monthly", "quarterly", "annual"]] = None


@router.post("/log_bill", summary="Log a pending bill", operation_id="log_bill")
async def finance_log_bill(req: LogBillRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    return await log_bill(
        amount=req.amount,
        vendor=req.vendor,
        due_date=req.due_date,
        category=req.category,
        recurring=req.recurring,
        frequency=req.frequency,
    )


class MarkBillPaidRequest(BaseModel):
    bead_id: str = Field(..., description="UUID returned when the bill was logged.")
    paid_date: Optional[str] = Field(
        None,
        description="ISO date (YYYY-MM-DD). Defaults to today.",
    )


@router.post("/mark_bill_paid", summary="Transition a bill from pending to paid", operation_id="mark_bill_paid")
async def finance_mark_bill_paid(req: MarkBillPaidRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    return await mark_bill_paid(bead_id=req.bead_id, paid_date=req.paid_date)


class GetBillsDueRequest(BaseModel):
    days_ahead: int = Field(14, ge=1, le=365)
    only_pending: bool = True


@router.post("/get_bills_due", summary="Bills due in the next N days", operation_id="get_bills_due")
async def finance_get_bills_due(req: GetBillsDueRequest) -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_bills_due(days_ahead=req.days_ahead, only_pending=req.only_pending)


class SetBudgetRequest(BaseModel):
    category: FinanceCategory
    amount: float = Field(..., gt=0)
    period: Literal["monthly", "quarterly", "annual"] = "monthly"


@router.post("/set_budget", summary="Set or replace the active budget for a category", operation_id="set_budget")
async def finance_set_budget(req: SetBudgetRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    return await set_budget(category=req.category, amount=req.amount, period=req.period)


class BudgetStatusRequest(BaseModel):
    month: MonthSelector = Field(
        "current",
        description="'current', 'last', or a specific month as 'YYYY-MM'.",
    )


@router.post("/budget_status", summary="Per-category budget status", operation_id="get_budget_status")
async def finance_budget_status(req: BudgetStatusRequest) -> Dict[str, Any]:
    check_scope("finance.read")
    return await get_budget_status(month=req.month)


class GetTransactionsRequest(BaseModel):
    month: MonthSelector = Field("current", description="'current', 'last', or 'YYYY-MM'")
    category: Optional[FinanceCategory] = None
    institution: Optional[Literal["chase"]] = None
    limit: int = Field(100, ge=1, le=500)


@router.post("/get_transactions", summary="Real bank transactions imported via Plaid", operation_id="get_transactions")
async def finance_get_transactions(req: GetTransactionsRequest) -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_transactions(
        month=req.month,
        category=req.category,
        institution=req.institution,
        limit=req.limit
    )


@router.get("/transactions/recent", summary="Recent bank transactions ordered by posted date", operation_id="get_recent_transactions")
async def finance_recent_transactions(
    category: Optional[FinanceCategory] = None,
    institution: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=2000),
    include_transfers: bool = True,
) -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_recent_transactions(
        category=category,
        institution=institution,
        limit=limit,
        include_transfers=include_transfers,
    )


@router.post("/get_account_balances", summary="Current balances for all connected accounts", operation_id="get_account_balances")
async def finance_get_account_balances() -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_account_balances()


class GetUnreconciledRequest(BaseModel):
    days_back: int = Field(30, ge=1, le=180)


@router.post("/get_unreconciled_transactions", summary="Imported transactions that have no LLM category (need manual fix)", operation_id="get_unreconciled_transactions")
async def finance_get_unreconciled(req: GetUnreconciledRequest) -> List[Dict[str, Any]]:
    check_scope("finance.read")
    return await get_unreconciled_transactions(days_back=req.days_back)


@router.get("/subscriptions", summary="Detected recurring subscriptions: monthly-equivalent cost, price hikes, and stale/cancel candidates", operation_id="list_subscriptions")
async def finance_subscriptions(include_stale: bool = True) -> Dict[str, Any]:
    check_scope("finance.read")
    return await list_subscriptions(include_stale=include_stale)


@router.get("/bills", summary="Bill guardian: bills grouped into overdue / due-soon / upcoming / paid, with proof-of-payment", operation_id="get_bill_ledger")
async def finance_bill_ledger(days_ahead: int = 14) -> Dict[str, Any]:
    check_scope("finance.read")
    return await get_bill_ledger(days_ahead=days_ahead)


@router.get("/run_rate", summary="Average monthly burn over recent complete months, split Fixed (recurring) vs Variable", operation_id="calculate_run_rate")
async def finance_run_rate(months: int = 3) -> Dict[str, Any]:
    check_scope("finance.read")
    return await calculate_run_rate(months=months)


@router.get("/ledger_variance", summary="Cumulative unexplained ledger variance per account and in total", operation_id="get_ledger_variance")
async def finance_ledger_variance() -> Dict[str, Any]:
    check_scope("finance.read")
    try:
        return await get_ledger_variance()
    except Exception as e:
        logger.exception("ledger variance read failed")
        raise HTTPException(status_code=502, detail=f"Ledger variance read error: {e}")


@router.get("/category_history", summary="Per-category monthly spend series + stats for budget recommendation", operation_id="get_category_history")
async def finance_category_history(months: int = 6) -> Dict[str, Any]:
    check_scope("finance.read")
    return await get_category_history(months=months)


class ScenarioRequest(BaseModel):
    scenario_name: str = Field(..., min_length=1, max_length=120, description="Label for this what-if, e.g. 'Buy New Car'.")
    monthly_impact: float = Field(
        0.0,
        description="Recurring monthly cash-flow change. Negative for a new cost (e.g. -600 car payment), positive for new income.",
    )
    upfront_impact: float = Field(
        0.0,
        description="One-time cash impact applied at month 0. Negative for an outlay (e.g. -5000 down payment).",
    )


@router.post(
    "/scenario",
    summary="Run a 12-month cash-flow what-if (baseline vs scenario) and persist the projection",
    operation_id="run_scenario",
)
async def finance_scenario(req: ScenarioRequest) -> Dict[str, Any]:
    check_scope("finance.read")
    try:
        return await run_scenario(req.model_dump())
    except Exception as e:
        logger.exception("scenario run failed for %r", req.scenario_name)
        raise HTTPException(status_code=502, detail=f"Temporal error: {e}")


class RuleCommitRequest(BaseModel):
    field: str = Field(..., min_length=1, max_length=64, description="Transaction content field to match (e.g. 'normalized_merchant'; 'vendor' aliases to it).")
    operator: Literal["contains", "equals", "starts_with", "regex"] = Field(..., description="Match operator.")
    value: str = Field(..., min_length=1, max_length=256, description="Value/pattern to match against the field (case-insensitive).")
    target_category: str = Field(..., min_length=1, max_length=64, description="Category to assign to matching, currently-Uncategorized transactions.")


@router.post(
    "/rules/commit",
    summary="Persist an auto-categorization rule and start its background retroaction",
    operation_id="commit_rule",
)
async def finance_rule_commit(req: RuleCommitRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    try:
        return await commit_rule(req.model_dump())
    except Exception as e:
        logger.exception("rule commit failed for %r", req.model_dump())
        raise HTTPException(status_code=502, detail=f"Temporal error: {e}")


@router.get("/connections", summary="Per-institution Plaid connection health, last sync, and account counts", operation_id="get_connections_status")
async def finance_connections(probe: bool = True) -> Dict[str, Any]:
    check_scope("finance.read")
    return await get_connections_status(probe=probe)


@router.post("/connections/{institution_slug}/sync", summary="Run an institution's bank sync now", operation_id="trigger_bank_sync")
async def finance_sync_now(institution_slug: str) -> Dict[str, Any]:
    check_scope("finance.write")
    _validate_institution_slug(institution_slug)
    try:
        return await trigger_bank_sync(institution_slug)
    except Exception as e:
        logger.exception("sync-now failed for %s", institution_slug)
        raise HTTPException(status_code=502, detail=f"Temporal error: {e}")


@router.get("/schedules", summary="Bank-sync schedule state per linked institution", operation_id="get_bank_sync_schedules")
async def finance_schedules() -> Dict[str, Any]:
    check_scope("finance.read")
    try:
        return await get_bank_sync_schedules()
    except Exception as e:
        logger.exception("schedule describe failed")
        raise HTTPException(status_code=502, detail=f"Temporal error: {e}")


@router.post("/schedules/reconcile", summary="Re-register Temporal schedules without a worker restart", operation_id="reconcile_schedules")
async def finance_schedules_reconcile() -> Dict[str, Any]:
    check_scope("finance.write")
    try:
        return await reconcile_schedules()
    except Exception as e:
        logger.exception("schedule reconcile failed")
        raise HTTPException(status_code=502, detail=f"Temporal error: {e}")


# ---------------------------------------------------------------------------
# Plaid Link & Webhook logic
# ---------------------------------------------------------------------------

_LINK_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{action_word} {institution} — Truline</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            max-width: 640px; margin: 4rem auto; padding: 0 1.5rem; color: #1a1a1a; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
    .env {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
            font-size: 0.75rem; background: #fef3c7; color: #92400e;
            text-transform: uppercase; letter-spacing: 0.05em; }}
    .env.production {{ background: #fee2e2; color: #991b1b; }}
    button {{ background: #2563eb; color: white; border: none; padding: 0.75rem 1.5rem;
              font-size: 1rem; border-radius: 6px; cursor: pointer; margin-top: 1.5rem; }}
    button:hover {{ background: #1d4ed8; }}
    .result {{ margin-top: 2rem; padding: 1.25rem; background: #f0fdf4;
               border: 1px solid #86efac; border-radius: 6px; display: none; }}
    .result.error {{ background: #fef2f2; border-color: #fca5a5; }}
    .token {{ display: block; padding: 0.75rem; background: #fff; border: 1px solid #d1d5db;
              border-radius: 4px; font-family: ui-monospace, SFMono-Regular, monospace;
              word-break: break-all; margin: 0.5rem 0; user-select: all; }}
    code {{ background: #f3f4f6; padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }}
    .instructions {{ margin-top: 0.75rem; font-weight: 600; color: #166534; }}
  </style>
</head>
<body>
  <h1>{action_word} {institution}</h1>
  <p>Plaid environment: <span class="env {env}">{env}</span></p>
  <button id="link-button">Launch Plaid Link</button>
  <div id="result" class="result">
    <div id="result-body"></div>
  </div>

  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    (function() {{
      var LINK_TOKEN = {link_token_json};
      var INSTITUTION = {institution_json};
      var UPDATE_MODE = {update_mode_json};
      var ENV_KEY = "PLAID_ACCESS_TOKEN_" + INSTITUTION.toUpperCase();

      var resultEl = document.getElementById("result");
      var bodyEl = document.getElementById("result-body");

      function showSuccess(accessToken, itemId) {{
        resultEl.classList.remove("error");
        resultEl.style.display = "block";
        bodyEl.innerHTML =
          '<p class="instructions">Copy this Access Token and save it in Infisical:</p>' +
          '<p>Key: <code>' + ENV_KEY + '</code></p>' +
          '<span class="token">' + accessToken + '</span>' +
          '<p style="font-size: 0.875rem; color: #4b5563;">Item ID: <code>' + itemId + '</code></p>';
      }}

      function showSavedSuccess(itemId) {{
        resultEl.classList.remove("error");
        resultEl.style.display = "block";
        bodyEl.innerHTML =
          '<p class="instructions">Connected — token saved to Infisical automatically.</p>' +
          '<p>Key: <code>' + ENV_KEY + '</code>. Secrets sync to the cluster and pods ' +
          'restart on their own; the first sync starts within a couple of minutes.</p>' +
          '<p style="font-size: 0.875rem; color: #4b5563;">Item ID: <code>' + itemId + '</code></p>';
      }}

      function showError(msg) {{
        resultEl.classList.add("error");
        resultEl.style.display = "block";
        bodyEl.textContent = "Error: " + msg;
      }}

      function showUpdateSuccess() {{
        resultEl.classList.remove("error");
        resultEl.style.display = "block";
        bodyEl.innerHTML =
          '<p class="instructions">Re-authentication complete.</p>' +
          '<p>The existing access token (<code>' + ENV_KEY + '</code>) remains valid — ' +
          'no Infisical changes or pod restarts needed. Sync resumes on the next run.</p>';
      }}

      var handler = Plaid.create({{
        token: LINK_TOKEN,
        onSuccess: function(public_token, metadata) {{
          if (UPDATE_MODE) {{
            showUpdateSuccess();
            return;
          }}
          fetch("/api/v1/finance/exchange", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              public_token: public_token,
              institution_slug: INSTITUTION
            }})
          }}).then(function(r) {{
            return r.json().then(function(j) {{ return {{ ok: r.ok, body: j }}; }});
          }}).then(function(r) {{
            if (r.ok && r.body.token_saved) {{
              showSavedSuccess(r.body.item_id || "");
            }} else if (r.ok) {{
              showSuccess(r.body.access_token, r.body.item_id || "");
            }} else {{
              showError(r.body.detail || "exchange failed");
            }}
          }}).catch(function(e) {{ showError(e.message); }});
        }},
        onExit: function(err, metadata) {{
          if (err) {{ showError(err.display_message || err.error_message || "Plaid Link exited"); }}
        }}
      }});

      document.getElementById("link-button").addEventListener("click", function() {{
        handler.open();
      }});
    }})();
  </script>
</body>
</html>
"""


@router.get("/link/{institution_slug}", response_class=HTMLResponse, include_in_schema=False)
async def finance_link_portal(institution_slug: str, mode: Optional[str] = None) -> HTMLResponse:
    check_scope("finance.read")
    _validate_institution_slug(institution_slug)
    if mode is not None and mode != "update":
        raise HTTPException(status_code=400, detail="mode must be 'update' or omitted")

    update_mode = mode == "update"
    access_token: Optional[str] = None
    if update_mode:
        token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
        access_token = os.environ.get(token_env)
        if not access_token:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No stored access token ({token_env}) for '{institution_slug}' — "
                    f"link it first at /finance/link/{institution_slug}"
                ),
            )

    try:
        link_token = await create_link_token(
            client_user_id=f"truline-{institution_slug}",
            access_token=access_token,
        )
    except Exception as e:
        logger.exception(
            "plaid link_token create failed for %s (update_mode=%s)",
            institution_slug, update_mode,
        )
        raise HTTPException(status_code=502, detail=f"Plaid link_token error: {e}")

    env = os.environ.get("PLAID_ENV", "sandbox")
    page = _LINK_PAGE.format(
        institution=html.escape(institution_slug),
        action_word="Reconnect" if update_mode else "Connect",
        env=html.escape(env),
        link_token_json=json.dumps(link_token),
        institution_json=json.dumps(institution_slug),
        update_mode_json=json.dumps(update_mode),
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


class FinanceExchangeRequest(BaseModel):
    public_token: str = Field(..., min_length=1)
    institution_slug: Optional[str] = None


@router.post("/exchange", include_in_schema=False)
async def finance_exchange(req: FinanceExchangeRequest) -> Dict[str, Any]:
    check_scope("finance.write")
    try:
        exchange = await exchange_public_token(req.public_token)
    except Exception as e:
        logger.exception("plaid public_token exchange failed")
        raise HTTPException(status_code=502, detail=f"Plaid exchange error: {e}")

    access_token = exchange["access_token"]
    item_id = exchange.get("item_id")

    env = os.environ.get("PLAID_ENV", "sandbox")
    infisical_key = (
        f"PLAID_ACCESS_TOKEN_{req.institution_slug.upper()}"
        if req.institution_slug else None
    )

    item_registered = False
    if req.institution_slug and item_id:
        try:
            await register_plaid_item(req.institution_slug, item_id, env)
            item_registered = True
        except Exception:
            logger.exception(
                "plaid_item registry write failed for %s (item_id=%s)",
                req.institution_slug, item_id,
            )

    token_saved = False
    if infisical_key and infisical_write_configured():
        try:
            await infisical_write_secret(
                infisical_key,
                access_token,
                comment=f"Plaid {env} token for {req.institution_slug}; "
                        f"written by mcp-hub /finance/exchange (item {item_id})",
            )
            token_saved = True
        except Exception:
            logger.exception(
                "infisical token write failed for %s — falling back to manual flow",
                infisical_key,
            )

    logger.info(
        "plaid token exchanged: env=%s institution=%s item_id=%s registered=%s saved=%s",
        env, req.institution_slug or "<unspecified>", item_id, item_registered,
        token_saved,
    )
    response: Dict[str, Any] = {
        "item_id": item_id,
        "plaid_env": env,
        "infisical_key": infisical_key,
        "item_registered": item_registered,
        "token_saved": token_saved,
    }
    if not token_saved:
        response["access_token"] = access_token
    return response
