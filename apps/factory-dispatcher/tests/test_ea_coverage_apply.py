"""Tests for the nightly EA coverage report (S53-4, PC-ASR-002).

FakeEACoverageStore mirrors what the live substrate would accept -- same discipline as
test_doctrine_staleness.py's FakePrincipleObservationStore and
test_ea_observation.py's FakeEAObserverStore: it validates ``arch.observation`` content
through the real ``validate_bead_content`` and checks writer enrolment through the real
``check_source_class_admission`` / ``check_source_class_ownership``, imported directly
from ``apps/substrate/src/schemas.py`` (the ``beadstore.py``-style bare-module import),
never hand-copied. ``report_ea_coverage`` is exercised against the real
``scripts/ea-coverage.py`` (loaded by ``eca.load_ea_coverage_module``, not a mock) -- only
the substrate boundary is faked.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
_SUBSTRATE_SRC = str(_REPO_ROOT / "apps" / "substrate" / "src")
if _SUBSTRATE_SRC not in sys.path:
    sys.path.insert(0, _SUBSTRATE_SRC)

from activities import ea_coverage_apply as eca  # noqa: E402
from activities.ea_observation import OBSERVER_STATUS_REF  # noqa: E402
from schemas import (  # noqa: E402
    check_source_class_admission,
    check_source_class_ownership,
    validate_bead_content,
)


def _now(iso: str):
    def _fn():
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    return _fn


class FakeEACoverageStore:
    def __init__(self, *, observer_status: dict | None = None):
        self.observations: dict[str, dict] = {}
        self._next_id = 1
        if observer_status is not None:
            self.observations[OBSERVER_STATUS_REF] = {
                "id": "obs-status",
                "content": observer_status.get("content", {}),
                "context": observer_status.get("context", {}),
            }

    def find_observation(self, ref):
        return self.observations.get(ref)

    def create_observation(self, payload):
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["created_by"] == eca.CREATED_BY
        # Rejects malformed content exactly as POST /beads does: the real
        # pydantic model (via validate_bead_content), not a hand-rolled
        # check that can drift from it -- the #1005 gate proved a fake that
        # accepts any content masks a 422 the live store would raise.
        validate_bead_content("arch", "observation", payload["content"])
        declared_class = payload["content"].get("source_class", "authored")
        violation = check_source_class_admission(declared_class, payload["created_by"])
        if violation is not None:
            raise AssertionError(
                f"source_class_admission_violation: {payload['created_by']!r} is not "
                f"enrolled for declared_class {violation.owning_class!r} "
                "(apps/substrate/src/bead_rules.py:SOURCE_CLASS_WRITERS)"
            )
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

    def update_observation(self, bead_id, *, content=None, context=None):
        for bead in self.observations.values():
            if bead["id"] == bead_id:
                if content is not None:
                    validate_bead_content("arch", "observation", content)
                    existing_class = bead["content"].get("source_class", "authored")
                    violation = check_source_class_ownership(
                        existing_class, bead["created_by"], eca.CREATED_BY
                    )
                    if violation is not None:
                        raise AssertionError(
                            f"source_class_ownership_violation: {eca.CREATED_BY!r} may not "
                            f"overwrite a {violation.owning_class!r} fact owned by "
                            f"{bead['created_by']!r}"
                        )
                    bead["content"] = content
                if context is not None:
                    bead["context"] = context
                return dict(bead)
        raise AssertionError(f"update_observation called for unknown bead_id {bead_id!r}")


def _ok_status(*, attributed=4, unattributed=2, total_apps=32, known=13, assessed_none=6, unknown=13):
    return {
        "content": {"observed_at": "2026-09-23T00:00:00Z"},
        "context": {
            "status": "ok",
            "ci_counts": {"attributed": attributed, "unattributed": unattributed},
            "dependency_posture": {
                "total_applications": total_apps,
                "known": known,
                "assessed_none": assessed_none,
                "unknown": unknown,
            },
        },
    }


# ---------------------------------------------------------------------------
# report_ea_coverage: end-to-end against the real ea-coverage.py functions
# ---------------------------------------------------------------------------


def test_report_ea_coverage_lands_one_bead_per_row():
    store = FakeEACoverageStore(observer_status=_ok_status())

    result = eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))

    assert result["status"] == "reported"
    assert result["created"] == result["checked"]
    assert result["updated"] == 0
    assert result["unchanged"] == 0

    row_refs = {ref for ref in store.observations if ref != OBSERVER_STATUS_REF}
    assert row_refs == {
        "obs.ea-coverage.application-depends-on-authored.2026-09-23",
        "obs.ea-coverage.application-loc.2026-09-23",
        "obs.ea-coverage.application-realizes.2026-09-23",
        "obs.ea-coverage.capability-supports.2026-09-23",
        "obs.ea-coverage.technology-layer.2026-09-23",
    }
    for ref in row_refs:
        bead = store.observations[ref]
        assert bead["created_by"] == eca.CREATED_BY
        assert bead["context"]["observation_kind"] == "ea_coverage_measurement"


def test_report_ea_coverage_lands_the_technology_row_as_not_evaluated_without_a_status_bead():
    store = FakeEACoverageStore()  # no obs.ea-observer-status bead at all

    eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))

    tech_bead = store.observations["obs.ea-coverage.technology-layer.2026-09-23"]
    row = tech_bead["context"]["row"]
    assert row["evaluated"] is False
    assert "no live observer-status" in row["reason"]


def test_report_ea_coverage_reflects_the_technology_row_counts_when_the_observer_ran_cleanly():
    store = FakeEACoverageStore(observer_status=_ok_status(attributed=5, unattributed=1))

    eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))

    tech_bead = store.observations["obs.ea-coverage.technology-layer.2026-09-23"]
    row = tech_bead["context"]["row"]
    assert row["evaluated"] is True
    assert row["arch_ci"] == {"total": 6, "attributed": 5, "unattributed": 1}


# ---------------------------------------------------------------------------
# NFR-2: a trend, not a reading -- two runs, two dates, two beads per row;
# same date, same bead.
# ---------------------------------------------------------------------------


def test_two_runs_on_different_dates_each_mint_a_new_bead_per_row():
    store = FakeEACoverageStore(observer_status=_ok_status())

    eca.report_ea_coverage(store, now_fn=_now("2026-09-22T02:00:00+00:00"))
    eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))

    tech_refs = sorted(
        ref for ref in store.observations
        if ref.startswith("obs.ea-coverage.technology-layer.")
    )
    assert tech_refs == [
        "obs.ea-coverage.technology-layer.2026-09-22",
        "obs.ea-coverage.technology-layer.2026-09-23",
    ]


def test_two_runs_on_the_same_date_reconcile_the_same_bead():
    store = FakeEACoverageStore(observer_status=_ok_status())

    first = eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))
    second = eca.report_ea_coverage(store, now_fn=_now("2026-09-23T05:00:00+00:00"))

    tech_refs = [
        ref for ref in store.observations if ref.startswith("obs.ea-coverage.technology-layer.")
    ]
    assert tech_refs == ["obs.ea-coverage.technology-layer.2026-09-23"]
    assert first["created"] == first["checked"]
    assert second["created"] == 0
    assert second["unchanged"] == second["checked"]


def test_a_same_day_retry_with_a_changed_row_updates_rather_than_duplicates():
    store = FakeEACoverageStore(observer_status=_ok_status(attributed=1, unattributed=1))
    eca.report_ea_coverage(store, now_fn=_now("2026-09-23T02:00:00+00:00"))

    # The observer re-ran mid-day and the standing status bead moved.
    store.observations[OBSERVER_STATUS_REF]["context"] = _ok_status(attributed=9, unattributed=0)["context"]
    result = eca.report_ea_coverage(store, now_fn=_now("2026-09-23T06:00:00+00:00"))

    tech_bead = store.observations["obs.ea-coverage.technology-layer.2026-09-23"]
    assert tech_bead["context"]["row"]["arch_ci"]["total"] == 9
    assert result["updated"] >= 1


# ---------------------------------------------------------------------------
# the live worker registers this activity
# ---------------------------------------------------------------------------


def test_report_ea_coverage_activity_is_registered():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_ea_coverage_activity" in names


def test_no_kubectl_or_subprocess_import():
    """NFR-3: this reconciler is a substrate-only reader -- no new credential
    surface beyond what the doctrine-staleness worker already holds."""
    import inspect

    source = inspect.getsource(eca)
    assert "subprocess" not in source
    assert "cluster_health" not in source
