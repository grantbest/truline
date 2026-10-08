"""baseline: bead, bead_event, indexes, confidence constraint, notify trigger

Mirrors the schema previously created by main.py's startup hook so this
migration represents the as-deployed Phase 3-S state. Idempotent on
upgrade: if `bead` already exists (databases provisioned by the old
startup hook), the migration short-circuits and only stamps the version.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-22

"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode():
        bind = op.get_bind()
        if inspect(bind).has_table("bead"):
            # Pre-existing schema from the legacy startup hook — treat this
            # revision as a stamp rather than re-creating objects.
            return

    op.create_table(
        "bead",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("trust_tier", sa.String(), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["bead.id"]),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_bead_confidence_range",
        ),
    )
    op.create_index("ix_bead_namespace", "bead", ["namespace"])
    op.create_index("ix_bead_type", "bead", ["type"])
    op.create_index("ix_bead_state", "bead", ["state"])
    op.create_index(
        "idx_bead_created_at",
        "bead",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "bead_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "bead_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("from_state", sa.String(), nullable=True),
        sa.Column("to_state", sa.String(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["bead_id"], ["bead.id"]),
    )
    op.create_index("idx_bead_event_bead_id", "bead_event", ["bead_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_bead_event() RETURNS TRIGGER AS $$
        DECLARE
            payload TEXT;
        BEGIN
            payload := json_build_object(
                'id', NEW.id,
                'namespace', NEW.namespace,
                'type', NEW.type,
                'state', NEW.state,
                'old_state', OLD.state
            )::text;
            PERFORM pg_notify('bead_event', payload);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER bead_event_trigger
        AFTER INSERT OR UPDATE ON bead
        FOR EACH ROW EXECUTE FUNCTION notify_bead_event();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS bead_event_trigger ON bead;")
    op.execute("DROP FUNCTION IF EXISTS notify_bead_event();")
    op.drop_index("idx_bead_event_bead_id", table_name="bead_event")
    op.drop_table("bead_event")
    op.drop_index("idx_bead_created_at", table_name="bead")
    op.drop_index("ix_bead_state", table_name="bead")
    op.drop_index("ix_bead_type", table_name="bead")
    op.drop_index("ix_bead_namespace", table_name="bead")
    op.drop_table("bead")
