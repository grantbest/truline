"""Scheduled EA live-model observation run (Track B, PC-ASR-006, PC-ASR-007).

The credentialed half of Track B. ``ea_reflect.py`` is pure judgment with no
scheduler and no writer -- this module gives it both, in the same shape every
other scheduled activity in this dispatcher already uses
(``activities/staleness_report.py``, ``activities/knowledge_ingestion.py``):
Temporal executes one activity; the activity does all the I/O; the workflow
history carries only counts, never finding content (Pillar 10).

Three kinds of read happen here, all with credentials this host already holds
-- no SSH, nothing new added:

* ``kubectl get deployments,statefulsets,applications.argoproj.io -A -o json``,
  via ``cluster_health._kubectl_json`` (reused, not duplicated).
* ``kubectl get namespaces -o json``, same helper, same identity.
* the live ``arch.application`` beads from substrate -- the loaded model, the
  same source every other scheduled activity here reads from.

``ea_reflect.reflect()`` compares the workload reads to the declared model and
returns divergence findings; this module's own job is landing them
idempotently (PRIN-014): the same divergence observed on two runs updates one
standing ``arch.observation`` bead rather than minting a second, and a
divergence that stops recurring closes its record. See ``.factory/design.md``
for why ``ExternalApplicationDeclared`` is deliberately excluded from that
landing loop.

Separately, this module also lands the technology layer itself:
``arch.ci`` records for every kubectl/ArgoCD-observed workload, namespace and
database that a declared ``arch.application`` claims ownership of, each with
a ``depends_on`` edge from its owning application. This is not divergence
judgment -- it is a mirror of what already exists, so it lands unconditionally
rather than through ``ea_reflect``. See ``.factory/design.md`` for the full
design record (source_class locking, edge vocabulary reuse, ownership
derivation, the database-name heuristic, and the zero-writes-when-unchanged
contract this landing loop honors that ``land_findings`` above does not).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from temporalio import activity

logger = logging.getLogger(__name__)

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import ea_reflect  # noqa: E402
from cluster_health import MissingKubectlOutputError, _kubectl_json  # noqa: E402
import failure_diagnosis  # noqa: E402
import process_env  # noqa: E402
from activities import ea_dependency  # noqa: E402

_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)
from tools import notify  # noqa: E402

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

CREATED_BY = "factory-dispatcher/ea-observer"
OBSERVATION_KIND = "ea_reflect_finding"
HTTP_TIMEOUT_S = 30.0

# dev.finding 5c3cf2b3: list_active_findings must walk the whole active
# arch.observation population, not just page one -- see .factory/design.md
# for the full rationale, including why this walk's exhaustion check differs
# from substrate.py's BeadStore.list_beads.
ACTIVE_OBSERVATION_PAGE_SIZE = 500
# Bound on the walk, same magnitude as scripts/substrate_client.py's
# MAX_PAGES (:24) and apps/factory-dispatcher/substrate.py's MAX_PAGES
# (:73-127): 500 pages at the default page size covers 250k active
# observations -- far past the live population (1,448 on 2026-09-24) -- so
# reaching it means the server stopped honouring offset, not that the
# estate grew. Loud, per PRIN-015.
MAX_ACTIVE_OBSERVATION_PAGES = 500


class ActiveObservationPagingError(RuntimeError):
    """The active-observation paging walk (dev.finding 5c3cf2b3) could not
    trust the page it just received, and refuses to loop or return a
    partial population. Raised in exactly two cases: a page whose first
    bead id repeats the previous page's (the server is not honouring
    ``offset``), or a page count exceeding
    ``MAX_ACTIVE_OBSERVATION_PAGES`` (the walk never terminated)."""
DEFAULT_CLUSTER_ENV = "EA_OBSERVER_CLUSTER"
# Deliberately generic, not a real cluster name (R2603-5): this is the
# last-resort label used only when EA_OBSERVER_CLUSTER is unset AND `kubectl
# config current-context` itself fails. _current_kubectl_context()'s own
# contract is that an unreadable context name must never block the run, so
# the value here only needs to be a legible placeholder, never a household's
# real cluster.
DEFAULT_CLUSTER_FALLBACK = "unknown-cluster"

# R2605-8: a bare `kubectl` with no KUBECONFIG resolves to localhost:8080 and
# is refused -- every kubectl call this module makes then returns None
# through `_kubectl_json`'s own "unavailable, logged, treated as this-check-
# did-not-run" contract, which is indistinguishable from a healthy cluster
# with nothing to report. Following EA_CANONICAL_CLUSTER_ENV's precedent
# exactly (scripts/ea-derive.py's `_canonical_cluster`): only the variable
# NAME is declared here and in dispatch.VERIFICATION_ENV_ALLOWLIST; the
# household value stays in the operator's worker environment.
KUBECONFIG_ENV = "KUBECONFIG"


# -- the narrow store this activity writes/reads through ---------------------


class EAObserverStore(Protocol):
    """Deliberately has no method that can write ``arch.application`` content
    or state. The observer measures; it cannot transition a lifecycle,
    because the interface it is handed gives it no way to try.
    """

    def list_applications(self) -> list[dict[str, Any]]: ...

    def find_observation(self, ref: str) -> dict[str, Any] | None: ...

    def list_active_findings(self) -> list[dict[str, Any]]: ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def create_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict[str, Any]: ...


class CIObserverStore(Protocol):
    """Narrow store for the technology-layer ``arch.ci`` records this
    activity lands.

    Declared separately from :class:`EAObserverStore` rather than widening
    it -- the same "one tested consumer, six methods, nothing more"
    reasoning that Protocol's own docstring gives for its concern. The
    concrete :class:`SubstrateEAObserverStore` implements both; nothing
    stops one class satisfying two narrow interfaces. ``list_links`` reads
    edges already written by ``create_link`` -- it exists so a run that finds
    an ``arch.ci`` bead already landed can tell which of its declared owners
    already have a ``depends_on`` edge and which are still missing one,
    rather than assuming a matching input record means a matching edge.
    """

    def find_ci(self, ref: str) -> dict[str, Any] | None: ...

    def list_active_cis(self) -> list[dict[str, Any]]: ...

    def create_ci(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_ci(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]: ...

    def create_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict[str, Any]: ...

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict[str, Any]]: ...


class SubstrateEAObserverStore:
    """Narrow client for the bead reads/writes this activity owns.

    Deliberately not a widening of ``substrate.Substrate``: same reasoning as
    ``knowledge_ingestion.SubstrateKnowledgeStore`` and
    ``staleness_report.SubstrateObservationStore`` -- one tested consumer,
    six methods, nothing more (plus :class:`CIObserverStore`'s six, for the
    same reasoning applied to the technology-layer landing this class also
    serves).
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def list_applications(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/beads", params={"namespace": "arch", "type": "application", "limit": 500}
        )

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "observation", "content_ref": ref, "limit": 1},
        )
        return found[0] if found else None

    def list_active_findings(self) -> list[dict[str, Any]]:
        """Every active ``arch.observation`` bead whose ``context.observation_kind``
        is :data:`OBSERVATION_KIND` -- walking ``offset`` across the whole active
        population first, filtering only after every page is concatenated
        (dev.finding 5c3cf2b3: the prior single-page read put every one of the
        observer's own 52 findings beyond page one, so the close pass below
        resolved zero of them every run).

        Exhaustion is judged by comparing each page's length to the
        *previous* page's actual length, not to the fixed
        ``ACTIVE_OBSERVATION_PAGE_SIZE`` requested: the first page is never
        treated as short on its own (nothing yet to compare it to), and from
        the second page on, a page shorter than its predecessor -- or
        genuinely empty -- ends the walk. This tolerates a server that caps
        a response below the ``limit`` requested (every page reports the
        same length as the one before it, so the walk keeps going by actual
        rows consumed) while still stopping immediately on a real server's
        true final page, exactly where ``len(page) < limit`` would have
        stopped it too. See .factory/design.md.

        Bounded two ways, the first to trigger wins, both raising
        :class:`ActiveObservationPagingError` rather than looping or
        returning a partial population: a page whose first bead id repeats
        the previous page's (the server is not honouring ``offset``), and a
        page count exceeding :data:`MAX_ACTIVE_OBSERVATION_PAGES`.
        """
        pages: list[list[dict[str, Any]]] = []
        offset = 0
        page_count = 0
        previous_length: int | None = None
        previous_first_id: Any = None
        while True:
            page_count += 1
            if page_count > MAX_ACTIVE_OBSERVATION_PAGES:
                raise ActiveObservationPagingError(
                    f"active-observation paging did not exhaust after "
                    f"{MAX_ACTIVE_OBSERVATION_PAGES} pages of {ACTIVE_OBSERVATION_PAGE_SIZE} "
                    f"(attempting page {page_count}, offset {offset}). Either the active "
                    "population is larger than this walk is designed for, or the server is "
                    "not honouring offset. Refusing to return a population that may be "
                    "silently incomplete."
                )
            params: dict[str, Any] = {
                "namespace": "arch",
                "type": "observation",
                "state": "active",
                "limit": ACTIVE_OBSERVATION_PAGE_SIZE,
            }
            if offset:
                params["offset"] = offset
            page = self._request("GET", "/beads", params=params)
            if page:
                first_id = page[0].get("id")
                if previous_first_id is not None and first_id == previous_first_id:
                    raise ActiveObservationPagingError(
                        f"active-observation page at offset {offset} repeats the previous "
                        f"page's first bead id ({first_id!r}) -- the server is not honouring "
                        "offset. Refusing to loop or return a partial population."
                    )
                previous_first_id = first_id
            pages.append(page)
            page_length = len(page)
            is_final = page_length == 0 or (
                previous_length is not None and page_length < previous_length
            )
            if is_final:
                break
            previous_length = page_length
            offset += page_length

        found = [bead for page in pages for bead in page]
        return [
            bead for bead in found if (bead.get("context") or {}).get("observation_kind") == OBSERVATION_KIND
        ]

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"created_by": CREATED_BY}
        if content is not None:
            body["content"] = content
        if state is not None:
            body["state"] = state
        if context is not None:
            body["context"] = context
        return self._request("PATCH", f"/beads/{bead_id}", json=body)

    def create_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str,
        created_by: str,
        content: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /beads/{source_id}/links. ``created_by`` travels as the
        ``X-Created-By`` header, per the route's contract — unlike every
        other write here, this endpoint does not read it from the JSON
        body (same shape as ``substrate.Substrate.add_link``). ``content``
        is optional and, when given, is what ``ea_dependency.py`` uses to
        carry an edge's justification (``method``/``target_ref``) on the
        edge itself -- existing callers that never pass it are unaffected,
        since ``BeadLinkCreate.content`` already defaults to ``{}``."""
        body: dict[str, Any] = {"target_id": target_id, "link_type": link_type}
        if content is not None:
            body["content"] = content
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            headers={"X-Created-By": created_by},
            json=body,
        )

    def delete_link(self, link_id: str) -> Any:
        """DELETE /links/{link_id} (routes.py:1044) -- the retraction path
        ``ea_dependency.land_dependency_posture`` needs (F1b): an edge this
        observer previously wrote and can no longer justify from current
        cluster facts must be removable, not merely stale forever."""
        return self._request("DELETE", f"/links/{link_id}")

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /beads/{bead_id}/links?direction=&link_type=. Returns the real
        ``BeadLinkRead`` shape (``source_id``, ``target_id``, ``link_type``,
        ...) -- no adaptation needed, the caller reads ``source_id`` off it
        directly."""
        params: dict[str, Any] = {"direction": direction}
        if link_type is not None:
            params["link_type"] = link_type
        return self._request("GET", f"/beads/{bead_id}/links", params=params)

    def find_ci(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "ci", "content_ref": ref, "limit": 1},
        )
        return found[0] if found else None

    def list_active_cis(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/beads", params={"namespace": "arch", "type": "ci", "state": "active", "limit": 500}
        )

    def create_ci(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_ci(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"created_by": CREATED_BY}
        if content is not None:
            body["content"] = content
        if state is not None:
            body["state"] = state
        return self._request("PATCH", f"/beads/{bead_id}", json=body)


def default_store() -> EAObserverStore:
    return SubstrateEAObserverStore()


# -- credentialed collection: kubectl, no new credential ----------------------


def _require_kubeconfig() -> None:
    """Refuse to shell out to kubectl without declared configuration.

    A bare ``kubectl`` with no ``KUBECONFIG`` resolves to ``localhost:8080``
    and is refused -- indistinguishable, through ``_kubectl_json``'s
    None-on-error contract, from a healthy cluster that legitimately has
    nothing to report for a resource type. Refusing here, before any kubectl
    call is attempted, turns that silent misconfiguration into an
    immediate, named failure instead. Names only the variable, never its
    value or any path it resolves to (an operator's kubeconfig path can
    itself be sensitive).
    """
    if not os.environ.get(KUBECONFIG_ENV):
        raise RuntimeError(
            f"{KUBECONFIG_ENV} is not set. The EA observer refuses to invoke kubectl against "
            "whatever configuration a bare invocation would resolve to rather than silently "
            f"observing nothing. Set {KUBECONFIG_ENV} in the operator's worker environment and "
            "try again."
        )


def _current_kubectl_context() -> str:
    """The cluster identity to stamp on observations.

    Read from ``EA_OBSERVER_CLUSTER`` if set (kubectl's context name need not
    match the model's ``cluster`` string), else from
    ``kubectl config current-context``, else a fallback. Never fails the run:
    an unreadable context name is not a reason to skip observing.
    """
    override = os.environ.get(DEFAULT_CLUSTER_ENV)
    if override:
        return override
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_CLUSTER_FALLBACK
    return proc.stdout.strip() or DEFAULT_CLUSTER_FALLBACK


def _argocd_owner_by_identity(applications: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """Map (namespace, kind, name) of every resource an ArgoCD Application
    reports owning, to that Application's name."""
    owners: dict[tuple[str, str, str], str] = {}
    for application in applications.get("items") or []:
        app_name = str((application.get("metadata") or {}).get("name") or "")
        status = application.get("status") or {}
        for resource in status.get("resources") or []:
            key = (
                str(resource.get("namespace") or ""),
                str(resource.get("kind") or ""),
                str(resource.get("name") or ""),
            )
            owners[key] = app_name
    return owners


def workload_observations_from_kubectl(
    *,
    cluster: str,
    deployments: dict[str, Any] | None,
    statefulsets: dict[str, Any] | None,
    applications: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Pure translation from parsed ``kubectl get -o json`` output to the
    observation shape ``ea_reflect.reflect`` expects. Takes already-parsed
    dicts and runs no subprocess -- same split as ``cluster_health.py``'s
    checking functions, so this is testable from fixtures alone.
    """
    owners = _argocd_owner_by_identity(applications or {"items": []})
    observations: list[dict[str, Any]] = []
    for kind, payload in (("Deployment", deployments), ("StatefulSet", statefulsets)):
        for item in (payload or {}).get("items") or []:
            metadata = item.get("metadata") or {}
            status = item.get("status") or {}
            namespace = str(metadata.get("namespace") or "")
            name = str(metadata.get("name") or "")
            observations.append(
                {
                    "cluster": cluster,
                    "namespace": namespace,
                    "kind": kind,
                    "name": name,
                    "replicas": status.get("replicas"),
                    "ready_replicas": status.get("readyReplicas"),
                    "owning_argocd_application": owners.get((namespace, kind, name)),
                }
            )
    return observations


def collect_cluster_observations(*, runner: Any = _kubectl_json) -> list[dict[str, Any]]:
    """Read-only kubectl collection. Adds no credential: it shells out with
    whatever identity this host's kubeconfig already holds, the same wrapper
    ``cluster_health.py`` already uses for the same reason.

    ``runner`` mirrors ``cluster_health.collect_kubectl_snapshot(runner=...)``'s
    own injection seam (R2605-8): when every tracked resource type comes back
    ``None``, this raises ``cluster_health.MissingKubectlOutputError`` rather
    than silently returning an empty observation list -- the same "all four
    empty is the checker itself being broken, not a clean run" reasoning
    ``collect_kubectl_snapshot`` already applies, now shared by this caller
    instead of skipped by it.
    """
    _require_kubeconfig()
    cluster = _current_kubectl_context()
    deployments = runner("deployments", "-A")
    statefulsets = runner("statefulsets", "-A")
    applications = runner("applications.argoproj.io", "-A")
    if deployments is None and statefulsets is None and applications is None:
        raise MissingKubectlOutputError(
            "kubectl produced no output for any tracked resource type (deployments, "
            "statefulsets, applications.argoproj.io) in the EA observer's workload "
            "collection. kubectl may be missing or the cluster may be unreachable."
        )
    return workload_observations_from_kubectl(
        cluster=cluster,
        deployments=deployments,
        statefulsets=statefulsets,
        applications=applications,
    )


def _namespace_names_from_kubectl(payload: dict[str, Any] | None) -> list[str]:
    """Pure translation from parsed ``kubectl get namespaces -o json`` output
    to a name list -- same split as ``workload_observations_from_kubectl``,
    testable from fixtures alone.
    """
    items = (payload or {}).get("items") or []
    names = [str((item.get("metadata") or {}).get("name") or "") for item in items]
    return [name for name in names if name]


def collect_cluster_namespaces(*, runner: Any = _kubectl_json) -> dict[str, Any]:
    """Read-only kubectl collection of live namespace identity. No new
    credential -- same kubeconfig identity as every other ``collect_cluster_*``
    call in this module; namespaces are cluster-scoped, so no ``-A``.

    Raises ``cluster_health.MissingKubectlOutputError`` when the one tracked
    resource type here (namespaces) comes back ``None`` -- the single-call
    analogue of ``collect_cluster_observations``'s all-None guard (R2605-8):
    a namespace-only kubectl failure would otherwise silently starve every
    namespace-owned ``arch.ci`` record with no signal.
    """
    _require_kubeconfig()
    cluster = _current_kubectl_context()
    namespaces = runner("namespaces")
    if namespaces is None:
        raise MissingKubectlOutputError(
            "kubectl produced no output for the tracked resource type (namespaces) in the "
            "EA observer's namespace collection. kubectl may be missing or the cluster may "
            "be unreachable."
        )
    return {
        "cluster": cluster,
        "names": _namespace_names_from_kubectl(namespaces),
    }


# -- idempotent landing (PRIN-014) --------------------------------------------


def _observed_at(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"


def _observation_ref(identity: str) -> str:
    return f"obs.ea-reflect.{_ref_part(identity)}"


def _finding_workload(finding: ea_reflect.ReflectFinding) -> ea_reflect.WorkloadRef:
    for attr in ("observed_object", "declared_object"):
        ref = getattr(finding, attr, None)
        if ref is not None:
            return ref
    raise TypeError(f"finding carries no workload reference to land: {finding!r}")


def _finding_content(finding: ea_reflect.ReflectFinding, ref: str, observed_at: str) -> dict[str, Any]:
    workload = _finding_workload(finding)
    return {
        "ref": ref,
        "observed_at": observed_at,
        "workload": {
            "cluster": workload.cluster,
            "namespace": workload.namespace,
            "kind": workload.kind,
            "name": workload.name,
        },
        "source_class": "observed",
    }


def _finding_context(finding: ea_reflect.ReflectFinding, identity: str) -> dict[str, Any]:
    context: dict[str, Any] = {
        "observation_kind": OBSERVATION_KIND,
        "finding_kind": finding.kind,
        "identity": identity,
    }
    application_ref = getattr(finding, "application_ref", None)
    if application_ref:
        context["application_ref"] = application_ref
    reason = getattr(finding, "reason", None)
    if reason:
        context["reason"] = reason
    return context


def _application_bead_ids(applications: list[dict[str, Any]]) -> dict[str, str]:
    """``content.ref -> bead id`` for every application that has both.

    The ``measures`` edge must carry the measured application's real bead
    UUID -- ``POST /beads/{id}/links`` validates ``target_id`` as
    ``BeadLinkCreate.target_id: UUID4`` and rejects a content ref string
    with 422 (the defect this module now fixes; see .factory/design.md).
    Resolving up front, once, means ``create_link`` is never even asked to
    write an unresolvable target -- there is nothing for it to fail on.
    """
    resolved: dict[str, str] = {}
    for app in applications:
        ref = app.get("ref")
        bead_id = app.get("id")
        if ref and bead_id:
            resolved[str(ref)] = str(bead_id)
    return resolved


def land_findings(
    store: EAObserverStore,
    findings: list[ea_reflect.ReflectFinding],
    *,
    applications: list[dict[str, Any]],
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Land findings as standing ``arch.observation`` beads, one per
    ``ea_reflect.finding_identity`` -- update in place if the identity
    already has a record, close records whose identity did not recur.

    ``applications`` resolves each finding's ``application_ref`` (a content
    ref) to its bead id for the ``measures`` edge. A ref with no entry in
    ``applications`` does not abandon the pass: the observation still lands,
    the unresolvable ref is reported in ``unresolved_application_refs``
    instead, and every remaining finding is still processed.
    """
    observed_at = _observed_at(now_fn())
    application_ids = _application_bead_ids(applications)
    seen_refs: set[str] = set()
    unresolved_refs: set[str] = set()
    created = 0
    updated = 0
    closed = 0
    skipped_external = 0

    for finding in findings:
        if isinstance(finding, ea_reflect.ExternalApplicationDeclared):
            # Never landed and never probed -- see .factory/design.md.
            skipped_external += 1
            continue

        identity = ea_reflect.finding_identity(finding)
        ref = _observation_ref(identity)
        seen_refs.add(ref)
        content = _finding_content(finding, ref, observed_at)
        context = _finding_context(finding, identity)

        existing = store.find_observation(ref)
        if existing is None:
            bead = store.create_observation(
                {
                    "namespace": "arch",
                    "type": "observation",
                    "state": "active",
                    "trust_tier": "system",
                    "created_by": CREATED_BY,
                    "content": content,
                    "context": context,
                }
            )
            application_ref = getattr(finding, "application_ref", None)
            if application_ref:
                application_id = application_ids.get(application_ref)
                if application_id:
                    store.create_link(
                        bead["id"], application_id, "measures", created_by=CREATED_BY
                    )
                else:
                    unresolved_refs.add(application_ref)
            created += 1
        else:
            reopen_state = "active" if existing.get("state") != "active" else None
            store.update_observation(existing["id"], content=content, state=reopen_state)
            updated += 1

    for existing in store.list_active_findings():
        ref = (existing.get("content") or {}).get("ref")
        if ref and ref not in seen_refs:
            store.update_observation(existing["id"], state="resolved")
            closed += 1

    return {
        "status": "observed",
        "checked": len(findings),
        "created": created,
        "updated": updated,
        "closed": closed,
        "skipped_external": skipped_external,
        "unresolved_application_refs": sorted(unresolved_refs),
    }


# -- the technology layer: arch.ci landing (PC-ASR-007) ----------------------


DATABASE_NAME_MARKERS = (
    "postgres",
    "postgresql",
    "redis",
    "mysql",
    "mariadb",
    "mongo",
    "mongodb",
    "cockroach",
    "cassandra",
)


def _ci_kind_for_workload(name: str) -> str:
    """Workload vs. database, by name -- a stated heuristic, not a claim of
    certainty (.factory/design.md §5). kubectl's Deployment/StatefulSet JSON
    carries no semantic "this is a database" flag; ``"db"`` alone is
    deliberately excluded from the marker list -- too broad, matches
    unrelated names.
    """
    lowered = name.lower()
    if any(marker in lowered for marker in DATABASE_NAME_MARKERS):
        return "database"
    return "workload"


def _ci_ref(*, cluster: str, namespace: str, kind: str, name: str) -> str:
    return f"ci.{_ref_part(cluster)}.{_ref_part(namespace)}.{_ref_part(kind)}.{_ref_part(name)}"


def _owner_lookup(applications: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[str]]:
    """``(cluster, namespace, kind, name) -> owning application bead ids``.

    Reads declared ``workload.objects`` the same way the ``measures`` edge
    above already cross-references, but keeps each application's real bead
    ``id`` rather than its ``content.ref`` -- ``POST /beads/{id}/links``
    requires real UUIDs on both ends (``BeadLinkCreate.target_id: UUID4``),
    the same resolution ``land_findings`` now does for the ``measures``
    writer via ``_application_bead_ids``. See .factory/design.md.
    """
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


def ci_records_from_observations(
    *,
    observations: list[dict[str, Any]],
    applications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Live kubectl-observed workloads -> CI record drafts.

    An object kubectl reports that no ``arch.application`` declares in its
    ``workload.objects`` still lands here, with ``owner_ids: []`` -- ownership
    is read off the declared model, never guessed, but a workload the
    observer cannot attribute is a fact about the technology layer too, not a
    reason to drop it (a dropped object and an owned-and-healthy object were
    both indistinguishable "nothing to report" before this change). Surfacing
    *why* it is unattributed is ``ea_reflect.UnmodelledWorkload``'s job
    already; duplicating its judgment here would be a second implementation
    of the same finding. ``land_ci_records`` is what turns an empty
    ``owner_ids`` into "land the record, skip the edge."
    """
    owners = _owner_lookup(applications)
    records: list[dict[str, Any]] = []
    for obs in observations:
        cluster = str(obs.get("cluster") or "")
        namespace = str(obs.get("namespace") or "")
        kind = str(obs.get("kind") or "")
        name = str(obs.get("name") or "")
        owner_ids = owners.get((cluster, namespace, kind, name)) or []
        records.append(
            {
                "ref": _ci_ref(cluster=cluster, namespace=namespace, kind=kind, name=name),
                "ci_kind": _ci_kind_for_workload(name),
                "cluster": cluster,
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "owner_ids": owner_ids,
            }
        )
    return records


def ci_records_from_namespaces(
    *,
    cluster: str,
    namespace_names: list[str],
    applications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Live namespaces -> CI record drafts, fanning out to every application
    with declared presence there. A shared namespace legitimately has more
    than one owner; picking one arbitrarily would misrepresent the model.
    """
    owners = _owner_lookup(applications)
    owners_by_namespace: dict[str, set[str]] = {}
    for (obj_cluster, obj_namespace, _kind, _name), owner_ids in owners.items():
        if obj_cluster != cluster:
            continue
        owners_by_namespace.setdefault(obj_namespace, set()).update(owner_ids)

    records: list[dict[str, Any]] = []
    for name in namespace_names:
        owner_ids = sorted(owners_by_namespace.get(name, set()))
        if not owner_ids:
            continue
        records.append(
            {
                "ref": _ci_ref(cluster=cluster, namespace=name, kind="Namespace", name=name),
                "ci_kind": "namespace",
                "cluster": cluster,
                "namespace": name,
                "kind": "Namespace",
                "name": name,
                "owner_ids": owner_ids,
            }
        )
    return records


def _ci_content(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": record["ref"],
        "ci_kind": record["ci_kind"],
        "cluster": record["cluster"],
        "namespace": record["namespace"],
        "kind": record["kind"],
        "name": record["name"],
        "source_class": "observed",
    }


def land_ci_records(store: CIObserverStore, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Land CI records idempotently: create, update only on a real change,
    close on vanish, reopen without a second edge -- mirroring
    ``land_findings``, except a cycle that observes no change performs
    literally zero writes. Unlike an ``arch.observation``, CI content carries
    no per-cycle timestamp to force a write every run -- identity and
    attributes are the whole fact, so an unchanged fact needs no write.

    ``attributed``/``unattributed`` are tallied from every record processed
    this cycle, not just newly-created ones -- a workload that landed
    unattributed last run and is still unattributed this run performs zero
    writes below, but must still count on the status bead every run, the same
    "ran and found nothing new" distinction ``land_findings``'s own
    ``checked`` already draws. The count is read off confirmed edges, not the
    input record: on the existing-record path, any owner in
    ``record["owner_ids"]`` that has no ``depends_on`` edge yet gets one
    created here (``_ci_content`` deliberately excludes ``owner_ids``, so a
    workload landed unattributed and later attributed produces byte-identical
    content and would otherwise never take a write on this path at all) --
    ``attributed`` is only ever true for a record whose edge this run has just
    confirmed exists, never for one the input merely claims should.
    """
    seen_refs: set[str] = set()
    created = 0
    updated = 0
    closed = 0
    linked = 0
    attributed = 0
    unattributed = 0

    for record in records:
        ref = record["ref"]
        seen_refs.add(ref)
        content = _ci_content(record)
        owner_ids = record["owner_ids"]

        existing = store.find_ci(ref)
        is_new = existing is None
        if is_new:
            bead = store.create_ci(
                {
                    "namespace": "arch",
                    "type": "ci",
                    "state": "active",
                    "trust_tier": "system",
                    "created_by": CREATED_BY,
                    "content": content,
                }
            )
            for owner_id in owner_ids:
                store.create_link(
                    owner_id, bead["id"], "depends_on", created_by=CREATED_BY
                )
                linked += 1
        else:
            if owner_ids:
                existing_owner_ids = {
                    link["source_id"]
                    for link in store.list_links(
                        existing["id"], direction="incoming", link_type="depends_on"
                    )
                }
                for owner_id in owner_ids:
                    if owner_id not in existing_owner_ids:
                        store.create_link(
                            owner_id, existing["id"], "depends_on", created_by=CREATED_BY
                        )
                        linked += 1

            needs_reopen = existing.get("state") != "active"
            needs_content = existing.get("content") != content
            if needs_reopen or needs_content:
                store.update_ci(
                    existing["id"],
                    content=content if needs_content else None,
                    state="active" if needs_reopen else None,
                )
                updated += 1

        if is_new:
            created += 1
        if owner_ids:
            attributed += 1
        else:
            unattributed += 1

    for existing in store.list_active_cis():
        ref = (existing.get("content") or {}).get("ref")
        if ref and ref not in seen_refs:
            store.update_ci(existing["id"], state="resolved")
            closed += 1

    return {
        "checked": len(records),
        "created": created,
        "updated": updated,
        "closed": closed,
        "linked": linked,
        "attributed": attributed,
        "unattributed": unattributed,
    }


# -- cluster identity: can this run's observed and declared strings even be
# compared? (PRIN-015, S51-3 second filing) -------------------------------


CLUSTER_IDENTITY_OK = "ok"
CLUSTER_IDENTITY_CANNOT_EVALUATE = "cannot_evaluate"


def _observed_cluster_strings(observations: list[dict[str, Any]]) -> set[str]:
    return {str(obs["cluster"]) for obs in observations if obs.get("cluster")}


def _declared_cluster_strings(applications: list[dict[str, Any]]) -> set[str]:
    clusters: set[str] = set()
    for app in applications:
        workload = (app.get("content") or {}).get("workload") or {}
        if workload.get("runtime") != "kubernetes":
            continue
        for obj in workload.get("objects") or []:
            cluster = obj.get("cluster")
            if cluster:
                clusters.add(str(cluster))
    return clusters


def evaluate_cluster_identity(
    *, observations: list[dict[str, Any]], applications: list[dict[str, Any]]
) -> dict[str, Any]:
    """Whether this run's observed and declared cluster strings can even be
    compared, before a single CI record is drafted from them.

    ``_owner_lookup``'s match key opens with ``cluster`` -- when the set of
    cluster strings this run observed shares no member with the set the
    model declares, every owner lookup this run performs fails on that first
    key alone, which reads exactly like a healthy cluster with nothing to
    report. That is PRIN-015's shape: a check reporting healthy because it
    could not actually evaluate its question. Checked here, as its own named
    condition, rather than inferred later from an all-empty CI landing.

    Only evaluated when both sides have something to compare (each set
    non-empty) -- a cluster with zero live workloads, or a model with no
    kubernetes-runtime applications yet, is a legitimately different
    condition, not this one.
    """
    observed_clusters = _observed_cluster_strings(observations)
    declared_clusters = _declared_cluster_strings(applications)
    if observed_clusters and declared_clusters and observed_clusters.isdisjoint(declared_clusters):
        condition = CLUSTER_IDENTITY_CANNOT_EVALUATE
    else:
        condition = CLUSTER_IDENTITY_OK
    return {
        "condition": condition,
        "observed_clusters": sorted(observed_clusters),
        "declared_clusters": sorted(declared_clusters),
    }


CLUSTER_MISMATCH_ALERT_ID = "factory_dispatcher.ea_observer_cluster_mismatch"


async def _announce_cluster_identity_mismatch_async(
    observed_clusters: list[str], declared_clusters: list[str], policy: "notify.AlertPolicy"
) -> bool:
    observed_text = ", ".join(observed_clusters) or "(none)"
    declared_text = ", ".join(declared_clusters) or "(none)"
    content = (
        "The EA observer's kubectl cluster identity shares no member with the model's "
        f"declared workload cluster identity: observed [{observed_text}] vs declared "
        f"[{declared_text}]. Every owner lookup fails on the cluster key alone, so no "
        "arch.ci landed this run."
    )
    fingerprint = notify.alert_content_fingerprint(
        f"{'|'.join(sorted(observed_clusters))}::{'|'.join(sorted(declared_clusters))}"
    )
    return await notify.send_alert(
        policy,
        CLUSTER_MISMATCH_ALERT_ID,
        fingerprint,
        content,
        template_values={"observed_clusters": observed_text, "declared_clusters": declared_text},
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_cluster_identity_mismatch(
    observed_clusters: list[str],
    declared_clusters: list[str],
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a cannot-evaluate cluster-identity run.

    Best-effort, matching ``doctrine_registry_view.announce_registry_drift``:
    alerting must never crash a run whose standing observation already
    landed. Fingerprinted on the two cluster string sets alone (never a
    timestamp), so the identical mismatch recurring night after night is one
    suppressed-after-first-post alert, not a repost every run (the OPS-69
    class this bead's acceptance criteria name).
    """
    try:
        return asyncio.run(
            _announce_cluster_identity_mismatch_async(
                observed_clusters,
                declared_clusters,
                policy or failure_diagnosis.dev_task_alert_policy(),
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the nightly.
        logger.exception(
            "could not announce EA observer cluster identity mismatch "
            "(observed=%s, declared=%s); the standing observation is unaffected",
            observed_clusters,
            declared_clusters,
        )
        return False


def _application_model(beads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt substrate ``arch.application`` bead reads to the model shape
    ``ea_reflect.reflect`` expects.

    ``ea_reflect.py`` was written against the git YAML model shape, where
    ``ref`` sits beside ``state``/``content`` on each application entry. A
    substrate bead read carries the same business ``ref`` nested inside
    ``content`` instead (the bead's own top-level identity is its UUID
    ``id``), so it is promoted here rather than teaching the pure reflector
    two input shapes. ``id`` is carried through alongside it -- unused by
    ``ea_reflect.reflect``, but it is what lets ``land_findings`` resolve a
    finding's ``application_ref`` back to the bead the ``measures`` edge
    must actually point at.
    """
    return [
        {
            "id": bead.get("id"),
            "ref": (bead.get("content") or {}).get("ref"),
            "state": bead.get("state"),
            "content": bead.get("content"),
        }
        for bead in beads
    ]


#: R2605-8 DEFECT 1's other half: landing zero ``arch.ci`` records because the
#: collector was blind must never read as a green ``EAObservationWorkflow``
#: run. This is the PRIN-008 standing-failure-record half of the acceptance
#: criterion, mirroring ``activities/ea_apply.py`` /
#: ``activities/requirements_apply.py``'s own status beads -- see
#: .factory/design.md for why it is a local ref/content/context shape here
#: rather than a reuse of ``activities/status_bead.py`` (that module's
#: ``StatusSubstrate`` Protocol wants ``list_beads``/``create``/``patch``;
#: ``EAObserverStore`` is the narrower, content-ref-keyed REST shape the rest
#: of this module already uses). S51-3's second filing closed the other half
#: this comment used to name out of scope: landing zero ``arch.ci``
#: because the observed and declared *cluster strings* disagree -- collection
#: succeeded, so this is not ``status: failed`` -- now reads as its own
#: ``status: cannot_evaluate`` condition, raised through
#: ``factory_dispatcher.ea_observer_cluster_mismatch`` in
#: ``apps/mcp-hub/src/tools/notify.py.ALERT_INVENTORY``, never a log line.
OBSERVER_STATUS_REF = "obs.ea-observer-status"


def _observer_status_content(observed_at: str) -> dict[str, Any]:
    return {
        "ref": OBSERVER_STATUS_REF,
        "observed_at": observed_at,
        "workload": {
            "cluster": "repository",
            "namespace": "architecture",
            "kind": "EaObserver",
            "name": "ea_observation.observe_ea_model",
        },
        "source_class": "observed",
    }


def _prior_observer_failure(context: dict[str, Any]) -> dict[str, Any] | None:
    if context.get("status") != "failed":
        return None
    return {"reason": context.get("reason"), "failed_at": context.get("failed_at")}


def _write_observer_status(
    store: EAObserverStore,
    *,
    status: str,
    reason: str | None,
    now: datetime,
    observed_clusters: list[str] | None = None,
    declared_clusters: list[str] | None = None,
    ci_counts: dict[str, int] | None = None,
    dependency_posture: dict[str, Any] | None = None,
) -> None:
    """Standing ``arch.observation`` bead reporting whether the observer's own
    collection succeeded this run -- refreshed on every run (PRIN-008), never
    only on the runs that found something.

    Three conditions, kept structurally distinct (PRIN-015 -- a control that
    cannot evaluate its question fails closed): ``"ok"`` (collected and, when
    cluster identity was evaluable, landed the technology layer cleanly),
    ``"failed"`` (collection itself could not see the cluster -- R2605-8
    DEFECT 1, unchanged), and ``"cannot_evaluate"`` (collection succeeded but
    the observed and declared cluster strings share no member, so no CI
    landing was even attempted this run -- S51-3's second filing). A frozen
    ``observed_at`` means the workflow stopped firing entirely; any of these
    three ``status`` values means it fired and reports what it found.
    """
    observed_at = _observed_at(now)
    existing = store.find_observation(OBSERVER_STATUS_REF)
    prior_context = (existing.get("context") or {}) if existing else {}
    content = _observer_status_content(observed_at)

    if status == "ok":
        context: dict[str, Any] = {
            "status": "ok",
            "ci_counts": ci_counts or {"attributed": 0, "unattributed": 0},
        }
        if dependency_posture is not None:
            # scripts/ea-coverage.py's technology-layer row (S53-4) is a pure
            # function over this bead alone (measure_technology_coverage) --
            # this is that row's only source for the known/assessed-none/
            # unknown split ea_dependency.summarize_dependency_posture already
            # computes every run and would otherwise be discarded once this
            # activity's return value leaves Temporal history. See .factory/design.md.
            context["dependency_posture"] = dependency_posture
        recovered_from = _prior_observer_failure(prior_context)
        if recovered_from is not None:
            context["recovered_from"] = recovered_from
    elif status == "cannot_evaluate":
        context = {
            "status": "cannot_evaluate",
            "reason": reason,
            "checked_at": observed_at,
            "observed_clusters": observed_clusters or [],
            "declared_clusters": declared_clusters or [],
        }
    else:
        previous_failure = _prior_observer_failure(prior_context)
        repeated = previous_failure is not None and previous_failure.get("reason") == reason
        consecutive_failures = (
            prior_context.get("consecutive_failures", 1) + 1 if repeated else 1
        )
        context = {
            "status": "failed",
            "reason": reason,
            "failed_at": observed_at,
            "consecutive_failures": consecutive_failures,
        }
        if repeated:
            context["repeat_of_previous_failure"] = True
        if previous_failure is not None:
            context["previous_failure"] = previous_failure

    if existing is None:
        store.create_observation(
            {
                "namespace": "arch",
                "type": "observation",
                "state": "active",
                "trust_tier": "system",
                "created_by": CREATED_BY,
                "content": content,
                "context": context,
            }
        )
    else:
        reopen_state = "active" if existing.get("state") != "active" else None
        store.update_observation(
            existing["id"], content=content, state=reopen_state, context=context
        )


_NO_DEPENDENCY_LANDING = {
    "edges_created": 0,
    "edges_retracted": 0,
    "assessed_none_created": 0,
    "assessed_none_updated": 0,
    "assessed_none_closed": 0,
    "known_by_edges": 0,
    "assessed_none": 0,
    "unknown": 0,
}


def observe_ea_model(
    store: EAObserverStore,
    *,
    collect_observations_fn: Any = collect_cluster_observations,
    collect_namespaces_fn: Any = collect_cluster_namespaces,
    collect_dependency_facts_fn: Any = ea_dependency.no_dependency_facts,
    reflect_fn: Any = ea_reflect.reflect,
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Land both halves of the observer's pass in one activity execution:
    divergence findings (unchanged) and, per PC-ASR-007, the technology
    layer itself -- ``arch.ci`` records for what kubectl/ArgoCD already
    report, mirrored rather than judged. ``store`` must satisfy both
    :class:`EAObserverStore` and :class:`CIObserverStore`; the concrete
    :class:`SubstrateEAObserverStore` does.

    A collector that cannot see the cluster (``MissingKubectlOutputError``,
    including the ``KUBECONFIG`` refusal) writes the standing observer-status
    failure record and re-raises (R2605-8 DEFECT 1) -- ``EAObservationWorkflow``
    then reports Failed instead of Completed over zero landed records.

    A collector that *can* see the cluster but whose cluster-identity string
    shares no member with the model's declared strings cannot evaluate
    ownership -- or anything else about this run's data -- at all (PRIN-015):
    the identity check runs before ``reflect_fn``/``land_findings`` are even
    called, so a cannot-evaluate run drafts no CI record *and* lands no
    finding derived from this run's observations (see .factory/design.md; a
    prior version of this bead let findings land here regardless, which is
    the defect .factory/design.md records fixing). The standing bead records
    ``status: cannot_evaluate`` instead of ``ok``.

    A run whose cluster identity *is* evaluable records ``status: ok`` with
    the attributed/unattributed CI counts landed this cycle, and lands
    findings exactly as before.
    """
    now = now_fn()
    applications = store.list_applications()
    application_model = _application_model(applications)

    try:
        observations = collect_observations_fn()
        namespaces = collect_namespaces_fn()
    except RuntimeError as exc:
        # Covers both `MissingKubectlOutputError` (kubectl ran and saw
        # nothing anywhere) and the plain `RuntimeError` `_require_kubeconfig`
        # raises (kubectl was never even invoked) -- both mean this run could
        # not observe the cluster, so both get the same standing record.
        _write_observer_status(store, status="failed", reason=str(exc), now=now)
        raise

    cluster_identity = evaluate_cluster_identity(observations=observations, applications=applications)
    if cluster_identity["condition"] == CLUSTER_IDENTITY_CANNOT_EVALUATE:
        # Evaluated before `reflect_fn`/`land_findings` run at all: this
        # run's observations and the model's declared cluster strings share
        # no member, so no finding derived from these observations may land
        # either, not only no CI record. See .factory/design.md.
        observed_clusters = cluster_identity["observed_clusters"]
        declared_clusters = cluster_identity["declared_clusters"]
        reason = (
            f"observed cluster strings {observed_clusters} share no member with declared "
            f"cluster strings {declared_clusters}"
        )
        result: dict[str, Any] = {
            "status": CLUSTER_IDENTITY_CANNOT_EVALUATE,
            "checked": 0,
            "created": 0,
            "updated": 0,
            "closed": 0,
            "skipped_external": 0,
            "unresolved_application_refs": [],
            "ci": {
                "status": CLUSTER_IDENTITY_CANNOT_EVALUATE,
                "checked": 0,
                "created": 0,
                "updated": 0,
                "closed": 0,
                "linked": 0,
                "attributed": 0,
                "unattributed": 0,
            },
        }
        result["dependencies"] = dict(_NO_DEPENDENCY_LANDING)
        result["dependencies"]["ci_only_dependency_refs"] = []
        result["dependencies"]["posture_summary"] = ea_dependency.summarize_dependency_posture(
            applications=applications,
            edges_out_by_app={},
            assessed_none_refs=set(),
            ci_only_dependency_refs=set(),
        )
        _write_observer_status(
            store,
            status="cannot_evaluate",
            reason=reason,
            now=now,
            observed_clusters=observed_clusters,
            declared_clusters=declared_clusters,
        )
        announce_cluster_identity_mismatch(observed_clusters, declared_clusters)
        return result

    findings = reflect_fn(observations, application_model)
    result = land_findings(store, findings, applications=application_model, now_fn=now_fn)

    ci_records = ci_records_from_observations(observations=observations, applications=applications)
    ci_records += ci_records_from_namespaces(
        cluster=namespaces["cluster"],
        namespace_names=namespaces["names"],
        applications=applications,
    )
    ci_result = land_ci_records(store, ci_records)
    result["ci"] = ci_result

    try:
        dependency_facts = collect_dependency_facts_fn()
    except RuntimeError as exc:
        # AC-8 (M3): on #997 this call sat outside every try/except in this
        # function, so a collector failure here (MissingKubectlOutputError,
        # or the plain RuntimeError `_require_kubeconfig` raises) propagated
        # with land_findings/land_ci_records already landed but
        # `_write_observer_status` never reached -- a run that failed left no
        # record of failing. Same handling as the collectors above.
        _write_observer_status(store, status="failed", reason=str(exc), now=now)
        raise
    dependency_units = ea_dependency.workload_units_from_kubectl(
        cluster=dependency_facts["cluster"],
        deployments=dependency_facts["deployments"],
        statefulsets=dependency_facts["statefulsets"],
    )
    dependency_edges = ea_dependency.compute_dependency_edges(
        deployments=dependency_facts["deployments"],
        statefulsets=dependency_facts["statefulsets"],
        networkpolicies=dependency_facts["networkpolicies"],
        cluster=dependency_facts["cluster"],
        applications=applications,
    )
    result["dependencies"] = ea_dependency.land_dependency_posture(
        store,
        applications=applications,
        units=dependency_units,
        edges=dependency_edges,
        now_fn=now_fn,
    )

    _write_observer_status(
        store,
        status="ok",
        reason=None,
        now=now,
        ci_counts={"attributed": ci_result["attributed"], "unattributed": ci_result["unattributed"]},
        dependency_posture=result["dependencies"]["posture_summary"],
    )
    return result


@activity.defn(name="observe_ea_model")
def observe_ea_model_activity(request: dict[str, Any]) -> dict[str, Any]:
    request = request or {}
    return observe_ea_model(
        default_store(), collect_dependency_facts_fn=ea_dependency.collect_dependency_cluster_facts
    )


ACTIVITIES = [observe_ea_model_activity]
