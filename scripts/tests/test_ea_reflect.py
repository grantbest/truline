"""Tests for the pure EA live-model reflector.

The reflector is the judgment half of Track B. It compares caller-supplied
cluster observations to the EA model and returns contradictions. These tests use
fixtures rather than a cluster so the function stays pure and importable in CI.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("ea_reflect", REPO / "scripts" / "ea_reflect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reflector = _load()


def _app(ref="app.x", state="operate", workload=None, **content):
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
    return {"ref": ref, "state": state, "content": base}


def _k8s_obj(**over):
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


def _workload(*objects):
    return {"runtime": "kubernetes", "objects": list(objects)}


def _external_binding(**over):
    binding = {
        "host": "host-b",
        "compose_path": "infrastructure/docker/host-b/docker-compose.pihole.yml",
        "service": "pihole",
    }
    binding.update(over)
    return {"runtime": "external", "binding": binding}


def _obs(**over):
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


def _only(kind, contradictions):
    matches = [c for c in contradictions if isinstance(c, kind)]
    assert len(matches) == 1, contradictions
    return matches[0]


def test_eol_application_running_is_a_contradiction():
    """Regression fixture for ea-live-model.md §3.4: app.itop was scored eol
    while itop-app and itop-db were both running 1/1 on cluster-a."""
    itop_app = _k8s_obj(
        namespace="platform-itsm",
        kind="Deployment",
        name="itop-app",
        manifest=None,
        managed_by="none",
    )
    itop_db = _k8s_obj(
        namespace="platform-itsm",
        kind="StatefulSet",
        name="itop-db",
        manifest=None,
        managed_by="none",
    )
    model = {
        "applications": [
            _app("app.itop", state="eol", workload=_workload(itop_app, itop_db))
        ]
    }
    observations = {
        "workloads": [
            _obs(namespace="platform-itsm", kind="Deployment", name="itop-app", ready="1/1"),
            _obs(namespace="platform-itsm", kind="StatefulSet", name="itop-db", ready="1/1"),
        ]
    }

    contradictions = reflector.reflect(observations, model)

    matches = [c for c in contradictions if isinstance(c, reflector.RetiredApplicationRunning)]
    assert len(matches) == 2, contradictions
    assert {m.application_ref for m in matches} == {"app.itop"}
    assert {m.observed_object.name for m in matches} == {"itop-app", "itop-db"}


def test_retire_application_running_is_a_contradiction():
    model = {"applications": [_app("app.old", state="retire", workload=_workload(_k8s_obj()))]}
    contradictions = reflector.reflect({"workloads": [_obs()]}, model)

    match = _only(reflector.RetiredApplicationRunning, contradictions)
    assert match.application_ref == "app.old"
    assert match.observed_object.display() == "cluster-a/platform-core/Deployment/x"


@pytest.mark.parametrize("observations, reason", [({"workloads": []}, "absent"),
                                                  ({"workloads": [_obs(ready_replicas=0)]},
                                                   "zero-ready")])
def test_operate_application_without_ready_workload_is_a_contradiction(observations, reason):
    model = {"applications": [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]}

    contradictions = reflector.reflect(observations, model)

    match = _only(reflector.OperateApplicationNotRunning, contradictions)
    assert match.application_ref == "app.live"
    assert match.declared_object.name == "x"
    assert match.reason == reason


def test_argocd_managed_workload_without_argocd_owner_is_a_contradiction():
    """The goose-agent shape: reported ArgoCD health is not evidence that the
    workload object is owned by an ArgoCD Application."""
    goose_service = _k8s_obj(kind="Service", name="goose-agent")
    model = {
        "applications": [
            _app("app.goose-agent", state="plan", workload=_workload(goose_service))
        ]
    }
    observations = {
        "workloads": [
            _obs(
                kind="Service",
                name="goose-agent",
                ready_replicas=None,
                endpoint_count=1,
                owning_argocd_application=None,
                argocd_health="Healthy",
            )
        ]
    }

    contradictions = reflector.reflect(observations, model)

    match = _only(reflector.ArgoCDOwnerMissing, contradictions)
    assert match.application_ref == "app.goose-agent"
    assert match.declared_object.name == "goose-agent"


def test_running_workload_no_application_claims_is_unmodelled():
    model = {"applications": [_app("app.claimed", state="operate", workload=_workload(_k8s_obj()))]}
    observations = {
        "workloads": [
            _obs(name="x"),
            _obs(namespace="infra-ai", kind="Deployment", name="surprise", ready_replicas=1),
        ]
    }

    contradictions = reflector.reflect(observations, model)

    match = _only(reflector.UnmodelledWorkload, contradictions)
    assert match.observed_object.display() == "cluster-a/infra-ai/Deployment/surprise"


def test_observed_application_ref_can_attribute_terminal_state_without_declared_workload():
    model = {
        "applications": [
            _app("app.itop", state="eol", workload={"runtime": "none", "note": "gone"})
        ]
    }
    observations = {
        "workloads": [
            _obs(
                namespace="platform-itsm",
                kind="Deployment",
                name="itop-app",
                application_ref="app.itop",
            )
        ]
    }

    contradictions = reflector.reflect(observations, model)

    match = _only(reflector.RetiredApplicationRunning, contradictions)
    assert match.application_ref == "app.itop"
    assert match.observed_object.name == "itop-app"


def test_model_and_observations_agree_returns_empty():
    model = {"applications": [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]}
    observations = {"workloads": [_obs()]}

    assert reflector.reflect(observations, model) == []


def test_external_runtime_binding_is_reported_as_declared_not_live_verified():
    model = {"applications": [_app("app.pihole", state="operate", workload=_external_binding())]}

    findings = reflector.reflect({"workloads": []}, model)

    match = _only(reflector.ExternalApplicationDeclared, findings)
    assert match.kind == "external-declared"
    assert match.application_ref == "app.pihole"
    assert match.host == "host-b"
    assert match.compose_path == "infrastructure/docker/host-b/docker-compose.pihole.yml"
    assert match.service == "pihole"
    assert match.reason == "declared-but-not-live-verified"


def test_contradictions_do_not_propose_reserved_judgment_fields():
    model = {"applications": [_app("app.old", state="eol", workload=_workload(_k8s_obj()))]}
    contradictions = reflector.reflect({"workloads": [_obs()]}, model)

    reserved = {"state", "technical_health", "business_value", "time_disposition"}
    for contradiction in contradictions:
        assert reserved.isdisjoint(vars(contradiction))


# -- finding_identity: the PRIN-014 idempotence key ---------------------------


def test_finding_identity_is_stable_across_two_runs_of_the_same_divergence():
    model = {"applications": [_app("app.old", state="retire", workload=_workload(_k8s_obj()))]}
    observations = {"workloads": [_obs()]}

    first = reflector.reflect(observations, model)
    second = reflector.reflect(observations, model)

    assert [reflector.finding_identity(f) for f in first] == [
        reflector.finding_identity(f) for f in second
    ]


def test_finding_identity_is_stable_when_operate_not_running_reason_changes():
    """The same declared object going absent, then reappearing zero-ready,
    is one ongoing divergence, not two -- reason must not enter identity."""
    model = {"applications": [_app("app.live", state="operate", workload=_workload(_k8s_obj()))]}

    absent = _only(
        reflector.OperateApplicationNotRunning,
        reflector.reflect({"workloads": []}, model),
    )
    zero_ready = _only(
        reflector.OperateApplicationNotRunning,
        reflector.reflect({"workloads": [_obs(ready_replicas=0)]}, model),
    )

    assert absent.reason == "absent"
    assert zero_ready.reason == "zero-ready"
    assert reflector.finding_identity(absent) == reflector.finding_identity(zero_ready)


def test_finding_identity_differs_across_finding_kinds_and_targets():
    itop_app = _k8s_obj(namespace="platform-itsm", kind="Deployment", name="itop-app")
    itop_db = _k8s_obj(namespace="platform-itsm", kind="StatefulSet", name="itop-db")
    model = {
        "applications": [
            _app("app.itop", state="eol", workload=_workload(itop_app, itop_db)),
        ]
    }
    observations = {
        "workloads": [
            _obs(namespace="platform-itsm", kind="Deployment", name="itop-app", ready="1/1"),
            _obs(namespace="platform-itsm", kind="StatefulSet", name="itop-db", ready="1/1"),
        ]
    }

    findings = reflector.reflect(observations, model)
    identities = [reflector.finding_identity(f) for f in findings]

    assert len(identities) == len(set(identities)) == 2


def test_finding_identity_is_defined_for_external_declared_too():
    """External-declared bindings are excluded from the landing loop entirely
    (see .factory/design.md); finding_identity still has to return *something*
    stable for them so a future caller that does choose to key on it is not
    surprised by an exception."""
    model = {"applications": [_app("app.pihole", state="operate", workload=_external_binding())]}
    finding = _only(reflector.ExternalApplicationDeclared, reflector.reflect({"workloads": []}, model))

    assert reflector.finding_identity(finding) == "external-declared:app.pihole"
