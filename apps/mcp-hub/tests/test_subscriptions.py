"""Unit tests for the subscriptions console view (tools.finance).

These cover the read-side that turns ``finance.subscription`` beads (written
weekly by the subscription auditor) into the console ledger: monthly-equivalent
normalization, stale/cancel-candidate detection, per-merchant dedup, and the
rollup totals.
"""

from datetime import date, datetime, timedelta

import pytest

from tools import finance


# --- _subscription_monthly_equivalent --------------------------------------


def test_monthly_equivalent_by_cadence():
    f = finance._subscription_monthly_equivalent
    assert f(15.99, "monthly") == 15.99
    assert f(120.0, "annual") == 10.0
    assert f(30.0, "quarterly") == 10.0
    assert f(60.0, "semiannual") == 10.0
    assert f(5.0, "weekly") == 21.65  # 5 * 4.33


def test_monthly_equivalent_unknown_cadence_treated_as_monthly():
    assert finance._subscription_monthly_equivalent(9.99, "unknown") == 9.99
    assert finance._subscription_monthly_equivalent(9.99, None) == 9.99


def test_monthly_equivalent_bad_amount_is_zero():
    assert finance._subscription_monthly_equivalent(None, "monthly") == 0.0
    assert finance._subscription_monthly_equivalent("not-a-number", "monthly") == 0.0


# --- _subscription_is_stale -------------------------------------------------


def test_stale_when_last_charge_exceeds_1_5x_cadence():
    today = date(2026, 6, 13)
    # Monthly cadence (30d) -> stale after 45 days. 60 days ago = stale.
    assert finance._subscription_is_stale("2026-04-10", "monthly", today=today) is True


def test_not_stale_within_cadence():
    today = date(2026, 6, 13)
    # Charged 20 days ago on a monthly cadence -> still live.
    assert finance._subscription_is_stale("2026-05-24", "monthly", today=today) is False


def test_stale_unknown_cadence_uses_monthly_fallback():
    today = date(2026, 6, 13)
    # 35d * 1.5 = 52.5d threshold. 90 days ago -> stale even with unknown cadence.
    assert finance._subscription_is_stale("2026-03-15", None, today=today) is True


def test_annual_not_false_flagged_as_stale():
    today = date(2026, 6, 13)
    # Annual cadence (365d) charged 100 days ago is perfectly healthy.
    assert finance._subscription_is_stale("2026-03-05", "annual", today=today) is False


def test_missing_last_seen_is_not_stale():
    assert finance._subscription_is_stale(None, "monthly") is False


# --- list_subscriptions -----------------------------------------------------


def _today_iso(days_ago: int = 0) -> str:
    return (datetime.now().date() - timedelta(days=days_ago)).isoformat()


def _sub_bead(bead_id, *, merchant_key, name, amount, frequency, created_at,
              last_seen, is_price_hike=False):
    return {
        "id": bead_id,
        "created_at": created_at,
        "content": {
            "merchant_key": merchant_key,
            "name": name,
            "category": "streaming",
            "amount": amount,
            "frequency": frequency,
            "last_seen": last_seen,
            "first_seen": "2025-06-01",
            "occurrences": 12,
            "confidence": 0.9,
            "is_price_hike": is_price_hike,
            "price_change": {"detected": is_price_hike},
        },
    }


@pytest.mark.asyncio
async def test_list_subscriptions_dedups_to_latest_bead_per_merchant(monkeypatch):
    # Two beads for NETFLIX (the auditor appends weekly) — keep the newest.
    async def fake_query_beads(params):
        assert params["type"] == "subscription"
        return [
            _sub_bead("old", merchant_key="NETFLIX", name="Netflix",
                      amount=15.49, frequency="monthly",
                      created_at="2026-05-01T09:00:00", last_seen=_today_iso(40)),
            _sub_bead("new", merchant_key="NETFLIX", name="Netflix Standard",
                      amount=17.99, frequency="monthly",
                      created_at="2026-06-08T09:00:00", last_seen=_today_iso(5)),
        ]

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    result = await finance.list_subscriptions()

    assert len(result["subscriptions"]) == 1
    sub = result["subscriptions"][0]
    assert sub["bead_id"] == "new"
    assert sub["name"] == "Netflix Standard"
    assert sub["amount"] == 17.99


@pytest.mark.asyncio
async def test_list_subscriptions_totals_and_sorting(monkeypatch):
    async def fake_query_beads(params):
        return [
            _sub_bead("a", merchant_key="ADOBE", name="Adobe CC",
                      amount=59.99, frequency="monthly",
                      created_at="2026-06-01T00:00:00", last_seen=_today_iso(3)),
            _sub_bead("s", merchant_key="SPOTIFY", name="Spotify",
                      amount=11.99, frequency="monthly",
                      created_at="2026-06-01T00:00:00", last_seen=_today_iso(2),
                      is_price_hike=True),
            # Stale: annual charged ~2 years ago -> cancel candidate, excluded from total.
            _sub_bead("g", merchant_key="OLDGYM", name="Old Gym",
                      amount=240.0, frequency="annual",
                      created_at="2026-06-01T00:00:00", last_seen="2024-01-01"),
        ]

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    result = await finance.list_subscriptions()

    # Total excludes the stale gym; price hike + stale counters set.
    assert result["total_monthly"] == 71.98  # 59.99 + 11.99
    assert result["active_count"] == 2
    assert result["stale_count"] == 1
    assert result["price_hike_count"] == 1

    # Sorted by monthly_equivalent desc. Stale annual ($20/mo) sorts last.
    names = [s["name"] for s in result["subscriptions"]]
    assert names == ["Adobe CC", "Old Gym", "Spotify"]
    assert [s["status"] for s in result["subscriptions"]] == ["active", "stale", "price_hike"]


@pytest.mark.asyncio
async def test_list_subscriptions_can_exclude_stale_from_list(monkeypatch):
    async def fake_query_beads(params):
        return [
            _sub_bead("g", merchant_key="OLDGYM", name="Old Gym",
                      amount=240.0, frequency="annual",
                      created_at="2026-06-01T00:00:00", last_seen="2024-01-01"),
        ]

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    result = await finance.list_subscriptions(include_stale=False)

    # Stale subscription is filtered out of the list but still counted.
    assert result["subscriptions"] == []
    assert result["stale_count"] == 1
    assert result["active_count"] == 0
