"""``tools.factory_status`` must answer exactly what the dispatcher and
``release-status.py`` would -- it imports and calls their own code rather
than re-deriving the rules, and it must say "unknown" (not "empty") when
that code or its data cannot be reached. No browser, no cluster, no network:
every substrate/reader dependency below is a small in-memory fake.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER_DIR = REPO_ROOT / "apps" / "factory-dispatcher"
if str(DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(DISPATCHER_DIR))

import guards  # noqa: E402

from tools import factory_status  # noqa: E402


# ---------------------------------------------------------------------------
# task_runnability
# ---------------------------------------------------------------------------


class FakeStore:
    """A BeadStore-shaped double -- no substrate, no network."""

    def __init__(self, tasks, notes_by_task=None):
        self.tasks = tasks
        self.notes_by_task = notes_by_task or {}

    def list_tasks(self, state=None, limit=200):
        if state:
            return [t for t in self.tasks if t.get("state") == state]
        return list(self.tasks)

    def list_notes(self, parent_id, limit=500):
        return list(self.notes_by_task.get(parent_id, []))

    def list_beads(self, namespace, type, state=None, limit=200):
        if namespace == "arch" and type == "release":
            return []
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return []


def _task(task_id, **content_overrides):
    content = {"lane": "code-health", "title": "t", "scope": {"paths": ["apps/mcp-hub/"]}}
    content.update(content_overrides)
    return {
        "id": task_id,
        "state": "pending",
        "created_at": "2026-09-01T00:00:00Z",
        "content": content,
    }


def test_task_runnable_matches_guards_is_runnable_for_a_task_that_cannot_start():
    question = {
        "id": "note-q1",
        "created_at": "2026-09-01T01:00:00Z",
        "content": {"kind": "question", "blocking": True, "body": "does this need a migration?"},
    }
    task = _task("task-1")
    store = FakeStore(tasks=[task], notes_by_task={"task-1": [question]})

    result = factory_status.task_runnability("task-1", store=store)

    # guards.is_runnable's own reason is exactly hold_reasons[0] --
    # queue_order.selectable_verdict pins it there by construction.
    expected = guards.is_runnable(task, [question], [task], release_by_task_id={})
    assert result == {
        "status": "ok",
        "found": True,
        "runnable": expected.ok,
        "reason": expected.reason,
        "selectable": expected.ok,
        "hold_reasons": [expected.reason],
        "latched": False,
    }
    assert result["runnable"] is False
    assert "waiting on blocking question" in result["reason"]


def test_task_runnable_reports_selectable_with_no_hold_reasons_when_claimable():
    task = _task("task-1")
    store = FakeStore(tasks=[task], notes_by_task={})

    result = factory_status.task_runnability("task-1", store=store)

    assert result["runnable"] is True
    assert result["selectable"] is True
    assert result["reason"] is None
    assert result["hold_reasons"] == []
    assert result["latched"] is False


def test_task_runnable_reports_latched_when_environmental_fault_breaker_trips():
    task = _task("task-1")
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
    store = FakeStore(tasks=[task], notes_by_task={"task-1": notes})

    result = factory_status.task_runnability("task-1", store=store)

    assert result["runnable"] is False
    assert result["selectable"] is False
    assert result["latched"] is True
    assert result["reason"] == result["hold_reasons"][0]
    assert result["reason"].startswith("same environmental fault recorded")


def test_task_runnable_reports_every_hold_reason_in_order():
    """RC-5 (AC-2, kills M4c): ``reason`` is ``hold_reasons[0]``, never the
    last one -- a task held by more than one gate must still name the first
    as its headline reason while carrying every hold in ``hold_reasons``."""
    question = {
        "id": "note-q1",
        "created_at": "2026-09-01T01:00:00Z",
        "content": {"kind": "question", "blocking": True, "body": "needs input"},
    }
    task = _task("task-1", scope={})  # no scope.paths
    store = FakeStore(tasks=[task], notes_by_task={"task-1": [question]})

    result = factory_status.task_runnability("task-1", store=store)

    assert len(result["hold_reasons"]) == 2
    assert result["reason"] == result["hold_reasons"][0]
    assert result["hold_reasons"][0].startswith("waiting on blocking question")
    assert result["hold_reasons"][1] == "task declares no scope.paths"


def test_task_runnable_not_found():
    store = FakeStore(tasks=[])
    assert factory_status.task_runnability("missing", store=store) == {
        "status": "ok",
        "found": False,
    }


def test_task_runnable_reports_unknown_when_the_store_cannot_be_read():
    """An unreadable population must not be reported as though it were empty."""

    class BrokenStore(FakeStore):
        def list_notes(self, parent_id, limit=500):
            raise RuntimeError("substrate unreachable")

    store = BrokenStore(tasks=[_task("task-1")])
    result = factory_status.task_runnability("task-1", store=store)

    assert result["status"] == "unknown"
    assert result["code"] == "unavailable"
    assert "substrate unreachable" in result["detail"]
    assert "found" not in result
    assert "runnable" not in result


# ---------------------------------------------------------------------------
# release_delivery
# ---------------------------------------------------------------------------


class FakeReleaseReader:
    """A SubstrateReader-shaped double for release-status.py's own reader protocol."""

    def __init__(self, tasks, release_bead, delivers_links, conformances):
        self.tasks = tasks
        self.release_bead = release_bead
        self.delivers_links = delivers_links
        self.conformances = conformances

    def list_beads(self, namespace, type_, **params):
        if namespace == "dev" and type_ == "task":
            return list(self.tasks)
        if namespace == "arch" and type_ == "release":
            return [self.release_bead] if self.release_bead else []
        if namespace == "arch" and type_ == "requirement_conformance":
            return list(self.conformances)
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return [link for link in self.delivers_links if link.get("target_id") == bead_id]


