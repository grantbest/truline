import inspect

import pytest
from src.tools import litellm_client
from src.workflows import bank_sync
from src.workflows.bank_sync import categorize_transactions_bulk_activity


def _disable_retirement_guard(monkeypatch):
    """gemini-flash was retired 2026-09-04 (docs/plans/2026-09-04-decision-
    record-claude-loops.md) — litellm_client now refuses it before any
    network call. The tests below exercise the *parsing/prompt* logic
    downstream of that call, which remains real code for whenever a live
    provider is wired up again, so they clear the guard to reach it.
    Retirement itself is covered separately by
    test_categorize_transactions_bulk_llm_fallback_is_retired.
    """
    monkeypatch.setattr(litellm_client, "RETIRED_MODELS", frozenset())


class MockClient:
    def __init__(self, mock_post):
        self._mock_post = mock_post

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def post(self, url, json=None, headers=None, timeout=None):
        return await self._mock_post(url, json=json, headers=headers, timeout=timeout)


def _llm_post_calls(mock_post):
    return [
        call
        for call in mock_post.await_args_list
        if isinstance(call.kwargs.get("json"), dict)
        and isinstance(call.kwargs["json"].get("messages"), list)
    ]


def _mock_litellm(monkeypatch, response_content):
    from unittest.mock import AsyncMock, MagicMock

    litellm_resp = MagicMock()
    litellm_resp.status_code = 200
    litellm_resp.json.return_value = {
        "choices": [{"message": {"content": response_content}}],
        "usage": {"total_tokens": 10},
    }

    substrate_resp = MagicMock()
    substrate_resp.status_code = 200
    substrate_resp.json.return_value = {"id": "cost-bead"}

    async def fake_post(url, json=None, headers=None, timeout=None):
        if isinstance(json, dict) and isinstance(json.get("messages"), list):
            return litellm_resp
        return substrate_resp

    mock_post = AsyncMock(side_effect=fake_post)
    monkeypatch.setattr(bank_sync.httpx, "AsyncClient", lambda: MockClient(mock_post))
    return mock_post


