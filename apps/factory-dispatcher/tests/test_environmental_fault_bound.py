"""OPS-6, 2026-08-23: dev.task 05262e55 recorded the identical environmental
fault every ~15 minutes for three hours before anything stopped it.
retry_policy.ENVIRONMENT_FAILURE correctly never spends the retry budget for a
TRANSIENT fault (an auth outage, a dead tunnel) — that must keep working
unbounded, exactly as before. What was missing is a bound on a PERMANENT one:
the SAME fault recurring on every CONSECUTIVE dispatch, which spends no
budget while starving every other pending task behind it forever.

These tests cover dispatch.environmental_fault_signature (stable identity,
ignoring run-to-run timing jitter), dispatch.trailing_environmental_fault_streak
(what "consecutive" means), dispatch.record_environment_failure (the bound
never touches the retry budget), and dispatch.pick_task (the bound is what
actually stops re-dispatching)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import guards  # noqa: E402
import retry_policy  # noqa: E402


LIMIT = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT


def task(task_id: str = "task-1") -> dict:
    return {
        "id": task_id,
        "state": "pending",
        "created_at": "2026-08-22T00:00:00Z",
        "content": {
            "lane": "code-health",
            "title": "t",
            "scope": {"paths": ["apps/x/"]},
        },
    }


class FakeSubstrate:
    def __init__(self, notes_by_id=None):
        self.notes_by_id = notes_by_id or {}
        self.states = []

    def list_notes(self, task_id, limit=500):
        return list(self.notes_by_id.get(task_id) or [])

    def list_tasks(self, state=None, limit=200):
        return []

    def list_beads(self, namespace, type, **params):
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return []

    def add_note(self, parent_id, kind, body, created_by, provenance=None, **extra):
        note = {
            "id": f"note-{len(self.notes_by_id.get(parent_id, []))}",
            "created_at": f"2026-08-22T{len(self.notes_by_id.get(parent_id, [])):02d}:00:00Z",
            "content": {"kind": kind, "body": body, **extra},
        }
        self.notes_by_id.setdefault(parent_id, []).append(note)
        return note

    def set_state(self, task_id, state, created_by):
        self.states.append((task_id, state, created_by))


def _fault_note(ordinal: int, signature: str, body: str = "same defect") -> dict:
    return {
        "id": f"fault-{ordinal}",
        "created_at": f"2026-08-22T00:{ordinal:02d}:00Z",
        "content": {
            "kind": "status",
            "body": f"{dispatch.ENVIRONMENTAL_FAULT_NOTE_PREFIX} {body}",
            dispatch.ENVIRONMENTAL_FAULT_SIGNATURE_FIELD: signature,
        },
    }


def _claim_note(ordinal: int) -> dict:
    return {
        "id": f"claim-{ordinal}",
        "created_at": f"2026-08-22T00:{ordinal:02d}:30Z",
        "content": {"kind": "status", "body": "Claimed by factory-dispatcher/claude."},
    }


def _run_failed_note(ordinal: int) -> dict:
    return {
        "id": f"failed-{ordinal}",
        "created_at": f"2026-08-22T00:{ordinal:02d}:45Z",
        "content": {
            "kind": "status",
            "body": f"{dispatch.RUN_FAILED_NOTE_PREFIX} attempt {ordinal}",
        },
    }


# ---------------------------------------------------------------------------
# environmental_fault_signature — stable identity, ignoring timing jitter
# ---------------------------------------------------------------------------


def test_signature_is_stable_across_duration_jitter():
    a = "Ran: `cmd` (exit 1, 3s)\nsame pytest failure body"
    b = "Ran: `cmd` (exit 1, 11s)\nsame pytest failure body"

    assert dispatch.environmental_fault_signature(a) == dispatch.environmental_fault_signature(b)


def test_signature_differs_for_a_different_failure():
    a = "Ran: `cmd` (exit 1, 3s)\nImportError: no module named foo"
    b = "Ran: `cmd` (exit 1, 3s)\nRuntimeError: missing FOO_DSN"

    assert dispatch.environmental_fault_signature(a) != dispatch.environmental_fault_signature(b)


# ---------------------------------------------------------------------------
# trailing_environmental_fault_streak — what "consecutive" means
# ---------------------------------------------------------------------------


def test_no_notes_is_no_streak():
    assert dispatch.trailing_environmental_fault_streak([]) == ("", 0)


def test_most_recent_outcome_not_a_fault_is_no_streak():
    notes = [_fault_note(0, "sig-a"), _run_failed_note(1)]
    assert dispatch.trailing_environmental_fault_streak(notes) == ("", 0)


def test_consecutive_same_signature_faults_count_up():
    notes = [_claim_note(0), _fault_note(0, "sig-a"), _claim_note(1), _fault_note(1, "sig-a")]
    assert dispatch.trailing_environmental_fault_streak(notes) == ("sig-a", 2)


def test_claim_notes_do_not_break_the_streak():
    """A claim note carries no outcome — it must not look like an interruption."""
    notes = [
        _fault_note(0, "sig-a"),
        _claim_note(1),
        _fault_note(1, "sig-a"),
        _claim_note(2),
        _fault_note(2, "sig-a"),
    ]
    assert dispatch.trailing_environmental_fault_streak(notes) == ("sig-a", 3)


def test_a_different_signature_breaks_the_streak():
    notes = [_fault_note(0, "sig-a"), _fault_note(1, "sig-b")]
    assert dispatch.trailing_environmental_fault_streak(notes) == ("sig-b", 1)


def test_a_real_work_failure_in_between_breaks_the_streak():
    """The fault did not recur on EVERY consecutive dispatch — attempt 2 ran
    past preflight and failed on its own merits, so it is not the same story."""
    notes = [_fault_note(0, "sig-a"), _run_failed_note(1), _fault_note(2, "sig-a")]
    assert dispatch.trailing_environmental_fault_streak(notes) == ("sig-a", 1)


# ---------------------------------------------------------------------------
# record_environment_failure — never spends the retry budget, either way
# ---------------------------------------------------------------------------


def test_record_environment_failure_never_writes_a_run_failed_note():
    sub = FakeSubstrate()
    for _ in range(LIMIT + 2):
        dispatch.record_environment_failure(sub, task(), "same defect every time")

    notes = sub.list_notes("task-1")
    assert all(not n["content"]["body"].startswith("Run failed:") for n in notes)
    assert all(s == ("task-1", "pending", dispatch.CREATED_BY) for s in sub.states)
    assert len(guards.prior_failures(notes)) == 0


def test_bound_is_named_on_the_bead_once_reached():
    sub = FakeSubstrate()
    for _ in range(LIMIT - 1):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    notes_before = sub.list_notes("task-1")
    assert f"{LIMIT}" not in notes_before[-1]["content"]["body"]

    dispatch.record_environment_failure(sub, task(), "same defect every time")

    tripped = sub.list_notes("task-1")[-1]["content"]["body"]
    assert f"{LIMIT} consecutive times" in tripped
    assert "not a verdict on the work" in tripped


def test_bound_reached_raises_the_declared_breaker_latch_alert(monkeypatch):
    """The breaker latching must announce itself, not just print and note
    (dispatch.py:3950-3956, the queue-level silent stop this closes)."""
    import failure_diagnosis

    announced: list[tuple] = []
    monkeypatch.setattr(
        failure_diagnosis,
        "announce_environmental_fault_breaker_latched",
        lambda bead_id, sig, streak, limit, **_k: announced.append(
            (bead_id, sig, streak, limit)
        )
        or True,
    )

    sub = FakeSubstrate()
    for _ in range(LIMIT - 1):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    assert announced == []

    dispatch.record_environment_failure(sub, task(), "same defect every time")

    assert len(announced) == 1
    bead_id, _sig, streak, limit_seen = announced[0]
    assert bead_id == "task-1"
    assert streak == LIMIT
    assert limit_seen == LIMIT


def test_further_forced_failures_at_the_bound_are_deduped_by_the_real_alert_policy(
    tmp_path, monkeypatch
):
    """Once latched, a forced retry that fails identically again must not
    spam a fresh Discord post on every attempt: the call site announces every
    time bound_reached is true, but the real, file-backed AlertPolicy this
    goes through (same one dev_task_failed uses) is what actually collapses
    repeats of the identical (bead, signature) into a single delivery."""
    import failure_diagnosis

    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    posts: list[str] = []

    async def fake_post(content, **_kwargs):
        posts.append(content)
        return True

    monkeypatch.setattr(
        failure_diagnosis,
        "dev_task_alert_policy",
        lambda post=None: failure_diagnosis.notify.AlertPolicy(
            load_alert_state=failure_diagnosis._load_dev_task_alert_state,
            record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
            post=fake_post,
        ),
    )

    sub = FakeSubstrate()
    for _ in range(LIMIT + 2):
        dispatch.record_environment_failure(sub, task(), "same defect every time")

    assert len(posts) == 1


def test_a_transient_fault_that_does_not_repeat_never_mentions_a_bound():
    sub = FakeSubstrate()
    dispatch.record_environment_failure(sub, task(), "flaky network blip #1")
    dispatch.record_environment_failure(sub, task(), "flaky network blip #2, unrelated")

    for note in sub.list_notes("task-1"):
        assert "consecutive times" not in note["content"]["body"]


def test_attempts_are_unchanged_by_the_bound_tripping_or_releasing():
    sub = FakeSubstrate()
    for _ in range(LIMIT):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    assert len(guards.prior_failures(sub.list_notes("task-1"))) == 0

    # the fault stops repeating — a differently-signed outcome lands
    dispatch.record_environment_failure(sub, task(), "a completely different defect now")
    assert len(guards.prior_failures(sub.list_notes("task-1"))) == 0


# ---------------------------------------------------------------------------
# pick_task — the bound is what actually stops re-dispatching
# ---------------------------------------------------------------------------


def test_pick_task_skips_a_bead_that_tripped_the_bound(capsys):
    sub = FakeSubstrate()
    for _ in range(LIMIT):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    bead = task()
    bead["state"] = "pending"

    result = dispatch.pick_task(sub, None, [bead])

    assert result is None
    assert "same environmental fault" in capsys.readouterr().out


def test_pick_task_selects_the_bead_again_once_the_fault_changes():
    sub = FakeSubstrate()
    for _ in range(LIMIT):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    dispatch.record_environment_failure(sub, task(), "a completely different defect now")
    bead = task()

    result = dispatch.pick_task(sub, None, [bead])

    assert result is not None
    assert result["id"] == "task-1"


def test_pick_task_never_trips_on_a_transient_non_repeating_fault():
    """A transient fault (auth outage, dead tunnel) that does not recur must
    keep retrying unbounded, exactly as today — the bound must not be
    mistaken for a general retry cap."""
    sub = FakeSubstrate()
    dispatch.record_environment_failure(sub, task(), "auth outage, try #1")
    dispatch.record_environment_failure(sub, task(), "auth outage resolved, unrelated blip")
    bead = task()

    result = dispatch.pick_task(sub, None, [bead])

    assert result is not None


def _stale_base_ref_reason(commits_behind: int = 4) -> str:
    status = dispatch.BaseRefStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        local_only=0,
        upstream_only=commits_behind,
    )
    return str(dispatch.BaseRefStaleError(status))


# ---------------------------------------------------------------------------
# A stale base ref is not a generic environmental fault: it has a one-command
# remedy and must never contribute to the bound-3 breaker (2026-08-26 — a
# merge to main left the checkout's local main behind origin/main for ten
# hours, and three identical refusals against the same bead tripped bound-3,
# turning recovery into "fix the ref AND force every latched bead
# individually").
# ---------------------------------------------------------------------------


def test_record_stale_base_ref_fault_never_writes_an_environmental_fault_note():
    sub = FakeSubstrate()
    reason = _stale_base_ref_reason()
    for _ in range(LIMIT + 3):
        dispatch.record_stale_base_ref_fault(sub, task(), reason)

    notes = sub.list_notes("task-1")
    assert len(notes) == LIMIT + 3
    assert all(
        n["content"]["body"].startswith(dispatch.STALE_BASE_REF_NOTE_PREFIX)
        for n in notes
    )
    assert all(
        not n["content"]["body"].startswith(dispatch.ENVIRONMENTAL_FAULT_NOTE_PREFIX)
        for n in notes
    )
    assert all(s == ("task-1", "pending", dispatch.CREATED_BY) for s in sub.states)


def test_repeated_stale_base_ref_faults_never_trip_the_bound():
    sub = FakeSubstrate()
    reason = _stale_base_ref_reason()
    # Well past LIMIT — three identical refusals used to be enough to trip
    # the bound when this was recorded as a generic environmental fault.
    for _ in range(LIMIT * 3):
        dispatch.record_stale_base_ref_fault(sub, task(), reason)

    notes = sub.list_notes("task-1")
    assert dispatch.trailing_environmental_fault_streak(notes) == ("", 0)


def test_pick_task_never_skips_a_bead_stuck_on_a_stale_base_ref():
    """Unlike a generic environmental fault, a stale base ref must never
    cause pick_task to move on to the next pending bead: every bead is
    blocked by the identical systemic cause, so latching one bead out and
    starting a fresh streak on the next just spreads the same problem
    across more beads (the exact compounding OPS-6-style failure this
    exists to avoid)."""
    sub = FakeSubstrate()
    reason = _stale_base_ref_reason()
    for _ in range(LIMIT * 5):
        dispatch.record_stale_base_ref_fault(sub, task(), reason)
    bead = task()

    result = dispatch.pick_task(sub, None, [bead])

    assert result is not None
    assert result["id"] == "task-1"


def test_the_exact_incident_shape_five_beads_six_cycles_never_latches_or_trips():
    """2026-08-26's exact shape: base ref four commits behind its tracking
    remote, clean tree, no local-only commits, five runnable beads, and the
    Temporal schedule firing normally (i.e. dispatch invoked repeatedly).
    The factory must not come to rest silently refusing every task: no bead
    is ever individually latched out by the bound-3 breaker, and the same
    (oldest) bead keeps being offered back to pick_task every cycle so a
    fixed ref resolves the whole queue on the very next tick — not "fix the
    ref and then force five beads one at a time"."""
    sub = FakeSubstrate()
    beads = [task(f"task-{i}") for i in range(1, 6)]
    reason = _stale_base_ref_reason(commits_behind=4)

    for _cycle in range(6):
        picked = dispatch.pick_task(sub, None, beads)
        assert picked is not None, "the factory must not go silent while work is pending"
        assert picked["id"] == "task-1", (
            "the oldest bead must keep being offered every cycle — no bead "
            "may latch out from a systemic, not per-bead, cause"
        )
        dispatch.record_stale_base_ref_fault(sub, picked, reason)

    for bead in beads:
        notes = sub.list_notes(bead["id"])
        signature, streak = dispatch.trailing_environmental_fault_streak(notes)
        assert (signature, streak) == ("", 0), (
            f"{bead['id']} must never accumulate a bound-3 streak from a "
            "stale-base-ref cause"
        )

    # Every cycle's refusal must be recorded, not silently dropped.
    assert len(sub.list_notes("task-1")) == 6
    for i in range(2, 6):
        assert sub.list_notes(f"task-{i}") == []


def test_explicit_task_targeting_bypasses_the_bound():
    """--task <bead-id> is the operator's way to force a run and find out
    whether a tripped fault still occurs; it must not be blocked by the same
    check that governs automatic scanning."""
    sub = FakeSubstrate()
    for _ in range(LIMIT):
        dispatch.record_environment_failure(sub, task(), "same defect every time")
    bead = task()

    result = dispatch.pick_task(sub, "task-1", [bead])

    assert result is not None
    assert result["id"] == "task-1"
