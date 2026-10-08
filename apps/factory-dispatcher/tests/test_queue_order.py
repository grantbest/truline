"""R26.12 B7 (docs/plans/2026-09-23-design-r2612-steerable-and-legible.md,
"Mechanism" > "Queue order"): the declared queue order -- time-box urgency x
per-outcome impact, predecessor inheritance, bounded aging on an injected
claimable_since -- as a pure, unwired function that explains every position.

Every call to ``queue_order.rank_pending`` in this file goes through ``_rank``
(AC-15): the one seam a later bead's new argument has to touch.

No substrate, no network: the two fixtures the outer loop committed
(tests/fixtures/queue-synthetic-2026-09-23.json,
tests/fixtures/queue-2026-09-23-latched.json) are read as plain JSON and
turned into rank_pending's injected inputs by this file's own helpers.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import guards  # noqa: E402
import queue_order  # noqa: E402
import retry_policy  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

DEFAULT_SCOPE = {"scope": {"paths": ["apps/factory-dispatcher/queue_order.py"]}}


# ---------------------------------------------------------------------------
# AC-15: the one seam every rank_pending call in this file goes through.
# ---------------------------------------------------------------------------


def _rank(inputs: dict, **overrides) -> queue_order.QueueOrder:
    kwargs = dict(inputs)
    kwargs.update(overrides)
    return queue_order.rank_pending(**kwargs)


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------


def _load(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    text = value
    if "T" not in text:
        text = text + "T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _all_positions(order: queue_order.QueueOrder) -> dict[str, queue_order.QueuePosition]:
    return {p.task_id: p for p in (*order.ranked, *order.held)}


def _reduced_note_to_full(note: dict) -> dict:
    content = dict(note.get("content") or {})
    if "body_first_line" in content:
        content["body"] = content.pop("body_first_line")
    return {**note, "content": content}


def _build_all_tasks_from_ids(tasks: list[dict], all_task_ids_by_state: dict[str, str]) -> list[dict]:
    """``tasks`` plus a ``{id, state, content: {}}`` stub for every id in
    ``all_task_ids_by_state`` that is not already present and not pending --
    a pending id missing from ``tasks`` was dropped by fixture construction
    on purpose (header.excluded_selectable_ids) and must not be revived."""
    result = list(tasks)
    present_ids = {t["id"] for t in result}
    for task_id, state in all_task_ids_by_state.items():
        if task_id in present_ids:
            continue
        if state == "pending":
            continue
        result.append({"id": task_id, "state": state, "content": {}})
    return result


def _release_by_task_id(fixture: dict) -> dict[str, guards.ReleaseState]:
    releases_by_id = {r["id"]: r for r in fixture["releases"]}
    result: dict[str, guards.ReleaseState] = {}
    for link in fixture["links"]:
        release = releases_by_id[link["target_id"]]
        result[link["source_id"]] = guards.ReleaseState(
            ref=release["content"]["ref"], state=release["state"]
        )
    return result


def _time_box_by_release_ref(fixture: dict) -> dict[str, queue_order.TimeBox]:
    result: dict[str, queue_order.TimeBox] = {}
    for release in fixture["releases"]:
        content = release["content"]
        result[content["ref"]] = queue_order.TimeBox(
            opened_at=_parse_dt(content.get("opened_at")),
            target_at=_parse_dt(content.get("target_at")),
        )
    return result


def _synthetic_inputs(fixture: dict, tasks_override: list[dict] | None = None) -> dict:
    header = fixture["header"]
    tasks = tasks_override if tasks_override is not None else list(fixture["tasks"])
    all_tasks = _build_all_tasks_from_ids(tasks, fixture["all_task_ids_by_state"])
    notes_by_id = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    claimable_since_by_task_id = {
        t["id"]: _parse_dt(t["claimable_since"]) for t in fixture["tasks"] if "claimable_since" in t
    }
    return dict(
        tasks=all_tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=_release_by_task_id(fixture),
        time_box_by_release_ref=_time_box_by_release_ref(fixture),
        policy=queue_order.RankPolicy.from_mapping(header["policy"]),
        now=_parse_dt(header["now"]),
        claimable_since_by_task_id=claimable_since_by_task_id,
    )


def _prepare_latched_notes(fixture: dict) -> dict[str, list[dict]]:
    """AC-9 step (2): truncate each latched bead's notes to those no newer
    than its latest matching-signature fault -- the same rule
    test_queue_order_parity.py's fixture replay already proved correct."""
    header = fixture["header"]
    notes_by_id = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    for signature, latched_ids in header["latched_signatures"].items():
        for task_id in latched_ids:
            notes = notes_by_id[task_id]
            matching = [
                n
                for n in notes
                if (n.get("content") or {}).get("environmental_fault_signature") == signature
            ]
            newest = max(matching, key=lambda n: n["created_at"])
            notes_by_id[task_id] = [n for n in notes if n["created_at"] <= newest["created_at"]]
    return notes_by_id


def _prepare_latched(fixture: dict) -> dict:
    """The one helper every test replaying queue-2026-09-23-latched.json uses
    (AC-9): body_first_line restored to body, latched notes truncated, non-
    pending stubs added, claimable_since_by_task_id empty so waiting_since
    falls back to created_at throughout."""
    header = fixture["header"]
    notes_by_id = {
        tid: [_reduced_note_to_full(n) for n in notes]
        for tid, notes in _prepare_latched_notes(fixture).items()
    }
    all_tasks = _build_all_tasks_from_ids(fixture["tasks"], fixture["all_task_ids_by_state"])
    return dict(
        tasks=all_tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=_release_by_task_id(fixture),
        time_box_by_release_ref=_time_box_by_release_ref(fixture),
        policy=queue_order.RankPolicy(
            urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
        ),
        now=_parse_dt(header["captured_at"]),
        claimable_since_by_task_id={},
    )


class _NotesOnlyStore:
    """A store double exposing only ``list_notes`` -- one BeadStore-named
    method, below the two-name overlap the hand-rolled-double ratchet
    (tests/test_store_double_call_surface.py) counts, so this does not grow
    that ceiling."""

    def __init__(self, notes_by_id: dict[str, list[dict]]):
        self._notes_by_id = notes_by_id

    def list_notes(self, parent_id: str, limit: int = 500) -> list[dict]:
        return list(self._notes_by_id.get(parent_id, []))


# ===========================================================================
# AC-1: module surface
# ===========================================================================


def test_timebox_and_queueposition_and_queueorder_are_frozen_dataclasses():
    for cls in (queue_order.TimeBox, queue_order.RankPolicy, queue_order.QueuePosition, queue_order.QueueOrder):
        assert cls.__dataclass_params__.frozen is True


def test_rankpolicy_from_mapping_reads_only_declared_keys_and_defaults_max_aging_steps():
    policy = queue_order.RankPolicy.from_mapping(
        {
            "urgency_bands": {"medium": 0.3, "high": 0.7},
            "aging_days": 10,
            "some_other_governed_key": "ignored",
            "declared_balance": {"feature": 100},
        }
    )
    assert policy.urgency_band_medium == 0.3
    assert policy.urgency_band_high == 0.7
    assert policy.aging_days == 10
    assert policy.max_aging_steps == 3
    assert policy.policy_revision is None


