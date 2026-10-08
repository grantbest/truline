"""Unit tests for the bill guardian view (tools.finance.get_bill_ledger).

Covers classification (overdue / due-soon / upcoming / paid), proof-of-payment
attachment via transaction.parent_id, group totals, and sorting.
"""

from datetime import datetime, timedelta

import pytest

from tools import finance


def _iso(days_from_now: int) -> str:
    return (datetime.now().date() + timedelta(days=days_from_now)).isoformat()


def _bill(bead_id, *, vendor, amount, due_in_days=None, state="pending"):
    content = {"vendor": vendor, "amount": amount, "category": "utilities", "source": "manual"}
    if due_in_days is not None:
        content["due_date"] = _iso(due_in_days)
    return {"id": bead_id, "state": state, "content": content}


@pytest.mark.asyncio
async def test_classifies_overdue_due_soon_and_upcoming(monkeypatch):
    async def fake_query_beads(params):
        if params["type"] == "bill":
            return [
                _bill("b1", vendor="ComEd", amount=120.0, due_in_days=-3),   # overdue
                _bill("b2", vendor="Comcast", amount=80.0, due_in_days=5),   # due soon
                _bill("b3", vendor="Mortgage", amount=2200.0, due_in_days=40),  # upcoming
            ]
        if params["type"] == "transaction":
            return []
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    led = await finance.get_bill_ledger(days_ahead=14)

    assert led["overdue_count"] == 1
    assert led["due_soon_count"] == 1
    assert led["overdue_total"] == 120.0
    assert led["due_soon_total"] == 80.0
    assert led["upcoming_total"] == 2200.0
    assert [e["vendor"] for e in led["groups"]["overdue"]] == ["ComEd"]
    assert [e["vendor"] for e in led["groups"]["due_soon"]] == ["Comcast"]
    assert [e["vendor"] for e in led["groups"]["upcoming"]] == ["Mortgage"]


@pytest.mark.asyncio
async def test_paid_bill_gets_proof_of_payment(monkeypatch):
    async def fake_query_beads(params):
        if params["type"] == "bill":
            return [_bill("bill-1", vendor="ComEd", amount=120.0, due_in_days=-10, state="paid")]
        if params["type"] == "transaction":
            # reconcile stamped parent_id -> the bill id.
            return [
                {
                    "id": "txn-9",
                    "state": "reconciled",
                    "parent_id": "bill-1",
                    "content": {
                        "amount": 120.0,
                        "posted_date": "2026-06-03",
                        "merchant_name": "COMED UTILITY",
                    },
                }
            ]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    led = await finance.get_bill_ledger()

    paid = led["groups"]["paid"]
    assert len(paid) == 1
    assert paid[0]["proof"] == {
        "transaction_id": "txn-9",
        "amount": 120.0,
        "posted_date": "2026-06-03",
        "merchant": "COMED UTILITY",
    }


@pytest.mark.asyncio
async def test_paid_bill_without_matching_txn_has_null_proof(monkeypatch):
    async def fake_query_beads(params):
        if params["type"] == "bill":
            return [_bill("bill-x", vendor="Manual Co", amount=50.0, due_in_days=-2, state="paid")]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    led = await finance.get_bill_ledger()
    assert led["groups"]["paid"][0]["proof"] is None


@pytest.mark.asyncio
async def test_archived_bills_are_omitted_and_overdue_sorted(monkeypatch):
    async def fake_query_beads(params):
        if params["type"] == "bill":
            return [
                _bill("a", vendor="Archived", amount=1.0, due_in_days=-1, state="archived"),
                _bill("o2", vendor="Later", amount=10.0, due_in_days=-2),
                _bill("o1", vendor="Earlier", amount=10.0, due_in_days=-9),
            ]
        return []

    monkeypatch.setattr(finance, "query_beads", fake_query_beads)
    led = await finance.get_bill_ledger()

    # Archived dropped; overdue sorted by due_date ascending (Earlier first).
    assert led["overdue_count"] == 2
    assert [e["vendor"] for e in led["groups"]["overdue"]] == ["Earlier", "Later"]
