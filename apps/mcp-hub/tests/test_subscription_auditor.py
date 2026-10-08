import json

import pytest

from workflows import subscription_auditor


def test_extract_json_payload_recovers_json():
    """subscription_auditor no longer has its own private fence-stripper --
    it imports the shared tools.litellm_client.extract_json_payload (also
    used by vision.py). This proves the wiring, not the helper itself
    (covered exhaustively in test_litellm_client.py)."""
    raw = '```json\n{"is_subscription": true}\n```'

    assert subscription_auditor.extract_json_payload(raw) == '{"is_subscription": true}'


def _netflix_cluster():
    return {
        "merchant": "NETFLIX",
        "display_name": "Netflix",
        "occurrences": 3,
        "first_seen": "2026-03-15",
        "last_seen": "2026-05-15",
        "amounts": [17.99, 17.99, 17.99],
        "samples": [],
    }


def _netflix_analysis_json():
    return json.dumps(
        {
            "is_subscription": True,
            "name": "Netflix",
            "category": "streaming",
            "amount": 17.99,
            "frequency": "monthly",
            "price_change": {
                "detected": False,
                "previous_amount": None,
                "new_amount": None,
                "change_pct": None,
                "first_increased_at": None,
            },
            "confidence": 0.9,
            "notes": "Recurring Netflix charge.",
        }
    )


# --- AC-1/AC-3: model resolution ------------------------------------------


@pytest.mark.asyncio
async def test_analyze_subscription_uses_default_model_and_json_schema(monkeypatch, httpx_mock):
    monkeypatch.delenv("SUBSCRIPTION_AUDIT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": _netflix_analysis_json()}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    assert result["model"] == "claude-haiku"
    assert result["analysis"]["is_subscription"] is True

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku"
    assert request_body["response_format"]["type"] == "json_schema"
    assert request_body["response_format"]["json_schema"]["name"] == "SubscriptionAnalysis"


