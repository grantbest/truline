"""application --depends_on--> application from cluster facts (S52-3, a728d995).

No Temporal, no substrate database, no cluster, no network -- same shape as
``test_ea_observation.py``. The NetworkPolicy direction test below is
ground-truthed against a real cluster object (see the docstring on
``test_networkpolicy_egress_direction_matches_podselector_owner_not_egress_target``
and ``.factory/design.md``) precisely because PR #863's F1 finding was a
direction bug that no fixture caught before it shipped.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "substrate"))

from activities import ea_dependency  # noqa: E402
from activities import ea_observation  # noqa: E402
from src.schemas import (  # noqa: E402
    ArchObservationContent,
    check_source_class_admission,
    check_source_class_ownership,
)

DEPENDENCY_POSTURE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dependency-posture-fixture.json"


def _now(day: int = 23) -> Any:
    return lambda: datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc)


# -- fixture builders: raw kubectl JSON and raw substrate application beads --


def _object(*, cluster: str = "cluster-a", namespace: str, kind: str, name: str) -> dict[str, Any]:
    return {"cluster": cluster, "namespace": namespace, "kind": kind, "name": name, "managed_by": "argocd"}


def _bead_app(ref: str, *, bead_id: str, objects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if objects is None:
        workload = {"runtime": "none", "note": "not on kubernetes"}
    else:
        workload = {"runtime": "kubernetes", "objects": objects}
    return {"id": bead_id, "state": "operate", "content": {"ref": ref, "workload": workload}}


def _container(name: str, *, env: list[dict[str, Any]] | None = None, env_from: bool = False) -> dict[str, Any]:
    container: dict[str, Any] = {"name": name}
    if env is not None:
        container["env"] = env
    if env_from:
        container["envFrom"] = [{"configMapRef": {"name": "x"}}]
    return container


def _literal_env(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value}


def _secret_env(name: str) -> dict[str, Any]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}


def _deployment(
    *,
    namespace: str,
    name: str,
    labels: dict[str, str] | None = None,
    containers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "metadata": {"namespace": namespace, "name": name},
        "spec": {
            "template": {
                "metadata": {"labels": labels or {}},
                "spec": {"containers": containers or []},
            }
        },
    }


def _egress_policy(
    *, namespace: str, name: str, pod_selector: dict[str, Any], egress: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"policyTypes": ["Egress"], "podSelector": pod_selector, "egress": egress},
    }


class FakeDependencyStore:
    """In-memory double for :class:`ea_dependency.DependencyObserverStore`."""

    def __init__(self) -> None:
        self.links: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self._next_id = 1

    def _fresh_id(self, prefix: str) -> str:
        bead_id = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return bead_id

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict[str, Any]]:
        result = []
        for link in self.links:
            if direction == "outgoing" and link["source_id"] != bead_id:
                continue
            if direction == "incoming" and link["target_id"] != bead_id:
                continue
            if direction == "both" and bead_id not in (link["source_id"], link["target_id"]):
                continue
            if link_type is not None and link["link_type"] != link_type:
                continue
            result.append(dict(link))
        return result

    def create_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str,
        created_by: str,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        link = {
            "id": self._fresh_id("link"),
            "source_id": source_id,
            "target_id": target_id,
            "link_type": link_type,
            "created_by": created_by,
            "content": content or {},
        }
        self.links.append(link)
        return link

    def delete_link(self, link_id: str) -> dict[str, Any]:
        for link in list(self.links):
            if link["id"] == link_id:
                self.links.remove(link)
                return {"status": "deleted"}
        raise AssertionError(f"no such link {link_id}")

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        for bead in self.observations:
            if (bead.get("content") or {}).get("ref") == ref:
                return dict(bead)
        return None

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["state"] == "active"
        # AC-7(a2): refuse what the live store refuses -- POST /beads runs
        # check_source_class_admission against bead_rules.SOURCE_CLASS_WRITERS
        # before it ever gets to content validation. A double that accepts a
        # writer the live enrollment refuses is a second, more permissive
        # implementation of the same contract.
        declared_class = (payload["content"] or {}).get("source_class", "authored")
        violation = check_source_class_admission(declared_class, payload["created_by"])
        if violation is not None:
            raise AssertionError(
                f"source_class_admission_violation: {payload['created_by']!r} is not "
                f"enrolled for declared_class {violation.owning_class!r} "
                "(apps/substrate/src/bead_rules.py:SOURCE_CLASS_WRITERS)"
            )
        ArchObservationContent.model_validate(payload["content"])
        bead = {"id": self._fresh_id("obs"), **payload}
        self.observations.append(bead)
        return dict(bead)

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for bead in self.observations:
            if bead["id"] == bead_id:
                # The live SubstrateEAObserverStore.update_observation
                # hard-codes the PATCH body's created_by to
                # ea_observation.CREATED_BY regardless of caller (every
                # caller shares one store instance) -- so that is the writer
                # the content-PATCH route's ownership check evaluates here
                # too, per AC-7(a2).
                writer = ea_observation.CREATED_BY
                existing_class = (bead.get("content") or {}).get("source_class", "authored")
                violation = check_source_class_ownership(existing_class, bead["created_by"], writer)
                if violation is not None:
                    raise AssertionError(
                        f"source_class_ownership_violation: {writer!r} may not overwrite a "
                        f"{violation.owning_class!r} fact owned by {bead['created_by']!r}"
                    )
                if content is not None:
                    ArchObservationContent.model_validate(content)
                    bead["content"] = content
                if state is not None:
                    bead["state"] = state
                if context is not None:
                    bead["context"] = context
                return dict(bead)
        raise AssertionError(f"no such observation {bead_id}")


# -- signal 1: env-var host reference -----------------------------------------


def test_env_signal_resolves_edge_and_names_target_application_in_method():
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="platform-core", kind="Deployment", name="substrate")],
    )
    qdrant = _bead_app(
        "app.qdrant", bead_id="id-qdrant",
        objects=[_object(namespace="platform-core", kind="Deployment", name="qdrant")],
    )
    deployments = {
        "items": [
            _deployment(
                namespace="platform-core",
                name="substrate",
                containers=[
                    _container(
                        "substrate",
                        env=[_literal_env("QDRANT_URL", "http://qdrant.platform-core.svc.cluster.local:6333")],
                    )
                ],
            ),
            _deployment(namespace="platform-core", name="qdrant"),
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=None,
        cluster="cluster-a", applications=[substrate, qdrant],
    )

    assert len(edges) == 1
    [edge] = edges
    assert edge.source_id == "id-substrate"
    assert edge.target_id == "id-qdrant"
    assert "app.qdrant" in edge.method
    assert "QDRANT_URL" in edge.method


def test_env_signal_never_reads_a_valuefrom_env_as_a_reference():
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b = _bead_app("app.b", bead_id="id-b", objects=[_object(namespace="ns", kind="Deployment", name="b")])
    deployments = {
        "items": [
            _deployment(namespace="ns", name="a", containers=[_container("a", env=[_secret_env("B_URL")])]),
            _deployment(namespace="ns", name="b"),
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=None,
        cluster="cluster-a", applications=[app_a, app_b],
    )

    assert edges == []


def test_env_signal_requires_an_unambiguous_single_owner_on_each_end():
    # Two applications both declare a Deployment named "shared" -- the host
    # reference cannot name exactly one target, so no edge is written (F1).
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b1 = _bead_app("app.b1", bead_id="id-b1", objects=[_object(namespace="ns", kind="Deployment", name="shared")])
    app_b2 = _bead_app("app.b2", bead_id="id-b2", objects=[_object(namespace="ns", kind="Deployment", name="shared")])
    deployments = {
        "items": [
            _deployment(
                namespace="ns", name="a",
                containers=[_container("a", env=[_literal_env("URL", "shared.ns.svc.cluster.local")])],
            ),
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=None,
        cluster="cluster-a", applications=[app_a, app_b1, app_b2],
    )

    assert edges == []


# -- signal 2: NetworkPolicy egress -------------------------------------------


def test_networkpolicy_egress_direction_matches_podselector_owner_not_egress_target():
    """F1 regression. Ground-truthed against the live cluster before writing
    this fixture: ``allow-substrate-egress`` in ``platform-substrate-prod``
    carries ``podSelector: {app: substrate}`` (the substrate Deployment's own
    pod-template labels) and an egress rule to ``{app: qdrant}`` in the same
    namespace. PR #863 attributed all of this policy's edges to app.qdrant,
    as if qdrant were the source -- the exact opposite of what the policy
    says. The correct edge is substrate -> depends_on -> qdrant."""
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="platform-substrate-prod", kind="Deployment", name="substrate")],
    )
    qdrant = _bead_app(
        "app.qdrant", bead_id="id-qdrant",
        objects=[_object(namespace="platform-substrate-prod", kind="Deployment", name="qdrant")],
    )
    deployments = {
        "items": [
            _deployment(namespace="platform-substrate-prod", name="substrate", labels={"app": "substrate"}),
            _deployment(namespace="platform-substrate-prod", name="qdrant", labels={"app": "qdrant"}),
        ]
    }
    networkpolicies = {
        "items": [
            _egress_policy(
                namespace="platform-substrate-prod",
                name="allow-substrate-egress",
                pod_selector={"matchLabels": {"app": "substrate"}},
                egress=[{"to": [{"podSelector": {"matchLabels": {"app": "qdrant"}}}], "ports": [{"port": 6333}]}],
            )
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[substrate, qdrant],
    )

    assert len(edges) == 1
    [edge] = edges
    assert edge.source_id == "id-substrate"
    assert edge.target_id == "id-qdrant"
    assert edge.source_ref == "app.substrate"
    assert edge.target_ref == "app.qdrant"
    assert "app.qdrant" in edge.method
    assert "allow-substrate-egress" in edge.method


def test_networkpolicy_egress_resolves_cross_namespace_target_via_metadata_name_selector():
    """The same live policy's second egress rule: temporal-postgres, in a
    different namespace than the policy, named via
    ``namespaceSelector.matchLabels['kubernetes.io/metadata.name']``."""
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="platform-substrate-prod", kind="Deployment", name="substrate")],
    )
    postgres = _bead_app(
        "app.temporal-postgres", bead_id="id-postgres",
        objects=[_object(namespace="platform-core", kind="Deployment", name="temporal-postgres")],
    )
    deployments = {
        "items": [
            _deployment(namespace="platform-substrate-prod", name="substrate", labels={"app": "substrate"}),
            _deployment(namespace="platform-core", name="temporal-postgres", labels={"app": "temporal-postgres"}),
        ]
    }
    networkpolicies = {
        "items": [
            _egress_policy(
                namespace="platform-substrate-prod",
                name="allow-substrate-egress",
                pod_selector={"matchLabels": {"app": "substrate"}},
                egress=[
                    {
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "platform-core"}},
                                "podSelector": {"matchLabels": {"app": "temporal-postgres"}},
                            }
                        ],
                        "ports": [{"port": 5432}],
                    }
                ],
            )
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[substrate, postgres],
    )

    assert len(edges) == 1
    [edge] = edges
    assert edge.source_id == "id-substrate"
    assert edge.target_id == "id-postgres"


def test_networkpolicy_empty_podselector_matches_no_one_and_produces_no_edge():
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="ns", kind="Deployment", name="substrate")],
    )
    deployments = {"items": [_deployment(namespace="ns", name="substrate", labels={"app": "substrate"})]}
    networkpolicies = {
        "items": [
            _egress_policy(namespace="ns", name="default-deny-egress", pod_selector={}, egress=[]),
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[substrate],
    )

    assert edges == []


def test_networkpolicy_egress_namespaceselector_without_metadata_name_is_unresolved():
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="ns", kind="Deployment", name="substrate")],
    )
    other = _bead_app("app.other", bead_id="id-other", objects=[_object(namespace="ns2", kind="Deployment", name="other")])
    deployments = {
        "items": [
            _deployment(namespace="ns", name="substrate", labels={"app": "substrate"}),
            _deployment(namespace="ns2", name="other", labels={"app": "other"}),
        ]
    }
    networkpolicies = {
        "items": [
            _egress_policy(
                namespace="ns",
                name="broad-egress",
                pod_selector={"matchLabels": {"app": "substrate"}},
                egress=[{"to": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"app": "other"}}}]}],
            )
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[substrate, other],
    )

    assert edges == []


# -- combine, dedupe -----------------------------------------------------


def test_compute_dependency_edges_dedupes_the_same_pair_seen_by_both_signals():
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b = _bead_app("app.b", bead_id="id-b", objects=[_object(namespace="ns", kind="Deployment", name="b")])
    deployments = {
        "items": [
            _deployment(
                namespace="ns", name="a", labels={"app": "a"},
                containers=[_container("a", env=[_literal_env("URL", "b.ns.svc.cluster.local")])],
            ),
            _deployment(namespace="ns", name="b", labels={"app": "b"}),
        ]
    }
    networkpolicies = {
        "items": [
            _egress_policy(
                namespace="ns", name="a-egress",
                pod_selector={"matchLabels": {"app": "a"}},
                egress=[{"to": [{"podSelector": {"matchLabels": {"app": "b"}}}]}],
            )
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[app_a, app_b],
    )

    assert len(edges) == 1


# -- F1: every written edge's method names its target application ------------


def test_every_computed_edge_method_names_its_target_application():
    substrate = _bead_app(
        "app.substrate", bead_id="id-substrate",
        objects=[_object(namespace="platform-substrate-prod", kind="Deployment", name="substrate")],
    )
    qdrant = _bead_app(
        "app.qdrant", bead_id="id-qdrant",
        objects=[_object(namespace="platform-substrate-prod", kind="Deployment", name="qdrant")],
    )
    deployments = {
        "items": [
            _deployment(
                namespace="platform-substrate-prod", name="substrate", labels={"app": "substrate"},
                containers=[_container("substrate", env=[_literal_env("Q", "qdrant.platform-substrate-prod.svc")])],
            ),
            _deployment(namespace="platform-substrate-prod", name="qdrant", labels={"app": "qdrant"}),
        ]
    }
    networkpolicies = {
        "items": [
            _egress_policy(
                namespace="platform-substrate-prod", name="allow-substrate-egress",
                pod_selector={"matchLabels": {"app": "substrate"}},
                egress=[{"to": [{"podSelector": {"matchLabels": {"app": "qdrant"}}}]}],
            )
        ]
    }

    edges = ea_dependency.compute_dependency_edges(
        deployments=deployments, statefulsets=None, networkpolicies=networkpolicies,
        cluster="cluster-a", applications=[substrate, qdrant],
    )

    assert edges  # the fixture produces at least one edge
    for edge in edges:
        assert edge.target_ref in edge.method


# -- landing: create, existence-check, retraction (F1, F1b) ------------------


def test_land_dependency_posture_creates_edge_carrying_method_as_link_content():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a")
    app_b = _bead_app("app.b", bead_id="id-b")
    edge = ea_dependency.DependencyEdge("id-a", "app.a", "id-b", "app.b", "env: names app.b")

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=[], edges=[edge], now_fn=_now()
    )

    assert result["edges_created"] == 1
    [link] = store.links
    assert link["source_id"] == "id-a"
    assert link["target_id"] == "id-b"
    assert link["link_type"] == "depends_on"
    assert link["created_by"] == ea_dependency.CREATED_BY
    assert link["content"]["method"] == "env: names app.b"
    assert link["content"]["target_ref"] == "app.b"


def test_land_dependency_posture_never_touches_or_duplicates_an_edge_ea_derive_already_owns():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a")
    app_b = _bead_app("app.b", bead_id="id-b")
    store.links.append(
        {"id": "existing", "source_id": "id-a", "target_id": "id-b", "link_type": "depends_on",
         "created_by": "ea-derive", "content": {}}
    )
    # This cycle independently (re-)computes the same edge from cluster facts.
    edge = ea_dependency.DependencyEdge("id-a", "app.a", "id-b", "app.b", "env: names app.b")

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=[], edges=[edge], now_fn=_now()
    )

    assert result["edges_created"] == 0
    assert result["edges_retracted"] == 0
    assert len(store.links) == 1
    assert store.links[0]["created_by"] == "ea-derive"
    assert result["known_by_edges"] == 1


def test_land_dependency_posture_retracts_an_edge_it_previously_wrote_when_no_longer_justified():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b = _bead_app("app.b", bead_id="id-b", objects=[_object(namespace="ns", kind="Deployment", name="b")])
    store.links.append(
        {"id": "stale", "source_id": "id-a", "target_id": "id-b", "link_type": "depends_on",
         "created_by": ea_dependency.CREATED_BY, "content": {"method": "no longer true"}}
    )
    deployments = {"items": [_deployment(namespace="ns", name="a")]}  # no env referencing b anymore
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=units, edges=[], now_fn=_now()
    )

    assert result["edges_retracted"] == 1
    assert store.links == []
    # a is now fully assessed with zero dependency edges -> assessed-none.
    assert result["assessed_none_created"] == 1


def test_land_dependency_posture_never_retracts_an_application_to_ci_edge_land_ci_records_wrote():
    """AC-7 (the #908 gate's finding 1, proven by execution): before this
    fix, ``ea_observation.CREATED_BY`` and ``ea_dependency.CREATED_BY`` were
    the literal same string, so this module's retraction loop deleted any
    outgoing depends_on edge with that created_by whose target wasn't in
    this cycle's desired set -- including an application -> ci edge
    land_ci_records had just written, since a ci bead is never in
    ``desired``. Both land in the same ``observe_ea_model`` call, so every
    cycle would create then immediately delete these edges. The fix gives
    this module its own distinct created_by AND restricts retraction to
    targets that are themselves arch.application beads -- an application ->
    ci edge is never retracted by this path regardless of who wrote it,
    because this module never justifies (and so can never withdraw) an edge
    whose target isn't an application it can name."""
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    # "id-ci-1" is a ci bead, never present in `applications` below.
    store.links.append(
        {"id": "app-to-ci", "source_id": "id-a", "target_id": "id-ci-1", "link_type": "depends_on",
         "created_by": "factory-dispatcher/ea-observer", "content": {}}
    )
    deployments = {"items": [_deployment(namespace="ns", name="a")]}  # no app-to-app edge this cycle
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=units, edges=[], now_fn=_now()
    )

    assert result["edges_retracted"] == 0
    assert store.links == [
        {"id": "app-to-ci", "source_id": "id-a", "target_id": "id-ci-1", "link_type": "depends_on",
         "created_by": "factory-dispatcher/ea-observer", "content": {}}
    ]
    # Not known (no application-type edge), not assessed-none (it does
    # depend on something) -- the coherence case, reported separately.
    assert result["known_by_edges"] == 0
    assert result["assessed_none_created"] == 0
    assert store.observations == []
    assert result["ci_only_dependency_refs"] == ["app.a"]
    assert result["posture_summary"]["known"] == 0
    assert result["posture_summary"]["assessed_none"] == 0
    assert result["posture_summary"]["unknown"] == 1
    assert result["posture_summary"]["coherence_case_refs"] == ["app.a"]


