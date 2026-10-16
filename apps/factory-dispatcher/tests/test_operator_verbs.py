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


# ---------------------------------------------------------------------------
# R26.12 B8 AC-6: claimable_since from the event log, with a per-bead
# fallback to created_at (not the synthetic fixture, which carries no
# events of its own -- a separately constructed event list instead).
# ---------------------------------------------------------------------------


def test_read_rank_inputs_claimable_since_from_events_vs_created_at_fallback():
    from test_dispatch import FakeSubstrate as DispatchFakeSubstrate

    requeued_at = "2026-03-01T00:00:00Z"
    with_event = {
        "id": "with-event", "state": "pending", "created_at": "2026-01-01T00:00:00Z",
        "content": {"scope": {"paths": ["x"]}},
    }
    no_event = {
        "id": "no-event", "state": "pending", "created_at": "2026-01-02T00:00:00Z",
        "content": {"scope": {"paths": ["x"]}},
    }
    store = DispatchFakeSubstrate(
        tasks=[with_event, no_event],
        events={
            "with-event": [
                {"event_type": "transitioned", "from_state": "doing", "to_state": "pending", "created_at": requeued_at},
            ],
        },
    )

    inputs = operator_verbs.read_rank_inputs(store)

    assert inputs["claimable_since_by_task_id"]["with-event"] == _parse_dt(requeued_at)
    assert inputs["claimable_since_by_task_id"]["no-event"] is None

    policy = queue_order.RankPolicy(
        urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14, max_aging_steps=3
    )
    now = _parse_dt("2026-03-15T00:00:00Z")
    order = queue_order.rank_pending(
        inputs["tasks"],
        notes_by_id=inputs["notes_by_id"],
        release_by_task_id=inputs["release_by_task_id"],
        time_box_by_release_ref=inputs["time_box_by_release_ref"],
        policy=policy,
        now=now,
        claimable_since_by_task_id=inputs["claimable_since_by_task_id"],
        claimable_since_source=inputs["claimable_since_source"],
    )
    positions = {p.task_id: p for p in order.ranked}

    assert positions["with-event"].waiting_since == _parse_dt(requeued_at)
    assert positions["with-event"].waiting_since_source == "events"
    assert positions["with-event"].aged_steps == 1  # 14 days of the 2026-03-01 -> 2026-03-15 span

    assert positions["no-event"].waiting_since == _parse_dt("2026-01-02T00:00:00Z")
    assert positions["no-event"].waiting_since_source == "created_at"


def test_read_rank_inputs_falls_back_per_bead_when_list_events_raises():
    """AC-1's per-bead fallback: one bead's unreadable events must not
    refuse the whole tick -- it just reads as if there were no events."""
    from test_dispatch import FakeSubstrate as DispatchFakeSubstrate

    broken = {
        "id": "broken", "state": "pending", "created_at": "2026-01-01T00:00:00Z",
        "content": {"scope": {"paths": ["x"]}},
    }
    store = DispatchFakeSubstrate(tasks=[broken])

    def _raise(_task_id):
        raise RuntimeError("events unreachable")

    store.list_events = _raise

    inputs = operator_verbs.read_rank_inputs(store)

    assert inputs["claimable_since_by_task_id"]["broken"] is None


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


def test_cmd_explain_order_json_prints_only_queue_order_to_json(tmp_path, monkeypatch, capsys):
    import argparse
    import json as _json

    monkeypatch.setattr(operator_verbs, "order_file_path", lambda *a, **k: tmp_path / "queue-order.json")

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

    parsed = _json.loads(out)
    assert parsed.pop("live_order_file") is None
    assert parsed == _json.loads(order.to_json())


# ---------------------------------------------------------------------------
# R26.12 B8 AC-10: explain-order says which mode is live.
# ---------------------------------------------------------------------------


def _bare_order(mode: str) -> queue_order.QueueOrder:
    return queue_order.QueueOrder(
        ranked=(), held=(), computed_at=_parse_dt("2026-10-08T00:00:00Z"),
        computed_by_revision=None, policy_revision=None, inputs_unavailable=(), mode=mode,
    )


def test_render_mode_lines_with_no_live_file_prints_only_the_first_line():
    text = operator_verbs.render_mode_lines(_bare_order("rank"), {"FACTORY_QUEUE_ORDER": "rank"}, None)
    assert text == "mode: rank (this computation; FACTORY_QUEUE_ORDER from the invoking shell: rank)"


def test_render_mode_lines_names_unset_when_the_shell_has_no_value():
    text = operator_verbs.render_mode_lines(_bare_order("rank"), {}, None)
    assert "FACTORY_QUEUE_ORDER from the invoking shell: unset" in text


