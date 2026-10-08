"""AC-3: each writer's real POST body validates against the real Substrate
schema.

Imports the actual ``apps/substrate/src/schemas.py`` module (never a copy or
a stub -- a test double that accepts more than the live contract is a second
implementation) and drives each of the five writers through its public
function with ``pytest_httpx`` mocking the store, exactly like the recorder
tests in ``test_substrate_client_migration_recorder.py``. Each assertion
takes the JSON body the writer actually POSTed, not a body this test
rebuilds.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pydantic
import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SUBSTRATE_SRC = _REPO_ROOT / "apps" / "substrate" / "src"
if str(_SUBSTRATE_SRC) not in sys.path:
    sys.path.insert(0, str(_SUBSTRATE_SRC))

import schemas  # noqa: E402 -- the real substrate schema module, path-inserted above

from tools import cost, vision  # noqa: E402
from workflows import budget_pulse, financial_insights, subscription_auditor  # noqa: E402

# asyncio_mode = "auto" (pyproject.toml) collects the async tests below;
# no blanket pytestmark, since two tests here are deliberately sync.

_SIX_KEYS = {"worker", "model", "prompt_ref", "tokens", "cost_usd", "duration_s"}


def test_schemas_module_is_the_real_substrate_module():
    resolved = pathlib.Path(schemas.__file__).resolve()
    assert resolved == (_SUBSTRATE_SRC / "schemas.py").resolve()


def _assert_body_is_accepted(body: dict) -> None:
    schemas.BeadCreate.model_validate(body)
    schemas.validate_candidate_bead(
        body["namespace"],
        body["type"],
        body["content"],
        body["provenance"],
        body["created_by"],
        trust_tier=body["trust_tier"],
        state=body["state"],
    )
    assert set(body["provenance"]) == _SIX_KEYS


@pytest.fixture(autouse=True)
def _substrate_env(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")


async def test_subscription_auditor_persist_body_validates(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json={"id": "sub-1"})

    bead_id = await subscription_auditor.persist_subscription_activity(
        {
            "cluster": {
                "merchant": "NETFLIX",
                "display_name": "Netflix",
                "first_seen": "2026-03-15",
                "last_seen": "2026-05-15",
                "occurrences": 3,
                "evidence_tx_ids": ["tx-1", "tx-2"],
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
            "prompt_hash": "abc123",
            "latency_ms": 50.0,
            "analysis_source": "llm",
        }
    )
    assert bead_id == "sub-1"

    post_request = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    _assert_body_is_accepted(json.loads(post_request.content))


async def test_subscription_auditor_persist_body_validates_deterministic_fallback(httpx_mock):
    """analysis_source == 'deterministic' -> provenance.model == 'none', the
    one path where BeadProvenance's nullable tokens/cost_usd default to 0/0.0
    instead of None."""
    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json={"id": "sub-2"})

    await subscription_auditor.persist_subscription_activity(
        {
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
                "confidence": 0.65,
                "notes": "LLM unavailable; deterministic fallback used.",
            },
            "model": "gemini-pro",
            "analysis_source": "deterministic",
        }
    )

    post_request = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(post_request.content)
    assert body["provenance"]["model"] == "none"
    _assert_body_is_accepted(body)


async def test_financial_insights_persist_body_validates(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "insight-1"})

    summary = {
        "window": {"since": "2026-05-15", "until": "2026-05-22"},
        "by_category": {},
        "totals": {"spent": 0, "budget": 0, "remaining": 0},
        "transaction_ids": ["tx-1"],
    }
    await financial_insights.persist_insight_activity("Narrative.", summary)

    body = json.loads(httpx_mock.get_requests()[0].content)
    _assert_body_is_accepted(body)


async def test_budget_pulse_persist_body_validates(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "alert-1"})

    report = {
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
    }
    await budget_pulse.persist_budget_alerts_activity(report)

    body = json.loads(httpx_mock.get_requests()[0].content)
    _assert_body_is_accepted(body)


async def test_log_llm_cost_body_validates(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "cost-1"})

    bead_id = await cost.log_llm_cost(
        model="gemini-flash",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        agent="test/agent",
        workflow_id="wf-1",
        latency_ms=42.0,
        prompt_hash_value="hash123",
    )
    assert bead_id == "cost-1"

    body = json.loads(httpx_mock.get_requests()[0].content)
    _assert_body_is_accepted(body)


async def test_log_llm_cost_body_validates_with_no_usage(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "cost-2"})

    await cost.log_llm_cost(model="gemini-flash", usage=None, agent="test/agent")

    body = json.loads(httpx_mock.get_requests()[0].content)
    _assert_body_is_accepted(body)


async def test_vision_stage_extraction_body_validates(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "vision-1"})

    extraction = vision.VisionExtraction(todos=[], events=[], expenses=[], summary="note")
    bead_id = await vision.stage_vision_extraction(extraction, source="vision/extract")
    assert bead_id == "vision-1"

    body = json.loads(httpx_mock.get_requests()[0].content)
    _assert_body_is_accepted(body)


# --- negative control: proof the check can fail -----------------------------


def test_negative_control_legacy_provenance_shapes_are_all_refused():
    """MEASURED on 8040e219: all five legacy shapes raise ValidationError
    against the live schema; the six-key record does not."""
    legacy_shapes = [
        {
            "chain": [{"agent": "x"}],
            "parent_ids": ["tx-1"],
            "generator": "subscription-auditor/persist",
            "model": "gemini-pro",
        },
        {"parent_ids": ["tx-1"], "generator": "financial-insights/synthesize", "model": "gemini-pro"},
        {"generator": "budget-pulse/persist"},
        {"chain": [{"agent": "a", "model": "m"}], "parent_workflow_id": "wf-1"},
        {"source": "vision/extract", "model": "gemini-flash"},
    ]
    for shape in legacy_shapes:
        with pytest.raises(pydantic.ValidationError):
            schemas.validate_candidate_bead(
                "finance",
                "subscription",
                {"name": "Netflix"},
                shape,
                "subscription-auditor-workflow",
                trust_tier="system",
                state="active",
            )