def test_land_dependency_posture_never_retracts_an_edge_to_a_non_application_even_under_its_own_identity():
    """AC-8 (M1): the #997 gate removed the target-type guard on AC-7(b) in a
    throwaway edit and no test caught it, because every existing retraction
    test that used this module's own created_by also happened to target an
    arch.application. This pins the guard directly: even an edge THIS
    module's own identity wrote, whose target is not an arch.application
    (here, a ci bead this module never itself creates in practice, but the
    guard checks the target's type, not who could plausibly have written it),
    must never be retracted -- this module never justifies, and so never
    withdraws, an edge whose target isn't an application it can name."""
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    # "id-ci-1" is a ci bead, never present in `applications` below.
    store.links.append(
        {"id": "app-to-ci-own-identity", "source_id": "id-a", "target_id": "id-ci-1", "link_type": "depends_on",
         "created_by": ea_dependency.CREATED_BY, "content": {}}
    )
    deployments = {"items": [_deployment(namespace="ns", name="a")]}  # no app-to-app edge this cycle
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=units, edges=[], now_fn=_now()
    )

    assert result["edges_retracted"] == 0
    assert store.links == [
        {"id": "app-to-ci-own-identity", "source_id": "id-a", "target_id": "id-ci-1", "link_type": "depends_on",
         "created_by": ea_dependency.CREATED_BY, "content": {}}
    ]


