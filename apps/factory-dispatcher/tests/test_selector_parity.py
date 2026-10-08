"""R26.12 B4 AC-2: four readers, one function.

``dispatch.pick_task``, the claim-time re-check
(``activities.dispatch_steps._claim_task``), ``schedule_status``'s
waiting-queue describer, and the hub's ``tools.factory_status.task_runnability``
all obtain their answer from ``queue_order.selectable_verdict`` and compute
nothing of their own. This drives all four over the two committed fixtures
(``tests/fixtures/queue-synthetic-2026-09-23.json``,
``tests/fixtures/queue-2026-09-23-latched.json``) and asserts they agree, and
separately proves the one remaining divergence -- the ``--task`` operator
force path -- is intentional and bounded to exactly that path.

No substrate, no network, no new store double: every fixture replay reuses
``test_dispatch.FakeSubstrate`` (already imported this way by
``test_queue_order_parity.py``) and this file's own helpers reuse that file's
and ``test_queue_order.py``'s fixture plumbing rather than re-deriving it.
The hub module is loaded by file path (it does not live on this process's
``sys.path`` as an importable package) with ``factory_status.DISPATCHER_DIR``
monkeypatched to this checkout's own ``apps/factory-dispatcher``.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import dispatch  # noqa: E402
import queue_order  # noqa: E402
import retry_policy  # noqa: E402
import schedule_status  # noqa: E402
from activities import dispatch_steps  # noqa: E402

from test_claim_atomicity import make_task  # noqa: E402
from test_dispatch import FakeSubstrate  # noqa: E402
from test_queue_order import _prepare_latched_notes  # noqa: E402
from test_queue_order_parity import _build_store, _load, _pending_sorted  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
HUB_FACTORY_STATUS_PATH = REPO_ROOT / "apps" / "mcp-hub" / "src" / "tools" / "factory_status.py"


def _load_factory_status_module():
    """Load the hub's ``tools/factory_status.py`` by file path -- it is not
    on this process's sys.path as an importable package (it lives under a
    sibling app, apps/mcp-hub/src)."""
    spec = importlib.util.spec_from_file_location("factory_status_under_test", HUB_FACTORY_STATUS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_dt(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------------
# AC-2: the synthetic fixture -- a non-empty order with both selectable and
# held tasks.
# ---------------------------------------------------------------------------


def test_four_surfaces_agree_on_a_non_empty_order(monkeypatch):
    fixture = _load("queue-synthetic-2026-09-23.json")
    header = fixture["header"]
    notes_by_task = {tid: list(notes) for tid, notes in fixture["notes_by_task"].items()}
    store, all_tasks = _build_store(fixture, notes_by_task)
    # schedule_status._describe_waiting_queue_from_store also computes
    # oldest_waiting_age (unaffected by this bead) off store.list_events,
    # which FakeSubstrate does not model; this test does not assert on that
    # field, so a bare empty log is enough to let the call through.
    store.list_events = lambda _task_id: []
    pending = _pending_sorted(all_tasks)
    release_by_task_id = dispatch.resolve_task_release_states(store, pending)

    verdicts = {
        task["id"]: queue_order.selectable_verdict(
            task, store.list_notes(task["id"]), all_tasks, release_by_task_id
        )
        for task in pending
    }
    selectable_ids = {tid for tid, verdict in verdicts.items() if verdict.selectable}
    assert selectable_ids, "fixture must carry at least one selectable task"

    # 1. dispatch.pick_task returns the first selectable, in FIFO order.
    picked = dispatch.pick_task(store, None, all_tasks)
    expected_pick = next(t for t in pending if verdicts[t["id"]].selectable)
    assert picked is not None
    assert picked["id"] == expected_pick["id"]

    # 2. schedule_status's selectable_count is exactly the number selectable.
    queue_status = schedule_status._describe_waiting_queue_from_store(
        store, now=_parse_dt(header["now"])
    )
    assert queue_status.selectable_count == len(selectable_ids)
    assert queue_status.latched_count == 0

    # 3. the hub's task_runnability agrees with every verdict, for every id.
    factory_status = _load_factory_status_module()
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", APP)
    for task in pending:
        result = factory_status.task_runnability(task["id"], store=store)
        verdict = verdicts[task["id"]]
        assert result["runnable"] == verdict.selectable
        assert result["selectable"] == verdict.selectable
        assert result["hold_reasons"] == list(verdict.hold_reasons)
        if verdict.selectable:
            assert result["reason"] is None
        else:
            assert result["reason"] == verdict.hold_reasons[0]


# ---------------------------------------------------------------------------
# AC-2: the latched snapshot -- every pending task held, several latched.
# ---------------------------------------------------------------------------


def test_four_surfaces_agree_on_the_latched_snapshot(monkeypatch):
    fixture = _load("queue-2026-09-23-latched.json")
    header = fixture["header"]
    assert header["selectable_ids"] == []

    notes_by_task = _prepare_latched_notes(fixture)
    store, all_tasks = _build_store(fixture, notes_by_task)
    pending = _pending_sorted(all_tasks)
    release_by_task_id = dispatch.resolve_task_release_states(store, pending)

    verdicts = {
        task["id"]: queue_order.selectable_verdict(
            task, store.list_notes(task["id"]), all_tasks, release_by_task_id
        )
        for task in pending
    }
    for task_id, verdict in verdicts.items():
        assert verdict.selectable is False, f"{task_id} unexpectedly selectable"
        assert verdict.hold_reasons, f"{task_id} is held but carries no hold reasons"

    latched_ids = {tid for ids in header["latched_signatures"].values() for tid in ids}
    for task_id in latched_ids:
        assert verdicts[task_id].latched is True

    # 1. pick_task picks nothing.
    assert dispatch.pick_task(store, None, all_tasks) is None

    # 2. schedule_status counts zero selectable, every latched id counted.
    queue_status = schedule_status._describe_waiting_queue_from_store(
        store, now=_parse_dt(header["captured_at"])
    )
    assert queue_status.selectable_count == 0
    assert queue_status.latched_count == len(latched_ids)

    # 3. the hub agrees, naming every hold reason, for every pending id.
    factory_status = _load_factory_status_module()
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", APP)
    for task in pending:
        result = factory_status.task_runnability(task["id"], store=store)
        verdict = verdicts[task["id"]]
        assert result["runnable"] is False
        assert result["selectable"] is False
        assert result["reason"] == verdict.hold_reasons[0]
        assert result["hold_reasons"] == list(verdict.hold_reasons)
        assert result["latched"] == verdict.latched


# ---------------------------------------------------------------------------
# AC-2: operator force is kept -- apply_breaker=False on the --task path
# only.
# ---------------------------------------------------------------------------


def _environmental_fault_notes(signature: str = "sig1", count: int = 3) -> list[dict]:
    return [
        {
            "id": f"fault-{i}",
            "created_at": f"2026-01-0{i}T00:00:00Z",
            "content": {
                "kind": "status",
                "body": "Environmental fault: boom",
                "environmental_fault_signature": signature,
            },
        }
        for i in range(1, count + 1)
    ]


def test_operator_forced_latched_bead_still_claims(monkeypatch):
    task = make_task("task-1", "2026-08-22T00:00:00Z")
    notes = _environmental_fault_notes(count=retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT)
    sub = FakeSubstrate(tasks=[task], notes={"task-1": notes})

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    forced = dispatch_steps.claim_activity({"task_id": "task-1", "dry_run": True})
    assert forced["status"] == "dry_run", forced

    not_forced = dispatch.pick_task(sub, None, sub.list_tasks())
    assert not_forced is None


def test_auto_pick_still_refuses_the_same_latched_bead(monkeypatch):
    """The other half of the same guarantee: with no task_id, the breaker
    still applies and claim_activity reports no_task."""
    task = make_task("task-1", "2026-08-22T00:00:00Z")
    notes = _environmental_fault_notes(count=retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT)
    sub = FakeSubstrate(tasks=[task], notes={"task-1": notes})

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({"dry_run": True})
    assert result["status"] == "no_task", result


def test_auto_pick_claim_recheck_applies_the_breaker(monkeypatch):
    """RC-6 (AC-2, kills M2a): a pick-then-latch race -- pick_task hands back
    a bead that the claim-time re-check must independently find latched --
    must still be refused on the auto-pick path (no task_id), because
    apply_breaker is True there. Only the explicit --task path (covered by
    test_operator_forced_latched_bead_still_claims, above) may force it."""
    task = make_task("task-1", "2026-08-22T00:00:00Z")
    notes = _environmental_fault_notes(count=retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT)
    sub = FakeSubstrate(tasks=[task], notes={"task-1": notes})

    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.setattr(dispatch, "pick_task", lambda *args, **kwargs: task)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({"dry_run": True})

    assert result["status"] == "not_runnable", result
    assert "same environmental fault recorded" in result["message"]


# ---------------------------------------------------------------------------
# AC-4: unreadable release state refuses the whole answer, for every reader.
# ---------------------------------------------------------------------------


def _raising_list_beads(*_args, **_kwargs):
    raise RuntimeError("release beads unreachable")


def _unreadable_release_store() -> FakeSubstrate:
    task = {
        "id": "task-1",
        "state": "pending",
        "created_at": "2026-01-01T00:00:00Z",
        "content": {"lane": "code-health", "title": "t", "scope": {"paths": ["apps/x/"]}},
    }
    store = FakeSubstrate(tasks=[task])
    store.list_beads = _raising_list_beads
    # Otherwise a missing list_events raises its own (unrelated) error first
    # and masks the release read this test exists to exercise (RC-4).
    store.list_events = lambda _task_id: []
    return store


def test_schedule_status_reports_unknown_when_release_read_fails():
    store = _unreadable_release_store()

    status = schedule_status.describe_waiting_queue(store, now=datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert status.claimable_count is None
    assert status.selectable_count is None
    assert status.latched_count is None
    assert status.error
    assert "release state" in status.error


def test_task_runnability_reports_unknown_when_release_read_fails(monkeypatch):
    store = _unreadable_release_store()

    factory_status = _load_factory_status_module()
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", APP)
    result = factory_status.task_runnability("task-1", store=store)

    assert result["status"] == "unknown"
    assert result["code"] == "unavailable"
    assert "found" not in result
    assert "runnable" not in result
