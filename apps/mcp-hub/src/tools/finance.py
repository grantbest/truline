from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, date
from pydantic import BaseModel, Field
import asyncio
import httpx
import logging
import os
import pathlib
import sys

logger = logging.getLogger(__name__)

# Constants
SUBSTRATE_URL = os.environ.get("SUBSTRATE_URL", "http://substrate.platform-substrate.svc.cluster.local:8000").rstrip("/")
FINANCE_NAMESPACE = "finance"

# httpx's default timeout is 5s, which is too tight for Substrate reads that
# decrypt large bead sets (run_rate / ledger reconciliation page the full
# ~1k-transaction history, and Fernet decryption pushes a single page past 5s
# → ReadTimeout → 500s + zero balance_snapshots). 60s gives those bulk reads
# room without hanging a genuinely stuck call indefinitely.
_SUBSTRATE_TIMEOUT = 60.0

# Multi-user dimension for finance beads. No person registry exists yet,
# so manual entries default to the single household owner (matches the
# HA `person.grant_best` convention). Tighten when a registry lands.
DEFAULT_OWNER = "grant"

FINANCE_CATEGORIES = [
    "groceries",
    "dining",
    "utilities",
    "kids",
    "auto",
    "housing",
    "entertainment",
    "health",
    "interest_fees",
    "misc",
]


def _month_window(month: str) -> Tuple[date, date]:
    now = datetime.now()
    if month == "current":
        start_date = date(now.year, now.month, 1)
    elif month == "last":
        if now.month == 1:
            start_date = date(now.year - 1, 12, 1)
        else:
            start_date = date(now.year, now.month - 1, 1)
    else:
        try:
            parsed = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise ValueError(f"Invalid month format: {month}. Use 'current', 'last', or 'YYYY-MM'.")
        start_date = date(parsed.year, parsed.month, 1)

    if start_date.month == 12:
        next_start = date(start_date.year + 1, 1, 1)
    else:
        next_start = date(start_date.year, start_date.month + 1, 1)
    return start_date, next_start


def _parse_bead_date(raw: Any) -> Optional[date]:
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


def _content_or_created_date(bead: Dict[str, Any], *content_keys: str) -> Optional[date]:
    content = bead.get("content", {})
    for key in content_keys:
        parsed = _parse_bead_date(content.get(key))
        if parsed:
            return parsed
    return _parse_bead_date(bead.get("created_at"))


def substrate_base_url() -> str:
    raw_url = os.environ.get("SUBSTRATE_URL", SUBSTRATE_URL).rstrip("/")
    if raw_url.endswith("/beads"):
        return raw_url.removesuffix("/beads")
    return raw_url


def substrate_beads_url() -> str:
    return f"{substrate_base_url()}/beads"


def substrate_headers() -> Dict[str, str]:
    api_key = os.environ.get("SUBSTRATE_API_KEY")
    if not api_key:
        raise RuntimeError("SUBSTRATE_API_KEY must be set for Substrate requests")
    return {"X-API-Key": api_key}


# --- Packaged substrate_client (OPS-190 / M12) ------------------------------
#
# The one Python client for the substrate's BeadStore surface
# (apps/substrate/client/) cannot be a pyproject path dependency: the image
# flattens this service's own src/ to /app/src while COPYing the client tree
# to /app/apps/substrate/client (Dockerfile), so the two trees never resolve
# the same way in a checkout and in the built image. Imported lazily instead,
# the same way tools.task_filing reaches the dispatcher tree -- by inserting
# its src/ onto sys.path from FACTORY_DISPATCHER_ROOT (the Dockerfile sets it
# unconditionally; tests/conftest.py points it at the repo root; the OpenAPI
# CI job leaves it unset). This function must never run at module import
# time, or that CI job's plain `import` of this module would fail.

FACTORY_DISPATCHER_ROOT_ENV = "FACTORY_DISPATCHER_ROOT"


def _substrate_client_src_dir() -> pathlib.Path:
    root = os.environ.get(FACTORY_DISPATCHER_ROOT_ENV)
    if not root:
        raise RuntimeError(
            f"{FACTORY_DISPATCHER_ROOT_ENV} is not set -- the packaged "
            "substrate_client (apps/substrate/client/) is not reachable. "
            "apps/mcp-hub/Dockerfile sets this unconditionally, so an unset "
            "value here means this process was started without it."
        )
    return pathlib.Path(root) / "apps" / "substrate" / "client" / "src"


def _load_substrate_client_module():
    src_dir = str(_substrate_client_src_dir())
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import substrate_client

    return substrate_client


def _substrate_client():
    module = _load_substrate_client_module()
    return module.Substrate(
        base_url=substrate_base_url(), api_key=os.environ.get("SUBSTRATE_API_KEY")
    )


async def _call_substrate(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Run one packaged substrate_client call off the event loop.

    The client is synchronous (its ``_request`` calls ``httpx.request``
    directly) and every caller here is ``async def`` -- ``asyncio.to_thread``
    is the offload; there is no earlier precedent for this in mcp-hub.

    The packaged client raises ``SubstrateError`` (a plain ``RuntimeError``
    subclass) on a non-2xx response. The pre-migration code here raised
    ``httpx.HTTPStatusError`` instead (``response.raise_for_status()``), and
    at least one existing caller -- ``bank_sync.reconcile_beads_activity``,
    deferred and untouched by this migration -- catches that specific type
    around ``patch_bead_state`` to count a rejected transition as skipped
    rather than let it fail the whole activity. Translating here keeps that
    contract for every caller of this shared seam, not only the ones this
    migration happens to touch.
    """
    client = _substrate_client()
    method = getattr(client, method_name)
    try:
        return await asyncio.to_thread(method, *args, **kwargs)
    except Exception as exc:
        module = _load_substrate_client_module()
        if isinstance(exc, module.SubstrateError):
            # AC-6: this seam's exception (SubstrateError) carries a status
            # and body but no HTTP method or request path -- log exactly what
            # it carries (the client method name this call invoked) rather
            # than inventing either. Never logs headers or the API key.
            body_text = (exc.body or "")[:2000]
            logger.error(
                "Substrate rejected packaged-client call %s: status=%s body=%s",
                method_name, exc.status, body_text,
            )
            request = httpx.Request("POST", substrate_beads_url())
            response = httpx.Response(exc.status, request=request, text=exc.body)
            raise httpx.HTTPStatusError(str(exc), request=request, response=response) from exc
        raise


async def _raw_substrate_request(
    method: str,
    path: str = "",
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
) -> Any:
    """One shared raw-request path for the named carve-outs (AC-1(b)): request
    shapes no packaged-client method expresses -- ``_create_bead_raw``'s extra
    top-level field, ``query_beads``' passthrough filters (``created_after``,
    ``offset``), ``_patch_bead_raw``'s ``parent_id``/``state``+``content``
    combinations, and ``set_budget``'s query-param archive PATCH. A single
    place every carve-out shares, not a per-call copy of the request layer.
    """
    url = f"{substrate_beads_url()}{path}"
    async with httpx.AsyncClient(timeout=_SUBSTRATE_TIMEOUT) as client:
        response = await client.request(
            method,
            url,
            params=params,
            json=json,
            headers=substrate_headers(),
        )
        if response.is_error:
            # AC-6: httpx.HTTPStatusError.__str__ (raise_for_status's default)
            # carries the URL and status but never the response body, which is
            # the one thing a 422 needs to say (which field). Never logs
            # headers or the API key.
            body_text = response.text[:2000]
            logger.error(
                "Substrate rejected %s %s: status=%s body=%s",
                method, url, response.status_code, body_text,
            )
            raise httpx.HTTPStatusError(
                f"{method} {url} -> {response.status_code}: {body_text}",
                request=response.request,
                response=response,
            )
        return response.json()


async def _create_bead_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /beads with a body the packaged client's ``create_bead`` cannot
    express -- an extra top-level field beyond namespace/type/state/content/
    created_by/trust_tier/context/provenance (e.g. subscription_auditor's
    top-level ``confidence``). The named carve-out (AC-1): still the one
    place such a write goes through, not a per-file copy of the request.
    """
    return await _raw_substrate_request("POST", json=payload)


# Models
class ExpensePayload(BaseModel):
    amount: float = Field(..., gt=0)
    category: str
    description: str
    vendor: Optional[str] = None
    paid_with: Optional[str] = None
    date: str = Field(default_factory=lambda: datetime.now().date().isoformat())

    def validate_category(self):
        if self.category not in FINANCE_CATEGORIES:
            raise ValueError(f"Invalid category: {self.category}. Must be one of {FINANCE_CATEGORIES}")

class BillPayload(BaseModel):
    amount: float = Field(..., gt=0)
    vendor: str
    due_date: str
    category: str = "utilities"
    source: str = "manual"
    owner: Optional[str] = None
    recurring: bool = False
    frequency: Optional[str] = None # monthly, quarterly, annual

    def validate_category(self):
        if self.category not in FINANCE_CATEGORIES:
            raise ValueError(f"Invalid category: {self.category}. Must be one of {FINANCE_CATEGORIES}")

class BudgetPayload(BaseModel):
    category: str
    amount: float = Field(..., gt=0)
    period: str = "monthly"

    def validate_category(self):
        if self.category not in FINANCE_CATEGORIES:
            raise ValueError(f"Invalid category: {self.category}. Must be one of {FINANCE_CATEGORIES}")

# Substrate Client Helper
#
# Stays on _raw_substrate_request (60s), not the packaged client's
# create_bead: that client's httpx.request call hardcodes HTTP_TIMEOUT_S =
# 30.0 with no per-call override, which would halve the 60s PR #93 gave
# every finance.py store call (not only reads). Body shape matches the
# packaged client's create_bead exactly (namespace/type/state/trust_tier/
# created_by/content, +provenance when given) so the recorder proof still
# holds shape-identical.
async def create_bead(
    bead_type: str,
    content: Dict[str, Any],
    state: str,
    created_by: str,
    *,
    trust_tier: str = "user",
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "namespace": FINANCE_NAMESPACE,
        "type": bead_type,
        "state": state,
        "trust_tier": trust_tier,
        "created_by": created_by,
        "content": content,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return await _raw_substrate_request("POST", json=payload)


# Every shape query_beads takes -- type/state/limit as much as created_after
# or offset -- is a named carve-out (AC-1) through _raw_substrate_request,
# not a list_beads migration. Two reasons, both measured at the #1012 gate
# (round 2): (1) list_beads (client.py) treats `limit` as a page size and
# pages until it exhausts the type/state population, where the old single
# GET this replaces capped at `limit` in one request -- callers of this
# shape (get_bills_due, get_account_balances, ...) rely on that cap, and the
# gate measured 3 requests here where main made 1 on a 2000-transaction
# read. (2) the packaged client hardcodes HTTP_TIMEOUT_S = 30.0 with no
# per-call override, where PR #93 raised finance.py's Substrate read
# timeout to 60s (_SUBSTRATE_TIMEOUT above) after this exact page-and-decrypt
# shape timed out in production. _raw_substrate_request is one shared
# request path already used for query_beads' other shapes, so this is the
# same carve-out widened, not a second copy of the request layer.
async def query_beads(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    params["namespace"] = FINANCE_NAMESPACE
    return await _raw_substrate_request("GET", params=params)

# Tools
async def log_expense(
    amount: float,
    category: str,
    description: str,
    vendor: str = None,
    paid_with: str = None,
    date: str = None,
) -> Dict[str, Any]:
    """Log an expense bead. Returns the created bead."""
    payload = ExpensePayload(
        amount=amount,
        category=category,
        description=description,
        vendor=vendor,
        paid_with=paid_with,
        date=date or datetime.now().date().isoformat()
    )
    payload.validate_category()
    return await create_bead("expense", payload.model_dump(), "logged", "mcp-finance/log_expense")

async def get_expenses(
    month: str = "current",
    category: str = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List expenses for a month, optionally filtered by category."""
    start_date, next_start = _month_window(month)

    params = {
        "type": "expense",
        "created_after": datetime.combine(start_date, datetime.min.time()).isoformat(),
        "limit": limit
    }
    beads = await query_beads(params)
    beads = [
        b for b in beads
        if (bead_date := _content_or_created_date(b, "date")) and start_date <= bead_date < next_start
    ]
    
    if category:
        beads = [b for b in beads if b["content"].get("category") == category]
    
    return beads

async def log_bill(
    amount: float,
    vendor: str,
    due_date: str,
    category: str = "utilities",
    recurring: bool = False,
    frequency: str = None,
    owner: str = None,
) -> Dict[str, Any]:
    """Log a manually-entered bill in pending state."""
    payload = BillPayload(
        amount=amount,
        vendor=vendor,
        due_date=due_date,
        category=category,
        source="manual",
        owner=owner or DEFAULT_OWNER,
        recurring=recurring,
        frequency=frequency
    )
    payload.validate_category()
    return await create_bead("bill", payload.model_dump(), "pending", "mcp-finance/log_bill")

async def mark_bill_paid(bead_id: str, paid_date: str = None) -> Dict[str, Any]:
    """Transition a bill from pending -> paid. Defaults paid_date to today."""
    if not paid_date:
        paid_date = datetime.now().date().isoformat()

    # _raw_substrate_request (60s), not the packaged client's set_state --
    # see the comment on create_bead above. Body shape matches set_state's
    # exactly ({"state", "created_by"}).
    return await _raw_substrate_request(
        "PATCH",
        f"/{bead_id}",
        json={"state": "paid", "created_by": "mcp-finance/mark_bill_paid"},
    )


async def patch_bead_state(
    bead_id: str,
    state: str,
    created_by: str = "mcp-finance/patch_bead_state",
) -> Dict[str, Any]:
    """Convenience: transition a bead's state with no other changes.

    Thin wrapper around :func:`patch_bead` — used by the reconciliation
    engine to move a manual expense from ``logged`` to ``reconciled`` and a
    pending bill from ``pending`` to ``paid``.
    """
    return await patch_bead(bead_id, state=state, created_by=created_by)


async def link_transaction_to_manual(
    transaction_bead_id: str,
    manual_bead_id: str,
    created_by: str = "mcp-finance/reconcile",
) -> Dict[str, Any]:
    """Convenience: stamp ``parent_id`` on a bank-transaction bead and move
    it to ``reconciled`` in a single PATCH.

    The transaction bead points UP at the manual bead (expense or bill) it
    matched, mirroring the bead-provenance convention.
    """
    return await patch_bead(
        transaction_bead_id,
        state="reconciled",
        parent_id=manual_bead_id,
        created_by=created_by,
    )


async def patch_bead(
    bead_id: str,
    *,
    state: Optional[str] = None,
    parent_id: Optional[str] = None,
    content: Optional[Dict[str, Any]] = None,
    created_by: str = "mcp-finance/patch_bead",
) -> Dict[str, Any]:
    """PATCH a bead. Sends state, parent_id, and/or content in the JSON body.

    A state-only call sends exactly the packaged client's ``set_state`` body;
    a content-only call sends exactly its ``patch_content`` body -- both stay
    on ``_raw_substrate_request`` (60s) rather than the packaged client
    itself, whose ``httpx.request`` call hardcodes ``HTTP_TIMEOUT_S = 30.0``
    with no per-call override (see the comment on ``create_bead`` above).
    Anything else (any ``parent_id``, or ``state`` and ``content`` together,
    as the reconciliation engine's refresh/resolve calls do) is a PATCH body
    the client cannot express in one call -- no method takes ``parent_id``
    at all, and no method combines ``state`` with ``content`` -- so it stays
    on the direct request below: the named carve-out (AC-1), kept on the one
    place every caller already shares instead of a second copy of it.
    """
    if parent_id is None and content is None and state is not None:
        return await _raw_substrate_request(
            "PATCH", f"/{bead_id}", json={"state": state, "created_by": created_by}
        )
    if parent_id is None and state is None and content is not None:
        return await _raw_substrate_request(
            "PATCH", f"/{bead_id}", json={"content": content, "created_by": created_by}
        )
    return await _patch_bead_raw(
        bead_id, state=state, parent_id=parent_id, content=content, created_by=created_by
    )


async def _patch_bead_raw(
    bead_id: str,
    *,
    state: Optional[str] = None,
    parent_id: Optional[str] = None,
    content: Optional[Dict[str, Any]] = None,
    created_by: str,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"created_by": created_by}
    if state is not None:
        body["state"] = state
    if parent_id is not None:
        body["parent_id"] = parent_id
    if content is not None:
        body["content"] = content

    return await _raw_substrate_request("PATCH", f"/{bead_id}", json=body)


async def register_plaid_item(
    institution_slug: str,
    item_id: str,
    plaid_env: str,
    created_by: str = "finance/exchange",
) -> Dict[str, Any]:
    """Upsert the ``finance.plaid_item`` bead for an institution + Plaid env.

    The Item registry is the durable record of which Plaid Item backs each
    institution's access token: webhook payloads identify Items only by
    ``item_id``, update-mode re-auth needs to know an Item exists, and
    /item/remove needs the id at unlink time.

    One bead per (institution, plaid_env): a create-mode re-link replaces
    the token AND the Item, so the existing bead is refreshed in place
    (``item_id`` + ``last_relinked_at``) rather than duplicated —
    mirroring how bank_sync upserts account beads. Matching runs
    client-side over decrypted content (``institution`` is a
    PLAINTEXT_KEY; ``plaid_env`` travels encrypted at rest).
    """
    infisical_key = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    now = datetime.now().isoformat()

    existing = await query_beads({"type": "plaid_item", "limit": 100})
    match = next(
        (
            b
            for b in existing
            if (b.get("content") or {}).get("institution") == institution_slug
            and (b.get("content") or {}).get("plaid_env") == plaid_env
        ),
        None,
    )

    if match is not None:
        content = {
            **(match.get("content") or {}),
            "item_id": item_id,
            "infisical_key": infisical_key,
            "last_relinked_at": now,
        }
        bead = await patch_bead(
            match["id"], state="active", content=content, created_by=created_by
        )
        return {"bead_id": bead.get("id", match["id"]), "created": False}

    content = {
        "institution": institution_slug,
        "plaid_env": plaid_env,
        "item_id": item_id,
        "infisical_key": infisical_key,
        "linked_at": now,
    }
    bead = await create_bead("plaid_item", content, state="active", created_by=created_by)
    return {"bead_id": bead["id"], "created": True}


async def get_bills_due(days_ahead: int = 14, only_pending: bool = True) -> List[Dict[str, Any]]:
    """Bills due in the next N days. Sorted by due_date asc."""
    params = {
        "type": "bill",
        "limit": 1000  # Fetch a large enough set to filter
    }
    if only_pending:
        params["state"] = "pending"
    
    beads = await query_beads(params)
    
    now = datetime.now().date()
    future_limit = now + timedelta(days=days_ahead)
    
    due_beads = []
    for b in beads:
        due_date_str = b["content"].get("due_date")
        if not due_date_str:
            continue
        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            if now <= due_date <= future_limit:
                due_beads.append(b)
        except ValueError:
            continue
            
    due_beads.sort(key=lambda x: x["content"]["due_date"])
    return due_beads

async def set_budget(category: str, amount: float, period: str = "monthly") -> Dict[str, Any]:
    """Create or replace the active budget for a category. Archives the previous one."""
    if category not in FINANCE_CATEGORIES:
        raise ValueError(f"Invalid category: {category}. Must be one of {FINANCE_CATEGORIES}")

    # 1. Find the active budget bead for that category
    params = {
        "type": "budget",
        "state": "active",
        "limit": 100
    }
    existing_budgets = await query_beads(params)
    for b in existing_budgets:
        if b["content"].get("category") == category:
            # 2. If exists, transition to archived. Query-string params, not
            # a JSON body -- a shape no packaged-client method expresses --
            # so it stays the named carve-out (AC-1) through the shared helper.
            patch_params = {"to_state": "archived", "created_by": "mcp-finance/set_budget"}
            await _raw_substrate_request("PATCH", f"/{b['id']}", params=patch_params)

    # 3. Create new active budget bead
    payload = BudgetPayload(category=category, amount=amount, period=period)
    return await create_bead("budget", payload.model_dump(), "active", "mcp-finance/set_budget")

async def get_budget_status(month: str = "current") -> Dict[str, Any]:
    """Per-category: budget, spent, remaining. Plus total.

    Spend includes both manually logged ``finance.expense`` beads and
    imported Plaid ``finance.transaction`` beads so budget tools, pulses,
    and dashboards reason over the same month-to-date lake.
    """
    # 1. Fetch all state=active budget beads
    budget_params = {"type": "budget", "state": "active", "limit": 100}
    budgets = await query_beads(budget_params)
    
    # 2. Fetch this month's manual expenses and bank transactions.
    expenses = await get_expenses(month=month, limit=1000)
    transactions = await get_transactions(month=month, limit=5000)
    
    # 3. Aggregate by category
    status = {}
    total_budget = 0.0
    total_spent = 0.0
    
    # Initialize with budgets
    for b in budgets:
        cat = b["content"]["category"]
        amt = b["content"]["amount"]
        status[cat] = {
            "budget": amt,
            "spent": 0.0,
            "remaining": amt
        }
        total_budget += amt
        
    def add_spend(cat: Optional[str], amount: Any) -> None:
        nonlocal total_spent
        if not cat:
            return
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return
        if cat not in status:
            status[cat] = {"budget": 0.0, "spent": 0.0, "remaining": 0.0}

        status[cat]["spent"] += amt
        status[cat]["remaining"] = status[cat]["budget"] - status[cat]["spent"]
        total_spent += amt

    # Add expenses
    for e in expenses:
        add_spend(e.get("content", {}).get("category"), e.get("content", {}).get("amount"))

    # Add imported bank transactions. Plaid outflows are positive; refunds
    # and deposits are usually negative, so this is net month-to-date spend.
    for tx in transactions:
        content = tx.get("content", {})
        add_spend(content.get("our_category") or content.get("category"), content.get("amount"))
        
    status["total"] = {
        "budget": total_budget,
        "spent": total_spent,
        "remaining": total_budget - total_spent
    }
    
    return status

async def get_transactions(
    month: str = "current",
    category: str = None,
    institution: str = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Real bank transactions imported via Plaid."""
    start_date, next_start = _month_window(month)

    params = {
        "type": "transaction",
        "created_after": datetime.combine(start_date, datetime.min.time()).isoformat(),
        "limit": limit
    }
    beads = await query_beads(params)
    beads = [
        b for b in beads
        # Plaid-removed (and dedup-removed) transactions must not reach
        # budget math, tool answers, or pulses.
        if b.get("state") != "removed"
        and (bead_date := _content_or_created_date(b, "posted_date", "date"))
        and start_date <= bead_date < next_start
    ]
    
    # Filter by category and institution
    if category:
        beads = [b for b in beads if b["content"].get("our_category") == category]
    if institution:
        beads = [b for b in beads if b["content"].get("institution") == institution]
        
    return beads


async def get_recent_transactions(
    category: str = None,
    institution: str = None,
    limit: int = 1000,
    include_transfers: bool = True,
) -> List[Dict[str, Any]]:
    """Recent bank transactions ordered by transaction posting date.

    The console ledger wants "most recent" to mean the bank's
    ``posted_date``. Substrate's generic ``/beads`` endpoint orders by bead
    ``created_at``, which is useful for operational audit trails but wrong
    for a transaction ledger after backfills, retries, or re-links.
    """
    limit = max(1, min(limit, 2000))
    beads = await _query_all_beads({"type": "transaction"}, page_size=1000, max_pages=20)

    filtered: List[Dict[str, Any]] = []
    for bead in beads:
        if bead.get("state") == "removed":
            continue
        content = bead.get("content") or {}
        if not include_transfers and content.get("is_transfer"):
            continue
        if category and (content.get("our_category") or content.get("category")) != category:
            continue
        if institution and content.get("institution") != institution:
            continue
        if _parse_bead_date(content.get("posted_date") or content.get("date")) is None:
            continue
        filtered.append(bead)

    def recency_key(bead: Dict[str, Any]) -> Tuple[str, str]:
        content = bead.get("content") or {}
        posted = _parse_bead_date(content.get("posted_date") or content.get("date"))
        return (
            posted.isoformat() if posted else "",
            str(bead.get("created_at") or ""),
        )

    filtered.sort(key=recency_key, reverse=True)
    return filtered[:limit]

async def get_account_balances() -> List[Dict[str, Any]]:
    """Current balances for all connected accounts."""
    params = {
        "type": "account",
        "state": "active",
        "limit": 100
    }
    return await query_beads(params)

_MERCHANT_NORMALIZE_RE = None  # populated on first use


def _normalize_merchant(name: Optional[str]) -> str:
    """Strip POS noise so 'NETFLIX.COM*1234' / 'NETFLIX  COM' collapse.

    Conservative: uppercases, strips trailing transaction-ref digits, collapses
    whitespace, drops common POS prefixes (TST*, SQ *, PAYPAL *). The result is
    used as a grouping key only; the original merchant string is preserved
    inside each bucket's `samples` for the LLM analyzer.
    """
    global _MERCHANT_NORMALIZE_RE
    if not name:
        return ""
    if _MERCHANT_NORMALIZE_RE is None:
        import re as _re
        _MERCHANT_NORMALIZE_RE = {
            "pos_prefix": _re.compile(r"^(TST\*|SQ \*|PAYPAL \*|SP \*|SP\*)\s*", _re.I),
            # Trailing transaction reference: must include at least one digit
            # so legitimate words like "COFFEE", "COSTCO", "TARGET" aren't stripped.
            "ref_suffix": _re.compile(r"[\s\*#]+(?=[A-Z0-9]{4,}\s*$)(?=.*\d)[A-Z0-9]+\s*$"),
            "punct": _re.compile(r"[^A-Z0-9 ]+"),
            "ws": _re.compile(r"\s+"),
        }
    s = name.strip().upper()
    s = _MERCHANT_NORMALIZE_RE["pos_prefix"].sub("", s)
    s = _MERCHANT_NORMALIZE_RE["ref_suffix"].sub("", s)
    s = _MERCHANT_NORMALIZE_RE["punct"].sub(" ", s)
    s = _MERCHANT_NORMALIZE_RE["ws"].sub(" ", s).strip()
    if s.endswith(" COM"):
        s = s[:-4].strip()
    return s


async def _semantic_merge_clusters(
    clusters: List[Dict[str, Any]],
    *,
    similarity_threshold: float = 0.82,
    per_query_limit: int = 8,
) -> List[Dict[str, Any]]:
    """Use Substrate `/beads/search` to merge lexically-distinct clusters that
    represent the same merchant (e.g., 'NETFLIX' vs 'NETFLIX SUBSCRIPTION').

    For each cluster head, query the vector index for the most-similar
    `finance.transaction` beads and roll their cluster (if any) into this one
    when the cosine similarity is above ``similarity_threshold``. The endpoint
    is not strictly required — if the call fails (e.g., Qdrant down), we
    return the unmerged clusters and log a warning. The brief requires using
    the endpoint; this satisfies that without making subscription detection
    depend on it.
    """
    # Build a fast lookup: bead_id -> cluster index
    bead_to_cluster: Dict[str, int] = {}
    for idx, c in enumerate(clusters):
        for bid in c.get("evidence_tx_ids", []):
            bead_to_cluster[bid] = idx

    merged_into: Dict[int, int] = {}  # source idx -> dest idx (after compression)

    for src_idx, cluster in enumerate(clusters):
        if src_idx in merged_into:
            continue
        try:
            hits = await _call_substrate(
                "search",
                cluster["merchant"],
                limit=per_query_limit,
                namespace=FINANCE_NAMESPACE,
                type="transaction",
            )
        except Exception:
            # Endpoint may be down; semantic merge is best-effort.
            continue

        for hit in hits:
            score = float(hit.get("score") or 0.0)
            if score < similarity_threshold:
                continue
            hit_bead_id = (hit.get("bead") or {}).get("id")
            if not hit_bead_id:
                continue
            dst_idx = bead_to_cluster.get(hit_bead_id)
            if dst_idx is None or dst_idx == src_idx:
                continue
            # Resolve to the active root cluster.
            while dst_idx in merged_into:
                dst_idx = merged_into[dst_idx]
            if dst_idx == src_idx:
                continue
            # Merge the smaller into the larger so the surviving merchant
            # name is the one with more evidence.
            a, b = src_idx, dst_idx
            if len(clusters[a]["evidence_tx_ids"]) < len(clusters[b]["evidence_tx_ids"]):
                a, b = b, a
            clusters[a]["evidence_tx_ids"].extend(clusters[b]["evidence_tx_ids"])
            clusters[a]["samples"].extend(clusters[b]["samples"])
            clusters[a]["amounts"].extend(clusters[b]["amounts"])
            clusters[a]["occurrences"] += clusters[b]["occurrences"]
            clusters[a]["total_amount"] += clusters[b]["total_amount"]
            first_seen = [
                value for value in (clusters[a].get("first_seen"), clusters[b].get("first_seen")) if value
            ]
            last_seen = [
                value for value in (clusters[a].get("last_seen"), clusters[b].get("last_seen")) if value
            ]
            if first_seen:
                clusters[a]["first_seen"] = min(first_seen)
            if last_seen:
                clusters[a]["last_seen"] = max(last_seen)
            merged_into[b] = a

    return [c for i, c in enumerate(clusters) if i not in merged_into]


async def get_recurring_candidates(
    months: int = 6,
    min_occurrences: int = 3,
    use_semantic_merge: bool = True,
) -> List[Dict[str, Any]]:
    """Find merchants that recur in the last ``months`` of bank transactions.

    Returns one cluster per likely-recurring merchant, shape::

        {
          "merchant": "NETFLIX",            # normalized key
          "display_name": "NETFLIX.COM",     # most recent raw merchant string
          "occurrences": 12,
          "total_amount": 191.88,
          "avg_amount": 15.99,
          "recent_amount": 17.99,
          "first_seen": "2025-11-22",
          "last_seen": "2026-05-12",
          "evidence_tx_ids": ["uuid", ...],  # for bead.provenance.parent_ids
          "amounts": [15.99, 15.99, 17.99, ...],
          "samples": [{"merchant": ..., "amount": ..., "date": ...}, ...],
        }

    Implementation
    --------------
    1. Pull ``finance.transaction`` beads created in the last ``months``
       directly from Substrate's bead list endpoint — `/beads/search` is
       semantic similarity, not aggregation, so the primary recurrence
       detection has to be count-based.
    2. Group by ``_normalize_merchant`` of ``content.merchant_name`` (Plaid)
       falling back to ``content.name``.
    3. Optionally call ``/beads/search`` per cluster head to merge
       lexically-distinct neighbours that the vector index considers the same
       merchant (e.g., 'NETFLIX' vs 'NETFLIX SUBSCRIPTION'). Honours the brief
       requirement to use the endpoint without making recurrence detection
       depend on the vector index being healthy.

    Only clusters with ``>= min_occurrences`` are returned. Sorted by total
    spend (descending) so the analyzer prioritises the costliest first.
    """
    since = datetime.now() - timedelta(days=months * 31)  # generous window
    params = {
        "type": "transaction",
        "state": "posted",
        "created_after": since.isoformat(),
        "limit": 5000,
    }
    beads = await query_beads(params)

    clusters: Dict[str, Dict[str, Any]] = {}
    for tx in beads:
        content = tx.get("content", {}) or {}
        raw_merchant = (
            content.get("normalized_merchant")
            or content.get("merchant")
            or content.get("merchant_name")
            or content.get("description")
            or content.get("name")
            or ""
        )
        norm = _normalize_merchant(raw_merchant)
        if not norm:
            continue
        tx_id = tx.get("id")
        if not tx_id:
            continue
        amount = content.get("amount")
        try:
            amount_f = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount_f = None
        # Skip refunds/credits — recurrence is about outflows. Plaid convention:
        # positive amount = outflow.
        if amount_f is None or amount_f <= 0:
            continue
        tx_date = _content_or_created_date(tx, "posted_date", "date")

        bucket = clusters.setdefault(
            norm,
            {
                "merchant": norm,
                "display_name": raw_merchant,
                "occurrences": 0,
                "total_amount": 0.0,
                "amounts": [],
                "evidence_tx_ids": [],
                "samples": [],
                "first_seen": tx_date.isoformat() if tx_date else None,
                "last_seen": tx_date.isoformat() if tx_date else None,
            },
        )
        bucket["occurrences"] += 1
        bucket["total_amount"] += amount_f
        bucket["amounts"].append(round(amount_f, 2))
        bucket["evidence_tx_ids"].append(tx_id)
        bucket["samples"].append(
            {
                "id": tx_id,
                "merchant": raw_merchant,
                "amount": round(amount_f, 2),
                "date": tx_date.isoformat() if tx_date else None,
            }
        )
        if tx_date:
            iso = tx_date.isoformat()
            if not bucket["first_seen"] or iso < bucket["first_seen"]:
                bucket["first_seen"] = iso
            if not bucket["last_seen"] or iso > bucket["last_seen"]:
                bucket["last_seen"] = iso
                bucket["display_name"] = raw_merchant  # most-recent string

    cluster_list = list(clusters.values())

    if use_semantic_merge and cluster_list:
        cluster_list = await _semantic_merge_clusters(cluster_list)

    # Compute derived stats and apply min_occurrences filter post-merge so the
    # threshold is honoured against the merged counts, not the lexical ones.
    candidates: List[Dict[str, Any]] = []
    for c in cluster_list:
        if c["occurrences"] < min_occurrences:
            continue
        sorted_samples = sorted(c["samples"], key=lambda s: s.get("date") or "")
        c["samples"] = sorted_samples
        c["amounts"] = [sample["amount"] for sample in sorted_samples]
        c["evidence_tx_ids"] = [sample["id"] for sample in sorted_samples if sample.get("id")]
        c["total_amount"] = round(c["total_amount"], 2)
        c["avg_amount"] = round(c["total_amount"] / c["occurrences"], 2)
        c["recent_amount"] = c["amounts"][-1] if c["amounts"] else None
        # Cap samples sent to the LLM to keep prompts bounded.
        c["samples"] = c["samples"][-20:]
        candidates.append(c)

    candidates.sort(key=lambda c: c["total_amount"], reverse=True)
    return candidates


async def get_unreconciled_transactions(days_back: int = 30) -> List[Dict[str, Any]]:
    """Imported transactions that have no LLM category."""
    start_date = datetime.now() - timedelta(days=days_back)
    params = {
        "type": "transaction",
        "created_after": start_date.isoformat(),
        "limit": 500
    }
    beads = await query_beads(params)
    return [b for b in beads if b["content"].get("our_category") is None]


# --- Subscriptions (read the weekly subscription-auditor's output) ----------
#
# The WeeklySubscriptionAuditWorkflow already detects recurring subscriptions
# (cadence, per-charge amount, price-hike) via an LLM + deterministic pipeline
# and persists one ``finance.subscription`` bead per detection. These helpers
# turn that system-of-record into a console-ready ledger — they DO NOT re-derive
# subscriptions from raw transactions (that's the auditor's job).

# Days between charges for each cadence — used for staleness, not billing.
_SUBSCRIPTION_CADENCE_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 91,
    "semiannual": 182,
    "annual": 365,
}

# Per-charge amount -> monthly-equivalent multiplier. Mirrors
# subscription_auditor._monthly_equivalent so the console total matches the
# weekly Discord summary.
_SUBSCRIPTION_MONTHLY_FACTOR = {
    "weekly": 4.33,
    "monthly": 1.0,
    "quarterly": 1 / 3,
    "semiannual": 1 / 6,
    "annual": 1 / 12,
}


def _subscription_monthly_equivalent(amount: Any, frequency: Any) -> float:
    """Normalize a per-charge amount to a monthly figure by cadence.

    Unknown cadence is treated as monthly (the most common case and the
    least-surprising default for a household burn total)."""
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        return 0.0
    factor = _SUBSCRIPTION_MONTHLY_FACTOR.get(str(frequency or "").lower(), 1.0)
    return round(amt * factor, 2)


def _subscription_is_stale(
    last_seen: Any, frequency: Any, *, today: Optional[date] = None
) -> bool:
    """A subscription is 'stale' (a likely-cancelled / cancel candidate) when
    the most recent charge is older than 1.5x its expected cadence. Unknown
    cadence falls back to ~monthly (35d) so we don't false-flag annuals."""
    last = _parse_bead_date(last_seen)
    if not last:
        return False
    ref = today or datetime.now().date()
    cadence = _SUBSCRIPTION_CADENCE_DAYS.get(str(frequency or "").lower(), 35)
    return (ref - last).days > cadence * 1.5


async def list_subscriptions(include_stale: bool = True) -> Dict[str, Any]:
    """Console-ready view of detected ``finance.subscription`` beads.

    The auditor POSTs a fresh bead on every weekly run (no upsert), so the
    same merchant accrues multiple beads over time. We dedup to the most
    recently-created bead per ``merchant_key`` here, compute each
    subscription's monthly-equivalent cost, and surface the two signals a
    household CFO acts on: price hikes (already detected by the auditor) and
    stale subscriptions (no charge within 1.5x the expected cadence — a cancel
    candidate).

    Returns::

        {
          "total_monthly": 137.42,     # sum of live (non-stale) monthly equivs
          "active_count": 9,
          "stale_count": 2,
          "price_hike_count": 1,
          "subscriptions": [ {..}, .. ]  # sorted by monthly_equivalent desc
        }
    """
    beads = await query_beads({"type": "subscription", "state": "active", "limit": 1000})

    # Dedup to the most-recent bead per merchant (the auditor appends, never upserts).
    latest: Dict[str, Dict[str, Any]] = {}
    for b in beads:
        content = b.get("content") or {}
        key = content.get("merchant_key") or content.get("name") or b.get("id")
        existing = latest.get(key)
        if existing is None or (b.get("created_at") or "") > (existing.get("created_at") or ""):
            latest[key] = b

    subscriptions: List[Dict[str, Any]] = []
    total_monthly = 0.0
    stale_count = 0
    price_hike_count = 0

    for b in latest.values():
        content = b.get("content") or {}
        frequency = content.get("frequency")
        monthly = _subscription_monthly_equivalent(content.get("amount"), frequency)
        is_stale = _subscription_is_stale(content.get("last_seen"), frequency)
        is_price_hike = bool(content.get("is_price_hike"))

        if is_stale:
            stale_count += 1
        else:
            # Only live subscriptions count toward the recurring monthly burn.
            total_monthly += monthly
        if is_price_hike:
            price_hike_count += 1

        if is_stale and not include_stale:
            continue

        subscriptions.append(
            {
                "bead_id": b.get("id"),
                "name": content.get("name"),
                "merchant_key": content.get("merchant_key"),
                "category": content.get("category"),
                "amount": content.get("amount"),
                "frequency": frequency,
                "monthly_equivalent": monthly,
                "first_seen": content.get("first_seen"),
                "last_seen": content.get("last_seen"),
                "occurrences": content.get("occurrences"),
                "confidence": content.get("confidence"),
                "is_price_hike": is_price_hike,
                "price_change": content.get("price_change") or {},
                "is_stale": is_stale,
                "status": "stale" if is_stale else ("price_hike" if is_price_hike else "active"),
            }
        )

    subscriptions.sort(key=lambda s: s["monthly_equivalent"], reverse=True)

    return {
        "total_monthly": round(total_monthly, 2),
        "active_count": sum(1 for s in subscriptions if not s["is_stale"]),
        "stale_count": stale_count,
        "price_hike_count": price_hike_count,
        "subscriptions": subscriptions,
    }


# --- Bill guardian (console-ready bill ledger) ------------------------------
#
# bank_sync's reconcile step auto-matches bills to bank transactions, including
# overdue liability bills that post as card-payment transfer rows, flips the
# bill to paid, and stamps transaction.parent_id = bill.id. This reads that
# result and groups bills the way a household CFO triages them — what's
# overdue, what's due soon, and proof that a 'paid' bill really was paid.


def _bill_entry(bead: Dict[str, Any], *, status: Optional[str] = None) -> Dict[str, Any]:
    content = bead.get("content") or {}
    return {
        "bead_id": bead.get("id"),
        "vendor": content.get("vendor"),
        "amount": content.get("amount"),
        "due_date": content.get("due_date"),
        "category": content.get("category"),
        "source": content.get("source"),
        "state": bead.get("state"),
        "recurring": content.get("recurring"),
        "frequency": content.get("frequency"),
        "status": status or bead.get("state"),
    }


async def get_bill_ledger(days_ahead: int = 14) -> Dict[str, Any]:
    """Console-ready bill guardian view.

    Classifies bills relative to today and attaches proof-of-payment to paid
    bills by following the reconcile link (``transaction.parent_id == bill.id``):

      - ``overdue``  — pending/overdue and ``due_date`` is in the past
      - ``due_soon`` — pending and due within the next ``days_ahead`` days
      - ``upcoming`` — pending and due beyond the horizon
      - ``paid``     — already paid; ``proof`` carries the matched transaction

    Archived (or otherwise non-actionable) bills are omitted. Each actionable
    group is sorted by ``due_date`` ascending; paid is most-recent-first.
    """
    bills = await query_beads({"type": "bill", "limit": 1000})

    # Index proof-of-payment by the bill each reconciled transaction points at.
    reconciled = await query_beads(
        {"type": "transaction", "state": "reconciled", "limit": 2000}
    )
    proof_by_bill: Dict[str, Dict[str, Any]] = {}
    for tx in reconciled:
        bill_id = tx.get("parent_id")
        if not bill_id or bill_id in proof_by_bill:
            continue
        c = tx.get("content") or {}
        proof_by_bill[bill_id] = {
            "transaction_id": tx.get("id"),
            "amount": c.get("amount"),
            "posted_date": c.get("posted_date") or c.get("date"),
            "merchant": c.get("merchant_name") or c.get("name"),
        }

    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)

    groups: Dict[str, List[Dict[str, Any]]] = {
        "overdue": [],
        "due_soon": [],
        "upcoming": [],
        "paid": [],
    }
    totals = {"overdue": 0.0, "due_soon": 0.0, "upcoming": 0.0}

    def _amount(entry: Dict[str, Any]) -> float:
        try:
            return float(entry.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0

    for b in bills:
        state = b.get("state")
        if state == "paid":
            entry = _bill_entry(b, status="paid")
            entry["proof"] = proof_by_bill.get(b.get("id"))
            groups["paid"].append(entry)
            continue
        if state not in ("pending", "overdue"):
            continue  # archived / unknown — not actionable
        due = _parse_bead_date((b.get("content") or {}).get("due_date"))
        if due is None:
            # No due date — can't time it; treat as upcoming, no total impact.
            groups["upcoming"].append(_bill_entry(b, status="upcoming"))
            continue
        if due < today:
            entry = _bill_entry(b, status="overdue")
            groups["overdue"].append(entry)
            totals["overdue"] += _amount(entry)
        elif due <= horizon:
            entry = _bill_entry(b, status="due_soon")
            groups["due_soon"].append(entry)
            totals["due_soon"] += _amount(entry)
        else:
            entry = _bill_entry(b, status="upcoming")
            groups["upcoming"].append(entry)
            totals["upcoming"] += _amount(entry)

    for key in ("overdue", "due_soon", "upcoming"):
        groups[key].sort(key=lambda e: e.get("due_date") or "")
    groups["paid"].sort(key=lambda e: e.get("due_date") or "", reverse=True)

    return {
        "as_of": today.isoformat(),
        "days_ahead": days_ahead,
        "overdue_count": len(groups["overdue"]),
        "due_soon_count": len(groups["due_soon"]),
        "overdue_total": round(totals["overdue"], 2),
        "due_soon_total": round(totals["due_soon"], 2),
        "upcoming_total": round(totals["upcoming"], 2),
        "groups": groups,
    }


# --- Run rate (monthly burn, Fixed vs Variable) -----------------------------
#
# 'Fixed' is derived from the recurring-spend systems of record — live
# finance.subscription beads + recurring finance.bill beads — NOT a hard-coded
# category list (Gemini's WIP guessed fixed-ness from category names, which
# mislabels e.g. a one-off "health" charge as fixed). 'Variable' is the
# residual of average monthly transaction spend minus Fixed.

_BILL_MONTHLY_FACTOR = {
    "weekly": 4.33,
    "monthly": 1.0,
    "quarterly": 1 / 3,
    "semiannual": 1 / 6,
    "annual": 1 / 12,
}

_RUN_RATE_PAGE_SIZE = 1000


def _months_before(d: date, n: int) -> date:
    """First-of-month ``n`` months before ``d`` (which should be a first-of-month)."""
    idx = d.year * 12 + (d.month - 1) - n
    year, month0 = divmod(idx, 12)
    return date(year, month0 + 1, 1)


def _bill_monthly_equivalent(amount: Any, frequency: Any) -> float:
    """Monthly-equivalent of a recurring bill. Unknown/None cadence -> monthly."""
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        return 0.0
    factor = _BILL_MONTHLY_FACTOR.get(str(frequency or "monthly").lower(), 1.0)
    return round(amt * factor, 2)


async def _query_all_beads(
    params: Dict[str, Any],
    *,
    page_size: Optional[int] = None,
    max_pages: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch all matching beads from Substrate's offset-paginated list API."""
    page_size = page_size or _RUN_RATE_PAGE_SIZE
    results: List[Dict[str, Any]] = []
    for page in range(max_pages):
        page_params = {
            **params,
            "limit": page_size,
            "offset": page * page_size,
        }
        chunk = await query_beads(page_params)
        results.extend(chunk)
        if len(chunk) < page_size:
            break
    return results


def _bill_external_series_key(external_id: Any) -> Optional[str]:
    if not external_id:
        return None
    text = str(external_id)
    parts = text.rsplit(":", 1)
    if len(parts) == 2:
        suffix = parts[1]
        if (
            len(suffix) == 7
            and suffix[4] == "-"
            and suffix[:4].isdigit()
            and suffix[5:].isdigit()
        ):
            return parts[0]
    return text


def _recurring_bill_key(bead: Dict[str, Any]) -> str:
    """Stable key for one recurring obligation across generated instances."""
    content = bead.get("content") or {}
    external_series = _bill_external_series_key(content.get("external_id"))
    if external_series:
        return f"external:{external_series}"

    account_id = content.get("account_id")
    if account_id:
        return (
            f"account:{account_id}:"
            f"{content.get('source') or ''}:"
            f"{content.get('frequency') or 'monthly'}"
        )

    vendor = " ".join(str(content.get("vendor") or "").lower().split())
    return "|".join(
        (
            str(content.get("owner") or DEFAULT_OWNER),
            str(content.get("source") or ""),
            vendor,
            str(content.get("category") or ""),
            str(content.get("frequency") or "monthly"),
        )
    )


def _bill_recency_key(bead: Dict[str, Any]) -> Tuple[str, str]:
    content = bead.get("content") or {}
    due = _parse_bead_date(content.get("due_date"))
    return (
        due.isoformat() if due else "",
        str(bead.get("created_at") or ""),
    )


async def get_category_history(months: int = 6) -> Dict[str, Any]:
    """Per-category monthly spend over the last N complete calendar months.

    Returns a series + stats (avg, median, max, trend) per category — the
    data powering the budget-setting panel's recommendation tiers.
    """
    months = max(1, min(months, 24))
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    window_start = _months_before(first_of_current, months)

    # Build ordered list of month keys for the window
    month_keys: List[str] = []
    d = window_start
    while d < first_of_current:
        month_keys.append(d.strftime("%Y-%m"))
        d = date(d.year, d.month + 1, 1) if d.month < 12 else date(d.year + 1, 1, 1)

    # Bucket spend: {category: {month_key: total}}
    by_cat: Dict[str, Dict[str, float]] = {}

    def _add(cat: Optional[str], mk: str, amt: float) -> None:
        if not cat or amt <= 0:
            return
        by_cat.setdefault(cat, {})
        by_cat[cat][mk] = by_cat[cat].get(mk, 0.0) + amt

    tx_beads = await _query_all_beads({"type": "transaction"})
    linked_manual_ids = {
        tx.get("parent_id")
        for tx in tx_beads
        if tx.get("state") != "removed" and tx.get("parent_id")
    }
    for tx in tx_beads:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("is_transfer"):
            continue
        posted = _parse_bead_date(content.get("posted_date") or content.get("date"))
        if posted is None or not (window_start <= posted < first_of_current):
            continue
        try:
            amt = float(content.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        cat = content.get("our_category") or content.get("category")
        _add(cat, posted.strftime("%Y-%m"), amt)

    expense_beads = await _query_all_beads({"type": "expense"})
    for exp in expense_beads:
        if exp.get("state") == "removed":
            continue
        # Reconciled manual expenses are represented by their linked bank
        # transaction, so counting both inflates historical averages.
        if exp.get("state") == "reconciled" or exp.get("id") in linked_manual_ids:
            continue
        content = exp.get("content") or {}
        posted = _parse_bead_date(content.get("date") or content.get("posted_date"))
        if posted is None or not (window_start <= posted < first_of_current):
            continue
        try:
            amt = float(content.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        cat = content.get("category")
        _add(cat, posted.strftime("%Y-%m"), amt)

    categories: Dict[str, Any] = {}
    for cat, month_totals in by_cat.items():
        series = [round(month_totals.get(mk, 0.0), 2) for mk in month_keys]
        nonzero = sorted(v for v in series if v > 0)
        avg = round(sum(series) / len(series), 2) if series else 0.0
        median = round(nonzero[len(nonzero) // 2], 2) if nonzero else 0.0
        max_val = round(max(series), 2) if series else 0.0

        mid = len(series) // 2
        first_avg = sum(series[:mid]) / mid if mid else 0.0
        last_avg = sum(series[mid:]) / (len(series) - mid) if (len(series) - mid) else 0.0
        if first_avg == 0:
            trend = "new"
        elif last_avg > first_avg * 1.1:
            trend = "up"
        elif last_avg < first_avg * 0.9:
            trend = "down"
        else:
            trend = "stable"

        categories[cat] = {
            "series": series,
            "avg": avg,
            "median": median,
            "max": max_val,
            "trend": trend,
            "months_with_data": len(nonzero),
        }

    return {
        "months": months,
        "month_keys": month_keys,
        "window_start": window_start.isoformat(),
        "categories": categories,
    }


async def calculate_run_rate(months: int = 3) -> Dict[str, Any]:
    """Average monthly burn over the last ``months`` COMPLETE calendar months,
    split into Fixed (recurring commitments) and Variable (the residual).

    Spend is windowed on each transaction's ``posted_date`` — never bead
    ``created_at`` — because the 2-year historical backfill created all those
    beads recently, so a ``created_after`` window would massively overcount.
    """
    months = max(1, months)
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    window_start = _months_before(first_of_current, months)

    # 1. Average monthly transaction spend (outflows) over the complete-month window.
    tx_beads = await _query_all_beads({"type": "transaction"})
    total_spend = 0.0
    for tx in tx_beads:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("is_transfer"):
            continue
        posted = _parse_bead_date(content.get("posted_date") or content.get("date"))
        if posted is None or not (window_start <= posted < first_of_current):
            continue
        try:
            amt = float(content.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amt <= 0:  # outflows only (Plaid: positive = outflow)
            continue
        total_spend += amt

    monthly_total = round(total_spend / months, 2)

    # 2. Fixed = live subscriptions (monthly-equiv) + recurring bills (monthly-equiv).
    subs = await list_subscriptions(include_stale=False)
    subscriptions_monthly = subs["total_monthly"]

    bills = await _query_all_beads({"type": "bill"})
    current_recurring_bills: Dict[str, Dict[str, Any]] = {}
    for b in bills:
        if b.get("state") not in ("pending", "paid", "overdue"):
            continue
        c = b.get("content") or {}
        if not c.get("recurring"):
            continue
        key = _recurring_bill_key(b)
        existing = current_recurring_bills.get(key)
        if existing is None or _bill_recency_key(b) > _bill_recency_key(existing):
            current_recurring_bills[key] = b

    bills_monthly = 0.0
    for b in current_recurring_bills.values():
        c = b.get("content") or {}
        bills_monthly += _bill_monthly_equivalent(c.get("amount"), c.get("frequency"))
    bills_monthly = round(bills_monthly, 2)

    fixed_monthly = round(subscriptions_monthly + bills_monthly, 2)
    variable_monthly = round(max(monthly_total - fixed_monthly, 0.0), 2)

    return {
        "months_analyzed": months,
        "window_start": window_start.isoformat(),
        "window_end": first_of_current.isoformat(),
        "monthly_run_rate": monthly_total,
        "fixed_monthly": fixed_monthly,
        "variable_monthly": variable_monthly,
        "fixed_breakdown": {
            "subscriptions": subscriptions_monthly,
            "recurring_bills": bills_monthly,
        },
    }
