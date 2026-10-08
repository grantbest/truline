"""Tests for the nightly release status activity/workflow.

No Temporal server, no substrate database, no cluster, no network: every reader/store below is
an in-process fake. `release_status`/`release_load` are the *real*
scripts/release-status.py/scripts/release-load.py, loaded by path the same way
scripts/tests/test_release_balance_report.py and activities/ea_apply.py's own tests do -- the
filenames are not valid module names.

The fakes below expose no call that can mutate an arch.release bead's state -- only
`observation_exists`/`create_observation` are writable. That is deliberate: it makes "this
workflow cannot advance release lifecycle" a structural property of the activity's dependency
surface, not a claim to be taken on faith.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[3]

from activities import release_status  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load("release_status_under_test", "release-status.py")
rl = _load("release_load_under_test", "release-load.py")


def _charter(ref="R26.01", **overrides):
    base = {
        "ref": ref,
        "name": "Trust earns its amendment",
        "objective": "Safety claims become measurements.",
        "opened_at": "2026-08-25",
        "declared_balance": {"enabling": 100},
        "outcomes": [
            {
                "id": "O-1",
                "statement": "A bad deploy is undone by one command.",
                "work_class": "enabling",
                "requirement_refs": [],
            },
        ],
    }
    base.update(overrides)
    return base


def _write_charter(releases_dir: Path, data: dict) -> None:
    releases_dir.mkdir(parents=True, exist_ok=True)
    (releases_dir / f"{data['ref']}.json").write_text(json.dumps(data))


def _requirements_dir(tmp_path: Path) -> Path:
    requirements_dir = tmp_path / "docs" / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    return requirements_dir


class FakeReader:
    """Read-only: list_beads/list_links only -- exactly scripts/release-status.py's Protocol."""

    def __init__(self, beads=None, links=None):
        self.beads = beads or {}
        self.links = links or {}

    def list_beads(self, namespace, type_, **params):
        key = (namespace, type_)
        results = self.beads.get(key, [])
        content_ref = params.get("content_ref")
        if content_ref is not None:
            results = [b for b in results if (b.get("content") or {}).get("ref") == content_ref]
        return results

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return self.links.get(bead_id, [])


class FakeObservationStore:
    """Write-only surface this report may use: observation_exists/create_observation."""

    def __init__(self):
        self.created: list[dict] = []

    def observation_exists(self, ref: str) -> bool:
        return any((o.get("content") or {}).get("ref") == ref for o in self.created)

    def create_observation(self, payload: dict) -> dict:
        self.created.append(payload)
        return payload


def _fixed_now(when: str = "2026-08-28T00:00:00+00:00"):
    parsed = datetime.fromisoformat(when)
    return lambda: parsed


def _run(reader, store, releases_dir, requirements_dir):
    return release_status.run_release_status_report(
        reader,
        store,
        release_load=rl,
        release_status=rs,
        releases_dir=releases_dir,
        requirements_dir=requirements_dir,
        now_fn=_fixed_now(),
    )


def _empty_reader():
    return FakeReader(
        beads={
            ("dev", "task"): [],
            ("arch", "release"): [],
            ("arch", "requirement_conformance"): [],
        }
    )


# --- writes one observation per release not in state released ------------------------------


def test_writes_one_observation_for_a_release_with_no_mirrored_bead_yet(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    store = FakeObservationStore()

    result = _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))

    assert result["checked"] == 1
    assert result["reported"] == 1
    assert result["skipped_released"] == 0
    assert len(store.created) == 1
    assert store.created[0]["content"]["workload"]["name"] == "R26.01"
    assert store.created[0]["context"]["release_ref"] == "R26.01"


def test_a_release_in_state_released_is_not_reported(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    reader = FakeReader(
        beads={
            ("dev", "task"): [],
            ("arch", "release"): [{"id": "b1", "state": "released", "content": {"ref": "R26.01"}}],
            ("arch", "requirement_conformance"): [],
        }
    )
    store = FakeObservationStore()

    result = _run(reader, store, releases_dir, _requirements_dir(tmp_path))

    assert result["checked"] == 1
    assert result["reported"] == 0
    assert result["skipped_released"] == 1
    assert store.created == []


def test_a_release_in_state_planned_is_reported(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    reader = FakeReader(
        beads={
            ("dev", "task"): [],
            ("arch", "release"): [{"id": "b1", "state": "planned", "content": {"ref": "R26.01"}}],
            ("arch", "requirement_conformance"): [],
        }
    )
    store = FakeObservationStore()

    result = _run(reader, store, releases_dir, _requirements_dir(tmp_path))

    assert result["reported"] == 1
    assert store.created[0]["context"]["release_state"] == "planned"


def test_observation_declares_source_class_derived_on_create(tmp_path):
    """"factory-dispatcher/release-status" is enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822).
    `_emit_once` never updates an existing observation (mint-fresh-or-skip, per-day ref),
    so there is no separate update path to pin here."""
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    store = FakeObservationStore()

    _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))

    assert store.created[0]["content"]["source_class"] == "derived"
    assert store.created[0]["created_by"] == release_status.CREATED_BY


def test_multiple_unreleased_releases_each_get_their_own_observation(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter(ref="R26.01"))
    _write_charter(releases_dir, _charter(ref="R26.02"))
    store = FakeObservationStore()

    result = _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))

    assert result["checked"] == 2
    assert result["reported"] == 2
    refs = {o["context"]["release_ref"] for o in store.created}
    assert refs == {"R26.01", "R26.02"}


# --- read-only w.r.t. release lifecycle ------------------------------------------------------


def test_the_fake_reader_exposes_no_state_mutating_call(tmp_path):
    """Structural guarantee: nothing this activity is given can patch a release bead's state."""
    reader = _empty_reader()
    for forbidden in ("create", "patch", "update", "transition", "set_state"):
        assert not hasattr(reader, forbidden)


def test_the_fake_store_exposes_no_call_beyond_observation_create(tmp_path):
    store = FakeObservationStore()
    for forbidden in ("patch", "update_release", "transition", "set_state"):
        assert not hasattr(store, forbidden)


def test_repeat_nightly_runs_do_not_deduplicate_across_days(tmp_path):
    """Each run's observation ref carries the day, matching staleness_report.py's own
    per-day idiom -- a re-run on a later day is a fresh record, not an update-in-place."""
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    store = FakeObservationStore()

    _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))
    result = release_status.run_release_status_report(
        _empty_reader(),
        store,
        release_load=rl,
        release_status=rs,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        now_fn=_fixed_now("2026-08-29T00:00:00+00:00"),
    )

    assert result["observations_created"] == 1
    assert len(store.created) == 2


def test_a_repeat_run_the_same_day_skips_the_existing_observation(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    store = FakeObservationStore()

    _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))
    result = _run(_empty_reader(), store, releases_dir, _requirements_dir(tmp_path))

    assert result["observations_created"] == 0
    assert result["observations_skipped_existing"] == 1
    assert len(store.created) == 1


# --- worker registration --------------------------------------------------------------------


def test_release_status_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.release_status import ReleaseStatusWorkflow

    definition = temporal_workflow._Definition.from_class(ReleaseStatusWorkflow)
    assert definition.name == "ReleaseStatusWorkflow"
    assert "ReleaseStatusWorkflow" in inspect.getsource(worker.build_worker)


def test_release_status_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_release_status_activity" in names
