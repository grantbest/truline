"""OPS-75: `principles_sync check-view` mechanized on the doctrine-staleness nightly.

No substrate, no network, no Temporal server: `evaluate_registry_view`/`land_check_view`
are pure functions over a fake store and a `pathlib.Path`-shaped double, and the alert
functions are exercised against a recording fake policy, the same technique
test_stranded_alerts.py already uses for failure_diagnosis.py's shared dedup mechanism.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import doctrine_registry_view as drv  # noqa: E402


def fixed_now() -> datetime:
    return datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


def later() -> datetime:
    return datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)


class FakeRegistryPath:
    """Stands in for `pathlib.Path`: `.read_text()` either returns text or raises."""

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error

    def read_text(self) -> str:
        if self._error is not None:
            raise self._error
        return self._text or ""


class ExplodingStore:
    """`list_principles` always raises -- the substrate-unreachable shape."""

    def __init__(self, error: Exception):
        self._error = error

    def list_principles(self):
        raise self._error


class FakeDoctrineRegistryViewStore:
    """Mirrors the live store's `list_principles` + observation-CRUD shape."""

    def __init__(self, principle_beads=None):
        self._principle_beads = list(principle_beads or [])
        self.observations: dict[str, dict] = {}
        self._next_id = 1

    def list_principles(self):
        return list(self._principle_beads)

    def find_observation(self, ref):
        return self.observations.get(ref)

    def create_observation(self, payload):
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["created_by"] == drv.CREATED_BY
        assert payload["content"]["source_class"] == "observed"  # #800 enrolled this identity
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


def principle_bead(ref: str, **overrides) -> dict:
    content = {
        "ref": ref,
        "name": ref,
        "statement": "a statement",
        "source": "incident-1",
        "status": "adopted",
        "status_note": "",
        "notes": [],
    }
    content.update(overrides)
    return {"id": f"bead-{ref}", "content": content}


PRIN_HEADING = "Seeded 2026-08-01\n"


def registry_text(*entries: str) -> str:
    return PRIN_HEADING + "\n\n" + "\n\n".join(entries)


def entry_block(ref: str, name: str = "Name") -> str:
    return (
        f"### {ref} — {name}\n\n"
        "- **Statement:** a statement\n"
        "- **Source:** incident-1\n"
        "- **Status:** `adopted`"
    )


# ---------------------------------------------------------------------------
# evaluate_registry_view — never raises; failure is its own condition
# ---------------------------------------------------------------------------


def test_evaluate_reports_clean_when_file_matches_beads():
    store = FakeDoctrineRegistryViewStore([principle_bead("PRIN-001", name="Name")])
    path = FakeRegistryPath(registry_text(entry_block("PRIN-001")))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.CLEAN


def test_evaluate_reports_diverged_and_names_the_id_present_only_in_the_file():
    store = FakeDoctrineRegistryViewStore([])
    path = FakeRegistryPath(registry_text(entry_block("PRIN-001")))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.DIVERGED
    assert evaluation["diverging_ids"] == ["PRIN-001"]
    assert evaluation["leader"] == "file"


def test_evaluate_reports_diverged_and_names_the_id_present_only_in_the_beads():
    store = FakeDoctrineRegistryViewStore([principle_bead("PRIN-001")])
    path = FakeRegistryPath(registry_text())

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.DIVERGED
    assert "PRIN-001" in evaluation["diverging_ids"]
    assert evaluation["leader"] == "beads"


def test_evaluate_reports_mixed_leader_for_a_field_level_mismatch():
    store = FakeDoctrineRegistryViewStore([principle_bead("PRIN-001", status="retired")])
    path = FakeRegistryPath(registry_text(entry_block("PRIN-001")))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.DIVERGED
    assert evaluation["leader"] == "mixed"


def test_evaluate_reports_failed_to_evaluate_when_the_substrate_is_unreachable():
    store = ExplodingStore(RuntimeError("connection refused"))
    path = FakeRegistryPath(registry_text(entry_block("PRIN-001")))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.FAILED_TO_EVALUATE
    assert "connection refused" in evaluation["error"]
    assert evaluation["error_type"] == "RuntimeError"


