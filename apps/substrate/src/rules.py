"""SDD Phase 4 — Rule Definition & Backtesting Sandbox (dry-run half).

The console lets a user draft an auto-categorization rule and backtest it
against historical ``finance.transaction`` beads *before* anything mutates the
ledger. The hot constraint (spec §2): we cannot Fernet-decrypt thousands of
beads on every keystroke.

Fast path, corrected from the spec's literal wording
----------------------------------------------------
The spec says to read transactions "bypassing ALE decryption … assuming
categorical data is accessible." That assumption is false at rest — the fields
a rule matches on (``merchant_name``, ``normalized_merchant``, ``our_category``)
are NOT in :data:`crypto.PLAINTEXT_KEYS`; they are ciphertext. What IS true is
that :func:`routes.list_beads` decrypts once and caches the *decrypted* JSON in
Redis. So the real fast path is: reuse that decrypted cache (Tier 2) and only
fall back to a single bulk decrypt on a cold miss.

We get that reuse by calling :func:`routes.list_beads` directly instead of
hand-building a parallel cache key/shape — a hand-built copy is exactly what
drifted out of sync with ``list_beads``' key shape last time (it gained
``parent_id``/``created_after`` segments and this module's copy did not
follow). Calling the same function means the sandbox always reads a key
``list_beads`` writes, writes a key ``list_beads`` (and itself) reads, and is
invalidated by the same finance writes that invalidate ``list_beads``' cache.

Commit lives elsewhere
----------------------
``/commit`` is NOT here. Substrate has no Temporal client; retroaction is a
background workflow owned by mcp-hub (``POST /finance/rules/commit`` →
``FinanceRuleApplyWorkflow``). This module is read-only.
"""

import logging
import re
from typing import Any, List, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .namespace_registry import NAMESPACE_ROUTERS
from .routes import list_beads, require_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rules", tags=["rules"])

# Cap on how many recent transactions we backtest against. Spec §3.1: "last
# 5,000". Matches the implicit list_beads page this shares a cache key with.
MAX_BACKTEST_BEADS = 5000
# How many concrete before/after rows we hand the UI. The count is exact;
# the diff list is a sample so the payload stays small.
MAX_SAMPLE_DIFFS = 100
# Sentinel shown when our_category is absent/empty.
UNCATEGORIZED = "Uncategorized"

Operator = Literal["contains", "equals", "starts_with", "regex"]

# Rule field aliases. The spec's example uses ``vendor``, but that is a *bill*
# field — transactions carry merchant fields instead. Alias it so the spec's
# example rule still resolves to a real field.
_FIELD_ALIASES = {"vendor": "normalized_merchant"}


class RuleSpec(BaseModel):
    """JSON representation of one auto-categorization rule (spec §3.1)."""

    field: str = Field(..., min_length=1, max_length=64)
    operator: Operator
    value: str = Field(..., min_length=1, max_length=256)
    target_category: str = Field(..., min_length=1, max_length=64)


class SampleDiff(BaseModel):
    id: str
    vendor: str
    old: str
    new: str
    # False when the txn matches the rule but already carries a category —
    # commit (Uncategorized-only policy) will skip it.
    will_change: bool


class DryRunResult(BaseModel):
    # Total txns matching the rule predicate.
    matched: int
    # Subset commit will actually patch (currently: only Uncategorized).
    beads_affected: int
    # How many beads were backtested (warms-cache visibility for QA).
    scanned: int
    sample_diffs: List[SampleDiff]


def _resolve_field(field: str) -> str:
    return _FIELD_ALIASES.get(field, field)


def _compile_matcher(rule: RuleSpec):
    """Return a predicate ``(str) -> bool`` for the rule, or raise 422."""
    needle = rule.value.lower()
    op = rule.operator
    if op == "contains":
        return lambda hay: needle in hay
    if op == "equals":
        return lambda hay: hay == needle
    if op == "starts_with":
        return lambda hay: hay.startswith(needle)
    if op == "regex":
        try:
            pattern = re.compile(rule.value, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "invalid_regex", "message": str(exc)},
            ) from exc
        return lambda hay: pattern.search(hay) is not None
    # Unreachable — Literal guards the input — but keep mypy/readers honest.
    raise HTTPException(status_code=422, detail="unsupported operator")


