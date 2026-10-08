"""Newly filed work must carry a requirement that resolves.

the Operator's decision 2026-08-08, taken on the count TF-1 produced: 12 of 64 dev.task
records carried a requirement reference, 52 carried none, 0 pointed nowhere.

The refusal is at the FILING boundary and nowhere else. That is the whole design.
Refusing inside ``DevTaskContent`` would make those 52 historical beads
unreadable, and their content records what was true when they ran; gating the
whole population would block today's work on July's debt and be abandoned inside
a week. ``test_a_historical_bead_without_references_still_validates`` is the test
that holds the line.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

DISPATCHER = pathlib.Path(__file__).resolve().parents[1]
REPO = DISPATCHER.parents[1]
sys.path.insert(0, str(DISPATCHER))
sys.path.insert(0, str(REPO / "apps" / "substrate" / "src"))

import file_task  # noqa: E402


def _spec(**over):
    spec = {
        "lane": "code-health",
        "title": "a task",
        "intent": "why it exists",
        "acceptance": ["THE thing SHALL happen."],
        "scope": {"paths": ["apps/factory-dispatcher/"]},
        "risk_class": "behavioral",
        # This scope is a boundary scope (C-4) -- these fixtures are about
        # requirement traceability, not C-4, so they carry declarations here.
        "principal": "test fixture; exercises unrelated behaviour",
        "reaches": ["none"],
    }
    spec.update(over)
    return spec


# --- the refusal ------------------------------------------------------------


def test_filing_without_a_reference_or_waiver_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec())

    message = str(excinfo.value)
    assert "no requirement_refs" in message
    # A refusal must tell the author how to satisfy it, not only that they failed.
    assert "searched:" in message
    assert "requirement_refs_waived" in message


def test_a_reference_that_resolves_is_accepted():
    content = file_task.build_content(_spec(requirement_refs=["PC-FAC-001"]))

    assert content["requirement_refs"] == ["PC-FAC-001"]


def test_a_criterion_level_reference_that_resolves_is_accepted():
    content = file_task.build_content(_spec(requirement_refs=["PC-FAC-001/AC-4"]))

    assert content["requirement_refs"] == ["PC-FAC-001/AC-4"]


def test_a_well_formed_reference_that_resolves_to_nothing_is_refused():
    """The schema accepts this shape. Accepting it here teaches people to type it."""
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(requirement_refs=["PC-SUB-999"]))

    message = str(excinfo.value)
    assert "PC-SUB-999" in message
    assert "resolve to nothing" in message


def test_a_missing_registry_directory_is_blamed_on_the_environment_not_the_citation(
    tmp_path, monkeypatch
):
    """PR #826, F1: a missing docs/requirements must not read as a bad citation.

    Measured 2026-09-13: with the registry directory unreachable, this branch
    used to say "requirement reference(s) resolve to nothing: PC-FAC-001/AC-5"
    -- accusing a real, valid citation of pointing nowhere, when the actual
    fault was that the registry was never reachable in the first place.
    """
    monkeypatch.setattr(file_task, "REQUIREMENTS_DIR", tmp_path / "does-not-exist")

    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(requirement_refs=["PC-FAC-001/AC-5"]))

    message = str(excinfo.value)
    assert "PC-FAC-001/AC-5" in message
    assert "resolve to nothing" not in message
    assert "points nowhere" not in message
    assert str(tmp_path / "does-not-exist") in message
    assert "environment" in message


def test_no_refs_and_a_missing_registry_directory_is_an_environment_fault(
    tmp_path, monkeypatch
):
    """The second refusal branch shares the same ambiguous '(no registries
    found)' text as the first, and must be fixed the same way."""
    monkeypatch.setattr(file_task, "REQUIREMENTS_DIR", tmp_path / "does-not-exist")

    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec())

    message = str(excinfo.value)
    assert "no requirement_refs" in message
    assert str(tmp_path / "does-not-exist") in message
    assert "environment" in message
    assert "0 requirements" not in message


def test_a_reachable_empty_registry_still_uses_the_original_wording(tmp_path, monkeypatch):
    """An existing-but-empty directory is not an environment fault -- the
    existing wording must be kept for the case it actually describes."""
    empty = tmp_path / "requirements"
    empty.mkdir()
    monkeypatch.setattr(file_task, "REQUIREMENTS_DIR", empty)

    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(requirement_refs=["PC-FAC-001/AC-5"]))

    message = str(excinfo.value)
    assert "resolve to nothing" in message
    assert "environment" not in message


def test_one_bad_reference_among_good_ones_is_still_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(
            _spec(requirement_refs=["PC-FAC-001", "PC-SUB-999"])
        )

    assert "PC-SUB-999" in str(excinfo.value)
    assert "PC-FAC-001" not in str(excinfo.value).split("resolve to nothing")[1]


# --- the waiver is explicit, reasoned, and recorded -------------------------


def test_file_task_traceability_a_waiver_with_a_reason_is_accepted_and_reaches_the_bead():
    content = file_task.build_content(
        _spec(requirement_refs_waived="upstream version assessment; no requirement yet")
    )

    assert content["requirement_refs_waived"] == (
        "upstream version assessment; no requirement yet"
    )


def test_a_blank_waiver_is_refused_exactly_like_a_missing_reference():
    """An empty string is not a reason. It is the escape hatch going silent."""
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(requirement_refs_waived="   "))

    assert "no requirement_refs" in str(excinfo.value)


def test_a_waiver_is_not_needed_when_a_reference_resolves():
    content = file_task.build_content(_spec(requirement_refs=["PC-FAC-001"]))

    assert "requirement_refs_waived" not in content


# --- the historical population is untouched ---------------------------------


def test_a_historical_bead_without_references_still_validates():
    """52 records carry none. They must stay readable, and are not migrated.

    If this ever fails, the refusal has leaked out of the filing boundary and
    into the schema, which is the one thing this design must not do.
    """
    from schemas import DevTaskContent

    historical = {
        "lane": "code-health",
        "title": "filed in July, before any of this existed",
        "intent": "...",
        "context_refs": [],
        "acceptance": ["THE thing SHALL happen."],
        "verification": {"commands": ["true"], "must_report_unverified": True},
        "scope": {"paths": ["apps/"], "forbidden_paths": [".github/workflows/**"]},
        "risk_class": "behavioral",
        "budget": {"max_agent_minutes": 30, "max_usd": 2.0, "max_tokens": 250000},
    }

    model = DevTaskContent(**historical)

    assert model.requirement_refs == []
    assert model.nfrs == []
    assert model.arch_impact is None
