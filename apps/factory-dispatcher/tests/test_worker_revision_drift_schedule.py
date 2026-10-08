"""Tests for the scheduled worker-revision-drift check (2026-08-29).

On 2026-08-29 a launchd worker ran twelve hours on a revision eleven commits behind main, and the
only thing that ever noticed was an operator running `schedule_status.py` by hand.
`worker_revision.describe_worker_revision_drift` already computed the drift and `schedule_status.py`
already rendered it clearly -- the gap was that neither ever ran unless a human asked. This module
adds the sixth registered schedule and its activity, which lands the same detection durably instead
of only printing it.

No Temporal server, no substrate database, no network, no launchd, no real git repository:
`WorkerRevisionStatus` is constructed directly (the same injected-state approach
`test_worker_revision_drift.py` already uses for `schedule_status.py`'s own tests), and the store
below is an in-memory fake exercising exactly `WorkerRevisionDriftStore`'s three methods.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import dispatch  # noqa: E402
import worker_revision  # noqa: E402
from activities import worker_revision_drift as wrd  # noqa: E402
from worker_revision import CheckoutFreshness, WorkerRevisionStatus  # noqa: E402
from test_dispatch import (  # noqa: E402
    create_chain_repo_mirror_canonical,
    git,
    push_new_commit_to_canonical,
)

MAIN_REF = "main"
OLD_REVISION = "aaaaaaa"
MAIN_REVISION = "ccccccc"


@pytest.fixture(autouse=True)
def _skip_base_ref_sync_by_default(monkeypatch):
    """dev.finding 5170b3f9a (AC-2): every pre-existing test in this file calls
    `report_worker_revision_drift_activity()` directly, with none of the Temporal/dispatch
    wiring the new base-ref-sync step needs -- left unstubbed, `default_bring_base_ref_current`
    would reach `default_factory_activity_in_flight`'s real `Client.connect`. `SYNC_SKIPPED`
    with an empty detail reproduces exactly the pre-this-bead activity: the sync step is
    bypassed and `describe_worker_revision_drift()` (already monkeypatched per test) drives the
    rest of the function unchanged. Tests that exercise the sync step itself override this.
    """
    monkeypatch.setattr(
        wrd, "default_bring_base_ref_current", lambda: wrd.BaseRefSyncResult(wrd.SYNC_SKIPPED)
    )


class FakeWorkerRevisionDriftStore:
    """In-memory double exposing exactly find_observation/create_observation/update_observation."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self._next_id = 1

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        for bead in self.observations:
            if (bead.get("content") or {}).get("ref") == ref:
                return dict(bead)
        return None

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["state"] == "active"
        assert payload["created_by"] == wrd.CREATED_BY
        bead = {"id": f"obs-{self._next_id}", **payload}
        self._next_id += 1
        self.observations.append(bead)
        return dict(bead)

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        for bead in self.observations:
            if bead["id"] == bead_id:
                if content is not None:
                    bead["content"] = content
                if context is not None:
                    bead["context"] = context
                if state is not None:
                    bead["state"] = state
                return dict(bead)
        raise AssertionError(f"no such observation {bead_id}")


def _drifted_status(
    *,
    revision: str = OLD_REVISION,
    started_at: str = "2026-08-28T13:24:00Z",
    is_ancestor: bool = True,
    commits_behind: int | None = 11,
) -> WorkerRevisionStatus:
    return WorkerRevisionStatus(
        worker_revision=revision,
        worker_started_at=started_at,
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=is_ancestor,
        commits_behind=commits_behind,
    )


def _clean_status() -> WorkerRevisionStatus:
    return WorkerRevisionStatus(
        worker_revision=MAIN_REVISION,
        worker_started_at="2026-08-29T01:00:00Z",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=0,
    )


def _unknown_status(error: str = "no worker revision record found") -> WorkerRevisionStatus:
    return WorkerRevisionStatus.could_not_determine(MAIN_REF, error)


def _now(when: str = "2026-08-29T01:55:00+00:00"):
    parsed = datetime.fromisoformat(when)
    return lambda: parsed


# -- a drift behind main produces the record ----------------------------------------------------


def test_a_worker_behind_main_lands_as_one_active_observation_with_revision_and_distance():
    store = FakeWorkerRevisionDriftStore()

    result = wrd.land_worker_revision_drift(store, _drifted_status(), now_fn=_now())

    assert result["condition"] == "drifted"
    assert result["action"] == "created"
    assert len(store.observations) == 1
    bead = store.observations[0]
    assert bead["state"] == "active"
    assert bead["content"]["ref"] == wrd.OBSERVATION_REF
    assert bead["context"]["condition"] == "drifted"
    assert bead["context"]["worker_revision"] == OLD_REVISION
    assert bead["context"]["commits_behind"] == 11
    assert bead["context"]["running_for_seconds"] == 45_060.0  # 12h 31m, 2026-08-28T13:24 -> 01:55


def test_a_worker_on_an_unreviewed_branch_lands_as_drifted_with_no_commit_count():
    store = FakeWorkerRevisionDriftStore()
    status = _drifted_status(is_ancestor=False, commits_behind=None)

    result = wrd.land_worker_revision_drift(store, status, now_fn=_now())

    assert result["condition"] == "drifted"
    bead = store.observations[0]
    assert bead["context"]["is_ancestor"] is False
    assert bead["context"]["commits_behind"] is None


# -- unknown is recorded as unknown, never as clean ---------------------------------------------


def test_an_indeterminate_revision_lands_as_unknown_not_clear():
    store = FakeWorkerRevisionDriftStore()

    result = wrd.land_worker_revision_drift(store, _unknown_status(), now_fn=_now())

    assert result["condition"] == "unknown"
    assert result["condition"] != "clear"
    bead = store.observations[0]
    assert bead["state"] == "active"
    assert bead["context"]["condition"] == "unknown"
    assert bead["context"]["error"] != ""


def test_unknown_does_not_close_a_record_that_was_already_open_as_drifted():
    """Losing pid correspondence mid-incident must not read as the drift resolving."""
    store = FakeWorkerRevisionDriftStore()
    wrd.land_worker_revision_drift(store, _drifted_status(), now_fn=_now("2026-08-29T01:00:00+00:00"))

    result = wrd.land_worker_revision_drift(
        store, _unknown_status(), now_fn=_now("2026-08-29T01:15:00+00:00")
    )

    assert result["condition"] == "unknown"
    assert result["action"] == "updated"
    assert len(store.observations) == 1
    assert store.observations[0]["state"] == "active"


# -- idempotent on the condition, not the run (AC-3 / PRIN-014) ---------------------------------


def test_persisting_drift_across_two_evaluations_updates_not_duplicates():
    store = FakeWorkerRevisionDriftStore()

    first = wrd.land_worker_revision_drift(
        store, _drifted_status(), now_fn=_now("2026-08-29T01:00:00+00:00")
    )
    second = wrd.land_worker_revision_drift(
        store, _drifted_status(), now_fn=_now("2026-08-29T01:15:00+00:00")
    )

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert first["bead_id"] == second["bead_id"]
    assert len(store.observations) == 1
    bead = store.observations[0]
    # first_observed_at survives the second evaluation; last_observed_at advances.
    assert bead["context"]["first_observed_at"] == "2026-08-29T01:00:00Z"
    assert bead["context"]["last_observed_at"] == "2026-08-29T01:15:00Z"


def test_distance_growing_across_evaluations_still_updates_the_one_record():
    store = FakeWorkerRevisionDriftStore()

    wrd.land_worker_revision_drift(
        store, _drifted_status(commits_behind=3), now_fn=_now("2026-08-29T01:00:00+00:00")
    )
    wrd.land_worker_revision_drift(
        store, _drifted_status(commits_behind=11), now_fn=_now("2026-08-29T01:15:00+00:00")
    )

    assert len(store.observations) == 1
    assert store.observations[0]["context"]["commits_behind"] == 11


# -- a drift that clears closes its record, and a clean check with nothing open writes nothing --


