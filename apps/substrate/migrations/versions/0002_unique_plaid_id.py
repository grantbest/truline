"""unique partial index on content->>'plaid_transaction_id'

Phase 5: DB-level de-duplication for Plaid transactions. Final hard stop
behind the workflow's local idempotency check — if two workers race a
write for the same Plaid transaction, exactly one survives and the
other surfaces as 409 Conflict at the API.

The index is intentionally on the JSON path, not a hoisted column,
because identifier-shaped keys stay in plaintext under our selective
ALE policy (see apps/substrate/src/crypto.py). Sensitive values inside
content/context are encrypted; the plaid_transaction_id key is not.

Revision ID: 0002_unique_plaid_id
Revises: 0001_baseline
Create Date: 2026-05-26

"""
from alembic import op


revision = "0002_unique_plaid_id"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_plaid_id
        ON bead ((content->>'plaid_transaction_id'))
        WHERE (content->>'plaid_transaction_id' IS NOT NULL);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_unique_plaid_id;")
