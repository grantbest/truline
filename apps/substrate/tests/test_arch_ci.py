"""``arch.ci`` — the technology-layer CI carrier (PC-ASR-006/AC-2, PC-ASR-007/AC-2).

ea-metamodel.md §7: Manage Technical Services opens 2026-08-22, populated by
observation only. Every other ``arch.*`` content model defaults
``source_class`` to ``authored`` and accepts all three values in
``SOURCE_CLASSES`` — ``ArchCiContent`` instead locks the field to
``Literal["observed"]``, so a hand-created record (written the way every
other arch type naturally is: omit ``source_class``, or declare
``"authored"``) is rejected by the schema itself at ``POST /beads``, before
any ``PATCH``-time ownership question is ever reached. See ``.factory/design.md``
§2 for why this is the schema layer's job, not a new route check.

At HEAD (before this change) there is no ``"ci"`` entry in ``ARCH_TYPE_SCHEMAS``,
so every test in this file fails: the import itself fails, and
``validate_arch_content("ci", ...)`` is a silent no-op (unmodelled types pass
through unchanged) rather than rejecting anything.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import src.cache
from src.database import get_db
from src.routes import router
from src.schemas import ARCH_TYPE_SCHEMAS, ArchCiContent, validate_arch_content

HEADERS = {"x-api-key": "test-key"}


def valid_ci_content(**over) -> dict:
    content = {
        "ref": "ci.cluster-a.platform-substrate.deployment.substrate",
        "ci_kind": "workload",
        "cluster": "cluster-a",
        "namespace": "platform-substrate",
        "kind": "Deployment",
        "name": "substrate",
        "source_class": "observed",
    }
    content.update(over)
    return content


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_ci_type_is_registered_in_arch_type_schemas():
    assert ARCH_TYPE_SCHEMAS["ci"] is ArchCiContent


def test_valid_observed_ci_content_is_accepted():
    validate_arch_content("ci", valid_ci_content())


@pytest.mark.parametrize("ci_kind", ["workload", "namespace", "database"])
def test_ci_content_accepts_each_declared_kind(ci_kind):
    validate_arch_content("ci", valid_ci_content(ci_kind=ci_kind))


def test_ci_content_rejects_unknown_ci_kind():
    with pytest.raises(ValidationError):
        validate_arch_content("ci", valid_ci_content(ci_kind="pod"))


@pytest.mark.parametrize("required_field", ["ref", "ci_kind", "cluster", "namespace", "kind", "name"])
def test_ci_content_rejects_missing_required_field(required_field):
    content = valid_ci_content()
    del content[required_field]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("ci", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert (required_field,) in missing


def test_ci_content_rejects_blank_required_strings():
    content = valid_ci_content(name="   ")

    with pytest.raises(ValidationError):
        validate_arch_content("ci", content)


def test_ci_content_is_closed_shape():
    content = valid_ci_content(unexpected_field="surprise")

    with pytest.raises(ValidationError):
        validate_arch_content("ci", content)


# ---------------------------------------------------------------------------
# source_class is locked to "observed" — the schema-level rejection of a
# hand-created record.
# ---------------------------------------------------------------------------


def test_ci_content_rejects_missing_source_class():
    content = valid_ci_content()
    del content["source_class"]

    with pytest.raises(ValidationError) as exc_info:
        validate_arch_content("ci", content)

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("source_class",) in missing


@pytest.mark.parametrize("hand_authored_value", ["authored", "derived"])
def test_ci_content_rejects_non_observed_source_class(hand_authored_value):
    """The values every other arch type's source_class accepts are rejected here."""
    content = valid_ci_content(source_class=hand_authored_value)

    with pytest.raises(ValidationError):
        validate_arch_content("ci", content)


def test_ci_content_accepts_only_observed_source_class():
    content = valid_ci_content(source_class="observed")
    validate_arch_content("ci", content)


# ---------------------------------------------------------------------------
# HTTP: POST /beads — a hand-created record is rejected by the source-class
# contract before it is ever written.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def test_create_bead_rejects_hand_created_ci_record_missing_source_class(app):
    async def unused_db():
        yield None

    content = valid_ci_content()
    del content["source_class"]

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "ci",
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


def test_create_bead_rejects_hand_created_ci_record_declaring_authored(app):
    async def unused_db():
        yield None

    content = valid_ci_content(source_class="authored")

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "ci",
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
# HTTP: PATCH /beads/{id} — the observed-only ownership check, same contract
# as every other observed arch type (test_arch_source_class.py).
# ---------------------------------------------------------------------------


