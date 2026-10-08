"""Connection status must not claim what it cannot measure (LO-OBS-001), and
the verdict it drives must be learned per institution, not declared globally
(LO-OBS-002).

`status: ok` answers "does Plaid answer for this Item?". It never answered
"is data arriving?", and `last_synced` — the timestamp of the sync JOB — sat
next to it under a name that read like it did.

On 2026-08-02 that produced the Operator's report: all five institutions showed ok
with last_synced that morning, while amex had produced no transaction since
07-27 and citi/discover none since 07-28. LO-OBS-001/AC-3 was recorded FAIL
against that same observation: the freshness number existed and was rendered
as a caption, but the verdict was derived from the Plaid probe and nothing
else.

The two fixtures that matter for the verdict are the chase case (gone quiet)
and the amex case (legitimately slow — 18 transactions across 5 months). They
pull in opposite directions, and a threshold that satisfies one by breaking
the other has not fixed anything, it has moved the defect.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tools.connections import (
    _derive_status,
    _freshness_from_transactions,
    _freshness_slo_days,
    _freshness_verdict_message,
)

WINDOW = 60


def _tx(slug, posted, state="posted"):
    posted_date = posted.isoformat() if hasattr(posted, "isoformat") else posted
    return {"state": state, "content": {"institution": slug, "posted_date": posted_date}}


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _entry(**over):
    entry = {
        "token_present": True,
        "healthy": True,
        "error_code": None,
        "repairable": False,
        "data_freshness": None,
    }
    entry.update(over)
    return entry


def _fresh(days_ago, slo, known=True):
    return {
        "days_since_last_transaction": days_ago,
        "freshness_slo_days": slo,
        "known": known,
        "window_days": WINDOW,
    }


# --- freshness is an observation, and it distinguishes unknown from fresh ---

def test_reports_days_since_the_newest_transaction():
    out = _freshness_from_transactions(
        [_tx("chase", _days_ago(2)), _tx("chase", _days_ago(9))], ["chase"], window_days=WINDOW
    )
    assert out["chase"]["days_since_last_transaction"] == 2
    assert out["chase"]["known"] is True


def test_an_institution_with_no_data_is_known_false_not_zero():
    """The bug this whole change exists to prevent: an unknown rendering as a
    green. `known: False` is not `days_since == 0`."""
    out = _freshness_from_transactions([], ["amex"], window_days=WINDOW)
    assert out["amex"]["known"] is False
    assert out["amex"]["days_since_last_transaction"] is None
    assert out["amex"]["last_transaction_date"] is None


def test_removed_transactions_do_not_count_as_freshness():
    """A Plaid-retracted transaction is not evidence the feed is alive."""
    out = _freshness_from_transactions(
        [_tx("citi", _days_ago(1), state="removed")], ["citi"], window_days=WINDOW
    )
    assert out["citi"]["known"] is False


def test_freshness_is_per_institution():
    out = _freshness_from_transactions(
        [_tx("chase", _days_ago(1)), _tx("amex", _days_ago(30))],
        ["chase", "amex", "sofi"],
        window_days=WINDOW,
    )
    assert out["chase"]["days_since_last_transaction"] == 1
    assert out["amex"]["days_since_last_transaction"] == 30
    assert out["sofi"]["known"] is False


def test_freshness_states_no_verdict():
    """No 'stale' flag anywhere in the payload.

    amex produced 18 transactions in five months, so any global threshold
    either cries wolf on it forever or is too loose to catch a real outage.
    Learning a per-institution threshold is LO-OBS-002, below; until then
    this reports the number and lets the UI say 'data 30d ago'.
    """
    out = _freshness_from_transactions(
        [_tx("amex", _days_ago(40))], ["amex"], window_days=WINDOW
    )
    assert set(out["amex"]) == {
        "last_transaction_date",
        "days_since_last_transaction",
        "known",
        "window_days",
        "freshness_slo_days",
    }
    # A single transaction describes no cadence, so there is nothing to measure
    # against and the SLO is withheld rather than defaulted.
    assert out["amex"]["freshness_slo_days"] is None


def test_missing_token_and_unprobed_are_unchanged():
    assert _derive_status(_entry(token_present=False)) == "no_token"
    assert _derive_status(_entry(healthy=None)) == "unprobed"
    assert _derive_status(_entry(healthy=False, error_code="PROBE_TIMEOUT")) == "unknown"


# --- a degraded Item no longer reports ok, whether the evidence is the ------
# --- probe alone or the probe plus a freshness/SLO reading ------------------

@pytest.mark.parametrize(
    "entry_kwargs, expected_status",
    [
        pytest.param({}, "freshness_unknown", id="no_freshness_evidence_is_not_ok"),
        pytest.param(
            {"data_freshness": _fresh(days_ago=2, slo=14)},
            "ok",
            id="healthy_item_inside_its_slo_is_ok",
        ),
        pytest.param(
            {"error_code": "INSTITUTION_DOWN"},
            "degraded",
            id="non_reauth_plaid_error_is_degraded_not_ok",
        ),
        pytest.param(
            {"healthy": False, "error_code": "ITEM_LOGIN_REQUIRED", "repairable": True},
            "reauth_required",
            id="repairable_failure_still_routes_to_reauth",
        ),
        pytest.param(
            {"healthy": False, "error_code": "INVALID_ACCESS_TOKEN", "repairable": False},
            "relink_required",
            id="unrecoverable_failure_still_routes_to_relink",
        ),
        pytest.param(
            {"error_code": "RATE_LIMIT", "data_freshness": _fresh(days_ago=0, slo=14)},
            "degraded",
            id="degraded_still_beats_a_fresh_feed",
        ),
    ],
)
def test_derive_status_cases(entry_kwargs, expected_status):
    assert _derive_status(_entry(**entry_kwargs)) == expected_status


# --- the SLO is learned, not declared ---------------------------------------


def test_slo_is_derived_from_the_institutions_own_gaps():
    today = date.today()
    # A card used roughly weekly.
    dates = [today - timedelta(days=d) for d in (28, 21, 14, 7, 1)]
    slo = _freshness_slo_days([d.isoformat() for d in dates], window_days=WINDOW)

    assert slo is not None
    # Wider than its own worst ordinary gap, or the widest normal gap alerts by
    # construction roughly half the time.
    assert slo > 7


def test_too_little_history_yields_no_slo_rather_than_a_default():
    today = date.today()
    slo = _freshness_slo_days([(today - timedelta(days=1)).isoformat()], window_days=WINDOW)

    assert slo is None


def test_a_cadence_wider_than_the_window_yields_no_slo():
    """A feed silent longer than the lookback looks identical to one with no history."""
    today = date.today()
    dates = [today - timedelta(days=d) for d in (120, 80, 40)]
    slo = _freshness_slo_days([d.isoformat() for d in dates], window_days=WINDOW)

    assert slo is None


# --- the two fixtures the requirement is really about -----------------------


def test_the_chase_case_is_not_ok(monkeypatch):
    """Credentials valid, provider answering, no data past the SLO."""
    today = date.today()
    # A daily-ish feed, then silence.
    dates = [today - timedelta(days=d) for d in (40, 39, 38, 37, 36, 35)]
    freshness = _freshness_from_transactions(
        [_tx("chase", d) for d in dates], ["chase"], window_days=WINDOW
    )["chase"]

    status = _derive_status(_entry(data_freshness=freshness))

    assert status != "ok"
    assert status == "stale"


def test_the_amex_case_is_not_stale():
    """18 transactions across 5 months, newest a fortnight old.

    This is the test that proves the threshold is per-institution. A global
    three-day rule would report this card unhealthy almost every week.
    """
    today = date.today()
    dates = [today - timedelta(days=d) for d in (58, 51, 44, 37, 30, 23, 14)]
    freshness = _freshness_from_transactions(
        [_tx("amex", d) for d in dates], ["amex"], window_days=WINDOW
    )["amex"]

    status = _derive_status(_entry(data_freshness=freshness))

    assert status != "stale"


def test_a_healthy_verdict_names_the_date_it_was_based_on():
    today = date.today()
    dates = [today - timedelta(days=d) for d in (21, 14, 7, 1)]
    freshness = _freshness_from_transactions(
        [_tx("citi", d) for d in dates], ["citi"], window_days=WINDOW
    )["citi"]

    assert freshness["last_transaction_date"] == (today - timedelta(days=1)).isoformat()
    assert freshness["freshness_slo_days"] is not None


def test_unmeasurable_freshness_is_neither_ok_nor_stale():
    status = _derive_status(_entry(data_freshness=_fresh(days_ago=3, slo=None)))

    assert status == "freshness_unknown"
    assert status not in {"ok", "stale"}


def test_no_transactions_in_the_window_is_not_green():
    status = _derive_status(
        _entry(data_freshness=_fresh(days_ago=None, slo=14, known=False))
    )

    assert status != "ok"


# --- the existing vocabulary is untouched -----------------------------------


def test_actionable_states_still_win_over_staleness():
    """Stale AND needing re-auth must report the one a human can act on."""
    stale = _fresh(days_ago=99, slo=7)

    assert _derive_status(
        _entry(healthy=False, repairable=True, error_code="ITEM_LOGIN_REQUIRED", data_freshness=stale)
    ) == "reauth_required"
    assert _derive_status(
        _entry(healthy=False, repairable=False, error_code="INVALID_ACCESS_TOKEN", data_freshness=stale)
    ) == "relink_required"
    assert _derive_status(_entry(token_present=False, data_freshness=stale)) == "no_token"
    assert _derive_status(_entry(healthy=None, data_freshness=stale)) == "unprobed"
    assert _derive_status(
        _entry(healthy=False, error_code="PROBE_TIMEOUT", data_freshness=stale)
    ) == "unknown"


# --- the verdict is prose, not just fields ----------------------------------


def test_the_stale_verdict_names_the_institution_slo_and_age():
    today = date.today()
    dates = [today - timedelta(days=d) for d in (40, 39, 38, 37, 36, 35)]
    freshness = _freshness_from_transactions(
        [_tx("chase", d) for d in dates], ["chase"], window_days=WINDOW
    )["chase"]
    entry = _entry(data_freshness=freshness)
    status = _derive_status(entry)

    verdict = _freshness_verdict_message("chase", status, entry)

    assert status == "stale"
    assert "chase" in verdict
    assert str(freshness["freshness_slo_days"]) in verdict
    assert str(freshness["days_since_last_transaction"]) in verdict


def test_the_healthy_verdict_names_the_newest_transaction_date():
    today = date.today()
    dates = [today - timedelta(days=d) for d in (21, 14, 7, 1)]
    freshness = _freshness_from_transactions(
        [_tx("citi", d) for d in dates], ["citi"], window_days=WINDOW
    )["citi"]
    entry = _entry(data_freshness=freshness)
    status = _derive_status(entry)

    verdict = _freshness_verdict_message("citi", status, entry)

    assert status == "ok"
    assert freshness["last_transaction_date"] in verdict


def test_other_statuses_get_no_invented_verdict_prose():
    entry = _entry(data_freshness=_fresh(days_ago=3, slo=None))
    status = _derive_status(entry)

    assert status == "freshness_unknown"
    assert _freshness_verdict_message("amex", status, entry) is None


def test_health_is_never_aggregated_across_institutions():
    today = date.today()
    txs = [_tx("chase", today - timedelta(days=1)), _tx("amex", today - timedelta(days=40))]
    out = _freshness_from_transactions(txs, ["chase", "amex"], window_days=WINDOW)

    assert set(out) == {"chase", "amex"}
    assert out["chase"]["last_transaction_date"] != out["amex"]["last_transaction_date"]