def test_a_drift_that_clears_closes_the_record():
    store = FakeWorkerRevisionDriftStore()
    created = wrd.land_worker_revision_drift(
        store, _drifted_status(), now_fn=_now("2026-08-29T01:00:00+00:00")
    )
    bead_id = created["bead_id"]

    result = wrd.land_worker_revision_drift(
        store, _clean_status(), now_fn=_now("2026-08-29T02:00:00+00:00")
    )

    assert result["condition"] == "clear"
    assert result["action"] == "closed"
    assert len(store.observations) == 1
    assert store.observations[0]["id"] == bead_id
    assert store.observations[0]["state"] == "resolved"


def test_a_clean_evaluation_with_nothing_open_writes_no_record():
    store = FakeWorkerRevisionDriftStore()

    result = wrd.land_worker_revision_drift(store, _clean_status(), now_fn=_now())

    assert result["condition"] == "clear"
    assert result["action"] == "none"
    assert store.observations == []


def test_a_recurring_drift_reopens_the_same_bead_rather_than_minting_a_second():
    store = FakeWorkerRevisionDriftStore()
    first = wrd.land_worker_revision_drift(
        store, _drifted_status(), now_fn=_now("2026-08-29T01:00:00+00:00")
    )
    bead_id = first["bead_id"]

    closed = wrd.land_worker_revision_drift(
        store, _clean_status(), now_fn=_now("2026-08-29T02:00:00+00:00")
    )
    assert closed["action"] == "closed"

    reopened = wrd.land_worker_revision_drift(
        store, _drifted_status(), now_fn=_now("2026-08-29T03:00:00+00:00")
    )

    assert reopened["action"] == "updated"
    assert reopened["bead_id"] == bead_id
    assert len(store.observations) == 1
    bead = store.observations[0]
    assert bead["state"] == "active"
    # A fresh occurrence, not the closed one's history bleeding through.
    assert bead["context"]["first_observed_at"] == "2026-08-29T03:00:00Z"


# -- read-only w.r.t. the worker process ----------------------------------------------------------


def test_the_store_protocol_exposes_no_call_that_can_touch_a_process():
    declared = {name for name in vars(wrd.WorkerRevisionDriftStore) if not name.startswith("_")}
    assert declared == {"find_observation", "create_observation", "update_observation"}


# -- activity wiring: no Temporal, no substrate, no network ---------------------------------------


def test_worker_revision_drift_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.worker_revision_drift import WorkerRevisionDriftWorkflow

    definition = temporal_workflow._Definition.from_class(WorkerRevisionDriftWorkflow)
    assert definition.name == "WorkerRevisionDriftWorkflow"
    assert "WorkerRevisionDriftWorkflow" in inspect.getsource(worker.build_worker)


def test_worker_revision_drift_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_worker_revision_drift_activity" in names


# -- activity wiring: a drifted status also drives a response, a clean one never does ------------


def test_activity_invokes_the_response_and_reports_its_outcome_when_drifted(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)

    seen = []

    def fake_respond(status):
        seen.append(status)
        return DriftResponse("advanced", "advanced to ddddddd", revision="ddddddd")

    monkeypatch.setattr(wrd, "respond_to_drifted_status", fake_respond)

    result = wrd.report_worker_revision_drift_activity()

    assert len(seen) == 1
    assert result["response_action"] == "advanced"
    assert result["response_detail"] == "advanced to ddddddd"


def test_activity_does_not_invoke_the_response_for_a_clean_status(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _clean_status)

    def unreachable_respond(status):
        raise AssertionError("must not respond when the checkout is not drifted")

    monkeypatch.setattr(wrd, "respond_to_drifted_status", unreachable_respond)

    result = wrd.report_worker_revision_drift_activity()

    assert "response_action" not in result


def test_activity_does_not_invoke_the_response_for_an_unknown_status(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)

    def unreachable_respond(status):
        raise AssertionError("must not respond when the status is could-not-determine")

    monkeypatch.setattr(wrd, "respond_to_drifted_status", unreachable_respond)

    result = wrd.report_worker_revision_drift_activity()

    assert "response_action" not in result


# ---------------------------------------------------------------------------
# f51e057c AC3: a deferral that never ends becomes visible -- past the declared bound, the
# activity escalates through the declared alert policy (pausing dispatch) rather than returning
# "deferred" silently forever.
#
# FACT AT FILING (2026-09-13): the same worker-revision-drift condition was deferred 15
# consecutive times, 20:15Z-22:45Z, because the dispatch queue was effectively never idle --
# `respond_to_worker_revision_drift`'s own DEFERRED choice was correct every single time (never
# race a running task), and nothing ever noticed the streak.
# ---------------------------------------------------------------------------


def test_persistent_deferral_escalates_once_it_passes_the_declared_bound(monkeypatch):
    # dev.finding 639a20c5 AC-4: updated from main -- escalation now pauses dispatch only when
    # the crossing deferral's blocker was dispatch itself (`blocked_by_dispatch=True`), not on
    # every escalation unconditionally. This case names a genuine dispatch run as the blocker, so
    # the pause must still happen, exactly as before.
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse(
            "deferred", "a dispatch run is in flight; deferring", blocked_by_dispatch=True
        ),
    )
    halted = []
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: halted.append(note))

    results = [
        wrd.report_worker_revision_drift_activity()
        for _ in range(wrd.MAX_CONSECUTIVE_DEFERRALS + 1)
    ]

    # every deferral up to and including the bound is reported as an ordinary deferral --
    # deferring once, or a few times in a row, is not itself the problem -- and none of them
    # escalated early.
    for result in results[: wrd.MAX_CONSECUTIVE_DEFERRALS]:
        assert result["response_action"] == "deferred"

    # the (bound + 1)th consecutive deferral of the *same* condition escalates instead.
    escalated = results[wrd.MAX_CONSECUTIVE_DEFERRALS]
    assert escalated["response_action"] == "escalated"
    assert escalated["response_action"] != "deferred"
    assert len(halted) == 1
    assert "consecutive deferral" in halted[0]


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-4: escalation pauses dispatch only when dispatch is the blocker. The
# 2026-09-30/10-01 incident's ~21h was 83 of 87 escalating deferrals blocked by a 15-minute
# reconciler, not dispatch -- pausing dispatch harder could never clear that, and it stopped the
# whole factory for no benefit.
# ---------------------------------------------------------------------------


def test_escalation_blocked_by_a_reconciler_announces_without_pausing_dispatch(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse(
            "deferred",
            "schedule 'factory-cluster-health-15m' (cluster_health) has an in-flight workflow; "
            "deferring",
            blocked_by_dispatch=False,
        ),
    )
    halted = []
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: halted.append(note))
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated",
        lambda note, **kwargs: announced.append(kwargs),
    )

    results = [
        wrd.report_worker_revision_drift_activity()
        for _ in range(wrd.MAX_CONSECUTIVE_DEFERRALS + 1)
    ]

    assert results[wrd.MAX_CONSECUTIVE_DEFERRALS]["response_action"] == "escalated"
    assert halted == []  # not dispatch's fault -- pausing it would not help and must not happen
    assert len(announced) == 1
    assert announced[0]["dispatch_paused"] is False


def test_escalation_blocked_by_dispatch_announces_and_pauses(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse(
            "deferred", "a dispatch run is in flight; deferring", blocked_by_dispatch=True
        ),
    )
    halted = []
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: halted.append(note))
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated",
        lambda note, **kwargs: announced.append(kwargs),
    )

    results = [
        wrd.report_worker_revision_drift_activity()
        for _ in range(wrd.MAX_CONSECUTIVE_DEFERRALS + 1)
    ]

    assert results[wrd.MAX_CONSECUTIVE_DEFERRALS]["response_action"] == "escalated"
    assert len(halted) == 1
    assert len(announced) == 1
    assert announced[0]["dispatch_paused"] is True


def test_escalation_decision_follows_only_the_crossing_deferral_not_the_earlier_streak(
    monkeypatch,
):
    """Whatever blocked the first three deferrals of the streak, only the (bound + 1)th -- the
    one that actually crosses the bound and triggers escalation -- decides whether dispatch
    pauses."""
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    responses = iter(
        [
            DriftResponse("deferred", "d1", blocked_by_dispatch=True),
            DriftResponse("deferred", "d2", blocked_by_dispatch=True),
            DriftResponse("deferred", "d3", blocked_by_dispatch=True),
            DriftResponse("deferred", "d4 (crossing)", blocked_by_dispatch=False),
        ]
    )
    monkeypatch.setattr(wrd, "respond_to_drifted_status", lambda status: next(responses))
    halted = []
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: halted.append(note))
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated",
        lambda note, **kwargs: announced.append(kwargs),
    )

    results = [wrd.report_worker_revision_drift_activity() for _ in range(4)]

    assert results[-1]["response_action"] == "escalated"
    assert halted == []  # the crossing deferral was not dispatch's fault
    assert announced[0]["dispatch_paused"] is False


