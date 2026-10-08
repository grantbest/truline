"""A sync failure must leave a record, not just a shout.

LO-OBS-004: AC-1 passed and AC-2 failed. The signal was right — a crashed
BankSyncWorkflow did alert — and it had no volume control, because the call site
posted directly and bypassed the alert policy entirely. A persistent failure
was worth up to twenty identical Discord messages a day.

Volume is the lesser half. A Discord message is not a record: once it scrolls,
the failure has left nothing that can be counted, queried, or closed, which is
why the correct signal never stopped this class of problem recurring.

Deliberately absent from these tests: any assertion about a NEW notification
path. Track D1 owns the single outbound policy and is not built; this change
reuses the suppression already in the module and adds no channel of its own.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from workflows import bank_sync  # noqa: E402


class _Store:
    def __init__(self):
        self.beads = []
        self.posts = []
        self.patches = []
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
        bead = {"id": f"sf-{len(self.beads)}", "type": bead_type,
                "content": content, "state": state}
        self.beads.append(bead)
        return bead

    async def patch(self, bead_id, content=None, state=None, created_by=None):
        if self.substrate_raises:
            raise RuntimeError("substrate unreachable")
        self.patches.append((bead_id, created_by))
        for b in self.beads:
            if b["id"] == bead_id:
                if content is not None:
                    b["content"] = content
                if state is not None:
                    b["state"] = state
                return b
        return None

    async def send(self, kind, fingerprint, content, **kw):
        """Stand-in for the real suppressor: same change-only contract."""
        last = next((p for p in self.posts if p["kind"] == kind), None)
        if last and last["fingerprint"] == fingerprint:
            return False
        self.posts.append({"kind": kind, "fingerprint": fingerprint})
        return True


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(bank_sync, "finance_query_beads", s.query)
    monkeypatch.setattr(bank_sync, "create_bead", s.create)
    monkeypatch.setattr(bank_sync, "patch_bead", s.patch)
    monkeypatch.setattr(bank_sync, "_alert_policy", lambda: s)
    monkeypatch.setattr(bank_sync, "notify_env_label", lambda: "prod")
    return s


def _open_records(store, institution="chase"):
    return [
        b for b in store.beads
        if b["state"] == "pending" and b["content"]["institution"] == institution
    ]


# --- one record, however many nights ----------------------------------------


@pytest.mark.asyncio
async def test_four_failing_runs_leave_one_record_and_one_alert(store):
    for _ in range(4):
        await bank_sync.notify_sync_failure_activity("chase", "connection reset by peer")

    assert len(_open_records(store)) == 1
    assert len(store.posts) == 1, "an unchanged failure is not new information"
    assert _open_records(store)[0]["content"]["occurrences"] == 4


@pytest.mark.asyncio
async def test_the_record_names_the_institution_environment_and_attempt(store):
    await bank_sync.notify_sync_failure_activity("amex", "boom")

    content = _open_records(store, "amex")[0]["content"]
    assert content["institution"] == "amex"
    assert content["environment"] == "prod"
    assert content["attempted"]
    assert content["last_error"] == "boom"
    assert content["first_observed_at"]


@pytest.mark.asyncio
async def test_a_different_failure_still_gets_through(store):
    """Suppression must not swallow a genuinely new problem."""
    await bank_sync.notify_sync_failure_activity("chase", "connection reset")
    await bank_sync.notify_sync_failure_activity("chase", "ITEM_LOGIN_REQUIRED")

    assert len(store.posts) == 2
    assert len(_open_records(store)) == 1, "still one standing condition"


@pytest.mark.asyncio
async def test_two_institutions_keep_separate_records(store):
    await bank_sync.notify_sync_failure_activity("chase", "boom")
    await bank_sync.notify_sync_failure_activity("amex", "boom")

    assert len(_open_records(store, "chase")) == 1
    assert len(_open_records(store, "amex")) == 1


# --- a success closes it ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_success_resolves_the_record(store):
    await bank_sync.notify_sync_failure_activity("chase", "boom")

    closed = await bank_sync.resolve_sync_failure_activity("chase")

    assert closed is True
    assert _open_records(store) == []
    resolved = [b for b in store.beads if b["state"] == "resolved"]
    assert resolved[0]["content"]["resolved_by"] == "bank-sync/successful-run"
    assert resolved[0]["content"]["resolved_at"]


@pytest.mark.asyncio
async def test_resolving_with_nothing_open_is_harmless(store):
    assert await bank_sync.resolve_sync_failure_activity("chase") is False


@pytest.mark.asyncio
async def test_a_failure_after_a_resolution_opens_a_new_record(store):
    await bank_sync.notify_sync_failure_activity("chase", "boom")
    await bank_sync.resolve_sync_failure_activity("chase")
    await bank_sync.notify_sync_failure_activity("chase", "boom again")

    assert len(_open_records(store)) == 1
    assert len([b for b in store.beads if b["state"] == "resolved"]) == 1


# --- bookkeeping must never silence a real alert ----------------------------


@pytest.mark.asyncio
async def test_a_substrate_failure_does_not_suppress_the_alert(store):
    """Fail open — the same reasoning the suppression helper already carries."""
    store.substrate_raises = True

    posted = await bank_sync.notify_sync_failure_activity("chase", "boom")

    assert posted is True
    assert store.beads == [], "nothing could be written, and that is survivable"


@pytest.mark.asyncio
async def test_a_substrate_failure_does_not_fail_a_good_sync(store):
    store.substrate_raises = True

    assert await bank_sync.resolve_sync_failure_activity("chase") is False


# --- the activity is reachable ----------------------------------------------


def test_the_resolve_activity_is_registered_with_the_worker():
    """An activity the worker does not know about cannot run.

    This is the FA-S26 failure class — the Temporal worker could not start
    because activities were registered wrongly — and it is invisible to every
    unit test that calls the function directly.
    """
    worker_src = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "temporal_worker.py"
    ).read_text()

    assert worker_src.count("resolve_sync_failure_activity") >= 2, (
        "expected the activity in both the import list and the registration list"
    )