def test_evaluate_reports_failed_to_evaluate_when_the_file_cannot_be_read():
    store = FakeDoctrineRegistryViewStore([])
    path = FakeRegistryPath(error=OSError("no such file"))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] == drv.FAILED_TO_EVALUATE
    assert "no such file" in evaluation["error"]


def test_doctrine_registry_view_evaluate_never_reports_clean_on_a_read_failure():
    store = ExplodingStore(RuntimeError("boom"))
    path = FakeRegistryPath(registry_text(entry_block("PRIN-001")))

    evaluation = drv.evaluate_registry_view(store, path)

    assert evaluation["condition"] != drv.CLEAN


# ---------------------------------------------------------------------------
# land_check_view — one standing observation, updated on every run
# ---------------------------------------------------------------------------


def test_doctrine_registry_view_clean_run_still_lands_and_updates_the_observation():
    store = FakeDoctrineRegistryViewStore()

    first = drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=fixed_now)
    assert first["action"] == "created"
    [obs] = store.observations.values()
    assert obs["state"] == "resolved"
    assert obs["content"]["observed_at"] == drv._iso(fixed_now())

    second = drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=later)
    assert second["action"] == "updated"
    assert len(store.observations) == 1
    [obs_after] = store.observations.values()
    assert obs_after["content"]["observed_at"] == drv._iso(later())


def test_diverged_run_creates_an_active_observation_naming_ids_and_leader():
    store = FakeDoctrineRegistryViewStore()
    evaluation = {
        "condition": drv.DIVERGED,
        "diffs": ["PRIN-001: present in the file, missing from the beads"],
        "diverging_ids": ["PRIN-001"],
        "leader": "file",
    }

    drv.land_check_view(store, evaluation, now_fn=fixed_now)

    [obs] = store.observations.values()
    assert obs["state"] == "active"
    assert obs["context"]["diverging_ids"] == ["PRIN-001"]
    assert obs["context"]["leader"] == "file"


def test_doctrine_registry_view_observation_resolves_the_run_divergence_clears():
    store = FakeDoctrineRegistryViewStore()
    drv.land_check_view(
        store,
        {"condition": drv.DIVERGED, "diffs": ["x"], "diverging_ids": ["PRIN-001"], "leader": "file"},
        now_fn=fixed_now,
    )
    [obs_before] = store.observations.values()
    assert obs_before["state"] == "active"

    drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=later)

    [obs_after] = store.observations.values()
    assert obs_after["state"] == "resolved"


def test_doctrine_registry_view_observation_reopens_without_a_second_bead_when_divergence_recurs():
    store = FakeDoctrineRegistryViewStore()
    drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=fixed_now)

    drv.land_check_view(
        store,
        {"condition": drv.DIVERGED, "diffs": ["x"], "diverging_ids": ["PRIN-001"], "leader": "file"},
        now_fn=later,
    )

    assert len(store.observations) == 1
    [obs] = store.observations.values()
    assert obs["state"] == "active"


def test_doctrine_registry_view_land_sets_source_class_observed_and_enrolled_created_by_on_create():
    """#800 enrolled "factory-dispatcher/doctrine-registry-view" in bead_rules.py's
    SOURCE_CLASS_WRITERS["observed"] -- the fresh-ref (create) path must declare it."""
    store = FakeDoctrineRegistryViewStore()
    result = drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=fixed_now)

    assert result["action"] == "created"
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "observed"
    assert obs["created_by"] == drv.CREATED_BY


def test_doctrine_registry_view_land_sets_source_class_observed_and_enrolled_created_by_on_update():
    """Same identity, existing-ref (update) path -- the live obs.principles-check-view
    bead (2d1c48f9) already exists, so the doctrine writer's first post-fix run takes this
    path. Asserted by value against the stored bead since
    FakeDoctrineRegistryViewStore.update_observation accepts content without asserting on
    it."""
    store = FakeDoctrineRegistryViewStore()
    drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=fixed_now)
    result = drv.land_check_view(store, {"condition": drv.CLEAN}, now_fn=later)

    assert result["action"] == "updated"
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "observed"
    assert obs["created_by"] == drv.CREATED_BY