def test_ea_dependency_created_by_is_distinct_from_ea_observations():
    """AC-7 (a): the edge writer's identity must be its own, not a string
    reused from ea_observation -- ownership is nominal otherwise. (The
    observation-bead identity is the opposite: AC-7(a) requires it to be
    ea_observation.CREATED_BY exactly, pinned separately above.)"""
    assert ea_dependency.CREATED_BY != ea_observation.CREATED_BY


def test_land_dependency_posture_never_retracts_an_edge_it_does_not_own_even_when_unjustified():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b = _bead_app("app.b", bead_id="id-b")
    store.links.append(
        {"id": "human-made", "source_id": "id-a", "target_id": "id-b", "link_type": "depends_on",
         "created_by": "someone-else", "content": {}}
    )
    deployments = {"items": [_deployment(namespace="ns", name="a")]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=units, edges=[], now_fn=_now()
    )

    assert result["edges_retracted"] == 0
    assert len(store.links) == 1


# -- assessed-none vs unknown (AC-2, F3) --------------------------------------


def test_land_dependency_posture_writes_assessed_none_when_fully_assessed_with_no_edge():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    deployments = {"items": [_deployment(namespace="ns", name="a", containers=[_container("a")])]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=units, edges=[], now_fn=_now()
    )

    assert result["assessed_none_created"] == 1
    assert result["unknown"] == 0
    [obs] = store.observations
    assert obs["content"]["ref"] == ea_dependency.dependency_assessment_ref("app.a")
    assert obs["context"]["assessment"] == "assessed_none"
    assert obs["context"]["application_ref"] == "app.a"
    assert obs["state"] == "active"
    # AC-7(a): the observation bead is created under ea_observation's
    # identity, not this module's own -- SOURCE_CLASS_WRITERS["observed"]
    # does not enroll ea_dependency.CREATED_BY, so a create under it 409s on
    # the live store (proven at the #997 gate by executing
    # check_source_class_admission with the exact identities). This is what
    # the FakeDependencyStore.create_observation enrollment check above now
    # enforces too -- this test failed against the #997 tree.
    assert obs["created_by"] == ea_observation.CREATED_BY
    assert obs["created_by"] != ea_dependency.CREATED_BY