def test_rankpolicy_from_mapping_accepts_explicit_max_aging_steps_and_revision():
    policy = queue_order.RankPolicy.from_mapping(
        {"urgency_bands": {"medium": 0.33, "high": 0.66}, "aging_days": 14, "max_aging_steps": 1},
        policy_revision="rev-1",
    )
    assert policy.max_aging_steps == 1
    assert policy.policy_revision == "rev-1"


@pytest.mark.parametrize(
    "mapping",
    [
        {"urgency_bands": {"medium": 0.66, "high": 0.33}, "aging_days": 14},  # medium >= high
        {"urgency_bands": {"medium": 0.5, "high": 0.5}, "aging_days": 14},  # medium == high
        {"urgency_bands": {"medium": 0, "high": 0.66}, "aging_days": 14},  # medium not > 0
        {"urgency_bands": {"medium": 0.33, "high": 1.5}, "aging_days": 14},  # high > 1
        {"urgency_bands": {"medium": 0.33, "high": 0.66}, "aging_days": 0},  # aging_days not positive
        {"urgency_bands": {"medium": 0.33, "high": 0.66}, "aging_days": 14.5},  # aging_days not int
        {"urgency_bands": {"medium": 0.33, "high": 0.66}, "aging_days": 14, "max_aging_steps": 4},  # out of range
        {"urgency_bands": {"medium": 0.33, "high": 0.66}, "aging_days": 14, "max_aging_steps": -1},
        {"urgency_bands": {"high": 0.66}, "aging_days": 14},  # missing medium
        {"aging_days": 14},  # missing urgency_bands entirely
    ],
)
def test_rankpolicy_from_mapping_rejects_invalid_policies(mapping):
    with pytest.raises(ValueError):
        queue_order.RankPolicy.from_mapping(mapping)


def test_rank_pending_requires_timezone_aware_now():
    task = {"id": "T1", "state": "pending", "created_at": "2026-01-01T00:00:00Z", "content": DEFAULT_SCOPE}
    with pytest.raises(ValueError):
        _rank(
            dict(
                tasks=[task],
                notes_by_id={"T1": []},
                release_by_task_id={},
                time_box_by_release_ref={},
                policy=queue_order.RankPolicy(
                    urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
                ),
                now=datetime(2026, 1, 1),  # naive
                claimable_since_by_task_id={},
            )
        )


def test_policy_revision_carried_through_to_queue_order_and_json():
    task = {"id": "T1", "state": "pending", "created_at": "2020-01-01T00:00:00Z", "content": dict(DEFAULT_SCOPE)}
    policy = queue_order.RankPolicy(
        urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3, policy_revision="rev-7"
    )
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={"T1": []},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=policy,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            claimable_since_by_task_id={},
        )
    )
    assert order.policy_revision == "rev-7"
    assert "rev-7" in order.to_json()


def test_policy_revision_carried_through_on_unknown_input_path():
    task = {"id": "T1", "state": "pending", "created_at": "2020-01-01T00:00:00Z", "content": dict(DEFAULT_SCOPE)}
    policy = queue_order.RankPolicy(
        urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3, policy_revision="rev-9"
    )
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={"T1": []},
            release_by_task_id=None,
            time_box_by_release_ref={},
            policy=policy,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            claimable_since_by_task_id={},
        )
    )
    assert order.ranked == ()
    assert order.held == ()
    assert order.inputs_unavailable == ("release_by_task_id",)
    assert order.policy_revision == "rev-9"
    assert "rev-9" in order.to_json()


# ===========================================================================
# AC-2: urgency
# ===========================================================================


def _urgency_position(*, release, time_box_by_release_ref, now, outcome_ref=None):
    task_id = "T1"
    task = {
        "id": task_id,
        "state": "pending",
        "created_at": "2020-01-01T00:00:00Z",
        "content": {**DEFAULT_SCOPE, "outcome_ref": outcome_ref},
    }
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={task_id: []},
            release_by_task_id={task_id: release} if release is not None else {},
            time_box_by_release_ref=time_box_by_release_ref,
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={task_id: now},
        )
    )
    return _all_positions(order)[task_id]


_OPENED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TARGET = _OPENED + timedelta(days=100)
_RELEASE = guards.ReleaseState(ref="R1", state="in_flight")


def test_urgency_exactly_at_medium_band_edge():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=_TARGET)
    now = _OPENED + timedelta(days=33)  # elapsed == 0.33
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=now)
    assert (p.urgency, p.urgency_source) == ("medium", "time_box")


def test_urgency_exactly_at_high_band_edge():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=_TARGET)
    now = _OPENED + timedelta(days=66)  # elapsed == 0.66
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=now)
    assert (p.urgency, p.urgency_source) == ("high", "time_box")


def test_urgency_high_when_target_has_passed():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=_TARGET)
    now = _TARGET + timedelta(days=5)
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=now)
    assert (p.urgency, p.urgency_source) == ("high", "time_box")


def test_urgency_low_when_release_not_yet_opened():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=_TARGET)
    now = _OPENED - timedelta(days=10)
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=now)
    assert (p.urgency, p.urgency_source) == ("low", "time_box")


def test_urgency_time_unmeasurable_when_time_box_missing_for_ref():
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={}, now=_OPENED + timedelta(days=10))
    assert (p.urgency, p.urgency_source) == ("low", "time_unmeasurable")
    assert p.release_ref == "R1"


def test_urgency_time_unmeasurable_when_opened_at_missing():
    time_box = queue_order.TimeBox(opened_at=None, target_at=_TARGET)
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=_OPENED + timedelta(days=10))
    assert (p.urgency, p.urgency_source) == ("low", "time_unmeasurable")


def test_urgency_time_unmeasurable_when_target_at_missing():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=None)
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=_OPENED + timedelta(days=10))
    assert (p.urgency, p.urgency_source) == ("low", "time_unmeasurable")


def test_urgency_time_unmeasurable_when_target_at_not_after_opened_at():
    time_box = queue_order.TimeBox(opened_at=_OPENED, target_at=_OPENED)
    p = _urgency_position(release=_RELEASE, time_box_by_release_ref={"R1": time_box}, now=_OPENED + timedelta(days=10))
    assert (p.urgency, p.urgency_source) == ("low", "time_unmeasurable")


def test_urgency_no_release_when_task_absent_from_release_by_task_id():
    p = _urgency_position(release=None, time_box_by_release_ref={}, now=_OPENED + timedelta(days=10))
    assert (p.urgency, p.urgency_source) == ("low", "no_release")
    assert p.release_ref is None


# ===========================================================================
# AC-3: impact
# ===========================================================================


def test_impact_defaults_to_medium_across_whole_synthetic_fixture_when_none_injected():
    fixture = _load("queue-synthetic-2026-09-23.json")
    order = _rank(_synthetic_inputs(fixture), impact_by_outcome=None)
    for position in (*order.ranked, *order.held):
        assert position.impact == "medium"
        assert position.impact_source == "default"


def test_impact_outcome_override_lifts_one_task_and_leaves_another_outcome_at_default():
    fixture = _load("queue-synthetic-2026-09-23.json")
    tasks = [dict(t) for t in fixture["tasks"]]
    by_id = {t["id"]: t for t in tasks}

    lifted = dict(by_id["Q"])
    lifted["content"] = dict(lifted["content"])
    lifted["content"]["outcome_ref"] = "O-1"
    by_id["Q"] = lifted

    unaffected = dict(by_id["Y"])
    unaffected["content"] = dict(unaffected["content"])
    unaffected["content"]["outcome_ref"] = "O-9"
    by_id["Y"] = unaffected

    inputs = _synthetic_inputs(fixture, tasks_override=list(by_id.values()))
    order = _rank(inputs, impact_by_outcome={"RA/O-1": "high"})
    positions = _all_positions(order)

    assert positions["Q"].impact == "high"
    assert positions["Q"].impact_source == "outcome"
    assert positions["Q"].priority == 3  # low urgency x high impact

    assert positions["Y"].impact == "medium"
    assert positions["Y"].impact_source == "default"
    assert positions["Y"].priority == 4


