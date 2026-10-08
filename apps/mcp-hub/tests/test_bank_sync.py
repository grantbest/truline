import json
import random
from pathlib import Path

import pytest
import httpx

from src.tools import litellm_client
from workflows import bank_sync
from workflows.bank_sync import (
    FLOW_VALUES,
    _liability_from_credit,
    _manual_matches_transaction,
    _match_existing_account,
    _merchant_substring_match,
    _sync_failure_message,
    assign_liability_payments,
    categorize_transaction_activity,
    category_baseline,
    classify_transaction_flow,
    count_low_or_unmeasured_categorization_confidence,
    is_category_anomaly,
    normalize_merchant_name,
    spend_total_by_flow,
    write_transaction_bead_activity,
)


def _acct_bead(bead_id, *, persistent_account_id=None, institution=None, mask=None, plaid_account_id=None):
    return {
        "id": bead_id,
        "content": {
            "persistent_account_id": persistent_account_id,
            "institution": institution,
            "mask": mask,
            "plaid_account_id": plaid_account_id,
        },
    }


def test_match_priority1_persistent_id_wins_over_relinked_plaid_id():
    # Re-link scenario: same real-world account, NEW plaid_account_id, same
    # persistent_account_id. Must match the existing bead via Priority 1.
    beads = [
        _acct_bead("bead-A", persistent_account_id="STABLE", institution="chase", mask="5913", plaid_account_id="OLD_PLAID"),
    ]
    match = _match_existing_account(
        beads, persistent_id="STABLE", institution="chase", mask="5913", plaid_id="NEW_PLAID"
    )
    assert match["id"] == "bead-A"


def test_match_priority2_institution_mask_when_no_persistent_id():
    # Institution omits persistent_account_id; fall back to (institution, mask).
    beads = [
        _acct_bead("bead-B", persistent_account_id=None, institution="amex", mask="2002", plaid_account_id="OLD"),
    ]
    match = _match_existing_account(
        beads, persistent_id=None, institution="amex", mask="2002", plaid_id="NEW"
    )
    assert match["id"] == "bead-B"


def test_match_priority3_legacy_plaid_id_fallback():
    # Legacy bead with no stable id and a different mask — only plaid_id matches.
    beads = [
        _acct_bead("bead-C", persistent_account_id=None, institution="chase", mask=None, plaid_account_id="LEGACY"),
    ]
    match = _match_existing_account(
        beads, persistent_id=None, institution="chase", mask="9999", plaid_id="LEGACY"
    )
    assert match["id"] == "bead-C"


def test_match_returns_none_for_genuinely_new_account():
    beads = [
        _acct_bead("bead-D", persistent_account_id="OTHER", institution="chase", mask="0001", plaid_account_id="X"),
    ]
    match = _match_existing_account(
        beads, persistent_id="STABLE", institution="citi", mask="3312", plaid_id="Y"
    )
    assert match is None


def test_normalize_merchant_maps_amazon_processor_names():
    assert normalize_merchant_name("AMZN-US-123") == "Amazon"
    assert normalize_merchant_name("Amazon Marketplace") == "Amazon"


def test_normalize_merchant_strips_common_numeric_suffixes():
    assert normalize_merchant_name("LOCAL COFFEE SHOP 12345") == "Local Coffee Shop"


def test_reconcile_merchant_match_handles_substrings():
    assert _merchant_substring_match("Starbucks", "Starbucks Store #123")
    assert _merchant_substring_match("Comcast Cable", "Comcast")
    assert not _merchant_substring_match("Starbucks", "Comcast")


def test_transaction_flow_classifies_distinct_money_movements():
    examples = [
        (
            {
                "amount": -3200.00,
                "merchant_name": "ACME INC PAYROLL",
                "name": "DIRECT DEPOSIT ACME INC PAYROLL",
            },
            "income",
        ),
        (
            {
                "amount": 1250.00,
                "merchant_name": "CHASE CREDIT CARD",
                "name": "Online Payment, Thank You",
            },
            "transfer",
        ),
        (
            {
                "amount": -42.18,
                "merchant_name": "TARGET",
                "name": "TARGET MERCHANDISE RETURN REFUND",
            },
            "refund",
        ),
        (
            {
                "amount": 19.45,
                "merchant_name": "STARBUCKS STORE 1234",
                "name": "STARBUCKS STORE 1234 CHICAGO IL",
            },
            "spend",
        ),
    ]

    for transaction, expected_flow in examples:
        flow = classify_transaction_flow(transaction)
        assert flow == expected_flow
        assert flow in FLOW_VALUES


def test_spend_total_by_flow_excludes_income_transfers_and_subtracts_refunds():
    transactions = [
        {"content": {"amount": 120.00, "flow": "spend"}},
        {"content": {"amount": -3200.00, "flow": "income"}},
        {"content": {"amount": 750.00, "flow": "transfer"}},
        {"content": {"amount": -20.00, "flow": "refund"}},
    ]

    assert spend_total_by_flow(transactions) == 100.00


def test_refund_flow_reduces_spend_and_is_not_income():
    refund = {
        "amount": -58.12,
        "merchant_name": "COSTCO",
        "name": "COSTCO MERCHANDISE RETURN REFUND",
    }

    assert classify_transaction_flow(refund) == "refund"
    assert spend_total_by_flow([{"content": {**refund, "flow": "refund"}}]) == -58.12


def test_undeterminable_flow_is_explicit_not_spend():
    transaction = {"amount": -12.34, "merchant_name": "", "name": ""}

    assert classify_transaction_flow(transaction) == "undetermined"


def test_liability_bill_matches_negative_card_payment_by_account():
    bill = {
        "id": "bill-1",
        "type": "bill",
        "state": "overdue",
        "content": {
            "source": "liability",
            "vendor": "Citi AAdvantage Platinum Select",
            "amount": 35.0,
            "due_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
        },
    }
    tx = {
        "id": "tx-1",
        "type": "transaction",
        "state": "posted",
        "content": {
            "amount": -35.0,
            "posted_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "merchant_name": "PAYMENT THANK YOU",
            "description": "CITI CARD ONLINE PAYMENT THANK YOU",
            "is_transfer": True,
        },
    }

    assert _manual_matches_transaction(bill, tx)


