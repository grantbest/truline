"""Operator status reports the claimable pending queue without live services."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
from schedule_runtime import FactoryScheduleStatus  # noqa: E402

NOW = datetime(2026, 8, 14, 13, 20, tzinfo=timezone.utc)
SCHEDULE_ID = "factory-dispatcher-dev"


def content(**overrides):
    base = {
        "lane": "code-health",
        "title": "queue gauge task",
        "intent": "make waiting work visible",
        "context_refs": [],
        "acceptance": ["queue is visible"],
        "verification": {"commands": ["python -m pytest tests/ -q"]},
        "scope": {"paths": ["apps/factory-dispatcher/"], "forbidden_paths": []},
        "risk_class": "behavioral",
        "budget": {"max_agent_minutes": 20, "max_usd": 1.0, "max_tokens": 200000},
    }
    base.update(overrides)
    return base


def task(task_id, state="pending", **overrides):
    return {
        "id": task_id,
        "state": state,
        "content": content(**overrides),
    }


def created_event(created_at):
    """A bead's first event -- `from_state` is never set on creation."""
    return {
        "event_type": "created",
        "from_state": None,
        "to_state": "pending",
        "created_at": created_at,
    }


def transitioned_event(created_at, from_state, to_state):
    return {
        "event_type": "transitioned",
        "from_state": from_state,
        "to_state": to_state,
        "created_at": created_at,
    }


def content_patch_event(created_at):
    """A content-only PATCH on an already-pending task: the substrate records
    `to_state == from_state == "pending"` because the state never changed --
    see dispatch.bind_task_to_release and file_task._repoint_dependents,
    the two live paths that do this to a still-filed task."""
    return {
        "event_type": "updated",
        "from_state": "pending",
        "to_state": "pending",
        "created_at": created_at,
    }


class FakeStore:
    """A hand-rolled double over the BeadStore GET surface only -- no write
    method exists on it at all, so recording every call in ``self.calls`` and
    asserting its contents is a direct way for a caller (e.g. operator_verbs's
    explain-order tests) to prove it made reads and nothing else.

    ``beads`` is keyed by ``(namespace, type)`` -- the same two-argument shape
    ``list_beads`` takes -- so a caller registering, say, the arch/release
    population does not need ``list_beads`` to understand any bead's own
    content to find it.
    """

    def __init__(self, tasks, notes=None, events=None, beads=None, links=None):
        self._tasks = list(tasks)
        self._notes = notes or {}
        self._events = events or {}
        self._beads = beads or {}
        self._links = links or []
        self.calls: list[str] = []

    def list_tasks(self, state=None, limit=200):
        self.calls.append("list_tasks")
        if state is None:
            return list(self._tasks[:limit])
        return [task for task in self._tasks if task.get("state") == state][:limit]

    def list_notes(self, parent_id, limit=500):
        self.calls.append("list_notes")
        return list((self._notes.get(parent_id) or [])[:limit])

    def list_beads(self, namespace, type, **params):
        self.calls.append("list_beads")
        return list(self._beads.get((namespace, type), []))

    def list_links(self, bead_id, *, direction="both", link_type=None):
        self.calls.append("list_links")
        result = []
        for link in self._links:
            matches_direction = (direction in ("outgoing", "both") and link.get("source_id") == bead_id) or (
                direction in ("incoming", "both") and link.get("target_id") == bead_id
            )
            if not matches_direction:
                continue
            if link_type is not None and link.get("link_type") != link_type:
                continue
            result.append(link)
        return result

    def list_events(self, bead_id):
        self.calls.append("list_events")
        events = self._events.get(bead_id)
        if events is None:
            raise AssertionError(f"no fixture events registered for {bead_id!r}")
        return list(events)


class RaisingEventsStore(FakeStore):
    """One task's event read fails -- the whole status must degrade, not crash."""

    def list_events(self, bead_id):
        raise RuntimeError(f"events unavailable for {bead_id}")


def quiet_status():
    return FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=True,
        in_flight=(),
        recent=(),
    )


def render_for(tasks, notes=None, events=None, store=None):
    queue = schedule_status.describe_waiting_queue(
        store or FakeStore(tasks, notes, events),
        now=NOW,
    )
    return schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        queue_status=queue,
        now=NOW,
    )


def failure_notes(task_id, count):
    return [
        {
            "id": f"{task_id}-failure-{index}",
            "parent_id": task_id,
            "content": {"kind": "status", "body": f"Run failed: {index}"},
            "created_at": f"2026-08-14T08:0{index}:00Z",
        }
        for index in range(1, count + 1)
    ]


def test_waiting_count_and_oldest_age_render_from_known_beads():
    rendered = render_for(
        [
            task("new-claimable"),
            task("done", state="review"),
            task("old-claimable"),
        ],
        events={
            "new-claimable": [created_event("2026-08-14T13:05:00Z")],  # 15m before NOW
            "old-claimable": [created_event("2026-08-14T08:05:00Z")],  # 5h15m before NOW
        },
    )

    assert "waiting_claimable_tasks: 2" in rendered
    assert "oldest_waiting_age: 5h 15m" in rendered
    assert "pending_not_claimable_tasks: 0" in rendered


