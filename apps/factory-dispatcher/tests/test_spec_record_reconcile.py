"""Schedule scanner.scan_spec_record/scan_ahead_of_gate_filings on the credentialed
nightly (PRIN-005: this reuses the reconciler that already exists rather than write a
second one -- every read below is a call into scanner.py, exactly as scanner.py's own
CLI calls it).

No substrate, no network, no Temporal server: evaluate_spec_record/land_spec_record_check
are pure functions over a fake store and injected resolvers, and the alert functions are
exercised against a recording fake policy -- the same technique
test_doctrine_registry_view.py already uses for the OPS-75 precedent this activity follows.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import spec_record_reconcile as src  # noqa: E402
import scanner  # noqa: E402


def fixed_now() -> datetime:
    return datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)


def later() -> datetime:
    return datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)


def spec_file(tasks_dir, name, title, note=None):
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payload = {"title": title}
    if note is not None:
        payload["_filing_note"] = note
    (tasks_dir / name).write_text(json.dumps(payload))


def task_with_title(title, state="pending", task_id="t-1", spec_identity=None):
    content = {"title": title}
    if spec_identity is not None:
        content["spec_identity"] = spec_identity
    return {"id": task_id, "state": state, "content": content}


class FakeSpecRecordStore:
    """Mirrors the live store's list_tasks + observation-CRUD shape."""

    def __init__(self, tasks=None):
        self._tasks = list(tasks or [])
        self.observations: dict[str, dict] = {}
        self._next_id = 1

    def list_tasks(self):
        return list(self._tasks)

    def find_observation(self, ref):
        return self.observations.get(ref)

    def create_observation(self, payload):
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["created_by"] == src.CREATED_BY
        ref = payload["content"]["ref"]
        assert ref not in self.observations, "create called for an existing ref"
        bead = {
            "id": f"obs-{self._next_id}",
            "created_by": payload["created_by"],
            "state": payload["state"],
            "content": payload["content"],
            "context": payload["context"],
        }
        self._next_id += 1
        self.observations[ref] = bead
        return dict(bead)

    def update_observation(self, bead_id, *, content=None, context=None, state=None):
        for bead in self.observations.values():
            if bead["id"] == bead_id:
                if content is not None:
                    bead["content"] = content
                if context is not None:
                    bead["context"] = context
                if state is not None:
                    bead["state"] = state
                return dict(bead)
        raise AssertionError(f"update_observation called for unknown bead_id {bead_id!r}")


class ReadsFailStore(FakeSpecRecordStore):
    """list_tasks (the evaluation side) is down; the observation CRUD works."""

    def list_tasks(self):
        raise RuntimeError("connection refused")


class DeadStore:
    """Every method raises -- the full-substrate-outage shape (reads AND writes)."""

    def __getattr__(self, name):
        def method(*args, **kwargs):
            raise ConnectionRefusedError("substrate down")

        return method


class LandingDeadStore(FakeSpecRecordStore):
    """Evaluation-side reads work; the observation landing side is down."""

    def find_observation(self, ref):
        raise ConnectionRefusedError("substrate died between read and landing")


class EvaluationDeadStore(FakeSpecRecordStore):
    """Evaluation-side reads are down; the observation landing side works."""

    def list_tasks(self):
        raise ConnectionRefusedError("substrate down")


# ---------------------------------------------------------------------------
# resolve_credentials — PRIN-015's credless half
# ---------------------------------------------------------------------------


def test_resolve_credentials_none_when_url_is_unset(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    assert src.resolve_credentials() is None


def test_resolve_credentials_none_when_key_is_unset(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    assert src.resolve_credentials() is None


def test_resolve_credentials_none_when_both_are_unset(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    assert src.resolve_credentials() is None


def test_resolve_credentials_returns_both_when_present(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    assert src.resolve_credentials() == ("https://sub.example", "k")


# ---------------------------------------------------------------------------
# evaluate_spec_record — never raises; "cannot tell" is its own condition
# ---------------------------------------------------------------------------


def test_evaluate_reports_clean_when_record_and_beads_agree(tmp_path):
    store = FakeSpecRecordStore([])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: frozenset()
    )

    assert evaluation["condition"] == src.CLEAN


def test_evaluate_reports_diverged_for_a_dropped_filing(tmp_path):
    spec_file(tmp_path, "dropped.json", "Dropped filing")
    store = FakeSpecRecordStore([])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: frozenset()
    )

    assert evaluation["condition"] == src.DIVERGED
    assert [i.title for i in evaluation["issues"]] == ["Dropped filing"]


def test_evaluate_excludes_done_spec_no_bead_from_drift(tmp_path):
    spec_file(tmp_path / "done", "predates-beads.json", "Shipped before beads existed")
    store = FakeSpecRecordStore([])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: frozenset()
    )

    assert evaluation["condition"] == src.CLEAN


def test_evaluate_a_valid_hold_note_keeps_the_run_clean(tmp_path):
    spec_file(
        tmp_path, "held.json", "Held work", note="Do not file until the Operator directs the next step."
    )
    store = FakeSpecRecordStore([])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: frozenset()
    )

    assert evaluation["condition"] == src.CLEAN


