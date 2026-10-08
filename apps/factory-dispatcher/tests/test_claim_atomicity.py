"""claim_activity's compare-and-set is not the whole claim.

2026-08-23 incident: claim_activity transitions a bead pending -> doing and
THEN writes the claim note. When the note write failed (substrate 422 on null
provenance during a schema skew), the bead was left in doing with no note: no
claim record, no failure record, no worker, and nothing that will ever move
it. Temporal then retried the workflow, and each retry selected the NEXT
runnable bead and did the same thing to it -- one failed dispatch stranded
three beads, because a bead in doing with no notes is indistinguishable from
live work on every view the platform has.

These tests drive activities.dispatch_steps.claim_activity directly against a
substrate double whose add_note raises, exactly as workflow_core would call it
-- no substrate, no cluster, no network.

Further down this file: the same incident shape one step later in the
pipeline. A Temporal retry that reclaims can claim a DIFFERENT bead than the
attempt before it; record_failure_activity must never let the workflow's own
attempt count decide whether THAT bead is retry-exhausted.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guards  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def make_task(task_id: str, created_at: str) -> dict:
    return {
        "id": task_id,
        "created_at": created_at,
        "state": "pending",
        "content": {
            "lane": "code-health",
            "title": f"task {task_id}",
            "intent": "do the work",
            "context_refs": [],
            "acceptance": ["a"],
            "verification": {"commands": []},
            "scope": {"paths": ["apps/factory-dispatcher/"], "forbidden_paths": []},
            "risk_class": "structural",
        },
    }


class FakeSubstrate:
    """A minimal BeadStore double: real compare-and-set, a scriptable add_note."""

    def __init__(self, tasks, note_failures: int = 0, fail_compensation: bool = False):
        self.tasks = tasks
        self.notes: list[tuple] = []
        self.transitions: list[tuple] = []
        self.note_calls = 0
        self.note_failures = note_failures
        self.fail_compensation = fail_compensation

    def list_tasks(self, state=None, limit=200):
        if state is None:
            return list(self.tasks)
        return [t for t in self.tasks if (t.get("state") or "pending") == state]

    def list_notes(self, parent_id, limit=500):
        return []

    def find_bead(self, namespace, type, content_ref):
        return None

    def list_beads(self, namespace, type, **params):
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return []

    def _task_by_id(self, task_id):
        for task in self.tasks:
            if task["id"] == task_id:
                return task
        raise KeyError(task_id)

    def transition_state(self, bead_id, from_state, to_state, created_by):
        if from_state == "doing" and to_state == "pending" and self.fail_compensation:
            raise RuntimeError("substrate unreachable: compensation failed")
        task = self._task_by_id(bead_id)
        current = task.get("state") or "pending"
        if current != from_state:
            raise RuntimeError(f"bead is {current}, not {from_state}")
        self.transitions.append((bead_id, from_state, to_state))
        task["state"] = to_state
        return task

    def add_note(
        self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
    ):
        self.note_calls += 1
        if self.note_calls <= self.note_failures:
            raise RuntimeError("substrate 422: null provenance during schema skew")
        args = (parent_id, kind, body, created_by)
        kwargs = dict(extra)
        if provenance is not None:
            kwargs["provenance"] = provenance
        self.notes.append((args, kwargs))
        return {"id": f"note-{self.note_calls}"}

    def patch_content(self, bead_id, content, created_by):
        raise AssertionError("claim_activity must not patch content")

    def set_state(self, bead_id, state, created_by):
        raise AssertionError("claim_activity must not call set_state directly")

    def add_link(self, source_id, target_id, link_type, created_by):
        raise AssertionError("claim_activity does not add links")


def _claim(sub, monkeypatch, request=None):
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    return dispatch_steps.claim_activity(request or {})


def test_claim_note_write_failure_does_not_strand_the_bead_in_doing(monkeypatch):
    """TDD driver: fails today, leaving exactly the stranded bead the incident
    describes. Passes once the note write is compensated."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task_a], note_failures=1)

    result = _claim(sub, monkeypatch)

    assert result["status"] == "claim_failed"
    assert result["task_id"] == "bead-a"
    assert task_a["state"] == "pending", "note write failed; the transition must not stick"
    assert sub.transitions == [
        ("bead-a", "pending", "doing"),
        ("bead-a", "doing", "pending"),
    ]