def test_a_non_deferred_response_resets_the_consecutive_deferral_count(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    halted = []
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: halted.append(note))

    responses = iter(
        [
            DriftResponse("deferred", "d1"),
            DriftResponse("deferred", "d2"),
            DriftResponse("deferred", "d3"),
            DriftResponse("advanced", "advanced past the deferral", revision="zzzzzzz"),
            DriftResponse("deferred", "d4"),
        ]
    )
    monkeypatch.setattr(wrd, "respond_to_drifted_status", lambda status: next(responses))

    results = [wrd.report_worker_revision_drift_activity() for _ in range(5)]

    assert [r["response_action"] for r in results] == [
        "deferred",
        "deferred",
        "deferred",
        "advanced",
        "deferred",
    ]
    # three consecutive deferrals never escalated (the bound is 3), and the advance in between
    # reset the streak -- the fourth deferral is the first of a fresh streak, not a fourth.
    assert halted == []
    bead = store.find_observation(wrd.OBSERVATION_REF)
    assert bead["context"]["consecutive_deferrals"] == 1


def test_max_consecutive_deferrals_bound_is_pinned_to_its_declared_value():
    # f51e057c F3: the mechanism (escalate-past-the-bound) was already pinned by the tests above,
    # but nothing pinned the *value* -- a mutation of the constant itself (3 -> 999) left both
    # deferral tests green. 3 is the implementer's documented choice (45-60 minutes of deferral
    # at this schedule's 15-minute cadence); assert it directly so a future change to it is a
    # deliberate edit here, not a silent drift of how long an unannounced halt can go unnoticed.
    assert wrd.MAX_CONSECUTIVE_DEFERRALS == 3


# ---------------------------------------------------------------------------
# f51e057c F1: an escalation that only pauses the schedule announces nothing -- PRIN-008 requires
# a problem to announce itself, not just repeat an unremarkable-looking halt. Past the declared
# bound, the escalation must also raise the declared alert policy, not merely pause dispatch.
# ---------------------------------------------------------------------------


def test_escalation_announces_through_the_declared_alert_policy(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse("deferred", "a dispatch run is in flight; deferring"),
    )
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: None)
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated",
        lambda note, **kwargs: announced.append(note),
    )

    for _ in range(wrd.MAX_CONSECUTIVE_DEFERRALS):
        wrd.report_worker_revision_drift_activity()
    assert announced == []  # ordinary deferrals, up to and including the bound, never announce

    result = wrd.report_worker_revision_drift_activity()

    assert result["response_action"] == "escalated"
    assert len(announced) == 1
    assert "consecutive deferral" in announced[0]


def test_an_ordinary_deferral_never_announces_the_escalation_alert(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse("deferred", "a dispatch run is in flight; deferring"),
    )
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: None)

    def unreachable_announce(note, **kwargs):
        raise AssertionError("must not announce before the deferral streak passes the bound")

    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_escalated", unreachable_announce
    )

    result = wrd.report_worker_revision_drift_activity()

    assert result["response_action"] == "deferred"


def test_escalation_announces_with_the_drifted_revision_so_the_alert_fingerprint_stays_stable(
    monkeypatch,
):
    """f51e057c F-D: the escalation announcement must carry the drifted worker revision (not
    just the counter-bearing note) so `failure_diagnosis.announce_worker_revision_drift_escalated`
    can fingerprint a stable identity instead of the ever-changing consecutive-deferral count --
    see that function's own test coverage in `tests/test_failure_diagnosis.py` for why a
    per-tick-changing fingerprint defeated the 24h dedup entirely."""
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse("deferred", "a dispatch run is in flight; deferring"),
    )
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: None)
    announced_revisions = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated",
        lambda note, **kwargs: announced_revisions.append(kwargs.get("revision")),
    )

    for _ in range(wrd.MAX_CONSECUTIVE_DEFERRALS + 1):
        wrd.report_worker_revision_drift_activity()

    assert announced_revisions == [OLD_REVISION]


# ---------------------------------------------------------------------------
# f51e057c F2: a drift-escalation pause is not a one-way factory stop -- once the condition it
# exists to enforce actually clears (RESTARTED, ADVANCED, or the drift found clear outright), the
# schedule is resumed automatically rather than staying paused until a person notices by hand.
# Only a pause this mechanism itself made (its own note prefix) is ever touched.
# ---------------------------------------------------------------------------


def test_a_restarted_response_resumes_a_schedule_this_mechanism_paused(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd, "respond_to_drifted_status", lambda status: DriftResponse("restarted", "restarted")
    )
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))
    resumed = []
    monkeypatch.setattr(wrd, "default_resume_dispatch", lambda note: resumed.append(note))

    wrd.report_worker_revision_drift_activity()

    assert len(resumed) == 1
    assert "cleared" in resumed[0]


def test_an_advanced_response_resumes_a_schedule_this_mechanism_paused(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd,
        "respond_to_drifted_status",
        lambda status: DriftResponse("advanced", "advanced", revision="zzzzzzz"),
    )
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))
    resumed = []
    monkeypatch.setattr(wrd, "default_resume_dispatch", lambda note: resumed.append(note))

    wrd.report_worker_revision_drift_activity()

    assert len(resumed) == 1


def test_a_clean_evaluation_resumes_a_schedule_this_mechanism_paused(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _clean_status)

    def unreachable_respond(status):
        raise AssertionError("must not respond when the checkout is not drifted")

    monkeypatch.setattr(wrd, "respond_to_drifted_status", unreachable_respond)
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))
    resumed = []
    monkeypatch.setattr(wrd, "default_resume_dispatch", lambda note: resumed.append(note))

    wrd.report_worker_revision_drift_activity()

    assert len(resumed) == 1


def test_a_restarted_response_announces_the_escalation_resolved_alert(monkeypatch):
    """f51e057c F-E: an escalation that announces itself but never announces its own resolution
    leaves an operator who saw the escalation alert with no signal that dispatch is running
    again, short of checking by hand -- mirrors `capacity_pause_response.py`'s pairing of
    `resume_schedule` with `announce_resume` for the sibling capacity pause."""
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd, "respond_to_drifted_status", lambda status: DriftResponse("restarted", "restarted")
    )
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))
    monkeypatch.setattr(wrd, "default_resume_dispatch", lambda note: None)
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated_resolved",
        lambda note, **kwargs: announced.append(note),
    )

    wrd.report_worker_revision_drift_activity()

    assert len(announced) == 1
    assert "cleared" in announced[0]


def test_an_unpaused_schedule_never_announces_the_escalation_resolved_alert(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _clean_status)
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (False, ""))

    def unreachable_announce(note, **kwargs):
        raise AssertionError("must not announce a resolution for a schedule that was never paused")

    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_escalated_resolved", unreachable_announce
    )

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_a_manually_paused_schedule_is_never_resumed_by_drift_clearing(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _clean_status)
    monkeypatch.setattr(
        wrd, "default_dispatch_schedule_pause_state", lambda: (True, "paused by the Operator for maintenance")
    )

    def unreachable_resume(note):
        raise AssertionError("must not resume a pause this mechanism did not create")

    monkeypatch.setattr(wrd, "default_resume_dispatch", unreachable_resume)

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_an_unpaused_schedule_is_left_alone_when_drift_clears(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _clean_status)
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (False, ""))

    def unreachable_resume(note):
        raise AssertionError("must not resume a schedule that is not paused")

    monkeypatch.setattr(wrd, "default_resume_dispatch", unreachable_resume)

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_a_deferred_response_never_checks_the_schedule_pause_state(monkeypatch):
    from worker_checkout_drift_response import DriftResponse

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _drifted_status)
    monkeypatch.setattr(
        wrd, "respond_to_drifted_status", lambda status: DriftResponse("deferred", "deferring")
    )

    def unreachable_pause_state():
        raise AssertionError("must not check schedule pause state for an ordinary deferral")

    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", unreachable_pause_state)
    monkeypatch.setattr(wrd, "default_halt_dispatch", lambda note: None)

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_an_unknown_status_never_resumes_the_schedule_even_though_it_checks_pause_state(
    monkeypatch,
):
    """f51e057c / dev.finding 9a10f2aa: an unconfirmed (unknown) status now DOES check the
    schedule's pause state -- read-only, to decide whether to announce (see the
    unknown-while-paused tests below) -- but must never resume on it. PRIN-015 fail-closed: an
    unresolved condition must never resolve toward resuming."""
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))

    def unreachable_resume(note):
        raise AssertionError("must not resume a pause while the condition is unknown")

    monkeypatch.setattr(wrd, "default_resume_dispatch", unreachable_resume)
    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_unknown_while_paused", lambda *a, **k: None
    )

    wrd.report_worker_revision_drift_activity()  # must not raise


