"""POST /beads with a nonexistent parent_id — dev.finding 32bb1216.

Production measured this shape returning 500: create_bead's ``except
IntegrityError`` branch handled only the two unique-constraint cases
(SQLSTATE 23505) and bare-``raise``d everything else, so the self-FK
violation on ``bead.parent_id`` (SQLSTATE 23503) fell through as an
unhandled 500 — indistinguishable from the substrate actually being down.

No live Postgres is available to this suite (see
``tests/test_migration_0006.py`` for the pattern used when one is wanted),
so the FK violation is provoked the same way ``test_bead_link.py`` provokes
a unique violation: a fake session whose ``flush`` raises an
``IntegrityError`` shaped like the real one.
"""

from datetime import datetime, timezone
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
        "INSERT INTO bead ...",
        {},
        _FakeOrig(
            "23503",
            'insert or update on table "bead" violates foreign key constraint '
            f'"bead_parent_id_fkey"\nDETAIL:  Key (parent_id)=({parent_id}) is '
            'not present in table "bead".',
        ),
    )


def _plaid_duplicate() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO bead ...",
        {},
        _FakeOrig(
            "23505",
            "duplicate key value violates unique constraint idx_unique_plaid_id",
        ),
    )


def _spec_identity_duplicate() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO bead ...",
        {},
        _FakeOrig(
            "23505",
            "duplicate key value violates unique constraint "
            '"idx_unique_dev_task_spec_identity_live"',
        ),
    )


class _FakeCreateBeadSession:
    """Stands in for AsyncSession across ``create_bead``.

    The real INSERT into ``bead`` happens at ``flush()`` — that is where
    Postgres would actually raise a constraint violation, not at
    ``commit()`` — so ``flush_exc`` fires there, matching production timing.
    """

    def __init__(self, flush_exc=None):
        self.flush_exc = flush_exc
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.refreshed = []

    def add(self, obj):
        self.added.append(obj)
        if obj.__class__.__name__ == "Bead" and getattr(obj, "id", None) is None:
            now = datetime.now(timezone.utc)
            obj.id = uuid4()
            obj.created_at = now
            obj.updated_at = now

    async def flush(self):
        if self.flush_exc is not None:
            raise self.flush_exc

    async def commit(self):
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


def _client(app, session):
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def _post_note(client, *, parent_id=None):
    body = {
        "namespace": "dev",
        "type": "note",
        "state": "open",
        "content": {"kind": "comment", "body": "hello"},
        "trust_tier": "user",
        "created_by": "test-user",
    }
    if parent_id is not None:
        body["parent_id"] = str(parent_id)
    return client.post("/beads", json=body, headers=HEADERS)


# --- the defect: nonexistent parent_id must 4xx, not 500 --------------------

def test_nonexistent_parent_id_is_404_not_500(app):
    parent_id = uuid4()
    session = _FakeCreateBeadSession(flush_exc=_fk_violation(parent_id))

    resp = _post_note(_client(app, session), parent_id=parent_id)

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "parent_not_found"
    assert detail["parent_id"] == str(parent_id)


def test_nonexistent_parent_id_rolls_back(app):
    """Pins the rollback rather than assuming it: a refused create must not
    leave the bead behind."""
    parent_id = uuid4()
    session = _FakeCreateBeadSession(flush_exc=_fk_violation(parent_id))

    _post_note(_client(app, session), parent_id=parent_id)

    assert session.rolled_back is True
    assert session.committed is False


# --- the two pre-existing branches must keep working -------------------------

def test_plaid_duplicate_still_409s_ahead_of_the_new_branch(app):
    session = _FakeCreateBeadSession(flush_exc=_plaid_duplicate())

    resp = _post_note(_client(app, session))

    assert resp.status_code == 409
    assert session.rolled_back is True


def test_spec_identity_duplicate_still_409s_with_its_own_error_code(app):
    """Must stay ordered ahead of the plaid check's broader fallback clause
    (see ``_is_spec_identity_duplicate_error``'s docstring) — and, now, ahead
    of or independent from the new missing-parent branch."""
    session = _FakeCreateBeadSession(flush_exc=_spec_identity_duplicate())

    resp = _post_note(_client(app, session))

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "spec_identity_duplicate"
    assert session.rolled_back is True


# --- valid creates are unaffected --------------------------------------------

def test_create_with_real_parent_id_still_succeeds(app):
    session = _FakeCreateBeadSession()

    resp = _post_note(_client(app, session), parent_id=uuid4())

    assert resp.status_code == 200
    assert session.committed is True


def test_create_with_no_parent_id_still_succeeds(app):
    session = _FakeCreateBeadSession()

    resp = _post_note(_client(app, session))

    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None
    assert session.committed is True