def test_liability_bill_matches_larger_card_payment_by_account():
    bill = {
        "id": "bill-1",
        "type": "bill",
        "state": "overdue",
        "content": {
            "source": "liability",
            "vendor": "Citi AAdvantage Platinum Select",
            "amount": 35.0,
            "due_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
        },
    }
    tx = {
        "id": "tx-1",
        "type": "transaction",
        "state": "posted",
        "content": {
            "amount": -500.0,
            "posted_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "merchant_name": "PAYMENT THANK YOU",
            "description": "CITI CARD ONLINE PAYMENT THANK YOU",
            "is_transfer": True,
        },
    }

    assert _manual_matches_transaction(bill, tx)


def test_liability_bill_does_not_match_underpayment_by_account():
    bill = {
        "id": "bill-1",
        "type": "bill",
        "state": "overdue",
        "content": {
            "source": "liability",
            "vendor": "Citi AAdvantage Platinum Select",
            "amount": 35.0,
            "due_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
        },
    }
    tx = {
        "id": "tx-1",
        "type": "transaction",
        "state": "posted",
        "content": {
            "amount": -20.0,
            "posted_date": "2026-06-10",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "merchant_name": "PAYMENT THANK YOU",
            "description": "CITI CARD ONLINE PAYMENT THANK YOU",
            "is_transfer": True,
        },
    }

    assert not _manual_matches_transaction(bill, tx)


def test_bill_matching_uses_wider_window_than_expense_matching():
    bill = {
        "type": "bill",
        "content": {
            "source": "manual",
            "vendor": "Comcast",
            "amount": 80.0,
            "due_date": "2026-06-10",
        },
    }
    expense = {
        "type": "expense",
        "content": {
            "vendor": "Comcast",
            "amount": 80.0,
            "date": "2026-06-10",
        },
    }
    tx = {
        "content": {
            "amount": 80.0,
            "posted_date": "2026-06-16",
            "normalized_merchant": "Comcast",
        },
    }

    assert _manual_matches_transaction(bill, tx)
    assert not _manual_matches_transaction(expense, tx)


# --- assign_liability_payments (dev.finding d7cdc16c) --------------------

_LIABILITY_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "lifeops" / "liability_bill_matching_2026-09-27.json"
)


def _load_liability_fixture():
    return json.loads(_LIABILITY_FIXTURE_PATH.read_text())


def _expected_liability_pairings(fixture):
    return {p["bill_id"]: p["expected_transaction_id"] for p in fixture["expected_pairings"]}


def _liability_bill(bill_id, *, due_date, amount, account_id=None, institution=None, mask="****"):
    external_id = f"grant:{institution}:{mask}:{due_date[:7]}" if institution else None
    return {
        "id": bill_id,
        "type": "bill",
        "state": "pending",
        "content": {
            "source": "liability",
            "account_id": account_id,
            "amount": amount,
            "due_date": due_date,
            "external_id": external_id,
            "vendor": "Test Card",
        },
    }


def _payment_tx(tx_id, *, posted_date, amount, account_id=None, institution=None):
    return {
        "id": tx_id,
        "type": "transaction",
        "state": "posted",
        "content": {
            "account_id": account_id,
            "institution": institution,
            "amount": amount,
            "posted_date": posted_date,
            "is_transfer": True,
            "description": "ONLINE PAYMENT, THANK YOU",
        },
    }


def test_assign_liability_payments_reproduces_fixture_pairing_table():
    fixture = _load_liability_fixture()
    result = assign_liability_payments(fixture["bills"], fixture["transactions"])
    assert result == _expected_liability_pairings(fixture)


def test_assign_liability_payments_is_order_independent():
    fixture = _load_liability_fixture()
    expected = _expected_liability_pairings(fixture)

    reversed_result = assign_liability_payments(
        list(reversed(fixture["bills"])), list(reversed(fixture["transactions"]))
    )
    assert reversed_result == expected

    rng = random.Random(1234)
    shuffled_bills = list(fixture["bills"])
    shuffled_transactions = list(fixture["transactions"])
    rng.shuffle(shuffled_bills)
    rng.shuffle(shuffled_transactions)
    shuffled_result = assign_liability_payments(shuffled_bills, shuffled_transactions)
    assert shuffled_result == expected


def test_assign_liability_payments_never_crosses_accounts_on_fixture():
    fixture = _load_liability_fixture()
    result = assign_liability_payments(fixture["bills"], fixture["transactions"])
    bills_by_id = {b["id"]: b for b in fixture["bills"]}
    txs_by_id = {t["id"]: t for t in fixture["transactions"]}

    cross_account = 0
    for bill_id, tx_id in result.items():
        if not tx_id:
            continue
        bill_account = bills_by_id[bill_id]["content"].get("account_id")
        tx_account = txs_by_id[tx_id]["content"].get("account_id")
        if bill_account and tx_account and bill_account != tx_account:
            cross_account += 1
    assert cross_account == 0


def test_assign_liability_payments_no_fallback_when_bill_has_account_id():
    # Bill carries account_id A; the only in-window, sufficient candidate is
    # on account B at the same institution. No institution/vendor fallback.
    bill = _liability_bill(
        "bill-a", due_date="2026-08-17", amount=100.0, account_id="acct-A", institution="citi"
    )
    tx = _payment_tx(
        "tx-b", posted_date="2026-08-01", amount=-100.0, account_id="acct-B", institution="citi"
    )
    assert assign_liability_payments([bill], [tx]) == {"bill-a": None}


def test_assign_liability_payments_institution_fallback_prefers_citi_over_chase():
    # The fixture's real instance: 08866637 (no account_id, citi) must match
    # the card-side citi row, not the checking-side chase row with the same
    # amount and date (060fa99c in the fixture).
    bill = _liability_bill(
        "bill-c", due_date="2026-06-17", amount=41.0, account_id=None, institution="citi"
    )
    citi_tx = _payment_tx(
        "tx-citi", posted_date="2026-06-10", amount=-41.0, account_id="acct-citi", institution="citi"
    )
    chase_tx = _payment_tx(
        "tx-chase", posted_date="2026-06-10", amount=41.0, account_id="acct-chase", institution="chase"
    )
    result = assign_liability_payments([bill], [chase_tx, citi_tx])
    assert result == {"bill-c": "tx-citi"}


