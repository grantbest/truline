import importlib.util
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

import src.routes
from src.database import get_db
from src.routes import router
from src.schemas import (
    ARCH_TYPE_SCHEMAS,
    ArchPrincipleContent,
    ArchRequirementContent,
    ArchWorkload,
    validate_arch_content,
    validate_bead_content,
)


HEADERS = {"x-api-key": "test-key"}


def valid_arch_common(ref: str = "bc.finance.visibility") -> dict:
    return {
        "ref": ref,
        "name": "Account and Balance Visibility",
        "description": "A current, trustworthy view of every account balance.",
        "layer": "demand",
        "owner": "grant",
        "evidence": ["docs/reference/mcp-finance.md"],
        "assessed_at": "2026-07-30",
    }


def valid_capability_content() -> dict:
    return {**valid_arch_common(), "maturity": "operating"}


def valid_application_content() -> dict:
    return {
        **valid_arch_common("app.substrate"),
        "name": "Bead Substrate",
        "description": "FastAPI and Postgres service holding typed work units.",
        "layer": "supply",
        "build": "custom",
        "business_value": "high",
        "technical_health": "healthy",
        "time_disposition": "invest",
        # Required since EA-S1 (#197): an application must say what it runs as.
        # The real app.substrate object carries both the dev and prod objects;
        # one is enough to exercise the contract.
        "workload": {
            "runtime": "kubernetes",
            "objects": [
                {
                    "cluster": "cluster-a",
                    "namespace": "platform-substrate",
                    "kind": "Deployment",
                    "name": "substrate",
                    "manifest": "infrastructure/k8s/base/substrate/substrate.yaml",
                    "managed_by": "argocd",
                }
            ],
        },
        "realizes": ["pc.substrate.persistence"],
        "depends_on": ["app.temporal-postgres"],
    }


def valid_service_content() -> dict:
    return {
        **valid_arch_common("svc.substrate.api"),
        "name": "Substrate API",
        "layer": "supply",
        "depends_on": ["svc.postgres"],
    }


def valid_information_object_content() -> dict:
    return {
        **valid_arch_common("io.bead"),
        "name": "Bead",
        "layer": "supply",
    }


def valid_observation_content() -> dict:
    return {
        "observed_at": "2026-07-30T20:15:00Z",
        "workload": {
            "cluster": "cluster-a",
            "namespace": "platform-substrate",
            "kind": "Deployment",
            "name": "substrate",
        },
        "image_ref": "ghcr.io/grantbest/substrate@sha256:1234abcd",
        "replicas": 2,
        "ready_replicas": 2,
        "argocd_sync_status": "Synced",
        "argocd_health_status": "Healthy",
        "last_synced_revision": "0470b0a",
    }


def valid_requirement_content() -> dict:
    return {
        "registry_id": "REG-PLATFORM",
        "id": "PC-SUB-003",
        "capability": "CAP-SUB",
        "title": "The edge vocabulary is closed and validated",
        "status": "defective",
        "priority": "P1",
        "source": "architecture-mandated",
        "rationale": (
            "A graph whose edge types are free text cannot be queried with "
            "confidence: a typo creates a new relationship rather than an error."
        ),
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "given": "a bead_link with an undocumented link_type",
                "when": "it is created",
                "then": "it is rejected",
                "verification": "code inspection",
            },
            {
                "id": "AC-2",
                "given": "the documented edge vocabulary",
                "when": "it is compared with the edge types the EA loader writes",
                "then": "they agree",
            },
        ],
    }


def valid_requirement_conformance_content() -> dict:
    return {
        "ref": "requirement.REG-PLATFORM.PC-SUB-003.AC-1.2026-08-14.4540407",
        "registry_id": "REG-PLATFORM",
        "requirement_id": "PC-SUB-003",
        "acceptance_criterion_id": "AC-1",
        "measured_at": "2026-08-14",
        "verdict": "fail",
        "measured_revision": "4540407",
        "source_class": "derived",
    }


def valid_principle_content() -> dict:
    return {
        "statement": (
            "Every dispatch is a cheap, rejectable option only because the "
            "release gate can reject it; changes to the gate are "
            "amendment-class."
        ),
        "rationale": (
            "Eight PRs merged on green CI with no verdict-bearing gate "
            "(incident 2026-08-06) showed a rejectable option needs an "
            "actual gate behind it, not just green CI."
        ),
        "source": "DRv2 pp. 153-158 (A1); incident 2026-08-06.",
        "status": "adopted",
        "status_history": [
            {
                "date": "2026-08-06",
                "status": "adopted",
                "reason": (
                    "CLAUDE.md merging rules and GEMINI.md already carry it "
                    "as binding text."
                ),
            }
        ],
    }


