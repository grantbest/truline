"""``arch.*`` ``source_class`` — the reconciliation contract's precedence half.

the Operator's directive (2026-08-22): architecture records are automated records based on
the state of the managed information, and every ``arch.*`` record must carry
``content.source_class`` in ``{authored, derived, observed}``. The class decides who
may overwrite the fact:

* ``authored`` — never overwritable by an automated (factory-agent) writer.
* ``derived`` — overwritable only by the identity that declared itself the deriver
  (the bead's own ``created_by``, at creation).
* ``observed`` — overwritable only by that same declared observer identity.

Identification already exists (``content.ref``, migration 0006). This is the
precedence half, enforced once at the write path (``PATCH /beads/{id}``) rather than
re-implemented per writer.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
import src.schemas as schemas
from src.database import get_db
from src.routes import router
from src.schemas import (
    ARCH_TYPE_SCHEMAS,
    HUMAN_EXEMPTION,
    check_source_class_admission,
    validate_arch_content,
    validate_bead_content,
)

from test_arch_schema import (
    valid_application_content,
    valid_capability_content,
    valid_information_object_content,
    valid_observation_content,
    valid_principle_content,
    valid_requirement_content,
    valid_service_content,
)

HEADERS = {"x-api-key": "test-key"}

SOURCE_CLASSES = ("authored", "derived", "observed")


def valid_change_content() -> dict:
    return {
        "change_type": "standard",
        "summary": "Roll out the source_class reconciliation contract.",
        "applications": ["app.substrate"],
        "evidence": ["apps/substrate/src/schemas.py"],
    }


VALID_CONTENT_BY_TYPE = {
    "capability": valid_capability_content,
    "application": valid_application_content,
    "service": valid_service_content,
    "information_object": valid_information_object_content,
    "requirement": valid_requirement_content,
    "principle": valid_principle_content,
    "observation": valid_observation_content,
    "change": valid_change_content,
}


# ---------------------------------------------------------------------------
# Schema-level: every arch.* content model accepts the enum and defaults it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bead_type", sorted(VALID_CONTENT_BY_TYPE))
def test_arch_content_defaults_source_class_to_authored_when_absent(bead_type):
    content = VALID_CONTENT_BY_TYPE[bead_type]()
    assert "source_class" not in content

    model_cls = ARCH_TYPE_SCHEMAS[bead_type]
    validated = model_cls.model_validate(content)

    assert validated.source_class == "authored"


@pytest.mark.parametrize("bead_type", sorted(VALID_CONTENT_BY_TYPE))
@pytest.mark.parametrize("source_class", SOURCE_CLASSES)
def test_arch_content_accepts_each_declared_source_class(bead_type, source_class):
    content = VALID_CONTENT_BY_TYPE[bead_type]()
    content["source_class"] = source_class

    validate_arch_content(bead_type, content)


@pytest.mark.parametrize("bead_type", sorted(VALID_CONTENT_BY_TYPE))
def test_arch_content_rejects_unknown_source_class_naming_allowed_set(bead_type):
    content = VALID_CONTENT_BY_TYPE[bead_type]()
    content["source_class"] = "guessed"

    with pytest.raises(Exception) as exc_info:
        validate_arch_content(bead_type, content)

    messages = " ".join(error["msg"] for error in exc_info.value.errors())
    for allowed in SOURCE_CLASSES:
        assert allowed in messages, messages


def test_validate_bead_content_enforces_source_class_for_unmodelled_arch_type():
    """Defense in depth: a future/undeclared arch type still gets the enum."""
    with pytest.raises(Exception) as exc_info:
        validate_bead_content("arch", "widget", {"source_class": "guessed"})

    messages = " ".join(error["msg"] for error in exc_info.value.errors())
    for allowed in SOURCE_CLASSES:
        assert allowed in messages, messages


def test_validate_bead_content_accepts_unmodelled_arch_type_without_source_class():
    validate_bead_content("arch", "widget", {"anything": "still writes"})


# ---------------------------------------------------------------------------
# HTTP: POST /beads surfaces the 422 for an unknown value.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def test_create_bead_rejects_unknown_source_class_422_naming_allowed_set(app):
    async def unused_db():
        yield None

    content = valid_capability_content()
    content["source_class"] = "guessed"

    app.dependency_overrides[get_db] = unused_db
    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "capability",
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
    messages = " ".join(error["msg"] for error in detail["errors"])
    for allowed in SOURCE_CLASSES:
        assert allowed in messages, messages


# ---------------------------------------------------------------------------
# HTTP: PATCH /beads/{id} — the ownership check.
# ---------------------------------------------------------------------------


def _fake_bead(namespace: str, type_: str, content: dict, created_by: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace=namespace,
        type=type_,
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
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj):
        return None


def _client(app, session) -> TestClient:
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def _patch(client: TestClient, bead_id, body: dict):
    return client.patch(f"/beads/{bead_id}", json=body, headers=HEADERS)


def test_update_bead_rejects_authored_fact_overwritten_by_agent_writer(app):
    existing = valid_capability_content()
    existing["source_class"] = "authored"
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["name"] = "Changed by an agent"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "claude"}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_ownership_violation"
    assert detail["owning_class"] == "authored"
    assert detail["rejected_writer"] == "claude"
    assert detail["bead_id"] == str(bead.id)
    assert session.committed is False


def test_update_bead_allows_authored_fact_overwritten_by_a_different_human(app):
    existing = valid_capability_content()
    existing["source_class"] = "authored"
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["name"] = "Changed by a different human"

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "someone-else"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_historical_bead_missing_source_class_by_automated_writer_requires_declaration(
    app,
):
    """D8(c): an automated writer rewriting a bead that never declared its own
    ``source_class`` gets a refusal distinct from a genuine authored-fact
    violation -- naming the missing field and saying the bead must be
    declared first, rather than the generic ``owning_class: authored`` detail
    a real human-authored fact gets. #767's naive predicate fix collapsed
    this into the same 409 a permanent lock produces, giving the reconciler
    nothing to act on; this is the distinguished refusal that replaces it.
    """
    existing = valid_capability_content()
    assert "source_class" not in existing
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["name"] = "An agent tries to slip a change in"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "codex"}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_declaration_required"
    assert detail["missing_field"] == "source_class"
    assert "declared" in detail["message"]
    assert detail["rejected_writer"] == "codex"
    assert session.committed is False


def test_update_bead_human_writer_may_rewrite_bead_missing_source_class(app):
    """The positive half of the same demonstration: a genuinely human writer
    rewriting a bead that never declared its own source_class is unaffected
    -- the declaration-required refusal only ever fires for an automated
    writer.
    """
    existing = valid_capability_content()
    assert "source_class" not in existing
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["name"] = "A human corrects this record"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "grant"}
    )

    assert response.status_code == 200
    assert session.committed is True


# ---------------------------------------------------------------------------
# is_automated_writer — the predicate fix D8(c) lands: factory-dispatcher/*
# is automated regardless of whether the tail after the slash also happens to
# split into an AGENT_PROVENANCE_WORKERS part; loader scripts, which write
# under their own bare identity rather than that namespace, stay human.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "writer",
    [
        "factory-dispatcher/ea-observer",
        "factory-dispatcher/change-apply",
        "factory-dispatcher/worker-revision-drift",
        # Not (yet) an enrolled writer for any class -- still automated by
        # namespace alone, which is the point: admission/enrollment is a
        # separate question from whether this identity is a human.
        "factory-dispatcher/not-yet-enrolled",
    ],
)
def test_is_automated_writer_recognizes_factory_dispatcher_namespace(writer):
    assert schemas.is_automated_writer(writer) is True


@pytest.mark.parametrize(
    "writer",
    ["ea-load", "requirements-load", "principles-sync", "release-load"],
)
def test_is_automated_writer_still_treats_loader_scripts_as_human(writer):
    """Loader scripts mirror human-authored, source-controlled docs and write
    under their own bare CREATED_BY, never the factory-dispatcher/* namespace
    -- demonstrated here by executing the predicate against their actual
    constants (scripts/ea-load.py, scripts/requirements-load.py,
    scripts/principles_sync.py, scripts/release-load.py), not just asserted.
    """
    assert schemas.is_automated_writer(writer) is False


def test_update_bead_rejects_derived_fact_written_by_non_deriver(app):
    existing = valid_capability_content()
    existing["source_class"] = "derived"
    bead = _fake_bead("arch", "capability", existing, created_by="ea-derive-bot")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["source_class"] = "derived"
    new_content["name"] = "Changed by someone who isn't the deriver"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "grant"}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["owning_class"] == "derived"
    assert detail["rejected_writer"] == "grant"
    assert session.committed is False


def test_update_bead_allows_derived_fact_written_by_its_declared_deriver(app):
    existing = valid_capability_content()
    existing["source_class"] = "derived"
    bead = _fake_bead("arch", "capability", existing, created_by="ea-derive-bot")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["source_class"] = "derived"
    new_content["name"] = "Refreshed by the deriver"

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "ea-derive-bot"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_rejects_observed_fact_written_by_non_observer(app):
    existing = valid_observation_content()
    existing["source_class"] = "observed"
    bead = _fake_bead("arch", "observation", existing, created_by="cluster-reflector")
    session = _FakeSession(bead)

    new_content = valid_observation_content()
    new_content["source_class"] = "observed"
    new_content["replicas"] = 3
    new_content["ready_replicas"] = 3

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "grant"}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["owning_class"] == "observed"
    assert detail["rejected_writer"] == "grant"


def test_update_bead_allows_observed_fact_written_by_its_observer(app):
    existing = valid_observation_content()
    existing["source_class"] = "observed"
    bead = _fake_bead("arch", "observation", existing, created_by="cluster-reflector")
    session = _FakeSession(bead)

    new_content = valid_observation_content()
    new_content["source_class"] = "observed"
    new_content["replicas"] = 3
    new_content["ready_replicas"] = 3

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "cluster-reflector"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_relabeling_source_class_is_itself_governed(app):
    """Attempting to relabel a fact's class is an overwrite of the existing fact."""
    existing = valid_capability_content()
    existing["source_class"] = "authored"
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["source_class"] = "derived"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "claude"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["owning_class"] == "authored"


def test_update_bead_non_arch_namespace_bypasses_ownership_check(app):
    bead = _fake_bead(
        "ops", "misc", {"source_class": "authored", "note": "x"}, created_by="grant"
    )
    session = _FakeSession(bead)

    response = _patch(
        _client(app, session), bead.id, {"content": {"note": "changed"}, "created_by": "claude"}
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_state_only_patch_does_not_trigger_ownership_check(app):
    existing = valid_capability_content()
    existing["source_class"] = "authored"
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    response = _patch(
        _client(app, session), bead.id, {"confidence": 0.5, "created_by": "claude"}
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_rejects_unknown_source_class_422_before_write(app):
    existing = valid_capability_content()
    existing["source_class"] = "authored"
    bead = _fake_bead("arch", "capability", existing, created_by="grant")
    session = _FakeSession(bead)

    new_content = valid_capability_content()
    new_content["source_class"] = "guessed"

    response = _patch(
        _client(app, session), bead.id, {"content": new_content, "created_by": "grant"}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    messages = " ".join(error["msg"] for error in detail["errors"])
    for allowed in SOURCE_CLASSES:
        assert allowed in messages, messages
    assert session.committed is False


# ---------------------------------------------------------------------------
# OPS-86 — writer -> source_class admission. Ownership (above) governs who
# may OVERWRITE a fact that already carries a class; admission governs who
# may CLAIM one in the first place, for a fresh ref (or no ref) where no
# existing bead exists for an ownership check to compare against at all.
# ---------------------------------------------------------------------------


def test_check_source_class_admission_rejects_unenrolled_writer_for_derived():
    violation = check_source_class_admission("derived", "attacker")
    assert violation is not None
    assert violation.owning_class == "derived"
    assert violation.rejected_writer == "attacker"


@pytest.mark.parametrize(
    "writer",
    ["factory-dispatcher/change-apply", "requirements-load"],
)
def test_check_source_class_admission_allows_each_enrolled_derived_writer(writer):
    assert check_source_class_admission("derived", writer) is None


@pytest.mark.parametrize(
    "writer",
    sorted(
        w
        for w in schemas.SOURCE_CLASS_WRITERS["derived"]
        if w.startswith("factory-dispatcher/")
    ),
)
def test_check_source_class_admission_allows_every_enrolled_factory_dispatcher_derived_writer(
    writer,
):
    """Enumerated from the map, not a hand list: every factory-dispatcher/*
    identity SOURCE_CLASS_WRITERS['derived'] enrolls must be admitted -- this
    fails automatically if a future enrollment PR adds a writer this test
    doesn't already know about, rather than passing silently.
    """
    assert check_source_class_admission("derived", writer) is None


def test_check_source_class_admission_rejects_unenrolled_factory_dispatcher_writer_for_derived():
    """An identity merely shaped like an enrolled writer -- same
    'factory-dispatcher/' prefix -- is not admitted by the prefix alone; it
    must be the exact enrolled identity.
    """
    writer = "factory-dispatcher/not-actually-enrolled"
    assert writer not in schemas.SOURCE_CLASS_WRITERS["derived"]

    violation = check_source_class_admission("derived", writer)

    assert violation is not None
    assert violation.owning_class == "derived"
    assert violation.rejected_writer == writer


@pytest.mark.parametrize(
    "writer",
    [
        "factory-dispatcher/ea-observer",
        "factory-dispatcher/worker-revision-drift",
        # doctrine_registry_view.py:62 at filing.
        "factory-dispatcher/doctrine-registry-view",
        # CREATED_BY at deployed_revision_drift.py:47 on main c729c15; :75 on the preserved head 58eb4e0.
        "factory-dispatcher/deployed-revision-drift",
        # probe_console_surface.py's CREATED_BY, R26.09/O-7.
        "factory-dispatcher/probe-console-surface",
    ],
)
def test_check_source_class_admission_allows_each_enrolled_observed_writer(writer):
    assert check_source_class_admission("observed", writer) is None


def test_check_source_class_admission_rejects_unenrolled_writer_for_observed():
    violation = check_source_class_admission("observed", "attacker")
    assert violation is not None
    assert violation.owning_class == "observed"


def test_check_source_class_admission_human_exemption_allows_non_agent_writer():
    """``authored`` carries the human exemption, not an enumerated allowlist:
    any writer that isn't a recognized automated agent may claim it."""
    assert check_source_class_admission("authored", "grant") is None
    assert check_source_class_admission("authored", "someone-nobody-enrolled") is None


@pytest.mark.parametrize("agent_writer", ["claude", "codex", "gemini", "factory-dispatcher/claude"])
def test_check_source_class_admission_human_exemption_rejects_agent_writer(agent_writer):
    violation = check_source_class_admission("authored", agent_writer)
    assert violation is not None
    assert violation.owning_class == "authored"
    assert violation.rejected_writer == agent_writer


def test_source_class_writers_declares_authored_as_the_human_exemption_sentinel():
    assert schemas.SOURCE_CLASS_WRITERS["authored"] is HUMAN_EXEMPTION


def test_check_source_class_admission_empty_enrollment_refuses_every_claimant(monkeypatch):
    """A class whose enrollment is emptied refuses every writer -- including
    its own real writer -- until a PR re-enrolls one. No writer is admitted
    to a governed class by default.
    """
    monkeypatch.setitem(schemas.SOURCE_CLASS_WRITERS, "derived", frozenset())

    violation = check_source_class_admission("derived", "requirements-load")

    assert violation is not None
    assert violation.owning_class == "derived"
    assert violation.rejected_writer == "requirements-load"


# ---------------------------------------------------------------------------
# HTTP: POST /beads — admission fires for a fresh ref (or no ref), the vector
# the ownership guard structurally cannot see (no existing bead to compare
# the writer against). arch.change is the live "derived" writer's own type.
# ---------------------------------------------------------------------------


def valid_change_content_with_ref(ref: str) -> dict:
    content = valid_change_content()
    content["ref"] = ref
    content["source_class"] = "derived"
    return content


class _FakeCreateSession:
    """Answers the create path's ref lookup (``None`` -- a fresh ref), then
    behaves like a real session for a create that reaches the write.
    """

    def __init__(self, existing: Optional[SimpleNamespace] = None):
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


def _post_change(client: TestClient, content: dict, created_by: str):
    return client.post(
        "/beads",
        headers=HEADERS,
        json={
            "namespace": "arch",
            "type": "change",
            "state": "active",
            "content": content,
            "trust_tier": "system",
            "created_by": created_by,
        },
    )


def test_create_bead_rejects_impersonated_derived_change_at_a_fresh_ref(app):
    """OPS-86: a fresh ref -- no existing bead -- previously let any writer
    mint a "derived" fact merely by declaring the class. "attacker" is not
    enrolled for "derived", so this is refused before it reaches the database.
    """
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_change(
            TestClient(app),
            valid_change_content_with_ref("ch.impersonated.derived"),
            "attacker",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "derived"
    assert detail["rejected_writer"] == "attacker"
    assert detail["enrollment_path"]
    assert session.committed is False


def test_create_bead_allows_derived_change_at_a_fresh_ref_from_its_enrolled_deriver(app):
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_change(
            TestClient(app),
            valid_change_content_with_ref("ch.legitimate.derived"),
            "factory-dispatcher/change-apply",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert session.committed is True


def test_create_bead_rejects_agent_writer_claiming_authored_at_a_fresh_ref(app):
    """The human exemption mirrors check_source_class_ownership's existing
    authored rule at creation too: an automated (factory-agent) writer may
    not mint even the default "authored" class.
    """
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    content = valid_capability_content()
    content["ref"] = "cap.agent-authored.fresh"

    try:
        response = TestClient(app).post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "capability",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": "claude",
                # BeadCreate requires a complete provenance record for any
                # agent-authored bead before the request body even validates;
                # this must be present so the request reaches the admission
                # gate this test targets, rather than failing earlier on that
                # unrelated contract.
                "provenance": {
                    "worker": "claude",
                    "model": "claude-test",
                    "prompt_ref": "dev.task/test",
                    "tokens": 1,
                    "cost_usd": 0.0,
                    "duration_s": 0.1,
                },
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "authored"
    assert detail["rejected_writer"] == "claude"


# ---------------------------------------------------------------------------
# Closing the two-request laundering gap: a class-changing write (PATCH, or a
# POST that names an existing ref) must re-run the same admission gate the
# POST fresh-mint path runs, not just the ownership check. Ownership alone
# waves an "authored" relabel through for any non-automated writer regardless
# of the class it's relabeling to -- that's the vector this closes.
# ---------------------------------------------------------------------------


class _FakeTwoRequestSession:
    """One persistent bead across a POST then a PATCH against it -- the
    literal two-request laundering fixture: POST mints the fact as
    "authored" (always admitted -- the human exemption), then PATCH attempts
    to relabel that same fact's class, both from the same writer.
    """

    def __init__(self):
        self.bead: Optional[SimpleNamespace] = None
        self.committed = False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        if obj.__class__.__name__ == "Bead" and self.bead is None:
            now = datetime.now(timezone.utc)
            obj.id = uuid4()
            obj.created_at = now
            obj.updated_at = now
            self.bead = obj

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None

    async def rollback(self):
        return None


def test_two_request_laundering_authored_post_then_observed_patch_is_refused(app):
    """The fixture the acceptance criteria names: an unenrolled writer cannot
    do in two requests what OPS-86's POST gate already refuses in one. POST
    mints an "authored" capability (open to any non-automated writer); a
    follow-up PATCH from that same writer relabeling it to "observed" must
    hit the identical admission refusal a direct POST of "observed" would.
    """
    writer = "an-unenrolled-writer"
    session = _FakeTwoRequestSession()
    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app)

    try:
        content = valid_capability_content()
        content["source_class"] = "authored"
        post_response = client.post(
            "/beads",
            headers=HEADERS,
            json={
                "namespace": "arch",
                "type": "capability",
                "state": "active",
                "content": content,
                "trust_tier": "system",
                "created_by": writer,
            },
        )
        assert post_response.status_code == 200

        bead_id = post_response.json()["id"]
        relabeled = valid_capability_content()
        relabeled["source_class"] = "observed"
        patch_response = client.patch(
            f"/beads/{bead_id}",
            headers=HEADERS,
            json={"content": relabeled, "created_by": writer},
        )
    finally:
        app.dependency_overrides.clear()

    assert patch_response.status_code == 409
    detail = patch_response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "observed"
    assert detail["rejected_writer"] == writer
    assert detail["enrollment_path"]


def test_update_bead_class_preserving_patch_from_owner_is_unaffected(app):
    """Non-regression: a class-preserving PATCH from the fact's own owner
    only ever needed the ownership check, and still does -- the new
    admission re-check fires strictly on a class CHANGE.
    """
    existing = valid_observation_content()
    existing["source_class"] = "observed"
    bead = _fake_bead("arch", "observation", existing, created_by="cluster-reflector")
    session = _FakeSession(bead)

    new_content = valid_observation_content()
    new_content["source_class"] = "observed"
    new_content["replicas"] = 7
    new_content["ready_replicas"] = 7

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "cluster-reflector"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_owner_cannot_relabel_own_bead_into_a_class_it_is_not_enrolled_for(app):
    """Ownership alone is not enough once the class itself is changing: the
    fact's own declared owner is refused if it isn't enrolled for the class
    it's relabeling into -- demonstrating the negative half of "enrolled
    writers may correct their own beads' class within their enrollment".
    """
    existing = valid_change_content()
    existing["ref"] = "ch.owned-by-change-apply"
    existing["source_class"] = "derived"
    bead = _fake_bead("arch", "change", existing, created_by="factory-dispatcher/change-apply")
    session = _FakeSession(bead)

    relabeled = valid_change_content()
    relabeled["ref"] = "ch.owned-by-change-apply"
    relabeled["source_class"] = "observed"

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": relabeled, "created_by": "factory-dispatcher/change-apply"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "observed"
    assert detail["rejected_writer"] == "factory-dispatcher/change-apply"
    assert session.committed is False


def test_update_bead_owner_can_relabel_own_bead_into_a_class_it_is_enrolled_for(app, monkeypatch):
    """The positive half of the same demonstration: once enrolled for the
    target class too, that same writer's correction of its own bead's class
    succeeds -- the admission re-check governs entitlement to the class, not
    ownership of the fact, which this writer already had.
    """
    monkeypatch.setitem(
        schemas.SOURCE_CLASS_WRITERS,
        "observed",
        frozenset(
            {
                "factory-dispatcher/ea-observer",
                "factory-dispatcher/worker-revision-drift",
                "factory-dispatcher/change-apply",
            }
        ),
    )

    existing = valid_change_content()
    existing["ref"] = "ch.owned-by-change-apply"
    existing["source_class"] = "derived"
    bead = _fake_bead("arch", "change", existing, created_by="factory-dispatcher/change-apply")
    session = _FakeSession(bead)

    relabeled = valid_change_content()
    relabeled["ref"] = "ch.owned-by-change-apply"
    relabeled["source_class"] = "observed"

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": relabeled, "created_by": "factory-dispatcher/change-apply"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_create_bead_admission_violation_names_the_enrollment_path_when_empty(app, monkeypatch):
    """PRIN-008: the refusal carries its own cause -- naming the enrollment
    path a PR must edit, for a class emptied down to no admitted writer.
    """
    monkeypatch.setitem(schemas.SOURCE_CLASS_WRITERS, "derived", frozenset())
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_change(
            TestClient(app),
            valid_change_content_with_ref("ch.orphaned-class.fresh"),
            "factory-dispatcher/change-apply",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert "bead_rules" in detail["enrollment_path"]
    assert "SOURCE_CLASS_WRITERS" in detail["enrollment_path"]


# ---------------------------------------------------------------------------
# knowledge-ingestion enrollment: land_knowledge_principles
# (knowledge_ingestion.py:398-411) mints "derived" arch.principle beads,
# mechanically mirroring a prior, PR-reviewed extraction task's candidates --
# no synthesis in that activity. See .factory/design.md for why "derived"
# and not "authored" or "observed". Exclusive-owner enrollment: this writer
# must be admitted for "derived" and refused for every other class.
# ---------------------------------------------------------------------------

KNOWLEDGE_INGESTION_WRITER = "factory-dispatcher/knowledge-ingestion"

# The "derived" enrollment as it stood immediately before this bead -- used
# to prove only this one writer was added, not a broader widening.
_DERIVED_BASELINE_BEFORE_KNOWLEDGE_INGESTION = frozenset(
    {
        "factory-dispatcher/change-apply",
        "requirements-load",
        "factory-dispatcher/doctrine-staleness",
        "factory-dispatcher/requirements-apply",
        "factory-dispatcher/spec-record-reconcile",
        "factory-dispatcher/staleness-report",
        "factory-dispatcher/release-status",
        "factory-dispatcher/ea-apply",
    }
)


def test_knowledge_ingestion_is_enrolled_for_derived_only():
    assert KNOWLEDGE_INGESTION_WRITER in schemas.SOURCE_CLASS_WRITERS["derived"]
    assert KNOWLEDGE_INGESTION_WRITER not in schemas.SOURCE_CLASS_WRITERS["observed"]
    # "authored" is the HUMAN_EXEMPTION sentinel, not an enumerated set --
    # there is no membership to check it against.
    assert schemas.SOURCE_CLASS_WRITERS["authored"] is HUMAN_EXEMPTION


def test_check_source_class_admission_allows_knowledge_ingestion_for_derived():
    assert check_source_class_admission("derived", KNOWLEDGE_INGESTION_WRITER) is None


def test_check_source_class_admission_rejects_knowledge_ingestion_for_observed():
    violation = check_source_class_admission("observed", KNOWLEDGE_INGESTION_WRITER)
    assert violation is not None
    assert violation.owning_class == "observed"
    assert violation.rejected_writer == KNOWLEDGE_INGESTION_WRITER


def test_source_class_writers_enrollment_counts_are_pinned():
    """Pins the table's shape so an accidental widening anywhere is a visible
    line in a diff, not a silent pass. authored is the HUMAN_EXEMPTION
    sentinel; derived is 9; observed is 5 (was 4, R26.09/O-7 adds
    probe-console-surface) -- no other writer's enrollment changes here.
    """
    assert schemas.SOURCE_CLASS_WRITERS["authored"] is HUMAN_EXEMPTION
    assert len(schemas.SOURCE_CLASS_WRITERS["derived"]) == 9
    assert len(schemas.SOURCE_CLASS_WRITERS["observed"]) == 5


def test_only_knowledge_ingestion_writer_was_added_to_derived():
    """Diffs against the pre-this-bead baseline so any *other* addition or
    removal in "derived" fails loudly instead of passing inside a bare count.
    """
    assert schemas.SOURCE_CLASS_WRITERS["derived"] == (
        _DERIVED_BASELINE_BEFORE_KNOWLEDGE_INGESTION | {KNOWLEDGE_INGESTION_WRITER}
    )


# ---------------------------------------------------------------------------
# probe-console-surface enrollment: probe_console_surface.py's standing
# obs.probe.console-surface write (R26.09/O-7) -- a dated comparison of what
# the console's release index and factory-state panel claim against what the
# store and Temporal independently say. "observed", not "derived": the value
# is read directly from two authorities and diffed, never synthesized from
# either. Exclusive-owner enrollment, mirroring the knowledge-ingestion
# precedent above: admitted for "observed" only.
# ---------------------------------------------------------------------------

PROBE_CONSOLE_SURFACE_WRITER = "factory-dispatcher/probe-console-surface"

# The "observed" enrollment as it stood immediately before this bead -- used
# to prove only this one writer was added, not a broader widening.
_OBSERVED_BASELINE_BEFORE_PROBE_CONSOLE_SURFACE = frozenset(
    {
        "factory-dispatcher/ea-observer",
        "factory-dispatcher/worker-revision-drift",
        "factory-dispatcher/doctrine-registry-view",
        "factory-dispatcher/deployed-revision-drift",
    }
)


def test_probe_console_surface_is_enrolled_for_observed_only():
    assert PROBE_CONSOLE_SURFACE_WRITER in schemas.SOURCE_CLASS_WRITERS["observed"]
    assert PROBE_CONSOLE_SURFACE_WRITER not in schemas.SOURCE_CLASS_WRITERS["derived"]
    assert schemas.SOURCE_CLASS_WRITERS["authored"] is HUMAN_EXEMPTION


def test_check_source_class_admission_allows_probe_console_surface_for_observed():
    assert check_source_class_admission("observed", PROBE_CONSOLE_SURFACE_WRITER) is None


def test_check_source_class_admission_rejects_probe_console_surface_for_derived():
    violation = check_source_class_admission("derived", PROBE_CONSOLE_SURFACE_WRITER)
    assert violation is not None
    assert violation.owning_class == "derived"
    assert violation.rejected_writer == PROBE_CONSOLE_SURFACE_WRITER


def test_only_probe_console_surface_writer_was_added_to_observed():
    """Diffs against the pre-this-bead baseline so any *other* addition or
    removal in "observed" fails loudly instead of passing inside a bare count.
    """
    assert schemas.SOURCE_CLASS_WRITERS["observed"] == (
        _OBSERVED_BASELINE_BEFORE_PROBE_CONSOLE_SURFACE | {PROBE_CONSOLE_SURFACE_WRITER}
    )


def valid_principle_content_for(source_class: str) -> dict:
    content = valid_principle_content()
    content["source_class"] = source_class
    return content


def _post_principle(client: TestClient, content: dict, created_by: str):
    return client.post(
        "/beads",
        headers=HEADERS,
        json={
            "namespace": "arch",
            "type": "principle",
            "state": "active",
            "content": content,
            "trust_tier": "system",
            "created_by": created_by,
        },
    )


def test_create_bead_allows_derived_principle_from_knowledge_ingestion(app):
    """Door 1 (POST), driven the way the #862 gate drove it: a fresh
    arch.principle declaring "derived" from the newly enrolled identity
    succeeds."""
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_principle(
            TestClient(app),
            valid_principle_content_for("derived"),
            KNOWLEDGE_INGESTION_WRITER,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert session.committed is True


# ---------------------------------------------------------------------------
# D8(c) — the predicate fix closes the two-request lock-stripping vector:
# a governed (derived/observed) fact's own declared owner, now recognized
# automated under the factory-dispatcher/* namespace, can no longer downgrade
# its own fact to "authored" to open the human exemption on it.
# ---------------------------------------------------------------------------


def test_update_bead_class_preserving_patch_from_declaring_factory_dispatcher_writer_succeeds(
    app,
):
    """AC-4: the class-preserving rewrite door, for a bead whose source_class
    is DECLARED (not missing), stays open for its own enrolled writer even
    though that writer's identity is now recognized automated. #767 flipped
    this OK->409 for an UNDECLARED bead; this is the declared case the
    predicate fix must not regress.
    """
    existing = valid_observation_content()
    existing["source_class"] = "observed"
    bead = _fake_bead(
        "arch", "observation", existing, created_by="factory-dispatcher/ea-observer"
    )
    session = _FakeSession(bead)

    new_content = valid_observation_content()
    new_content["source_class"] = "observed"
    new_content["replicas"] = 4
    new_content["ready_replicas"] = 4

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "factory-dispatcher/ea-observer"},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_create_bead_rejects_derived_principle_from_an_unenrolled_control_writer(app):
    """Known-bad control: the same POST from a writer that is NOT enrolled
    for "derived" is still refused -- proving the prior test passed because
    of this writer's enrollment, not because the gate is disabled."""
    session = _FakeCreateSession()
    app.dependency_overrides[get_db] = lambda: session

    try:
        response = _post_principle(
            TestClient(app),
            valid_principle_content_for("derived"),
            "an-unenrolled-writer",
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["rejected_writer"] == "an-unenrolled-writer"


def test_update_bead_allows_derived_principle_owned_by_knowledge_ingestion(app):
    """Door 2 (PATCH): the identity updates a "derived" principle it already
    owns."""
    existing = valid_principle_content_for("derived")
    bead = _fake_bead("arch", "principle", existing, created_by=KNOWLEDGE_INGESTION_WRITER)
    session = _FakeSession(bead)

    new_content = valid_principle_content_for("derived")
    new_content["rationale"] = "Refreshed by the deriver."

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": KNOWLEDGE_INGESTION_WRITER},
    )

    assert response.status_code == 200
    assert session.committed is True


def test_update_bead_rejects_derived_principle_from_an_unenrolled_control_writer(app):
    """Known-bad control for door 2: a writer that isn't the declared owner
    (and isn't enrolled) is still refused the same PATCH."""
    existing = valid_principle_content_for("derived")
    bead = _fake_bead("arch", "principle", existing, created_by=KNOWLEDGE_INGESTION_WRITER)
    session = _FakeSession(bead)

    new_content = valid_principle_content_for("derived")
    new_content["rationale"] = "Attempted by someone who isn't the deriver."

    response = _patch(
        _client(app, session),
        bead.id,
        {"content": new_content, "created_by": "an-unenrolled-writer"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["owning_class"] == "derived"
    assert detail["rejected_writer"] == "an-unenrolled-writer"


def test_two_request_downgrade_of_governed_fact_to_authored_is_refused(app):
    """The exploit this bead closes: a governed fact's own declared owner
    downgrades it to "authored" in one request (opening the human exemption
    on a fact no non-owner could otherwise touch), then a second, unrelated
    writer overwrites it in a follow-up request that only the stripped lock
    would ever have let through.

    Before the predicate fix, request 1 succeeded: check_source_class_ownership
    let the owner overwrite its own "observed" fact (writer == owner), and the
    resulting class change to "authored" ran the admission gate -- which
    is_automated_writer waved through, because "factory-dispatcher/ea-observer"
    matched no AGENT_PROVENANCE_WORKERS part and so read as HUMAN, opening the
    HUMAN_EXEMPTION. Request 2 then succeeded too: the fact was now genuinely
    "authored" in storage, and ownership blocks only automated writers from an
    authored fact -- which the same broken predicate said this attacker was not.

    After the fix, request 1 is refused with the same 409 shape any other
    class-changing admission violation gets, so the bead's stored class never
    actually changes -- proven here by actually attempting request 2 and
    confirming it is refused too, by the ordinary observed-class ownership
    rule, exactly as it would have been if no downgrade had ever been tried.
    """
    owner = "factory-dispatcher/ea-observer"
    existing = valid_observation_content()
    existing["source_class"] = "observed"
    bead = _fake_bead("arch", "observation", existing, created_by=owner)
    session = _FakeSession(bead)
    client = _client(app, session)

    downgraded = valid_observation_content()
    downgraded["source_class"] = "authored"

    request_1 = _patch(client, bead.id, {"content": downgraded, "created_by": owner})

    assert request_1.status_code == 409
    detail = request_1.json()["detail"]
    assert detail["error"] == "source_class_admission_violation"
    assert detail["declared_class"] == "authored"
    assert detail["rejected_writer"] == owner
    assert session.committed is False

    # The stored bead's class never actually moved -- a follow-up write from
    # an unrelated writer, which the stripped lock would have opened the door
    # to, is still refused by the ordinary "observed" ownership rule.
    attacker_content = valid_observation_content()
    attacker_content["source_class"] = "observed"
    attacker_content["replicas"] = 99

    request_2 = _patch(
        client, bead.id, {"content": attacker_content, "created_by": "an-unrelated-writer"}
    )

    assert request_2.status_code == 409
    attacker_detail = request_2.json()["detail"]
    assert attacker_detail["error"] == "source_class_ownership_violation"
    assert attacker_detail["owning_class"] == "observed"
    assert attacker_detail["rejected_writer"] == "an-unrelated-writer"
    assert session.committed is False
