"""SDD Phase 4 — Rule retroaction worker (commit half of the Rule Sandbox).

The sandbox dry-run lives in Substrate (read-only, Tier 2 cache). *Committing*
a rule has to (1) persist it and (2) retroactively re-categorize historical
transactions — and #2 is a background job, not a request-blocking loop over
thousands of writes. Substrate has no Temporal client, so retroaction lives
here, mirroring the Phase 3 scenario seam: a thin mcp-hub endpoint
(``POST /finance/rules/commit``) saves the ``finance.rule`` bead and **starts**
(fire-and-forget) this workflow.

Commit policy (confirmed with the Operator)
------------------------------------
Only transactions whose ``our_category`` is empty/Uncategorized are patched,
even when the rule predicate matches a broader set. We never clobber an
existing human/LLM categorization. The dry-run surfaces the same distinction
(``will_change``).

Lineage without clobbering ``parent_id``
----------------------------------------
A transaction's ``parent_id`` can already point at a manual reconciliation
bead. So we record the rule's authorship inside content
(``categorization_source="rule"``, ``categorization_rule_id=<bead>``) rather
than rewiring ``parent_id``. PATCH on a finance bead replaces content wholesale
and Substrate re-validates the full :class:`FinanceTransactionContent` schema,
so we send the merged full content, never a partial.

``rule`` is a free-form single-token type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), matching the Phase 1–3 precedent.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Callable, Dict, List

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import patch_bead, query_beads


_TXN_PAGE_SIZE = 1000
_MAX_TXN_PAGES = 100
UNCATEGORIZED = "Uncategorized"

# Mirror of the Substrate dry-run aliasing: the spec's example uses ``vendor``,
# a bill field; transactions carry merchant fields. Keep the two in lockstep so
# a rule that previews on the dry-run applies to the same beads on commit.
_FIELD_ALIASES = {"vendor": "normalized_merchant"}


# --- Pure helpers (deterministic; unit-tested directly) -------------------

def resolve_field(field: str) -> str:
    return _FIELD_ALIASES.get(field, field)


def build_matcher(operator: str, value: str) -> Callable[[str], bool]:
    """Return a ``(str) -> bool`` predicate for a rule. Raises on bad regex."""
    needle = value.lower()
    if operator == "contains":
        return lambda hay: needle in hay
    if operator == "equals":
        return lambda hay: hay == needle
    if operator == "starts_with":
        return lambda hay: hay.startswith(needle)
    if operator == "regex":
        pattern = re.compile(value, re.IGNORECASE)
        return lambda hay: pattern.search(hay) is not None
    raise ValueError(f"unsupported operator: {operator}")


def _coerce(value: Any) -> str:
    return "" if value is None else str(value)


def transaction_matches(content: Dict[str, Any], field: str, matcher: Callable[[str], bool]) -> bool:
    hay = _coerce(content.get(field)).lower()
    return bool(hay) and matcher(hay)


def is_uncategorized(content: Dict[str, Any]) -> bool:
    return (_coerce(content.get("our_category")) or UNCATEGORIZED) == UNCATEGORIZED


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
async def apply_rule_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Retroactively stamp the rule's category onto matching, uncategorized
    transactions. Patches one bead at a time (heartbeating) so a partial run is
    safe to retry — each PATCH is idempotent (re-applying the same category is a
    no-op write). Returns a summary the commit endpoint logged."""
    rule = payload["rule"]
    rule_bead_id = payload.get("rule_bead_id")
    field = resolve_field(str(rule["field"]))
    target = str(rule["target_category"])
    matcher = build_matcher(str(rule["operator"]), str(rule["value"]))

    transactions = await _fetch_all_transactions()
    matched = 0
    patched = 0
    errors = 0
    for tx in transactions:
        content = tx.get("content") or {}
        if not transaction_matches(content, field, matcher):
            continue
        matched += 1
        if not is_uncategorized(content) or target == UNCATEGORIZED:
            continue
        # Merge full content (PATCH replaces wholesale + re-validates schema).
        new_content = dict(content)
        new_content["our_category"] = target
        new_content["categorization_source"] = "rule"
        new_content["categorization_confidence"] = 1.0
        if rule_bead_id:
            new_content["categorization_rule_id"] = rule_bead_id
        try:
            await patch_bead(
                str(tx["id"]),
                content=new_content,
                created_by="rule-apply/retroact",
            )
            patched += 1
            activity.heartbeat(patched)
        except Exception:  # noqa: BLE001 — one bad bead shouldn't abort the batch
            errors += 1
            logger.warning("rule-apply: failed to patch %s", tx.get("id"), exc_info=True)

    summary = {
        "rule_bead_id": rule_bead_id,
        "scanned": len(transactions),
        "matched": matched,
        "patched": patched,
        "errors": errors,
    }
    # Record the outcome on the rule bead so the console can show what it did.
    if rule_bead_id:
        try:
            await patch_bead(
                rule_bead_id,
                state="applied",
                content={**rule, "last_apply": summary},
                created_by="rule-apply/summary",
            )
        except Exception:  # noqa: BLE001 — summary is best-effort
            logger.warning("rule-apply: failed to stamp summary on %s", rule_bead_id, exc_info=True)

    logger.info(
        "rule-apply %s: scanned=%d matched=%d patched=%d errors=%d",
        rule_bead_id, len(transactions), matched, patched, errors,
    )
    return summary


# --- Workflow -------------------------------------------------------------

@workflow.defn
class FinanceRuleApplyWorkflow:
    """Background retroaction for a committed categorization rule. Started
    fire-and-forget by ``POST /finance/rules/commit``; not scheduled."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=3,
        )
        return await workflow.execute_activity(
            apply_rule_activity,
            payload,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )
