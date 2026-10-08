"""What can be proven about migration 0007 without a database.

Same constraint as ``test_migration_0005.py``: the substrate suite has no
Postgres, so the SQL in ``0007_encrypt_bead_event_payloads`` (the keyset
pagination, the ``bead_event``/``bead`` join) is not covered here and cannot
be — that path needs rehearsal against a restored backup, per the migration's
own docstring.

What IS provable here: the payload transform only touches ``content``/
``context``, leaves everything else (and falsy content/context) alone, is
idempotent against already-encrypted values, and the namespace filter is
derived from ``ENCRYPTED_NAMESPACES`` rather than hardcoded.
"""

import importlib.util
import pathlib

import pytest
from cryptography.fernet import Fernet

from src import crypto
from src.crypto import ENCRYPTED_NAMESPACES, decrypt_jsonb, encrypt_jsonb

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0007_encrypt_bead_event_payloads.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0007", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_singleton", None)
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_migration_module_imports():
    mod = _load()
    assert mod.revision == "0007_encrypt_bead_event_payloads"
    assert mod.down_revision == "0006_unique_arch_ref"


def test_rewrite_payload_encrypts_content_and_context():
    mod = _load()
    ns = sorted(ENCRYPTED_NAMESPACES)[0]
    payload = {
        "state": "active",
        "content": {"amount": 42.5, "merchant": "Coffee Shop"},
        "context": {"note": "operator override"},
        "confidence": 0.9,
    }

    out = mod._rewrite_payload(ns, payload, lambda v, n: encrypt_jsonb(v, n))

    assert "_enc" in out["content"]["amount"]
    assert "_enc" in out["content"]["merchant"]
    assert "_enc" in out["context"]["note"]
    # Non-crypto fields are untouched.
    assert out["state"] == "active"
    assert out["confidence"] == 0.9
    assert decrypt_jsonb(out["content"]) == payload["content"]
    assert decrypt_jsonb(out["context"]) == payload["context"]


def test_rewrite_payload_leaves_falsy_content_and_context_alone():
    """An 'updated' event where the caller didn't touch content/context.

    ``update.model_dump()`` always carries the keys; None/{} must not be
    turned into a spurious {} by round-tripping through encrypt_jsonb.
    """
    mod = _load()
    ns = sorted(ENCRYPTED_NAMESPACES)[0]
    payload = {"state": "doing", "content": None, "context": {}, "confidence": None}

    out = mod._rewrite_payload(ns, payload, lambda v, n: encrypt_jsonb(v, n))

    assert out == payload


def test_rewrite_payload_ignores_payload_with_no_content_or_context_key():
    """A 'transitioned' event payload: {"from_state": ..., "to_state": ...}."""
    mod = _load()
    ns = sorted(ENCRYPTED_NAMESPACES)[0]
    payload = {"from_state": "pending", "to_state": "doing"}

    out = mod._rewrite_payload(ns, payload, lambda v, n: encrypt_jsonb(v, n))

    assert out == payload


def test_rewrite_payload_is_idempotent_on_already_encrypted_values():
    mod = _load()
    ns = sorted(ENCRYPTED_NAMESPACES)[0]
    once = mod._rewrite_payload(
        ns,
        {"content": {"amount": 42.5}, "context": {}},
        lambda v, n: encrypt_jsonb(v, n),
    )

    twice = mod._rewrite_payload(ns, once, lambda v, n: encrypt_jsonb(v, n))

    assert once == twice


def test_upgrade_and_downgrade_transforms_are_inverses():
    ns = sorted(ENCRYPTED_NAMESPACES)[0]
    mod = _load()
    payload = {
        "state": "active",
        "content": {"amount": 12.34, "nested": {"merchant": "shop"}},
        "context": {"tag": "x"},
    }

    encrypted = mod._rewrite_payload(ns, payload, lambda v, n: encrypt_jsonb(v, n))
    restored = mod._rewrite_payload(ns, encrypted, lambda v, _n: decrypt_jsonb(v))

    assert restored == payload


def test_namespace_filter_is_derived_not_hardcoded():
    mod = _load()

    for ns in ENCRYPTED_NAMESPACES:
        assert f"'{ns}'" in mod._encrypted_namespaces_sql()

    original = crypto.ENCRYPTED_NAMESPACES
    try:
        mod.ENCRYPTED_NAMESPACES = frozenset({"finance", "personal"})
        assert "'personal'" in mod._encrypted_namespaces_sql()
    finally:
        mod.ENCRYPTED_NAMESPACES = original
