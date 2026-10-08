"""application --depends_on--> application, computed from cluster facts only.

S52-3 (a728d995): 26 (now 19, per the 2026-09-15 snapshot) of 32 applications
carry an unknown dependency posture, and nothing measures it from the running
system. This module sits beside ``ea_observation.py``'s technology-layer
(``arch.ci``) landing rather than inside it — a separate, narrow Protocol, same
"one tested consumer, six-ish methods, nothing more" reasoning
``EAObserverStore``/``CIObserverStore`` already use for the same file.

Two cluster-observable signals, both requiring source *and* target to each
resolve to exactly one ``arch.application`` before anything is written — see
``.factory/design.md`` for why these two, why NetworkPolicy attribution reads
the policy's own ``podSelector`` (not its egress target) as the source, and
for the live-cluster examples that ground both:

* an env var's *literal* ``value`` (never ``valueFrom``/``envFrom`` — see F3)
  naming a Kubernetes Service DNS host (``<name>.<namespace>.svc[.cluster.local]``).
* a NetworkPolicy's own ``podSelector`` (the application traffic leaves *from*)
  and its ``egress[].to[].podSelector`` (the application traffic goes *to*).

PRIN-011: no manifest or source file in this repository is ever opened here —
``ArchWorkloadObject.manifest`` is carried through purely as an opaque string,
never read from. See
``test_ea_dependency_module_has_no_code_path_that_reads_repository_manifests``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

from cluster_health import MissingKubectlOutputError, _kubectl_json  # noqa: E402

#: Deliberately distinct from ``ea_observation.CREATED_BY``, even though both
#: strings named the same observer identity before this fix (AC-7). The two
#: modules' ``depends_on`` edges are retracted along different rules --
#: this module's own retraction loop below only ever considers edges *it*
#: wrote -- so a shared identity string let that loop mistake an
#: app-to-ci edge ``land_ci_records`` had just written for one of its own,
#: and delete it every cycle.
#:
#: EDGES ONLY (AC-7(a), corrected 2026-09-23 at the #997 gate): a
#: ``bead_link`` carries no ``source_class`` and ``POST /beads/{id}/links``
#: takes its writer from the ``X-Created-By`` header with no enrollment gate,
#: so this identity is real and safe to use there. It is never used for an
#: ``arch.observation`` bead -- see ``_observation_created_by`` below.
CREATED_BY = "factory-dispatcher/ea-observer/dependency"
DEPENDENCY_ASSESSMENT_OBSERVATION_KIND = "ea_dependency_assessment"


def _observation_created_by() -> str:
    """The identity every ``arch.observation`` bead this module writes is
    created AND updated under -- ``ea_observation.CREATED_BY``
    ("factory-dispatcher/ea-observer"), never this module's own
    :data:`CREATED_BY`.

    ``apps/substrate/src/bead_rules.py``'s ``SOURCE_CLASS_WRITERS["observed"]``
    is a closed enrollment that admits ``factory-dispatcher/ea-observer`` but
    not ``factory-dispatcher/ea-observer/dependency`` -- ``POST /beads`` 409s
    on the latter, proven by executing ``check_source_class_admission`` at
    the #997 gate. Enrolling a second writer identity is a scope decision
    reserved to the Operator, and ``apps/substrate/**`` is forbidden here, so the
    only available fix is to write under the identity that is already
    enrolled. This does not weaken edge ownership (AC-7(a) still applies
    :data:`CREATED_BY` to every ``depends_on`` link this module creates and
    retracts) -- it only affects the assessed-none observation bead, which
    ``land_findings``'s close-on-vanish loop never touches (it filters on
    ``observation_kind == "ea_reflect_finding"``, and this bead's is
    ``"ea_dependency_assessment"``).

    Local import: ``ea_observation`` imports this module at load time (for
    wiring), so importing it back at module scope here would be a cycle. By
    call time both modules are fully loaded, so this is safe -- the same
    technique :func:`collect_dependency_cluster_facts` already uses.
    """
    from activities.ea_observation import CREATED_BY as observation_created_by

    return observation_created_by


_HOST_PATTERN = re.compile(
    r"\b([a-z0-9]([a-z0-9-]*[a-z0-9])?)\.([a-z0-9]([a-z0-9-]*[a-z0-9])?)\.svc(\.cluster\.local)?\b"
)

#: The safe, no-cluster-call default for ``ea_observation.observe_ea_model``'s
#: injection seam -- production wiring passes ``collect_dependency_cluster_facts``
#: explicitly at ``observe_ea_model_activity`` instead of defaulting to it on
#: the function signature, unlike the two kubectl collectors ``ea_observation.py``
#: already has. Every existing ``observe_ea_model`` test predates this module
#: and injects no dependency-fact collector; a live-kubectl default here would
#: make every one of them shell out to kubectl. See .factory/design.md.
def no_dependency_facts() -> dict[str, Any]:
    return {"cluster": "", "deployments": None, "statefulsets": None, "networkpolicies": None}


class DependencyObserverStore(Protocol):
    """Deliberately narrow: exactly what ``land_dependency_posture`` calls."""

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def create_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str,
        created_by: str,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def delete_link(self, link_id: str) -> Any: ...

    def find_observation(self, ref: str) -> dict[str, Any] | None: ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


# -- collection: raw kubectl JSON, no translation -----------------------------


def collect_dependency_cluster_facts(*, runner: Any = _kubectl_json) -> dict[str, Any]:
    """Read-only kubectl collection for dependency inference.

    Deliberately its own kubectl round trip rather than threaded through
    ``ea_observation.collect_cluster_observations`` -- that function's tested
    return shape is the reduced ``ea_reflect`` view (metadata/status only) and
    changing it risks the exact "call site outside the diff" class of breakage
    F2 named. Deployments/StatefulSets come back raw here because dependency
    inference needs pod template labels and container env, neither of which
    the reduced view carries.

    Unlike ``collect_cluster_observations``, a single absent resource type
    degrades this collector's *signal*, not its correctness: NetworkPolicy
    RBAC being denied, for instance, should not block env-var-based inference.
    Only "both Deployments and StatefulSets came back None" -- structurally
    zero workload units to assess, the same blindness
    ``collect_cluster_observations`` refuses to treat as a healthy empty
    cluster -- raises.
    """
    # Local import: ``ea_observation`` imports this module at load time (for
    # wiring), so importing it back at module scope here would be a cycle.
    # By call time both modules are fully loaded, so this is safe.
    from activities.ea_observation import _current_kubectl_context, _require_kubeconfig

    _require_kubeconfig()
    cluster = _current_kubectl_context()
    deployments = runner("deployments", "-A")
    statefulsets = runner("statefulsets", "-A")
    networkpolicies = runner("networkpolicies", "-A")
    if deployments is None and statefulsets is None:
        raise MissingKubectlOutputError(
            "kubectl produced no output for either tracked workload type (deployments, "
            "statefulsets) in the dependency observer's collection. kubectl may be missing "
            "or the cluster may be unreachable."
        )
    return {
        "cluster": cluster,
        "deployments": deployments,
        "statefulsets": statefulsets,
        "networkpolicies": networkpolicies,
    }


# -- pure translation: raw kubectl workload items -> assessable units --------


def _pod_template(item: dict[str, Any]) -> dict[str, Any]:
    return ((item.get("spec") or {}).get("template") or {}) or {}


def _workload_unit(item: dict[str, Any], *, cluster: str) -> dict[str, Any]:
    """One Deployment/StatefulSet -> its identity, pod labels, literal env
    facts, and whether any container reads an env source this observer
    cannot resolve (F3)."""
    metadata = item.get("metadata") or {}
    template = _pod_template(item)
    pod_labels = (template.get("metadata") or {}).get("labels") or {}
    containers = (template.get("spec") or {}).get("containers") or []

    literal_envs: list[tuple[str, str, str]] = []
    has_unresolvable_env = False
    for container in containers:
        container_name = str(container.get("name") or "")
        if container.get("envFrom"):
            has_unresolvable_env = True
        for env in container.get("env") or []:
            if "valueFrom" in env:
                has_unresolvable_env = True
                continue
            value = env.get("value")
            if value:
                literal_envs.append((container_name, str(env.get("name") or ""), str(value)))

    return {
        "cluster": cluster,
        "namespace": str(metadata.get("namespace") or ""),
        "kind": str(item.get("kind") or ""),
        "name": str(metadata.get("name") or ""),
        "pod_labels": {str(k): str(v) for k, v in pod_labels.items()},
        "literal_envs": literal_envs,
        "has_unresolvable_env": has_unresolvable_env,
    }


def workload_units_from_kubectl(
    *, cluster: str, deployments: dict[str, Any] | None, statefulsets: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Pure translation, testable from fixtures alone -- same split as
    ``ea_observation.workload_observations_from_kubectl``."""
    units: list[dict[str, Any]] = []
    for kind, payload in (("Deployment", deployments), ("StatefulSet", statefulsets)):
        for item in (payload or {}).get("items") or []:
            item = {**item, "kind": item.get("kind") or kind}
            units.append(_workload_unit(item, cluster=cluster))
    return units


# -- application-identity indexes (mirrors ea_observation._owner_lookup) -----


def _owner_by_object(applications: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[str]]:
    owners: dict[tuple[str, str, str, str], list[str]] = {}
    for app in applications:
        app_id = app.get("id")
        if not app_id:
            continue
        workload = (app.get("content") or {}).get("workload") or {}
        if workload.get("runtime") != "kubernetes":
            continue
        for obj in workload.get("objects") or []:
            key = (
                str(obj.get("cluster") or ""),
                str(obj.get("namespace") or ""),
                str(obj.get("kind") or ""),
                str(obj.get("name") or ""),
            )
            owners.setdefault(key, []).append(str(app_id))
    return owners


def _owner_by_namespace_name(applications: list[dict[str, Any]]) -> dict[tuple[str, str], set[str]]:
    """``(namespace, name) -> owning application ids``, ignoring ``kind`` --
    a Service and the Deployment/StatefulSet it fronts conventionally share a
    name in this cluster, and no ``Service`` kind is separately declared in
    the model today. See .factory/design.md."""
    owners: dict[tuple[str, str], set[str]] = {}
    for app in applications:
        app_id = app.get("id")
        if not app_id:
            continue
        workload = (app.get("content") or {}).get("workload") or {}
        if workload.get("runtime") != "kubernetes":
            continue
        for obj in workload.get("objects") or []:
            key = (str(obj.get("namespace") or ""), str(obj.get("name") or ""))
            owners.setdefault(key, set()).add(str(app_id))
    return owners


def _ref_by_id(applications: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(app["id"]): str((app.get("content") or {}).get("ref"))
        for app in applications
        if app.get("id") and (app.get("content") or {}).get("ref")
    }


# -- signal 1: env-var host reference -----------------------------------------


def _hostnames_in(value: str) -> list[tuple[str, str]]:
    """``(name, namespace)`` pairs for every ``name.namespace.svc[.cluster.local]``
    substring found in a literal env value."""
    return [(match.group(1), match.group(3)) for match in _HOST_PATTERN.finditer(value.lower())]


@dataclass(frozen=True)
class DependencyEdge:
    source_id: str
    source_ref: str
    target_id: str
    target_ref: str
    method: str


def _edges_from_env(*, units: list[dict[str, Any]], applications: list[dict[str, Any]]) -> list[DependencyEdge]:
    owner_by_object = _owner_by_object(applications)
    owner_by_namespace_name = _owner_by_namespace_name(applications)
    ref_by_id = _ref_by_id(applications)
    edges: list[DependencyEdge] = []

    for unit in units:
        source_ids = owner_by_object.get((unit["cluster"], unit["namespace"], unit["kind"], unit["name"])) or []
        if len(source_ids) != 1:
            continue
        source_id = source_ids[0]
        source_ref = ref_by_id.get(source_id)
        if not source_ref:
            continue
        for container_name, env_name, value in unit["literal_envs"]:
            for host_name, host_namespace in _hostnames_in(value):
                target_ids = owner_by_namespace_name.get((host_namespace, host_name)) or set()
                if len(target_ids) != 1:
                    continue
                (target_id,) = target_ids
                if target_id == source_id:
                    continue
                target_ref = ref_by_id.get(target_id)
                if not target_ref:
                    continue
                method = (
                    f"env:{unit['kind']}/{unit['namespace']}/{unit['name']} container={container_name} "
                    f"{env_name}={value} names host {host_namespace}/{host_name}, owned by {target_ref}"
                )
                edges.append(DependencyEdge(source_id, source_ref, target_id, target_ref, method))
    return edges


# -- signal 2: NetworkPolicy egress -------------------------------------------


def _labels_match(pod_labels: dict[str, str], match_labels: dict[str, Any]) -> bool:
    return all(pod_labels.get(str(k)) == str(v) for k, v in match_labels.items())


def _units_matching_selector(
    units: list[dict[str, Any]], namespace: str, selector: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Applications this bead can name from a selector require ``matchLabels``
    that resolves to exactly one workload unit. An empty selector (matches
    every pod in the namespace) or a ``matchExpressions``-only selector
    cannot name one application, so both resolve to nothing rather than a
    guess (F1)."""
    match_labels = (selector or {}).get("matchLabels")
    if not match_labels:
        return []
    return [unit for unit in units if unit["namespace"] == namespace and _labels_match(unit["pod_labels"], match_labels)]


def _resolve_namespace_selector(selector: dict[str, Any] | None, *, default: str) -> str | None:
    if selector is None:
        return default
    match_labels = selector.get("matchLabels") or {}
    name = match_labels.get("kubernetes.io/metadata.name")
    return name if name else None


def _edges_from_networkpolicies(
    *, networkpolicies: dict[str, Any] | None, units: list[dict[str, Any]], applications: list[dict[str, Any]], cluster: str
) -> list[DependencyEdge]:
    owner_by_object = _owner_by_object(applications)
    ref_by_id = _ref_by_id(applications)
    edges: list[DependencyEdge] = []

    def _owner_of(unit: dict[str, Any]) -> tuple[str, str] | None:
        ids = owner_by_object.get((cluster, unit["namespace"], unit["kind"], unit["name"])) or []
        if len(ids) != 1:
            return None
        ref = ref_by_id.get(ids[0])
        if not ref:
            return None
        return ids[0], ref

    for policy in (networkpolicies or {}).get("items") or []:
        spec = policy.get("spec") or {}
        if "Egress" not in (spec.get("policyTypes") or []):
            continue
        policy_metadata = policy.get("metadata") or {}
        policy_namespace = str(policy_metadata.get("namespace") or "")
        policy_name = str(policy_metadata.get("name") or "")

        source_units = _units_matching_selector(units, policy_namespace, spec.get("podSelector"))
        if len(source_units) != 1:
            continue
        source_owner = _owner_of(source_units[0])
        if source_owner is None:
            continue
        source_id, source_ref = source_owner

        for rule in spec.get("egress") or []:
            for to in rule.get("to") or []:
                pod_selector = to.get("podSelector")
                if not pod_selector:
                    continue
                target_namespace = _resolve_namespace_selector(to.get("namespaceSelector"), default=policy_namespace)
                if target_namespace is None:
                    continue
                target_units = _units_matching_selector(units, target_namespace, pod_selector)
                if len(target_units) != 1:
                    continue
                target_owner = _owner_of(target_units[0])
                if target_owner is None:
                    continue
                target_id, target_ref = target_owner
                if target_id == source_id:
                    continue
                target_unit = target_units[0]
                source_unit = source_units[0]
                method = (
                    f"networkpolicy:{policy_namespace}/{policy_name} egress from "
                    f"{source_unit['kind']}/{source_unit['namespace']}/{source_unit['name']} to "
                    f"{target_unit['kind']}/{target_unit['namespace']}/{target_unit['name']}, owned by {target_ref}"
                )
                edges.append(DependencyEdge(source_id, source_ref, target_id, target_ref, method))
    return edges


# -- combine, dedupe --------------------------------------------------------


def compute_dependency_edges(
    *,
    deployments: dict[str, Any] | None,
    statefulsets: dict[str, Any] | None,
    networkpolicies: dict[str, Any] | None,
    cluster: str,
    applications: list[dict[str, Any]],
) -> list[DependencyEdge]:
    """Both signals, resolved to bead ids, deduped by ``(source_id, target_id)``
    -- the first method encountered wins; a second fact confirming the same
    edge adds no new information the writer needs."""
    units = workload_units_from_kubectl(cluster=cluster, deployments=deployments, statefulsets=statefulsets)
    candidates = _edges_from_env(units=units, applications=applications)
    candidates += _edges_from_networkpolicies(
        networkpolicies=networkpolicies, units=units, applications=applications, cluster=cluster
    )

    seen: set[tuple[str, str]] = set()
    edges: list[DependencyEdge] = []
    for edge in candidates:
        key = (edge.source_id, edge.target_id)
        if key in seen:
            continue
        seen.add(key)
        edges.append(edge)
    return edges


# -- idempotent landing, with retraction (F1b) --------------------------------


def _observed_at(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in str(value).strip() if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"


def dependency_assessment_ref(app_ref: str) -> str:
    return f"obs.ea-dependency.{_ref_part(app_ref)}"


def _dependency_assessment_content(app_ref: str, observed_at: str) -> dict[str, Any]:
    return {
        "ref": dependency_assessment_ref(app_ref),
        "observed_at": observed_at,
        "workload": {
            "cluster": "repository",
            "namespace": "architecture",
            "kind": "EaDependencyAssessment",
            "name": app_ref,
        },
        "source_class": "observed",
    }


def land_dependency_posture(
    store: DependencyObserverStore,
    *,
    applications: list[dict[str, Any]],
    units: list[dict[str, Any]],
    edges: list[DependencyEdge],
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Write this cycle's computed edges, retract edges this observer
    previously wrote that no longer hold, and record/refresh/close dated
    assessed-none observations -- one pass, per application, over the fixed
    32-application population (no open-ended "list everything active" scan is
    needed: every application's assessed-none ref is deterministic).

    Never touches a ``depends_on`` edge unless BOTH hold: its ``created_by``
    is this module's own ``CREATED_BY`` (AC-1: ea-derive's edges are left
    exactly as they are) AND its target is one of the ``arch.application``
    beads in ``applications`` (AC-7: an application -> ci edge carries a
    *different* ``created_by`` than this module's own since the AC-7 fix, but
    the target-type check holds even if some future writer ever reused this
    identity string again -- this module never justifies, and therefore never
    retracts, an edge whose target isn't an application it can name). Never
    creates an edge whose target is already linked by anyone (the
    ``land_ci_records`` existence-check discipline, honoring
    ``bead_link``'s ``UNIQUE(source_id, target_id, link_type)``).

    Edges and observations are written under two DIFFERENT identities
    (AC-7(a)): every ``depends_on`` link created or retracted here carries
    this module's own :data:`CREATED_BY`, but the assessed-none
    ``arch.observation`` bead below is created and updated under
    ``ea_observation.CREATED_BY`` (:func:`_observation_created_by`) --
    ``SOURCE_CLASS_WRITERS["observed"]`` is a closed enrollment that admits
    the former, not the latter. A ``bead_link`` carries no ``source_class``
    and is ungated by that enrollment, so it is safe for this module's edges
    to assert an identity of their own; an ``arch.observation`` bead is not.

    "Known" (AC-2) only counts an application-to-application edge -- an
    application whose sole surviving ``depends_on`` target is a non-application
    (e.g. the ``arch.ci`` edges ``land_ci_records`` writes) is the coherence
    case named at the #866 gate: not known (no application-type edge), not
    assessed-none (it does depend on something), tracked separately and
    handed to :func:`summarize_dependency_posture` below.
    """
    ref_by_id = _ref_by_id(applications)
    application_ids = {str(app["id"]) for app in applications if app.get("id")}
    edges_by_source: dict[str, dict[str, DependencyEdge]] = {}
    for edge in edges:
        edges_by_source.setdefault(edge.source_id, {})[edge.target_id] = edge

    owner_by_object = _owner_by_object(applications)
    units_by_app: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        for app_id in owner_by_object.get((unit["cluster"], unit["namespace"], unit["kind"], unit["name"])) or []:
            units_by_app.setdefault(app_id, []).append(unit)

    edges_created = 0
    edges_retracted = 0
    assessed_none_created = 0
    assessed_none_updated = 0
    assessed_none_closed = 0
    known_by_edges = 0
    assessed_none_count = 0
    unknown_count = 0
    edges_out_by_app: dict[str, list[str]] = {}
    assessed_none_refs: set[str] = set()
    ci_only_dependency_refs: set[str] = set()

    for app in applications:
        app_id = app.get("id")
        app_ref = ref_by_id.get(str(app_id)) if app_id else None
        if not app_id or not app_ref:
            continue

        desired = edges_by_source.get(str(app_id), {})
        existing_links = store.list_links(str(app_id), direction="outgoing", link_type="depends_on")
        final_targets = {str(link["target_id"]) for link in existing_links}

        for link in existing_links:
            target_id = str(link["target_id"])
            if (
                link.get("created_by") == CREATED_BY
                and target_id in application_ids
                and target_id not in desired
            ):
                store.delete_link(link["id"])
                final_targets.discard(target_id)
                edges_retracted += 1

        for target_id, edge in desired.items():
            if target_id in final_targets:
                continue
            store.create_link(
                str(app_id),
                target_id,
                "depends_on",
                created_by=CREATED_BY,
                content={"method": edge.method, "target_ref": edge.target_ref},
            )
            final_targets.add(target_id)
            edges_created += 1

        assessment_ref = dependency_assessment_ref(app_ref)
        existing_observation = store.find_observation(assessment_ref)
        application_targets = final_targets & application_ids
        ci_only = bool(final_targets) and not application_targets

        if application_targets:
            known_by_edges += 1
            edges_out_by_app[app_ref] = sorted(
                ref_by_id[t] for t in application_targets if t in ref_by_id
            )
            if existing_observation is not None and existing_observation.get("state") == "active":
                store.update_observation(existing_observation["id"], state="resolved")
                assessed_none_closed += 1
            continue

        if ci_only:
            ci_only_dependency_refs.add(app_ref)
            unknown_count += 1
            if existing_observation is not None and existing_observation.get("state") == "active":
                store.update_observation(existing_observation["id"], state="resolved")
                assessed_none_closed += 1
            continue

        owned_units = units_by_app.get(str(app_id)) or []
        fully_assessed = bool(owned_units) and not any(unit["has_unresolvable_env"] for unit in owned_units)

        if fully_assessed:
            assessed_none_count += 1
            assessed_none_refs.add(app_ref)
            observed_at = _observed_at(now_fn())
            content = _dependency_assessment_content(app_ref, observed_at)
            context = {
                "observation_kind": DEPENDENCY_ASSESSMENT_OBSERVATION_KIND,
                "application_ref": app_ref,
                "assessment": "assessed_none",
            }
            if existing_observation is None:
                store.create_observation(
                    {
                        "namespace": "arch",
                        "type": "observation",
                        "state": "active",
                        "trust_tier": "system",
                        "created_by": _observation_created_by(),
                        "content": content,
                        "context": context,
                    }
                )
                assessed_none_created += 1
            else:
                reopen_state = "active" if existing_observation.get("state") != "active" else None
                store.update_observation(
                    existing_observation["id"], content=content, state=reopen_state, context=context
                )
                assessed_none_updated += 1
        else:
            unknown_count += 1
            if existing_observation is not None and existing_observation.get("state") == "active":
                store.update_observation(existing_observation["id"], state="resolved")
                assessed_none_closed += 1

    posture_summary = summarize_dependency_posture(
        applications=applications,
        edges_out_by_app=edges_out_by_app,
        assessed_none_refs=assessed_none_refs,
        ci_only_dependency_refs=ci_only_dependency_refs,
    )

    return {
        "edges_created": edges_created,
        "edges_retracted": edges_retracted,
        "assessed_none_created": assessed_none_created,
        "assessed_none_updated": assessed_none_updated,
        "assessed_none_closed": assessed_none_closed,
        "known_by_edges": known_by_edges,
        "assessed_none": assessed_none_count,
        "unknown": unknown_count,
        "ci_only_dependency_refs": sorted(ci_only_dependency_refs),
        "posture_summary": posture_summary,
    }


# -- reporting: the AC-3 shape ------------------------------------------------


def summarize_dependency_posture(
    *,
    applications: list[dict[str, Any]],
    edges_out_by_app: dict[str, list[str]],
    assessed_none_refs: set[str],
    ci_only_dependency_refs: set[str] = frozenset(),
) -> dict[str, Any]:
    """The AC-3 shape: known / assessed_none / unknown counts, edge-based,
    plus the coherence case named separately rather than folded into either
    bucket. ``edges_out_by_app`` and ``assessed_none_refs`` are supplied by
    the caller (this bead does not itself decide what "currently in the
    store" means) -- ``land_dependency_posture`` is production's one caller,
    and the outer loop re-measures before/after counts against the live
    store separately, at the gate.
    """
    known: list[str] = []
    assessed_none: list[str] = []
    unknown: list[str] = []
    coherence_case: list[str] = []

    for app in applications:
        ref = (app.get("content") or {}).get("ref") or app.get("ref")
        if not ref:
            continue
        has_app_edge = bool(edges_out_by_app.get(ref))
        if has_app_edge:
            known.append(ref)
        elif ref in assessed_none_refs:
            assessed_none.append(ref)
        elif ref in ci_only_dependency_refs:
            coherence_case.append(ref)
            unknown.append(ref)
        else:
            unknown.append(ref)

    return {
        "total_applications": len(applications),
        "known": len(known),
        "assessed_none": len(assessed_none),
        "unknown": len(unknown),
        "known_refs": sorted(known),
        "assessed_none_refs": sorted(assessed_none),
        "unknown_refs": sorted(unknown),
        "coherence_case_refs": sorted(coherence_case),
    }


ACTIVITIES: list[Any] = []