def test_non_runnable_pending_bead_is_not_counted_as_claimable_waiting_work():
    rendered = render_for(
        [
            task("exhausted"),
            task("claimable"),
        ],
        notes={"exhausted": failure_notes("exhausted", 3)},
        events={
            # "exhausted" is filtered out before ages are read -- no events
            # fixture needed, and FakeStore.list_events would raise if it
            # were consulted, proving that.
            "claimable": [created_event("2026-08-14T12:50:00Z")],  # 30m before NOW
        },
    )

    assert "waiting_claimable_tasks: 1" in rendered
    assert "oldest_waiting_age: 30m" in rendered
    assert "pending_not_claimable_tasks: 1" in rendered


def test_unreachable_substrate_renders_could_not_determine_not_zero():
    class UnreachableStore:
        def list_tasks(self, state=None, limit=200):
            raise RuntimeError("substrate unavailable")

    queue = schedule_status.describe_waiting_queue(UnreachableStore(), now=NOW)
    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        queue_status=queue,
        now=NOW,
    )

    assert "waiting_claimable_tasks: could-not-determine" in rendered
    assert "oldest_waiting_age: could-not-determine" in rendered
    assert "pending_not_claimable_tasks: could-not-determine" in rendered
    assert "waiting_queue_error: substrate unavailable" in rendered
    assert "waiting_claimable_tasks: 0" not in rendered


def test_requeued_task_ages_from_when_it_became_claimable_not_when_it_was_filed():
    """Mirrors bead 466dd117 (OBSERVED 2026-09-16 19:26Z): filed 3h01m before
    the check, sat in `review` with an open PR, and was requeued back to
    `pending` only 6m before it -- for almost all of those three hours it was
    not claimable at all. This is the case #923 fixed and this bead must not
    regress: the reported age must reflect the 6 minutes since the last
    `transitioned` event into `pending`, not the 3+ hours since the `created`
    event.
    """
    rendered = render_for(
        [task("466dd117")],
        events={
            "466dd117": [
                created_event("2026-08-14T10:19:00Z"),  # filed 3h01m before NOW
                transitioned_event(
                    "2026-08-14T10:20:00Z", from_state="pending", to_state="review"
                ),
                transitioned_event(
                    "2026-08-14T13:14:00Z",  # requeued 6m before NOW
                    from_state="review",
                    to_state="pending",
                ),
            ],
        },
    )

    assert "waiting_claimable_tasks: 1" in rendered
    assert "oldest_waiting_age: 6m" in rendered
    assert "oldest_waiting_age: 3h" not in rendered


def test_content_only_patch_of_a_pending_task_does_not_erase_days_of_starvation():
    """The headline case this bead exists for. A task has been claimable for
    5 days, untouched, when a sprint-planning sweep (dispatch.bind_task_to_release
    or file_task._repoint_dependents) content-patches it without transitioning
    it -- the substrate records an `updated` event with `to_state ==
    from_state == "pending"`. The reported age must still read 5 days, not
    reset to the 1-minute-old patch: filtering the event log on `to_state ==
    "pending"` alone would get this wrong (that patch event qualifies), which
    is exactly why `_became_claimable_at` also requires `from_state !=
    "pending"`.
    """
    rendered = render_for(
        [task("stale-but-patched")],
        events={
            "stale-but-patched": [
                created_event("2026-08-09T13:20:00Z"),  # claimable 5 days before NOW
                content_patch_event("2026-08-14T13:19:00Z"),  # patched 1m before NOW
            ],
        },
    )

    assert "waiting_claimable_tasks: 1" in rendered
    assert "oldest_waiting_age: 5d" in rendered
    assert "oldest_waiting_age: 1m" not in rendered


def test_one_unresolvable_task_among_claimable_renders_unknown_not_the_others_age():
    """AC-5, from #937's gate finding 4 (measured at its head): two claimable
    tasks, one whose event log resolves to no became-claimable moment and one
    filed 5 minutes ago, rendered `oldest_waiting_age: 5m` -- silently
    dropping the unresolvable task's own starvation, which could be enormous.
    The correct answer is `unknown`, not the minimum of the resolvable ones.
    """
    rendered = render_for(
        [
            task("unresolvable"),
            task("resolvable"),
        ],
        events={
            "unresolvable": [],  # no event ever put it in `pending` -- unresolvable
            "resolvable": [created_event("2026-08-14T13:15:00Z")],  # 5m before NOW
        },
    )

    assert "waiting_claimable_tasks: 2" in rendered
    assert "oldest_waiting_age: unknown" in rendered
    assert "oldest_waiting_age: 5m" not in rendered


def test_single_claimable_task_with_unresolvable_event_log_renders_unknown():
    rendered = render_for(
        [task("no-qualifying-event")],
        events={"no-qualifying-event": []},
    )

    assert "waiting_claimable_tasks: 1" in rendered
    assert "oldest_waiting_age: unknown" in rendered
    assert "oldest_waiting_age: 0s" not in rendered
    assert "oldest_waiting_age: could-not-determine" not in rendered


def test_a_failing_event_read_degrades_the_whole_status_instead_of_crashing():
    """AC-7: an events read that fails must still render `could-not-determine`
    -- the same outer `describe_waiting_queue` try/except that already covers
    an unreachable `list_tasks` covers a failing `list_events` too, so this
    must not raise out of `render_for`.
    """
    rendered = render_for(
        [task("boom")],
        store=RaisingEventsStore([task("boom")], events={"boom": []}),
    )

    assert "waiting_claimable_tasks: could-not-determine" in rendered
    assert "oldest_waiting_age: could-not-determine" in rendered
    assert "waiting_queue_error: events unavailable for boom" in rendered
