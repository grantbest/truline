"""`PATCH /beads/{id}` must encrypt content/context in the event it records.

``create_bead`` has always encrypted its ``bead_event.payload`` content/context
before writing (routes.py). ``update_bead`` did not: it wrote
``update.model_dump()`` raw, which for an ``ENCRYPTED_NAMESPACES`` bead
(``finance``) put plaintext amounts/merchants into an append-only audit table
that nothing ever cleans — the exact store ALE exists to protect. These tests
pin the fix at the route layer; ``tests/test_migration_0007.py`` covers the
data-repair half for rows the bug already wrote.

No live Postgres: the DB session is faked, same pattern as
``test_transition.py``.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
from src import crypto
from src.database import get_db
from src.models import BeadEvent
from src.routes import router


HEADERS = {"x-api-key": "test-key"}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_singleton", None)
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def _client(app, session):
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def _fake_bead(namespace: str, type_: str = "budget", state: str = "active"):
    # "budget" (finance) and "comment" (dev) are deliberately types with no
    # entry in NAMESPACE_TYPE_SCHEMAS, so _validate_content_or_422 passes
    # through arbitrary content and these tests exercise encryption, not the
    # content-schema gate.
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace=namespace,
        type=type_,
        state=state,
        trust_tier="system",
        parent_id=None,
        content={},
        context={},
        provenance={},
        confidence=None,
        created_at=now,
        updated_at=now,
        created_by="test",
    )


class _FakeSession:
    def __init__(self, bead):
        self.bead = bead
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, _obj):
        return None

    def events(self):
        return [o for o in self.added if isinstance(o, BeadEvent)]


def _patch(client, bead_id, body):
    return client.patch(f"/beads/{bead_id}", json=body, headers=HEADERS)


def test_encrypted_namespace_update_event_payload_is_ciphertext(app):
    bead = _fake_bead(namespace="finance")
    session = _FakeSession(bead)

    resp = _patch(
        _client(app, session),
        bead.id,
        {"content": {"amount": 42.5, "merchant": "Coffee Shop"}},
    )

    assert resp.status_code == 200
    [event] = session.events()
    assert "_enc" in event.payload["content"]["amount"]
    assert "_enc" in event.payload["content"]["merchant"]
    assert crypto.decrypt_jsonb(event.payload["content"]) == {
        "amount": 42.5,
        "merchant": "Coffee Shop",
    }


def test_encrypted_namespace_update_event_encrypts_context_too(app):
    bead = _fake_bead(namespace="finance")
    session = _FakeSession(bead)

    resp = _patch(
        _client(app, session),
        bead.id,
        {"context": {"note": "reviewed by ops"}},
    )

    assert resp.status_code == 200
    [event] = session.events()
    assert "_enc" in event.payload["context"]["note"]


def test_non_encrypted_namespace_update_event_payload_is_plaintext(app):
    bead = _fake_bead(namespace="dev", type_="comment")
    session = _FakeSession(bead)

    resp = _patch(
        _client(app, session),
        bead.id,
        {"content": {"title": "not secret"}},
    )

    assert resp.status_code == 200
    [event] = session.events()
    assert event.payload["content"] == {"title": "not secret"}


def test_untouched_content_and_context_stay_none_in_the_event(app):
    """A confidence-only PATCH must not turn untouched content/context into {}."""
    bead = _fake_bead(namespace="finance")
    session = _FakeSession(bead)

    resp = _patch(_client(app, session), bead.id, {"confidence": 0.7})

    assert resp.status_code == 200
    [event] = session.events()
    assert event.payload["content"] is None
    assert event.payload["context"] is None
