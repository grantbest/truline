"""Cross-cutting checks for the LiteLLM call-site consolidation.

Six modules (vision, morning_brief, financial_insights, bank_sync x2,
budget_pulse) plus a seventh that the original audit's `client.post(LITELLM_URL)`
grep missed (subscription_auditor, which called `client.post(_litellm_url())`)
used to each build their own request to the LiteLLM chat-completions
endpoint. They now all route through `src.tools.litellm_client`. These
tests assert the two things a pure structural refactor must not get wrong:
nothing outside the client can reach the endpoint directly, and cost
accounting/error handling at each call site is unchanged.
"""

import ast
import json
from pathlib import Path

import httpx
import pytest

from src.tools import litellm_client
from workflows import bank_sync, morning_brief
from tools import vision

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# AC-2: a reintroduced gemini-* name must still be caught even wrapped, e.g.
# bank_sync.py's historical "litellm/gemini-flash" provenance string.
_RETIRED_MODEL_SUBSTRINGS = ("gemini-flash", "gemini-pro", "gemini-embedding")


def _string_constants(source: str):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _disable_retirement_guard(monkeypatch):
    """gemini-flash/gemini-pro were retired 2026-09-04 (docs/plans/2026-09-
    04-decision-record-claude-loops.md) — litellm_client now refuses them
    before any network call. Tests below that exercise cost-logging /
    error-propagation logic downstream of a *successful or erroring* LLM
    response clear the guard to reach it; that logic remains real for
    whenever a live provider is wired up. Note this patches
    ``src.tools.litellm_client`` specifically — the module object every
    production call site actually imports, regardless of whether a given
    test file reaches it via the bare ``tools``/``workflows`` alias or the
    ``src.``-qualified one.
    """
    monkeypatch.setattr(litellm_client, "RETIRED_MODELS", frozenset())


def test_only_litellm_client_module_references_the_endpoint():
    """Repository check backing the acceptance criterion that the sixth
    (or seventh) call site cannot quietly reappear: no module besides the
    client may know the literal endpoint URL or the LITELLM_URL env var
    name."""
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "litellm_client.py":
            continue
        text = path.read_text()
        if "chat/completions" in text or "LITELLM_URL" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == []


def test_no_retired_model_literal_in_src_outside_litellm_client():
    """AC-2: no caller may name a retired model, even LiteLLM_client's own
    RETIRED_MODELS carve-out aside. Scans string constants via ast (not raw
    text), so a comment or docstring narrating the 2026-09-04 retirement --
    several already do -- doesn't trip it."""
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "litellm_client.py":
            continue
        for value in _string_constants(path.read_text()):
            if any(needle in value for needle in _RETIRED_MODEL_SUBSTRINGS):
                offenders.append((str(path.relative_to(SRC_ROOT)), value))
    assert offenders == []


def test_no_retired_model_literal_scan_can_actually_fail():
    """Negative control: the scan above must be capable of catching a
    planted retired-model string constant, proving it isn't vacuously
    passing."""
    fixture_source = (
        'PROVENANCE_SOURCE = "litellm/gemini-flash"  # deliberately planted for the test\n'
    )
    caught = [
        value
        for value in _string_constants(fixture_source)
        if any(needle in value for needle in _RETIRED_MODEL_SUBSTRINGS)
    ]
    assert caught == ["litellm/gemini-flash"]


