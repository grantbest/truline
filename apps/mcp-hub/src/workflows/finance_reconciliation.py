"""SDD Phase 1: Ledger Reconciliation Engine.

Detect when an account's tracked balance in Substrate diverges from reality.

Why not "balance - Σ(all transactions)" (the original brief)?
  We backfill only ~2 years of transactions and store no opening-balance
  anchor, so summing every transaction never equals the institution balance —
  it would flag every account as massively drifted on day one.

Instead we reconcile on the DELTA between two nightly balance snapshots:

    expected_current = prev_snapshot.balance - Σ(amount of txns since prev)
    drift            = current_balance - expected_current

Plaid convention: outflows are positive, inflows negative, so a real balance
falls by the summed transaction amount. We include transfers here — unlike
spend aggregation, EVERY money movement affects the real balance.

Each run:
  1. ``reconcile_balances_activity`` — fetch accounts + latest prior snapshots
     + transactions, run the pure :func:`compute_drift`, return the (small)
     list of drifted accounts.
  2. ``emit_discrepancies_activity`` — upsert one ``finance.audit_discrepancy``
     bead per drifted account (PATCH an existing ``pending`` one in place so we
     don't pile up duplicates nightly).
  3. ``write_balance_snapshots_activity`` — write a fresh
     ``finance.balance_snapshot`` per account so the NEXT run has a baseline.
     Accounts with no prior snapshot are seeded only — no drift on first run.

``balance_snapshot`` and ``audit_discrepancy`` are free-form types (not in
``schemas.FINANCE_TYPE_SCHEMAS``), so no substrate schema change is needed.

Auth: every Substrate call goes through ``src.tools.finance.substrate_headers``
(X-API-Key required), matching the other finance workflows.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

# The finance tool module imports httpx at load, which trips the workflow
# sandbox. Activities are deterministic-free, so pass this through for the
# workflow validation pass only.
with workflow.unsafe.imports_passed_through():
    from src.tools.finance import create_bead, patch_bead, query_beads


# --- Tunables -------------------------------------------------------------

# Absolute USD difference above which we treat balance movement as real drift.
# Set above $0.00 (the brief's literal threshold) to absorb pending-vs-posted
# timing noise: a transaction can post a day after it cleared the balance.
DRIFT_TOLERANCE = 1.00

_SNAPSHOT_TYPE = "balance_snapshot"
_DISCREPANCY_TYPE = "audit_discrepancy"
_TXN_PAGE_SIZE = 1000
_MAX_TXN_PAGES = 100
_LIABILITY_ACCOUNT_TYPES = {"credit", "loan"}


# --- Pure helpers (deterministic; safe to call inside the workflow / tests) --

def _to_date(raw: Any) -> Optional[date]:
    """Parse an ISO date or datetime string to a ``date``. None on failure."""
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


def _latest_snapshot_by_account(snapshots: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index snapshot beads to the most-recent one per ``account_id``.

    Recency is keyed on ``content.as_of`` (the run timestamp), falling back to
    bead ``created_at`` so a snapshot missing as_of still orders sensibly.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for s in snapshots:
        content = s.get("content") or {}
        account_id = content.get("account_id")
        if not account_id:
            continue
        key = str(content.get("as_of") or s.get("created_at") or "")
        existing = latest.get(account_id)
        if existing is None or key > existing["_key"]:
            latest[account_id] = {
                "balance": content.get("balance"),
                "as_of": content.get("as_of") or s.get("created_at"),
                "_key": key,
            }
    return latest


def compute_drift(
    accounts: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    *,
    tolerance: float = DRIFT_TOLERANCE,
) -> List[Dict[str, Any]]:
    """Return one discrepancy dict per account whose balance has drifted.

    Pure and deterministic — no I/O, no clock, no RNG — so it is safe to unit
    test directly and (in principle) to run inside the workflow sandbox.

    Args:
      accounts: ``[{"id", "name", "type", "current_balance"}]`` (live account
        beads). ``credit``/``loan`` accounts are reconciled as liabilities:
        positive Plaid amounts increase the amount owed.
      snapshots: prior ``finance.balance_snapshot`` beads (any age; we pick the
        latest per account).
      transactions: ``finance.transaction`` beads. ``state == "removed"`` beads
        are ignored. Each contributes ``content.amount`` to its
        ``content.account_id``.
      tolerance: absolute USD drift below which an account is considered clean.

    Boundary convention: a transaction counts toward an account's window when
    its ``posted_date`` is strictly AFTER the prior snapshot's ``as_of`` date.
    Same-day transactions are assumed already reflected in that snapshot's
    balance (posted_date carries no time-of-day to disambiguate).

    Accounts with no prior snapshot are skipped (first-run seeding — no drift).
    """
    latest = _latest_snapshot_by_account(snapshots)

    # Sum non-removed transaction amounts per account, partitioned by whether
    # they fall after each account's prior-snapshot date.
    sums_after: Dict[str, float] = {}
    for tx in transactions:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        account_id = content.get("account_id")
        if not account_id or account_id not in latest:
            continue
        snap_date = _to_date(latest[account_id]["as_of"])
        tx_date = _to_date(content.get("posted_date") or content.get("authorized_date"))
        if snap_date is None or tx_date is None or tx_date <= snap_date:
            continue
        try:
            amt = float(content.get("amount"))
        except (TypeError, ValueError):
            continue
        sums_after[account_id] = sums_after.get(account_id, 0.0) + amt

    discrepancies: List[Dict[str, Any]] = []
    for acct in accounts:
        account_id = acct.get("id")
        snap = latest.get(account_id)
        if snap is None:
            continue  # no baseline yet — seeded this run, reconciled next run
        try:
            prev_balance = float(snap["balance"])
            current_balance = float(acct.get("current_balance"))
        except (TypeError, ValueError):
            continue
        net_flow = sums_after.get(account_id, 0.0)
        if _is_liability_account(acct):
            expected = round(prev_balance + net_flow, 2)
        else:
            expected = round(prev_balance - net_flow, 2)
        drift = round(current_balance - expected, 2)
        if abs(drift) <= tolerance:
            continue
        discrepancies.append(
            {
                "account_id": account_id,
                "account_name": acct.get("name"),
                "expected_balance": expected,
                "actual_balance": round(current_balance, 2),
                "drift": drift,
                "prev_snapshot_at": snap["as_of"],
            }
        )
    return discrepancies


# --- Activities -----------------------------------------------------------

async def _fetch_all_transactions() -> List[Dict[str, Any]]:
    """Page through every ``finance.transaction`` bead (offset pagination)."""
    results: List[Dict[str, Any]] = []
    for page in range(_MAX_TXN_PAGES):
        chunk = await query_beads(
            {
                "type": "transaction",
                "limit": _TXN_PAGE_SIZE,
                "offset": page * _TXN_PAGE_SIZE,
            }
        )
        results.extend(chunk)
        if len(chunk) < _TXN_PAGE_SIZE:
            break
    return results


def _account_state(bead: Dict[str, Any]) -> Dict[str, Any]:
    content = bead.get("content") or {}
    return {
        "id": bead.get("id"),
        "name": content.get("name"),
        "type": content.get("type"),
        "subtype": content.get("subtype"),
        "current_balance": content.get("current_balance"),
        "iso_currency_code": content.get("iso_currency_code"),
    }


def _is_liability_account(account: Dict[str, Any]) -> bool:
    return str(account.get("type") or "").lower() in _LIABILITY_ACCOUNT_TYPES


@activity.defn
async def reconcile_balances_activity() -> List[Dict[str, Any]]:
    """Fetch accounts + prior snapshots + transactions and return drifted
    accounts. Computation stays inside the activity so the (potentially large)
    transaction set never crosses the Temporal payload boundary.
    """
    accounts_raw = await query_beads({"type": "account", "state": "active", "limit": 200})
    accounts = [_account_state(a) for a in accounts_raw]
    snapshots = await query_beads({"type": _SNAPSHOT_TYPE, "limit": 5000})
    transactions = await _fetch_all_transactions()

    discrepancies = compute_drift(accounts, snapshots, transactions)
    logger.info(
        "Reconciliation: %d accounts, %d prior snapshots, %d transactions -> %d drifted.",
        len(accounts), len(snapshots), len(transactions), len(discrepancies),
    )
    return discrepancies


@activity.defn
async def emit_discrepancies_activity(discrepancies: List[Dict[str, Any]]) -> int:
    """Upsert one ``finance.audit_discrepancy`` (state=pending) per drifted account.

    Two rules here, and both exist because this lifecycle used to erase its own
    evidence. the Operator: *"I have drift detection that I don't really [think] serves
    the purpose its supposed to."* LO-REC-001 records the diagnosis — the math is
    sound and well-tested, the lifecycle was not: detection was instantaneous and
    memoryless, so drift was a nightly weather report rather than a measure of
    ledger integrity.

    **A clean run does not resolve an open discrepancy.** It used to. Because
    ``write_balance_snapshots_activity`` writes a fresh snapshot every night, the
    next run reconciles against a baseline that already contains the unexplained
    gap — so a one-off $400 discrepancy on night 1 goes clean on night 2 and was
    marked ``resolved``. Nothing was found and nothing was fixed. The money is
    still missing and the system said it was not. Marking something resolved that
    nobody resolved is worse than never flagging it, because it converts an open
    question into a false answer.

    A clean run is still real information, and it is recorded as what it is: no
    NEW drift was observed. Only an explanation or a correction resolves.

    **A refresh does not overwrite the first observation.** The old code PATCHed
    content wholesale, so ``drift`` and the window became tonight's values and
    whatever was first seen was gone — the record then claimed tonight's gap had
    been pending since it was created.

    Returns the number of beads written, refreshed, or marked as still-open.
    """
    existing = await query_beads({"type": _DISCREPANCY_TYPE, "state": "pending", "limit": 1000})
    pending_by_account: Dict[str, Dict[str, Any]] = {}
    for b in existing:
        account_id = (b.get("content") or {}).get("account_id")
        if account_id and account_id not in pending_by_account:
            pending_by_account[account_id] = b

    drifted_account_ids = {disc["account_id"] for disc in discrepancies}
    now = datetime.now().isoformat()
    written = 0
    for disc in discrepancies:
        match = pending_by_account.get(disc["account_id"])
        prior = (match.get("content") or {}) if match is not None else {}
        content = {
            "account_id": disc["account_id"],
            "account_name": disc.get("account_name"),
            "expected_balance": disc["expected_balance"],
            "actual_balance": disc["actual_balance"],
            "drift": disc["drift"],
            "prev_snapshot_at": disc.get("prev_snapshot_at"),
            "window_end": now,
            # The first sighting survives every later refresh. Without it the
            # record cannot say how long the gap has been open or how big it was
            # when it appeared, which is most of what makes it an integrity
            # measure rather than a nightly reading.
            "first_observed_at": prior.get("first_observed_at") or now,
            "first_observed_drift": (
                prior["first_observed_drift"]
                if "first_observed_drift" in prior
                else disc["drift"]
            ),
        }
        if match is not None:
            await patch_bead(
                match["id"],
                content=content,
                created_by="ledger-reconciliation/refresh_discrepancy",
            )
        else:
            await create_bead(
                _DISCREPANCY_TYPE,
                content,
                state="pending",
                created_by="ledger-reconciliation/emit_discrepancy",
            )
        written += 1

    for account_id, pending in pending_by_account.items():
        if account_id in drifted_account_ids:
            continue
        # Clean tonight, still unexplained. Record the observation and leave the
        # discrepancy OPEN. Resolving here is the defect: the account reconciles
        # because the baseline moved, not because anyone accounted for the money.
        prior = pending.get("content") or {}
        content = dict(prior)
        content["last_clean_run_at"] = now
        content["clean_runs_since_first_observed"] = (
            int(prior.get("clean_runs_since_first_observed") or 0) + 1
        )
        await patch_bead(
            pending["id"],
            content=content,
            created_by="ledger-reconciliation/observed_clean_still_open",
        )
        written += 1

    logger.info("Emitted/refreshed/held-open %d audit_discrepancy beads.", written)
    return written


# The only two ways a discrepancy legitimately closes. A clean night is not one
# of them, which is the whole point of this lifecycle: the account reconciling
# again does not mean the money was accounted for.
RESOLUTION_ACCEPTED = "accepted"      # a human looked and accepted the gap
RESOLUTION_CORRECTED = "corrected"    # a correcting transaction accounts for it
_VALID_RESOLUTIONS = {RESOLUTION_ACCEPTED, RESOLUTION_CORRECTED}


async def resolve_discrepancy(
    bead_id: str,
    *,
    resolution: str,
    resolved_by: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a discrepancy, recording WHICH of the two ways closed it.

    Deliberately not an activity and not reachable from the nightly workflow.
    Nothing on a schedule may resolve a discrepancy — if it could, the defect
    this lifecycle exists to prevent would return through whichever door was
    left open.
    """
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {sorted(_VALID_RESOLUTIONS)}, got {resolution!r}. "
            "A discrepancy closes because someone accepted it or because a correction "
            "accounts for it — never because time passed."
        )
    if not str(resolved_by).strip():
        raise ValueError("resolved_by is required: a resolution with no author is not one")

    # patch_bead does a whole-content replace — the API has no field-level merge —
    # so the current content has to be read first or resolving would silently drop
    # first_observed_at and everything else the record carries. There is no
    # single-bead read in this client, so the discrepancy is located by id within
    # its own type.
    candidates = await query_beads({"type": _DISCREPANCY_TYPE, "limit": 1000})
    existing = next((b for b in candidates if b.get("id") == bead_id), None)
    if existing is None:
        raise ValueError(f"no {_DISCREPANCY_TYPE} bead with id {bead_id!r}")
    content = dict(existing.get("content") or {})
    content["resolution"] = resolution
    content["resolved_by"] = resolved_by
    content["resolved_at"] = datetime.now().isoformat()
    if note:
        content["resolution_note"] = note

    return await patch_bead(
        bead_id,
        content=content,
        state="resolved",
        created_by=f"ledger-reconciliation/resolve_{resolution}",
    )


