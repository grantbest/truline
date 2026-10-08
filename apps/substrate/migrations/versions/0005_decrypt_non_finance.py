"""Decrypt non-finance bead content — bring ALE back in line with Amendment 11

Amendment 11 mandates Application Layer Encryption "for financial beads".
``crypto.encrypt_jsonb`` was called unconditionally, so every namespace was
encrypted. That is why ``list_beads`` is a decrypt-then-filter path behind a
Redis cache and a single-flight lock, and why no content field can ever be a
server-side filter — recorded as ``app.substrate`` debt on 2026-07-29, a day
before Amendment 24 named it.

This migration is **conformance remediation, not a security relaxation**: it
removes encryption from namespaces Amendment 11 never covered. ``finance`` is
untouched. See ARCHITECTURE.md Amendment 24.

Why this is safe to deploy in either order
------------------------------------------
``decrypt_jsonb`` is driven by the ``_enc`` sentinel in the data, not by policy,
and takes no namespace. So a row written by the old code (encrypted) and a row
written by the new code (plaintext) both read correctly through the same call.
There is no window in which the table must be uniformly one or the other, and no
flag to coordinate between the migration and the rollout.

Scope: ``bead`` and ``bead_link``. ``bead_event.payload`` is deliberately left
alone — it is an append-only audit log, it is only ever read by bead id rather
than filtered on, and it decrypts correctly on read regardless. Rewriting
history to buy a query nobody makes is the worse trade.

Reversibility
-------------
``downgrade()`` re-encrypts with the same key. ``tests/test_migration_0005.py``
proves the *transforms* are genuine inverses and that the namespace filter is
derived from ``ENCRYPTED_NAMESPACES`` rather than hardcoded. It does **not**
cover the SQL — the substrate suite has no database — so the pagination, the
two-endpoint join, and the jsonb casts are unproven until this is rehearsed
against a restored backup. Do that before running it on prod.

Note that a re-encrypted value is a *new* ciphertext (Fernet embeds a timestamp
and IV), so a full round trip preserves plaintext but changes bytes at rest.

``downgrade()`` is a **superset-inverse**: it encrypts every non-finance bead,
including any that were plaintext before ``upgrade()`` ran. The 2026-07-30
rehearsal found four such beads in prod — 2 ``personal.digest`` and 2
``platform.cost``, all created 2026-05-26/27, days after Amendment 11 mandated
ALE. They were never encrypted, so the "global" ALE this migration narrows was
never actually global. Reversing therefore leaves the database slightly *more*
encrypted than it started, which is the safe direction.

Revision ID: 0005_decrypt_non_finance
Revises: 0004_bead_link
Create Date: 2026-07-30

"""
import json

import sqlalchemy as sa
from alembic import op

from src.crypto import ENCRYPTED_NAMESPACES, decrypt_jsonb, encrypt_jsonb


revision = "0005_decrypt_non_finance"
down_revision = "0004_bead_link"
branch_labels = None
depends_on = None

# Rows per round trip. The work is Fernet decryption, which is CPU-bound in the
# migration process rather than in Postgres, so this bounds memory and keeps any
# single statement short enough not to sit on locks.
BATCH = 500


def _encrypted_namespaces_sql() -> str:
    return ", ".join(f"'{ns}'" for ns in sorted(ENCRYPTED_NAMESPACES))


def _rewrite_beads(conn, transform) -> int:
    """Apply ``transform(namespace, payload)`` to every non-finance bead."""
    keep = _encrypted_namespaces_sql()
    total = 0
    last_id = None

    while True:
        # Keyset pagination on the primary key: OFFSET would re-scan, and the
        # rows are being rewritten underneath us, which makes OFFSET unsound.
        where = f"WHERE namespace NOT IN ({keep})"
        if last_id is not None:
            where += " AND id > :last_id"
        rows = conn.execute(
            sa.text(
                f"SELECT id, namespace, content, context FROM bead {where} "
                f"ORDER BY id LIMIT {BATCH}"
            ),
            {"last_id": last_id} if last_id is not None else {},
        ).fetchall()

        if not rows:
            return total

        for bead_id, namespace, content, context in rows:
            conn.execute(
                sa.text(
                    "UPDATE bead SET content = CAST(:c AS jsonb), "
                    "context = CAST(:x AS jsonb) WHERE id = :id"
                ),
                {
                    "c": json.dumps(transform(namespace, content)),
                    "x": json.dumps(transform(namespace, context)),
                    "id": bead_id,
                },
            )
            total += 1

        last_id = rows[-1][0]


def _rewrite_links(conn, transform) -> int:
    """Apply ``transform`` to links where NEITHER endpoint is encrypted.

    Mirrors the write path in ``routes.create_link``: a link inherits the
    stricter of its two endpoints, so one touching finance stays encrypted.
    """
    keep = _encrypted_namespaces_sql()
    rows = conn.execute(
        sa.text(
            f"""
            SELECT l.id, l.content
            FROM bead_link l
            JOIN bead s ON s.id = l.source_id
            JOIN bead t ON t.id = l.target_id
            WHERE s.namespace NOT IN ({keep})
              AND t.namespace NOT IN ({keep})
            """
        )
    ).fetchall()

    for link_id, content in rows:
        conn.execute(
            sa.text("UPDATE bead_link SET content = CAST(:c AS jsonb) WHERE id = :id"),
            {"c": json.dumps(transform("dev", content)), "id": link_id},
        )
    return len(rows)


def upgrade() -> None:
    conn = op.get_bind()
    beads = _rewrite_beads(conn, lambda _ns, payload: decrypt_jsonb(payload))
    links = _rewrite_links(conn, lambda _ns, payload: decrypt_jsonb(payload))
    print(f"0005: decrypted {beads} beads and {links} links outside {sorted(ENCRYPTED_NAMESPACES)}")


def downgrade() -> None:
    conn = op.get_bind()
    # encrypt_jsonb refuses non-encrypted namespaces by design, so the reverse
    # direction passes a namespace that IS encrypted. The rows being restored
    # are exactly the ones the old unconditional code would have encrypted.
    restore_as = sorted(ENCRYPTED_NAMESPACES)[0]
    beads = _rewrite_beads(conn, lambda _ns, payload: encrypt_jsonb(payload, restore_as))
    links = _rewrite_links(conn, lambda _ns, payload: encrypt_jsonb(payload, restore_as))
    print(f"0005: re-encrypted {beads} beads and {links} links")
