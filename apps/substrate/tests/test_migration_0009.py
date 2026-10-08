"""0009_repair_stranded_incident_pending_state — the chosen repair, exercised.

Finding 1 (the stranded-incident spec): bead d861f65e-74ff-437e-a068-05e16b49c99a
was created before arch.incident's machine (R2605-1) existed, in state
'pending' -- a state the machine has never declared, so both
POST /beads/{id}/transition and a state-changing PATCH refuse every edge out
of it (machine.get('pending', frozenset()) is always empty). This migration is
the reviewed repair, option (b) as reasoned in migration 0009's module
docstring: move the one named
bead to 'detected', recorded as its own bead_event rather than a bare mutation.

What is proven here without a database: the module loads, the SQL names the
right bead/states/namespace/type, and -- mechanically, not asserted by
authority -- that 'pending' really is absent from the declared machine and
'detected' really is present in it, so the repair target is legal.

What is proven here WITH a database (skipped without SUBSTRATE_TEST_DATABASE_URL,
same convention as test_migration_0008.py): running upgrade() against a temp
table built to d861f65e's exact shape actually moves it, records the
bead_event, and is idempotent -- a second run, or a run against a bead that
has since progressed normally through the machine, is a no-op rather than an
error or a silent overwrite of live progress. None of this touches a real bead
or production: the table is temporary and dropped at commit.
"""

import importlib.util
import json
import os
import pathlib
from types import SimpleNamespace

import pytest

from src.bead_rules import STATE_MACHINES

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0009_repair_stranded_incident_pending_state.py"
)


