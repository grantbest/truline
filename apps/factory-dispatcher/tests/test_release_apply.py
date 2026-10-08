"""Tests for the in-cluster release-charter applier.

No Temporal server, no substrate database, no cluster, no network: reconcile writes go through a
FakeSubstrate that mirrors exactly the three calls release-load.py's own `Substrate` class makes
(list_beads/create/patch). The reconcile logic itself is the *real* scripts/release-load.py,
loaded the same way scripts/tests/test_release_load.py and activities/ea_apply.py's own tests do
-- the filename is not a valid module name, so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))

from activities import release_apply  # noqa: E402
from worker_revision import CheckoutFreshness  # noqa: E402

TRACKING_REF = "origin/main"
MAIN_REVISION = "ccccccc"
OLD_REVISION = "aaaaaaa"


def _load_release_load():
    spec = importlib.util.spec_from_file_location(
        "release_load_under_test", _REPO_ROOT / "scripts" / "release-load.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rl = _load_release_load()


class FakeSubstrate:
    """Exactly release-load.py's Substrate surface: list_beads/create/patch."""

    def __init__(self, beads: list[dict] | None = None):
        self.beads = list(beads or [])
        self.write_calls: list[str] = []
        self._next_id = len(self.beads) + 1

    def list_beads(self, bead_type: str, limit: int = 1000) -> list[dict]:
        return [dict(b) for b in self.beads if b["type"] == bead_type]

    def create(self, bead_type: str, state: str, content: dict) -> dict:
        bead = {
            "id": f"bead-{self._next_id}",
            "type": bead_type,
            "state": state,
            "content": dict(content),
        }
        self._next_id += 1
        self.beads.append(bead)
        self.write_calls.append(f"create:{content.get('ref')}")
        return dict(bead)

    def patch(self, bead_id: str, body: dict) -> dict:
        for bead in self.beads:
            if bead["id"] == bead_id:
                bead.update(body)
                self.write_calls.append(f"patch:{bead_id}")
                return dict(bead)
        raise AssertionError(f"unknown bead id {bead_id}")


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


# --- hand-constructed CheckoutFreshness values -- no git, no network, same shape
# --- tests/test_worker_checkout_drift_response.py's checkout_current/checkout_stale use ---


def checkout_current() -> CheckoutFreshness:
    """HEAD is confirmed equal to the tracking ref."""
    return CheckoutFreshness(
        revision=MAIN_REVISION,
        main_ref=TRACKING_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=0,
    )


def checkout_stale(*, commits_behind: int = 1) -> CheckoutFreshness:
    """HEAD is a valid but outdated ancestor of the tracking ref."""
    return CheckoutFreshness(
        revision=OLD_REVISION,
        main_ref=TRACKING_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=commits_behind,
    )


def checkout_diverged() -> CheckoutFreshness:
    """HEAD is ahead of or diverged from the tracking ref."""
    return CheckoutFreshness(
        revision=OLD_REVISION,
        main_ref=TRACKING_REF,
        main_revision=MAIN_REVISION,
        is_ancestor=False,
    )


def checkout_unconfirmable() -> CheckoutFreshness:
    """The comparison itself could not be made."""
    return CheckoutFreshness(
        revision="",
        main_ref=TRACKING_REF,
        main_revision=None,
        is_ancestor=None,
        error="no git binary found",
    )


# --- PRIN-014: the same revision applied twice is zero writes ------------------------------


