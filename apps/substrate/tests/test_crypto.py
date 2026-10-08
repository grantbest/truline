import pytest
from cryptography.fernet import Fernet

from src import crypto


@pytest.fixture(autouse=True)
def reset_fernet(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_singleton", None)


def test_encrypt_decrypt_jsonb_preserves_plaintext_identifier(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())

    encrypted = crypto.encrypt_jsonb(
        {
            "plaid_transaction_id": "txn-123",
            "amount": 12.34,
            "merchant": "Coffee Shop",
            "nested": {"category": "food"},
        },
        "finance",
    )

    assert encrypted["plaid_transaction_id"] == "txn-123"
    assert encrypted["amount"] != 12.34
    assert "_enc" in encrypted["amount"]
    assert encrypted["merchant"] != "Coffee Shop"
    assert "_enc" in encrypted["nested"]["category"]

    assert crypto.decrypt_jsonb(encrypted) == {
        "plaid_transaction_id": "txn-123",
        "amount": 12.34,
        "merchant": "Coffee Shop",
        "nested": {"category": "food"},
    }


def test_encrypt_decrypt_jsonb_preserves_plaintext_ref(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())

    encrypted = crypto.encrypt_jsonb(
        {"ref": "app.substrate", "note": "stable business identifier"},
        "finance",
    )

    assert encrypted["ref"] == "app.substrate"
    assert encrypted["note"] != "stable business identifier"
    assert "_enc" in encrypted["note"]
    assert crypto.decrypt_jsonb(encrypted) == {
        "ref": "app.substrate",
        "note": "stable business identifier",
    }


def test_validate_encryption_config_rejects_missing_key(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_ENCRYPTION_KEY", raising=False)

    with pytest.raises(crypto.EncryptionConfigError):
        crypto.validate_encryption_config()


# --- ALE is scoped to finance (Amendment 24 / Amendment 11 conformance) -----
#
# Amendment 11 mandates ALE "for financial beads". The implementation encrypted
# every namespace, which is what forced list_beads into a decrypt-then-filter
# path behind a Redis cache. These tests pin the narrowing so it cannot silently
# widen again — and pin the property that makes it safe to deploy.


@pytest.mark.parametrize("namespace", ["dev", "home", "personal", "betting", "arch", "platform"])
def test_non_finance_namespaces_are_not_encrypted(monkeypatch, namespace):
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    payload = {"title": "ship it", "nested": {"lane": "code-health"}}

    assert crypto.encrypt_jsonb(payload, namespace) == payload


def test_finance_is_still_encrypted(monkeypatch):
    """The narrowing must not become a removal."""
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())

    encrypted = crypto.encrypt_jsonb({"amount": 12.34}, "finance")

    assert "_enc" in encrypted["amount"]
    assert crypto.decrypt_jsonb(encrypted) == {"amount": 12.34}


def test_decrypt_reads_both_eras_without_a_flag(monkeypatch):
    """The property that makes migration 0005 deploy-order independent.

    Rows written before the migration are encrypted; rows written after are not.
    Both must read correctly through the same call, because there is no moment
    at which the table is uniformly one or the other.
    """
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())

    old_row = crypto.encrypt_jsonb({"title": "written before 0005"}, "finance")
    new_row = {"title": "written after 0005"}

    assert crypto.decrypt_jsonb(old_row) == {"title": "written before 0005"}
    assert crypto.decrypt_jsonb(new_row) == {"title": "written after 0005"}


def test_encrypt_requires_an_explicit_namespace():
    """A default would let a new call site silently keep encrypting everything."""
    with pytest.raises(TypeError):
        crypto.encrypt_jsonb({"a": 1})  # type: ignore[call-arg]
