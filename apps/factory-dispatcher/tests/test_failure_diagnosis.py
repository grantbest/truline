"""A dev.task entering `failed` must announce itself and carry a diagnosis.

Before failure_diagnosis.py existed, a bead that exhausted its retries wrote
its failure to bead notes and the run just ended — nothing told anyone, and
nothing gathered the declared-verification output, the failure class, the
worker/main revision gap, or the preserved patch path into one place. These
tests cover the three things the task asked for: the alert fires through the
existing, policy-gated alert inventory (not a bespoke send); the diagnosis
names a worker/main revision gap when one exists; and disposition (requeue /
supersede / dispatch) is never touched by any of this — that stays a human
decision.

No Discord, no substrate, no network, no Temporal server: every alert
delivery and every bead write below is a plain Python fake.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import failure_diagnosis  # noqa: E402
import retry_policy  # noqa: E402
from worker_revision import WorkerRevisionStatus  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeSubstrate:
    """Minimal BeadStore double: just enough for fail_task's write path."""

    def __init__(self, task):
        self.task = task
        self.existing_notes = []
        self.notes = []
        self.states = []

    def list_notes(self, task_id):
        return list(self.existing_notes)

    def add_note(self, task_id, kind, body, created_by, **kwargs):
        self.notes.append({"content": {"kind": kind, "body": body}, "created_by": created_by})

    def set_state(self, task_id, state, created_by):
        self.states.append((task_id, state, created_by))
        self.task["state"] = state