def _load():
    pytest.importorskip("alembic")
    spec = importlib.util.spec_from_file_location("m0009", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sync_database_url() -> str | None:
    url = os.environ.get("SUBSTRATE_TEST_DATABASE_URL")
    if not url:
        return None
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


# --- what is provable with no database --------------------------------------


def test_migration_module_imports():
    mod = _load()
    assert mod.revision == "0009_repair_stranded_incident"
    assert mod.down_revision == "0008_unique_spec_identity"


def test_revision_id_fits_alembics_varchar32_version_column():
    mod = _load()
    assert len(mod.revision) <= 32


def test_targets_the_exact_bead_finding_1_named():
    mod = _load()
    assert mod.INCIDENT_ID == "d861f65e-74ff-437e-a068-05e16b49c99a"
    assert mod.STRANDED_STATE == "pending"
    assert mod.REPAIR_STATE == "detected"


def test_stranded_state_is_genuinely_absent_from_the_declared_machine():
    """Mechanical proof this migration is needed at all: 'pending' is not a
    key arch.incident's machine declares, so no governed transition can ever
    move a bead out of it -- machine.get('pending', frozenset()) is always
    empty, for every possible to_state."""
    machine = STATE_MACHINES[("arch", "incident")]
    assert "pending" not in machine


def test_repair_target_state_is_a_real_declared_state():
    """The migration must not repair a stranded bead into a second kind of
    stranded -- 'detected' has to actually be a key of the declared machine."""
    machine = STATE_MACHINES[("arch", "incident")]
    assert "detected" in machine


def test_upgrade_sql_names_the_incident_and_both_states():
    mod = _load()
    sql = " ".join(mod.UPDATE_STATE_SQL.text.split())
    assert "UPDATE bead" in sql
    assert "SET state = :repair_state" in sql
    assert "namespace = 'arch'" in sql
    assert "type = 'incident'" in sql
    assert "state = :stranded_state" in sql
    assert "RETURNING id" in sql


def test_downgrade_sql_only_reverts_a_bead_still_in_the_repaired_state():
    mod = _load()
    sql = " ".join(mod.REVERT_STATE_SQL.text.split())
    assert "SET state = :stranded_state" in sql
    assert "state = :repair_state" in sql, (
        "downgrade must only revert a bead still exactly in the state this "
        "migration produced -- reverting from any other state would clobber "
        "progress the machine made after the repair"
    )


def test_repair_is_recorded_as_a_migrated_event_not_a_transition():
    """PRIN-008: the bead's own history must keep saying honestly that this
    state was reached by a reviewed migration, not an edge the machine
    actually governs."""
    mod = _load()
    sql = " ".join(mod.INSERT_EVENT_SQL.text.split())
    assert "'migrated'" in sql
    assert "bead_event" in sql


# --- what is provable with a real database -----------------------------------


@pytest.fixture
def _pg_conn():
    url = _sync_database_url()
    if not url:
        pytest.skip("requires SUBSTRATE_TEST_DATABASE_URL pointing at PostgreSQL")
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.exc import OperationalError

    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    CREATE TEMP TABLE bead (
                        id uuid PRIMARY KEY,
                        namespace text NOT NULL,
                        type text NOT NULL,
                        state text NOT NULL
                    ) ON COMMIT DROP;
                    CREATE TEMP TABLE bead_event (
                        id uuid PRIMARY KEY,
                        bead_id uuid NOT NULL,
                        event_type text NOT NULL,
                        from_state text,
                        to_state text,
                        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                        created_by text NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now()
                    ) ON COMMIT DROP;
                    """
                )
            )
            yield conn
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is unavailable: {exc}")
    finally:
        engine.dispose()


def _insert_d861f65e_shaped_bead(conn, mod, sa, state):
    conn.execute(
        sa.text(
            "INSERT INTO bead (id, namespace, type, state) "
            "VALUES (CAST(:id AS uuid), 'arch', 'incident', :state)"
        ),
        {"id": mod.INCIDENT_ID, "state": state},
    )


def test_upgrade_repairs_the_stranded_bead_and_records_the_event(_pg_conn):
    mod = _load()
    sa = pytest.importorskip("sqlalchemy")
    conn = _pg_conn
    _insert_d861f65e_shaped_bead(conn, mod, sa, mod.STRANDED_STATE)

    monkeypatch_op = SimpleNamespace(get_bind=lambda: conn)
    mod.op = monkeypatch_op
    mod.upgrade()

    state = conn.execute(
        sa.text("SELECT state FROM bead WHERE id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert state == mod.REPAIR_STATE

    events = conn.execute(
        sa.text(
            "SELECT event_type, from_state, to_state, payload FROM bead_event "
            "WHERE bead_id = CAST(:id AS uuid)"
        ),
        {"id": mod.INCIDENT_ID},
    ).fetchall()
    assert len(events) == 1
    event_type, from_state, to_state, payload = events[0]
    assert event_type == "migrated"
    assert from_state == mod.STRANDED_STATE
    assert to_state == mod.REPAIR_STATE
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["reason"]


def test_upgrade_is_idempotent_on_a_second_run(_pg_conn):
    mod = _load()
    sa = pytest.importorskip("sqlalchemy")
    conn = _pg_conn
    _insert_d861f65e_shaped_bead(conn, mod, sa, mod.STRANDED_STATE)
    mod.op = SimpleNamespace(get_bind=lambda: conn)

    mod.upgrade()
    mod.upgrade()

    events = conn.execute(
        sa.text("SELECT count(*) FROM bead_event WHERE bead_id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert events == 1, "a second run must not record a second migration event"


def test_upgrade_does_not_clobber_a_bead_that_progressed_normally(_pg_conn):
    """A bead already moved past 'detected' by a real governed transition must
    not be touched -- the WHERE clause matches only the exact stranded shape."""
    mod = _load()
    sa = pytest.importorskip("sqlalchemy")
    conn = _pg_conn
    _insert_d861f65e_shaped_bead(conn, mod, sa, "mitigating")
    mod.op = SimpleNamespace(get_bind=lambda: conn)

    mod.upgrade()

    state = conn.execute(
        sa.text("SELECT state FROM bead WHERE id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert state == "mitigating"
    events = conn.execute(
        sa.text("SELECT count(*) FROM bead_event WHERE bead_id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert events == 0


def test_downgrade_reverts_only_from_the_exact_repaired_state(_pg_conn):
    mod = _load()
    sa = pytest.importorskip("sqlalchemy")
    conn = _pg_conn
    _insert_d861f65e_shaped_bead(conn, mod, sa, mod.STRANDED_STATE)
    mod.op = SimpleNamespace(get_bind=lambda: conn)

    mod.upgrade()
    mod.downgrade()

    state = conn.execute(
        sa.text("SELECT state FROM bead WHERE id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert state == mod.STRANDED_STATE


def test_downgrade_refuses_to_revert_a_bead_that_moved_on(_pg_conn):
    mod = _load()
    sa = pytest.importorskip("sqlalchemy")
    conn = _pg_conn
    _insert_d861f65e_shaped_bead(conn, mod, sa, "resolved")
    mod.op = SimpleNamespace(get_bind=lambda: conn)

    mod.downgrade()

    state = conn.execute(
        sa.text("SELECT state FROM bead WHERE id = CAST(:id AS uuid)"),
        {"id": mod.INCIDENT_ID},
    ).scalar_one()
    assert state == "resolved", "downgrade must never revert a state it did not produce"
