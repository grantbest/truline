"""Tests for the in-cluster EA-model applier (PC-ASR-007/AC-3; PC-ASR-001/AC-2).

No Temporal server, no substrate database, no cluster, no network: the reconcile writes go
through a FakeSubstrate that mirrors exactly the six calls ea-load.py's own `Substrate` class
makes, validated against the real schemas (apps/substrate/src/schemas.py's
ArchCapabilityContent/ArchApplicationContent/ArchObservationContent/BEAD_LINK_TYPES, imported
directly -- not hand-copied, the FakeSubstrate.add_note lesson and B-110). The reconcile logic
itself is the *real* scripts/ea-load.py, loaded the same way its own test suite
(scripts/tests/test_ea_load.py) and ea-conformance.py do -- the filename is not a valid module
name, so it is loaded by path rather than imported. Git operations run against a throwaway repo
under tmp_path; nothing here touches the real repo, a substrate, or the network.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "substrate"))

import dispatch  # noqa: E402
from activities import ea_apply  # noqa: E402
from src.schemas import (  # noqa: E402
    ArchApplicationContent,
    ArchCapabilityContent,
    ArchObservationContent,
    BEAD_LINK_TYPES,
)


def _load_ea_load():
    spec = importlib.util.spec_from_file_location(
        "ea_load_under_test", _REPO_ROOT / "scripts" / "ea-load.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ea_derive():
    spec = importlib.util.spec_from_file_location(
        "ea_derive_under_test", _REPO_ROOT / "scripts" / "ea-derive.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eal = _load_ea_load()
ead = _load_ea_derive()


# --- FakeSubstrate: exactly ea-load.py's Substrate surface, real-schema validated -------------

_CONTENT_SCHEMAS = {
    "capability": ArchCapabilityContent,
    "application": ArchApplicationContent,
    "observation": ArchObservationContent,
}


class SubstrateError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"substrate {status}: {body}")
        self.status = status


class FakeSubstrate:
    def __init__(self, beads: list[dict[str, Any]] | None = None):
        self._beads: dict[str, dict[str, Any]] = {b["id"]: dict(b) for b in (beads or [])}
        self._links: dict[str, dict[str, Any]] = {}
        self._n_bead = len(self._beads)
        self._n_link = 0
        self.write_calls: list[str] = []

    def list_beads(
        self, bead_type: str, limit: int = 1000, offset: int = 0
    ) -> list[dict[str, Any]]:
        """One page, like the real store: `limit` bounds what a single call returns,
        `offset` walks further in -- ea-load.py's `_list_all` pages through both
        (dev.finding 2bf3c19b), the same contract `scripts/tests/test_ea_load.py`'s
        FakeSubstrate and the real `scripts/substrate_client.py.Substrate` hold."""
        matches = [dict(b) for b in self._beads.values() if b["type"] == bead_type]
        return matches[offset:offset + limit]

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict[str, Any] | None:
        for bead in self._beads.values():
            if bead["type"] == type and (bead.get("content") or {}).get("ref") == content_ref:
                return dict(bead)
        return None

    def create(self, bead_type, state, content, parent_id, *, created_by: str = ""):
        _CONTENT_SCHEMAS[bead_type](**content)
        self._n_bead += 1
        bead_id = f"bead-{self._n_bead}"
        bead = {
            "id": bead_id, "type": bead_type, "state": state,
            "content": dict(content), "context": {}, "parent_id": parent_id,
            "created_by": created_by,
        }
        self._beads[bead_id] = bead
        self.write_calls.append(f"create:{bead_type}")
        return dict(bead)

    def patch(self, bead_id, body, *, created_by: str = ""):
        bead = self._beads[bead_id]
        if "content" in body:
            _CONTENT_SCHEMAS[bead["type"]](**body["content"])
            bead["content"] = dict(body["content"])
        if "context" in body:
            bead["context"] = dict(body["context"])
        if "parent_id" in body:
            bead["parent_id"] = body["parent_id"]
        if "state" in body:
            bead["state"] = body["state"]
        if created_by:
            bead["created_by"] = created_by
        self.write_calls.append(f"patch:{bead_id}")
        return dict(bead)

    def links(self, bead_id, direction: str = "outgoing"):
        if direction != "outgoing":
            return []
        return [dict(link) for link in self._links.values() if link["source_id"] == bead_id]

    def add_link(self, source_id, target_id, link_type):
        normalized = link_type.strip().lower()
        if normalized not in BEAD_LINK_TYPES:
            raise SubstrateError(422, f"link_type must be one of: {sorted(BEAD_LINK_TYPES)}")
        self._n_link += 1
        link_id = f"link-{self._n_link}"
        link = {
            "id": link_id, "source_id": source_id, "target_id": target_id,
            "link_type": normalized,
        }
        self._links[link_id] = link
        self.write_calls.append(f"add_link:{normalized}")
        return dict(link)

    def delete_link(self, link_id) -> None:
        self._links.pop(link_id, None)
        self.write_calls.append(f"delete_link:{link_id}")

    def delete_bead(self, bead_id) -> None:
        self._beads.pop(bead_id, None)
        self.write_calls.append(f"delete_bead:{bead_id}")


# --- FakeDependencySubstrate: what ea-derive.py's dependency reconcile needs -------------------
#
# Distinct from FakeSubstrate above: PC-ASR-007/AC-2's dependency writer tags a writer
# identity per link (`created_by`), which ea-load.py's Substrate/FakeSubstrate surface has
# no parameter for. Real-schema validated the same way (BEAD_LINK_TYPES, imported above).


class FakeDependencySubstrate:
    def __init__(self, beads: list[dict[str, Any]] | None = None, links: list[dict[str, Any]] | None = None):
        self._beads: dict[str, dict[str, Any]] = {b["id"]: dict(b) for b in (beads or [])}
        self._links: dict[str, dict[str, Any]] = {link["id"]: dict(link) for link in (links or [])}
        self._n_link = len(self._links)
        self.write_calls: list[str] = []

    def list_beads(self, bead_type, limit=1000):
        return [dict(b) for b in self._beads.values() if b["type"] == bead_type]

    def links(self, bead_id, direction="outgoing"):
        if direction != "outgoing":
            return []
        return [dict(link) for link in self._links.values() if link["source_id"] == bead_id]

    def add_link(self, source_id, target_id, link_type, *, created_by):
        normalized = link_type.strip().lower()
        if normalized not in BEAD_LINK_TYPES:
            raise ValueError(f"link_type must be one of: {sorted(BEAD_LINK_TYPES)}")
        self._n_link += 1
        link_id = f"dep-link-{self._n_link}"
        link = {
            "id": link_id, "source_id": source_id, "target_id": target_id,
            "link_type": normalized, "created_by": created_by,
        }
        self._links[link_id] = link
        self.write_calls.append(f"add_link:{source_id}->{target_id}:{created_by}")
        return dict(link)

    def delete_link(self, link_id) -> None:
        self._links.pop(link_id, None)
        self.write_calls.append(f"delete_link:{link_id}")


def _dep_bead(bead_id: str, ref: str) -> dict[str, Any]:
    return {"id": bead_id, "type": "application", "content": {"ref": ref}}


def _write_fixture_model(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "business-layer.yaml").write_text(
        "capabilities:\n"
        "  - ref: bc.test\n"
        "    state: active\n"
        "    content:\n"
        "      name: Test capability\n"
        "      description: a test capability\n"
        "      layer: demand\n"
        "      owner: test-owner\n"
        "      evidence: [README.md]\n"
        "      assessed_at: 2026-08-01\n"
        "      maturity: operating\n"
    )
    (model_dir / "application-portfolio.yaml").write_text(
        "applications:\n"
        "  - ref: app.test\n"
        "    state: operate\n"
        "    content:\n"
        "      name: Test app\n"
        "      description: a test application\n"
        "      layer: supply\n"
        "      owner: test-owner\n"
        "      evidence: [README.md]\n"
        "      assessed_at: 2026-08-01\n"
        "      workload:\n"
        "        runtime: none\n"
        "        note: test fixture, runs nowhere\n"
        "      build: custom\n"
        "      technical_health: healthy\n"
        "      business_value: low\n"
        "      time_disposition: tolerate\n"
        "      realizes: [bc.test]\n"
    )


def _write_fixture_model_with_kubernetes_app(model_dir: Path) -> None:
    """The observed shape: a kubernetes-runtime application authored with no objects,
    post-#514 -- `content.workload.objects` is derived, not hand-copied into the YAML."""
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "business-layer.yaml").write_text(
        "capabilities:\n"
        "  - ref: bc.test\n"
        "    state: active\n"
        "    content:\n"
        "      name: Test capability\n"
        "      description: a test capability\n"
        "      layer: demand\n"
        "      owner: test-owner\n"
        "      evidence: [README.md]\n"
        "      assessed_at: 2026-08-01\n"
        "      maturity: operating\n"
    )
    (model_dir / "application-portfolio.yaml").write_text(
        "applications:\n"
        "  - ref: app.k8s\n"
        "    state: operate\n"
        "    content:\n"
        "      name: K8s app\n"
        "      description: a kubernetes application, authored runtime-only\n"
        "      layer: supply\n"
        "      owner: test-owner\n"
        "      evidence: [README.md]\n"
        "      assessed_at: 2026-08-01\n"
        "      workload:\n"
        "        runtime: kubernetes\n"
        "      build: custom\n"
        "      technical_health: healthy\n"
        "      business_value: low\n"
        "      time_disposition: tolerate\n"
        "      realizes: [bc.test]\n"
    )


