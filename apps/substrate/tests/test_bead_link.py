"""bead_link — ARCHITECTURE.md §3.1 `link`.

These tests lean the same way the factory's guard tests do: they are written
against the directions that would be *wrong*, because a check that fails open
is worse than no check. Specifically:

* a link to a bead that does not exist must 404 and say WHICH end was missing;
* an unrecognised `direction` must 422, not silently fall back to a default
  (B-106 — a silently ignored filter cannot be distinguished from an applied
  one, and that produced a false positive in a verification spike);
* new writes must use the documented edge vocabulary, after normalisation, or
  a typo mints a new relationship instead of failing at the gate.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import src.cache
from src.database import get_db
from src.routes import router
from src.schemas import BeadLinkCreate


HEADERS = {"x-api-key": "test-key"}
DOCUMENTED_LINK_TYPE = "designs"
LEGACY_LINK_TYPE = "realizes"


class _FakeLinkSession:
    """Stands in for AsyncSession.

    `existing_bead_ids` drives the endpoint-existence probe; `commit_exc`
    simulates the unique-constraint violation.
    """

    def __init__(self, existing_bead_ids=(), commit_exc=None, links=(), namespaces=None):
        self.existing_bead_ids = list(existing_bead_ids)
        # The probe selects (id, namespace) since Amendment 24 — a link inherits
        # the stricter of its endpoints for encryption. Default non-finance so
        # existing cases keep exercising the plaintext path.
        self.namespaces = dict(namespaces or {})
        self.commit_exc = commit_exc
        self.links = list(links)
        self.added = []
        self.deleted = []
        self.rolled_back = False
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        compiled = str(stmt)
        if "bead_link" in compiled:
            rows = list(self.links)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: rows),
                scalar_one_or_none=lambda: rows[0] if rows else None,
            )
        # The endpoint-existence probe: SELECT bead.id, bead.namespace WHERE id IN (...)
        rows = [(b, self.namespaces.get(b, "dev")) for b in self.existing_bead_ids]
        return SimpleNamespace(
            all=lambda: rows,
            scalars=lambda: SimpleNamespace(all=lambda: [r[0] for r in rows]),
            scalar_one_or_none=lambda: rows[0][0] if rows else None,
        )

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        if self.commit_exc:
            raise self.commit_exc

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj):
        # The DB would populate these server-side defaults.
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)


def _fake_link(source_id, target_id, link_type=LEGACY_LINK_TYPE):
    return SimpleNamespace(
        id=uuid4(),
        source_id=source_id,
        target_id=target_id,
        link_type=link_type,
        content={},
        created_at=datetime.now(timezone.utc),
        created_by="test",
    )


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def _client(app, session):
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


# --- link_type normalisation ------------------------------------------------

def test_link_type_is_normalised():
    link = BeadLinkCreate(target_id=uuid4(), link_type="  Designs  ")
    assert link.link_type == DOCUMENTED_LINK_TYPE


def test_blank_link_type_is_rejected():
    with pytest.raises(ValueError):
        BeadLinkCreate(target_id=uuid4(), link_type="   ")


def test_undocumented_link_type_is_rejected():
    with pytest.raises(ValueError):
        BeadLinkCreate(target_id=uuid4(), link_type="desgins")


# --- creation ---------------------------------------------------------------

def test_create_link_persists_edge(app):
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(existing_bead_ids=[source, target])

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={"target_id": str(target), "link_type": DOCUMENTED_LINK_TYPE},
        headers=HEADERS,
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source_id"] == str(source)
    assert body["target_id"] == str(target)
    assert body["link_type"] == DOCUMENTED_LINK_TYPE
    assert len(session.added) == 1


def test_create_link_rejects_undocumented_link_type(app):
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(existing_bead_ids=[source, target])

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={"target_id": str(target), "link_type": "desgins"},
        headers=HEADERS,
    )

    assert resp.status_code == 422
    assert session.added == []


def test_link_content_is_plaintext_between_non_finance_beads(app):
    """Amendment 24: only finance is encrypted, so a dev↔dev link is queryable."""
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(existing_bead_ids=[source, target])

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={
            "target_id": str(target),
            "link_type": DOCUMENTED_LINK_TYPE,
            "content": {"why": "traces to"},
        },
        headers=HEADERS,
    )

    assert resp.status_code == 201
    assert session.added[0].content == {"why": "traces to"}


@pytest.mark.parametrize("finance_end", ["source", "target"])
def test_link_touching_finance_is_encrypted_from_either_end(app, finance_end):
    """A link has no namespace, so it inherits the stricter of its endpoints.

    Source-only inheritance would leave a dev→finance link's content in
    plaintext, and that content can carry financial detail.
    """
    source, target = uuid4(), uuid4()
    finance_id = source if finance_end == "source" else target
    session = _FakeLinkSession(
        existing_bead_ids=[source, target],
        namespaces={finance_id: "finance"},
    )

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={
            "target_id": str(target),
            "link_type": DOCUMENTED_LINK_TYPE,
            "content": {"amount": 12.34},
        },
        headers=HEADERS,
    )

    assert resp.status_code == 201
    stored = session.added[0].content
    assert stored != {"amount": 12.34}
    assert "_enc" in stored["amount"]


def test_create_link_404s_and_names_the_missing_end(app):
    """The FKs would reject this anyway — but as an error that cannot say
    which end was wrong, which is the only useful part."""
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(existing_bead_ids=[source])  # target absent

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={"target_id": str(target), "link_type": DOCUMENTED_LINK_TYPE},
        headers=HEADERS,
    )

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "bead_not_found"
    assert detail["missing"] == [str(target)]
    assert session.added == []


def test_self_link_is_422_not_500(app):
    bead = uuid4()
    session = _FakeLinkSession(existing_bead_ids=[bead])

    resp = _client(app, session).post(
        f"/beads/{bead}/links",
        json={"target_id": str(bead), "link_type": DOCUMENTED_LINK_TYPE},
        headers=HEADERS,
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "self_link"
    assert session.added == []


def test_duplicate_edge_is_409_and_rolls_back(app):
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(
        existing_bead_ids=[source, target],
        commit_exc=IntegrityError("stmt", {}, Exception("uq_bead_link_edge")),
    )

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={"target_id": str(target), "link_type": DOCUMENTED_LINK_TYPE},
        headers=HEADERS,
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "duplicate_link"
    assert session.rolled_back is True


# --- arch.incident: the edges admitted ahead of the type (B-110) now have a
# real source to write from --------------------------------------------------
#
# caused_by/resolved_by/affects were already in BEAD_LINK_TYPES before
# arch.incident existed (bead-object-inventory.md); no type-pair enforcement
# exists anywhere in this vocabulary (confirmed against every other admitted
# edge), so these are demonstrations that a real arch.incident bead can write
# each one through the existing endpoint, not new enforcement.


@pytest.mark.parametrize(
    "link_type",
    ["caused_by", "resolved_by", "affects"],
)
def test_incident_writes_each_of_its_declared_edges(app, link_type):
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(
        existing_bead_ids=[source, target], namespaces={source: "arch"}
    )

    resp = _client(app, session).post(
        f"/beads/{source}/links",
        json={"target_id": str(target), "link_type": link_type},
        headers=HEADERS,
    )

    assert resp.status_code == 201
    assert resp.json()["link_type"] == link_type


# --- traversal --------------------------------------------------------------

@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
def test_valid_directions_are_accepted(app, direction):
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(links=[_fake_link(source, target)])

    resp = _client(app, session).get(
        f"/beads/{source}/links?direction={direction}", headers=HEADERS
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_unknown_direction_is_422_not_a_silent_default(app):
    """B-106's lesson, applied at the gate.

    Falling back to `both` on a typo would return a superset and look correct,
    which is exactly how a mistyped `parent_id` filter produced a false
    positive in the 2026-07-27 verification spike.
    """
    session = _FakeLinkSession()

    resp = _client(app, session).get(
        f"/beads/{uuid4()}/links?direction=sideways", headers=HEADERS
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_direction"
    assert detail["given"] == "sideways"
    assert set(detail["allowed"]) == {"outgoing", "incoming", "both"}


def test_link_type_filter_is_normalised_on_read(app):
    """A filter of "Realizes" must still match legacy stored edges."""
    source, target = uuid4(), uuid4()
    session = _FakeLinkSession(links=[_fake_link(source, target)])

    resp = _client(app, session).get(
        f"/beads/{source}/links?link_type=Realizes", headers=HEADERS
    )

    assert resp.status_code == 200
    compiled_params = str(session.statements[-1].compile().params.values()).lower()
    assert LEGACY_LINK_TYPE in compiled_params


# --- auth -------------------------------------------------------------------

def test_links_require_api_key(app):
    session = _FakeLinkSession()
    app.dependency_overrides[get_db] = lambda: session

    assert TestClient(app).get(f"/beads/{uuid4()}/links").status_code == 401
    assert (
        TestClient(app)
        .post(
            f"/beads/{uuid4()}/links",
            json={"target_id": str(uuid4()), "link_type": DOCUMENTED_LINK_TYPE},
        )
        .status_code
        == 401
    )
