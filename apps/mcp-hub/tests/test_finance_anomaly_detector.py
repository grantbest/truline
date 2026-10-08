"""Unit tests for the SDD Phase 2 anomaly detectors (pure helpers only)."""

from workflows.finance_anomaly_detector import (
    detect_anomalies,
    detect_duplicates,
    detect_price_anomalies,
)


def _tx(tx_id, account_id, amount, posted_date, vendor, *, state="posted", is_transfer=False):
    return {
        "id": tx_id,
        "state": state,
        "content": {
            "account_id": account_id,
            "amount": amount,
            "posted_date": posted_date,
            "normalized_merchant": vendor,
            "is_transfer": is_transfer,
        },
    }


# --- duplicate detection --------------------------------------------------

def test_duplicate_charge_flagged():
    recent = [
        _tx("a", "chk", 45.00, "2026-06-18", "Target"),
        _tx("b", "chk", 45.00, "2026-06-18", "Target"),
    ]
    anomalies = detect_duplicates(recent)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a["reason"] == "duplicate_charge"
    assert sorted(a["transaction_ids"]) == ["a", "b"]
    assert a["amount"] == 45.00
    assert a["vendor"] == "Target"
    assert a["count"] == 2


def test_distinct_amounts_not_duplicate():
    recent = [
        _tx("a", "chk", 45.00, "2026-06-18", "Target"),
        _tx("b", "chk", 46.00, "2026-06-18", "Target"),
    ]
    assert detect_duplicates(recent) == []


def test_different_days_not_duplicate():
    recent = [
        _tx("a", "chk", 45.00, "2026-06-18", "Target"),
        _tx("b", "chk", 45.00, "2026-06-17", "Target"),
    ]
    assert detect_duplicates(recent) == []


def test_known_multi_charge_vendor_excluded():
    recent = [
        _tx("a", "chk", 3.50, "2026-06-18", "Starbucks"),
        _tx("b", "chk", 3.50, "2026-06-18", "Starbucks"),
    ]
    assert detect_duplicates(recent) == []


def test_removed_and_transfer_legs_ignored():
    recent = [
        _tx("a", "chk", 45.00, "2026-06-18", "Target", state="removed"),
        _tx("b", "chk", 45.00, "2026-06-18", "Target", is_transfer=True),
        _tx("c", "chk", 45.00, "2026-06-18", "Target"),
    ]
    # Only one usable charge remains -> no duplicate group.
    assert detect_duplicates(recent) == []


def test_inflows_ignored():
    recent = [
        _tx("a", "chk", -45.00, "2026-06-18", "Target"),
        _tx("b", "chk", -45.00, "2026-06-18", "Target"),
    ]
    assert detect_duplicates(recent) == []


# --- price-hike detection -------------------------------------------------

def test_price_hike_above_threshold_flagged():
    recent = [_tx("new", "chk", 24.00, "2026-06-18", "Netflix")]
    history = [
        _tx("h1", "chk", 15.00, "2026-01-18", "Netflix"),
        _tx("h2", "chk", 15.00, "2026-02-18", "Netflix"),
        _tx("h3", "chk", 15.00, "2026-03-18", "Netflix"),
        recent[0],  # recent row also lives in the history window; must be excluded from baseline
    ]
    anomalies = detect_price_anomalies(recent, history)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a["reason"] == "price_hike"
    assert a["baseline_amount"] == 15.00
    assert a["amount"] == 24.00
    assert a["change_pct"] == 60.0
    assert a["transaction_ids"] == ["new"]


def test_small_increase_below_threshold_not_flagged():
    recent = [_tx("new", "chk", 16.00, "2026-06-18", "Netflix")]
    history = [
        _tx("h1", "chk", 15.00, "2026-01-18", "Netflix"),
        _tx("h2", "chk", 15.00, "2026-02-18", "Netflix"),
        _tx("h3", "chk", 15.00, "2026-03-18", "Netflix"),
        recent[0],
    ]
    # ~6.7% increase < 15% threshold.
    assert detect_price_anomalies(recent, history) == []


def test_insufficient_history_not_flagged():
    recent = [_tx("new", "chk", 24.00, "2026-06-18", "Netflix")]
    history = [
        _tx("h1", "chk", 15.00, "2026-02-18", "Netflix"),
        _tx("h2", "chk", 15.00, "2026-03-18", "Netflix"),
        recent[0],
    ]
    # Only 2 priors < MIN_HISTORY_FOR_BASELINE (3).
    assert detect_price_anomalies(recent, history) == []


def test_combined_detect_anomalies():
    recent = [
        _tx("a", "chk", 45.00, "2026-06-18", "Target"),
        _tx("b", "chk", 45.00, "2026-06-18", "Target"),
        _tx("new", "chk", 24.00, "2026-06-18", "Netflix"),
    ]
    history = [
        _tx("h1", "chk", 15.00, "2026-01-18", "Netflix"),
        _tx("h2", "chk", 15.00, "2026-02-18", "Netflix"),
        _tx("h3", "chk", 15.00, "2026-03-18", "Netflix"),
        *recent,
    ]
    anomalies = detect_anomalies(recent, history)
    reasons = sorted(a["reason"] for a in anomalies)
    assert reasons == ["duplicate_charge", "price_hike"]
