"""unique partial index on content->>'spec_identity' for pending dev.task beads

OPS-68: two outer-loop sessions filed the same two specs within about sixty
seconds of each other (2026-09-07 ~02:24-02:27Z). Each filing's duplicate
check (file_task.py's ``_check_not_a_duplicate``, FA-S39/#489, the
mechanization PRIN-014 was promoted on) listed live beads, found no match —
the other's insert had not landed yet — and both filings succeeded. That
check is check-then-act with no serialization; it only holds for sequential
writers. This closes the gap the way ``0006_unique_arch_ref`` closes the
equivalent one for ``content.ref``: the store refuses the second row itself.

Scoped to the live pre-terminal states -- ``pending``, ``doing`` and
``review`` -- rather than pending alone (release-gate blocking finding: a
racing filer's read-to-create window spans a minutes-long pristine
verification, so the winner is routinely already ``doing`` when the loser's
create lands; a pending-only index waves it through). ``failed`` stays
excluded so a refile after exhausted retries still works, and
``superseded``/``done`` stay excluded so supersede-refile and history keep
working.

``--supersede`` interplay under the widened scope: a supersede of a
``failed`` bead never touches this index (``failed`` is excluded), and a
supersede of a live bead orders its transition to ``superseded`` before the
replacement's insert (``file_task.py`` does this), so the identity is free
at create time. The earlier pending-only draft reasoned that racing
creators are both ``pending`` when they collide — true, but it assumed the
loser's read happened after the winner's create; in the real incident shape
the loser's read-to-create window spans a minutes-long pristine
verification, during which the winner is routinely claimed to ``doing``.

``spec_identity`` needs no addition to ``src.crypto.PLAINTEXT_KEYS``: the
``dev`` namespace is not in ``ENCRYPTED_NAMESPACES``, so its content is
already stored in plaintext.

Revision ID: 0008_unique_spec_identity
Revises: 0007_encrypt_bead_event_payloads
Create Date: 2026-09-07

"""
from alembic import op


revision = "0008_unique_spec_identity"
down_revision = "0007_encrypt_bead_event_payloads"
branch_labels = None
depends_on = None

INDEX_NAME = "idx_unique_dev_task_spec_identity_live"

CREATE_SPEC_IDENTITY_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
ON bead ((content->>'spec_identity'))
WHERE (namespace = 'dev' AND type = 'task' AND state IN ('pending', 'doing', 'review')
       AND content->>'spec_identity' IS NOT NULL);
"""

DROP_SPEC_IDENTITY_INDEX_SQL = f"DROP INDEX IF EXISTS {INDEX_NAME};"


def upgrade() -> None:
    op.execute(CREATE_SPEC_IDENTITY_INDEX_SQL)


def downgrade() -> None:
    op.execute(DROP_SPEC_IDENTITY_INDEX_SQL)
