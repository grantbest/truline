"""Unit tests for the SDD Phase 3 yield/cash-drag optimizer (pure logic)."""

from workflows.finance_yield_optimizer import analyze_allocation


def _checking(acc_id, name, balance, *, subtype="checking"):
    return {
        "id": acc_id,
        "state": "active",
        "content": {
            "name": name,
            "type": "depository",
            "subtype": subtype,
            "current_balance": balance,
        },
    }


def _liability(acc_id, name, balance, apr, *, type_="credit"):
    return {
        "id": acc_id,
        "state": "active",
        "content": {
            "name": name,
            "type": type_,
            "current_balance": balance,
            "liabilities": {"apr": apr, "principal": balance},
        },
    }


def test_idle_cash_paid_against_highest_apr_liability():
    accounts = [
        _checking("chk", "Checking", 20_000),
        _liability("amex", "Amex", 4_000, 22.0),
        _liability("visa", "Visa", 3_000, 15.0),
    ]
    # buffer = 1 month burn = 5000 -> deployable 15000
    out = analyze_allocation(accounts, monthly_burn=5_000)
    assert len(out) == 2
    # Highest APR first, fully paid (4000), then visa gets the rest (3000).
    assert out[0]["target_account_id"] == "amex"
    assert out[0]["amount"] == 4_000
    assert out[0]["projected_annual_savings"] == round(4_000 * 0.22, 2)
    assert out[0]["source_account_id"] == "chk"
    assert out[0]["fingerprint"] == "paydown:chk:amex"
    assert out[1]["target_account_id"] == "visa"
    assert out[1]["amount"] == 3_000


def test_no_excess_after_buffer_yields_nothing():
    accounts = [
        _checking("chk", "Checking", 5_200),
        _liability("amex", "Amex", 4_000, 22.0),
    ]
    # buffer 5000 -> deployable 200 < MIN_EXCESS_CASH
    assert analyze_allocation(accounts, monthly_burn=5_000) == []


def test_no_liability_yields_nothing():
    accounts = [_checking("chk", "Checking", 50_000)]
    assert analyze_allocation(accounts, monthly_burn=3_000) == []


def test_liability_without_apr_skipped():
    accounts = [
        _checking("chk", "Checking", 20_000),
        _liability("loan", "Mystery loan", 9_000, None),
    ]
    assert analyze_allocation(accounts, monthly_burn=2_000) == []


def test_savings_not_treated_as_idle_cash():
    # Savings is excluded from the zero-yield pool; only checking counts.
    accounts = [
        _checking("sav", "Savings", 30_000, subtype="savings"),
        _liability("amex", "Amex", 4_000, 22.0),
    ]
    assert analyze_allocation(accounts, monthly_burn=2_000) == []


def test_removed_accounts_ignored():
    accounts = [
        _checking("chk", "Checking", 20_000),
        _liability("amex", "Amex", 4_000, 22.0),
    ]
    accounts[1]["state"] = "removed"
    assert analyze_allocation(accounts, monthly_burn=2_000) == []


def test_deployable_capped_by_liability_balance():
    accounts = [
        _checking("chk", "Checking", 100_000),
        _liability("amex", "Amex", 4_000, 22.0),
    ]
    out = analyze_allocation(accounts, monthly_burn=3_000)
    assert len(out) == 1
    # Can't pay down more than the balance owed.
    assert out[0]["amount"] == 4_000


def test_tiny_savings_below_floor_skipped():
    accounts = [
        _checking("chk", "Checking", 12_000),
        _liability("amex", "Amex", 50, 1.0),  # annual savings ~ $0.50
    ]
    out = analyze_allocation(accounts, monthly_burn=5_000)
    assert out == []
