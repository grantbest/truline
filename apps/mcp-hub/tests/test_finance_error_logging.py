"""AC-6: a refused write logs the store's 422 body.

``finance._raw_substrate_request`` and ``finance._call_substrate`` are the
two seams every writer already goes through. Neither logged the response
body before this -- httpx's ``HTTPStatusError`` (raise_for_status's default)
carries the URL and status but never the body, so a 422 read as "Client
error '422 Unprocessable Entity' for url .../beads" with no hint which
field. (dev.finding d333e0cf)
"""

import logging

import httpx
import pytest

from tools import cost
from workflows import subscription_auditor

_ERROR_BODY = {
    "detail": [
        {
            "type": "extra_forbidden",
            "loc": ["body", "provenance", "chain"],
            "msg": "Extra inputs are not permitted",
        }
    ]
}


def _analyzed_netflix():
    return {
        "cluster": {
            "merchant": "NETFLIX",
            "display_name": "Netflix",
            "first_seen": "2026-03-15",
            "last_seen": "2026-05-15",
            "occurrences": 3,
            "evidence_tx_ids": ["tx-1"],
        },
        "analysis": {
            "is_subscription": True,
            "name": "Netflix",
            "category": "streaming",
            "amount": 17.99,
            "frequency": "monthly",
            "is_price_hike": False,
            "price_change": {"detected": False},
            "confidence": 0.9,
            "notes": "ok",
        },
        "model": "gemini-pro",
        "analysis_source": "llm",
    }


@pytest.mark.asyncio
async def test_raw_substrate_request_refusal_is_logged_and_reraised(httpx_mock, monkeypatch, caplog):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-secret-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json=_ERROR_BODY, status_code=422)

    caplog.set_level(logging.ERROR, logger="tools.finance")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await subscription_auditor.persist_subscription_activity(_analyzed_netflix())

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("422" in r.getMessage() and "extra_forbidden" in r.getMessage() for r in error_records)
    assert not any("test-secret-key" in r.getMessage() for r in caplog.records)
    assert "extra_forbidden" in str(exc_info.value)


@pytest.mark.asyncio
async def test_call_substrate_refusal_is_logged_and_swallowed(httpx_mock, monkeypatch, caplog):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-secret-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(method="POST", json=_ERROR_BODY, status_code=422)

    caplog.set_level(logging.ERROR, logger="tools.finance")

    bead_id = await cost.log_llm_cost(
        model="gemini-flash",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        agent="test/agent",
    )

    assert bead_id is None
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "422" in r.getMessage() and "extra_forbidden" in r.getMessage() and "create_bead" in r.getMessage()
        for r in error_records
    )
    assert not any("test-secret-key" in r.getMessage() for r in caplog.records)
