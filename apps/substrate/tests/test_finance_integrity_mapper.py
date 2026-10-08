"""The finance mapper's constraint-name scoping — finding e806eaed.

Measured 2026-09-19: the finance integrity mapper matched ANY 23505 whose
text contained the generic Postgres phrase "duplicate key value violates
unique constraint" — every unique violation carries it — so an
arch.observation duplicate on migration 0006's ``idx_unique_arch_ref`` index
was reported as a plaid-duplicate 409 for three days (215 ``EaApplyWorkflow``
runs from 2026-09-17T00:15Z) before the real defect (a duplicate-filing EA
reconciler) was found. The mapper must claim only the two unique indexes
finance actually owns (migrations 0002, 0003), by name — and ``create_bead``
must consult a namespace's mapper only for a write to that namespace.
"""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import src.cache
from src.database import get_db
from src.finance_integrity import (
    _map_finance_integrity_error,
    _violated_finance_constraint,
)
from src.routes import router


HEADERS = {"x-api-key": "test-key"}


class _FakeOrig:
    def __init__(self, sqlstate: str, text: str):
        self.sqlstate = sqlstate
        self._text = text

    def __str__(self):
        return self._text


def _integrity_error(sqlstate: str, text: str) -> IntegrityError:
    return IntegrityError("INSERT INTO bead ...", {}, _FakeOrig(sqlstate, text))


def _plaid_duplicate() -> IntegrityError:
    # Realistic asyncpg/psycopg shape: a quoted constraint name followed by a
    # DETAIL line naming the JSON-path key and the colliding value.
    return _integrity_error(
        "23505",
        'duplicate key value violates unique constraint "idx_unique_plaid_id"\n'
        "DETAIL:  Key ((content ->> 'plaid_transaction_id'::text))=(txn-42) "
        "already exists.",
    )


def _account_mask_duplicate() -> IntegrityError:
    return _integrity_error(
        "23505",
        'duplicate key value violates unique constraint "idx_unique_account_mask"\n'
        "DETAIL:  Key ((content ->> 'institution'::text), (content ->> "
        "'mask'::text))=(chase, 1234) already exists.",
    )


def _arch_ref_duplicate() -> IntegrityError:
    return _integrity_error(
        "23505",
        'duplicate key value violates unique constraint "idx_unique_arch_ref"\n'
        "DETAIL:  Key ((content ->> 'ref'::text))=(app.substrate) already "
        "exists.",
    )


# --- the mapper itself: named constraints only, no generic phrase match -----


def test_finance_mapper_claims_plaid_id_by_name():
    assert _violated_finance_constraint(_plaid_duplicate()) == "idx_unique_plaid_id"
    mapped = _map_finance_integrity_error(_plaid_duplicate())
    assert mapped is not None
    assert mapped.status_code == 409
    assert "plaid_transaction_id" in mapped.detail


def test_finance_mapper_claims_account_mask_by_name():
    assert (
        _violated_finance_constraint(_account_mask_duplicate())
        == "idx_unique_account_mask"
    )
    mapped = _map_finance_integrity_error(_account_mask_duplicate())
    assert mapped is not None
    assert mapped.status_code == 409


def test_finance_mapper_does_not_claim_an_unrelated_unique_violation():
    """The regression: a constraint finance does not own must fall through,
    even though its text carries the same generic Postgres phrasing every
    finance violation also carries."""
    assert _violated_finance_constraint(_arch_ref_duplicate()) is None
    assert _map_finance_integrity_error(_arch_ref_duplicate()) is None


def test_finance_mapper_does_not_claim_a_nameless_generic_unique_violation():
    generic = _integrity_error(
        "23505", "duplicate key value violates unique constraint"
    )
    assert _violated_finance_constraint(generic) is None
    assert _map_finance_integrity_error(generic) is None


# --- namespace-scoped dispatch through create_bead --------------------------


class _FakeCreateBeadSession:
    """Stands in for AsyncSession across ``create_bead`` — see
    ``tests/test_create_bead_parent_fk.py`` for the pattern this mirrors.
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


def _app(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(src.cache, "DISABLE_CACHE", True)
    a = FastAPI()
    a.include_router(router)
    return a


def _client(app, session):
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def _finance_transaction_body():
    return {
        "namespace": "finance",
        "type": "transaction",
        "state": "open",
        "content": {
            "amount": 12.34,
            "merchant_name": "Coffee Shop",
            "normalized_merchant": "coffee shop",
            "posted_date": "2026-09-01",
            "plaid_transaction_id": "txn-42",
            "account_id": str(uuid4()),
        },
        "trust_tier": "system",
        "created_by": "plaid-sync",
    }


def _arch_observation_body():
    # No "ref" — a fresh mint has no existing bead to run the source-class
    # write-violation pre-check against, so this reaches the same flush()
    # the DB-level unique index (migration 0006) actually enforces.
    return {
        "namespace": "arch",
        "type": "observation",
        "state": "open",
        "content": {
            "observed_at": "2026-09-17T00:15:00Z",
            "workload": {
                "cluster": "prod",
                "namespace": "substrate",
                "kind": "Deployment",
                "name": "substrate",
            },
        },
        "trust_tier": "system",
        # A non-automated writer: with no declared source_class, content
        # defaults to "authored" (HUMAN_EXEMPTION), which only a
        # non-automated writer may mint — this keeps the admission gate out
        # of this test's way so it exercises the flush()-time unique
        # violation, not source-class enrollment.
        "created_by": "test-user",
    }


def test_finance_write_violating_plaid_id_still_gets_the_plaid_message(monkeypatch):
    app = _app(monkeypatch)
    session = _FakeCreateBeadSession(flush_exc=_plaid_duplicate())

    resp = _client(app, session).post(
        "/beads", json=_finance_transaction_body(), headers=HEADERS
    )

    assert resp.status_code == 409
    assert "plaid_transaction_id" in resp.json()["detail"]
    assert session.rolled_back is True


def test_arch_write_violating_arch_ref_gets_a_namespace_neutral_409_naming_it(
    monkeypatch,
):
    """The regression this bead fixes: this used to come back as the plaid
    duplicate 409 (finding e806eaed) because the finance mapper matched the
    generic Postgres phrase regardless of which constraint fired, and
    ``create_bead`` consulted every registered mapper regardless of the
    write's own namespace."""
    app = _app(monkeypatch)
    session = _FakeCreateBeadSession(flush_exc=_arch_ref_duplicate())

    resp = _client(app, session).post(
        "/beads", json=_arch_observation_body(), headers=HEADERS
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "unique_constraint_violation"
    assert detail["constraint"] == "idx_unique_arch_ref"
    assert "plaid" not in str(detail).lower()
    assert session.rolled_back is True
