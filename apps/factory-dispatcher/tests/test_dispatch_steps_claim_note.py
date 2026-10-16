"""R26.12 B8 AC-3: the claim note and the prompt say why this bead is next.

Driven through the real production entry point, ``activities.dispatch_steps.
claim_activity`` -- no mocking of ``dispatch.pick_task`` itself -- over
``test_claim_atomicity.FakeSubstrate``, the double that already models the
rest of the claim pipeline (compare-and-set transitions, a scriptable
``add_note``). No substrate, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import dispatch_steps  # noqa: E402

from test_claim_atomicity import FakeSubstrate, make_task  # noqa: E402


def _claim(sub, monkeypatch, request=None):
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    monkeypatch.delenv("FACTORY_QUEUE_ORDER", raising=False)
    return dispatch_steps.claim_activity(request or {})


def test_claim_note_carries_queue_provenance(monkeypatch):
    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])

    result = _claim(sub, monkeypatch)

    assert result["status"] == "claimed", result
    assert len(sub.notes) == 1
    _args, kwargs = sub.notes[0]
    assert kwargs["queue_position"] == 0
    assert isinstance(kwargs["priority"], int)
    assert kwargs["class_source"] == "none"

    prompt = result["prompt"]
    assert "Why this bead is next: Queue position:" in prompt
    assert "Why this bead is next: selected by the operator (--task)" not in prompt


def test_claim_note_names_the_operator_force_when_no_position_was_computed(monkeypatch):
    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])

    result = _claim(sub, monkeypatch, {"task_id": "bead-a"})

    assert result["status"] == "claimed", result
    assert len(sub.notes) == 1
    _args, kwargs = sub.notes[0]
    assert kwargs["queue_position"] is None
    assert kwargs["priority"] is None
    assert kwargs["class_source"] is None

    assert "Why this bead is next: selected by the operator (--task)" in result["prompt"]


# ---------------------------------------------------------------------------
# R26.12 B8 AC-1, AC-2, AC-4 on the claim path, added at the release gate
# (2026-10-09): each of these pins a mutation the gate found the suite did
# not kill -- the invalid kill-switch value raising out of the claim or
# leaving fallback_reason unstamped, a failed queue-order.json write failing
# the tick, and the ranked table never reaching the tick's log.
# ---------------------------------------------------------------------------


def _claim_with_order_env(sub, monkeypatch, value):
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    monkeypatch.setenv("FACTORY_QUEUE_ORDER", value)
    return dispatch_steps.claim_activity({})


def test_invalid_queue_order_value_falls_back_to_fifo_and_still_claims(monkeypatch, caplog):
    """AC-1: a typo in FACTORY_QUEUE_ORDER never fails the tick; the claim
    runs FIFO, says so in the log, and the written order records mode fifo
    with the typo as its fallback_reason."""
    import json

    import operator_verbs

    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])

    with caplog.at_level("WARNING", logger="factory-dispatcher.dispatch-steps"):
        result = _claim_with_order_env(sub, monkeypatch, "ranked")

    assert result["status"] == "claimed", result
    assert "invalid FACTORY_QUEUE_ORDER='ranked'; this tick runs FIFO" in caplog.text

    written = json.loads(operator_verbs.order_file_path().read_text(encoding="utf-8"))
    assert written["mode"] == "fifo"
    assert written["fallback_reason"] == "invalid FACTORY_QUEUE_ORDER='ranked'"
    assert [p["task_id"] for p in written["ranked"]] == ["bead-a"]


def test_failed_order_file_write_is_logged_and_never_fails_the_claim(monkeypatch, caplog, tmp_path):
    """AC-4: the state directory is a plain file, so queue-order.json cannot
    be written; the tick still claims and the failure is one warning line."""
    import operator_verbs

    blocker = tmp_path / "state-dir-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(blocker))

    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])

    with caplog.at_level("WARNING", logger="factory-dispatcher.dispatch-steps"):
        result = _claim(sub, monkeypatch)

    assert result["status"] == "claimed", result
    assert "failed to write queue-order.json" in caplog.text
    assert not operator_verbs.order_file_path().exists()
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_claim_path_prints_the_ranked_table_and_the_held_list(monkeypatch, capsys):
    """AC-2: the tick's own output carries the ranked table (header and one
    row per ranked bead) and the held list, once, beside the claim."""
    tasks = [
        make_task("bead-a", "2026-08-23T00:00:00Z"),
        make_task("bead-b", "2026-08-24T00:00:00Z"),
    ]
    sub = FakeSubstrate(tasks)

    result = _claim(sub, monkeypatch)
    out = capsys.readouterr().out

    assert result["status"] == "claimed", result
    header_lines = [line for line in out.splitlines() if "position" in line and "task_id" in line]
    assert len(header_lines) == 1, out
    assert "effective_priority" in header_lines[0]
    assert "bead-a" in out and "bead-b" in out
    assert out.count("Held:") == 1, out