def _mock_litellm_status(monkeypatch, status_code):
    from unittest.mock import AsyncMock, MagicMock

    litellm_resp = MagicMock()
    litellm_resp.status_code = status_code
    litellm_resp.text = f"status {status_code}"

    substrate_resp = MagicMock()
    substrate_resp.status_code = 200
    substrate_resp.json.return_value = {"id": "cost-bead"}

    async def fake_post(url, json=None, headers=None, timeout=None):
        if isinstance(json, dict) and isinstance(json.get("messages"), list):
            return litellm_resp
        return substrate_resp

    mock_post = AsyncMock(side_effect=fake_post)
    monkeypatch.setattr(bank_sync.httpx, "AsyncClient", lambda: MockClient(mock_post))
    return mock_post


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_rule_matching(monkeypatch):
    async def fake_query(params):
        if params.get("type") == "rule":
            return [
                {
                    "id": "rule-123",
                    "state": "active",
                    "content": {
                        "field": "merchant_name",
                        "operator": "contains",
                        "value": "starbucks",
                        "target_category": "dining",
                    },
                }
            ]
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)

    txns = [{"merchant_name": "Starbucks Coffee", "amount": 5.45, "name": "Starbucks 123"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert len(results) == 1
    assert results[0]["category"] == "dining"
    assert results[0]["categorization_source"] == "rule"
    assert results[0]["categorization_rule_id"] == "rule-123"
    assert results[0]["categorization_confidence"] == 1.0


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_naive_bayes(monkeypatch):
    async def fake_query(params):
        if params.get("type") == "rule":
            return []
        if params.get("type") == "transaction":
            training_data = []
            for _ in range(10):
                training_data.append({
                    "content": {
                        "merchant_name": "Mariano's",
                        "our_category": "Groceries",
                    }
                })
                training_data.append({
                    "content": {
                        "merchant_name": "AMC Theater",
                        "our_category": "entertainment",
                    }
                })
            return training_data
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)

    txns = [
        {"merchant_name": "Marianos Food Store", "amount": 42.50, "name": "Marianos"},
        {"merchant_name": "AMC Cinema", "amount": 15.00, "name": "AMC Movie"},
    ]
    results = await categorize_transactions_bulk_activity(txns)

    assert len(results) == 2
    assert results[0]["category"] == "groceries"
    assert results[0]["categorization_source"] == "local_ml/naive_bayes"
    assert results[0]["categorization_confidence"] >= 0.85

    assert results[1]["category"] == "entertainment"
    assert results[1]["categorization_source"] == "local_ml/naive_bayes"
    assert results[1]["categorization_confidence"] >= 0.85


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_ignores_invalid_training_categories(monkeypatch):
    async def fake_query(params):
        if params.get("type") == "rule":
            return []
        if params.get("type") == "transaction":
            return [
                {
                    "content": {
                        "merchant_name": "Ghost Vendor",
                        "our_category": "Uncategorized",
                    }
                },
                {
                    "content": {
                        "merchant_name": "Ghost Vendor",
                        "our_category": "not_a_real_category",
                    }
                },
            ] * 10
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    _disable_retirement_guard(monkeypatch)
    mock_post = _mock_litellm(monkeypatch, "dining")

    txns = [{"merchant_name": "Ghost Vendor", "amount": 9.99, "name": "Ghost Vendor"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert results[0]["category"] == "dining"
    assert results[0]["categorization_source"] == "litellm/claude-haiku"
    llm_calls = _llm_post_calls(mock_post)
    assert len(llm_calls) == 1
    prompt = llm_calls[0].kwargs["json"]["messages"][0]["content"]
    assert "Ghost Vendor" in prompt
    assert "not_a_real_category" not in prompt


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_fallback_to_llm(monkeypatch):
    async def fake_query(params):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    _disable_retirement_guard(monkeypatch)
    _mock_litellm(monkeypatch, "utilities")

    txns = [{"merchant_name": "Comcast Cable", "amount": 80.00, "name": "Comcast"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert len(results) == 1
    assert results[0]["category"] == "utilities"
    assert results[0]["categorization_source"] == "litellm/claude-haiku"
    assert results[0]["categorization_confidence"] is None

    source = inspect.getsource(categorize_transactions_bulk_activity)
    assert '"categorization_confidence": 0.8' not in source
    assert "0.8 if category else 0.0" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "expected_category", "expected_transfer"),
    [
        ("Interest/Fees", "interest_fees", False),
        ("interest fees", "interest_fees", False),
        ("interest-fees", "interest_fees", False),
        ("Transfer", "transfer", True),
    ],
)
async def test_categorize_transactions_bulk_normalizes_llm_category(
    monkeypatch,
    llm_response,
    expected_category,
    expected_transfer,
):
    async def fake_query(params):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    _disable_retirement_guard(monkeypatch)
    _mock_litellm(monkeypatch, llm_response)

    txns = [{"merchant_name": "Chase Credit Card", "amount": 18.42, "name": "Interest Charge"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert results[0]["category"] == expected_category
    assert results[0]["is_transfer"] is expected_transfer
    assert results[0]["categorization_source"] == "litellm/claude-haiku"


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_invalid_llm_category_falls_back_to_misc(monkeypatch):
    async def fake_query(params):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    _disable_retirement_guard(monkeypatch)
    _mock_litellm(monkeypatch, "maybe snacks?")

    txns = [{"merchant_name": "Mystery Vendor", "amount": 12.34, "name": "Mystery Vendor"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert results[0]["category"] == "misc"
    assert results[0]["is_transfer"] is False
    assert results[0]["categorization_source"] == "litellm/unparsed"
    assert results[0]["categorization_confidence"] == 0.0


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_llm_retry_exhaustion_records_failed_source(monkeypatch):
    async def fake_query(params):
        return []

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    monkeypatch.setattr(bank_sync.asyncio, "sleep", fake_sleep)
    _disable_retirement_guard(monkeypatch)
    mock_post = _mock_litellm_status(monkeypatch, 429)

    txns = [{"merchant_name": "Mystery Vendor", "amount": 12.34, "name": "Mystery Vendor"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert results[0]["category"] is None
    assert results[0]["is_transfer"] is False
    assert results[0]["categorization_source"] == "litellm/failed"
    assert results[0]["categorization_confidence"] == 0.0
    assert len(_llm_post_calls(mock_post)) == 3


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_llm_fallback_is_retired(monkeypatch):
    """A caller that ends up requesting a model litellm_client has flagged
    retired (gemini-flash was, 2026-09-04) must have the cloud-LLM fallback
    fail closed on the first attempt (no retries, no network call) and say
    why, not silently 401. claude-haiku (the current default) is not
    retired, so this simulates the condition directly rather than relying
    on the live default happening to be retired."""

    async def fake_query(params):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    monkeypatch.setattr(
        litellm_client, "RETIRED_MODELS", frozenset({litellm_client.LIFEOPS_DEFAULT_MODEL})
    )
    mock_post = _mock_litellm(monkeypatch, "dining")

    txns = [{"merchant_name": "Mystery Vendor", "amount": 12.34, "name": "Mystery Vendor"}]
    results = await categorize_transactions_bulk_activity(txns)

    assert results[0]["category"] is None
    assert results[0]["is_transfer"] is False
    assert results[0]["categorization_source"] == "litellm/retired"
    assert results[0]["categorization_confidence"] == 0.0
    assert _llm_post_calls(mock_post) == []
