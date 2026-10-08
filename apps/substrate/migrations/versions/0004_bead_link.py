"""bead_link — the typed edge table (ARCHITECTURE.md §3.1 ``link``)

§3.1 names the canonical substrate API as create, transition, query, link,
subscribe. ``link`` was never built, so every edge on the platform is a string
inside ``content``: ``dev.note.answers_ref``, ``dev.task.source_bead_ids``, and
the EA model's ``realizes``/``depends_on`` arrays.

Two problems that a table fixes and a convention cannot:

1. Content refs dangle. Delete the bead a ref points at and nothing notices —
   a deleted question leaves ``guards.open_questions()`` treating the task as
   blocked forever. Both FKs here are ON DELETE CASCADE, so an edge cannot
   outlive its endpoints.
2. Content refs are unqueryable. ALE encrypts every leaf value in ``content``
   (apps/substrate/src/crypto.py), so a ref stored there is ciphertext at rest.
   ``source_id``, ``target_id``, and ``link_type`` are plain indexed columns —
   traversal becomes SQL rather than a client-side scan of decrypted rows.

This IMPLEMENTS §3.1 rather than amending it, so it carries no governance gate
(see docs/architecture/ea-metamodel.md §5.4 and the 2026-07-29 kanban review
§4.3, which reached this conclusion independently).

Revision ID: 0004_bead_link
Revises: 0003
Create Date: 2026-07-29

"""
from alembic import op


revision = "0004_bead_link"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bead_link (
            id          uuid PRIMARY KEY,
            source_id   uuid NOT NULL REFERENCES bead(id) ON DELETE CASCADE,
            target_id   uuid NOT NULL REFERENCES bead(id) ON DELETE CASCADE,
            link_type   text NOT NULL,
            content     jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at  timestamptz NOT NULL DEFAULT now(),
            created_by  text NOT NULL,
            CONSTRAINT uq_bead_link_edge UNIQUE (source_id, target_id, link_type),
            CONSTRAINT ck_bead_link_no_self CHECK (source_id <> target_id)
        );
        """
    )
    # Traversal in both directions is the whole point, so both endpoints are
    # indexed. link_type is indexed because "every realizes edge" is a query
    # the EA views run on every page load.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bead_link_source_id ON bead_link (source_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bead_link_target_id ON bead_link (target_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bead_link_link_type ON bead_link (link_type);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bead_link;")
