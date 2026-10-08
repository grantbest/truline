"""``arch.release_health`` — a release's governed health posture (R26.12/O-5,
dev.finding 934989e6 F-B).

Content model precedent is ``ArchObservationContent``: this type is closed
(``extra="forbid"``), never ``ArchIncidentContent``/``ArchRiskContent``'s
``extra="allow"`` — it is a reconciler's derivation (B12/B13), not an
envelope over authored prose. The eleven signal ids are spelled out as
string literals here, never imported from the model, so this test fails if
the model's closed set drifts independently of this spec. See
``.factory/design.md``.
"""

import pytest
from pydantic import ValidationError

from src.schemas import ARCH_TYPE_SCHEMAS, ArchReleaseHealthContent, validate_arch_content


_SIGNAL_IDS = (
    "class_absent",
    "class_over_share",
    "outcome_no_landed_work",
    "outcome_unmeasured",
    "criterion_stale",
    "nothing_delivering",
    "unbound_closed_in_window",
    "queue_starvation",
    "time_unmeasurable",
    "rework",
    "merged_without_verdict",
)


def valid_release_health_content(**over) -> dict:
    content = {
        "ref": "health.R26.12",
        "release_ref": "R26.12",
        "measured_at": "2026-10-04T00:00:00Z",
        "measured_revision": "4dbd4195",
        "policy_revision": "abc123sha",
        "time_box": {
            "opened_at": "2026-09-01",
            "target_at": "2026-11-01",
            "elapsed_pct": 0.2,
        },
        "urgency": "low",
        "signals": [
            {
                "id": signal_id,
                "fired": False,
                "pending": False,
                "impact": "low",
                "evidence": "no evidence yet",
                "since": None,
            }
            for signal_id in _SIGNAL_IDS
        ],
        "balance": [
            {
                "work_class": "feature",
                "declared_pct": 40,
                "actual_count": 4,
                "actual_pct": 40.0,
                "absent": False,
                "insufficient_sample": False,
            }
        ],
        "outcomes": [
            {
                "id": "O-1",
                "work_class": "feature",
                "delivery": "in_progress",
                "tasks_by_state": {"doing": 1},
                "unmeasured_candidates": [],
            }
        ],
        "criteria": [
            {"ref": "R26.12/AC-1", "stale": False, "changed": False, "unmeasurable": False}
        ],
        "changes": {"count": 2, "by_change_type": {"standard": 2}, "without_verdict_record": 0},
        "rework": {
            "bound": 10,
            "request_changes_tasks": 2,
            "requeued_tasks": 1,
            "union": 3,
            "superseded": 0,
            "rate": 30.0,
            "regresses_edges_read": 0,
            "coverage_note": "rework measured over bound tasks",
        },
        "close_readiness": {"ready": "unknown", "blockers": ["O-1 not yet delivered"]},
        "queue": {
            "oldest_claimable_age_days_by_class": {"feature": 1.5},
            "read_from": "queue-order.json",
            "computed_at": "2026-10-04T00:00:00Z",
        },
        "coverage": {
            "complete": True,
            "unreadable": [],
            "notes": [],
            "tasks_read": True,
            "delivers_read": True,
            "conformances_read": True,
            "time_box_read": True,
            "policy_read": True,
            "changes_read": True,
            "queue_order_file_read": True,
        },
        "disposition": None,
        "source_class": "derived",
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_release_health_type_is_registered_in_arch_type_schemas():
    assert ARCH_TYPE_SCHEMAS["release_health"] is ArchReleaseHealthContent


def test_validate_arch_content_uses_the_registered_model():
    validate_arch_content("release_health", valid_release_health_content())


def test_valid_release_health_content_is_accepted():
    ArchReleaseHealthContent.model_validate(valid_release_health_content())


def test_valid_content_carries_all_eleven_signal_ids():
    content = valid_release_health_content()
    ids = {signal["id"] for signal in content["signals"]}
    assert ids == {
        "class_absent",
        "class_over_share",
        "outcome_no_landed_work",
        "outcome_unmeasured",
        "criterion_stale",
        "nothing_delivering",
        "unbound_closed_in_window",
        "queue_starvation",
        "time_unmeasurable",
        "rework",
        "merged_without_verdict",
    }
    assert len(ids) == 11
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# extra="forbid" — closed model
# ---------------------------------------------------------------------------


def test_release_health_content_is_closed_to_extra_model_allow():
    assert ArchReleaseHealthContent.model_config.get("extra") == "forbid"


def test_release_health_content_rejects_unknown_key():
    content = valid_release_health_content(actual_balance={"feature": 100})
    with pytest.raises(ValidationError):
        validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# ref must start with "health."
# ---------------------------------------------------------------------------


def test_release_health_content_rejects_ref_without_health_prefix():
    content = valid_release_health_content(ref="R26.12")
    with pytest.raises(ValidationError, match="ref"):
        validate_arch_content("release_health", content)


def test_release_health_content_accepts_a_well_formed_health_ref():
    content = valid_release_health_content(ref="health.R26.12")
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# signals: closed id set, no duplicates
# ---------------------------------------------------------------------------


def test_release_health_content_rejects_signal_id_outside_the_closed_set():
    content = valid_release_health_content()
    content["signals"][0]["id"] = "something_else"
    with pytest.raises(ValidationError, match="id"):
        validate_arch_content("release_health", content)


def test_release_health_content_rejects_duplicate_signal_id():
    content = valid_release_health_content()
    duplicate = dict(content["signals"][0])
    content["signals"].append(duplicate)
    with pytest.raises(ValidationError, match="duplicate signal id"):
        validate_arch_content("release_health", content)


@pytest.mark.parametrize("fired", [True, False, "unknown"])
def test_release_health_content_accepts_each_fired_value(fired):
    content = valid_release_health_content()
    content["signals"][0]["fired"] = fired
    validate_arch_content("release_health", content)


def test_release_health_content_rejects_unknown_fired_value():
    content = valid_release_health_content()
    content["signals"][0]["fired"] = "maybe"
    with pytest.raises(ValidationError):
        validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# balance: actual_pct must be null when insufficient_sample is True
# ---------------------------------------------------------------------------


def test_release_health_content_rejects_actual_pct_with_insufficient_sample():
    content = valid_release_health_content()
    content["balance"][0]["insufficient_sample"] = True
    content["balance"][0]["actual_pct"] = 40.0
    with pytest.raises(ValidationError, match="insufficient_sample"):
        validate_arch_content("release_health", content)


def test_release_health_content_accepts_null_actual_pct_with_insufficient_sample():
    content = valid_release_health_content()
    content["balance"][0]["insufficient_sample"] = True
    content["balance"][0]["actual_pct"] = None
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# coverage: complete=True requires an empty unreadable list
# ---------------------------------------------------------------------------


def test_release_health_content_rejects_complete_coverage_with_unreadable_inputs():
    content = valid_release_health_content()
    content["coverage"]["complete"] = True
    content["coverage"]["unreadable"] = ["policy"]
    with pytest.raises(ValidationError, match="unreadable"):
        validate_arch_content("release_health", content)


def test_release_health_content_accepts_incomplete_coverage_with_unreadable_inputs():
    content = valid_release_health_content()
    content["coverage"]["complete"] = False
    content["coverage"]["unreadable"] = ["policy"]
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# Required top-level fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_field",
    [
        "ref",
        "release_ref",
        "measured_at",
        "measured_revision",
        "policy_revision",
        "time_box",
        "urgency",
        "changes",
        "rework",
        "close_readiness",
        "queue",
        "coverage",
        "source_class",
    ],
)
def test_release_health_content_rejects_missing_required_field(required_field):
    content = valid_release_health_content()
    del content[required_field]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("release_health", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


# ---------------------------------------------------------------------------
# source_class — locked to "derived"
# ---------------------------------------------------------------------------


def test_release_health_content_requires_source_class_derived():
    content = valid_release_health_content(source_class="observed")
    with pytest.raises(ValidationError):
        validate_arch_content("release_health", content)


def test_release_health_content_accepts_source_class_derived():
    content = valid_release_health_content(source_class="derived")
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# disposition — optional
# ---------------------------------------------------------------------------


def test_release_health_content_accepts_null_disposition():
    content = valid_release_health_content(disposition=None)
    validate_arch_content("release_health", content)


def test_release_health_content_accepts_a_disposition():
    content = valid_release_health_content(
        disposition={
            "kind": "accepted",
            "until": "2026-12-01",
            "note_id": "note-123",
            "recorded_by": "the Operator",
            "fired_ids": ["class_absent"],
        }
    )
    validate_arch_content("release_health", content)


def test_release_health_content_rejects_unknown_disposition_kind():
    content = valid_release_health_content(
        disposition={
            "kind": "waived",
            "until": "2026-12-01",
            "note_id": "note-123",
            "recorded_by": "the Operator",
            "fired_ids": [],
        }
    )
    with pytest.raises(ValidationError):
        validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# Degraded but valid content — the nullable/"unknown" paths AC-1 makes legal
# ---------------------------------------------------------------------------


def _degraded_release_health_content(*, time_box: dict) -> dict:
    return valid_release_health_content(
        time_box=time_box,
        urgency="unknown",
        changes={"count": None, "by_change_type": {}, "without_verdict_record": None},
        rework={
            "bound": None,
            "request_changes_tasks": None,
            "requeued_tasks": None,
            "union": None,
            "superseded": None,
            "rate": None,
            "regresses_edges_read": 0,
            "coverage_note": "rework unmeasurable: tasks unreadable",
        },
        close_readiness={"ready": "unknown", "blockers": []},
        queue={
            "oldest_claimable_age_days_by_class": "unknown",
            "read_from": "queue-order.json",
            "computed_at": None,
        },
        coverage={
            "complete": False,
            "unreadable": ["tasks", "changes", "queue_order_file", "time_box"],
            "notes": [],
            "tasks_read": False,
            "delivers_read": False,
            "conformances_read": False,
            "time_box_read": False,
            "policy_read": True,
            "changes_read": False,
            "queue_order_file_read": False,
        },
    )


@pytest.mark.parametrize(
    "time_box",
    [
        pytest.param(
            {"opened_at": None, "target_at": None, "elapsed_pct": None}, id="explicit-nulls"
        ),
        pytest.param({}, id="omitted-keys"),
    ],
)
def test_degraded_release_health_content_validates(time_box):
    """AC-1's nullable/"unknown" paths together on one content instance: a
    time_box with no dates (either spelled out as null or omitted entirely,
    both legal per ``ArchReleaseHealthTimeBox``'s own defaults), an unknown
    urgency and queue reading, null changes/rework counts, and an unknown
    close_readiness over an incomplete coverage ledger. This is the shape B12
    emits when the inputs it needs are unreadable, not a report of zero
    activity.
    """
    content = _degraded_release_health_content(time_box=time_box)
    validate_arch_content("release_health", content)


# ---------------------------------------------------------------------------
# Pillar 10 — no execution-state bookkeeping in release_health content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_field", ["attempt_count", "lease_holder", "retry_count"]
)
def test_release_health_content_model_declares_no_execution_state_fields(forbidden_field):
    assert forbidden_field not in ArchReleaseHealthContent.model_fields
