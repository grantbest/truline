from workflows.transfer_pairing import _match_transfer_pairs


def _tx(tx_id, amount, account_id, posted_date):
    return {
        "id": tx_id,
        "content": {
            "amount": amount,
            "account_id": account_id,
            "posted_date": posted_date,
            "is_transfer": True,
        },
    }


def test_pairs_outflow_with_matching_inflow_across_accounts():
    # $25 leaves checking (+25 outflow), arrives in savings (-25 inflow).
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-10"),
        _tx("in1", -25.0, "savings", "2026-04-10"),
    ]
    pairs = _match_transfer_pairs(transfers)
    assert len(pairs) == 1
    out, inf = pairs[0]
    assert out["id"] == "out1" and inf["id"] == "in1"


def test_no_pair_when_same_account():
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-10"),
        _tx("in1", -25.0, "checking", "2026-04-10"),
    ]
    assert _match_transfer_pairs(transfers) == []


def test_no_pair_when_amounts_differ_beyond_tolerance():
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-10"),
        _tx("in1", -30.0, "savings", "2026-04-10"),
    ]
    assert _match_transfer_pairs(transfers) == []


def test_no_pair_when_dates_too_far_apart():
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-01"),
        _tx("in1", -25.0, "savings", "2026-04-10"),  # 9 days > window
    ]
    assert _match_transfer_pairs(transfers) == []


def test_each_leg_claimed_once():
    # Two identical $25 outflows, one inflow -> only one pair.
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-10"),
        _tx("out2", 25.0, "checking", "2026-04-10"),
        _tx("in1", -25.0, "savings", "2026-04-10"),
    ]
    pairs = _match_transfer_pairs(transfers)
    assert len(pairs) == 1


def test_two_independent_pairs():
    transfers = [
        _tx("out1", 25.0, "checking", "2026-04-10"),
        _tx("in1", -25.0, "savings", "2026-04-10"),
        _tx("out2", 100.0, "checking", "2026-04-12"),
        _tx("in2", -100.0, "brokerage", "2026-04-13"),
    ]
    pairs = _match_transfer_pairs(transfers)
    assert len(pairs) == 2
    paired_ids = {p[0]["id"] for p in pairs} | {p[1]["id"] for p in pairs}
    assert paired_ids == {"out1", "in1", "out2", "in2"}
