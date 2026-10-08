import asyncio
import time

import pytest

import tools.cost as cost_module
from tools.cost import (
    GPT4O_REFERENCE_PRICING,
    SIMULATED_REFERENCE_MODEL,
    calculate_simulated_value_usd,
    log_llm_cost,
    prompt_hash,
    redact_context,
)


# --- redact_context ------------------------------------------------------

def test_redact_drops_pii_keys():
    raw = {
        "location": "office",
        "merchant": "Mariano's",
        "amount": 42.10,
        "description": "groceries weekly",
        "transaction_id": "abc",
        "account_id": "xyz",
        "outcome": "categorized",
    }
    safe = redact_context(raw)
    assert safe == {"location": "office", "outcome": "categorized"}


def test_redact_drops_prompt_response_summary():
    raw = {
        "prompt": "long prompt text",
        "response": "long response",
        "completion_text": "...",
        "messages": [{"role": "user"}],
        "summary": "weekly digest",
        "content": "anything",
        "data": "anything",
        "narrative": "...",
        "keep_me": "ok",
    }
    safe = redact_context(raw)
    assert safe == {"keep_me": "ok"}


def test_redact_truncates_long_string_values():
    long_value = "x" * 500
    safe = redact_context({"note": long_value})
    assert safe["note"].endswith("...[truncated]")
    assert len(safe["note"]) <= 215  # 200 + suffix


def test_redact_summarizes_collections():
    safe = redact_context({"tags": [1, 2, 3], "meta": {"a": 1, "b": 2}})
    assert safe["tags"] == "<3 items>"
    assert safe["meta"] == "<2 keys>"


def test_redact_handles_none_and_empty():
    assert redact_context(None) == {}
    assert redact_context({}) == {}


def test_redact_is_case_insensitive_on_keys():
    safe = redact_context({"PROMPT": "x", "Merchant": "y", "Tag": "z"})
    assert safe == {"Tag": "z"}


# --- simulated value -----------------------------------------------------

def test_reference_model_is_gpt4o():
    assert SIMULATED_REFERENCE_MODEL == "gpt-4o"
    assert GPT4O_REFERENCE_PRICING["input_per_mtok"] > 0
    assert GPT4O_REFERENCE_PRICING["output_per_mtok"] > 0


def test_simulated_value_uses_gpt4o_rates():
    # 1M prompt @ $2.50 + 0.5M completion @ $10.00 = $2.50 + $5.00 = $7.50
    value = calculate_simulated_value_usd(
        {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    )
    assert value == 7.5


def test_simulated_value_ignores_actual_model():
    # Calculation must not depend on what served the call.
    usage = {"prompt_tokens": 1000, "completion_tokens": 200}
    # 1000/1M*$2.50 + 200/1M*$10 = $0.0025 + $0.002 = $0.0045
    value = calculate_simulated_value_usd(usage)
    assert value == 0.0045


def test_simulated_value_handles_missing_or_empty_usage():
    assert calculate_simulated_value_usd({}) == 0.0
    assert calculate_simulated_value_usd(None) == 0.0
    assert calculate_simulated_value_usd({"total_tokens": 50}) == 0.0  # no in/out split


# --- prompt_hash ---------------------------------------------------------

def test_prompt_hash_is_stable_and_short():
    h1 = prompt_hash("hello world")
    h2 = prompt_hash("hello world")
    assert h1 == h2
    assert len(h1) == 16
    assert prompt_hash("different") != h1


# --- log_llm_cost --------------------------------------------------------

@pytest.mark.asyncio
async def test_log_llm_cost_writes_bead(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        json={"id": "00000000-0000-0000-0000-000000000099"},
        status_code=200,
    )

    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        agent="morning-brief/synthesize",
        context={"location": "office", "merchant": "should-not-leak"},
        workflow_id="daily-brief-office-test",
        latency_ms=842.0,
        prompt_hash_value="deadbeefcafebabe",
    )
    assert bead_id == "00000000-0000-0000-0000-000000000099"

    request = httpx_mock.get_requests()[0]
    assert request.headers["X-API-Key"] == "test-key"
    import json
    body = json.loads(request.content)
    assert body["namespace"] == "platform"
    assert body["type"] == "cost"
    assert body["content"]["model"] == "claude-haiku"
    assert body["content"]["usage"]["prompt_tokens"] == 120
    assert body["content"]["agent"] == "morning-brief/synthesize"
    # No cost_usd was passed — never coerced to 0.0, since Haiku is paid and
    # an un-measured cost is not a free one.
    assert body["content"]["cost_usd"] is None
    # Simulated value: 120/1M*$2.50 + 30/1M*$10 = $0.0003 + $0.0003 = $0.0006
    assert body["content"]["simulated_value_usd"] == 0.0006
    assert body["content"]["simulated_reference_model"] == "gpt-4o"
    # PII was stripped, tags kept
    assert body["content"]["context"] == {"location": "office"}
    assert body["content"]["workflow_id"] == "daily-brief-office-test"
    assert body["provenance"] == {
        "worker": "morning-brief/synthesize",
        "model": "claude-haiku",
        "prompt_ref": "prompt-sha256:deadbeefcafebabe",
        "tokens": 150,
        "cost_usd": None,
        "duration_s": 0.842,
    }


