"""Idempotent landing tests for the scheduled EA observation run (PC-ASR-006).

No Temporal server, no substrate database, no cluster, no network. The
`FakeEAObserverStore` below rejects what the live substrate rejects by
construction -- it validates `arch.observation` content with the real
`ArchObservationContent` model and checks link types against the real
`BEAD_LINK_TYPES`, both imported directly from `apps/substrate/src/schemas.py`
(the `test_knowledge_ingestion.py` pattern), never hand-copied. Cluster reads
are always injected as already-parsed data, matching the split
`cluster_health.py` already uses for the same testability reason -- `kubectl`
itself is never invoked from these tests.
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
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "substrate"))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "mcp-hub" / "src"))

import ea_reflect  # noqa: E402
from activities import ea_dependency  # noqa: E402
from activities import ea_observation as eo  # noqa: E402
from src.schemas import (  # noqa: E402
    ArchCiContent,
    ArchObservationContent,
    BEAD_LINK_TYPES,
    check_source_class_admission,
    check_source_class_ownership,
)
from tools import notify  # noqa: E402

# The fake's link vocabulary IS the live one, with no gap -- a double that
# admits more than the live contract is a second implementation.
FAKE_LINK_TYPES = BEAD_LINK_TYPES

EA_OBSERVATION_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ea_observation"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((EA_OBSERVATION_FIXTURES / name).read_text())


def _k8s_obj(**over: Any) -> dict[str, Any]:
    obj = {
        "cluster": "cluster-a",
        "namespace": "platform-core",
        "kind": "Deployment",
        "name": "x",
        "manifest": "infrastructure/k8s/platform-core/x.yaml",
        "managed_by": "argocd",
    }
    obj.update(over)
    return obj


def _workload(*objects: Any) -> dict[str, Any]:
    return {"runtime": "kubernetes", "objects": list(objects)}


def _external_binding(**over: Any) -> dict[str, Any]:
    binding = {
        "host": "host-b",
        "compose_path": "infrastructure/docker/host-b/docker-compose.pihole.yml",
        "service": "pihole",
    }
    binding.update(over)
    return {"runtime": "external", "binding": binding}


def _app(
    ref: str = "app.x", state: str = "operate", workload: dict | None = None, bead_id: str | None = None, **content: Any
) -> dict[str, Any]:
    base = {
        "name": "X",
        "description": "d",
        "build": "oss",
        "layer": "supply",
        "owner": "grant",
        "business_value": "low",
        "technical_health": "healthy",
        "time_disposition": "tolerate",
        "workload": workload if workload is not None else {"runtime": "none", "note": "none"},
    }
    base.update(content)
    return {"id": bead_id or f"bead-{ref}", "ref": ref, "state": state, "content": base}


def _obs(**over: Any) -> dict[str, Any]:
    item = {
        "cluster": "cluster-a",
        "namespace": "platform-core",
        "kind": "Deployment",
        "name": "x",
        "ready_replicas": 1,
        "replicas": 1,
        "owning_argocd_application": "platform-core",
    }
    item.update(over)
    return item


def _now(day: int = 23) -> Any:
    return lambda: datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc)


class FakeEAObserverStore:
    """In-memory substrate double, honest about what the live one rejects."""

    def __init__(self) -> None:
        self.applications: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.cis: list[dict[str, Any]] = []
        self._next_id = 1

    def _fresh_id(self, prefix: str) -> str:
        bead_id = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return bead_id

    def list_applications(self) -> list[dict[str, Any]]:
        return [dict(app) for app in self.applications]

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        for bead in self.observations:
            if (bead.get("content") or {}).get("ref") == ref:
                return dict(bead)
        return None

    def list_active_findings(self) -> list[dict[str, Any]]:
        return [dict(bead) for bead in self.observations if bead.get("state") == "active"]

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["state"] == "active"
        # AC-7(a2): every arch.observation bead written through this store
        # -- eo.CREATED_BY for findings (land_findings) AND for
        # dependency-assessment observations (land_dependency_posture,
        # which deliberately writes under ea_observation.CREATED_BY, not its
        # own ea_dependency.CREATED_BY -- SOURCE_CLASS_WRITERS["observed"]
        # does not enroll the latter) -- is checked against the real
        # enrollment map, exactly as POST /beads does. A double that admits
        # a writer the live contract refuses is a second, more permissive
        # implementation of the same contract.
        declared_class = (payload["content"] or {}).get("source_class", "authored")
        violation = check_source_class_admission(declared_class, payload["created_by"])
        if violation is not None:
            raise AssertionError(
                f"source_class_admission_violation: {payload['created_by']!r} is not "
                f"enrolled for declared_class {violation.owning_class!r} "
                "(apps/substrate/src/bead_rules.py:SOURCE_CLASS_WRITERS)"
            )
        # Rejects malformed content exactly as POST /beads does: the real
        # pydantic model, not a hand-rolled check that can drift from it.
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
                # SubstrateEAObserverStore.update_observation hard-codes the
                # PATCH body's created_by to eo.CREATED_BY regardless of
                # caller -- every module that calls update_observation on
                # this shared store shares that one writer identity.
                writer = eo.CREATED_BY
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

    def create_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str,
        created_by: str,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = link_type.strip().lower()
        if normalized not in FAKE_LINK_TYPES:
            allowed = ", ".join(sorted(FAKE_LINK_TYPES))
            raise ValueError(f"link_type must be one of: {allowed}")
        link = {
            "id": self._fresh_id("link"),
            "source_id": source_id,
            "target_id": target_id,
            "link_type": normalized,
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

    # -- arch.ci: the technology layer ----------------------------------

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

    def find_ci(self, ref: str) -> dict[str, Any] | None:
        for bead in self.cis:
            if (bead.get("content") or {}).get("ref") == ref:
                return dict(bead)
        return None

    def list_active_cis(self) -> list[dict[str, Any]]:
        return [dict(bead) for bead in self.cis if bead.get("state") == "active"]

    def create_ci(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["namespace"] == "arch"
        assert payload["type"] == "ci"
        assert payload["state"] == "active"
        assert payload["created_by"] == eo.CREATED_BY
        # Rejects malformed content exactly as POST /beads does: the real
        # pydantic model, not a hand-rolled check that can drift from it.
        ArchCiContent.model_validate(payload["content"])
        bead = {"id": self._fresh_id("ci"), **payload}
        self.cis.append(bead)
        return dict(bead)

    def update_ci(
        self, bead_id: str, *, content: dict[str, Any] | None = None, state: str | None = None
    ) -> dict[str, Any]:
        for bead in self.cis:
            if bead["id"] == bead_id:
                if content is not None:
                    ArchCiContent.model_validate(content)
                    bead["content"] = content
                if state is not None:
                    bead["state"] = state
                return dict(bead)
        raise AssertionError(f"no such ci {bead_id}")


# -- landing: creation + the measures edge ------------------------------------


def test_finding_lands_as_dated_observation_with_measures_edge_to_its_application():
    store = FakeEAObserverStore()
    model = [
        _app("app.live", state="operate", workload=_workload(_k8s_obj()), bead_id="bead-app-live")
    ]
    findings = ea_reflect.reflect({"workloads": []}, model)

    result = eo.land_findings(store, findings, applications=model, now_fn=_now())

    assert result == {
        "status": "observed",
        "checked": 1,
        "created": 1,
        "updated": 0,
        "closed": 0,
        "skipped_external": 0,
        "unresolved_application_refs": [],
    }
    assert len(store.observations) == 1
    bead = store.observations[0]
    assert bead["state"] == "active"
    assert bead["content"]["observed_at"] == "2026-08-23T12:00:00Z"
    assert bead["content"]["ref"].startswith("obs.ea-reflect.")
    assert bead["content"]["source_class"] == "observed"
    [link] = store.links
    assert link["source_id"] == bead["id"]
    assert link["target_id"] == "bead-app-live"
    assert link["link_type"] == "measures"


def test_measures_edge_resolves_application_content_ref_to_the_applications_bead_uuid():
    """Exact shape of the 2026-08-27 nightly failure: a finding carrying
    ``application_ref`` ``'app.reloader'`` against an application bead
    holding that content ref. ``BeadLinkCreate.target_id`` is a UUID4 and
    rejects the ref string with 422 -- this asserts the link goes out with
    the bead's real uuid instead, and fails against today's behaviour, which
    sends the ref string as ``target_id``."""
    store = FakeEAObserverStore()
    bead_uuid = "2b6b6fff-9973-4f65-ae99-230838883931"
    model = [
        _app(
            "app.reloader",
            state="operate",
            workload=_workload(_k8s_obj(name="reloader")),
            bead_id=bead_uuid,
        )
    ]
    findings = ea_reflect.reflect({"workloads": []}, model)

    result = eo.land_findings(store, findings, applications=model, now_fn=_now())

    assert result["unresolved_application_refs"] == []
    [link] = store.links
    assert link["target_id"] == bead_uuid
    assert link["target_id"] != "app.reloader"


def test_unresolvable_application_ref_is_reported_and_does_not_abandon_the_rest_of_the_pass():
    store = FakeEAObserverStore()
    resolvable = _app(
        "app.live", state="operate", workload=_workload(_k8s_obj(name="live")), bead_id="bead-app-live"
    )
    unresolvable = _app("app.ghost", state="operate", workload=_workload(_k8s_obj(name="ghost")))
    findings = ea_reflect.reflect({"workloads": []}, [resolvable, unresolvable])

    # The resolution list omits app.ghost's bead entirely, as if its
    # content ref could not be resolved to any known application bead.
    result = eo.land_findings(store, findings, applications=[resolvable], now_fn=_now())

    assert result["created"] == 2
    assert len(store.observations) == 2
    assert result["unresolved_application_refs"] == ["app.ghost"]
    [link] = store.links
    assert link["target_id"] == "bead-app-live"


def test_unmodelled_workload_lands_with_no_measures_edge():
    store = FakeEAObserverStore()
    model = [_app("app.claimed", state="operate", workload=_workload(_k8s_obj()))]
    observations = {
        "workloads": [
            _obs(name="x"),
            _obs(namespace="infra-ai", kind="Deployment", name="surprise", ready_replicas=1),
        ]
    }
    findings = ea_reflect.reflect(observations, model)

    result = eo.land_findings(store, findings, applications=model, now_fn=_now())

    assert result["created"] == 1
    assert len(store.observations) == 1
    assert store.links == []


def test_external_declared_binding_is_counted_but_never_landed_or_probed():
    store = FakeEAObserverStore()
    model = [_app("app.pihole", state="operate", workload=_external_binding())]
    findings = ea_reflect.reflect({"workloads": []}, model)

    result = eo.land_findings(store, findings, applications=model, now_fn=_now())

    assert result == {
        "status": "observed",
        "checked": 1,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "skipped_external": 1,
        "unresolved_application_refs": [],
    }
    assert store.observations == []
    assert store.links == []


# -- idempotence: update, not duplicate (PRIN-014) ----------------------------


def test_persisting_divergence_across_two_runs_updates_not_duplicates():
    store = FakeEAObserverStore()
    model = [
        _app("app.live", state="operate", workload=_workload(_k8s_obj()), bead_id="bead-app-live")
    ]
    findings = ea_reflect.reflect({"workloads": []}, model)

    first = eo.land_findings(store, findings, applications=model, now_fn=_now(23))
    second = eo.land_findings(store, findings, applications=model, now_fn=_now(24))

    assert first["created"] == 1 and first["updated"] == 0
    assert second["created"] == 0 and second["updated"] == 1
    assert len(store.observations) == 1
    assert store.observations[0]["content"]["observed_at"] == "2026-08-24T12:00:00Z"
    assert len(store.links) == 1


def test_persisting_divergence_updates_even_when_its_own_reason_field_changes():
    """absent -> zero-ready is the same ongoing divergence, not a new one."""
    store = FakeEAObserverStore()
    model = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    absent = ea_reflect.reflect({"workloads": []}, model)
    zero_ready = ea_reflect.reflect({"workloads": [_obs(ready_replicas=0)]}, model)

    first = eo.land_findings(store, absent, applications=model, now_fn=_now(23))
    second = eo.land_findings(store, zero_ready, applications=model, now_fn=_now(24))

    assert first["created"] == 1
    assert second["created"] == 0 and second["updated"] == 1
    assert len(store.observations) == 1


def test_divergence_that_resolves_closes_the_record_and_reopens_if_it_recurs():
    store = FakeEAObserverStore()
    model = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]
    diverging = ea_reflect.reflect({"workloads": []}, model)

    first = eo.land_findings(store, diverging, applications=model, now_fn=_now(23))
    assert first["created"] == 1
    bead_id = store.observations[0]["id"]

    second = eo.land_findings(store, [], applications=model, now_fn=_now(24))
    assert second["closed"] == 1
    assert store.observations[0]["state"] == "resolved"
    assert store.observations[0]["id"] == bead_id  # same record, not archived away

    third = eo.land_findings(store, diverging, applications=model, now_fn=_now(25))
    assert third["created"] == 0 and third["updated"] == 1
    assert store.observations[0]["state"] == "active"
    assert store.observations[0]["id"] == bead_id
    assert len(store.observations) == 1
    # Reopening must not mint a second measures edge onto the same bead.
    assert len(store.links) == 1


# -- lifecycle stays human -----------------------------------------------------


def test_observer_store_protocol_exposes_no_application_write_method():
    declared = {name for name in vars(eo.EAObserverStore) if not name.startswith("_")}
    assert declared == {
        "list_applications",
        "find_observation",
        "list_active_findings",
        "create_observation",
        "update_observation",
        "create_link",
    }


# -- observe_ea_model wiring: no Temporal, no substrate DB, no cluster, no network


def test_observe_ea_model_wires_collection_and_judgment_without_network_or_cluster():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["created"] == 1
    # The finding plus the observer's own standing status bead (R2605-8).
    assert len(store.observations) == 2
    assert result["ci"] == {
        "checked": 0,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 0,
        "unattributed": 0,
    }
    assert store.cis == []


def test_observe_ea_model_resolves_measures_edge_from_raw_substrate_application_beads():
    """End-to-end through ``observe_ea_model``: ``store.list_applications()``
    returns the real substrate bead shape (content ``ref`` nested, no
    top-level ``ref`` -- see ``BeadRead`` in apps/substrate/src/schemas.py),
    the same shape the live observer reads. The measures edge must land with
    the bead's real id, not the content ref -- this is the exact pipeline
    that produced the 2026-08-27 outage."""
    store = FakeEAObserverStore()
    bead_uuid = "2b6b6fff-9973-4f65-ae99-230838883931"
    store.applications = [
        {
            "id": bead_uuid,
            "state": "operate",
            "content": {
                "ref": "app.reloader",
                "workload": _workload(_k8s_obj(name="reloader")),
            },
        }
    ]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["created"] == 1
    assert result["unresolved_application_refs"] == []
    [link] = store.links
    assert link["target_id"] == bead_uuid
    assert link["link_type"] == "measures"


def test_observe_ea_model_defaults_wire_the_real_collector_and_judgment():
    """The production defaults are the live kubectl collector and the live
    reflector -- tests never call them directly (they'd need a cluster), but
    the wiring itself is asserted here without invoking either."""
    import inspect

    parameters = inspect.signature(eo.observe_ea_model).parameters
    assert parameters["collect_observations_fn"].default is eo.collect_cluster_observations
    assert parameters["collect_namespaces_fn"].default is eo.collect_cluster_namespaces
    assert parameters["reflect_fn"].default is ea_reflect.reflect


def test_observe_ea_model_dependency_facts_default_touches_no_cluster():
    """Unlike the two kubectl collectors above, ``collect_dependency_facts_fn``
    defaults to the safe no-op -- every ``observe_ea_model`` test predating
    this module injects no override for it, and a live-kubectl default here
    would make every one of them shell out to kubectl. Production wiring
    passes the real collector explicitly at ``observe_ea_model_activity``
    instead. See .factory/design.md."""
    import inspect

    parameters = inspect.signature(eo.observe_ea_model).parameters
    assert parameters["collect_dependency_facts_fn"].default is ea_dependency.no_dependency_facts


def test_observe_ea_model_activity_wires_the_real_dependency_collector(monkeypatch):
    """The claim the previous test's docstring makes -- that production
    wiring passes the real collector explicitly at
    ``observe_ea_model_activity`` -- was false until this bead: the activity
    called ``observe_ea_model(default_store())`` with no override, so the
    safe no-op default ran in production and no dependency edge was ever
    computed there. Pin the actual call so this cannot silently regress
    back to the safe default."""
    captured: dict[str, Any] = {}

    def fake_observe_ea_model(store: Any, **kwargs: Any) -> dict[str, Any]:
        captured["store"] = store
        captured.update(kwargs)
        return {}

    sentinel_store = object()
    monkeypatch.setattr(eo, "default_store", lambda: sentinel_store)
    monkeypatch.setattr(eo, "observe_ea_model", fake_observe_ea_model)

    eo.observe_ea_model_activity({})

    assert captured["store"] is sentinel_store
    assert captured["collect_dependency_facts_fn"] is ea_dependency.collect_dependency_cluster_facts


# -- collection: pure translation from already-parsed kubectl JSON -----------


def test_workload_observations_from_kubectl_attributes_argocd_owner_by_resource_identity():
    deployments = {
        "items": [
            {
                "metadata": {"namespace": "platform-core", "name": "x"},
                "status": {"replicas": 1, "readyReplicas": 1},
            }
        ]
    }
    applications = {
        "items": [
            {
                "metadata": {"name": "platform-core"},
                "status": {
                    "resources": [
                        {"namespace": "platform-core", "kind": "Deployment", "name": "x"}
                    ]
                },
            }
        ]
    }

    observations = eo.workload_observations_from_kubectl(
        cluster="cluster-a",
        deployments=deployments,
        statefulsets=None,
        applications=applications,
    )

    assert observations == [
        {
            "cluster": "cluster-a",
            "namespace": "platform-core",
            "kind": "Deployment",
            "name": "x",
            "replicas": 1,
            "ready_replicas": 1,
            "owning_argocd_application": "platform-core",
        }
    ]


def test_workload_observations_from_kubectl_reports_missing_owner_as_none():
    deployments = {
        "items": [{"metadata": {"namespace": "platform-core", "name": "goose"}, "status": {}}]
    }

    observations = eo.workload_observations_from_kubectl(
        cluster="cluster-a", deployments=deployments, statefulsets=None, applications=None
    )

    assert observations[0]["owning_argocd_application"] is None


# -- the technology layer: arch.ci (PC-ASR-007) -------------------------------
#
# At HEAD before this change, none of ``ci_records_from_observations``,
# ``ci_records_from_namespaces``, ``land_ci_records``, ``eo.CIObserverStore``
# exist, and ``arch.ci`` is not a registered substrate type -- every test
# below fails against today's behaviour.


def test_namespace_names_from_kubectl_is_a_pure_translation():
    payload = {"items": [{"metadata": {"name": "platform-core"}}, {"metadata": {"name": "infra-ai"}}]}

    assert eo._namespace_names_from_kubectl(payload) == ["platform-core", "infra-ai"]


def test_namespace_names_from_kubectl_handles_missing_payload():
    assert eo._namespace_names_from_kubectl(None) == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("substrate", "workload"),
        ("postgres", "database"),
        ("app-postgresql-primary", "database"),
        ("redis-cache", "database"),
        ("mysql-0", "database"),
        ("adbot", "workload"),  # "db" alone is not a marker -- too broad
    ],
)
def test_ci_kind_for_workload_classifies_by_name_marker(name, expected):
    assert eo._ci_kind_for_workload(name) == expected


def test_ci_records_from_observations_lands_both_owner_matched_and_unattributed_objects():
    observations = [
        _obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x"),
        _obs(cluster="cluster-a", namespace="infra-ai", kind="Deployment", name="surprise"),
    ]
    applications = [_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))]

    records = eo.ci_records_from_observations(observations=observations, applications=applications)

    assert len(records) == 2
    matched, unattributed = records
    assert matched["cluster"] == "cluster-a"
    assert matched["namespace"] == "platform-core"
    assert matched["kind"] == "Deployment"
    assert matched["name"] == "x"
    assert matched["ci_kind"] == "workload"
    assert matched["owner_ids"] == ["bead-app-live"]
    assert matched["ref"] == "ci.cluster-a.platform-core.deployment.x"

    assert unattributed["namespace"] == "infra-ai"
    assert unattributed["name"] == "surprise"
    assert unattributed["owner_ids"] == []


def test_ci_records_from_observations_classifies_database_workloads():
    observations = [_obs(cluster="cluster-a", namespace="platform-core", kind="StatefulSet", name="postgres")]
    applications = [
        _app(
            "app.live",
            bead_id="bead-app-live",
            workload=_workload(_k8s_obj(kind="StatefulSet", name="postgres")),
        )
    ]

    records = eo.ci_records_from_observations(observations=observations, applications=applications)

    assert records[0]["ci_kind"] == "database"


def test_ci_records_from_namespaces_fans_out_to_every_owning_application():
    applications = [
        _app("app.a", bead_id="bead-a", workload=_workload(_k8s_obj(name="a"))),
        _app("app.b", bead_id="bead-b", workload=_workload(_k8s_obj(name="b"))),
    ]

    records = eo.ci_records_from_namespaces(
        cluster="cluster-a", namespace_names=["platform-core"], applications=applications
    )

    assert len(records) == 1
    record = records[0]
    assert record["ci_kind"] == "namespace"
    assert record["namespace"] == "platform-core"
    assert record["kind"] == "Namespace"
    assert record["owner_ids"] == ["bead-a", "bead-b"]


def test_ci_records_from_namespaces_skips_namespace_with_no_declared_presence():
    applications = [_app("app.a", bead_id="bead-a", workload=_workload(_k8s_obj(namespace="platform-core")))]

    records = eo.ci_records_from_namespaces(
        cluster="cluster-a", namespace_names=["platform-core", "kube-system"], applications=applications
    )

    assert [r["namespace"] for r in records] == ["platform-core"]


def test_ci_records_from_namespaces_ignores_a_different_cluster():
    applications = [_app("app.a", bead_id="bead-a", workload=_workload(_k8s_obj(cluster="other-cluster")))]

    records = eo.ci_records_from_namespaces(
        cluster="cluster-a", namespace_names=["platform-core"], applications=applications
    )

    assert records == []


# -- landing: creation, the depends_on edge, source_class -----------------


def test_ci_record_lands_as_active_observed_ci_with_depends_on_edge_from_owner():
    store = FakeEAObserverStore()
    records = eo.ci_records_from_observations(
        observations=[_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")],
        applications=[_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))],
    )

    result = eo.land_ci_records(store, records)

    assert result == {
        "checked": 1,
        "created": 1,
        "updated": 0,
        "closed": 0,
        "linked": 1,
        "attributed": 1,
        "unattributed": 0,
    }
    [bead] = store.cis
    assert bead["state"] == "active"
    assert bead["content"]["source_class"] == "observed"
    assert bead["content"]["ci_kind"] == "workload"
    [link] = store.links
    assert link["source_id"] == "bead-app-live"
    assert link["target_id"] == bead["id"]
    assert link["link_type"] == "depends_on"


def test_ci_landing_second_identical_cycle_writes_nothing():
    store = FakeEAObserverStore()
    records = eo.ci_records_from_observations(
        observations=[_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")],
        applications=[_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))],
    )

    first = eo.land_ci_records(store, records)
    second = eo.land_ci_records(store, records)

    assert first["created"] == 1
    assert second == {
        "checked": 1,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 1,
        "unattributed": 0,
    }
    assert len(store.cis) == 1
    assert len(store.links) == 1


def test_ci_landing_owner_change_creates_the_new_edge_without_a_content_write():
    """A second application claims the same already-landed object on the next
    cycle. ``ci_kind``/``cluster``/``namespace``/``kind``/``name`` are
    identical either way -- the owner list isn't part of stored content (it
    drives the edge, not the fact) -- so this is correctly a content no-op.
    But the new owner has no edge yet, and F1's fix is precisely that a
    content no-op must not also mean an edge no-op: the missing
    ``depends_on`` edge is still created, and the old owner's edge is left
    alone (removal is not this bead's job)."""
    store = FakeEAObserverStore()
    app_a = _app("app.a", bead_id="bead-a", workload=_workload(_k8s_obj()))
    app_b = _app("app.b", bead_id="bead-b", workload=_workload(_k8s_obj()))

    first = eo.land_ci_records(
        store,
        eo.ci_records_from_observations(
            observations=[_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")],
            applications=[app_a],
        ),
    )
    second = eo.land_ci_records(
        store,
        eo.ci_records_from_observations(
            observations=[_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")],
            applications=[app_b],
        ),
    )

    assert first["created"] == 1
    assert second == {
        "checked": 1,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 1,
        "attributed": 1,
        "unattributed": 0,
    }
    assert len(store.cis) == 1
    [bead] = store.cis
    link_targets = {(link["source_id"], link["target_id"]) for link in store.links}
    assert link_targets == {("bead-a", bead["id"]), ("bead-b", bead["id"])}


def test_ci_landing_closes_record_on_vanish_and_reopens_without_a_second_edge():
    store = FakeEAObserverStore()
    application = _app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))
    observed = [_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")]

    first = eo.land_ci_records(
        store, eo.ci_records_from_observations(observations=observed, applications=[application])
    )
    assert first["created"] == 1
    bead_id = store.cis[0]["id"]

    second = eo.land_ci_records(
        store, eo.ci_records_from_observations(observations=[], applications=[application])
    )
    assert second["closed"] == 1
    assert store.cis[0]["state"] == "resolved"
    assert store.cis[0]["id"] == bead_id

    third = eo.land_ci_records(
        store, eo.ci_records_from_observations(observations=observed, applications=[application])
    )
    assert third["created"] == 0
    assert store.cis[0]["state"] == "active"
    assert store.cis[0]["id"] == bead_id
    assert len(store.cis) == 1
    # Reopening must not mint a second depends_on edge onto the same bead.
    assert len(store.links) == 1


def test_ci_landing_zero_records_and_nothing_active_is_zero_writes():
    store = FakeEAObserverStore()

    result = eo.land_ci_records(store, [])

    assert result == {
        "checked": 0,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 0,
        "unattributed": 0,
    }
    assert store.cis == []


def test_ci_observer_store_protocol_exposes_only_the_six_ci_methods():
    declared = {name for name in vars(eo.CIObserverStore) if not name.startswith("_")}
    assert declared == {
        "find_ci",
        "list_active_cis",
        "create_ci",
        "update_ci",
        "create_link",
        "list_links",
    }


def test_ea_observation_create_link_requires_created_by_with_no_default():
    """#758-class fix: a header-less link write must be unrepresentable at
    this seam. Both Protocols and the implementation declare `created_by`
    with no default, so a caller cannot satisfy any of them by omission."""
    import inspect

    for callable_ in (
        eo.EAObserverStore.create_link,
        eo.CIObserverStore.create_link,
        eo.SubstrateEAObserverStore.create_link,
    ):
        params = inspect.signature(callable_).parameters
        assert "created_by" in params
        assert params["created_by"].default is inspect.Parameter.empty


def test_measures_and_depends_on_link_writes_carry_the_true_writer_identity():
    """The two live link writers named by #729's gate landed edges attributed
    'unknown' because they never passed X-Created-By. This asserts the seam
    for both edge kinds this module writes: `measures` (land_findings) and
    `depends_on` (land_ci_records) must both carry eo.CREATED_BY."""
    store = FakeEAObserverStore()
    model = [_app("app.live", state="operate", workload=_workload(_k8s_obj()), bead_id="bead-app-live")]
    findings = ea_reflect.reflect({"workloads": []}, model)
    eo.land_findings(store, findings, applications=model, now_fn=_now())

    ci_records = eo.ci_records_from_observations(
        observations=[_obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")],
        applications=[_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))],
    )
    eo.land_ci_records(store, ci_records)

    assert store.links
    for link in store.links:
        assert link["created_by"] == eo.CREATED_BY


def test_ea_observation_live_store_create_link_sends_created_by_as_header(monkeypatch):
    """HTTP-seam pin: SubstrateEAObserverStore.create_link must forward its
    `created_by` argument as the `X-Created-By` header on the live request,
    since POST /beads/{id}/links reads attribution from the header, not the
    JSON body."""
    import httpx

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "link-1"}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "request", fake_request)

    store = eo.SubstrateEAObserverStore()
    store.create_link("src-1", "tgt-1", "measures", "someone-real")

    assert captured["headers"]["X-Created-By"] == "someone-real"
    assert captured["json"] == {"target_id": "tgt-1", "link_type": "measures"}


