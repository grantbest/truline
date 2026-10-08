"""Weekly subscription auditor — Phase 4.4.

Scans the last six months of ``finance.transaction`` beads for recurring
merchants, asks the LLM (via LiteLLM) to extract subscription metadata and
detect price changes, persists one ``finance.subscription`` bead per
detected subscription, then posts a Discord summary highlighting any
"Price Hike" warnings.

Scheduled by ``temporal_worker.ensure_schedules`` (see
``subscription-audit-weekly`` — Sunday 09:00 America/Chicago).

Constraints honoured
--------------------
- Model: every LLM call routes through LiteLLM. The default model is
  ``tools.litellm_client.LIFEOPS_DEFAULT_MODEL`` (``claude-haiku``, per
  the Operator's 2026-09-27 decision). Override with the ``SUBSCRIPTION_AUDIT_MODEL``
  / ``SUBSCRIPTION_NOTIFY_MODEL`` env vars.
- Per-bead evidence: each ``finance.subscription`` bead records the exact
  transaction UUIDs used as evidence in ``content.evidence_tx_ids``.
  ``provenance`` (``tools.provenance.build_provenance``) carries only the
  six ``BeadProvenance`` keys Substrate's schema accepts (LO-d333e0cf) —
  it is not a place to grep for evidence.
- Upsert: one bead per series key (``content.series_key`` — the cluster's
  merchant), not one per weekly run (LO-d333e0cf/AC-5).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    # httpx is unused here since M12's migration moved this module's one
    # substrate write to finance._create_bead_raw -- kept only because
    # tests/test_subscription_auditor.py's notify test still monkeypatches
    # this module's own httpx.AsyncClient as a network trip-wire.
    import httpx  # noqa: F401
    from src.tools.cost import log_llm_cost, prompt_hash
    from src.tools.litellm_client import (
        LIFEOPS_DEFAULT_MODEL,
        chat_completion,
        extract_json_payload,
    )
    from src.tools.notify import (
        AlertSeverity,
        alert_content_fingerprint,
        finance_alert_policy,
        post_discord_raise_on_error,
        send_alert,
    )
    from src.tools.finance import (
        FINANCE_NAMESPACE,
        _create_bead_raw,
        _raw_substrate_request,
        get_recurring_candidates,
        query_beads,
    )
    from src.tools.provenance import build_provenance, prompt_ref_for

logger = logging.getLogger(__name__)


class SubscriptionPriceChange(BaseModel):
    detected: bool
    previous_amount: Optional[float] = None
    new_amount: Optional[float] = None
    change_pct: Optional[float] = None
    first_increased_at: Optional[str] = None


class SubscriptionAnalysis(BaseModel):
    """Mirrors the JSON shape documented in ``analyze_subscription_activity``'s
    prompt — used only to derive a ``json_schema`` response_format hint for
    the gateway; the reply is still parsed with ``json.loads`` since it may
    omit fields the deterministic fallback fills in."""

    is_subscription: bool
    name: Optional[str] = None
    category: str
    amount: Optional[float] = None
    frequency: str
    price_change: SubscriptionPriceChange
    confidence: float
    notes: str


DEFAULT_MODEL = LIFEOPS_DEFAULT_MODEL
DEFAULT_NOTIFY_MODEL = LIFEOPS_DEFAULT_MODEL
MIN_OCCURRENCES = 3
LOOKBACK_MONTHS = 6
SUBSCRIPTION_KEYWORDS = (
    "ADOBE",
    "APPLE",
    "AUDIBLE",
    "CHATGPT",
    "CLAUDE",
    "CLOUDFLARE",
    "CODEX AUDIT STREAMING",
    "DISNEY",
    "DROPBOX",
    "GEMINI",
    "GOOGLE",
    "GYM",
    "HULU",
    "ICLOUD",
    "INSURANCE",
    "MAX",
    "NETFLIX",
    "OPENAI",
    "PARAMOUNT",
    "PEACOCK",
    "SPOTIFY",
    "STREAMING",
    "SUBSCRIPTION",
    "YOUTUBE",
)
MERCHANT_REFERENCE_RE = re.compile(
    r"(?i)\b(account|acct|card|mask|ending(?:\s+in)?)\b([\s:#*\-]*)(\*{2,}\d{2,}|x{2,}\d{2,}|\d{2,})"
)
MERCHANT_SUFFIX_RE = re.compile(r"(?i)\b([A-Z][A-Z0-9 ._-]{2,}?)([*#-]\d{4,})\b")


def _current_workflow_id() -> Optional[str]:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _alert_policy():
    return finance_alert_policy(post=post_discord_raise_on_error)


async def _send_discord_notification(message: str) -> bool:
    return await send_alert(
        _alert_policy(),
        "subscription_auditor.price_hike",
        alert_content_fingerprint(message),
        message,
        severity=AlertSeverity.ACTIONABLE,
    )


def _coerce_amounts(raw_amounts: Any) -> List[float]:
    amounts: List[float] = []
    for raw in raw_amounts or []:
        try:
            amount = round(float(raw), 2)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            amounts.append(amount)
    return amounts


def detect_price_hike(amounts: Any, samples: Any = None) -> Dict[str, Any]:
    """Deterministically flag a held >=3% increase.

    The LLM still explains the subscription, but the pass/fail price-hike
    signal should not depend on model temperament. We require the two most
    recent charges to be equal and at least 3% above a previous lower
    recurring amount.
    """
    clean_amounts = _coerce_amounts(amounts)
    if len(clean_amounts) < 3:
        return {
            "detected": False,
            "previous_amount": None,
            "new_amount": None,
            "change_pct": None,
            "first_increased_at": None,
        }

    new_amount = clean_amounts[-1]
    previous_recent = clean_amounts[-2]
    if previous_recent == 0 or abs(previous_recent - new_amount) / new_amount > 0.01:
        return {
            "detected": False,
            "previous_amount": None,
            "new_amount": None,
            "change_pct": None,
            "first_increased_at": None,
        }

    previous_amount = None
    for amount in reversed(clean_amounts[:-2]):
        if amount < new_amount * 0.97:
            previous_amount = amount
            break
    if previous_amount is None or previous_amount <= 0:
        return {
            "detected": False,
            "previous_amount": None,
            "new_amount": None,
            "change_pct": None,
            "first_increased_at": None,
        }

    change_pct = round(((new_amount - previous_amount) / previous_amount) * 100, 1)
    first_increased_at = None
    clean_samples = samples or []
    if len(clean_samples) == len(clean_amounts):
        for sample in clean_samples:
            try:
                sample_amount = round(float(sample.get("amount")), 2)
            except (AttributeError, TypeError, ValueError):
                continue
            if abs(sample_amount - new_amount) / new_amount <= 0.01:
                first_increased_at = sample.get("date")
                break

    return {
        "detected": True,
        "previous_amount": previous_amount,
        "new_amount": new_amount,
        "change_pct": change_pct,
        "first_increased_at": first_increased_at,
    }


def enforce_price_hike(cluster: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    deterministic = detect_price_hike(cluster.get("amounts"), cluster.get("samples"))
    if not deterministic["detected"]:
        return analysis

    merged = dict(analysis)
    llm_change = dict(merged.get("price_change") or {})
    llm_change.update({k: v for k, v in deterministic.items() if v is not None})
    llm_change["detected"] = True
    merged["price_change"] = llm_change
    merged["is_price_hike"] = True
    return merged


def redact_account_references(message: str) -> str:
    message = MERCHANT_REFERENCE_RE.sub(lambda m: f"{m.group(1)} [redacted]", message)
    return MERCHANT_SUFFIX_RE.sub(lambda m: f"{m.group(1).rstrip()}[redacted]", message)


def _deterministic_frequency(cluster: Dict[str, Any]) -> str:
    first_seen = cluster.get("first_seen")
    last_seen = cluster.get("last_seen")
    occurrences = int(cluster.get("occurrences") or 0)
    if not first_seen or not last_seen or occurrences < 2:
        return "unknown"
    try:
        first = datetime.fromisoformat(str(first_seen)[:10])
        last = datetime.fromisoformat(str(last_seen)[:10])
    except ValueError:
        return "unknown"
    span_days = max((last - first).days, 1)
    avg_gap = span_days / max(occurrences - 1, 1)
    if 24 <= avg_gap <= 38:
        return "monthly"
    if 6 <= avg_gap <= 9:
        return "weekly"
    if 75 <= avg_gap <= 105:
        return "quarterly"
    if 330 <= avg_gap <= 400:
        return "annual"
    return "unknown"


def deterministic_subscription_analysis(
    cluster: Dict[str, Any],
    *,
    reason: str = "LLM unavailable; deterministic fallback used.",
) -> Dict[str, Any]:
    merchant_key = str(cluster.get("merchant") or "").upper()
    display_name = cluster.get("display_name") or cluster.get("merchant")
    is_subscription = any(keyword in merchant_key for keyword in SUBSCRIPTION_KEYWORDS)
    price_change = detect_price_hike(cluster.get("amounts"), cluster.get("samples"))
    return {
        "is_subscription": is_subscription,
        "name": display_name if is_subscription else None,
        "category": "streaming" if "STREAMING" in merchant_key else "other",
        "amount": cluster.get("recent_amount") if is_subscription else None,
        "frequency": _deterministic_frequency(cluster) if is_subscription else "unknown",
        "price_change": price_change,
        "is_price_hike": bool(price_change.get("detected")),
        "confidence": 0.65 if is_subscription else 0.0,
        "notes": reason,
    }


def _monthly_equivalent(subscription: Dict[str, Any]) -> float:
    try:
        amount = float(subscription.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    frequency = str(subscription.get("frequency") or "unknown").lower()
    if frequency == "annual":
        return amount / 12
    if frequency == "quarterly":
        return amount / 3
    if frequency == "semiannual":
        return amount / 6
    if frequency == "weekly":
        return amount * 4.33
    return amount


def _frequency_short(frequency: Any) -> str:
    return {
        "monthly": "mo",
        "annual": "yr",
        "quarterly": "qtr",
        "weekly": "wk",
        "semiannual": "biyr",
    }.get(str(frequency or "").lower(), "?")


def format_subscription_summary(subscriptions: List[Dict[str, Any]]) -> str:
    sorted_subs = sorted(subscriptions, key=_monthly_equivalent, reverse=True)
    total = round(sum(_monthly_equivalent(s) for s in sorted_subs))
    lines = [f"📺 Weekly Subscription Audit: ≈ ${total}/mo"]
    for subscription in sorted_subs:
        name = subscription.get("name") or "Unknown subscription"
        category = subscription.get("category") or "other"
        amount = subscription.get("amount") or 0
        lines.append(f"- {name} ({category}): ${amount}/{_frequency_short(subscription.get('frequency'))}")
    for subscription in sorted_subs:
        change = subscription.get("price_change") or {}
        if not change.get("detected"):
            continue
        lines.append(
            "⚠️ Price Hike: "
            f"{subscription.get('name') or 'Unknown subscription'} went from "
            f"${change.get('previous_amount')} to ${change.get('new_amount')} "
            f"({change.get('change_pct')}% increase)"
        )
    return redact_account_references("\n".join(lines[:25]))


# --- Activities ----------------------------------------------------------


@activity.defn
async def cluster_transactions_activity() -> Dict[str, Any]:
    """Activity 1 — Search/Cluster.

    Pulls the last six months of ``finance.transaction`` beads, groups them
    by normalized merchant name, then uses ``/beads/search`` to merge
    lexically-distinct neighbours into the same cluster (e.g.,
    ``NETFLIX.COM`` vs ``NETFLIX SUBSCRIPTION``). See
    ``tools/finance.get_recurring_candidates`` for the full algorithm.

    Returns::

        {"as_of": ISO, "lookback_months": 6, "clusters": [ ... ]}

    where each cluster has the shape documented on
    ``get_recurring_candidates``.
    """
    clusters = await get_recurring_candidates(
        months=LOOKBACK_MONTHS,
        min_occurrences=MIN_OCCURRENCES,
        use_semantic_merge=True,
    )
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "lookback_months": LOOKBACK_MONTHS,
        "min_occurrences": MIN_OCCURRENCES,
        "clusters": clusters,
    }


@activity.defn
async def analyze_subscription_activity(cluster: Dict[str, Any]) -> Dict[str, Any]:
    """Activity 2 — Analyze.

    Sends a single cluster to the LLM and asks for structured subscription
    metadata: name, amount, frequency, and whether a price change occurred.
    One activity invocation per cluster so retries are cluster-scoped and a
    single Gemini quota blip doesn't blow up the whole audit.

    The activity is the LLM seam; cost is logged regardless of outcome.
    """
    model = os.environ.get("SUBSCRIPTION_AUDIT_MODEL", DEFAULT_MODEL)
    litellm_api_key = os.environ.get("LITELLM_API_KEY")

    from src.untrusted import wrap_untrusted, wrap_untrusted_short

    llm_view = {
        "merchant_display": wrap_untrusted_short(cluster.get("display_name")),
        "merchant_key": wrap_untrusted_short(cluster.get("merchant")),
        "occurrences": cluster.get("occurrences"),
        "first_seen": cluster.get("first_seen"),
        "last_seen": cluster.get("last_seen"),
        "amount_history": cluster.get("amounts"),
        "samples": [
            {**s, "name": wrap_untrusted(s.get("name")), "merchant_name": wrap_untrusted_short(s.get("merchant_name"))}
            if isinstance(s, dict) else wrap_untrusted(s)
            for s in (cluster.get("samples") or [])
        ],
    }

    prompt = f"""You are a personal-finance analyst classifying recurring charges.