def _fixed_now(when: str = "2026-08-23T12:00:00+00:00"):
    parsed = datetime.fromisoformat(when)
    return lambda: parsed


def _cfg(tmp_path: Path) -> "dispatch.Config":
    return dispatch.Config(base_ref="main", repo_root=tmp_path)


# --- OPS-191 round 4: the second caller of ea-load.py's paging fix (dev.finding 2bf3c19b) ------
#
# ea_apply.py drives the same build_plan/reconcile_objects as scripts/ea-load.py's own CLI,
# through its own ModelSubstrate Protocol and its own FakeSubstrate double. Those two call
# sites did not move when ea-load.py's `_list_all` started walking `offset` to page past a
# 1,000-row `list_beads` page -- this regression exercises that caller specifically, not a
# re-run of scripts/tests/test_ea_load.py's equivalent test under a different name.


def _write_fixture_model_with_observation(model_dir: Path) -> None:
    _write_fixture_model(model_dir)
    (model_dir / "observations.yaml").write_text(
        "observations:\n"
        "  - ref: obs.test.overflow\n"
        "    state: active\n"
        "    content:\n"
        "      observed_at: 2026-08-04T16:18:17Z\n"
        "      workload:\n"
        "        cluster: test-cluster\n"
        "        namespace: test-ns\n"
        "        kind: Deployment\n"
        "        name: overflow\n"
        "      measures: [app.test]\n"
    )