# -- dev.finding 5c3cf2b3: list_active_findings must walk every page --------
#
# The 2026-09-24 live shape: 1,448 active arch.observation beads, the
# observer's own 52 ea_reflect_finding beads sitting entirely beyond the
# first 500-row page. Reduced here to a 1,200-row population (three pages at
# the real ACTIVE_OBSERVATION_PAGE_SIZE) with the 52 straddling pages two and
# three, so a walk that stops one page early still comes up short.


class _JsonResponse:
    """The same two-method response double every ``fake_request`` in this
    file already uses (e.g. :func:`test_ea_observation_live_store_create_link_sends_created_by_as_header`'s
    ``_Resp``) -- neither ``raise_for_status`` nor ``json`` is a BeadStore
    protocol name, so pulling it out to a shared helper does not create a
    new hand-rolled store double."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._payload


ACTIVE_OBSERVATION_POPULATION_SIZE = 1200
ACTIVE_FINDING_START_INDEX = 980
ACTIVE_FINDING_COUNT = 52  # page two is [500, 1000), page three is [1000, 1200)


def _seed_active_observation_population() -> list[dict[str, Any]]:
    """1,200 active ``arch.observation`` beads of mixed ``observation_kind``,
    with exactly :data:`ACTIVE_FINDING_COUNT` ``ea_reflect_finding`` beads at
    indices 980-1031 -- 20 on page two, 32 on page three, matching the bead's
    'pages two and three only' shape. Every other bead carries a real sibling
    kind (``ea_dependency``'s own ``arch.observation`` writer) so the
    population is genuinely mixed, not a monoculture the filter can't fail
    against."""
    population: list[dict[str, Any]] = []
    for i in range(ACTIVE_OBSERVATION_POPULATION_SIZE):
        is_finding = ACTIVE_FINDING_START_INDEX <= i < ACTIVE_FINDING_START_INDEX + ACTIVE_FINDING_COUNT
        kind = (
            eo.OBSERVATION_KIND
            if is_finding
            else ea_dependency.DEPENDENCY_ASSESSMENT_OBSERVATION_KIND
        )
        population.append(
            {
                "id": f"obs-seed-{i}",
                "state": "active",
                "created_by": eo.CREATED_BY,
                "content": {"ref": f"obs.seed.{i}", "source_class": "observed"},
                "context": {"observation_kind": kind},
            }
        )
    return population


def _expected_active_finding_refs() -> set[str]:
    return {
        f"obs.seed.{i}"
        for i in range(ACTIVE_FINDING_START_INDEX, ACTIVE_FINDING_START_INDEX + ACTIVE_FINDING_COUNT)
    }


# Mirrors apps/substrate/src/routes.py's _LIST_BEADS_QUERY_PARAMS (:806-818),
# hand-copied rather than imported: importing routes.py pulls in database.py,
# which raises RuntimeError at import time unless DATABASE_URL is already
# set, so there is no import-side-effect-free way to share the real value.
_LIVE_LIST_BEADS_QUERY_PARAMS = frozenset(
    {
        "namespace",
        "type",
        "state",
        "trust_tier",
        "parent_id",
        "created_after",
        "content_ref",
        "limit",
        "offset",
    }
)


def _raise_as_the_live_route_would(url: str, params: dict[str, Any], detail: str) -> None:
    """Raise a genuine ``httpx.HTTPStatusError`` -- the same exception type
    ``SubstrateEAObserverStore._request``'s own ``response.raise_for_status()``
    raises against a real 4xx -- rather than a hand-rolled assertion, so a
    caller handling the live shape and a caller handling this fake's shape
    are handling the same thing."""
    request = eo.httpx.Request("GET", url, params=params)
    eo.httpx.Response(400, json={"error": detail}, request=request).raise_for_status()


def _reject_as_the_live_route_would(url: str, params: dict[str, Any]) -> None:
    """``GET /beads`` validation this fake must mirror after the PR 1138
    release gate found it accepting what the live route rejects: a query
    parameter outside :data:`_LIVE_LIST_BEADS_QUERY_PARAMS` (routes.py's
    ``_reject_unknown_list_beads_params``, :821-832), or a non-int or
    negative ``limit``/``offset`` (both are ``int``-typed, unsigned paging
    params on the live route)."""
    unknown = sorted(set(params) - _LIVE_LIST_BEADS_QUERY_PARAMS)
    if unknown:
        _raise_as_the_live_route_would(url, params, f"unknown_query_parameter: {unknown}")
    for key in ("limit", "offset"):
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _raise_as_the_live_route_would(url, params, f"invalid {key}: {value!r}")


def _active_observation_fake_request(
    population: list[dict[str, Any]],
    *,
    ignore_offset: bool = False,
    cap_page_at: int | None = None,
    resolved_ids: list[str] | None = None,
    get_calls: list[tuple[int, int]] | None = None,
):
    """A fake ``httpx.request`` transport serving ``GET /beads`` over
    ``population`` and ``PATCH /beads/{id}`` (the close pass's resolve
    writes), honouring ``limit``/``offset`` by default. ``ignore_offset``
    reproduces a server that always answers from the start regardless of the
    ``offset`` sent (the pathological case the first-bead-id check must
    catch); ``cap_page_at`` reproduces a server that answers with fewer rows
    than ``limit`` requested while more remain (the case this walk's
    previous-page-length exhaustion check must not mistake for the end).
    ``get_calls``, when given, records each GET's ``(limit, offset)`` exactly
    as the client sent it (``offset`` defaulting to 0 when the client omitted
    the key, as the walk's own first call does) -- the pin the PR 1138
    release gate asked for, that the walk actually pages rather than merely
    returning the right rows by coincidence of ``population`` layout. Every
    GET is validated the way the live route validates it
    (:func:`_reject_as_the_live_route_would`) before it is served."""
    resolved = resolved_ids if resolved_ids is not None else []

    def fake_request(method, url, **kwargs):
        if method == "GET":
            params = kwargs.get("params") or {}
            _reject_as_the_live_route_would(url, params)
            assert params["namespace"] == "arch"
            assert params["type"] == "observation"
            assert params["state"] == "active"
            limit = params["limit"]
            requested_offset = params.get("offset", 0)
            if get_calls is not None:
                get_calls.append((limit, requested_offset))
            offset = 0 if ignore_offset else requested_offset
            page = population[offset : offset + limit]
            if cap_page_at is not None:
                page = page[:cap_page_at]
            return _JsonResponse(page)
        if method == "PATCH":
            bead_id = url.rsplit("/", 1)[-1]
            body = kwargs.get("json") or {}
            if body.get("state") == "resolved":
                resolved.append(bead_id)
            return _JsonResponse({"id": bead_id, **body})
        raise AssertionError(
            f"unexpected method {method!r} for {url!r} in the active-observation fake transport"
        )

    return fake_request


def _live_store(monkeypatch, fake_request) -> "eo.SubstrateEAObserverStore":
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(eo.httpx, "request", fake_request)
    return eo.SubstrateEAObserverStore()


def test_list_active_findings_pages_through_the_whole_active_population(monkeypatch):
    """dev.finding 5c3cf2b3: the prior single GET (limit=500, no offset)
    returned only page one, where none of the 52 findings lived. The walk
    must reach pages two and three before filtering on observation_kind.

    PR 1138's release gate held on this: the result-set assertion alone
    passes even if the walk pages wrong (e.g. a larger page size that
    happens to cover the same rows in one GET), because nothing pinned the
    GETs themselves. Asserting the exact ``(limit, offset)`` sequence closes
    that: a page size of 600 or 2000 over this 1,200-row population would
    change the number of GETs and their offsets, not just coincidentally
    return the same findings.
    """
    population = _seed_active_observation_population()
    get_calls: list[tuple[int, int]] = []
    store = _live_store(monkeypatch, _active_observation_fake_request(population, get_calls=get_calls))

    found = store.list_active_findings()

    assert {bead["content"]["ref"] for bead in found} == _expected_active_finding_refs()
    assert get_calls == [(500, 0), (500, 500), (500, 1000)]


def test_list_active_findings_detects_a_server_that_ignores_offset(monkeypatch):
    """A server that answers every page from the start regardless of
    ``offset`` would otherwise hand back the same full page forever. The
    first-bead-id repeat check must raise before that loop ever starts,
    naming the offset.

    PR 1138's release gate held on this: ``match="offset"`` alone also
    matches the page-count-exhaustion error's message (it names the offset
    of the page it gave up on too), so deleting the first-id check entirely
    left this test green against the *other* bound. Matching the specific
    wording of the first-id-repeat message, and pinning the walk to exactly
    two GETs before it raises, closes that gap. The second half -- running
    ``land_findings`` over the same offset-ignoring store -- is AC-2's own
    requirement: the close pass's only caller of ``list_active_findings``
    must also see the raise, and must resolve nothing on the way.
    """
    population = _seed_active_observation_population()
    get_calls: list[tuple[int, int]] = []
    resolved: list[str] = []
    store = _live_store(
        monkeypatch,
        _active_observation_fake_request(
            population, ignore_offset=True, get_calls=get_calls, resolved_ids=resolved
        ),
    )

    with pytest.raises(eo.ActiveObservationPagingError, match="repeats the previous page"):
        store.list_active_findings()
    assert len(get_calls) == 2

    with pytest.raises(eo.ActiveObservationPagingError, match="repeats the previous page"):
        eo.land_findings(store, [], applications=[], now_fn=_now())
    assert resolved == []


def test_list_active_findings_tolerates_a_server_capping_pages_below_the_request(monkeypatch):
    """A server that answers with fewer rows than ``limit`` while more data
    remains (every page here is capped at 100, though 500 was requested)
    must not be mistaken for exhaustion -- the result must match the
    uncapped walk exactly."""
    population = _seed_active_observation_population()
    store = _live_store(
        monkeypatch,
        _active_observation_fake_request(population, cap_page_at=100),
    )

    found = store.list_active_findings()

    assert {bead["content"]["ref"] for bead in found} == _expected_active_finding_refs()


def test_close_pass_resolves_every_finding_the_walk_found(monkeypatch):
    """Once the walk (pinned above) correctly returns the full 52, the close
    pass (``land_findings``, whose only caller of ``list_active_findings`` is
    its trailing resolve loop) must resolve every one of them on a run that
    saw none of their refs -- checked here against ``FakeEAObserverStore``,
    seeded with exactly those 52, isolating close-pass logic from the HTTP
    paging mechanics the tests above already cover."""
    population = _seed_active_observation_population()
    live_store = _live_store(monkeypatch, _active_observation_fake_request(population))
    findings_found = live_store.list_active_findings()
    assert len(findings_found) == ACTIVE_FINDING_COUNT

    store = FakeEAObserverStore()
    store.observations = [dict(bead) for bead in findings_found]

    result = eo.land_findings(store, [], applications=[], now_fn=_now())

    assert result["closed"] == ACTIVE_FINDING_COUNT
    assert all(bead["state"] == "resolved" for bead in store.observations)


def test_active_observation_paging_bound_raises_rather_than_silently_truncating(monkeypatch):
    """dev.finding 5c3cf2b3's own defect, pinned against regression: capping
    the walk at one page (the pre-fix shape -- a single GET, nothing beyond
    it) over this same seeded population must not come back reporting a
    clean, empty close pass. AC-1 requires every bound hit to raise rather
    than return a partial population -- the seeded population needs three
    real pages, so forcing the walk to stop after one must fail loudly
    instead of letting the close pass silently resolve zero findings (the
    exact shape that made 'closed' structurally 0 on 2026-09-24). If the
    walk or its bound is ever removed, ``monkeypatch.setattr`` below fails
    with ``AttributeError`` before this assertion even runs."""
    monkeypatch.setattr(eo, "MAX_ACTIVE_OBSERVATION_PAGES", 1)
    population = _seed_active_observation_population()
    resolved: list[str] = []
    store = _live_store(
        monkeypatch,
        _active_observation_fake_request(population, resolved_ids=resolved),
    )

    with pytest.raises(eo.ActiveObservationPagingError, match="1"):
        store.list_active_findings()

    with pytest.raises(eo.ActiveObservationPagingError):
        eo.land_findings(store, [], applications=[], now_fn=_now())

    assert resolved == []


def test_active_observation_fake_transport_rejects_what_the_live_route_rejects(monkeypatch):
    """PR 1138's release gate found the fake transport above accepting an
    unknown query parameter and a negative ``limit``/``offset`` -- both of
    which the live ``GET /beads`` route rejects (routes.py's
    ``_reject_unknown_list_beads_params`` for the former, its ``int``-typed
    paging params for the latter). A fake that accepts more than the live
    route accepts is a second, looser implementation of the route's
    contract, not a stand-in for it. This pins the fake's own refusal
    directly, independent of the walk."""
    population = _seed_active_observation_population()
    store = _live_store(monkeypatch, _active_observation_fake_request(population))

    with pytest.raises(eo.httpx.HTTPStatusError):
        store._request("GET", "/beads", params={"namespace": "arch", "bogus": 1})
    with pytest.raises(eo.httpx.HTTPStatusError):
        store._request("GET", "/beads", params={"namespace": "arch", "limit": -1})
    with pytest.raises(eo.httpx.HTTPStatusError):
        store._request("GET", "/beads", params={"namespace": "arch", "offset": -1})


# -- observe_ea_model wiring lands both findings and the technology layer ----


def test_observe_ea_model_lands_ci_records_alongside_findings():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [
            _obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x")
        ],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": ["platform-core"]},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    # The workload is claimed (declared object present and ready), so no
    # divergence finding lands for it -- only the CI landing is exercised.
    assert result["ci"]["created"] == 2  # the workload CI and the namespace CI
    assert len(store.cis) == 2
    ci_kinds = {bead["content"]["ci_kind"] for bead in store.cis}
    assert ci_kinds == {"workload", "namespace"}


# -- R2605-8 DEFECT 1: a blind collector must raise, not return emptiness ----


def test_collect_cluster_observations_refuses_without_kubeconfig(monkeypatch):
    monkeypatch.delenv(eo.KUBECONFIG_ENV, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        eo.collect_cluster_observations(runner=lambda *a: (_ for _ in ()).throw(
            AssertionError("kubectl must never be invoked without KUBECONFIG declared")
        ))

    message = str(excinfo.value)
    assert eo.KUBECONFIG_ENV in message
    # The refusal names the variable, never a resolved path or value.
    assert "/" not in message


def test_collect_cluster_namespaces_refuses_without_kubeconfig(monkeypatch):
    monkeypatch.delenv(eo.KUBECONFIG_ENV, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        eo.collect_cluster_namespaces(runner=lambda *a: (_ for _ in ()).throw(
            AssertionError("kubectl must never be invoked without KUBECONFIG declared")
        ))

    assert eo.KUBECONFIG_ENV in str(excinfo.value)


def test_collect_cluster_observations_raises_when_every_resource_type_is_none(monkeypatch):
    """The injection seam collect_kubectl_snapshot(runner=...) already has --
    collect_cluster_observations had none before this fix, so three None
    payloads silently became an empty observation list instead of the
    checker-is-broken signal cluster_health.py's own equivalent already
    gives (R2605-8 DEFECT 1)."""
    monkeypatch.setenv(eo.KUBECONFIG_ENV, "/example/kubeconfig")

    with pytest.raises(eo.MissingKubectlOutputError):
        eo.collect_cluster_observations(runner=lambda *args: None)


def test_collect_cluster_observations_runs_checks_it_has_data_for(monkeypatch):
    """A partial outage (some resource types answer, some don't) is not blindness --
    matches cluster_health.collect_kubectl_snapshot's own "all four empty" threshold."""
    monkeypatch.setenv(eo.KUBECONFIG_ENV, "/example/kubeconfig")
    monkeypatch.setattr(eo, "_current_kubectl_context", lambda: "cluster-a")

    def runner(kind, *args):
        if kind == "deployments":
            return {
                "items": [{"metadata": {"namespace": "platform-core", "name": "x"}, "status": {}}]
            }
        return None

    observations = eo.collect_cluster_observations(runner=runner)

    assert len(observations) == 1
    assert observations[0]["name"] == "x"


def test_collect_cluster_namespaces_raises_when_kubectl_returns_none(monkeypatch):
    monkeypatch.setenv(eo.KUBECONFIG_ENV, "/example/kubeconfig")

    with pytest.raises(eo.MissingKubectlOutputError):
        eo.collect_cluster_namespaces(runner=lambda *args: None)


# -- R2605-8 DEFECT 1 (second half): a blind pass is a reported failure, never a
# silent Completed run -----------------------------------------------------


def test_observe_ea_model_writes_a_standing_failure_and_reraises_when_the_collector_raises():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    def _blind_collector():
        raise eo.MissingKubectlOutputError("kubectl produced no output for any tracked resource type")

    with pytest.raises(eo.MissingKubectlOutputError):
        eo.observe_ea_model(
            store,
            collect_observations_fn=_blind_collector,
            collect_namespaces_fn=lambda: (_ for _ in ()).throw(
                AssertionError("namespaces must never be collected after observations failed")
            ),
            reflect_fn=ea_reflect.reflect,
            now_fn=_now(),
        )

    # Nothing else in the pass -- no finding, no arch.ci landing -- was even attempted.
    [status_bead] = [
        bead for bead in store.observations
        if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "failed"
    assert "kubectl produced no output" in status_bead["context"]["reason"]
    assert store.cis == []


def test_observe_ea_model_records_ok_status_on_a_clean_pass():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    [status_bead] = [
        bead for bead in store.observations
        if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "ok"


def test_observe_ea_model_failure_status_recovers_and_carries_consecutive_failures():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    def _blind_collector():
        raise eo.MissingKubectlOutputError("boom")

    for now in (_now(23), _now(24)):
        with pytest.raises(eo.MissingKubectlOutputError):
            eo.observe_ea_model(
                store,
                collect_observations_fn=_blind_collector,
                collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
                reflect_fn=ea_reflect.reflect,
                now_fn=now,
            )

    [status_bead] = [
        bead for bead in store.observations
        if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["consecutive_failures"] == 2
    assert status_bead["context"]["repeat_of_previous_failure"] is True

    eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(25),
    )

    [status_bead] = [
        bead for bead in store.observations
        if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "ok"
    assert status_bead["context"]["recovered_from"]["reason"] == "boom"


# -- S51-3 second filing: cluster-identity cannot-evaluate (PRIN-015) --------
#
# The model and the observer can disagree on what to call the cluster even
# when kubectl and the substrate are both perfectly reachable -- collection
# succeeds, but the owner lookup's cluster key can never match anything. That
# must read as "could not evaluate," never "ok," and must never silently drop
# the entire technology layer with nothing to show for it.


def _observations_from_fixture(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return eo.workload_observations_from_kubectl(
        cluster=fixture["observed_cluster"],
        deployments=fixture["deployments"],
        statefulsets=fixture["statefulsets"],
        applications=fixture["argocd_applications"],
    )


def _namespaces_from_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster": fixture["observed_cluster"],
        "names": eo._namespace_names_from_kubectl(fixture["namespaces"]),
    }


def test_evaluate_cluster_identity_is_ok_when_the_strings_agree():
    fixture = _load_fixture("cluster_identity_match.json")
    observations = _observations_from_fixture(fixture)

    result = eo.evaluate_cluster_identity(
        observations=observations, applications=fixture["declared_applications"]
    )

    assert result["condition"] == eo.CLUSTER_IDENTITY_OK
    assert result["observed_clusters"] == ["cluster-a"]
    assert result["declared_clusters"] == ["cluster-a"]


def test_evaluate_cluster_identity_cannot_evaluate_when_the_strings_are_disjoint():
    fixture = _load_fixture("cluster_identity_mismatch.json")
    observations = _observations_from_fixture(fixture)

    result = eo.evaluate_cluster_identity(
        observations=observations, applications=fixture["declared_applications"]
    )

    assert result["condition"] == eo.CLUSTER_IDENTITY_CANNOT_EVALUATE
    assert result["observed_clusters"] == ["default"]
    assert result["declared_clusters"] == ["cluster-a"]


def test_evaluate_cluster_identity_is_ok_when_either_side_has_nothing_to_compare():
    # No live workloads observed yet -- a legitimately empty cluster, not a
    # naming mismatch (the AC's N>0/M>0 guard).
    assert eo.evaluate_cluster_identity(
        observations=[], applications=[_app("app.live", workload=_workload(_k8s_obj()))]
    )["condition"] == eo.CLUSTER_IDENTITY_OK
    # No kubernetes-runtime applications declared yet.
    assert eo.evaluate_cluster_identity(
        observations=[_obs(cluster="anything")], applications=[]
    )["condition"] == eo.CLUSTER_IDENTITY_OK


def test_observe_ea_model_records_cannot_evaluate_and_lands_no_ci_when_clusters_disagree(monkeypatch):
    # This module's alert path defaults to the real, credentialed
    # `failure_diagnosis.dev_task_alert_policy()` (the same default
    # `doctrine_registry_view.announce_registry_drift`/
    # `announce_registry_view_unreachable` use) -- any test exercising
    # `observe_ea_model`'s cannot-evaluate branch without patching the
    # announce call would otherwise post to the live alert channel. Patched
    # here the same way `test_doctrine_registry_view.py` patches its own
    # `announce_registry_view_unreachable` at the activity-wiring level;
    # `announce_cluster_identity_mismatch` itself is exercised directly,
    # with an explicit fake policy, in the tests above.
    announced = {}
    monkeypatch.setattr(
        eo,
        "announce_cluster_identity_mismatch",
        lambda observed, declared: announced.update(observed=observed, declared=declared),
    )

    fixture = _load_fixture("cluster_identity_mismatch.json")
    store = FakeEAObserverStore()
    store.applications = fixture["declared_applications"]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: _observations_from_fixture(fixture),
        collect_namespaces_fn=lambda: _namespaces_from_fixture(fixture),
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["ci"]["status"] == eo.CLUSTER_IDENTITY_CANNOT_EVALUATE
    assert store.cis == []
    assert [link for link in store.links if link["link_type"] == "depends_on"] == []
    assert announced == {"observed": ["default"], "declared": ["cluster-a"]}

    [status_bead] = [
        bead for bead in store.observations if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "cannot_evaluate"
    assert status_bead["context"]["status"] != "ok"
    assert status_bead["context"]["observed_clusters"] == ["default"]
    assert status_bead["context"]["declared_clusters"] == ["cluster-a"]


def test_observe_ea_model_lands_no_findings_when_clusters_disagree(monkeypatch):
    """The bug this bead closes: on the 2026-09-12 shape, ``land_findings``
    ran (at the time, unconditionally) before ``evaluate_cluster_identity``
    ever got a say, so 48 observations carrying a disowned cluster string
    still produced standing findings -- an ``ArgoCDOwnerMissing``/
    ``UnmodelledWorkload`` pair here, since the fixture declares ``x`` and
    ``postgres`` as owned by ``cluster-a`` while kubectl reports them
    under ``default``. On today's (pre-fix) code this test fails: the store
    ends up with those finding beads in addition to the status bead. The fix
    must make ``reflect_fn`` never even run on a cannot-evaluate night, not
    merely make its output unlandable.
    """
    monkeypatch.setattr(
        eo,
        "announce_cluster_identity_mismatch",
        lambda observed, declared: None,
    )
    reflect_calls: list[Any] = []

    def _spy_reflect(observations: Any, model: Any) -> list[Any]:
        reflect_calls.append((observations, model))
        return ea_reflect.reflect(observations, model)

    fixture = _load_fixture("cluster_identity_mismatch.json")
    store = FakeEAObserverStore()
    store.applications = fixture["declared_applications"]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: _observations_from_fixture(fixture),
        collect_namespaces_fn=lambda: _namespaces_from_fixture(fixture),
        reflect_fn=_spy_reflect,
        now_fn=_now(),
    )

    assert reflect_calls == []
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["ci"]["status"] == eo.CLUSTER_IDENTITY_CANNOT_EVALUATE

    non_status_beads = [
        bead for bead in store.observations if bead["content"]["ref"] != eo.OBSERVER_STATUS_REF
    ]
    assert non_status_beads == []

    # PR #832's guarantee, unweakened by suppressing the findings above.
    [status_bead] = [
        bead for bead in store.observations if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "cannot_evaluate"
    assert status_bead["context"]["checked_at"]
    assert store.cis == []


def test_observe_ea_model_lands_ci_normally_when_clusters_agree():
    fixture = _load_fixture("cluster_identity_match.json")
    store = FakeEAObserverStore()
    store.applications = fixture["declared_applications"]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: _observations_from_fixture(fixture),
        collect_namespaces_fn=lambda: _namespaces_from_fixture(fixture),
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["ci"]["created"] > 0
    assert store.cis

    [status_bead] = [
        bead for bead in store.observations if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "ok"


def test_observe_ea_model_lands_findings_normally_when_clusters_agree():
    """The clean path (AC5): a run whose cluster strings agree must land
    findings exactly as it did before this bead -- suppression is scoped to
    the cannot-evaluate condition alone. Declares a ``postgres`` StatefulSet
    under the agreeing ``cluster-a`` cluster that kubectl never reports
    running, so ``ea_reflect.reflect`` has a real divergence to find.
    """
    fixture = {
        "observed_cluster": "cluster-a",
        "deployments": {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {
                    "metadata": {"namespace": "platform-core", "name": "x"},
                    "status": {"replicas": 1, "readyReplicas": 1},
                }
            ],
        },
        "statefulsets": {"apiVersion": "v1", "kind": "List", "items": []},
        "argocd_applications": {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {
                    "metadata": {"name": "platform-core"},
                    "status": {
                        "resources": [
                            {"namespace": "platform-core", "kind": "Deployment", "name": "x"},
                        ]
                    },
                }
            ],
        },
        "namespaces": {
            "apiVersion": "v1",
            "kind": "List",
            "items": [{"metadata": {"name": "platform-core"}}],
        },
        "declared_applications": [
            {
                "id": "bead-app-live",
                "state": "operate",
                "content": {
                    "ref": "app.live",
                    "workload": {
                        "runtime": "kubernetes",
                        "objects": [
                            {
                                "cluster": "cluster-a",
                                "namespace": "platform-core",
                                "kind": "Deployment",
                                "name": "x",
                                "manifest": "infrastructure/k8s/platform-core/x.yaml",
                                "managed_by": "argocd",
                            },
                            {
                                "cluster": "cluster-a",
                                "namespace": "platform-core",
                                "kind": "StatefulSet",
                                "name": "postgres",
                                "manifest": "infrastructure/k8s/platform-core/postgres.yaml",
                                "managed_by": "argocd",
                            },
                        ],
                    },
                },
            }
        ],
    }
    store = FakeEAObserverStore()
    store.applications = fixture["declared_applications"]

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: _observations_from_fixture(fixture),
        collect_namespaces_fn=lambda: _namespaces_from_fixture(fixture),
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert "status" not in result["ci"]
    assert result["created"] > 0

    non_status_beads = [
        bead for bead in store.observations if bead["content"]["ref"] != eo.OBSERVER_STATUS_REF
    ]
    assert non_status_beads


def test_cluster_mismatch_alert_is_registered_and_actionable():
    definition = notify.get_alert_definition(eo.CLUSTER_MISMATCH_ALERT_ID)

    assert definition.has_next_step
    assert definition.severity == notify.AlertSeverity.ACTIONABLE


def test_cluster_mismatch_alert_fingerprint_is_stable_across_nights_on_the_same_condition():
    """This asserts ``notify.alert_content_fingerprint`` itself is a pure
    function of its input string -- necessary, but not sufficient: it does
    not exercise how ``announce_cluster_identity_mismatch`` builds that input
    from the two cluster string sets. See
    ``test_announce_cluster_identity_mismatch_posts_a_stable_fingerprint_across_nights``
    for the assertion against the real alert path (F2, review note
    30cd7c03-190c-438b-8775-22f932543e7b)."""
    first = notify.alert_content_fingerprint("default::cluster-a")
    second = notify.alert_content_fingerprint("default::cluster-a")
    different_condition = notify.alert_content_fingerprint("other-cluster::cluster-a")

    assert first == second
    assert first != different_condition


def test_announce_cluster_identity_mismatch_posts_through_the_declared_alert(monkeypatch):
    posted = {}

    class _FakePolicy:
        async def send(self, kind, fingerprint, content, **kwargs):
            posted["kind"] = kind
            posted["fingerprint"] = fingerprint
            posted["content"] = content
            return True

    result = eo.announce_cluster_identity_mismatch(
        ["default"], ["cluster-a"], policy=_FakePolicy()
    )

    assert result is True
    assert posted["kind"] == "ea_observer_cluster_mismatch"
    assert "default" in posted["content"]
    assert "cluster-a" in posted["content"]


def test_announce_cluster_identity_mismatch_posts_a_stable_fingerprint_across_nights():
    """F2 (review note 30cd7c03-190c-438b-8775-22f932543e7b): OPS-69's class
    of failure is a fingerprint that depends on wall-clock time, reposting an
    urgent alert on an unchanged condition every run forever. Unlike
    ``test_cluster_mismatch_alert_fingerprint_is_stable_across_nights_on_the_same_condition``,
    this calls the real alert path -- ``announce_cluster_identity_mismatch``,
    which builds the fingerprint from the two cluster string sets -- twice,
    and asserts on what it actually posted, not on a hand-built string handed
    directly to ``notify.alert_content_fingerprint``. A mutation that appends
    ``time.time()`` inside ``_announce_cluster_identity_mismatch_async``'s
    fingerprint construction must turn this test red."""
    posted = []

    class _RecordingPolicy:
        async def send(self, kind, fingerprint, content, **kwargs):
            posted.append(fingerprint)
            return True

    eo.announce_cluster_identity_mismatch(["default"], ["cluster-a"], policy=_RecordingPolicy())
    eo.announce_cluster_identity_mismatch(["default"], ["cluster-a"], policy=_RecordingPolicy())
    eo.announce_cluster_identity_mismatch(["other-cluster"], ["cluster-a"], policy=_RecordingPolicy())

    same_condition_first, same_condition_second, different_condition = posted
    assert same_condition_first == same_condition_second
    assert same_condition_first != different_condition


def test_announce_cluster_identity_mismatch_never_raises_on_a_broken_policy():
    class _BrokenPolicy:
        async def send(self, *args, **kwargs):
            raise RuntimeError("webhook is down")

    # Best-effort, matching doctrine_registry_view.py's announce_* functions:
    # alerting must never crash a run whose standing observation already landed.
    assert eo.announce_cluster_identity_mismatch(["default"], ["cluster-a"], policy=_BrokenPolicy()) is False


# -- S51-3 second filing: unattributed workloads land, unlinked, and count ---


def test_land_ci_records_lands_unattributed_workload_with_no_edge_and_counts_it():
    store = FakeEAObserverStore()
    records = eo.ci_records_from_observations(
        observations=[_obs(cluster="cluster-a", namespace="infra-ai", kind="Deployment", name="surprise")],
        applications=[_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))],
    )

    result = eo.land_ci_records(store, records)

    assert result == {
        "checked": 1,
        "created": 1,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 0,
        "unattributed": 1,
    }
    [bead] = store.cis
    assert bead["content"]["name"] == "surprise"
    assert store.links == []


def test_land_ci_records_second_run_over_an_unchanged_unattributed_workload_writes_nothing():
    store = FakeEAObserverStore()
    records = eo.ci_records_from_observations(
        observations=[_obs(cluster="cluster-a", namespace="infra-ai", kind="Deployment", name="surprise")],
        applications=[_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))],
    )

    first = eo.land_ci_records(store, records)
    second = eo.land_ci_records(store, records)

    assert first["created"] == 1
    assert second == {
        "checked": 1,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 0,
        "unattributed": 1,
    }
    assert len(store.cis) == 1
    assert store.links == []


def test_land_ci_records_workload_landed_unattributed_gains_its_edge_once_an_application_claims_it():
    """F1 regression (review note 30cd7c03-190c-438b-8775-22f932543e7b): a
    workload first landed unattributed must still be able to gain its
    ``depends_on`` edge on a later cycle where an ``arch.application`` claims
    it -- ``_ci_content`` excludes ``owner_ids``, so the CI's content is
    byte-identical before and after the application starts claiming it, and
    the pre-fix ``needs_content``/``needs_reopen`` gate on the existing-record
    path meant that edge could never be created on the normal lifecycle
    path."""
    store = FakeEAObserverStore()
    observed = [_obs(cluster="cluster-a", namespace="infra-ai", kind="Deployment", name="surprise")]

    first = eo.land_ci_records(store, eo.ci_records_from_observations(observations=observed, applications=[]))
    assert first == {
        "checked": 1,
        "created": 1,
        "updated": 0,
        "closed": 0,
        "linked": 0,
        "attributed": 0,
        "unattributed": 1,
    }
    assert store.links == []

    owner = _app(
        "app.live",
        bead_id="bead-app-live",
        workload=_workload(_k8s_obj(namespace="infra-ai", name="surprise")),
    )
    second = eo.land_ci_records(
        store, eo.ci_records_from_observations(observations=observed, applications=[owner])
    )

    assert second == {
        "checked": 1,
        "created": 0,
        "updated": 0,
        "closed": 0,
        "linked": 1,
        "attributed": 1,
        "unattributed": 0,
    }
    assert len(store.cis) == 1
    [link] = store.links
    assert link["source_id"] == "bead-app-live"
    assert link["target_id"] == store.cis[0]["id"]
    assert link["link_type"] == "depends_on"


def test_observe_ea_model_ok_status_names_attributed_and_unattributed_ci_counts():
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", bead_id="bead-app-live", workload=_workload(_k8s_obj()))]

    eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [
            _obs(cluster="cluster-a", namespace="platform-core", kind="Deployment", name="x"),
            _obs(cluster="cluster-a", namespace="infra-ai", kind="Deployment", name="surprise"),
        ],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    [status_bead] = [
        bead for bead in store.observations if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "ok"
    assert status_bead["context"]["ci_counts"] == {"attributed": 1, "unattributed": 1}


def test_ea_observation_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
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
    monkeypatch.setattr(eo.httpx, "request", fake_request)

    store = eo.SubstrateEAObserverStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": eo.CREATED_BY, "Content-Type": "text/plain"},
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == eo.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"


# -- S52-3 (a728d995): application --depends_on--> application wiring --------
#
# End-to-end through observe_ea_model, with a real collect_dependency_facts_fn
# injected (the production wiring at observe_ea_model_activity) -- everything
# else about ea_dependency.py's own computation/landing logic is covered by
# test_ea_dependency.py directly.


def _raw_bead_app(ref: str, *, bead_id: str, objects: list[dict] | None) -> dict[str, Any]:
    if objects is None:
        workload = {"runtime": "none", "note": "not on kubernetes"}
    else:
        workload = {"runtime": "kubernetes", "objects": objects}
    return {"id": bead_id, "state": "operate", "content": {"ref": ref, "workload": workload}}


def test_observe_ea_model_lands_dependency_edges_alongside_findings_and_ci():
    store = FakeEAObserverStore()
    store.applications = [
        _raw_bead_app(
            "app.substrate", bead_id="id-substrate",
            objects=[{"cluster": "cluster-a", "namespace": "platform-core", "kind": "Deployment", "name": "substrate", "managed_by": "argocd"}],
        ),
        _raw_bead_app(
            "app.qdrant", bead_id="id-qdrant",
            objects=[{"cluster": "cluster-a", "namespace": "platform-core", "kind": "Deployment", "name": "qdrant", "managed_by": "argocd"}],
        ),
    ]
    deployments = {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"namespace": "platform-core", "name": "substrate"},
                "status": {},
                "spec": {
                    "template": {
                        "metadata": {"labels": {}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "substrate",
                                    "env": [
                                        {"name": "QDRANT_URL", "value": "http://qdrant.platform-core.svc.cluster.local:6333"}
                                    ],
                                }
                            ]
                        },
                    }
                },
            },
            {
                "kind": "Deployment",
                "metadata": {"namespace": "platform-core", "name": "qdrant"},
                "status": {},
                "spec": {"template": {"metadata": {"labels": {}}, "spec": {"containers": [{"name": "qdrant"}]}}},
            },
        ]
    }

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [],
        collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
        collect_dependency_facts_fn=lambda: {
            "cluster": "cluster-a", "deployments": deployments, "statefulsets": None, "networkpolicies": None,
        },
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["dependencies"]["edges_created"] == 1
    [link] = [entry for entry in store.links if entry["link_type"] == "depends_on"]
    assert link["source_id"] == "id-substrate"
    assert link["target_id"] == "id-qdrant"
    # AC-7: the dependency writer's identity is distinct from eo.CREATED_BY,
    # so a shared string can never make the retraction loop mistake an
    # app-to-ci edge land_ci_records wrote for one of this module's own.
    assert link["created_by"] == ea_dependency.CREATED_BY
    assert link["created_by"] != eo.CREATED_BY
    assert "app.qdrant" in link["content"]["method"]
    assert result["dependencies"]["posture_summary"]["known"] == 1


