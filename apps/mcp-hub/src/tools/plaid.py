import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional
import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.exceptions import ApiException

def get_client() -> plaid_api.PlaidApi:
    """Returns a configured Plaid client."""
    # Plaid sunset the Development environment in 2024; plaid-python 39.x
    # only exposes Sandbox and Production.
    env = os.environ.get("PLAID_ENV", "sandbox")
    if env == "sandbox":
        host = plaid.Environment.Sandbox
        secret = os.environ.get("PLAID_SANDBOX_SECRET")
    elif env == "production":
        host = plaid.Environment.Production
        secret = os.environ.get("PLAID_SECRET")
    else:
        raise ValueError(f"Invalid PLAID_ENV: {env}")

    client_id = os.environ.get("PLAID_CLIENT_ID")
    
    # Fail fast with a clear message if credentials are missing.
    # Passing None to the Plaid SDK causes a cryptic TypeError deep in urllib3.
    if not client_id:
        raise ValueError("Missing PLAID_CLIENT_ID environment variable.")
    if not secret:
        secret_var = "PLAID_SANDBOX_SECRET" if env == "sandbox" else "PLAID_SECRET"
        raise ValueError(f"Missing {secret_var} environment variable for PLAID_ENV={env}.")

    configuration = plaid.Configuration(
        host=host,
        api_key={
            'clientId': client_id,
            'secret': secret,
        }
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)

async def exchange_public_token(public_token: str) -> Dict[str, str]:
    """Exchanges a public_token for ``{"access_token", "item_id"}``.

    ``item_id`` is the durable identifier of the Plaid Item this token
    belongs to — webhooks reference Items only by item_id, and
    /item/remove needs it at unlink time, so callers must persist it
    (see finance.register_plaid_item).
    """
    client = get_client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return {
        "access_token": response['access_token'],
        "item_id": response['item_id'],
    }

def _to_jsonable(obj: Any) -> Any:
    """Coerce Plaid SDK dicts into JSON-safe shapes.

    Plaid's .to_dict() leaves datetime.date and decimal.Decimal in-place; both
    fail Temporal's default JSON payload converter. Recurse and convert.
    """
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


async def get_accounts(access_token: str) -> List[Dict[str, Any]]:
    """Returns accounts with *live* balances for an institution.

    Uses /accounts/balance/get, not /accounts/get: the latter returns
    Plaid's cached balance (as of its last institution sync, often hours
    stale), which made stored balances drift from the real bank value.
    /accounts/balance/get forces a real-time pull from the institution.
    """
    client = get_client()
    request = AccountsBalanceGetRequest(access_token=access_token)
    response = client.accounts_balance_get(request)
    return [_to_jsonable(a.to_dict()) for a in response['accounts']]

async def get_transactions(access_token: str, start: date, end: date) -> List[Dict[str, Any]]:
    """Returns ALL posted transactions for a date range.

    Used by the date-windowed historical backfill. Paginates on
    ``total_transactions`` — the old single-page pull silently dropped
    everything past the first 500 rows in a dense window.

    Nightly sync should use :func:`sync_transactions_page` instead
    (cursor-based, no date math, surfaces removals).
    """
    client = get_client()

    transactions: List[Dict[str, Any]] = []
    offset = 0
    while True:
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start,
            end_date=end,
            options=TransactionsGetRequestOptions(
                count=500,
                offset=offset,
            )
        )
        response = client.transactions_get(request)
        page = response['transactions']
        # Filter for posted only (no pending), then strip non-JSON types.
        transactions.extend(
            _to_jsonable(t.to_dict()) for t in page if not t['pending']
        )
        offset += len(page)
        if offset >= response['total_transactions'] or not page:
            break
    return transactions


