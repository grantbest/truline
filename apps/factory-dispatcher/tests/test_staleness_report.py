"""Tests for the nightly requirement verdict staleness report.

No Temporal server, no substrate database, no network: every reader/store below is an
in-process fake, and revision freshness is driven entirely by injected functions rather
than real git history. `release_status`/`release_load` are the *real*
scripts/release-status.py/scripts/release-load.py, loaded by path the same way
apps/factory-dispatcher/tests/test_release_status_activity.py loads them -- the filenames are not
valid module names -- so the cited-criteria resolution under test is the one
release-status.py itself runs, never a second copy of it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[3]

from activities import staleness_report as sr  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load("release_status_under_test_sr", "release-status.py")
rl = _load("release_load_under_test_sr", "release-load.py")


class FakeObservationStore:
    def __init__(self):
        self.created: list[dict] = []

    def observation_exists(self, ref: str) -> bool:
        return any((o.get("content") or {}).get("ref") == ref for o in self.created)

    def create_observation(self, payload: dict) -> dict:
        self.created.append(payload)
        return payload


class FakeReader:
    """Exactly the surface `_release_states` needs: `list_beads`."""

    def __init__(self, beads=None):
        self.beads = beads or {}

    def list_beads(self, namespace, type_, **params):
        return self.beads.get((namespace, type_), [])


def _registry(tmp_path: Path) -> Path:
    """Two revision-measured criteria, both made stale by the injected
    `revision_exists_fn` below -- PC-AAA-001/AC-1 and PC-BBB-002/AC-1."""
    requirements_dir = tmp_path / "docs" / "requirements"
    requirements_dir.mkdir(parents=True, exist_ok=True)
    (requirements_dir / "test-requirements.json").write_text(
        json.dumps(
            {
                "registry": {"id": "REG-TEST"},
                "requirements": [
                    {
                        "id": "PC-AAA-001",
                        "acceptance_criteria": [
                            {"id": "AC-1", "conformance": "pass", "measured_revision": "rev1"}
                        ],
                    },
                    {
                        "id": "PC-BBB-002",
                        "acceptance_criteria": [
                            {"id": "AC-1", "conformance": "pass", "measured_revision": "rev2"}
                        ],
                    },
                ],
            }
        )
    )
    return requirements_dir


def _write_charter(releases_dir: Path, ref: str, requirement_refs: list[str]) -> None:
    releases_dir.mkdir(parents=True, exist_ok=True)
    (releases_dir / f"{ref}.json").write_text(
        json.dumps(
            {
                "ref": ref,
                "name": "Test release",
                "opened_at": "2026-08-01",
                "declared_balance": {"enabling": 100},
                "outcomes": [
                    {
                        "id": "O-1",
                        "statement": "A test outcome.",
                        "work_class": "enabling",
                        "requirement_refs": requirement_refs,
                    }
                ],
            }
        )
    )


def _fixed_now(when: str = "2026-08-28T00:00:00+00:00"):
    parsed = datetime.fromisoformat(when)
    return lambda: parsed


# every criterion in `_registry` is stale under this -- the revision rule fails first,
# before `last_changed_revision_fn`/`is_ancestor_fn` are even consulted for the verdict.
_ALWAYS_STALE = dict(
    last_changed_revision_fn=lambda paths: "abc123",
    revision_exists_fn=lambda rev: False,
    is_ancestor_fn=lambda a, b: True,
)


def _run(store, registries_dir, releases_dir, reader=None, **overrides):
    kwargs = {**_ALWAYS_STALE, **overrides}
    return sr.run_staleness_report(
        store,
        registries_dir=registries_dir,
        releases_dir=releases_dir,
        reader=reader,
        release_load=rl,
        release_status=rs,
        now_fn=_fixed_now(),
        **kwargs,
    )


def test_a_criterion_cited_by_an_open_release_is_distinguished_from_one_cited_by_none(tmp_path):
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, "R-TEST", ["PC-AAA-001/AC-1"])
    store = FakeObservationStore()

    result = _run(store, registries_dir, releases_dir, reader=FakeReader())

    assert result["checked"] == 2
    assert result["stale"] == 2
    # Only PC-AAA-001/AC-1 is cited by R-TEST; PC-BBB-002/AC-1 is stale too but
    # nothing open cites it, so the burndown target is 1, not 2.
    assert result["cited_stale"] == 1

    summary = next(
        o
        for o in store.created
        if o["context"]["observation_kind"] == sr.OBSERVATION_KIND_SUMMARY
    )
    assert summary["context"]["total_stale"] == 2
    assert summary["context"]["cited_stale"] == 1


def test_no_open_release_reports_unknown_rather_than_zero(tmp_path):
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, "R-TEST", ["PC-AAA-001/AC-1"])
    store = FakeObservationStore()
    # The only charter's mirrored release bead is already `released` -- nothing is open.
    reader = FakeReader(
        beads={("arch", "release"): [{"id": "b1", "state": "released", "content": {"ref": "R-TEST"}}]}
    )

    result = _run(store, registries_dir, releases_dir, reader=reader)

    assert result["stale"] == 2
    assert result["cited_stale"] is None

    summary = next(
        o
        for o in store.created
        if o["context"]["observation_kind"] == sr.OBSERVATION_KIND_SUMMARY
    )
    assert summary["context"]["cited_stale"] is None


def test_no_charters_at_all_reports_unknown_rather_than_zero(tmp_path):
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"  # never written to
    store = FakeObservationStore()

    result = _run(store, registries_dir, releases_dir, reader=FakeReader())

    assert result["stale"] == 2
    assert result["cited_stale"] is None


def test_an_open_charter_citing_no_criteria_reports_unknown_rather_than_zero(tmp_path):
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, "R-TEST", [])
    store = FakeObservationStore()

    result = _run(store, registries_dir, releases_dir, reader=FakeReader())

    assert result["stale"] == 2
    assert result["cited_stale"] is None


def test_per_verdict_observation_shape_is_unchanged(tmp_path):
    """The distinguishing information lives only in the summary; a per-verdict
    stale observation carries none of it, and its idempotence key is untouched."""
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, "R-TEST", ["PC-AAA-001/AC-1"])
    store = FakeObservationStore()

    _run(store, registries_dir, releases_dir, reader=FakeReader())

    per_verdict = [
        o
        for o in store.created
        if o["context"]["observation_kind"] == sr.OBSERVATION_KIND_STALE
    ]
    assert len(per_verdict) == 2
    for observation in per_verdict:
        assert set(observation["context"]) == {
            "observation_kind",
            "registry_id",
            "registry_path",
            "requirement_id",
            "criterion_id",
            "requirement_ref",
            "conformance",
            "measured_revision",
            "invalidating_revision",
            "reason",
        }


def test_repeat_same_day_run_still_skips_existing_observations(tmp_path):
    registries_dir = _registry(tmp_path)
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, "R-TEST", ["PC-AAA-001/AC-1"])
    store = FakeObservationStore()

    _run(store, registries_dir, releases_dir, reader=FakeReader())
    result = _run(store, registries_dir, releases_dir, reader=FakeReader())

    assert result["observations_created"] == 0
    assert result["observations_skipped_existing"] == 3  # 2 per-verdict + 1 summary


def test_staleness_report_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
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
    monkeypatch.setattr(sr.httpx, "request", fake_request)

    store = sr.SubstrateObservationStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": sr.CREATED_BY, "Content-Type": "text/plain"},
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == sr.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"