def test_land_dependency_posture_never_stamps_assessed_none_on_an_app_whose_env_is_unresolvable():
    """F3 pin: an application whose environment arrives via valueFrom/envFrom
    must stay unknown, never assessed-none, even with zero computed edges."""
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    deployments = {"items": [_deployment(namespace="ns", name="a", containers=[_container("a", env=[_secret_env("PW")])])]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=units, edges=[], now_fn=_now()
    )

    assert result["assessed_none_created"] == 0
    assert result["unknown"] == 1
    assert store.observations == []


def test_land_dependency_posture_never_stamps_assessed_none_on_an_app_with_envfrom():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    deployments = {"items": [_deployment(namespace="ns", name="a", containers=[_container("a", env_from=True)])]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=units, edges=[], now_fn=_now()
    )

    assert result["assessed_none_created"] == 0
    assert result["unknown"] == 1
    assert store.observations == []


def test_land_dependency_posture_leaves_an_unobserved_application_unknown():
    store = FakeDependencyStore()
    # workload_runtime "none" -- never on kubernetes, nothing this bead could observe.
    app_a = _bead_app("app.a", bead_id="id-a", objects=None)

    result = ea_dependency.land_dependency_posture(
        store, applications=[app_a], units=[], edges=[], now_fn=_now()
    )

    assert result["assessed_none_created"] == 0
    assert result["edges_created"] == 0
    assert result["unknown"] == 1
    assert store.observations == []
    assert store.links == []


