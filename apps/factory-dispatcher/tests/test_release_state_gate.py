"""2026-08-30 decision record ("the backlog is filed in reverse"), D3.

`pick_task` sorted pending beads oldest-first and asked `guards.is_runnable`
whether each was claimable, but nothing checked the state of the release a
bead *delivers*. Bead 9353814b delivered R26.06/O-1 (sprints 42-44) and was
claimed while R26.06 was still `planned` — sprint-42 work landed in sprint 35,
held back only by a blocking question that answering released regardless of
what it said (OPS-40).

These tests drive `dispatch.pick_task` against a `FakeSubstrate` double --
no substrate, no network, no Temporal -- to prove the release-state filter:
a bead whose `delivers` edge resolves to a release that is not `in_flight` is
skipped and the skip names the release and its state; a bead delivering an
`in_flight` release, or carrying a release waiver and no `delivers` edge at
all, remains selectable; and a substrate that cannot be read at all produces
a refusal rather than an unfiltered queue.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import guards  # noqa: E402

from test_dispatch import FakeSubstrate  # noqa: E402


def task(task_id: str, created_at: str, **content_overrides) -> dict:
    content = {
        "lane": "code-health",
        "title": f"task {task_id}",
        "scope": {"paths": ["apps/factory-dispatcher/"]},
    }
    content.update(content_overrides)
    return {
        "id": task_id,
        "state": "pending",
        "created_at": created_at,
        "content": content,
    }


def release(release_id: str, ref: str, state: str) -> dict:
    return {"id": release_id, "state": state, "content": {"ref": ref}}


class UnreadableReleaseStore(FakeSubstrate):
    """A substrate whose release population cannot be listed at all --
    distinct from a task with no `delivers` edge (which resolves cleanly to
    "nothing to check"). This is the network-partition/outage case."""

    def list_beads(self, namespace, type, **params):
        if namespace == "arch" and type == "release":
            raise RuntimeError("substrate unreachable")
        return super().list_beads(namespace, type, **params)


def test_pick_task_skips_a_bead_delivering_a_planned_release(capsys):
    bead = task("bead-planned", "2026-08-29T17:58:00Z")
    sub = FakeSubstrate(tasks=[bead], releases=[release("rel-1", "R26.06", "planned")])
    sub.add_link("bead-planned", "rel-1", "delivers", "operator")

    picked = dispatch.pick_task(sub, None, sub.list_tasks())

    assert picked is None
    skip_line = [line for line in capsys.readouterr().out.splitlines() if "skip" in line]
    assert skip_line, "expected a skip line naming why the bead was not selectable"
    assert "R26.06" in skip_line[0]
    assert "planned" in skip_line[0]


def test_pick_task_selects_a_bead_delivering_an_in_flight_release():
    bead = task("bead-in-flight", "2026-08-29T17:58:00Z")
    sub = FakeSubstrate(tasks=[bead], releases=[release("rel-1", "R26.01", "in_flight")])
    sub.add_link("bead-in-flight", "rel-1", "delivers", "operator")

    picked = dispatch.pick_task(sub, None, sub.list_tasks())

    assert picked is not None
    assert picked["id"] == "bead-in-flight"


def test_pick_task_orders_two_in_flight_beads_oldest_first_unchanged():
    older = task("bead-older", "2026-08-29T10:00:00Z")
    newer = task("bead-newer", "2026-08-29T11:00:00Z")
    sub = FakeSubstrate(
        tasks=[newer, older], releases=[release("rel-1", "R26.01", "in_flight")]
    )
    sub.add_link("bead-older", "rel-1", "delivers", "operator")
    sub.add_link("bead-newer", "rel-1", "delivers", "operator")

    picked = dispatch.pick_task(sub, None, sub.list_tasks())

    assert picked["id"] == "bead-older"


def test_pick_task_selects_a_waived_bead_with_no_delivers_edge():
    bead = task("bead-waived", "2026-08-29T17:58:00Z", release_ref_waived="not applicable yet")
    sub = FakeSubstrate(tasks=[bead], releases=[release("rel-1", "R26.06", "planned")])
    # Deliberately no add_link: a waived bead names no release at all.

    picked = dispatch.pick_task(sub, None, sub.list_tasks())

    assert picked is not None
    assert picked["id"] == "bead-waived"


def test_pick_task_refuses_when_release_state_cannot_be_read(capsys):
    bead = task("bead-unknown", "2026-08-29T17:58:00Z")
    sub = UnreadableReleaseStore(tasks=[bead])
    sub.add_link("bead-unknown", "rel-1", "delivers", "operator")

    picked = dispatch.pick_task(sub, None, sub.list_tasks())

    assert picked is None
    out = capsys.readouterr().out
    assert "refuse" in out
    assert "substrate unreachable" in out


def test_release_block_reason_pure_function_matches_pick_task_behaviour():
    """The same gate every reader of runnability shares (guards.is_runnable),
    exercised directly with no substrate at all -- the pure-decision-logic
    layer pick_task's behaviour above is built on."""
    planned_bead = task("bead-planned", "2026-08-29T17:58:00Z")
    by_task = {"bead-planned": guards.ReleaseState(ref="R26.07", state="planned")}
    assert guards.release_block_reason(planned_bead, by_task) == (
        "delivers R26.07 (planned), not in_flight"
    )

    in_flight_bead = task("bead-in-flight", "2026-08-29T17:58:00Z")
    by_task = {"bead-in-flight": guards.ReleaseState(ref="R26.01", state="in_flight")}
    assert guards.release_block_reason(in_flight_bead, by_task) == ""

    waived_bead = task("bead-waived", "2026-08-29T17:58:00Z")
    assert guards.release_block_reason(waived_bead, {}) == ""
    assert guards.release_block_reason(waived_bead, None) == ""
