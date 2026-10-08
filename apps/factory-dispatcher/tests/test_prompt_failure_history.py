"""A retry must be told why its predecessors failed.

fail_task has always written the reason to the bead as a status note, and
build_prompt has never read it back, so every attempt began as attempt #1.
cd5e27ce failed six times against a spec that was unsatisfiable by
construction, each attempt rediscovering the same impossibility from nothing.

See ARCHITECTURE.md Amendment 29 (PROPOSED) and PC-FAC-006.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guards  # noqa: E402

TASK = {
    "id": "task-1",
    "content": {
        "title": "a task that has been tried before",
        "intent": "do the thing",
        "acceptance": ["THE thing SHALL be done."],
        "scope": {"paths": ["apps/factory-dispatcher/"], "forbidden_paths": [".github/**"]},
        "risk_class": "behavioral",
    },
}


def status_note(note_id, created_at, body):
    return {
        "id": note_id,
        "created_at": created_at,
        "content": {"kind": "status", "body": body},
    }


def failure(note_id, created_at, reason):
    return status_note(note_id, created_at, f"Run failed: {reason}")


def test_a_first_attempt_claims_no_history():
    prompt = guards.build_prompt(TASK, [])

    assert "Why previous attempts failed" not in prompt


def test_a_retry_is_told_why_the_last_attempt_failed():
    notes = [failure("n1", "2026-08-08T10:00:00Z", "verification command exited 1")]

    prompt = guards.build_prompt(TASK, notes)

    assert "Why previous attempts failed" in prompt
    assert "verification command exited 1" in prompt


def test_reasons_are_oldest_first_and_attributed_to_their_attempt():
    notes = [
        failure("n2", "2026-08-08T11:00:00Z", "second reason"),
        failure("n1", "2026-08-08T10:00:00Z", "first reason"),
        failure("n3", "2026-08-08T12:00:00Z", "third reason"),
    ]

    prompt = guards.build_prompt(TASK, notes)

    assert prompt.index("first reason") < prompt.index("second reason")
    assert prompt.index("second reason") < prompt.index("third reason")
    assert "**Attempt 1:** first reason" in prompt
    assert "**Attempt 3:** third reason" in prompt


def test_capacity_backpressure_is_not_reported_as_a_failure():
    # FA-S27 returns a capacity-blocked task to pending WITHOUT spending an
    # attempt. Telling a worker it "failed" because a subscription ran out
    # would push it to change something to dodge a failure it never caused.
    notes = [
        status_note(
            "n1",
            "2026-08-08T10:00:00Z",
            "Capacity backpressure: usage limit exhaustion, retry_at=Aug 13th. "
            "Task returned to pending without incrementing attempts.",
        )
    ]

    prompt = guards.build_prompt(TASK, notes)

    assert "Why previous attempts failed" not in prompt
    assert "Capacity backpressure" not in prompt


def test_a_capacity_note_does_not_shift_the_attempt_numbering():
    notes = [
        failure("n1", "2026-08-08T10:00:00Z", "real failure one"),
        status_note("n2", "2026-08-08T11:00:00Z", "Capacity backpressure: exhausted"),
        failure("n3", "2026-08-08T12:00:00Z", "real failure two"),
    ]

    prompt = guards.build_prompt(TASK, notes)

    assert "**Attempt 1:** real failure one" in prompt
    assert "**Attempt 2:** real failure two" in prompt


def test_other_status_notes_are_not_mistaken_for_failures():
    notes = [status_note("n1", "2026-08-08T10:00:00Z", "Claimed by worker codex")]

    prompt = guards.build_prompt(TASK, notes)

    assert "Why previous attempts failed" not in prompt


def test_answered_questions_still_reach_the_prompt():
    # The existing collaboration payoff must not be displaced by the new block.
    notes = [
        {
            "id": "q1",
            "created_at": "2026-08-08T09:00:00Z",
            "content": {"kind": "question", "body": "which database?"},
        },
        {
            "id": "a1",
            "created_at": "2026-08-08T09:30:00Z",
            "content": {"kind": "answer", "body": "postgres", "answers_ref": "q1"},
        },
        failure("n1", "2026-08-08T10:00:00Z", "a real failure"),
    ]

    prompt = guards.build_prompt(TASK, notes)

    assert "which database?" in prompt
    assert "postgres" in prompt
    assert "a real failure" in prompt


def test_history_is_bounded_so_the_prompt_cannot_grow_without_limit():
    # #319: unbounded worker output exceeded Temporal's transport limit and one
    # task wedged the whole queue. Beads have reached seven attempts, and each
    # reason can carry a 400-char output tail.
    notes = [
        failure(f"n{i}", f"2026-08-08T{10 + i:02d}:00:00Z", f"reason number {i}")
        for i in range(1, 8)
    ]

    prompt = guards.build_prompt(TASK, notes)

    assert "reason number 1" not in prompt
    assert "reason number 7" in prompt
    assert "**Attempt 7:** reason number 7" in prompt
    assert "4 earlier failure(s) are not shown" in prompt


def test_an_oversized_reason_is_truncated_and_says_so():
    notes = [failure("n1", "2026-08-08T10:00:00Z", "x" * 5000)]

    prompt = guards.build_prompt(TASK, notes)

    assert "[…truncated]" in prompt
    assert len(prompt) < 3000


def test_the_worker_is_told_to_stop_rather_than_repeat_a_rejected_approach():
    notes = [failure("n1", "2026-08-08T10:00:00Z", "out of scope: needed to edit a forbidden file")]

    prompt = guards.build_prompt(TASK, notes)

    assert "STOP and say so instead of attempting it again" in prompt


def test_notes_given_as_a_generator_are_not_consumed_before_the_history_is_read():
    notes = [failure("n1", "2026-08-08T10:00:00Z", "a real failure")]

    prompt = guards.build_prompt(TASK, (n for n in notes))

    assert "a real failure" in prompt


def test_prior_failures_ignores_an_empty_reason():
    notes = [status_note("n1", "2026-08-08T10:00:00Z", "Run failed:")]

    assert guards.prior_failures(notes) == []