@pytest.mark.parametrize(
    "posted_date,expected",
    [
        ("2026-07-17", None),
        ("2026-07-18", "tx-1"),
        ("2026-08-24", "tx-1"),
        ("2026-08-25", None),
    ],
)
def test_assign_liability_payments_window_edges(posted_date, expected):
    bill = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    tx = _payment_tx("tx-1", posted_date=posted_date, amount=-50.0, account_id="acct-1")
    assert assign_liability_payments([bill], [tx]) == {"bill-1": expected}


@pytest.mark.parametrize(
    "posted_date,expected",
    [
        ("2026-02-28", None),
        ("2026-03-01", "tx-1"),
    ],
)
def test_assign_liability_payments_month_subtraction_clamps_short_month(posted_date, expected):
    # 2026-03-31 minus one calendar month clamps to 2026-02-28, which is the
    # (exclusive) lower bound.
    bill = _liability_bill("bill-1", due_date="2026-03-31", amount=50.0, account_id="acct-1")
    tx = _payment_tx("tx-1", posted_date=posted_date, amount=-50.0, account_id="acct-1")
    assert assign_liability_payments([bill], [tx]) == {"bill-1": expected}


def test_assign_liability_payments_picks_nearest_candidate():
    bill = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    near = _payment_tx("tx-near", posted_date="2026-08-15", amount=-50.0, account_id="acct-1")
    far = _payment_tx("tx-far", posted_date="2026-08-01", amount=-50.0, account_id="acct-1")
    result = assign_liability_payments([bill], [far, near])
    assert result == {"bill-1": "tx-near"}


def test_assign_liability_payments_equal_distance_prefers_earlier_posted_date():
    bill = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    before = _payment_tx("tx-before", posted_date="2026-08-15", amount=-50.0, account_id="acct-1")
    after = _payment_tx("tx-after", posted_date="2026-08-19", amount=-50.0, account_id="acct-1")
    result = assign_liability_payments([bill], [after, before])
    assert result == {"bill-1": "tx-before"}


def test_assign_liability_payments_each_bill_gets_its_own_cycle_payment():
    bill1 = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    bill2 = _liability_bill("bill-2", due_date="2026-09-17", amount=50.0, account_id="acct-1")
    tx1 = _payment_tx("tx-1", posted_date="2026-08-01", amount=-50.0, account_id="acct-1")
    tx2 = _payment_tx("tx-2", posted_date="2026-08-29", amount=-50.0, account_id="acct-1")
    result = assign_liability_payments([bill1, bill2], [tx1, tx2])
    assert result == {"bill-1": "tx-1", "bill-2": "tx-2"}


def test_assign_liability_payments_one_payment_in_two_windows_goes_to_the_earlier_due_bill():
    b1 = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    b2 = _liability_bill("bill-2", due_date="2026-08-24", amount=50.0, account_id="acct-1")
    tx = _payment_tx("tx-1", posted_date="2026-08-20", amount=-50.0, account_id="acct-1")
    assert assign_liability_payments([b2, b1], [tx]) == {"bill-1": "tx-1", "bill-2": None}


def test_assign_liability_payments_rejects_underpayment():
    bill = _liability_bill("bill-1", due_date="2026-08-17", amount=50.0, account_id="acct-1")
    tx = _payment_tx("tx-1", posted_date="2026-08-01", amount=-49.0, account_id="acct-1")
    assert assign_liability_payments([bill], [tx]) == {"bill-1": None}


def test_assign_liability_payments_ignores_non_liability_bill():
    bill = {
        "id": "bill-1",
        "type": "bill",
        "state": "pending",
        "content": {
            "source": "manual",
            "account_id": "acct-1",
            "amount": 50.0,
            "due_date": "2026-08-17",
        },
    }
    tx = _payment_tx("tx-1", posted_date="2026-08-01", amount=-50.0, account_id="acct-1")
    assert assign_liability_payments([bill], [tx]) == {"bill-1": None}


@pytest.mark.asyncio
async def test_categorize_transaction_reraises_429_for_temporal_retry(
    httpx_mock, monkeypatch
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    # gemini-flash was retired 2026-09-04 (docs/plans/2026-09-04-decision-
    # record-claude-loops.md); litellm_client refuses it before any network
    # call now. This test is about the 429-reraise contract for whichever
    # model is live, so it clears the guard to reach that logic.
    monkeypatch.setattr(litellm_client, "RETIRED_MODELS", frozenset())

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=429,
        json={"error": "rate limit"},
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "cost-bead"},
    )

    with pytest.raises(httpx.HTTPStatusError):
        await categorize_transaction_activity("Coffee Shop", 4.25, "Coffee Shop 123")


@pytest.mark.asyncio
async def test_categorize_transaction_preserves_credit_card_interest_as_fees(
    httpx_mock, monkeypatch
):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    # See comment on test_categorize_transaction_reraises_429_for_temporal_retry.
    monkeypatch.setattr(litellm_client, "RETIRED_MODELS", frozenset())

    httpx_mock.add_response(
        url="http://litellm.test/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "choices": [{"message": {"content": "Interest/Fees"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "cost-bead"},
    )

    result = await categorize_transaction_activity(
        "Chase Credit Card",
        18.42,
        "Credit Card Interest Charge",
    )

    assert result == {"category": "interest_fees", "is_transfer": False, "flow": "spend"}


@pytest.mark.asyncio
async def test_categorize_transaction_degrades_when_provider_retired(
    httpx_mock, monkeypatch
):
    """When the requested model is flagged retired (gemini-flash was,
    2026-09-04), single-transaction categorization must degrade to an
    uncategorized result (not crash, not silently 401) and must not attempt
    the LLM call at all. claude-haiku (the current default) is not retired,
    so this simulates the condition directly."""
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test/v1/chat/completions")
    monkeypatch.setenv("LITELLM_API_KEY", "test-litellm-key")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setattr(
        litellm_client, "RETIRED_MODELS", frozenset({litellm_client.LIFEOPS_DEFAULT_MODEL})
    )

    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "cost-bead"},
    )

    result = await categorize_transaction_activity(
        "Mystery Vendor", 12.34, "Mystery Vendor"
    )

    assert result == {"category": None, "is_transfer": False, "flow": "spend"}
    assert httpx_mock.get_requests(url="http://litellm.test/v1/chat/completions") == []
    cost_requests = httpx_mock.get_requests(url="http://substrate.test/beads")
    assert len(cost_requests) == 1
    cost_payload = json.loads(cost_requests[0].read())
    assert cost_payload["content"]["context"]["outcome"] == "provider_retired"
    assert cost_payload["content"]["context"]["exc_type"] == "LiteLLMProviderRetiredError"


