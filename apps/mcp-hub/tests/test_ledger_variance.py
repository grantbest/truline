"""How wrong is the ledger, in dollars, right now.

LO-REC-002 and LO-REC-004. The nightly delta answers what moved last night; this
answers what has never been accounted for. The registry's note on the gap: "The
current design deliberately traded the true anchor for a delta approximation;
this requirement is the replacement for what that trade gave up."

The property that matters most is the one in
``test_the_figure_survives_five_clean_nights``. A measure that quietly returns to
zero because the baseline moved is the defect TA-2 removed, arriving through a
different door.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from workflows.finance_reconciliation import compute_ledger_variance  # noqa: E402


def _disc(account_id, *, first_drift, current_drift=None, state="pending",
          first_at="2026-07-11T03:45:00", name="Checking"):
    return {
        "id": f"disc-{account_id}-{first_at}",
        "state": state,
        "content": {
            "account_id": account_id,
            "account_name": name,
            "first_observed_drift": first_drift,
            "first_observed_at": first_at,
            "drift": current_drift if current_drift is not None else first_drift,
        },
    }


def _row(report, account_id):
    return next(r for r in report["accounts"] if r["account_id"] == account_id)


# --- the number exists and is anchored --------------------------------------


def test_one_number_per_account_and_a_total():
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0),
        _disc("sav", first_drift=125.50, name="Savings"),
    ])

    assert _row(report, "chk")["unexplained_variance"] == 400.0
    assert _row(report, "sav")["unexplained_variance"] == 125.50
    assert report["total"]["unexplained_variance"] == 525.50


def test_every_figure_carries_the_date_it_counts_from():
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0, first_at="2026-07-11T03:45:00"),
        _disc("sav", first_drift=-10.0, first_at="2026-06-01T03:45:00"),
    ])

    assert _row(report, "chk")["anchor_date"] == "2026-07-11T03:45:00"
    # The total counts from the earliest anchor any account has.
    assert report["total"]["anchor_date"] == "2026-06-01T03:45:00"


# --- the defect this measure must not reproduce -----------------------------


def test_the_figure_uses_what_was_first_observed_not_tonights_delta():
    """After a re-baseline, tonight's drift is ~0 while the money is still gone.

    Summing the current values would report a healthy ledger with a hole in it.
    """
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0, current_drift=-0.02),
    ])

    assert _row(report, "chk")["unexplained_variance"] == 400.0


def test_the_figure_survives_five_clean_nights():
    """Clean runs do not move it. Only an explanation does.

    Since TA-2, a clean night holds the discrepancy open and records the
    observation. The dollars must be unmoved by that.
    """
    open_disc = _disc("chk", first_drift=-400.0)
    open_disc["content"]["clean_runs_since_first_observed"] = 5
    open_disc["content"]["drift"] = 0.0

    report = compute_ledger_variance([open_disc])

    assert _row(report, "chk")["unexplained_variance"] == 400.0
    assert report["total"]["unexplained_variance"] == 400.0


def test_resolving_reduces_it_by_exactly_that_amount_and_nothing_else():
    before = compute_ledger_variance([
        _disc("chk", first_drift=-400.0),
        _disc("sav", first_drift=-60.0, name="Savings"),
    ])
    after = compute_ledger_variance([
        _disc("chk", first_drift=-400.0, state="resolved"),
        _disc("sav", first_drift=-60.0, name="Savings"),
    ])

    assert before["total"]["unexplained_variance"] == 460.0
    assert after["total"]["unexplained_variance"] == 60.0
    assert _row(after, "sav")["unexplained_variance"] == 60.0


def test_a_resolved_record_still_anchors_the_account():
    """Resolving stops the dollars counting; it does not restart the clock."""
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0, state="resolved", first_at="2026-06-01T03:45:00"),
        _disc("chk", first_drift=-25.0, first_at="2026-07-20T03:45:00"),
    ])

    row = _row(report, "chk")
    assert row["unexplained_variance"] == 25.0
    assert row["anchor_date"] == "2026-06-01T03:45:00"


# --- not-yet-measuring is not the same as clean -----------------------------


def test_an_account_with_no_anchor_reports_none_not_zero():
    report = compute_ledger_variance([], accounts=[{"id": "new", "name": "New Account"}])

    row = _row(report, "new")
    assert row["unexplained_variance"] is None
    assert row["anchor_date"] is None


def test_honest_null_survives_when_other_accounts_are_measured():
    report = compute_ledger_variance(
        [_disc("chk", first_drift=-400.0)],
        accounts=[{"id": "chk", "name": "Checking"}, {"id": "new", "name": "New Account"}],
    )

    row = _row(report, "new")
    assert row["unexplained_variance"] is None
    assert row["anchor_date"] is None
    assert row["open_discrepancies"] == 0


def test_the_total_states_what_it_could_not_measure():
    report = compute_ledger_variance(
        [_disc("chk", first_drift=-400.0)],
        accounts=[{"id": "chk", "name": "Checking"}, {"id": "new", "name": "New Account"}],
    )

    assert report["total"]["accounts_measured"] == 1
    assert report["total"]["accounts_not_measured"] == 1
    # The number is real, and the reader can see it does not cover everything.
    assert report["total"]["unexplained_variance"] == 400.0


def test_an_empty_estate_reports_no_anchor_rather_than_a_confident_zero():
    report = compute_ledger_variance([], accounts=[])

    assert report["total"]["anchor_date"] is None
    assert report["total"]["accounts_measured"] == 0


# --- sign and shape ---------------------------------------------------------


def test_direction_does_not_cancel_out():
    """Two gaps in opposite directions are two unexplained gaps, not zero."""
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0),
        _disc("sav", first_drift=400.0, name="Savings"),
    ])

    assert report["total"]["unexplained_variance"] == 800.0


def test_open_discrepancies_are_counted_alongside_the_dollars():
    report = compute_ledger_variance([
        _disc("chk", first_drift=-400.0, first_at="2026-07-11T03:45:00"),
        _disc("chk", first_drift=-50.0, first_at="2026-07-20T03:45:00"),
        _disc("sav", first_drift=-1.0, state="resolved", name="Savings"),
    ])

    assert _row(report, "chk")["open_discrepancies"] == 2
    assert _row(report, "sav")["open_discrepancies"] == 0
    assert report["total"]["open_discrepancies"] == 2


def test_a_record_without_a_first_observation_falls_back_to_its_drift():
    """Discrepancies written before TA-2 carry no first_observed_drift.

    They must still count. Dropping them would make the figure understate the
    gap for exactly the period the platform was worst at tracking it.
    """
    legacy = {
        "id": "legacy",
        "state": "pending",
        "content": {"account_id": "chk", "drift": -75.0, "window_end": "2026-05-01T03:45:00"},
    }

    report = compute_ledger_variance([legacy])

    assert _row(report, "chk")["unexplained_variance"] == 75.0
    assert _row(report, "chk")["anchor_date"] == "2026-05-01T03:45:00"
