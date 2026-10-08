"""unique partial index on content->>'ref' for architecture beads

The EA model uses ``content.ref`` as its stable business identifier. It must be
enforced by the database, not only by the loader that writes the rows.

``ref`` is intentionally left in plaintext by ``src.crypto.PLAINTEXT_KEYS`` so
Postgres can index it, following the 0002 Plaid identifier precedent.

Revision ID: 0006_unique_arch_ref
Revises: 0005_decrypt_non_finance
Create Date: 2026-08-01

"""
from alembic import op


revision = "0006_unique_arch_ref"
down_revision = "0005_decrypt_non_finance"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_unique_arch_ref"

CREATE_ARCH_REF_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
ON bead ((content->>'ref'))
WHERE (namespace = 'arch' AND content->>'ref' IS NOT NULL);
"""

DROP_ARCH_REF_INDEX_SQL = f"DROP INDEX IF EXISTS {INDEX_NAME};"


def upgrade() -> None:
    op.execute(CREATE_ARCH_REF_INDEX_SQL)


def downgrade() -> None:
    op.execute(DROP_ARCH_REF_INDEX_SQL)
