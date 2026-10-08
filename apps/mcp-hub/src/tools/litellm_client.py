"""Shared LiteLLM chat-completion client."""

import json
import os
from typing import Any, Mapping, NamedTuple, Optional

import httpx

_DEFAULT_CHAT_COMPLETIONS_URL = (
    "http://litellm.infra-ai.svc.cluster.local:4000/v1/chat/completions"
)

# The Gemini API key backing every model these aliases resolved to was
# revoked 2026-09-04 (the Operator's decision — see
# docs/plans/2026-09-04-decision-record-claude-loops.md). No replacement
# provider is in scope. Every caller (vision.py, bank_sync.py,
# financial_insights.py, morning_brief.py, budget_pulse.py,
# subscription_auditor.py) still hardcodes or defaults to one of these
# model names, so the guard lives once, here, at the shared chokepoint —
# not duplicated at each of the six call sites.
RETIRED_MODELS = frozenset({"gemini-flash", "gemini-pro", "gemini-embedding"})

PROVIDER_RETIREMENT_NOTICE = "docs/plans/2026-09-04-decision-record-claude-loops.md"

# the Operator's decision 2026-09-27: LifeOps' LLM jobs use Claude Haiku, via the
# one gateway route PR #1069 restored (infrastructure/k8s/infra-ai/litellm.yaml).
# Every LifeOps caller resolves its model from this constant, or from its own
# env override whose default is this constant — see .factory/design.md §1.
LIFEOPS_DEFAULT_MODEL = "claude-haiku"


class LiteLLMProviderRetiredError(RuntimeError):
    """Raised when a call targets a model whose provider was retired 2026-09-04."""


def _check_model_available(payload: Mapping[str, Any]) -> None:
    model = payload.get("model")
    if model in RETIRED_MODELS:
        raise LiteLLMProviderRetiredError(
            f"model {model!r} is unavailable: its provider (Gemini) was revoked "
            f"2026-09-04 and no replacement is configured — see "
            f"{PROVIDER_RETIREMENT_NOTICE}."
        )


def response_cost_usd(response: httpx.Response) -> Optional[float]:
    """Parse the gateway's per-call cost off ``x-litellm-response-cost``.

    Measured against LiteLLM 1.83.14 (the deployed image): this header, not
    any response-body field, carries the actual spend on a non-streaming
    response. An absent header, or the literal string ``"None"`` LiteLLM
    sends for an unpriced call, both give ``None`` — never ``0.0``, which
    would misrepresent "unknown" as "free".
    """
    raw = response.headers.get("x-litellm-response-cost")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def extract_json_payload(text: str, expected_type: Optional[str] = None) -> str:
    """Narrow ``text`` to the JSON object/array it most likely contains.

    Forced-tool-call JSON output (what ``{"type": "json_schema", ...}``
    becomes on claude-haiku-4-5) is more reliable than free-form JSON mode
    but not guaranteed literal — a reply may still arrive wrapped in a
    ```` ```json ... ``` ```` fence or surrounding prose. Strips a fence if
    present, then tries ``json.JSONDecoder.raw_decode`` at each candidate
    position in order, returning the first substring that decodes as JSON.
    Scanning from the first bracket to the *last* one (as a naive version of
    this once did) breaks on any reply with bracket-shaped text before or
    after the JSON, e.g. ``'Here is the result [analysis]: {"a": 1}'`` or
    ``'{"a": 1} ... {else}.'`` — both contain valid JSON but neither is
    "first bracket to last bracket". Returns the input unchanged (stripped)
    if no position decodes.

    ``expected_type`` narrows which candidate positions are tried: ``"object"``
    only tries ``{`` (a JSON value starting at ``{`` can only decode to an
    object), ``"array"`` only tries ``[``. Without it, both are tried in
    whichever order they appear in the text — which is wrong for a caller
    that needs an object: a reply like ``'Per rule [1]: {"is_subscription":
    true, ...}'`` contains a *valid* array, ``[1]``, before the object, and
    an unqualified scan would return that array instead of the object the
    caller actually wants.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    start_chars = {"object": "{", "array": "["}.get(expected_type, "{[")

    decoder = json.JSONDecoder()
    for i, c in enumerate(stripped):
        if c not in start_chars:
            continue
        try:
            _, end = decoder.raw_decode(stripped, i)
        except json.JSONDecodeError:
            continue
        return stripped[i:end]
    return stripped


class ChatCompletionResult(NamedTuple):
    """Return shape of :func:`chat_completion`: the parsed gateway body plus
    the per-call cost derived from the response headers (see
    :func:`response_cost_usd`). ``body`` keeps the exact
    ``resp.json()`` shape every caller already parses
    (``body["choices"][...]``, ``body.get("usage")``) unchanged."""

    body: dict[str, Any]
    cost_usd: Optional[float]


def chat_completions_url(*, required: bool = False) -> str:
    """Resolve the LiteLLM chat-completions endpoint from LITELLM_URL.

    ``required=True`` mirrors vision.py's historical
    ``os.environ["LITELLM_URL"]`` (raises ``KeyError`` if unset, no
    fallback) — kept distinct from the other call sites, which fall back
    to the literal default below. The asymmetry looks accidental but is
    out of scope to resolve here; see .factory/design.md.
    """
    if required:
        return os.environ["LITELLM_URL"]
    return os.environ.get("LITELLM_URL", _DEFAULT_CHAT_COMPLETIONS_URL)


async def chat_completion(
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    timeout: float | httpx.Timeout,
    required_url: bool = False,
) -> ChatCompletionResult:
    """POST and return the parsed JSON body plus its cost, raising on a
    non-2xx response.

    ``result.body`` is exactly the old bare-dict return value
    (``resp.json()``) — callers that did ``body = await chat_completion(...)``
    now do ``result = await chat_completion(...); body = result.body``.
    ``result.cost_usd`` is :func:`response_cost_usd` on the same response,
    for callers to forward to ``log_llm_cost``.
    """
    url = chat_completions_url(required=required_url)
    _check_model_available(payload)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers=dict(headers),
            timeout=timeout,
        )
        resp.raise_for_status()
        return ChatCompletionResult(body=resp.json(), cost_usd=response_cost_usd(resp))


async def post_chat_completion(
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    timeout: float | httpx.Timeout,
    required_url: bool = False,
) -> httpx.Response:
    """POST and return the raw response, without raising on error status.

    For callers that branch on ``resp.status_code`` themselves (e.g. to
    retry on 429 but tolerate other non-200s) instead of calling
    ``raise_for_status()`` unconditionally.
    """
    url = chat_completions_url(required=required_url)
    _check_model_available(payload)
    async with httpx.AsyncClient() as client:
        return await client.post(
            url,
            json=payload,
            headers=dict(headers),
            timeout=timeout,
        )
