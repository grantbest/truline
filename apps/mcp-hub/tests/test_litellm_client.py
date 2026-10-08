import inspect

import httpx
import pytest

from tools import litellm_client


class _StubAsyncClient:
    def __init__(self, response: httpx.Response, calls: list[dict]):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None, timeout=None):
        self._calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self._response


def _stub_async_client(monkeypatch, response: httpx.Response) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        litellm_client.httpx,
        "AsyncClient",
        lambda: _StubAsyncClient(response, calls),
    )
    return calls


@pytest.mark.asyncio
async def test_chat_completion_sends_caller_payload_headers_and_timeout(
    monkeypatch,
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {"Authorization": "Bearer test-key", "X-Caller": "budget-pulse"}
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok"}}]},
        request=httpx.Request("POST", "http://litellm.test/v1/chat/completions"),
    )
    calls = _stub_async_client(monkeypatch, response)

    result = await litellm_client.chat_completion(
        payload,
        headers=headers,
        timeout=12.5,
    )

    assert result.body == {"choices": [{"message": {"content": "ok"}}]}
    assert result.cost_usd is None  # no x-litellm-response-cost header on this response
    assert calls == [
        {
            "url": "http://litellm.test/v1/chat/completions",
            "json": payload,
            "headers": headers,
            "timeout": 12.5,
        }
    ]
    assert inspect.signature(litellm_client.chat_completion).parameters[
        "timeout"
    ].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_chat_completion_reraises_endpoint_status_error(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    request = httpx.Request("POST", "http://litellm.test/v1/chat/completions")
    response = httpx.Response(429, json={"error": "rate limit"}, request=request)
    _stub_async_client(monkeypatch, response)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await litellm_client.chat_completion(
            {"model": "test-model", "messages": []},
            headers={},
            timeout=60.0,
        )

    assert exc_info.value.request is request
    assert exc_info.value.response is response
    assert "429 Too Many Requests" in str(exc_info.value)


@pytest.mark.asyncio
async def test_post_chat_completion_sends_caller_payload_headers_and_timeout(
    monkeypatch,
):
    """bank_sync's two call sites branch on status_code themselves (only
    raising for 429, tolerating other non-200s) instead of calling
    raise_for_status() unconditionally — post_chat_completion must hand
    back the raw response so that branching still works."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Authorization": "Bearer test-key"}
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "groceries"}}]},
        request=httpx.Request("POST", "http://litellm.test/v1/chat/completions"),
    )
    calls = _stub_async_client(monkeypatch, response)

    result = await litellm_client.post_chat_completion(
        payload, headers=headers, timeout=30.0
    )

    assert result is response
    assert calls == [
        {
            "url": "http://litellm.test/v1/chat/completions",
            "json": payload,
            "headers": headers,
            "timeout": 30.0,
        }
    ]
    assert inspect.signature(litellm_client.post_chat_completion).parameters[
        "timeout"
    ].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_post_chat_completion_does_not_raise_on_error_status(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    request = httpx.Request("POST", "http://litellm.test/v1/chat/completions")
    response = httpx.Response(500, json={"error": "boom"}, request=request)
    _stub_async_client(monkeypatch, response)

    result = await litellm_client.post_chat_completion(
        {"model": "test-model", "messages": []},
        headers={},
        timeout=30.0,
    )

    assert result.status_code == 500
    assert result.json() == {"error": "boom"}


def test_chat_completions_url_required_has_no_fallback(monkeypatch):
    """vision.py historically read os.environ["LITELLM_URL"] directly (no
    default) — required=True preserves that KeyError-if-unset behaviour so
    it doesn't silently start defaulting to the cluster URL."""
    monkeypatch.delenv("LITELLM_URL", raising=False)

    with pytest.raises(KeyError):
        litellm_client.chat_completions_url(required=True)

    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    assert (
        litellm_client.chat_completions_url(required=True)
        == "http://litellm.test/v1/chat/completions"
    )


def test_chat_completions_url_default_falls_back_to_literal(monkeypatch):
    monkeypatch.delenv("LITELLM_URL", raising=False)
    assert (
        litellm_client.chat_completions_url()
        == "http://litellm.infra-ai.svc.cluster.local:4000/v1/chat/completions"
    )


@pytest.mark.asyncio
async def test_chat_completion_required_url_raises_before_any_network_call(
    monkeypatch,
):
    monkeypatch.delenv("LITELLM_URL", raising=False)
    calls = _stub_async_client(
        monkeypatch,
        httpx.Response(200, json={}, request=httpx.Request("POST", "http://x/")),
    )

    with pytest.raises(KeyError):
        await litellm_client.chat_completion(
            {"model": "gemini-flash", "messages": []},
            headers={},
            timeout=60.0,
            required_url=True,
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-flash", "gemini-pro", "gemini-embedding"])
async def test_chat_completion_raises_for_retired_model_before_any_network_call(
    monkeypatch, model
):
    """The Gemini API key backing every one of these aliases was revoked
    2026-09-04 (docs/plans/2026-09-04-decision-record-claude-loops.md).
    Every caller must get that named reason, not a 401 from a route
    LiteLLM no longer serves — and no network call should even be
    attempted."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    calls = _stub_async_client(
        monkeypatch,
        httpx.Response(200, json={}, request=httpx.Request("POST", "http://x/")),
    )

    with pytest.raises(litellm_client.LiteLLMProviderRetiredError, match="2026-09-04"):
        await litellm_client.chat_completion(
            {"model": model, "messages": []},
            headers={},
            timeout=60.0,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_post_chat_completion_raises_for_retired_model_before_any_network_call(
    monkeypatch,
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    calls = _stub_async_client(
        monkeypatch,
        httpx.Response(200, json={}, request=httpx.Request("POST", "http://x/")),
    )

    with pytest.raises(litellm_client.LiteLLMProviderRetiredError, match="2026-09-04"):
        await litellm_client.post_chat_completion(
            {"model": "gemini-flash", "messages": []},
            headers={},
            timeout=30.0,
        )

    assert calls == []


# --- LIFEOPS_DEFAULT_MODEL (AC-1) -----------------------------------------


def test_lifeops_default_model_is_claude_haiku():
    assert litellm_client.LIFEOPS_DEFAULT_MODEL == "claude-haiku"


# --- response_cost_usd (AC-7) ---------------------------------------------


def _response_with_headers(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={},
        headers=headers,
        request=httpx.Request("POST", "http://litellm.test/v1/chat/completions"),
    )


def test_response_cost_usd_parses_header_value():
    response = _response_with_headers({"x-litellm-response-cost": "0.000123"})
    assert litellm_client.response_cost_usd(response) == 0.000123


def test_response_cost_usd_absent_header_is_none():
    response = _response_with_headers({})
    assert litellm_client.response_cost_usd(response) is None


def test_response_cost_usd_literal_none_string_is_none():
    """LiteLLM 1.83.14 sends the literal string "None" for an unpriced call
    -- never coerced to 0.0, which would misrepresent "unknown" as "free"."""
    response = _response_with_headers({"x-litellm-response-cost": "None"})
    assert litellm_client.response_cost_usd(response) is None


@pytest.mark.asyncio
async def test_chat_completion_returns_cost_from_response_header(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "ok"}}]},
        headers={"x-litellm-response-cost": "0.000123"},
        request=httpx.Request("POST", "http://litellm.test/v1/chat/completions"),
    )
    _stub_async_client(monkeypatch, response)

    result = await litellm_client.chat_completion(
        {"model": "test-model", "messages": []}, headers={}, timeout=10.0
    )

    assert result.cost_usd == 0.000123
    assert result.body == {"choices": [{"message": {"content": "ok"}}]}


# --- extract_json_payload (AC-6) -------------------------------------------


def test_extract_json_payload_passes_through_bare_json():
    assert litellm_client.extract_json_payload('{"a": 1}') == '{"a": 1}'


def test_extract_json_payload_strips_code_fence():
    raw = '```json\n{"a": 1}\n```'
    assert litellm_client.extract_json_payload(raw) == '{"a": 1}'


def test_extract_json_payload_strips_surrounding_prose():
    raw = 'Sure, here is the JSON you asked for:\n{"a": 1}\nLet me know if you need more.'
    assert litellm_client.extract_json_payload(raw) == '{"a": 1}'


def test_extract_json_payload_handles_array():
    raw = "The result is [1, 2, 3] as requested."
    assert litellm_client.extract_json_payload(raw) == "[1, 2, 3]"


def test_extract_json_payload_no_bracket_returns_stripped_input():
    assert litellm_client.extract_json_payload("  no json here  ") == "no json here"


def test_extract_json_payload_ignores_bracket_shaped_prose_before_the_json():
    """A naive first-bracket-to-last-bracket scan narrows this to
    '[analysis]: {"a": 1}', which isn't valid JSON — the real payload
    starts at the next '{'."""
    raw = 'Here is the result [analysis]: {"a": 1}'
    assert litellm_client.extract_json_payload(raw) == '{"a": 1}'


def test_extract_json_payload_ignores_bracket_shaped_prose_after_the_json():
    """A naive first-bracket-to-last-bracket scan narrows this to
    '{"a": 1} ... {else}', which isn't valid JSON — raw_decode at the first
    '{' finds the complete, valid payload and stops there."""
    raw = '{"a": 1} ... {else}.'
    assert litellm_client.extract_json_payload(raw) == '{"a": 1}'


def test_extract_json_payload_without_expected_type_returns_first_decode():
    """Documents the pre-existing (unqualified) behaviour the next test
    guards against regressing on: without expected_type, the first position
    that decodes wins, even when it's an array a caller didn't want. ``[1]``
    is valid JSON on its own, and it sits before the object in this text."""
    raw = 'Per rule [1]: {"is_subscription": true}'
    assert litellm_client.extract_json_payload(raw) == "[1]"


def test_extract_json_payload_object_expected_skips_a_leading_array():
    """PR #1092 gate regression: 'Per rule [1]: {...}' used to make
    extract_json_payload return '[1]' (the first substring that decodes),
    and json.loads(...) then handed object-expecting callers a list, which
    doesn't have .get(). expected_type="object" restricts the scan to '{'
    starts, so the leading '[1]' is never tried. See
    test_subscription_auditor.py for the end-to-end case this guards."""
    raw = 'Per rule [1]: {"is_subscription": true}'
    assert (
        litellm_client.extract_json_payload(raw, expected_type="object")
        == '{"is_subscription": true}'
    )
