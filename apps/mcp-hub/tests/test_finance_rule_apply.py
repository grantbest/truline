"""Unit tests for the SDD Phase 4 rule retroaction (pure matching logic)."""

import re

import pytest

from workflows import finance_rule_apply
from workflows.finance_rule_apply import (
    apply_rule_activity,
    build_matcher,
    is_uncategorized,
    resolve_field,
    transaction_matches,
)


def test_resolve_field_aliases_vendor():
    # Spec's canonical rule uses "vendor"; transactions carry merchant fields.
    assert resolve_field("vendor") == "normalized_merchant"
    assert resolve_field("merchant_name") == "merchant_name"


def test_build_matcher_operators():
    assert build_matcher("contains", "ama")("amazon.com")
    assert not build_matcher("contains", "xyz")("amazon.com")
    assert build_matcher("equals", "amazon")("amazon")
    assert not build_matcher("equals", "amazon")("amazon.com")
    assert build_matcher("starts_with", "amaz")("amazon")
    assert build_matcher("regex", r"^amazon")("amazon fresh")


def test_build_matcher_rejects_unknown_operator():
    with pytest.raises(ValueError):
        build_matcher("between", "x")


def test_build_matcher_bad_regex_raises():
    with pytest.raises(re.error):
        build_matcher("regex", "(unclosed")


def test_transaction_matches_uses_resolved_field_case_insensitive():
    content = {"normalized_merchant": "Amazon.com"}
    matcher = build_matcher("contains", "amazon")
    assert transaction_matches(content, resolve_field("vendor"), matcher)
    # Missing field never matches.
    assert not transaction_matches({}, "normalized_merchant", matcher)


def test_is_uncategorized():
    assert is_uncategorized({})
    assert is_uncategorized({"our_category": ""})
    assert is_uncategorized({"our_category": "Uncategorized"})
    assert not is_uncategorized({"our_category": "Groceries"})


@pytest.mark.asyncio
async def test_apply_rule_activity_records_measured_rule_confidence(monkeypatch):
    patched = []

    async def fake_query_beads(params):
        assert params["type"] == "transaction"
        return [
            {
                "id": "tx-1",
                "content": {
                    "merchant_name": "Starbucks",
                    "normalized_merchant": "starbucks",
                    "our_category": "Uncategorized",
                },
            }
        ]

    async def fake_patch_bead(bead_id, *, state=None, content=None, created_by="x"):
        patched.append({"id": bead_id, "state": state, "content": content, "created_by": created_by})
        return {"id": bead_id}

    monkeypatch.setattr(finance_rule_apply, "query_beads", fake_query_beads)
    monkeypatch.setattr(finance_rule_apply, "patch_bead", fake_patch_bead)
    monkeypatch.setattr(finance_rule_apply.activity, "heartbeat", lambda _patched: None)

    result = await apply_rule_activity({
        "rule_bead_id": "rule-1",
        "rule": {
            "field": "merchant_name",
            "operator": "contains",
            "value": "starbucks",
            "target_category": "dining",
        },
    })

    tx_patch = next(p for p in patched if p["id"] == "tx-1")
    assert result["patched"] == 1
    assert tx_patch["content"]["our_category"] == "dining"
    assert tx_patch["content"]["categorization_source"] == "rule"
    assert tx_patch["content"]["categorization_rule_id"] == "rule-1"
    assert tx_patch["content"]["categorization_confidence"] == 1.0