def test_render_mode_lines_shows_the_dispatchers_last_tick_and_warns_on_mismatch():
    live = {"mode": "fifo", "computed_at": "2026-10-08T00:00:00Z", "fallback_reason": "boom"}
    text = operator_verbs.render_mode_lines(_bare_order("rank"), {}, live)
    assert "dispatcher's last tick: mode=fifo at 2026-10-08T00:00:00Z, fallback_reason=boom" in text
    assert "WARNING: the dispatcher's live mode differs from this shell's" in text


def test_render_mode_lines_no_warning_when_the_dispatchers_mode_agrees():
    live = {"mode": "rank", "computed_at": "2026-10-08T00:00:00Z", "fallback_reason": None}
    text = operator_verbs.render_mode_lines(_bare_order("rank"), {}, live)
    assert "fallback_reason=None" in text
    assert "WARNING" not in text


def test_cmd_explain_order_text_mode_prints_mode_lines_and_warning(tmp_path, monkeypatch, capsys):
    import argparse

    order_path = tmp_path / "queue-order.json"
    order_path.write_text('{"mode":"fifo","computed_at":"2026-10-08T00:00:00Z","fallback_reason":null}')
    monkeypatch.setattr(operator_verbs, "order_file_path", lambda *a, **k: order_path)
    monkeypatch.delenv("FACTORY_QUEUE_ORDER", raising=False)

    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    args = argparse.Namespace(json=False, now=header["now"])

    exit_code = operator_verbs.cmd_explain_order(args, store=store, policy_path=operator_verbs.POLICY_PATH)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "mode: rank (this computation; FACTORY_QUEUE_ORDER from the invoking shell: unset)" in out
    assert "dispatcher's last tick: mode=fifo at 2026-10-08T00:00:00Z, fallback_reason=None" in out
    assert "WARNING: the dispatcher's live mode differs from this shell's" in out


def test_cmd_explain_order_json_mode_carries_live_order_file(tmp_path, monkeypatch):
    import argparse
    import json as _json

    order_path = tmp_path / "queue-order.json"
    order_path.write_text('{"mode":"fifo","computed_at":"2026-10-08T00:00:00Z","fallback_reason":"x"}')
    monkeypatch.setattr(operator_verbs, "order_file_path", lambda *a, **k: order_path)

    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    args = argparse.Namespace(json=True, now=header["now"])

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        operator_verbs.cmd_explain_order(args, store=store, policy_path=operator_verbs.POLICY_PATH)

    payload = _json.loads(buf.getvalue())
    assert payload["live_order_file"] == {
        "mode": "fifo", "computed_at": "2026-10-08T00:00:00Z", "fallback_reason": "x"
    }


def test_cmd_explain_order_never_opens_the_dispatchers_env_file(tmp_path, monkeypatch):
    import argparse

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    decoy_env = state_dir / "dispatcher.env"
    decoy_env.write_text("FACTORY_QUEUE_ORDER=fifo\n")
    order_path = state_dir / "queue-order.json"
    order_path.write_text('{"mode":"rank","computed_at":"2026-10-08T00:00:00Z","fallback_reason":null}')
    monkeypatch.setattr(operator_verbs, "order_file_path", lambda *a, **k: order_path)

    opened: list[Path] = []
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(self), real_read_text(self, *a, **k))[1])
    monkeypatch.setattr(Path, "read_bytes", lambda self, *a, **k: (opened.append(self), real_read_bytes(self, *a, **k))[1])

    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    store = _store_from_fixture(fixture)
    args = argparse.Namespace(json=False, now=header["now"])

    operator_verbs.cmd_explain_order(args, store=store, policy_path=operator_verbs.POLICY_PATH)

    assert decoy_env not in opened
    assert order_path in opened
    assert operator_verbs.POLICY_PATH in opened


_EXCLUDED_SCAN_DIRS = {"tests", "venv", "site-packages"}


#: R26.12 B8: the wiring this bead lands. Every reference to the kill
#: switch or its fallback outside queue_order.py/operator_verbs.py must come
#: from exactly one of these -- dispatch.pick_task (AC-1) and the claim path
#: in activities/dispatch_steps.py (AC-1, AC-10's mode-mismatch handling).
_ALLOWED_OFFENDER_FILES = {"dispatch.py", "activities/dispatch_steps.py"}


def test_only_operator_verbs_imports_rank_or_fifo_or_order_mode():
    """AC-7 (narrowed from the rail's "nothing but operator_verbs" ratchet,
    now that B8 lands the wiring both of these existed to guard against
    landing silently): every reference outside queue_order.py/
    operator_verbs.py must come from the allow-list above. Scans every
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
        if str(relative) in _ALLOWED_OFFENDER_FILES:
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
    """AC-7 (narrowed to guards.py only): dispatch.py's --explain-order now
    delegates to operator_verbs.main by design (AC-5), so only guards.py --
    which has no business with the queue order at all -- stays ratcheted."""
    source = (APP_DIR / "guards.py").read_text()
    assert "operator_verbs" not in source
    assert "rank_or_fifo" not in source
    assert "order_mode" not in source