def test_impact_outcome_override_with_invalid_value_raises_naming_the_key():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    with pytest.raises(ValueError, match=re.escape("RB/O-1")):
        _rank(inputs, impact_by_outcome={"RB/O-1": "urgent"})


# ===========================================================================
# AC-4: matrix -- all nine (urgency, impact) pairs
# ===========================================================================


def _priority_case(urgency_choice: str, impact_choice: str) -> queue_order.QueuePosition:
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    target = opened + timedelta(days=100)
    elapsed_days = {"low": 10, "medium": 50, "high": 100}[urgency_choice]
    now = opened + timedelta(days=elapsed_days)
    task_id = "T1"
    task = {
        "id": task_id,
        "state": "pending",
        "created_at": "2020-01-01T00:00:00Z",
        "content": {**DEFAULT_SCOPE, "outcome_ref": "O-1"},
    }
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={task_id: []},
            release_by_task_id={task_id: guards.ReleaseState(ref="R1", state="in_flight")},
            time_box_by_release_ref={"R1": queue_order.TimeBox(opened_at=opened, target_at=target)},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={task_id: now},
        ),
        impact_by_outcome={"R1/O-1": impact_choice},
    )
    return _all_positions(order)[task_id]


@pytest.mark.parametrize(
    "urgency,impact,expected",
    [
        ("high", "high", 1),
        ("high", "medium", 2),
        ("medium", "high", 2),
        ("medium", "medium", 3),
        ("high", "low", 3),
        ("low", "high", 3),
        ("medium", "low", 4),
        ("low", "medium", 4),
        ("low", "low", 4),
    ],
)
def test_priority_matrix_all_nine_pairs(urgency, impact, expected):
    position = _priority_case(urgency, impact)
    assert position.urgency == urgency
    assert position.impact == impact
    assert position.priority == expected


# ===========================================================================
# AC-5: aging
# ===========================================================================


def _aging_position(*, claimable_since, created_at, now, aging_days=14, max_aging_steps=3):
    task_id = "T1"
    task = {"id": task_id, "state": "pending", "created_at": created_at, "content": dict(DEFAULT_SCOPE)}
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={task_id: []},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=aging_days, max_aging_steps=max_aging_steps
            ),
            now=now,
            claimable_since_by_task_id={task_id: claimable_since} if claimable_since is not None else {},
        )
    )
    return _all_positions(order)[task_id]


_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_aging_one_step_at_aging_days_plus_one():
    p = _aging_position(claimable_since=_NOW - timedelta(days=15), created_at="2020-01-01T00:00:00Z", now=_NOW)
    assert p.age_days == 15
    assert p.aged_steps == 1


def test_aging_zero_steps_at_thirteen_days():
    p = _aging_position(claimable_since=_NOW - timedelta(days=13), created_at="2020-01-01T00:00:00Z", now=_NOW)
    assert p.age_days == 13
    assert p.aged_steps == 0


def test_aging_caps_at_max_aging_steps_and_p4_reaches_p1_but_no_further():
    p = _aging_position(claimable_since=_NOW - timedelta(days=100), created_at="2020-01-01T00:00:00Z", now=_NOW)
    assert p.age_days == 100
    assert p.aged_steps == 3
    assert p.priority == 4  # no_release x default-medium, own tier
    assert p.effective_priority == 1


def test_aging_requeue_case_claimable_since_now_ignores_old_created_at():
    p = _aging_position(
        claimable_since=_NOW, created_at=(_NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), now=_NOW
    )
    assert p.age_days == 0
    assert p.waiting_since_source == "injected"


def test_waiting_since_falls_back_to_created_at_when_claimable_since_absent():
    created_at = (_NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = _aging_position(claimable_since=None, created_at=created_at, now=_NOW)
    assert p.waiting_since_source == "created_at"
    assert p.age_days == 5


def test_aging_future_claimable_since_clamps_age_days_to_zero_not_negative():
    p = _aging_position(claimable_since=_NOW + timedelta(days=5), created_at="2020-01-01T00:00:00Z", now=_NOW)
    assert p.age_days == 0
    assert p.aged_steps == 0


# ===========================================================================
# AC-6: inheritance and unblocks
# ===========================================================================


def test_inheritance_three_bead_chain_propagates_through_middle_without_leaking_target():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    time_box_a = queue_order.TimeBox(opened_at=now - timedelta(days=10), target_at=now + timedelta(days=1000))
    time_box_c = queue_order.TimeBox(opened_at=now - timedelta(days=200), target_at=now - timedelta(days=1))

    def task(task_id, predecessor_bead_ids, outcome_ref=None):
        return {
            "id": task_id,
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids, "outcome_ref": outcome_ref},
        }

    tasks = [
        task("A", []),
        task("B", ["A"]),
        task("C", ["B"], outcome_ref="O-1"),
    ]
    order = _rank(
        dict(
            tasks=tasks,
            notes_by_id={t["id"]: [] for t in tasks},
            release_by_task_id={
                "A": guards.ReleaseState(ref="RA", state="in_flight"),
                "C": guards.ReleaseState(ref="RC", state="in_flight"),
            },
            time_box_by_release_ref={"RA": time_box_a, "RC": time_box_c},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={t["id"]: now for t in tasks},
        ),
        impact_by_outcome={"RC/O-1": "high"},
    )
    positions = _all_positions(order)

    assert positions["C"].priority == 1
    assert positions["C"].effective_priority == 1
    assert positions["C"].inherited_from is None

    assert positions["A"].priority == 4
    assert positions["A"].effective_priority == 1
    assert positions["A"].inherited_from == "C"
    assert positions["A"].release_target_at == time_box_a.target_at  # own release's target, not C's

    assert positions["B"].priority == 4
    assert positions["B"].effective_priority == 1
    assert positions["B"].inherited_from == "C"

    assert positions["A"].unblocks == ("B",)
    assert positions["B"].unblocks == ("C",)
    assert positions["C"].unblocks == ()


def test_inheritance_two_bead_cycle_terminates_and_both_held():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def task(task_id, predecessor_bead_ids):
        return {
            "id": task_id,
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids},
        }

    tasks = [task("A", ["B"]), task("B", ["A"])]
    order = _rank(
        dict(
            tasks=tasks,
            notes_by_id={t["id"]: [] for t in tasks},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={t["id"]: now for t in tasks},
        )
    )

    assert order.ranked == ()
    held_ids = {p.task_id for p in order.held}
    assert held_ids == {"A", "B"}
    for p in order.held:
        assert "predecessor cycle detected" in p.hold_reasons[0]


def test_unblocks_is_sorted_regardless_of_input_order_with_multiple_dependents():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def task(task_id, predecessor_bead_ids):
        return {
            "id": task_id,
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids},
        }

    # "Zed" and "Able" both name "Base" as a predecessor; "Zed" appears
    # first in the input list, so unblocks must be sorted, not left in
    # input/discovery order.
    tasks = [task("Base", []), task("Zed", ["Base"]), task("Able", ["Base"])]
    order = _rank(
        dict(
            tasks=tasks,
            notes_by_id={t["id"]: [] for t in tasks},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={t["id"]: now for t in tasks},
        )
    )
    positions = _all_positions(order)
    assert positions["Base"].unblocks == ("Able", "Zed")


