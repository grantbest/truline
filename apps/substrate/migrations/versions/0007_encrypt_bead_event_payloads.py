"""Encrypt plaintext content/context in bead_event.payload for encrypted namespaces

``create_bead`` and ``update_bead`` (``src/routes.py``) both write a
``bead_event`` row alongside the ``bead`` row they mutate. ``create_bead``
has always run its event's ``content``/``context`` through ``encrypt_jsonb``
before writing. ``update_bead`` did not — every PATCH to a bead in an
``ENCRYPTED_NAMESPACES`` namespace (today: ``finance``) wrote the caller's
plaintext straight into ``bead_event.payload``. That is fixed at the
application layer alongside this migration; this migration is the data-half
of the fix, rewriting the plaintext this bug already produced.

``bead_event`` is append-only and hard-deleted only with its bead
(``routes.delete_bead``), so every plaintext row this bug ever wrote is still
there, in every backup and every read-only replica taken since. There is no
key rotation, ALE, or passage of time that un-writes it — only rewriting the
rows does.

Scope: rows in ``bead_event`` whose ``bead_id`` joins to a ``bead`` with
``namespace IN ENCRYPTED_NAMESPACES``. Only the ``content`` and ``context``
keys *inside* ``payload`` are touched — ``state``/``parent_id``/``confidence``/
``created_by``/``from_state``/``to_state`` were never encrypted on the create
path either and stay as they are. A payload with no ``content``/``context``
key (e.g. a ``transitioned`` event, whose payload is just
``{"from_state": ..., "to_state": ...}``) is left alone entirely.

Why this is safe to run against a mix of old (plaintext) and new (already
correct) rows
--------------------------------------------------------------------------
Same reasoning as 0005: ``crypto.encrypt_jsonb``'s tree-walk
(``_encrypt_node``) already treats an existing ``{"_enc": ...}`` leaf as
ciphertext and passes it through unchanged. So running the transform on a row
``update_bead`` already encrypted correctly (post-fix) is a value-level no-op.
The migration additionally skips the ``UPDATE`` entirely when the transformed
payload equals the stored one, so re-running this migration touches zero rows
the second time — idempotent at both the value level and the write level.

Three operational properties migration 0005 left the *next* migration to
rediscover at apply time. Stated here instead:

1. **One transaction, not one per batch.** ``migrations/env.py`` wraps the
   entire ``context.run_migrations()`` call — every migration in the run — in
   a single ``begin_transaction()``. ``BATCH`` below bounds the memory and
   statement size of each round trip (same reason 0005 uses it), but it does
   **not** split this migration's own work into separate commits: everything
   still lands or rolls back together as one transaction, and locks taken
   along the way are held for the migration's full duration, not per batch.
2. **No NOTIFY fires.** ``notify_bead_event()`` / ``bead_event_trigger``
   (``0001_baseline.py``) is ``AFTER INSERT OR UPDATE ON bead`` — it is not
   attached to ``bead_event`` at all. This migration only writes to
   ``bead_event``, so it fires no trigger and sends no ``pg_notify``, unlike
   0005's ``_rewrite_beads``, which updates ``bead`` itself and fires the
   trigger once per rewritten row.
3. **Needs the encryption key.** Like 0005, this migration calls
   ``encrypt_jsonb`` directly (via ``src.crypto``), so
   ``SUBSTRATE_ENCRYPTION_KEY`` must be set in whatever environment runs
   ``alembic upgrade`` — it is not optional here, the migration raises
   ``EncryptionConfigError`` on the first row without it.

Reversibility
-------------
``downgrade()`` decrypts the same rows back to plaintext. This is a
deliberate reintroduction of the exposure this migration exists to close, and
should only be used to roll back a bad schema deploy in a narrow window
before the corrected code has written any new (correctly encrypted) events —
downgrading after that would decrypt rows the fixed ``update_bead`` wrote
correctly, which is a regression, not a rollback. There is no dry-run
distinction between those two cases in this migration; operational judgment
is required.

Like 0005, the SQL here (the join, the keyset pagination) is not covered by
the no-database substrate suite and needs rehearsal against a restored backup
before running on prod. ``tests/test_migration_0007.py`` proves only the
transform and the idempotence predicate.

Revision ID: 0007_encrypt_bead_event_payloads
Revises: 0006_unique_arch_ref
Create Date: 2026-09-01

"""
import json

import sqlalchemy as sa
from alembic import op

from src.crypto import ENCRYPTED_NAMESPACES, decrypt_jsonb, encrypt_jsonb


revision = "0007_encrypt_bead_event_payloads"
down_revision = "0006_unique_arch_ref"
branch_labels = None
depends_on = None

# Rows per round trip. As in 0005, the work is Fernet en/decryption, which is
# CPU-bound in the migration process rather than in Postgres — this bounds
# memory and keeps any single SELECT/UPDATE pair short, but (see docstring)
# does not create separate commits.
BATCH = 500

_PAYLOAD_CRYPTO_KEYS = ("content", "context")


def _encrypted_namespaces_sql() -> str:
    return ", ".join(f"'{ns}'" for ns in sorted(ENCRYPTED_NAMESPACES))


def _rewrite_payload(namespace: str, payload: dict, transform) -> dict:
    """Apply ``transform(value, namespace)`` to payload['content']/['context'].

    Skips a key that is absent or falsy (``None`` or ``{}``) rather than
    running it through ``transform`` anyway: ``encrypt_jsonb(None, ...)``
    round-trips falsy input to ``{}``, which would turn an "untouched by this
    update" ``None`` into a spurious empty dict and change the payload's
    shape for no reason.
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for key in _PAYLOAD_CRYPTO_KEYS:
        value = out.get(key)
        if value:
            out[key] = transform(value, namespace)
    return out


def _rewrite_events(conn, transform) -> int:
    """Apply ``transform`` to bead_event.payload for encrypted-namespace beads.

    Keyset pagination on ``bead_event.id``, mirroring 0005's
    ``_rewrite_beads``: OFFSET would re-scan and is unsound against rows being
    rewritten underneath the scan.
    """
    keep = _encrypted_namespaces_sql()
    total = 0
    last_id = None

    while True:
        where = f"WHERE b.namespace IN ({keep})"
        if last_id is not None:
            where += " AND e.id > :last_id"
        rows = conn.execute(
            sa.text(
                "SELECT e.id, b.namespace, e.payload FROM bead_event e "
                "JOIN bead b ON b.id = e.bead_id "
                f"{where} ORDER BY e.id LIMIT {BATCH}"
            ),
            {"last_id": last_id} if last_id is not None else {},
        ).fetchall()

        if not rows:
            return total

        for event_id, namespace, payload in rows:
            new_payload = _rewrite_payload(namespace, payload, transform)
            if new_payload != payload:
                conn.execute(
                    sa.text(
                        "UPDATE bead_event SET payload = CAST(:p AS jsonb) "
                        "WHERE id = :id"
                    ),
                    {"p": json.dumps(new_payload), "id": event_id},
                )
                total += 1

        last_id = rows[-1][0]


def upgrade() -> None:
    conn = op.get_bind()
    rewritten = _rewrite_events(conn, lambda value, ns: encrypt_jsonb(value, ns))
    print(
        f"0007: encrypted content/context in {rewritten} bead_event payload(s) "
        f"for namespaces {sorted(ENCRYPTED_NAMESPACES)}"
    )


def downgrade() -> None:
    conn = op.get_bind()
    rewritten = _rewrite_events(conn, lambda value, _ns: decrypt_jsonb(value))
    print(f"0007: decrypted content/context in {rewritten} bead_event payload(s)")