def test_a_ref_past_the_first_page_is_updated_not_recreated_through_ea_apply(tmp_path):
    """Fails against a FakeSubstrate.list_beads that accepts `offset` and ignores it: with
    1,000 filler observations ahead of the declared one, `ea-load.py`'s `_list_all` would
    never see a page shorter than `_PAGE_SIZE`, walk past `_MAX_PAGES`, and `sys.exit` --
    loud, not silent, but still a failure of this test. With `offset` honoured, the declared
    ref is found on the second page and reconciled as unchanged, never re-created."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model_with_observation(model_dir)
    objects, _types = eal.load_model(model_dir)
    target = objects["obs.test.overflow"]

    beads = [
        {"id": f"filler-{i}", "type": "observation", "state": "active",
         "content": {"ref": f"obs.filler-{i}",
                     "observed_at": "2026-08-04T16:18:17Z",
                     "workload": {"cluster": "c", "namespace": "n", "kind": "Deployment",
                                  "name": f"filler-{i}"}}}
        for i in range(eal._PAGE_SIZE)
    ]
    beads.append({
        "id": "target-0", "type": "observation", "state": target["state"],
        "content": eal.content_for(target),
    })
    sub = FakeSubstrate(beads)

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    assert result["status"] == "applied"
    # capability + application only -- the observation past page one must not recreate.
    assert result["summary"]["created"] == 2, result["summary"]
    assert "target-0" in sub._beads, "the pre-existing bead past page one must survive unreplaced"
    assert sub._beads["target-0"]["content"] == eal.content_for(target)


# --- PRIN-014: same revision twice is zero writes ----------------------------------------------


def test_a_changed_revision_applies_and_records_revision_and_summary(tmp_path):
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    assert result["status"] == "applied"
    assert result["revision"] == "rev-1"
    assert result["summary"]["created"] == 2
    assert result["summary"]["links_added"] == 1

    status = ea_apply.find_status(sub)
    assert status["content"]["ref"] == ea_apply.STATUS_REF
    assert status["content"]["last_synced_revision"] == "rev-1"
    assert status["context"]["status"] == "ok"
    assert status["context"]["applied_revision"] == "rev-1"
    assert status["context"]["summary"]["created"] == 2


def test_status_observation_declares_source_class_derived_and_created_by_on_create(tmp_path):
    """"factory-dispatcher/ea-apply" is enrolled in apps/substrate/src/bead_rules.py's
    SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822) -- the first landing (create) path
    must declare it."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    status = ea_apply.find_status(sub)
    assert status["content"]["source_class"] == "derived"
    assert status["created_by"] == ea_apply.CREATED_BY


