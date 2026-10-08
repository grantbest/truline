"""The ``finance`` namespace — an extension, not the core.

We moved off "loose JSON" for finance beads so net-worth calculations
can rely on a stable shape. New beads in namespace=="finance" must
match the per-type model below; everything else (other namespaces,
legacy types we haven't modelled yet) still accepts free-form content.

This module registers its own content types against the core's
:data:`schemas.NAMESPACE_TYPE_SCHEMAS` registry, and its own router
(finance's summary endpoint) against :data:`namespace_registry.NAMESPACE_ROUTERS`
— rather than either being compiled into the core. Neither ``schemas.py`` nor
``main.py`` names a finance symbol directly; ``routes.py`` is this module's
composition root instead (see its comment on the import at the bottom of
that file), and ``validate_finance_content`` imports it lazily as a second
safety net for callers that import ``schemas`` bare (see schemas.py's
``validate_finance_content``). The separately-authored ``rules.py`` dry-run
router, the namespace's integrity-error mapper, and its encryption policy are
three further hooks, each registered from its own module (``rules.py``,
``finance_integrity.py``, ``finance_encryption.py``) rather than from here —
each has a production entry point, or an import-cycle constraint, of its
own; see those modules' docstrings.

Trade-off documented for future readers:
  * The models use ``extra = "allow"`` so additive fields don't break
    producers mid-deploy. We trim them out of net-worth math, not at
    the gate.
  * Validation runs against the PLAINTEXT content dict, BEFORE the
    encryption pass in :func:`crypto.encrypt_jsonb`. The encryption
    layer is opaque to type information.
  * Types not in :data:`FINANCE_TYPE_SCHEMAS` (e.g. ``budget``,
    ``expense``, ``subscription``, ``backfill_status``) pass through
    unchanged. Add models here as those types harden.
"""

import asyncio
from datetime import date
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, UUID4
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

try:
    from . import routes as _routes
    from .crypto import decrypt_jsonb
    from .database import get_db
    from .models import Bead
    from .namespace_registry import NAMESPACE_ROUTERS
    from .schemas import NAMESPACE_TYPE_SCHEMAS
except ImportError as exc:  # pragma: no cover - bare-module consumers
    # A circular-import failure (a module reached before one of its own
    # dependencies has finished initialising) must propagate as itself, not
    # be reinterpreted as "this is a bare, package-less import" and retried
    # as an absolute import naming the wrong missing module.
    if "circular import" in str(exc):
        raise
    import routes as _routes
    from crypto import decrypt_jsonb
    from database import get_db
    from models import Bead
    from namespace_registry import NAMESPACE_ROUTERS
    from schemas import NAMESPACE_TYPE_SCHEMAS

require_api_key = _routes.require_api_key


class LiabilityDetails(BaseModel):
    """Optional liability data merged into a credit/loan account bead.

    All four fields are optional individually because different account
    types expose different subsets (e.g. a fixed-rate mortgage has no
    APR variability; an unactivated credit card has no min_payment yet).
    """

    model_config = ConfigDict(extra="allow")

    apr: Optional[float] = None
    min_payment: Optional[float] = None
    principal: Optional[float] = None
    due_date: Optional[date] = None
    next_payment_date: Optional[date] = None


class FinanceAccountContent(BaseModel):
    """Validated shape for ``finance.account`` bead content."""

    model_config = ConfigDict(extra="allow")

    institution: str
    plaid_account_id: str
    name: str
    type: str
    # Phase 6: liabilities is optional — only depository accounts that
    # support /liabilities/get will have it populated.
    liabilities: Optional[LiabilityDetails] = None


class FinanceTransactionContent(BaseModel):
    """Validated shape for ``finance.transaction`` bead content.

    Phase 6 brief:
      * ``amount``: outflows positive, inflows negative (Plaid convention)
      * ``is_transfer``: True iff merchant is a financial institution
        and the description is a payment/auto-pay/electronic transfer.
      * ``account_id``: UUID of the parent ``finance.account`` bead.
    """

    model_config = ConfigDict(extra="allow")

    amount: float
    iso_currency_code: str = "USD"
    merchant_name: str
    normalized_merchant: str
    posted_date: date
    authorized_date: Optional[date] = None
    plaid_transaction_id: str
    is_transfer: bool = False
    account_id: UUID4


