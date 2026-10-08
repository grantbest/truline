"""Unit tests for the SDD Phase 3 scenario engine (pure projection logic)."""

from datetime import date

from workflows.finance_scenario_runner import (
    build_projection,
    liquid_assets,
    month_labels,
    net_monthly_flow,
    project_wealth,
)

TODAY = date(2026, 6, 19)


def test_month_labels_cross_year_boundary():
    labels = month_labels(date(2026, 11, 15), 3)
    assert labels == ["2026-12", "2027-01", "2027-02"]


def test_liquid_assets_sums_only_depository():
    accounts = [
        {"state": "active", "content": {"type": "depository", "current_balance": 10_000}},
        {"state": "active", "content": {"type": "depository", "current_balance": 2_500}},
        {"state": "active", "content": {"type": "credit", "current_balance": 4_000}},
        {"state": "removed", "content": {"type": "depository", "current_balance": 9_999}},
    ]
    assert liquid_assets(accounts) == 12_500


def _txn(amount, day):
    return {"state": "active", "content": {"amount": amount, "posted_date": day.isoformat()}}


def test_net_monthly_flow_income_minus_spend():
    # Over 90 days: -9000 inflow (Plaid negative) + 3000 spend (positive).
    txns = [
        _txn(-3000, date(2026, 6, 1)),
        _txn(-3000, date(2026, 5, 1)),
        _txn(-3000, date(2026, 4, 1)),
        _txn(1000, date(2026, 6, 2)),
        _txn(1000, date(2026, 5, 2)),
        _txn(1000, date(2026, 4, 2)),
    ]
    # signed spend total = -6000 over 3 months -> net flow +2000/mo
    assert net_monthly_flow(txns, today=TODAY) == 2000


def test_net_monthly_flow_excludes_transfers_and_old():
    txns = [
        {"state": "active", "content": {"amount": 5000, "posted_date": "2026-06-01", "is_transfer": True}},
        _txn(900, date(2026, 1, 1)),  # outside 90-day window
    ]
    assert net_monthly_flow(txns, today=TODAY) == 0.0


def test_project_wealth_applies_upfront_once_and_monthly():
    series = project_wealth(10_000, 500, 3, upfront=-2_000, monthly_delta=-600)
    # start 10000-2000=8000; step = 500-600 = -100 each month
    assert series == [7_900, 7_800, 7_700]


def test_build_projection_baseline_vs_scenario_delta():
    proj = build_projection(
        scenario_name="Buy New Car",
        starting_liquid=20_000,
        net_flow=1_000,
        monthly_burn=5_000,
        monthly_impact=-600,
        upfront_impact=-5_000,
        today=TODAY,
        months=12,
    )
    assert proj["name"] == "Buy New Car"
    assert len(proj["months"]) == 12
    assert len(proj["baseline_wealth"]) == 12
    assert len(proj["scenario_wealth"]) == 12
    assert proj["months"][0] == "2026-07"
    # baseline ends at 20000 + 12*1000 = 32000
    assert proj["ending_baseline"] == 32_000
    # scenario: 20000 - 5000 upfront + 12*(1000-600) = 15000 + 4800 = 19800
    assert proj["ending_scenario"] == 19_800
    assert proj["delta_ending"] == -12_200