def test_status_observation_declares_source_class_derived_and_created_by_on_update(tmp_path):
    """Same identity, existing-status (update/patch) path: the unchanged-revision branch
    still refreshes the standing record (R2605-8 DEFECT 2)."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now("2026-08-24T12:00:00+00:00"), revision_fn=lambda cfg: "rev-1",
    )

    status = ea_apply.find_status(sub)
    assert status["content"]["source_class"] == "derived"
    assert status["created_by"] == ea_apply.CREATED_BY


def test_the_same_revision_applied_twice_issues_no_model_writes_the_second_time(tmp_path):
    """PRIN-014 preserves zero MODEL writes on an unchanged revision -- not zero writes
    outright. The standing status record still refreshes every run (R2605-8 DEFECT 2,
    covered below); the only thing the short-circuit must skip is a second `create`."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    first = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )
    assert first["status"] == "applied"
    assert sub.write_calls, "the first apply must have written"
    status_id = ea_apply.find_status(sub)["id"]

    sub.write_calls = []
    second = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    assert second == {"status": "unchanged", "revision": "rev-1"}
    # No `create:application`/`create:capability` -- only the standing status
    # bead's own patch, which is not a model write.
    assert sub.write_calls == [f"patch:{status_id}"]


def test_unchanged_revision_still_refreshes_the_standing_status_record(tmp_path):
    """R2605-8 DEFECT 2: before this fix, `apply_ea_model` returned before writing
    anything on the unchanged path, so five Completed runs in a row over 12 days left
    `obs.ea-apply-status`'s `observed_at` frozen at the last revision change -- a
    reconciler idle because nothing changed and one that had stopped produced the
    identical record."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now("2026-08-23T12:00:00+00:00"), revision_fn=lambda cfg: "rev-1",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("build_plan must not be called when the revision is unchanged")

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=_boom, model_dir=model_dir,
        now_fn=_fixed_now("2026-09-04T02:00:00+00:00"), revision_fn=lambda cfg: "rev-1",
    )

    assert result == {"status": "unchanged", "revision": "rev-1"}
    status = ea_apply.find_status(sub)
    assert status["content"]["observed_at"] == "2026-09-04T02:00:00Z"
    # Distinct from "ok" (applied): a reader must never have to guess whether
    # this run applied something or found nothing to do.
    assert status["context"]["status"] == "unchanged"
    assert status["context"]["applied_revision"] == "rev-1"


def test_nothing_under_the_model_path_changing_means_no_reconcile_is_even_attempted(tmp_path):
    """The short-circuit is the revision comparison, not ea-load's own diffing."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("build_plan must not be called when the revision is unchanged")

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=_boom, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )
    assert result["status"] == "unchanged"