def test_liability_mapper_keeps_next_payment_date():
    mapped = _liability_from_credit(
        {
            "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 24.99}],
            "minimum_payment_amount": 35.0,
            "last_statement_balance": 1200.0,
            "next_payment_due_date": "2026-06-15",
        }
    )

    assert mapped["apr"] == 24.99
    assert mapped["principal"] == 1200.0
    assert mapped["next_payment_date"] == "2026-06-15"


@pytest.mark.asyncio
async def test_register_liability_bill_stores_account_id(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    account_id = "11111111-1111-4111-8111-111111111111"

    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json={"id": "bill-1"})

    result = await bank_sync.register_liability_bills_activity(
        "citi",
        [{"account_id": "plaid-card-1", "name": "Citi AAdvantage", "mask": "1234"}],
        {"plaid-card-1": {"next_payment_date": "2026-06-10", "min_payment": 35.0}},
        {"plaid-card-1": account_id},
    )

    assert result == {"created": 1, "updated": 0, "skipped": 0}
    request = httpx_mock.get_request(method="POST", url="http://substrate.test/beads")
    payload = request.read()
    assert f'"account_id":"{account_id}"'.encode() in payload


@pytest.mark.asyncio
async def test_reconcile_activity_clears_overdue_liability_bill(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    calls = []

    async def fake_patch_state(bead_id, *, state, created_by="x"):
        calls.append(("state", bead_id, state, created_by))
        return {"id": bead_id, "state": state}

    async def fake_link(tx_id, manual_bead_id, created_by="x"):
        calls.append(("link", tx_id, manual_bead_id, created_by))
        return {"id": tx_id, "parent_id": manual_bead_id}

    monkeypatch.setattr(bank_sync, "patch_bead_state", fake_patch_state)
    monkeypatch.setattr(bank_sync, "link_transaction_to_manual", fake_link)

    account_id = "11111111-1111-4111-8111-111111111111"
    httpx_mock.add_response(method="GET", json=[])  # logged expenses
    httpx_mock.add_response(method="GET", json=[])  # pending bills
    httpx_mock.add_response(
        method="GET",
        json=[
            {
                "id": "bill-citi",
                "type": "bill",
                "state": "overdue",
                "content": {
                    "source": "liability",
                    "vendor": "Citi AAdvantage Platinum Select",
                    "amount": 35.0,
                    "due_date": "2026-06-10",
                    "account_id": account_id,
                },
            }
        ],
    )
    httpx_mock.add_response(
        method="GET",
        json=[
            {
                "id": "tx-payment",
                "state": "posted",
                "content": {
                    "amount": -35.0,
                    "posted_date": "2026-06-10",
                    "account_id": account_id,
                    "merchant_name": "PAYMENT THANK YOU",
                    "description": "CITI CARD ONLINE PAYMENT THANK YOU",
                    "is_transfer": True,
                },
            }
        ],
    )

    result = await bank_sync.reconcile_beads_activity()

    assert result == {"manuals_considered": 1, "matched": 1, "skipped": 0}
    assert calls == [
        ("state", "bill-citi", "paid", "bank-sync/reconcile"),
        ("link", "tx-payment", "bill-citi", "bank-sync/reconcile"),
    ]


@pytest.mark.asyncio
async def test_reconcile_activity_uses_assign_liability_payments_for_liability_bills(
    monkeypatch, httpx_mock
):
    """AC-5 / dev.finding d7cdc16c: liability bills route through
    assign_liability_payments, not the first-fit loop. On the fixture's
    posted-only pool, only 129c20b8 (whose payment aa5471dc is still
    state=posted) resolves; 3da69195 and 958a9bde stay unmatched because
    their correct payments are already reconciled to the wrong bills and so
    are absent from the posted pool.
    """
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    calls = []

    async def fake_patch_state(bead_id, *, state, created_by="x"):
        calls.append(("state", bead_id, state, created_by))
        return {"id": bead_id, "state": state}

    async def fake_link(tx_id, manual_bead_id, created_by="x"):
        calls.append(("link", tx_id, manual_bead_id, created_by))
        return {"id": tx_id, "parent_id": manual_bead_id}

    monkeypatch.setattr(bank_sync, "patch_bead_state", fake_patch_state)
    monkeypatch.setattr(bank_sync, "link_transaction_to_manual", fake_link)

    fixture = _load_liability_fixture()
    bills_by_id = {b["id"]: b for b in fixture["bills"]}
    pending_bills = [
        bills_by_id["958a9bde-d220-4c01-b16f-4ae288234859"],
        bills_by_id["42bd5da7-9151-4cbd-9818-a9de9a00594f"],
    ]
    overdue_bills = [
        bills_by_id["3da69195-3ff1-41da-b17f-6c39ebdd21bf"],
        bills_by_id["129c20b8-3812-4b80-93dc-f4f338958076"],
    ]
    posted_transactions = [t for t in fixture["transactions"] if t["state"] == "posted"]
    assert len(posted_transactions) == 24

    httpx_mock.add_response(method="GET", json=[])  # logged expenses
    httpx_mock.add_response(method="GET", json=pending_bills)
    httpx_mock.add_response(method="GET", json=overdue_bills)
    httpx_mock.add_response(method="GET", json=posted_transactions)

    result = await bank_sync.reconcile_beads_activity()

    assert calls == [
        (
            "state",
            "129c20b8-3812-4b80-93dc-f4f338958076",
            "paid",
            "bank-sync/reconcile",
        ),
        (
            "link",
            "aa5471dc-6bd3-440b-a0e7-dd26f536631d",
            "129c20b8-3812-4b80-93dc-f4f338958076",
            "bank-sync/reconcile",
        ),
    ]
    assert result == {"manuals_considered": 4, "matched": 1, "skipped": 3}


@pytest.mark.asyncio
async def test_write_transaction_payload_preserves_raw_description_and_normalized_merchant(
    httpx_mock, monkeypatch
):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "tx-bead"},
    )

    await write_transaction_bead_activity(
        {
            "institution": "chase",
            "transaction_id": "txn-123",
            "account_id": "plaid-account-1",
            "amount": 5.5,
            "iso_currency_code": "USD",
            "merchant_name": "STARBUCKS STORE 1234",
            "name": "STARBUCKS STORE 1234 CHICAGO IL",
            "date": "2026-05-27",
        },
        "11111111-1111-4111-8111-111111111111",
        {"category": "dining", "is_transfer": False},
    )

    request = httpx_mock.get_request(method="POST", url="http://substrate.test/beads")
    payload = request.read()
    assert b'"description":"STARBUCKS STORE 1234 CHICAGO IL"' in payload
    assert b'"normalized_merchant":"Starbucks"' in payload


