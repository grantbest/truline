"""explain-order is read-only and agrees with rank_pending (R26.12 S55 rail)."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operator_verbs  # noqa: E402
import queue_order  # noqa: E402
from test_queue_order import _load, _parse_dt, _synthetic_inputs  # noqa: E402
from test_schedule_status_waiting_queue import FakeStore  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1]

_KNOWN_GET_CALLS = {"list_tasks", "list_notes", "list_beads", "list_links", "list_events"}


def _events_for(task: dict) -> list[dict]:
    """A one-event log whose `_became_claimable_at` reading is exactly the
    fixture's own `claimable_since` -- a transition into pending from a
    non-pending state satisfies the qualifying filter regardless of value."""
    return [
        {
            "event_type": "transitioned",
            "from_state": "doing",
            "to_state": "pending",
            "created_at": task["claimable_since"],
        }
    ]


def _store_from_fixture(fixture: dict) -> FakeStore:
    tasks = fixture["tasks"]
    return FakeStore(
        tasks=tasks,
        notes=fixture["notes_by_task"],
        events={task["id"]: _events_for(task) for task in tasks},
        beads={("arch", "release"): fixture["releases"]},
        links=[{**link, "link_type": "delivers"} for link in fixture["links"]],
    )


def _policy_from_header(fixture: dict) -> queue_order.RankPolicy:
    return queue_order.RankPolicy.from_mapping(fixture["header"]["policy"])


def test_explain_order_makes_only_get_calls_and_matches_the_header_order():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    order = operator_verbs.compute_queue_order(
        store, now=now, environ={}, policy=_policy_from_header(fixture)
    )

    assert set(store.calls) <= _KNOWN_GET_CALLS
    assert [p.task_id for p in order.ranked] == header["expected_ranked"]
    assert {p.task_id for p in order.held} == set(header["expected_held"].keys())
    assert order.fallback_reason is None


def test_explain_order_rank_mode_matches_rank_pending_over_the_same_inputs():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])
    policy = _policy_from_header(fixture)

    via_operator_verbs = operator_verbs.compute_queue_order(store, now=now, environ={}, policy=policy)

    # operator_verbs reads claimable_since from the event log (source
    # "events"), where test_queue_order's own fixture-input helper injects it
    # directly (source "injected" by rank_pending's default) -- the only
    # legitimate difference; every computed value must still agree.
    reference = queue_order.rank_pending(
        **{**_synthetic_inputs(fixture), "claimable_since_source": "events"}
    )

    assert via_operator_verbs.to_json() == reference.to_json()


def test_explain_order_table_lists_ranked_rows_in_order_with_computed_fields():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    order = operator_verbs.compute_queue_order(store, now=now, environ={}, policy=_policy_from_header(fixture))
    table = operator_verbs.render_table(order)
    lines = table.splitlines()

    assert lines[0].split(" | ")[0].strip() == "position"
    assert len(lines) == 1 + len(order.ranked)
    for index, position in enumerate(order.ranked):
        row = [cell.strip() for cell in lines[index + 1].split(" | ")]
        expected = [
            str(position.position),
            position.task_id,
            str(position.priority),
            str(position.effective_priority),
            position.urgency,
            position.release_ref or "-",
            operator_verbs._iso_z(position.release_target_at) if position.release_target_at is not None else "-",
            position.class_source,
            operator_verbs._iso_z(position.waiting_since),
            str(position.age_days),
            str(position.aged_steps),
        ]
        assert row == expected


def test_explain_order_held_section_names_every_hold_reason():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    order = operator_verbs.compute_queue_order(store, now=now, environ={}, policy=_policy_from_header(fixture))
    held_text = operator_verbs.render_held(order)

    for task_id, hold_fragment in header["expected_held"].items():
        assert f"{task_id}:" in held_text
        line = next(line for line in held_text.splitlines() if line.strip().startswith(f"{task_id}:"))
        assert hold_fragment.split()[-1] in line or hold_fragment.split()[0] in line


def test_explain_order_explanations_section_covers_every_position():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    order = operator_verbs.compute_queue_order(store, now=now, environ={}, policy=_policy_from_header(fixture))
    explanations_text = operator_verbs.render_explanations(order)

    all_ids = {p.task_id for p in (*order.ranked, *order.held)}
    assert all_ids == set(header["expected_ranked"]) | set(header["expected_held"].keys())
    for position in (*order.ranked, *order.held):
        assert f"{position.task_id}: {position.explanation}" in explanations_text


def test_explain_order_kill_switch_selects_fifo_and_never_writes():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    order = operator_verbs.compute_queue_order(
        store, now=now, environ={"FACTORY_QUEUE_ORDER": "fifo"}, policy=_policy_from_header(fixture)
    )

    assert set(store.calls) <= _KNOWN_GET_CALLS
    for position in (*order.ranked, *order.held):
        assert position.explanation == queue_order.FIFO_EXPLANATION

    rank_order = operator_verbs.compute_queue_order(
        _store_from_fixture(fixture), now=now, environ={}, policy=_policy_from_header(fixture)
    )
    rank_ids = {p.task_id for p in (*rank_order.ranked, *rank_order.held)}
    fifo_ids = {p.task_id for p in (*order.ranked, *order.held)}
    assert rank_ids == fifo_ids


def test_explain_order_rejects_an_unrecognised_kill_switch_value():
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    now = _parse_dt(header["now"])

    try:
        operator_verbs.compute_queue_order(
            store, now=now, environ={"FACTORY_QUEUE_ORDER": "off"}, policy=_policy_from_header(fixture)
        )
    except ValueError as exc:
        assert "off" in str(exc)
    else:
        raise AssertionError("expected a ValueError naming the unrecognised value")


def test_load_policy_reads_health_policy_json_with_a_git_blob_sha_revision():
    policy = operator_verbs.load_policy(operator_verbs.POLICY_PATH)
    assert policy.urgency_band_medium == 0.33
    assert policy.urgency_band_high == 0.66
    assert policy.aging_days == 14

    data = operator_verbs.POLICY_PATH.read_bytes()
    expected = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    assert policy.policy_revision == expected


def test_cmd_explain_order_exits_2_and_prints_unknown_when_policy_file_is_missing(tmp_path, capsys):
    import argparse

    args = argparse.Namespace(json=False, now=None)
    missing = tmp_path / "does-not-exist.json"

    exit_code = operator_verbs.cmd_explain_order(args, store=FakeStore([]), policy_path=missing)

    assert exit_code == 2
    out = capsys.readouterr().out
    assert out.strip() == "unknown: policy_unreadable"


def test_cmd_explain_order_exits_2_on_an_invalid_policy_file_never_a_default(tmp_path, capsys):
    import argparse

    bad_policy = tmp_path / "health-policy.json"
    bad_policy.write_text('{"urgency_bands": {"medium": 0.9, "high": 0.1}, "aging_days": 14}')

    args = argparse.Namespace(json=False, now=None)
    exit_code = operator_verbs.cmd_explain_order(args, store=FakeStore([]), policy_path=bad_policy)

    assert exit_code == 2
    assert capsys.readouterr().out.strip() == "unknown: policy_unreadable"


def test_cmd_explain_order_exits_2_when_policy_file_is_not_an_object(tmp_path, capsys):
    import argparse

    bad_policy = tmp_path / "health-policy.json"
    bad_policy.write_text("[]")

    args = argparse.Namespace(json=False, now=None)
    exit_code = operator_verbs.cmd_explain_order(args, store=FakeStore([]), policy_path=bad_policy)

    assert exit_code == 2
    assert capsys.readouterr().out.strip() == "unknown: policy_unreadable"


def test_cmd_explain_order_json_prints_only_queue_order_to_json(capsys):
    import argparse

    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)

    args = argparse.Namespace(json=True, now=header["now"])
    exit_code = operator_verbs.cmd_explain_order(args, store=store, policy_path=operator_verbs.POLICY_PATH)

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    # cmd_explain_order loads the real health-policy.json (with its own
    # policy_revision), not the fixture header's policy embed -- use the same
    # loaded policy here so only the inputs, not the policy identity, differ.
    policy = operator_verbs.load_policy(operator_verbs.POLICY_PATH)
    inputs = {**_synthetic_inputs(fixture), "claimable_since_source": "events", "policy": policy}
    order = queue_order.rank_pending(**inputs)
    assert out == order.to_json()


_EXCLUDED_SCAN_DIRS = {"tests", "venv", "site-packages"}


def test_only_operator_verbs_imports_rank_or_fifo_or_order_mode():
    """AC-4: nothing besides operator_verbs.py (and the test suite) reaches
    for the kill switch or its fallback -- pick_task is unwired. Scans every
    production module under apps/factory-dispatcher (activities/, workflows/,
    etc.), not just the top-level *.py files, so a wire-up buried in a
    subdirectory is caught too."""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(APP_DIR)
        if _EXCLUDED_SCAN_DIRS & set(relative.parts[:-1]):
            continue
        if path.name in {"queue_order.py", "operator_verbs.py"}:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "queue_order":
                for alias in node.names:
                    if alias.name in ("rank_or_fifo", "order_mode"):
                        offenders.append((str(relative), alias.name))
            if isinstance(node, ast.Attribute) and node.attr in ("rank_or_fifo", "order_mode"):
                offenders.append((str(relative), node.attr))
    assert offenders == []


def test_dispatch_py_and_guards_py_do_not_reference_operator_verbs():
    for name in ("dispatch.py", "guards.py"):
        source = (APP_DIR / name).read_text()
        assert "operator_verbs" not in source
        assert "rank_or_fifo" not in source
        assert "order_mode" not in source