def test_retry_after_failed_claim_reclaims_the_same_bead_not_a_different_one(monkeypatch):
    """The retry shape from the incident: Temporal re-invokes claim with the
    same (task-id-less) request. Only one bead may ever be touched across both
    attempts -- the retry must resume/release the bead it already claimed,
    never claim a fresh one out from under it."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    task_b = make_task("bead-b", "2026-08-23T00:05:00Z")
    sub = FakeSubstrate([task_a, task_b], note_failures=1)

    first = _claim(sub, monkeypatch)
    assert first["status"] == "claim_failed"
    assert first["task_id"] == "bead-a"

    second = _claim(sub, monkeypatch)

    assert second["status"] == "claimed"
    assert second["task"]["id"] == "bead-a"
    assert task_b["state"] == "pending"
    assert all(bead_id != "bead-b" for bead_id, *_ in sub.transitions)


def test_double_failure_is_reported_not_silently_stranded(monkeypatch):
    """When compensation itself also fails, the bead really is stuck in doing
    with no note -- claim_activity must say so plainly rather than returning a
    result indistinguishable from an ordinary claim failure."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task_a], note_failures=1, fail_compensation=True)

    result = _claim(sub, monkeypatch)

    assert result["status"] == "claim_failed"
    assert task_a["state"] == "doing"
    assert "stranded" in result["message"]
    assert not guards.has_claim_note(sub.list_notes("bead-a"))


def test_claim_note_success_path_is_unaffected(monkeypatch):
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task_a])

    result = _claim(sub, monkeypatch)

    assert result["status"] == "claimed"
    assert task_a["state"] == "doing"
    assert len(sub.notes) == 1
    body = sub.notes[0][0][2]
    assert body.startswith(guards.CLAIM_NOTE_PREFIX)


# ---------------------------------------------------------------------------
# retry attribution: a Temporal workflow retry's attempt count is the
# WORKFLOW's, not any particular bead's -- a retried workflow re-runs claim
# and can pick a different pending bead each time (same shape as the
# 2026-08-23 incident above, one step further down the pipeline). fail_task
# must derive exhaustion solely from the bead it is actually failing, never
# from what a differently-claimed earlier attempt already spent.
# ---------------------------------------------------------------------------


class RecordingSubstrate:
    """A BeadStore double that threads real notes/state per bead, for
    record_failure_activity -- unlike FakeSubstrate above, which is scoped to
    claim_activity and stubs list_notes to `[]`."""

    def __init__(self, tasks):
        self.tasks = {t["id"]: t for t in tasks}
        self.notes: dict[str, list[dict]] = {t["id"]: [] for t in tasks}
        self._note_seq = 0

    def list_notes(self, parent_id, limit=500):
        return list(self.notes.get(parent_id, []))

    def add_note(self, task_id, kind, body, created_by, **kwargs):
        self._note_seq += 1
        self.notes.setdefault(task_id, []).append(
            {
                "content": {"kind": kind, "body": body},
                "created_at": f"2026-08-23T00:{self._note_seq:02d}:00Z",
            }
        )
        return {"id": f"note-{self._note_seq}"}

    def set_state(self, task_id, state, created_by):
        self.tasks[task_id]["state"] = state

    def patch_content(self, bead_id, content, created_by):
        raise AssertionError("record_failure_activity must not patch content")

    def add_link(self, source_id, target_id, link_type, created_by):
        raise AssertionError("record_failure_activity does not add links")


def _attempt_failure_state(task: dict, attempt: int, maximum_attempts: int = 3) -> dict:
    """The state workflow_core hands to record_failure for one failed
    attempt -- carries the WORKFLOW's retry_attempt/retry_maximum_attempts
    for observability, and (since the fix) nothing that lets that number
    override which bead it is actually about."""
    return {
        "task": task,
        "worker": {"name": "codex"},
        "run_started": time.time() - 4,
        "failure_reason": "worker exited 1: pytest failed",
        "worker_result": {
            "exit_code": 1,
            "stdout": "pytest failed",
            "duration_s": 4.0,
            "timed_out": False,
        },
        "retry_attempt": attempt,
        "retry_maximum_attempts": maximum_attempts,
    }