@pytest.mark.asyncio
async def test_write_transaction_preserves_unmeasured_confidence(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "tx-bead"},
    )

    await write_transaction_bead_activity(
        {
            "institution": "chase",
            "transaction_id": "txn-unmeasured",
            "account_id": "plaid-account-1",
            "amount": 80.0,
            "merchant_name": "Comcast Cable",
            "name": "Comcast Cable",
            "date": "2026-05-27",
        },
        "11111111-1111-4111-8111-111111111111",
        {
            "category": "utilities",
            "is_transfer": False,
            "categorization_source": "litellm/gemini-flash",
            "categorization_confidence": None,
        },
    )

    request = httpx_mock.get_request(method="POST", url="http://substrate.test/beads")
    payload = json.loads(request.read())
    assert payload["content"]["our_category"] == "utilities"
    assert payload["content"]["categorization_source"] == "litellm/gemini-flash"
    assert payload["content"]["categorization_confidence"] is None


@pytest.mark.asyncio
async def test_write_transaction_source_fallback_records_the_actual_default_model(
    monkeypatch, httpx_mock
):
    """categorize_transaction_activity's return dict never sets
    categorization_source (only the bulk path does) — write_transaction_
    bead_activity's fallback must record the model that call actually (and
    only ever) requests, not a stale literal (AC-5)."""
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "tx-bead-fallback"},
    )

    await write_transaction_bead_activity(
        {
            "institution": "chase",
            "transaction_id": "txn-fallback",
            "account_id": "plaid-account-1",
            "amount": 5.5,
            "merchant_name": "Whole Foods",
            "name": "Whole Foods",
            "date": "2026-05-27",
        },
        "11111111-1111-4111-8111-111111111111",
        {"category": "groceries", "is_transfer": False},
    )

    request = httpx_mock.get_request(method="POST", url="http://substrate.test/beads")
    payload = json.loads(request.read())
    assert payload["content"]["categorization_source"] == f"litellm/{litellm_client.LIFEOPS_DEFAULT_MODEL}"
    assert payload["content"]["categorization_source"] == "litellm/claude-haiku"


@pytest.mark.asyncio
async def test_write_transaction_payload_records_flow_independent_of_category(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-substrate-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    httpx_mock.add_response(
        url="http://substrate.test/beads",
        method="POST",
        status_code=200,
        json={"id": "tx-bead"},
    )

    await write_transaction_bead_activity(
        {
            "institution": "chase",
            "transaction_id": "txn-payment",
            "account_id": "plaid-account-1",
            "amount": 850.0,
            "merchant_name": "CHASE CREDIT CARD",
            "name": "Online Payment, Thank You",
            "date": "2026-05-27",
        },
        "11111111-1111-4111-8111-111111111111",
        {"category": "dining", "is_transfer": False},
    )

    request = httpx_mock.get_request(method="POST", url="http://substrate.test/beads")
    payload = json.loads(request.read())
    assert payload["content"]["our_category"] == "dining"
    assert payload["content"]["is_transfer"] is False
    assert payload["content"]["flow"] == "transfer"
    assert payload["content"]["flow"] in FLOW_VALUES


def test_low_confidence_metric_counts_unparsed_and_failed_calls():
    rows = [
        {"categorization_source": "rule", "categorization_confidence": 1.0},
        {"categorization_source": "local_ml/naive_bayes", "categorization_confidence": 0.91},
        {"categorization_source": "litellm/gemini-flash", "categorization_confidence": None},
        {"categorization_source": "litellm/unparsed", "categorization_confidence": 0.0},
        {"categorization_source": "litellm/failed", "categorization_confidence": 0.0},
    ]

    assert count_low_or_unmeasured_categorization_confidence(rows) > 0
    assert count_low_or_unmeasured_categorization_confidence(rows) == 3


@pytest.mark.asyncio
async def test_check_item_status_alerts_on_bad_access_token(monkeypatch):
    class FakePlaidException(Exception):
        body = '{"error_code": "INVALID_ACCESS_TOKEN"}'

    messages = []

    async def fake_get_item_status(_access_token):
        raise FakePlaidException("bad token")

    async def fake_post_discord(content):
        messages.append(content)
        return True

    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "bad-token")
    monkeypatch.setenv("MCP_HUB_PUBLIC_URL", "https://mcp-hub-prod.example.org")
    monkeypatch.setattr(bank_sync, "get_item_status", fake_get_item_status)
    monkeypatch.setattr(bank_sync, "_post_discord", fake_post_discord)

    result = await bank_sync.check_item_status_activity("chase")

    assert result == {
        "healthy": False,
        "error_code": "INVALID_ACCESS_TOKEN",
        "alert_sent": True,
    }
    assert len(messages) == 1
    assert "Plaid re-auth required" in messages[0]
    assert "https://mcp-hub-prod.example.org/finance/link/chase" in messages[0]
    # INVALID_ACCESS_TOKEN means the Item is unrecoverable — update mode
    # can't repair it, so the alert must link to create mode.
    assert "?mode=update" not in messages[0]


