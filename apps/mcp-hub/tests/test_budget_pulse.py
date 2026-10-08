import json

import pytest

from workflows import budget_pulse


async def _noop_notify(*args, **kwargs):
    return True


def _minimal_report():
    return {
        "month": "2026-09",
        "as_of_date": "2026-09-15",
        "threshold": 0.8,
        "alerts": [
            {
                "category": "groceries",
                "budget_amount": 500.0,
                "spent_amount": 450.0,
                "remaining_amount": 50.0,
                "utilization_pct": 90.0,
            }
        ],
        "bills_upcoming": [],
        "bills_overdue": [],
    }


@pytest.mark.asyncio
async def test_notify_budget_alerts_uses_default_model(monkeypatch, httpx_mock):
    monkeypatch.delenv("BUDGET_PULSE_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(budget_pulse, "_send_discord_notification", _noop_notify)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "⚠️ Finance Nudge: groceries at 90%."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await budget_pulse.notify_budget_alerts_activity(_minimal_report())

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_notify_budget_alerts_respects_budget_pulse_model_override(monkeypatch, httpx_mock):
    monkeypatch.setenv("BUDGET_PULSE_MODEL", "claude-haiku-override")
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(budget_pulse, "_send_discord_notification", _noop_notify)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "⚠️ Finance Nudge: groceries at 90%."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await budget_pulse.notify_budget_alerts_activity(_minimal_report())

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku-override"


@pytest.mark.asyncio
async def test_notify_budget_alerts_cost_bead_carries_gateway_response_cost_header(
    monkeypatch, httpx_mock
):
    monkeypatch.delenv("BUDGET_PULSE_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(budget_pulse, "_send_discord_notification", _noop_notify)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        headers={"x-litellm-response-cost": "0.000456"},
        json={"choices": [{"message": {"content": "⚠️ Finance Nudge: groceries at 90%."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    await budget_pulse.notify_budget_alerts_activity(_minimal_report())

    cost_payload = json.loads(
        httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")[0].read()
    )
    assert cost_payload["content"]["cost_usd"] == 0.000456


@pytest.mark.asyncio
async def test_analyze_budget_status_alerts_only_over_threshold(monkeypatch):
    async def fake_budget_status(month="current"):
        assert month == "current"
        return {
            "misc": {"budget": 10.0, "spent": 10.01, "remaining": -0.01},
            "groceries": {"budget": 100.0, "spent": 80.0, "remaining": 20.0},
            "auto": {"budget": 100.0, "spent": 79.99, "remaining": 20.01},
            "total": {"budget": 210.0, "spent": 170.0, "remaining": 40.0},
        }

    monkeypatch.setattr(budget_pulse, "get_budget_status", fake_budget_status)

    report = await budget_pulse.analyze_budget_status_activity()

    assert report["threshold"] == 0.80
    assert [alert["category"] for alert in report["alerts"]] == ["misc"]
    assert report["alerts"][0]["utilization_pct"] == 100.1


@pytest.mark.asyncio
async def test_analyze_budget_status_skips_zero_spend_and_zero_budget(monkeypatch):
    async def fake_budget_status(month="current"):
        return {
            "misc": {"budget": 10.0, "spent": 0.0, "remaining": 10.0},
            "dining": {"budget": 0.0, "spent": 500.0, "remaining": -500.0},
            "total": {"budget": 10.0, "spent": 500.0, "remaining": -490.0},
        }

    monkeypatch.setattr(budget_pulse, "get_budget_status", fake_budget_status)

    report = await budget_pulse.analyze_budget_status_activity()

    assert report["alerts"] == []


def test_redact_account_references_keeps_budget_amounts():
    message = (
        "Budget Nudge:\n"
        "- You've used 95% of your Misc budget ($1900/$2000).\n"
        "- Card ending in 1234 should not be shown."
    )

    redacted = budget_pulse.redact_account_references(message)

    assert "$1900/$2000" in redacted
    assert "1234" not in redacted
    assert "ending in [redacted]" in redacted
