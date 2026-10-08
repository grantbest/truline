import importlib.util
import os
import pathlib

import pytest

from src.crypto import ENCRYPTED_NAMESPACES


MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0008_unique_spec_identity.py"
)


def _load():
    pytest.importorskip("alembic")
    spec = importlib.util.spec_from_file_location("m0008", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sync_database_url() -> str | None:
    url = os.environ.get("SUBSTRATE_TEST_DATABASE_URL")
    if not url:
        return None
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


def test_dev_namespace_is_not_encrypted_so_spec_identity_needs_no_plaintext_key():
    assert "dev" not in ENCRYPTED_NAMESPACES


def test_migration_module_imports():
    mod = _load()
    assert mod.revision == "0008_unique_spec_identity"
    assert mod.down_revision == "0007_encrypt_bead_event_payloads"


def test_upgrade_sql_is_unique_partial_spec_identity_index():
    mod = _load()
    sql = " ".join(mod.CREATE_SPEC_IDENTITY_INDEX_SQL.split())

    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_dev_task_spec_identity_live" in sql
    )
    assert "ON bead ((content->>'spec_identity'))" in sql
    assert "namespace = 'dev'" in sql
    assert "type = 'task'" in sql
    assert "state IN ('pending', 'doing', 'review')" in sql
    assert "content->>'spec_identity' IS NOT NULL" in sql


def test_downgrade_sql_is_idempotent():
    mod = _load()
    assert (
        mod.DROP_SPEC_IDENTITY_INDEX_SQL
        == "DROP INDEX IF EXISTS idx_unique_dev_task_spec_identity_live;"
    )


def test_duplicate_pending_spec_identity_is_rejected_by_postgres_when_available():
    url = _sync_database_url()
    if not url:
        pytest.skip("requires SUBSTRATE_TEST_DATABASE_URL pointing at PostgreSQL")

    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.exc import IntegrityError, OperationalError

    mod = _load()
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    CREATE TEMP TABLE bead (
                        id text PRIMARY KEY,
                        namespace text NOT NULL,
                        type text NOT NULL,
                        state text NOT NULL,
                        content jsonb NOT NULL DEFAULT '{}'::jsonb
                    ) ON COMMIT DROP;
                    """
                )
            )
            conn.execute(sa.text(mod.CREATE_SPEC_IDENTITY_INDEX_SQL))
            # Idempotence against a database where the index already exists.
            conn.execute(sa.text(mod.CREATE_SPEC_IDENTITY_INDEX_SQL))

            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, type, state, content) "
                    "VALUES ('dev-task-1', 'dev', 'task', 'pending', "
                    "'{\"spec_identity\":\"tasks/OPS-68.json\"}'::jsonb)"
                )
            )

            # A second concurrently-created pending row for the same identity
            # is exactly the OPS-68 race — the store must refuse it.
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(
                        sa.text(
                            "INSERT INTO bead (id, namespace, type, state, content) "
                            "VALUES ('dev-task-2', 'dev', 'task', 'pending', "
                            "'{\"spec_identity\":\"tasks/OPS-68.json\"}'::jsonb)"
                        )
                    )

            # A --supersede re-file is unaffected: once the original is no
            # longer 'pending', the same identity is free again.
            conn.execute(
                sa.text("UPDATE bead SET state = 'superseded' WHERE id = 'dev-task-1'")
            )
            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, type, state, content) "
                    "VALUES ('dev-task-3', 'dev', 'task', 'pending', "
                    "'{\"spec_identity\":\"tasks/OPS-68.json\"}'::jsonb)"
                )
            )

            # Different namespace/type sharing the same JSON key never collides.
            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, type, state, content) "
                    "VALUES ('arch-1', 'arch', 'application', 'pending', "
                    "'{\"spec_identity\":\"tasks/OPS-68.json\"}'::jsonb)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, type, state, content) "
                    "VALUES ('dev-task-4', 'dev', 'task', 'pending', '{}'::jsonb), "
                    "('dev-task-5', 'dev', 'task', 'pending', '{}'::jsonb)"
                )
            )
            conn.execute(sa.text(mod.DROP_SPEC_IDENTITY_INDEX_SQL))
            conn.execute(sa.text(mod.DROP_SPEC_IDENTITY_INDEX_SQL))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is unavailable: {exc}")
    finally:
        engine.dispose()


def test_duplicate_is_rejected_while_the_winner_is_doing_or_review():
    """The widened scope's own case (release-gate re-verify nit): the loser's
    read-to-create window spans a minutes-long pristine verification, so the
    winner is routinely already claimed when the duplicate lands. The index
    must refuse against a doing or review winner, not only a pending one."""
    url = _sync_database_url()
    if not url:
        pytest.skip("requires SUBSTRATE_TEST_DATABASE_URL pointing at PostgreSQL")

    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.exc import IntegrityError

    mod = _load()
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                CREATE TEMP TABLE bead (
                    id text PRIMARY KEY,
                    namespace text NOT NULL,
                    type text NOT NULL,
                    state text NOT NULL,
                    content jsonb NOT NULL DEFAULT '{}'::jsonb
                ) ON COMMIT DROP;
                """
            )
        )
        conn.execute(sa.text(mod.CREATE_SPEC_IDENTITY_INDEX_SQL))
        for i, winner_state in enumerate(("doing", "review")):
            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, type, state, content) "
                    f"VALUES ('winner-{i}', 'dev', 'task', '{winner_state}', "
                    "'{\"spec_identity\":\"tasks/claimed.json\"}'::jsonb)"
                )
            )
            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(
                        sa.text(
                            "INSERT INTO bead (id, namespace, type, state, content) "
                            f"VALUES ('loser-{i}', 'dev', 'task', 'pending', "
                            "'{\"spec_identity\":\"tasks/claimed.json\"}'::jsonb)"
                        )
                    )
            conn.execute(sa.text(f"DELETE FROM bead WHERE id = 'winner-{i}'"))
        conn.execute(sa.text(mod.DROP_SPEC_IDENTITY_INDEX_SQL))