@pytest.mark.asyncio
async def test_log_llm_cost_writes_actual_cost_when_provided(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        json={"id": "cost-bead-priced"},
        status_code=200,
    )

    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        agent="morning-brief/synthesize",
        cost_usd=0.000123,
    )
    assert bead_id == "cost-bead-priced"

    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["content"]["cost_usd"] == 0.000123
    assert body["provenance"]["cost_usd"] == 0.000123


@pytest.mark.asyncio
async def test_log_llm_cost_swallows_substrate_failure(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=500,
    )

    # Must not raise — cost logging is best-effort.
    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        agent="bank-sync/categorize",
        context={"outcome": "categorized"},
    )
    assert bead_id is None


@pytest.mark.asyncio
async def test_log_llm_cost_handles_missing_usage(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        json={"id": "no-usage"},
        status_code=200,
    )

    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage=None,
        agent="bank-sync/categorize",
        context={"outcome": "exception"},
    )
    assert bead_id == "no-usage"

    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["content"]["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert body["content"]["cost_usd"] is None
    assert body["content"]["simulated_value_usd"] == 0.0
    assert body["content"]["simulated_reference_model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_log_llm_cost_treats_unparseable_cost_string_as_none(httpx_mock, monkeypatch):
    """LiteLLM 1.83.14 sends the literal string "None" for an unpriced call.
    A caller that forwards float()-parsed None must not have it coerced to
    0.0 here either -- the bead is still written."""
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        json={"id": "unpriced-bead"},
        status_code=200,
    )

    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        agent="bank-sync/categorize",
        cost_usd=None,
    )
    assert bead_id == "unpriced-bead"

    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["content"]["cost_usd"] is None


@pytest.mark.asyncio
async def test_log_llm_cost_returns_none_within_timeout_when_store_hangs(monkeypatch):
    """The packaged substrate_client fixes its own HTTP_TIMEOUT_S (30s) and
    takes no per-call override, so `timeout` no longer bounds the request
    itself -- log_llm_cost bounds the whole call with asyncio.wait_for
    instead, so a hung store still returns None (best-effort) within
    `timeout` rather than the client's fixed 30s."""
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    async def _hangs(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(cost_module, "_call_substrate", _hangs)

    started = time.monotonic()
    bead_id = await log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        agent="test/agent",
        timeout=0.05,
    )
    elapsed = time.monotonic() - started

    assert bead_id is None
    assert elapsed < 5.0  # bounded by `timeout`, not the 10s hang