def _load_real_charter(ref: str):
    release_status = factory_status._release_status_module()
    release_load = release_status._load_release_load_module()
    releases_dir = release_status.REPO / release_status.RELEASES_DIR
    requirements_dir = release_status.REPO / release_status.REQUIREMENTS_DIR
    items = release_load.load_charters(
        release_load.charter_paths(releases_dir), requirements_dir=requirements_dir
    )
    return next(item for item in items if item.ref == ref)


def test_release_delivery_matches_release_status_build_release_report():
    charter_item = _load_real_charter("R26.02")
    outcome_id = charter_item.content["outcomes"][0]["id"]

    task = {"id": "task-1", "state": "done", "content": {"outcome_ref": outcome_id}}
    release_bead = {"id": "rel-1", "state": "in_flight", "content": {"ref": "R26.02"}}
    delivers_links = [{"source_id": "task-1", "target_id": "rel-1", "link_type": "delivers"}]
    reader = FakeReleaseReader(
        tasks=[task], release_bead=release_bead, delivers_links=delivers_links, conformances=[]
    )

    result = factory_status.release_delivery("R26.02", reader=reader)

    release_status = factory_status._release_status_module()
    expected = release_status.build_release_report(
        charter_item.content, [task], {"task-1": "R26.02"}, []
    )

    assert result["status"] == "ok"
    assert result["found"] is True
    assert result["ref"] == expected.ref
    assert result["name"] == expected.name

    matched = next(o for o in result["outcomes"] if o["id"] == outcome_id)
    expected_outcome = next(o for o in expected.outcomes if o.id == outcome_id)
    assert matched["task_count"] == expected_outcome.task_count == 1
    assert matched["tasks_by_state"] == expected_outcome.tasks_by_state == {"done": ["task-1"]}

    expected_balance = {b.work_class: b for b in expected.balance}
    for entry in result["balance"]:
        want = expected_balance[entry["work_class"]]
        assert entry["declared_pct"] == want.declared_pct
        assert entry["actual_count"] == want.actual_count
        assert entry["actual_pct"] == want.actual_pct
        assert entry["absent"] == want.absent
        assert entry["sample_size"] == want.sample_size
        assert entry["insufficient_sample"] == want.insufficient_sample


def test_release_delivery_carries_insufficient_sample():
    """Below release-status.py's MIN_CLASSIFIED_FOR_PERCENTAGE (5), every
    served balance entry must say so and carry no percentage -- copied
    straight off the BalanceEntry, not re-derived in the hub (AC5)."""
    charter_item = _load_real_charter("R26.02")
    outcome_id = charter_item.content["outcomes"][0]["id"]

    tasks = [{"id": f"task-{i}", "state": "done", "content": {"outcome_ref": outcome_id}} for i in range(4)]
    release_bead = {"id": "rel-1", "state": "in_flight", "content": {"ref": "R26.02"}}
    delivers_links = [
        {"source_id": t["id"], "target_id": "rel-1", "link_type": "delivers"} for t in tasks
    ]
    reader = FakeReleaseReader(
        tasks=tasks, release_bead=release_bead, delivers_links=delivers_links, conformances=[]
    )

    result = factory_status.release_delivery("R26.02", reader=reader)

    for entry in result["balance"]:
        assert entry["sample_size"] == 4
        assert entry["insufficient_sample"] is True
        assert entry["actual_pct"] is None

    matched = next(b for b in result["balance"] if b["work_class"] == charter_item.content["outcomes"][0]["work_class"])
    assert matched["declared_pct"] == charter_item.content["declared_balance"].get(matched["work_class"], 0)


def test_release_delivery_not_found_for_an_unknown_ref():
    assert factory_status.release_delivery("R99.99") == {"status": "ok", "found": False}


def test_release_delivery_reports_unknown_when_release_data_cannot_be_read():
    """An unreadable population must not be reported as though it were empty."""

    class BrokenReader(FakeReleaseReader):
        def list_beads(self, namespace, type_, **params):
            if namespace == "dev" and type_ == "task":
                raise RuntimeError("SUBSTRATE_URL and SUBSTRATE_API_KEY must be set")
            return super().list_beads(namespace, type_, **params)

    reader = BrokenReader(tasks=[], release_bead=None, delivers_links=[], conformances=[])
    result = factory_status.release_delivery("R26.02", reader=reader)

    assert result["status"] == "unknown"
    assert result["code"] == "unavailable"
    assert "SUBSTRATE_URL" in result["detail"]
    assert "balance" not in result