def test_land_dependency_posture_closes_assessed_none_observation_once_the_app_gains_an_edge():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    app_b = _bead_app("app.b", bead_id="id-b")
    deployments = {"items": [_deployment(namespace="ns", name="a", containers=[_container("a")])]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    first = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=units, edges=[], now_fn=_now(23)
    )
    assert first["assessed_none_created"] == 1

    edge = ea_dependency.DependencyEdge("id-a", "app.a", "id-b", "app.b", "env: names app.b")
    second = ea_dependency.land_dependency_posture(
        store, applications=[app_a, app_b], units=units, edges=[edge], now_fn=_now(24)
    )

    assert second["edges_created"] == 1
    assert second["assessed_none_closed"] == 1
    [obs] = store.observations
    assert obs["state"] == "resolved"


def test_land_dependency_posture_second_identical_cycle_creates_nothing_new():
    store = FakeDependencyStore()
    app_a = _bead_app("app.a", bead_id="id-a", objects=[_object(namespace="ns", kind="Deployment", name="a")])
    deployments = {"items": [_deployment(namespace="ns", name="a", containers=[_container("a")])]}
    units = ea_dependency.workload_units_from_kubectl(cluster="cluster-a", deployments=deployments, statefulsets=None)

    first = ea_dependency.land_dependency_posture(store, applications=[app_a], units=units, edges=[], now_fn=_now(23))
    second = ea_dependency.land_dependency_posture(store, applications=[app_a], units=units, edges=[], now_fn=_now(24))

    assert first["assessed_none_created"] == 1
    assert second["assessed_none_created"] == 0
    assert second["assessed_none_updated"] == 1
    assert len(store.observations) == 1


