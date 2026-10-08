import asyncio
import calendar
import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from temporalio import workflow, activity
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

# httpx imports urllib.request.Request which trips Temporal's workflow sandbox
# even though httpx is only used inside activities (deterministic-free zone).
# Mark pass-through so workflow validation doesn't traverse it.
with workflow.unsafe.imports_passed_through():
    import httpx
    from src.tools.notify import AlertPolicy, send_alert
    from src.tools.notify import AlertSeverity
    from src.tools.notify import post_discord as _post_discord
    # Private by name, imported deliberately. LO-OBS-005 made one mechanism
    # decide what environment this is. Re-deriving DEPLOY_ENV locally would be
    # a SECOND way of answering that question — which is how a dev pod's
    # sandbox failure gets recorded as a real account problem.
    from src.tools.notify import _env_label as notify_env_label
    from src.tools.plaid import (
        get_accounts,
        get_item_status,
        get_liabilities,
        get_transactions,
        sync_transactions_page,
        # Canonical home of the Item-health classification (also used by
        # /finance/connections); aliased to the legacy private names so
        # the activity code below is unchanged.
        REAUTH_ERROR_CODES as _REAUTH_ERROR_CODES,
        TOKEN_REPAIR_ERROR_CODES as _TOKEN_REPAIR_ERROR_CODES,
        plaid_error_code_from_exception as _plaid_error_code_from_exception,
    )
    from src.tools.finance import (
        FINANCE_CATEGORIES,
        DEFAULT_OWNER,
        create_bead,
        link_transaction_to_manual,
        patch_bead,
        patch_bead_state,
        query_beads as finance_query_beads,
        register_plaid_item,
        substrate_beads_url,
        substrate_headers,
    )
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.tools.litellm_client import (
        LIFEOPS_DEFAULT_MODEL,
        LiteLLMProviderRetiredError,
        post_chat_completion,
        response_cost_usd,
    )


def _current_workflow_id() -> Optional[str]:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _sync_failure_message(exc: BaseException) -> str:
    """Return the most actionable error from a nested Temporal failure.

    Workflow-level activity failures stringify as "Activity task failed",
    which is too generic for the Discord alert. Walk the exception chain and
    prefer the deepest non-empty message so rate limits, Plaid errors, and
    network failures survive the workflow wrapper.
    """
    seen: set[int] = set()
    messages: List[str] = []
    cursor: Optional[BaseException] = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        message = str(cursor).strip()
        if message:
            messages.append(message)
        cursor = cursor.__cause__ or cursor.__context__

    for message in reversed(messages):
        if message != "Activity task failed":
            return message
    return messages[0] if messages else type(exc).__name__


def _bill_external_id(owner: str, institution: str, mask: str, due_date: str) -> str:
    """Deterministic dedup key: owner:inst:mask:YYYY-MM."""
    # due_date is ISO YYYY-MM-DD
    month = due_date[:7]
    return f"{owner}:{institution}:{mask}:{month}"


