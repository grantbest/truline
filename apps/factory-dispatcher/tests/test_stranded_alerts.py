"""Stranded work must announce itself, not just wait for --report-stuck.

Before stranded_alerts.py existed, a doing bead whose owner could not be
shown live was only visible to a human who ran ``--report-stuck``, and a
review bead with no ``pr_url`` was only ever printed as ``missing_pr_url`` by
``reconcile_review_tasks`` to a log nobody tails. Both shapes were already
computed correctly by existing checks (``dispatch.stuck_doing_tasks``,
``guards.has_claim_note``, the ``pr_url`` check in ``reconcile_review_tasks``
itself) -- these tests cover the missing announcement: the alert fires
through the declared, policy-gated inventory; it is bounded per subject and
re-fires once a bead recovers and is later found stranded again; and nothing
here ever touches disposition (requeue / release / bind / dispatch).

No Discord, no substrate, no network, no Temporal server: every alert
delivery and every bead store below is a plain Python fake.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import stranded_alerts  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class RecordingPolicy:
    """Stands in for notify.AlertPolicy: records what would have posted."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, kind, fingerprint, content, *, severity=None, min_interval_hours=0.0, members=None, **policy_kwargs):
        # **policy_kwargs absorbs policy-layer options the real AlertPolicy
        # grows (re_alert_interval_hours arrived with the R2602-12 dedup
        # mechanism after this double was written) so the double tracks the
        # interface instead of failing on every new keyword.
        self.sent.append((kind, {"fingerprint": fingerprint, "content": content, **policy_kwargs}))
        return True


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


def doing_task(task_id, updated_at, title="task"):
    return {"id": task_id, "state": "doing", "updated_at": updated_at, "content": {"title": title}}


def review_task(task_id, pr_url=None, updated_at="2026-08-03T12:00:00Z", title="task"):
    content = {"title": title}
    if pr_url is not None:
        content["pr_url"] = pr_url
    return {"id": task_id, "state": "review", "updated_at": updated_at, "content": content}


def claim_note(body: str = "Claimed by factory-dispatcher/claude. Running.") -> dict:
    return {"content": {"kind": "status", "body": body}}


class DispositionGuard:
    """Fails the test the moment anything tries to dispose of a bead.

    Disposition (requeue / release-stranded / bind-pr / dispatch) must stay a
    human decision made through the existing operator commands -- see the
    task's "NOTHING in this change SHALL release, rebind, requeue or dispatch
    work" boundary. Wired in place of those entry points so any call from the
    code under test fails loudly instead of quietly succeeding.
    """

    def __init__(self):
        self.calls: list[str] = []

    def release_stranded_task(self, *_a, **_k):
        self.calls.append("release_stranded_task")
        raise AssertionError("announcing stranded work must never release it")

    def requeue_task(self, *_a, **_k):
        self.calls.append("requeue_task")
        raise AssertionError("announcing stranded work must never requeue it")

    def bind_task_to_pull_request(self, *_a, **_k):
        self.calls.append("bind_task_to_pull_request")
        raise AssertionError("announcing stranded work must never bind a PR")


# ---------------------------------------------------------------------------
# announce: doing bead with no live owner shown
# ---------------------------------------------------------------------------


def test_announce_stranded_doing_task_raises_declared_alert_with_bead_id_and_age():
    policy = RecordingPolicy()
    task = doing_task("d1fbc00b", "2026-08-30T16:45:00Z", title="OPS repro")

    posted = stranded_alerts.announce_stranded_doing_task(
        task, 62, "age_exceeded_threshold", policy=policy
    )

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "stranded_doing_task:d1fbc00b"
    assert "d1fbc00b" in payload["content"]
    assert "62" in payload["content"]


