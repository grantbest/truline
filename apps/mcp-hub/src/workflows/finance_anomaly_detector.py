"""SDD Phase 2 — Agentic Anomaly Engine.

Catch financial leaks the night they land: duplicate charges (the same
merchant billing the same amount twice) and silent price hikes (a recurring
vendor's charge jumping well above its own moving average).

Why a separate workflow from the weekly subscription auditor?
  The auditor is a slow, LLM-driven *classifier* (is this a subscription? did
  its held price change ≥3%?) running once a week. This engine is a fast,
  deterministic *anomaly* scanner running every night right after the bank
  sync, on the last 48 hours of fresh transactions. Different cadence,
  different signal, no LLM in the hot path — so detection never depends on
  model temperament and the QA contract is reproducible.

Each run (``run`` orchestrates three activities):
  1. ``fetch_anomaly_inputs_activity`` — pull the recent (48h) window plus a
     longer history window (for the moving-average baseline) of
     ``finance.transaction`` beads. Computation stays inside the activity so
     the (potentially large) history never crosses the Temporal payload
     boundary — only the small anomaly list comes back.
  2. (pure) :func:`detect_duplicates` + :func:`detect_price_anomalies` run
     inside that activity.
  3. ``emit_anomalies_activity`` — upsert one ``finance.review_anomaly`` bead
     (state=pending) per detection, keyed on a stable ``fingerprint`` so
     nightly re-runs refresh rather than pile up duplicates.

``review_anomaly`` is a free-form type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), matching the Phase 1 precedent of
single-token ``audit_discrepancy`` / ``balance_snapshot`` (NOT the brief's
dotted ``review.anomaly`` — substrate stores ``type`` as a flat string and the
console queries it flat).

Auth: every Substrate call goes through ``src.tools.finance.substrate_*``
(X-API-Key required), matching the other finance workflows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import create_bead, patch_bead, query_beads


# --- Tunables -------------------------------------------------------------

# How far back "fresh" transactions count for duplicate/price scanning. The
# nightly bank sync runs at 03:00; 48h absorbs a sync that slips a day or a
# transaction that posts late without re-alerting on already-seen history.
RECENT_WINDOW_HOURS = 48

# History depth for the per-vendor moving-average baseline used by price-hike
# detection. Six months matches the subscription auditor's lookback.
HISTORY_WINDOW_DAYS = 183

# A recurring vendor's new charge must exceed its prior moving average by this
# fraction to count as a price hike. The brief specifies 15%.
PRICE_HIKE_THRESHOLD = 0.15

# Need at least this many prior charges to trust a moving average — one or two
# data points make the "average" meaningless and would false-flag.
MIN_HISTORY_FOR_BASELINE = 3

# Vendors that legitimately bill the same amount multiple times a day (transit,
# fuel, coffee, etc.). Identical same-day charges from these are NOT duplicates.
# Matched as a case-insensitive substring of the normalized merchant.
KNOWN_MULTI_CHARGE_VENDORS = (
    "uber",
    "lyft",
    "parkmobile",
    "starbucks",
    "shell",
    "exxon",
    "chevron",
    "bp",
    "mcdonald",
    "doordash",
    "grubhub",
)

_ANOMALY_TYPE = "review_anomaly"
_TXN_PAGE_SIZE = 1000
_MAX_TXN_PAGES = 100


# --- Pure helpers (deterministic; safe to unit test directly) -------------

def _to_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _vendor_of(content: Dict[str, Any]) -> str:
    return str(
        content.get("normalized_merchant")
        or content.get("merchant_name")
        or content.get("description")
        or ""
    ).strip()


def _is_multi_charge_vendor(vendor: str) -> bool:
    v = vendor.lower()
    return any(known in v for known in KNOWN_MULTI_CHARGE_VENDORS)


def _usable_transactions(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop beads that must not feed anomaly math: Plaid-retracted (removed)
    rows, transfers (internal moves, not spend), and non-positive amounts
    (inflows / refunds)."""
    usable = []
    for tx in transactions:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        if content.get("is_transfer"):
            continue
        try:
            amount = float(content.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        usable.append(tx)
    return usable


def detect_duplicates(recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag groups of transactions with identical (account, amount, posted
    date, vendor) — the classic double-charge.

    Pure and deterministic. ``recent`` is the fresh window; known multi-charge
    vendors (transit, fuel, coffee) are excluded since a second identical
    same-day charge there is normal. Each group of ≥2 becomes one anomaly
    carrying every transaction id in the group.
    """
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for tx in _usable_transactions(recent):
        content = tx.get("content") or {}
        vendor = _vendor_of(content)
        if not vendor or _is_multi_charge_vendor(vendor):
            continue
        amount = round(float(content.get("amount")), 2)
        posted = str(content.get("posted_date") or "")[:10]
        if not posted:
            continue
        key = (content.get("account_id"), amount, posted, vendor.lower())
        groups.setdefault(key, []).append(tx)

    anomalies: List[Dict[str, Any]] = []
    for (account_id, amount, posted, _vendor_key), txs in groups.items():
        if len(txs) < 2:
            continue
        tx_ids = sorted(t.get("id") for t in txs if t.get("id"))
        display_vendor = _vendor_of(txs[0].get("content") or {})
        anomalies.append(
            {
                "reason": "duplicate_charge",
                "transaction_ids": tx_ids,
                "account_id": account_id,
                "vendor": display_vendor,
                "amount": amount,
                "posted_date": posted,
                "count": len(txs),
                "fingerprint": f"dup:{account_id}:{amount}:{posted}:{_vendor_key}",
            }
        )
    return anomalies


def detect_price_anomalies(
    recent: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    *,
    threshold: float = PRICE_HIKE_THRESHOLD,
    min_history: int = MIN_HISTORY_FOR_BASELINE,
) -> List[Dict[str, Any]]:
    """Flag a recent charge that jumps >``threshold`` above its vendor's prior
    moving average.

    Pure and deterministic. The baseline is the mean of all *prior* (history,
    excluding the recent window) charges for that normalized vendor; we require
    at least ``min_history`` priors so a one-off vendor can't set a baseline.
    Only the largest recent charge per vendor is evaluated, so a vendor with
    several recent charges yields at most one anomaly.
    """
    recent_usable = _usable_transactions(recent)
    history_usable = _usable_transactions(history)

    recent_ids = {t.get("id") for t in recent_usable}
    # Build the prior baseline from history rows that are NOT in the recent
    # window (the recent charge shouldn't inflate its own baseline).
    baseline_amounts: Dict[str, List[float]] = {}
    for tx in history_usable:
        if tx.get("id") in recent_ids:
            continue
        content = tx.get("content") or {}
        vendor = _vendor_of(content).lower()
        if not vendor:
            continue
        baseline_amounts.setdefault(vendor, []).append(round(float(content.get("amount")), 2))

    # The candidate recent charge per vendor = the largest recent amount.
    candidate: Dict[str, Dict[str, Any]] = {}
    for tx in recent_usable:
        content = tx.get("content") or {}
        vendor = _vendor_of(content).lower()
        if not vendor:
            continue
        amount = round(float(content.get("amount")), 2)
        existing = candidate.get(vendor)
        if existing is None or amount > existing["amount"]:
            candidate[vendor] = {"tx": tx, "amount": amount}

    anomalies: List[Dict[str, Any]] = []
    for vendor, cand in candidate.items():
        priors = baseline_amounts.get(vendor) or []
        if len(priors) < min_history:
            continue
        avg = sum(priors) / len(priors)
        if avg <= 0:
            continue
        new_amount = cand["amount"]
        change = (new_amount - avg) / avg
        if change <= threshold:
            continue
        tx = cand["tx"]
        content = tx.get("content") or {}
        anomalies.append(
            {
                "reason": "price_hike",
                "transaction_ids": [tx.get("id")] if tx.get("id") else [],
                "account_id": content.get("account_id"),
                "vendor": _vendor_of(content),
                "amount": new_amount,
                "baseline_amount": round(avg, 2),
                "change_pct": round(change * 100, 1),
                "posted_date": str(content.get("posted_date") or "")[:10],
                "sample_size": len(priors),
                "fingerprint": f"hike:{vendor}:{new_amount}",
            }
        )
    return anomalies


def detect_anomalies(
    recent: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Run both detectors and return the combined anomaly list."""
    return detect_duplicates(recent) + detect_price_anomalies(recent, history)


def _confidence_for(reason: str) -> float:
    # Duplicates are an exact-match signal; price hikes are statistical.
    return 0.9 if reason == "duplicate_charge" else 0.75


# --- Activities -----------------------------------------------------------

async def _fetch_all_transactions() -> List[Dict[str, Any]]:
    """Page through every ``finance.transaction`` bead (offset pagination)."""
    results: List[Dict[str, Any]] = []
    for page in range(_MAX_TXN_PAGES):
        chunk = await query_beads(
            {"type": "transaction", "limit": _TXN_PAGE_SIZE, "offset": page * _TXN_PAGE_SIZE}
        )
        results.extend(chunk)
        if len(chunk) < _TXN_PAGE_SIZE:
            break
    return results


def _partition_recent(
    transactions: List[Dict[str, Any]], *, now: datetime
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into (recent ≤48h, history ≤183d). The recent window is a subset
    of history — price detection needs both. Boundary is the bead's
    ``posted_date`` (falling back to ``authorized_date``)."""
    recent_cutoff = now - timedelta(hours=RECENT_WINDOW_HOURS)
    history_cutoff = now - timedelta(days=HISTORY_WINDOW_DAYS)
    recent: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for tx in transactions:
        content = tx.get("content") or {}
        when = _to_datetime(content.get("posted_date") or content.get("authorized_date"))
        if when is None:
            continue
        # Compare naive-to-naive: posted_date is a date, our window is UTC.
        when_naive = when.replace(tzinfo=None)
        if when_naive >= history_cutoff.replace(tzinfo=None):
            history.append(tx)
        if when_naive >= recent_cutoff.replace(tzinfo=None):
            recent.append(tx)
    return recent, history


@activity.defn
async def fetch_anomaly_inputs_activity() -> List[Dict[str, Any]]:
    """Fetch transactions, run both pure detectors, return the (small) anomaly
    list. All heavy data stays inside the activity."""
    transactions = await _fetch_all_transactions()
    now = datetime.now(timezone.utc)
    recent, history = _partition_recent(transactions, now=now)
    anomalies = detect_anomalies(recent, history)
    logger.info(
        "Anomaly scan: %d txns (%d recent / %d history-window) -> %d anomalies.",
        len(transactions), len(recent), len(history), len(anomalies),
    )
    return anomalies


@activity.defn
async def emit_anomalies_activity(anomalies: List[Dict[str, Any]]) -> int:
    """Upsert one ``finance.review_anomaly`` bead (state=pending) per anomaly,
    keyed on ``content.fingerprint`` so nightly re-runs refresh in place rather
    than accrete duplicates. Returns the count written or refreshed."""
    if not anomalies:
        return 0

    existing = await query_beads({"type": _ANOMALY_TYPE, "state": "pending", "limit": 1000})
    pending_by_fp: Dict[str, Dict[str, Any]] = {}
    for b in existing:
        fp = (b.get("content") or {}).get("fingerprint")
        if fp and fp not in pending_by_fp:
            pending_by_fp[fp] = b

    written = 0
    for anomaly in anomalies:
        content = {
            **anomaly,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }
        match = pending_by_fp.get(anomaly.get("fingerprint"))
        if match is not None:
            await patch_bead(
                match["id"],
                content=content,
                created_by="anomaly-detector/refresh",
            )
        else:
            await create_bead(
                _ANOMALY_TYPE,
                content,
                state="pending",
                created_by="anomaly-detector/emit",
            )
        written += 1

    logger.info("Emitted/refreshed %d review_anomaly beads.", written)
    return written


# --- Workflow -------------------------------------------------------------

@workflow.defn
class FinanceAnomalyDetectorWorkflow:
    """Nightly deterministic anomaly scan over recent transactions.
    Scheduled by ``temporal_worker.ensure_schedules`` for 04:00 America/Chicago
    (after the 03:00 bank syncs and 03:45 reconciliation)."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        anomalies = await workflow.execute_activity(
            fetch_anomaly_inputs_activity,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        emitted = await workflow.execute_activity(
            emit_anomalies_activity,
            anomalies,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        duplicates = sum(1 for a in anomalies if a.get("reason") == "duplicate_charge")
        price_hikes = sum(1 for a in anomalies if a.get("reason") == "price_hike")
        return {
            "anomalies_detected": len(anomalies),
            "duplicate_charges": duplicates,
            "price_hikes": price_hikes,
            "beads_emitted": emitted,
        }
