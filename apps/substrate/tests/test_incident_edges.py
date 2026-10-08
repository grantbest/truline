"""arch.incident's two ITSM edges, written end to end -- finding 2 of the
stranded-incident spec.

`caused_by` and `resolved_by` have been admitted to `BEAD_LINK_TYPES` since
before `arch.incident` existed (B-110), and `test_bead_link.py::
test_incident_writes_each_of_its_declared_edges` (landed with R2605-1, #698)
already proves the generic route accepts them from an arbitrary arch source.
What was still true live on 2026-09-10 is that a per-link-id census of the
`arch` namespace found `caused_by` and `resolved_by` at zero -- the vocabulary
was open, but the edge had never actually been written by anything shaped like
the real records: an `arch.incident` bead, a `dev.task` bead (the fix the
factory ran), an `arch.change` bead (the change that closed it). These tests
close that gap with the real content shapes, using the incident this whole
spec is about (`d861f65e-74ff-437e-a068-05e16b49c99a`,
`inc.ci-runner-outage-2026-08-06`) as the source, and introduce zero new
entries to `BEAD_LINK_TYPES`.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.cache
from src.database import get_db
from src.routes import router
from src.schemas import BEAD_LINK_TYPES

HEADERS = {"x-api-key": "test-key"}

# The bead this whole spec is about -- see
# apps/substrate/migrations/versions/0009_repair_stranded_incident_pending_state.py.
INCIDENT_ID = UUID("d861f65e-74ff-437e-a068-05e16b49c99a")


class _FakeLinkSession:
    """Mirrors test_bead_link.py's fake session: the endpoint-existence probe
    plus enough of AsyncSession for create_bead_link's add/commit/refresh."""

    def __init__(self, existing_bead_ids=(), namespaces=None):
        self.existing_bead_ids = list(existing_bead_ids)
        self.namespaces = dict(namespaces or {})
        self.added = []

    async def execute(self, stmt):
        rows = [(b, self.namespaces.get(b, "dev")) for b in self.existing_bead_ids]
        return SimpleNamespace(
            all=lambda: rows,
            scalars=lambda: SimpleNamespace(all=lambda: [r[0] for r in rows]),
            scalar_one_or_none=lambda: rows[0][0] if rows else None,
        )

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)


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


def _post_link(client, source_id, target_id, link_type, content=None):
    body = {"target_id": str(target_id), "link_type": link_type}
    if content is not None:
        body["content"] = content
    return client.post(f"/beads/{source_id}/links", json=body, headers=HEADERS)


# --- zero new edge types -----------------------------------------------------


def test_caused_by_and_resolved_by_are_pre_existing_vocabulary():
    """Both were admitted to BEAD_LINK_TYPES ahead of arch.incident (B-110);
    this spec introduces neither."""
    assert "caused_by" in BEAD_LINK_TYPES
    assert "resolved_by" in BEAD_LINK_TYPES
    assert len(BEAD_LINK_TYPES) == 19


# --- arch.incident --caused_by--> dev.task -----------------------------------


def test_incident_caused_by_dev_task_edge_end_to_end(app):
    """'the fix the factory ran' (itsm-target-state.md §1) -- a real dev.task
    id as the target, an arch.incident id as the source."""
    task_id = uuid4()
    session = _FakeLinkSession(
        existing_bead_ids=[INCIDENT_ID, task_id],
        namespaces={INCIDENT_ID: "arch", task_id: "dev"},
    )

    resp = _post_link(
        _client(app, session),
        INCIDENT_ID,
        task_id,
        "caused_by",
        content={"note": "the fix the factory ran for inc.ci-runner-outage-2026-08-06"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source_id"] == str(INCIDENT_ID)
    assert body["target_id"] == str(task_id)
    assert body["link_type"] == "caused_by"
    assert len(session.added) == 1


# --- arch.incident --resolved_by--> arch.change ------------------------------


def test_incident_resolved_by_arch_change_edge_end_to_end(app):
    """'the change that closed it' (itsm-target-state.md §1) -- both source
    and target are arch namespace, which is exactly the shape finding 2's
    dedup warning is about for GET .../links (not exercised here -- this is
    the write path)."""
    change_id = uuid4()
    session = _FakeLinkSession(
        existing_bead_ids=[INCIDENT_ID, change_id],
        namespaces={INCIDENT_ID: "arch", change_id: "arch"},
    )

    resp = _post_link(
        _client(app, session),
        INCIDENT_ID,
        change_id,
        "resolved_by",
        content={"note": "the change that closed inc.ci-runner-outage-2026-08-06"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source_id"] == str(INCIDENT_ID)
    assert body["target_id"] == str(change_id)
    assert body["link_type"] == "resolved_by"
    assert len(session.added) == 1


def test_caused_by_and_resolved_by_both_write_from_the_same_incident(app):
    """The loop bead-object-inventory.md describes -- both edges out of the
    same incident bead in one flow, not two unrelated fixtures."""
    task_id, change_id = uuid4(), uuid4()
    session = _FakeLinkSession(
        existing_bead_ids=[INCIDENT_ID, task_id, change_id],
        namespaces={INCIDENT_ID: "arch", task_id: "dev", change_id: "arch"},
    )
    client = _client(app, session)

    caused_by_resp = _post_link(client, INCIDENT_ID, task_id, "caused_by")
    resolved_by_resp = _post_link(client, INCIDENT_ID, change_id, "resolved_by")

    assert caused_by_resp.status_code == 201
    assert resolved_by_resp.status_code == 201
    assert len(session.added) == 2
    assert {edge.link_type for edge in session.added} == {"caused_by", "resolved_by"}
    assert {edge.source_id for edge in session.added} == {INCIDENT_ID}
