"""LLM value tracking — Phase 4.1 (AI Value Tracker).

Records every LLM call as a `platform.cost` bead in Substrate so the
dashboard can show value extracted from the free tier (ARCHITECTURE §8
cross-cutting Cost requirement).

Cost
----
LifeOps' LLM calls run on Claude Haiku (the Operator's decision 2026-09-27 — see
docs/plans/2026-09-04-decision-record-claude-loops.md), a paid model, so
``content.cost_usd`` is whatever the caller measured for that call (via
``tools.litellm_client.response_cost_usd``), not a fixed policy value.
``None`` means "a model ran but its cost wasn't measured" (no pricing
header, or an unpriced call) — it is never coerced to ``0.0``, which would
misrepresent "unknown" as "free". Every bead also carries
``simulated_value_usd`` — what the same prompt/completion token count
would have cost on GPT-4o (the chosen reference), independent of what
actually served the call — kept as a stable cross-model yardstick.

Design notes
------------
- ``log_llm_cost`` is best-effort. It catches exceptions and logs them
  rather than re-raising, so a Substrate outage never fails the calling
  activity. The LLM work is already done at the point we record cost; we
  don't want to lose the model output because the spend bead failed.
- All Substrate calls carry ``X-API-Key`` (Amendment 7).
- The ``context`` arg is for routing tags (workflow id, location, etc.).
  Callers must NOT pass prompt text, transaction details, or any PII.
  ``redact_context`` is a defence-in-depth pass that strips well-known
  PII-shaped keys and caps the length of any remaining string values.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from src.tools.finance import _call_substrate
from src.tools.provenance import build_provenance, prompt_ref_for

logger = logging.getLogger(__name__)


# --- Value model ---------------------------------------------------------

# GPT-4o is the public reference we benchmark "value extracted" against.
# Rates are USD per 1,000,000 tokens, current OpenAI list pricing as of
# 2026-05. Updating this constant retroactively re-prices the dashboard;
# do NOT mutate per-call.
SIMULATED_REFERENCE_MODEL: str = "gpt-4o"
GPT4O_REFERENCE_PRICING: Dict[str, float] = {
    "input_per_mtok": 2.50,
    "output_per_mtok": 10.00,
}


def calculate_simulated_value_usd(usage: Mapping[str, Any]) -> float:
    """Return what this usage *would* have cost on the reference model.

    Independent of the model that actually served the call — by design,
    the platform might serve any free-tier model and we still want a
    stable yardstick. Returns 0.0 when usage is empty/missing.
    """
    if not usage:
        return 0.0
    prompt = float(usage.get("prompt_tokens", 0) or 0)
    completion = float(usage.get("completion_tokens", 0) or 0)
    return round(
        (prompt / 1_000_000) * GPT4O_REFERENCE_PRICING["input_per_mtok"]
        + (completion / 1_000_000) * GPT4O_REFERENCE_PRICING["output_per_mtok"],
        6,
    )


# --- Redaction -----------------------------------------------------------

# Substrings that mark a context key as PII/financial — case-insensitive.
# Keys matching any of these are dropped wholesale from the cost bead.
_SENSITIVE_KEY_MARKERS = (
    "prompt",
    "response",
    "completion",
    "messages",
    "merchant",
    "amount",
    "balance",
    "description",
    "transaction",
    "account",
    "plaid",
    "email",
    "phone",
    "address",
    "ssn",
    "password",
    "secret",
    "token",
    "api_key",
    "summary",
    "narrative",
    "content",
    "data",
)

# Cap individual string values so a caller can't accidentally smuggle a
# whole prompt through a non-sensitive-looking key like "note".
_VALUE_TRUNCATE_AT = 200


def redact_context(context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Strip PII-shaped keys and cap string lengths.

    Defence-in-depth — callers are expected to pass only routing tags,
    but this enforces it. Returns a new dict; never mutates the input.
    """
    if not context:
        return {}
    safe: Dict[str, Any] = {}
    for key, value in context.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
            continue
        if isinstance(value, str) and len(value) > _VALUE_TRUNCATE_AT:
            safe[key] = value[:_VALUE_TRUNCATE_AT] + "...[truncated]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = f"<{len(value)} items>"
        elif isinstance(value, dict):
            safe[key] = f"<{len(value)} keys>"
        else:
            safe[key] = f"<{type(value).__name__}>"
    return safe


