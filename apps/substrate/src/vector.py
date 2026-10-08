"""Vector indexing client — Phase 4.3 (Semantic Substrate).

**Retired 2026-09-04.** The Gemini API key that backed embeddings (direct
and via the LiteLLM gateway) was revoked as part of the Operator's 2026-09-04
decision — see docs/plans/2026-09-04-decision-record-claude-loops.md's
"Loose end surfaced, not decided" addendum, resolved: delete, no
migration. No replacement embedding provider is in scope. ``embed_text``
now raises ``EmbeddingProviderRetiredError`` unconditionally instead of
calling out to a route that no longer resolves to a live credential.

The Qdrant plumbing (collection lifecycle, upsert, search) is left in
place below — it costs nothing idle, needs no data migration for
whatever is already indexed, and is provider-agnostic — but nothing can
successfully embed a bead or a query until a new provider is chosen and
``embed_text`` is reimplemented against it.

The module is intentionally HTTP-only — no ``qdrant-client`` SDK — so the
substrate container stays slim. Qdrant is reached over plain HTTP
in-cluster (NetworkPolicy is the boundary, per ARCHITECTURE §3.10.1).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


# --- Configuration -------------------------------------------------------

# No manifest sets an embedding dimension anymore — GEMINI_EMBED_DIM (which
# production had overridden to 3072, for gemini-embedding-001) was deleted
# from infrastructure/k8s/base/substrate/substrate.yaml along with the rest
# of the retired Gemini config. Nothing produces vectors of any dimension
# until a replacement provider exists; this constant only matters for the
# ``ensure_collection`` call on a Qdrant instance that has no collection
# yet, so its exact value is inert. Kept at the old dev-default so a
# pre-existing collection created before the retirement still matches.
_VECTOR_DIM = 768

GEMINI_EMBED_RETIREMENT_NOTICE = (
    "docs/plans/2026-09-04-decision-record-claude-loops.md"
)

QDRANT_URL = os.environ.get(
    "QDRANT_URL", "http://qdrant.platform-substrate.svc.cluster.local:6333"
).rstrip("/")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "beads")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")  # optional; in-cluster usually unauth

# Qdrant uses cosine for text embeddings by convention; matches Gemini's
# normalized output.
QDRANT_DISTANCE = "Cosine"

_EMBED_TIMEOUT = float(os.environ.get("EMBED_HTTP_TIMEOUT", "20"))
_QDRANT_TIMEOUT = float(os.environ.get("QDRANT_HTTP_TIMEOUT", "10"))


class VectorConfigError(RuntimeError):
    """Raised when the embedding path is unusable."""


class EmbeddingProviderRetiredError(VectorConfigError):
    """Raised on every embedding attempt after the 2026-09-04 Gemini retirement."""


EMBEDDING_RETIREMENT_MESSAGE = (
    "Bead vector search is retired: the Gemini API key that powered embeddings "
    "(direct and via LiteLLM) was revoked 2026-09-04 — see "
    f"{GEMINI_EMBED_RETIREMENT_NOTICE}. No replacement embedding provider is "
    "configured; this is not a misconfiguration to fix by re-setting an env var."
)


def _qdrant_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY
    return headers


# --- Bead → search text --------------------------------------------------

# Limit the JSON we shove at the embedder. Gemini accepts long inputs but
# (a) cost scales with tokens even on the free tier (quota), and (b)
# extremely long content tends to dilute the embedding. 4 KB is enough to
# capture content + provenance for typical beads.
_MAX_TEXT_CHARS = 4096


def bead_search_text(bead: Mapping[str, Any]) -> str:
    """Render a deterministic text representation of a bead for embedding.

    The order matters — embedding the same bead twice MUST produce the
    same vector. We sort the JSON keys for that reason.
    """
    parts = [
        f"namespace: {bead.get('namespace', '')}",
        f"type: {bead.get('type', '')}",
        f"state: {bead.get('state', '')}",
        f"created_by: {bead.get('created_by', '')}",
    ]
    content = bead.get("content") or {}
    if content:
        parts.append("content: " + json.dumps(content, sort_keys=True, default=str))
    context = bead.get("context") or {}
    if context:
        parts.append("context: " + json.dumps(context, sort_keys=True, default=str))
    text = "\n".join(parts)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    return text


# --- Embeddings — retired 2026-09-04 --------------------------------------

async def embed_text(
    text: str,
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
    client: Optional[httpx.AsyncClient] = None,
) -> List[float]:
    """Raise ``EmbeddingProviderRetiredError``.

    Both call paths this used to dispatch to (direct Gemini, and Gemini
    via the LiteLLM gateway) depended on a Google API key that was
    revoked 2026-09-04. Rather than let either path attempt a network
    call and surface a bare 401/connection error, fail immediately with a
    message naming the decision — this is not something a caller can fix
    by setting an env var.
    """
    raise EmbeddingProviderRetiredError(EMBEDDING_RETIREMENT_MESSAGE)


async def embed_bead(bead: Mapping[str, Any]) -> List[float]:
    """Convenience: serialize a bead and embed it as a document."""
    return await embed_text(bead_search_text(bead), task_type="RETRIEVAL_DOCUMENT")


# --- Qdrant operations ---------------------------------------------------

async def ensure_collection(client: Optional[httpx.AsyncClient] = None) -> None:
    """Create the bead collection in Qdrant if it doesn't exist yet.

    Safe to call on every startup — the PUT call is idempotent for an
    already-existing collection with the same schema. We don't migrate
    schemas here; changing the dim/distance is an explicit ops task.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=_QDRANT_TIMEOUT)
    try:
        # Probe first so we don't spam Qdrant logs with PUT noise.
        resp = await client.get(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
            headers=_qdrant_headers(),
        )
        if resp.status_code == 200:
            return
        if resp.status_code not in (404,):
            resp.raise_for_status()

        create_payload = {
            "vectors": {"size": _VECTOR_DIM, "distance": QDRANT_DISTANCE}
        }
        put = await client.put(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
            json=create_payload,
            headers=_qdrant_headers(),
        )
        put.raise_for_status()
        logger.info(
            "Qdrant collection %s created (dim=%d, distance=%s)",
            QDRANT_COLLECTION,
            _VECTOR_DIM,
            QDRANT_DISTANCE,
        )
    finally:
        if own_client:
            await client.aclose()


