"""``arch.requirement_conformance`` — the dated verdict carrier (OPS-28-2).

Neither ``ArchRequirementContent`` (structure only, embedding a measurement is
a schema violation — see ``test_arch_schema.py``'s
``test_arch_requirement_rejects_*_measurement_fields``) nor
``ArchObservationContent`` (closed on ``observed_at``/``workload``, neither of
which a requirement measurement has — see ``test_arch_observation_schema_
rejects_missing_required_content`` below) can hold a dated conformance
verdict. This is the sibling type both refusals actually name: mirrors
``test_arch_ci.py``'s structure, the closed/derived type this repository
already has one of.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import src.cache
from src.database import get_db
from src.routes import router
from src.schemas import (
    ARCH_TYPE_SCHEMAS,
    ArchObservationContent,
    ArchRequirementConformanceContent,
    validate_arch_content,
)

HEADERS = {"x-api-key": "test-key"}


def valid_conformance_content(**over) -> dict:
    content = {
        "ref": "requirement.REG-PLATFORM.PC-SUB-001.AC-1.2026-08-14.4540407",
        "registry_id": "REG-PLATFORM",
        "requirement_id": "PC-SUB-001",
        "acceptance_criterion_id": "AC-1",
        "measured_at": "2026-08-14",
        "verdict": "fail",
        "measured_revision": "4540407",
        "verification": "code inspection",
        "source_class": "derived",
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_conformance_type_is_registered_in_arch_type_schemas():
    assert ARCH_TYPE_SCHEMAS["requirement_conformance"] is ArchRequirementConformanceContent


def test_valid_conformance_content_is_accepted():
    validate_arch_content("requirement_conformance", valid_conformance_content())


@pytest.mark.parametrize(
    "optional_field", ["acceptance_criterion_id", "measured_revision", "verification"]
)
def test_conformance_content_accepts_optional_fields_omitted(optional_field):
    content = valid_conformance_content()
    del content[optional_field]

    validate_arch_content("requirement_conformance", content)


@pytest.mark.parametrize(
    "required_field",
    ["ref", "registry_id", "requirement_id", "measured_at", "verdict", "source_class"],
)
def test_conformance_content_rejects_missing_required_field(required_field):
    content = valid_conformance_content()
    del content[required_field]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("requirement_conformance", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


def test_conformance_content_rejects_blank_required_strings():
    content = valid_conformance_content(verdict="   ")

    with pytest.raises(ValidationError):
        validate_arch_content("requirement_conformance", content)


def test_conformance_content_is_closed_shape():
    content = valid_conformance_content(unexpected_field="surprise")

    with pytest.raises(ValidationError):
        validate_arch_content("requirement_conformance", content)


@pytest.mark.parametrize(
    "requirement_id", ["PC-SUB-1", "pc-sub-001", "PC-001", "PC-SUB-CAT-LOG-EXTRA-001"]
)
def test_conformance_content_rejects_malformed_requirement_id(requirement_id):
    content = valid_conformance_content(requirement_id=requirement_id)

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("requirement_conformance", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("requirement_id",) in bad


def test_conformance_content_rejects_the_arch_observation_shape():
    """The defect this type exists to fix: a conformance-shaped payload was
    never accepted by ``arch.observation`` (closed on ``observed_at`` /
    ``workload``), and widening that type to admit one was rejected in favor
    of this sibling. The closure stays intact."""
    content = valid_conformance_content()

    with pytest.raises(ValidationError):
        ArchObservationContent.model_validate(content)


# ---------------------------------------------------------------------------
# source_class is locked to "derived" — the schema-level rejection of a
# hand-created record, before any PATCH-time ownership question is reached.
# ---------------------------------------------------------------------------


def test_conformance_content_rejects_missing_source_class():
    content = valid_conformance_content()
    del content["source_class"]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("requirement_conformance", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("source_class",) in missing


@pytest.mark.parametrize("hand_authored_value", ["authored", "observed"])
def test_conformance_content_rejects_non_derived_source_class(hand_authored_value):
    content = valid_conformance_content(source_class=hand_authored_value)

    with pytest.raises(ValidationError):
        validate_arch_content("requirement_conformance", content)


def test_conformance_content_accepts_only_derived_source_class():
    content = valid_conformance_content(source_class="derived")
    validate_arch_content("requirement_conformance", content)


# ---------------------------------------------------------------------------
# HTTP: POST /beads — a hand-created record is rejected before it is written.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def test_create_bead_rejects_hand_created_conformance_missing_source_class(app):
    async def unused_db():
        yield None

    content = valid_conformance_content()
    del content["source_class"]

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "requirement_conformance",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "grant",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "arch_content_schema_violation"


def test_create_bead_rejects_hand_created_conformance_declaring_authored(app):
    async def unused_db():
        yield None

    content = valid_conformance_content(source_class="authored")

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "requirement_conformance",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "grant",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# HTTP: PATCH /beads/{id} — the derived-only ownership check, same contract
# as every other non-authored arch type (test_arch_source_class.py).
# ---------------------------------------------------------------------------


def _fake_bead(content: dict, created_by: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace="arch",
        type="requirement_conformance",
        state="active",
        trust_tier="system",
        parent_id=None,
        content=content,
        context={},
        provenance={},
        confidence=None,
        created_at=now,
        updated_at=now,
        created_by=created_by,
    )


class _FakeSession:
    def __init__(self, bead: SimpleNamespace):
        self.bead = bead
        self.committed = False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        return None


def _client(app, session) -> TestClient:
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def test_update_bead_rejects_derived_conformance_written_by_non_loader(app):
    bead = _fake_bead(valid_conformance_content(), created_by="requirements-load")
    session = _FakeSession(bead)

    new_content = valid_conformance_content(verdict="pass")

    response = _client(app, session).patch(
        f"/beads/{bead.id}",
        json={"content": new_content, "created_by": "grant"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_ownership_violation"
    assert detail["owning_class"] == "derived"
    assert session.committed is False


def test_update_bead_allows_derived_conformance_written_by_its_loader(app):
    bead = _fake_bead(valid_conformance_content(), created_by="requirements-load")
    session = _FakeSession(bead)

    new_content = valid_conformance_content(verdict="pass")

    response = _client(app, session).patch(
        f"/beads/{bead.id}",
        json={"content": new_content, "created_by": "requirements-load"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert session.committed is True