# ---------------------------------------------------------------------------
# dev.finding 9a10f2aa: a drift-escalation pause is never resumed and never announced when the
# condition becomes `unknown`. `unknown` correctly reaches neither resume branch (drifted-
# resolved, or condition-clear) -- staying paused IS the correct fail-closed behaviour (PRIN-015).
# The defect was the silence: the escalation alert's own 24h re-alert interval had already
# suppressed a repeat of itself, and nothing else ever announced that the pause was still in
# force with no way to clear on its own.
# ---------------------------------------------------------------------------


def test_unknown_while_paused_by_this_mechanism_announces_the_declared_alert(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)
    our_note = f"{wrd.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; 4th consecutive deferral"
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (True, our_note))
    announced = []
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_unknown_while_paused",
        lambda note, **kwargs: announced.append((note, kwargs)),
    )

    wrd.report_worker_revision_drift_activity()

    assert len(announced) == 1
    note, kwargs = announced[0]
    assert note == our_note
    assert kwargs.get("error")


def test_unknown_while_not_paused_at_all_stays_silent(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (False, ""))

    def unreachable_announce(note, **kwargs):
        raise AssertionError("must not announce when the schedule is not paused at all")

    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_unknown_while_paused", unreachable_announce
    )

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_unknown_while_paused_by_an_operator_stays_silent(monkeypatch):
    """It shall only speak for its own pause -- an operator's manual pause must not trigger it."""
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)
    monkeypatch.setattr(
        wrd, "default_dispatch_schedule_pause_state", lambda: (True, "paused by the Operator for maintenance")
    )

    def unreachable_announce(note, **kwargs):
        raise AssertionError("must not announce for a pause this mechanism did not create")

    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_unknown_while_paused", unreachable_announce
    )

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_unknown_while_paused_by_the_capacity_mechanism_stays_silent(monkeypatch):
    """It shall only speak for its own pause -- the sibling capacity-backpressure pause must not
    trigger it either, even though both pauses use the exact same underlying schedule-pause
    mechanism."""
    from schedule_runtime import CAPACITY_PAUSE_NOTE_PREFIX

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)
    monkeypatch.setattr(
        wrd,
        "default_dispatch_schedule_pause_state",
        lambda: (True, f"{CAPACITY_PAUSE_NOTE_PREFIX}: queue depth exceeded"),
    )

    def unreachable_announce(note, **kwargs):
        raise AssertionError("must not announce for the sibling capacity-backpressure pause")

    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_unknown_while_paused", unreachable_announce
    )

    wrd.report_worker_revision_drift_activity()  # must not raise


def test_a_pause_state_check_that_raises_does_not_crash_the_unknown_report(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(wrd.worker_revision, "describe_worker_revision_drift", _unknown_status)

    def exploding_pause_state():
        raise RuntimeError("temporal unreachable")

    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", exploding_pause_state)

    result = wrd.report_worker_revision_drift_activity()  # must not raise

    assert result["condition"] == "unknown"


# ---------------------------------------------------------------------------
# OPS-99 follow-up (2026-09-16): the checkout-advance guard must check every schedule this
# worker registers, not only the dispatch schedule -- OPS-99 raised
# worker.ACTIVITY_EXECUTOR_CONCURRENCY from 1 to 12, so the eleven 15-minute reconcilers
# (ea-apply, cluster-health, ...) can now be mid-flight, reading the shared checkout, at the
# exact moment `default_factory_activity_in_flight` used to only ask "is dispatch running".
# ---------------------------------------------------------------------------


class _FakeInFlightHandle:
    def __init__(self, running_workflow_ids: list[str]):
        self._running_workflow_ids = running_workflow_ids

    async def describe(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            schedule=SimpleNamespace(state=SimpleNamespace(paused=False)),
            info=SimpleNamespace(
                running_actions=tuple(
                    SimpleNamespace(workflow_id=workflow_id)
                    for workflow_id in self._running_workflow_ids
                ),
                recent_actions=(),
            ),
        )


class _FakeMultiScheduleClient:
    """Per-schedule-id running-workflow state -- unlike a single shared `.schedule`/
    `.running_actions` pair, this can say "schedule A is idle, schedule B is running" at once,
    which is exactly what proving the broadened guard requires."""

    def __init__(self, running_by_schedule_id: dict[str, list[str]]):
        self._running_by_schedule_id = running_by_schedule_id

    def get_schedule_handle(self, schedule_id: str) -> _FakeInFlightHandle:
        return _FakeInFlightHandle(self._running_by_schedule_id.get(schedule_id, []))


def _patch_connect(monkeypatch, client: _FakeMultiScheduleClient) -> None:
    async def fake_connect():
        return client

    monkeypatch.setattr(wrd, "_connect_temporal", fake_connect)


def test_factory_activity_in_flight_is_clear_when_every_registered_schedule_is_idle(monkeypatch):
    monkeypatch.setattr(
        wrd.schedule_runtime,
        "non_dispatch_schedule_ids",
        lambda: {"ea_apply": "factory-ea-apply-15m", "cluster_health": "factory-cluster-health-15m"},
    )
    _patch_connect(monkeypatch, _FakeMultiScheduleClient({}))

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is False


def test_factory_activity_in_flight_declines_on_a_non_dispatch_schedule(monkeypatch):
    """The blind spot this bead exists to close: dispatch itself is idle, but a NON-dispatch
    reconciler (ea-apply) is mid-flight. A guard that only asked "is dispatch running" -- the
    guard this replaces -- would report clear here and let `advance()` proceed underneath it.
    A test that only ever puts the *dispatch* schedule in flight would reproduce exactly that
    blind spot rather than close it, so this one deliberately leaves the dispatch schedule idle.
    """
    monkeypatch.setattr(
        wrd.schedule_runtime,
        "non_dispatch_schedule_ids",
        lambda: {
            "ea_apply": "factory-ea-apply-15m",
            "cluster_health": "factory-cluster-health-15m",
        },
    )
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient(
            {"factory-ea-apply-15m": ["factory-ea-apply-2026-09-16T12:00:00Z"]}
        ),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True
    # PRIN-008 / the acceptance criterion this test exists for: the refusal names what it is
    # waiting on, not just that it is declining.
    assert "factory-ea-apply-15m" in result.description
    assert "ea_apply" in result.description
    assert "factory-ea-apply-2026-09-16T12:00:00Z" in result.description
    assert wrd.config.DISPATCH_SCHEDULE_ID not in result.description


def test_factory_activity_in_flight_still_checks_the_dispatch_schedule_itself(monkeypatch):
    """Broadening the guard must not narrow it -- the original dispatch-in-flight case this
    mechanism has always covered must keep working."""
    monkeypatch.setattr(wrd.schedule_runtime, "non_dispatch_schedule_ids", lambda: {})
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient(
            {wrd.config.DISPATCH_SCHEDULE_ID: ["factory-dispatcher-2026-09-16T12:00:00Z"]}
        ),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True
    assert wrd.config.DISPATCH_SCHEDULE_ID in result.description


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-4: FactoryActivityInFlight names WHICH schedule it found running, not
# just that one is running -- `_escalate_persistent_deferral` needs this to decide whether
# pausing dispatch can help.
# ---------------------------------------------------------------------------


def test_factory_activity_in_flight_marks_dispatch_true_when_dispatch_is_the_blocker(monkeypatch):
    monkeypatch.setattr(wrd.schedule_runtime, "non_dispatch_schedule_ids", lambda: {})
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient(
            {wrd.config.DISPATCH_SCHEDULE_ID: ["factory-dispatcher-2026-09-16T12:00:00Z"]}
        ),
    )

    result = wrd.default_factory_activity_in_flight()

    assert result.dispatch is True


