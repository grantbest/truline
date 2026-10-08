"""`POST /beads/{id}/transition` — the claim primitive (ARCHITECTURE.md §3.1).

**What these tests can and cannot prove.** The substrate suite has no database:
`DATABASE_URL` is a dummy string and every test drives a fake session. So there
is no honest way here to run two claims concurrently and observe that exactly
one wins — that requires real Postgres and real MVCC.

What is provable without a database is the *mechanism*, and the mechanism is the
part that was wrong. The first implementation of this endpoint did
`SELECT` → compare in Python → assign → `commit`, which under READ COMMITTED
lets two callers both read `pending`, both pass the check, and both claim. The
fix is that the swap is a single conditional `UPDATE ... WHERE id = :id AND
state = :from_state` and that a zero-row result is the 409. These tests assert
exactly that shape:

* the emitted statement is an UPDATE whose predicate constrains `state`;
* no row matched ⇒ 409, and nothing was committed;
* the Python-side comparison that used to gate the write is gone, so it cannot
  regress back into a read-check-write.

A regression to the racy form fails `test_claim_is_a_conditional_update`,
because a read-check-write emits no state predicate. Closing the remaining gap —
a real concurrent claim against Postgres — is filed as FA-S16.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Update

import src.cache
from src.database import get_db
from src.models import BeadEvent
from src.routes import STATE_MACHINE_ENTRY_STATES, STATE_MACHINES, router


HEADERS = {"x-api-key": "test-key"}


def _is_update(stmt) -> bool:
    return isinstance(stmt, Update)


def _selected_column_names(stmt) -> list:
    return [c["name"] for c in getattr(stmt, "column_descriptions", [])]


def _fake_bead(namespace="dev", type_="task", state="pending"):
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


class _FakeSession:
    """Records every statement so the test can inspect the SQL that was built.

    ``update_matched`` decides whether the conditional UPDATE finds its row —
    that is the entire compare-and-set outcome, expressed as the one knob the
    database would actually control.
    """

    def __init__(self, bead=None, update_matched=True, current_state=None):
        self.bead = bead
        self.update_matched = update_matched
        self.current_state = current_state
        self.statements = []
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        self.statements.append(stmt)

        if _is_update(stmt):
            matched = self.bead.id if self.update_matched else None
            return SimpleNamespace(scalar_one_or_none=lambda: matched)

        # Discriminate on the selected columns, not on the SQL text: the full
        # `select(Bead)` also mentions `bead.state`, so a substring match sends
        # the entity load down the failure-path branch and every test 404s.
        if _selected_column_names(stmt) == ["state"]:
            return SimpleNamespace(scalar_one_or_none=lambda: self.current_state)

        return SimpleNamespace(scalar_one_or_none=lambda: self.bead)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj):
        # The DB would return the new row; the endpoint refreshes because the
        # Core UPDATE deliberately does not synchronise the identity map.
        if (
            obj is self.bead
            and self.update_matched
            and self._target_state is not None
        ):
            obj.state = self._target_state

    _target_state = None

    def updates(self):
        return [s for s in self.statements if _is_update(s)]


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


def _transition(client, bead_id, from_state, to_state):
    return client.post(
        f"/beads/{bead_id}/transition",
        json={"from_state": from_state, "to_state": to_state, "created_by": "test"},
        headers=HEADERS,
    )


def _patch(client, bead_id, body):
    return client.patch(f"/beads/{bead_id}", json=body, headers=HEADERS)


# --- the compare-and-set mechanism -----------------------------------------

def test_claim_is_a_conditional_update(app):
    """The swap must be one UPDATE constrained on the expected state.

    This is the test that fails if anyone reintroduces read-check-write.
    """
    bead = _fake_bead(state="pending")
    session = _FakeSession(bead=bead)
    session._target_state = "doing"

    resp = _transition(_client(app, session), bead.id, "pending", "doing")

    assert resp.status_code == 200
    updates = session.updates()
    assert len(updates) == 1, "the state swap must be a single statement"

    sql = str(updates[0])
    assert "WHERE" in sql.upper()
    # Both halves of the predicate: the right row, and the expected state.
    assert "bead.id" in sql
    assert "bead.state" in sql, (
        "UPDATE has no state predicate — this is a read-then-write and two "
        "runners can both claim the same task"
    )


def test_lost_race_returns_409_and_names_the_winner(app):
    bead = _fake_bead(state="pending")
    session = _FakeSession(bead=bead, update_matched=False, current_state="doing")

    resp = _transition(_client(app, session), bead.id, "pending", "doing")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "state_mismatch"
    assert detail["expected_state"] == "pending"
    assert detail["current_state"] == "doing"
    assert session.rolled_back
    assert not session.committed


def test_lost_race_records_no_event(app):
    bead = _fake_bead(state="pending")
    session = _FakeSession(bead=bead, update_matched=False, current_state="doing")

    _transition(_client(app, session), bead.id, "pending", "doing")

    assert not any(isinstance(o, BeadEvent) for o in session.added)


# --- the state machine ------------------------------------------------------

@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("pending", "done"),     # skips the whole pipeline
        ("pending", "review"),   # skips doing
        ("done", "doing"),       # reopens a finished task
        ("archived", "pending"), # archived is terminal
        ("superseded", "pending"),  # superseded is terminal
        ("superseded", "doing"),    # superseded is terminal
        ("doing", "superseded"),    # live work is not dead work
        ("review", "superseded"),   # live work is not dead work
        ("done", "superseded"),     # landed work is not dead work
    ],
)
def test_illegal_dev_task_edges_are_422(app, from_state, to_state):
    bead = _fake_bead(state=from_state)
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "illegal_transition"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not session.committed


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("pending", "doing"),
        ("doing", "review"),
        ("doing", "pending"),
        ("doing", "failed"),
        ("review", "done"),
        ("review", "pending"),
        ("review", "failed"),
        ("done", "archived"),
        ("failed", "pending"),
        ("pending", "superseded"),
        ("failed", "superseded"),
    ],
)
def test_legal_dev_task_edges_are_allowed(app, from_state, to_state):
    bead = _fake_bead(state=from_state)
    session = _FakeSession(bead=bead)
    session._target_state = to_state

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 200


def test_dev_task_machine_declares_review_to_failed():
    assert "failed" in STATE_MACHINES[("dev", "task")]["review"]


def test_dev_task_machine_declares_superseded_as_terminal():
    machine = STATE_MACHINES[("dev", "task")]
    assert machine["superseded"] == frozenset()


def test_dev_task_machine_reaches_superseded_only_from_pending_and_failed():
    machine = STATE_MACHINES[("dev", "task")]
    reachable_from = {
        state for state, edges in machine.items() if "superseded" in edges
    }
    assert reachable_from == {"pending", "failed"}


def test_unknown_from_state_is_rejected_for_a_declared_machine(app):
    bead = _fake_bead(state="wibble")
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, "wibble", "doing")

    assert resp.status_code == 422
    assert resp.json()["detail"]["allowed"] == []


def test_undeclared_machine_stays_permissive(app):
    """finance.* predates this endpoint and must not start being rejected."""
    assert ("finance", "transaction") not in STATE_MACHINES
    bead = _fake_bead(namespace="finance", type_="transaction", state="anything")
    session = _FakeSession(bead=bead)
    session._target_state = "whatever"

    resp = _transition(_client(app, session), bead.id, "anything", "whatever")

    assert resp.status_code == 200


# --- PATCH follows the declared machine when it changes state ---------------

@pytest.mark.parametrize("to_state", ["review", "pending", "failed"])
def test_patch_preserves_dispatcher_doing_edges(app, to_state):
    bead = _fake_bead(state="doing")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": to_state})

    assert resp.status_code == 200
    assert bead.state == to_state
    assert session.committed


def test_patch_allows_review_to_failed_for_dev_task(app):
    bead = _fake_bead(state="review")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "failed"})

    assert resp.status_code == 200
    assert bead.state == "failed"
    assert session.committed


def test_patch_rejects_undeclared_dev_task_transition_without_commit(app):
    bead = _fake_bead(state="review")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "archived"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_transition"
    assert detail["from_state"] == "review"
    assert detail["to_state"] == "archived"
    assert detail["allowed"] == ["done", "failed", "pending"]
    assert bead.state == "review"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not any(isinstance(o, BeadEvent) for o in session.added)
    assert not session.committed


def test_patch_undeclared_machine_stays_permissive(app):
    assert ("finance", "transaction") not in STATE_MACHINES
    bead = _fake_bead(namespace="finance", type_="transaction", state="anything")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "whatever"})

    assert resp.status_code == 200
    assert bead.state == "whatever"
    assert session.committed


@pytest.mark.parametrize(
    "body,assertion",
    [
        (
            {"context": {"operator": "note"}},
            lambda bead: bead.context == {"operator": "note"},
        ),
        ({"confidence": 0.6}, lambda bead: bead.confidence == 0.6),
        ({"parent_id": str(uuid4())}, lambda bead: bead.parent_id is not None),
    ],
)
def test_patch_without_state_does_not_require_a_legal_transition(app, body, assertion):
    bead = _fake_bead(state="archived")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, body)

    assert resp.status_code == 200
    assert bead.state == "archived"
    assert assertion(bead)
    assert session.committed


# --- provenance and the basics ---------------------------------------------

def test_success_records_a_bead_event(app):
    bead = _fake_bead(state="doing")
    session = _FakeSession(bead=bead)
    session._target_state = "review"

    resp = _transition(_client(app, session), bead.id, "doing", "review")

    assert resp.status_code == 200
    events = [o for o in session.added if isinstance(o, BeadEvent)]
    assert len(events) == 1
    assert events[0].event_type == "transitioned"
    assert events[0].from_state == "doing"
    assert events[0].to_state == "review"
    assert session.committed


def test_missing_bead_is_404(app):
    session = _FakeSession(bead=None)

    resp = _transition(_client(app, session), uuid4(), "pending", "doing")

    assert resp.status_code == 404
    assert session.updates() == []


def test_from_state_is_required(app):
    """An unconditional transition would just be PATCH with extra steps."""
    bead = _fake_bead()
    resp = _client(app, _FakeSession(bead=bead)).post(
        f"/beads/{bead.id}/transition",
        json={"to_state": "doing", "created_by": "test"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


# --- POST /beads: create is a state-changing write too ---------------------
#
# Creation assigns a bead's first state without ever checking a from_state,
# so a declared machine needs its own notion of which states a bead may be
# *born* into — see STATE_MACHINE_ENTRY_STATES. Without this gate, a creator
# could hand a fresh dev.task a state no legal edge in the machine produces
# (e.g. "done" with no verification having ever run).

def _valid_dev_task_content() -> dict:
    return {
        "lane": "code-health",
        "title": "Example task",
        "intent": "Exercise the entry-state gate.",
        "context_refs": [],
        "acceptance": ["it works"],
        "verification": {"commands": ["true"]},
        "scope": {
            "paths": ["apps/substrate/src/routes.py"],
            "forbidden_paths": [".github/workflows/**"],
        },
        "risk_class": "structural",
        "budget": {"max_agent_minutes": 30, "max_usd": 1.0, "max_tokens": 1000},
    }


class _FakeCreateSession:
    """Enough of AsyncSession for create_bead: add / flush / commit / refresh.

    ``execute`` answers create_bead's arch content.ref lookup (routes.py's
    ``_find_existing_arch_bead_by_ref``) with "no existing bead" — none of
    these tests are exercising the ref-ownership guard, just the entry-state
    check.
    """

    def __init__(self):
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, obj):
        self.added.append(obj)
        if obj.__class__.__name__ == "Bead" and getattr(obj, "id", None) is None:
            now = datetime.now(timezone.utc)
            obj.id = uuid4()
            obj.created_at = now
            obj.updated_at = now

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, _obj):
        return None


def _create(client, **overrides):
    payload = {
        "namespace": "dev",
        "type": "task",
        "state": "pending",
        "content": _valid_dev_task_content(),
        "trust_tier": "system",
        "created_by": "test",
    }
    payload.update(overrides)
    return client.post("/beads", json=payload, headers=HEADERS)


def test_create_dev_task_at_declared_entry_state_succeeds(app):
    session = _FakeCreateSession()

    resp = _create(_client(app, session), state="pending")

    assert resp.status_code == 200
    assert session.committed


@pytest.mark.parametrize("state", ["doing", "review", "done", "failed", "archived"])
def test_create_dev_task_at_undeclared_entry_state_is_422(app, state):
    session = _FakeCreateSession()

    resp = _create(_client(app, session), state=state)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_entry_state"
    assert detail["machine"] == "dev.task"
    assert detail["allowed"] == ["pending"]
    assert session.added == [], "an illegal entry state must not reach the database"
    assert not session.committed


def test_create_undeclared_machine_stays_permissive(app):
    """dev.note has no declared machine and must not start being rejected."""
    assert ("dev", "note") not in STATE_MACHINES
    session = _FakeCreateSession()

    resp = _client(app, session).post(
        "/beads",
        json={
            "namespace": "dev",
            "type": "note",
            "state": "whatever",
            "content": {"kind": "comment", "body": "note body"},
            "trust_tier": "system",
            "created_by": "test",
        },
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert session.committed


# --- arch.release: the second declared machine -------------------------------
#
# A release charter under docs/releases/ stays authoritative for structure, but
# nothing in git may declare a release "released" — that claim is only true once
# the work landed. So the lifecycle is the half the substrate owns outright, and
# it is governed rather than permissive.


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("planned", "in_flight"),
        ("planned", "abandoned"),
        ("in_flight", "closing"),
        ("in_flight", "abandoned"),
        ("closing", "released"),
        ("closing", "in_flight"),
    ],
)
def test_legal_arch_release_edges_are_allowed(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="release", state=from_state)
    session = _FakeSession(bead=bead)
    session._target_state = to_state

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 200


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("planned", "released"),    # shipping something never opened
        ("planned", "closing"),     # closing something never started
        ("in_flight", "released"),  # skips the pass where notes are generated
        ("released", "in_flight"),  # released is terminal
        ("released", "closing"),    # released is terminal
        ("abandoned", "planned"),   # abandoned is terminal
        ("abandoned", "in_flight"),
    ],
)
def test_illegal_arch_release_edges_are_422(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="release", state=from_state)
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "illegal_transition"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not session.committed


def test_a_release_cannot_skip_closing():
    """`closing` is where notes are generated, and where an undelivered outcome
    is found. Allowing in_flight -> released would let a release be declared
    shipped without anything ever checking what it actually delivered."""
    assert "released" not in STATE_MACHINES[("arch", "release")]["in_flight"]
    assert "released" in STATE_MACHINES[("arch", "release")]["closing"]


def test_closing_can_fall_back_to_in_flight():
    """The honest response to finding an outcome undelivered at close.

    Without this edge the only options would be to ship notes that overclaim,
    or to abandon a release that is merely late.
    """
    assert "in_flight" in STATE_MACHINES[("arch", "release")]["closing"]


def test_released_and_abandoned_are_terminal():
    machine = STATE_MACHINES[("arch", "release")]
    assert machine["released"] == frozenset()
    assert machine["abandoned"] == frozenset()


def _valid_release_content() -> dict:
    return {
        "ref": "R26.01",
        "name": "Autonomy earns its amendment",
        "objective": "The code-health lane merges unattended, and we can undo it.",
        "sprints": ["33", "34", "35"],
        "outcomes": [
            {
                "id": "O-1",
                "statement": "A code-health change merges without a human at the gate.",
                "work_class": "enabling",
            }
        ],
        "declared_balance": {"enabling": 100},
        "opened_at": "2026-08-25",
    }


def _create_release(client, **overrides):
    payload = {
        "namespace": "arch",
        "type": "release",
        "state": "planned",
        "content": _valid_release_content(),
        "trust_tier": "user",
        "created_by": "test",
    }
    payload.update(overrides)
    return client.post("/beads", json=payload, headers=HEADERS)


def test_create_release_at_declared_entry_state_succeeds(app):
    session = _FakeCreateSession()

    resp = _create_release(_client(app, session), state="planned")

    assert resp.status_code == 200
    assert session.committed


@pytest.mark.parametrize("state", ["in_flight", "closing", "released", "abandoned"])
def test_create_release_at_undeclared_entry_state_is_422(app, state):
    """A loader must not be able to assert the outcome of work that has not run.

    docs/releases/*.json is authoritative for a charter's structure; it is not
    authoritative for whether the release shipped.
    """
    session = _FakeCreateSession()

    resp = _create_release(_client(app, session), state=state)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_entry_state"
    assert detail["machine"] == "arch.release"
    assert detail["allowed"] == ["planned"]
    assert session.added == [], "an illegal entry state must not reach the database"
    assert not session.committed


# --- arch.incident: the third declared machine -------------------------------
#
# Registered in bead_rules.py per the 2026-09-03 decision record's standing
# proposal (docs/architecture/itsm-target-state.md §1). The entire point of
# the R26.06 registry lift is that this machine needs no code here: every
# STATE_MACHINES / STATE_MACHINE_ENTRY_STATES consult below is the same
# generic dict lookup that already served dev.task and arch.release.


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("detected", "mitigating"),
        ("detected", "closed"),
        ("mitigating", "resolved"),
        ("mitigating", "detected"),
        ("resolved", "closed"),
        ("resolved", "detected"),
    ],
)
def test_legal_arch_incident_edges_are_allowed(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="incident", state=from_state)
    session = _FakeSession(bead=bead)
    session._target_state = to_state

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 200


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("detected", "resolved"),      # skips mitigating
        ("mitigating", "closed"),      # mitigation must resolve before closing
        ("closed", "detected"),        # closed is terminal
        ("closed", "mitigating"),      # closed is terminal
        ("closed", "resolved"),        # closed is terminal
    ],
)
def test_illegal_arch_incident_edges_are_422(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="incident", state=from_state)
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "illegal_transition"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not session.committed


def test_an_incident_cannot_skip_mitigating_to_close():
    """Closing mid-fix without ever declaring resolved would lose the record
    of what the resolution was."""
    assert "closed" not in STATE_MACHINES[("arch", "incident")]["mitigating"]
    assert "resolved" in STATE_MACHINES[("arch", "incident")]["mitigating"]


def test_incident_closed_is_terminal():
    assert STATE_MACHINES[("arch", "incident")]["closed"] == frozenset()


def test_incident_reopens_to_detected_from_mitigating_and_resolved():
    """Recurrence within a declared window reopens; a later recurrence outside
    it is a new incident bead, not a further reopen of this one."""
    machine = STATE_MACHINES[("arch", "incident")]
    reachable_from = {state for state, edges in machine.items() if "detected" in edges}
    assert reachable_from == {"mitigating", "resolved"}


def _valid_incident_content() -> dict:
    return {
        "summary": "Bank-sync thundering herd overwhelmed the connections pool",
        "severity": "urgent",
        "detected_at": "2026-07-18T14:03:00Z",
        "source": "alert:bank-sync-thundering-herd",
        "applications": ["lifeops-console"],
    }


def _create_incident(client, **overrides):
    payload = {
        "namespace": "arch",
        "type": "incident",
        "state": "detected",
        "content": _valid_incident_content(),
        "trust_tier": "user",
        "created_by": "test",
    }
    payload.update(overrides)
    return client.post("/beads", json=payload, headers=HEADERS)


def test_create_incident_at_declared_entry_state_succeeds(app):
    session = _FakeCreateSession()

    resp = _create_incident(_client(app, session), state="detected")

    assert resp.status_code == 200
    assert session.committed


@pytest.mark.parametrize("state", ["mitigating", "resolved", "closed"])
def test_create_incident_at_undeclared_entry_state_is_422(app, state):
    """Nothing may create an incident already mitigating, resolved, or closed
    -- that would assert a response happened before the triggering record
    exists."""
    session = _FakeCreateSession()

    resp = _create_incident(_client(app, session), state=state)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_entry_state"
    assert detail["machine"] == "arch.incident"
    assert detail["allowed"] == ["detected"]
    assert session.added == [], "an illegal entry state must not reach the database"
    assert not session.committed


def test_patch_rejects_undeclared_incident_transition_without_commit(app):
    """The machined-type PATCH guard that holds for dev.task and arch.release
    (test_patch_rejects_undeclared_dev_task_transition_without_commit) holds
    for arch.incident too, because both are the same generic dict lookup."""
    bead = _fake_bead(namespace="arch", type_="incident", state="detected")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "resolved"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_transition"
    assert detail["from_state"] == "detected"
    assert detail["to_state"] == "resolved"
    assert detail["allowed"] == ["closed", "mitigating"]
    assert bead.state == "detected"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not any(isinstance(o, BeadEvent) for o in session.added)
    assert not session.committed


def test_incident_transition_is_the_same_compare_and_set_mechanism(app):
    """A legal move on an arch.incident bead goes through the identical
    conditional UPDATE ... WHERE state = :from_state that
    test_claim_is_a_conditional_update proves for dev.task -- there is no
    per-type branch in transition_bead, so proving the mechanism once per
    type is proving it never diverged."""
    bead = _fake_bead(namespace="arch", type_="incident", state="detected")
    session = _FakeSession(bead=bead)
    session._target_state = "mitigating"

    resp = _transition(_client(app, session), bead.id, "detected", "mitigating")

    assert resp.status_code == 200
    updates = session.updates()
    assert len(updates) == 1
    sql = str(updates[0])
    assert "bead.id" in sql
    assert "bead.state" in sql


def test_incident_machine_registration_touches_no_route_code():
    """Registering the third machine required editing only bead_rules.py.

    routes.py's STATE_MACHINES/STATE_MACHINE_ENTRY_STATES consults are
    generic dict lookups keyed by (namespace, type); proving that, rather
    than asserting it, means showing routes.py's source names none of the
    incident machine's states or the dotted type name -- "incident" alone is
    too broad a substring (it appears in an unrelated log message), so this
    checks the specific vocabulary a hand-added special case would need.
    """
    routes_source = (
        Path(__file__).resolve().parents[1] / "src" / "routes.py"
    ).read_text()
    assert "arch.incident" not in routes_source
    assert "mitigating" not in routes_source
    assert '"detected"' not in routes_source


# --- dev.finding: the fourth declared machine (R26.05/O-10) ----------------
#
# Eight dev.finding beads filed 2026-08-03 and 2026-08-06 sat in "pending"
# for 35 days: with no entry here, STATE_MACHINES.get(("dev","finding")) was
# None, so every consult in routes.py treated the pair as permissive and no
# transition -- compare-and-set or bare PATCH -- could ever record what
# happened to one. Registering the machine needs no route code, same as
# arch.incident before it: every STATE_MACHINES / STATE_MACHINE_ENTRY_STATES
# consult below is the identical generic dict lookup.


def test_finding_entry_state_is_pending():
    """The eight live findings this machine was written for are already
    "pending"; if entry states didn't already include it, registering this
    machine would invalidate every one of them and demand a migration.
    Entry states only gate creation, never existing rows, but "pending" must
    still be declared here or a *new* finding could never be filed."""
    assert STATE_MACHINE_ENTRY_STATES[("dev", "finding")] == frozenset({"pending"})
    assert "pending" in STATE_MACHINES[("dev", "finding")]


@pytest.mark.parametrize(
    "to_state",
    ["backlogged", "ruled", "already_fixed", "not_a_defect"],
)
def test_legal_dev_finding_edges_are_allowed(app, to_state):
    """CLAUDE.md's absorption rule names two outcomes for an absorbed finding
    -- a backlog item ("backlogged") or a rule ("ruled") -- and this machine
    adds the two ways a finding closes without absorbing into either:
    "already_fixed" and "not_a_defect". All four are one edge from pending,
    so a disposition can be recorded rather than implied by the bead
    disappearing."""
    bead = _fake_bead(namespace="dev", type_="finding", state="pending")
    session = _FakeSession(bead=bead)
    session._target_state = to_state

    resp = _transition(_client(app, session), bead.id, "pending", to_state)

    assert resp.status_code == 200


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("backlogged", "pending"),   # backlogged is terminal
        ("ruled", "pending"),        # ruled is terminal
        ("already_fixed", "pending"),  # already_fixed is terminal
        ("not_a_defect", "pending"),   # not_a_defect is terminal
        ("pending", "pending"),      # not a real edge
    ],
)
def test_illegal_dev_finding_edges_are_422(app, from_state, to_state):
    bead = _fake_bead(namespace="dev", type_="finding", state=from_state)
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "illegal_transition"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not session.committed


def test_dev_finding_terminal_states_have_no_outgoing_edges():
    machine = STATE_MACHINES[("dev", "finding")]
    for terminal in ("backlogged", "ruled", "already_fixed", "not_a_defect"):
        assert machine[terminal] == frozenset()


def test_patch_rejects_undeclared_dev_finding_transition_without_commit(app):
    """The machined-type PATCH guard that holds for dev.task, arch.release and
    arch.incident (test_patch_rejects_undeclared_dev_task_transition_without_commit,
    test_patch_rejects_undeclared_incident_transition_without_commit) holds
    for dev.finding too -- attempting the bypass: a bare PATCH trying to move
    a pending finding straight to an edge the machine does not declare."""
    bead = _fake_bead(namespace="dev", type_="finding", state="backlogged")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "pending"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_transition"
    assert detail["machine"] == "dev.finding"
    assert detail["from_state"] == "backlogged"
    assert detail["to_state"] == "pending"
    assert bead.state == "backlogged"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not any(isinstance(o, BeadEvent) for o in session.added)
    assert not session.committed


def _valid_finding_content() -> dict:
    return {
        "kind": "bug",
        "disposition": "backlog",
        "severity": "medium",
        "summary": "Example finding for machine-registration tests.",
    }


def _create_finding(client, **overrides):
    payload = {
        "namespace": "dev",
        "type": "finding",
        "state": "pending",
        "content": _valid_finding_content(),
        "trust_tier": "user",
        "created_by": "test",
    }
    payload.update(overrides)
    return client.post("/beads", json=payload, headers=HEADERS)


def test_create_finding_at_declared_entry_state_succeeds(app):
    session = _FakeCreateSession()

    resp = _create_finding(_client(app, session), state="pending")

    assert resp.status_code == 200
    assert session.committed


@pytest.mark.parametrize(
    "state", ["backlogged", "ruled", "already_fixed", "not_a_defect"]
)
def test_create_finding_at_undeclared_entry_state_is_422(app, state):
    """Nothing may create a finding already dispositioned -- that would
    assert an absorption outcome happened before the finding itself was
    filed."""
    session = _FakeCreateSession()

    resp = _create_finding(_client(app, session), state=state)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_entry_state"
    assert detail["machine"] == "dev.finding"
    assert detail["allowed"] == ["pending"]
    assert session.added == [], "an illegal entry state must not reach the database"
    assert not session.committed


def test_finding_machine_registration_touches_no_route_code():
    """Same proof as test_incident_machine_registration_touches_no_route_code:
    registering the fourth machine required editing only bead_rules.py."""
    routes_source = (
        Path(__file__).resolve().parents[1] / "src" / "routes.py"
    ).read_text()
    assert "dev.finding" not in routes_source
    assert "backlogged" not in routes_source
    assert "already_fixed" not in routes_source
    assert "not_a_defect" not in routes_source


# --- arch.risk: the fifth declared machine (R26.09/O-5) ---------------------
#
# Entered "accepted" only: a register of risks already accepted, not a
# proposal queue for risks under consideration. "under_review" -> "accepted"
# is the renewal edge (filed with a fresh review_by); "under_review" ->
# "retired" closes a risk that no longer applies. "retired" is terminal.


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("accepted", "under_review"),
        ("under_review", "retired"),
        ("under_review", "accepted"),
    ],
)
def test_legal_arch_risk_edges_are_allowed(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="risk", state=from_state)
    session = _FakeSession(bead=bead)
    session._target_state = to_state

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 200


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("accepted", "retired"),       # skips under_review
        ("retired", "accepted"),       # retired is terminal
        ("retired", "under_review"),   # retired is terminal
    ],
)
def test_illegal_arch_risk_edges_are_422(app, from_state, to_state):
    bead = _fake_bead(namespace="arch", type_="risk", state=from_state)
    session = _FakeSession(bead=bead)

    resp = _transition(_client(app, session), bead.id, from_state, to_state)

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "illegal_transition"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not session.committed


def test_a_risk_cannot_skip_review_to_retire():
    """A risk must pass through under_review before it retires -- otherwise
    nothing ever looked at it again before closing it out."""
    assert "retired" not in STATE_MACHINES[("arch", "risk")]["accepted"]
    assert "retired" in STATE_MACHINES[("arch", "risk")]["under_review"]


def test_risk_retired_is_terminal():
    assert STATE_MACHINES[("arch", "risk")]["retired"] == frozenset()


def test_risk_renews_from_under_review_back_to_accepted():
    """The renewal edge: a risk survives its review and gets a fresh
    review_by, rather than the only options being retire it or leave it
    stuck in under_review forever."""
    assert "accepted" in STATE_MACHINES[("arch", "risk")]["under_review"]


def _valid_risk_content() -> dict:
    return {
        "statement": "The control plane runs on a single node with no redundancy.",
        "owner": "the Operator",
        "accepted_at": "2026-05-30",
        "review_by": "2026-11-30",
        "severity": "high",
        "decision_ref": "ARCHITECTURE.md#amendment-18",
    }


def _create_risk(client, **overrides):
    payload = {
        "namespace": "arch",
        "type": "risk",
        "state": "accepted",
        "content": _valid_risk_content(),
        "trust_tier": "user",
        "created_by": "test",
    }
    payload.update(overrides)
    return client.post("/beads", json=payload, headers=HEADERS)


def test_create_risk_at_declared_entry_state_succeeds(app):
    session = _FakeCreateSession()

    resp = _create_risk(_client(app, session), state="accepted")

    assert resp.status_code == 200
    assert session.committed


@pytest.mark.parametrize("state", ["under_review", "retired"])
def test_create_risk_at_undeclared_entry_state_is_422(app, state):
    """Nothing may create a risk already under review or retired -- that
    would assert a lifecycle happened before the risk itself was accepted."""
    session = _FakeCreateSession()

    resp = _create_risk(_client(app, session), state=state)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_entry_state"
    assert detail["machine"] == "arch.risk"
    assert detail["allowed"] == ["accepted"]
    assert session.added == [], "an illegal entry state must not reach the database"
    assert not session.committed


def test_patch_rejects_undeclared_risk_transition_without_commit(app):
    """The machined-type PATCH guard that holds for dev.task, arch.release,
    arch.incident and dev.finding holds for arch.risk too, because all five
    are the same generic dict lookup."""
    bead = _fake_bead(namespace="arch", type_="risk", state="accepted")
    session = _FakeSession(bead=bead)

    resp = _patch(_client(app, session), bead.id, {"state": "retired"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "illegal_transition"
    assert detail["from_state"] == "accepted"
    assert detail["to_state"] == "retired"
    assert detail["allowed"] == ["under_review"]
    assert bead.state == "accepted"
    assert session.updates() == [], "an illegal edge must not reach the database"
    assert not any(isinstance(o, BeadEvent) for o in session.added)
    assert not session.committed


def test_risk_machine_registration_touches_no_route_code():
    """Same proof as test_incident_machine_registration_touches_no_route_code
    and test_finding_machine_registration_touches_no_route_code: registering
    the fifth machine required editing only bead_rules.py."""
    routes_source = (
        Path(__file__).resolve().parents[1] / "src" / "routes.py"
    ).read_text()
    assert "arch.risk" not in routes_source
    assert "under_review" not in routes_source
    assert '"retired"' not in routes_source
