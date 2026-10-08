import json
from datetime import datetime

import pytest

from workflows import financial_insights
from workflows.financial_insights import _build_insight_payload, _finance_window


def _minimal_summary():
    return {
        "window": {"since": "2026-05-15", "until": "2026-05-22"},
        "by_category": {},
        "totals": {"spent": 0, "budget": 0, "remaining": 0},
        "transaction_ids": [],
    }


@pytest.mark.asyncio
async def test_persist_insight_records_default_model(monkeypatch, httpx_mock):
    monkeypatch.delenv("INSIGHT_MODEL", raising=False)
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "insight-1"})

    await financial_insights.persist_insight_activity("Narrative.", _minimal_summary())

    body = json.loads(httpx_mock.get_requests()[0].read())
    assert body["content"]["model"] == "claude-haiku"
    assert body["provenance"]["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_persist_insight_records_insight_model_override(monkeypatch, httpx_mock):
    monkeypatch.setenv("INSIGHT_MODEL", "claude-haiku-override")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "insight-1"})

    await financial_insights.persist_insight_activity("Narrative.", _minimal_summary())

    body = json.loads(httpx_mock.get_requests()[0].read())
    assert body["content"]["model"] == "claude-haiku-override"


@pytest.mark.asyncio
async def test_synthesize_insight_uses_default_model(monkeypatch, httpx_mock):
    monkeypatch.delenv("INSIGHT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "Spending looks steady."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await financial_insights.synthesize_insight_activity(_minimal_summary())

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_synthesize_insight_respects_insight_model_override(monkeypatch, httpx_mock):
    monkeypatch.setenv("INSIGHT_MODEL", "claude-haiku-override")
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "Spending looks steady."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await financial_insights.synthesize_insight_activity(_minimal_summary())

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku-override"


@pytest.mark.asyncio
async def test_synthesize_insight_cost_bead_carries_gateway_response_cost_header(
    monkeypatch, httpx_mock
):
    monkeypatch.delenv("INSIGHT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        headers={"x-litellm-response-cost": "0.000789"},
        json={"choices": [{"message": {"content": "Spending looks steady."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await financial_insights.synthesize_insight_activity(_minimal_summary())

    cost_payload = json.loads(
        httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")[0].read()
    )
    assert cost_payload["content"]["cost_usd"] == 0.000789


def test_finance_window_starts_at_beginning_of_calendar_day():
    now = datetime(2026, 5, 22, 5, 58, 6)

    since, until = _finance_window(now)

    assert since == datetime(2026, 5, 15, 0, 0, 0)
    assert until == now


def test_build_insight_payload_sets_dashboard_summary_and_top_level_provenance():
    summary = {
        "window": {"since": "2026-05-15", "until": "2026-05-22"},
        "by_category": {
            "dining": {"spent": 42.5, "budget": 100.0, "remaining": 57.5, "tx_count": 2}
        },
        "totals": {"spent": 42.5, "budget": 100.0, "remaining": 57.5},
        "transaction_ids": [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ],
    }

    payload = _build_insight_payload("Dining is under control.", summary, "claude-haiku")

    assert payload["namespace"] == "finance"
    assert payload["type"] == "insight"
    assert payload["content"]["summary"] == "Dining is under control."
    assert payload["content"]["narrative"] == "Dining is under control."
    assert payload["content"]["transaction_ids"] == summary["transaction_ids"]
    assert "provenance" not in payload["content"]
    assert payload["provenance"] == {
        "worker": "financial-insights-workflow",
        "model": "claude-haiku",
        "prompt_ref": "financial-insights/synthesize",
        "tokens": None,
        "cost_usd": None,
        "duration_s": 0.0,
    }
