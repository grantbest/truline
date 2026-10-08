import pytest
from datetime import datetime as real_datetime
from typing import get_args

from tools import finance
from tools.finance import substrate_beads_url, substrate_headers


def test_substrate_beads_url_accepts_base_url(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.platform-substrate.svc.cluster.local:8000")
    assert substrate_beads_url() == "http://substrate.platform-substrate.svc.cluster.local:8000/beads"


def test_substrate_beads_url_accepts_legacy_beads_url(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.platform-substrate.svc.cluster.local:8000/beads")
    assert substrate_beads_url() == "http://substrate.platform-substrate.svc.cluster.local:8000/beads"


def test_substrate_headers_requires_api_key(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SUBSTRATE_API_KEY"):
        substrate_headers()


def test_substrate_headers_sets_x_api_key(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    assert substrate_headers() == {"X-API-Key": "test-key"}


@pytest.mark.asyncio
async def test_get_budget_status_includes_manual_expenses_and_transactions(monkeypatch):
    async def fake_query_beads(params):
        bead_type = params["type"]
        if bead_type == "budget":
            return [
                {
                    "content": {
                        "category": "misc",
                        "amount": 10.0,
                    }
                },
                {
                    "content": {
                        "category": "groceries",
                        "amount": 100.0,
                    }
                },
            ]
        if bead_type == "expense":
            return [
                {
                    "created_at": "2026-05-02T12:00:00",
                    "content": {
                        "category": "misc",
                        "amount": 2.5,
                        "date": "2026-05-02",
                    },
                }
            ]
        if bead_type == "transaction":
            return [
                {
                    "created_at": "2026-05-03T12:00:00",
                    "content": {
                        "our_category": "misc",
                        "amount": "9.00",
                        "posted_date": "2026-05-03",
                    },
                },
                {
                    "created_at": "2026-05-04T12:00:00",
                    "content": {
                        "our_category": "groceries",
                        "amount": 32.0,
                        "posted_date": "2026-05-04",
                    },
                },
                {
                    "created_at": "2026-05-05T12:00:00",
                    "content": {
                        "our_category": "misc",
                        "amount": 99.0,
                        "posted_date": "2026-04-30",
                    },
                },
            ]
        raise AssertionError(f"unexpected bead query: {params}")

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)

    status = await finance.get_budget_status(month="2026-05")

    assert status["misc"]["budget"] == 10.0
    assert status["misc"]["spent"] == 11.5
    assert status["misc"]["remaining"] == -1.5
    assert status["groceries"]["spent"] == 32.0
    assert status["total"] == {
        "budget": 110.0,
        "spent": 43.5,
        "remaining": 66.5,
    }


@pytest.mark.asyncio
async def test_get_recent_transactions_orders_by_posted_date(monkeypatch):
    async def fake_query_all_beads(params, **kwargs):
        assert params == {"type": "transaction"}
        assert kwargs["page_size"] == 1000
        return [
            {
                "id": "created-newer-posted-older",
                "state": "posted",
                "created_at": "2026-07-13T12:00:00",
                "content": {
                    "posted_date": "2026-07-09",
                    "amount": 10,
                    "our_category": "misc",
                    "institution": "chase",
                },
            },
            {
                "id": "created-older-posted-newer",
                "state": "posted",
                "created_at": "2026-07-10T12:00:00",
                "content": {
                    "posted_date": "2026-07-12",
                    "amount": 20,
                    "our_category": "groceries",
                    "institution": "chase",
                },
            },
            {
                "id": "removed-newest",
                "state": "removed",
                "created_at": "2026-07-13T13:00:00",
                "content": {
                    "posted_date": "2026-07-13",
                    "amount": 30,
                    "our_category": "misc",
                    "institution": "chase",
                },
            },
        ]

    monkeypatch.setattr(finance, "_query_all_beads", fake_query_all_beads)

    recent = await finance.get_recent_transactions(limit=10)

    assert [t["id"] for t in recent] == [
        "created-older-posted-newer",
        "created-newer-posted-older",
    ]


@pytest.mark.asyncio
async def test_get_recent_transactions_filters_transfer_category_and_institution(monkeypatch):
    async def fake_query_all_beads(params, **_kwargs):
        return [
            {
                "id": "keep",
                "state": "posted",
                "content": {
                    "posted_date": "2026-07-12",
                    "our_category": "groceries",
                    "institution": "chase",
                    "is_transfer": False,
                },
            },
            {
                "id": "wrong-category",
                "state": "posted",
                "content": {
                    "posted_date": "2026-07-12",
                    "our_category": "misc",
                    "institution": "chase",
                    "is_transfer": False,
                },
            },
            {
                "id": "wrong-institution",
                "state": "posted",
                "content": {
                    "posted_date": "2026-07-12",
                    "our_category": "groceries",
                    "institution": "amex",
                    "is_transfer": False,
                },
            },
            {
                "id": "transfer",
                "state": "posted",
                "content": {
                    "posted_date": "2026-07-13",
                    "our_category": "groceries",
                    "institution": "chase",
                    "is_transfer": True,
                },
            },
        ]

    monkeypatch.setattr(finance, "_query_all_beads", fake_query_all_beads)

    recent = await finance.get_recent_transactions(
        category="groceries",
        institution="chase",
        include_transfers=False,
    )

    assert [t["id"] for t in recent] == ["keep"]


def test_openapi_finance_category_enum_matches_finance_tools():
    import openapi_app

    assert set(get_args(openapi_app.FinanceCategory)) == set(finance.FINANCE_CATEGORIES)


@pytest.mark.asyncio
async def test_get_category_history_skips_reconciled_manual_expense_double_count(monkeypatch):
    class FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 26, tzinfo=tz)

    async def fake_query_all_beads(params, **_kwargs):
        if params["type"] == "transaction":
            return [
                {
                    "id": "tx-1",
                    "state": "reconciled",
                    "parent_id": "exp-1",
                    "content": {
                        "posted_date": "2026-05-15",
                        "amount": 100,
                        "our_category": "groceries",
                        "is_transfer": False,
                    },
                }
            ]
        if params["type"] == "expense":
            return [
                {
                    "id": "exp-1",
                    "state": "reconciled",
                    "content": {
                        "date": "2026-05-15",
                        "amount": 100,
                        "category": "groceries",
                    },
                },
                {
                    "id": "exp-2",
                    "state": "logged",
                    "content": {
                        "date": "2026-04-10",
                        "amount": 50,
                        "category": "misc",
                    },
                },
            ]
        raise AssertionError(f"unexpected bead query: {params}")

    monkeypatch.setattr(finance, "datetime", FixedDatetime)
    monkeypatch.setattr(finance, "_query_all_beads", fake_query_all_beads)

    history = await finance.get_category_history(months=6)

    assert history["month_keys"] == ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert history["categories"]["groceries"]["series"] == [0.0, 0.0, 0.0, 0.0, 0.0, 100.0]
    assert history["categories"]["groceries"]["avg"] == 16.67
    assert history["categories"]["misc"]["series"] == [0.0, 0.0, 0.0, 0.0, 50.0, 0.0]


@pytest.mark.asyncio
async def test_get_recurring_candidates_uses_plaid_merchant_fields(monkeypatch):
    async def fake_query_beads(params):
        assert params["type"] == "transaction"
        assert params["state"] == "posted"
        return [
            {
                "id": "tx-new-2",
                "created_at": "2026-05-15T12:00:00",
                "content": {
                    "normalized_merchant": "Netflix",
                    "merchant": "NETFLIX.COM*2222",
                    "amount": "17.99",
                    "posted_date": "2026-05-15",
                },
            },
            {
                "id": "tx-new-1",
                "created_at": "2026-04-15T12:00:00",
                "content": {
                    "merchant": "NETFLIX.COM*1111",
                    "amount": "17.99",
                    "posted_date": "2026-04-15",
                },
            },
            {
                "id": "tx-old",
                "created_at": "2026-03-15T12:00:00",
                "content": {
                    "description": "NETFLIX.COM*0000",
                    "amount": "15.49",
                    "posted_date": "2026-03-15",
                },
            },
        ]

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)

    candidates = await finance.get_recurring_candidates(
        months=6,
        min_occurrences=3,
        use_semantic_merge=False,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["merchant"] == "NETFLIX"
    assert candidate["recent_amount"] == 17.99
    assert candidate["amounts"] == [15.49, 17.99, 17.99]
    assert candidate["evidence_tx_ids"] == ["tx-old", "tx-new-1", "tx-new-2"]


@pytest.mark.asyncio
async def test_register_plaid_item_creates_bead_when_absent(monkeypatch):
    created = {}

    async def fake_query_beads(params):
        assert params["type"] == "plaid_item"
        return []

    async def fake_create_bead(bead_type, content, state, created_by):
        created.update({"type": bead_type, "content": content, "state": state})
        return {"id": "bead-new"}

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    monkeypatch.setattr(finance, "create_bead", fake_create_bead)

    result = await finance.register_plaid_item("chase", "item-xyz", "sandbox")

    assert result == {"bead_id": "bead-new", "created": True}
    assert created["type"] == "plaid_item"
    assert created["state"] == "active"
    assert created["content"]["institution"] == "chase"
    assert created["content"]["plaid_env"] == "sandbox"
    assert created["content"]["item_id"] == "item-xyz"
    assert created["content"]["infisical_key"] == "PLAID_ACCESS_TOKEN_CHASE"
    assert "linked_at" in created["content"]


@pytest.mark.asyncio
async def test_register_plaid_item_refreshes_existing_for_same_env(monkeypatch):
    """A create-mode re-link replaces the Item: the (institution, env) bead
    must be refreshed in place — new item_id, linked_at preserved — never
    duplicated. A different env's bead for the same institution must not
    be touched."""
    patched = {}

    async def fake_query_beads(params):
        return [
            {
                "id": "bead-prod",
                "content": {"institution": "chase", "plaid_env": "production", "item_id": "item-prod"},
            },
            {
                "id": "bead-sandbox",
                "content": {
                    "institution": "chase",
                    "plaid_env": "sandbox",
                    "item_id": "item-old",
                    "linked_at": "2026-01-01T00:00:00",
                },
            },
        ]

    async def fake_patch_bead(bead_id, *, state=None, parent_id=None, content=None, created_by="x"):
        patched.update({"id": bead_id, "state": state, "content": content})
        return {"id": bead_id}

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    monkeypatch.setattr(finance, "patch_bead", fake_patch_bead)

    result = await finance.register_plaid_item("chase", "item-new", "sandbox")

    assert result == {"bead_id": "bead-sandbox", "created": False}
    assert patched["id"] == "bead-sandbox"
    assert patched["state"] == "active"
    assert patched["content"]["item_id"] == "item-new"
    assert patched["content"]["linked_at"] == "2026-01-01T00:00:00"
    assert "last_relinked_at" in patched["content"]
