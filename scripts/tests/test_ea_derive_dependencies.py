"""Tests for the ea-derive.py dependency writing layer (PC-ASR-007/AC-2).

`derive_application_dependencies` -- the pure derivation logic -- is exercised by
scripts/tests/test_ea_derive.py and is not touched or re-tested here. This file covers the
new writing layer: `reconcile_dependencies` (which `bead_link` edges a
`depends-on --apply` run creates or removes) and `authored_derivable_overlap` (the function
the flipped ea-conformance.py DERIVED check now calls).

No live substrate, database, cluster, or network: writes go through a FakeSubstrate that
mirrors exactly the four calls ea-derive.py's own `Substrate` class makes for this feature,
validated against the real `BEAD_LINK_TYPES` (apps/substrate/src/schemas.py, imported --
not hand-copied, the FakeSubstrate.add_note lesson and B-110).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "substrate"))

from src.schemas import BEAD_LINK_TYPES  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "ea_derive_dependencies", REPO / "scripts" / "ea-derive.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ead = _load()


class FakeSubstrate:
    def __init__(self, beads=None, links=None):
        self._beads = {b["id"]: dict(b) for b in (beads or [])}
        self._links = {link["id"]: dict(link) for link in (links or [])}
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
        assert normalized in BEAD_LINK_TYPES, f"link_type must be one of {sorted(BEAD_LINK_TYPES)}"
        self._n_link += 1
        link_id = f"link-{self._n_link}"
        link = {
            "id": link_id, "source_id": source_id, "target_id": target_id,
            "link_type": normalized, "created_by": created_by,
        }
        self._links[link_id] = link
        self.write_calls.append(f"add_link:{source_id}->{target_id}:{created_by}")
        return dict(link)

    def delete_link(self, link_id):
        self._links.pop(link_id, None)
        self.write_calls.append(f"delete_link:{link_id}")


def _bead(bead_id, ref, bead_type="application"):
    return {"id": bead_id, "type": bead_type, "content": {"ref": ref}}


def _link(link_id, source_id, target_id, created_by, link_type="depends_on"):
    return {
        "id": link_id, "source_id": source_id, "target_id": target_id,
        "link_type": link_type, "created_by": created_by,
    }


# --- reconcile_dependencies: reconciled, not appended ---------------------------------------


def test_reconcile_writes_a_derived_edge_the_manifest_proves():
    """Fails against today's behaviour: reconcile_dependencies does not exist yet."""
    sub = FakeSubstrate(beads=[_bead("b-api", "app.api"), _bead("b-db", "app.db")])
    derived = {"app.api": {"app.db": None}}

    plan = ead.reconcile_dependencies(sub, derived)

    assert plan.created == ["app.api -depends_on-> app.db"]
    assert sub.write_calls == ["add_link:b-api->b-db:ea-derive"]
    links = sub.links("b-api")
    assert len(links) == 1
    assert links[0]["created_by"] == "ea-derive"
    assert links[0]["link_type"] == "depends_on"


def test_second_cycle_with_unchanged_inputs_is_zero_writes():
    sub = FakeSubstrate(beads=[_bead("b-api", "app.api"), _bead("b-db", "app.db")])
    derived = {"app.api": {"app.db": None}}

    ead.reconcile_dependencies(sub, derived)
    sub.write_calls = []
    plan = ead.reconcile_dependencies(sub, derived)

    assert plan.created == []
    assert plan.removed == []
    assert sub.write_calls == []


def test_a_dependency_that_leaves_every_manifest_is_removed_next_cycle():
    sub = FakeSubstrate(
        beads=[_bead("b-api", "app.api"), _bead("b-db", "app.db")],
        links=[_link("link-1", "b-api", "b-db", "ea-derive")],
    )

    plan = ead.reconcile_dependencies(sub, {})

    assert plan.removed == ["app.api -depends_on-> app.db"]
    assert sub.write_calls == ["delete_link:link-1"]
    assert sub.links("b-api") == []