# --- OPS-181/OPS-182: a duplicate-ref create refusal is recorded by status+ref, never body ------


def test_ea_apply_a_409_duplicate_ref_refusal_creating_the_status_bead_is_recorded_by_status_and_ref(
    tmp_path,
):
    """A residual create/create race `find_status` does not itself close (two
    concurrent reconcile runs racing the first landing): the store's own
    unique-ref constraint (migration 0006_unique_arch_ref) refuses the second
    create with 409. The failure this bead fixes (OPS-181) is exactly this
    shape; OPS-182 is that the refusal's response body -- read by a separate
    finance-integrity mapper -- gets relabelled as a plaid duplicate. The
    recorded reason here must never contain that body."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)

    class RacyFakeSubstrate(FakeSubstrate):
        def create(self, bead_type, state, content, parent_id, *, created_by: str = ""):
            if bead_type == "observation" and "obs-race" not in self._beads:
                # A concurrent writer's create landed first; stash the bead
                # directly (bypassing this fake's own create, which would
                # otherwise just succeed) so the post-failure find_bead lookup
                # below sees it, exactly as the live store would.
                self._beads["obs-race"] = {
                    "id": "obs-race", "type": "observation", "state": "active",
                    "content": dict(content), "context": {}, "parent_id": None,
                    "created_by": "factory-dispatcher/ea-apply-concurrent",
                }
                raise SubstrateError(409, "plaid: duplicate transaction reference detected")
            return super().create(bead_type, state, content, parent_id, created_by=created_by)

    sub = RacyFakeSubstrate()

    with pytest.raises(ea_apply.EaApplyError) as excinfo:
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
            now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        )

    message = str(excinfo.value)
    assert "409" in message
    assert ea_apply.STATUS_REF in message
    assert "plaid" not in message
    assert "duplicate transaction" not in message

    status = ea_apply.find_status(sub)
    assert status["id"] == "obs-race"
    assert status["context"]["status"] == "failed"
    assert status["context"]["duplicate_ref_refusal"] == {"status": 409, "ref": ea_apply.STATUS_REF}
    assert "plaid" not in status["context"]["reason"]


# --- PRIN-008: a standing, dated failure record; a retry carries the predecessor's reason ------


def test_a_reconcile_failure_writes_a_standing_dated_record_naming_the_failure_and_revision(
    tmp_path,
):
    """Fails against today's behaviour: no applier exists to fail loudly."""
    sub = FakeSubstrate()

    def _failing_build_plan(*args, **kwargs):
        raise RuntimeError("substrate 500 on /beads: boom")

    with pytest.raises(ea_apply.EaApplyError, match="rev-bad"):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=tmp_path,
            now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-bad",
        )

    status = ea_apply.find_status(sub)
    assert status is not None, "the failure must be visible without querying Temporal"
    assert status["context"]["status"] == "failed"
    assert status["context"]["model_revision"] == "rev-bad"
    assert "boom" in status["context"]["reason"]
    assert status["content"]["observed_at"] == "2026-08-23T12:00:00Z"