def _without_requirement_measurements(value):
    if isinstance(value, dict):
        return {
            key: _without_requirement_measurements(child)
            for key, child in value.items()
            if key
            not in {"conformance", "verdict", "measured_at", "measured_revision"}
        }
    if isinstance(value, list):
        return [_without_requirement_measurements(child) for child in value]
    return value


def _platform_requirement(requirement_id: str) -> dict:
    root = Path(__file__).resolve().parents[3]
    registry = json.loads(
        (root / "docs/requirements/platform-requirements.json").read_text()
    )
    requirement = next(
        req for req in registry["requirements"] if req["id"] == requirement_id
    )
    return {
        "registry_id": registry["registry"]["id"],
        **_without_requirement_measurements(requirement),
    }


def _requirements_load_module():
    """Load ``scripts/requirements-load.py`` by path.

    Its filename is not import-friendly (a hyphen), and it lives outside
    ``apps/substrate`` entirely — but its ``requirement_content`` is the exact
    transform every LifeOps requirement goes through before it reaches this
    schema in production, so a mirror rewritten by hand here could drift from
    it. Loading it by path (read-only; nothing here writes to it) lets the
    LifeOps round-trip test below assert against production behavior instead
    of a guess at it.
    """
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "requirements_load", root / "scripts" / "requirements-load.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lifeops_registry() -> dict:
    root = Path(__file__).resolve().parents[3]
    return json.loads(
        (root / "docs/requirements/lifeops-requirements.json").read_text()
    )


def _platform_registry() -> dict:
    root = Path(__file__).resolve().parents[3]
    return json.loads(
        (root / "docs/requirements/platform-requirements.json").read_text()
    )


def _principle_entry_from_registry(principle_id: str) -> dict:
    """Pull one ``PRIN-NNN`` entry's statement/source/status out of the
    interim markdown registry, so this test fails if principles.md and the
    schema disagree about what an entry looks like -- the same discipline
    ``test_arch_requirement_round_trips_real_platform_requirement_entry``
    applies to the JSON requirements registry. principles.md is prose, not
    structured data (that's F-DCE-2's job to fix); this parses just enough of
    its documented bullet format to prove the shape round-trips.
    """
    root = Path(__file__).resolve().parents[3]
    text = (root / "docs/architecture/principles.md").read_text()

    heading = re.search(rf"^### {re.escape(principle_id)} .+$", text, re.MULTILINE)
    assert heading, f"{principle_id} not found in principles.md"
    body = text[heading.end():]
    next_heading = re.search(r"^### ", body, re.MULTILINE)
    if next_heading:
        body = body[: next_heading.start()]

    statement = re.search(r"\*\*Statement:\*\* (.+?)\n- \*\*", body, re.DOTALL)
    source = re.search(r"\*\*Source:\*\* (.+?)\n- \*\*", body, re.DOTALL)
    status = re.search(r"\*\*Status:\*\* `(\w+)`", body)
    assert statement and source and status, f"could not parse {principle_id}"

    return {
        "statement": " ".join(statement.group(1).split()),
        "source": " ".join(source.group(1).split()),
        "status": status.group(1),
    }


@pytest.mark.parametrize(
    ("bead_type", "content"),
    [
        ("capability", valid_capability_content()),
        ("application", valid_application_content()),
        ("service", valid_service_content()),
        ("information_object", valid_information_object_content()),
        ("requirement", valid_requirement_content()),
        ("requirement_conformance", valid_requirement_conformance_content()),
        ("principle", valid_principle_content()),
        ("observation", valid_observation_content()),
    ],
)
def test_arch_schema_accepts_valid_hardened_arch_content(bead_type, content):
    validate_arch_content(bead_type, content)


def test_arch_schema_models_hardened_arch_types():
    assert set(ARCH_TYPE_SCHEMAS) == {
        "capability",
        "application",
        "service",
        "information_object",
        "requirement",
        "requirement_conformance",
        "principle",
        "observation",
        "change",
        "incident",
        "ci",
        "release",
        "risk",
        "release_health",
    }


