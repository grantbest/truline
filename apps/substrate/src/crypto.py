"""Application Layer Encryption (ALE) for bead content/context — Phase 5.

Selective encryption: we walk the JSON tree and encrypt leaf VALUES with
Fernet (AES-128-CBC + HMAC). Identifier-shaped keys listed in
``PLAINTEXT_KEYS`` are NOT encrypted, because they back DB-level
constraints (e.g. the unique partial index on
``content->>'plaid_transaction_id'``). The JSON structure itself stays
in plaintext so JSONB path lookups still work; only the sensitive
values become opaque.

Encrypted leaves are wrapped as ``{"_enc": "<token>"}`` so decrypt can
round-trip them transparently. Anything that's already a dict with that
sentinel is treated as an existing ciphertext and re-encrypted only if
it was never encrypted in the first place — see ``_is_ciphertext``.

Key material
------------
``SUBSTRATE_ENCRYPTION_KEY`` env var. 32 url-safe base64 bytes (the
shape ``cryptography.fernet.Fernet.generate_key()`` produces). Synced
from Infisical via ExternalSecret; see infrastructure/k8s/base/substrate.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

from cryptography.fernet import Fernet, InvalidToken


# Namespaces whose bead content/context is encrypted at rest, and the keys
# whose VALUES must remain plaintext within those namespaces so DB indexes /
# joins keep working. Both start empty here — the core defines no encrypted
# namespace of its own. A namespace outside the core (``finance`` is the one
# that exists today) declares itself via :func:`register_encrypted_namespace`
# from its own module; see ``finance_encryption.py``, imported at the bottom
# of this file precisely because that policy has a production entry point
# (alembic migrations) that never goes through the app's composition root.
#
# Amendment 11 mandates ALE "for financial beads". The implementation encrypted
# every namespace, which is why `list_beads` had to decrypt-then-filter behind a
# Redis cache and why no content field could ever be a server-side filter
# (recorded as app.substrate debt 2026-07-29). Narrowing this to what Amendment
# 11 actually says is conformance remediation, not a relaxation — see
# ARCHITECTURE.md Amendment 24.
ENCRYPTED_NAMESPACES: frozenset[str] = frozenset()

PLAINTEXT_KEYS: frozenset[str] = frozenset()


def register_encrypted_namespace(namespace: str, *, plaintext_keys: frozenset[str] = frozenset()) -> None:
    """Per-namespace hook the core owns: declare ``namespace`` encrypted at rest.

    Adding a namespace here is a security decision and costs that namespace
    its ability to be queried by content. Removing one requires a migration.
    A second registration for the same namespace is additive (its plaintext
    keys join the existing set) rather than replacing it, matching the
    module-level dict literal this style of registry replaces elsewhere in
    this codebase.
    """
    global ENCRYPTED_NAMESPACES, PLAINTEXT_KEYS
    ENCRYPTED_NAMESPACES = ENCRYPTED_NAMESPACES | {namespace}
    PLAINTEXT_KEYS = PLAINTEXT_KEYS | frozenset(plaintext_keys)

_ENC_SENTINEL = "_enc"

_fernet_singleton: Optional[Fernet] = None


class EncryptionConfigError(RuntimeError):
    """Raised when SUBSTRATE_ENCRYPTION_KEY is missing or malformed."""


def _fernet() -> Fernet:
    global _fernet_singleton
    if _fernet_singleton is not None:
        return _fernet_singleton
    key = os.environ.get("SUBSTRATE_ENCRYPTION_KEY")
    if not key:
        raise EncryptionConfigError(
            "SUBSTRATE_ENCRYPTION_KEY must be set (32 url-safe base64 bytes)."
        )
    try:
        _fernet_singleton = Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise EncryptionConfigError(
            f"SUBSTRATE_ENCRYPTION_KEY is not a valid Fernet key: {exc}"
        ) from exc
    return _fernet_singleton


def validate_encryption_config() -> None:
    """Fail fast when encryption is not configured correctly."""
    _fernet()


def _is_ciphertext(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) == 1
        and _ENC_SENTINEL in value
        and isinstance(value[_ENC_SENTINEL], str)
    )


def _encrypt_leaf(value: Any) -> dict:
    # Leaves are JSON-encodable scalars (str/int/float/bool/None). We
    # tag the type so decrypt can restore it; otherwise an int round-trips
    # to a string.
    import json

    token = _fernet().encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")
    return {_ENC_SENTINEL: token}


def _decrypt_leaf(value: dict) -> Any:
    import json

    token = value[_ENC_SENTINEL].encode("utf-8")
    try:
        plain = _fernet().decrypt(token)
    except InvalidToken as exc:
        raise EncryptionConfigError(
            "Failed to decrypt bead value — key rotated or ciphertext tampered."
        ) from exc
    return json.loads(plain.decode("utf-8"))


def _encrypt_node(node: Any, *, plaintext_keys: Iterable[str]) -> Any:
    if isinstance(node, dict):
        if _is_ciphertext(node):
            # Already encrypted (idempotent write path).
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in plaintext_keys:
                out[k] = v
            elif isinstance(v, (dict, list)):
                out[k] = _encrypt_node(v, plaintext_keys=plaintext_keys)
            else:
                out[k] = _encrypt_leaf(v)
        return out
    if isinstance(node, list):
        return [_encrypt_node(item, plaintext_keys=plaintext_keys) for item in node]
    # Bare top-level scalar — shouldn't happen for bead JSONB columns,
    # but be defensive.
    return _encrypt_leaf(node)


def _decrypt_node(node: Any) -> Any:
    if isinstance(node, dict):
        if _is_ciphertext(node):
            return _decrypt_leaf(node)
        return {k: _decrypt_node(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decrypt_node(item) for item in node]
    return node


def encrypt_jsonb(payload: Optional[dict], namespace: str) -> dict:
    """Encrypt a content/context dict in place-style (returns new dict).

    Empty / None inputs round-trip to ``{}`` to match the SQLAlchemy
    server_default. Keys listed in :data:`PLAINTEXT_KEYS` retain their
    raw values to support DB-level indexes.

    ``namespace`` is **required, not defaulted**. A default would let a new
    call site silently keep encrypting everything, which is the exact state
    this parameter exists to end — see :data:`ENCRYPTED_NAMESPACES`.
    """
    if not payload:
        return {}
    if namespace not in ENCRYPTED_NAMESPACES:
        return payload
    return _encrypt_node(payload, plaintext_keys=PLAINTEXT_KEYS)


def decrypt_jsonb(payload: Optional[dict]) -> dict:
    """Inverse of :func:`encrypt_jsonb`. Safe to call on plaintext dicts
    that contain no ``_enc`` markers (returns them unchanged).

    Deliberately takes **no namespace**. Decryption is driven by the ``_enc``
    sentinel in the data, not by policy, and that is what makes the ALE
    narrowing safe to deploy: rows written before migration 0005 are still
    encrypted, rows written after are not, and both read correctly through
    this function with no flag to coordinate. If this ever grows a namespace
    argument, the deploy stops being ordering-independent.
    """
    if not payload:
        return {}
    return _decrypt_node(payload)


__all__ = [
    "ENCRYPTED_NAMESPACES",
    "EncryptionConfigError",
    "PLAINTEXT_KEYS",
    "decrypt_jsonb",
    "encrypt_jsonb",
    "register_encrypted_namespace",
    "validate_encryption_config",
]

# Composition root for this module's own namespace hook: see
# register_encrypted_namespace's docstring and finance_encryption.py's module
# docstring for why this cannot wait for main.py's composition root to run.
# Placed last so every name finance_encryption.py touches on this module
# (ENCRYPTED_NAMESPACES, PLAINTEXT_KEYS, register_encrypted_namespace) is
# already defined above.
from . import finance_encryption  # noqa: E402,F401
