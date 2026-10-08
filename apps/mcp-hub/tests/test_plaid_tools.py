from datetime import date

import pytest

from tools import plaid as plaid_tools


class _FakePlaidClient:
    """Captures the LinkTokenCreateRequest so tests can assert its shape."""

    def __init__(self):
        self.request = None

    def link_token_create(self, request):
        self.request = request
        return {"link_token": "link-sandbox-fake"}


@pytest.mark.asyncio
async def test_create_link_token_create_mode_includes_products(monkeypatch):
    fake = _FakePlaidClient()
    monkeypatch.setattr(plaid_tools, "get_client", lambda: fake)

    token = await plaid_tools.create_link_token("truline-chase")

    assert token == "link-sandbox-fake"
    req = fake.request.to_dict()
    assert "products" in req
    assert "access_token" not in req


class _FakeExchangeClient:
    def item_public_token_exchange(self, request):
        return {"access_token": "access-sandbox-abc", "item_id": "item-xyz"}


@pytest.mark.asyncio
async def test_exchange_public_token_returns_token_and_item_id(monkeypatch):
    """item_id must surface to callers — it's the only handle webhooks and
    /item/remove have on the Item (Item registry, PR 3)."""
    monkeypatch.setattr(plaid_tools, "get_client", lambda: _FakeExchangeClient())

    result = await plaid_tools.exchange_public_token("public-sandbox-123")

    assert result == {
        "access_token": "access-sandbox-abc",
        "item_id": "item-xyz",
    }


@pytest.mark.asyncio
async def test_create_link_token_update_mode_omits_products(monkeypatch):
    """Update mode: Plaid rejects `products` alongside `access_token`, so the
    request must carry the token and nothing product-related."""
    fake = _FakePlaidClient()
    monkeypatch.setattr(plaid_tools, "get_client", lambda: fake)

    token = await plaid_tools.create_link_token(
        "truline-chase", access_token="access-sandbox-123"
    )

    assert token == "link-sandbox-fake"
    req = fake.request.to_dict()
    assert req["access_token"] == "access-sandbox-123"
    assert "products" not in req
    assert "required_if_supported_products" not in req


class _FakeTx:
    def __init__(self, txid, pending=False):
        self._d = {"transaction_id": txid, "pending": pending}

    def to_dict(self):
        return dict(self._d)

    def __getitem__(self, k):
        return self._d[k]


class _FakeTransactionsGetClient:
    def __init__(self, pages, total):
        self._pages = list(pages)
        self._total = total
        self.offsets = []

    def transactions_get(self, request):
        self.offsets.append(request.to_dict()["options"]["offset"])
        return {"transactions": self._pages.pop(0), "total_transactions": self._total}


@pytest.mark.asyncio
async def test_get_transactions_paginates_past_first_page(monkeypatch):
    """The old single-page pull silently dropped rows past the first 500.
    The loop must keep fetching until total_transactions is covered."""
    page1 = [_FakeTx("t0"), _FakeTx("t1")]
    page2 = [_FakeTx("t-last"), _FakeTx("t-pending", pending=True)]
    fake = _FakeTransactionsGetClient([page1, page2], total=4)
    monkeypatch.setattr(plaid_tools, "get_client", lambda: fake)

    txs = await plaid_tools.get_transactions(
        "tok", date(2026, 1, 1), date(2026, 3, 1)
    )

    assert fake.offsets == [0, 2]
    assert [t["transaction_id"] for t in txs] == ["t0", "t1", "t-last"]


class _FakeSyncClient:
    def __init__(self, payload):
        self._payload = payload
        self.request = None

    def transactions_sync(self, request):
        self.request = request
        payload = self._payload

        class _Resp:
            def to_dict(self):
                return payload

        return _Resp()


@pytest.mark.asyncio
async def test_sync_transactions_page_initial_omits_cursor(monkeypatch):
    fake = _FakeSyncClient(
        {
            "added": [{"transaction_id": "t1"}],
            "modified": None,
            "removed": [],
            "next_cursor": "c1",
            "has_more": False,
        }
    )
    monkeypatch.setattr(plaid_tools, "get_client", lambda: fake)

    page = await plaid_tools.sync_transactions_page("tok")

    assert "cursor" not in fake.request.to_dict()
    assert page == {
        "added": [{"transaction_id": "t1"}],
        "modified": [],
        "removed": [],
        "next_cursor": "c1",
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_sync_transactions_page_passes_cursor(monkeypatch):
    fake = _FakeSyncClient(
        {"added": [], "modified": [], "removed": [], "next_cursor": "c2", "has_more": True}
    )
    monkeypatch.setattr(plaid_tools, "get_client", lambda: fake)

    page = await plaid_tools.sync_transactions_page("tok", "c1")

    assert fake.request.to_dict()["cursor"] == "c1"
    assert page["next_cursor"] == "c2"
    assert page["has_more"] is True