def test_a_new_charter_creates_a_release_bead(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    sub = FakeSubstrate()

    result = release_apply.apply_release_charters(
        sub,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        release_load=rl,
    )

    assert result["status"] == "applied"
    assert result["creates"] == ["R26.01"]
    assert sub.write_calls == ["create:R26.01"]


def test_the_same_charter_applied_twice_is_zero_writes_the_second_time(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    sub = FakeSubstrate()

    first = release_apply.apply_release_charters(
        sub,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        release_load=rl,
    )
    assert first["status"] == "applied"
    assert sub.write_calls, "the first apply must have written"

    sub.write_calls = []
    second = release_apply.apply_release_charters(
        sub,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        release_load=rl,
    )

    assert second["status"] == "applied"
    assert second["creates"] == []
    assert second["updates"] == []
    assert second["unchanged"] == ["R26.01"]
    assert sub.write_calls == []


def test_a_changed_charter_updates_only_the_changed_release(tmp_path):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    sub = FakeSubstrate()
    release_apply.apply_release_charters(
        sub,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        release_load=rl,
    )

    sub.write_calls = []
    _write_charter(releases_dir, _charter(name="Trust earns its amendment, revised"))
    result = release_apply.apply_release_charters(
        sub,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        release_load=rl,
    )

    assert result["updates"] == ["R26.01"]
    assert sub.write_calls == ["patch:bead-1"]


def test_a_reconcile_error_raises_so_the_workflow_run_shows_failed(tmp_path, monkeypatch):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())

    class RejectingSubstrate(FakeSubstrate):
        def create(self, bead_type, state, content):
            raise rl.SubstrateError(422, "rejected", "/beads")

    with pytest.raises(release_apply.ReleaseApplyError):
        release_apply.apply_release_charters(
            RejectingSubstrate(),
            releases_dir=releases_dir,
            requirements_dir=_requirements_dir(tmp_path),
            release_load=rl,
        )


# --- worker registration --------------------------------------------------------------------


def test_release_apply_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.release_apply import ReleaseApplyWorkflow

    # A class temporalio can't validate as a workflow raises here.
    definition = temporal_workflow._Definition.from_class(ReleaseApplyWorkflow)
    assert definition.name == "ReleaseApplyWorkflow"
    # build_worker constructs the workflows list inline; confirm the class is
    # actually passed to Worker(...) by reading the registration source,
    # rather than duplicating Temporal SDK internals here.
    assert "ReleaseApplyWorkflow" in inspect.getsource(worker.build_worker)


def test_release_apply_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "apply_release_charters_activity" in names


# --- dev.finding 5170b3f9: refuse to reconcile from a checkout not confirmed current --------


def test_tracking_ref_is_origin_qualified_default_base_ref():
    import dispatch

    assert release_apply.TRACKING_REF == f"origin/{dispatch.DEFAULT_BASE_REF}"


@pytest.mark.parametrize(
    "freshness, expect_none, expect_fragment",
    [
        (checkout_current(), True, None),
        (checkout_stale(commits_behind=4), False, "behind by 4"),
        (checkout_diverged(), False, "ahead of or diverged from"),
        (checkout_unconfirmable(), False, "no git binary found"),
    ],
)
def test_charter_tree_refusal(freshness, expect_none, expect_fragment):
    reason = release_apply.charter_tree_refusal(freshness)
    if expect_none:
        assert reason is None
    else:
        assert reason is not None
        assert freshness.revision in reason or freshness.revision == ""
        assert freshness.main_ref in reason
        assert expect_fragment in reason


def _patch_charter_seam(monkeypatch, *, releases_dir: Path, requirements_dir: Path, substrate_factory):
    monkeypatch.setattr(rl, "RELEASES_DIR", releases_dir)
    monkeypatch.setattr(rl, "REQUIREMENTS_DIR", requirements_dir)
    monkeypatch.setattr(rl, "Substrate", substrate_factory)
    monkeypatch.setattr(release_apply, "_load_release_load_module", lambda: rl)


def test_skip_before_loading_anything_when_the_checkout_is_behind(tmp_path, monkeypatch):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())

    class RaisingSubstrate:
        def __init__(self):
            raise AssertionError("Substrate must not be constructed when the checkout is stale")

    load_calls: list[bool] = []
    apply_calls: list[tuple] = []

    def _fake_load_release_load_module():
        load_calls.append(True)
        return rl

    monkeypatch.setattr(rl, "RELEASES_DIR", releases_dir)
    monkeypatch.setattr(rl, "REQUIREMENTS_DIR", _requirements_dir(tmp_path))
    monkeypatch.setattr(rl, "Substrate", RaisingSubstrate)
    monkeypatch.setattr(release_apply, "_load_release_load_module", _fake_load_release_load_module)
    monkeypatch.setattr(
        release_apply, "apply_release_charters", lambda *a, **k: apply_calls.append((a, k))
    )
    monkeypatch.setattr(
        release_apply.worker_revision,
        "check_checkout_freshness",
        lambda **kwargs: checkout_stale(commits_behind=1),
    )

    result = release_apply.apply_release_charters_activity()

    assert result["status"] == "skipped"
    assert "behind by 1" in result["reason"]
    assert result["revision"] == OLD_REVISION
    assert result["tracking_ref"] == release_apply.TRACKING_REF
    assert result["creates"] == []
    assert result["updates"] == []
    assert result["unchanged"] == []
    assert result["errors"] == []
    assert load_calls == [], "_load_release_load_module must not be called on a skip"
    assert apply_calls == [], "apply_release_charters must not be called on a skip"


def test_applies_exactly_once_when_the_checkout_is_current(tmp_path, monkeypatch):
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter())
    fake = FakeSubstrate()

    _patch_charter_seam(
        monkeypatch,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        substrate_factory=lambda: fake,
    )
    monkeypatch.setattr(
        release_apply.worker_revision, "check_checkout_freshness", lambda **kwargs: checkout_current()
    )

    apply_calls: list = []
    real_apply = release_apply.apply_release_charters

    def _recording_apply(sub, **kwargs):
        apply_calls.append(sub)
        return real_apply(sub, **kwargs)

    monkeypatch.setattr(release_apply, "apply_release_charters", _recording_apply)

    result = release_apply.apply_release_charters_activity()

    assert apply_calls == [fake]
    assert result["status"] == "applied"


# --- AC-3: the 19:30Z revert, reproduced and refused -----------------------------------------


def test_a_stale_checkout_does_not_revert_the_store_to_its_own_old_charter(tmp_path, monkeypatch):
    """dev.finding 5170b3f9: a worker checkout behind main must not patch the store's current
    charter back to the stale content the checkout itself carries on disk."""
    releases_dir = tmp_path / "docs" / "releases"
    _write_charter(releases_dir, _charter(ref="R26.13", opened_at="2026-11-16"))

    store_content = _charter(ref="R26.13", opened_at="2026-11-02")
    store_content["source_class"] = "authored"
    fake = FakeSubstrate(
        beads=[
            {
                "id": "bead-1",
                "type": rl.RELEASE_TYPE,
                "state": rl.ENTRY_STATE,
                "content": store_content,
            }
        ]
    )

    _patch_charter_seam(
        monkeypatch,
        releases_dir=releases_dir,
        requirements_dir=_requirements_dir(tmp_path),
        substrate_factory=lambda: fake,
    )

    monkeypatch.setattr(
        release_apply.worker_revision,
        "check_checkout_freshness",
        lambda **kwargs: checkout_stale(commits_behind=1),
    )
    skipped = release_apply.apply_release_charters_activity()
    assert skipped["status"] == "skipped"
    assert fake.write_calls == []

    monkeypatch.setattr(
        release_apply.worker_revision, "check_checkout_freshness", lambda **kwargs: checkout_current()
    )
    applied = release_apply.apply_release_charters_activity()
    assert applied["status"] == "applied"
    assert fake.write_calls == ["patch:bead-1"]
