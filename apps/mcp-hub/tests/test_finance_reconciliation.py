import pytest

from workflows import finance_reconciliation as reconciliation
from workflows.finance_reconciliation import compute_drift


def _account(acct_id, name, current_balance, account_type="depository"):
    return {
        "id": acct_id,
        "name": name,
        "type": account_type,
        "current_balance": current_balance,
    }


def _snapshot(account_id, balance, as_of):
    return {"content": {"account_id": account_id, "balance": balance, "as_of": as_of}}


def _tx(account_id, amount, posted_date, state="posted"):
    return {
        "state": state,
        "content": {"account_id": account_id, "amount": amount, "posted_date": posted_date},
    }


def test_clean_account_no_drift():
    # Prev balance 1000; one $50 outflow after the snapshot -> expected 950,
    # and the account really reports 950. No drift.
    accounts = [_account("chk", "Checking", 950.0)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("chk", 50.0, "2026-06-18")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_drift_above_tolerance_flagged():
    # Expected 950 but the bank reports 900 -> a $50 inflow/outflow went
    # missing from Substrate. Drift = 900 - 950 = -50.
    accounts = [_account("chk", "Checking", 900.0)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("chk", 50.0, "2026-06-18")]
    drift = compute_drift(accounts, snapshots, txns)
    assert len(drift) == 1
    d = drift[0]
    assert d["account_id"] == "chk"
    assert d["expected_balance"] == 950.0
    assert d["actual_balance"] == 900.0
    assert d["drift"] == -50.0


def test_sub_tolerance_noise_ignored():
    # 50 cents of timing noise is below the $1 tolerance -> not flagged.
    accounts = [_account("chk", "Checking", 949.50)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("chk", 50.0, "2026-06-18")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_first_run_no_snapshot_is_seeded_only():
    # No prior snapshot for the account -> never flagged on the first run.
    accounts = [_account("chk", "Checking", 900.0)]
    assert compute_drift(accounts, [], []) == []


def test_transfers_are_included_in_balance_math():
    # A transfer leg (is_transfer) still moves the real balance, so it must
    # count. Outflow of 200 after the snapshot -> expected 800; bank says 800.
    accounts = [_account("chk", "Checking", 800.0)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [{"state": "posted", "content": {
        "account_id": "chk", "amount": 200.0, "posted_date": "2026-06-18",
        "is_transfer": True,
    }}]
    assert compute_drift(accounts, snapshots, txns) == []


def test_removed_transactions_excluded():
    # A removed (Plaid-retracted) transaction must not count toward the window.
    # Without it, expected = prev (1000) and the bank reports 1000 -> clean.
    accounts = [_account("chk", "Checking", 1000.0)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("chk", 50.0, "2026-06-18", state="removed")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_same_day_transaction_assumed_in_snapshot():
    # A transaction posted ON the snapshot date is assumed already reflected in
    # that snapshot's balance, so it is not re-counted (boundary convention).
    accounts = [_account("chk", "Checking", 1000.0)]
    snapshots = [_snapshot("chk", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("chk", 50.0, "2026-06-17")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_latest_snapshot_wins():
    # Two snapshots; the most recent (as_of) is the baseline. Latest balance
    # 950, one $50 outflow after it -> expected 900; bank says 900 -> clean.
    accounts = [_account("chk", "Checking", 900.0)]
    snapshots = [
        _snapshot("chk", 1000.0, "2026-06-16T03:45:00"),
        _snapshot("chk", 950.0, "2026-06-18T03:45:00"),
    ]
    txns = [_tx("chk", 50.0, "2026-06-19")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_liability_positive_amount_increases_expected_balance():
    # Credit balances are amount-owed. A $50 card purchase after the snapshot
    # increases the expected current balance from 1000 to 1050.
    accounts = [_account("card", "Credit Card", 1050.0, account_type="credit")]
    snapshots = [_snapshot("card", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("card", 50.0, "2026-06-18")]
    assert compute_drift(accounts, snapshots, txns) == []


def test_liability_payment_reduces_expected_balance():
    # Negative Plaid amount on a credit account is a payment/inflow, reducing
    # the amount owed.
    accounts = [_account("card", "Credit Card", 950.0, account_type="credit")]
    snapshots = [_snapshot("card", 1000.0, "2026-06-17T03:45:00")]
    txns = [_tx("card", -50.0, "2026-06-18")]
    assert compute_drift(accounts, snapshots, txns) == []


@pytest.mark.asyncio
async def test_emit_discrepancies_holds_open_stale_pending_when_clean(monkeypatch):
    """Renamed and inverted when LO-REC-001/AC-3 was fixed.

    This test used to assert that a clean run RESOLVES an open discrepancy, and
    that assertion was the defect written down. A fresh balance snapshot is
    written every night, so the next run reconciles against a baseline that
    already contains the unexplained gap — the account comes up clean because the
    baseline moved, not because anyone accounted for the money. Closing it there
    turned an open question into a false answer.

    The run is still recorded. It is an observation that no NEW drift appeared,
    which is not the same claim as the old one being explained.
    """
    calls = []

    async def fake_query_beads(params):
        assert params == {"type": "audit_discrepancy", "state": "pending", "limit": 1000}
        return [
            {
                "id": "disc-card",
                "content": {"account_id": "card", "drift": 100.0},
            }
        ]

    async def fake_patch_bead(bead_id, *, state=None, content=None, created_by="x"):
        calls.append((bead_id, state, content, created_by))
        return {"id": bead_id, "state": state}

    monkeypatch.setattr(reconciliation, "query_beads", fake_query_beads)
    monkeypatch.setattr(reconciliation, "patch_bead", fake_patch_bead)

    written = await reconciliation.emit_discrepancies_activity([])

    assert written == 1
    bead_id, state, content, created_by = calls[0]
    assert bead_id == "disc-card"
    assert state is None, "a clean run must not change the discrepancy's state"
    assert created_by == "ledger-reconciliation/observed_clean_still_open"
    assert content["clean_runs_since_first_observed"] == 1
    assert content["drift"] == 100.0, "the recorded gap is untouched by a clean night"


@pytest.mark.asyncio
async def test_emit_discrepancies_refreshes_current_and_holds_other_stale_open(monkeypatch):
    """Second half of the same inversion — see the test above for why."""
    patches = []
    created = []

    async def fake_query_beads(params):
        assert params == {"type": "audit_discrepancy", "state": "pending", "limit": 1000}
        return [
            {"id": "disc-card", "content": {"account_id": "card"}},
            {"id": "disc-old", "content": {"account_id": "old-clean"}},
        ]

    async def fake_patch_bead(bead_id, *, state=None, content=None, created_by="x"):
        patches.append((bead_id, state, content, created_by))
        return {"id": bead_id, "state": state, "content": content}

    async def fake_create_bead(bead_type, content, state, created_by):
        created.append((bead_type, content, state, created_by))
        return {"id": "created", "content": content, "state": state}

    monkeypatch.setattr(reconciliation, "query_beads", fake_query_beads)
    monkeypatch.setattr(reconciliation, "patch_bead", fake_patch_bead)
    monkeypatch.setattr(reconciliation, "create_bead", fake_create_bead)

    written = await reconciliation.emit_discrepancies_activity(
        [
            {
                "account_id": "card",
                "account_name": "Credit Card",
                "expected_balance": 10.0,
                "actual_balance": 15.0,
                "drift": 5.0,
                "prev_snapshot_at": "2026-07-11T03:45:00",
            }
        ]
    )

    assert written == 2
    assert created == []
    assert patches[0][0] == "disc-card"
    assert patches[0][1] is None
    assert patches[0][2]["account_id"] == "card"
    assert patches[0][3] == "ledger-reconciliation/refresh_discrepancy"
    assert patches[1][0] == "disc-old"
    assert patches[1][1] is None, "an account merely reconciling does not close its discrepancy"
    assert patches[1][3] == "ledger-reconciliation/observed_clean_still_open"
