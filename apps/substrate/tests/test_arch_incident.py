"""``arch.incident`` — the largest hole named in bead-object-inventory.md.

Content model precedent is ``ArchChangeContent`` (itsm-target-state.md §1): an
incident is an *event against* a CI, not a CI, so this is deliberately not an
``ArchContentBase`` subclass. ``severity`` mirrors ``ALERT_INVENTORY``'s closed
set (``apps/mcp-hub/src/tools/notify.py``'s ``AlertSeverity`` —
urgent/actionable/informational) as a hardcoded literal, since this tree has
no runtime dependency on mcp-hub. See ``.factory/design.md``.
"""


import pytest
from pydantic import ValidationError

from src.schemas import ARCH_TYPE_SCHEMAS, ArchIncidentContent, validate_arch_content


def valid_incident_content(**over) -> dict:
    content = {
        "summary": "Bank-sync thundering herd overwhelmed the connections pool",
        "severity": "urgent",
        "detected_at": "2026-07-18T14:03:00Z",
        "source": "alert:bank-sync-thundering-herd",
        "applications": ["lifeops-console"],
        "runbook_refs": ["docs/runbooks/bank-sync-thundering-herd.md"],
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_incident_type_is_registered_in_arch_type_schemas():
    assert ARCH_TYPE_SCHEMAS["incident"] is ArchIncidentContent


def test_valid_incident_content_is_accepted():
    validate_arch_content("incident", valid_incident_content())


def test_valid_resolved_incident_content_is_accepted():
    validate_arch_content(
        "incident",
        valid_incident_content(
            resolved_at="2026-07-18T15:30:00Z",
            resolution="Added per-institution stagger to the sync schedule.",
        ),
    )


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_field", ["summary", "severity", "detected_at", "source", "applications"]
)
def test_incident_content_rejects_missing_required_field(required_field):
    content = valid_incident_content()
    del content[required_field]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("incident", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


@pytest.mark.parametrize("blank_field", ["summary", "source"])
def test_incident_content_rejects_blank_required_strings(blank_field):
    content = valid_incident_content(**{blank_field: "   "})

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


def test_incident_content_rejects_empty_applications():
    content = valid_incident_content(applications=[])

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


# ---------------------------------------------------------------------------
# severity — the ALERT_INVENTORY closed set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["urgent", "actionable", "informational"])
def test_incident_content_accepts_each_declared_severity(severity):
    validate_arch_content("incident", valid_incident_content(severity=severity))


def test_incident_content_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        validate_arch_content("incident", valid_incident_content(severity="critical"))


# ---------------------------------------------------------------------------
# resolution / resolved_at — absent until resolved
# ---------------------------------------------------------------------------


def test_incident_content_rejects_blank_resolution_when_present():
    content = valid_incident_content(resolution="   ")

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


def test_incident_content_rejects_resolved_at_before_detected_at():
    content = valid_incident_content(
        detected_at="2026-07-18T14:03:00Z",
        resolved_at="2026-07-18T10:00:00Z",
    )

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


def test_incident_content_accepts_resolved_at_equal_to_detected_at():
    """The spurious/duplicate path: detected and closed at the same instant."""
    content = valid_incident_content(
        detected_at="2026-07-18T14:03:00Z",
        resolved_at="2026-07-18T14:03:00Z",
    )
    validate_arch_content("incident", content)


# ---------------------------------------------------------------------------
# runbook_refs — non-blank paths under docs/runbooks/
# ---------------------------------------------------------------------------


def test_incident_content_accepts_empty_runbook_refs():
    content = valid_incident_content(runbook_refs=[])
    validate_arch_content("incident", content)


def test_incident_content_rejects_blank_runbook_ref():
    content = valid_incident_content(runbook_refs=["   "])

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


def test_incident_content_rejects_runbook_ref_outside_docs_runbooks():
    content = valid_incident_content(runbook_refs=["runbooks/bank-sync.md"])

    with pytest.raises(ValidationError):
        validate_arch_content("incident", content)


def test_incident_content_accepts_multiple_runbook_refs():
    content = valid_incident_content(
        runbook_refs=[
            "docs/runbooks/bank-sync-thundering-herd.md",
            "docs/runbooks/connections-pool-exhaustion.md",
        ]
    )
    validate_arch_content("incident", content)


# ---------------------------------------------------------------------------
# source_class — not locked (authored default), unlike arch.ci
# ---------------------------------------------------------------------------


def test_incident_content_defaults_source_class_to_authored():
    model = ArchIncidentContent.model_validate(valid_incident_content())
    assert model.source_class == "authored"


@pytest.mark.parametrize("source_class", ["authored", "derived", "observed"])
def test_incident_content_accepts_any_declared_source_class(source_class):
    validate_arch_content(
        "incident", valid_incident_content(source_class=source_class)
    )


# ---------------------------------------------------------------------------
# Pillar 10 — no execution-state bookkeeping in incident content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_field", ["attempt_count", "lease_holder", "retry_count"]
)
def test_incident_content_model_declares_no_execution_state_fields(forbidden_field):
    assert forbidden_field not in ArchIncidentContent.model_fields