@pytest.mark.asyncio
async def test_check_item_status_links_update_mode_for_repairable_code(monkeypatch):
    """ITEM_LOGIN_REQUIRED is repairable in place: the alert must point at
    the update-mode portal so the existing Item (and token) is reused
    instead of minting a duplicate Item."""
    messages = []

    async def fake_get_item_status(_access_token):
        return {"item": {"error": {"error_code": "ITEM_LOGIN_REQUIRED"}}}

    async def fake_post_discord(content):
        messages.append(content)
        return True

    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "live-token")
    monkeypatch.setenv("MCP_HUB_PUBLIC_URL", "https://mcp-hub-prod.example.org")
    monkeypatch.setattr(bank_sync, "get_item_status", fake_get_item_status)
    monkeypatch.setattr(bank_sync, "_post_discord", fake_post_discord)

    result = await bank_sync.check_item_status_activity("chase")

    assert result == {
        "healthy": False,
        "error_code": "ITEM_LOGIN_REQUIRED",
        "alert_sent": True,
    }
    assert len(messages) == 1
    assert (
        "https://mcp-hub-prod.example.org/finance/link/chase?mode=update"
        in messages[0]
    )


@pytest.mark.asyncio
async def test_sync_page_activity_filters_pending_and_stamps_institution(monkeypatch):
    async def fake_sync(access_token, cursor=None):
        assert access_token == "tok"
        assert cursor == "cur-1"
        return {
            "added": [
                {"transaction_id": "t1", "pending": False},
                {"transaction_id": "t2", "pending": True},
            ],
            "modified": [{"transaction_id": "t3", "pending": False}],
            "removed": [{"transaction_id": "t4"}],
            "next_cursor": "cur-2",
            "has_more": True,
        }

    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "tok")
    monkeypatch.setattr(bank_sync, "sync_transactions_page", fake_sync)

    page = await bank_sync.sync_transactions_page_activity("chase", "cur-1")

    assert [t["transaction_id"] for t in page["added"]] == ["t1"]
    assert page["added"][0]["institution"] == "chase"
    assert page["modified"][0]["institution"] == "chase"
    assert page["removed"] == [{"transaction_id": "t4"}]
    assert page["next_cursor"] == "cur-2"
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_sync_page_activity_without_token_returns_empty_terminal_page(monkeypatch):
    monkeypatch.delenv("PLAID_ACCESS_TOKEN_NOWHERE", raising=False)

    page = await bank_sync.sync_transactions_page_activity("nowhere", None)

    assert page == {
        "added": [], "modified": [], "removed": [],
        "next_cursor": None, "has_more": False,
    }


@pytest.mark.asyncio
async def test_ensure_item_registry_returns_existing_bead_and_cursor(monkeypatch):
    async def fake_query(params):
        return [
            {
                "id": "bead-1",
                "content": {
                    "institution": "chase",
                    "plaid_env": "sandbox",
                    "transactions_cursor": "cur-9",
                },
            },
        ]

    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)

    result = await bank_sync.ensure_item_registry_activity("chase")

    assert result == {"bead_id": "bead-1", "cursor": "cur-9"}


@pytest.mark.asyncio
async def test_ensure_item_registry_self_heals_via_item_get(monkeypatch):
    """Institutions linked before the Item registry existed have no
    plaid_item bead — the activity recovers item_id from /item/get and
    registers one."""
    captured = {}

    async def fake_query(params):
        return []

    async def fake_item_status(_token):
        return {"item": {"item_id": "item-77"}}

    async def fake_register(slug, item_id, env, created_by="x"):
        captured.update({"slug": slug, "item_id": item_id, "env": env})
        return {"bead_id": "bead-77", "created": True}

    monkeypatch.setenv("PLAID_ACCESS_TOKEN_CHASE", "tok")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    monkeypatch.setattr(bank_sync, "get_item_status", fake_item_status)
    monkeypatch.setattr(bank_sync, "register_plaid_item", fake_register)

    result = await bank_sync.ensure_item_registry_activity("chase")

    assert result == {"bead_id": "bead-77", "cursor": None}
    assert captured == {"slug": "chase", "item_id": "item-77", "env": "sandbox"}


@pytest.mark.asyncio
async def test_store_sync_cursor_merges_existing_content(monkeypatch):
    """Substrate PATCH replaces content wholesale — the activity must
    re-read and merge, never clobber the registry fields."""
    patched = {}

    async def fake_query(params):
        return [
            {
                "id": "bead-1",
                "content": {"institution": "chase", "plaid_env": "sandbox", "item_id": "i-1"},
            },
        ]

    async def fake_patch(bead_id, *, state=None, parent_id=None, content=None, created_by="x"):
        patched.update({"id": bead_id, "content": content})
        return {"id": bead_id}

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    monkeypatch.setattr(bank_sync, "patch_bead", fake_patch)

    await bank_sync.store_sync_cursor_activity("bead-1", "cur-10")

    assert patched["id"] == "bead-1"
    assert patched["content"]["transactions_cursor"] == "cur-10"
    assert patched["content"]["institution"] == "chase"
    assert patched["content"]["item_id"] == "i-1"
    assert "last_synced" in patched["content"]


@pytest.mark.asyncio
async def test_apply_removed_marks_known_and_skips_unknown(monkeypatch, httpx_mock):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    httpx_mock.add_response(
        url="http://substrate.test/beads?namespace=finance&type=transaction&limit=5000",
        method="GET",
        json=[
            {"id": "b1", "state": "posted", "content": {"plaid_transaction_id": "t1"}},
            {"id": "b2", "state": "removed", "content": {"plaid_transaction_id": "t2"}},
        ],
    )
    calls = []

    async def fake_patch_state(bead_id, *, state, created_by="x"):
        calls.append((bead_id, state))
        return {}

    monkeypatch.setattr(bank_sync, "patch_bead_state", fake_patch_state)

    result = await bank_sync.apply_removed_transactions_activity(
        [
            {"transaction_id": "t1"},
            {"transaction_id": "t2"},   # already removed — no second patch
            {"transaction_id": "t-unknown"},
        ]
    )

    assert result == {"removed": 1, "unknown": 1}
    assert calls == [("b1", "removed")]


@pytest.mark.asyncio
async def test_write_transaction_skips_orphaned_account(monkeypatch):
    """Transactions for accounts with no bead (closed cards surfacing in
    full-history sync) must noop instead of 422ing against Substrate's
    UUID account_id requirement."""
    result = await write_transaction_bead_activity(
        {"transaction_id": "txn-orphan", "institution": "amex", "amount": 1.0,
         "merchant_name": "X", "name": "X", "date": "2026-06-01"},
        None,
        {"category": "misc", "is_transfer": False},
    )
    assert result is None