def test_release_delivery_reports_unknown_when_the_checkout_is_not_available(monkeypatch):
    """The production container ships only apps/mcp-hub/src -- if scripts/
    release-status.py is not present alongside this service, that must read
    as unknown, never as a release with nothing delivered."""
    missing = Path("/nonexistent-checkout-path-for-test")
    monkeypatch.setattr(factory_status, "SCRIPTS_DIR", missing)
    sys.modules.pop("release_status", None)
    try:
        result = factory_status.release_delivery("R26.02")
    finally:
        sys.modules.pop("release_status", None)

    assert result["status"] == "unknown"
    assert result["code"] == "unavailable"
    assert "not found" in result["detail"]


# ---------------------------------------------------------------------------
# criteria: a value that could not be computed must survive as Unknown,
# distinctly from zero/empty, all the way through this API's JSON boundary.
# ---------------------------------------------------------------------------


def test_release_delivery_includes_criteria_with_a_normal_verdict():
    charter_item = _load_real_charter("R26.02")
    outcome = next(o for o in charter_item.content["outcomes"] if o.get("requirement_refs"))
    ref = outcome["requirement_refs"][0]

    reader = FakeReleaseReader(tasks=[], release_bead=None, delivers_links=[], conformances=[])
    result = factory_status.release_delivery("R26.02", reader=reader)

    entry = next(c for c in result["criteria"] if c["ref"] == ref)
    assert entry["unmeasurable"] is None
    assert entry["latest"] is None  # genuinely no conformance recorded, not unknown


def test_release_delivery_reports_an_unmeasurable_criterion_as_unknown_through_json(monkeypatch):
    """A criterion reference that could not be evaluated must survive a full
    JSON round trip as Unknown -- never collapsing to null/0/"" -- at the
    actual API boundary this service serves over HTTP."""
    release_status = factory_status._release_status_module()
    unknown = factory_status._unknown_module()
    charter_item = _load_real_charter("R26.02")

    fake_report = release_status.ReleaseStatusReport(
        ref="R26.02",
        name=charter_item.content.get("name", "R26.02"),
        outcomes=[],
        unclassified_delivering=[],
        balance=[],
        criteria=[
            release_status.CriterionStatus(
                ref="not a real ref",
                as_of_opened=None,
                latest=None,
                stale=False,
                changed=False,
                unmeasurable=unknown.Unknown(reason="malformed reference"),
            )
        ],
    )
    monkeypatch.setattr(release_status, "build_release_report", lambda *a, **k: fake_report)

    reader = FakeReleaseReader(tasks=[], release_bead=None, delivers_links=[], conformances=[])
    result = factory_status.release_delivery("R26.02", reader=reader)

    # The actual transport step: this is what an HTTP client receives.
    wire = json.loads(json.dumps(result))
    entry = wire["criteria"][0]

    assert unknown.is_unknown(unknown.from_jsonable(entry["unmeasurable"])) is True
    assert entry["unmeasurable"] not in (0, 0.0, "", None, [], {})


def test_task_runnable_reports_unknown_when_the_checkout_is_not_available(monkeypatch):
    missing = Path("/nonexistent-checkout-path-for-test")
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", missing)

    result = factory_status.task_runnability("task-1")

    assert result["status"] == "unknown"
    assert "not found" in result["detail"]


def test_task_runnable_reports_unknown_when_repo_root_is_not_configured(monkeypatch):
    """The production container never sets FACTORY_STATUS_REPO_ROOT (it ships
    no factory-dispatcher checkout to point at) -- that is a real, permanent
    runtime state, not a misconfiguration, and must degrade to unknown the
    same way an unreadable checkout does, never raise at import or call
    time."""
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", None)

    result = factory_status.task_runnability("task-1")

    assert result["status"] == "unknown"
    assert result["code"] == "not_configured"
    assert "not configured" in result["detail"]
    assert factory_status.REPO_ROOT_ENV in result["detail"]


def test_release_delivery_reports_unknown_when_repo_root_is_not_configured(monkeypatch):
    monkeypatch.setattr(factory_status, "SCRIPTS_DIR", None)
    sys.modules.pop("release_status", None)
    try:
        result = factory_status.release_delivery("R26.02")
    finally:
        sys.modules.pop("release_status", None)

    assert result["status"] == "unknown"
    assert result["code"] == "not_configured"
    assert "not configured" in result["detail"]
    assert factory_status.REPO_ROOT_ENV in result["detail"]


def test_task_runnable_not_configured_is_distinct_from_checkout_present_but_broken(monkeypatch):
    """OPS-110: 'never shipped' (code: not_configured) must not collapse into
    'shipped but unreadable' (code: unavailable) -- the console renders them
    differently (see release-view.ts / dev-board.ts)."""
    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", Path("/nonexistent-checkout-path-for-test"))
    broken = factory_status.task_runnability("task-1")

    monkeypatch.setattr(factory_status, "DISPATCHER_DIR", None)
    unconfigured = factory_status.task_runnability("task-1")

    assert broken["code"] == "unavailable"
    assert unconfigured["code"] == "not_configured"