async def sync_transactions_page(
    access_token: str, cursor: Optional[str] = None
) -> Dict[str, Any]:
    """One page of /transactions/sync (max 500 changes).

    Plaid's recommended replacement for /transactions/get: instead of a
    date window, the caller holds a per-Item cursor and receives every
    change (added / modified / removed) since the last call. ``cursor``
    omitted or None = initial sync, which streams the Item's full
    available history across pages.

    Returns ``{"added", "modified", "removed", "next_cursor", "has_more"}``
    — all JSON-safe. The caller must persist ``next_cursor`` only AFTER
    durably storing the page (replays after a crash are then idempotent),
    and call again while ``has_more`` is true. Page-at-a-time keeps each
    Temporal activity result well under the 2MB payload limit even on the
    initial full-history sync.
    """
    client = get_client()
    kwargs: Dict[str, Any] = {"access_token": access_token, "count": 500}
    if cursor:
        kwargs["cursor"] = cursor
    request = TransactionsSyncRequest(**kwargs)
    response = client.transactions_sync(request)
    payload = _to_jsonable(response.to_dict())
    return {
        "added": payload.get("added") or [],
        "modified": payload.get("modified") or [],
        "removed": payload.get("removed") or [],
        "next_cursor": payload.get("next_cursor"),
        "has_more": bool(payload.get("has_more")),
    }

async def get_item_status(access_token: str) -> Dict[str, Any]:
    """Phase 5: poll /item/get to detect tokens that need user re-auth.

    Plaid signals re-auth by populating ``item.error`` with codes like
    ``ITEM_LOGIN_REQUIRED`` or ``PENDING_EXPIRATION``. The workflow uses
    these to fire a targeted Discord alert linking back to the
    /finance/link/{slug} portal so the Operator can re-link the institution.

    Returns a JSON-safe dict so the value travels through Temporal's
    payload converter unchanged.
    """
    client = get_client()
    request = ItemGetRequest(access_token=access_token)
    response = client.item_get(request)
    return _to_jsonable(response.to_dict())


# --- Item health classification ------------------------------------------
#
# Shared by bank-sync's re-auth alerting and the /finance/connections
# status endpoint. Canonical home is here so the API app never has to
# import the Temporal workflow module (which drags in temporalio).

# Codes Plaid Link's update mode can repair in place: the user
# re-authenticates the EXISTING Item and the stored token stays valid.
REAUTH_ERROR_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "PENDING_EXPIRATION",
    "ACCESS_NOT_GRANTED",
    "USER_PERMISSION_REVOKED",
    "PENDING_DISCONNECT",
}

# Superset including codes where the Item is unrecoverable — update mode
# can't help; the institution must be re-linked in create mode (new token).
TOKEN_REPAIR_ERROR_CODES = REAUTH_ERROR_CODES | {
    "INVALID_ACCESS_TOKEN",
    "ITEM_NOT_FOUND",
}

PLAID_TOKEN_ENV_PREFIX = "PLAID_ACCESS_TOKEN_"


def list_linked_institutions() -> List[str]:
    """Institution slugs discovered from PLAID_ACCESS_TOKEN_* env vars.

    Same convention the Temporal worker uses to register nightly
    schedules: the env suffix, lowercased, is the institution slug.
    """
    return sorted({
        key[len(PLAID_TOKEN_ENV_PREFIX):].lower()
        for key, value in os.environ.items()
        if key.startswith(PLAID_TOKEN_ENV_PREFIX)
        and key != PLAID_TOKEN_ENV_PREFIX
        and value
    })


def access_token_for(institution_slug: str) -> Optional[str]:
    return os.environ.get(f"{PLAID_TOKEN_ENV_PREFIX}{institution_slug.upper()}")


