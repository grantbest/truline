"""Tests for bill_guardian's audit: the one finance tool that mutates state.

``get_bill_guardian_summary`` fetches pending bills and, for any missed one,
PATCHes it to ``overdue`` in Substrate (src/tools/bill_guardian.py:57-63).
Every other finance tool only reads or appends; this one writes a state
transition, so it is the one that needs a test of the write itself, not just
the read it drives.
"""

from datetime import datetime, timedelta

import pytest

from src.tools import bill_guardian


def _iso(days_from_now: int) -> str:
    return (datetime.now().date() + timedelta(days=days_from_now)).isoformat()


def _bill(bead_id, *, due_in_days, state="pending"):
    return {
        "id": bead_id,
        "state": state,
        "content": {"vendor": "Vendor", "amount": 10.0, "due_date": _iso(due_in_days)},
    }


class _FakeSubstrate:
    """An in-memory bead store, so a second audit sees what the first wrote."""

    def __init__(self, bills):
        self.bills = {b["id"]: dict(b) for b in bills}
        self.patch_calls = []

    async def query_beads(self, params):
        if params.get("type") != "bill":
            return []
        return [b for b in self.bills.values() if b["state"] == params.get("state")]

    async def patch_bead_state(self, bead_id, state, created_by):
        self.patch_calls.append((bead_id, state, created_by))
        self.bills[bead_id]["state"] = state
        return {"id": bead_id, "state": state}


@pytest.mark.asyncio
async def test_a_missed_bill_is_flipped_to_overdue(monkeypatch):
    store = _FakeSubstrate([_bill("b1", due_in_days=-3)])
    monkeypatch.setattr(bill_guardian, "query_beads", store.query_beads)
    monkeypatch.setattr(bill_guardian, "patch_bead_state", store.patch_bead_state)

    await bill_guardian.get_bill_guardian_summary()

    assert store.patch_calls == [("b1", "overdue", "bill-guardian/audit")]


@pytest.mark.asyncio
async def test_a_bill_due_in_the_future_is_not_written(monkeypatch):
    store = _FakeSubstrate([_bill("b2", due_in_days=5)])
    monkeypatch.setattr(bill_guardian, "query_beads", store.query_beads)
    monkeypatch.setattr(bill_guardian, "patch_bead_state", store.patch_bead_state)

    await bill_guardian.get_bill_guardian_summary()

    assert store.patch_calls == []


@pytest.mark.asyncio
async def test_a_bill_already_overdue_is_not_written_again(monkeypatch):
    store = _FakeSubstrate([_bill("b1", due_in_days=-3)])
    monkeypatch.setattr(bill_guardian, "query_beads", store.query_beads)
    monkeypatch.setattr(bill_guardian, "patch_bead_state", store.patch_bead_state)

    await bill_guardian.get_bill_guardian_summary()
    await bill_guardian.get_bill_guardian_summary()

    assert store.patch_calls == [("b1", "overdue", "bill-guardian/audit")]


@pytest.mark.asyncio
async def test_a_patch_failure_propagates(monkeypatch):
    """Pins today's behaviour: :57-63 has no try/except around the PATCH, so
    a write failure propagates out of the audit. A swallow-and-continue would
    need its own decision.
    """

    async def fake_query_beads(params):
        return [_bill("b1", due_in_days=-3)]

    async def fake_patch_bead_state(bead_id, state, created_by):
        raise RuntimeError("substrate unavailable")

    monkeypatch.setattr(bill_guardian, "query_beads", fake_query_beads)
    monkeypatch.setattr(bill_guardian, "patch_bead_state", fake_patch_bead_state)

    with pytest.raises(RuntimeError, match="substrate unavailable"):
        await bill_guardian.get_bill_guardian_summary()