Given this merchant's transaction history, decide whether it is a recurring SUBSCRIPTION
(streaming, SaaS, gym, insurance, etc.) or NOT (groceries, restaurants, gas, one-offs).

Return STRICT JSON only — no markdown, no commentary — matching this schema:

{{
  "is_subscription": true|false,
  "name": "<canonical product name, e.g. 'Netflix Standard'>",
  "category": "streaming|saas|gym|insurance|news|cloud|gaming|other",
  "amount": <number, current per-charge amount in USD>,
  "frequency": "monthly|quarterly|semiannual|annual|weekly|unknown",
  "price_change": {{
    "detected": true|false,
    "previous_amount": <number or null>,
    "new_amount": <number or null>,
    "change_pct": <number or null>,
    "first_increased_at": "<YYYY-MM-DD or null>"
  }},
  "confidence": <0..1 float — how sure you are this is a subscription>,
  "notes": "<one short sentence explaining the decision>"
}}

If is_subscription is false, still return the full object — set the other fields to null/0 as appropriate.
A price_change.detected = true means the per-charge amount changed by ≥3% AND the new amount has held for ≥2 most-recent occurrences.

DATA:
{json.dumps(llm_view, indent=2)}
"""

    headers = {"Authorization": f"Bearer {litellm_api_key}"} if litellm_api_key else {}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "SubscriptionAnalysis",
                "schema": SubscriptionAnalysis.model_json_schema(),
            },
        },
    }

    started = time.monotonic()
    hash_value = prompt_hash(prompt)
    try:
        result = await chat_completion(payload, headers=headers, timeout=60.0)
        body = result.body
    except Exception as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        await log_llm_cost(
            model=model,
            usage=None,
            agent="subscription-auditor/analyze",
            context={
                "occurrences": cluster.get("occurrences"),
                "first_seen": cluster.get("first_seen"),
                "last_seen": cluster.get("last_seen"),
                "outcome": "llm_error",
                "exc_type": type(exc).__name__,
            },
            workflow_id=_current_workflow_id(),
            latency_ms=latency_ms,
            prompt_hash_value=hash_value,
        )
        return {
            "cluster": cluster,
            "analysis": deterministic_subscription_analysis(cluster),
            "model": model,
            "prompt_hash": hash_value,
            "latency_ms": round(latency_ms, 2),
            "analysis_source": "deterministic",
        }
    latency_ms = (time.monotonic() - started) * 1000.0

    raw_text = body["choices"][0]["message"]["content"]
    analysis_text = extract_json_payload(raw_text, expected_type="object")
    try:
        analysis = json.loads(analysis_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "subscription-auditor: LLM returned non-JSON for merchant=%s: %s",
            cluster.get("merchant"),
            exc,
        )
        analysis = {
            "is_subscription": False,
            "name": cluster.get("display_name") or cluster.get("merchant"),
            "category": "other",
            "amount": None,
            "frequency": "unknown",
            "price_change": {
                "detected": False,
                "previous_amount": None,
                "new_amount": None,
                "change_pct": None,
                "first_increased_at": None,
            },
            "confidence": 0.0,
            "notes": "LLM returned non-JSON; treated as non-subscription.",
        }

    analysis = enforce_price_hike(cluster, analysis)

    await log_llm_cost(
        model=model,
        usage=body.get("usage"),
        agent="subscription-auditor/analyze",
        # No merchant/amount in context — log_llm_cost.redact_context would
        # strip them anyway, but the contract is "tags only".
        context={
            "occurrences": cluster.get("occurrences"),
            "first_seen": cluster.get("first_seen"),
            "last_seen": cluster.get("last_seen"),
        },
        workflow_id=_current_workflow_id(),
        latency_ms=latency_ms,
        prompt_hash_value=hash_value,
        cost_usd=result.cost_usd,
    )

    return {
        "cluster": cluster,
        "analysis": analysis,
        "model": model,
        "prompt_hash": hash_value,
        "latency_ms": round(latency_ms, 2),
        "analysis_source": "llm",
    }


def _subscription_bead_matches(bead: Dict[str, Any], series_key: Any) -> bool:
    """A stored bead is this series when it already carries the key, or --
    for every bead written before AC-5 -- when it carries no key at all and
    its ``merchant_key`` (the only key those beads have) matches instead."""
    content = bead.get("content") or {}
    stored_series_key = content.get("series_key")
    if stored_series_key is not None:
        return stored_series_key == series_key
    return content.get("merchant_key") == series_key


def _subscription_match_sort_key(bead: Dict[str, Any]) -> Tuple[str, str]:
    return (str(bead.get("created_at") or ""), str(bead.get("id") or ""))


@activity.defn
async def persist_subscription_activity(analyzed: Dict[str, Any]) -> Optional[str]:
    """Activity 3 — Persist.

    Writes (or, per AC-5, upserts) one ``finance.subscription`` bead per
    series key (``content.series_key`` — the cluster's merchant). Evidence
    transaction UUIDs move into ``content.evidence_tx_ids`` (LO-d333e0cf's
    D7/design note (b): a ``BeadProvenance`` link would need one extra POST
    per transaction per bead every week, could leave a partial write, and
    nothing reads it). ``LO-BIL-002/AC-1`` is what this satisfies now;
    ``AC-2``'s "provenance.parent_ids" wording no longer holds for any
    writer after ``BeadProvenance`` went ``extra="forbid"`` (5618d3bf) —
    re-wording it is the outer loop's, not this bead's.

    Returns the (possibly pre-existing, on an upsert) bead id, or ``None``
    if the analyzer classified the cluster as non-subscription (we don't
    persist negatives — they'd just pollute the bead store on every weekly
    run). The non-subscription check runs before any request.
    """
    analysis = analyzed.get("analysis", {}) or {}
    if not analysis.get("is_subscription"):
        return None

    cluster = analyzed.get("cluster", {}) or {}
    model = analyzed.get("model") or DEFAULT_MODEL
    evidence_tx_ids = list(cluster.get("evidence_tx_ids") or [])
    series_key = cluster.get("merchant")

    price_change = analysis.get("price_change") or {}
    is_price_hike = bool(analysis.get("is_price_hike") or price_change.get("detected"))
    confidence = analysis.get("confidence")

    content = {
        "name": analysis.get("name") or cluster.get("display_name"),
        "merchant_key": cluster.get("merchant"),
        "series_key": series_key,
        "category": analysis.get("category"),
        "amount": analysis.get("amount"),
        "frequency": analysis.get("frequency"),
        "first_seen": cluster.get("first_seen"),
        "last_seen": cluster.get("last_seen"),
        "occurrences": cluster.get("occurrences"),
        "evidence_count": len(evidence_tx_ids),
        "evidence_tx_ids": evidence_tx_ids,
        "analysis_source": analyzed.get("analysis_source"),
        "is_price_hike": is_price_hike,
        "price_change": {
            "detected": is_price_hike,
            "previous_amount": price_change.get("previous_amount"),
            "new_amount": price_change.get("new_amount"),
            "change_pct": price_change.get("change_pct"),
            "first_increased_at": price_change.get("first_increased_at"),
        },
        "confidence": confidence,
        "notes": analysis.get("notes"),
        "model": model,
    }

    existing = await query_beads({"type": "subscription", "state": "active", "limit": 1000})
    matches = [bead for bead in existing if _subscription_bead_matches(bead, series_key)]

    if matches:
        target = max(matches, key=_subscription_match_sort_key)
        target_content = target.get("content") or {}
        first_seen_candidates = [
            value
            for value in (target_content.get("first_seen"), content.get("first_seen"))
            if value
        ]
        if len(first_seen_candidates) == 2:
            content["first_seen"] = min(first_seen_candidates)
        elif first_seen_candidates:
            content["first_seen"] = first_seen_candidates[0]

        await _raw_substrate_request(
            "PATCH",
            f"/{target['id']}",
            json={
                "content": content,
                "confidence": confidence,
                "created_by": "subscription-auditor-workflow",
            },
        )
        return target["id"]

    provenance_model = "none" if analyzed.get("analysis_source") == "deterministic" else model
    latency_ms = analyzed.get("latency_ms")
    provenance = build_provenance(
        worker="subscription-auditor-workflow",
        model=provenance_model,
        prompt_ref=prompt_ref_for(analyzed.get("prompt_hash"), "subscription-auditor/persist"),
        duration_s=(latency_ms / 1000.0) if latency_ms is not None else None,
    )

    payload = {
        "namespace": FINANCE_NAMESPACE,
        "type": "subscription",
        "state": "active",
        "content": content,
        "confidence": confidence,
        "provenance": provenance,
        "trust_tier": "system",
        "created_by": "subscription-auditor-workflow",
    }

    # AC-1 named carve-out: this body's top-level `confidence` field (a
    # duplicate of content.confidence, read by the console) is not one of
    # create_bead's parameters, and the packaged client would silently drop
    # it. Routed through finance._create_bead_raw -- the one shared helper
    # for a POST /beads body create_bead cannot express -- unchanged.
    bead = await _create_bead_raw(payload)
    return bead.get("id")


@activity.defn
async def notify_subscriptions_activity(report: Dict[str, Any]) -> Optional[str]:
    """Activity 4 — Notify.

    Formats a Discord summary of all active subscriptions and any
    price-hike warnings. Uses ``LIFEOPS_DEFAULT_MODEL`` by default (cheap,
    sufficient for a short formatting prompt). Cost is logged regardless of
    whether the Discord webhook is configured.

    Returns the message string (or ``None`` when there's nothing to send).
    """
    subscriptions: List[Dict[str, Any]] = report.get("subscriptions") or []
    if not subscriptions:
        return None

    model = os.environ.get("SUBSCRIPTION_NOTIFY_MODEL", DEFAULT_NOTIFY_MODEL)
    litellm_api_key = os.environ.get("LITELLM_API_KEY")

    # Trim context for the LLM — only the fields needed to write the message.
    llm_view = [
        {
            "name": s.get("name"),
            "category": s.get("category"),
            "amount": s.get("amount"),
            "frequency": s.get("frequency"),
            "price_change": s.get("price_change") or {},
        }
        for s in subscriptions
    ]
    price_hikes = [s for s in llm_view if (s.get("price_change") or {}).get("detected")]
    if not price_hikes:
        return None

    prompt = f"""You are the Truline CFO sending a weekly Discord summary of household subscriptions.

ACTIVE SUBSCRIPTIONS ({len(llm_view)} total):
{json.dumps(llm_view, indent=2)}

PRICE HIKES THIS WEEK ({len(price_hikes)}):
{json.dumps(price_hikes, indent=2)}

Write ONE Discord message (plain text, no markdown headers):
- Line 1: "📺 Weekly Subscription Audit:" followed by the total monthly-equivalent dollar amount.
  (Convert each subscription to a monthly figure: annual/12, quarterly/3, weekly*4.33, semiannual/6.
   Round to whole dollars. Show the total like "≈ $137/mo".)
- One bullet per active subscription, sorted by monthly cost descending. Format:
  "- {{name}} ({{category}}): ${{amount}}/{{frequency-short}}"
  where frequency-short is mo|yr|qtr|wk|biyr|? (best guess from the frequency field).
- If there are price hikes, add a "⚠️ Price Hike:" line for each, format:
  "⚠️ Price Hike: {{name}} went from ${{previous}} to ${{new}} ({{pct}}% increase)"
- No closing remark. Maximum 25 lines total."""

    headers = {"Authorization": f"Bearer {litellm_api_key}"} if litellm_api_key else {}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}

    started = time.monotonic()
    hash_value = prompt_hash(prompt)
    try:
        result = await chat_completion(payload, headers=headers, timeout=60.0)
        body = result.body
    except Exception as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        await log_llm_cost(
            model=model,
            usage=None,
            agent="subscription-auditor/notify",
            context={
                "subscription_count": len(llm_view),
                "price_hike_count": len(price_hikes),
                "outcome": "llm_error",
                "exc_type": type(exc).__name__,
            },
            workflow_id=_current_workflow_id(),
            latency_ms=latency_ms,
            prompt_hash_value=hash_value,
        )
        message = format_subscription_summary(llm_view)
        await _send_discord_notification(message)
        return message
    latency_ms = (time.monotonic() - started) * 1000.0

    message = redact_account_references(body["choices"][0]["message"]["content"].strip())

    await log_llm_cost(
        model=model,
        usage=body.get("usage"),
        agent="subscription-auditor/notify",
        context={
            "subscription_count": len(llm_view),
            "price_hike_count": len(price_hikes),
        },
        workflow_id=_current_workflow_id(),
        latency_ms=latency_ms,
        cost_usd=result.cost_usd,
        prompt_hash_value=hash_value,
    )

    await _send_discord_notification(message)

    return message


# --- Workflow -------------------------------------------------------------


@workflow.defn
class WeeklySubscriptionAuditWorkflow:
    """Weekly subscription auditor. Scheduled by
    ``temporal_worker.ensure_schedules`` for Sunday 09:00 America/Chicago.
    """

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        # Phase 4.1 convention: maximum_interval ≥ 60s so retries straddle the
        # Gemini free-tier per-minute quota reset; 90s adds headroom.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=90),
            maximum_attempts=3,
        )

        cluster_report = await workflow.execute_activity(
            cluster_transactions_activity,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )

        clusters: List[Dict[str, Any]] = cluster_report.get("clusters") or []
        if not clusters:
            return {
                "cluster_count": 0,
                "subscription_count": 0,
                "price_hike_count": 0,
                "notified": False,
                "subscription_bead_ids": [],
            }

        # Analyze each cluster as a separate activity invocation so a single
        # Gemini quota error doesn't take the whole audit down. Sequential
        # (not parallel) to stay friendly to the free-tier per-minute limit.
        analyzed_clusters: List[Dict[str, Any]] = []
        for cluster in clusters:
            analyzed = await workflow.execute_activity(
                analyze_subscription_activity,
                cluster,
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=retry_policy,
            )
            analyzed_clusters.append(analyzed)

        subscription_bead_ids: List[str] = []
        subscriptions_for_notify: List[Dict[str, Any]] = []
        price_hike_count = 0
        for analyzed in analyzed_clusters:
            bead_id = await workflow.execute_activity(
                persist_subscription_activity,
                analyzed,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if not bead_id:
                continue
            subscription_bead_ids.append(bead_id)
            analysis = analyzed.get("analysis") or {}
            subscriptions_for_notify.append(
                {
                    "name": analysis.get("name"),
                    "category": analysis.get("category"),
                    "amount": analysis.get("amount"),
                    "frequency": analysis.get("frequency"),
                    "price_change": analysis.get("price_change") or {},
                    "bead_id": bead_id,
                }
            )
            if (analysis.get("price_change") or {}).get("detected"):
                price_hike_count += 1

        notify_report = {
            "as_of": cluster_report.get("as_of"),
            "subscriptions": subscriptions_for_notify,
            "price_hike_count": price_hike_count,
        }

        notified_message: Optional[str] = None
        if subscriptions_for_notify:
            notified_message = await workflow.execute_activity(
                notify_subscriptions_activity,
                notify_report,
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=retry_policy,
            )

        return {
            "cluster_count": len(clusters),
            "subscription_count": len(subscriptions_for_notify),
            "price_hike_count": price_hike_count,
            "subscription_bead_ids": subscription_bead_ids,
            "notified": notified_message is not None,
        }