def test_evaluate_reports_diverged_for_an_ahead_of_gate_filing(tmp_path):
    task = task_with_title("Filed ahead", spec_identity="apps/factory-dispatcher/tasks/GHOST.json")
    store = FakeSpecRecordStore([task])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: frozenset()
    )

    assert evaluation["condition"] == src.DIVERGED
    assert [i.kind for i in evaluation["ahead_of_gate"]] == ["ahead-of-gate-live"]


def test_evaluate_reports_failed_to_evaluate_when_the_store_is_unreachable(tmp_path):
    evaluation = src.evaluate_spec_record(
        ReadsFailStore(),
        queue_dirs=(tmp_path,),
        ahead_of_gate_main_basenames_fn=lambda: frozenset(),
    )

    assert evaluation["condition"] == src.FAILED_TO_EVALUATE
    assert "connection refused" in evaluation["error"]
    assert evaluation["error_type"] == "RuntimeError"


def test_evaluate_reports_failed_to_evaluate_when_main_cannot_be_resolved(tmp_path):
    store = FakeSpecRecordStore([])

    evaluation = src.evaluate_spec_record(
        store, queue_dirs=(tmp_path,), ahead_of_gate_main_basenames_fn=lambda: None
    )

    assert evaluation["condition"] == src.FAILED_TO_EVALUATE
    assert "main" in evaluation["error"]


def test_spec_record_reconcile_evaluate_never_reports_clean_on_a_read_failure(tmp_path):
    evaluation = src.evaluate_spec_record(ReadsFailStore(), queue_dirs=(tmp_path,))
    assert evaluation["condition"] != src.CLEAN


# ---------------------------------------------------------------------------
# land_spec_record_check — one standing observation, updated on every run
# ---------------------------------------------------------------------------


def test_spec_record_reconcile_clean_run_still_lands_and_updates_the_observation():
    store = FakeSpecRecordStore()

    first = src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=fixed_now)
    assert first["action"] == "created"
    [obs] = store.observations.values()
    assert obs["state"] == "resolved"
    assert obs["content"]["observed_at"] == src._iso(fixed_now())

    second = src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=later)
    assert second["action"] == "updated"
    assert len(store.observations) == 1
    [obs_after] = store.observations.values()
    assert obs_after["content"]["observed_at"] == src._iso(later())


def test_land_sets_source_class_derived_and_enrolled_created_by_on_create():
    """"factory-dispatcher/spec-record-reconcile" is enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822) --
    the fresh-ref (create) path must declare it."""
    store = FakeSpecRecordStore()

    result = src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=fixed_now)

    assert result["action"] == "created"
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "derived"
    assert obs["created_by"] == src.CREATED_BY


def test_land_sets_source_class_derived_and_enrolled_created_by_on_update():
    """Same identity, existing-ref (update) path -- asserted by value against the
    stored bead since FakeSpecRecordStore.update_observation accepts content without
    asserting on it."""
    store = FakeSpecRecordStore()
    src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=fixed_now)

    result = src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=later)

    assert result["action"] == "updated"
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "derived"
    assert obs["created_by"] == src.CREATED_BY


def test_diverged_run_lands_active_with_issues_recorded(tmp_path):
    store = FakeSpecRecordStore()
    issue = scanner.SpecRecordIssue(
        kind="queued-spec-no-bead", path=tmp_path / "x.json", title="X", age_days=9
    )

    src.land_spec_record_check(
        store,
        {"condition": src.DIVERGED, "issues": (issue,), "ahead_of_gate": ()},
        now_fn=fixed_now,
    )

    [obs] = store.observations.values()
    assert obs["state"] == "active"
    assert obs["context"]["issues"] == [
        {
            "kind": "queued-spec-no-bead",
            "path": issue.display_path,
            "title": "X",
            "bead_id": "",
            "bead_state": "",
            "hold_note_category": "",
        }
    ]
    assert obs["context"]["ahead_of_gate"] == []