def test_announce_stranded_doing_task_uses_the_declared_inventory_entry():
    definition = stranded_alerts.notify.get_alert_definition(
        stranded_alerts.STRANDED_DOING_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_stranded_doing_task_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = stranded_alerts.announce_stranded_doing_task(
        doing_task("t1", "2026-08-30T16:45:00Z"), 5, "age_exceeded_threshold",
        policy=ExplodingPolicy(),
    )
    assert result is False


def test_repeated_observation_of_same_stranded_doing_episode_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    task = doing_task("d1fbc00b", "2026-08-30T16:45:00Z")

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = stranded_alerts.notify.AlertPolicy(
        load_alert_state=stranded_alerts.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=stranded_alerts.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = stranded_alerts.announce_stranded_doing_task(
        task, 60, "age_exceeded_threshold", policy=policy
    )
    second = stranded_alerts.announce_stranded_doing_task(
        task, 75, "age_exceeded_threshold", policy=policy
    )

    assert first is True
    assert second is False
    assert fake_post.calls == 1


def test_bead_stranded_again_after_recovery_re_alerts(tmp_path, monkeypatch):
    """A bead released back to pending and later reclaimed gets a fresh
    ``updated_at``, so a second stranding is a new episode, not a repeat."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = stranded_alerts.notify.AlertPolicy(
        load_alert_state=stranded_alerts.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=stranded_alerts.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first_episode = doing_task("d1fbc00b", "2026-08-30T16:45:00Z")
    second_episode = doing_task("d1fbc00b", "2026-09-02T09:00:00Z")

    first = stranded_alerts.announce_stranded_doing_task(
        first_episode, 60, "age_exceeded_threshold", policy=policy
    )
    unbounded_repeat = stranded_alerts.announce_stranded_doing_task(
        first_episode, 90, "age_exceeded_threshold", policy=policy
    )
    recovered_and_restranded = stranded_alerts.announce_stranded_doing_task(
        second_episode, 60, "age_exceeded_threshold", policy=policy
    )

    assert first is True
    assert unbounded_repeat is False
    assert recovered_and_restranded is True
    assert fake_post.calls == 2


# ---------------------------------------------------------------------------
# announce: review bead with no pr_url
# ---------------------------------------------------------------------------


def test_announce_review_missing_pr_url_raises_declared_alert_with_bead_id():
    policy = RecordingPolicy()
    task = review_task("r-1", pr_url=None, title="Ship the thing")

    posted = stranded_alerts.announce_review_missing_pr_url(task, policy=policy)

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "review_missing_pr_url:r-1"
    assert "r-1" in payload["content"]


def test_announce_review_missing_pr_url_uses_the_declared_inventory_entry():
    definition = stranded_alerts.notify.get_alert_definition(
        stranded_alerts.REVIEW_MISSING_PR_URL_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None


def test_announce_review_missing_pr_url_is_best_effort_and_never_raises():
    class ExplodingPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook exploded")

    result = stranded_alerts.announce_review_missing_pr_url(
        review_task("r-1"), policy=ExplodingPolicy()
    )
    assert result is False


def test_repeated_observation_of_same_missing_pr_url_episode_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    task = review_task("r-1", pr_url=None)

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = stranded_alerts.notify.AlertPolicy(
        load_alert_state=stranded_alerts.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=stranded_alerts.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = stranded_alerts.announce_review_missing_pr_url(task, policy=policy)
    second = stranded_alerts.announce_review_missing_pr_url(task, policy=policy)

    assert first is True
    assert second is False
    assert fake_post.calls == 1


# ---------------------------------------------------------------------------
# integration: dispatch.reconcile_review_tasks is the scheduled drain's pass
# ---------------------------------------------------------------------------


def test_reconcile_announces_stuck_doing_task_carrying_bead_id_and_age(monkeypatch):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    sub = FakeSubstrate(
        tasks=[doing_task("task-stuck", "2026-08-31T11:00:00Z", title="wedged")],
        notes={"task-stuck": [claim_note()]},
    )
    policy = RecordingPolicy()
    guard = DispositionGuard()
    monkeypatch.setattr(dispatch, "release_stranded_task", guard.release_stranded_task)
    monkeypatch.setattr(dispatch, "requeue_task", guard.requeue_task)
    monkeypatch.setattr(dispatch, "bind_task_to_pull_request", guard.bind_task_to_pull_request)

    rc = dispatch.reconcile_review_tasks(
        dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
        sub,
        stuck_threshold_minutes=45,
        now=now,
        alert_policy=policy,
    )

    assert rc == 0
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "stranded_doing_task:task-stuck"
    assert "task-stuck" in payload["content"]
    assert "60" in payload["content"]
    assert guard.calls == []
    assert sub.transitions == []
    assert sub.states == []


def test_reconcile_announces_stranded_no_claim_note_doing_task(monkeypatch):
    now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    sub = FakeSubstrate(
        tasks=[doing_task("task-stranded", "2026-08-23T11:58:00Z", title="orphaned")],
    )
    policy = RecordingPolicy()
    guard = DispositionGuard()
    monkeypatch.setattr(dispatch, "release_stranded_task", guard.release_stranded_task)
    monkeypatch.setattr(dispatch, "requeue_task", guard.requeue_task)

    dispatch.reconcile_review_tasks(
        dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
        sub,
        stuck_threshold_minutes=45,
        now=now,
        alert_policy=policy,
    )

    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "stranded_doing_task:task-stranded"
    assert "no_claim_note_recorded" in payload["content"]
    assert guard.calls == []


def test_reconcile_does_not_announce_healthy_doing_task(monkeypatch):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    sub = FakeSubstrate(
        tasks=[doing_task("task-active", "2026-08-31T11:55:00Z", title="active")],
        notes={"task-active": [claim_note()]},
    )
    policy = RecordingPolicy()

    dispatch.reconcile_review_tasks(
        dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
        sub,
        stuck_threshold_minutes=45,
        now=now,
        alert_policy=policy,
    )

    assert policy.sent == []


def test_reconcile_announces_review_task_missing_pr_url(capsys):
    def unexpected_lookup(*_args):
        raise AssertionError("no-pr_url task should not query GitHub")

    sub = FakeSubstrate(tasks=[review_task("task-missing-pr", pr_url=None)])
    policy = RecordingPolicy()

    rc = dispatch.reconcile_review_tasks(
        dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
        sub,
        lookup_pr=unexpected_lookup,
        is_merge_commit_ancestor=lambda _sha, _cfg: True,
        alert_policy=policy,
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "task-missing-pr\tmissing_pr_url\tstate=review" in out
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "review_missing_pr_url:task-missing-pr"
    assert "task-missing-pr" in payload["content"]
    assert sub.transitions == []
    assert sub.states == []
    assert sub.notes == []


def test_reconcile_dry_run_does_not_announce_anything(capsys):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    sub = FakeSubstrate(
        tasks=[
            doing_task("task-stuck", "2026-08-31T11:00:00Z"),
            review_task("task-missing-pr", pr_url=None),
        ],
    )
    policy = RecordingPolicy()

    dispatch.reconcile_review_tasks(
        dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
        sub,
        dry_run=True,
        stuck_threshold_minutes=45,
        now=now,
        alert_policy=policy,
        lookup_pr=lambda *_a: (_ for _ in ()).throw(AssertionError("should not be reached")),
    )

    assert policy.sent == []


def test_reconcile_repeated_calls_do_not_produce_an_unbounded_alert_stream(tmp_path, monkeypatch):
    """The scheduled drain runs reconcile every cycle; an unrecovered stranded
    bead must not post a fresh alert on every pass."""
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    sub = FakeSubstrate(
        tasks=[doing_task("task-stuck", "2026-08-31T11:00:00Z")],
        notes={"task-stuck": [claim_note()]},
    )

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = stranded_alerts.notify.AlertPolicy(
        load_alert_state=stranded_alerts.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=stranded_alerts.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    for _ in range(3):
        dispatch.reconcile_review_tasks(
            dispatch.Config(repo="example/repo", repo_root=Path.cwd()),
            sub,
            stuck_threshold_minutes=45,
            now=now,
            alert_policy=policy,
        )

    assert fake_post.calls == 1


def test_announce_stranded_doing_tasks_reuses_stuck_doing_tasks_detection(monkeypatch):
    """Detection must not be a second implementation: this stubs
    dispatch.stuck_doing_tasks itself and asserts its result decides which
    beads get announced."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    healthy = doing_task("task-healthy", "2026-08-31T11:55:00Z")
    sub = FakeSubstrate(tasks=[healthy], notes={"task-healthy": [claim_note()]})
    policy = RecordingPolicy()

    calls: list[tuple] = []

    def fake_stuck_doing_tasks(beads, checked_now, threshold):
        calls.append((list(beads), checked_now, threshold))
        return [healthy]  # force it "stuck" regardless of age to prove reuse

    monkeypatch.setattr(dispatch, "stuck_doing_tasks", fake_stuck_doing_tasks)

    dispatch.announce_stranded_doing_tasks(sub, 45, now, policy)

    assert calls, "dispatch.stuck_doing_tasks must be called for detection"
    assert len(policy.sent) == 1
    assert policy.sent[0][0] == "stranded_doing_task:task-healthy"
