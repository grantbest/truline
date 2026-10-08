"""R26.12 B3 (docs/plans/2026-09-23-design-r2612-steerable-and-legible.md,
"Mechanism" > "Queue order", BREAKER EXIT (B3)); requirement PC-EXE-007.

Before this fix, the environmental-fault breaker latched on the evidence
the bead already recorded -- a later "Worker finished in" success note or a
recorded requeue (guards.requeue_barrier_at) never ended the streak, and the
only exits were `dispatch.py --task <id>` or the bead terminating. This file
proves both new exits on the real capture the defect was measured on
(tests/fixtures/a728d995-notes-2026-09-23.json, dev.task a728d995, PR
#1026), on the one bead latched at the 2026-09-25 capture
(tests/fixtures/queue-2026-09-23-latched.json,
header.latched_with_later_exit_notes), and in-process, with cases proving
the breaker still latches what it genuinely should.

AC-3's fixture tests are all evaluated on THE 2026-09-17 VIEW -- the notes
with created_at <= header.requeue_barrier_note_at -- which is the state the
design and PC-EXE-007 describe; the fixture also carries later evidence
(header.worker_finished_notes_all, header.resets_attempts_notes_all) that
would each end the streak on their own and would make every case trivial,
which is exactly why the view excludes it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import dispatch  # noqa: E402
import guards  # noqa: E402
import queue_order  # noqa: E402
import retry_policy  # noqa: E402

from test_dispatch import FakeSubstrate  # noqa: E402
from test_queue_order_parity import _build_store, _reduced_note_to_full  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


def _full_notes(fixture: dict) -> list[dict]:
    return [_reduced_note_to_full(n) for n in fixture["notes"]]


def _the_view(fixture: dict) -> list[dict]:
    """The 2026-09-17 view: notes no newer than the recorded requeue barrier."""
    barrier_at = fixture["header"]["requeue_barrier_note_at"]
    return [n for n in _full_notes(fixture) if n["created_at"] <= barrier_at]


def _without_id(notes: list[dict], note_id: str) -> list[dict]:
    return [n for n in notes if n["id"] != note_id]


# ---------------------------------------------------------------------------
# AC-3: the three named fixture tests, plus the control.


def test_worker_finished_note_ends_streak():
    fixture = _load("a728d995-notes-2026-09-23.json")
    header = fixture["header"]
    notes = _without_id(_the_view(fixture), header["requeue_barrier_note_id"])
    assert queue_order.trailing_environmental_fault_streak(notes) == ("", 0)


def test_requeue_barrier_ends_streak():
    fixture = _load("a728d995-notes-2026-09-23.json")
    header = fixture["header"]
    notes = _without_id(_the_view(fixture), header["worker_finished_note_id"])
    assert queue_order.trailing_environmental_fault_streak(notes) == ("", 0)


def test_notes_are_never_mutated():
    fixture = _load("a728d995-notes-2026-09-23.json")
    for notes in (_the_view(fixture), _full_notes(fixture)):
        before = json.dumps(notes, sort_keys=True)
        before_failures = guards.prior_failures(notes)

        queue_order.trailing_environmental_fault_streak(notes)

        after = json.dumps(notes, sort_keys=True)
        after_failures = guards.prior_failures(notes)
        assert before == after
        assert before_failures == after_failures


def test_fixture_still_latches_without_either_exit():
    """CONTROL: the view minus both the success note and the requeue barrier
    note still latches -- the fixture's latch is real, not an artifact of a
    scanner that always returns ("", 0)."""
    fixture = _load("a728d995-notes-2026-09-23.json")
    header = fixture["header"]
    notes = _without_id(
        _without_id(_the_view(fixture), header["requeue_barrier_note_id"]),
        header["worker_finished_note_id"],
    )
    expected = header["streak_through_barrier"]
    assert expected["streak"] >= retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
    assert queue_order.trailing_environmental_fault_streak(notes) == (
        expected["signature"],
        expected["streak"],
    )


# ---------------------------------------------------------------------------
# AC-4b: the real latch lets go, replayed untruncated.


def test_latched_with_later_exit_notes_let_go():
    fixture = _load("queue-2026-09-23-latched.json")
    header = fixture["header"]
    later_exit_ids = list(header["latched_with_later_exit_notes"])
    assert later_exit_ids, "fixture must carry at least one later-exit case"

    notes_by_task = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    store, all_tasks = _build_store(fixture, notes_by_task)

    for task_id in later_exit_ids:
        notes = store.list_notes(task_id)
        assert queue_order.trailing_environmental_fault_streak(notes) == ("", 0), task_id

    picked = dispatch.pick_task(store, None, all_tasks)
    assert picked is not None
    assert picked["id"] in later_exit_ids


# ---------------------------------------------------------------------------
# AC-4: the breaker still latches what it should (no fixture).


def _fault_note(note_id: str, created_at: str, signature: str) -> dict:
    return {
        "id": note_id,
        "created_at": created_at,
        "content": {
            "kind": "status",
            "body": f"{queue_order.ENVIRONMENTAL_FAULT_NOTE_PREFIX} boom. Task "
            "returned to pending without incrementing attempts.",
            queue_order.ENVIRONMENTAL_FAULT_SIGNATURE_FIELD: signature,
        },
    }


def _worker_finished_note(note_id: str, created_at: str) -> dict:
    return {
        "id": note_id,
        "created_at": created_at,
        "content": {
            "kind": "status",
            "body": f"{queue_order.WORKER_FINISHED_NOTE_PREFIX} 10s, 1 file(s) "
            "changed, containment held. Awaiting human review.",
        },
    }


def _requeue_note(note_id: str, created_at: str) -> dict:
    return {
        "id": note_id,
        "created_at": created_at,
        "content": {
            "kind": "status",
            "body": "Returned to pending by the SRE for testing.",
            guards.REQUEUE_MARKER_FIELD: True,
        },
    }


def test_three_consecutive_faults_with_nothing_after_still_latch():
    signature = "sig-a"
    notes = [
        _fault_note("f1", "2026-01-01T00:00:00Z", signature),
        _fault_note("f2", "2026-01-01T01:00:00Z", signature),
        _fault_note("f3", "2026-01-01T02:00:00Z", signature),
    ]
    assert queue_order.trailing_environmental_fault_streak(notes) == (signature, 3)


def test_three_faults_after_a_worker_finished_note_still_latch():
    signature = "sig-b"
    notes = [
        _worker_finished_note("w1", "2026-01-01T00:00:00Z"),
        _fault_note("f1", "2026-01-01T01:00:00Z", signature),
        _fault_note("f2", "2026-01-01T02:00:00Z", signature),
        _fault_note("f3", "2026-01-01T03:00:00Z", signature),
    ]
    assert queue_order.trailing_environmental_fault_streak(notes) == (signature, 3)


def test_three_faults_after_a_recorded_requeue_still_latch():
    signature = "sig-c"
    notes = [
        _requeue_note("r1", "2026-01-01T00:00:00Z"),
        _fault_note("f1", "2026-01-01T01:00:00Z", signature),
        _fault_note("f2", "2026-01-01T02:00:00Z", signature),
        _fault_note("f3", "2026-01-01T03:00:00Z", signature),
    ]
    assert queue_order.trailing_environmental_fault_streak(notes) == (signature, 3)


def test_requeue_between_first_and_second_fault_leaves_streak_at_two():
    signature = "sig-d"
    notes = [
        _fault_note("f1", "2026-01-01T00:00:00Z", signature),
        _requeue_note("r1", "2026-01-01T00:30:00Z"),
        _fault_note("f2", "2026-01-01T01:00:00Z", signature),
        _fault_note("f3", "2026-01-01T02:00:00Z", signature),
    ]
    assert queue_order.trailing_environmental_fault_streak(notes) == (signature, 2)


def test_stale_base_ref_note_between_faults_does_not_break_streak():
    """RC-1 (PR #1196 gate, AC-1): STALE_BASE_REF_NOTE_PREFIX is deliberately
    NOT in _DISPATCH_OUTCOME_NOTE_PREFIXES -- a stale-base-ref defer is pure
    narration, not a dispatch outcome, so it must not reset the streak the
    way a real work failure or a success note does."""
    notes = [
        _fault_note("f1", "2026-01-01T00:00:00Z", "sig-s"),
        {
            "id": "s1",
            "created_at": "2026-01-01T00:30:00Z",
            "content": {
                "kind": "status",
                "body": f"{dispatch.STALE_BASE_REF_NOTE_PREFIX} main is behind origin/main.",
            },
        },
        _fault_note("f2", "2026-01-01T01:00:00Z", "sig-s"),
    ]
    assert queue_order.trailing_environmental_fault_streak(notes) == ("sig-s", 2)


def test_next_streak_after_a_worker_finished_note_is_one():
    """RC-2 (PR #1196 gate, AC-4): seeds a full latch BEFORE the success note
    so this discriminates -- without WORKER_FINISHED_NOTE_PREFIX wired in,
    the scanner walks past the success note into the fault streak behind it
    and reports LIMIT + 1, not 1."""
    signature = "sig-e"
    limit = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
    notes = [
        _fault_note(f"f{i}", f"2026-01-01T{i:02d}:00:00Z", signature)
        for i in range(limit)
    ]
    notes.append(_worker_finished_note("w1", f"2026-01-01T{limit + 1:02d}:00:00Z"))
    assert queue_order._next_environmental_fault_streak(notes, signature) == 1


def test_record_environment_failure_bound_text_absent_on_first_fault_after_success(
    monkeypatch,
):
    """RC-2 (PR #1196 gate, AC-4): same discrimination as the test above, but
    through record_environment_failure's own bound text and alert, using the
    real signature a reason of "boom" hashes to and the LIMIT faults that
    reason would already have recorded."""
    import failure_diagnosis

    sig = dispatch.environmental_fault_signature("boom")
    limit = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
    notes = [
        _fault_note(f"f{i}", f"2026-01-01T{i:02d}:00:00Z", sig) for i in range(limit)
    ]
    notes.append(_worker_finished_note("w1", f"2026-01-01T{limit + 1:02d}:00:00Z"))

    announced: list[tuple] = []
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_environmental_fault_breaker_latched",
        lambda *args, **kwargs: announced.append((args, kwargs)),
    )

    task = {"id": "task-x", "state": "pending"}
    sub = FakeSubstrate(tasks=[task])

    dispatch.record_environment_failure(sub, task, "boom", notes=notes)

    body = sub.notes[-1][0][2]
    assert "same fault" not in body
    assert announced == []