def _fake_bead(content: dict, created_by: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace="arch",
        type="ci",
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


def test_update_bead_rejects_observed_ci_written_by_non_observer(app):
    bead = _fake_bead(valid_ci_content(), created_by="factory-dispatcher/ea-observer")
    session = _FakeSession(bead)

    new_content = valid_ci_content(name="renamed")

    response = _client(app, session).patch(
        f"/beads/{bead.id}",
        json={"content": new_content, "created_by": "grant"},
        headers=HEADERS,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_ownership_violation"
    assert detail["owning_class"] == "observed"
    assert session.committed is False


def test_update_bead_allows_observed_ci_written_by_its_observer(app):
    bead = _fake_bead(valid_ci_content(), created_by="factory-dispatcher/ea-observer")
    session = _FakeSession(bead)

    new_content = valid_ci_content(name="renamed")

    response = _client(app, session).patch(
        f"/beads/{bead.id}",
        json={"content": new_content, "created_by": "factory-dispatcher/ea-observer"},
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert session.committed is True


# ---------------------------------------------------------------------------
# HTTP: POST /beads — the same ownership check applied at creation. A create
# names its own content.ref (migration 0006's identity key); a ref an existing
# bead already owns makes this create an overwrite of that fact, refused the
# same way the PATCH guard above already refuses one.
# ---------------------------------------------------------------------------


class _FakeCreateSession:
    """Answers the create path's ref lookup, then behaves like a real session
    for a create that reaches the write (mirrors test_arch_schema.py's
    FakeSession id/timestamp assignment on ``add`` for a successful POST).
    """

    def __init__(self, existing: Optional[SimpleNamespace]):
        self.existing = existing
        self.committed = False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, obj):
        if obj.__class__.__name__ == "Bead" and obj.id is None:
            now = datetime.now(timezone.utc)
            obj.id = uuid4()
            obj.created_at = now
            obj.updated_at = now

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None

    async def rollback(self):
        return None


def _post_ci(client: TestClient, content: dict, created_by: str):
    return client.post(
        "/beads",
        headers=HEADERS,
        json={
            "namespace": "arch",
            "type": "ci",
            "state": "active",
            "content": content,
            "trust_tier": "system",
            "created_by": created_by,
        },
    )


def test_create_bead_rejects_observed_ci_ref_already_owned_by_another_writer(app):
    existing = _fake_bead(valid_ci_content(), created_by="factory-dispatcher/ea-observer")
    session = _FakeCreateSession(existing)
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_ci(TestClient(app), valid_ci_content(name="impersonated"), "attacker")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_ownership_violation"
    assert detail["owning_class"] == "observed"
    assert detail["rejected_writer"] == "attacker"
    assert detail["bead_id"] == str(existing.id)
    assert session.committed is False


def test_create_bead_ownership_guard_does_not_fire_for_the_owning_observer(app):
    """Pins ONLY that the ownership guard passes the owner through: the fake
    session models no ``idx_unique_arch_ref`` (migration 0006), so the 200
    here is an artifact of the double -- production refuses this same POST
    at the unique index regardless of writer. The guard must not be the
    thing that refuses the owner; what happens after the guard is the
    index's business, not this test's.
    """
    existing = _fake_bead(valid_ci_content(), created_by="factory-dispatcher/ea-observer")
    session = _FakeCreateSession(existing)
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_ci(
            TestClient(app),
            valid_ci_content(name="refreshed"),
            "factory-dispatcher/ea-observer",
        )
    finally:
        app.dependency_overrides.clear()

    # Not a 409 from the ownership guard -- that is the entire claim. The
    # 200 is the double's, not production's (see docstring).
    assert response.status_code == 200
    assert session.committed is True


def test_create_bead_rejects_impersonated_observed_ci_at_a_fresh_ref(app):
    """OPS-86 inverts this vector: #715 (OPS-78) closed the overwrite half of
    source_class impersonation (a ref an existing bead already owns), but a
    fresh ref -- no existing bead for the ownership check to compare against
    -- still let any writer mint an "observed" fact by simply declaring the
    class. This pins that vector closed: "grant" is not enrolled for
    "observed" in ``bead_rules.SOURCE_CLASS_WRITERS``, so the create is
    refused before it ever reaches the database, not merely on a later edit.
    """
    session = _FakeCreateSession(existing=None)
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_ci(TestClient(app), valid_ci_content(), "grant")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "observed"
    assert detail["rejected_writer"] == "grant"
    assert detail["enrollment_path"]
    assert session.committed is False


def test_create_bead_allows_observed_ci_at_a_fresh_ref_from_an_enrolled_observer(app):
    """The vector above closed for an unenrolled writer; an enrolled one
    (the EA observation path's own identity) must still be able to mint the
    very first observation of a fresh CI -- this bead adds admission, it
    does not break the writers it was enrolled for.
    """
    session = _FakeCreateSession(existing=None)
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_ci(
            TestClient(app), valid_ci_content(), "factory-dispatcher/ea-observer"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert session.committed is True
