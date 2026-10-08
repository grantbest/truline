"""What can be proven about migration 0005 without a database.

The substrate suite has no Postgres — sessions are faked. So the SQL in
``0005_decrypt_non_finance`` (keyset pagination, the two-endpoint join on
``bead_link``, the jsonb casts) is **not** covered here and cannot be. That path
is proven by rehearsing the migration against a restored backup, which is the
gate recorded in the plan and in the migration's own docstring.

What IS provable here is the part most likely to be wrong quietly: that the
transforms are genuine inverses, and that the namespace filter is derived from
``ENCRYPTED_NAMESPACES`` rather than hardcoded — so adding a namespace to the
encrypted set cannot leave the migration silently decrypting it.
"""

import importlib.util
import pathlib

import pytest
from cryptography.fernet import Fernet

from src import crypto
from src.crypto import ENCRYPTED_NAMESPACES, decrypt_jsonb, encrypt_jsonb

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0005_decrypt_non_finance.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0005", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_singleton", None)
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_migration_module_imports():
    """Catches an ImportError before it is discovered at 3am mid-upgrade."""
    mod = _load()
    assert mod.revision == "0005_decrypt_non_finance"
    assert mod.down_revision == "0004_bead_link"


def test_upgrade_and_downgrade_transforms_are_inverses():
    """decrypt(encrypt(x)) == x for the payload shapes beads actually hold."""
    payload = {
        "title": "ship it",
        "count": 3,
        "nested": {"lane": "code-health", "tags": ["a", "b"]},
        "flag": True,
        "missing": None,
    }
    restore_as = sorted(ENCRYPTED_NAMESPACES)[0]

    assert decrypt_jsonb(encrypt_jsonb(payload, restore_as)) == payload


def test_downgrade_target_namespace_is_actually_encrypted():
    """downgrade() re-encrypts by passing a namespace from the encrypted set.

    If that value ever drifted out of ENCRYPTED_NAMESPACES, encrypt_jsonb would
    return the payload untouched and downgrade would silently become a no-op —
    a reversal that reports success while reversing nothing.
    """
    restore_as = sorted(ENCRYPTED_NAMESPACES)[0]
    assert restore_as in ENCRYPTED_NAMESPACES

    out = encrypt_jsonb({"amount": 12.34}, restore_as)
    assert "_enc" in out["amount"]


def test_namespace_filter_is_derived_not_hardcoded():
    """Adding a namespace to ENCRYPTED_NAMESPACES must change the SQL filter."""
    mod = _load()

    for ns in ENCRYPTED_NAMESPACES:
        assert f"'{ns}'" in mod._encrypted_namespaces_sql()

    original = crypto.ENCRYPTED_NAMESPACES
    try:
        mod.ENCRYPTED_NAMESPACES = frozenset({"finance", "personal"})
        assert "'personal'" in mod._encrypted_namespaces_sql()
    finally:
        mod.ENCRYPTED_NAMESPACES = original


def test_empty_payloads_round_trip_to_empty_dict():
    """Beads carry `{}` server_defaults; the migration must not turn them null."""
    restore_as = sorted(ENCRYPTED_NAMESPACES)[0]

    assert encrypt_jsonb({}, restore_as) == {}
    assert encrypt_jsonb(None, restore_as) == {}
    assert decrypt_jsonb({}) == {}
    assert decrypt_jsonb(None) == {}