@pytest.mark.asyncio
async def test_notify_sync_failure_posts_discord(monkeypatch):
    messages = []
    failure_records = []
    alert_records = []

    async def fake_post_discord(content):
        messages.append(content)
        return True

    async def fake_record_sync_failure(institution_slug, error):
        failure_records.append((institution_slug, error))

    async def fake_load_alert_state(kind):
        return None

    async def fake_record_alert_posted(kind, fingerprint, existing, members=None):
        alert_records.append((kind, fingerprint, existing, members))

    monkeypatch.setattr(bank_sync, "_record_sync_failure", fake_record_sync_failure)
    monkeypatch.setattr(bank_sync, "_load_alert_state", fake_load_alert_state)
    monkeypatch.setattr(bank_sync, "_record_alert_posted", fake_record_alert_posted)
    monkeypatch.setattr(bank_sync, "_post_discord", fake_post_discord)

    ok = await bank_sync.notify_sync_failure_activity("sofi", "Temporary failure in name resolution")

    assert ok is True
    assert failure_records == [("sofi", "Temporary failure in name resolution")]
    assert "Bank sync FAILED" in messages[0]
    assert "`sofi`" in messages[0]
    assert "name resolution" in messages[0]
    assert alert_records[0][0] == "sync_failure:sofi"


def test_sync_failure_message_prefers_nested_activity_cause():
    root = httpx.HTTPStatusError(
        "Client error '429 Too Many Requests' for url 'http://litellm.test/v1/chat/completions'",
        request=httpx.Request("POST", "http://litellm.test/v1/chat/completions"),
        response=httpx.Response(429),
    )
    wrapper = RuntimeError("Activity task failed")
    wrapper.__cause__ = root

    message = _sync_failure_message(wrapper)

    assert "429 Too Many Requests" in message
    assert "litellm.test" in message


def test_schedule_retry_attempts_stay_in_sync():
    """SCHEDULE_RETRY_MAX_ATTEMPTS gates the final-attempt failure alert,
    so every place that configures the workflow retry policy must agree
    with it — otherwise a lowered policy makes failing syncs silent
    forever (alert gated on an attempt number that never arrives)."""
    from tools import schedules

    assert (
        schedules.bank_sync_workflow_retry_policy().maximum_attempts
        == bank_sync.SCHEDULE_RETRY_MAX_ATTEMPTS
    )

    # temporal_worker mixes import roots (src.*), so check its source
    # text instead of importing it — same approach as the manifest test.
    worker_src = (
        Path(__file__).resolve().parents[1] / "src" / "temporal_worker.py"
    ).read_text()
    assert "maximum_attempts=SCHEDULE_RETRY_MAX_ATTEMPTS" in worker_src


def test_temporal_worker_declares_litellm_url():
    repo_root = Path(__file__).resolve().parents[3]
    manifest = repo_root / "infrastructure/k8s/base/mcp-hub/temporal-worker.yaml"
    text = manifest.read_text()

    assert "- name: LITELLM_URL" in text
    assert (
        'value: "http://litellm.infra-ai.svc.cluster.local:4000/v1/chat/completions"'
        in text
    )


# --- Category anomaly threshold (LO-BIL-003, 2026-08-02) -----------------
#
# The threshold decides whether the Operator gets interrupted, so it has to be able
# to fail. The old rule was `amount > 2 x category mean`, which fired on
# `$261.63 at Angelo Caputo's (groceries)` — an ordinary weekly shop — and
# re-sent it for two days.

# The real groceries sample from the 30 days to 2026-08-02: mean $89.52,
# sigma $72.93, so mean + 3 sigma = $308.30.
_GROCERIES_30D = [261.63, 154.92, 122.68, 99.14, 86.20, 61.55, 47.30, 33.18, 21.40, 7.20]


def test_ordinary_large_grocery_shop_is_not_an_anomaly():
    """The regression this retune exists for: Caputo's must not fire."""
    baseline = category_baseline(_GROCERIES_30D)
    assert baseline is not None
    assert is_category_anomaly(261.63, baseline) is False


def test_genuine_category_outlier_still_fires():
    """Suppressing the false positive must not suppress the real signal."""
    baseline = category_baseline(_GROCERIES_30D)
    assert is_category_anomaly(450.00, baseline) is True


def test_thin_sample_yields_no_baseline():
    """Standard deviation over 3 points is noise, not a threshold."""
    assert category_baseline([10.0, 20.0, 90.0]) is None


def test_identical_charges_yield_no_baseline():
    """sigma == 0 would set threshold == mean and fire on any variation."""
    assert category_baseline([25.0] * 12) is None


def test_absolute_floor_suppresses_small_category_outliers():
    """`health` averages ~$6; a $19 pharmacy trip is 3 sigma and still noise."""
    baseline = category_baseline([4.0, 5.0, 6.0, 7.0, 8.0, 5.5, 6.5, 4.5, 7.5, 6.0])
    assert baseline is not None
    assert 19.0 > baseline["threshold"]  # it IS a statistical outlier...
    assert is_category_anomaly(19.0, baseline) is False  # ...and still not worth a ping


def test_no_baseline_never_fires():
    assert is_category_anomaly(10_000.0, None) is False


# --- Alert suppression (notification hygiene, 2026-08-01) ----------------
#
# detect_anomalies_activity queries ALL finance transactions — its result does
# not depend on which institution triggered it — yet BankSyncWorkflow runs it
# once per institution per sync. Prod is 5 institutions x 4 runs/day = 20
# identical executions, and every one of them used to post.
#
# (The data-quality check was the second such caller until it was deleted on
# 2026-08-02 — see the note above _ALERT_STATE_TYPE in bank_sync.)


class _FakeAlertState:
    """In-memory stand-in for the finance.alert_state bead round-trip."""

    def __init__(self, initial=None):
        self.beads = list(initial or [])
        self.posts = []

    def install(self, monkeypatch):
        async def fake_query(params):
            assert params["type"] == bank_sync._ALERT_STATE_TYPE
            return list(self.beads)

        async def fake_patch(bead_id, *, content=None, created_by=None):
            for b in self.beads:
                if b["id"] == bead_id:
                    b["content"] = content
            return {"id": bead_id}

        async def fake_post(content):
            self.posts.append(content)
            return True

        monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
        monkeypatch.setattr(bank_sync, "patch_bead", fake_patch)
        monkeypatch.setattr(bank_sync, "_post_discord", fake_post)
        return self


