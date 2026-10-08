"""Route-level coverage for the off-loop decryption paths (Milestone 2.5 S1).

Bulk GETs decrypt via asyncio.to_thread; these tests drive the real
routes through FastAPI with a faked DB session to prove the thread-hop
round-trips ciphertext back to plaintext. Run like the other tests:

    DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
    SUBSTRATE_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
    SUBSTRATE_API_KEY=test pytest tests/
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
from src.crypto import encrypt_jsonb
from src.database import get_db
from src.routes import router


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _query):
        return _FakeResult(self._rows)


def _fake_bead(content: dict, context: dict, parent_id=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace="finance",
        type="transaction",
        state="active",
        parent_id=parent_id,
        content=encrypt_jsonb(content, "finance"),
        context=encrypt_jsonb(context, "finance"),
        confidence=0.9,
        trust_tier="verified",
        provenance={},
        created_by="test",
        created_at=now,
        updated_at=now,
    )


def _fake_plain_bead(namespace: str, bead_type: str, content: dict, context=None, parent_id=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace=namespace,
        type=bead_type,
        state="active",
        parent_id=parent_id,
        content=content,
        context=context or {},
        confidence=0.9,
        trust_tier="verified",
        provenance={},
        created_by="test",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    # DISABLE_CACHE is read at import time; patch the module flag so the
    # route skips redis instead of attempting a real connection.
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    app = FastAPI()
    app.include_router(router)
    return app


def test_list_beads_decrypts_off_loop(client):
    content = {"amount": 42.5, "merchant": "Coffee", "plaid_transaction_id": "tx_1"}
    rows = [_fake_bead(content, {"note": "hi"})]
    client.dependency_overrides[get_db] = lambda: _FakeSession(rows)

    resp = TestClient(client).get("/beads", headers={"x-api-key": "test-key"})

    assert resp.status_code == 200
    (bead,) = resp.json()
    # Encrypted leaves came back as plaintext values, not {"_enc": ...}.
    assert bead["content"]["amount"] == 42.5
    assert bead["content"]["merchant"] == "Coffee"
    assert bead["content"]["plaid_transaction_id"] == "tx_1"
    assert bead["context"]["note"] == "hi"


def test_list_beads_filters_by_parent_id(client):
    from uuid import uuid4

    parent_id = uuid4()
    other_parent_id = uuid4()
    matching = _fake_bead({"amount": 10.0, "plaid_transaction_id": "tx_match"}, {}, parent_id)
    other = _fake_bead({"amount": 20.0, "plaid_transaction_id": "tx_other"}, {}, other_parent_id)

    class _ParentFilteringSession(_FakeSession):
        async def execute(self, query):
            params = query.compile().params
            filter_parent_id = next(
                (value for key, value in params.items() if key.startswith("parent_id_")),
                None,
            )
            if filter_parent_id is None:
                return _FakeResult(self._rows)
            return _FakeResult(
                [row for row in self._rows if str(row.parent_id) == str(filter_parent_id)]
            )

    client.dependency_overrides[get_db] = lambda: _ParentFilteringSession([matching, other])

    resp = TestClient(client).get(
        f"/beads?parent_id={parent_id}", headers={"x-api-key": "test-key"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [bead["content"]["plaid_transaction_id"] for bead in body] == ["tx_match"]
    assert body[0]["parent_id"] == str(parent_id)


def test_list_beads_rejects_unknown_query_params(client):
    client.dependency_overrides[get_db] = lambda: _FakeSession([])

    resp = TestClient(client).get(
        "/beads?namespace=arch&content.ref=app.substrate",
        headers={"x-api-key": "test-key"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_query_parameter"
    assert detail["parameters"] == ["content.ref"]


def test_list_beads_filters_by_content_ref_with_namespace_and_type(client):
    matching = _fake_plain_bead(
        "arch",
        "arch.application",
        {"ref": "app.substrate", "name": "Substrate"},
    )
    wrong_ref = _fake_plain_bead(
        "arch",
        "arch.application",
        {"ref": "app.console", "name": "LifeOps Console"},
    )
    wrong_type = _fake_plain_bead(
        "arch",
        "arch.capability",
        {"ref": "app.substrate", "name": "Wrong type"},
    )

    class _ContentRefFilteringSession(_FakeSession):
        def __init__(self, rows):
            super().__init__(rows)
            self.statement = None

        async def execute(self, query):
            self.statement = query
            params = query.compile().params
            filter_namespace = next(
                (value for key, value in params.items() if key.startswith("namespace_")),
                None,
            )
            filter_type = next(
                (value for key, value in params.items() if key.startswith("type_")),
                None,
            )
            filter_ref = next(
                (
                    value
                    for value in params.values()
                    if value in {"app.substrate", "app.console"}
                ),
                None,
            )
            rows = self._rows
            if filter_namespace is not None:
                rows = [row for row in rows if row.namespace == filter_namespace]
            if filter_type is not None:
                rows = [row for row in rows if row.type == filter_type]
            if filter_ref is not None:
                rows = [row for row in rows if row.content.get("ref") == filter_ref]
            return _FakeResult(rows)

    session = _ContentRefFilteringSession([matching, wrong_ref, wrong_type])
    client.dependency_overrides[get_db] = lambda: session

    resp = TestClient(client).get(
        "/beads?namespace=arch&type=arch.application&content_ref=app.substrate",
        headers={"x-api-key": "test-key"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [bead["content"]["ref"] for bead in body] == ["app.substrate"]
    compiled = str(session.statement.compile())
    assert "->>" in compiled


def test_finance_summary_aggregates_off_loop(client):
    rows = [
        _fake_bead({"current_balance": 100.0, "iso_currency_code": "USD"}, {}),
        _fake_bead({"current_balance": 50.5, "iso_currency_code": "USD"}, {}),
        _fake_bead({"current_balance": "not-a-number"}, {}),
    ]
    client.dependency_overrides[get_db] = lambda: _FakeSession(rows)

    resp = TestClient(client).get(
        "/beads/finance/summary", headers={"x-api-key": "test-key"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_balance"] == pytest.approx(150.5)
    assert body["account_count"] == 2
    assert body["by_currency"]["USD"] == pytest.approx(150.5)


def test_list_beads_single_flight_collapses_concurrent_misses(monkeypatch):
    """Concurrent identical cache misses run ONE fetch; followers hit cache.

    Regression for 2026-07-18: five bank-sync workflows fired the same
    type=transaction&limit=5000 query in the same second, every miss ran
    the full fetch+decrypt concurrently, and the combined CPU pushed all
    of them past the caller's 30s ReadTimeout.
    """
    import asyncio

    from src import routes

    calls = {"db": 0}
    store: dict[str, list] = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, beads, ttl=300):
        store[key] = beads

    monkeypatch.setattr(routes, "get_cached_beads", fake_get)
    monkeypatch.setattr(routes, "set_cached_beads", fake_set)
    routes._list_beads_locks.clear()

    rows = [_fake_bead({"amount": 1.0, "plaid_transaction_id": "tx_sf"}, {})]

    class _SlowSession(_FakeSession):
        async def execute(self, _query):
            calls["db"] += 1
            # Hold the lock long enough for the follower to arrive.
            await asyncio.sleep(0.05)
            return _FakeResult(self._rows)

    session = _SlowSession(rows)

    async def scenario():
        return await asyncio.gather(
            routes.list_beads(namespace="finance", type="transaction", limit=5000, db=session),
            routes.list_beads(namespace="finance", type="transaction", limit=5000, db=session),
        )

    first, second = asyncio.run(scenario())

    assert calls["db"] == 1, "follower should be served from the leader-filled cache"
    assert len(first) == 1
    assert len(second) == 1


def test_list_beads_cache_key_includes_parent_id(monkeypatch):
    """Different parent filters must not share a Redis cache entry."""
    import asyncio

    from src import routes

    parent_id = uuid4()
    other_parent_id = uuid4()
    calls = {"db": 0}
    store: dict[str, list] = {}
    set_keys: list[str] = []

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, beads, ttl=300):
        set_keys.append(key)
        store[key] = beads

    monkeypatch.setattr(routes, "get_cached_beads", fake_get)
    monkeypatch.setattr(routes, "set_cached_beads", fake_set)
    routes._list_beads_locks.clear()

    class _ParentAwareSession(_FakeSession):
        async def execute(self, query):
            calls["db"] += 1
            params = query.compile().params
            filter_parent_id = next(
                (value for key, value in params.items() if key.startswith("parent_id_")),
                None,
            )
            rows = [_fake_bead({"plaid_transaction_id": str(filter_parent_id)}, {}, filter_parent_id)]
            return _FakeResult(rows)

    session = _ParentAwareSession([])

    async def scenario():
        first = await routes.list_beads(parent_id=str(parent_id), db=session)
        second = await routes.list_beads(parent_id=str(other_parent_id), db=session)
        return first, second

    first, second = asyncio.run(scenario())

    assert calls["db"] == 2
    assert len(set_keys) == 2
    assert set_keys[0] != set_keys[1]
    assert f":{parent_id}:" in set_keys[0]
    assert f":{other_parent_id}:" in set_keys[1]
    assert first[0].parent_id == parent_id
    assert second[0].parent_id == other_parent_id


def test_list_beads_cache_key_includes_content_ref(monkeypatch):
    """Different content_ref filters must not share a Redis cache entry."""
    import asyncio

    from src import routes

    calls = {"db": 0}
    store: dict[str, list] = {}
    set_keys: list[str] = []

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, beads, ttl=300):
        set_keys.append(key)
        store[key] = beads

    monkeypatch.setattr(routes, "get_cached_beads", fake_get)
    monkeypatch.setattr(routes, "set_cached_beads", fake_set)
    routes._list_beads_locks.clear()

    class _RefAwareSession(_FakeSession):
        async def execute(self, query):
            calls["db"] += 1
            params = query.compile().params
            filter_ref = next(
                (
                    value
                    for value in params.values()
                    if value in {"app.substrate", "app.console"}
                ),
                None,
            )
            return _FakeResult(
                [
                    _fake_plain_bead(
                        "arch",
                        "arch.application",
                        {"ref": filter_ref, "name": filter_ref},
                    )
                ]
            )

    session = _RefAwareSession([])

    async def scenario():
        first = await routes.list_beads(content_ref="app.substrate", db=session)
        second = await routes.list_beads(content_ref="app.console", db=session)
        return first, second

    first, second = asyncio.run(scenario())

    assert calls["db"] == 2
    assert len(set_keys) == 2
    assert set_keys[0] != set_keys[1]
    assert set_keys[0].endswith(":content_ref:app.substrate")
    assert set_keys[1].endswith(":content_ref:app.console")
    assert first[0].content["ref"] == "app.substrate"
    assert second[0].content["ref"] == "app.console"