def test_a_retry_carries_its_predecessors_failure_reason(tmp_path):
    sub = FakeSubstrate()

    def _failing_build_plan(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=tmp_path,
            now_fn=_fixed_now("2026-08-23T12:00:00+00:00"), revision_fn=lambda cfg: "rev-bad-1",
        )

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=tmp_path,
            now_fn=_fixed_now("2026-08-23T13:00:00+00:00"), revision_fn=lambda cfg: "rev-bad-2",
        )

    status = ea_apply.find_status(sub)
    assert status["context"]["model_revision"] == "rev-bad-2"
    assert status["context"]["previous_failure"] == {
        "revision": "rev-bad-1", "reason": "boom", "failed_at": "2026-08-23T12:00:00Z",
    }


def test_a_successful_retry_records_what_it_recovered_from(tmp_path):
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    def _failing_build_plan(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=model_dir,
            now_fn=_fixed_now("2026-08-23T12:00:00+00:00"), revision_fn=lambda cfg: "rev-bad",
        )

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now("2026-08-23T13:00:00+00:00"), revision_fn=lambda cfg: "rev-good",
    )
    assert result["status"] == "applied"

    status = ea_apply.find_status(sub)
    assert status["context"]["status"] == "ok"
    assert status["context"]["recovered_from"] == {
        "revision": "rev-bad", "reason": "boom", "failed_at": "2026-08-23T12:00:00Z",
    }
    # a failed attempt never advances what "applied" means; the record was left at None
    # (never applied) until this retry actually succeeded.
    assert status["content"]["last_synced_revision"] == "rev-good"


def test_a_failed_attempt_does_not_advance_the_applied_revision(tmp_path):
    """A failure at revision N+1 must not make the next tick think N+1 is already applied."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now("2026-08-23T12:00:00+00:00"), revision_fn=lambda cfg: "rev-good",
    )

    def _failing_build_plan(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=model_dir,
            now_fn=_fixed_now("2026-08-23T13:00:00+00:00"), revision_fn=lambda cfg: "rev-broken",
        )

    status = ea_apply.find_status(sub)
    assert status["context"]["status"] == "failed"
    assert status["context"]["applied_revision"] == "rev-good"
    assert status["content"]["last_synced_revision"] == "rev-good"


# --- a kubernetes application's workload is resolved before build_plan ever sees it ------------


def test_a_kubernetes_application_authored_runtime_only_reconciles_when_the_derivation_resolves(
    tmp_path,
):
    """The observed shape (#514): `workload: {runtime: kubernetes}`, no objects -- reconciles
    once the manifests resolve, instead of 422-ing against the substrate's schema."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model_with_kubernetes_app(model_dir)
    sub = FakeSubstrate()
    derived_workloads = {
        "app.k8s": [{
            "cluster": "cluster-a", "namespace": "prod", "kind": "Deployment",
            "name": "app-k8s", "manifest": "infrastructure/k8s/app-k8s/deployment.yaml",
            "managed_by": "argocd",
        }],
    }

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        derived_workloads=derived_workloads,
    )

    assert result["status"] == "applied"
    [app_bead] = sub.list_beads("application")
    assert app_bead["content"]["workload"]["objects"] == derived_workloads["app.k8s"]
    status = ea_apply.find_status(sub)
    assert status["context"]["status"] == "ok"


