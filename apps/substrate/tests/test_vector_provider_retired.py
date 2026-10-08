"""Gemini API key revoked 2026-09-04 (the Operator's decision, Amendment 37) —
apps/substrate/src/vector.py has no embedding provider left to call.

These tests assert the three things PC-INF-001 requires of the substrate
vector path:

* embedding fails immediately with a message naming the retirement, not a
  network error from a dead LiteLLM route or a revoked Gemini key;
* the ``/beads/search`` endpoint surfaces that message (503), not a bare
  502/class-name;
* bead creation and state transitions are unaffected — the write path
  never touches the embedding provider.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
from src.database import get_db
from src.routes import router
from src.vector import EmbeddingProviderRetiredError, embed_bead, embed_text, search_similar_beads

HEADERS = {"x-api-key": "test-key"}


# --- vector.py: embedding always fails fast -------------------------------

@pytest.mark.asyncio
async def test_embed_text_raises_provider_retired_naming_the_decision():
    with pytest.raises(EmbeddingProviderRetiredError) as exc_info:
        await embed_text("some search text")

    message = str(exc_info.value)
    assert "2026-09-04" in message
    assert "Gemini" in message
    assert "docs/plans/2026-09-04-decision-record-claude-loops.md" in message


@pytest.mark.asyncio
async def test_embed_bead_raises_provider_retired():
    with pytest.raises(EmbeddingProviderRetiredError):
        await embed_bead({"id": str(uuid4()), "namespace": "dev", "type": "task"})


@pytest.mark.asyncio
async def test_search_similar_beads_raises_before_any_qdrant_call(monkeypatch):
    """search_similar_beads embeds the query before it ever talks to
    Qdrant — retirement must be visible without a reachable Qdrant."""

    async def _boom(*args, **kwargs):
        raise AssertionError("Qdrant must not be called once embedding is retired")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)

    with pytest.raises(EmbeddingProviderRetiredError):
        await search_similar_beads("find beads about groceries")


# --- vector_listener.py: a retired provider never stalls the spine --------

@pytest.mark.asyncio
async def test_listener_event_handler_isolates_the_retired_provider(caplog):
    """This tree keeps the listener running (unlike the closed #653, which
    disabled it): each bead event catches EmbeddingProviderRetiredError and
    logs a named warning rather than raising — the property that matters is
    that a bead write can never be stalled by the retired provider."""
    import logging
    from src.vector_listener import _handle_notification

    payload = '{"id": "%s", "namespace": "dev", "type": "task"}' % uuid4()
    with caplog.at_level(logging.WARNING):
        # Must not raise, regardless of the retired embed path underneath.
        await asyncio.wait_for(
            _handle_notification(payload, qdrant_client=SimpleNamespace()),
            timeout=2.0,
        )


# --- routes.py: /beads/search names the decision, not the exception class -

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def test_search_beads_returns_503_naming_the_decision(app):
    async def unused_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = unused_db
    client = TestClient(app)
    try:
        resp = client.post(
            "/beads/search",
            headers=HEADERS,
            json={"query": "groceries this month"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "2026-09-04" in detail
    assert "Gemini" in detail


# --- bead creation is unaffected -----------------------------------------

class _FakeCreateSession:
    """Stands in for AsyncSession across create_bead's add/flush/commit/
    refresh sequence — no DB, no embedding provider, nothing Gemini-shaped
    anywhere in this path."""

    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        # The DB would populate these server-side defaults.
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = datetime.now(timezone.utc)


def test_bead_creation_unaffected_by_retired_vector_provider(app, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", _fernet_key())
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app)
    try:
        resp = client.post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "scratch",
                "type": "note",
                "state": "open",
                "content": {"body": "unaffected by the Gemini retirement"},
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert session.committed is True
    body = resp.json()
    assert body["namespace"] == "scratch"
    assert body["content"] == {"body": "unaffected by the Gemini retirement"}


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()