def test_unblocks_dedupes_repeated_predecessor_and_excludes_self_reference():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def task(task_id, predecessor_bead_ids):
        return {
            "id": task_id,
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids},
        }

    # "Dup" names "Base" as a predecessor twice; "SelfRef" names itself as
    # well as "Base". Neither repetition nor self-naming may make a
    # dependent appear more than once in its predecessor's unblocks.
    tasks = [
        task("Base", []),
        task("Dup", ["Base", "Base"]),
        task("SelfRef", ["SelfRef", "Base"]),
    ]
    order = _rank(
        dict(
            tasks=tasks,
            notes_by_id={t["id"]: [] for t in tasks},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={t["id"]: now for t in tasks},
        )
    )
    positions = _all_positions(order)

    assert positions["Base"].unblocks == ("Dup", "SelfRef")
    assert positions["Dup"].unblocks == ()
    assert positions["SelfRef"].unblocks == ()

    for task_id in ("Base", "Dup", "SelfRef"):
        expected = tuple(sorted(c["id"] for c in guards.dependents_of(task_id, tasks)))
        assert positions[task_id].unblocks == expected


def test_inheritance_diamond_tie_picks_lowest_id_dependent():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def task(task_id, predecessor_bead_ids, outcome_ref=None):
        return {
            "id": task_id,
            "state": "pending",
            "created_at": "2020-01-01T00:00:00Z",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids, "outcome_ref": outcome_ref},
        }

    # "Zeta" and "Alpha" both name "Base" as a predecessor and tie at the
    # same (better) aged tier; "Zeta" is discovered first (it precedes
    # "Alpha" in input order), so a correct tie-break must still choose the
    # lowest id, "Alpha".
    tasks = [
        task("Base", []),
        task("Zeta", ["Base"], outcome_ref="O-1"),
        task("Alpha", ["Base"], outcome_ref="O-1"),
    ]
    order = _rank(
        dict(
            tasks=tasks,
            notes_by_id={t["id"]: [] for t in tasks},
            release_by_task_id={
                "Zeta": guards.ReleaseState(ref="RX", state="in_flight"),
                "Alpha": guards.ReleaseState(ref="RX", state="in_flight"),
            },
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=now,
            claimable_since_by_task_id={t["id"]: now for t in tasks},
        ),
        impact_by_outcome={"RX/O-1": "high"},
    )
    positions = _all_positions(order)

    assert positions["Zeta"].priority == 3  # low urgency (no time box) x high impact
    assert positions["Alpha"].priority == 3
    assert positions["Base"].priority == 4  # no_release x default-medium

    assert positions["Base"].effective_priority == 3
    assert positions["Base"].inherited_from == "Alpha"


# ===========================================================================
# AC-7: the key, the split and conservation
# ===========================================================================


def _base_position() -> queue_order.QueuePosition:
    """A real rank_pending position (a selectable, otherwise-featureless
    pending task) used as the template ``_position`` overrides via
    ``dataclasses.replace`` (R1): no test pins QueuePosition's full field set
    by constructing one directly."""
    task_id = "BASE"
    task = {"id": task_id, "state": "pending", "created_at": "2026-01-01T00:00:00Z", "content": dict(DEFAULT_SCOPE)}
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={task_id: []},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            claimable_since_by_task_id={},
        )
    )
    return _all_positions(order)[task_id]


def _position(task_id, *, release_target_at=None, effective_priority=4, waiting_since=None, class_of_service="normal"):
    return dataclasses.replace(
        _base_position(),
        task_id=task_id,
        class_of_service=class_of_service,
        priority=effective_priority,
        effective_priority=effective_priority,
        waiting_since=waiting_since or datetime(2026, 1, 1, tzinfo=timezone.utc),
        release_target_at=release_target_at,
    )


def test_rank_key_orders_missing_target_at_after_every_present_one():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with_target = _position("A", release_target_at=now + timedelta(days=5))
    without_target = _position("B", release_target_at=None)
    ordered = sorted([without_target, with_target], key=queue_order.rank_key)
    assert [p.task_id for p in ordered] == ["A", "B"]


def test_rank_key_orders_by_effective_priority_then_target_then_waiting_since_then_id():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p_best = _position("Z", effective_priority=1, waiting_since=now)
    p_mid_earlier_target = _position("Y", effective_priority=2, release_target_at=now, waiting_since=now)
    p_mid_later_target = _position("X", effective_priority=2, release_target_at=now + timedelta(days=1), waiting_since=now)
    ordered = sorted([p_mid_later_target, p_best, p_mid_earlier_target], key=queue_order.rank_key)
    assert [p.task_id for p in ordered] == ["Z", "Y", "X"]


def test_selectable_verdict_true_when_runnable_and_no_breaker():
    task = {"id": "T1", "state": "pending", "content": dict(DEFAULT_SCOPE)}
    verdict = queue_order.selectable_verdict(task, [], [task], {})
    assert verdict == queue_order.SelectableVerdict(selectable=True, hold_reasons=(), latched=False)


def test_selectable_verdict_false_when_is_runnable_fails():
    task = {"id": "T1", "state": "pending", "content": {}}  # no scope.paths
    verdict = queue_order.selectable_verdict(task, [], [task], {})
    assert verdict.selectable is False
    assert verdict.hold_reasons == ("task declares no scope.paths",)
    assert verdict.latched is False


def test_selectable_verdict_false_when_environmental_fault_latched():
    task = {"id": "T1", "state": "pending", "content": dict(DEFAULT_SCOPE)}
    notes = [
        {
            "id": f"n{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": "sig1",
            },
        }
        for i in range(1, 4)
    ]
    verdict = queue_order.selectable_verdict(task, notes, [task], {})
    assert verdict.selectable is False
    assert verdict.hold_reasons[0].startswith("same environmental fault recorded 3 consecutive times")
    assert verdict.latched is True


def test_selectable_verdict_latch_suppressed_when_breaker_not_applied():
    """AC-1/AC-2: apply_breaker=False is the --task operator-force escape
    hatch -- the streak is not even computed, latched stays False, and no
    latch hold is added."""
    task = {"id": "T1", "state": "pending", "content": dict(DEFAULT_SCOPE)}
    notes = [
        {
            "id": f"n{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": "sig1",
            },
        }
        for i in range(1, 4)
    ]
    verdict = queue_order.selectable_verdict(task, notes, [task], {}, apply_breaker=False)
    assert verdict == queue_order.SelectableVerdict(selectable=True, hold_reasons=(), latched=False)