def test_a_kubernetes_application_with_no_derivable_workload_is_refused_by_name(tmp_path):
    """AC: refuse the application by name; never send an empty workload and let the
    substrate's 422 be what catches it."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model_with_kubernetes_app(model_dir)
    sub = FakeSubstrate()

    with pytest.raises(ea_apply.EaApplyError, match="app.k8s"):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
            now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
            derived_workloads={},
        )

    assert sub.list_beads("application") == [], (
        "the application must never reach the substrate with an empty workload"
    )
    status = ea_apply.find_status(sub)
    assert status["context"]["status"] == "failed"
    assert "app.k8s" in status["context"]["reason"]


def test_derived_workloads_not_wired_in_leaves_existing_callers_unaffected(tmp_path):
    """Every existing caller that never passes derived_workloads sees no new behaviour --
    a kubernetes application authored runtime-only is sent to the substrate unchanged, and
    the (real-schema) substrate is what rejects it, exactly like before this fix."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model_with_kubernetes_app(model_dir)
    sub = FakeSubstrate()

    with pytest.raises(ea_apply.EaApplyError, match="requires at least one object"):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
            now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        )


def test_the_same_revision_applied_twice_with_derived_workloads_wired_in_is_zero_writes(
    tmp_path,
):
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model_with_kubernetes_app(model_dir)
    sub = FakeSubstrate()
    derived_workloads = {
        "app.k8s": [{
            "cluster": "cluster-a", "namespace": "prod", "kind": "Deployment",
            "name": "app-k8s", "manifest": "infrastructure/k8s/app-k8s/deployment.yaml",
            "managed_by": "argocd",
        }],
    }

    first = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        derived_workloads=derived_workloads,
    )
    assert first["status"] == "applied"
    status_id = ea_apply.find_status(sub)["id"]

    sub.write_calls = []
    second = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        derived_workloads=derived_workloads,
    )

    assert second == {"status": "unchanged", "revision": "rev-1"}
    # Only the standing status bead's own patch -- no second model write.
    assert sub.write_calls == [f"patch:{status_id}"]


# --- a failure repeating its predecessor's exact reason is a standing condition -----------------


def test_a_failure_repeating_its_predecessors_reason_is_surfaced_as_a_standing_condition(
    tmp_path,
):
    sub = FakeSubstrate()

    def _failing_build_plan(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=tmp_path,
            now_fn=_fixed_now("2026-08-23T12:00:00+00:00"), revision_fn=lambda cfg: "rev-bad-1",
        )
    status = ea_apply.find_status(sub)
    assert status["context"]["consecutive_failures"] == 1
    assert "repeat_of_previous_failure" not in status["context"]

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_failing_build_plan, model_dir=tmp_path,
            now_fn=_fixed_now("2026-08-23T13:00:00+00:00"), revision_fn=lambda cfg: "rev-bad-2",
        )
    status = ea_apply.find_status(sub)
    assert status["context"]["consecutive_failures"] == 2
    assert status["context"]["repeat_of_previous_failure"] is True

    # a different reason is a new condition, not a continuation of the old one.
    def _differently_failing_build_plan(*args, **kwargs):
        raise RuntimeError("a completely different problem")

    with pytest.raises(ea_apply.EaApplyError):
        ea_apply.apply_ea_model(
            sub, cfg=_cfg(tmp_path), build_plan=_differently_failing_build_plan,
            model_dir=tmp_path,
            now_fn=_fixed_now("2026-08-23T14:00:00+00:00"), revision_fn=lambda cfg: "rev-bad-3",
        )
    status = ea_apply.find_status(sub)
    assert status["context"]["consecutive_failures"] == 1
    assert "repeat_of_previous_failure" not in status["context"]


# --- current_model_revision: only the model path moves it --------------------------------------


def _git(cwd: Path, *args: str):
    return dispatch.run([dispatch.GIT, *args], cwd=cwd)