def test_doctrine_registry_view_failed_to_evaluate_lands_active_with_the_error_recorded():
    store = FakeDoctrineRegistryViewStore()

    drv.land_check_view(
        store,
        {"condition": drv.FAILED_TO_EVALUATE, "error": "RuntimeError: boom"},
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


def test_announce_registry_drift_raises_the_declared_alert_naming_ids_and_leader():
    policy = RecordingPolicy()

    posted = drv.announce_registry_drift(["PRIN-001"], "file", ["PRIN-001: x"], policy=policy)

    assert posted is True
    assert len(policy.sent) == 1
    kind, payload = policy.sent[0]
    assert kind == "doctrine_registry_drift"
    assert "PRIN-001" in payload["content"]
    assert payload["re_alert_interval_hours"] == drv.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_announce_registry_drift_uses_the_declared_inventory_entry():
    definition = drv.notify.get_alert_definition(drv.DIVERGENCE_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_announce_registry_drift_is_best_effort_and_never_raises():
    result = drv.announce_registry_drift(["PRIN-001"], "file", ["x"], policy=ExplodingPolicy())
    assert result is False


def test_announce_registry_view_unreachable_raises_the_declared_alert():
    policy = RecordingPolicy()

    posted = drv.announce_registry_view_unreachable("RuntimeError: boom", policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == "doctrine_registry_view_unreachable"
    assert "boom" in payload["content"]
    assert payload["re_alert_interval_hours"] == drv.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_announce_registry_view_unreachable_uses_the_declared_inventory_entry():
    definition = drv.notify.get_alert_definition(drv.UNREACHABLE_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_announce_registry_view_unreachable_is_best_effort_and_never_raises():
    result = drv.announce_registry_view_unreachable("boom", policy=ExplodingPolicy())
    assert result is False


def test_repeated_identical_divergence_is_suppressed_until_the_declared_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = drv.notify.AlertPolicy(
        load_alert_state=drv.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=drv.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = drv.announce_registry_drift(["PRIN-001"], "file", ["PRIN-001: x"], policy=policy)
    second = drv.announce_registry_drift(["PRIN-001"], "file", ["PRIN-001: x"], policy=policy)

    assert first is True
    assert second is False
    assert fake_post.calls == 1


# ---------------------------------------------------------------------------
# activities.__init__ — the activity is registered for the live worker
# ---------------------------------------------------------------------------


def test_check_principles_registry_view_activity_is_registered():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "check_principles_registry_view_activity" in names


# ---------------------------------------------------------------------------
# the activity under an outage (the gate finding on attempt 1: landing before
# announcing crashed the activity in exactly the outage the alert exists for)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


class DeadStore:
    """Every method raises -- the full-substrate-outage shape (reads AND writes)."""

    def __getattr__(self, name):
        def method(*args, **kwargs):
            raise ConnectionRefusedError("substrate down")

        return method


class LandingDeadStore(FakeDoctrineRegistryViewStore):
    """Evaluation-side reads work; the observation landing side is down."""

    def find_observation(self, ref):
        raise ConnectionRefusedError("substrate died between read and landing")


def test_doctrine_registry_view_activity_announces_unreachable_under_a_full_outage_and_still_raises(monkeypatch):
    announced: list[str] = []
    monkeypatch.setattr(drv, "default_store", lambda: DeadStore())
    monkeypatch.setattr(
        drv,
        "announce_registry_view_unreachable",
        lambda error, **kw: announced.append(error) or True,
    )
    with pytest.raises(ConnectionRefusedError):
        drv.check_principles_registry_view_activity({})
    # The evaluation-side unreachable alert fired BEFORE the landing crash, and the
    # landing crash announced itself too rather than dying silent.
    assert len(announced) == 2
    assert "substrate down" in announced[0]
    assert "landing the standing observation failed" in announced[1]


def test_doctrine_registry_view_activity_announces_when_only_the_landing_side_is_down(monkeypatch):
    announced: list[str] = []
    monkeypatch.setattr(
        drv,
        "evaluate_registry_view",
        lambda store: {"condition": drv.CLEAN, "diverging_ids": [], "leader": None, "diffs": []},
    )
    monkeypatch.setattr(drv, "default_store", lambda: LandingDeadStore())
    monkeypatch.setattr(
        drv,
        "announce_registry_view_unreachable",
        lambda error, **kw: announced.append(error) or True,
    )
    with pytest.raises(ConnectionRefusedError):
        drv.check_principles_registry_view_activity({})
    assert len(announced) == 1
    assert "clean" in announced[0]
    assert "landing the standing observation failed" in announced[0]


def test_doctrine_registry_view_activity_announces_divergence_before_attempting_to_land(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        drv,
        "evaluate_registry_view",
        lambda store: {
            "condition": drv.DIVERGED,
            "diverging_ids": ["PRIN-001"],
            "leader": "file",
            "diffs": ["PRIN-001: x"],
        },
    )
    monkeypatch.setattr(drv, "default_store", lambda: FakeDoctrineRegistryViewStore())
    monkeypatch.setattr(
        drv,
        "announce_registry_drift",
        lambda ids, leader, diffs, **kw: order.append("announce") or True,
    )
    real_land = drv.land_check_view
    monkeypatch.setattr(
        drv,
        "land_check_view",
        lambda store, evaluation, **kw: order.append("land") or real_land(store, evaluation, **kw),
    )
    drv.check_principles_registry_view_activity({})
    assert order == ["announce", "land"]


# ---------------------------------------------------------------------------
# workflow sequencing: the check-view leg is independent of the staleness leg
# ---------------------------------------------------------------------------


def test_an_earlier_legs_failure_still_runs_every_later_leg():
    """AST-level (matching test_schedule_runtime's source-assertion idiom -- no Temporal
    server in the verification environment): each of the five nightly legs
    (staleness, check-view, spec-record reconcile, deployed-revision-drift, EA coverage)
    sits in its own try/except ActivityError that only records the failure -- no re-raise
    inside the handler, no other activity call -- so one leg's failure can never stop a
    later leg from running and raising its own declared alerts. The first recorded
    failure is re-raised exactly once, after every leg has had its turn."""
    import ast
    import inspect

    from workflows import doctrine_staleness

    tree = ast.parse(inspect.getsource(doctrine_staleness))
    run_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )

    def activity_names(nodes):
        names = []
        for node in nodes:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute_activity"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                ):
                    names.append(call.args[0].value)
        return names

    try_nodes = [node for node in ast.walk(run_fn) if isinstance(node, ast.Try)]
    assert len(try_nodes) == 5

    leg_names = {activity_names(node.body)[0] for node in try_nodes}
    assert leg_names == {
        "report_doctrine_staleness",
        "check_principles_registry_view",
        "check_spec_record",
        "check_deployed_revision_drift",
        "report_ea_coverage",
    }

    for node in try_nodes:
        assert len(node.handlers) == 1
        handler = node.handlers[0]
        assert isinstance(handler.type, ast.Name) and handler.type.id == "ActivityError"
        # A handler that awaited another activity or re-raised would block a
        # later leg (or hide an earlier leg's own failure) -- neither happens.
        assert activity_names(handler.body) == []
        assert not any(isinstance(n, ast.Raise) for n in ast.walk(handler))

    # Exactly one re-raise in the whole function -- the final, unconditional
    # one, outside every try/except, once every leg has run.
    raises = [node for node in ast.walk(run_fn) if isinstance(node, ast.Raise)]
    assert len(raises) == 1
    assert raises[0].exc is not None


def test_doctrine_registry_view_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
    """#727: the change reconciler's live `_request` passed `headers=self._headers`
    unconditionally, so a caller supplying its own `headers=` kwarg collided with it
    (`httpx.request() got multiple values for keyword argument 'headers'`) -- a
    live-only shape the FakeStore, which replaces the whole store, never walks. This
    store shares the same `_request` shape. Pins: one merged headers dict carrying
    BOTH the standing auth header and a per-call header, per-call winning on
    collision.
    """
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(drv.httpx, "request", fake_request)

    store = drv.SubstrateDoctrineRegistryViewStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": drv.CREATED_BY, "Content-Type": "text/plain"},
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == drv.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"
