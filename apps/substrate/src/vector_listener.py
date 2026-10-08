"""Real-time vector indexing — Phase 4.3 (Semantic Substrate).

**Retired 2026-09-04** (see ``vector.py``'s module docstring and
docs/plans/2026-09-04-decision-record-claude-loops.md): every embedding
attempt now fails with ``EmbeddingProviderRetiredError``. The listener
below still runs and still LISTENs — bead creation/state transitions do
not depend on it — but each event is a guaranteed no-op logged at
WARNING, not indexed.

Subscribes to the Postgres ``bead_event`` channel installed by migration
0001_baseline (``notify_bead_event`` trigger) and upserts the latest
embedding for the bead into Qdrant. Runs as a background task in the
substrate FastAPI process — no separate worker pod.

Why not a Temporal workflow?
----------------------------
ARCHITECTURE §3.6 mandates ``LISTEN/NOTIFY`` as the event spine until
fan-out proves it inadequate. Vector indexing is a single-subscriber
projection of bead writes — exactly the shape Postgres pub/sub handles
well. A Temporal workflow per bead would dwarf the actual work
(one embedding call + one upsert). We can promote to Temporal later if
indexing needs richer retry/visibility.

Robustness
----------
- ``asyncpg`` connection drops are caught + retried with exponential
  backoff (capped at ``_MAX_BACKOFF_SECONDS``).
- Per-event failures are logged but do NOT bring the listener down —
  one bead failing to embed must not stall the spine.
- The notify payload carries bead id + tags, enough to refetch from
  Postgres without a separate query.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import asyncpg
import httpx
from sqlalchemy import select

from .crypto import decrypt_jsonb
from .database import async_session
from .models import Bead
from .vector import (
    EmbeddingProviderRetiredError,
    ensure_collection,
    index_bead_by_id,
)

logger = logging.getLogger(__name__)


_CHANNEL = "bead_event"
_MAX_BACKOFF_SECONDS = 30.0
_INITIAL_BACKOFF_SECONDS = 1.0


# --- DSN translation -----------------------------------------------------

def _asyncpg_dsn() -> str:
    """asyncpg doesn't accept the ``postgresql+asyncpg://`` SQLAlchemy
    scheme — strip the driver suffix so the same DATABASE_URL works for
    both SQLAlchemy and the raw LISTEN connection."""
    raw = os.environ["DATABASE_URL"]
    if raw.startswith("postgresql+asyncpg://"):
        return "postgresql://" + raw[len("postgresql+asyncpg://"):]
    if raw.startswith("postgres+asyncpg://"):
        return "postgresql://" + raw[len("postgres+asyncpg://"):]
    return raw


# --- Bead loader (callback for vector.index_bead_by_id) ------------------

async def _fetch_bead_dict(bead_id: str) -> Optional[Dict[str, Any]]:
    """Load a bead from Postgres as a plain dict.

    Returns ``None`` if the row vanished between the NOTIFY and the
    fetch (shouldn't happen today, but keeps the listener defensive for
    future delete semantics)."""
    async with async_session() as session:
        result = await session.execute(select(Bead).filter(Bead.id == bead_id))
        bead = result.scalar_one_or_none()
        if bead is None:
            return None
        return {
            "id": str(bead.id),
            "namespace": bead.namespace,
            "type": bead.type,
            "state": bead.state,
            "trust_tier": bead.trust_tier,
            "created_by": bead.created_by,
            # Embeddings must be computed on plaintext — Phase 5 ALE
            # stores content/context as selectively encrypted JSONB.
            "context": decrypt_jsonb(bead.context),
            "content": decrypt_jsonb(bead.content),
            "provenance": bead.provenance or {},
        }


# --- Notification handler ------------------------------------------------

async def _handle_notification(
    payload_text: str,
    *,
    qdrant_client: httpx.AsyncClient,
) -> None:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        logger.warning("bead_event payload was not JSON: %s", payload_text[:200])
        return

    bead_id = payload.get("id")
    if not bead_id:
        logger.warning("bead_event payload missing 'id': %s", payload)
        return

    try:
        indexed = await index_bead_by_id(
            bead_id, _fetch_bead_dict, client=qdrant_client
        )
        if indexed:
            logger.debug(
                "Indexed bead %s (%s/%s/%s)",
                bead_id,
                payload.get("namespace"),
                payload.get("type"),
                payload.get("state"),
            )
    except EmbeddingProviderRetiredError as exc:
        # Expected on every event since 2026-09-04 — one line, no traceback,
        # so this doesn't read as an incident to whoever tails the logs.
        # The bead write itself already committed; only indexing is skipped.
        logger.warning("Bead %s not indexed: %s", bead_id, exc)
    except Exception as exc:  # noqa: BLE001 — never stall the spine
        logger.exception("Failed to index bead %s: %s", bead_id, exc)


# --- Listener loop -------------------------------------------------------

async def run_listener(stop_event: asyncio.Event) -> None:
    """Long-running coroutine: connect, LISTEN, dispatch.

    Reconnects on connection loss with exponential backoff. Returns when
    ``stop_event`` is set (typically on FastAPI shutdown)."""
    logger.warning(
        "Vector indexing is retired (2026-09-04): the Gemini API key that "
        "powered embeddings was revoked and no replacement provider is "
        "configured — see docs/plans/2026-09-04-decision-record-claude-loops.md. "
        "The listener still runs (bead writes are unaffected) but every event "
        "will be skipped, not indexed."
    )
    try:
        await ensure_collection()
    except Exception as exc:  # noqa: BLE001
        # Qdrant might not be up yet at startup; we'll retry on the first
        # event by re-calling ensure_collection inside the loop. But log
        # it loudly so it's visible.
        logger.warning("ensure_collection failed at startup: %s", exc)

    backoff = _INITIAL_BACKOFF_SECONDS
    dsn = _asyncpg_dsn()
    safe_dsn_host = urlparse(dsn).hostname or "?"

    async with httpx.AsyncClient(timeout=30.0) as qdrant_client:
        while not stop_event.is_set():
            conn: Optional[asyncpg.Connection] = None
            try:
                conn = await asyncpg.connect(dsn=dsn)
                logger.info(
                    "Vector listener connected to %s, LISTENing on %s",
                    safe_dsn_host,
                    _CHANNEL,
                )
                backoff = _INITIAL_BACKOFF_SECONDS

                queue: asyncio.Queue[str] = asyncio.Queue()

                def _on_notify(_conn, _pid, _channel, payload):
                    # asyncpg invokes this from the conn's event loop —
                    # offload onto the queue so handler work doesn't
                    # block further notifications.
                    queue.put_nowait(payload)

                await conn.add_listener(_CHANNEL, _on_notify)

                while not stop_event.is_set():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # Periodic wake-up so we notice stop_event promptly
                        # without holding queue.get() forever.
                        continue
                    await _handle_notification(payload, qdrant_client=qdrant_client)

            except (asyncpg.PostgresError, OSError, ConnectionError) as exc:
                logger.warning(
                    "Vector listener connection error (%s); reconnecting in %.1fs",
                    exc, backoff,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Vector listener crashed: %s", exc)
            finally:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass

            if stop_event.is_set():
                break

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    logger.info("Vector listener stopped.")


# --- Lifecycle helpers used by FastAPI ----------------------------------

class ListenerHandle:
    """Holds the asyncio Task + stop event so the FastAPI shutdown hook
    can cleanly cancel the listener."""

    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.stop_event.clear()
        self.task = asyncio.create_task(
            run_listener(self.stop_event), name="vector-listener"
        )

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=10.0)
            except asyncio.TimeoutError:
                self.task.cancel()


__all__ = ["ListenerHandle", "run_listener"]
