"""Unit tests for the SDD Phase 2 trial-sentinel deadline logic (pure)."""

from datetime import date

from workflows.subscription_sentinel import evaluate_trial_deadline, evaluate_trials


def _sub(sub_id, name, amount, trial_end_date, *, frequency="monthly"):
    return {
        "id": sub_id,
        "content": {
            "name": name,
            "amount": amount,
            "frequency": frequency,
            "trial_end_date": trial_end_date,
        },
    }


TODAY = date(2026, 6, 19)


def test_trial_exactly_three_days_out_flagged():
    sub = _sub("s1", "Hulu", 14.99, "2026-06-22")  # 3 days from TODAY
    alert = evaluate_trial_deadline(sub, today=TODAY)
    assert alert is not None
    assert alert["kind"] == "trial_ending"
    assert alert["vendor"] == "Hulu"
    assert alert["amount_at_risk"] == 14.99
    assert alert["end_date"] == "2026-06-22"
    assert alert["days_until"] == 3
    assert alert["fingerprint"] == "trial:s1"


def test_trial_two_days_out_not_flagged():
    sub = _sub("s1", "Hulu", 14.99, "2026-06-21")  # 2 days
    assert evaluate_trial_deadline(sub, today=TODAY) is None


def test_trial_four_days_out_not_flagged():
    sub = _sub("s1", "Hulu", 14.99, "2026-06-23")  # 4 days
    assert evaluate_trial_deadline(sub, today=TODAY) is None


def test_no_trial_end_date_skipped():
    sub = _sub("s1", "Netflix", 15.49, None)
    assert evaluate_trial_deadline(sub, today=TODAY) is None


def test_unparseable_trial_date_skipped():
    sub = _sub("s1", "Netflix", 15.49, "not-a-date")
    assert evaluate_trial_deadline(sub, today=TODAY) is None


def test_evaluate_trials_filters_to_matching():
    subs = [
        _sub("a", "Hulu", 14.99, "2026-06-22"),     # 3 days -> flagged
        _sub("b", "Disney+", 7.99, "2026-06-25"),   # 6 days -> not
        _sub("c", "Max", 9.99, None),               # no date -> not
    ]
    alerts = evaluate_trials(subs, today=TODAY)
    assert len(alerts) == 1
    assert alerts[0]["subscription_id"] == "a"