def prompt_hash(prompt: str) -> str:
    """Stable short hash for provenance.chain. Not reversible — fine to
    record in beads since prompts may contain PII."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


# --- Bead writer ---------------------------------------------------------

async def log_llm_cost(
    model: str,
    usage: Optional[Mapping[str, Any]],
    agent: str,
    context: Optional[Mapping[str, Any]] = None,
    *,
    workflow_id: Optional[str] = None,
    latency_ms: Optional[float] = None,
    prompt_hash_value: Optional[str] = None,
    cost_usd: Optional[float] = None,
    timeout: float = 5.0,
) -> Optional[str]:
    """Write a platform.cost bead. Returns bead id or None on failure.

    Parameters
    ----------
    model       : LiteLLM model name (claude-haiku, ...).
    usage       : Token usage dict from the LLM response. Accepts None
                  when the upstream call failed without returning usage.
    agent       : Caller identity, e.g. "morning-brief/synthesize".
    context     : Routing tags only. PII keys are stripped before write.
    workflow_id : Parent Temporal workflow id, when the caller is inside a workflow activity.
    latency_ms  : Optional end-to-end LLM call latency (ms).
    prompt_hash_value : Optional precomputed hash from ``prompt_hash``.
    cost_usd    : Actual spend for this call, from
                  ``tools.litellm_client.response_cost_usd``. ``None`` when
                  no model ran, or a model ran but its cost wasn't measured
                  (absent/unparseable pricing header) — never coerced to
                  ``0.0``.
    timeout     : Substrate POST timeout (seconds).
    """
    usage_dict = dict(usage) if usage else {}
    simulated_value_usd = calculate_simulated_value_usd(usage_dict)
    safe_context = redact_context(context)
    has_usage = usage is not None
    total_tokens = int(usage_dict.get("total_tokens", 0) or 0)

    content = {
        "model": model,
        "usage": {
            "prompt_tokens": int(usage_dict.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage_dict.get("completion_tokens", 0) or 0),
            "total_tokens": total_tokens,
        },
        "cost_usd": cost_usd,
        "simulated_value_usd": simulated_value_usd,
        "simulated_reference_model": SIMULATED_REFERENCE_MODEL,
        "agent": agent,
        "context": safe_context,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "workflow_id": workflow_id,
    }
    provenance = build_provenance(
        worker=agent,
        model=model,
        prompt_ref=prompt_ref_for(prompt_hash_value, agent),
        tokens=total_tokens if has_usage else None,
        cost_usd=cost_usd,
        duration_s=(latency_ms / 1000.0) if latency_ms is not None else None,
    )

    # The packaged substrate_client (apps/substrate/client/) fixes its own
    # HTTP_TIMEOUT_S and takes no per-call override, so `timeout` no longer
    # bounds the request itself -- it bounds the whole call via wait_for
    # instead, preserving the pre-migration guarantee that a slow/hung store
    # can't make cost logging (best-effort by design) block the caller
    # beyond `timeout`.
    try:
        result = await asyncio.wait_for(
            _call_substrate(
                "create_bead",
                "platform",
                "cost",
                "recorded",
                content,
                agent,
                trust_tier="system",
                provenance=provenance,
            ),
            timeout=timeout,
        )
        return result.get("id")
    except Exception as exc:  # noqa: BLE001 — best-effort logging
        logger.warning(
            "Failed to write platform.cost bead for model=%s agent=%s: %s",
            model, agent, exc,
        )
        return None


__all__ = [
    "GPT4O_REFERENCE_PRICING",
    "SIMULATED_REFERENCE_MODEL",
    "calculate_simulated_value_usd",
    "redact_context",
    "prompt_hash",
    "log_llm_cost",
]
