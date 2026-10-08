"""Repair the one arch.incident bead stranded in a state its machine does not declare

R2605-1 (PR #698) registered ``arch.incident``'s state machine with entry state
``detected`` only: ``detected -> {mitigating, closed}``, ``mitigating ->
{resolved, detected}``, ``resolved -> {closed, detected}``, ``closed -> {}``.
Bead ``d861f65e-74ff-437e-a068-05e16b49c99a`` (ref
``inc.ci-runner-outage-2026-08-06``) was created 2026-08-06 by
``claude:outer-loop`` — before that machine existed — in state ``pending``, a
state the machine has never declared. Measured live 2026-09-10:
``STATE_MACHINES[('arch', 'incident')].get('pending')`` is ``None``, so both
``POST /beads/{id}/transition`` and a state-changing ``PATCH`` refuse every
edge out of it — ``_validate_state_transition_or_422`` computes
``machine.get(from_state, frozenset())``, and ``'pending'`` is not a key, so
the allowed set is always empty. The record is unreachable by any governed
write.

Three repair options were on the table. All three, and the reasoning for
choosing (b), are set out here — this docstring is the authoritative record of
that decision, and ``REPAIR_REASON`` below cites it:

* (a) admit ``pending`` as a second entry state — weakens the machine,
  permanently, to fit one legacy record. Rejected.
* (b) a one-time reviewed migration that moves the record to ``detected`` —
  **chosen**.
* (c) supersede the bead and re-file it correctly, preserving the original as
  history. Rejected: the record's content (immediate cause, four contributing
  causes, a misleading-signal analysis) is genuinely complete; only the state
  column is wrong, and superseding would fork the incident's identity for a
  defect that lives in one column.

(b) follows the direct precedent already in this migrations directory —
``0005_decrypt_non_finance.py`` rewrites ``bead`` rows in place with
``op.execute(sa.text(...))``, not DDL alone. This migration is scoped to
exactly the one bead named above, by id — not "every ``arch.incident`` bead
found in ``pending``" — because a migration is a reviewed action against a
named, inspected record. A *second* stranded record, present or future, is
what ``scripts/stranded_bead_states.py`` exists to surface for its own
reviewed migration, not something this one silently sweeps up.

The repair is recorded as its own ``bead_event`` row (``event_type
='migrated'``, not ``'transitioned'``) so the bead's history keeps saying,
truthfully, that this state was reached by a reviewed migration and not by an
edge the machine actually governs — PRIN-008: a repair carries the reason its
predecessor failed, and history is never silently erased.

Running this against production is an **operator act** — a deliberate
``alembic upgrade head`` against the live database, after reading this record
— not something exercised by this repository's own test suite.
``tests/test_migration_0009.py`` runs the same SQL against a disposable temp
table built to d861f65e's shape; it never touches a real bead.

Idempotent both ways: the ``WHERE`` clause on each statement only ever matches
the bead in its exact pre- (or post-) repair shape, so a second run, or a run
after the record has since progressed normally through the machine, is a
no-op rather than an error or a silent overwrite of live progress.

Revision ID: 0009_repair_stranded_incident
Revises: 0008_unique_spec_identity
Create Date: 2026-09-10

"""
import json
import uuid

import sqlalchemy as sa
from alembic import op


# 29 characters -- alembic stamps this into a VARCHAR(32) version_num column,
# so the id stays short even though the filename stays descriptive
# (scripts/repo_invariants.py RULE 5).
revision = "0009_repair_stranded_incident"
down_revision = "0008_unique_spec_identity"
branch_labels = None
depends_on = None

# The one bead this migration repairs. Named, not discovered: a targeted
# reviewed repair for the one record finding 1 measured stranded, not a
# general rule over every arch.incident bead.
INCIDENT_ID = "d861f65e-74ff-437e-a068-05e16b49c99a"
STRANDED_STATE = "pending"
REPAIR_STATE = "detected"
MIGRATED_BY = "migration:0009_repair_stranded_incident_pending_state"

REPAIR_REASON = (
    "arch.incident's machine (R2605-1) declares entry state 'detected' only; "
    "this bead was created 2026-08-06, before that machine existed, in state "
    "'pending', which the machine has never declared as reachable. Repaired "
    "to 'detected' by a one-time reviewed migration (option b). The three "
    "options considered and the reasoning for this one are in migration "
    "0009's module docstring."
)

UPDATE_STATE_SQL = sa.text(
    """
    UPDATE bead
    SET state = :repair_state
    WHERE id = CAST(:incident_id AS uuid)
      AND namespace = 'arch'
      AND type = 'incident'
      AND state = :stranded_state
    RETURNING id
    """
)

REVERT_STATE_SQL = sa.text(
    """
    UPDATE bead
    SET state = :stranded_state
    WHERE id = CAST(:incident_id AS uuid)
      AND namespace = 'arch'
      AND type = 'incident'
      AND state = :repair_state
    RETURNING id
    """
)

INSERT_EVENT_SQL = sa.text(
    """
    INSERT INTO bead_event
        (id, bead_id, event_type, from_state, to_state, payload, created_by)
    VALUES
        (CAST(:event_id AS uuid), CAST(:bead_id AS uuid), 'migrated',
         :from_state, :to_state, CAST(:payload AS jsonb), :created_by)
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        UPDATE_STATE_SQL,
        {
            "incident_id": INCIDENT_ID,
            "repair_state": REPAIR_STATE,
            "stranded_state": STRANDED_STATE,
        },
    )
    if result.scalar_one_or_none() is None:
        print(
            f"0009: bead {INCIDENT_ID} is not in state {STRANDED_STATE!r} "
            "(already repaired, moved on, or absent) -- no-op"
        )
        return

    conn.execute(
        INSERT_EVENT_SQL,
        {
            "event_id": str(uuid.uuid4()),
            "bead_id": INCIDENT_ID,
            "from_state": STRANDED_STATE,
            "to_state": REPAIR_STATE,
            "payload": json.dumps(
                {
                    "from_state": STRANDED_STATE,
                    "to_state": REPAIR_STATE,
                    "reason": REPAIR_REASON,
                }
            ),
            "created_by": MIGRATED_BY,
        },
    )
    print(f"0009: repaired bead {INCIDENT_ID} {STRANDED_STATE!r} -> {REPAIR_STATE!r}")


def downgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        REVERT_STATE_SQL,
        {
            "incident_id": INCIDENT_ID,
            "repair_state": REPAIR_STATE,
            "stranded_state": STRANDED_STATE,
        },
    )
    if result.scalar_one_or_none() is None:
        print(
            f"0009 downgrade: bead {INCIDENT_ID} is not in state {REPAIR_STATE!r} "
            "(moved on since the repair, or absent) -- refusing to revert a "
            "state this migration did not produce"
        )
        return
    print(
        f"0009 downgrade: reverted bead {INCIDENT_ID} "
        f"{REPAIR_STATE!r} -> {STRANDED_STATE!r}"
    )
