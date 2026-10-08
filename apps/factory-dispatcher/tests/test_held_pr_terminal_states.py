"""A bead held on a PR that closes without merging waits forever unless
something re-reads that PR's state after the bead has left `review`.

`dispatch.reconcile_review_tasks` already re-reads PR state every cycle for
beads in `review`. A `pending`/`failed` bead held on a PR named only in a
note's prose (e.g. dev.task ad0a0f95: "the re-spec waits for #888 to settle
... Trigger: #888 reaching a terminal state") has no equivalent -- nothing
notices when that PR reaches the one terminal state that makes the wait
permanent (closed without merging) versus the one that discharges it
(merged).

Per CLAUDE.md's 2026-09-13 decision record D7, these are five hand-built
situations, not a measurement of production: no snapshot is supplied, and
none is needed -- the PR-state lookup is an injected collaborator, so no
network call and no live store read ever happens here. That collaborator
being called proves the *call*, never that live GitHub agrees with the
fixture; nothing here can prove that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeSubstrate:
    """Minimal BeadStore double: list_tasks(state=...) + list_notes(task_id)."""

    def __init__(self, tasks, notes=None):
        self.tasks = tasks
        self.existing_notes = notes or {}
        self.transitions: list[tuple] = []
        self.notes: list[dict] = []
        self.states: list[tuple] = []
        self.patches: list[tuple] = []

    def list_tasks(self, state=None):
        if state is None:
            return list(self.tasks)
        return [t for t in self.tasks if (t.get("state") or "pending") == state]

    def list_notes(self, task_id):
        return list(self.existing_notes.get(task_id) or [])

    def transition_state(self, bead_id, from_state, to_state, created_by):
        self.transitions.append((bead_id, from_state, to_state, created_by))

    def add_note(self, task_id, kind, body, created_by, **kwargs):
        self.notes.append({"task_id": task_id, "content": {"kind": kind, "body": body}})

    def set_state(self, task_id, state, created_by):
        self.states.append((task_id, state, created_by))

    def patch_content(self, task_id, content, created_by):
        self.patches.append((task_id, content, created_by))


class DispositionGuard:
    """Fails the test the moment anything tries to dispose of a bead.

    A hold is a judgement the outer loop made in prose; only the outer loop
    can retire it (AC-2). Wired in place of the operator entry points so any
    call from the code under test fails loudly instead of quietly succeeding.
    """

    def __init__(self):
        self.calls: list[str] = []

    def release_stranded_task(self, *_a, **_k):
        self.calls.append("release_stranded_task")
        raise AssertionError("reporting a held PR must never release it")

    def requeue_task(self, *_a, **_k):
        self.calls.append("requeue_task")
        raise AssertionError("reporting a held PR must never requeue it")

    def bind_task_to_pull_request(self, *_a, **_k):
        self.calls.append("bind_task_to_pull_request")
        raise AssertionError("reporting a held PR must never bind a PR")


def held_task(task_id, state="pending", title="task"):
    return {"id": task_id, "state": state, "content": {"title": title}}


def note(body, created_at="2026-09-01T00:00:00Z"):
    return {"created_at": created_at, "content": {"kind": "status", "body": body}}


def pr_status(number, state):
    return dispatch.PullRequestStatus(
        number=number,
        url=f"https://github.com/example/repo/pull/{number}",
        state=state,
        merge_commit="abc123" if state == "MERGED" else None,
    )


class RecordingLookup:
    """Injected PR-state collaborator: records calls, never touches a network."""

    def __init__(self, states: dict[int, str]):
        self.states = states
        self.calls: list[str] = []

    def __call__(self, pr_ref, cfg):
        self.calls.append(pr_ref)
        number = int(str(pr_ref).lstrip("#"))
        return pr_status(number, self.states[number])


# ---------------------------------------------------------------------------
# the five fixture cases (AC-3)
# ---------------------------------------------------------------------------

MERGED_PR_BEAD = held_task("bead-merged", title="waits on a PR that merged")
MERGED_PR_NOTE = note(
    "the re-spec waits for #900 to settle before this can be re-attempted. "
    "Trigger: #900 reaching a terminal state."
)

CLOSED_PR_BEAD = held_task("bead-closed", title="waits on a PR that closed unmerged")
CLOSED_PR_NOTE = note(
    "the re-spec waits for #888 to settle before this can be re-attempted. "
    "Trigger: #888 reaching a terminal state."
)

OPEN_PR_BEAD = held_task("bead-open", title="waits on a PR still open")
OPEN_PR_NOTE = note(
    "held for #901 to settle before this can be re-attempted. "
    "Trigger: #901 reaching a terminal state."
)

NO_PR_BEAD = held_task("bead-no-pr", title="held with no PR named")
NO_PR_NOTE = note(
    "blocked on the spec review landing before this can be re-attempted; "
    "no PR opened yet."
)

PASSING_MENTION_BEAD = held_task("bead-passing-mention", title="cites history, not a hold")
PASSING_MENTION_NOTE = note(
    "See PR #902 for background on why this approach was chosen originally."
)


def _all_tasks_and_notes():
    tasks = [MERGED_PR_BEAD, CLOSED_PR_BEAD, OPEN_PR_BEAD, NO_PR_BEAD, PASSING_MENTION_BEAD]
    notes = {
        "bead-merged": [MERGED_PR_NOTE],
        "bead-closed": [CLOSED_PR_NOTE],
        "bead-open": [OPEN_PR_NOTE],
        "bead-no-pr": [NO_PR_NOTE],
        "bead-passing-mention": [PASSING_MENTION_NOTE],
    }
    lookup = RecordingLookup({900: "MERGED", 888: "CLOSED", 901: "OPEN"})
    return tasks, notes, lookup


def test_closed_without_merging_is_the_only_finding():
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    assert [f.task_id for f in closed] == ["bead-closed"]
    assert closed[0].pr_number == 888
    assert closed[0].pr_state == "CLOSED"


def test_merged_is_a_separate_discharge_candidate_not_a_finding():
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    assert [f.task_id for f in merged] == ["bead-merged"]
    assert merged[0].pr_number == 900
    assert merged[0].pr_state == "MERGED"
    # The two outcomes never collide under a shared label (AC-1).
    assert set(f.task_id for f in closed).isdisjoint(f.task_id for f in merged)


def test_open_pr_is_not_reported_at_all_still_correctly_waiting():
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    reported = {f.task_id for f in closed} | {f.task_id for f in merged}
    assert "bead-open" not in reported


def test_hold_language_with_no_pr_reference_raises_no_finding():
    """AC-4: hold language alone, naming no PR, is not a trigger."""
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    reported = {f.task_id for f in closed} | {f.task_id for f in merged}
    assert "bead-no-pr" not in reported


def test_bare_pr_mention_without_hold_language_raises_no_finding():
    """AC-4: a PR number cited in passing is not evidence of a live hold."""
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    reported = {f.task_id for f in closed} | {f.task_id for f in merged}
    assert "bead-passing-mention" not in reported
    # The passing mention's PR (#902) is never looked up: no hold language
    # means no trigger is extracted from that note at all.
    assert "902" not in lookup.calls


def test_all_five_fixture_cases_together_fire_exactly_two_rules():
    """State, in one place, how many of the fixture's five cases each rule
    fires on: 1 closed-without-merging finding, 1 merged discharge candidate,
    0 for the remaining three (open, no-PR, passing-mention)."""
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    assert len(closed) == 1
    assert len(merged) == 1
    assert len(tasks) == 5


# ---------------------------------------------------------------------------
# injected collaborator: no network call, ever
# ---------------------------------------------------------------------------


def test_pr_state_lookup_is_an_injected_collaborator_never_a_network_call():
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    # Exactly the two named-with-hold-language PRs among the fixture's
    # terminal cases were looked up; #901 (open) is also looked up because
    # it too carries hold language -- only its *state* excludes it from
    # either output list.
    assert set(lookup.calls) == {"900", "888", "901"}


def test_a_failing_lookup_for_one_bead_does_not_stop_the_sweep():
    tasks = [CLOSED_PR_BEAD, held_task("bead-explodes", title="unreachable PR")]
    notes = {
        "bead-closed": [CLOSED_PR_NOTE],
        "bead-explodes": [note("waits for #999 to settle. Trigger: #999 reaching a terminal state.")],
    }
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    def flaky_lookup(pr_ref, _cfg):
        if pr_ref == "999":
            raise RuntimeError("gh: network unreachable")
        return pr_status(888, "CLOSED")

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=flaky_lookup)

    assert [f.task_id for f in closed] == ["bead-closed"]
    assert merged == []


# ---------------------------------------------------------------------------
# newest note wins, by created_at -- never by list position
# ---------------------------------------------------------------------------


def test_selects_the_newest_note_by_created_at_not_list_position():
    """A stale hold note followed by a later note that supersedes it (e.g.
    naming a different, still-open PR) must not raise a finding on the old
    PR merely because it appears earlier in the list."""
    tasks = [held_task("bead-superseded-hold")]
    stale_hold = note(
        "waits for #888 to settle. Trigger: #888 reaching a terminal state.",
        created_at="2026-09-01T00:00:00Z",
    )
    fresh_hold = note(
        "held for #901 to settle. Trigger: #901 reaching a terminal state.",
        created_at="2026-09-10T00:00:00Z",
    )
    # Deliberately out of chronological order in the list itself.
    notes = {"bead-superseded-hold": [fresh_hold, stale_hold]}
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())
    lookup = RecordingLookup({888: "CLOSED", 901: "OPEN"})

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    assert closed == []
    assert merged == []
    assert lookup.calls == ["901"]


# ---------------------------------------------------------------------------
# review/doing beads are out of scope for this check
# ---------------------------------------------------------------------------


def test_review_and_doing_beads_are_never_scanned():
    review_bead = {
        "id": "bead-in-review",
        "state": "review",
        "content": {"title": "in review"},
    }
    doing_bead = {
        "id": "bead-in-doing",
        "state": "doing",
        "content": {"title": "in doing"},
    }
    notes = {
        "bead-in-review": [note("waits for #888 to settle. Trigger: #888 reaching a terminal state.")],
        "bead-in-doing": [note("waits for #888 to settle. Trigger: #888 reaching a terminal state.")],
    }
    sub = FakeSubstrate(tasks=[review_bead, doing_bead], notes=notes)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())
    lookup = RecordingLookup({888: "CLOSED"})

    closed, merged = dispatch.held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup)

    assert closed == []
    assert merged == []
    assert lookup.calls == []


# ---------------------------------------------------------------------------
# report_held_prs: the CLI-facing, read-only wrapper (AC-2)
# ---------------------------------------------------------------------------


def test_report_prints_both_outcomes_and_never_touches_the_store(capsys, monkeypatch):
    tasks, notes, lookup = _all_tasks_and_notes()
    sub = FakeSubstrate(tasks=tasks, notes=notes)
    guard = DispositionGuard()
    monkeypatch.setattr(dispatch, "release_stranded_task", guard.release_stranded_task)
    monkeypatch.setattr(dispatch, "requeue_task", guard.requeue_task)
    monkeypatch.setattr(dispatch, "bind_task_to_pull_request", guard.bind_task_to_pull_request)
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())

    rc = dispatch.report_held_prs(sub, cfg, lookup_pr=lookup)
    out = capsys.readouterr().out

    assert rc == 1  # a closed-without-merging finding exists
    assert "bead-closed\tclosed_without_merging\tpr=#888" in out
    assert "bead-merged\tmerged_discharge_candidate\tpr=#900" in out
    assert "bead-open" not in out
    assert "bead-no-pr" not in out
    assert "bead-passing-mention" not in out
    assert sub.transitions == []
    assert sub.notes == []
    assert sub.states == []
    assert sub.patches == []
    assert guard.calls == []


def test_report_exits_zero_when_only_a_discharge_candidate_is_found():
    sub = FakeSubstrate(
        tasks=[MERGED_PR_BEAD],
        notes={"bead-merged": [MERGED_PR_NOTE]},
    )
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())
    lookup = RecordingLookup({900: "MERGED"})

    rc = dispatch.report_held_prs(sub, cfg, lookup_pr=lookup)

    assert rc == 0


def test_report_exits_zero_when_nothing_is_held_on_a_terminal_pr():
    sub = FakeSubstrate(tasks=[OPEN_PR_BEAD], notes={"bead-open": [OPEN_PR_NOTE]})
    cfg = dispatch.Config(repo="example/repo", repo_root=Path.cwd())
    lookup = RecordingLookup({901: "OPEN"})

    rc = dispatch.report_held_prs(sub, cfg, lookup_pr=lookup)

    assert rc == 0