class FinanceBillContent(BaseModel):
    """Validated shape for ``finance.bill`` bead content (Phase 8.5).

    A bill is a *future, expected* outflow we track to a ``paid`` or
    ``overdue`` resolution. Three sources feed it: ``liability`` (derived
    nightly from Plaid liability data on account beads), ``manual`` (logged
    via the MCP/console), and ``vision`` (deferred — email extraction).

    Field notes:
      * ``vendor`` — NOT ``payee``. ``reconcile_beads_activity`` matches a
        bill to a Plaid transaction on ``content.vendor``; a mismatched key
        leaves the bill stuck ``pending`` forever.
      * ``external_id`` — deterministic dedup key, namespaced per owner
        (e.g. ``"grant:chase:2026-06"``). Matched CLIENT-SIDE over decrypted
        content, never via a DB unique index: it lives outside
        ``crypto.PLAINTEXT_KEYS`` so at rest it is opaque ciphertext.
      * ``owner`` — multi-user dimension. Optional for now: no person
        registry exists yet, so the write path defaults it. Tighten to
        required once a registry lands.
      * ``account_id`` — soft ref to the source ``finance.account`` bead;
        provenance to the source liability, not a ``parent_id`` edge.
    """

    model_config = ConfigDict(extra="allow")

    vendor: str
    amount: float = Field(..., gt=0)
    due_date: date
    category: str = "utilities"
    source: Literal["liability", "vision", "manual"]
    owner: Optional[str] = None
    external_id: Optional[str] = None
    account_id: Optional[UUID4] = None
    min_payment: Optional[float] = None
    recurring: bool = False
    frequency: Optional[Literal["monthly", "quarterly", "annual"]] = None


# Map of (finance) bead type -> Pydantic model. POST /beads consults
# this when namespace == "finance"; unknown types pass through.
FINANCE_TYPE_SCHEMAS: Dict[str, type[BaseModel]] = {
    "account": FinanceAccountContent,
    "transaction": FinanceTransactionContent,
    "bill": FinanceBillContent,
}

for _finance_bead_type, _finance_model_cls in FINANCE_TYPE_SCHEMAS.items():
    NAMESPACE_TYPE_SCHEMAS.register("finance", _finance_bead_type, _finance_model_cls)
del _finance_bead_type, _finance_model_cls


class FinanceSummary(BaseModel):
    """Server-computed roll-up of ``finance.account`` balances.

    Balances live encrypted in bead content, so the sum can't be pushed
    into SQL — :func:`routes.finance_summary` decrypts each account bead
    and aggregates here. Mixed currencies are kept apart in
    ``by_currency``; ``total_balance`` is the naive sum across all
    accounts and is only meaningful in a single-currency portfolio.
    """

    total_balance: float
    account_count: int
    by_currency: Dict[str, float]


# ---------------------------------------------------------------------------
# Router — the finance namespace's own endpoints, registered through the
# per-namespace hook main.py consults generically (NAMESPACE_ROUTERS), rather
# than being imported and mounted by name in main.py. The rules dry-run
# router (rules.py) is a second, separately-authored finance router; it
# registers itself under the same namespace, from its own module (see
# rules.py's own comment for why).
# ---------------------------------------------------------------------------

finance_router = APIRouter()


@finance_router.get(
    "/beads/finance/summary",
    response_model=FinanceSummary,
    dependencies=[Depends(require_api_key)],
)
async def finance_summary(db: AsyncSession = Depends(get_db)):
    """Server-side roll-up of every ``finance.account`` balance.

    current_balance is an encrypted leaf (not a PLAINTEXT_KEY), so the
    aggregation can't run in SQL — we pull the account beads, decrypt
    content, and sum ``current_balance`` here. Accounts whose balance is
    missing or non-numeric are skipped rather than failing the request.
    """
    result = await db.execute(
        select(Bead).filter(Bead.namespace == "finance", Bead.type == "account")
    )
    accounts = result.scalars().all()

    def _aggregate() -> tuple[float, int, dict[str, float]]:
        total = 0.0
        n = 0
        per_currency: dict[str, float] = {}
        for bead in accounts:
            content = decrypt_jsonb(bead.content)
            balance = content.get("current_balance")
            if not isinstance(balance, (int, float)) or isinstance(balance, bool):
                continue
            currency = content.get("iso_currency_code") or "USD"
            total += float(balance)
            per_currency[currency] = per_currency.get(currency, 0.0) + float(balance)
            n += 1
        return total, n, per_currency

    total_balance, counted, by_currency = await asyncio.to_thread(_aggregate)

    return FinanceSummary(
        total_balance=total_balance,
        account_count=counted,
        by_currency=by_currency,
    )


NAMESPACE_ROUTERS.register("finance", finance_router)

# Nest into routes.router ourselves, rather than leaving routes.py to read
# this registry at its own import time: whichever of this module and
# routes.py the caller imports first, Python resolves the resulting cycle by
# handing the *other* module its still-incomplete self, so a read from
# routes.py's side cannot be trusted to see the entries above. This module,
# by contrast, only reaches this line once every registration above has
# already happened — regardless of which side started the import -- so
# mounting from here is the ordering-independent side of the pair.
# mount_pending is idempotent, so this being called only from here (and not
# also from routes.py) does not risk mounting anything twice. rules.py
# registers itself independently of this module (see its own comment); by
# the time this module is reached from routes.py's own bottom, routes.py has
# already imported rules.py first, so mount_pending here picks up both.
NAMESPACE_ROUTERS.mount_pending(_routes.router)


__all__ = [
    "FinanceAccountContent",
    "FinanceBillContent",
    "FinanceSummary",
    "FinanceTransactionContent",
    "FINANCE_TYPE_SCHEMAS",
    "LiabilityDetails",
    "finance_router",
]