def test_authored_edges_are_never_touched_by_the_deriver():
    """A human-authored edge (created_by="ea-load") must survive a reconcile that no longer
    derives it, and must never be counted among the edges the deriver may add or remove."""
    sub = FakeSubstrate(
        beads=[_bead("b-api", "app.api"), _bead("b-runner", "app.external-runner")],
        links=[_link("link-1", "b-api", "b-runner", "ea-load")],
    )

    plan = ead.reconcile_dependencies(sub, {})

    assert plan.created == []
    assert plan.removed == []
    assert sub.write_calls == []
    assert sub.links("b-api") == [
        {"id": "link-1", "source_id": "b-api", "target_id": "b-runner",
         "link_type": "depends_on", "created_by": "ea-load"}
    ]


def test_reconcile_adds_and_removes_in_the_same_cycle_leaving_authored_alone():
    sub = FakeSubstrate(
        beads=[
            _bead("b-api", "app.api"), _bead("b-db", "app.db"),
            _bead("b-cache", "app.cache"), _bead("b-runner", "app.external-runner"),
        ],
        links=[
            _link("link-1", "b-api", "b-db", "ea-derive"),      # stale derived edge
            _link("link-2", "b-api", "b-runner", "ea-load"),    # authored, must be untouched
        ],
    )
    derived = {"app.api": {"app.cache": None}}  # db dropped from the manifest, cache appeared

    plan = ead.reconcile_dependencies(sub, derived)

    assert plan.created == ["app.api -depends_on-> app.cache"]
    assert plan.removed == ["app.api -depends_on-> app.db"]
    remaining = {(link["target_id"], link["created_by"]) for link in sub.links("b-api")}
    assert remaining == {("b-runner", "ea-load"), ("b-cache", "ea-derive")}


def test_dry_run_computes_the_plan_without_writing():
    sub = FakeSubstrate(beads=[_bead("b-api", "app.api"), _bead("b-db", "app.db")])
    derived = {"app.api": {"app.db": None}}

    plan = ead.reconcile_dependencies(sub, derived, dry_run=True)

    assert plan.created == ["app.api -depends_on-> app.db"]
    assert sub.write_calls == []
    assert sub.links("b-api") == []


def test_a_target_not_yet_a_bead_in_the_substrate_is_skipped_not_errored():
    sub = FakeSubstrate(beads=[_bead("b-api", "app.api")])
    derived = {"app.api": {"app.not-yet-loaded": None}}

    plan = ead.reconcile_dependencies(sub, derived)

    assert plan.created == []
    assert sub.write_calls == []


# --- authored_derivable_overlap: the flipped floor -------------------------------------------


def test_authored_derivable_overlap_flags_a_hand_authored_derivable_edge():
    """Fails against today's behaviour: authored_derivable_overlap does not exist yet."""
    apps = [{"ref": "app.api", "content": {"depends_on": ["app.db"]}}]
    derived = {"app.api": {"app.db": ead.DerivedEdge(target="app.db")}}

    overlap = ead.authored_derivable_overlap(apps, derived)

    assert overlap == {"app.api": {"app.db"}}


def test_authored_derivable_overlap_allows_manifest_invisible_authored_edges():
    apps = [{"ref": "app.api", "content": {"depends_on": ["app.external-runner"]}}]
    derived = {"app.api": {"app.db": ead.DerivedEdge(target="app.db")}}

    overlap = ead.authored_derivable_overlap(apps, derived)

    assert overlap == {}


def test_authored_derivable_overlap_allows_omitting_a_derivable_edge_entirely():
    apps = [{"ref": "app.api", "content": {}}]
    derived = {"app.api": {"app.db": ead.DerivedEdge(target="app.db")}}

    overlap = ead.authored_derivable_overlap(apps, derived)

    assert overlap == {}
