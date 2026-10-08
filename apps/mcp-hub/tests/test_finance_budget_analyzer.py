"""Unit tests for the SDD Phase 3 budget auto-adjuster (pure logic)."""

from datetime import date

from workflows.finance_budget_analyzer import compute_recommendations

TODAY = date(2026, 6, 19)


def _budget(category, amount, *, period="monthly", bid=None):
    return {
        "id": bid or f"budget-{category}",
        "content": {"category": category, "amount": amount, "period": period},
    }


def _txn(category, amount, day, *, removed=False, transfer=False):
    return {
        "state": "removed" if removed else "active",
        "content": {
            "our_category": category,
            "amount": amount,
            "posted_date": day.isoformat(),
            "is_transfer": transfer,
        },
    }


def _spread(category, monthly_amount, *, months=3, per_month=4):
    """Build `per_month` transactions/month summing to `monthly_amount` each,
    across the last `months` months ending before TODAY."""
    out = []
    each = monthly_amount / per_month
    for m in range(months):
        # days 5..(5+per_month) of months back-dated within the 90-day window
        base = date(2026, 6 - m, 10)
        for i in range(per_month):
            out.append(_txn(category, each, date(base.year, base.month, 5 + i)))
    return out


def test_over_budget_category_recommends_increase():
    # ~$950/mo actual vs $800 cap -> +18.75% deviation -> increase.
    txns = _spread("groceries", 950)
    out = compute_recommendations([_budget("groceries", 800)], txns, [], today=TODAY)
    assert len(out) == 1
    rec = out[0]
    assert rec["category"] == "groceries"
    assert rec["direction"] == "increase"
    assert rec["current_cap"] == 800
    assert rec["suggested_cap"] == 950  # rounded to nearest $10
    assert rec["fingerprint"] == "budget_rec:groceries"
    assert rec["sample_size"] == 12


def test_under_budget_category_recommends_decrease():
    # ~$300/mo actual vs $800 cap -> -62% -> decrease.
    txns = _spread("dining", 300)
    out = compute_recommendations([_budget("dining", 800)], txns, [], today=TODAY)
    assert len(out) == 1
    assert out[0]["direction"] == "decrease"
    assert out[0]["suggested_cap"] == 300


def test_within_threshold_no_recommendation():
    # ~$850/mo vs $800 cap -> +6.25% < 15% -> nothing.
    txns = _spread("groceries", 850)
    assert compute_recommendations([_budget("groceries", 800)], txns, [], today=TODAY) == []


def test_thin_history_skipped():
    # Way over budget but only 2 charges total -> below MIN_SAMPLE_TXNS.
    txns = [
        _txn("auto", 600, date(2026, 6, 10)),
        _txn("auto", 600, date(2026, 5, 10)),
    ]
    assert compute_recommendations([_budget("auto", 300)], txns, [], today=TODAY) == []


def test_non_monthly_budget_skipped():
    txns = _spread("utilities", 950)
    out = compute_recommendations(
        [_budget("utilities", 800, period="quarterly")], txns, [], today=TODAY
    )
    assert out == []


def test_transfers_and_removed_excluded():
    txns = _spread("groceries", 950)
    # Pile on transfers + removed beads that would blow the average if counted.
    txns += [_txn("groceries", 5000, date(2026, 6, 12), transfer=True)]
    txns += [_txn("groceries", 5000, date(2026, 6, 12), removed=True)]
    out = compute_recommendations([_budget("groceries", 800)], txns, [], today=TODAY)
    assert len(out) == 1
    assert out[0]["suggested_cap"] == 950


def test_expenses_included_in_average():
    expenses = [
        {"id": f"e{i}", "content": {"category": "kids", "amount": 250, "date": d.isoformat()}}
        for i, d in enumerate(
            [date(2026, 6, 5), date(2026, 6, 6), date(2026, 5, 5), date(2026, 4, 20)]
        )
    ]
    out = compute_recommendations([_budget("kids", 200)], [], expenses, today=TODAY)
    assert len(out) == 1
    assert out[0]["category"] == "kids"
    assert out[0]["direction"] == "increase"


def test_old_spend_outside_window_ignored():
    # All spend is >90 days old -> nothing in the window -> no rec.
    txns = [_txn("groceries", 950, date(2026, 1, 10)) for _ in range(12)]
    assert compute_recommendations([_budget("groceries", 800)], txns, [], today=TODAY) == []
