"""Tests for Temporal worker registration."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import temporalio.bridge.worker as bridge_worker  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.service import ConnectConfig, _BridgeServiceClient  # noqa: E402

import worker  # noqa: E402
from activities import ACTIVITIES  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def test_reconcilers_are_not_bound_to_a_single_shared_activity_slot():
    # OPS-99/100: with exactly one slot shared by everything, an in-flight
    # ~80-minute dispatch `run` step starved every 15-minute reconciler for
    # as long as it ran (measured up to 646.7s). More than one slot means a
    # reconciler is never structurally forced to wait behind an in-flight
    # dispatch step just because both draw from the same single pool.
    assert worker.ACTIVITY_EXECUTOR_CONCURRENCY > 1


def test_at_most_one_dispatch_is_enforced_by_a_lock_not_by_executor_size(
    tmp_path, monkeypatch
):
    # AC: "never left to be inherited from a concurrency limit that also
    # governs the reconcilers." Widening ACTIVITY_EXECUTOR_CONCURRENCY above
    # must not, by itself, let two dispatch attempts run at once -- the run
    # lock is what enforces that, independent of how many executor slots
    # exist. This is also the one mechanism that reaches a manually
    # triggered `dispatch.py --once` run coinciding with the Temporal-
    # scheduled drain -- the race ScheduleOverlapPolicy.SKIP does not cover,
    # since SKIP only dedupes overlapping *scheduled* actions (FA-S26).
    monkeypatch.setenv(
        dispatch_steps.DISPATCH_RUN_LOCK_PATH_ENV,
        str(tmp_path / "dispatch-run.lock"),
    )

    first = dispatch_steps._acquire_dispatch_run_lock()
    assert first, "the first acquirer must succeed"
    try:
        second = dispatch_steps._acquire_dispatch_run_lock()
        assert not second, (
            "a second dispatch attempt (standing in for a manually triggered "
            "--once run racing the schedule) must be refused while the first "
            "is still in flight"
        )
        assert second.holder == str(os.getpid()), (
            "a refusal must name the pid holding the lock: a lock left held "
            "by an out-of-band end (terminate, or a lost workflow) otherwise "
            "presents as dispatch_in_flight with exit_code 0 and nothing "
            "named, which is a wedge that reports success"
        )
    finally:
        dispatch_steps._release_dispatch_run_lock()

    # Freed once released, so a later attempt (or a retried workflow) is
    # never wedged by a predecessor that already finished.
    assert dispatch_steps._acquire_dispatch_run_lock()
    dispatch_steps._release_dispatch_run_lock()


class FakeBridgeWorker:
    def initiate_shutdown(self):
        pass

    async def finalize_shutdown(self):
        pass


def test_build_worker_registers_real_sync_activities_without_temporal_server(
    monkeypatch,
):
    created = []

    def fake_create(*args):
        created.append(args)
        return FakeBridgeWorker()

    monkeypatch.setattr(bridge_worker.Worker, "create", fake_create)

    service_client = _BridgeServiceClient(ConnectConfig("temporal-test.invalid:7233"))
    service_client._bridge_client = object()
    client = Client(service_client, namespace="dev")

    async def construct_worker():
        activity_executor = worker.new_activity_executor()
        try:
            return worker.build_worker(client, activity_executor), activity_executor
        except Exception:
            activity_executor.shutdown(wait=True, cancel_futures=True)
            raise

    temporal_worker, activity_executor = asyncio.run(construct_worker())
    activity_executor.shutdown(wait=True, cancel_futures=True)

    assert temporal_worker is not None
    assert created
    # temporalio.worker.Worker exposes no public constructor-config accessor here.
    # _config is a private SDK dependency; if temporalio renames or removes it,
    # this test stops proving which activities, executor, and concurrency were
    # registered and must move to the SDK's replacement public surface.
    assert temporal_worker._config["activities"] == ACTIVITIES
    assert temporal_worker._config["activity_executor"] is activity_executor
    assert (
        temporal_worker._config["max_concurrent_activities"]
        == worker.ACTIVITY_EXECUTOR_CONCURRENCY
    )


class _ClaimReachedTheStore(Exception):
    """Raised by the store double the instant claim_activity gets past the lock."""


class _RefusesEveryBeadRead:
    """A store that fails the test if claim_activity touches a bead at all.

    Anything claim_activity could do to a bead begins with reading one, so a
    single tripwire here is enough to prove "claimed no bead" without
    enumerating the store's whole surface.
    """

    def __getattr__(self, name):
        def _tripwire(*args, **kwargs):
            raise _ClaimReachedTheStore(name)

        return _tripwire


def _isolate_lock(tmp_path, monkeypatch):
    monkeypatch.setenv(
        dispatch_steps.DISPATCH_RUN_LOCK_PATH_ENV,
        str(tmp_path / "dispatch-run.lock"),
    )
    monkeypatch.setattr(dispatch_steps, "default_store", _RefusesEveryBeadRead)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)


def test_claim_activity_refuses_and_touches_no_bead_while_the_lock_is_held(
    tmp_path, monkeypatch
):
    # The mechanism tests above drive the private _acquire_/_release_ helpers
    # directly, which leaves the thing that actually matters unpinned: that
    # the DISPATCH PIPELINE uses them. The gate on #753 proved the gap by
    # unwiring the lock from claim_activity and cleanup_activity entirely --
    # the invariant wholly unenforced on every real code path -- and the
    # suite still reported 1519 passed. This test closes that: it goes
    # through claim_activity, not through the helpers.
    _isolate_lock(tmp_path, monkeypatch)

    held = dispatch_steps._acquire_dispatch_run_lock()
    assert held, "precondition: the standing dispatch must hold the lock"
    try:
        result = dispatch_steps.claim_activity({})
    finally:
        dispatch_steps._release_dispatch_run_lock()

    assert result["status"] == "dispatch_in_flight"
    assert result["exit_code"] == 0
    # The point of the refusal: no bead was read, so none was claimed. The
    # store double raises on any access, so arriving here at all is the proof.
    assert result["holder"] == str(os.getpid()), (
        "the refusal must name the holder, so a lock left held by an "
        "out-of-band end is diagnosable instead of a silent stall"
    )


def test_cleanup_activity_releases_the_lock_so_the_next_claim_proceeds(
    tmp_path, monkeypatch
):
    # cleanup_activity is the release point for the one path claim_activity
    # does not release itself (a successful claim hands the lock off to it).
    # If that hand-off is not wired, the first successful dispatch wedges
    # every later one -- and no helper-level test can see it.
    _isolate_lock(tmp_path, monkeypatch)

    claimed = dispatch_steps._acquire_dispatch_run_lock()
    assert claimed, "precondition: stand in for the successful claim's lock"

    assert dispatch_steps.cleanup_activity({}) == {
        "status": "cleaned",
        "exit_code": 0,
    }

    # The lock is genuinely free again. Asserted directly rather than via
    # claim_activity, because a claim_activity that never TAKES the lock also
    # "proceeds" -- that version of this test passes against the very mutant
    # it is supposed to catch.
    regained = dispatch_steps._acquire_dispatch_run_lock()
    assert regained, (
        "cleanup_activity did not release the run lock, so the first "
        "successful dispatch wedges every later one"
    )
    dispatch_steps._release_dispatch_run_lock()

    # And the pipeline's own entry point now gets past the lock to the store,
    # which is exactly as far as this test wants it to go.
    try:
        dispatch_steps.claim_activity({})
    except _ClaimReachedTheStore:
        pass
    else:
        raise AssertionError(
            "claim_activity did not reach the store after cleanup_activity ran, "
            "so cleanup_activity did not release the run lock"
        )
    finally:
        dispatch_steps._release_dispatch_run_lock()