@pytest.mark.asyncio
async def test_alert_suppressed_when_fingerprint_unchanged(monkeypatch):
    """The 2nd..20th run of an unchanged alert must stay quiet."""
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.quality",
                "fingerprint": "312:0",
                "last_posted_at": "2026-07-01T03:00:00",
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.quality", "312:0", "312 unreconciled", min_interval_hours=24.0
    )

    assert posted is False
    assert state.posts == []


@pytest.mark.asyncio
async def test_first_alert_posts_and_records_state(monkeypatch):
    state = _FakeAlertState().install(monkeypatch)
    created = []

    class _Resp:
        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            created.append(json)
            return _Resp()

    monkeypatch.setattr(bank_sync.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")

    posted = await bank_sync._alert_policy().send(
        "bank_sync.anomalies", "tx-1", ":rotating_light: one anomaly"
    )

    assert posted is True
    assert state.posts == [":rotating_light: one anomaly"]
    assert created[0]["type"] == bank_sync._ALERT_STATE_TYPE
    assert created[0]["content"]["fingerprint"] == "tx-1"


@pytest.mark.asyncio
async def test_changed_alert_still_respects_min_interval(monkeypatch):
    """A drifting counter must not become a new daily alert every run.

    The unreconciled backlog moves by a transaction or two through the day;
    without the interval floor, "changed" would be true at 03:00, 09:00,
    15:00 and 21:00 and the fix would buy nothing.
    """
    from datetime import datetime, timedelta

    recent = (datetime.now() - timedelta(hours=6)).isoformat()
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.quality",
                "fingerprint": "312:0",
                "last_posted_at": recent,
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.quality", "313:0", "313 unreconciled", min_interval_hours=24.0
    )

    assert posted is False
    assert state.posts == []


@pytest.mark.asyncio
async def test_changed_alert_posts_once_interval_elapsed(monkeypatch):
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(hours=30)).isoformat()
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.quality",
                "fingerprint": "312:0",
                "last_posted_at": old,
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.quality", "313:0", "313 unreconciled", min_interval_hours=24.0
    )

    assert posted is True
    assert state.posts == ["313 unreconciled"]
    assert state.beads[0]["content"]["fingerprint"] == "313:0"


@pytest.mark.asyncio
async def test_new_anomaly_alerts_immediately(monkeypatch):
    """Suppression is change-based, not time-based: a newly flagged
    transaction must alert on the very next sync, not wait out a window."""
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.anomalies",
                "fingerprint": "tx-1",
                "last_posted_at": "2026-08-01T03:00:00",
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.anomalies", "tx-1|tx-2", ":rotating_light: two anomalies"
    )

    assert posted is True
    assert len(state.posts) == 1


@pytest.mark.asyncio
async def test_anomaly_set_draining_does_not_realert(monkeypatch):
    """A transaction ageing OUT of the 24h window is not new information.

    detect_anomalies_activity looks back 24h, so its flagged set shrinks on
    its own. Fingerprinting the set alone, {tx-1,tx-2} -> {tx-2} reads as
    "changed" and re-announces tx-2, which the earlier post already named —
    and a window of N anomalies draining one at a time costs N-1 redundant
    posts. That is the same noise the suppression gate exists to remove,
    arriving by a slower door.
    """
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.anomalies",
                "fingerprint": "tx-1|tx-2",
                "members": ["tx-1", "tx-2"],
                "last_posted_at": "2026-08-01T03:00:00",
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.anomalies",
        "tx-2",
        ":rotating_light: one anomaly",
        members=["tx-2"],
    )

    assert posted is False
    assert state.posts == []


@pytest.mark.asyncio
async def test_anomaly_set_with_an_unseen_member_still_alerts(monkeypatch):
    """The shrink gate must not swallow genuinely new transactions.

    tx-1 ages out and tx-3 appears in the same run: the set is neither equal
    to nor a subset of the last one, so it must interrupt.
    """
    state = _FakeAlertState([
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.anomalies",
                "fingerprint": "tx-1|tx-2",
                "members": ["tx-1", "tx-2"],
                "last_posted_at": "2026-08-01T03:00:00",
            },
        }
    ]).install(monkeypatch)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.anomalies",
        "tx-2|tx-3",
        ":rotating_light: two anomalies",
        members=["tx-2", "tx-3"],
    )

    assert posted is True
    assert state.posts == [":rotating_light: two anomalies"]
    # The recorded set is what was just announced, so tx-3 ageing out later
    # cannot reopen the channel on tx-2 alone.
    assert state.beads[0]["content"]["members"] == ["tx-2", "tx-3"]


@pytest.mark.asyncio
async def test_substrate_failure_fails_open(monkeypatch):
    """A bookkeeping outage may cost a duplicate; it must not eat an alert."""
    posts = []

    async def boom(params):
        raise RuntimeError("substrate down")

    async def fake_post(content):
        posts.append(content)
        return True

    async def fake_record(kind, fingerprint, existing):
        raise RuntimeError("substrate still down")

    monkeypatch.setattr(bank_sync, "finance_query_beads", boom)
    monkeypatch.setattr(bank_sync, "_post_discord", fake_post)
    monkeypatch.setattr(bank_sync, "_record_alert_posted", fake_record)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.quality", "312:0", "312 unreconciled", min_interval_hours=24.0
    )

    assert posted is True
    assert posts == ["312 unreconciled"]


@pytest.mark.asyncio
async def test_undelivered_alert_is_not_recorded_as_seen(monkeypatch):
    """If Discord delivery fails, the next run must retry rather than treat
    the alert as already announced."""
    recorded = []

    async def fake_query(params):
        return []

    async def fake_post(content):
        return False  # webhook missing or HTTP failure (best-effort mode)

    async def fake_record(kind, fingerprint, existing):
        recorded.append(kind)

    monkeypatch.setattr(bank_sync, "finance_query_beads", fake_query)
    monkeypatch.setattr(bank_sync, "_post_discord", fake_post)
    monkeypatch.setattr(bank_sync, "_record_alert_posted", fake_record)

    posted = await bank_sync._alert_policy().send(
        "bank_sync.anomalies", "tx-9", "anomaly"
    )

    assert posted is False
    assert recorded == []