def plaid_error_code_from_exception(exc: Exception) -> Optional[str]:
    """Extract Plaid's JSON error_code from SDK exceptions.

    Plaid raises API exceptions for invalid/expired access tokens before
    /item/get can return an item.error payload. Treat those the same as
    item-level re-auth errors so a bad token surfaces as a classified
    status instead of disappearing into logs.
    """
    candidates = [getattr(exc, "body", None), str(exc)]
    for raw in candidates:
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, dict):
            code = raw.get("error_code")
            if code:
                return str(code)
            continue
        text = str(raw).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'"error_code"\s*:\s*"([^"]+)"', text)
            if match:
                return match.group(1)
            continue
        if isinstance(parsed, dict) and parsed.get("error_code"):
            return str(parsed["error_code"])
    return None


async def get_liabilities(access_token: str) -> Dict[str, Any]:
    """Phase 6: pull credit / mortgage / student-loan liabilities.

    Plaid /liabilities/get returns a per-account-type breakdown::

        {
          "accounts": [...],          # echoes accounts_get, same account_ids
          "liabilities": {
            "credit":   [CreditCardLiability, ...],
            "mortgage": [MortgageLiability, ...],
            "student":  [StudentLoan, ...],
          }
        }

    Each entry carries ``account_id`` so callers can fan it back out to
    the matching ``finance.account`` bead.

    Not every item supports liabilities (a basic checking account, e.g.,
    will respond with ``NO_LIABILITY_ACCOUNTS`` or the product won't be
    enabled at all → ``PRODUCT_NOT_READY`` / ``INVALID_PRODUCT``). In
    those cases we return an *empty* shape rather than raising, so the
    caller can merge the result unconditionally.
    """
    client = get_client()
    request = LiabilitiesGetRequest(access_token=access_token)
    try:
        response = client.liabilities_get(request)
    except ApiException as exc:
        # Plaid surfaces structured error info in exc.body. We don't want
        # to parse JSON inline here — just match on the substring of the
        # well-known unsupported-product / no-accounts codes. Anything
        # else propagates so the workflow's retry policy sees it.
        body = (exc.body or "") if isinstance(exc.body, str) else str(exc.body)
        for benign in (
            "PRODUCT_NOT_READY",
            "PRODUCTS_NOT_SUPPORTED",
            "INVALID_PRODUCT",
            "NO_LIABILITY_ACCOUNTS",
            "NO_ACCOUNTS",
        ):
            if benign in body:
                return {"accounts": [], "liabilities": {"credit": [], "mortgage": [], "student": []}}
        raise

    payload = _to_jsonable(response.to_dict())
    # Defensive: ensure the three sub-keys exist so callers can
    # ``for c in payload["liabilities"]["credit"]`` without KeyError.
    liabs = payload.get("liabilities") or {}
    for k in ("credit", "mortgage", "student"):
        if liabs.get(k) is None:
            liabs[k] = []
    payload["liabilities"] = liabs
    return payload


async def create_link_token(client_user_id: str, access_token: Optional[str] = None) -> str:
    """Creates a link_token for Plaid Link.

    Phase 6: ``liabilities`` is added to ``required_if_supported_products``
    so credit-card and loan items report APR/min-payment/principal when
    the institution exposes them, but plain checking-only institutions
    still link successfully.

    Pass ``access_token`` to create the token in *update mode* — Plaid's
    repair flow for ``ITEM_LOGIN_REQUIRED`` / ``PENDING_EXPIRATION`` etc.
    Link re-authenticates the existing Item in place and the access token
    stays valid afterwards: no public-token exchange, no Infisical update,
    no pod restart. Plaid rejects ``products`` in update mode, so the two
    request shapes are built separately.
    """
    client = get_client()
    if access_token:
        request = LinkTokenCreateRequest(
            client_name="Truline Personal OS",
            country_codes=[CountryCode('US')],
            language='en',
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
            access_token=access_token,
        )
    else:
        request = LinkTokenCreateRequest(
            products=[Products('transactions')],
            required_if_supported_products=[Products('liabilities')],
            client_name="Truline Personal OS",
            country_codes=[CountryCode('US')],
            language='en',
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
        )
    response = client.link_token_create(request)
    return response['link_token']