@pytest.mark.asyncio
async def test_vision_extract_logs_cost_exactly_once_with_unchanged_values(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    extraction_body = {
        "todos": ["buy milk"],
        "events": [],
        "expenses": [],
        "summary": "a handwritten note",
    }
    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "choices": [{"message": {"content": json.dumps(extraction_body)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "cost-bead-1"},
    )

    extraction = await vision.extract_from_image("ZmFrZQ==")

    assert extraction.summary == "a handwritten note"

    litellm_requests = httpx_mock.get_requests(
        url="http://litellm.test/v1/chat/completions", method="POST"
    )
    assert len(litellm_requests) == 1

    cost_requests = httpx_mock.get_requests(
        url="http://substrate.test/beads", method="POST"
    )
    assert len(cost_requests) == 1
    cost_payload = json.loads(cost_requests[0].read())
    assert cost_payload["content"]["model"] == "claude-haiku"
    assert cost_payload["content"]["agent"] == "vision/extract"
    assert cost_payload["content"]["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


# --- AC-7: gateway cost header reaches the platform.cost bead -------------


@pytest.mark.asyncio
async def test_morning_brief_cost_bead_carries_gateway_response_cost_header(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        headers={"x-litellm-response-cost": "0.000123"},
        json={
            "choices": [{"message": {"content": "Good morning."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await morning_brief.synthesize_brief_activity({"location": "home"})

    cost_requests = httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")
    assert len(cost_requests) == 1
    cost_payload = json.loads(cost_requests[0].read())
    assert cost_payload["content"]["cost_usd"] == 0.000123
    assert cost_payload["provenance"]["cost_usd"] == 0.000123


@pytest.mark.asyncio
async def test_morning_brief_cost_bead_cost_usd_is_none_without_header(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "choices": [{"message": {"content": "Good morning."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await morning_brief.synthesize_brief_activity({"location": "home"})

    cost_payload = json.loads(
        httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")[0].read()
    )
    assert cost_payload["content"]["cost_usd"] is None


@pytest.mark.asyncio
async def test_bank_sync_categorize_cost_bead_carries_gateway_response_cost_header(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        headers={"x-litellm-response-cost": "0.000123"},
        json={
            "choices": [{"message": {"content": "groceries"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await bank_sync.categorize_transaction_activity("Whole Foods", 42.10, "Whole Foods #123")

    cost_payload = json.loads(
        httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")[0].read()
    )
    assert cost_payload["content"]["cost_usd"] == 0.000123


@pytest.mark.asyncio
async def test_bank_sync_categorize_cost_bead_cost_usd_is_none_for_unpriced_header(
    monkeypatch, httpx_mock
):
    """LiteLLM 1.83.14 sends the literal string "None" for an unpriced call
    -- the bead is still written, with cost_usd None, not 0.0."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        headers={"x-litellm-response-cost": "None"},
        json={
            "choices": [{"message": {"content": "groceries"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await bank_sync.categorize_transaction_activity("Whole Foods", 42.10, "Whole Foods #123")

    cost_requests = httpx_mock.get_requests(url="http://substrate.test/beads", method="POST")
    assert len(cost_requests) == 1
    cost_payload = json.loads(cost_requests[0].read())
    assert cost_payload["content"]["cost_usd"] is None


# --- AC-3: request-body model, asserted directly (not only via a label) ---
#
# These three previously went uncaught: a mutation of the model literal at
# the call site was only ever caught indirectly, by the retirement-guard
# tests (which only check that *some* retired name is refused, not which
# model a non-retired request actually names) or by
# categorize_transactions_bulk_activity's `categorization_source` label
# (which happens to embed the model name as a side effect of its own
# provenance format, not because a test asked it to). Each of these reads
# the JSON actually POSTed to the gateway.


@pytest.mark.asyncio
async def test_morning_brief_synthesize_requests_claude_haiku(monkeypatch, httpx_mock):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "Good morning."}}]},
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await morning_brief.synthesize_brief_activity({"location": "home"})

    request_body = json.loads(
        httpx_mock.get_requests(url="http://litellm.test/v1/chat/completions")[0].read()
    )
    assert request_body["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_categorize_transaction_activity_requests_claude_haiku(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "groceries"}}]},
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await bank_sync.categorize_transaction_activity("Whole Foods", 42.10, "Whole Foods #123")

    request_body = json.loads(
        httpx_mock.get_requests(url="http://litellm.test/v1/chat/completions")[0].read()
    )
    assert request_body["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_activity_requests_claude_haiku(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    async def no_rules_or_history(*args, **kwargs):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", no_rules_or_history)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "groceries"}}]},
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await bank_sync.categorize_transactions_bulk_activity(
        [{"merchant_name": "Whole Foods", "name": "Whole Foods #123", "amount": 42.10}]
    )

    request_body = json.loads(
        httpx_mock.get_requests(url="http://litellm.test/v1/chat/completions")[0].read()
    )
    assert request_body["model"] == "claude-haiku"


# --- AC-3 / AC-6: model default + json_schema survives drop_params --------


@pytest.mark.asyncio
async def test_vision_extract_requests_claude_haiku_and_json_schema(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    extraction_body = {"todos": [], "events": [], "expenses": [], "summary": "note"}
    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": json.dumps(extraction_body)}}]},
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )

    await vision.extract_from_image("ZmFrZQ==")

    request_body = json.loads(
        httpx_mock.get_requests(url="http://litellm.test/v1/chat/completions")[0].read()
    )
    assert request_body["model"] == "claude-haiku"
    assert request_body["response_format"]["type"] == "json_schema"
    assert request_body["response_format"]["json_schema"]["schema"] == vision.VisionExtraction.model_json_schema()


@pytest.mark.asyncio
async def test_vision_extract_parses_fenced_and_prose_wrapped_reply_the_same(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    extraction_body = {
        "todos": ["buy milk"],
        "events": [],
        "expenses": [],
        "summary": "a handwritten note",
    }
    bare = json.dumps(extraction_body)
    fenced = f"```json\n{bare}\n```"
    prose = f"Sure, here's the extraction:\n{bare}\nLet me know if that's right."

    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )
    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": fenced}}]},
    )
    fenced_extraction = await vision.extract_from_image("ZmFrZQ==")

    httpx_mock.add_response(
        url="http://substrate.test/beads", method="POST", status_code=200, json={"id": "cost-bead"}
    )
    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": prose}}]},
    )
    prose_extraction = await vision.extract_from_image("ZmFrZQ==")

    assert fenced_extraction == prose_extraction
    assert fenced_extraction.todos == ["buy milk"]
    assert fenced_extraction.summary == "a handwritten note"


@pytest.mark.asyncio
async def test_vision_extract_requires_litellm_url_with_no_fallback(
    monkeypatch, httpx_mock
):
    """vision.py's LITELLM_URL has no default — preserved via
    required_url=True. Missing the env var must still fail before any
    network call, exactly as the old `os.environ["LITELLM_URL"]` did."""
    monkeypatch.delenv("LITELLM_URL", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")

    with pytest.raises(KeyError):
        await vision.extract_from_image("ZmFrZQ==")

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_morning_brief_synthesize_reraises_endpoint_error_unchanged(
    monkeypatch, httpx_mock
):
    """morning_brief calls raise_for_status() with no surrounding
    try/except — an endpoint error must still propagate as
    httpx.HTTPStatusError, not be swallowed by the new client."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    _disable_retirement_guard(monkeypatch)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=500,
        json={"error": "upstream down"},
    )

    with pytest.raises(httpx.HTTPStatusError):
        await morning_brief.synthesize_brief_activity({"location": "home"})


@pytest.mark.asyncio
async def test_categorize_transactions_bulk_logs_cost_once_per_llm_fallback(
    monkeypatch, httpx_mock
):
    """The bulk categorizer's cloud-LLM fallback branch parses
    resp.json() twice per successful call (once for the category, once
    for usage) inside a 3-attempt retry loop — behaviour that must
    survive the move off a locally-constructed httpx.AsyncClient."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    _disable_retirement_guard(monkeypatch)

    async def no_rules_or_history(*args, **kwargs):
        return []

    monkeypatch.setattr(bank_sync, "finance_query_beads", no_rules_or_history)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "choices": [{"message": {"content": "groceries"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "cost-bead-2"},
    )

    results = await bank_sync.categorize_transactions_bulk_activity(
        [{"merchant_name": "Whole Foods", "name": "Whole Foods #123", "amount": 42.10}]
    )

    assert results == [
        {
            "category": "groceries",
            "is_transfer": False,
            "flow": "spend",
            "categorization_source": "litellm/claude-haiku",
            "categorization_rule_id": None,
            "categorization_confidence": None,
        }
    ]

    litellm_requests = httpx_mock.get_requests(
        url="http://litellm.test/v1/chat/completions", method="POST"
    )
    assert len(litellm_requests) == 1  # no retry needed on a 200

    cost_requests = httpx_mock.get_requests(
        url="http://substrate.test/beads", method="POST"
    )
    assert len(cost_requests) == 1
    cost_payload = json.loads(cost_requests[0].read())
    assert cost_payload["content"]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "total_tokens": 4,
    }


@pytest.mark.asyncio
async def test_vision_extract_fails_when_its_default_model_is_retired(
    monkeypatch, httpx_mock
):
    """Receipt/note vision extraction used gemini-flash, which the guard
    refused with a message naming the 2026-09-04 retirement instead of
    ever reaching -- and 401ing against -- LiteLLM. claude-haiku (the
    current default) is not retired, so this simulates the same failure
    mode directly: whatever vision's default model is, if litellm_client
    ever flags it retired, the call must still fail closed before any
    network request."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setattr(
        litellm_client, "RETIRED_MODELS", frozenset({litellm_client.LIFEOPS_DEFAULT_MODEL})
    )

    with pytest.raises(litellm_client.LiteLLMProviderRetiredError):
        await vision.extract_from_image("ZmFrZQ==")

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_morning_brief_synthesize_fails_when_its_default_model_is_retired(
    monkeypatch, httpx_mock
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setattr(
        litellm_client, "RETIRED_MODELS", frozenset({litellm_client.LIFEOPS_DEFAULT_MODEL})
    )

    with pytest.raises(litellm_client.LiteLLMProviderRetiredError):
        await morning_brief.synthesize_brief_activity({"location": "home"})

    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_vision_extract_fails_named_by_2026_09_04_retirement_for_gemini_flash(
    monkeypatch, httpx_mock
):
    """AC-4: the retirement guard itself (named message, gemini-flash
    specifically) is unchanged -- proven directly against vision's own
    payload construction rather than relying on it being the live default."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setattr(vision, "LIFEOPS_DEFAULT_MODEL", "gemini-flash")

    with pytest.raises(litellm_client.LiteLLMProviderRetiredError, match="2026-09-04"):
        await vision.extract_from_image("ZmFrZQ==")

    assert httpx_mock.get_requests() == []
