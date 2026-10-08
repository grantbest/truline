"""Unit tests for the run-rate calculator (tools.finance.calculate_run_rate).

Verifies the bead-derived Fixed/Variable split, posted_date windowing (the fix
for the historical-backfill created_at overcount), and exclusion of transfers
and refunds.
"""

from datetime import date, datetime

import pytest

from tools import finance


def test_months_before_crosses_year_boundary():
    assert finance._months_before(date(2026, 3, 1), 3) == date(2025, 12, 1)
    assert finance._months_before(date(2026, 6, 1), 1) == date(2026, 5, 1)
    assert finance._months_before(date(2026, 1, 1), 1) == date(2025, 12, 1)


def test_bill_monthly_equivalent():
    f = finance._bill_monthly_equivalent
    assert f(100.0, "monthly") == 100.0
    assert f(300.0, "quarterly") == 100.0
    assert f(1200.0, "annual") == 100.0
    assert f(50.0, None) == 50.0  # default monthly
    assert f("bad", "monthly") == 0.0


@pytest.mark.asyncio
async def test_run_rate_windows_on_posted_date_and_splits_fixed_variable(monkeypatch):
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    in_window = finance._months_before(first_of_current, 1).isoformat()   # last month
    out_window = finance._months_before(first_of_current, 12).isoformat()  # ~1yr ago

    async def fake_query_beads(params):
        t = params["type"]
        if t == "transaction":
            return [
                {"state": "posted", "content": {"amount": 300.0, "posted_date": in_window}},
                # transfer — excluded
                {"state": "posted", "content": {"amount": 200.0, "posted_date": in_window, "is_transfer": True}},
                # refund (negative) — excluded
                {"state": "posted", "content": {"amount": -50.0, "posted_date": in_window}},
                # out of window — excluded
                {"state": "posted", "content": {"amount": 999.0, "posted_date": out_window}},
                # Plaid-removed — excluded
                {"state": "removed", "content": {"amount": 500.0, "posted_date": in_window}},
                # backfilled bead with no posted_date (the created_at trap) — excluded
                {"state": "posted", "content": {"amount": 999.0}},
            ]
        if t == "subscription":
            return [
                {
                    "id": "sub1",
                    "created_at": "2026-06-01T00:00:00",
                    "content": {
                        "merchant_key": "NETFLIX",
                        "name": "Netflix",
                        "amount": 15.0,
                        "frequency": "monthly",
                        "last_seen": today.isoformat(),
                        "is_price_hike": False,
                    },
                }
            ]
        if t == "bill":
            return [
                {"state": "pending", "content": {"amount": 50.0, "recurring": True, "frequency": "monthly"}},
                # non-recurring bill — excluded from fixed
                {"state": "pending", "content": {"amount": 80.0, "recurring": False}},
            ]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    rr = await finance.calculate_run_rate(months=3)

    # Only the single in-window outflow (300) counts; /3 months = 100.
    assert rr["monthly_run_rate"] == 100.0
    # Fixed = subscriptions (15) + recurring bill (50) = 65.
    assert rr["fixed_monthly"] == 65.0
    assert rr["fixed_breakdown"] == {"subscriptions": 15.0, "recurring_bills": 50.0}
    # Variable = total - fixed = 35.
    assert rr["variable_monthly"] == 35.0
    assert rr["months_analyzed"] == 3


@pytest.mark.asyncio
async def test_run_rate_paginates_transactions(monkeypatch):
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    in_window = finance._months_before(first_of_current, 1).isoformat()
    calls = []

    async def fake_query_beads(params):
        t = params["type"]
        if t == "transaction":
            calls.append(params.get("offset"))
            if params.get("offset") == 0:
                return [
                    {"state": "posted", "content": {"amount": 100.0, "posted_date": in_window}},
                    {"state": "posted", "content": {"amount": 200.0, "posted_date": in_window}},
                ]
            if params.get("offset") == 2:
                return [
                    {"state": "posted", "content": {"amount": 300.0, "posted_date": in_window}},
                ]
            return []
        if t in ("subscription", "bill"):
            return []
        return []

    monkeypatch.setattr(finance, "_RUN_RATE_PAGE_SIZE", 2)
    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    rr = await finance.calculate_run_rate(months=1)

    assert calls == [0, 2]
    assert rr["monthly_run_rate"] == 600.0


@pytest.mark.asyncio
async def test_run_rate_dedupes_recurring_bill_instances(monkeypatch):
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    in_window = finance._months_before(first_of_current, 1).isoformat()

    async def fake_query_beads(params):
        t = params["type"]
        if t == "transaction":
            return [{"state": "posted", "content": {"amount": 500.0, "posted_date": in_window}}]
        if t == "subscription":
            return []
        if t == "bill":
            return [
                {
                    "state": "paid",
                    "created_at": "2026-05-01T00:00:00",
                    "content": {
                        "vendor": "Chase Card",
                        "amount": 45.0,
                        "recurring": True,
                        "frequency": "monthly",
                        "external_id": "grant:chase:1234:2026-05",
                        "due_date": "2026-05-15",
                    },
                },
                {
                    "state": "pending",
                    "created_at": "2026-06-01T00:00:00",
                    "content": {
                        "vendor": "Chase Card",
                        "amount": 50.0,
                        "recurring": True,
                        "frequency": "monthly",
                        "external_id": "grant:chase:1234:2026-06",
                        "due_date": "2026-06-15",
                    },
                },
                {
                    "state": "pending",
                    "content": {
                        "vendor": "Water Bill",
                        "amount": 80.0,
                        "recurring": False,
                        "frequency": "monthly",
                    },
                },
            ]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    rr = await finance.calculate_run_rate(months=1)

    assert rr["fixed_breakdown"]["recurring_bills"] == 50.0
    assert rr["fixed_monthly"] == 50.0


@pytest.mark.asyncio
async def test_run_rate_variable_floors_at_zero(monkeypatch):
    today = datetime.now().date()
    first_of_current = date(today.year, today.month, 1)
    in_window = finance._months_before(first_of_current, 1).isoformat()

    async def fake_query_beads(params):
        t = params["type"]
        if t == "transaction":
            return [{"state": "posted", "content": {"amount": 30.0, "posted_date": in_window}}]
        if t == "subscription":
            return [
                {
                    "id": "s",
                    "created_at": "2026-06-01T00:00:00",
                    "content": {
                        "merchant_key": "BIG",
                        "name": "Big",
                        "amount": 500.0,
                        "frequency": "monthly",
                        "last_seen": today.isoformat(),
                    },
                }
            ]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    rr = await finance.calculate_run_rate(months=1)

    # Fixed (500) exceeds measured spend (30) -> variable floors at 0, never negative.
    assert rr["variable_monthly"] == 0.0