def _bead_payload(bead: Mapping[str, Any]) -> Dict[str, Any]:
    """Routing tags stored alongside the vector — used for Qdrant
    payload-level filtering ("only return finance.transaction beads")
    without round-tripping to Postgres."""
    return {
        "bead_id": str(bead.get("id", "")),
        "namespace": bead.get("namespace"),
        "type": bead.get("type"),
        "state": bead.get("state"),
        "trust_tier": bead.get("trust_tier"),
        "created_by": bead.get("created_by"),
    }


async def upsert_bead_vector(
    bead: Mapping[str, Any],
    vector: Sequence[float],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Insert (or replace) the bead's point in Qdrant.

    Qdrant point id is the bead UUID — repeat calls for the same bead
    overwrite the old vector, which is exactly what we want when a bead
    transitions and its embedding shifts.
    """
    bead_id = bead.get("id")
    if bead_id is None:
        raise ValueError("Cannot upsert a bead without an 'id'")

    payload = {
        "points": [
            {
                "id": str(bead_id),
                "vector": list(vector),
                "payload": _bead_payload(bead),
            }
        ]
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=_QDRANT_TIMEOUT)
    try:
        # wait=true so we don't return until the point is durable; this
        # workflow is event-driven, not throughput-critical.
        resp = await client.put(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points?wait=true",
            json=payload,
            headers=_qdrant_headers(),
        )
        resp.raise_for_status()
    finally:
        if own_client:
            await client.aclose()


async def search_similar_beads(
    query_text: str,
    *,
    limit: int = 10,
    namespace: Optional[str] = None,
    type_: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """Embed ``query_text`` and return Qdrant's nearest points.

    Each returned dict has ``bead_id``, ``score``, and the stored payload.
    Optional ``namespace`` / ``type_`` filters apply at the Qdrant layer
    so we don't burn vector slots on unrelated beads.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=_QDRANT_TIMEOUT)
    try:
        vector = await embed_text(query_text, task_type="RETRIEVAL_QUERY", client=client)

        must_filters: List[Dict[str, Any]] = []
        if namespace:
            must_filters.append({"key": "namespace", "match": {"value": namespace}})
        if type_:
            must_filters.append({"key": "type", "match": {"value": type_}})

        search_payload: Dict[str, Any] = {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
        }
        if must_filters:
            search_payload["filter"] = {"must": must_filters}

        resp = await client.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
            json=search_payload,
            headers=_qdrant_headers(),
        )
        resp.raise_for_status()
        body = resp.json()
    finally:
        if own_client:
            await client.aclose()

    hits: List[Dict[str, Any]] = []
    for point in body.get("result", []) or []:
        payload = point.get("payload") or {}
        hits.append(
            {
                "bead_id": payload.get("bead_id") or str(point.get("id")),
                "score": point.get("score"),
                "payload": payload,
            }
        )
    return hits


# --- Index-on-event helper ----------------------------------------------

async def index_bead_by_id(
    bead_id: UUID | str,
    fetch_bead,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Fetch a bead from Postgres via ``fetch_bead(bead_id)`` and upsert
    its embedding into Qdrant.

    ``fetch_bead`` is injected to avoid a circular import between the
    vector module and the FastAPI app. Returns True on success, False
    when the bead was not found (e.g. deleted before we got to it).
    """
    bead = await fetch_bead(bead_id)
    if bead is None:
        logger.info("Bead %s not found; skipping vector upsert", bead_id)
        return False

    vector = await embed_bead(bead)
    await upsert_bead_vector(bead, vector, client=client)
    return True


__all__ = [
    "EMBEDDING_RETIREMENT_MESSAGE",
    "EmbeddingProviderRetiredError",
    "QDRANT_COLLECTION",
    "QDRANT_URL",
    "VectorConfigError",
    "bead_search_text",
    "embed_bead",
    "embed_text",
    "ensure_collection",
    "index_bead_by_id",
    "search_similar_beads",
    "upsert_bead_vector",
]