def test_current_model_revision_tracks_only_commits_touching_the_model_path(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    model_dir = tmp_path / "docs" / "architecture" / "model"
    model_dir.mkdir(parents=True)
    (model_dir / "business-layer.yaml").write_text("capabilities: []\n")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed model")
    cfg = dispatch.Config(base_ref="main", repo_root=tmp_path)
    first_rev = ea_apply.current_model_revision(cfg)

    (tmp_path / "README.md").write_text("hello again\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "unrelated change")
    second_rev = ea_apply.current_model_revision(cfg)
    assert second_rev == first_rev, "a commit outside the model path must not move the revision"

    (model_dir / "business-layer.yaml").write_text("capabilities: []\n# touched\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "touch the model")
    third_rev = ea_apply.current_model_revision(cfg)
    assert third_rev != first_rev


# --- PC-ASR-007/AC-2: derived depends_on edges are written, not hand-copied --------------------


def test_dependency_reconcile_runs_alongside_the_authored_apply_and_reports_a_summary(tmp_path):
    """Fails against today's behaviour: apply_ea_model has no dependency-reconcile step."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    dep_sub = FakeDependencySubstrate(
        beads=[_dep_bead("b-api", "app.api"), _dep_bead("b-db", "app.db")]
    )

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies={"app.api": {"app.db": None}},
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )

    assert result["status"] == "applied"
    assert result["dependencies"] == {"created": 1, "removed": 0}
    assert dep_sub.write_calls == ["add_link:b-api->b-db:ea-derive"]


def test_dependency_reconcile_is_skipped_when_not_wired_in(tmp_path):
    """Every existing caller that never passes dependency_sub sees no new behaviour."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
    )

    assert "dependencies" not in result


def test_dependency_reconcile_runs_even_when_the_model_revision_is_unchanged(tmp_path):
    """PRIN-014's revision gate only watches docs/architecture/model/**. A dependency can go
    stale from an infrastructure/k8s/** change alone, so the dependency reconcile must not be
    gated on the same short-circuit that skips the authored `build_plan` call."""
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    dep_sub = FakeDependencySubstrate(
        beads=[_dep_bead("b-api", "app.api"), _dep_bead("b-db", "app.db")]
    )

    first = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies={},
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )
    assert first["status"] == "applied"
    assert first["dependencies"] == {"created": 0, "removed": 0}

    # The model revision has not moved, but a manifest elsewhere now proves a new dependency.
    second = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies={"app.api": {"app.db": None}},
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )

    assert second["status"] == "unchanged"
    assert second["dependencies"] == {"created": 1, "removed": 0}
    assert dep_sub.write_calls[-1] == "add_link:b-api->b-db:ea-derive"


def test_a_dependency_removed_from_every_manifest_is_dropped_on_the_next_cycle(tmp_path):
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    dep_sub = FakeDependencySubstrate(
        beads=[_dep_bead("b-api", "app.api"), _dep_bead("b-db", "app.db")],
        links=[{
            "id": "dep-link-1", "source_id": "b-api", "target_id": "b-db",
            "link_type": "depends_on", "created_by": "ea-derive",
        }],
    )

    result = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies={},
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )

    assert result["dependencies"] == {"created": 0, "removed": 1}
    assert dep_sub.write_calls == ["delete_link:dep-link-1"]


def test_dependency_reconcile_second_pass_with_unchanged_inputs_is_zero_writes(tmp_path):
    model_dir = tmp_path / "docs" / "architecture" / "model"
    _write_fixture_model(model_dir)
    sub = FakeSubstrate()
    dep_sub = FakeDependencySubstrate(
        beads=[_dep_bead("b-api", "app.api"), _dep_bead("b-db", "app.db")]
    )
    derived = {"app.api": {"app.db": None}}

    ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies=derived,
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )
    dep_sub.write_calls = []

    second = ea_apply.apply_ea_model(
        sub, cfg=_cfg(tmp_path), build_plan=eal.build_plan, model_dir=model_dir,
        now_fn=_fixed_now(), revision_fn=lambda cfg: "rev-1",
        dependency_sub=dep_sub, derived_dependencies=derived,
        reconcile_dependencies_fn=ead.reconcile_dependencies,
    )

    assert second["status"] == "unchanged"
    assert second["dependencies"] == {"created": 0, "removed": 0}
    assert dep_sub.write_calls == []
