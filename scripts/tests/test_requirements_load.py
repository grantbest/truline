"""Tests for the requirement registry loader.

The real substrate is deliberately not involved here. These tests exercise the
part of the loader that can corrupt the mirror quietly: mapping registry JSON to
bead payloads, comparing by stable business keys, and continuing after a write
rejection without leaking response bodies.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
from pydantic import ValidationError

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "substrate"))

from src.schemas import (  # noqa: E402
    ArchObservationContent,
    ArchRequirementConformanceContent,
    ArchRequirementContent,
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


loader = _load("requirements_load", "requirements-load.py")
release_status = _load("release_status_for_requirements_load", "release-status.py")


def _registry():
    return {
        "registry": {"id": "REG-TEST"},
        "requirements": [
            {
                "id": "PC-SUB-001",
                "capability": "CAP-SUB",
                "title": "No execution bookkeeping",
                "status": "defective",
                "priority": "P0",
                "source": "architecture-mandated",
                "rationale": "Beads state intent.",
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "given": "a task bead",
                        "when": "content is read",
                        "then": "execution counters are absent",
                        "verification": "code inspection",
                        "conformance": "fail",
                        "measured_at": "2026-08-14",
                        "measured_revision": "4540407",
                    }
                ],
            },
            {
                "id": "LO-ING-001",
                "capability": "CAP-ING",
                "title": "Ingest receipts",
                "user_story": "As the Operator, I can capture receipts.",
                "status": "absent",
                "priority": "P1",
                "source": "analyst-proposed",
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "given": "a receipt",
                        "when": "it is submitted",
                        "then": "it is retained",
                        "verification": "not implemented",
                        "conformance": "absent",
                        "measured_at": "2026-08-06",
                    }
                ],
            },
        ],
    }


class FakeSubstrate:
    def __init__(self, reject_requirement_ids=None):
        self.beads = {"requirement": [], "requirement_conformance": []}
        self.creates = []
        self.patches = []
        self.reject_requirement_ids = set(reject_requirement_ids or [])
        self.next_id = 1

    def list_beads(self, bead_type, limit=1000):
        return list(self.beads[bead_type])

    def create(self, bead_type, state, content):
        self.creates.append((bead_type, state, content))
        requirement_id = content.get("id") or content.get("requirement_id")
        if requirement_id in self.reject_requirement_ids:
            raise loader.SubstrateError(422, "body containing secret-token", "/beads")
        bead = {
            "id": f"bead-{self.next_id}",
            "type": bead_type,
            "state": state,
            "content": content,
        }
        self.next_id += 1
        self.beads[bead_type].append(bead)
        return bead

    def patch(self, bead_id, body):
        self.patches.append((bead_id, body))
        for beads in self.beads.values():
            for bead in beads:
                if bead["id"] == bead_id:
                    bead.update(body)
                    return bead
        raise AssertionError(f"unknown bead id {bead_id}")


def test_requirement_content_strips_measurements_and_preserves_structure():
    requirements, _ = loader.mapped_items(_registry())
    platform = requirements[0].content
    lifeops = requirements[1].content

    criterion = platform["acceptance_criteria"][0]
    assert platform["registry_id"] == "REG-TEST"
    assert "conformance" not in criterion
    assert "measured_at" not in criterion
    assert "measured_revision" not in criterion
    assert criterion["verification"] == "code inspection"
    assert lifeops["user_story"] == "As the Operator, I can capture receipts."
    assert lifeops["rationale"] == lifeops["user_story"]


def test_conformance_entries_become_dated_conformance_records():
    _, conformances = loader.mapped_items(_registry())

    assert len(conformances) == 2
    first = conformances[0].content
    assert "kind" not in first
    assert "measurement" not in first
    assert "conformance" not in first
    assert first["registry_id"] == "REG-TEST"
    assert first["requirement_id"] == "PC-SUB-001"
    assert first["acceptance_criterion_id"] == "AC-1"
    assert first["verdict"] == "fail"
    assert first["measured_at"] == "2026-08-14"
    assert first["measured_revision"] == "4540407"
    assert first["source_class"] == "derived"


def test_dry_run_plans_creates_and_writes_nothing():
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate()

    plan = loader.reconcile(fake, requirements, conformances, apply=False)

    assert len(plan.requirement_creates) == 2
    assert len(plan.conformance_creates) == 2
    assert not fake.creates
    assert not fake.patches


def test_apply_twice_is_idempotent_against_business_keys():
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate()

    first = loader.reconcile(fake, requirements, conformances, apply=True)
    second = loader.reconcile(fake, requirements, conformances, apply=True)

    assert len(first.requirement_creates) == 2
    assert len(first.conformance_creates) == 2
    assert second.requirement_creates == []
    assert second.requirement_updates == []
    assert second.conformance_creates == []
    assert second.conformance_updates == []
    assert len(second.requirement_unchanged) == 2
    assert len(second.conformance_unchanged) == 2


def test_rejected_payload_reports_requirement_id_and_continues(capsys):
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate(reject_requirement_ids={"PC-SUB-001"})

    plan = loader.reconcile(fake, requirements, conformances, apply=True)
    exit_code = loader.report(plan)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "PC-SUB-001: requirement create rejected" in output
    assert "PC-SUB-001: conformance create rejected" in output
    assert "LO-ING-001" not in output
    assert "secret-token" not in output
    assert any(
        content.get("id") == "LO-ING-001" for bead_type, _, content in fake.creates
        if bead_type == "requirement"
    )


def test_all_refused_reports_zero_creates_nonempty_errors_and_nonzero_exit(capsys):
    """The defect: a run in which every write is refused must never be
    reportable as a success. Both ids are rejected, so nothing lands."""
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate(reject_requirement_ids={"PC-SUB-001", "LO-ING-001"})

    plan = loader.reconcile(fake, requirements, conformances, apply=True)
    exit_code = loader.report(plan)
    output = capsys.readouterr().out

    assert plan.requirement_creates == []
    assert plan.conformance_creates == []
    assert len(plan.requirement_create_failures) == 2
    assert len(plan.conformance_create_failures) == 2
    assert exit_code == 1
    assert "errors:" in output
    assert "4 write(s) refused by the substrate" in output
    assert "creates=0" in output
    assert not fake.beads["requirement"]
    assert not fake.beads["requirement_conformance"]


def test_mixed_run_reports_accepted_and_refused_separately(capsys):
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate(reject_requirement_ids={"PC-SUB-001"})

    plan = loader.reconcile(fake, requirements, conformances, apply=True)
    exit_code = loader.report(plan)
    output = capsys.readouterr().out

    assert plan.requirement_creates == ["LO-ING-001"]
    assert plan.requirement_create_failures == ["PC-SUB-001"]
    assert plan.conformance_creates == [
        c.content["ref"] for c in conformances if c.requirement_id == "LO-ING-001"
    ]
    assert plan.conformance_create_failures == [
        c.content["ref"] for c in conformances if c.requirement_id == "PC-SUB-001"
    ]
    assert exit_code == 1
    assert "creates=1 create_failures=1" in output
    assert "2 write(s) refused by the substrate" in output


def test_fully_successful_apply_run_output_and_exit_are_unchanged(capsys):
    requirements, conformances = loader.mapped_items(_registry())
    fake = FakeSubstrate()

    plan = loader.reconcile(fake, requirements, conformances, apply=True)
    exit_code = loader.report(plan)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert plan.errors == []
    assert output == (
        "requirements-load: applied\n"
        "requirements: creates=2 updates=0 unchanged=0\n"
        "conformances: creates=2 updates=0 unchanged=0\n"
    )


# --- against the real substrate schemas (not a stub) -------------------------


def test_requirement_and_conformance_content_validate_against_real_substrate_schemas():
    """A stub schema is exactly what let the writer, the store and the reader
    drift apart unnoticed (see the task history). This validates against the
    actual pydantic models `apps/substrate/src/schemas.py` uses at POST/PATCH
    time."""
    requirements, conformances = loader.mapped_items(_registry())

    for item in requirements:
        ArchRequirementContent.model_validate(item.content)
    for item in conformances:
        ArchRequirementConformanceContent.model_validate(item.content)


def test_conformance_content_is_rejected_by_the_closed_observation_schema():
    """The defect this loader exists to fix: arch.observation cannot hold a
    conformance verdict — it never could, and widening it to admit one is
    exactly what was rejected in favor of a sibling type. This is the
    guarantee, not a regression to route around."""
    _, conformances = loader.mapped_items(_registry())

    with pytest.raises(ValidationError):
        ArchObservationContent.model_validate(conformances[0].content)


def test_release_status_reads_the_conformance_records_it_is_fed():
    """End-to-end against release-status.py's own matcher: a criterion a
    charter cites reports a verdict and a measured date, not
    'no arch.requirement_conformance recorded'."""
    _, conformances = loader.mapped_items(_registry())
    for item in conformances:
        ArchRequirementConformanceContent.model_validate(item.content)

    charter = {
        "ref": "R99.99",
        "name": "test charter",
        "objective": "x",
        "opened_at": "2026-08-14",
        "declared_balance": {},
        "outcomes": [
            {
                "id": "O-1",
                "statement": "x",
                "work_class": "enabling",
                "requirement_refs": ["PC-SUB-001/AC-1"],
            }
        ],
    }
    conformance_beads = [{"content": item.content} for item in conformances]

    report = release_status.build_release_report(charter, [], {}, conformance_beads)

    status = report.criteria[0]
    assert status.latest is not None
    assert status.latest.value == "fail"
    assert status.latest.measured_at == "2026-08-14"
    rendered = release_status.format_criterion(status)
    assert "no arch.requirement_conformance recorded" not in rendered
