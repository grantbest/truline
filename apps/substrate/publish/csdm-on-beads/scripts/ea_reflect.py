"""Pure EA reflector judgment.

The credentialed half of Track B observes Kubernetes and ArgoCD. This module
does not. It accepts those observations as data, compares them to the EA model,
and returns findings for review.

It deliberately proposes no replacement lifecycle state, health, value, or
TIME disposition. The metamodel reserves those judgments for review.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True, order=True)
class WorkloadRef:
    cluster: str
    namespace: str
    kind: str
    name: str

    @classmethod
    def from_mapping(cls, item: dict[str, Any]) -> "WorkloadRef":
        return cls(
            cluster=str(item["cluster"]),
            namespace=str(item["namespace"]),
            kind=str(item["kind"]),
            name=str(item["name"]),
        )

    def display(self) -> str:
        return f"{self.cluster}/{self.namespace}/{self.kind}/{self.name}"


@dataclass(frozen=True)
class RetiredApplicationRunning:
    application_ref: str
    observed_object: WorkloadRef
    kind: str = field(init=False, default="retired-application-running")


@dataclass(frozen=True)
class OperateApplicationNotRunning:
    application_ref: str
    declared_object: WorkloadRef
    reason: str
    kind: str = field(init=False, default="operate-application-not-running")


@dataclass(frozen=True)
class ArgoCDOwnerMissing:
    application_ref: str
    declared_object: WorkloadRef
    observed_object: WorkloadRef
    kind: str = field(init=False, default="argocd-owner-missing")


@dataclass(frozen=True)
class UnmodelledWorkload:
    observed_object: WorkloadRef
    kind: str = field(init=False, default="unmodelled-workload")


@dataclass(frozen=True)
class ExternalApplicationDeclared:
    application_ref: str
    host: str
    compose_path: str
    service: str
    reason: str = field(init=False, default="declared-but-not-live-verified")
    kind: str = field(init=False, default="external-declared")


ReflectFinding = (
    RetiredApplicationRunning
    | OperateApplicationNotRunning
    | ArgoCDOwnerMissing
    | UnmodelledWorkload
    | ExternalApplicationDeclared
)
Contradiction = ReflectFinding


def finding_identity(finding: ReflectFinding) -> str:
    """A finding's identity, stable across runs of the same divergence.

    A scheduled caller lands each finding as a standing record keyed on this
    string (PRIN-014): the same divergence observed on two different runs
    must resolve to the same identity so the second run updates the first
    run's record instead of minting a duplicate, and a divergence's absence
    on a later run is detected by its identity no longer appearing. That
    means identity must depend only on *what* is diverging, never on a value
    that can legitimately differ between two observations of the same
    ongoing condition -- ``OperateApplicationNotRunning.reason`` flips
    between ``"absent"`` and ``"zero-ready"`` run to run without the
    divergence itself having changed, so ``reason`` is deliberately excluded.
    """
    if isinstance(finding, RetiredApplicationRunning):
        return (
            f"{finding.kind}:{finding.application_ref}:"
            f"{finding.observed_object.display()}"
        )
    if isinstance(finding, OperateApplicationNotRunning):
        return (
            f"{finding.kind}:{finding.application_ref}:"
            f"{finding.declared_object.display()}"
        )
    if isinstance(finding, ArgoCDOwnerMissing):
        return (
            f"{finding.kind}:{finding.application_ref}:"
            f"{finding.declared_object.display()}"
        )
    if isinstance(finding, UnmodelledWorkload):
        return f"{finding.kind}:{finding.observed_object.display()}"
    if isinstance(finding, ExternalApplicationDeclared):
        return f"{finding.kind}:{finding.application_ref}"
    raise TypeError(f"finding_identity: unknown finding type {type(finding)!r}")


def reflect(observations: Any, model: Any) -> list[ReflectFinding]:
    """Return findings between observed workload facts and the EA model.

    Expected model shape is the existing YAML shape loaded as Python data:

        {"applications": [{"ref": "app.x", "state": "operate", "content": {...}}]}

    ``model`` may also be passed as the application list directly.

    Expected observation shape is either a list of workload observation dicts or
    a dict with a ``workloads`` key. Each workload observation must carry
    ``cluster``, ``namespace``, ``kind``, and ``name``. Optional fields used by
    the judgment are:

    * ``application_ref`` / ``app_ref`` / ``claimed_by``: observed deploy shape
      attribution supplied by the credentialed collector.
    * ``running``: explicit boolean; otherwise inferred from ready/available
      replica or endpoint counts.
    * ``ready_replicas`` or ``ready: "1/1"``: used to detect operate apps with
      zero ready replicas.
    * ``owning_argocd_application`` / ``argocd_application`` / ``argocd_app``:
      if present and falsey for a declared ArgoCD-managed object, that is a
      contradiction. ArgoCD health alone is intentionally ignored.
    """
    apps = list(_applications(model))
    observed = list(_workload_observations(observations))
    observed_by_ref = {_observed_ref(o): o for o in observed}
    declared_by_app = {app["ref"]: _declared_workloads(app) for app in apps}
    claimed_refs = {ref for objects in declared_by_app.values() for ref, _ in objects}

    findings: list[ReflectFinding] = []

    for app in apps:
        app_ref = app["ref"]
        lifecycle = app.get("state")
        declared = declared_by_app[app_ref]
        external_binding = _external_binding(app)
        if external_binding is not None:
            findings.append(
                ExternalApplicationDeclared(
                    application_ref=app_ref,
                    host=str(external_binding["host"]),
                    compose_path=str(external_binding["compose_path"]),
                    service=str(external_binding["service"]),
                )
            )

        if lifecycle in {"retire", "eol"}:
            for obs in observed:
                obs_ref = _observed_ref(obs)
                if _is_running(obs) and _observation_belongs_to(obs, app_ref, declared):
                    findings.append(
                        RetiredApplicationRunning(
                            application_ref=app_ref,
                            observed_object=obs_ref,
                        )
                    )

        if lifecycle == "operate":
            for declared_ref, _ in declared:
                obs = observed_by_ref.get(declared_ref)
                if obs is None:
                    findings.append(
                        OperateApplicationNotRunning(
                            application_ref=app_ref,
                            declared_object=declared_ref,
                            reason="absent",
                        )
                    )
                    continue
                ready = _ready_replicas(obs)
                if ready == 0:
                    findings.append(
                        OperateApplicationNotRunning(
                            application_ref=app_ref,
                            declared_object=declared_ref,
                            reason="zero-ready",
                        )
                    )

        for declared_ref, declared_obj in declared:
            if declared_obj.get("managed_by") != "argocd":
                continue
            obs = observed_by_ref.get(declared_ref)
            if obs is not None and _reports_no_argocd_owner(obs):
                findings.append(
                    ArgoCDOwnerMissing(
                        application_ref=app_ref,
                        declared_object=declared_ref,
                        observed_object=_observed_ref(obs),
                    )
                )

    for obs in observed:
        obs_ref = _observed_ref(obs)
        if _is_running(obs) and obs_ref not in claimed_refs:
            findings.append(UnmodelledWorkload(observed_object=obs_ref))

    return findings


def _applications(model: Any) -> Iterable[dict[str, Any]]:
    if isinstance(model, dict):
        return model.get("applications", [])
    return model


def _workload_observations(observations: Any) -> Iterable[dict[str, Any]]:
    if isinstance(observations, dict):
        return observations.get("workloads", observations.get("objects", []))
    return observations


def _declared_workloads(app: dict[str, Any]) -> list[tuple[WorkloadRef, dict[str, Any]]]:
    workload = (app.get("content") or {}).get("workload") or {}
    objects = workload.get("objects") or []
    return [(WorkloadRef.from_mapping(obj), obj) for obj in objects]


def _external_binding(app: dict[str, Any]) -> dict[str, Any] | None:
    workload = (app.get("content") or {}).get("workload") or {}
    if workload.get("runtime") != "external":
        return None
    binding = workload.get("binding")
    if not isinstance(binding, dict):
        return None
    required = {"host", "compose_path", "service"}
    if not required <= set(binding):
        return None
    return binding


def _observed_ref(observation: dict[str, Any]) -> WorkloadRef:
    obj = observation.get("object")
    if isinstance(obj, dict):
        merged = {**observation, **obj}
        return WorkloadRef.from_mapping(merged)
    return WorkloadRef.from_mapping(observation)


def _observation_belongs_to(
    observation: dict[str, Any],
    application_ref: str,
    declared: list[tuple[WorkloadRef, dict[str, Any]]],
) -> bool:
    observed_app = (
        observation.get("application_ref")
        or observation.get("app_ref")
        or observation.get("claimed_by")
    )
    if observed_app == application_ref:
        return True
    declared_refs = {ref for ref, _ in declared}
    return _observed_ref(observation) in declared_refs


def _is_running(observation: dict[str, Any]) -> bool:
    if "running" in observation:
        return bool(observation["running"])
    ready = _ready_replicas(observation)
    if ready is not None:
        return ready > 0
    for key in ("available_replicas", "replicas", "endpoint_count", "endpoints"):
        count = _nonnegative_int(observation.get(key))
        if count is not None:
            return count > 0
    return False


def _ready_replicas(observation: dict[str, Any]) -> int | None:
    ready = _nonnegative_int(observation.get("ready_replicas"))
    if ready is not None:
        return ready

    ready_text = observation.get("ready")
    if isinstance(ready_text, str) and "/" in ready_text:
        numerator = ready_text.split("/", 1)[0]
        return _nonnegative_int(numerator)

    return None


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _reports_no_argocd_owner(observation: dict[str, Any]) -> bool:
    for key in ("owning_argocd_application", "argocd_application", "argocd_app"):
        if key in observation:
            return not bool(observation[key])

    argocd = observation.get("argocd")
    if isinstance(argocd, dict) and "application" in argocd:
        return not bool(argocd["application"])

    return False