@activity.defn
async def write_balance_snapshots_activity() -> int:
    """Write a fresh ``finance.balance_snapshot`` bead per account so the next
    run has a baseline to reconcile against. Returns the count written."""
    accounts_raw = await query_beads({"type": "account", "state": "active", "limit": 200})
    as_of = datetime.now().isoformat()
    written = 0
    for a in accounts_raw:
        content = a.get("content") or {}
        balance = content.get("current_balance")
        if balance is None:
            continue
        await _create_snapshot(a.get("id"), balance, content.get("iso_currency_code"), as_of)
        written += 1
    logger.info("Wrote %d balance_snapshot beads.", written)
    return written


async def _create_snapshot(
    account_id: Optional[str], balance: Any, currency: Any, as_of: str
) -> None:
    content = {
        "account_id": account_id,
        "balance": balance,
        "iso_currency_code": currency,
        "as_of": as_of,
    }
    await create_bead(
        _SNAPSHOT_TYPE,
        content,
        "active",
        "ledger-reconciliation/snapshot",
        trust_tier="system",
    )


# --- Workflow -------------------------------------------------------------

@workflow.defn
class LedgerReconciliationWorkflow:
    """Nightly balance-delta reconciliation across all finance accounts."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        # 1. Detect drift against the PRIOR snapshots (before writing new ones).
        discrepancies = await workflow.execute_activity(
            reconcile_balances_activity,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        # 2. Surface drift as pending audit_discrepancy beads.
        emitted = await workflow.execute_activity(
            emit_discrepancies_activity,
            discrepancies,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        # 3. Snapshot tonight's balances LAST, so the next run has a baseline.
        snapshots = await workflow.execute_activity(
            write_balance_snapshots_activity,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        return {
            "accounts_drifted": len(discrepancies),
            "discrepancies_emitted": emitted,
            "snapshots_written": snapshots,
        }


# --- Cumulative ledger integrity (LO-REC-002, LO-REC-004) -------------------
#
# LO-REC-002 is what the Operator means by "serves the purpose": a standing answer to
# "how wrong is my ledger, in dollars, right now". The nightly delta answers a
# different question — what moved last night — and the registry's note is exact:
# "The current design deliberately traded the true anchor for a delta
# approximation; this requirement is the replacement for what that trade gave
# up."
#
# The figure is built from the OPEN discrepancy records, and specifically from
# `first_observed_drift` rather than `drift`. That distinction is the whole
# measure. `drift` is what the account showed on the most recent run, and after
# a re-baseline that is near zero for a gap that has been open for weeks.
# `first_observed_drift` is the money that went missing and was never accounted
# for. Summing the current values would report a healthy ledger with a hole in
# it — the defect TA-2 removed, arriving through a different door.
#
# Everything here derives from the bank feed and the recorded ledger. Nothing
# depends on manual entry or manual reconciliation, per LO-REC-004 and RC-5:
# the Operator has never reconciled by hand and will not start.


def _variance_of(content: Dict[str, Any]) -> float:
    """The unexplained dollar amount a single open discrepancy represents."""
    raw = content.get("first_observed_drift")
    if raw is None:
        raw = content.get("drift")
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return 0.0


def compute_ledger_variance(
    discrepancies: List[Dict[str, Any]],
    accounts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Cumulative unexplained variance per account and in total.

    Pure and deterministic — no I/O, no clock — so the lifecycle it measures can
    be asserted directly.

    Args:
      discrepancies: ``finance.audit_discrepancy`` beads, in ANY state. Resolved
        ones are needed too: they establish how long the measure has been keeping
        score for an account even after their dollars stop counting.
      accounts: live account beads, so an account that has never produced a
        discrepancy can be reported as measured-and-clean rather than omitted.

    Every figure carries the anchor date it counts from. A dollar number without
    the date it starts at is not interpretable, and reporting one without the
    other is the failure this whole track exists to end.

    ``None`` for an account's variance means NOT YET MEASURING — no anchor has
    been established. Zero means measured and clean. Collapsing those two is how
    a blank graph reports itself as healthy.
    """
    by_account: Dict[str, Dict[str, Any]] = {}

    for bead in discrepancies:
        content = bead.get("content") or {}
        account_id = content.get("account_id")
        if not account_id:
            continue
        entry = by_account.setdefault(
            account_id,
            {
                "account_id": account_id,
                "account_name": content.get("account_name"),
                "unexplained_variance": 0.0,
                "open_discrepancies": 0,
                "anchor_date": None,
            },
        )
        if content.get("account_name") and not entry["account_name"]:
            entry["account_name"] = content.get("account_name")

        # The anchor is the earliest thing this account has ever recorded,
        # resolved or not — it is when the measure started, not when the current
        # gap appeared.
        first_seen = content.get("first_observed_at") or content.get("window_end")
        if first_seen and (entry["anchor_date"] is None or str(first_seen) < entry["anchor_date"]):
            entry["anchor_date"] = str(first_seen)

        # Only what is still unexplained counts toward the dollars. A resolved
        # record keeps contributing its anchor and nothing else.
        if bead.get("state") == "pending":
            entry["unexplained_variance"] = round(
                entry["unexplained_variance"] + _variance_of(content), 2
            )
            entry["open_discrepancies"] += 1

    for account in accounts or []:
        account_id = account.get("id")
        if not account_id or account_id in by_account:
            continue
        # Live, and has never recorded a discrepancy. There is no anchor, so
        # there is nothing to report a number against yet.
        by_account[account_id] = {
            "account_id": account_id,
            "account_name": account.get("name"),
            "unexplained_variance": None,
            "open_discrepancies": 0,
            "anchor_date": None,
        }

    rows = sorted(by_account.values(), key=lambda r: r["account_id"])
    measured = [r for r in rows if r["anchor_date"] is not None]
    unmeasured = [r for r in rows if r["anchor_date"] is None]
    for row in unmeasured:
        row["unexplained_variance"] = None

    anchors = [r["anchor_date"] for r in measured if r["anchor_date"]]
    return {
        "accounts": rows,
        "total": {
            "unexplained_variance": round(
                sum(r["unexplained_variance"] or 0.0 for r in measured), 2
            ),
            # A total is never quietly computed over a subset. Both numbers are
            # stated so a reader can see what the figure does not cover.
            "accounts_measured": len(measured),
            "accounts_not_measured": len(unmeasured),
            "anchor_date": min(anchors) if anchors else None,
            "open_discrepancies": sum(r["open_discrepancies"] for r in measured),
        },
    }


async def get_ledger_variance() -> Dict[str, Any]:
    """Read path for the cumulative integrity figure.

    Uses the finance read path the console and the MCP tools already share; no
    new transport, port or service is introduced.
    """
    discrepancies = await query_beads({"type": _DISCREPANCY_TYPE, "limit": 1000})
    accounts = await query_beads({"type": "account", "state": "active", "limit": 200})
    return compute_ledger_variance(
        discrepancies,
        [{"id": a.get("id"), "name": (a.get("content") or {}).get("name")} for a in accounts],
    )