def test_factory_activity_in_flight_marks_dispatch_false_when_a_reconciler_is_the_blocker(
    monkeypatch,
):
    monkeypatch.setattr(
        wrd.schedule_runtime,
        "non_dispatch_schedule_ids",
        lambda: {"cluster_health": "factory-cluster-health-15m"},
    )
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient(
            {"factory-cluster-health-15m": ["factory-cluster-health-2026-09-16T12:00:00Z"]}
        ),
    )

    result = wrd.default_factory_activity_in_flight()

    assert result.dispatch is False


def test_factory_activity_in_flight_clear_result_is_not_dispatch(monkeypatch):
    monkeypatch.setattr(wrd.schedule_runtime, "non_dispatch_schedule_ids", lambda: {})
    _patch_connect(monkeypatch, _FakeMultiScheduleClient({}))

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is False
    assert result.dispatch is False


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-3: the bounded wait, pinned to its declared constants and their
# relationship to the activity's own start-to-close timeout.
# ---------------------------------------------------------------------------


def test_wait_and_poll_constants_are_pinned_to_their_declared_values():
    assert wrd.WORKER_REVISION_DRIFT_WAIT_SECONDS == 300
    assert wrd.WORKER_REVISION_DRIFT_POLL_INTERVAL_SECONDS == 15


def test_wait_window_fits_inside_the_activity_start_to_close_timeout_with_margin():
    from workflows.worker_revision_drift import ACTIVITY_START_TO_CLOSE_TIMEOUT

    margin = ACTIVITY_START_TO_CLOSE_TIMEOUT.total_seconds() - wrd.WORKER_REVISION_DRIFT_WAIT_SECONDS
    assert margin >= 120


def test_respond_to_drifted_status_wires_the_broadened_guard_into_the_drift_response(monkeypatch):
    """`respond_to_drifted_status` (what `report_worker_revision_drift_activity` actually calls)
    must pass the broadened check, not the old dispatch-only one, into
    `respond_to_worker_revision_drift`."""
    seen = {}

    def fake_respond_to_worker_revision_drift(status, **kwargs):
        seen.update(kwargs)
        return wrd.drift_response.DriftResponse(wrd.drift_response.NONE, "stub")

    monkeypatch.setattr(
        wrd.drift_response,
        "respond_to_worker_revision_drift",
        fake_respond_to_worker_revision_drift,
    )
    monkeypatch.setattr(wrd, "default_supervisor", lambda: None)
    monkeypatch.setattr(
        wrd.worker_checkout, "default_checkout_root", lambda: Path("/factory/worker-checkout")
    )
    monkeypatch.setattr(
        wrd, "default_source_repo_root", lambda checkout_root: Path("/operator/gastown")
    )

    status = WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-09-16T00:00:00Z",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=3,
    )
    wrd.respond_to_drifted_status(status)

    assert seen["factory_activity_in_flight"] is wrd.default_factory_activity_in_flight


def test_respond_to_drifted_status_passes_the_declared_wait_and_poll_constants(monkeypatch):
    """dev.finding 639a20c5 AC-3: this is the one caller that opts into the bounded wait --
    pinned here so the constants it passes can never silently drift from what it actually uses."""
    seen = {}

    def fake_respond_to_worker_revision_drift(status, **kwargs):
        seen.update(kwargs)
        return wrd.drift_response.DriftResponse(wrd.drift_response.NONE, "stub")

    monkeypatch.setattr(
        wrd.drift_response,
        "respond_to_worker_revision_drift",
        fake_respond_to_worker_revision_drift,
    )
    monkeypatch.setattr(wrd, "default_supervisor", lambda: None)
    monkeypatch.setattr(
        wrd.worker_checkout, "default_checkout_root", lambda: Path("/factory/worker-checkout")
    )
    monkeypatch.setattr(
        wrd, "default_source_repo_root", lambda checkout_root: Path("/operator/gastown")
    )

    status = WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-09-16T00:00:00Z",
        main_ref=MAIN_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=3,
    )
    wrd.respond_to_drifted_status(status)

    assert seen["wait_seconds"] == wrd.WORKER_REVISION_DRIFT_WAIT_SECONDS
    assert seen["poll_interval_seconds"] == wrd.WORKER_REVISION_DRIFT_POLL_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Release-gate DO-NOT-MERGE on PR #894 (2026-09-16): `non_dispatch_schedule_ids()` correctly
# includes this mechanism's own schedule ("worker_revision_drift") -- `worker.py` registers it
# exactly like every other reconciler, so the registry must not omit it. But the first broadened
# guard never excluded THIS evaluation's own currently-running workflow from that schedule's
# `running_actions`, so it always observed itself in flight, always deferred, and after
# MAX_CONSECUTIVE_DEFERRALS escalated to a dispatch pause that nothing could ever clear -- the
# remedy that would clear the pause was the thing being blocked. These tests deliberately use the
# REAL `schedule_runtime.non_dispatch_schedule_ids()`, not a fake subset, since a fake that never
# includes "worker_revision_drift" (as the tests above do, to prove the broadened walk itself)
# would pass whether or not the self-exclusion below exists.
# ---------------------------------------------------------------------------


def test_current_activity_workflow_id_is_none_outside_a_context():
    # The real temporalio.activity.in_activity(): no worker ever established a context for
    # this direct call, matching activities.dispatch_steps._current_workflow_run_identity's
    # own equivalent test.
    assert wrd._current_activity_workflow_id() is None


def test_current_activity_workflow_id_reads_activity_info_inside_a_context(monkeypatch):
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: True)
    monkeypatch.setattr(wrd.activity, "info", lambda: SimpleNamespace(workflow_id="wf-1"))

    assert wrd._current_activity_workflow_id() == "wf-1"