def test_observe_ea_model_dependency_step_is_skipped_when_cluster_identity_cannot_evaluate(monkeypatch):
    monkeypatch.setattr(
        eo, "announce_cluster_identity_mismatch",
        lambda observed, declared: None,
    )
    store = FakeEAObserverStore()
    store.applications = [
        _raw_bead_app(
            "app.live", bead_id="id-live",
            objects=[{"cluster": "cluster-a", "namespace": "platform-core", "kind": "Deployment", "name": "x", "managed_by": "argocd"}],
        )
    ]
    called = {"dependency_facts": False}

    def _dependency_facts():
        called["dependency_facts"] = True
        return ea_dependency.no_dependency_facts()

    result = eo.observe_ea_model(
        store,
        collect_observations_fn=lambda: [_obs(cluster="default", namespace="platform-core", kind="Deployment", name="x")],
        collect_namespaces_fn=lambda: {"cluster": "default", "names": []},
        collect_dependency_facts_fn=_dependency_facts,
        reflect_fn=ea_reflect.reflect,
        now_fn=_now(),
    )

    assert result["ci"]["status"] == eo.CLUSTER_IDENTITY_CANNOT_EVALUATE
    dependencies = dict(result["dependencies"])
    posture_summary = dependencies.pop("posture_summary")
    assert dependencies == {
        "edges_created": 0,
        "edges_retracted": 0,
        "assessed_none_created": 0,
        "assessed_none_updated": 0,
        "assessed_none_closed": 0,
        "known_by_edges": 0,
        "assessed_none": 0,
        "unknown": 0,
        "ci_only_dependency_refs": [],
    }
    # No application was assessed this cycle -- every one of them is unknown,
    # never silently reported as known or assessed-none.
    assert posture_summary["total_applications"] == 1
    assert posture_summary["known"] == 0
    assert posture_summary["assessed_none"] == 0
    assert posture_summary["unknown"] == 1
    assert posture_summary["unknown_refs"] == ["app.live"]
    assert called["dependency_facts"] is False
    assert [entry for entry in store.links if entry["link_type"] == "depends_on"] == []


