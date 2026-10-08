from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from src.schemas import validate_finance_content


def test_finance_transaction_schema_rejects_missing_required_fields():
    with pytest.raises(Exception) as exc_info:
        validate_finance_content(
            "transaction",
            {
                "iso_currency_code": "USD",
                "merchant_name": "Starbucks",
                "normalized_merchant": "Starbucks",
                "posted_date": "2026-05-27",
            },
        )

    errors = exc_info.value.errors()
    missing = {tuple(error["loc"]) for error in errors}
    assert ("amount",) in missing
    assert ("plaid_transaction_id",) in missing
    assert ("account_id",) in missing


def test_finance_transaction_post_returns_422_before_db(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DISABLE_VECTOR_LISTENER", "true")

    from src.main import app
    from src.database import get_db

    async def unused_db():
        yield None

    app.dependency_overrides[get_db] = unused_db
    client = TestClient(app)
    try:
        response = client.post(
            "/beads",
            headers={"x-api-key": "test-substrate-key"},
            json={
                "namespace": "finance",
                "type": "transaction",
                "state": "posted",
                "content": {
                    "iso_currency_code": "USD",
                    "merchant_name": "Starbucks",
                    "normalized_merchant": "Starbucks",
                    "posted_date": "2026-05-27",
                },
                "trust_tier": "system",
                "created_by": "test",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "finance_content_schema_violation"
    missing = {tuple(error["loc"]) for error in detail["errors"]}
    assert ("amount",) in missing
    assert ("plaid_transaction_id",) in missing
    assert ("account_id",) in missing


def test_finance_summary_sums_decrypted_balances(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DISABLE_VECTOR_LISTENER", "true")

    from src.main import app
    from src.database import get_db
    from src.crypto import encrypt_jsonb

    class FakeBead:
        def __init__(self, content):
            self.namespace = "finance"
            self.type = "account"
            # Mirror the at-rest shape: content is encrypted JSONB.
            self.content = encrypt_jsonb(content, "finance")

    beads = [
        FakeBead({"name": "Checking", "current_balance": 100.50, "iso_currency_code": "USD"}),
        FakeBead({"name": "Savings", "current_balance": 200.00, "iso_currency_code": "USD"}),
        FakeBead({"name": "Euro fund", "current_balance": 50.0, "iso_currency_code": "EUR"}),
        FakeBead({"name": "Pending link"}),  # no balance — skipped
    ]

    class FakeScalars:
        def all(self):
            return beads

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def execute(self, _query):
            return FakeResult()

    async def fake_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app)
    try:
        response = client.get(
            "/beads/finance/summary",
            headers={"x-api-key": "test-substrate-key"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["account_count"] == 3
    assert body["total_balance"] == pytest.approx(350.50)
    assert body["by_currency"]["USD"] == pytest.approx(300.50)
    assert body["by_currency"]["EUR"] == pytest.approx(50.0)


def test_finance_schema_allows_raw_description_next_to_normalized_merchant():
    validate_finance_content(
        "transaction",
        {
            "amount": 5.5,
            "iso_currency_code": "USD",
            "merchant_name": "STARBUCKS STORE 1234",
            "merchant": "STARBUCKS STORE 1234",
            "normalized_merchant": "Starbucks",
            "description": "STARBUCKS STORE 1234 CHICAGO IL",
            "posted_date": "2026-05-27",
            "plaid_transaction_id": "txn-123",
            "is_transfer": False,
            "account_id": "11111111-1111-4111-8111-111111111111",
        },
    )


def test_finance_bill_schema_accepts_minimal_and_full_content():
    # Minimal: only the required fields (owner is optional pre-registry).
    validate_finance_content(
        "bill",
        {
            "vendor": "Comcast",
            "amount": 89.99,
            "due_date": "2026-06-15",
            "source": "manual",
        },
    )
    # Full liability-derived bill with the soft account ref + dedup key.
    validate_finance_content(
        "bill",
        {
            "vendor": "Chase",
            "amount": 250.0,
            "due_date": "2026-06-15",
            "category": "interest_fees",
            "source": "liability",
            "owner": "grant",
            "external_id": "grant:chase:2026-06",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "min_payment": 35.0,
            "recurring": True,
            "frequency": "monthly",
        },
    )


def test_finance_bill_schema_rejects_missing_required_fields():
    with pytest.raises(Exception) as exc_info:
        validate_finance_content("bill", {"category": "utilities"})

    missing = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("vendor",) in missing
    assert ("amount",) in missing
    assert ("due_date",) in missing
    assert ("source",) in missing


def test_finance_bill_schema_rejects_unknown_source():
    # `source` is a strict Literal — a typo must 422 rather than silently
    # persist an unroutable bill.
    with pytest.raises(Exception) as exc_info:
        validate_finance_content(
            "bill",
            {
                "vendor": "Comcast",
                "amount": 89.99,
                "due_date": "2026-06-15",
                "source": "emailed",
            },
        )

    bad = {tuple(error["loc"]) for error in exc_info.value.errors()}
    assert ("source",) in bad