def test_factory_activity_in_flight_ignores_its_own_run_on_the_real_schedule_registry(monkeypatch):
    """The regression itself: only THIS run of the worker-revision-drift schedule is in flight,
    resolved against the real registry -- must be CLEAR, not a permanent self-deferral."""
    self_workflow_id = "factory-worker-revision-drift-2026-09-16T12:00:00Z"
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: True)
    monkeypatch.setattr(wrd.activity, "info", lambda: SimpleNamespace(workflow_id=self_workflow_id))
    drift_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["worker_revision_drift"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient({drift_schedule_id: [self_workflow_id]}),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is False


def test_factory_activity_in_flight_still_catches_a_genuinely_second_run_of_itself(monkeypatch):
    """The fix excludes this run's own workflow_id, not its whole schedule -- a second,
    genuinely concurrent execution of this same mechanism must still be caught."""
    self_workflow_id = "factory-worker-revision-drift-2026-09-16T12:00:00Z"
    other_workflow_id = "factory-worker-revision-drift-2026-09-16T12:15:00Z"
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: True)
    monkeypatch.setattr(wrd.activity, "info", lambda: SimpleNamespace(workflow_id=self_workflow_id))
    drift_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["worker_revision_drift"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient({drift_schedule_id: [self_workflow_id, other_workflow_id]}),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True
    assert other_workflow_id in result.description


def test_factory_activity_in_flight_still_declines_on_a_non_dispatch_schedule_with_the_real_registry(
    monkeypatch,
):
    """AC: a NON-dispatch activity in flight, resolved against the REAL registry (not a fake
    subset), must still decline -- proving the self-exclusion did not turn into a blanket
    exemption for anything running near this mechanism's own schedule."""
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: False)
    ea_apply_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["ea_apply"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient(
            {ea_apply_schedule_id: ["factory-ea-apply-2026-09-16T12:00:00Z"]}
        ),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True
    assert ea_apply_schedule_id in result.description


# ---------------------------------------------------------------------------
# Release-gate finding on PR #897 (2026-09-16), filed rather than patched there: the per-workflow
# self-exclusion above compares `workflow.workflow_id == self_workflow_id`, both real strings.
# `schedule_runtime._in_flight_workflow` falls back to the sentinel "(unknown workflow id)" when
# Temporal reports a running action with no readable workflow_id -- and that sentinel can never
# equal a real self_workflow_id, so an in-flight action on THIS mechanism's OWN schedule whose id
# cannot be read would be seen as foreign, deferred, and eventually escalated into the exact
# permanent dispatch pause #897 fixed (#894). An empty-string workflow_id on the fake running
# action (rather than the literal sentinel text) is what drives that fallback for real, through
# the same `_in_flight_workflow` conversion production uses -- not a hand-typed stand-in for it.
# ---------------------------------------------------------------------------


def test_factory_activity_in_flight_treats_an_unreadable_id_on_its_own_schedule_as_self(
    monkeypatch,
):
    """The regression this bead exists to close: Temporal reports OUR OWN schedule's in-flight
    action with an unreadable workflow_id (surfaces as `schedule_runtime.UNKNOWN_WORKFLOW_ID`).
    Comparing that sentinel against a real `self_workflow_id` can never match, so the guard must
    not fall through to treating it as a foreign activity -- that path is exactly what re-opens
    #894's permanent self-deferral. Must be CLEAR.

    Before the fix, this failed with:
        assert True is False
         +  where True = bool(FactoryActivityInFlight(in_flight=True, description="schedule "
        "'factory-worker-revision-drift-15m' (worker_revision_drift) has an in-flight workflow "
        "(workflow_id=(unknown workflow id))"))
    i.e. the guard saw the unreadable id as a foreign in-flight workflow and declined, rather than
    excluding it as this evaluation's own unreadable-id run.
    """
    self_workflow_id = "factory-worker-revision-drift-2026-09-16T12:00:00Z"
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: True)
    monkeypatch.setattr(wrd.activity, "info", lambda: SimpleNamespace(workflow_id=self_workflow_id))
    drift_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["worker_revision_drift"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient({drift_schedule_id: [""]}),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is False


def test_factory_activity_in_flight_still_declines_an_unreadable_id_on_a_foreign_schedule(
    monkeypatch,
):
    """The self-only carve-out must not widen into "any unreadable id anywhere is fine": an
    unreadable workflow_id on a DIFFERENT (non-drift) schedule must still block, exactly as a
    readable one does."""
    self_workflow_id = "factory-worker-revision-drift-2026-09-16T12:00:00Z"
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: True)
    monkeypatch.setattr(wrd.activity, "info", lambda: SimpleNamespace(workflow_id=self_workflow_id))
    ea_apply_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["ea_apply"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient({ea_apply_schedule_id: [""]}),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True
    assert ea_apply_schedule_id in result.description
    assert wrd.schedule_runtime.UNKNOWN_WORKFLOW_ID in result.description


def test_factory_activity_in_flight_still_declines_an_unreadable_id_on_its_own_schedule_outside_a_context(
    monkeypatch,
):
    """Outside a real activity context there is no `self_workflow_id` to exclude by, so an
    unreadable id on the drift schedule itself carries no evidence it is this run -- it must
    still block, matching the existing `self_workflow_id is not None` guard on the readable-id
    branch above."""
    monkeypatch.setattr(wrd.activity, "in_activity", lambda: False)
    drift_schedule_id = wrd.schedule_runtime.non_dispatch_schedule_ids()["worker_revision_drift"]
    _patch_connect(
        monkeypatch,
        _FakeMultiScheduleClient({drift_schedule_id: [""]}),
    )

    result = wrd.default_factory_activity_in_flight()

    assert bool(result) is True


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-5: the 2026-09-30/10-01 incident, replayed as it happened.
#
# The last DispatchTaskWorkflow started 2026-09-30T02:45Z, the next 2026-10-01T00:30Z, after the
# outer loop restarted the worker by hand -- ~21h. A release gate replayed all 97 drift runs from
# Temporal: the first four deferrals (02:15-03:00Z) named a dispatch run in flight, so the 03:00Z
# escalation's dispatch pause was legitimate; the next 83 named a 15-minute reconciler. Every
# blocker started in the same second as the drift run, and reconcilers finish in 19s or less --
# the pause was right, what made it last ~21h is that, with dispatch paused, the drift run kept
# colliding with a same-tick reconciler and never found the quiet moment that would let it
# restart and clear its own pause.
# ---------------------------------------------------------------------------


class _ReplaySupervisor:
    def __init__(self) -> None:
        self.recycled = 0

    def recycle(self) -> None:
        self.recycled += 1


class _ReplayPauseState:
    """Mirrors the real pause/resume/describe triangle closely enough to prove the resume path
    end to end. TRAP (AC-5): production prefixes the pause note with
    `drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX` inside `_halt_dispatch_async`, before
    the schedule is actually paused -- a fake `default_halt_dispatch` that records the bare note
    would make `respond_to_cleared_drift` see a note that does not carry this mechanism's own
    prefix (`is_our_pause_note`), return `NOT_OURS`, and never resume. `halt` below reproduces
    that prefixing exactly as production writes it.
    """

    def __init__(self) -> None:
        self.paused = False
        self.note = ""

    def halt(self, note: str) -> None:
        self.paused = True
        self.note = f"{wrd.drift_response.WORKER_REVISION_DRIFT_PAUSE_NOTE_PREFIX}; {note}"

    def resume(self, note: str) -> None:
        self.paused = False
        self.note = ""

    def state(self) -> tuple[bool, str]:
        return self.paused, self.note


#: Whether `FactoryActivityInFlight` carries the `dispatch` field this replay wants to set
#: (dev.finding 639a20c5 AC-4) -- feature-detected, not hard-coded, so this same test file also
#: runs unchanged against main's pre-fix `FactoryActivityInFlight(in_flight, description)` shape
#: (AC-5a): there the field does not exist and passing it would raise `TypeError` in setup before
#: any behaviour assertion ran.
_SUPPORTS_DISPATCH_FIELD = "dispatch" in wrd.FactoryActivityInFlight._fields


def _in_flight(in_flight: bool, description: str = "", *, dispatch: bool = False):
    if _SUPPORTS_DISPATCH_FIELD:
        return wrd.FactoryActivityInFlight(in_flight, description, dispatch=dispatch)
    return wrd.FactoryActivityInFlight(in_flight, description)


class _ReplayInFlight:
    """Run 1-4: a dispatch run in flight for the whole bounded-wait window (the real incident's
    legitimate escalation). Run 5 on: a 15-minute reconciler starts in the same second as the
    drift run (the first in-flight check sees it) and is still finishing at the first re-poll
    (15s elapsed, <19s), but is gone by the second re-poll (30s elapsed) -- the bounded wait
    outlasts it. On main's pre-fix code there is no bounded wait at all, so every one of these
    in-flight results is seen exactly once and deferred immediately."""

    def __init__(self) -> None:
        self.run = 0
        self.calls_this_run = 0

    def start_run(self) -> None:
        self.run += 1
        self.calls_this_run = 0

    def __call__(self) -> "wrd.FactoryActivityInFlight":
        self.calls_this_run += 1
        if self.run <= 4:
            return _in_flight(True, "dispatch run in flight", dispatch=True)
        if self.calls_this_run <= 2:
            return _in_flight(
                True,
                "schedule 'factory-cluster-health-15m' (cluster_health) has an in-flight "
                "workflow",
                dispatch=False,
            )
        return _in_flight(False)


def test_2026_09_30_incident_replay_finds_a_quiet_moment_and_recovers(monkeypatch):
    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(
        wrd.failure_diagnosis, "announce_worker_revision_drift_escalated", lambda *a, **k: None
    )
    monkeypatch.setattr(
        wrd.failure_diagnosis,
        "announce_worker_revision_drift_escalated_resolved",
        lambda *a, **k: None,
    )

    pause = _ReplayPauseState()
    monkeypatch.setattr(wrd, "default_halt_dispatch", pause.halt)
    monkeypatch.setattr(wrd, "default_resume_dispatch", pause.resume)
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", pause.state)

    supervisor = _ReplaySupervisor()
    monkeypatch.setattr(wrd, "default_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        wrd.worker_checkout, "default_checkout_root", lambda: Path("/factory/worker-checkout")
    )
    monkeypatch.setattr(
        wrd, "default_source_repo_root", lambda checkout_root: Path("/operator/gastown")
    )

    advanced = []

    def fake_advance(*, checkout_root, source_repo_root, main_ref):
        advanced.append((checkout_root, source_repo_root, main_ref))
        return "ddddddd"

    monkeypatch.setattr(wrd.worker_checkout, "advance", fake_advance)
    monkeypatch.setattr(
        wrd.worker_revision,
        "check_checkout_freshness",
        lambda **_kw: CheckoutFreshness(
            revision=OLD_REVISION,
            main_ref=MAIN_REF,
            main_revision=MAIN_REVISION,
            is_ancestor=True,
            commits_behind=9,
        ),
    )

    in_flight = _ReplayInFlight()
    monkeypatch.setattr(wrd, "default_factory_activity_in_flight", in_flight)

    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def fake_clock() -> float:
        return float(sum(sleep_calls))

    # Sleep/clock are injected only through `respond_to_worker_revision_drift`'s own `sleep`/
    # `clock` keyword parameters, and only when that function actually accepts them (dev.finding
    # 639a20c5 AC-5a). Ten runs x up to a 300s bounded wait would otherwise take the better part
    # of an hour for real, but reaching into `drift_response.time` directly -- the module-level
    # `time` import the bounded wait resolves its defaults from -- is a seam that exists only on
    # this fix's own tree: on main's pre-fix code `worker_checkout_drift_response` never imports
    # `time` at all, so `drift_response.time` raises `AttributeError` in setup, before any
    # behaviour assertion runs. Feature-detecting the real function's own signature instead keeps
    # this test file byte-for-byte identical on both trees: on main, no bounded wait exists to
    # inject into, `sleep`/`clock` are simply never passed, and every in-flight result the fake
    # above returns is deferred immediately -- which is exactly the pre-fix behaviour this replay
    # exists to demonstrate.
    _real_respond_to_worker_revision_drift = wrd.drift_response.respond_to_worker_revision_drift
    _supports_bounded_wait = (
        "sleep" in inspect.signature(_real_respond_to_worker_revision_drift).parameters
    )

    def _patched_respond_to_worker_revision_drift(*args, **kwargs):
        if _supports_bounded_wait:
            kwargs.setdefault("sleep", fake_sleep)
            kwargs.setdefault("clock", fake_clock)
        return _real_respond_to_worker_revision_drift(*args, **kwargs)

    monkeypatch.setattr(
        wrd.drift_response,
        "respond_to_worker_revision_drift",
        _patched_respond_to_worker_revision_drift,
    )

    cleared = False

    def status_for_run() -> WorkerRevisionStatus:
        # Derived from whether an earlier run's response actually RESTARTED or ADVANCED (dev.
        # finding 639a20c5 AC-5b) -- not hard-coded to a run number: the worker's recorded
        # revision stays behind main until a restart actually rewrites it. On main's pre-fix code
        # no run ever clears the condition, so this reports `drifted` through all ten runs,
        # matching the incident's actual signature (deferral on every remaining run, dispatch
        # still paused) rather than a clean recovery that never happened.
        return _clean_status() if cleared else _drifted_status()

    results = []
    pause_after_run: list[bool] = []
    for _n in range(1, 11):
        in_flight.start_run()
        monkeypatch.setattr(
            wrd.worker_revision, "describe_worker_revision_drift", status_for_run
        )
        result = wrd.report_worker_revision_drift_activity()
        results.append(result)
        pause_after_run.append(pause.paused)  # intermediate state, checked per-run below
        if result.get("response_action") in (
            wrd.drift_response.RESTARTED,
            wrd.drift_response.ADVANCED,
        ):
            cleared = True

    # Runs 1-3: ordinary deferrals -- a dispatch run really is in flight the whole window.
    for result in results[:3]:
        assert result["response_action"] == "deferred"
    assert pause_after_run[:3] == [False, False, False]

    # Run 4: the 4th consecutive deferral crosses MAX_CONSECUTIVE_DEFERRALS (3) and escalates.
    # Dispatch really was the blocker, so the escalation pauses it -- exactly as today.
    assert results[3]["response_action"] == "escalated"
    assert pause_after_run[3] is True

    # Run 5: a reconciler is in flight when the check first fires, but the bounded wait (300s
    # window, 15s polls) outlasts its <=19s runtime -- the checkout advances, the worker is
    # "recycled" (production: `launchctl kickstart -k`, which ends the process running this very
    # activity -- here, proof the response reached ADVANCED and the supervisor was invoked), and
    # the cleared-drift path resumes the pause this mechanism itself set.
    assert results[4]["response_action"] == "advanced"
    assert advanced == [(Path("/factory/worker-checkout"), Path("/operator/gastown"), MAIN_REF)]
    assert supervisor.recycled == 1
    assert pause_after_run[4] is False

    # Runs 6-10: the condition is clear -- no further escalation, dispatch stays running.
    for result in results[5:]:
        assert result["condition"] == "clear"
        assert "response_action" not in result
    assert pause_after_run[5:] == [False] * 5


# ---------------------------------------------------------------------------
# dev.finding 5170b3f9a: bring_base_ref_current -- the drift tick's own seam for bringing local
# main current with GitHub (through the source mirror) before the comparison, so an idle
# factory (empty queue, or every claim refused) tracks main within one tick instead of waiting
# for a hand scripts/factory-redeploy.py.
# ---------------------------------------------------------------------------


def test_bring_base_ref_current_skips_the_sync_when_something_is_in_flight():
    def unreachable_ensure(*_a, **_k):
        raise AssertionError("must not run ensure_base_ref_current while something is in flight")

    def unreachable_config():
        raise AssertionError("must not build a Config while something is in flight")

    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=lambda: wrd.FactoryActivityInFlight(
            True, "dispatch run in flight"
        ),
        ensure_base_ref_current=unreachable_ensure,
        config_factory=unreachable_config,
    )

    assert result.outcome == wrd.SYNC_SKIPPED
    assert "dispatch run in flight" in result.detail


