"""PATCH /beads/{id} with a nonexistent parent_id -- dev.finding 32d444af.

#851 taught ``create_bead`` to classify SQLSTATE 23503 on ``parent_id`` as a
404 rather than an unhandled 500, via ``_is_missing_parent_error``. That fix
was scoped to POST; ``update_bead`` still had only
``except Exception: await db.rollback(); raise`` on its commit, so the same
foreign-key violation reaching PATCH fell through as a 500 --
indistinguishable from the substrate itself being down, on a route that
live callers (finance's ``link_transaction_to_manual``, ``ea-load.py``, the
console's ``updateBead``) use to stamp ``parent_id``.

No live Postgres is available to this suite. ``update_bead`` never calls
``flush()`` explicitly (unlike ``create_bead``), so its first point of
contact with a real constraint violation is ``commit()`` -- the fake
session's ``commit()`` raises here, matching production timing, the same
way ``test_create_bead_parent_fk.py``'s fake raises from ``flush()`` to
match create_bead's timing.
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


HEADERS = {"x-api-key": "test-key"}


class _FakeOrig:
    def __init__(self, sqlstate: str, text: str):
        self.sqlstate = sqlstate
        self._text = text

    def __str__(self):
        return self._text


def _fk_violation(parent_id) -> IntegrityError:
    return IntegrityError(
        "UPDATE bead ...",
        {},
        _FakeOrig(
            "23503",
            'insert or update on table "bead" violates foreign key constraint '
            f'"bead_parent_id_fkey"\nDETAIL:  Key (parent_id)=({parent_id}) is '
            'not present in table "bead".',
        ),
    )


def _bead_event_fk_violation() -> IntegrityError:
    """A DIFFERENT constraint -- must NOT be classified as a missing parent.

    update_bead operates on beads that are themselves parents of other
    beads; mis-widening ``_is_missing_parent_error`` to match this would
    convert a real integrity failure into a misleading 404.
    """
    return IntegrityError(
        "INSERT INTO bead_event ...",
        {},
        _FakeOrig(
            "23503",
            'insert or update on table "bead_event" violates foreign key '
            'constraint "bead_event_bead_id_fkey"\nDETAIL:  Key '
            "(bead_id)=(deadbeef-0000-0000-0000-000000000000) is not present "
            'in table "bead".',
        ),
    )


def _bead_link_fk_violation() -> IntegrityError:
    """A second, differently-named constraint -- same negative requirement."""
    return IntegrityError(
        "INSERT INTO bead_link ...",
        {},
        _FakeOrig(
            "23503",
            'insert or update on table "bead_link" violates foreign key '
            'constraint "bead_link_source_id_fkey"\nDETAIL:  Key '
            "(source_id)=(deadbeef-0000-0000-0000-000000000000) is not "
            'present in table "bead".',
        ),
    )


def _fake_bead(namespace: str = "dev", type_: str = "comment", state: str = "open"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        namespace=namespace,
        type=type_,
        state=state,
        trust_tier="system",
        parent_id=None,
        content={},
        context={},
        provenance={},
        confidence=None,
        created_at=now,
        updated_at=now,
        created_by="test",
    )


class _FakeUpdateBeadSession:
    """Stands in for AsyncSession across ``update_bead``.

    ``update_bead`` has no explicit ``flush()`` -- its ``commit()`` is the
    first point that would actually hit Postgres, so ``commit_exc`` fires
    there.
    """

    def __init__(self, bead, commit_exc=None):
        self.bead = bead
        self.commit_exc = commit_exc
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.refreshed = []

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        if self.commit_exc is not None:
            raise self.commit_exc
        self.committed = True

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def _client(app, session, *, raise_server_exceptions=True):
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _patch(client, bead_id, parent_id):
    return client.patch(
        f"/beads/{bead_id}",
        json={"parent_id": str(parent_id)},
        headers=HEADERS,
    )


# --- the defect: nonexistent parent_id must 4xx, not 500 --------------------

def test_nonexistent_parent_id_is_404_not_500(app):
    bead = _fake_bead()
    parent_id = uuid4()
    session = _FakeUpdateBeadSession(bead, commit_exc=_fk_violation(parent_id))

    resp = _patch(_client(app, session), bead.id, parent_id)

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "parent_not_found"
    assert detail["parent_id"] == str(parent_id)


def test_nonexistent_parent_id_rolls_back(app):
    """Pins the rollback rather than assuming it, mirroring
    test_create_bead_parent_fk.py::test_nonexistent_parent_id_rolls_back."""
    bead = _fake_bead()
    parent_id = uuid4()
    session = _FakeUpdateBeadSession(bead, commit_exc=_fk_violation(parent_id))

    _patch(_client(app, session), bead.id, parent_id)

    assert session.rolled_back is True
    assert session.committed is False


# --- negative cases: a differently-named constraint must NOT be caught here -

def test_bead_event_fk_violation_is_not_classified_as_missing_parent(app):
    bead = _fake_bead()
    session = _FakeUpdateBeadSession(bead, commit_exc=_bead_event_fk_violation())
    client = _client(app, session, raise_server_exceptions=False)

    resp = _patch(client, bead.id, uuid4())

    assert resp.status_code == 500
    assert session.rolled_back is True


def test_bead_link_fk_violation_is_not_classified_as_missing_parent(app):
    bead = _fake_bead()
    session = _FakeUpdateBeadSession(bead, commit_exc=_bead_link_fk_violation())
    client = _client(app, session, raise_server_exceptions=False)

    resp = _patch(client, bead.id, uuid4())

    assert resp.status_code == 500
    assert session.rolled_back is True


# --- a valid update is unaffected --------------------------------------------

def test_update_with_real_parent_id_still_succeeds(app):
    bead = _fake_bead()
    session = _FakeUpdateBeadSession(bead)

    resp = _patch(_client(app, session), bead.id, uuid4())

    assert resp.status_code == 200
    assert session.committed is True
