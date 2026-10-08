"""R26.12 B2: the move changes no pick. dispatch.pick_task's queue-selection
loop body now lives in queue_order.first_selectable; this drives both over
the same inputs, on the two fixtures the outer loop committed with the
first specs (PR #1026, design "Fixtures the outer loop commits with the
first specs"), and asserts they agree.

(a) tests/fixtures/queue-2026-09-23-latched.json -- a real capture with a
    latched bead, replayed as the breaker saw it at capture time (before
    B3 lands): each latched bead's notes are truncated to those no newer
    than its latest matching-signature fault, per header.latched_signatures.

(b) tests/fixtures/queue-synthetic-2026-09-23.json -- a small synthetic
    queue exercising predecessor and release holds alongside today's FIFO.

No substrate, no network: both fixtures replay against the existing
``FakeSubstrate`` double from test_dispatch.py (imported the way
test_release_state_gate.py does) -- no new hand-rolled store double, per
tests/test_store_double_call_surface.py's MAX_HAND_ROLLED_DOUBLES cap.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import dispatch  # noqa: E402
import queue_order  # noqa: E402

from test_dispatch import FakeSubstrate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


def _reduced_note_to_full(note: dict) -> dict:
    content = dict(note.get("content") or {})
    if "body_first_line" in content:
        content["body"] = content.pop("body_first_line")
    return {**note, "content": content}


def _build_all_tasks(fixture: dict) -> list[dict]:
    tasks = list(fixture["tasks"])
    fixture_ids = {t["id"] for t in tasks}
    for task_id, state in fixture["all_task_ids_by_state"].items():
        if task_id in fixture_ids:
            continue
        if state == "pending":
            # Dropped by construction (header.excluded_selectable_ids): a
            # pending stub with no content would fabricate a hold or a pick
            # the capture never had.
            continue
        tasks.append({"id": task_id, "state": state, "content": {}})
    return tasks


def _build_store(fixture: dict, notes_by_task: dict[str, list[dict]]) -> FakeSubstrate:
    all_tasks = _build_all_tasks(fixture)
    notes = {
        task_id: [_reduced_note_to_full(n) for n in notes_by_task.get(task_id, [])]
        for task_id in notes_by_task
    }
    store = FakeSubstrate(tasks=all_tasks, notes=notes, releases=fixture["releases"])
    for link in fixture["links"]:
        store.add_link(link["source_id"], link["target_id"], "delivers", "test")
    return store, all_tasks


def _pending_sorted(all_tasks: list[dict]) -> list[dict]:
    pending = [t for t in all_tasks if (t.get("state") or "pending") == "pending"]
    pending.sort(key=lambda t: t.get("created_at") or "")
    return pending


def test_latched_fixture_pick_agrees_and_is_none(capsys):
    fixture = _load("queue-2026-09-23-latched.json")
    header = fixture["header"]

    # Fixture is not vacuous.
    assert header["pending_count"] >= 1
    assert len(header["hold_classes_present"]) >= 2
    assert len(header["latched_signatures"]) >= 1
    assert header["selectable_ids"] == []

    notes_by_task = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    for signature, latched_ids in header["latched_signatures"].items():
        for task_id in latched_ids:
            notes = notes_by_task[task_id]
            matching = [
                n for n in notes if (n.get("content") or {}).get("environmental_fault_signature") == signature
            ]
            assert matching, f"{task_id} has no note carrying signature {signature}"
            newest = max(matching, key=lambda n: n["created_at"])
            later_exit = header.get("latched_with_later_exit_notes", {}).get(task_id)
            if later_exit is not None:
                assert newest["created_at"] == later_exit["last_fault_at"]
            notes_by_task[task_id] = [n for n in notes if n["created_at"] <= newest["created_at"]]

    store, all_tasks = _build_store(fixture, notes_by_task)
    pending = _pending_sorted(all_tasks)
    release_by_task_id = dispatch.resolve_task_release_states(store, pending)

    pick_via_dispatch = dispatch.pick_task(store, None, all_tasks)
    out_dispatch = capsys.readouterr().out

    pick_via_queue_order = queue_order.first_selectable(store, pending, all_tasks, release_by_task_id)
    out_queue_order = capsys.readouterr().out

    assert pick_via_dispatch is None
    assert pick_via_queue_order is None

    for out in (out_dispatch, out_queue_order):
        skip_lines = {}
        for line in out.splitlines():
            m = re.match(r"^  skip (\S+) — (.*)$", line)
            if m:
                short_id, reason = m.groups()
                assert short_id not in skip_lines, f"{short_id} named in more than one skip line"
                skip_lines[short_id] = reason

        for pending_id in header["pending_ids"]:
            short_id = pending_id[:8]
            assert short_id in skip_lines, f"{pending_id} missing a skip line"

        for latched_ids in header["latched_signatures"].values():
            for task_id in latched_ids:
                assert "same environmental fault recorded" in skip_lines[task_id[:8]]


def test_synthetic_fixture_pick_agrees_and_is_fifo(capsys):
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    expected_held = header["expected_held"]

    notes_by_task = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    store, all_tasks = _build_store(fixture, notes_by_task)
    pending = _pending_sorted(all_tasks)
    release_by_task_id = dispatch.resolve_task_release_states(store, pending)

    pick_via_dispatch = dispatch.pick_task(store, None, all_tasks)
    pick_via_queue_order = queue_order.first_selectable(store, pending, all_tasks, release_by_task_id)

    assert pick_via_dispatch is not None
    assert pick_via_queue_order is not None
    assert pick_via_dispatch["id"] == pick_via_queue_order["id"]
    assert pick_via_dispatch["id"] not in expected_held

    candidates = [t for t in fixture["tasks"] if t["id"] not in expected_held]
    expected_pick = min(candidates, key=lambda t: t["created_at"])
    assert pick_via_dispatch["id"] == expected_pick["id"]