class RecordingPolicy:
    """Stands in for notify.AlertPolicy: records what would have posted."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(
        self,
        kind,
        fingerprint,
        content,
        *,
        severity=None,
        min_interval_hours=0.0,
        re_alert_interval_hours=0.0,
        members=None,
    ):
        self.sent.append((kind, {"fingerprint": fingerprint, "content": content}))
        return True


class DispositionGuard:
    """Fails the test the moment anything tries to dispose of a failed bead.

    Disposition (requeue / supersede / dispatching a new run) must stay a
    human decision — see the task's own "SCOPE IS ANNOUNCE AND DIAGNOSE, NOT
    DECIDE" boundary. Wired in place of the real requeue/supersede/dispatch
    entry points so any call from the code under test fails loudly instead
    of quietly succeeding.
    """

    def __init__(self):
        self.calls: list[str] = []

    def requeue(self, *_a, **_k):
        self.calls.append("requeue")
        raise AssertionError("failure announcement/diagnosis must never requeue a task")

    def supersede(self, *_a, **_k):
        self.calls.append("supersede")
        raise AssertionError("failure announcement/diagnosis must never supersede a task")

    def dispatch(self, *_a, **_k):
        self.calls.append("dispatch")
        raise AssertionError("failure announcement/diagnosis must never dispatch new work")


def _task(bead_id="dev-task-1", title="Fix the thing"):
    return {"id": bead_id, "state": "doing", "content": {"title": title}}


# ---------------------------------------------------------------------------
# announce: the declared alert fires, carrying bead id and failure class
# ---------------------------------------------------------------------------


def test_announce_failed_task_raises_declared_alert_with_bead_id_and_failure_class():
    policy = RecordingPolicy()
    task = _task(bead_id="d1fbc00b", title="OPS-41 repro")

    posted = failure_diagnosis.announce_failed_task(task, "work", policy=policy)

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "dev_task_failed:d1fbc00b"
    assert "d1fbc00b" in payload["content"]
    assert "work" in payload["content"]


def test_announce_failed_task_uses_the_declared_inventory_entry():
    """The alert is a real ALERT_INVENTORY entry, not a bespoke send.

    get_alert_definition raises KeyError for anything not registered — this
    fails loudly if DEV_TASK_FAILED_ALERT_ID is ever removed from the
    inventory out from under this module.
    """
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.DEV_TASK_FAILED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_failed_task_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    # Must not raise: a bad alert path must never crash the dispatcher, and
    # must never look like the bead's own transition failed.
    result = failure_diagnosis.announce_failed_task(_task(), "work", policy=ExplodingPolicy())
    assert result is False


# ---------------------------------------------------------------------------
# bounded: repeated identical failures do not produce an unbounded stream
# ---------------------------------------------------------------------------


def test_repeated_identical_failure_is_suppressed_after_the_first_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    task = _task()

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = failure_diagnosis.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = failure_diagnosis.announce_failed_task(task, "work", policy=policy)
    second = failure_diagnosis.announce_failed_task(task, "work", policy=policy)

    assert first is True
    assert second is False
    assert fake_post.calls == 1


# ---------------------------------------------------------------------------
# diagnose: worker/main revision gap is named when one exists
# ---------------------------------------------------------------------------


def test_diagnosis_names_the_commit_gap_when_worker_is_behind_main():
    status = WorkerRevisionStatus(
        worker_revision="76da5019aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        worker_started_at="2026-08-30T16:45:00+00:00",
        main_ref="main",
        main_revision="c049be5bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        is_ancestor=True,
        commits_behind=9,
    )

    diagnosis = failure_diagnosis.build_failure_diagnosis(
        "d1fbc00b", "work", revision_status=status
    )

    rendered = diagnosis.render()
    assert "9 commit" in rendered
    assert "76da5019" in rendered
    assert "c049be5b" in rendered
    assert diagnosis.commits_behind == 9


def test_diagnosis_reports_current_when_worker_matches_main():
    status = WorkerRevisionStatus(
        worker_revision="abc123",
        worker_started_at="2026-08-30T16:45:00+00:00",
        main_ref="main",
        main_revision="abc123",
        is_ancestor=True,
        commits_behind=0,
    )

    rendered = failure_diagnosis.build_failure_diagnosis(
        "bead-1", "environment", revision_status=status
    ).render()

    assert "matching" in rendered
    assert "9 commit" not in rendered


def test_diagnosis_reports_could_not_determine_when_no_record_exists():
    status = WorkerRevisionStatus.could_not_determine("main", "no worker revision record found")

    rendered = failure_diagnosis.build_failure_diagnosis(
        "bead-1", "work", revision_status=status
    ).render()

    assert "unavailable" in rendered


def test_diagnosis_carries_the_preserved_patch_path():
    diagnosis = failure_diagnosis.build_failure_diagnosis(
        "bead-1",
        "work",
        patch_path="/home/grant/.factory-dispatcher/failed/bead-1-123.patch",
        revision_status=WorkerRevisionStatus.could_not_determine("main", "no record"),
    )
    assert "bead-1-123.patch" in diagnosis.render()

    without_patch = failure_diagnosis.build_failure_diagnosis(
        "bead-1",
        "work",
        revision_status=WorkerRevisionStatus.could_not_determine("main", "no record"),
    )
    assert "No worker patch preserved" in without_patch.render()


# ---------------------------------------------------------------------------
# integration: dispatch.fail_task, on retry exhaustion, both diagnoses and
# announces — and never touches disposition
# ---------------------------------------------------------------------------


def test_fail_task_on_retry_exhaustion_writes_diagnosis_and_announces(monkeypatch):
    fixed_status = WorkerRevisionStatus(
        worker_revision="76da5019",
        worker_started_at="2026-08-30T16:45:00+00:00",
        main_ref="main",
        main_revision="c049be5b",
        is_ancestor=True,
        commits_behind=9,
    )
    monkeypatch.setattr(
        failure_diagnosis.worker_revision,
        "describe_worker_revision_drift",
        lambda: fixed_status,
    )

    announced: list[tuple[str, str]] = []
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_failed_task",
        lambda task, failure_class, **_k: announced.append((task["id"], failure_class)) or True,
    )

    guard = DispositionGuard()
    monkeypatch.setattr(dispatch, "requeue_task", guard.requeue)
    monkeypatch.setattr(dispatch, "backfill_superseded_task", guard.supersede)
    monkeypatch.setattr(dispatch, "dispatch_once", guard.dispatch)

    task = _task(bead_id="d1fbc00b")
    sub = FakeSubstrate(task)
    # Two prior "Run failed:" notes of this bead's own history exhaust the
    # retry budget (DISPATCH_RETRY_MAXIMUM_ATTEMPTS=3) on this, its third.
    sub.existing_notes = [
        {"content": {"kind": "status", "body": "Run failed: boom 1"}, "created_at": "2026-08-28T00:00:00Z"},
        {"content": {"kind": "status", "body": "Run failed: boom 2"}, "created_at": "2026-08-29T00:00:00Z"},
    ]

    dispatch.fail_task(
        sub,
        task,
        "declared verification did not pass:\nFailed: pytest (exit 1)",
        classification=retry_policy.WORK_FAILURE,
        patch_path="/home/grant/.factory-dispatcher/failed/d1fbc00b-1.patch",
    )

    assert sub.states == [("d1fbc00b", "failed", dispatch.CREATED_BY)]
    assert announced == [("d1fbc00b", "work")]
    assert guard.calls == []

    [note] = sub.notes
    body = note["content"]["body"]
    assert "Failure diagnosis:" in body
    assert "Failure class: work." in body
    assert "9 commit" in body
    assert "d1fbc00b-1.patch" in body


def test_fail_task_retry_eligible_failure_does_not_diagnose_or_announce(monkeypatch):
    """An ordinary retry-eligible failure returns to pending, unannounced.

    The diagnosis and alert are specifically about a bead leaving the retry
    chain — an attempt that still has budget left goes straight back to
    pending, and the next attempt already sees this same note via
    guards.prior_failures without needing a revision/patch diagnosis.
    """
    announced: list[tuple[str, str]] = []
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_failed_task",
        lambda task, failure_class, **_k: announced.append((task["id"], failure_class)) or True,
    )

    task = _task()
    sub = FakeSubstrate(task)

    dispatch.fail_task(
        sub,
        task,
        "worker exited 1: boom",
        classification=retry_policy.WORK_FAILURE,
    )

    assert sub.states == [(task["id"], "pending", dispatch.CREATED_BY)]
    assert announced == []
    [note] = sub.notes
    assert "Failure diagnosis:" not in note["content"]["body"]


def test_record_already_satisfied_work_diagnoses_and_announces(monkeypatch):
    monkeypatch.setattr(
        failure_diagnosis.worker_revision,
        "describe_worker_revision_drift",
        lambda: WorkerRevisionStatus.could_not_determine("main", "no record"),
    )
    announced: list[tuple[str, str]] = []
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_failed_task",
        lambda task, failure_class, **_k: announced.append((task["id"], failure_class)) or True,
    )
    guard = DispositionGuard()
    monkeypatch.setattr(dispatch, "requeue_task", guard.requeue)
    monkeypatch.setattr(dispatch, "backfill_superseded_task", guard.supersede)
    monkeypatch.setattr(dispatch, "dispatch_once", guard.dispatch)

    task = _task(bead_id="bead-satisfied")
    sub = FakeSubstrate(task)

    dispatch.record_already_satisfied_work(sub, task, "already-satisfied work: nothing to do")

    assert sub.states == [("bead-satisfied", "failed", dispatch.CREATED_BY)]
    assert announced == [("bead-satisfied", retry_policy.ALREADY_SATISFIED_FAILURE.name)]
    assert guard.calls == []
    [note] = sub.notes
    assert "Failure diagnosis:" in note["content"]["body"]


# ---------------------------------------------------------------------------
# queue-level silent stops: breaker latch and capacity pause each raise a
# declared alert (OPS-46 covered dev.task -> failed; this covers the states
# that wedge the queue WITHOUT ever failing a bead).
# ---------------------------------------------------------------------------


def test_announce_environmental_fault_breaker_latched_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_environmental_fault_breaker_latched(
        "task-1", "sig-abc", 3, 3, policy=policy
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "environmental_fault_breaker_latched:task-1"
    assert "task-1" in payload["content"]
    assert "sig-abc" in payload["content"]


def test_announce_environmental_fault_breaker_latched_uses_the_declared_inventory_entry():
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.ENVIRONMENTAL_FAULT_BREAKER_LATCHED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_environmental_fault_breaker_latched_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_environmental_fault_breaker_latched(
        "task-1", "sig-abc", 3, 3, policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_capacity_pause_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_capacity_pause(
        "task-1", "worker reported usage limit exhaustion", "Temporal schedule paused.",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "capacity_pause"
    assert "usage limit exhaustion" in payload["content"]
    assert "Temporal schedule paused" in payload["content"]


def test_announce_capacity_pause_uses_the_declared_inventory_entry():
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.CAPACITY_PAUSE_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_capacity_pause_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_capacity_pause(
        "task-1", "capacity exhausted", "pause failed", policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_capacity_resume_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_capacity_resume(
        "factory dispatcher resumed: capacity-pause resume responder; live probe succeeded",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "capacity_resume"
    assert "capacity-pause resume responder" in payload["content"]


def test_announce_capacity_resume_uses_the_declared_inventory_entry():
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.CAPACITY_RESUME_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_capacity_resume_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_capacity_resume(
        "resumed", policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_worker_revision_drift_escalated_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_worker_revision_drift_escalated(
        "This is the 4th consecutive deferral of the same worker-revision-drift condition "
        "(bound: 3); escalating through the declared alert policy.",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "worker_revision_drift_escalated"
    assert "4th consecutive deferral" in payload["content"]


def test_announce_worker_revision_drift_escalated_uses_the_declared_inventory_entry():
    """get_alert_definition raises KeyError for anything not registered -- this fails loudly
    if WORKER_REVISION_DRIFT_ESCALATED_ALERT_ID is ever removed from the inventory out from
    under this module.

    2026-09-13/14 (f51e057c): a prior attempt shaped `announce_worker_revision_drift_escalated`
    correctly and had `activities.worker_revision_drift._escalate_persistent_deferral` call it,
    but never registered the alert id in `notify.ALERT_INVENTORY` -- `send_alert`'s own
    `get_alert_definition` lookup then raised `KeyError`, caught by this function's blanket
    `except Exception`, so the escalation silently posted nothing. A test that only monkeypatches
    `announce_worker_revision_drift_escalated` (as `tests/test_worker_revision_drift_schedule.py`
    does, to prove the activity *calls* it) cannot catch that: it proves the call, never the
    delivery. This test proves the delivery is even possible.
    """
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.WORKER_REVISION_DRIFT_ESCALATED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_announce_worker_revision_drift_escalated_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_worker_revision_drift_escalated_fingerprint_is_stable_across_repeated_firings():
    """f51e057c F-D: `note` embeds the per-tick consecutive-deferral counter
    (`activities.worker_revision_drift._escalate_persistent_deferral` writes "This is the Nth
    consecutive deferral..."), so fingerprinting `note` itself produced a distinct fingerprint on
    every one of 5 reproduced firings for what is the SAME open condition -- defeating
    `re_alert_interval_hours` entirely and posting one ACTIONABLE alert every 15 minutes with a
    run in flight (OPS-69's shape). The fingerprint must instead be a stable identity (the
    observation ref plus the drifted revision) that does not change while the *same* condition
    keeps recurring, even though the note's counter does.
    """
    policy = RecordingPolicy()

    for n in range(4, 9):  # 5 firings, same drifted revision, a different counter each time
        failure_diagnosis.announce_worker_revision_drift_escalated(
            f"This is the {n}th consecutive deferral of the same worker-revision-drift "
            "condition (bound: 3); escalating through the declared alert policy.",
            revision="ffde7c8f",
            policy=policy,
        )

    assert len(policy.sent) == 5
    fingerprints = {payload["fingerprint"] for _kind, payload in policy.sent}
    assert len(fingerprints) == 1


def test_announce_worker_revision_drift_escalated_fingerprint_differs_for_a_different_revision():
    """A genuinely new drift (a different recorded revision) must not inherit the previous
    condition's suppression -- only the SAME revision's repeated escalation should collapse to
    one fingerprint."""
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_escalated(
        "This is the 4th consecutive deferral...", revision="ffde7c8f", policy=policy
    )
    failure_diagnosis.announce_worker_revision_drift_escalated(
        "This is the 4th consecutive deferral...", revision="a17b2f7a", policy=policy
    )

    fingerprints = [payload["fingerprint"] for _kind, payload in policy.sent]
    assert fingerprints[0] != fingerprints[1]


# ---------------------------------------------------------------------------
# dev.finding 639a20c5 AC-4b: the alert tells the truth in both cases -- escalating no longer
# always pauses dispatch, so the content must say which actually happened.
# ---------------------------------------------------------------------------


def test_announce_worker_revision_drift_escalated_defaults_to_the_paused_wording():
    """`dispatch_paused` defaults to True, preserving this alert's original wording for any
    caller that does not pass it explicitly."""
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", revision="ffde7c8f", policy=policy
    )

    content = policy.sent[0][1]["content"]
    assert content.startswith("Dispatch queue paused:")


def test_announce_worker_revision_drift_escalated_states_the_pause_when_true():
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", revision="ffde7c8f", dispatch_paused=True, policy=policy
    )

    content = policy.sent[0][1]["content"]
    assert content.startswith("Dispatch queue paused:")
    assert not content.startswith("Dispatch left running:")


def test_announce_worker_revision_drift_escalated_states_dispatch_was_left_running_when_false():
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", revision="ffde7c8f", dispatch_paused=False, policy=policy
    )

    content = policy.sent[0][1]["content"]
    assert content.startswith(
        "Dispatch left running: worker-revision-drift deferral escalated; the blocker was a "
        "non-dispatch schedule."
    )
    assert not content.startswith("Dispatch queue paused:")


def test_announce_worker_revision_drift_escalated_fingerprint_does_not_depend_on_dispatch_paused():
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", revision="ffde7c8f", dispatch_paused=True, policy=policy
    )
    failure_diagnosis.announce_worker_revision_drift_escalated(
        "escalating", revision="ffde7c8f", dispatch_paused=False, policy=policy
    )

    fingerprints = [payload["fingerprint"] for _kind, payload in policy.sent]
    assert fingerprints[0] == fingerprints[1]


def test_announce_worker_revision_drift_escalated_resolved_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_worker_revision_drift_escalated_resolved(
        "factory dispatcher resumed: worker-revision-drift condition cleared "
        "(was paused: escalating through the declared alert policy)",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "worker_revision_drift_escalated_resolved"
    assert "condition cleared" in payload["content"]


def test_announce_worker_revision_drift_escalated_resolved_uses_the_declared_inventory_entry():
    """get_alert_definition raises KeyError for anything not registered -- proves the alert can
    actually be delivered, not merely that the announce function was called (2026-09-13/14:
    a prior escalation alert was shaped correctly and called but never registered, so
    `send_alert`'s inventory lookup raised and the call was silently swallowed)."""
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.WORKER_REVISION_DRIFT_ESCALATED_RESOLVED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is failure_diagnosis.notify.AlertSeverity.INFORMATIONAL
    assert definition.has_next_step


def test_announce_worker_revision_drift_escalated_resolved_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_worker_revision_drift_escalated_resolved(
        "resolved", policy=ExplodingPolicy()
    )
    assert result is False


# ---------------------------------------------------------------------------
# dev.finding 9a10f2aa: a drift-escalation pause is never resumed and never announced when the
# condition becomes `unknown`. This alert is the missing announcement -- staying paused is
# correct (PRIN-015 fail closed); the silence was the defect.
# ---------------------------------------------------------------------------


def test_announce_worker_revision_drift_unknown_while_paused_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(
        "factory dispatcher paused: worker checkout drifted from main and could not be advanced "
        "safely; 4th consecutive deferral",
        error="no worker revision record found",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "worker_revision_drift_unknown_while_paused"
    assert "no worker revision record found" in payload["content"]


def test_announce_worker_revision_drift_unknown_while_paused_uses_the_declared_inventory_entry():
    """get_alert_definition raises KeyError for anything not registered -- proves the alert can
    actually be delivered, not merely that the announce function was called. #849's predecessor
    bead shipped a call with no matching inventory entry and lost two gate cycles to it; this is
    the check that would have caught it."""
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.WORKER_REVISION_DRIFT_UNKNOWN_WHILE_PAUSED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_announce_worker_revision_drift_unknown_while_paused_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(
        "paused", error="unresolvable main", policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_worker_revision_drift_unknown_while_paused_fingerprint_is_stable_across_repeated_firings():
    """Mirrors `test_announce_worker_revision_drift_escalated_fingerprint_is_stable_across_
    repeated_firings` exactly, and for the same reason: the predecessor bead shipped a
    per-tick-changing fingerprint once already (f51e057c F-D) and was sent back for it. Here the
    pause note stays byte-identical across every tick of the same stuck pause (nothing rewrites
    it while the condition stays unknown -- see `failure_diagnosis.py`'s own docstring for why),
    but the underlying `error` text is allowed to vary slightly tick to tick without splitting
    the fingerprint.
    """
    policy = RecordingPolicy()
    note = (
        "factory dispatcher paused: worker checkout drifted from main and could not be advanced "
        "safely; 4th consecutive deferral"
    )

    for n in range(5):  # 5 firings, same pause note, a slightly different error each time
        failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(
            note, error=f"git rev-parse failed (attempt {n})", policy=policy
        )

    assert len(policy.sent) == 5
    fingerprints = {payload["fingerprint"] for _kind, payload in policy.sent}
    assert len(fingerprints) == 1


def test_announce_worker_revision_drift_unknown_while_paused_fingerprint_differs_for_a_different_pause():
    """A genuinely different pause (a fresh escalation, with its own note) must not inherit the
    previous pause's suppression -- only the SAME pause's repeated announcement should collapse
    to one fingerprint."""
    policy = RecordingPolicy()

    failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(
        "factory dispatcher paused: worker checkout drifted from main and could not be advanced "
        "safely; 4th consecutive deferral",
        error="no worker revision record found",
        policy=policy,
    )
    failure_diagnosis.announce_worker_revision_drift_unknown_while_paused(
        "factory dispatcher paused: worker checkout drifted from main and could not be advanced "
        "safely; 9th consecutive deferral",
        error="no worker revision record found",
        policy=policy,
    )

    fingerprints = [payload["fingerprint"] for _kind, payload in policy.sent]
    assert fingerprints[0] != fingerprints[1]


# ---------------------------------------------------------------------------
# dedup: per-subject with a declared re-alert interval, not suppress-forever
# ---------------------------------------------------------------------------


def test_repeated_identical_failure_re_alerts_after_the_declared_interval(tmp_path, monkeypatch):
    """The #593 delivered-vs-seen shape this task cites: a subject re-failing
    identically after the declared interval must re-alert, not inherit an
    old suppression forever."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    task = _task()

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = failure_diagnosis.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = failure_diagnosis.announce_failed_task(task, "work", policy=policy)

    # Simulate the interval having elapsed since the first post by rewriting
    # the host-local state file's timestamp directly — no sleep, no clock
    # injection needed for a plain file read/modify/write.
    import json
    from datetime import datetime, timedelta, timezone

    # +1h margin: the comparison this exercises reads a UTC-stamped
    # last_posted_at against a UTC "now" (AlertPolicy._re_alert_due always
    # compares in UTC — see notify._utc_now/_parse_stored_timestamp), so a
    # small margin past the declared interval is enough regardless of the
    # host's local timezone.
    state_path = failure_diagnosis._alert_state_path()
    doc = json.loads(state_path.read_text())
    stale = datetime.now(timezone.utc) - timedelta(
        hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS + 1
    )
    for entry in doc.values():
        entry["last_posted_at"] = stale.isoformat()
    state_path.write_text(json.dumps(doc))

    second = failure_diagnosis.announce_failed_task(task, "work", policy=policy)

    assert first is True
    assert second is True
    assert fake_post.calls == 2


def test_unrelated_bead_never_suppresses_another_beads_alert(tmp_path, monkeypatch):
    """Per-subject dedup: the host-local alert-state file keys by kind (which
    is bead-scoped for dev_task_failed), so one bead alerting must never
    suppress an unrelated bead's alert of the same failure class."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = failure_diagnosis.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = failure_diagnosis.announce_failed_task(_task(bead_id="bead-a"), "work", policy=policy)
    second = failure_diagnosis.announce_failed_task(_task(bead_id="bead-b"), "work", policy=policy)

    assert first is True
    assert second is True
    assert fake_post.calls == 2


def test_announce_base_ref_needs_person_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_base_ref_needs_person(
        "main", "abc1234", "origin/main", "def5678",
        "3 local-only commit(s) not on origin/main; a fast-forward would lose them.",
        policy=policy,
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "base_ref_needs_person:main"
    assert "abc1234" in payload["content"]
    assert "def5678" in payload["content"]


def test_announce_base_ref_needs_person_uses_the_declared_inventory_entry():
    """The alert is a real ALERT_INVENTORY entry, not a bespoke send.

    FACT AT FILING (dev.finding 606a8e8d): get_alert_definition raised KeyError for
    this id — every call from dispatch.ensure_base_ref_current had always returned
    False, silently. This resolves against the real registry, not a monkeypatched
    announce function, because a mocked call proves the call happened and never
    proves delivery could ever succeed.
    """
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.BASE_REF_NEEDS_PERSON_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_base_ref_needs_person_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_base_ref_needs_person(
        "main", "abc1234", "origin/main", "def5678", "blocked", policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_concurrent_clone_defer_wedged_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_concurrent_clone_defer_wedged(
        ("worker-a", "worker-b"), 3, 3, policy=policy
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "concurrent_clone_defer_wedged"
    assert "worker-a" in payload["content"]
    assert "worker-b" in payload["content"]


def test_announce_concurrent_clone_defer_wedged_uses_the_declared_inventory_entry():
    """FACT AT FILING (dev.finding 606a8e8d): get_alert_definition raised KeyError
    for this id too — every call from dispatch._record_concurrent_clone_defer had
    always returned False. Resolved against the real registry, same reasoning as
    test_announce_base_ref_needs_person_uses_the_declared_inventory_entry above.
    """
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.CONCURRENT_CLONE_DEFER_WEDGED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_concurrent_clone_defer_wedged_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_concurrent_clone_defer_wedged(
        ("worker-a",), 3, 3, policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_schedule_wedged_raises_declared_alert():
    policy = RecordingPolicy()

    posted = failure_diagnosis.announce_schedule_wedged(
        "factory-dispatcher-2026-09-11T13:00:00Z", 48, 45, policy=policy
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "dispatch_schedule_wedged"
    assert "factory-dispatcher-2026-09-11T13:00:00Z" in payload["content"]
    assert "48m" in payload["content"]
    assert "45m" in payload["content"]


def test_announce_schedule_wedged_uses_the_declared_inventory_entry():
    definition = failure_diagnosis.notify.get_alert_definition(
        failure_diagnosis.SCHEDULE_WEDGED_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_schedule_wedged_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = failure_diagnosis.announce_schedule_wedged(
        "wf-1", 48, 45, policy=ExplodingPolicy()
    )
    assert result is False


def test_announce_schedule_wedged_does_not_re_alert_every_tick_for_the_same_workflow(
    tmp_path, monkeypatch
):
    """The dedup this bead requires: the SAME wedged workflow_id must not
    re-alert on every schedule_status.py tick while it stays stuck -- only
    after the declared re-alert interval, reusing the exact AlertPolicy dedup
    mechanism every sibling alert in this module already relies on."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = failure_diagnosis.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = failure_diagnosis.announce_schedule_wedged("wf-1", 48, 45, policy=policy)
    second = failure_diagnosis.announce_schedule_wedged("wf-1", 60, 45, policy=policy)

    assert first is True
    assert second is False  # unchanged fingerprint, interval not elapsed: suppressed
    assert fake_post.calls == 1


def test_announce_schedule_wedged_alerts_immediately_for_a_different_workflow_id(
    tmp_path, monkeypatch
):
    """A different workflow_id becoming wedged after the first one cleared is
    a genuinely new occurrence (changed fingerprint), not a re-alert of the
    same suppressed condition -- it must post immediately."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = failure_diagnosis.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = failure_diagnosis.announce_schedule_wedged("wf-1", 48, 45, policy=policy)
    second = failure_diagnosis.announce_schedule_wedged("wf-2", 46, 45, policy=policy)

    assert first is True
    assert second is True
    assert fake_post.calls == 2


# ---------------------------------------------------------------------------
# structural control: every *_ALERT_ID this module declares must resolve in
# the real ALERT_INVENTORY. The tests above assert this one-by-one for each
# alert this module raises today; this enumerates the module's own constants
# so a future announce_* added with a new *_ALERT_ID but no inventory entry
# fails here immediately, rather than staying silently dead the way
# BASE_REF_NEEDS_PERSON_ALERT_ID and CONCURRENT_CLONE_DEFER_WEDGED_ALERT_ID
# did (dev.finding 606a8e8d) until something noticed by hand.
# ---------------------------------------------------------------------------

_ALERT_ID_CONSTANT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_ALERT_ID$")

_DECLARED_ALERT_IDS = sorted(
    (name, value)
    for name, value in vars(failure_diagnosis).items()
    if _ALERT_ID_CONSTANT_PATTERN.match(name) and isinstance(value, str)
)


def test_declared_alert_id_enumeration_is_not_empty():
    """Guards the parametrized test below: a enumeration that silently collects
    zero ids passes vacuously and is indistinguishable from a real pass."""
    assert len(_DECLARED_ALERT_IDS) >= 6, _DECLARED_ALERT_IDS


@pytest.mark.parametrize(
    "name,alert_id", _DECLARED_ALERT_IDS, ids=[name for name, _ in _DECLARED_ALERT_IDS]
)
def test_every_declared_alert_id_constant_resolves_in_the_real_inventory(name, alert_id):
    definition = failure_diagnosis.notify.get_alert_definition(alert_id)
    assert not definition.is_removed, f"{name} ({alert_id}) is registered but removed"


def test_asyncio_run_is_used_synchronously_with_no_running_loop():
    """announce_failed_task must be callable from ordinary sync code.

    fail_task and friends are plain synchronous functions (called directly
    from dispatch_once and from Temporal's sync @activity.defn functions),
    so announce_failed_task must not assume an event loop is already
    running.
    """
    assert asyncio.get_event_loop_policy() is not None  # sanity: no loop required
    posted = failure_diagnosis.announce_failed_task(_task(), "work", policy=RecordingPolicy())
    assert posted is True


# ---------------------------------------------------------------------------
# OPS-99 follow-up (2026-09-16): the alert-state read-modify-write must survive concurrent
# writers, not just concurrent readers -- ACTIVITY_EXECUTOR_CONCURRENCY moved from 1 to 12, so
# `record_failure` (dispatch, via announce_failed_task) and `probe_capacity_pause_resume` (a
# reconciler, via announce_capacity_resume) -- previously mutually exclusive by accident of that
# same arithmetic -- can now both be mid-read-modify-write on ~/.factory-dispatcher/alert-state.json
# at once. Before the file lock, whichever writer finished last silently discarded the other's
# just-recorded fingerprint.
# ---------------------------------------------------------------------------


def test_concurrent_alert_posts_do_not_lose_a_committed_fingerprint(tmp_path, monkeypatch):
    """Many threads race `_record_dev_task_alert_posted` for DISTINCT alert kinds against the
    same state file -- standing in for `record_failure` and `probe_capacity_pause_resume`
    racing each other now that both can run at once. Without the lock added for this bead, this
    reliably lost at least one kind on a real filesystem; with it, every kind survives."""
    import threading

    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    kinds = [f"race_kind_{i}" for i in range(20)]
    errors: list[Exception] = []

    def post_one(kind: str) -> None:
        try:
            asyncio.run(
                failure_diagnosis._record_dev_task_alert_posted(kind, f"fp-{kind}", None, None)
            )
        except Exception as exc:  # pragma: no cover - surfaced via `errors` below
            errors.append(exc)

    threads = [threading.Thread(target=post_one, args=(kind,)) for kind in kinds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    doc = failure_diagnosis._read_alert_state_doc(failure_diagnosis._alert_state_path())
    assert set(doc) == set(kinds), (
        "a lost update: at least one concurrent writer's kind never made it into the shared "
        f"alert-state file. Missing: {sorted(set(kinds) - set(doc))}"
    )
    for kind in kinds:
        assert doc[kind]["fingerprint"] == f"fp-{kind}"
