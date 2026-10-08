from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pydantic
import pytest

from src.schemas import DevTaskContent, validate_dev_content


def valid_dev_task_content() -> dict:
    return {
        "lane": "code-health",
        "title": "Add substrate dev task schema",
        "intent": "Define a typed work unit for autonomous development handoffs.",
        "context_refs": ["docs/reference/substrate-api.md"],
        "acceptance": [
            "WHEN a valid dev.task is written, THEN validation SHALL accept it."
        ],
        "verification": {
            "commands": ["cd apps/substrate && .venv/bin/python -m pytest tests/ -q"]
        },
        "scope": {
            "paths": ["apps/substrate/src/schemas.py", "apps/substrate/tests/"],
            "forbidden_paths": [
                ".github/workflows/**",
                "apps/mcp-hub/**",
                "infrastructure/**",
                "docs/plans/**",
            ],
        },
        "risk_class": "structural",
        "budget": {
            "max_agent_minutes": 20,
            "max_usd": 2.0,
            "max_tokens": 20000,
        },
    }


@pytest.mark.parametrize(
    "required_field",
    ["lane", "acceptance", "verification", "scope", "budget", "risk_class"],
)
def test_dev_task_schema_rejects_missing_required_fields(required_field):
    content = valid_dev_task_content()
    del content[required_field]

    with pytest.raises(Exception) as exc_info:
        validate_dev_content("task", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


def test_dev_task_schema_rejects_empty_acceptance():
    content = valid_dev_task_content()
    content["acceptance"] = []

    with pytest.raises(Exception) as exc_info:
        validate_dev_content("task", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("acceptance",) in bad


def test_dev_task_schema_rejects_scope_that_can_write_github_workflows():
    content = valid_dev_task_content()
    content["scope"]["forbidden_paths"] = ["apps/mcp-hub/**"]

    with pytest.raises(Exception) as exc_info:
        validate_dev_content("task", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("scope", "forbidden_paths") in bad


def test_dev_task_schema_defaults_autonomy_to_propose():
    task = DevTaskContent.model_validate(valid_dev_task_content())

    assert task.autonomy == "propose"


def test_dev_task_schema_accepts_valid_content():
    validate_dev_content("task", valid_dev_task_content())


@pytest.mark.parametrize("lane", ["code-health", "drift", "bug-triage", "feature"])
def test_dev_task_schema_accepts_every_known_lane(lane):
    content = valid_dev_task_content()
    content["lane"] = lane

    task = DevTaskContent.model_validate(content)

    assert task.lane == lane


def test_dev_task_schema_rejects_lane_outside_the_known_vocabulary():
    content = valid_dev_task_content()
    content["lane"] = "product"

    with pytest.raises(Exception) as exc_info:
        validate_dev_content("task", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("lane",) in bad


def test_dev_task_schema_does_not_declare_attempt_fields():
    assert "attempts" not in DevTaskContent.model_fields
    assert "max_attempts" not in DevTaskContent.model_fields


def test_dev_task_without_attempt_fields_validates():
    task = DevTaskContent.model_validate(valid_dev_task_content())
    assert task.title == "Add substrate dev task schema"


def test_historical_dev_task_with_attempt_fields_still_validates():
    content = valid_dev_task_content()
    content["attempts"] = 2
    content["max_attempts"] = 3

    task = DevTaskContent.model_validate(content)

    assert task.model_extra["attempts"] == 2
    assert task.model_extra["max_attempts"] == 3

def test_dev_task_schema_accepts_expected_pristine_failure():
    content = valid_dev_task_content()
    content["verification"]["expect_pristine_failure"] = True

    task = DevTaskContent.model_validate(content)

    assert task.verification.expect_pristine_failure is True


def test_dev_task_post_returns_422_before_db(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DISABLE_VECTOR_LISTENER", "true")

    from src.database import get_db
    from src.main import app

    async def unused_db():
        yield None

    content = valid_dev_task_content()
    content["scope"]["forbidden_paths"] = ["apps/mcp-hub/**"]

    app.dependency_overrides[get_db] = unused_db
    client = TestClient(app)
    try:
        response = client.post(
            "/beads",
            headers={"x-api-key": "test-substrate-key"},
            json={
                "namespace": "dev",
                "type": "task",
                "state": "open",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "dev_content_schema_violation"
    bad = {tuple(error["loc"]) for error in detail["errors"]}
    assert ("scope", "forbidden_paths") in bad


# --- traceability contract (2026-08-02) -------------------------------------


def test_task_accepts_requirement_nfrs_and_arch_impact():
    """The three fields four personas were specified to use and never had."""
    content = valid_dev_task_content()
    content["requirement_refs"] = ["LO-CAT-004", "LO-CAT-004/AC-1"]
    content["nfrs"] = [{
        "category": "latency",
        "statement": "the ledger route stays responsive under a full month of rows",
        "threshold": "p99 < 200ms",
        "verification": "cd apps/substrate && .venv/bin/python -m pytest tests/ -q -k latency",
    }]
    content["arch_impact"] = {"applications": ["substrate"], "capabilities": ["CAP-CON"]}
    content["pr_refs"] = ["#254"]
    task = DevTaskContent(**content)
    assert task.nfrs[0].threshold == "p99 < 200ms"
    assert task.arch_impact.applications == ["substrate"]


def test_task_without_the_new_fields_still_validates():
    """Optional by decision: the rollout warns for a sprint before it refuses,
    so every bead written before this change must keep validating."""
    task = DevTaskContent(**valid_dev_task_content())
    assert task.requirement_refs == []
    assert task.nfrs == []
    assert task.arch_impact is None


@pytest.mark.parametrize("bad_ref", ["LO-CAT-4", "whatever", "LO-CAT-004/AC", "lo-cat-004"])
def test_unresolvable_requirement_refs_are_rejected(bad_ref):
    """A reference pointing nowhere reads as traceability while providing none."""
    content = valid_dev_task_content()
    content["requirement_refs"] = [bad_ref]
    with pytest.raises(Exception):
        DevTaskContent(**content)


def test_merge_verdict_cannot_rest_on_a_suite_that_never_ran():
    """The false green this platform keeps having to fix, caught in the schema."""
    from src.schemas import DevReleaseContent

    with pytest.raises(Exception):
        DevReleaseContent(
            reviewer="gemini/release-gate",
            verdict="merge",
            pr_refs=["#254"],
            required_suites=["apps/mcp-hub/tests"],
            results=[{
                "category": "regression",
                "suite": "apps/mcp-hub/tests",
                "outcome": "not-run",
                "evidence": "never executed",
            }],
        )


def test_merge_verdict_passes_when_the_required_suite_actually_passed():
    from src.schemas import DevReleaseContent

    release = DevReleaseContent(
        reviewer="gemini/release-gate",
        verdict="merge",
        pr_refs=["#254"],
        required_suites=["apps/mcp-hub/tests"],
        results=[{
            "category": "regression",
            "suite": "apps/mcp-hub/tests",
            "outcome": "pass",
            "evidence": "321 passed, 6 skipped",
        }],
    )
    assert release.verdict == "merge"


def test_a_blocking_finding_must_carry_evidence():
    """Two audit rounds produced confident wrong findings that would have
    blocked a merge. Evidence does not make a finding right; it makes it
    checkable."""
    from src.schemas import DevFindingContent

    with pytest.raises(Exception):
        DevFindingContent(
            kind="bug", disposition="blocking", severity="high",
            summary="PR #246 reverts the ruff pin",
        )
    backlog = DevFindingContent(
        kind="enhancement", disposition="backlog", severity="low",
        summary="portfolio has no source-path mapping",
    )
    assert backlog.evidence is None


def test_arch_change_is_not_forced_into_the_configuration_item_contract():
    """A change is an event against a CI, not a CI. It needs no layer."""
    from src.schemas import ArchChangeContent

    change = ArchChangeContent(
        change_type="normal",
        summary="dev.task gains traceability fields",
        applications=["substrate"],
        evidence=["PR #256"],
    )
    assert not hasattr(change, "layer")


# --- release traceability ----------------------------------------------------
#
# WHICH release a task delivers is the `delivers` edge and lives nowhere else.
# What a task carries is the part no edge can express: which outcome within
# that release, and — when no release applies — the written reason.


def test_outcome_ref_accepts_a_bare_outcome_id():
    content = valid_dev_task_content()
    content["outcome_ref"] = "O-2"

    assert DevTaskContent(**content).outcome_ref == "O-2"


def test_outcome_ref_rejects_a_qualified_reference():
    """`R26.01/O-2` here would be a second place the release id is written.

    The two could then disagree about which release a task is in — with the
    string being the one a human reads and the edge being the one every query
    uses.
    """
    content = valid_dev_task_content()
    content["outcome_ref"] = "R26.01/O-2"

    with pytest.raises(Exception) as exc_info:
        DevTaskContent(**content)
    assert "delivers edge" in str(exc_info.value)


@pytest.mark.parametrize("value", ["O2", "outcome-2", "O-", "2"])
def test_outcome_ref_rejects_malformed_ids(value):
    content = valid_dev_task_content()
    content["outcome_ref"] = value

    with pytest.raises(Exception):
        DevTaskContent(**content)


def test_release_fields_are_optional():
    """Both default to absent, for the reason requirement_refs does.

    The refusal belongs at filing, which is the boundary between what we do
    from now on and what we already did. Enforcing it in the schema would make
    every historical bead unreadable, and their content records what was true
    when they ran.
    """
    task = DevTaskContent(**valid_dev_task_content())

    assert task.outcome_ref is None
    assert task.release_ref_waived is None


def test_a_waiver_is_recorded_on_the_bead():
    """A waiver is a visible choice, inspectable after the fact — not silence."""
    content = valid_dev_task_content()
    content["release_ref_waived"] = "auto-filed by scanner; awaiting PO triage"

    task = DevTaskContent(**content)

    assert task.release_ref_waived == "auto-filed by scanner; awaiting PO triage"


# --- emergency marker (R26.12/B15) -------------------------------------------
#
# Intent recorded on the bead, not execution state: this bead adds no writer
# and no reader. B17 sets these fields, B16 refuses an unattested one at
# intake, B7/B8 read them. All three travel together or not at all.


def test_emergency_marker_fields_are_optional():
    task = DevTaskContent(**valid_dev_task_content())

    assert task.class_of_service is None
    assert task.expedite_reason is None
    assert task.expedite_until is None


def test_emergency_marker_accepts_all_three_fields_together():
    content = valid_dev_task_content()
    content["class_of_service"] = "emergency"
    content["expedite_reason"] = "prod outage LO-INC-42"
    content["expedite_until"] = "2026-10-05T12:00:00+00:00"

    task = DevTaskContent(**content)

    assert task.class_of_service == "emergency"
    assert task.expedite_reason == "prod outage LO-INC-42"
    assert task.expedite_until.isoformat() == "2026-10-05T12:00:00+00:00"


@pytest.mark.parametrize(
    "partial, expected_field",
    [
        ({"class_of_service": "emergency"}, "expedite_reason"),
        ({"class_of_service": "emergency", "expedite_reason": "prod outage"}, "expedite_until"),
        (
            {"class_of_service": "emergency", "expedite_until": "2026-10-05T12:00:00+00:00"},
            "expedite_reason",
        ),
        ({"class_of_service": "emergency", "expedite_reason": "   "}, "expedite_reason"),
        ({"expedite_reason": "prod outage"}, "expedite_reason"),
        ({"expedite_until": "2026-10-05T12:00:00+00:00"}, "expedite_until"),
        (
            {
                "class_of_service": "emergency",
                "expedite_reason": "   ",
                "expedite_until": "2026-10-05T12:00:00+00:00",
            },
            "expedite_reason",
        ),
    ],
)
def test_emergency_marker_fields_are_refused_unless_all_three_travel_together(
    partial, expected_field
):
    content = valid_dev_task_content()
    content.update(partial)

    with pytest.raises(pydantic.ValidationError) as exc_info:
        DevTaskContent(**content)

    messages = " ".join(error["msg"] for error in exc_info.value.errors())
    assert expected_field in messages


def test_emergency_marker_rejects_normal_as_class_of_service():
    """The only admitted value is 'emergency'; absence means normal."""
    content = valid_dev_task_content()
    content["class_of_service"] = "normal"
    content["expedite_reason"] = "prod outage"
    content["expedite_until"] = "2026-10-05T12:00:00+00:00"

    with pytest.raises(Exception) as exc_info:
        DevTaskContent(**content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("class_of_service",) in bad


def test_emergency_marker_rejects_a_naive_expedite_until():
    content = valid_dev_task_content()
    content["class_of_service"] = "emergency"
    content["expedite_reason"] = "prod outage"
    content["expedite_until"] = "2026-10-05T12:00:00"

    with pytest.raises(Exception) as exc_info:
        DevTaskContent(**content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("expedite_until",) in bad


def test_emergency_marker_rejects_an_int_expedite_until():
    """Pydantic lax mode would otherwise coerce epoch seconds into a datetime."""
    content = valid_dev_task_content()
    content["class_of_service"] = "emergency"
    content["expedite_reason"] = "prod outage"
    content["expedite_until"] = 1791000000

    with pytest.raises(pydantic.ValidationError) as exc_info:
        DevTaskContent(**content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("expedite_until",) in bad


def test_emergency_marker_rejects_a_numeric_string_expedite_until():
    content = valid_dev_task_content()
    content["class_of_service"] = "emergency"
    content["expedite_reason"] = "prod outage"
    content["expedite_until"] = "1791000000"

    with pytest.raises(pydantic.ValidationError) as exc_info:
        DevTaskContent(**content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("expedite_until",) in bad


def test_emergency_marker_accepts_a_valid_iso8601_expedite_until():
    content = valid_dev_task_content()
    content["class_of_service"] = "emergency"
    content["expedite_reason"] = "prod outage"
    content["expedite_until"] = "2026-10-05T12:00:00Z"

    task = DevTaskContent(**content)

    assert task.expedite_until.isoformat() == "2026-10-05T12:00:00+00:00"
