"""Phase 6: 2-year historical backfill of Plaid transactions.

Why a dedicated workflow (vs. extending BankSyncWorkflow)
---------------------------------------------------------
* **Different cadence.** Nightly sync pulls a small delta (~24h). Backfill
  pulls 720 days in 90-day chunks and is one-shot per institution.
* **Different failure mode.** A nightly-sync failure means one day of
  missing data; a half-finished backfill means a *gap* in the wealth
  history. So backfill checkpoints aggressively (one status bead per
  chunk) and is designed to resume cleanly after a worker restart.
* **Different rate-limit budget.** ``workflow.sleep(5)`` between chunks
  keeps us under Plaid's `/transactions/get` rate ceiling without
  starving the nightly sync's quota.

Resume contract
---------------
The workflow stores a single ``finance.backfill_status`` bead per
institution. ``content.last_completed_date`` is the *earliest* date for
which we've successfully pulled transactions; the next chunk we attempt
is ``[last_completed_date - 90 days, last_completed_date]``. On startup
the workflow reads the existing status bead (if any) and resumes from
that boundary. If ``state == "completed"`` we no-op and return early —
the caller can force a re-run by deleting the bead.

Deduplication
-------------
We do NOT rely on a per-chunk dedup query. Substrate's partial unique
index on ``content->>'plaid_transaction_id'`` (migration 0002) returns
409 on duplicates, and ``write_transaction_bead_activity`` swallows the
409 silently — so overlapping windows at chunk boundaries are safe by
construction. This is the contract Part 4 of the Phase 6 brief calls
out as "the 409 Conflict logic already implemented in Substrate".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    import httpx
    from src.tools.finance import substrate_beads_url, substrate_headers
    from src.workflows.bank_sync import (
        categorize_transactions_bulk_activity,
        fetch_liabilities_activity,
        list_accounts_activity,
        pull_transactions_activity,
        write_account_bead_activity,
        write_transaction_bead_activity,
    )


# --- Tunables -------------------------------------------------------------

# Phase 6 brief: 2-year history, 90-day chunks → 8 chunks. We compute
# total_chunks at runtime so changing the constants here is the single
# point of truth.
_BACKFILL_TOTAL_DAYS = 730  # ~2 years, leap-safe overshoot
_BACKFILL_CHUNK_DAYS = 90
_INTER_CHUNK_SLEEP_SECONDS = 5  # rate-limit cushion per brief
_BACKFILL_BEAD_TYPE = "backfill_status"
_FINANCE_NAMESPACE = "finance"


def _resume_cursor_from_last_completed(today: date, last_completed_date: str) -> date:
    """Return the upper bound for the next chunk after a checkpoint.

    ``last_completed_date`` is the earliest date covered by the last
    successful chunk. The next chunk should end on that exact date, not
    ``+1 day``; using ``+1`` shifts the next 90-day window forward and
    can skip the oldest boundary day after a kill/restart.
    """
    return min(today, date.fromisoformat(last_completed_date))


# --- Activities -----------------------------------------------------------

@activity.defn
async def fetch_backfill_status_activity(institution_slug: str) -> Optional[Dict[str, Any]]:
    """Return the most recent ``finance.backfill_status`` bead for this
    institution, or ``None`` if no backfill has ever run.

    Substrate's list endpoint sorts by ``created_at`` desc, so the first
    matching bead is the freshest — older status beads may exist if a
    backfill was force-restarted, but we always honour the latest one.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            substrate_beads_url(),
            params={
                "namespace": _FINANCE_NAMESPACE,
                "type": _BACKFILL_BEAD_TYPE,
                "limit": 50,
            },
            headers=substrate_headers(),
        )
        resp.raise_for_status()
        for bead in resp.json():
            content = bead.get("content") or {}
            if content.get("institution") == institution_slug:
                return {
                    "id": bead["id"],
                    "state": bead.get("state"),
                    "content": content,
                }
    return None


