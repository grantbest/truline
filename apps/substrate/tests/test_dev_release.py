"""``dev.release`` verdict-record fields (R26.12/B20, O-8).

DB-free, following ``test_arch_risk.py``'s pattern: imports ``src.schemas``
only, never ``src.routes`` or ``src.database``, so this runs without
DATABASE_URL.
"""

import pytest
from pydantic import ValidationError

from src.schemas import DevReleaseContent


def valid_gate_run_content(**over) -> dict:
    content = {
        "reviewer": "gemini/release-gate",
        "verdict": "merge",
        "pr_refs": ["#254"],
        "required_suites": ["apps/mcp-hub/tests"],
        "results": [{
            "category": "regression",
            "suite": "apps/mcp-hub/tests",
            "outcome": "pass",
            "evidence": "321 passed, 6 skipped",
        }],
    }
    content.update(over)
    return content


def valid_verdict_record_content(**over) -> dict:
    content = {
        "reviewer": "gemini/release-gate",
        "verdict": "merge",
        "pr_refs": ["https://github.com/acme/widgets/pull/254"],
        "pr_url": "https://github.com/acme/widgets/pull/254",
        "pr_number": 254,
        "verdict_source": "comment",
        "bead_id": "dev-task-abc123",
        "change_kind": "behavioral",
        "merged_at": "2026-10-04T18:30:00+00:00",
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# The existing gate-run shape still validates
# ---------------------------------------------------------------------------


def test_existing_gate_run_shape_still_validates():
    release = DevReleaseContent(**valid_gate_run_content())
    assert release.verdict == "merge"
    assert release.pr_url is None
    assert release.pr_number is None
    assert release.verdict_source is None
    assert release.bead_id is None
    assert release.change_kind is None
    assert release.merged_at is None


# ---------------------------------------------------------------------------
# A B21-shaped verdict record validates
# ---------------------------------------------------------------------------


def test_verdict_record_shaped_content_validates():
    release = DevReleaseContent(**valid_verdict_record_content())
    assert release.pr_url == "https://github.com/acme/widgets/pull/254"
    assert release.pr_number == 254
    assert release.verdict_source == "comment"
    assert release.bead_id == "dev-task-abc123"
    assert release.change_kind == "behavioral"
    assert release.merged_at is not None


@pytest.mark.parametrize("verdict_source", ["body", "comment", "override"])
def test_verdict_source_accepts_each_declared_value(verdict_source):
    DevReleaseContent(**valid_verdict_record_content(verdict_source=verdict_source))


@pytest.mark.parametrize("change_kind", ["structural", "behavioral", "emergency"])
def test_change_kind_accepts_each_declared_value(change_kind):
    DevReleaseContent(**valid_verdict_record_content(change_kind=change_kind))


@pytest.mark.parametrize("change_kind", ["standard", "normal", "Behavioral"])
def test_unknown_change_kind_is_refused(change_kind):
    content = valid_verdict_record_content(change_kind=change_kind)
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


@pytest.mark.parametrize("verdict", ["MERGE", "approve", "merge_with_changes"])
def test_verdict_outside_its_literal_is_refused(verdict):
    content = valid_verdict_record_content(verdict=verdict)
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


# ---------------------------------------------------------------------------
# pr_url must agree with pr_refs and pr_number
# ---------------------------------------------------------------------------


def test_pr_url_without_pr_refs_containing_it_is_refused():
    content = valid_verdict_record_content(pr_refs=["#254"])
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


def test_pr_url_with_mismatched_pr_number_is_refused():
    content = valid_verdict_record_content(pr_number=999)
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


def test_pr_url_with_no_pull_segment_is_refused():
    content = valid_verdict_record_content(
        pr_url="https://github.com/acme/widgets",
        pr_refs=["https://github.com/acme/widgets"],
    )
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


def test_pr_number_alone_with_no_pr_url_is_accepted():
    """pr_url is what the validator keys off; pr_number without it is not checked."""
    content = valid_gate_run_content(pr_number=254)
    release = DevReleaseContent(**content)
    assert release.pr_number == 254


# ---------------------------------------------------------------------------
# merged_at must be timezone-aware
# ---------------------------------------------------------------------------


def test_naive_merged_at_is_refused():
    content = valid_verdict_record_content(merged_at="2026-10-04T18:30:00")
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


def test_aware_merged_at_is_accepted():
    content = valid_verdict_record_content(merged_at="2026-10-04T18:30:00+00:00")
    release = DevReleaseContent(**content)
    assert release.merged_at is not None


# ---------------------------------------------------------------------------
# verdict_source is a closed vocabulary
# ---------------------------------------------------------------------------


def test_unknown_verdict_source_is_refused():
    content = valid_verdict_record_content(verdict_source="webhook")
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


# ---------------------------------------------------------------------------
# the existing required-suites validator keeps its behaviour
# ---------------------------------------------------------------------------


def test_merge_verdict_still_cannot_rest_on_a_suite_that_never_ran():
    content = valid_gate_run_content(
        results=[{
            "category": "regression",
            "suite": "apps/mcp-hub/tests",
            "outcome": "not-run",
            "evidence": "never executed",
        }],
    )
    with pytest.raises(ValidationError):
        DevReleaseContent(**content)


def test_verdict_record_with_no_required_suites_is_not_subject_to_that_validator():
    release = DevReleaseContent(**valid_verdict_record_content())
    assert release.required_suites == []