def test_spec_record_reconcile_observation_resolves_the_run_divergence_clears(tmp_path):
    store = FakeSpecRecordStore()
    issue = scanner.SpecRecordIssue(kind="queued-spec-no-bead", path=tmp_path / "x.json", title="X")
    src.land_spec_record_check(
        store,
        {"condition": src.DIVERGED, "issues": (issue,), "ahead_of_gate": ()},
        now_fn=fixed_now,
    )
    [obs_before] = store.observations.values()
    assert obs_before["state"] == "active"

    src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=later)

    [obs_after] = store.observations.values()
    assert obs_after["state"] == "resolved"


def test_spec_record_reconcile_observation_reopens_without_a_second_bead_when_divergence_recurs(tmp_path):
    store = FakeSpecRecordStore()
    src.land_spec_record_check(store, {"condition": src.CLEAN}, now_fn=fixed_now)

    issue = scanner.SpecRecordIssue(kind="queued-spec-no-bead", path=tmp_path / "x.json", title="X")
    src.land_spec_record_check(
        store,
        {"condition": src.DIVERGED, "issues": (issue,), "ahead_of_gate": ()},
        now_fn=later,
    )

    assert len(store.observations) == 1
    [obs] = store.observations.values()
    assert obs["state"] == "active"


def test_spec_record_reconcile_failed_to_evaluate_lands_active_with_the_error_recorded():
    store = FakeSpecRecordStore()

    src.land_spec_record_check(
        store,
        {"condition": src.FAILED_TO_EVALUATE, "error": "RuntimeError: boom"},
        now_fn=fixed_now,
    )

    [obs] = store.observations.values()
    assert obs["state"] == "active"
    assert obs["context"]["error"] == "RuntimeError: boom"


# ---------------------------------------------------------------------------
# announce_* — the standing per-subject dedup policy, shared across the app
# ---------------------------------------------------------------------------


class RecordingPolicy:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, kind, fingerprint, content, *, severity=None, min_interval_hours=0.0, members=None, **kwargs):
        self.sent.append((kind, {"fingerprint": fingerprint, "content": content, **kwargs}))
        return True


class ExplodingPolicy:
    async def send(self, *args, **kwargs):
        raise RuntimeError("webhook exploded")