def test_observe_ea_model_writes_a_standing_failure_and_reraises_when_the_dependency_collector_raises():
    """AC-8 (M3): on #997 the ``collect_dependency_facts_fn()`` call sat
    outside every try/except in ``observe_ea_model``, so a failure there
    (kubectl unreachable, or the plain RuntimeError ``_require_kubeconfig``
    raises) propagated after ``land_findings``/``land_ci_records`` had
    already written this cycle's beads, with ``_write_observer_status``
    never reached -- a run that failed this way left no record that it had
    failed at all, unlike every other collector failure in this function."""
    store = FakeEAObserverStore()
    store.applications = [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]

    def _blind_dependency_collector():
        raise eo.MissingKubectlOutputError("kubectl produced no output for either tracked workload type")

    with pytest.raises(eo.MissingKubectlOutputError):
        eo.observe_ea_model(
            store,
            collect_observations_fn=lambda: [],
            collect_namespaces_fn=lambda: {"cluster": "cluster-a", "names": []},
            collect_dependency_facts_fn=_blind_dependency_collector,
            reflect_fn=ea_reflect.reflect,
            now_fn=_now(),
        )

    [status_bead] = [
        bead for bead in store.observations
        if bead["content"]["ref"] == eo.OBSERVER_STATUS_REF
    ]
    assert status_bead["context"]["status"] == "failed"
    assert "kubectl produced no output" in status_bead["context"]["reason"]
