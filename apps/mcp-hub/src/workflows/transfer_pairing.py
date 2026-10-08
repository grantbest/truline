"""Phase 6.5: Transfer pairing engine.

Internal money movement (e.g. a $25 standing transfer from Chase checking
into Chase savings) shows up in Plaid as TWO transactions: an **outflow**
in the source account and an **inflow** in the destination account.
``categorize_transaction_activity`` already flags both with
``is_transfer=True``. If the wealth/cash-flow layer counts them naively it
double-counts the move as both spending and income.

This workflow pairs those two legs and records the relationship as a
``finance.transfer`` bead linking the two transaction ids. It also stamps a
shared ``transfer_pair_id`` on both transaction beads so the cash-flow layer
can cheaply exclude paired legs from spend/income aggregates.

Representation decision (2026-05-28): a dedicated ``finance.transfer`` bead,
NOT a mutation-only approach. ``transfer`` is not in
``schemas.FINANCE_TYPE_SCHEMAS``, so it passes schema-on-write as free-form
content (same as ``budget``/``backfill_status``); no substrate change needed.

Matching heuristic (deliberately conservative — scaffold, tune later):
  * One leg's amount is positive (outflow), the other negative (inflow) —
    Plaid convention: outflows positive, inflows negative.
  * |outflow.amount| == |inflow.amount| within ``_AMOUNT_TOLERANCE``.
  * posted dates within ``_DATE_WINDOW_DAYS``.
  * the two legs are in DIFFERENT accounts.
Each transaction is claimed at most once per pass.

TODO(tune): cross-institution transfers can settle a few days apart; the
date window may need widening. Amount tolerance assumes 1:1 transfers (no
FX, no fees). Revisit once we have real paired data to measure against.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

# httpx trips the workflow sandbox at import; it is only used inside
# activities. Mark pass-through so workflow validation doesn't traverse it.
with workflow.unsafe.imports_passed_through():
    import uuid

    import httpx

    from src.tools.finance import substrate_beads_url, substrate_headers


# --- Tunables -------------------------------------------------------------

_AMOUNT_TOLERANCE = 0.01  # absolute USD; transfers are 1:1 (no fees/FX yet)
_DATE_WINDOW_DAYS = 3
_DEFAULT_LOOKBACK_DAYS = 30
_NAMESPACE = "finance"
_TRANSFER_TYPE = "transfer"


# --- Pure helpers (deterministic; safe to call inside the workflow) -------

def _to_date(content: Dict[str, Any], *keys: str) -> Optional[date]:
    for k in keys:
        raw = content.get(k)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def _match_transfer_pairs(
    transfers: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Pair outflow legs with their matching inflow legs.

    ``transfers`` is a list of transaction beads (``{"id", "content"}``)
    already filtered to ``is_transfer`` and unpaired. Returns a list of
    ``(outflow_bead, inflow_bead)`` tuples. Pure and deterministic: no I/O,
    no clock, no RNG — safe to run inside the workflow sandbox.
    """
    outflows: List[Dict[str, Any]] = []
    inflows: List[Dict[str, Any]] = []
    for t in transfers:
        c = t.get("content") or {}
        try:
            amount = float(c.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount > 0:
            outflows.append(t)
        elif amount < 0:
            inflows.append(t)
        # amount == 0 is not a transfer leg; skip.

    # Deterministic ordering so replay produces identical pairings.
    outflows.sort(key=lambda t: (str(_to_date(t["content"], "posted_date", "authorized_date")), t["id"]))
    inflows.sort(key=lambda t: (str(_to_date(t["content"], "posted_date", "authorized_date")), t["id"]))

    claimed: set[str] = set()
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    for out in outflows:
        oc = out["content"]
        o_amt = float(oc["amount"])
        o_date = _to_date(oc, "posted_date", "authorized_date")
        o_acct = oc.get("account_id")
        if o_date is None:
            continue
        for inf in inflows:
            if inf["id"] in claimed:
                continue
            ic = inf["content"]
            i_amt = float(ic["amount"])
            i_date = _to_date(ic, "posted_date", "authorized_date")
            i_acct = ic.get("account_id")
            if i_date is None:
                continue
            if i_acct == o_acct:
                continue  # must cross accounts
            if abs(abs(i_amt) - abs(o_amt)) > _AMOUNT_TOLERANCE:
                continue
            if abs((i_date - o_date).days) > _DATE_WINDOW_DAYS:
                continue
            claimed.add(inf["id"])
            claimed.add(out["id"])
            pairs.append((out, inf))
            break

    return pairs


# --- Activities -----------------------------------------------------------

@activity.defn
async def fetch_unpaired_transfers_activity(lookback_days: int) -> List[Dict[str, Any]]:
    """Return transfer-flagged transaction beads not yet paired.

    Pulls recent ``finance.transaction`` beads and keeps those with
    ``is_transfer`` true and no ``transfer_pair_id`` yet. Filtering happens
    over the decrypted content returned by GET /beads.
    """
    created_after = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            substrate_beads_url(),
            params={
                "namespace": _NAMESPACE,
                "type": "transaction",
                "created_after": created_after,
                "limit": 5000,
            },
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        out: List[Dict[str, Any]] = []
        for b in resp.json():
            if b.get("state") == "removed":
                # A retracted leg must never pair with a live one.
                continue
            c = b.get("content") or {}
            if c.get("is_transfer") and not c.get("transfer_pair_id"):
                out.append({"id": b["id"], "content": c})
        return out


@activity.defn
async def persist_transfer_pair_activity(
    outflow: Dict[str, Any],
    inflow: Dict[str, Any],
) -> str:
    """Record a matched pair: emit a ``finance.transfer`` bead and stamp a
    shared ``transfer_pair_id`` on both transaction legs.

    Returns the new transfer bead id. The pair id is minted here (not in the
    workflow) so workflow replay stays deterministic.
    """
    pair_id = str(uuid.uuid4())
    oc, ic = outflow["content"], inflow["content"]
    beads_url = substrate_beads_url()

    transfer_content = {
        "transfer_pair_id": pair_id,
        "from_tx_id": outflow["id"],
        "to_tx_id": inflow["id"],
        "from_account_id": oc.get("account_id"),
        "to_account_id": ic.get("account_id"),
        "amount": abs(float(oc["amount"])),
        "iso_currency_code": oc.get("iso_currency_code", "USD"),
        "posted_date": oc.get("posted_date"),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        post_resp = await client.post(
            beads_url,
            json={
                "namespace": _NAMESPACE,
                "type": _TRANSFER_TYPE,
                "state": "paired",
                "content": transfer_content,
                "trust_tier": "system",
                "created_by": "transfer-pairing",
            },
            headers=substrate_headers(),
        )
        post_resp.raise_for_status()
        transfer_bead_id = post_resp.json()["id"]

        # Stamp both legs. PATCH replaces content, so send the full content
        # dict with transfer_pair_id added (FinanceTransactionContent is
        # extra=allow, so the extra key validates).
        for leg in (outflow, inflow):
            leg_content = dict(leg["content"])
            leg_content["transfer_pair_id"] = pair_id
            patch_resp = await client.patch(
                f"{beads_url}/{leg['id']}",
                json={"content": leg_content, "created_by": "transfer-pairing"},
                headers=substrate_headers(),
            )
            patch_resp.raise_for_status()

    logger.info(
        "Paired transfer %s: outflow %s + inflow %s (amount=%.2f).",
        pair_id, outflow["id"][:8], inflow["id"][:8], transfer_content["amount"],
    )
    return transfer_bead_id


# --- Workflow -------------------------------------------------------------

@workflow.defn
class TransferPairingWorkflow:
    """Pair internal-transfer transaction legs into finance.transfer beads."""

    @workflow.run
    async def run(self, lookback_days: int = _DEFAULT_LOOKBACK_DAYS) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        transfers = await workflow.execute_activity(
            fetch_unpaired_transfers_activity,
            lookback_days,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        # Pure, deterministic matching — safe inside the workflow.
        pairs = _match_transfer_pairs(transfers)

        persisted = 0
        for outflow, inflow in pairs:
            await workflow.execute_activity(
                persist_transfer_pair_activity,
                args=[outflow, inflow],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry,
            )
            persisted += 1

        return {
            "unpaired_considered": len(transfers),
            "pairs_persisted": persisted,
        }