@activity.defn
async def register_liability_bills_activity(
    institution_slug: str,
    accounts: List[Dict[str, Any]],
    liability_map: Dict[str, Dict[str, Any]],
    account_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Phase 8.5: derive finance.bill beads from liability data.

    For each account with liability data (next_payment_date + min_payment),
    generate a deterministic external_id. Client-side dedup against
    existing bills for this institution.

    Returns registration counts.
    """
    if not liability_map:
        return {"created": 0, "updated": 0, "skipped": 0}

    # 1. Fetch existing bills to avoid duplicates and backfill account_id on
    # older liability bills that were created before reconciliation used it.
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            substrate_beads_url(),
            params={"namespace": "finance", "type": "bill", "limit": 1000},
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        existing_bills = resp.json()

    # Map external_id -> bill_bead.
    existing_by_external_id = {
        b["content"]["external_id"]: b
        for b in existing_bills
        if b.get("content", {}).get("external_id")
    }

    created = 0
    updated = 0
    skipped = 0

    for acc in accounts:
        plaid_id = acc["account_id"]
        liabilities = liability_map.get(plaid_id)
        if not liabilities:
            continue

        due_date = liabilities.get("next_payment_date")
        amount = liabilities.get("min_payment")
        mask = acc.get("mask") or "0000"
        account_bead_id = (account_map or {}).get(plaid_id)

        if not due_date or not amount or amount <= 0:
            continue

        ext_id = _bill_external_id(DEFAULT_OWNER, institution_slug, mask, due_date)
        existing = existing_by_external_id.get(ext_id)

        if existing:
            if existing["state"] != "pending":
                c = existing.get("content") or {}
                if account_bead_id and not c.get("account_id"):
                    body = {
                        "content": {**c, "account_id": account_bead_id},
                        "created_by": "bank-sync/register_liability_bills",
                    }
                    async with httpx.AsyncClient() as client:
                        patch_resp = await client.patch(
                            f"{substrate_beads_url()}/{existing['id']}",
                            json=body,
                            headers=substrate_headers(),
                        )
                        patch_resp.raise_for_status()
                    updated += 1
                else:
                    skipped += 1
                continue

            # Update if amount or due_date changed.
            c = existing["content"]
            if (
                c.get("amount") != amount
                or c.get("due_date") != due_date
                or (account_bead_id and not c.get("account_id"))
            ):
                updated += 1
                content = {
                    **c,
                    "amount": amount,
                    "due_date": due_date,
                    "account_id": c.get("account_id") or account_bead_id,
                }
                body = {
                    "content": content,
                    "created_by": "bank-sync/register_liability_bills",
                }
                async with httpx.AsyncClient() as client:
                    patch_resp = await client.patch(
                        f"{substrate_beads_url()}/{existing['id']}",
                        json=body,
                        headers=substrate_headers(),
                    )
                    patch_resp.raise_for_status()
            else:
                skipped += 1
        else:
            # Create new pending bill.
            created += 1
            payload = {
                "namespace": "finance",
                "type": "bill",
                "state": "pending",
                "trust_tier": "user",
                "content": {
                    "vendor": acc.get("name", institution_slug),
                    "amount": amount,
                    "due_date": due_date,
                    "category": "interest_fees",  # Default for credit/loans
                    "source": "liability",
                    "owner": DEFAULT_OWNER,
                    "external_id": ext_id,
                    "account_id": account_bead_id,
                },
                "created_by": "bank-sync/register_liability_bills",
            }
            async with httpx.AsyncClient() as client:
                post_resp = await client.post(
                    substrate_beads_url(),
                    json=payload,
                    headers=substrate_headers(),
                )
                post_resp.raise_for_status()

    return {"created": created, "updated": updated, "skipped": skipped}


MERCHANT_NORMALIZATION_RULES = (
    ("Amazon", ("AMAZON", "AMZN")),
    ("Apple", ("APPLE.COM", "APPLE STORE", "APPLE")),
    ("Costco", ("COSTCO",)),
    ("DoorDash", ("DOORDASH", "DD DOORDASH")),
    ("Instacart", ("INSTACART",)),
    ("Jewel-Osco", ("JEWEL", "JEWEL OSCO")),
    ("Mariano's", ("MARIANO",)),
    ("Netflix", ("NETFLIX",)),
    ("Shell", ("SHELL",)),
    ("Starbucks", ("STARBUCKS",)),
    ("Target", ("TARGET",)),
    ("Uber Eats", ("UBER EATS", "UBEREATS")),
    ("Uber", ("UBER",)),
    ("Walmart", ("WAL-MART", "WALMART")),
    ("Whole Foods", ("WHOLEFDS", "WHOLE FOODS")),
)

FLOW_VALUES = ("spend", "income", "refund", "transfer", "financing", "undetermined")

_FLOW_TRANSFER_MARKERS = (
    "autopay",
    "auto pay",
    "electronic transfer",
    "online payment",
    "online transfer",
    "ach transfer",
    "cc pymt",
)
_FLOW_CARD_PAYMENT_MARKERS = ("payment", "pymt", "thank you")
_FLOW_FINANCIAL_ACCOUNT_MARKERS = (
    "amex",
    "american express",
    "bank",
    "capital one",
    "card",
    "chase",
    "citi",
    "credit",
    "discover",
    "mastercard",
    "visa",
)
_FLOW_INCOME_MARKERS = (
    "payroll",
    "paycheck",
    "salary",
    "direct deposit",
    "dir dep",
    "paychex",
    "adp wage",
    "wages",
)
_FLOW_REFUND_MARKERS = (
    "refund",
    "returned purchase",
    "merchandise return",
    "return credit",
    "credit voucher",
    "merchant credit",
)
_FLOW_FINANCING_MARKERS = (
    "loan advance",
    "loan disbursement",
    "loan payment",
    "mortgage payment",
    "cash advance",
    "financing",
    "principal advance",
    "principal payment",
)


def _transaction_flow_text(transaction: Dict[str, Any]) -> str:
    parts = []
    for key in ("merchant_name", "merchant", "normalized_merchant", "name", "description"):
        value = transaction.get(key)
        if value:
            parts.append(str(value))
    personal_finance_category = transaction.get("personal_finance_category")
    if isinstance(personal_finance_category, dict):
        for key in ("primary", "detailed"):
            value = personal_finance_category.get(key)
            if value:
                parts.append(str(value).replace("_", " "))
    return " ".join(parts).lower()


def normalize_transaction_flow(flow: Any) -> str:
    value = str(flow or "").strip().lower().replace("-", "_").replace(" ", "_")
    return value if value in FLOW_VALUES else "undetermined"


def classify_transaction_flow(transaction: Dict[str, Any]) -> str:
    """Classify what moved independently from what it bought.

    Plaid amounts are positive for money leaving the account and negative for
    money coming in. A positive amount with merchant text is spend unless a
    stronger movement marker says otherwise; ambiguous inflows stay explicit.
    """
    text = _transaction_flow_text(transaction)
    try:
        amount = float(transaction.get("amount"))
    except (TypeError, ValueError):
        return "undetermined"

    if not text or amount == 0:
        return "undetermined"
    if any(marker in text for marker in _FLOW_FINANCING_MARKERS):
        return "financing"
    if any(marker in text for marker in _FLOW_TRANSFER_MARKERS):
        return "transfer"
    if (
        any(marker in text for marker in _FLOW_CARD_PAYMENT_MARKERS)
        and any(marker in text for marker in _FLOW_FINANCIAL_ACCOUNT_MARKERS)
    ):
        return "transfer"
    if amount < 0:
        if any(marker in text for marker in _FLOW_INCOME_MARKERS):
            return "income"
        if any(marker in text for marker in _FLOW_REFUND_MARKERS):
            return "refund"
        return "undetermined"
    if any(marker in text for marker in _FLOW_REFUND_MARKERS):
        return "refund"
    return "spend"


def transaction_spend_amount_by_flow(transaction_or_bead: Dict[str, Any]) -> float:
    content = transaction_or_bead.get("content")
    if not isinstance(content, dict):
        content = transaction_or_bead
    flow = normalize_transaction_flow(content.get("flow"))
    try:
        amount = float(content.get("amount"))
    except (TypeError, ValueError):
        return 0.0
    if flow == "spend":
        return amount
    if flow == "refund":
        return -abs(amount)
    return 0.0


def spend_total_by_flow(transactions: List[Dict[str, Any]]) -> float:
    return sum(transaction_spend_amount_by_flow(t) for t in transactions)


def normalize_merchant_name(raw_name: Optional[str]) -> Optional[str]:
    if not raw_name:
        return None

    merchant = re.sub(r"\s+", " ", raw_name).strip()
    if not merchant:
        return None

    upper = merchant.upper()
    for canonical, markers in MERCHANT_NORMALIZATION_RULES:
        if any(marker in upper for marker in markers):
            return canonical

    # Remove common processor/location/reference suffixes while preserving
    # readable local merchant names.
    cleaned = re.sub(r"^[A-Z]{2}\s*\*\s*", "", merchant, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_*#")
    cleaned = re.sub(r"(?:[-_*#\s]+\d{3,}.*)$", "", cleaned).strip(" -_*#")
    return cleaned.title() if cleaned else merchant.title()


def transaction_fingerprint(
    account_bead_id: str,
    amount: Any,
    posted_date: Optional[str],
    description: Optional[str],
) -> str:
    """Identity of a transaction WITHOUT its plaid_transaction_id.

    A create-mode re-link mints a new Plaid Item and re-issues every
    transaction_id, so the id-based dedup (existing-ids set + DB unique
    index) sees the re-imported history as brand new — observed
    2026-06-12: the 06-11 re-links duplicated 150 transactions. The
    fingerprint keys on what survives a re-link: the account BEAD id
    (stable across re-links thanks to _match_existing_account), amount,
    posted date, and the institution's raw description string.
    """
    return "|".join([
        str(account_bead_id),
        f"{float(amount):.2f}",
        str(posted_date or ""),
        (description or "").strip().lower(),
    ])


# --- Activities ---

@activity.defn
async def list_accounts_activity(institution_slug: str) -> List[Dict[str, Any]]:
    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        logger.warning(
            "No Plaid access token for %s (env %s). Skipping sync — link it "
            "via the portal (/finance/link/%s) and save the token to Infisical.",
            institution_slug, token_env, institution_slug,
        )
        return []

    accounts = await get_accounts(access_token)
    for a in accounts:
        a["institution"] = institution_slug
    return accounts

@activity.defn
async def pull_transactions_activity(institution_slug: str, since: str, until: str) -> List[Dict[str, Any]]:
    # Activity args travel through JSON; pass ISO date strings and parse here.
    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        logger.warning(
            "No Plaid access token for %s (env %s). Returning zero transactions.",
            institution_slug, token_env,
        )
        return []

    since_date = date.fromisoformat(since)
    until_date = date.fromisoformat(until)
    transactions = await get_transactions(access_token, since_date, until_date)
    for t in transactions:
        t["institution"] = institution_slug
    return transactions

@activity.defn
async def fetch_existing_transaction_ids_activity() -> List[str]:
    """Fetches all known plaid_transaction_id values from Substrate."""
    beads_url = substrate_beads_url()
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch a large window of recent transactions to dedup against.
        # v1 limit 5000 is enough for a 90-day backfill across multiple banks.
        resp = await client.get(
            beads_url,
            params={"namespace": "finance", "type": "transaction", "limit": 5000},
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        return [
            b["content"].get("plaid_transaction_id")
            for b in resp.json()
            if b.get("content", {}).get("plaid_transaction_id")
        ]


@activity.defn
async def fetch_existing_fingerprint_counts_activity(
    institution_slug: str,
) -> Dict[str, int]:
    """Multiset of transaction fingerprints already stored for an institution.

    Consumed only on INITIAL syncs (no stored cursor — i.e. a fresh or
    re-linked Item, where Plaid replays full history under new
    transaction ids). Multiset semantics keep genuinely repeated charges
    honest: two real identical charges occupy two fingerprint slots, so
    a re-import of both is skipped twice while a third NEW identical
    charge still lands.
    """
    counts: Dict[str, int] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            substrate_beads_url(),
            params={"namespace": "finance", "type": "transaction", "limit": 5000},
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        for b in resp.json():
            c = b.get("content") or {}
            if c.get("institution") != institution_slug:
                continue
            if b.get("state") == "removed":
                continue
            fp = transaction_fingerprint(
                c.get("account_id"),
                c.get("amount") or 0.0,
                c.get("posted_date"),
                c.get("description") or c.get("merchant_name"),
            )
            counts[fp] = counts.get(fp, 0) + 1
    return counts


# --- /transactions/sync activities (cursor-based nightly sync) -----------
#
# Replaces the 90-day /transactions/get window: the cursor hands us exactly
# the changes since the last run (no truncation at 500, no date math) and
# surfaces removals, which the window pull never could. The cursor lives on
# the institution's finance.plaid_item bead (Item registry, PR 3).


async def _transaction_beads_by_plaid_id() -> Dict[str, Dict[str, Any]]:
    """Map plaid_transaction_id -> bead over recent transaction beads.

    Client-side, like every content lookup here — plaid_transaction_id is
    a PLAINTEXT_KEY but Substrate has no JSONB-content query yet.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            substrate_beads_url(),
            params={"namespace": "finance", "type": "transaction", "limit": 5000},
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        beads = resp.json()
    return {
        (b.get("content") or {}).get("plaid_transaction_id"): b
        for b in beads
        if (b.get("content") or {}).get("plaid_transaction_id")
    }


@activity.defn
async def ensure_item_registry_activity(institution_slug: str) -> Dict[str, Any]:
    """Resolve ``{"bead_id", "cursor"}`` from the institution's
    ``finance.plaid_item`` bead.

    Self-heals institutions linked before the Item registry existed:
    when no bead matches (institution, PLAID_ENV), item_id is recovered
    via /item/get and register_plaid_item creates the bead. bead_id is
    None only when the institution has no access token at all.
    """
    env = os.environ.get("PLAID_ENV", "sandbox")
    beads = await finance_query_beads({"type": "plaid_item", "limit": 100})
    match = next(
        (
            b
            for b in beads
            if (b.get("content") or {}).get("institution") == institution_slug
            and (b.get("content") or {}).get("plaid_env") == env
        ),
        None,
    )
    if match is not None:
        return {
            "bead_id": match["id"],
            "cursor": (match.get("content") or {}).get("transactions_cursor"),
        }

    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        return {"bead_id": None, "cursor": None}

    item_payload = await get_item_status(access_token)
    item_id = (item_payload.get("item") or {}).get("item_id")
    if not item_id:
        logger.warning(
            "ensure_item_registry: /item/get returned no item_id for %s",
            institution_slug,
        )
        return {"bead_id": None, "cursor": None}

    registered = await register_plaid_item(
        institution_slug,
        item_id,
        env,
        created_by="bank-sync/ensure_item_registry",
    )
    return {"bead_id": registered["bead_id"], "cursor": None}


@activity.defn
async def sync_transactions_page_activity(
    institution_slug: str, cursor: Optional[str] = None
) -> Dict[str, Any]:
    """One /transactions/sync page: pending entries filtered out and
    ``institution`` stamped on each change — the same shaping
    pull_transactions_activity did for the date-window path.
    """
    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        logger.warning(
            "No Plaid access token for %s (env %s). Returning empty sync page.",
            institution_slug, token_env,
        )
        return {
            "added": [], "modified": [], "removed": [],
            "next_cursor": cursor, "has_more": False,
        }

    page = await sync_transactions_page(access_token, cursor)
    for key in ("added", "modified"):
        kept = []
        for t in page[key]:
            if t.get("pending"):
                continue
            t["institution"] = institution_slug
            kept.append(t)
        page[key] = kept
    return page


@activity.defn
async def store_sync_cursor_activity(bead_id: str, cursor: str) -> None:
    """Persist next_cursor on the plaid_item bead AFTER a page's writes.

    Plaid's contract: never advance the stored cursor past data you
    haven't durably applied — a crash then replays the page, and the
    existing-ids set + DB unique index make the replay a no-op.
    Substrate PATCH replaces content wholesale, so re-read and merge.
    """
    beads = await finance_query_beads({"type": "plaid_item", "limit": 100})
    bead = next((b for b in beads if b["id"] == bead_id), None)
    if bead is None:
        logger.warning("store_sync_cursor: plaid_item bead %s not found", bead_id)
        return
    content = {
        **(bead.get("content") or {}),
        "transactions_cursor": cursor,
        "last_synced": datetime.now().isoformat(),
    }
    await patch_bead(bead_id, content=content, created_by="bank-sync/store_cursor")


@activity.defn
async def apply_removed_transactions_activity(
    removed: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Transition beads for Plaid-removed transactions to state=removed.

    Entries carry ``{"transaction_id": ...}``. Unknown ids (e.g. a pending
    transaction we filtered and never wrote) are counted and skipped.
    """
    ids = {r.get("transaction_id") for r in removed if r.get("transaction_id")}
    if not ids:
        return {"removed": 0, "unknown": 0}

    by_plaid_id = await _transaction_beads_by_plaid_id()
    removed_count = 0
    unknown = 0
    for tx_id in ids:
        bead = by_plaid_id.get(tx_id)
        if bead is None:
            unknown += 1
            continue
        if bead.get("state") == "removed":
            continue
        await patch_bead_state(
            bead["id"], state="removed", created_by="bank-sync/apply_removed",
        )
        removed_count += 1
    return {"removed": removed_count, "unknown": unknown}


@activity.defn
async def apply_modified_transactions_activity(
    modified: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Refresh amount/date/merchant on beads Plaid reports as modified.

    pending→posted flows arrive as removed+added, so true modifies are
    rare. Unknown ids are counted and skipped, not created — a pending
    entry filtered on an earlier page arrives later as added.
    """
    if not modified:
        return {"modified": 0, "unknown": 0}

    by_plaid_id = await _transaction_beads_by_plaid_id()
    modified_count = 0
    unknown = 0
    for t in modified:
        bead = by_plaid_id.get(t.get("transaction_id"))
        if bead is None:
            unknown += 1
            continue
        merchant_name = t.get("merchant_name") or t.get("name")
        content = {
            **(bead.get("content") or {}),
            "amount": t["amount"],
            "posted_date": t["date"],
            "authorized_date": t.get("authorized_date"),
            "merchant_name": merchant_name,
            "merchant": merchant_name,
            "normalized_merchant": normalize_merchant_name(merchant_name) or merchant_name or "",
            "description": t["name"],
        }
        await patch_bead(
            bead["id"], content=content, created_by="bank-sync/apply_modified",
        )
        modified_count += 1
    return {"modified": modified_count, "unknown": unknown}


@activity.defn
async def categorize_transaction_activity(merchant: str, amount: float, description: str) -> Dict[str, Any]:
    """LLM-categorize a single transaction.

    Phase 6 (Part 4): the prompt also detects credit-card payments,
    auto-pays, and electronic transfers. Those return
    ``{"category": "transfer", "is_transfer": True}`` so the wealth-flow
    layer can pair the outflow (checking) with the inflow (credit card
    statement balance reduction) instead of double-counting them as
    spending.

    Returns ``{"category": Optional[str], "is_transfer": bool,
    "flow": str}``. The category may be ``None`` if the LLM call failed
    or returned an out-of-vocabulary token — callers must treat it as
    missing, not as an opt-out from validation.
    """
    import time

    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    model = LIFEOPS_DEFAULT_MODEL
    flow = classify_transaction_flow(
        {"merchant_name": merchant, "name": description, "description": description, "amount": amount}
    )

    # Phase 6: include "transfer" in the category vocabulary so the LLM
    # can return it directly. We treat transfer as a first-class label
    # rather than overloading "misc" — wealth math needs them excluded.
    categories_str = ", ".join(FINANCE_CATEGORIES + ["transfer"])
    from src.untrusted import wrap_untrusted, wrap_untrusted_short

    prompt = f"""
You are categorizing a single bank transaction into one of these categories:
{categories_str}

Transaction:
- Merchant: {wrap_untrusted_short(merchant)}
- Description: {wrap_untrusted(description)}
- Amount: ${amount}

Rules:
- Coffee shops, restaurants, fast food → dining
- Supermarkets (Mariano's, Whole Foods, Costco food) → groceries
- Gas, parking, car repair → auto
- Streaming services, concerts, games → entertainment
- Medical, pharmacy, dental → health
- Rent, mortgage, home repair, lawn → housing
- Internet, electric, gas, water, phone → utilities
- Daycare, kids' activities, kids' clothes → kids
- Credit-card interest, finance charges, late fees, overdraft fees →
  interest_fees. These are NOT transfers, even when the merchant is a
  credit-card issuer.
- If the merchant is a financial institution (bank, credit card issuer,
  brokerage) AND the description contains 'Payment', 'Auto-Pay',
  'Autopay', 'AUTOPAY', 'Electronic Transfer', 'ACH', 'Transfer',
  'CC PYMT', 'Online Payment' → transfer
- Anything else → misc

Respond with ONLY the category name (one word). No explanation.
"""

    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }

    started = time.monotonic()
    try:
        resp = await post_chat_completion(payload, headers=headers, timeout=30.0)
        latency_ms = (time.monotonic() - started) * 1000.0
        if resp.status_code != 200:
            # Still record the spend (zero usage) so failed calls are visible
            # in the dashboard. No category returned.
            await log_llm_cost(
                model=model,
                usage=None,
                agent="bank-sync/categorize",
                # NB: merchant/amount/description are PII — keep them out
                # of the cost-bead context. redact_context defends if a
                # future caller forgets, but the contract is "tags only".
                context={"http_status": resp.status_code, "outcome": "http_error"},
                workflow_id=_current_workflow_id(),
                latency_ms=latency_ms,
                prompt_hash_value=prompt_hash(prompt),
            )
            if resp.status_code == 429:
                resp.raise_for_status()
            return {"category": None, "is_transfer": False, "flow": flow}

        body = resp.json()
        raw = body["choices"][0]["message"]["content"].strip().lower()
        raw = raw.replace("/", "_").replace(" ", "_").replace("-", "_")
        is_transfer = raw == "transfer"
        cat = raw if raw in FINANCE_CATEGORIES else ("transfer" if is_transfer else None)
        await log_llm_cost(
            model=model,
            usage=body.get("usage"),
            agent="bank-sync/categorize",
            context={
                "outcome": "categorized" if cat else "invalid_category",
                "is_transfer": is_transfer,
            },
            workflow_id=_current_workflow_id(),
            latency_ms=latency_ms,
            prompt_hash_value=prompt_hash(prompt),
            cost_usd=response_cost_usd(resp),
        )
        return {"category": cat, "is_transfer": is_transfer, "flow": flow}
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            raise
        latency_ms = (time.monotonic() - started) * 1000.0
        outcome = (
            "provider_retired"
            if isinstance(exc, LiteLLMProviderRetiredError)
            else "exception"
        )
        await log_llm_cost(
            model=model,
            usage=None,
            agent="bank-sync/categorize",
            context={"outcome": outcome, "exc_type": type(exc).__name__},
            workflow_id=_current_workflow_id(),
            latency_ms=latency_ms,
            prompt_hash_value=prompt_hash(prompt),
        )
        return {"category": None, "is_transfer": False, "flow": flow}


@activity.defn
async def categorize_transactions_bulk_activity(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bulk categorize transactions using rules, Naive Bayes local ML, and Gemini fallback.

    Returns a list of dicts: [{"category": str|None, "is_transfer": bool, "flow": str, "categorization_source": str, "categorization_rule_id": str|None, "categorization_confidence": float|None}]
    """
    import math
    import time
    from collections import Counter, defaultdict
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.workflows.finance_rule_apply import resolve_field, build_matcher, transaction_matches

    # 1. Fetch active rules from Substrate
    rules = []
    try:
        rule_beads = await finance_query_beads({"type": "rule", "limit": 500})
        rules = [r for r in rule_beads if r.get("state") in ("active", "applied")]
    except Exception as exc:
        logger.warning("Failed to fetch rules from Substrate: %s", exc)

    compiled_rules = []
    for r in rules:
        content = r.get("content", {})
        field = content.get("field")
        operator = content.get("operator")
        value = content.get("value")
        target_category = content.get("target_category")
        if field and operator and value and target_category:
            try:
                matcher = build_matcher(operator, value)
                compiled_rules.append({
                    "bead_id": r.get("id"),
                    "field": resolve_field(field),
                    "matcher": matcher,
                    "target_category": target_category
                })
            except Exception as exc:
                logger.warning("Failed to compile rule %s: %s", r.get("id"), exc)

    # 2. Fetch historical transactions to train Naive Bayes
    priors = {}
    word_counts = defaultdict(Counter)
    vocab = set()
    total_train = 0
    alpha = 0.1
    vocab_size = 0

    def tokenize(text):
        if not text:
            return []
        return [w for w in re.findall(r"\b\w+\b", text.lower()) if len(w) > 1]

    valid_categories = set(FINANCE_CATEGORIES + ["transfer"])

    try:
        historical_beads = await finance_query_beads({"type": "transaction", "limit": 2000})
        cat_counts = Counter()
        for t in historical_beads:
            content = t.get("content", {})
            merchant = content.get("merchant_name") or content.get("name")
            raw_cat = content.get("our_category")
            cat = ""
            if raw_cat:
                cat = str(raw_cat).strip().lower()
                cat = cat.replace("/", "_").replace(" ", "_").replace("-", "_")
            if cat in valid_categories and merchant:
                cat_counts[cat] += 1
                tokens = tokenize(merchant)
                for token in tokens:
                    word_counts[cat][token] += 1
                    vocab.add(token)

        total_train = sum(cat_counts.values())
        if total_train > 0:
            priors = {cat: count / total_train for cat, count in cat_counts.items()}
            vocab_size = len(vocab)
    except Exception as exc:
        logger.warning("Failed to fetch historical transactions or train Naive Bayes: %s", exc)

    def predict_local(merchant_name):
        if not priors or not word_counts:
            return None, 0.0
        tokens = tokenize(merchant_name)
        if not tokens:
            return None, 0.0

        log_probs = {}
        for cat in priors:
            log_prob = math.log(priors[cat])
            cat_total_words = sum(word_counts[cat].values())
            for token in tokens:
                count = word_counts[cat].get(token, 0)
                prob = (count + alpha) / (cat_total_words + alpha * vocab_size)
                log_prob += math.log(prob)
            log_probs[cat] = log_prob

        # Softmax
        max_log = max(log_probs.values())
        exps = {cat: math.exp(lp - max_log) for cat, lp in log_probs.items()}
        sum_exps = sum(exps.values())
        probs = {cat: exp / sum_exps for cat, exp in exps.items()}

        best_cat = max(probs, key=probs.get)
        return best_cat, probs[best_cat]

    # 3. Categorize each transaction
    results = []
    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    model = LIFEOPS_DEFAULT_MODEL
    categories_str = ", ".join(FINANCE_CATEGORIES + ["transfer"])

    for t in transactions:
        merchant = t.get("merchant_name") or t.get("name") or ""
        amount = t.get("amount", 0.0)
        description = t.get("name") or ""
        flow = classify_transaction_flow(t)

        # A. Rule matching
        matched_category = None
        matched_rule_id = None
        for rule in compiled_rules:
            mock_content = {
                "merchant_name": merchant,
                "normalized_merchant": merchant,
                "description": description,
                "vendor": merchant
            }
            if transaction_matches(mock_content, rule["field"], rule["matcher"]):
                matched_category = rule["target_category"]
                matched_rule_id = rule["bead_id"]
                break

        if matched_category:
            is_transfer = (matched_category == "transfer")
            results.append({
                "category": matched_category,
                "is_transfer": is_transfer,
                "flow": flow,
                "categorization_source": "rule",
                "categorization_rule_id": matched_rule_id,
                "categorization_confidence": 1.0
            })
            continue

        # B. Naive Bayes Local ML
        predicted_category, confidence = predict_local(merchant)
        if predicted_category and confidence >= 0.85:
            is_transfer = (predicted_category == "transfer")
            results.append({
                "category": predicted_category,
                "is_transfer": is_transfer,
                "flow": flow,
                "categorization_source": "local_ml/naive_bayes",
                "categorization_rule_id": None,
                "categorization_confidence": confidence
            })
            continue

        # C. Cloud LLM Fallback
        from src.untrusted import wrap_untrusted, wrap_untrusted_short

        prompt = f"""
You are categorizing a single bank transaction into one of these categories:
{categories_str}

Transaction:
- Merchant: {wrap_untrusted_short(merchant)}
- Description: {wrap_untrusted(description)}
- Amount: ${amount}

Rules:
- Coffee shops, restaurants, fast food → dining
- Supermarkets (Mariano's, Whole Foods, Costco food) → groceries
- Gas, parking, car repair → auto
- Streaming services, concerts, games → entertainment
- Medical, pharmacy, dental → health
- Rent, mortgage, home repair, lawn → housing
- Internet, electric, gas, water, phone → utilities
- Daycare, kids' activities, kids' clothes → kids
- Credit-card interest, finance charges, late fees, overdraft fees →
  interest_fees. These are NOT transfers, even when the merchant is a
  credit-card issuer.
- If the merchant is a financial institution (bank, credit card issuer,
  brokerage) AND the description contains 'Payment', 'Auto-Pay',
  'Autopay', 'AUTOPAY', 'Electronic Transfer', 'ACH', 'Transfer',
  'CC PYMT', 'Online Payment' → transfer
- Anything else → misc

Respond with ONLY the category name (one word). No explanation.
"""
        headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }

        category = None
        is_transfer = False
        categorization_source = "litellm/failed"
        categorization_confidence = 0.0
        started = time.monotonic()

        for attempt in range(3):
            try:
                resp = await post_chat_completion(payload, headers=headers, timeout=30.0)
                latency_ms = (time.monotonic() - started) * 1000.0
                if resp.status_code == 200:
                    choice = resp.json()["choices"][0]["message"]["content"].strip().lower()
                    choice = choice.replace("/", "_").replace(" ", "_").replace("-", "_")
                    if choice in valid_categories:
                        category = choice
                        is_transfer = (choice == "transfer")
                        categorization_source = f"litellm/{model}"
                        categorization_confidence = None
                    else:
                        category = "misc"
                        is_transfer = False
                        categorization_source = "litellm/unparsed"
                        categorization_confidence = 0.0

                    await log_llm_cost(
                        model=model,
                        usage=resp.json().get("usage"),
                        agent="bank-sync/categorize",
                        context={
                            "outcome": "success" if choice in valid_categories else "unparsed",
                        },
                        workflow_id=_current_workflow_id(),
                        latency_ms=latency_ms,
                        prompt_hash_value=prompt_hash(prompt),
                        cost_usd=response_cost_usd(resp),
                    )
                    break
                else:
                    logger.warning("LiteLLM returned status code %s: %s", resp.status_code, resp.text)
                    await log_llm_cost(
                        model=model,
                        usage=None,
                        agent="bank-sync/categorize",
                        context={"http_status": resp.status_code, "outcome": "http_error"},
                        workflow_id=_current_workflow_id(),
                        latency_ms=latency_ms,
                        prompt_hash_value=prompt_hash(prompt),
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    break
            except LiteLLMProviderRetiredError as exc:
                # Permanent, not transient — retrying a revoked provider on a
                # backoff schedule would turn a bulk sync of thousands of
                # transactions into a multi-hour stall for no benefit. Log
                # once (named reason, no traceback) and stop immediately.
                logger.warning("LiteLLM call skipped: %s", exc)
                categorization_source = "litellm/retired"
                break
            except Exception as exc:
                logger.warning("LiteLLM call failed (attempt %s): %s", attempt, exc)
                await asyncio.sleep(2.0 * (attempt + 1))

        results.append({
            "category": category,
            "is_transfer": is_transfer,
            "flow": flow,
            "categorization_source": categorization_source,
            "categorization_rule_id": None,
            "categorization_confidence": categorization_confidence
        })

    return results


def _liability_from_credit(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Map Plaid CreditCardLiability -> our liabilities schema (Phase 6)."""
    aprs = entry.get("aprs") or []
    apr = None
    if aprs:
        # Prefer the purchase APR if labelled; else fall back to first.
        for a in aprs:
            if a.get("apr_type", "").lower().startswith("purchase"):
                apr = a.get("apr_percentage")
                break
        if apr is None:
            apr = aprs[0].get("apr_percentage")
    return {
        "apr": apr,
        "min_payment": entry.get("minimum_payment_amount"),
        "principal": entry.get("last_statement_balance"),
        "due_date": entry.get("next_payment_due_date"),
        "next_payment_date": entry.get("next_payment_due_date"),
    }


def _liability_from_mortgage(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "apr": (entry.get("interest_rate") or {}).get("percentage"),
        "min_payment": entry.get("next_monthly_payment"),
        "principal": entry.get("origination_principal_amount"),
        "due_date": entry.get("next_payment_due_date"),
        "next_payment_date": entry.get("next_payment_due_date"),
    }


def _liability_from_student(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "apr": entry.get("interest_rate_percentage"),
        "min_payment": entry.get("minimum_payment_amount"),
        "principal": entry.get("origination_principal_amount"),
        "due_date": entry.get("next_payment_due_date"),
        "next_payment_date": entry.get("next_payment_due_date"),
    }


@activity.defn
async def fetch_liabilities_activity(institution_slug: str) -> Dict[str, Dict[str, Any]]:
    """Phase 6: pull liabilities once per institution and return a map
    ``{plaid_account_id: liability_dict}`` keyed by Plaid account id.

    Returns an empty dict if the access token is missing OR the
    institution doesn't expose the liabilities product — both are
    expected for plain checking-only items, so we don't alert.
    """
    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        return {}

    try:
        payload = await get_liabilities(access_token)
    except Exception as exc:
        # Phase 6 resilience: if the item doesn't have the liabilities
        # product enabled (e.g. sandbox item created before the code
        # change), log and return empty rather than crashing the sync.
        error_code = _plaid_error_code_from_exception(exc)
        if error_code == "ADDITIONAL_CONSENT_REQUIRED":
            logger.info("Liabilities product not enabled for %s; skipping.", institution_slug)
            return {}
        logger.warning("fetch_liabilities_activity failed for %s: %s", institution_slug, exc)
        return {}

    liabilities_block = payload.get("liabilities") or {}

    result: Dict[str, Dict[str, Any]] = {}
    for entry in liabilities_block.get("credit") or []:
        acct = entry.get("account_id")
        if acct:
            result[acct] = _liability_from_credit(entry)
    for entry in liabilities_block.get("mortgage") or []:
        acct = entry.get("account_id")
        if acct:
            result[acct] = _liability_from_mortgage(entry)
    for entry in liabilities_block.get("student") or []:
        acct = entry.get("account_id")
        if acct:
            result[acct] = _liability_from_student(entry)
    return result


def _match_existing_account(
    beads: List[Dict[str, Any]],
    *,
    persistent_id: Optional[str],
    institution: str,
    mask: Optional[str],
    plaid_id: str,
) -> Optional[Dict[str, Any]]:
    """Resolve the existing account bead for an incoming Plaid account by a
    STABLE identity, most → least durable. Returns the matched bead or None.

    The lookup order is what stops re-link duplication: Plaid re-issues
    ``account_id`` whenever an Item is re-linked, so keying only on it (the
    old behaviour) created a fresh bead every re-link.

      Priority 1 — ``persistent_account_id``: Plaid's stable identifier
                   that survives re-link across Items. Authoritative when
                   the institution provides it.
      Priority 2 — ``(institution, mask)``: the real-world account key,
                   also enforced by the DB unique index (substrate
                   migration 0003). Catches re-links where the institution
                   omits ``persistent_account_id``.
      Priority 3 — ``plaid_account_id``: legacy match for beads written
                   before we stored a stable id.

    Note: matching runs over the *decrypted* content returned by GET
    /beads, so it works even though most content values are encrypted at
    rest (institution/mask are plaintext; persistent_account_id is not,
    but decrypts here).
    """
    # Priority 1: stable persistent_account_id.
    if persistent_id:
        for b in beads:
            if b["content"].get("persistent_account_id") == persistent_id:
                return b
    # Priority 2: (institution, mask) composite — the unique-index key.
    if mask is not None:
        for b in beads:
            c = b["content"]
            if c.get("institution") == institution and c.get("mask") == mask:
                return b
    # Priority 3: legacy current plaid_account_id.
    for b in beads:
        if b["content"].get("plaid_account_id") == plaid_id:
            return b
    return None


@activity.defn
async def write_account_bead_activity(
    account_info: Dict[str, Any],
    liability_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Create or refresh a ``finance.account`` bead, keyed on a STABLE identity.

    Phase 6 changes:
      * Accepts optional ``liability_data`` from
        :func:`fetch_liabilities_activity` and merges it under
        ``content.liabilities`` (matches FinanceAccountContent schema).
      * Uses the PATCH-with-content endpoint to REFRESH balances +
        liabilities on re-sync. Account beads are the source of truth for
        net-worth math, so they must reflect the latest snapshot.

    Phase 6.5 (duplication fix):
      * Resolves the existing bead via :func:`_match_existing_account`
        (persistent_account_id → institution+mask → plaid_account_id)
        instead of plaid_account_id alone, so a Plaid re-link (which
        re-issues account_id) updates the existing bead rather than
        creating a duplicate.
      * On match we REFRESH ``plaid_account_id`` and
        ``persistent_account_id`` to the current values, so the bead
        tracks the latest Item even across re-links.
    """
    beads_url = substrate_beads_url()
    plaid_id = account_info["account_id"]
    persistent_id = account_info.get("persistent_account_id")
    institution = account_info["institution"]
    mask = account_info.get("mask")

    def _build_content(existing_first_synced: Optional[str] = None) -> Dict[str, Any]:
        content: Dict[str, Any] = {
            "institution": institution,
            "plaid_account_id": plaid_id,
            # Stable cross-Item id (may be None if the institution omits it).
            "persistent_account_id": persistent_id,
            "name": account_info["name"],
            "type": account_info["type"],
            "subtype": account_info.get("subtype"),
            "mask": mask,
            "current_balance": account_info["balances"]["current"],
            "available_balance": account_info["balances"].get("available"),
            "iso_currency_code": account_info["balances"].get("iso_currency_code"),
            "first_synced": existing_first_synced or datetime.now().isoformat(),
            "last_synced": datetime.now().isoformat(),
        }
        # Drop None values out of the liability dict so the schema's
        # Optional fields aren't reduced to ``null`` noise.
        if liability_data:
            content["liabilities"] = {
                k: v for k, v in liability_data.items() if v is not None
            }
        return content

    async with httpx.AsyncClient() as client:
        # Pull all account beads (Substrate has no JSONB-content query yet)
        # and resolve identity client-side over the decrypted content.
        list_resp = await client.get(
            beads_url,
            params={"namespace": "finance", "type": "account", "limit": 100},
            headers=substrate_headers(),
        )
        list_resp.raise_for_status()
        existing = _match_existing_account(
            list_resp.json(),
            persistent_id=persistent_id,
            institution=institution,
            mask=mask,
            plaid_id=plaid_id,
        )

        if existing is not None:
            existing_first_synced = existing["content"].get("first_synced")
            patch_resp = await client.patch(
                f"{beads_url}/{existing['id']}",
                json={
                    "content": _build_content(existing_first_synced),
                    "created_by": "bank-sync/refresh_account",
                },
                headers=substrate_headers(),
            )
            patch_resp.raise_for_status()
            logger.info(
                "Account %s (%s) matched bead %s; refreshed "
                "(persistent_id=%s, liabilities=%s).",
                account_info["name"], plaid_id, existing["id"],
                bool(persistent_id), bool(liability_data),
            )
            return existing["id"]

        # No match → create a new account bead.
        payload = {
            "namespace": "finance",
            "type": "account",
            "state": "active",
            "content": _build_content(),
            "trust_tier": "system",
            "created_by": "bank-sync/list_accounts",
        }
        post_resp = await client.post(beads_url, json=payload, headers=substrate_headers())
        post_resp.raise_for_status()
        return post_resp.json()["id"]

@activity.defn
async def write_transaction_bead_activity(
    transaction_info: Dict[str, Any],
    account_bead_id: str,
    categorization: Any,
) -> Optional[str]:
    """Write one finance.transaction bead. 409 on the plaid_transaction_id
    unique index is treated as success-noop (returns ``None``) so the
    Phase 6 backfill can replay overlapping chunks safely.

    ``categorization`` is the dict returned by
    :func:`categorize_transaction_activity` — ``{"category": str|None,
    "is_transfer": bool, "flow": str}``. For backward compatibility we
    still accept a bare string (legacy callers); migrate them when
    convenient.
    """
    if not account_bead_id:
        # Defensive mirror of the workflow-level orphan skip: Substrate's
        # FinanceTransactionContent requires a UUID account_id and 422s
        # otherwise, which would burn the retry policy for nothing.
        logger.warning(
            "write_transaction_bead: no account bead for plaid tx %s; skipping.",
            transaction_info.get("transaction_id"),
        )
        return None

    beads_url = substrate_beads_url()
    plaid_id = transaction_info["transaction_id"]
    merchant_name = transaction_info.get("merchant_name") or transaction_info.get("name")

    # Normalize categorization arg shape.
    confidence_supplied = False
    if isinstance(categorization, dict):
        category = categorization.get("category")
        is_transfer = bool(categorization.get("is_transfer"))
        source = categorization.get("categorization_source")
        rule_id = categorization.get("categorization_rule_id")
        confidence = categorization.get("categorization_confidence")
        flow = categorization.get("flow")
        confidence_supplied = "categorization_confidence" in categorization
    else:
        category = categorization  # legacy str|None
        is_transfer = False
        source = None
        rule_id = None
        confidence = None
        flow = None

    if not source:
        # No categorization_source supplied means the caller used the legacy
        # bare-string categorization shape (categorize_transaction_activity's
        # dict return also omits it). LIFEOPS_DEFAULT_MODEL is the only model
        # that path can have requested — no env override exists for it — so
        # this is the actual model, not a guess. See .factory/design.md §4.
        source = f"litellm/{LIFEOPS_DEFAULT_MODEL}" if category else "litellm/failed"
    if confidence is None and not confidence_supplied:
        confidence = 1.0 if category else 0.0
    flow = normalize_transaction_flow(flow) if flow is not None else classify_transaction_flow(transaction_info)

    # Phase 6 schema (apps/substrate/src/schemas.py FinanceTransactionContent):
    # required keys = amount, iso_currency_code, merchant_name,
    # normalized_merchant, posted_date, plaid_transaction_id, is_transfer,
    # account_id. authorized_date optional. flow is an independent B1 money
    # movement dimension and must not be inferred from our_category.
    content = {
        "institution": transaction_info["institution"],
        "plaid_transaction_id": plaid_id,
        "account_id": account_bead_id,
        # Keep account_bead_id alongside for one release so any downstream
        # consumer that hasn't migrated to account_id keeps working.
        "account_bead_id": account_bead_id,
        "amount": transaction_info["amount"],
        "iso_currency_code": transaction_info.get("iso_currency_code") or "USD",
        "merchant_name": merchant_name,
        # Legacy key — preserved so existing dashboards / queries that
        # read content.merchant don't have to migrate this release.
        "merchant": merchant_name,
        "normalized_merchant": normalize_merchant_name(merchant_name) or merchant_name or "",
        "description": transaction_info["name"],
        "posted_date": transaction_info["date"],
        "authorized_date": transaction_info.get("authorized_date"),
        "is_transfer": is_transfer,
        "flow": flow,
        "our_category": category,
        "categorization_confidence": confidence,
        "categorization_source": source,
        "categorization_rule_id": rule_id,
    }

    payload = {
        "namespace": "finance",
        "type": "transaction",
        "state": "posted",
        "content": content,
        "trust_tier": "system",
        "created_by": "bank-sync/pull_transactions",
    }

    async with httpx.AsyncClient() as client:
        post_resp = await client.post(beads_url, json=payload, headers=substrate_headers())
        # 409 = duplicate plaid_transaction_id (substrate migration 0002).
        # That's expected during Phase 6 backfill chunk overlap and during
        # nightly-sync retries — treat as success-noop.
        if post_resp.status_code == 409:
            return None
        post_resp.raise_for_status()
        return post_resp.json()["id"]

# --- Phase 5 helpers: Discord + Substrate query/patch ---
# _post_discord lives in src/tools/notify.py since BG-S1 (imported above).


def _public_link_portal_url(institution_slug: str, *, update: bool = False) -> str:
    """URL of the re-auth portal we built in openapi_app.finance_link_portal.

    ``update=True`` targets Plaid Link's update mode, which repairs the
    existing Item in place (token stays valid — no Infisical edit, no pod
    restart). Create mode mints a NEW Item and orphans the broken one.
    """
    base = os.environ.get("MCP_HUB_PUBLIC_URL", "https://mcp-hub.example.org").rstrip("/")
    url = f"{base}/finance/link/{institution_slug}"
    return f"{url}?mode=update" if update else url


# _REAUTH_ERROR_CODES / _TOKEN_REPAIR_ERROR_CODES /
# _plaid_error_code_from_exception moved to src.tools.plaid (imported above).


async def _alert_plaid_reauth_required(
    institution_slug: str,
    error_code: str,
) -> None:
    # Repairable codes (ITEM_LOGIN_REQUIRED, PENDING_EXPIRATION, …) get the
    # update-mode portal: one click, existing token stays valid. The token-
    # repair-only codes (INVALID_ACCESS_TOKEN, ITEM_NOT_FOUND) mean the Item
    # is gone — update mode can't help, so fall back to a fresh create-mode
    # link (which does require saving the new token to Infisical).
    repairable = error_code in _REAUTH_ERROR_CODES
    link_url = _public_link_portal_url(institution_slug, update=repairable)
    action = "Repair (one click, no token changes)" if repairable else "Re-link (new token)"
    message = (
        f"**Plaid re-auth required** for `{institution_slug}` "
        f"(code: `{error_code}`)."
    )
    await send_alert(
        _alert_policy(),
        "plaid.reauth_required",
        hashlib.sha256(f"{error_code}:{link_url}".encode()).hexdigest()[:16],
        message,
        severity=AlertSeverity.URGENT,
        template_values={
            "institution_slug": institution_slug,
            "action": action,
            "portal_url": link_url,
        },
    )


_SYNC_FAILURE_TYPE = "sync_failure"


async def _open_sync_failure(institution_slug: str) -> Optional[Dict[str, Any]]:
    """The open failure record for this institution in this environment, if any."""
    env = notify_env_label()
    beads = await finance_query_beads(
        {"type": _SYNC_FAILURE_TYPE, "state": "pending", "limit": 200}
    )
    for bead in beads:
        content = bead.get("content") or {}
        if content.get("institution") == institution_slug and content.get("environment") == env:
            return bead
    return None


async def _record_sync_failure(institution_slug: str, error: str) -> None:
    """One standing record per failing institution, however many nights it repeats.

    A Discord message is not a record. Once it scrolls, the failure has left
    nothing that can be counted, queried, or closed — which is why LO-OBS-004's
    correct signal has never stopped this class of problem recurring. The record
    is the thing with a lifecycle; the message is an event that already happened.
    """
    env = notify_env_label()
    now = datetime.now(timezone.utc).isoformat()
    existing = await _open_sync_failure(institution_slug)

    if existing is not None:
        prior = dict(existing.get("content") or {})
        prior.update({
            "last_observed_at": now,
            "last_error": error[:400],
            "occurrences": int(prior.get("occurrences") or 1) + 1,
        })
        await patch_bead(
            existing["id"],
            content=prior,
            created_by="bank-sync/refresh_sync_failure",
        )
        return

    await create_bead(
        _SYNC_FAILURE_TYPE,
        {
            "institution": institution_slug,
            # Reuses the one mechanism that already decides what environment this
            # is (LO-OBS-005). A second way of answering that question is how a
            # dev pod's sandbox failure gets mistaken for a real account problem.
            "environment": env,
            "attempted": "bank sync",
            "first_observed_at": now,
            "last_observed_at": now,
            "last_error": error[:400],
            "occurrences": 1,
        },
        state="pending",
        created_by="bank-sync/open_sync_failure",
    )


async def resolve_sync_failure(institution_slug: str, *, succeeded_at: str) -> bool:
    """Close the open failure record because a later sync actually worked.

    Returns whether a record was closed. Unlike a drift discrepancy, a sync
    failure IS resolved by evidence the machine can produce on its own — a
    successful run is the explanation.
    """
    existing = await _open_sync_failure(institution_slug)
    if existing is None:
        return False
    content = dict(existing.get("content") or {})
    content.update({"resolved_at": succeeded_at, "resolved_by": "bank-sync/successful-run"})
    await patch_bead(
        existing["id"],
        content=content,
        state="resolved",
        created_by="bank-sync/resolve_sync_failure",
    )
    return True


@activity.defn
async def resolve_sync_failure_activity(institution_slug: str) -> bool:
    """Best-effort close-out after a sync succeeds."""
    try:
        return await resolve_sync_failure(
            institution_slug, succeeded_at=datetime.now(timezone.utc).isoformat()
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail a good sync
        logger.warning("could not resolve sync failure record for %s: %s", institution_slug, exc)
        return False


@activity.defn
async def notify_sync_failure_activity(institution_slug: str, error: str) -> bool:
    """Record a failed BankSyncWorkflow, and alert at most once per policy window.

    Workflow-level crashes were previously silent (re-auth and quality
    issues alert, but an uncaught activity failure told no one —
    observed 2026-06-11 when three first-run syncs died unnoticed).

    LO-OBS-004/AC-1 passed and AC-2 failed: the signal was right and had no
    volume control, because this call site posted directly and bypassed alert
    policy entirely. A persistent failure was worth up to twenty identical
    messages a day.

    Two changes, and the ORDER matters. The record is written first and its
    failure is swallowed, because a bookkeeping outage must never silence a real
    alert — the same fail-open reasoning the suppression helper already carries.
    Then the alert goes through the shared alert policy. Nothing new is
    introduced: no channel, no transport, and no schedule.
    """
    try:
        await _record_sync_failure(institution_slug, error)
    except Exception as exc:  # noqa: BLE001 — fail open, exactly like the suppressor
        logger.warning(
            "could not record sync failure for %s (%s); alerting anyway",
            institution_slug, exc,
        )

    return await send_alert(
        _alert_policy(),
        "bank_sync.sync_failure",
        # Collapse on the error, so the same failure repeating is not new
        # information while a DIFFERENT failure still gets through.
        hashlib.sha256(error[:400].encode()).hexdigest()[:16],
        f":x: **Bank sync FAILED** for `{institution_slug}` "
        f"(all {SCHEDULE_RETRY_MAX_ATTEMPTS} attempts exhausted)\n"
        f"```{error[:400]}```",
        severity=AlertSeverity.URGENT,
        template_values={"institution_slug": institution_slug},
    )


_FRESHNESS_ALERT_TYPE = "freshness_alert"

# Same lookback the read-time /finance/connections status page uses
# (tools.connections._FRESHNESS_WINDOW_DAYS) — a second, differently-tuned
# window here would let the persisted verdict and the API disagree about
# what "stale" means for the same institution on the same day.
_FRESHNESS_EVAL_WINDOW_DAYS = 60


async def _open_freshness_alert(institution_slug: str) -> Optional[Dict[str, Any]]:
    """The open freshness-alert record for this institution in this
    environment, if any. Mirrors :func:`_open_sync_failure`."""
    env = notify_env_label()
    beads = await finance_query_beads(
        {"type": _FRESHNESS_ALERT_TYPE, "state": "pending", "limit": 200}
    )
    for bead in beads:
        content = bead.get("content") or {}
        if content.get("institution") == institution_slug and content.get("environment") == env:
            return bead
    return None


async def _record_freshness_alert(
    institution_slug: str,
    *,
    freshness_slo_days: int,
    days_since_last_transaction: int,
    last_transaction_date: Optional[str],
) -> None:
    """One standing record per institution while its feed stays stale,
    however many nightly syncs observe it — the LO-OBS-004 pattern
    (:func:`_record_sync_failure`) applied to a freshness verdict instead of
    a crashed sync. Credentials are not touched by this path: the caller
    only reaches here when ``check_item_status_activity`` already reported
    the Item healthy, which is the whole point of the Chase scenario — the
    provider answers and the token is fine, and the feed is stale anyway.
    """
    env = notify_env_label()
    now = datetime.now(timezone.utc).isoformat()
    message = (
        f"{institution_slug}: no transactions in {days_since_last_transaction} days, "
        f"exceeding its {freshness_slo_days}-day freshness SLO."
    )
    existing = await _open_freshness_alert(institution_slug)

    if existing is not None:
        prior = dict(existing.get("content") or {})
        prior.update({
            "last_observed_at": now,
            "freshness_slo_days": freshness_slo_days,
            "days_since_last_transaction": days_since_last_transaction,
            "last_transaction_date": last_transaction_date,
            "message": message,
            "occurrences": int(prior.get("occurrences") or 1) + 1,
        })
        await patch_bead(
            existing["id"], content=prior, created_by="bank-sync/refresh_freshness_alert",
        )
        return

    await create_bead(
        _FRESHNESS_ALERT_TYPE,
        {
            "institution": institution_slug,
            "environment": env,
            "freshness_slo_days": freshness_slo_days,
            "days_since_last_transaction": days_since_last_transaction,
            "last_transaction_date": last_transaction_date,
            "message": message,
            "first_observed_at": now,
            "last_observed_at": now,
            "occurrences": 1,
        },
        state="pending",
        created_by="bank-sync/open_freshness_alert",
    )


async def resolve_freshness_alert(institution_slug: str, *, last_transaction_date: str) -> bool:
    """Close the open freshness-alert record because data arrived again.

    Returns whether a record was closed. The resolved record names the date
    of the newest transaction it saw — the durable form of "a healthy
    verdict names the date of the most recent transaction it saw."
    """
    existing = await _open_freshness_alert(institution_slug)
    if existing is None:
        return False
    content = dict(existing.get("content") or {})
    content.update({
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolved_by": "bank-sync/data-resumed",
        "last_transaction_date": last_transaction_date,
        "message": f"{institution_slug}: healthy — newest transaction {last_transaction_date}.",
    })
    await patch_bead(
        existing["id"], content=content, state="resolved",
        created_by="bank-sync/resolve_freshness_alert",
    )
    return True


@activity.defn
async def evaluate_freshness_activity(institution_slug: str) -> Dict[str, Any]:
    """The measurement half of LO-OBS-002: does this institution's own
    freshness SLO say money data is still arriving?

    Only called from the branch where the Item already probed healthy, so a
    ``stale`` verdict here can never be explained by bad credentials or an
    unreachable provider — the two must be ruled out first, or the record
    says something the evidence doesn't support.

    Best-effort like ``reconcile_beads_activity``/``detect_anomalies_activity``:
    a bookkeeping failure here must not fail a sync that otherwise succeeded.
    """
    from src.tools.connections import _freshness_slo_days

    since = (datetime.now(timezone.utc) - timedelta(days=_FRESHNESS_EVAL_WINDOW_DAYS)).isoformat()
    txs = await finance_query_beads(
        {"type": "transaction", "created_after": since, "limit": 2000}
    )
    posted_dates: List[str] = []
    newest: Optional[str] = None
    for tx in txs:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("institution") != institution_slug:
            continue
        posted = content.get("posted_date")
        if not posted:
            continue
        posted_dates.append(posted)
        if newest is None or posted > newest:
            newest = posted

    slo = _freshness_slo_days(posted_dates, window_days=_FRESHNESS_EVAL_WINDOW_DAYS)
    if slo is None or newest is None:
        # Not enough cadence to measure, or no data at all in the window —
        # neither a pass nor a failure. Leave any prior record untouched;
        # this is not evidence the feed recovered OR that it is stale.
        return {"status": "freshness_unknown", "institution": institution_slug}

    days_since = (date.today() - date.fromisoformat(newest[:10])).days

    if days_since > slo:
        await _record_freshness_alert(
            institution_slug,
            freshness_slo_days=slo,
            days_since_last_transaction=days_since,
            last_transaction_date=newest,
        )
        return {
            "status": "stale",
            "institution": institution_slug,
            "freshness_slo_days": slo,
            "days_since_last_transaction": days_since,
        }

    await resolve_freshness_alert(institution_slug, last_transaction_date=newest)
    return {
        "status": "ok",
        "institution": institution_slug,
        "freshness_slo_days": slo,
        "last_transaction_date": newest,
    }


@activity.defn
async def check_item_status_activity(institution_slug: str) -> Dict[str, Any]:
    """Phase 5: poll Plaid /item/get and alert if re-auth is needed.

    Returns ``{"healthy": bool, "error_code": Optional[str]}`` so the
    workflow can decide whether to short-circuit the rest of the sync.
    On re-auth-required, posts a targeted Discord alert linking to the
    /finance/link/{slug} portal.
    """
    token_env = f"PLAID_ACCESS_TOKEN_{institution_slug.upper()}"
    access_token = os.environ.get(token_env)
    if not access_token:
        # No token at all — treat as soft-missing, no Discord noise.
        return {"healthy": False, "error_code": "NO_TOKEN"}

    try:
        item_payload = await get_item_status(access_token)
    except Exception as exc:  # noqa: BLE001
        error_code = _plaid_error_code_from_exception(exc)
        if error_code in _TOKEN_REPAIR_ERROR_CODES:
            await _alert_plaid_reauth_required(institution_slug, error_code)
            return {
                "healthy": False,
                "error_code": error_code,
                "alert_sent": True,
            }
        logger.exception("item_get failed for %s: %s", institution_slug, exc)
        # Network/Plaid hiccup — don't alert, let the next sync retry.
        return {
            "healthy": False,
            "error_code": error_code or "PLAID_API_ERROR",
            "alert_sent": False,
        }

    item = item_payload.get("item") or {}
    error = item.get("error") or {}
    error_code = error.get("error_code") if isinstance(error, dict) else None

    if error_code in _TOKEN_REPAIR_ERROR_CODES:
        await _alert_plaid_reauth_required(institution_slug, error_code)
        return {"healthy": False, "error_code": error_code, "alert_sent": True}

    if error_code:
        # Non-reauth error (rate limit, internal, etc) — log but proceed.
        logger.warning(
            "Plaid item.error for %s: %s (proceeding with sync)",
            institution_slug, error_code,
        )

    return {"healthy": True, "error_code": None, "alert_sent": False}


# --- Phase 5: Reconciliation engine v2 -----------------------------------

# Matching window thresholds. Kept as module constants so the unit test
# (when it lands) can dial them without touching workflow code.
_RECONCILE_AMOUNT_TOLERANCE = 0.50  # absolute USD
_RECONCILE_DATE_WINDOW_DAYS = 3
_RECONCILE_BILL_DATE_WINDOW_DAYS = 14
_RECONCILE_EXPENSE_LOOKBACK_DAYS = 60
_RECONCILE_TRANSACTION_LOOKBACK_DAYS = 30
_PAYMENT_TEXT_MARKERS = (
    "payment",
    "pymt",
    "autopay",
    "auto pay",
    "thank you",
    "electronic transfer",
    "online transfer",
)


def _content_date(content: Dict[str, Any], *keys: str) -> Optional[date]:
    for k in keys:
        raw = content.get(k)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def _merchant_substring_match(manual_vendor: str, tx_merchant: str) -> bool:
    """Case-insensitive substring match either direction.

    Manual entries often have short labels ("Amazon") that appear inside
    Plaid's longer merchant string ("AMAZON.COM*AB12"), and vice versa.
    """
    a = (manual_vendor or "").strip().lower()
    b = (tx_merchant or "").strip().lower()
    if not a or not b:
        return False
    return a in b or b in a


def _is_payment_like_transaction(content: Dict[str, Any]) -> bool:
    """True for card payments / transfers that reduce a liability balance."""
    if content.get("is_transfer") is True or content.get("our_category") == "transfer":
        return True
    text = " ".join(
        str(content.get(k) or "")
        for k in ("merchant_name", "merchant", "normalized_merchant", "description")
    ).lower()
    return any(marker in text for marker in _PAYMENT_TEXT_MARKERS)


def _external_id_institution(content: Dict[str, Any]) -> Optional[str]:
    external_id = content.get("external_id")
    if not external_id:
        return None
    parts = str(external_id).split(":")
    if len(parts) < 2:
        return None
    return parts[1].lower()


def _liability_payment_matches_bill(manual_content: Dict[str, Any], tx_content: Dict[str, Any]) -> bool:
    """Fallback for Plaid liability bills when merchant names are generic.

    Credit-card payments often post on the liability account as negative
    "PAYMENT THANK YOU" rows, while the expected bill amount is positive and
    the bill vendor is the card product name. Account id is the strongest key;
    older bills may only have the institution embedded in external_id.
    """
    if manual_content.get("source") != "liability":
        return False
    if not _is_payment_like_transaction(tx_content):
        return False

    bill_account = manual_content.get("account_id")
    tx_account = tx_content.get("account_id") or tx_content.get("account_bead_id")
    if bill_account and tx_account and str(bill_account) == str(tx_account):
        return True

    bill_inst = _external_id_institution(manual_content)
    tx_inst = str(tx_content.get("institution") or "").lower()
    if bill_inst and tx_inst and bill_inst == tx_inst:
        return True

    vendor = str(manual_content.get("vendor") or "").lower()
    return bool(tx_inst and tx_inst in vendor)


def _manual_matches_transaction(manual: Dict[str, Any], tx: Dict[str, Any]) -> bool:
    mcontent = manual.get("content") or {}
    tcontent = tx.get("content") or {}

    try:
        mamount = float(mcontent.get("amount"))
        tamount = float(tcontent.get("amount"))
    except (TypeError, ValueError):
        return False

    is_bill = manual.get("type") == "bill"
    liability_payment_match = is_bill and _liability_payment_matches_bill(mcontent, tcontent)
    if liability_payment_match:
        if abs(tamount) + _RECONCILE_AMOUNT_TOLERANCE < mamount:
            return False
    elif abs(tamount - mamount) > _RECONCILE_AMOUNT_TOLERANCE:
        return False

    mdate = _content_date(mcontent, "date", "due_date") or _content_date(
        {"created_at": manual.get("created_at")}, "created_at"
    )
    tdate = _content_date(tcontent, "posted_date", "date")
    if mdate is None or tdate is None:
        return False

    date_window = _RECONCILE_BILL_DATE_WINDOW_DAYS if is_bill else _RECONCILE_DATE_WINDOW_DAYS
    if abs((tdate - mdate).days) > date_window:
        return False

    if liability_payment_match:
        return True

    mvendor = mcontent.get("vendor") or mcontent.get("description") or ""
    tmerchant = (
        tcontent.get("normalized_merchant")
        or tcontent.get("merchant")
        or tcontent.get("merchant_name")
        or tcontent.get("description")
        or ""
    )
    return _merchant_substring_match(mvendor, tmerchant)


def _subtract_one_calendar_month(d: date) -> date:
    """One month before ``d``, clamping the day into the shorter month.

    2026-03-31 minus one month is 2026-02-28, not an invalid 2026-02-31.
    """
    month = d.month - 1
    year = d.year
    if month == 0:
        month = 12
        year -= 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def assign_liability_payments(
    bills: List[Dict[str, Any]], transactions: List[Dict[str, Any]]
) -> Dict[str, Optional[str]]:
    """Pair each liability bill with the nearest same-cycle payment.

    Pure and order-independent: bills are sorted (due_date, id) and
    transactions are matched account-first (falling back to the bill's
    external_id institution only when the bill carries no account_id — no
    vendor fallback, unlike ``_liability_payment_matches_bill``), within
    (due_date - 1 calendar month, due_date + 7 days], one payment per bill,
    nearest due date first. See the fixture's ``window_rule`` for the exact
    contract this reproduces.
    """
    result: Dict[str, Optional[str]] = {}
    liability_bills = []
    for bill in bills:
        content = bill.get("content") or {}
        if content.get("source") == "liability":
            liability_bills.append(bill)
        else:
            result[bill["id"]] = None

    def _due_sort_key(bill: Dict[str, Any]):
        content = bill.get("content") or {}
        due = _content_date(content, "due_date")
        return (due or date.max, bill["id"])

    liability_bills.sort(key=_due_sort_key)

    claimed: set = set()
    for bill in liability_bills:
        content = bill.get("content") or {}
        due = _content_date(content, "due_date")
        try:
            bill_amount = float(content.get("amount"))
        except (TypeError, ValueError):
            bill_amount = None
        if due is None or bill_amount is None:
            result[bill["id"]] = None
            continue

        bill_account = content.get("account_id")
        bill_institution = _external_id_institution(content)
        lower_bound = _subtract_one_calendar_month(due)
        upper_bound = due + timedelta(days=7)

        best_tx = None
        best_key = None
        for tx in transactions:
            tx_id = tx["id"]
            if tx_id in claimed:
                continue
            tx_content = tx.get("content") or {}
            if not _is_payment_like_transaction(tx_content):
                continue
            try:
                tx_amount = float(tx_content.get("amount"))
            except (TypeError, ValueError):
                continue
            if abs(tx_amount) + _RECONCILE_AMOUNT_TOLERANCE < bill_amount:
                continue

            if bill_account:
                tx_account = tx_content.get("account_id")
                if not tx_account or str(tx_account) != str(bill_account):
                    continue
            else:
                tx_institution = str(tx_content.get("institution") or "").lower()
                if not bill_institution or tx_institution != bill_institution:
                    continue

            posted = _content_date(tx_content, "posted_date", "date")
            if posted is None:
                continue
            if not (lower_bound < posted <= upper_bound):
                continue

            key = (abs((due - posted).days), posted, tx_id)
            if best_key is None or key < best_key:
                best_key = key
                best_tx = tx

        if best_tx is not None:
            result[bill["id"]] = best_tx["id"]
            claimed.add(best_tx["id"])
        else:
            result[bill["id"]] = None

    return result


@activity.defn
async def reconcile_beads_activity() -> Dict[str, Any]:
    """Phase 5 v2: pair manual expenses/bills with Plaid transactions.

    Liability bills (content.source == "liability") are matched by
    :func:`assign_liability_payments` — account-first, nearest payment in
    the bill's due-date cycle window. Every other manual (expenses,
    non-liability bills) keeps the first-fit loop: merchant substring,
    amount ±$0.50, and date ±3 days for expenses, a wider date window for
    bills. On match:
      * Transition manual bead → ``reconciled`` (or ``paid`` for bills).
      * PATCH the transaction bead with parent_id=manual.id and
        state=reconciled.

    Stops at the first match per manual so we never double-pair a single
    transaction. Returns counts for the workflow result.
    """
    beads_url = substrate_beads_url()
    today = datetime.now().date()
    exp_after = (today - timedelta(days=_RECONCILE_EXPENSE_LOOKBACK_DAYS)).isoformat()
    tx_after = (today - timedelta(days=_RECONCILE_TRANSACTION_LOOKBACK_DAYS)).isoformat()

    matched = 0
    skipped = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Manual expenses (state=logged) and payable bills. Bills can be
        # flipped to overdue before a real bank payment posts, so overdue
        # must stay eligible for reconciliation.
        manual_pool: List[Dict[str, Any]] = []
        for type_, state in (
            ("expense", "logged"),
            ("bill", "pending"),
            ("bill", "overdue"),
        ):
            r = await client.get(
                beads_url,
                params={
                    "namespace": "finance",
                    "type": type_,
                    "state": state,
                    "created_after": exp_after,
                    "limit": 500,
                },
                headers=substrate_headers(),
            )
            r.raise_for_status()
            manual_pool.extend(r.json())

        if not manual_pool:
            return {"manuals_considered": 0, "matched": 0, "skipped": 0}

        # 2. Candidate transactions to match against.
        r = await client.get(
            beads_url,
            params={
                "namespace": "finance",
                "type": "transaction",
                "state": "posted",
                "created_after": tx_after,
                "limit": 2000,
            },
            headers=substrate_headers(),
        )
        r.raise_for_status()
        transactions = r.json()

        # Track which tx we've already paired in this pass so the second
        # manual scanning the same window can't re-claim it.
        claimed_tx: set[str] = set()

        # Liability bills go through the account-first, cycle-window matcher
        # instead of the first-fit loop below — see assign_liability_payments.
        liability_bills = [
            m for m in manual_pool
            if m.get("type") == "bill" and (m.get("content") or {}).get("source") == "liability"
        ]
        liability_bill_ids = {m["id"] for m in liability_bills}
        liability_assignments = assign_liability_payments(liability_bills, transactions)
        transactions_by_id = {tx["id"]: tx for tx in transactions}

        for bill in liability_bills:
            tx_id = liability_assignments.get(bill["id"])
            match = transactions_by_id.get(tx_id) if tx_id else None
            if not match:
                skipped += 1
                continue
            try:
                await patch_bead_state(
                    bill["id"],
                    state="paid",
                    created_by="bank-sync/reconcile",
                )
                await link_transaction_to_manual(
                    match["id"],
                    manual_bead_id=bill["id"],
                    created_by="bank-sync/reconcile",
                )
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "reconcile patch failed (manual=%s tx=%s): %s",
                    bill["id"], match["id"], exc,
                )
                skipped += 1
                continue

            claimed_tx.add(match["id"])
            matched += 1

        for manual in manual_pool:
            if manual["id"] in liability_bill_ids:
                continue

            match = None
            for tx in transactions:
                if tx["id"] in claimed_tx:
                    continue
                if not _manual_matches_transaction(manual, tx):
                    continue
                match = tx
                break

            if not match:
                skipped += 1
                continue

            # Manual: logged-expense → reconciled, pending-bill → paid.
            new_manual_state = "paid" if manual["type"] == "bill" else "reconciled"
            try:
                await patch_bead_state(
                    manual["id"],
                    state=new_manual_state,
                    created_by="bank-sync/reconcile",
                )
                await link_transaction_to_manual(
                    match["id"],
                    manual_bead_id=manual["id"],
                    created_by="bank-sync/reconcile",
                )
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "reconcile patch failed (manual=%s tx=%s): %s",
                    manual["id"], match["id"], exc,
                )
                skipped += 1
                continue

            claimed_tx.add(match["id"])
            matched += 1

    return {
        "manuals_considered": len(manual_pool),
        "matched": matched,
        "skipped": skipped,
    }


# --- Phase 5: Anomaly alerting -------------------------------------------

_LARGE_TX_ABSOLUTE_USD = 500.0

# Category-relative anomaly, retuned 2026-08-02 (LO-BIL-003).
#
# The old rule was `amount > 2 x category mean`. A mean with no dispersion term
# fires on every ordinary large purchase in any category with a long tail of
# small ones: prod alerted on `$261.63 at Angelo Caputo's (groceries) — 2x avg
# groceries`, which is a normal weekly shop, and re-sent it for two days.
#
# Replaced with mean + 3 sigma, plus two guards:
#   * an absolute floor, because 3 sigma over a low-value category is still
#     small change — `health` averages $6.06, and a $19 pharmacy trip is not
#     worth an interrupt;
#   * a minimum sample count, because standard deviation over 3 points is
#     noise. Categories below it are covered by the absolute rule only.
#
# Tuned against the live 30-day ledger rather than chosen: this configuration
# fires 4 times where the old one fired 15, and does not fire on Caputo's
# (groceries mean+3sigma = $308.30 vs the $261.63 charge).
_CATEGORY_ANOMALY_SIGMA = 3.0
_CATEGORY_ANOMALY_FLOOR_USD = 100.0
_CATEGORY_ANOMALY_MIN_SAMPLES = 8
_LOW_CATEGORIZATION_CONFIDENCE_THRESHOLD = 0.8

# The data-quality alert was deleted 2026-08-02 (LO-REC-003 / RC-5). It
# reported two numbers and neither could mean anything:
#
#   "N transaction(s) still unreconciled" counted transactions not paired with
#   a hand-entered expense. the Operator has never manually reconciled an account, so
#   nothing ever leaves state=posted and the number only grows — 296 -> 312
#   across 2026-08-01 alone. That monotonic climb is also what defeated
#   fingerprint suppression: every increment reads as new information.
#
#   "N with low categorization confidence (<80%)" was structurally zero. The
#   categorizer emits `misc` at high confidence rather than declining to
#   answer, so 379 of a 400-row misc sample sat at >=0.85 while the metric
#   reported 0.
#
# Tuning either number would have preserved the lie at lower volume. The
# review queue that replaces this (LO-CAT-003, Sprint 3) keys on the real
# signal and lives in the console, where it is actionable — a Discord message
# carrying two counts and no link never was.

_ALERT_STATE_TYPE = "alert_state"


async def _load_alert_state(kind: str) -> Optional[Dict[str, Any]]:
    """Most recent ``finance.alert_state`` bead for ``kind``, or None."""
    beads = await finance_query_beads({"type": _ALERT_STATE_TYPE, "limit": 100})
    matches = [b for b in beads if (b.get("content") or {}).get("kind") == kind]
    if not matches:
        return None
    return max(
        matches,
        key=lambda b: (b.get("content") or {}).get("last_posted_at") or "",
    )


async def _record_alert_posted(
    kind: str,
    fingerprint: str,
    existing: Optional[Dict[str, Any]],
    members: Optional[List[str]] = None,
) -> None:
    content: Dict[str, Any] = {
        "kind": kind,
        "fingerprint": fingerprint,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
    }
    if members is not None:
        # The identities this alert named, for the set-shrink gate below.
        content["members"] = sorted(members)
    if existing and existing.get("id"):
        await patch_bead(
            existing["id"], content=content, created_by="bank-sync/alert_state"
        )
        return
    payload = {
        "namespace": "finance",
        "type": _ALERT_STATE_TYPE,
        "state": "active",
        "content": content,
        "trust_tier": "system",
        "created_by": "bank-sync/alert_state",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            substrate_beads_url(), json=payload, headers=substrate_headers()
        )
        resp.raise_for_status()


def _alert_policy() -> AlertPolicy:
    return AlertPolicy(
        load_alert_state=_load_alert_state,
        record_alert_posted=_record_alert_posted,
        post=_post_discord,
    )


def category_baseline(amounts: List[float]) -> Optional[Dict[str, float]]:
    """Mean/sigma/threshold for one category, or None if it cannot be trusted.

    Pure and deterministic — the anomaly threshold is the thing that decides
    whether the Operator gets interrupted, so it is unit-testable on its own.

    Returns None when the sample is too small for a standard deviation to mean
    anything, or when every charge in the category is identical (sigma == 0,
    which would make the threshold equal to the mean and fire on any variation
    at all).
    """
    if len(amounts) < _CATEGORY_ANOMALY_MIN_SAMPLES:
        return None
    n = float(len(amounts))
    mean = sum(amounts) / n
    variance = sum((a - mean) ** 2 for a in amounts) / n
    sigma = variance ** 0.5
    if sigma <= 0:
        return None
    return {
        "mean": mean,
        "sigma": sigma,
        "threshold": mean + _CATEGORY_ANOMALY_SIGMA * sigma,
    }


def is_category_anomaly(amount: float, baseline: Optional[Dict[str, float]]) -> bool:
    """True when ``amount`` is a category-relative outlier worth interrupting for."""
    if not baseline:
        return False
    if amount <= _CATEGORY_ANOMALY_FLOOR_USD:
        return False
    return amount > baseline["threshold"]


def count_low_or_unmeasured_categorization_confidence(rows: List[Dict[str, Any]]) -> int:
    """Count transaction rows needing category-confidence review.

    Accepts transaction bead dictionaries with ``content`` or bare
    categorization/content dictionaries so tests can exercise the metric without
    Substrate.
    """
    count = 0
    for row in rows:
        content = row.get("content") or row
        confidence = content.get("categorization_confidence")
        if confidence is None:
            count += 1
            continue
        try:
            if float(confidence) < _LOW_CATEGORIZATION_CONFIDENCE_THRESHOLD:
                count += 1
        except (TypeError, ValueError):
            count += 1
    return count


@activity.defn
async def detect_anomalies_activity() -> Dict[str, Any]:
    """Flag transactions added in the last 24h that are unusually large.

    Two rules (OR-combined):
      1. amount > $500.
      2. amount is a category-relative outlier — see :func:`is_category_anomaly`
         and the tuning note on ``_CATEGORY_ANOMALY_SIGMA``.

    Posts one Discord summary message per run (not per anomaly) to avoid
    notification spam during catch-up syncs.
    """
    beads_url = substrate_beads_url()
    now = datetime.now()
    recent_after = (now - timedelta(hours=24)).isoformat()
    history_after = (now - timedelta(days=30)).isoformat()

    async with httpx.AsyncClient(timeout=30.0) as client:
        recent_resp = await client.get(
            beads_url,
            params={
                "namespace": "finance",
                "type": "transaction",
                "created_after": recent_after,
                "limit": 1000,
            },
            headers=substrate_headers(),
        )
        recent_resp.raise_for_status()
        # state=removed beads are Plaid-retracted — they must not trigger
        # anomaly alerts nor inflate category baselines.
        recent_txs = [b for b in recent_resp.json() if b.get("state") != "removed"]

        history_resp = await client.get(
            beads_url,
            params={
                "namespace": "finance",
                "type": "transaction",
                "created_after": history_after,
                "limit": 5000,
            },
            headers=substrate_headers(),
        )
        history_resp.raise_for_status()
        history_txs = [b for b in history_resp.json() if b.get("state") != "removed"]

    # Build per-category trailing average from history, EXCLUDING the
    # transactions we're evaluating so a single huge purchase doesn't
    # pull the threshold up enough to mask itself.
    recent_ids = {t["id"] for t in recent_txs}
    samples: Dict[str, List[float]] = {}
    for tx in history_txs:
        if tx["id"] in recent_ids:
            continue
        c = tx.get("content") or {}
        cat = c.get("our_category")
        try:
            amt = float(c.get("amount"))
        except (TypeError, ValueError):
            continue
        if not cat or amt <= 0:
            continue
        samples.setdefault(cat, []).append(amt)

    baselines: Dict[str, Dict[str, float]] = {}
    for cat, amounts in samples.items():
        baseline = category_baseline(amounts)
        if baseline:
            baselines[cat] = baseline

    anomalies: List[str] = []
    anomaly_ids: List[str] = []
    for tx in recent_txs:
        c = tx.get("content") or {}
        try:
            amt = float(c.get("amount"))
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        cat = c.get("our_category")
        merchant = c.get("normalized_merchant") or c.get("merchant") or c.get("description") or "(unknown)"
        reasons: List[str] = []
        if amt > _LARGE_TX_ABSOLUTE_USD:
            reasons.append(f">${_LARGE_TX_ABSOLUTE_USD:.0f}")
        baseline = baselines.get(cat) if cat else None
        if is_category_anomaly(amt, baseline):
            reasons.append(
                f"unusual for `{cat}` "
                f"(avg ${baseline['mean']:.2f}, flags above ${baseline['threshold']:.2f})"
            )
        if reasons:
            anomalies.append(
                f"• `${amt:.2f}` at **{merchant}** "
                f"({cat or 'uncategorized'}) — {', '.join(reasons)}"
            )
            anomaly_ids.append(str(tx.get("id")))

    posted = False
    if anomalies:
        body = "\n".join(anomalies[:15])
        more = f"\n_…and {len(anomalies) - 15} more_" if len(anomalies) > 15 else ""
        # Fingerprint on the flagged transactions themselves, so the same
        # set stays quiet across the other 19 runs of the day but a newly
        # flagged transaction alerts immediately. Passing them as `members`
        # too means the set draining as transactions age out of the 24h
        # window stays quiet as well — only a transaction we have never
        # announced reopens the channel.
        posted = await send_alert(
            _alert_policy(),
            "bank_sync.anomalies",
            "|".join(sorted(anomaly_ids)),
            f":rotating_light: **Unusual transactions detected** "
            f"({len(anomalies)} in last 24h):\n{body}{more}",
            severity=AlertSeverity.ACTIONABLE,
            members=anomaly_ids,
        )

    return {
        "anomalies_found": len(anomalies),
        "categories_with_baseline": len(baselines),
        "alert_posted": posted,
    }


# --- Workflow ---

# Mirrors the schedule-level RetryPolicy(maximum_attempts=...) in
# temporal_worker.bank_sync_workflow_retry_policy and its deliberately
# decoupled copy in tools/schedules.py (test_bank_sync guards all three
# against drift). The workflow needs the number to know which attempt is
# the last one — temporalio 1.28 does not expose the retry policy via
# workflow.info().
SCHEDULE_RETRY_MAX_ATTEMPTS = 4


@workflow.defn
class BankSyncWorkflow:
    @workflow.run
    async def run(self, institution_slug: str = "chase") -> Dict[str, Any]:
        try:
            result = await self._run_inner(institution_slug)
        except Exception as exc:
            # Alert-then-reraise: a crashed sync must never be silent —
            # but only the FINAL attempt alerts. Every attempt used to
            # post, so one shared-dependency hiccup fanned out to
            # 5 institutions x 4 attempts = 20 Discord messages for a
            # single incident (2026-07-18).
            attempt = workflow.info().attempt
            if attempt < SCHEDULE_RETRY_MAX_ATTEMPTS:
                workflow.logger.warning(
                    "bank sync failed for %s (attempt %d/%d); retry pending, alert deferred",
                    institution_slug, attempt, SCHEDULE_RETRY_MAX_ATTEMPTS,
                )
                raise
            # Best-effort — the alert failing must not mask the original.
            try:
                await workflow.execute_activity(
                    notify_sync_failure_activity,
                    args=[institution_slug, _sync_failure_message(exc)[:400]],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except Exception:  # noqa: BLE001
                workflow.logger.warning(
                    "failure alert could not be sent for %s", institution_slug
                )
            raise

        # A record that cannot be closed is a different kind of noise. A
        # successful run IS the explanation for a sync failure — unlike a ledger
        # discrepancy, where the account reconciling proves nothing.
        try:
            await workflow.execute_activity(
                resolve_sync_failure_activity,
                args=[institution_slug],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:  # noqa: BLE001 — closing the record must not fail a good sync
            workflow.logger.warning(
                "could not resolve sync failure record for %s", institution_slug
            )
        return result

    async def _run_inner(self, institution_slug: str) -> Dict[str, Any]:
        # Phase 4.1: maximum_interval >= 60s lets retries straddle the
        # Gemini free-tier per-minute quota reset. 90s gives headroom for
        # rate-limit windows that drift past the minute boundary.
        # maximum_attempts=5 (was 3): transient cluster-DNS failures during
        # pod churn exhausted 3 attempts and killed a first-run sync
        # (2026-06-11). This is a nightly batch path — latency is free.
        default_retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=90),
            maximum_attempts=5,
        )


        # Phase 5 step 0: probe Plaid item health. If re-auth is needed,
        # the activity itself fires the Discord alert; we then short-
        # circuit so we don't accumulate stale-token errors against the
        # rest of the sync.
        item_status = await workflow.execute_activity(
            check_item_status_activity,
            institution_slug,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )
        if not item_status.get("healthy") and item_status.get("alert_sent"):
            return {
                "accounts_synced": 0,
                "transactions_written": 0,
                "transactions_skipped": 0,
                "reauth_required": True,
                "plaid_error": item_status.get("error_code"),
            }

        # 1. List + upsert accounts (with Phase 6 liability merge)
        accounts = await workflow.execute_activity(
            list_accounts_activity,
            institution_slug,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=default_retry,
        )

        # One /liabilities/get per institution → merged into the per-
        # account bead writes below. Empty dict for institutions that
        # don't support liabilities.
        liability_map: Dict[str, Dict[str, Any]] = await workflow.execute_activity(
            fetch_liabilities_activity,
            institution_slug,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=default_retry,
        )

        account_map: Dict[str, str] = {}  # plaid_id -> bead_id
        for acc in accounts:
            bead_id = await workflow.execute_activity(
                write_account_bead_activity,
                args=[acc, liability_map.get(acc["account_id"])],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=default_retry,
            )
            account_map[acc["account_id"]] = bead_id

        # Phase 8.5: Register upcoming bills from liability data.
        bill_registration: Dict[str, Any] = {}
        try:
            bill_registration = await workflow.execute_activity(
                register_liability_bills_activity,
                args=[institution_slug, accounts, liability_map, account_map],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=default_retry,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("register_liability_bills_activity failed: %s", exc)

        # 2. Resolve the Item registry bead + transactions cursor (self-
        # heals pre-registry institutions via /item/get).
        registry = await workflow.execute_activity(
            ensure_item_registry_activity,
            institution_slug,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=default_retry,
        )

        # 3. Known transaction ids — replay protection. A crash after a
        # page's writes but before its cursor PATCH means the next run
        # re-receives the same page; this set (plus the DB unique index,
        # substrate migration 0002) makes the replay a no-op.
        existing_ids = await workflow.execute_activity(
            fetch_existing_transaction_ids_activity,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=default_retry,
        )
        existing_set = set(existing_ids)

        # 3b. Re-link defense: an initial sync (no stored cursor) means a
        # fresh Item — and a re-linked institution replays its FULL history
        # under brand-new transaction ids, sailing past the id-based dedup
        # (150 duplicates observed after the 2026-06-11 re-links). Build a
        # fingerprint multiset of what's already stored and skip incoming
        # adds that merely re-describe it.
        fingerprint_budget: Dict[str, int] = {}
        if not registry.get("cursor"):
            fingerprint_budget = await workflow.execute_activity(
                fetch_existing_fingerprint_counts_activity,
                institution_slug,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=default_retry,
            )

        written = 0
        skipped = 0
        deduped = 0
        orphaned = 0
        modified_total = 0
        removed_total = 0

        # 4./5. Cursor loop: one /transactions/sync page per activity
        # call (≤500 changes keeps every Temporal payload far below the
        # 2MB gRPC limit, even on an initial full-history sync), cursor
        # persisted only AFTER the page's writes land.
        cursor = registry.get("cursor")
        pages = 0
        # Safety valve — 40 pages × 500 changes per run; anything beyond
        # resumes from the stored cursor on the next nightly run.
        _MAX_SYNC_PAGES = 40

        while True:
            page = await workflow.execute_activity(
                sync_transactions_page_activity,
                args=[institution_slug, cursor],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=default_retry,
            )

            new_transactions = [
                t for t in page["added"] if t["transaction_id"] not in existing_set
            ]
            skipped += len(page["added"]) - len(new_transactions)

            # Filter transactions to find only those that actually need to be categorized and written
            to_categorize = []
            tx_accounts = []

            for t in new_transactions:
                account_bead_id = account_map.get(t["account_id"])
                if account_bead_id is None:
                    orphaned += 1
                    continue
                if fingerprint_budget:
                    fp = transaction_fingerprint(
                        account_bead_id, t["amount"], t["date"], t["name"]
                    )
                    if fingerprint_budget.get(fp, 0) > 0:
                        fingerprint_budget[fp] -= 1
                        deduped += 1
                        continue
                to_categorize.append(t)
                tx_accounts.append(account_bead_id)

            if to_categorize:
                categorizations = await workflow.execute_activity(
                    categorize_transactions_bulk_activity,
                    args=[to_categorize],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=default_retry,
                )

                # Write transaction beads concurrently (concurrency cap 2)
                write_semaphore = asyncio.Semaphore(2)

                async def write_tx(t, acc_id, cat):
                    nonlocal written
                    async with write_semaphore:
                        bead_id = await workflow.execute_activity(
                            write_transaction_bead_activity,
                            args=[t, acc_id, cat],
                            start_to_close_timeout=timedelta(minutes=5),
                            retry_policy=default_retry
                        )
                        if bead_id is not None:
                            written += 1

                await asyncio.gather(*[
                    write_tx(t, acc_id, cat)
                    for t, acc_id, cat in zip(to_categorize, tx_accounts, categorizations)
                ])

            existing_set.update(t["transaction_id"] for t in new_transactions)

            if page["modified"]:
                mod_result = await workflow.execute_activity(
                    apply_modified_transactions_activity,
                    page["modified"],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=default_retry,
                )
                modified_total += mod_result.get("modified", 0)

            if page["removed"]:
                rem_result = await workflow.execute_activity(
                    apply_removed_transactions_activity,
                    page["removed"],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=default_retry,
                )
                removed_total += rem_result.get("removed", 0)

            cursor = page["next_cursor"]
            if registry.get("bead_id") and cursor:
                await workflow.execute_activity(
                    store_sync_cursor_activity,
                    args=[registry["bead_id"], cursor],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=default_retry,
                )

            pages += 1
            if not page["has_more"] or pages >= _MAX_SYNC_PAGES:
                break

        # 6. Phase 5: reconcile new transactions against unreconciled
        # manual expenses/bills. Best-effort — never crash the sync.
        reconcile_summary: Dict[str, Any] = {}
        try:
            reconcile_summary = await workflow.execute_activity(
                reconcile_beads_activity,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=default_retry,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("reconcile_beads_activity failed: %s", exc)

        # 7. Phase 5: anomaly detection (large or 2× category mean).
        anomaly_summary: Dict[str, Any] = {}
        try:
            anomaly_summary = await workflow.execute_activity(
                detect_anomalies_activity,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=default_retry,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("detect_anomalies_activity failed: %s", exc)

        # 8. (removed 2026-08-02) The 48h stale-reconciliation / low-confidence
        # alert lived here. See the note above _ALERT_STATE_TYPE.

        # 9. LO-OBS-002: does this institution's own freshness SLO say money
        # data is still arriving? Only reached when step 0 already found the
        # Item healthy, so a "stale" verdict here can never be a re-auth
        # problem wearing a different name — credentials and reachability
        # are already ruled out. Best-effort like reconcile/anomalies above.
        freshness_summary: Dict[str, Any] = {}
        try:
            freshness_summary = await workflow.execute_activity(
                evaluate_freshness_activity,
                institution_slug,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=default_retry,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning("evaluate_freshness_activity failed: %s", exc)

        return {
            "accounts_synced": len(accounts),
            "transactions_written": written,
            "transactions_skipped": skipped,
            "transactions_deduped": deduped,
            "transactions_orphaned": orphaned,
            "transactions_modified": modified_total,
            "transactions_removed": removed_total,
            "sync_pages": pages,
            "bill_registration": bill_registration,
            "reconcile": reconcile_summary,
            "anomalies": anomaly_summary,
            "freshness": freshness_summary,
        }
