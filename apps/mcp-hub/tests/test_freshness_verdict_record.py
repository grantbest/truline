"""A freshness verdict must leave a record, not just an API response.

LO-OBS-002: `tools.connections._derive_status` already derives a per-institution
freshness SLO and a `stale`/`ok` verdict correctly (see
test_connections_freshness_slo.py) — but only at read time, recomputed fresh on
every `/finance/connections` call. Nothing about the verdict was written down,
so it could not survive the way LO-OBS-004's sync_failure record does: query it,
count it, close it.

The Chase incident this whole track exists to prevent — credentials valid,
provider reachable, no transactions for days, all green — is reproduced here as
a persisted record, not just a status string: `evaluate_freshness_activity` is
only ever called from the branch where the Item already probed healthy, so a
`stale` record here can never be a re-auth problem in disguise.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from workflows import bank_sync  # noqa: E402


class _Store:
    def __init__(self):
        self.beads = []
        self.substrate_raises = False

    async def query(self, params):
        if self.substrate_raises:
            raise RuntimeError("substrate unreachable")
        state = params.get("state")
        return [
            b for b in self.beads
            if b.get("type") == params.get("type")
            and (state is None or b.get("state") == state)
        ]

    async def create(self, bead_type, content, state, created_by):
        if self.substrate_raises:
            raise RuntimeError("substrate unreachable")
        bead = {"id": f"fr-{len(self.beads)}", "type": bead_type,
                "content": content, "state": state}
        self.beads.append(bead)
        return bead

    async def patch(self, bead_id, content=None, state=None, created_by=None):
        if self.substrate_raises:
            raise RuntimeError("substrate unreachable")
        for b in self.beads:
            if b.get("id") == bead_id:
                if content is not None:
                    b["content"] = content
                if state is not None:
                    b["state"] = state
                return b
        return None


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(bank_sync, "finance_query_beads", s.query)
    monkeypatch.setattr(bank_sync, "create_bead", s.create)
    monkeypatch.setattr(bank_sync, "patch_bead", s.patch)
    monkeypatch.setattr(bank_sync, "notify_env_label", lambda: "prod")
    return s


def _tx(slug: str, posted: date, state: str = "posted") -> dict:
    return {"type": "transaction", "state": state, "content": {
        "institution": slug, "posted_date": posted.isoformat(),
    }}


def _open_alerts(store, institution="chase"):
    return [
        b for b in store.beads
        if b["type"] == "freshness_alert"
        and b["state"] == "pending"
        and b["content"]["institution"] == institution
    ]


def _resolved_alerts(store, institution="chase"):
    return [
        b for b in store.beads
        if b["type"] == "freshness_alert"
        and b["state"] == "resolved"
        and b["content"]["institution"] == institution
    ]


# --- the Chase scenario: credentials valid, provider reachable, no data ------


def _seed_stale_chase(store):
    """A daily-ish feed, then 35+ days of silence — same fixture shape as
    test_connections_freshness_slo.test_the_chase_case_is_not_ok."""
    today = date.today()
    for d in (40, 39, 38, 37, 36, 35):
        store.beads.append(_tx("chase", today - timedelta(days=d)))


@pytest.mark.asyncio
async def test_the_chase_case_opens_an_unhealthy_record_with_no_credential_change(store):
    _seed_stale_chase(store)

    result = await bank_sync.evaluate_freshness_activity("chase")

    assert result["status"] == "stale"
    records = _open_alerts(store)
    assert len(records) == 1
    content = records[0]["content"]
    assert content["institution"] == "chase"
    assert content["environment"] == "prod"
    assert content["freshness_slo_days"] == result["freshness_slo_days"]
    assert content["days_since_last_transaction"] == result["days_since_last_transaction"]
    assert content["days_since_last_transaction"] > content["freshness_slo_days"]
    # The verdict names the SLO, the age, and the institution.
    assert str(content["freshness_slo_days"]) in content["message"]
    assert str(content["days_since_last_transaction"]) in content["message"]
    assert "chase" in content["message"]
    # Nothing here is a credential or reachability field — this is a data-
    # arrival verdict only.
    assert "error_code" not in content
    assert "healthy" not in content


@pytest.mark.asyncio
async def test_four_nightly_evaluations_of_the_same_condition_leave_one_record(store):
    _seed_stale_chase(store)

    for _ in range(4):
        await bank_sync.evaluate_freshness_activity("chase")

    records = _open_alerts(store)
    assert len(records) == 1
    assert records[0]["content"]["occurrences"] == 4


@pytest.mark.asyncio
async def test_two_institutions_keep_separate_records(store):
    _seed_stale_chase(store)
    today = date.today()
    for d in (58, 51, 44, 37, 30, 23, 16):
        store.beads.append(_tx("amex", today - timedelta(days=d)))

    await bank_sync.evaluate_freshness_activity("chase")
    await bank_sync.evaluate_freshness_activity("amex")

    assert len(_open_alerts(store, "chase")) == 1
    assert len(_open_alerts(store, "amex")) == 1


# --- a later arrival resolves it, and names the date it saw -----------------


def _seed_recoverable_stale_chase(store):
    """A weekly cadence, then silence just past its own SLO — chosen so that,
    unlike ``_seed_stale_chase``, the gap closed by a resumed transaction
    stays inside the lookback window instead of pushing the derived SLO past
    it (which would read as unmeasurable rather than recovered)."""
    today = date.today()
    for d in (30, 23, 16):
        store.beads.append(_tx("chase", today - timedelta(days=d)))


@pytest.mark.asyncio
async def test_data_resuming_resolves_the_record_and_names_the_date(store):
    _seed_recoverable_stale_chase(store)
    first = await bank_sync.evaluate_freshness_activity("chase")
    assert first["status"] == "stale"
    assert len(_open_alerts(store)) == 1

    newest = date.today()
    store.beads.append(_tx("chase", newest))

    result = await bank_sync.evaluate_freshness_activity("chase")

    assert result["status"] == "ok"
    assert result["last_transaction_date"] == newest.isoformat()
    assert _open_alerts(store) == []
    resolved = _resolved_alerts(store)
    assert len(resolved) == 1
    assert resolved[0]["content"]["last_transaction_date"] == newest.isoformat()
    assert newest.isoformat() in resolved[0]["content"]["message"]
    assert resolved[0]["content"]["resolved_at"]


@pytest.mark.asyncio
async def test_resolving_with_nothing_open_is_harmless(store):
    assert await bank_sync.resolve_freshness_alert("chase", last_transaction_date="2026-08-01") is False


@pytest.mark.asyncio
async def test_a_second_stale_spell_opens_a_new_record(store):
    _seed_stale_chase(store)
    await bank_sync.evaluate_freshness_activity("chase")
    await bank_sync.resolve_freshness_alert("chase", last_transaction_date=date.today().isoformat())
    assert _open_alerts(store) == []

    _seed_stale_chase(store)
    await bank_sync.evaluate_freshness_activity("chase")

    assert len(_open_alerts(store)) == 1
    assert len(_resolved_alerts(store)) == 1


# --- an unmeasurable cadence is neither healthy nor stale --------------------


@pytest.mark.asyncio
async def test_too_little_history_opens_no_record(store):
    store.beads.append(_tx("sofi", date.today() - timedelta(days=1)))

    result = await bank_sync.evaluate_freshness_activity("sofi")

    assert result["status"] == "freshness_unknown"
    assert _open_alerts(store, "sofi") == []


@pytest.mark.asyncio
async def test_no_data_at_all_opens_no_record(store):
    result = await bank_sync.evaluate_freshness_activity("brand_new_bank")

    assert result["status"] == "freshness_unknown"
    assert _open_alerts(store, "brand_new_bank") == []


@pytest.mark.asyncio
async def test_unmeasurable_cadence_does_not_clear_an_existing_alert(store):
    """Silence long enough to exceed the lookback window looks identical to no
    history at all — that must not read as recovery for an already-open
    record."""
    _seed_stale_chase(store)
    await bank_sync.evaluate_freshness_activity("chase")
    assert len(_open_alerts(store)) == 1

    store.beads = [b for b in store.beads if b["type"] != "transaction"]
    result = await bank_sync.evaluate_freshness_activity("chase")

    assert result["status"] == "freshness_unknown"
    assert len(_open_alerts(store)) == 1, "an unmeasurable read must not silently resolve a real alert"


# --- the activity is reachable -----------------------------------------------


def test_the_evaluate_activity_is_registered_with_the_worker():
    worker_src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "temporal_worker.py"
    ).read_text()

    assert worker_src.count("evaluate_freshness_activity") >= 2, (
        "expected the activity in both the import list and the registration list"
    )