def test_selectable_verdict_collects_every_applicable_hold_in_design_order():
    """AC-1: a bead carrying three holds at once (attempts exhausted, a
    blocking question, and no scope.paths) must surface all three, in the
    design's declared order, with the latch appended last."""
    content = {
        "lane": "code-health",
        "title": "t",
        # no scope.paths -- item 7
    }
    task = {"id": "T1", "state": "pending", "content": content}
    failure_notes = [
        {
            "id": f"fail{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {"kind": "status", "body": f"Run failed: attempt {i}"},
        }
        for i in range(1, retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS + 1)
    ]
    question_note = {
        "id": "q1",
        "created_at": "2026-01-05T00:00:00Z",
        "content": {"kind": "question", "blocking": True, "body": "needs a decision"},
    }
    notes = failure_notes + [question_note]

    verdict = queue_order.selectable_verdict(task, notes, [task], {})

    assert verdict.selectable is False
    assert verdict.latched is False
    assert len(verdict.hold_reasons) == 3
    assert verdict.hold_reasons[0].startswith("attempts exhausted")
    assert verdict.hold_reasons[1].startswith("waiting on blocking question")
    assert verdict.hold_reasons[2] == "task declares no scope.paths"


def test_selectable_verdict_names_every_hold_class_at_once():
    """RC-1 (AC-1, kills M3a-d): a bead carrying every SELECTABLE-list hold at
    once -- superseded, a missing predecessor, an unlanded predecessor, a
    non-in_flight release, attempts exhausted, a blocking question, no
    scope.paths, and a latched environmental-fault streak -- names every one
    of them, in the design's order, not just the first."""
    task_id = "T-multi"
    absent_predecessor_id = "PRED-missing"
    pending_predecessor_id = "PRED-pending"

    failure_notes = [
        {
            "id": f"fail{i}",
            "created_at": f"2026-01-01T00:00:0{i}Z",
            "content": {"kind": "status", "body": f"Run failed: attempt {i}"},
        }
        for i in range(1, retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS + 1)
    ]
    question_note = {
        "id": "q1",
        "created_at": "2026-01-02T00:00:00Z",
        "content": {"kind": "question", "blocking": True, "body": "needs a decision"},
    }
    fault_notes = [
        {
            "id": f"fault{i}",
            "created_at": f"2026-01-03T00:00:0{i}Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": "sig1",
            },
        }
        for i in range(1, retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT + 1)
    ]
    notes = failure_notes + [question_note] + fault_notes

    task = {
        "id": task_id,
        "state": "pending",
        "content": {
            "lane": "code-health",
            "title": "t",
            "predecessor_bead_ids": [absent_predecessor_id, pending_predecessor_id],
            # no scope.paths -- item 7
        },
    }
    replacement = {
        "id": "T-replacement",
        "state": "pending",
        "content": {"source_bead_ids": [task_id]},
    }
    pending_predecessor = {
        "id": pending_predecessor_id,
        "state": "pending",
        "content": {},
    }
    all_tasks = [task, replacement, pending_predecessor]
    release_by_task_id = {task_id: guards.ReleaseState(ref="R1", state="planned")}

    verdict = queue_order.selectable_verdict(task, notes, all_tasks, release_by_task_id)

    expected_superseded_reason = guards.superseded_reason(("T-replacement",))
    expected_release_reason = guards.release_block_reason(task, release_by_task_id)
    expected_attempts_reason = queue_order._attempts_exhausted_reason(notes)
    expected_blocking_reason = queue_order._blocking_question_reason(notes)
    expected_scope_reason = queue_order._scope_paths_reason(task)

    assert verdict.hold_reasons[:-1] == (
        expected_superseded_reason,
        f"predecessor bead {absent_predecessor_id} not found",
        f"waiting for predecessor bead {pending_predecessor_id} to land (state=pending)",
        expected_release_reason,
        expected_attempts_reason,
        expected_blocking_reason,
        expected_scope_reason,
    )
    assert len(verdict.hold_reasons) == 8
    assert verdict.hold_reasons[-1].startswith("same environmental fault recorded 3 consecutive times")
    assert verdict.latched is True
    assert verdict.selectable is False


def test_selectable_verdict_latch_follows_other_holds():
    """RC-2 (AC-6, kills M5): the latch is appended last, after every other
    applicable hold, never inserted ahead of them."""
    task = {"id": "T1", "state": "pending", "content": {}}  # no scope.paths
    notes = [
        {
            "id": f"fault{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": "sig1",
            },
        }
        for i in range(1, retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT + 1)
    ]

    verdict = queue_order.selectable_verdict(task, notes, [task], {})

    assert len(verdict.hold_reasons) == 2
    assert verdict.hold_reasons[0] == "task declares no scope.paths"
    assert verdict.hold_reasons[1].startswith("same environmental fault recorded 3 consecutive times")
    assert verdict.latched is True


def test_selectable_verdict_not_latched_below_limit():
    """RC-2 (AC-6, kills M6): one fault short of the limit never latches."""
    task = {"id": "T1", "state": "pending", "content": dict(DEFAULT_SCOPE)}
    notes = [
        {
            "id": f"fault{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": "sig1",
            },
        }
        for i in range(1, retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT)
    ]

    verdict = queue_order.selectable_verdict(task, notes, [task], {})

    assert verdict == queue_order.SelectableVerdict(selectable=True, hold_reasons=(), latched=False)


def test_selectable_verdict_single_predecessor_cycle_names_the_predecessor():
    """RC-A (AC-1, kills M3b): when is_runnable stops on a predecessor
    cycle, the full predecessor walk still runs even though the task
    declares only one predecessor -- a cycle is not "a single predecessor
    that can only reproduce the ordering gate's own reason"."""

    def task(task_id, predecessor_bead_ids):
        return {
            "id": task_id,
            "state": "pending",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids},
        }

    # A(pred B), B(pred A): a bare two-bead cycle.
    a = task("A", ["B"])
    b = task("B", ["A"])
    verdict = queue_order.selectable_verdict(a, [], [a, b], {})
    assert verdict.hold_reasons == (
        "predecessor cycle detected: A -> B -> A",
        "waiting for predecessor bead B to land (state=pending)",
    )
    assert verdict.selectable is False

    # C(pred C2), C2(pred C), R(source_bead_ids=["C2"]): the cycle partner
    # is itself superseded.
    c = task("C", ["C2"])
    c2 = task("C2", ["C"])
    r = {"id": "R", "state": "pending", "content": {"source_bead_ids": ["C2"]}}
    verdict = queue_order.selectable_verdict(c, [], [c, c2, r], {})
    assert verdict.hold_reasons == (
        "predecessor cycle detected: C -> C2 -> C",
        "predecessor C2 is held (superseded by bead R) and can never land",
    )
    assert verdict.selectable is False


def test_selectable_verdict_superseded_cycle_predecessor_names_cycle_after_superseded_reason():
    """RC-A variant (kills M3b for the superseded branch too): a bead that
    is itself superseded AND whose single predecessor forms a cycle with it
    must still surface the cycle text, after the superseded reason."""

    def task(task_id, predecessor_bead_ids):
        return {
            "id": task_id,
            "state": "pending",
            "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": predecessor_bead_ids},
        }

    s = task("S", ["S2"])
    s2 = task("S2", ["S"])
    replacement = {"id": "X", "state": "pending", "content": {"source_bead_ids": ["S"]}}
    all_tasks = [s, s2, replacement]

    verdict = queue_order.selectable_verdict(s, [], all_tasks, {})

    assert verdict.hold_reasons == (
        "superseded by bead X",
        "waiting for predecessor bead S2 to land (state=pending)",
        "predecessor cycle detected: S -> S2 -> S",
    )
    assert verdict.selectable is False


