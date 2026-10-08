import importlib.util
import os
import pathlib

import pytest

from src.crypto import PLAINTEXT_KEYS


MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0006_unique_arch_ref.py"
)


def _load():
    pytest.importorskip("alembic")
    spec = importlib.util.spec_from_file_location("m0006", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sync_database_url() -> str | None:
    url = os.environ.get("SUBSTRATE_TEST_DATABASE_URL")
    if not url:
        return None
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


def test_ref_is_plaintext_for_database_constraints():
    assert "ref" in PLAINTEXT_KEYS


def test_migration_module_imports():
    mod = _load()
    assert mod.revision == "0006_unique_arch_ref"
    assert mod.down_revision == "0005_decrypt_non_finance"


def test_upgrade_sql_is_unique_partial_arch_ref_index():
    mod = _load()
    sql = " ".join(mod.CREATE_ARCH_REF_INDEX_SQL.split())

    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_arch_ref" in sql
    assert "ON bead ((content->>'ref'))" in sql
    assert "namespace = 'arch'" in sql
    assert "content->>'ref' IS NOT NULL" in sql


def test_downgrade_sql_is_idempotent():
    mod = _load()
    assert mod.DROP_ARCH_REF_INDEX_SQL == "DROP INDEX IF EXISTS idx_unique_arch_ref;"


def test_duplicate_arch_ref_is_rejected_by_postgres_when_available():
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
                        content jsonb NOT NULL DEFAULT '{}'::jsonb
                    ) ON COMMIT DROP;
                    """
                )
            )
            conn.execute(sa.text(mod.CREATE_ARCH_REF_INDEX_SQL))
            # Idempotence against a database where the index already exists.
            conn.execute(sa.text(mod.CREATE_ARCH_REF_INDEX_SQL))

            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, content) "
                    "VALUES ('arch-1', 'arch', '{\"ref\":\"app.substrate\"}'::jsonb)"
                )
            )

            with pytest.raises(IntegrityError):
                with conn.begin_nested():
                    conn.execute(
                        sa.text(
                            "INSERT INTO bead (id, namespace, content) "
                            "VALUES ('arch-2', 'arch', '{\"ref\":\"app.substrate\"}'::jsonb)"
                        )
                    )

            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, content) "
                    "VALUES ('dev-1', 'dev', '{\"ref\":\"app.substrate\"}'::jsonb)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO bead (id, namespace, content) "
                    "VALUES ('arch-3', 'arch', '{}'::jsonb), ('arch-4', 'arch', '{}'::jsonb)"
                )
            )
            conn.execute(sa.text(mod.DROP_ARCH_REF_INDEX_SQL))
            conn.execute(sa.text(mod.DROP_ARCH_REF_INDEX_SQL))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is unavailable: {exc}")
    finally:
        engine.dispose()