@activity.defn
async def upsert_backfill_status_activity(
    institution_slug: str,
    last_completed_date: str,
    chunks_completed: int,
    total_chunks: int,
    state: str,
    existing_bead_id: Optional[str] = None,
) -> str:
    """Create-or-update the per-institution ``backfill_status`` bead.

    The bead is the *only* durable resume marker. We write after every
    successful chunk so a worker crash mid-backfill loses at most one
    chunk's worth of work — which is dedup-safe to repeat.
    """
    beads_url = substrate_beads_url()
    content = {
        "institution": institution_slug,
        "last_completed_date": last_completed_date,
        "chunks_completed": chunks_completed,
        "total_chunks": total_chunks,
        "updated_at": datetime.now().isoformat(),
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        if existing_bead_id:
            # PATCH path: update state + content together so the
            # in_progress → completed transition is atomic with the
            # final chunk's data.
            patch_resp = await client.patch(
                f"{beads_url}/{existing_bead_id}",
                json={
                    "state": state,
                    "content": content,
                    "created_by": "bank-sync/backfill",
                },
                headers=substrate_headers(),
            )
            patch_resp.raise_for_status()
            return existing_bead_id

        post_resp = await client.post(
            beads_url,
            json={
                "namespace": _FINANCE_NAMESPACE,
                "type": _BACKFILL_BEAD_TYPE,
                "state": state,
                "content": content,
                "trust_tier": "system",
                "created_by": "bank-sync/backfill",
            },
            headers=substrate_headers(),
        )
        post_resp.raise_for_status()
        return post_resp.json()["id"]


# --- Workflow -------------------------------------------------------------

@workflow.defn
class HistoricalBackfillWorkflow:
    """Idempotent 2-year backfill, resumable across worker restarts."""

    @workflow.run
    async def run(
        self,
        institution_slug: str = "chase",
        force: bool = False,
    ) -> Dict[str, Any]:
        # All clocks via workflow.now() — required for determinism.
        today = workflow.now().date()
        oldest_target = today - timedelta(days=_BACKFILL_TOTAL_DAYS)
        total_chunks = (_BACKFILL_TOTAL_DAYS + _BACKFILL_CHUNK_DAYS - 1) // _BACKFILL_CHUNK_DAYS

        default_retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=120),
            maximum_attempts=3,
        )


        # --- Step 1: Resume probe -------------------------------------
        # The single most important read in this workflow. Get it wrong
        # and we re-fetch 2 years of transactions on every retry.
        status = await workflow.execute_activity(
            fetch_backfill_status_activity,
            institution_slug,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        existing_bead_id: Optional[str] = None
        chunks_completed = 0
        # `cursor` is the upper bound of the NEXT chunk to fetch. We
        # walk it backward by _BACKFILL_CHUNK_DAYS each iteration.
        cursor: date = today

        if status:
            existing_bead_id = status["id"]
            content = status["content"]
            prior_state = status.get("state")
            last_done_str = content.get("last_completed_date")

            if prior_state == "completed" and not force:
                # The whole 2-year window is already on disk. No-op
                # unless the caller explicitly asks for a re-run.
                return {
                    "institution": institution_slug,
                    "status": "already_completed",
                    "last_completed_date": last_done_str,
                    "chunks_completed": content.get("chunks_completed"),
                }

            if last_done_str:
                try:
                    # Resume with the previous chunk's earliest date as
                    # the next upper bound. That re-attempts boundary
                    # transactions posted ON last_completed_date, and
                    # the 409 dedup makes that overlap free.
                    cursor = _resume_cursor_from_last_completed(today, last_done_str)
                    chunks_completed = int(content.get("chunks_completed") or 0)
                    workflow.logger.info(
                        "backfill: resuming %s from cursor=%s (%d/%d chunks done)",
                        institution_slug, cursor.isoformat(),
                        chunks_completed, total_chunks,
                    )
                except ValueError:
                    workflow.logger.warning(
                        "backfill: malformed last_completed_date=%r — restarting",
                        last_done_str,
                    )

        # --- Step 2: Account upsert (cheap, always do it) -------------
        # The transaction beads carry account_id, so the account bead
        # must exist before we start writing tx beads. We also refresh
        # liabilities here — a backfill run is a perfectly good moment
        # to capture the current credit/loan picture.
        accounts = await workflow.execute_activity(
            list_accounts_activity,
            institution_slug,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=default_retry,
        )
        liability_map: Dict[str, Dict[str, Any]] = await workflow.execute_activity(
            fetch_liabilities_activity,
            institution_slug,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=default_retry,
        )
        account_map: Dict[str, str] = {}
        for acc in accounts:
            bead_id = await workflow.execute_activity(
                write_account_bead_activity,
                args=[acc, liability_map.get(acc["account_id"])],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=default_retry,
            )
            account_map[acc["account_id"]] = bead_id

        # --- Step 3: Chunked backward sweep ---------------------------
        total_written = 0
        total_chunks_run = 0

        while cursor > oldest_target:
            chunk_until = cursor
            chunk_since = max(oldest_target, cursor - timedelta(days=_BACKFILL_CHUNK_DAYS))

            workflow.logger.info(
                "backfill: chunk %s -> %s (institution=%s)",
                chunk_since.isoformat(), chunk_until.isoformat(), institution_slug,
            )

            transactions = await workflow.execute_activity(
                pull_transactions_activity,
                args=[institution_slug, chunk_since.isoformat(), chunk_until.isoformat()],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=default_retry,
            )

            # We deliberately skip the per-chunk dedup pre-fetch used by
            # nightly sync. Substrate's partial unique index on
            # plaid_transaction_id returns 409 and write_transaction_
            # bead_activity treats that as a no-op, so chunk overlap is
            # free.
            # Filter transactions to find only those that actually need to be categorized and written
            to_categorize = []
            tx_accounts = []
            for t in transactions:
                account_bead_id = account_map.get(t["account_id"])
                if account_bead_id is None:
                    continue
                to_categorize.append(t)
                tx_accounts.append(account_bead_id)

            written_in_chunk = 0
            if to_categorize:
                categorizations = await workflow.execute_activity(
                    categorize_transactions_bulk_activity,
                    args=[to_categorize],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=default_retry,
                )

                write_semaphore = asyncio.Semaphore(5)
                written_lock = asyncio.Lock()

                async def write_tx(t, acc_id, cat):
                    nonlocal written_in_chunk
                    async with write_semaphore:
                        written = await workflow.execute_activity(
                            write_transaction_bead_activity,
                            args=[t, acc_id, cat],
                            start_to_close_timeout=timedelta(minutes=5),
                            retry_policy=default_retry,
                        )
                        if written:
                            async with written_lock:
                                written_in_chunk += 1

                await asyncio.gather(*[
                    write_tx(t, acc_id, cat)
                    for t, acc_id, cat in zip(to_categorize, tx_accounts, categorizations)
                ])

            total_written += written_in_chunk
            total_chunks_run += 1
            chunks_completed += 1

            # --- Checkpoint: write the status bead BEFORE we sleep ---
            # If the worker dies during the sleep, we re-enter at this
            # exact chunk boundary on the next run.
            existing_bead_id = await workflow.execute_activity(
                upsert_backfill_status_activity,
                args=[
                    institution_slug,
                    chunk_since.isoformat(),
                    chunks_completed,
                    total_chunks,
                    "in_progress",
                    existing_bead_id,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=default_retry,
            )

            cursor = chunk_since
            # Don't sleep after the final chunk — pointless wait.
            if cursor > oldest_target:
                await workflow.sleep(timedelta(seconds=_INTER_CHUNK_SLEEP_SECONDS))

        # --- Step 4: Mark complete ------------------------------------
        await workflow.execute_activity(
            upsert_backfill_status_activity,
            args=[
                institution_slug,
                oldest_target.isoformat(),
                chunks_completed,
                total_chunks,
                "completed",
                existing_bead_id,
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=default_retry,
        )

        return {
            "institution": institution_slug,
            "status": "completed",
            "chunks_run_this_invocation": total_chunks_run,
            "chunks_completed": chunks_completed,
            "total_chunks": total_chunks,
            "transactions_written_this_invocation": total_written,
            "oldest_date_covered": oldest_target.isoformat(),
        }
