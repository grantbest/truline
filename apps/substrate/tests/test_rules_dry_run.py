"""Route coverage for the SDD Phase 4 Rule Sandbox dry-run.

Drives ``POST /rules/dry-run`` through FastAPI with a faked DB session, the
same harness shape as test_routes_offload.py. Run like the other tests:

    DATABASE_URL=postgresql+asyncpg://x:x@localhost/x \
    SUBSTRATE_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
    SUBSTRATE_API_KEY=test pytest tests/
"""

import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
from src import routes
from src.crypto import encrypt_jsonb
from src.database import get_db
from src.rules import RuleSpec, dry_run_rule
from src.rules import router as rules_router


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


def _txn(content: dict):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace="finance",
        type="transaction",
        state="active",
        parent_id=None,
        content=encrypt_jsonb(content, "finance"),
        context=encrypt_jsonb({}, "finance"),
        confidence=0.9,
        trust_tier="user",
        provenance={},
        created_by="test",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    # Force the cold path (no Redis); the route then queries + decrypts.
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    application = FastAPI()
    application.include_router(rules_router)
    return application


def _rows():
    return [
        _txn({"normalized_merchant": "Amazon.com", "merchant_name": "AMZN Mktp",
              "amount": 12.0, "plaid_transaction_id": "tx_a"}),  # uncategorized → will change
        _txn({"normalized_merchant": "Amazon Fresh", "amount": 8.0,
              "our_category": "Groceries", "plaid_transaction_id": "tx_b"}),  # matches but already categorized
        _txn({"normalized_merchant": "Starbucks", "amount": 5.0,
              "plaid_transaction_id": "tx_c"}),  # no match
    ]


def _post(app, body):
    app.dependency_overrides[get_db] = lambda: _FakeSession(_rows())
    return TestClient(app).post("/rules/dry-run", json=body, headers={"x-api-key": "test-key"})