def test_bring_base_ref_current_syncs_and_reports_current_when_clear():
    calls = []

    def ensure(cfg, *, force_tracking_refresh=False):
        calls.append((cfg, force_tracking_refresh))
        return dispatch.BaseRefStatus(
            base_ref="main",
            local_rev="c" * 40,
            upstream_ref="origin/main",
            upstream_rev="c" * 40,
            local_only=0,
            upstream_only=0,
        )

    cfg = dispatch.Config(base_ref="main")
    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=lambda: wrd.FactoryActivityInFlight(False),
        ensure_base_ref_current=ensure,
        config_factory=lambda: cfg,
    )

    assert result.outcome == wrd.SYNC_CURRENT
    assert result.revision == "c" * 40
    assert len(calls) == 1
    called_cfg, force = calls[0]
    assert called_cfg is cfg
    assert force is True


def test_bring_base_ref_current_reports_refused_when_ensure_raises_base_ref_stale():
    status = dispatch.BaseRefStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        local_only=0,
        upstream_only=1,
    )

    def refusing_ensure(cfg, *, force_tracking_refresh=False):
        raise dispatch.BaseRefStaleError(status)

    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=lambda: wrd.FactoryActivityInFlight(False),
        ensure_base_ref_current=refusing_ensure,
        config_factory=lambda: dispatch.Config(base_ref="main"),
    )

    assert result.outcome == wrd.SYNC_REFUSED
    assert "upstream_only=1" in result.detail


def test_bring_base_ref_current_skips_when_the_in_flight_check_itself_raises():
    def exploding_in_flight():
        raise RuntimeError("temporal unreachable")

    def unreachable_ensure(*_a, **_k):
        raise AssertionError("must not run ensure_base_ref_current when the in-flight check raised")

    def unreachable_config():
        raise AssertionError("must not build a Config when the in-flight check raised")

    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=exploding_in_flight,
        ensure_base_ref_current=unreachable_ensure,
        config_factory=unreachable_config,
    )

    assert result.outcome == wrd.SYNC_SKIPPED
    assert "temporal unreachable" in result.detail