def test_arch_capability_schema_rejects_missing_required_content():
    content = valid_capability_content()
    del content["evidence"]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("capability", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("evidence",) in missing


def test_arch_application_schema_rejects_invalid_modelled_relationships():
    content = valid_application_content()
    content["depends_on"] = "app.temporal-postgres"

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("application", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("depends_on",) in bad


def test_arch_requirement_round_trips_real_platform_requirement_entry():
    content = _platform_requirement("PC-SUB-003")

    requirement = ArchRequirementContent.model_validate(content)

    assert requirement.model_dump(mode="json")["registry_id"] == "REG-PLATFORM"
    assert requirement.model_dump(mode="json")["id"] == content["id"]
    assert requirement.model_dump(mode="json")["capability"] == content["capability"]
    assert requirement.model_dump(mode="json")["acceptance_criteria"] == (
        content["acceptance_criteria"]
    )


@pytest.mark.parametrize(
    "requirement_id",
    ["PC-SUB-3", "pc-sub-003", "PC-003", "PC-SUB-CAT-LOG-EXTRA-003"],
)
def test_arch_requirement_rejects_malformed_requirement_id(requirement_id):
    content = valid_requirement_content()
    content["id"] = requirement_id

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("requirement", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("id",) in bad


def test_arch_requirement_accepts_three_and_four_uppercase_requirement_segments():
    for requirement_id in ["LO-CAT-004", "AA-BB-CC-001", "AA-BB-CC-DD-001"]:
        content = valid_requirement_content()
        content["id"] = requirement_id

        validate_arch_content("requirement", content)


def test_arch_requirement_rejects_unknown_status():
    content = valid_requirement_content()
    content["status"] = "unverified"

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("requirement", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("status",) in bad


@pytest.mark.parametrize("required_field", ["id", "given", "when", "then"])
def test_arch_requirement_rejects_incomplete_acceptance_criteria(required_field):
    content = valid_requirement_content()
    del content["acceptance_criteria"][0][required_field]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("requirement", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("acceptance_criteria", 0, required_field) in missing


@pytest.mark.parametrize(
    "measurement_field",
    ["conformance", "verdict", "measured_at", "measured_revision"],
)
def test_arch_requirement_rejects_top_level_measurement_fields(measurement_field):
    content = valid_requirement_content()
    content[measurement_field] = "pass"

    with pytest.raises(Exception):
        validate_arch_content("requirement", content)


@pytest.mark.parametrize(
    "measurement_field",
    ["conformance", "verdict", "measured_at", "measured_revision"],
)
def test_arch_requirement_rejects_acceptance_criteria_measurement_fields(
    measurement_field,
):
    content = valid_requirement_content()
    content["acceptance_criteria"][0][measurement_field] = "pass"

    with pytest.raises(Exception):
        validate_arch_content("requirement", content)


def test_arch_requirement_rejects_nested_acceptance_criteria_measurement_fields():
    content = valid_requirement_content()
    content["acceptance_criteria"][0]["scanner_metadata"] = {
        "latest": {"measured_revision": "4540407"}
    }

    with pytest.raises(Exception):
        validate_arch_content("requirement", content)


def test_arch_requirement_accepts_additive_structure_fields():
    content = valid_requirement_content()
    content["implementation"] = ["apps/substrate/src/schemas.py"]
    content["acceptance_criteria"][0]["notes"] = {"owner": "gemini/release-gate"}

    validate_arch_content("requirement", content)


def test_arch_requirement_accepts_a_user_story_rationale():
    """LifeOps' own shape: prose is not the only admitted ``rationale``."""
    content = valid_requirement_content()
    content["rationale"] = {
        "as_a": "household operator",
        "i_want": "each linked institution's transactions pulled on a schedule",
        "so_that": "the ledger is current without me touching it",
    }

    requirement = ArchRequirementContent.model_validate(content)

    assert requirement.model_dump(mode="json")["rationale"] == content["rationale"]


@pytest.mark.parametrize(
    "bad_rationale",
    [
        42,
        ["as_a", "i_want", "so_that"],
        {"as_a": "grant", "i_want": "x"},  # missing so_that
        {"as_a": "grant", "i_want": "x", "so_that": "y", "extra": "z"},  # stray key
        "",  # blank prose
    ],
    ids=["int", "list", "missing-so_that", "stray-extra-key", "blank-string"],
)
def test_arch_requirement_rejects_rationale_that_is_neither_admitted_shape(
    bad_rationale,
):
    """``rationale`` admits exactly two shapes, not a third, near-miss one.

    Widening it to ``Any``/``dict`` would let any of these through; the
    declared union on ``rationale`` (str, or the closed
    ``ArchRequirementUserStoryRationale``) refuses all of them the same way
    the pre-existing string rationale was always refused for a non-string.
    """
    content = valid_requirement_content()
    content["rationale"] = bad_rationale

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("requirement", content)

    bad = {error["loc"][0] for error in exc_info.value.errors()}
    assert "rationale" in bad


def test_arch_requirement_post_returns_422_for_a_rationale_that_is_neither_shape(app):
    """The same refusal, through the real create-bead HTTP path.

    ``ArchRequirementUserStoryRationale.so_that`` is missing here — not a
    string and not a complete user story either — so this must still be a
    422, not a silently-accepted third shape.
    """

    async def unused_db():
        yield None

    content = valid_requirement_content()
    content["rationale"] = {"as_a": "grant", "i_want": "reconcile my own bills"}

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "requirement",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    bad = {error["loc"][0] for error in detail["errors"]}
    assert "rationale" in bad


def test_arch_requirement_accepts_every_real_lifeops_registry_entry():
    """Every requirement in the real LifeOps registry, mirrored the way
    production does, must be creatable as an ``arch.requirement`` bead.

    This is the regression test for the defect this change fixes: all 43
    LifeOps requirements write ``rationale`` as a user story, and every one
    of them was refused with a 422 before ``rationale`` admitted that shape.
    It reads the live ``docs/requirements/lifeops-requirements.json`` and the
    live ``scripts/requirements-load.py::requirement_content`` transform, so
    a 44th entry (or any entry edited into a bad shape) fails this test
    instead of silently reintroducing the defect.
    """
    module = _requirements_load_module()
    registry = _lifeops_registry()
    requirements = registry["requirements"]
    assert requirements, "lifeops-requirements.json holds no requirements"

    failures = []
    for requirement in requirements:
        content = module.requirement_content(registry["registry"]["id"], requirement)
        try:
            validate_arch_content("requirement", content)
        except Exception as exc:  # pragma: no cover - the message is the point
            failures.append(f"{requirement['id']}: {exc}")

    assert not failures, "\n".join(failures)


def test_arch_requirement_accepts_every_real_platform_registry_entry():
    """Every requirement in the real platform registry, mirrored the way
    production does, must be creatable as an ``arch.requirement`` bead.

    Before this test, only ``PC-SUB-003`` was ever checked against the live
    schema (``test_arch_requirement_round_trips_real_platform_requirement_entry``,
    above); every other entry in the registry merged unexamined. This is the
    platform counterpart of
    ``test_arch_requirement_accepts_every_real_lifeops_registry_entry`` and
    applies the same discipline: it reads the live
    ``docs/requirements/platform-requirements.json`` and the live
    ``scripts/requirements-load.py::requirement_content`` transform, so a
    malformed or newly added ``PC-*`` entry fails this test naming its own id
    instead of merging silently.
    """
    module = _requirements_load_module()
    registry = _platform_registry()
    requirements = registry["requirements"]
    assert requirements, "platform-requirements.json holds no requirements"

    failures = []
    for requirement in requirements:
        content = module.requirement_content(registry["registry"]["id"], requirement)
        try:
            validate_arch_content("requirement", content)
        except Exception as exc:  # pragma: no cover - the message is the point
            failures.append(f"{requirement['id']}: {exc}")

    assert not failures, "\n".join(failures)


def test_arch_requirement_preserves_lifeops_user_story_content_verbatim():
    """The mirrored rationale is the registry's own words, not a rewrite.

    Guards against a future "fix" that flattens the user story into prose
    when mirroring it onto ``rationale`` — that would be exactly the lossy
    migration the schema change was chosen to avoid performing.
    """
    module = _requirements_load_module()
    registry = _lifeops_registry()
    requirement = next(
        req for req in registry["requirements"] if req["id"] == "LO-ING-001"
    )
    assert isinstance(requirement["user_story"], dict)

    content = module.requirement_content(registry["registry"]["id"], requirement)
    parsed = ArchRequirementContent.model_validate(content)

    dumped = parsed.model_dump(mode="json")
    assert dumped["rationale"] == requirement["user_story"]
    assert set(dumped["rationale"]) == {"as_a", "i_want", "so_that"}


def test_arch_principle_round_trips_prin_001_from_registry():
    parsed = _principle_entry_from_registry("PRIN-001")
    assert parsed["status"] == "adopted"

    content = {
        **parsed,
        # principles.md's interim format hasn't split rationale out from
        # source yet (that split is part of the F-DCE-2 migration); reuse
        # the incident text already on the entry rather than invent one.
        "rationale": parsed["source"],
        "status_history": [],
    }

    principle = ArchPrincipleContent.model_validate(content)

    dumped = principle.model_dump(mode="json")
    assert dumped["statement"] == parsed["statement"]
    assert dumped["source"] == parsed["source"]
    assert dumped["status"] == "adopted"


def test_arch_principle_accepts_proposed_status_with_empty_history():
    content = valid_principle_content()
    content["status"] = "proposed"
    content["status_history"] = []

    validate_arch_content("principle", content)


def test_arch_principle_rejects_unknown_status():
    content = valid_principle_content()
    content["status"] = "deprecated"

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("principle", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("status",) in bad


@pytest.mark.parametrize("required_field", ["date", "status", "reason"])
def test_arch_principle_rejects_incomplete_status_history_entry(required_field):
    content = valid_principle_content()
    del content["status_history"][0][required_field]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("principle", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("status_history", 0, required_field) in missing


@pytest.mark.parametrize("blank_field", ["statement", "rationale"])
def test_arch_principle_rejects_blank_required_strings(blank_field):
    content = valid_principle_content()
    content[blank_field] = "   "

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("principle", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (blank_field,) in bad


@pytest.mark.parametrize("required_field", ["statement", "rationale"])
def test_arch_principle_rejects_missing_required_strings(required_field):
    content = valid_principle_content()
    del content[required_field]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("principle", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


@pytest.mark.parametrize(
    "measurement_field",
    ["measured_at", "verdict", "applied_count", "conformance"],
)
def test_arch_principle_rejects_top_level_measurement_fields(measurement_field):
    content = valid_principle_content()
    content[measurement_field] = "pass"

    with pytest.raises(Exception):
        validate_arch_content("principle", content)


@pytest.mark.parametrize(
    "measurement_field",
    ["measured_at", "verdict", "applied_count", "conformance"],
)
def test_arch_principle_rejects_status_history_measurement_fields(measurement_field):
    content = valid_principle_content()
    content["status_history"][0][measurement_field] = "pass"

    with pytest.raises(Exception):
        validate_arch_content("principle", content)


def test_arch_principle_accepts_additive_fields():
    content = valid_principle_content()
    content["notes"] = "seeded 2026-08-17"
    content["status_history"][0]["pr_ref"] = "#425"

    validate_arch_content("principle", content)


def test_unmodelled_arch_type_passes_through_unchanged():
    content = {
        "anything": {"can": ["still", "write"]},
        "missing": "all modelled EA fields",
    }
    before = deepcopy(content)

    validate_bead_content("arch", "person", content)

    assert content == before


@pytest.mark.parametrize("required_field", ["observed_at", "workload"])
def test_arch_observation_schema_rejects_missing_required_content(required_field):
    content = valid_observation_content()
    del content[required_field]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("observation", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


@pytest.mark.parametrize(
    "judgment_field",
    [
        "time_disposition",
        "business_value",
        "technical_health",
        "state",
        "lifecycle_state",
    ],
)
def test_arch_observation_schema_rejects_judgment_fields(judgment_field):
    content = valid_observation_content()
    content[judgment_field] = "invest"

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("observation", content)

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (judgment_field,) in bad


def test_arch_observation_schema_rejects_missing_workload_identity():
    content = valid_observation_content()
    del content["workload"]["name"]

    with pytest.raises(Exception) as exc_info:
        validate_arch_content("observation", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("workload", "name") in missing


def test_arch_observation_schema_rejects_ready_count_above_replica_count():
    content = valid_observation_content()
    content["replicas"] = 1
    content["ready_replicas"] = 2

    with pytest.raises(Exception):
        validate_arch_content("observation", content)


def test_arch_observation_schema_accepts_optional_ref_for_reviewed_metrics():
    content = valid_observation_content()
    content["ref"] = "obs.app-substrate.prod-ready"

    validate_arch_content("observation", content)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")

    async def no_cache_invalidation(_namespace: str) -> None:
        return None

    monkeypatch.setattr(src.routes, "invalidate_cache", no_cache_invalidation)
    a = FastAPI()
    a.include_router(router)
    return a


def test_arch_capability_post_returns_422_before_write(app):
    async def unused_db():
        yield None

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "capability",
                "state": "active",
                "content": {"name": "Not enough"},
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    missing = {tuple(error["loc"]) for error in detail["errors"]}
    assert ("ref",) in missing
    assert ("evidence",) in missing


@pytest.mark.parametrize(
    ("mutate", "expected_loc"),
    [
        (lambda content: content.update({"id": "PC-SUB-3"}), ("id",)),
        (lambda content: content.update({"status": "unverified"}), ("status",)),
        (
            lambda content: content["acceptance_criteria"][0].pop("then"),
            ("acceptance_criteria", 0, "then"),
        ),
        (lambda content: content.update({"measured_at": "2026-08-14"}), ()),
        (
            lambda content: content["acceptance_criteria"][0].update(
                {"conformance": "pass"}
            ),
            ("acceptance_criteria", 0),
        ),
    ],
)
def test_arch_requirement_post_returns_422_before_write(app, mutate, expected_loc):
    async def unused_db():
        yield None

    content = valid_requirement_content()
    mutate(content)

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "requirement",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    bad = {tuple(error["loc"]) for error in detail["errors"]}
    assert expected_loc in bad


def test_arch_principle_valid_content_reaches_create_path(app):
    """The create-bead endpoint returns FastAPI's default 200 on success for
    every namespace/type (see test_arch_observation_valid_content_reaches_
    create_path) rather than a REST-purist 201. That is existing, namespace
    wide behavior this task's schema does not change.
    """

    class FakeSession:
        def add(self, obj):
            if obj.__class__.__name__ == "Bead" and obj.id is None:
                now = datetime.now(timezone.utc)
                obj.id = uuid4()
                obj.created_at = now
                obj.updated_at = now

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

        async def rollback(self):
            return None

    async def fake_db():
        yield FakeSession()

    content = valid_principle_content()
    content["status"] = "proposed"
    content["status_history"] = []

    app.dependency_overrides[get_db] = fake_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "principle",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["type"] == "principle"


@pytest.mark.parametrize(
    ("mutate", "expected_loc"),
    [
        (lambda content: content.update({"status": "deprecated"}), ("status",)),
        (
            lambda content: content["status_history"][0].pop("reason"),
            ("status_history", 0, "reason"),
        ),
        (lambda content: content.update({"statement": "   "}), ("statement",)),
        (lambda content: content.update({"measured_at": "2026-08-14"}), ()),
    ],
)
def test_arch_principle_post_returns_422_before_write(app, mutate, expected_loc):
    async def unused_db():
        yield None

    content = valid_principle_content()
    mutate(content)

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "principle",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    bad = {tuple(error["loc"]) for error in detail["errors"]}
    assert expected_loc in bad


@pytest.mark.parametrize("required_field", ["observed_at", "workload"])
def test_arch_observation_post_returns_422_before_write(app, required_field):
    async def unused_db():
        yield None

    content = valid_observation_content()
    del content[required_field]

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "observation",
                "state": "observed",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    missing = {tuple(error["loc"]) for error in detail["errors"]}
    assert (required_field,) in missing


@pytest.mark.parametrize(
    "judgment_field",
    [
        "time_disposition",
        "business_value",
        "technical_health",
        "state",
        "lifecycle_state",
    ],
)
def test_arch_observation_post_returns_422_for_judgment_fields(app, judgment_field):
    async def unused_db():
        yield None

    content = valid_observation_content()
    content[judgment_field] = "invest"

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "observation",
                "state": "observed",
                "content": content,
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "arch_content_schema_violation"
    bad = {tuple(error["loc"]) for error in detail["errors"]}
    assert (judgment_field,) in bad


def test_arch_observation_valid_content_reaches_create_path(app):
    class FakeSession:
        def add(self, obj):
            if obj.__class__.__name__ == "Bead" and obj.id is None:
                now = datetime.now(timezone.utc)
                obj.id = uuid4()
                obj.created_at = now
                obj.updated_at = now

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

        async def rollback(self):
            return None

    async def fake_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = fake_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "observation",
                "state": "observed",
                "content": valid_observation_content(),
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["type"] == "observation"


# --- workload: the substrate and the conformance gate must agree ------------
#
# EA-S1 (#197) made `content.workload` required on every arch.application and
# `check_workload` errors when it is absent. FA-S23 (#201) was specced before
# EA-S1 existed, so ArchApplicationContent did not require it. For one commit
# the estate had two enforcement points that disagreed: the substrate accepted
# an application bead that ea-conformance.py rejected. Demonstrated, not
# assumed, before this was written. The softer gate wins by default, because
# it is the one the Stage 4 loader writes through.

def _app(**over):
    content = {
        "ref": "app.ghost", "name": "Ghost", "description": "d", "layer": "supply",
        "owner": "grant", "evidence": ["apps/substrate/"], "assessed_at": "2026-07-30",
        "build": "oss", "technical_health": "healthy", "business_value": "low",
        "time_disposition": "tolerate",
        "workload": {
            "runtime": "kubernetes",
            "objects": [{
                "cluster": "cluster-a", "namespace": "ns", "kind": "Deployment",
                "name": "x", "manifest": "infrastructure/k8s/x.yaml",
                "managed_by": "argocd",
            }],
        },
    }
    content.update(over)
    return content


def test_application_without_workload_is_rejected():
    """The divergence this file's last block exists to close."""
    content = _app()
    del content["workload"]
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", content)


def test_application_with_kubernetes_workload_is_accepted():
    validate_bead_content("arch", "application", _app())


def test_kubernetes_runtime_with_no_objects_is_rejected():
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(
            workload={"runtime": "kubernetes", "objects": []}))


def test_runtime_none_without_a_note_is_rejected():
    """`runtime: none` is a dated finding, not a shrug. Make it say what it saw."""
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(workload={"runtime": "none"}))


def test_runtime_none_with_a_note_is_accepted():
    validate_bead_content("arch", "application", _app(
        workload={"runtime": "none", "note": "no manifest, no pod — verified 2026-07-30"}))


def test_runtime_external_must_not_declare_objects():
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(workload={
            "runtime": "external", "note": "runs on Air",
            "objects": [{"cluster": "c", "namespace": "n", "kind": "Deployment",
                         "name": "x", "manifest": None, "managed_by": "none"}]}))


def test_unmanaged_object_without_a_manifest_is_expressible():
    """A GitOps orphan is a real state. app.itop was one for 40 days.

    The substrate must let the model *say* so — reporting it is the conformance
    gate's job, and it cannot report what it cannot store.
    """
    validate_bead_content("arch", "application", _app(workload={
        "runtime": "kubernetes",
        "objects": [{"cluster": "cluster-a", "namespace": "platform-itsm",
                     "kind": "Deployment", "name": "itop-app",
                     "manifest": None, "managed_by": "none"}]}))


def test_bad_managed_by_is_rejected():
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(workload={
            "runtime": "kubernetes",
            "objects": [{"cluster": "c", "namespace": "n", "kind": "Deployment",
                         "name": "x", "manifest": None, "managed_by": "vibes"}]}))


# --- workload.binding: declared, not just read (ea-metamodel.md §4.2) -------
#
# ArchWorkload was extra="allow" with no `binding` field at all, so
# ea-conformance.py and ea_reflect.py's seventeen and ten read sites agreed
# with the metamodel while the substrate — the only layer that could refuse a
# wrong spelling — accepted any of them. These tests fix that the schema now
# rejects what previously passed as an unvalidated extra field.

_EXTERNAL_BINDING = {
    "host": "host-b",
    "compose_path": "infrastructure/docker/host-b/docker-compose.pihole.yml",
    "service": "pihole",
}


def test_well_formed_binding_round_trips_unchanged():
    workload = ArchWorkload.model_validate({
        "runtime": "external",
        "binding": _EXTERNAL_BINDING,
    })
    assert workload.binding is not None
    assert workload.binding.model_dump() == _EXTERNAL_BINDING


def test_workload_with_no_binding_remains_valid():
    validate_bead_content("arch", "application", _app(workload={
        "runtime": "external", "note": "runs on Air"}))
    validate_bead_content("arch", "application", _app())  # kubernetes, no binding


def test_binding_with_misspelled_key_is_rejected():
    """``compose-path`` instead of ``compose_path`` — the required key is absent."""
    malformed = {"host": "host-b", "compose-path": "x", "service": "pihole"}
    with pytest.raises(ValidationError):
        ArchWorkload.model_validate({"runtime": "external", "binding": malformed})
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(workload={
            "runtime": "external", "binding": malformed}))


def test_binding_as_a_string_is_rejected():
    """A string where the structured object belongs — accepted today under extra=allow."""
    with pytest.raises(ValidationError):
        ArchWorkload.model_validate({
            "runtime": "external", "binding": "host-b:pihole",
        })
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "application", _app(workload={
            "runtime": "external", "binding": "host-b:pihole"}))


def test_runtime_none_must_not_declare_a_binding():
    """Mirrors ea-conformance.py's check_workload: a binding claims something
    runs somewhere, which contradicts 'runtime: none'."""
    with pytest.raises(ValidationError):
        ArchWorkload.model_validate({
            "runtime": "none", "note": "verified nowhere", "binding": _EXTERNAL_BINDING,
        })


# --- arch.release ------------------------------------------------------------
#
# The release is the level above the sprint: the objective a body of work is
# aimed at. These tests fix the two properties the release-notes generator and
# the balance report depend on — that an outcome id resolves to exactly one
# statement, and that a charter cannot quietly grow its own measurements.


def valid_release_content(**overrides) -> dict:
    content = {
        "ref": "R26.01",
        "name": "Autonomy earns its amendment",
        "objective": (
            "The code-health lane can merge unattended, and we can prove we can "
            "undo it."
        ),
        "sprints": ["33", "34", "35"],
        "outcomes": [
            {
                "id": "O-1",
                "statement": "A code-health change merges without a human at the gate.",
                "work_class": "enabling",
                "requirement_refs": ["PC-FAC-005/AC-1"],
            },
            {
                "id": "O-2",
                "statement": "A bad deploy is undone in under five minutes, measured.",
                "work_class": "risk",
                "requirement_refs": ["PC-TRU-001"],
            },
        ],
        "declared_balance": {"enabling": 60, "risk": 40},
        "opened_at": "2026-08-25",
    }
    content.update(overrides)
    return content


def test_release_content_validates():
    validate_bead_content("arch", "release", valid_release_content())


def test_release_ref_must_match_the_id_scheme():
    """`R1`, `R2.2` and `R3` are gitops-resilience *sprint* labels in this repo.

    A release id sharing that shape would make every cross-reference ambiguous,
    so the scheme is enforced rather than conventional.
    """
    for bad in ("R1", "R2.2", "26.01", "R2026.01", "REL-1"):
        with pytest.raises(ValidationError):
            validate_bead_content("arch", "release", valid_release_content(ref=bad))


def test_release_requires_at_least_one_outcome():
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "release", valid_release_content(outcomes=[]))


def test_duplicate_outcome_ids_are_rejected():
    """`outcome_ref` on a task must resolve to exactly one statement.

    Two outcomes sharing an id would attribute merged work to whichever the
    generator happened to see first.
    """
    outcomes = valid_release_content()["outcomes"]
    outcomes[1]["id"] = "O-1"
    with pytest.raises(ValidationError) as exc_info:
        validate_bead_content("arch", "release", valid_release_content(outcomes=outcomes))
    assert "duplicate outcome id" in str(exc_info.value)


def test_outcome_id_must_look_like_an_outcome_id():
    outcomes = valid_release_content()["outcomes"]
    outcomes[0]["id"] = "first"
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "release", valid_release_content(outcomes=outcomes))


def test_outcome_work_class_is_closed():
    outcomes = valid_release_content()["outcomes"]
    outcomes[0]["work_class"] = "chore"
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "release", valid_release_content(outcomes=outcomes))


def test_outcome_requirement_refs_must_be_well_formed():
    outcomes = valid_release_content()["outcomes"]
    outcomes[0]["requirement_refs"] = ["the factory one"]
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "release", valid_release_content(outcomes=outcomes))


def test_declared_balance_must_sum_to_one_hundred():
    """A mix that does not sum to 100 reads as a target and measures nothing."""
    with pytest.raises(ValidationError) as exc_info:
        validate_bead_content(
            "arch", "release",
            valid_release_content(declared_balance={"enabling": 60, "risk": 30}),
        )
    assert "must sum to 100" in str(exc_info.value)


def test_declared_balance_rejects_unknown_work_classes():
    with pytest.raises(ValidationError) as exc_info:
        validate_bead_content(
            "arch", "release",
            valid_release_content(declared_balance={"enabling": 60, "chore": 40}),
        )
    assert "chore" in str(exc_info.value)


def test_declared_balance_may_be_omitted_entirely():
    """A charter may decline to declare a mix. What it may not do is half-declare."""
    validate_bead_content(
        "arch", "release", valid_release_content(declared_balance={})
    )


def test_a_declared_class_with_no_outcome_is_rejected():
    """Declaring 20% security with no security outcome is an unhittable target.

    The balance report would show it permanently absent, and the charter would
    never say why.
    """
    with pytest.raises(ValidationError) as exc_info:
        validate_bead_content(
            "arch", "release",
            valid_release_content(
                declared_balance={"enabling": 50, "risk": 30, "security": 20}
            ),
        )
    assert "security" in str(exc_info.value)


def test_a_zero_share_needs_no_outcome():
    validate_bead_content(
        "arch", "release",
        valid_release_content(
            declared_balance={"enabling": 60, "risk": 40, "security": 0}
        ),
    )


@pytest.mark.parametrize(
    "key", ["conformance", "verdict", "measured_at", "measured_revision", "actual_balance"]
)
def test_release_content_must_not_embed_measurements(key):
    """A charter states intent. What happened is an arch.observation.

    Same line ArchRequirementContent draws for dated conformance verdicts, and
    Pillar 10 draws for execution bookkeeping — this would be the third place
    to keep in sync.
    """
    with pytest.raises(ValidationError) as exc_info:
        validate_bead_content("arch", "release", valid_release_content(**{key: "anything"}))
    assert key in str(exc_info.value)


def test_release_objective_must_not_be_blank():
    with pytest.raises(ValidationError):
        validate_bead_content("arch", "release", valid_release_content(objective="   "))

def test_every_committed_charter_validates():
    """The real docs/releases/*.json, against the real model.

    A charter is authored by a person and reviewed by PR, so nothing else would
    catch one hand-edited into a shape the substrate will refuse — and the
    refusal would otherwise surface on the Temporal schedule at 3am, as a 422
    in a loader log, rather than on the PR that introduced it.
    """
    root = Path(__file__).resolve().parents[3]
    charters = sorted((root / "docs" / "releases").glob("*.json"))
    assert charters, "docs/releases/ holds no charter; R26.01 should be there"

    for path in charters:
        content = json.loads(path.read_text())
        try:
            validate_bead_content("arch", "release", content)
        except Exception as exc:  # pragma: no cover - the message is the point
            raise AssertionError(f"{path.name} does not validate: {exc}") from None