def test_dry_run_counts_matches_and_changes(app):
    resp = _post(app, {
        "field": "normalized_merchant",
        "operator": "contains",
        "value": "amazon",
        "target_category": "Shopping",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scanned"] == 3
    assert body["matched"] == 2           # both Amazon rows
    assert body["beads_affected"] == 1    # only the Uncategorized one changes
    # Diffs surface the will-change row first.
    first = body["sample_diffs"][0]
    assert first["will_change"] is True
    assert first["old"] == "Uncategorized"
    assert first["new"] == "Shopping"
    # The already-categorized match is present but flagged skipped.
    skipped = [d for d in body["sample_diffs"] if not d["will_change"]]
    assert skipped and skipped[0]["old"] == "Groceries"


def test_dry_run_vendor_aliases_to_normalized_merchant(app):
    # Spec's canonical rule uses field "vendor", which transactions lack.
    resp = _post(app, {
        "field": "vendor",
        "operator": "starts_with",
        "value": "Amazon",
        "target_category": "Shopping",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched"] == 2


def test_dry_run_invalid_regex_is_422(app):
    resp = _post(app, {
        "field": "normalized_merchant",
        "operator": "regex",
        "value": "(unclosed",
        "target_category": "Shopping",
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_regex"


def test_dry_run_requires_api_key(app):
    app.dependency_overrides[get_db] = lambda: _FakeSession(_rows())
    resp = TestClient(app).post("/rules/dry-run", json={
        "field": "normalized_merchant", "operator": "contains",
        "value": "amazon", "target_category": "Shopping",
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cache coherence — these tests run with the cache ENABLED (an in-memory
# double stands in for Redis; DISABLE_CACHE is never set). They call the
# route handlers directly rather than through TestClient so multiple calls
# share one asyncio event loop — routes.list_beads' single-flight lock is a
# module-level asyncio.Lock keyed by cache_key, and reusing it across event
# loops raises "bound to a different event loop".
# ---------------------------------------------------------------------------


class _CountingSession(_FakeSession):
    """Tracks DB round trips, the injected double the assertions count on."""

    def __init__(self, rows):
        super().__init__(rows)
        self.execute_calls = 0

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeResult(self._rows)


def _cache_double():
    """In-memory stand-in for get_cached_beads/set_cached_beads, recording
    every key used so tests can compare read keys against write keys instead
    of hardcoding the key shape."""
    store: dict[str, list] = {}
    get_keys: list[str] = []
    set_keys: list[str] = []

    async def fake_get(key):
        get_keys.append(key)
        return store.get(key)

    async def fake_set(key, beads, ttl=300):
        set_keys.append(key)
        store[key] = beads

    return get_keys, set_keys, fake_get, fake_set


def _redis_glob_to_regex(pattern: str) -> re.Pattern:
    parts = []
    escaped = False
    for char in pattern:
        if escaped:
            parts.append(re.escape(char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    if escaped:
        parts.append(re.escape("\\"))
    return re.compile(f"^{''.join(parts)}$")


class _FakeRedis:
    """Minimal get/set/scan/delete/flushdb double — no real Redis server."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def scan(self, cursor=0, match=None, count=None):
        pattern = _redis_glob_to_regex(match)
        return 0, [k for k in self.store if pattern.match(k)]

    async def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                count += 1
        return count

    async def flushdb(self):
        self.store.clear()


def _rule() -> RuleSpec:
    return RuleSpec(
        field="normalized_merchant",
        operator="contains",
        value="amazon",
        target_category="Shopping",
    )


def test_dry_run_second_call_reuses_cache_without_refetch(monkeypatch):
    """AC1: an unchanged population's second dry-run is served from cache —
    asserted by counting DB fetches through the injected _CountingSession."""
    get_keys, set_keys, fake_get, fake_set = _cache_double()
    monkeypatch.setattr(routes, "get_cached_beads", fake_get)
    monkeypatch.setattr(routes, "set_cached_beads", fake_set)
    routes._list_beads_locks.clear()

    session = _CountingSession(_rows())
    rule = _rule()

    async def scenario():
        first = await dry_run_rule(rule, db=session)
        second = await dry_run_rule(rule, db=session)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.scanned == 3
    assert second.scanned == 3
    assert session.execute_calls == 1, "second dry-run should be served from cache, not re-fetched"
    assert set_keys, "first dry-run should have warmed the cache"
    # AC2: derive both keys from the code that built them, not from a
    # hardcoded string — the sandbox must read the key it (or list_beads)
    # actually wrote.
    assert get_keys[-1] == set_keys[-1]


def test_dry_run_shares_cache_key_with_list_beads(monkeypatch):
    """AC2: the key the sandbox reads is a key list_beads actually writes —
    warm the cache via list_beads directly, the way the ledger console does,
    then confirm the sandbox reuses it instead of refetching."""
    get_keys, set_keys, fake_get, fake_set = _cache_double()
    monkeypatch.setattr(routes, "get_cached_beads", fake_get)
    monkeypatch.setattr(routes, "set_cached_beads", fake_set)
    routes._list_beads_locks.clear()

    session = _CountingSession(_rows())
    rule = _rule()

    async def scenario():
        await routes.list_beads(
            request=None, namespace="finance", type="transaction",
            limit=5000, offset=0, db=session,
        )
        return await dry_run_rule(rule, db=session)

    result = asyncio.run(scenario())

    assert result.scanned == 3
    assert session.execute_calls == 1, "dry-run must reuse list_beads' warm cache, not refetch"
    assert set_keys[-1] in get_keys, "dry-run must have read the key list_beads wrote"


def test_dry_run_cache_invalidated_by_same_write_as_list_beads(monkeypatch):
    """AC3: the write that invalidates list_beads' cache must invalidate the
    sandbox's cached read too, so a dry-run never reports against a stale
    population. Runs against a fake Redis (get/set/scan/delete/flushdb) —
    no real Redis server, cache stays enabled throughout."""
    fake_redis = _FakeRedis()

    async def fake_get_redis():
        return fake_redis

    monkeypatch.setattr(src.cache, "get_redis", fake_get_redis)
    routes._list_beads_locks.clear()

    session = _CountingSession(_rows())
    rule = _rule()

    async def scenario():
        first = await dry_run_rule(rule, db=session)
        second = await dry_run_rule(rule, db=session)
        await src.cache.invalidate_cache("finance")
        third = await dry_run_rule(rule, db=session)
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first.scanned == second.scanned == third.scanned == 3
    assert session.execute_calls == 2, (
        "second call should hit cache (no refetch); invalidating finance's "
        "cache should force the third call to refetch rather than serve stale beads"
    )
