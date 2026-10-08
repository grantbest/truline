"""Nightly staleness report writes observations, not work."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import staleness_report  # noqa: E402


def registry(tmp_path: Path, requirements: list[dict], name: str = "r.json") -> Path:
    tmp_path.joinpath(name).write_text(
        json.dumps({"registry": {"id": "REG-TEST"}, "requirements": requirements})
    )
    return tmp_path


def requirement(
    rid: str,
    *,
    measured_revision: str = "measured",
    conformance: str = "fail",
) -> dict:
    return {
        "id": rid,
        "title": "requirement",
        "implementation": ["apps/example/service.py"],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "conformance": conformance,
                "measured_revision": measured_revision,
            }
        ],
    }


class FakeObservationStore:
    def __init__(self):
        self.created: list[dict] = []
        self.refs: set[str] = set()

    def observation_exists(self, ref: str) -> bool:
        return ref in self.refs

    def create_observation(self, payload: dict) -> dict:
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["state"] == "active"
        assert payload["created_by"] == staleness_report.CREATED_BY
        ref = payload["content"]["ref"]
        self.refs.add(ref)
        self.created.append(payload)
        return {"id": f"obs-{len(self.created)}", **payload}

    def list_tasks(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("staleness report must not read dev.task beads")

    def create_task(  # pragma: no cover - must not be called
        self, content, created_by, *, trust_tier="user"
    ):
        raise AssertionError("staleness report must not create dev.task beads")


def git_lookup(order: dict[str, int], last_touching: str):
    return {
        "last_changed_revision_fn": lambda _paths: last_touching,
        "revision_exists_fn": lambda rev: rev in order,
        "is_ancestor_fn": lambda ancestor, descendant: order[ancestor]
        <= order[descendant],
    }


def fixed_now() -> datetime:
    return datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def observations_by_kind(store: FakeObservationStore) -> dict[str, list[dict]]:
    by_kind: dict[str, list[dict]] = {}
    for payload in store.created:
        by_kind.setdefault(payload["context"]["observation_kind"], []).append(payload)
    return by_kind


def test_stale_verdict_produces_one_observation_identifying_the_revision_fact(tmp_path):
    store = FakeObservationStore()

    result = staleness_report.run_staleness_report(
        store,
        registries_dir=registry(
            tmp_path,
            [requirement("PC-X-001", measured_revision="oldrev")],
        ),
        now_fn=fixed_now,
        **git_lookup({"oldrev": 1, "touching": 2}, "touching"),
    )

    by_kind = observations_by_kind(store)
    stale = by_kind[staleness_report.OBSERVATION_KIND_STALE][0]
    assert result["checked"] == 1
    assert result["stale"] == 1
    assert stale["context"]["requirement_id"] == "PC-X-001"
    assert stale["context"]["criterion_id"] == "AC-1"
    assert stale["context"]["measured_revision"] == "oldrev"
    assert stale["context"]["invalidating_revision"] == "touching"
    assert stale["content"]["workload"]["name"] == "PC-X-001/AC-1"
    assert "oldrev" in stale["content"]["ref"]
    assert "touching" in stale["content"]["ref"]


def test_fresh_verdict_does_not_produce_a_stale_observation(tmp_path):
    store = FakeObservationStore()

    result = staleness_report.run_staleness_report(
        store,
        registries_dir=registry(tmp_path, [requirement("PC-X-001")]),
        now_fn=fixed_now,
        **git_lookup({"touching": 1, "measured": 2}, "touching"),
    )

    by_kind = observations_by_kind(store)
    assert result["checked"] == 1
    assert result["stale"] == 0
    assert staleness_report.OBSERVATION_KIND_STALE not in by_kind


def test_summary_observation_is_always_emitted_even_when_nothing_is_stale(tmp_path):
    store = FakeObservationStore()

    staleness_report.run_staleness_report(
        store,
        registries_dir=registry(tmp_path, []),
        now_fn=fixed_now,
        **git_lookup({}, "touching"),
    )

    by_kind = observations_by_kind(store)
    summary = by_kind[staleness_report.OBSERVATION_KIND_SUMMARY][0]
    assert summary["context"]["total_checked"] == 0
    assert summary["context"]["total_stale"] == 0
    assert summary["content"]["workload"]["name"] == "checked-0-stale-0"


def test_report_never_creates_or_modifies_dev_task_beads(tmp_path):
    store = FakeObservationStore()

    staleness_report.run_staleness_report(
        store,
        registries_dir=registry(tmp_path, [requirement("LO-CAT-001")], "lifeops.json"),
        now_fn=fixed_now,
        **git_lookup({"measured": 1, "touching": 2}, "touching"),
    )

    assert store.created
    assert {payload["namespace"] for payload in store.created} == {"arch"}
    assert {payload["type"] for payload in store.created} == {"observation"}
    assert all(
        payload["context"]["observation_kind"]
        in {
            staleness_report.OBSERVATION_KIND_STALE,
            staleness_report.OBSERVATION_KIND_SUMMARY,
        }
        for payload in store.created
    )


def test_stale_and_summary_observations_declare_source_class_derived_on_create(tmp_path):
    """"factory-dispatcher/staleness-report" is enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822).
    Both payload builders are create-only (`_emit_once` never updates -- mint-fresh-or-
    skip), so there is no separate update path to pin here."""
    store = FakeObservationStore()

    staleness_report.run_staleness_report(
        store,
        registries_dir=registry(
            tmp_path,
            [requirement("PC-X-001", measured_revision="oldrev")],
        ),
        now_fn=fixed_now,
        **git_lookup({"oldrev": 1, "touching": 2}, "touching"),
    )

    by_kind = observations_by_kind(store)
    stale = by_kind[staleness_report.OBSERVATION_KIND_STALE][0]
    summary = by_kind[staleness_report.OBSERVATION_KIND_SUMMARY][0]
    assert stale["content"]["source_class"] == "derived"
    assert stale["created_by"] == staleness_report.CREATED_BY
    assert summary["content"]["source_class"] == "derived"
    assert summary["created_by"] == staleness_report.CREATED_BY


def test_rerunning_same_day_does_not_duplicate_unchanged_observations(tmp_path):
    store = FakeObservationStore()
    registries = registry(tmp_path, [requirement("PC-X-001")])
    lookup = git_lookup({"measured": 1, "touching": 2}, "touching")

    first = staleness_report.run_staleness_report(
        store,
        registries_dir=registries,
        now_fn=fixed_now,
        **lookup,
    )
    second = staleness_report.run_staleness_report(
        store,
        registries_dir=registries,
        now_fn=fixed_now,
        **lookup,
    )

    assert first["observations_created"] == 2
    assert second["observations_created"] == 0
    assert second["observations_skipped_existing"] == 2
    assert len(store.created) == 2
