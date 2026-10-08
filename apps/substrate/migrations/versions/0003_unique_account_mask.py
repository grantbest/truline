"""unique index on account institution and mask

Phase 6.5: DB-level de-duplication for accounts. Uses a composite of
institution and mask (last 4 digits) to identify the same real-world 
account even if Plaid's internal account_id changes during re-link.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002_unique_plaid_id'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Partial index: only applies to 'account' type beads.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_unique_account_mask
        ON bead ((content->>'institution'), (content->>'mask'))
        WHERE (type = 'account' AND content->>'institution' IS NOT NULL AND content->>'mask' IS NOT NULL);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_unique_account_mask;")