def test_selectable_verdict_walks_every_predecessor_when_not_superseded():
    """AC-6 (kills M13): a non-superseded, non-cycling bead with more than
    one declared predecessor still gets the full walk, not just the first
    predecessor is_runnable's ordering gate stopped at."""
    absent_id = "PRED-missing"
    pending_id = "PRED-pending"
    task = {
        "id": "T1",
        "state": "pending",
        "content": {**DEFAULT_SCOPE, "predecessor_bead_ids": [absent_id, pending_id]},
    }
    pending_predecessor = {"id": pending_id, "state": "pending", "content": {}}
    all_tasks = [task, pending_predecessor]

    verdict = queue_order.selectable_verdict(task, [], all_tasks, {})

    assert verdict.hold_reasons == (
        f"predecessor bead {absent_id} not found",
        f"waiting for predecessor bead {pending_id} to land (state=pending)",
    )
    assert verdict.selectable is False


def test_selectable_verdict_matches_first_selectable_on_both_fixtures(capsys):
    synthetic = _load("queue-synthetic-2026-09-23.json")
    synthetic_all_tasks = _build_all_tasks_from_ids(synthetic["tasks"], synthetic["all_task_ids_by_state"])
    synthetic_notes_by_id = {
        tid: [_reduced_note_to_full(n) for n in notes] for tid, notes in synthetic["notes_by_task"].items()
    }

    # The latched fixture's notes and tasks come from _prepare_latched (AC-9's
    # one helper), not re-truncated/re-stubbed here.
    latched = _load("queue-2026-09-23-latched.json")
    latched_inputs = _prepare_latched(latched)

    cases = [
        (synthetic, synthetic_all_tasks, synthetic_notes_by_id),
        (latched, latched_inputs["tasks"], latched_inputs["notes_by_id"]),
    ]

    for fixture, all_tasks, full_notes_by_id in cases:
        pending = sorted(
            (t for t in all_tasks if t.get("state") == "pending"),
            key=lambda t: t.get("created_at") or "",
        )
        release_by_task_id = _release_by_task_id(fixture)
        store = _NotesOnlyStore(full_notes_by_id)

        picked = queue_order.first_selectable(store, pending, all_tasks, release_by_task_id)
        out = capsys.readouterr().out

        skip_lines: dict[str, str] = {}
        for line in out.splitlines():
            m = re.match(r"^  skip (\S+) — (.*)$", line)
            if m:
                short_id, reason = m.groups()
                skip_lines[short_id] = reason

        verdicts = {
            task["id"]: queue_order.selectable_verdict(
                task, full_notes_by_id.get(task["id"], []), all_tasks, release_by_task_id
            )
            for task in pending
        }

        expected_pick = next((t for t in pending if verdicts[t["id"]].selectable), None)
        if expected_pick is None:
            assert picked is None
        else:
            assert picked is not None
            assert picked["id"] == expected_pick["id"]

        # first_selectable returns as soon as it finds a selectable task, so
        # only the held tasks visited before the pick get a skip line at all
        # -- check the direction the AC actually requires: every skip line
        # that *was* printed names the same reason selectable_verdict gives.
        by_short_id = {task["id"][:8]: task["id"] for task in pending}
        for short_id, reason in skip_lines.items():
            assert short_id in by_short_id, f"skip line for unknown id {short_id}"
            task_id = by_short_id[short_id]
            verdict = verdicts[task_id]
            assert verdict.selectable is False
            assert verdict.hold_reasons[0] == reason


def test_pending_task_with_no_notes_entry_is_held_never_ranked():
    task = {"id": "T1", "state": "pending", "created_at": "2020-01-01T00:00:00Z", "content": dict(DEFAULT_SCOPE)}
    order = _rank(
        dict(
            tasks=[task],
            notes_by_id={},
            release_by_task_id={},
            time_box_by_release_ref={},
            policy=queue_order.RankPolicy(
                urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
            ),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            claimable_since_by_task_id={},
        )
    )
    assert order.ranked == ()
    assert len(order.held) == 1
    assert order.held[0].hold_reasons == ("notes unavailable for this bead",)


def test_unknown_order_when_release_by_task_id_is_none():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    inputs["release_by_task_id"] = None
    order = _rank(inputs)
    assert order.ranked == ()
    assert order.held == ()
    assert order.inputs_unavailable == ("release_by_task_id",)


def test_unknown_order_when_time_box_by_release_ref_is_none():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    inputs["time_box_by_release_ref"] = None
    order = _rank(inputs)
    assert order.ranked == ()
    assert order.held == ()
    assert order.inputs_unavailable == ("time_box_by_release_ref",)


def test_conservation_on_synthetic_fixture():
    fixture = _load("queue-synthetic-2026-09-23.json")
    order = _rank(_synthetic_inputs(fixture))
    pending_ids = {t["id"] for t in fixture["tasks"] if t["state"] == "pending"}
    all_ids = {p.task_id for p in order.ranked} | {p.task_id for p in order.held}
    assert all_ids == pending_ids
    assert len(order.ranked) + len(order.held) == len(pending_ids)
    assert order.inputs_unavailable == ()


def test_conservation_on_latched_fixture():
    fixture = _load("queue-2026-09-23-latched.json")
    order = _rank(_prepare_latched(fixture))
    pending_ids = set(fixture["header"]["pending_ids"])
    all_ids = {p.task_id for p in order.ranked} | {p.task_id for p in order.held}
    assert all_ids == pending_ids
    assert len(order.ranked) + len(order.held) == len(pending_ids)


# ===========================================================================
# AC-8: the synthetic fixture
# ===========================================================================


def test_order_matches_header_and_nearer_target_precedes():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    order = _rank(_synthetic_inputs(fixture))

    ranked_ids = [p.task_id for p in order.ranked]
    assert ranked_ids == header["expected_ranked"]

    held_ids = {p.task_id for p in order.held}
    assert held_ids == set(header["expected_held"].keys())
    assert not (set(ranked_ids) & held_ids)

    positions = _all_positions(order)
    assert "P" in positions["D"].hold_reasons[0]
    assert "RC" in positions["Z"].hold_reasons[0]

    rb_positions = [ranked_ids.index(i) for i in ("AG", "X", "AF")]
    ra_positions = [ranked_ids.index(i) for i in ("P", "Q", "Y")]
    assert max(rb_positions) < min(ra_positions)

    for position in (*order.ranked, *order.held):
        assert position.class_of_service == "normal"
        assert position.class_source == "none"


def _expected_effective_priority(header: dict, task_id: str) -> int:
    """header.verification.effective_priority reads 'P2'/'P3'/...; the ACs
    assert tiers, never the decimal elapsed figures (see the fixture
    header's arithmetic note), so this is the one place that parses it."""
    return int(header["verification"]["effective_priority"][task_id].removeprefix("P"))


def test_predecessor_inherits_dependent_tier_not_its_target():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    order = _rank(_synthetic_inputs(fixture))
    positions = _all_positions(order)
    ranked_ids = [p.task_id for p in order.ranked]

    p = positions["P"]
    assert p.inherited_from == "D"
    assert p.effective_priority == _expected_effective_priority(header, "P")
    assert ranked_ids.index("X") < ranked_ids.index("P")
    assert ranked_ids.index("AF") < ranked_ids.index("P")
    assert ranked_ids.index("P") < ranked_ids.index("Q")

    ra_target = _time_box_by_release_ref(fixture)["RA"].target_at
    assert p.release_target_at == ra_target