async def _load_recent_transactions(db: AsyncSession) -> list[dict]:
    """Decrypted recent ``finance.transaction`` beads, via list_beads' cache.

    Delegates to :func:`routes.list_beads` for
    ``namespace=finance&type=transaction&limit=5000&offset=0`` rather than
    querying/decrypting/caching independently, so the read key, the write
    key, and the value shape are all whatever ``list_beads`` uses — by
    construction, not by a hand-copied string that can drift.
    """
    beads = await list_beads(
        request=None,
        namespace="finance",
        type="transaction",
        limit=MAX_BACKTEST_BEADS,
        offset=0,
        db=db,
    )
    out: list[dict] = []
    for bead in beads:
        if isinstance(bead, dict):
            out.append({"id": bead.get("id"), "content": bead.get("content") or {}})
        else:
            out.append({"id": str(bead.id), "content": bead.content or {}})
    return out


def _coerce(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


@router.post("/dry-run", response_model=DryRunResult, dependencies=[Depends(require_api_key)])
async def dry_run_rule(rule: RuleSpec, db: AsyncSession = Depends(get_db)) -> DryRunResult:
    """Backtest a rule against recent transactions without mutating anything.

    "Read-only" above describes this module's effect on the database, not
    its HTTP verb: the rule spec is a request body, so this is a POST, and
    OPS-122's read key refuses it (403 "read-only key cannot write") like
    every other non-GET route. That is a deliberate boundary -- the read
    key's scope is "GET only", not "no side effects" -- not a coverage gap.
    """
    field = _resolve_field(rule.field)
    matcher = _compile_matcher(rule)
    beads = await _load_recent_transactions(db)

    matched = 0
    affected = 0
    diffs: List[SampleDiff] = []
    for bead in beads:
        content = bead.get("content") or {}
        hay = _coerce(content.get(field)).lower()
        if not hay or not matcher(hay):
            continue
        matched += 1
        old = _coerce(content.get("our_category")) or UNCATEGORIZED
        new = rule.target_category
        # Commit policy (confirmed): only re-categorize Uncategorized txns,
        # never clobber an existing human/LLM categorization.
        will_change = old == UNCATEGORIZED and new != old
        if will_change:
            affected += 1
        if len(diffs) < MAX_SAMPLE_DIFFS:
            vendor = (
                _coerce(content.get("normalized_merchant"))
                or _coerce(content.get("merchant_name"))
                or _coerce(content.get("merchant"))
                or "(unknown)"
            )
            diffs.append(
                SampleDiff(id=bead["id"], vendor=vendor, old=old, new=new, will_change=will_change)
            )

    # Surface will-change rows first so the sample is the useful half.
    diffs.sort(key=lambda d: not d.will_change)
    return DryRunResult(
        matched=matched,
        beads_affected=affected,
        scanned=len(beads),
        sample_diffs=diffs,
    )


# Composition root for this module's own namespace hook: registers this
# router on NAMESPACE_ROUTERS from its own bottom, mirroring
# finance_integrity.py/finance_encryption.py, rather than depending on
# finance_schemas.py to import and register it on this module's behalf.
# finance_schemas.py doing that used to close a
# routes -> finance_schemas -> rules -> routes import cycle: entering the
# import graph at ``src.rules`` (instead of ``src.routes``) left this module
# paused above, before ``router`` existed, so finance_schemas.py's import of
# it raised a circular-import ImportError. Self-registering here means
# nothing importing this module depends on finance_schemas.py at all.
NAMESPACE_ROUTERS.register("finance", router)

# Mount ourselves into routes.router too, not just register: entering the
# import graph at ``src.rules`` reaches this line only *after* routes.py has
# already run to completion and, at its own bottom, already called
# finance_schemas's ``mount_pending`` — which nested whatever was registered
# at THAT moment (finance's summary router only; this module's register()
# call above had not run yet, since this module was paused above, waiting on
# routes.py, when routes.py reached that point). Nothing then revisits
# ``routes.router`` afterwards, so without this call ``/rules/dry-run``
# would be present whenever ``main.py``/``routes.py`` starts the import, but
# silently absent whenever a caller enters at ``src.rules`` first.
# ``mount_pending`` tracks what it has already nested by ``id()``, so
# calling it again here — regardless of which side of the cycle got there
# first — is safe and cannot mount anything twice.
from . import routes as _routes  # noqa: E402

NAMESPACE_ROUTERS.mount_pending(_routes.router)
