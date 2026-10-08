"""The finance namespace's ALE policy -- an extension, not the core.

Registers the ``finance`` namespace as encrypted-at-rest, and the identifier
keys whose values must stay plaintext for DB-level indexes/joins to keep
working (Plaid identifiers, Phase 6+ uniqueness management; ``ref``, the EA
reconciliation contract's identity key -- see migration 0006).

Adding a namespace here is a security decision and costs that namespace its
ability to be queried by content. Removing one requires a migration. Amendment
11 mandates ALE "for financial beads"; narrowing ``crypto``'s encrypted set to
just this namespace is conformance remediation recorded in ARCHITECTURE.md
Amendment 24 -- see ``crypto.register_encrypted_namespace`` for how the set
got here from a module-level dict literal.

Imported eagerly from ``crypto.py`` itself (not lazily, and not only via
``main.py``'s composition root): migrations 0005 and 0007 do
``from src.crypto import ENCRYPTED_NAMESPACES`` standalone, and the
Dockerfile runs ``alembic upgrade head`` as its own process before
``uvicorn src.main:app`` ever starts, so nothing about the live app's startup
can be what makes this policy correct for them.
"""

from . import crypto

crypto.register_encrypted_namespace(
    "finance",
    plaintext_keys=frozenset(
        {
            "plaid_transaction_id",
            "plaid_account_id",
            "institution",
            "mask",
            "ref",
        }
    ),
)