def test_aging_promotes_one_tier():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    order = _rank(_synthetic_inputs(fixture))
    positions = _all_positions(order)
    aged_steps = header["verification"]["aged_steps"]

    assert positions["AG"].aged_steps == aged_steps["AG"]
    assert positions["AG"].effective_priority == _expected_effective_priority(header, "AG")
    assert order.ranked[0].task_id == "AG"

    assert positions["X"].aged_steps == aged_steps["X"]
    assert positions["X"].effective_priority == _expected_effective_priority(header, "X")
    assert positions["AF"].aged_steps == aged_steps["AF"]
    assert positions["AF"].effective_priority == _expected_effective_priority(header, "AF")


# ===========================================================================
# AC-9: the latched snapshot
# ===========================================================================


def test_snapshot_holds_name_predecessors_and_nothing_is_ranked():
    fixture = _load("queue-2026-09-23-latched.json")
    header = fixture["header"]
    assert header["selectable_ids"] == []

    order = _rank(_prepare_latched(fixture))

    assert order.ranked == ()
    assert len(order.held) == len(header["pending_ids"])
    held_ids = {p.task_id for p in order.held}
    assert held_ids == set(header["pending_ids"])

    positions = {p.task_id: p for p in order.held}
    for dependent_id, predecessor_id in header["predecessor_pairs"]:
        dependent = positions[dependent_id]
        assert predecessor_id in dependent.hold_reasons[0]
        if predecessor_id in positions:
            assert dependent_id in positions[predecessor_id].unblocks

    for latched_ids in header["latched_signatures"].values():
        for task_id in latched_ids:
            assert positions[task_id].latched is True


# ===========================================================================
# AC-10: explanation
# ===========================================================================


def _expected_render(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, datetime):
        return queue_order._iso_z(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(value) if value else "none"
    return str(value)


def test_every_position_is_explained():
    fixture = _load("queue-synthetic-2026-09-23.json")
    order = _rank(_synthetic_inputs(fixture))

    for position in (*order.ranked, *order.held):
        explanation = position.explanation
        values = [
            position.release_ref,
            position.release_target_at,
            position.outcome_ref,
            position.urgency,
            position.urgency_source,
            position.impact,
            position.impact_source,
            position.priority,
            position.effective_priority,
            position.inherited_from,
            list(position.unblocks),
            position.waiting_since,
            position.waiting_since_source,
            position.age_days,
            position.aged_steps,
        ]
        for value in values:
            rendered = _expected_render(value)
            assert rendered in explanation, (position.task_id, value, rendered, explanation)
        if position.hold_reasons:
            rendered = _expected_render(list(position.hold_reasons))
            assert rendered in explanation


# ===========================================================================
# AC-11: determinism
# ===========================================================================


def _shuffled_inputs(base_inputs: dict, rng: random.Random) -> dict:
    tasks = list(base_inputs["tasks"])
    rng.shuffle(tasks)

    notes_items = list(base_inputs["notes_by_id"].items())
    rng.shuffle(notes_items)
    notes_by_id = {}
    for tid, notes in notes_items:
        notes = list(notes)
        rng.shuffle(notes)
        notes_by_id[tid] = notes

    def shuffled_dict(mapping):
        if mapping is None:
            return None
        items = list(mapping.items())
        rng.shuffle(items)
        return dict(items)

    return dict(
        tasks=tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=shuffled_dict(base_inputs["release_by_task_id"]),
        time_box_by_release_ref=shuffled_dict(base_inputs["time_box_by_release_ref"]),
        policy=base_inputs["policy"],
        now=base_inputs["now"],
        claimable_since_by_task_id=shuffled_dict(base_inputs["claimable_since_by_task_id"]),
        impact_by_outcome=shuffled_dict(base_inputs.get("impact_by_outcome")),
    )


def _assert_shuffle_invariant(base_inputs: dict, seed: int):
    base_before = copy.deepcopy(base_inputs)
    baseline = _rank(base_inputs).to_json()
    assert base_inputs == base_before, "rank_pending mutated its baseline inputs"
    rng = random.Random(seed)
    for _ in range(1000):
        shuffled = _shuffled_inputs(base_inputs, rng)
        before = {key: copy.deepcopy(value) for key, value in shuffled.items()}

        result = _rank(shuffled)

        for key, value in shuffled.items():
            assert value == before[key], f"rank_pending mutated its {key!r} input"
        assert result.to_json() == baseline


def test_order_is_shuffle_invariant_on_synthetic_fixture():
    fixture = _load("queue-synthetic-2026-09-23.json")
    base_inputs = _synthetic_inputs(fixture)
    base_inputs["impact_by_outcome"] = {"RB/O-1": "medium", "RA/O-1": "low"}
    _assert_shuffle_invariant(base_inputs, seed=12345)


def test_order_is_shuffle_invariant_on_latched_fixture():
    fixture = _load("queue-2026-09-23-latched.json")
    base_inputs = _prepare_latched(fixture)
    base_inputs["impact_by_outcome"] = {"R26.11/O-1": "medium"}
    _assert_shuffle_invariant(base_inputs, seed=67890)


# ===========================================================================
# AC-12: import guard
# ===========================================================================


_FORBIDDEN_TOP_LEVEL_MODULES = {
    "substrate",
    "substrate_client",
    "beadstore",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "temporalio",
}
_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}
_CLOCK_MODULES = {"time", "datetime"}
_FORBIDDEN_IMPORTED_CLOCK_NAMES = {"time", "now"}


def test_queue_order_imports_no_io():
    source = Path(queue_order.__file__).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                segments = set(alias.name.split("."))
                assert not (segments & _FORBIDDEN_TOP_LEVEL_MODULES), f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                segments = set(node.module.split("."))
                assert not (segments & _FORBIDDEN_TOP_LEVEL_MODULES), f"forbidden import: {node.module}"
                if node.module in _CLOCK_MODULES:
                    for alias in node.names:
                        assert alias.name not in _FORBIDDEN_IMPORTED_CLOCK_NAMES, (
                            f"queue_order.py imports the clock via "
                            f"'from {node.module} import {alias.name}'"
                        )
        elif isinstance(node, ast.Attribute):
            assert node.attr not in _FORBIDDEN_NOW_ATTRS, f"queue_order.py reads the clock via .{node.attr}"
            if node.attr in ("time", "monotonic") and isinstance(node.value, ast.Name) and node.value.id == "time":
                raise AssertionError(f"queue_order.py references time.{node.attr}")


# ===========================================================================
# AC-13: performance
# ===========================================================================


def _wall_clock_budget(seconds: float) -> float:
    """The budget, widened when a line tracer is installed.

    The nightly coverage-ratchet job runs this suite under coverage's tracer,
    which made this test take 4.667s on the CI runner (run 37189789586,
    2026-10-04) against 0.16s untraced on the factory host. Untraced runs --
    every PR's CI and the factory pre-check -- still enforce the real budget.
    """
    monitoring = getattr(sys, "monitoring", None)
    traced = sys.gettrace() is not None or (
        monitoring is not None and monitoring.get_tool(monitoring.COVERAGE_ID) is not None
    )
    return seconds * 10 if traced else seconds