# ---------------------------------------------------------------------------
# dev.finding 5170b3f9a AC-4: the idle factory tracks main, proved on real repositories built
# by the test -- the same github/mirror/checkout three-hop topology
# test_dispatch.py's create_chain_repo_mirror_canonical already builds and proves
# _sync_source_mirror_main against (canonical stands in for GitHub).
# ---------------------------------------------------------------------------


def _redirect_base_ref_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "FACTORY_BASE_REF_CHECK_STATE_PATH", str(tmp_path / "base-ref-check-state.json")
    )
    monkeypatch.setenv(
        "FACTORY_CONCURRENT_CLONE_DEFER_STATE_PATH",
        str(tmp_path / "concurrent-clone-defer-state.json"),
    )
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(tmp_path / "state"))


def test_bring_base_ref_current_converges_a_real_three_hop_checkout_and_restarts(
    tmp_path, monkeypatch
):
    _redirect_base_ref_state(monkeypatch, tmp_path)
    repo, mirror, canonical = create_chain_repo_mirror_canonical(tmp_path)

    record_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=repo, state_path=record_path, git_runner=dispatch.run, pid=os.getpid()
    )

    # A fresh FETCH_HEAD inside the staleness bound -- AC-8's mutation control: an un-forced
    # refresh would skip the fetch below and leave origin/main stale, exactly the defect this
    # bead fixes.
    git(repo, "fetch", "origin", "main")
    age = dispatch._fetch_head_age_s(repo)
    assert age is not None and age < dispatch.BASE_REF_TRACKING_STALENESS_BOUND_S

    new_tip = push_new_commit_to_canonical(tmp_path, canonical)

    workdir_root = tmp_path / "workdir-root"
    workdir_root.mkdir()
    cfg = dispatch.Config(repo_root=repo, remote=str(canonical), base_ref="main", workdir_root=workdir_root)

    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=lambda: wrd.FactoryActivityInFlight(False),
        ensure_base_ref_current=dispatch.ensure_base_ref_current,
        config_factory=lambda: cfg,
    )

    assert result.outcome == wrd.SYNC_CURRENT
    assert result.revision == new_tip
    assert git(mirror, "rev-parse", "main").stdout.strip() == new_tip
    assert git(repo, "rev-parse", "origin/main").stdout.strip() == new_tip
    assert git(repo, "rev-parse", "main").stdout.strip() == new_tip
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == new_tip

    status = worker_revision.describe_worker_revision_drift(state_path=record_path, repo_root=repo)
    assert status.drifted is True
    assert status.commits_behind == 1
    assert status.main_ref == dispatch.DEFAULT_BASE_REF

    from test_worker_checkout_drift_response import FakeSupervisor

    def unreachable_advance(**_kwargs):
        raise AssertionError("checkout is confirmed current; must not advance")

    supervisor = FakeSupervisor()
    notes: list[str] = []
    response = wrd.drift_response.respond_to_worker_revision_drift(
        status,
        checkout_root=repo,
        source_repo_root=mirror,
        factory_activity_in_flight=lambda: False,
        supervisor=supervisor,
        halt_dispatch=notes.append,
        advance=unreachable_advance,
    )

    assert response.action == wrd.drift_response.RESTARTED
    assert supervisor.recycled == 1
    assert notes == []


def test_bring_base_ref_current_skips_when_in_flight_and_leaves_every_ref_untouched(
    tmp_path, monkeypatch
):
    _redirect_base_ref_state(monkeypatch, tmp_path)
    repo, mirror, canonical = create_chain_repo_mirror_canonical(tmp_path)
    old_tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    push_new_commit_to_canonical(tmp_path, canonical)

    workdir_root = tmp_path / "workdir-root"
    workdir_root.mkdir()
    cfg = dispatch.Config(repo_root=repo, remote=str(canonical), base_ref="main", workdir_root=workdir_root)

    def unreachable_ensure(*_a, **_k):
        raise AssertionError("must not run ensure_base_ref_current while something is in flight")

    result = wrd.bring_base_ref_current(
        factory_activity_in_flight=lambda: wrd.FactoryActivityInFlight(
            True, "dispatch run in flight"
        ),
        ensure_base_ref_current=unreachable_ensure,
        config_factory=lambda: cfg,
    )

    assert result.outcome == wrd.SYNC_SKIPPED
    assert git(mirror, "rev-parse", "main").stdout.strip() == old_tip
    assert git(repo, "rev-parse", "origin/main").stdout.strip() == old_tip
    assert git(repo, "rev-parse", "main").stdout.strip() == old_tip
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == old_tip


# ---------------------------------------------------------------------------
# dev.finding 5170b3f9a AC-5: the 22:00Z shape reproduced -- a concurrent clone's refusal is
# UNKNOWN, never CLEAR, even though local main (what the old, un-synced activity compared
# against) still coincidentally equals the worker's recorded revision. The second half
# demonstrates the defect itself: bypassing the sync step on the identical repositories reads
# CLEAR, exactly what the real 22:07Z/22:22Z drift runs reported.
# ---------------------------------------------------------------------------


def test_a_concurrent_clone_refusal_reports_unknown_never_clear_and_demonstrates_the_defect(
    tmp_path, monkeypatch
):
    _redirect_base_ref_state(monkeypatch, tmp_path)
    monkeypatch.setattr(wrd, "default_dispatch_schedule_pause_state", lambda: (False, ""))

    repo, mirror, canonical = create_chain_repo_mirror_canonical(tmp_path)
    old_tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    record_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=repo, state_path=record_path, git_runner=dispatch.run, pid=os.getpid()
    )

    new_tip = push_new_commit_to_canonical(tmp_path, canonical)

    # The mirror already synced from canonical (as an earlier drift tick would have), and the
    # checkout's own tracking ref already caught up -- but local main and HEAD, and the
    # worker-revision record, all still sit at the old tip: the exact 22:00Z shape (a concurrent
    # clone defers the fast-forward while the tracking ref already reads current).
    git(mirror, "fetch", "origin", "main")
    git(mirror, "branch", "-f", "main", new_tip)
    git(repo, "fetch", "origin", "main")

    assert git(repo, "rev-parse", "main").stdout.strip() == old_tip
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == old_tip
    assert git(repo, "rev-parse", "origin/main").stdout.strip() == new_tip

    store = FakeWorkerRevisionDriftStore()
    monkeypatch.setattr(wrd, "default_store", lambda: store)
    monkeypatch.setattr(
        wrd.worker_revision,
        "describe_worker_revision_drift",
        functools.partial(
            worker_revision.describe_worker_revision_drift,
            state_path=record_path,
            repo_root=repo,
        ),
    )

    refused_status = dispatch.BaseRefStatus(
        base_ref="main",
        local_rev=old_tip,
        upstream_ref="origin/main",
        upstream_rev=new_tip,
        local_only=0,
        upstream_only=1,
    )

    def refusing_ensure(cfg, *, force_tracking_refresh=False):
        raise dispatch.BaseRefStaleError(refused_status)

    workdir_root = tmp_path / "workdir-root"
    workdir_root.mkdir()
    cfg = dispatch.Config(repo_root=repo, remote=str(canonical), base_ref="main", workdir_root=workdir_root)

    monkeypatch.setattr(
        wrd,
        "default_bring_base_ref_current",
        lambda: wrd.bring_base_ref_current(
            factory_activity_in_flight=lambda: False,
            ensure_base_ref_current=refusing_ensure,
            config_factory=lambda: cfg,
        ),
    )

    result = wrd.report_worker_revision_drift_activity()

    assert result["condition"] == wrd.UNKNOWN
    assert result["condition"] != wrd.CLEAR
    assert "upstream_only=1" in result["base_ref_sync"]["detail"]

    # The defect itself: bypass the sync step (SYNC_SKIPPED with no detail -- exactly what this
    # activity did before this bead existed) against the SAME repositories, where local main
    # still has no idea the tracking ref moved.
    monkeypatch.setattr(
        wrd, "default_bring_base_ref_current", lambda: wrd.BaseRefSyncResult(wrd.SYNC_SKIPPED)
    )

    defect_result = wrd.report_worker_revision_drift_activity()

    assert defect_result["condition"] == wrd.CLEAR
