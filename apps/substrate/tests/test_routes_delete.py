"""DELETE /beads/{id} — child bead_events must go first (NO ACTION FK)."""

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


def _fake_bead():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(), namespace="finance", type="transaction", state="posted",
        parent_id=None, content={}, context={}, confidence=None,
        trust_tier="system", provenance={}, created_by="test",
        created_at=now, updated_at=now,
    )


class _FakeDeleteSession:
    def __init__(self, bead, commit_exc=None):
        self.bead = bead
        self.commit_exc = commit_exc
        self.executed = []
        self.deleted = []
        self.rolled_back = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        if stmt.__class__.__name__ == "Select":
            return SimpleNamespace(scalar_one_or_none=lambda: self.bead)
        return SimpleNamespace()

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        if self.commit_exc:
            raise self.commit_exc

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def test_delete_removes_events_before_bead(app):
    bead = _fake_bead()
    session = _FakeDeleteSession(bead)
    app.dependency_overrides[get_db] = lambda: session

    resp = TestClient(app).delete(f"/beads/{bead.id}", headers={"x-api-key": "test-key"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    # One Select (lookup) + one Delete (bead_events) before db.delete(bead).
    stmt_kinds = [s.__class__.__name__ for s in session.executed]
    assert stmt_kinds == ["Select", "Delete"]
    assert session.deleted == [bead]


def test_delete_409_when_referenced_as_parent(app):
    bead = _fake_bead()
    session = _FakeDeleteSession(
        bead, commit_exc=IntegrityError("stmt", {}, Exception("fk parent_id"))
    )
    app.dependency_overrides[get_db] = lambda: session

    resp = TestClient(app).delete(f"/beads/{bead.id}", headers={"x-api-key": "test-key"})

    assert resp.status_code == 409
    assert session.rolled_back is True
