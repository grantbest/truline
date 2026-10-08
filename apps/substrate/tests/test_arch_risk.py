"""``arch.risk`` — the risk register (R26.09/O-5).

Content model precedent is ``ArchPrincipleContent``: a risk is a standing
governance record with its own status ladder, so this is deliberately not an
``ArchContentBase`` subclass. ``severity`` mirrors ``ArchApplicationContent``'s
``business_criticality`` vocabulary rather than ``ArchIncidentContent``'s
alerting-severity one — the two measure different things. See
``.factory/design.md``.
"""

import pytest
from pydantic import ValidationError

from src.schemas import ARCH_TYPE_SCHEMAS, ArchRiskContent, validate_arch_content


def valid_risk_content(**over) -> dict:
    content = {
        "statement": "The control plane runs on a single node with no redundancy.",
        "owner": "the Operator",
        "accepted_at": "2026-05-30",
        "review_by": "2026-11-30",
        "severity": "high",
        "decision_ref": "ARCHITECTURE.md#amendment-18",
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_risk_type_is_registered_in_arch_type_schemas():
    assert ARCH_TYPE_SCHEMAS["risk"] is ArchRiskContent


def test_valid_risk_content_is_accepted():
    validate_arch_content("risk", valid_risk_content())


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_field",
    ["statement", "owner", "accepted_at", "review_by", "severity", "decision_ref"],
)
def test_risk_content_rejects_missing_required_field(required_field):
    content = valid_risk_content()
    del content[required_field]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("risk", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


@pytest.mark.parametrize("blank_field", ["statement", "owner", "decision_ref"])
def test_risk_content_rejects_blank_required_strings(blank_field):
    content = valid_risk_content(**{blank_field: "   "})

    with pytest.raises(ValidationError):
        validate_arch_content("risk", content)


# ---------------------------------------------------------------------------
# severity — reuses business_criticality's vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_risk_content_accepts_each_declared_severity(severity):
    validate_arch_content("risk", valid_risk_content(severity=severity))


def test_risk_content_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        validate_arch_content("risk", valid_risk_content(severity="urgent"))


# ---------------------------------------------------------------------------
# review_by cannot precede accepted_at
# ---------------------------------------------------------------------------


def test_risk_content_rejects_review_by_before_accepted_at():
    content = valid_risk_content(accepted_at="2026-05-30", review_by="2026-01-01")

    with pytest.raises(ValidationError):
        validate_arch_content("risk", content)


def test_risk_content_accepts_review_by_equal_to_accepted_at():
    content = valid_risk_content(accepted_at="2026-05-30", review_by="2026-05-30")
    validate_arch_content("risk", content)


# ---------------------------------------------------------------------------
# source_class — not locked (authored default)
# ---------------------------------------------------------------------------


def test_risk_content_defaults_source_class_to_authored():
    model = ArchRiskContent.model_validate(valid_risk_content())
    assert model.source_class == "authored"


@pytest.mark.parametrize("source_class", ["authored", "derived", "observed"])
def test_risk_content_accepts_any_declared_source_class(source_class):
    validate_arch_content("risk", valid_risk_content(source_class=source_class))


# ---------------------------------------------------------------------------
# Pillar 10 — no execution-state bookkeeping in risk content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_field", ["attempt_count", "lease_holder", "retry_count"]
)
def test_risk_content_model_declares_no_execution_state_fields(forbidden_field):
    assert forbidden_field not in ArchRiskContent.model_fields