@pytest.mark.asyncio
async def test_analyze_subscription_respects_audit_model_override(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSCRIPTION_AUDIT_MODEL", "claude-haiku-override")
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": _netflix_analysis_json()}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    assert result["model"] == "claude-haiku-override"
    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku-override"


@pytest.mark.asyncio
async def test_analyze_subscription_parses_fenced_and_prose_wrapped_reply_the_same(
    monkeypatch, httpx_mock
):
    monkeypatch.delenv("SUBSCRIPTION_AUDIT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    bare = _netflix_analysis_json()
    fenced = f"```json\n{bare}\n```"
    prose = f"Here you go:\n{bare}\nHope that helps."

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": fenced}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})
    fenced_result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": prose}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})
    prose_result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    assert fenced_result["analysis"] == prose_result["analysis"]
    assert fenced_result["analysis"]["is_subscription"] is True
    assert fenced_result["analysis"]["confidence"] == 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrap",
    [
        lambda bare: f"Here is the result [analysis]: {bare}",
        lambda bare: f"{bare} ... {{else}}.",
    ],
    ids=["bracket_shaped_prose_before", "bracket_shaped_prose_after"],
)
async def test_analyze_subscription_recovers_json_through_bracket_shaped_prose(
    monkeypatch, httpx_mock, wrap
):
    """PR #1092 gate, required change 1: these two raw replies each carry
    bracket-shaped text that is not the payload ('[analysis]', '{else}')
    next to the real JSON object, and must parse through the full
    analyze_subscription_activity path -- not just through
    extract_json_payload in isolation (test_litellm_client.py already covers
    the helper). analysis_source is "llm" on both the real and the
    deterministic-fallback path, so it proves nothing here; assert
    is_subscription/confidence instead."""
    monkeypatch.delenv("SUBSCRIPTION_AUDIT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    raw = wrap(_netflix_analysis_json())

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": raw}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    assert result["analysis"]["is_subscription"] is True
    assert result["analysis"]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_analyze_subscription_object_expected_skips_a_leading_array(
    monkeypatch, httpx_mock
):
    """PR #1092 gate, required change 2 (a regression the AC-6 fix
    introduced): 'Per rule [1]: {...}' contains a *valid* JSON array, [1],
    before the object. extract_json_payload's unqualified scan used to
    return that array; json.loads then produced a list, and
    enforce_price_hike's analysis.get(...) raised AttributeError (a list has
    no .get), so the activity raised instead of returning the parsed
    analysis -- the Temporal retry this caused is why the review called it a
    regression against #1085's deterministic-fallback behaviour. The fix:
    this call site now passes expected_type="object", which only tries '{'
    starts and so never considers '[1]'."""
    monkeypatch.delenv("SUBSCRIPTION_AUDIT_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    raw = f"Per rule [1]: {_netflix_analysis_json()}"

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": raw}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    result = await subscription_auditor.analyze_subscription_activity(_netflix_cluster())

    assert result["analysis"]["is_subscription"] is True
    assert result["analysis"]["name"] == "Netflix"
    assert result["analysis"]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_notify_subscriptions_uses_default_model(monkeypatch, httpx_mock):
    monkeypatch.delenv("SUBSCRIPTION_NOTIFY_MODEL", raising=False)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(subscription_auditor, "_send_discord_notification", _noop_send)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "Weekly summary."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    report = {
        "subscriptions": [
            {
                "name": "Netflix",
                "amount": 17.99,
                "frequency": "monthly",
                "price_change": {"detected": True, "previous_amount": 15.99, "new_amount": 17.99, "change_pct": 12.5},
            }
        ],
    }
    await subscription_auditor.notify_subscriptions_activity(report)

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku"


@pytest.mark.asyncio
async def test_notify_subscriptions_respects_notify_model_override(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSCRIPTION_NOTIFY_MODEL", "claude-haiku-notify-override")
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(subscription_auditor, "_send_discord_notification", _noop_send)

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={"choices": [{"message": {"content": "Weekly summary."}}]},
    )
    httpx_mock.add_response(url="http://substrate.test/beads", method="POST", json={"id": "cost-bead"})

    report = {
        "subscriptions": [
            {
                "name": "Netflix",
                "amount": 17.99,
                "frequency": "monthly",
                "price_change": {"detected": True, "previous_amount": 15.99, "new_amount": 17.99, "change_pct": 12.5},
            }
        ],
    }
    await subscription_auditor.notify_subscriptions_activity(report)

    request_body = json.loads(httpx_mock.get_requests()[0].read())
    assert request_body["model"] == "claude-haiku-notify-override"


async def _noop_send(*args, **kwargs):
    return True


def test_detect_price_hike_requires_two_recent_matching_charges():
    hike = subscription_auditor.detect_price_hike(
        [15.49, 17.99, 17.99],
        [
            {"amount": 15.49, "date": "2026-03-15"},
            {"amount": 17.99, "date": "2026-04-15"},
            {"amount": 17.99, "date": "2026-05-15"},
        ],
    )

    assert hike == {
        "detected": True,
        "previous_amount": 15.49,
        "new_amount": 17.99,
        "change_pct": 16.1,
        "first_increased_at": "2026-04-15",
    }


def test_detect_price_hike_ignores_one_off_spike():
    hike = subscription_auditor.detect_price_hike([15.49, 15.49, 24.99])

    assert hike["detected"] is False


def test_enforce_price_hike_sets_explicit_flag():
    cluster = {
        "amounts": [15.49, 17.99, 17.99],
        "samples": [
            {"amount": 15.49, "date": "2026-03-15"},
            {"amount": 17.99, "date": "2026-04-15"},
            {"amount": 17.99, "date": "2026-05-15"},
        ],
    }
    analysis = {
        "is_subscription": True,
        "price_change": {"detected": False},
    }

    enforced = subscription_auditor.enforce_price_hike(cluster, analysis)

    assert enforced["is_price_hike"] is True
    assert enforced["price_change"]["detected"] is True
    assert enforced["price_change"]["previous_amount"] == 15.49
    assert enforced["price_change"]["new_amount"] == 17.99


def test_deterministic_fallback_detects_marked_streaming_hike():
    cluster = {
        "merchant": "CODEX AUDIT STREAMING",
        "display_name": "Codex Audit Streaming*4242",
        "recent_amount": 9.99,
        "occurrences": 3,
        "first_seen": "2026-03-10",
        "last_seen": "2026-05-10",
        "amounts": [8.99, 9.99, 9.99],
        "samples": [
            {"amount": 8.99, "date": "2026-03-10"},
            {"amount": 9.99, "date": "2026-04-10"},
            {"amount": 9.99, "date": "2026-05-10"},
        ],
    }

    analysis = subscription_auditor.deterministic_subscription_analysis(cluster)

    assert analysis["is_subscription"] is True
    assert analysis["category"] == "streaming"
    assert analysis["frequency"] == "monthly"
    assert analysis["is_price_hike"] is True
    assert analysis["price_change"]["previous_amount"] == 8.99


def test_redact_account_references_keeps_prices():
    message = (
        "Weekly Subscription Audit: ≈ $18/mo\n"
        "- Netflix*1234: $17.99/mo\n"
        "- Card ending in 1234 should be hidden"
    )

    redacted = subscription_auditor.redact_account_references(message)

    assert "$17.99/mo" in redacted
    assert "1234" not in redacted
    assert "Netflix[redacted]" in redacted
    assert "ending in [redacted]" in redacted


def test_format_subscription_summary_highlights_hikes_and_redacts():
    message = subscription_auditor.format_subscription_summary(
        [
            {
                "name": "Codex Audit Streaming*4242",
                "category": "streaming",
                "amount": 9.99,
                "frequency": "monthly",
                "price_change": {
                    "detected": True,
                    "previous_amount": 8.99,
                    "new_amount": 9.99,
                    "change_pct": 11.1,
                },
            }
        ]
    )

    assert "Weekly Subscription Audit" in message
    assert "Price Hike" in message
    assert "4242" not in message
    assert "Codex Audit Streaming[redacted]" in message


@pytest.mark.asyncio
async def test_persist_subscription_writes_lineage_and_price_hike(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        json={"id": "subscription-bead"},
        status_code=200,
    )

    bead_id = await subscription_auditor.persist_subscription_activity(
        {
            "cluster": {
                "merchant": "NETFLIX",
                "display_name": "Netflix",
                "first_seen": "2026-03-15",
                "last_seen": "2026-05-15",
                "occurrences": 3,
                "evidence_tx_ids": ["tx-old", "tx-new-1", "tx-new-2"],
            },
            "analysis": {
                "is_subscription": True,
                "name": "Netflix",
                "category": "streaming",
                "amount": 17.99,
                "frequency": "monthly",
                "is_price_hike": True,
                "price_change": {
                    "detected": True,
                    "previous_amount": 15.49,
                    "new_amount": 17.99,
                    "change_pct": 16.1,
                    "first_increased_at": "2026-04-15",
                },
                "confidence": 0.93,
                "notes": "Monthly streaming charge.",
            },
            "model": "gemini-pro",
            "prompt_hash": "deadbeefcafebabe",
            "latency_ms": 123.45,
        }
    )

    assert bead_id == "subscription-bead"
    request = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    assert request.headers["X-API-Key"] == "test-key"
    body = json.loads(request.content)
    assert body["namespace"] == "finance"
    assert body["type"] == "subscription"
    assert body["content"]["is_price_hike"] is True
    assert body["content"]["price_change"]["detected"] is True
    assert body["content"]["evidence_tx_ids"] == ["tx-old", "tx-new-1", "tx-new-2"]
    assert body["content"]["series_key"] == "NETFLIX"
    assert body["provenance"] == {
        "worker": "subscription-auditor-workflow",
        "model": "gemini-pro",
        "prompt_ref": "prompt-sha256:deadbeefcafebabe",
        "tokens": None,
        "cost_usd": None,
        "duration_s": 0.12345,
    }


def _analyzed_netflix(evidence_tx_ids=("tx-1",)):
    return {
        "cluster": {
            "merchant": "NETFLIX",
            "display_name": "Netflix",
            "first_seen": "2026-03-15",
            "last_seen": "2026-05-15",
            "occurrences": 3,
            "evidence_tx_ids": list(evidence_tx_ids),
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
async def test_persist_subscription_rerun_on_unchanged_input_creates_zero_new_beads(
    httpx_mock, monkeypatch
):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json={"id": "sub-1"})

    first_id = await subscription_auditor.persist_subscription_activity(_analyzed_netflix())
    assert first_id == "sub-1"

    httpx_mock.add_response(
        method="GET",
        json=[
            {
                "id": "sub-1",
                "created_at": "2026-09-14T09:00:00Z",
                "content": {"series_key": "NETFLIX", "merchant_key": "NETFLIX", "first_seen": "2026-03-15"},
            }
        ],
    )
    httpx_mock.add_response(method="PATCH", json={"id": "sub-1"})

    second_id = await subscription_auditor.persist_subscription_activity(_analyzed_netflix())
    assert second_id == "sub-1"

    requests = httpx_mock.get_requests()
    assert sum(1 for r in requests if r.method == "POST") == 1
    assert sum(1 for r in requests if r.method == "PATCH") == 1


@pytest.mark.asyncio
async def test_persist_subscription_upserts_the_newest_legacy_bead_for_the_merchant(
    httpx_mock, monkeypatch
):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        method="GET",
        json=[
            {
                "id": "old-bead",
                "created_at": "2026-07-26T09:00:00Z",
                "content": {"merchant_key": "NETFLIX", "first_seen": "2026-01-26"},
            },
            {
                "id": "new-bead",
                "created_at": "2026-08-02T09:00:00Z",
                "content": {"merchant_key": "NETFLIX", "first_seen": "2026-02-02"},
            },
        ],
    )
    httpx_mock.add_response(method="PATCH", json={"id": "new-bead"})

    bead_id = await subscription_auditor.persist_subscription_activity(_analyzed_netflix())

    assert bead_id == "new-bead"
    requests = httpx_mock.get_requests()
    assert sum(1 for r in requests if r.method == "POST") == 0
    patch_requests = [r for r in requests if r.method == "PATCH"]
    assert len(patch_requests) == 1
    assert patch_requests[0].url.path == "/beads/new-bead"
    patch_body = json.loads(patch_requests[0].content)
    # first_seen merges to the earlier of the matched bead's and the new one.
    assert patch_body["content"]["first_seen"] == "2026-02-02"


@pytest.mark.asyncio
async def test_persist_subscription_posts_for_a_different_merchant(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")

    httpx_mock.add_response(
        method="GET",
        json=[
            {
                "id": "other-bead",
                "created_at": "2026-08-02T09:00:00Z",
                "content": {"merchant_key": "SPOTIFY", "first_seen": "2026-02-02"},
            }
        ],
    )
    httpx_mock.add_response(method="POST", json={"id": "sub-new"})

    bead_id = await subscription_auditor.persist_subscription_activity(_analyzed_netflix())

    assert bead_id == "sub-new"
    requests = httpx_mock.get_requests()
    assert sum(1 for r in requests if r.method == "POST") == 1
    assert sum(1 for r in requests if r.method == "PATCH") == 0


@pytest.mark.asyncio
async def test_persist_subscription_non_subscription_returns_none_before_any_request(monkeypatch):
    analyzed = _analyzed_netflix()
    analyzed["analysis"]["is_subscription"] = False

    async def _boom(*args, **kwargs):
        raise AssertionError("must not make a request for a non-subscription cluster")

    monkeypatch.setattr(subscription_auditor, "query_beads", _boom)

    bead_id = await subscription_auditor.persist_subscription_activity(analyzed)

    assert bead_id is None


@pytest.mark.asyncio
async def test_notify_routes_through_the_alert_policy(monkeypatch):
    """The auditor must not post straight to the webhook; the alert policy owns
    duplicate suppression and the post adapter owns Discord retry behavior."""
    sent = []

    class FakePolicy:
        async def send(self, kind, fingerprint, content, **kwargs):
            sent.append((kind, fingerprint, content, kwargs))
            return True

    monkeypatch.setattr(subscription_auditor, "_alert_policy", lambda: FakePolicy())
    monkeypatch.setattr(subscription_auditor, "log_llm_cost", _noop_cost)
    monkeypatch.setattr(subscription_auditor.httpx, "AsyncClient", _boom_client)

    report = {
        "subscriptions": [
            {
                "name": "Netflix",
                "amount": 17.99,
                "frequency": "monthly",
                "price_change": {
                    "detected": True,
                    "previous_amount": 15.99,
                    "new_amount": 17.99,
                    "change_pct": 12.5,
                },
            }
        ],
        "price_hikes": [],
    }
    message = await subscription_auditor.notify_subscriptions_activity(report)

    assert len(sent) == 1
    assert sent[0][0] == "discord.notification"
    assert sent[0][1] == subscription_auditor.alert_content_fingerprint(message)
    assert "Decision: decide whether to keep, cancel, or downgrade" in sent[0][2]
    assert "Netflix" in message


@pytest.mark.asyncio
async def test_notify_suppresses_status_only_subscription_rollup(monkeypatch):
    sent = []

    class FakePolicy:
        async def send(self, kind, fingerprint, content, **kwargs):
            sent.append((kind, fingerprint, content, kwargs))
            return True

    monkeypatch.setattr(subscription_auditor, "_alert_policy", lambda: FakePolicy())

    report = {
        "subscriptions": [
            {"name": "Netflix", "amount": 17.99, "frequency": "monthly"}
        ],
        "price_hikes": [],
    }

    assert await subscription_auditor.notify_subscriptions_activity(report) is None
    assert sent == []


async def _noop_cost(*args, **kwargs):
    return None


def _boom_client(*args, **kwargs):
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("LLM down")

    return _C()