# -- reporting: the AC-3 shape -------------------------------------------


def test_summarize_dependency_posture_counts_and_names_the_coherence_case():
    app_a = _bead_app("app.a", bead_id="id-a")
    app_b = _bead_app("app.b", bead_id="id-b")
    app_c = _bead_app("app.c", bead_id="id-c")

    summary = ea_dependency.summarize_dependency_posture(
        applications=[app_a, app_b, app_c],
        edges_out_by_app={"app.a": ["app.b"]},
        assessed_none_refs={"app.b"},
        ci_only_dependency_refs={"app.c"},
    )

    assert summary["total_applications"] == 3
    assert summary["known"] == 1
    assert summary["assessed_none"] == 1
    assert summary["unknown"] == 1
    assert summary["coherence_case_refs"] == ["app.c"]
    # The coherence case is reported separately, but still counts toward
    # unknown -- it is not folded into assessed_none or credited as known.
    assert "app.c" in summary["unknown_refs"]


# -- cross-app parity fixture: the posture definition is computed once, read
# twice (dev.finding 682ac674, part b) ---------------------------------------


def test_land_dependency_posture_classifies_the_committed_cross_app_fixture_exactly():
    """``apps/mcp-hub/tests/test_dependency_posture_fixture.py`` drives
    ``impact._depends_on_posture`` over a byte-identical copy of this same
    fixture (pinned together by
    ``scripts/tests/test_dependency_posture_fixture_drift.py``) -- this is
    the dispatcher side of that parity check. Every bucket asserted below
    (``known_refs``/``assessed_none_refs``/``unknown_refs``/
    ``coherence_case_refs``) is read off ``land_dependency_posture``'s own
    return value (:553-612) -- never re-derived here -- so the fixture's
    ``expected`` section is the only place either classifier's answer is
    written down by hand.
    """
    fixture = json.loads(DEPENDENCY_POSTURE_FIXTURE_PATH.read_text())
    applications = fixture["applications"]
    application_ids = {app["id"] for app in applications}
    ref_by_id = {app["id"]: app["content"]["ref"] for app in applications}

    store = FakeDependencyStore()
    store.observations.extend(fixture["observations"])

    # The fixture's `links` mix two kinds of depends_on edge: app-to-app ones
    # this cycle (re-)computes (fed through `edges`, same as a real
    # `compute_dependency_edges` result -- both ends resolve to an
    # application), and app-to-ci/app-to-service ones a DIFFERENT observer
    # already wrote in a prior cycle (pre-existing `store.links`, the same
    # shape `land_ci_records` produces -- ea_dependency.py never computes a
    # DependencyEdge whose target isn't an application).
    edges: list[ea_dependency.DependencyEdge] = []
    for link in fixture["links"]:
        if link["target_id"] in application_ids:
            edges.append(
                ea_dependency.DependencyEdge(
                    link["source_id"],
                    ref_by_id[link["source_id"]],
                    link["target_id"],
                    link["content"]["target_ref"],
                    link["content"]["method"],
                )
            )
        else:
            store.links.append(dict(link))

    result = ea_dependency.land_dependency_posture(
        store, applications=applications, units=fixture["units"], edges=edges, now_fn=_now()
    )

    expected = fixture["expected"]
    summary = result["posture_summary"]
    assert summary["known_refs"] == sorted(ref for ref, e in expected.items() if e["bucket"] == "known")
    assert summary["assessed_none_refs"] == sorted(
        ref for ref, e in expected.items() if e["bucket"] == "assessed_none"
    )
    assert summary["unknown_refs"] == sorted(ref for ref, e in expected.items() if e["bucket"] == "unknown")
    assert summary["coherence_case_refs"] == sorted(ref for ref, e in expected.items() if e["coherence_case"])