def test_retry_claiming_a_different_bead_each_attempt_does_not_misattribute_exhaustion(
    monkeypatch,
):
    """Three successive Temporal workflow retries, each claiming a different
    pending bead. The workflow's own attempt count reaches its policy bound
    (3/3) on the third attempt, but bead-c -- the one that attempt actually
    claimed -- has never failed before. Its own retry budget must decide its
    fate, not the workflow's."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    task_b = make_task("bead-b", "2026-08-23T00:05:00Z")
    task_c = make_task("bead-c", "2026-08-23T00:10:00Z")
    sub = RecordingSubstrate([task_a, task_b, task_c])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    dispatch_steps.record_failure_activity(_attempt_failure_state(task_a, attempt=1))
    assert task_a["state"] == "pending"

    dispatch_steps.record_failure_activity(_attempt_failure_state(task_b, attempt=2))
    assert task_b["state"] == "pending"

    dispatch_steps.record_failure_activity(_attempt_failure_state(task_c, attempt=3))

    assert task_c["state"] == "pending", (
        "bead-c has zero failures of its own; the workflow's exhausted "
        "attempt count belongs to bead-a and bead-b, not bead-c"
    )
    assert len(sub.notes["bead-c"]) == 1


def test_a_beads_own_exhausted_notes_still_fail_it(monkeypatch):
    """The other half of the same guarantee: a bead that has genuinely spent
    its own budget is still failed permanently, exactly as today, driven only
    by its own recorded failure notes."""
    task_c = make_task("bead-c", "2026-08-23T00:10:00Z")
    sub = RecordingSubstrate([task_c])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    # Two prior failures of bead-c's own recorded history.
    sub.notes["bead-c"] = [
        {"content": {"kind": "status", "body": "Run failed: boom 1"}, "created_at": "2026-08-23T00:01:00Z"},
        {"content": {"kind": "status", "body": "Run failed: boom 2"}, "created_at": "2026-08-23T00:02:00Z"},
    ]

    # A workflow attempt count that looks nowhere near exhausted -- it must
    # not rescue a bead whose own history says otherwise.
    dispatch_steps.record_failure_activity(_attempt_failure_state(task_c, attempt=1))

    assert task_c["state"] == "failed"


class _FakeSubstrateWithNotes(FakeSubstrate):
    """The claim double, but list_notes returns what the test hands it."""

    def __init__(self, tasks, notes_by_task):
        super().__init__(tasks)
        self._notes_by_task = notes_by_task

    def list_notes(self, parent_id, limit=500):
        return list(self._notes_by_task.get(parent_id, []))


def test_dry_run_brief_leads_with_the_review_note_for_a_resumed_bead(monkeypatch):
    """OPS-117 AC-4 (the #805 gate F2): the resume section is checked through
    the dispatcher's own dry-run rendering -- claim_activity(dry_run=True) --
    not only by calling guards.build_prompt directly. The bead carries
    preserved work and a request-changes review bound to it by pr_url; the
    brief must open on that note before the acceptance criteria."""
    pr = "https://github.com/example/repo/pull/42"
    task = make_task("bead-r", "2026-09-13T00:00:00Z")
    task["content"]["preserved_attempts"] = [{"pr": pr, "head_sha": "1b3ca0c6deadbeef"}]
    review = {
        "id": "n-review",
        "parent_id": "bead-r",
        "created_at": "2026-09-13T00:10:00Z",
        "content": {
            "kind": "review",
            "verdict": "request-changes",
            "pr_url": pr,
            "body": "KEEP the fix; F1: the fetch must not write FETCH_HEAD.",
        },
    }
    sub = _FakeSubstrateWithNotes([task], {"bead-r": [review]})

    result = _claim(sub, monkeypatch, {"task_id": "bead-r", "dry_run": True})

    assert result["status"] == "dry_run", result
    brief = result["message"]
    resume_at = brief.index("## Resuming from applied preserved work")
    acceptance_at = brief.index("## Acceptance criteria")
    assert resume_at < acceptance_at
    assert "F1: the fetch must not write FETCH_HEAD." in brief
    assert brief.index(pr) < acceptance_at
    # Dry run claims nothing and writes nothing.
    assert sub.transitions == [] and sub.notes == []
