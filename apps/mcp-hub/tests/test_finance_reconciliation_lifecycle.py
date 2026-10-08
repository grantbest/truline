"""A discrepancy must not be resolved by the calendar rolling over.

LO-REC-001/AC-3. The arithmetic in this module was always sound; the lifecycle
was not. Because a fresh balance snapshot is written every night, the next run
reconciles against a baseline that already contains the unexplained gap — so an
account that drifted on night 1 comes up clean on night 2. The old code marked
the discrepancy ``resolved`` at that point. Nobody resolved it. The money was
still missing and the system had stopped saying so.

These tests hold the two halves: a clean night does not close anything, and a
refresh does not overwrite what was first seen.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from workflows import finance_reconciliation as reconciliation  # noqa: E402


class _FakeSubstrate:
    """Records writes so the lifecycle can be asserted without a substrate."""

    def __init__(self, pending=None):
        self.beads = list(pending or [])
        self.created = []
        self.patches = []

    async def query_beads(self, params):
        state = params.get("state")
        return [
            b for b in self.beads
            if b.get("type", reconciliation._DISCREPANCY_TYPE) == params.get("type")
            and (state is None or b.get("state") == state)
        ]

    async def create_bead(self, bead_type, content, state, created_by):
        bead = {"id": f"new-{len(self.created)}", "type": bead_type,
                "content": content, "state": state}
        self.beads.append(bead)
        self.created.append({"content": content, "created_by": created_by})
        return bead

    async def patch_bead(self, bead_id, content=None, state=None, created_by=None):
        self.patches.append({"id": bead_id, "content": content,
                             "state": state, "created_by": created_by})
        for b in self.beads:
            if b["id"] == bead_id:
                if content is not None:
                    b["content"] = content
                if state is not None:
                    b["state"] = state
                return b
        return None


@pytest.fixture
def sub(monkeypatch):
    fake = _FakeSubstrate()
    monkeypatch.setattr(reconciliation, "query_beads", fake.query_beads)
    monkeypatch.setattr(reconciliation, "create_bead", fake.create_bead)
    monkeypatch.setattr(reconciliation, "patch_bead", fake.patch_bead)
    return fake


def _drift(account_id="chk", amount=-400.0):
    return {
        "account_id": account_id,
        "account_name": "Checking",
        "expected_balance": 1000.0,
        "actual_balance": 1000.0 + amount,
        "drift": amount,
        "prev_snapshot_at": "2026-07-10T03:45:00",
    }


def _pending(account_id="chk", **content):
    base = {
        "account_id": account_id,
        "drift": -400.0,
        "first_observed_at": "2026-07-11T03:45:00",
        "first_observed_drift": -400.0,
    }
    base.update(content)
    return {"id": "disc-1", "type": reconciliation._DISCREPANCY_TYPE,
            "state": "pending", "content": base}


# --- the defect ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_night_does_not_resolve_an_open_discrepancy(sub):
    """Night 1 drifts, night 2 is clean. The discrepancy stays open."""
    sub.beads.append(_pending())

    await reconciliation.emit_discrepancies_activity([])

    assert sub.beads[0]["state"] == "pending"
    assert all(p["state"] != "resolved" for p in sub.patches)


@pytest.mark.asyncio
async def test_a_clean_night_is_recorded_as_an_observation(sub):
    """It is still real information — just not an explanation."""
    sub.beads.append(_pending())

    await reconciliation.emit_discrepancies_activity([])

    content = sub.patches[0]["content"]
    assert content["clean_runs_since_first_observed"] == 1
    assert content["last_clean_run_at"]
    assert sub.patches[0]["created_by"].endswith("observed_clean_still_open")


@pytest.mark.asyncio
async def test_five_clean_nights_leave_it_open_and_counted(sub):
    sub.beads.append(_pending())

    for _ in range(5):
        await reconciliation.emit_discrepancies_activity([])

    assert sub.beads[0]["state"] == "pending"
    assert sub.beads[0]["content"]["clean_runs_since_first_observed"] == 5
    assert sub.beads[0]["content"]["first_observed_drift"] == -400.0


# --- the first observation survives ----------------------------------------


@pytest.mark.asyncio
async def test_a_refresh_preserves_the_first_observation(sub):
    """A bigger drift later must not rewrite what was first seen."""
    sub.beads.append(_pending())

    await reconciliation.emit_discrepancies_activity([_drift(amount=-950.0)])

    content = sub.patches[0]["content"]
    assert content["drift"] == -950.0                      # current
    assert content["first_observed_drift"] == -400.0       # unchanged
    assert content["first_observed_at"] == "2026-07-11T03:45:00"


@pytest.mark.asyncio
async def test_a_new_discrepancy_seeds_its_own_first_observation(sub):
    await reconciliation.emit_discrepancies_activity([_drift()])

    content = sub.created[0]["content"]
    assert content["first_observed_drift"] == content["drift"]
    assert content["first_observed_at"] == content["window_end"]


@pytest.mark.asyncio
async def test_one_open_record_per_account_however_many_nights(sub):
    for _ in range(3):
        await reconciliation.emit_discrepancies_activity([_drift()])

    open_for_chk = [b for b in sub.beads if b["content"]["account_id"] == "chk"]
    assert len(open_for_chk) == 1


# --- resolution is explicit, and says which kind ---------------------------


@pytest.mark.asyncio
async def test_resolution_requires_a_declared_kind(sub):
    sub.beads.append(_pending())

    with pytest.raises(ValueError, match="resolution must be one of"):
        await reconciliation.resolve_discrepancy(
            "disc-1", resolution="because time passed", resolved_by="grant"
        )
    assert sub.beads[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_resolution_requires_an_author(sub):
    sub.beads.append(_pending())

    with pytest.raises(ValueError, match="resolved_by is required"):
        await reconciliation.resolve_discrepancy(
            "disc-1", resolution=reconciliation.RESOLUTION_ACCEPTED, resolved_by="  "
        )


@pytest.mark.asyncio
async def test_accepting_closes_it_and_names_who_and_how(sub):
    sub.beads.append(_pending())

    await reconciliation.resolve_discrepancy(
        "disc-1", resolution=reconciliation.RESOLUTION_ACCEPTED, resolved_by="grant"
    )

    assert sub.beads[0]["state"] == "resolved"
    content = sub.beads[0]["content"]
    assert content["resolution"] == "accepted"
    assert content["resolved_by"] == "grant"
    assert content["resolved_at"]
    # The history the refresh path protects must survive resolution too.
    assert content["first_observed_at"] == "2026-07-11T03:45:00"


@pytest.mark.asyncio
async def test_a_resolved_discrepancy_stays_resolved_through_a_clean_run(sub):
    sub.beads.append(_pending())
    await reconciliation.resolve_discrepancy(
        "disc-1", resolution=reconciliation.RESOLUTION_CORRECTED, resolved_by="bank-feed"
    )

    await reconciliation.emit_discrepancies_activity([])

    assert sub.beads[0]["state"] == "resolved"