# -- collection: raw kubectl JSON, blindness guard ----------------------------


def test_collect_dependency_cluster_facts_refuses_without_kubeconfig(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)

    with pytest.raises(RuntimeError):
        ea_dependency.collect_dependency_cluster_facts(
            runner=lambda *a: (_ for _ in ()).throw(AssertionError("kubectl must never run without KUBECONFIG"))
        )


def test_collect_dependency_cluster_facts_raises_when_both_workload_types_are_none(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/example/kubeconfig")

    with pytest.raises(ea_dependency.MissingKubectlOutputError):
        ea_dependency.collect_dependency_cluster_facts(runner=lambda *a: None)


def test_collect_dependency_cluster_facts_tolerates_a_missing_networkpolicy_resource(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/example/kubeconfig")

    def runner(kind, *args):
        if kind == "deployments":
            return {"items": []}
        return None

    facts = ea_dependency.collect_dependency_cluster_facts(runner=runner)

    assert facts["deployments"] == {"items": []}
    assert facts["networkpolicies"] is None


def test_no_dependency_facts_returns_empty_shape_and_touches_nothing():
    facts = ea_dependency.no_dependency_facts()

    assert facts == {"cluster": "", "deployments": None, "statefulsets": None, "networkpolicies": None}


# -- PRIN-011: no manifest is ever read ---------------------------------------


def test_ea_dependency_module_has_no_code_path_that_reads_repository_manifests():
    import inspect

    source = inspect.getsource(ea_dependency)

    assert "open(" not in source
    assert ".read_text(" not in source
    assert "yaml" not in source.lower()