def test_spec_record_reconcile_announce_drift_raises_the_declared_alert(tmp_path):
    policy = RecordingPolicy()
    issue = scanner.SpecRecordIssue(kind="queued-spec-no-bead", path=tmp_path / "x.json", title="X")

    posted = src.announce_drift((issue,), (), policy=policy)

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "spec_record_drift"
    assert "queued-spec-no-bead" in payload["content"]
    assert payload["re_alert_interval_hours"] == src.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_spec_record_reconcile_announce_drift_uses_the_declared_inventory_entry():
    definition = src.notify.get_alert_definition(src.DRIFT_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_spec_record_reconcile_announce_drift_is_best_effort_and_never_raises():
    result = src.announce_drift((), (), policy=ExplodingPolicy())
    assert result is False


def test_spec_record_reconcile_announce_unreachable_raises_the_declared_alert():
    policy = RecordingPolicy()

    posted = src.announce_unreachable("RuntimeError: boom", policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == "spec_record_check_unreachable"
    assert "boom" in payload["content"]
    assert payload["re_alert_interval_hours"] == src.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_spec_record_reconcile_announce_unreachable_uses_the_declared_inventory_entry():
    definition = src.notify.get_alert_definition(src.UNREACHABLE_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_spec_record_reconcile_announce_unreachable_is_best_effort_and_never_raises():
    result = src.announce_unreachable("boom", policy=ExplodingPolicy())
    assert result is False


def test_spec_record_reconcile_repeated_identical_drift_is_suppressed_until_the_declared_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = src.notify.AlertPolicy(
        load_alert_state=src.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=src.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    issue = scanner.SpecRecordIssue(kind="queued-spec-no-bead", path=tmp_path / "x.json", title="X")

    first = src.announce_drift((issue,), (), policy=policy)
    second = src.announce_drift((issue,), (), policy=policy)

    assert first is True
    assert second is False
    assert fake_post.calls == 1


# ---------------------------------------------------------------------------
# activities.__init__ — the activity is registered for the live worker
# ---------------------------------------------------------------------------


def test_check_spec_record_activity_is_registered():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "check_spec_record_activity" in names


# ---------------------------------------------------------------------------
# check_spec_record_activity — credential posture (PRIN-015 both halves)
# ---------------------------------------------------------------------------


def test_activity_skips_quietly_on_a_credless_host_and_names_no_credential(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    announced: list[str] = []
    monkeypatch.setattr(src, "announce_drift", lambda *a, **kw: announced.append("drift") or True)
    monkeypatch.setattr(
        src, "announce_unreachable", lambda *a, **kw: announced.append("unreachable") or True
    )

    result = src.check_spec_record_activity({})

    assert result == {"status": "skipped", "condition": src.SKIPPED_CREDLESS}
    assert announced == []


def test_activity_violates_when_credentialed_and_the_store_is_unreachable(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    announced: list[str] = []
    monkeypatch.setattr(src, "SubstrateSpecRecordStore", lambda *a, **kw: EvaluationDeadStore())
    monkeypatch.setattr(
        src, "announce_unreachable", lambda error, **kw: announced.append(error) or True
    )

    result = src.check_spec_record_activity({})

    assert result["condition"] == src.FAILED_TO_EVALUATE
    assert len(announced) == 1
    assert "substrate down" in announced[0]


def test_spec_record_reconcile_activity_announces_unreachable_under_a_full_outage_and_still_raises(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    announced: list[str] = []
    monkeypatch.setattr(src, "SubstrateSpecRecordStore", lambda *a, **kw: DeadStore())
    monkeypatch.setattr(
        src, "announce_unreachable", lambda error, **kw: announced.append(error) or True
    )

    import pytest

    with pytest.raises(ConnectionRefusedError):
        src.check_spec_record_activity({})

    # The evaluation-side unreachable alert fired BEFORE the landing crash,
    # and the landing crash announced itself too rather than dying silent.
    assert len(announced) == 2
    assert "substrate down" in announced[0]
    assert "landing the standing observation failed" in announced[1]


def test_spec_record_reconcile_activity_announces_when_only_the_landing_side_is_down(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    announced: list[str] = []
    monkeypatch.setattr(
        src,
        "evaluate_spec_record",
        lambda store, **kw: {"condition": src.CLEAN},
    )
    monkeypatch.setattr(src, "SubstrateSpecRecordStore", lambda *a, **kw: LandingDeadStore())
    monkeypatch.setattr(
        src, "announce_unreachable", lambda error, **kw: announced.append(error) or True
    )

    import pytest

    with pytest.raises(ConnectionRefusedError):
        src.check_spec_record_activity({})

    assert len(announced) == 1
    assert "clean" in announced[0]
    assert "landing the standing observation failed" in announced[0]


def test_spec_record_reconcile_activity_announces_divergence_before_attempting_to_land(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    order: list[str] = []
    monkeypatch.setattr(
        src,
        "evaluate_spec_record",
        lambda store, **kw: {"condition": src.DIVERGED, "issues": (), "ahead_of_gate": ()},
    )
    monkeypatch.setattr(src, "SubstrateSpecRecordStore", lambda *a, **kw: FakeSpecRecordStore())
    monkeypatch.setattr(
        src, "announce_drift", lambda issues, ahead, **kw: order.append("announce") or True
    )
    real_land = src.land_spec_record_check
    monkeypatch.setattr(
        src,
        "land_spec_record_check",
        lambda store, evaluation, **kw: order.append("land") or real_land(store, evaluation, **kw),
    )

    src.check_spec_record_activity({})

    assert order == ["announce", "land"]


def test_activity_lands_clean_and_alerts_nothing_when_the_record_agrees(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://sub.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    announced: list[str] = []
    monkeypatch.setattr(
        src,
        "evaluate_spec_record",
        lambda store, **kw: {"condition": src.CLEAN},
    )
    monkeypatch.setattr(src, "SubstrateSpecRecordStore", lambda *a, **kw: FakeSpecRecordStore())
    monkeypatch.setattr(src, "announce_drift", lambda *a, **kw: announced.append("drift") or True)
    monkeypatch.setattr(
        src, "announce_unreachable", lambda *a, **kw: announced.append("unreachable") or True
    )

    result = src.check_spec_record_activity({})

    assert result["condition"] == src.CLEAN
    assert announced == []