def test_rank_pending_handles_500_tasks_under_two_seconds():
    rng = random.Random(7)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    releases = ["RA", "RB", "RC"]
    time_box_by_release_ref = {
        ref: queue_order.TimeBox(opened_at=now - timedelta(days=30), target_at=now + timedelta(days=30))
        for ref in releases
    }

    task_count = 500
    chain_size = 5
    tasks = []
    notes_by_id = {}
    release_by_task_id = {}
    for i in range(task_count):
        task_id = f"T{i}"
        predecessor_bead_ids = [f"T{i - 1}"] if i % chain_size != 0 else []
        note_count = rng.randint(0, 10)
        notes_by_id[task_id] = [
            {
                "id": f"{task_id}-n{j}",
                "created_at": f"2026-01-01T00:00:{j:02d}Z",
                "content": {"kind": "comment", "body": "note"},
            }
            for j in range(note_count)
        ]
        tasks.append(
            {
                "id": task_id,
                "state": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "content": {
                    **DEFAULT_SCOPE,
                    "predecessor_bead_ids": predecessor_bead_ids,
                    "outcome_ref": "O-1",
                },
            }
        )
        release_by_task_id[task_id] = guards.ReleaseState(ref=releases[i % 3], state="in_flight")

    claimable_since_by_task_id = {
        t["id"]: now - timedelta(days=rng.randint(0, 60)) for t in tasks
    }
    policy = queue_order.RankPolicy(urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3)

    inputs = dict(
        tasks=tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=release_by_task_id,
        time_box_by_release_ref=time_box_by_release_ref,
        policy=policy,
        now=now,
        claimable_since_by_task_id=claimable_since_by_task_id,
    )

    start = time.perf_counter()
    order = _rank(inputs)
    elapsed = time.perf_counter() - start

    assert elapsed < _wall_clock_budget(2.0), f"rank_pending took {elapsed:.3f}s for {task_count} tasks"
    assert len(order.ranked) + len(order.held) == task_count


# ===========================================================================
# R26.12 S55 rail: order_mode (the kill switch's read) and rank_or_fifo
# (rank or FIFO, never neither).
# ===========================================================================


@pytest.mark.parametrize(
    "environ,expected",
    [
        ({}, "rank"),
        ({"FACTORY_QUEUE_ORDER": "rank"}, "rank"),
        ({"FACTORY_QUEUE_ORDER": "fifo"}, "fifo"),
        ({"FACTORY_QUEUE_ORDER": "FIFO "}, "fifo"),
    ],
)
def test_order_mode_reads_the_explicit_environ(environ, expected):
    assert queue_order.order_mode(environ) == expected


def test_order_mode_rejects_an_unrecognised_value_naming_it():
    with pytest.raises(ValueError, match="off"):
        queue_order.order_mode({"FACTORY_QUEUE_ORDER": "off"})


def test_order_mode_does_not_read_os_environ(monkeypatch):
    monkeypatch.setenv("FACTORY_QUEUE_ORDER", "fifo")
    assert queue_order.order_mode({}) == "rank"


def _rank_or_fifo(mode, inputs, **overrides):
    kwargs = dict(inputs)
    kwargs.update(overrides)
    return queue_order.rank_or_fifo(mode, **kwargs)


def test_rank_or_fifo_rank_mode_matches_rank_pending_on_synthetic_fixture():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    direct = _rank(inputs)
    via = _rank_or_fifo("rank", inputs)
    assert via.to_json() == direct.to_json()
    assert via.fallback_reason is None


def test_rank_or_fifo_fifo_mode_explanation_and_inputs_unavailable():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    order = _rank_or_fifo("fifo", inputs)
    assert order.inputs_unavailable == ()
    assert order.fallback_reason is None
    for position in (*order.ranked, *order.held):
        assert position.explanation == queue_order.FIFO_EXPLANATION


@pytest.mark.parametrize("fixture_name", ["queue-synthetic-2026-09-23.json", "queue-2026-09-23-latched.json"])
def test_rank_or_fifo_conservation_rank_and_fifo_agree_on_task_id_set(fixture_name):
    fixture = _load(fixture_name)
    if fixture_name == "queue-2026-09-23-latched.json":
        inputs = _prepare_latched(fixture)
    else:
        inputs = _synthetic_inputs(fixture)

    rank_order = _rank_or_fifo("rank", inputs)
    fifo_order = _rank_or_fifo("fifo", inputs)

    rank_ids = {p.task_id for p in (*rank_order.ranked, *rank_order.held)}
    fifo_ids = {p.task_id for p in (*fifo_order.ranked, *fifo_order.held)}
    assert rank_ids == fifo_ids


def test_rank_or_fifo_falls_back_to_fifo_when_rank_pending_raises(monkeypatch):
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic rank failure")

    monkeypatch.setattr(queue_order, "rank_pending", _boom)

    order = _rank_or_fifo("rank", inputs)

    assert order.fallback_reason == "RuntimeError: synthetic rank failure"
    for position in (*order.ranked, *order.held):
        assert position.explanation == queue_order.FIFO_EXPLANATION

    rank_ids = {t["id"] for t in fixture["tasks"] if t["state"] == "pending"}
    fifo_ids = {p.task_id for p in (*order.ranked, *order.held)}
    assert rank_ids == fifo_ids


def test_rank_or_fifo_rank_mode_success_never_sets_fallback_reason():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    order = _rank_or_fifo("rank", inputs)
    assert order.fallback_reason is None


def test_rank_or_fifo_rejects_an_unrecognised_mode():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    with pytest.raises(ValueError, match="sideways"):
        _rank_or_fifo("sideways", inputs)


def test_fifo_order_pins_ascending_created_at_then_id():
    fixture = _load("queue-synthetic-2026-09-23.json")
    inputs = _synthetic_inputs(fixture)
    order = queue_order.rank_or_fifo("fifo", **inputs)

    ranked_ids = {p.task_id for p in order.ranked}
    tasks_by_id = {t["id"]: t for t in inputs["tasks"]}
    expected = sorted(ranked_ids, key=lambda tid: (str(tasks_by_id[tid].get("created_at") or ""), tid))

    assert [p.task_id for p in order.ranked] == expected
    assert [p.position for p in order.ranked] == list(range(len(order.ranked)))


def test_fifo_fallback_handles_unparsable_created_at_without_raising():
    task = {"id": "T1", "state": "pending", "content": dict(DEFAULT_SCOPE)}  # no created_at at all
    inputs = dict(
        tasks=[task],
        notes_by_id={"T1": []},
        release_by_task_id={},
        time_box_by_release_ref={},
        policy=queue_order.RankPolicy(
            urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
        ),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        claimable_since_by_task_id={},
    )

    order = _rank_or_fifo("rank", inputs)

    assert order.fallback_reason is not None
    assert order.fallback_reason.startswith("ValueError:")
    ids = {p.task_id for p in (*order.ranked, *order.held)}
    assert ids == {"T1"}
    position = _all_positions(order)["T1"]
    assert position.waiting_since_source == "unparsable_created_at"
    assert position.age_days == 0


def test_fifo_fallback_handles_naive_now_without_raising():
    task = {"id": "T1", "state": "pending", "created_at": "2026-01-01T00:00:00Z", "content": dict(DEFAULT_SCOPE)}
    inputs = dict(
        tasks=[task],
        notes_by_id={"T1": []},
        release_by_task_id={},
        time_box_by_release_ref={},
        policy=queue_order.RankPolicy(
            urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
        ),
        now=datetime(2026, 1, 1),  # naive -- rank_pending itself refuses this
        claimable_since_by_task_id={},
    )

    order = _rank_or_fifo("rank", inputs)

    assert order.fallback_reason is not None
    assert order.fallback_reason.startswith("ValueError:")
    ids = {p.task_id for p in (*order.ranked, *order.held)}
    assert ids == {"T1"}
