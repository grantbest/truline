#!/usr/bin/env python3
"""Derive EA model facts from the repository.

The application portfolio is still reviewed as YAML, because refs and edges
need to be readable in a PR. This script makes the part Kubernetes already
knows mechanically checkable: if a workload manifest names another in-cluster
service, consumes a secret produced by ExternalSecrets, or asks Reloader to
restart it on secret changes, the application model must carry that dependency.

The rule is deliberately a floor, not an exact rewrite. Some dependencies are
true but not visible in manifests yet: external runners, code defaults, and
dependencies stored behind a DSN secret all fit that category. Those authored
edges remain legal.

A manifest-derivable edge is now written by this script, not hand-copied
(PC-ASR-007/AC-2): `depends-on --apply` reconciles it into the substrate as a
`bead_link` row this deriver owns (`created_by="ea-derive"`), and
`ea-conformance.py`'s DERIVED check fails the opposite way it used to --
a YAML `depends_on` entry that duplicates a derivable edge is now the
violation, since it demands the hand copy AC-2 forbids.

`workload` applies the same treatment to a kubernetes-runtime application's
footprint: the manifest path, the ArgoCD ownership (infrastructure/k8s/argocd/
*.yaml), and each object's cluster/namespace/kind/name are derived rather than
hand-copied into the model YAML. external/none runtimes are untouched — a
compose binding or a note is a claim about a system this repo cannot see
(ea-metamodel.md §4.2).

Usage:
    python3 scripts/ea-derive.py depends-on             # print derived edges
    python3 scripts/ea-derive.py depends-on --check     # fail if YAML hand-authors one
    python3 scripts/ea-derive.py depends-on --dry-run   # print the substrate reconcile plan
    python3 scripts/ea-derive.py depends-on --apply     # reconcile derived edges into the substrate
    python3 scripts/ea-derive.py workload               # print derived workloads
    python3 scripts/ea-derive.py workload --check       # fail if a k8s app has none

Environment (--apply/--dry-run only): SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import os
import pathlib
import re
import sys
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml is required: pip install pyyaml")


REPO = pathlib.Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "docs" / "architecture" / "model"
K8S_DIR = REPO / "infrastructure" / "k8s"
ARGOCD_DIR = K8S_DIR / "argocd"
APP_EXTERNAL_SECRETS = "app.external-secrets"
APP_RELOADER = "app.reloader"

#: The single cluster every manifest under K8S_DIR targets. Required, not
#: defaulted (R2603-5): a wrong value here would be landed into the substrate
#: as wrong data, silently, for every derived workload object.
EA_CANONICAL_CLUSTER_ENV = "EA_CANONICAL_CLUSTER"
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}


def _canonical_cluster() -> str:
    """This deployment's cluster name, read lazily so importing this module
    (as ea-conformance.py, ea_apply.py, and several tests that never derive a
    workload all do) never demands it -- only actually deriving one does.
    """
    value = os.environ.get(EA_CANONICAL_CLUSTER_ENV)
    if not value:
        raise RuntimeError(
            f"{EA_CANONICAL_CLUSTER_ENV} is not set. ea-derive.py refuses to guess this "
            "deployment's cluster name rather than silently stamping derived workload "
            f"objects with a compiled-in default. Set {EA_CANONICAL_CLUSTER_ENV} and try again."
        )
    return value


# Directories that host exactly one kubernetes application's workload under a
# name that predates its ref — a shared platform-* directory, not a per-app
# one, so neither the file stem nor the parent directory name equals the
# ref's short name. Recorded once, here, for the same reason
# APP_EXTERNAL_SECRETS and APP_RELOADER are above: a structural fact with
# nowhere else in the repo to live, rather than a hand-authored copy in the
# reviewed YAML.
_WORKLOAD_DIR_OWNERS: dict[str, str] = {
    "infrastructure/k8s/platform-backup": "app.pg-backup",
    "infrastructure/k8s/monitoring": "app.monitoring-stack",
}

# Bootstrap/out-of-band installs with no committed manifest: ArgoCD itself
# (ea-metamodel.md's own note — it does GitOps, so it cannot be managed by
# it) and the Tailscale operator Helm chart. No repo file can prove these
# objects exist, so — like the table above — they are recorded once, in
# code, rather than invented as a hand-authored copy in the model.
def _no_manifest_workloads(cluster: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "app.argocd": [
            {"cluster": cluster, "namespace": "argocd", "kind": "Deployment",
             "name": "argocd-server", "manifest": None, "managed_by": "bootstrap"},
            {"cluster": cluster, "namespace": "argocd", "kind": "StatefulSet",
             "name": "argocd-application-controller", "manifest": None, "managed_by": "bootstrap"},
            {"cluster": cluster, "namespace": "argocd", "kind": "Deployment",
             "name": "argocd-repo-server", "manifest": None, "managed_by": "bootstrap"},
            {"cluster": cluster, "namespace": "argocd", "kind": "Deployment",
             "name": "argocd-notifications-controller", "manifest": None, "managed_by": "bootstrap"},
        ],
        "app.tailscale-operator": [
            {"cluster": cluster, "namespace": "tailscale", "kind": "Deployment",
             "name": "operator", "manifest": None, "managed_by": "helm"},
        ],
    }

_SERVICE_HOST_RE = re.compile(
    r"\b"
    r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)"
    r"\."
    r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)"
    r"(?:\.svc(?:\.cluster\.local)?)?"
    r"\b"
)


class K8sDocument:
    def __init__(self, path: pathlib.Path, body: dict[str, Any]) -> None:
        self.path = path
        self.body = body


class DerivedEdge:
    def __init__(self, target: str) -> None:
        self.target = target
        self.reasons: set[str] = set()


def _load_model_applications() -> list[dict[str, Any]]:
    path = MODEL_DIR / "application-portfolio.yaml"
    if not path.exists():
        sys.exit(f"model file missing: {path}")
    return yaml.safe_load(path.read_text())["applications"]


def _load_k8s_documents() -> list[K8sDocument]:
    docs: list[K8sDocument] = []
    if not K8S_DIR.exists():
        return docs
    for path in sorted(K8S_DIR.rglob("*.yaml")):
        for body in yaml.safe_load_all(path.read_text()):
            if isinstance(body, dict):
                docs.append(K8sDocument(path.relative_to(REPO), body))
    return docs


class ArgoApp:
    def __init__(self, name: str, source_path: pathlib.Path, recurse: bool) -> None:
        self.name = name
        self.source_path = source_path
        self.recurse = recurse


def _load_argocd_applications() -> list[ArgoApp]:
    apps: list[ArgoApp] = []
    if not ARGOCD_DIR.exists():
        return apps
    for path in sorted(ARGOCD_DIR.rglob("*.yaml")):
        for body in yaml.safe_load_all(path.read_text()):
            if not isinstance(body, dict) or body.get("kind") != "Application":
                continue
            source = (body.get("spec") or {}).get("source") or {}
            src_path = source.get("path")
            if not src_path:
                continue
            meta = body.get("metadata") or {}
            apps.append(ArgoApp(
                name=meta.get("name") or "",
                source_path=pathlib.Path(src_path),
                recurse=bool((source.get("directory") or {}).get("recurse")),
            ))
    return apps


def _walk_kustomization(dir_path: pathlib.Path, namespace: str | None) -> dict[pathlib.Path, str | None]:
    """{manifest path relative to REPO: effective namespace}, following a
    kustomization.yaml's `resources:` and `namespace:` transformer.

    Deliberately not a kustomize implementation: patches, generators, and
    other transformers are not applied. Nothing under infrastructure/k8s/
    uses one to introduce, rename, or move a workload object — the effect
    they do have (env vars, image tags, resource limits) is irrelevant to
    identity, which is all this resolves.
    """
    result: dict[pathlib.Path, str | None] = {}
    kustomization = dir_path / "kustomization.yaml"
    if not kustomization.exists():
        return result
    doc = yaml.safe_load(kustomization.read_text()) or {}
    ns = doc.get("namespace", namespace)
    for resource in doc.get("resources") or []:
        resource_path = (dir_path / resource).resolve()
        if resource_path.is_dir():
            result.update(_walk_kustomization(resource_path, ns))
        elif resource_path.suffix in {".yaml", ".yml"} and resource_path.exists():
            try:
                result[resource_path.relative_to(REPO)] = ns
            except ValueError:
                continue
    return result


def _argocd_resource_namespaces(argocd_apps: Iterable[ArgoApp]) -> dict[pathlib.Path, set[str | None]]:
    """{manifest path relative to REPO: namespaces it is synced into}.

    A base manifest reused by more than one overlay (dev + prod) is synced by
    more than one Application, each with its own `namespace:` override, so
    the value is a set, not a single namespace — the source of the two
    workload objects per file that the dev/prod pattern produces.
    """
    result: dict[pathlib.Path, set[str | None]] = defaultdict(set)
    for app in argocd_apps:
        root = REPO / app.source_path
        if not root.exists():
            continue
        if (root / "kustomization.yaml").exists():
            for relpath, ns in _walk_kustomization(root, None).items():
                result[relpath].add(ns)
        else:
            files = root.rglob("*.yaml") if app.recurse else root.glob("*.yaml")
            for f in files:
                result[f.relative_to(REPO)].add(None)
    return result


def _short_ref_names(apps: list[dict[str, Any]]) -> dict[str, str]:
    """{short-name: ref} for every kubernetes-runtime application.

    `short-name` is the ref with its `app.` prefix stripped — the same key
    check_reality already uses to correlate a ref with apps/<short>/ in the
    source tree.
    """
    names: dict[str, str] = {}
    for app in apps:
        workload = app["content"].get("workload") or {}
        if workload.get("runtime") != "kubernetes":
            continue
        names[app["ref"].split(".", 1)[1]] = app["ref"]
    return names


def _owning_ref(relpath: pathlib.Path, short_names: dict[str, str]) -> str | None:
    """Which kubernetes application a manifest file belongs to, or None.

    Exact matches only, in order: the tiny directory registry above, the
    file's own stem, then its parent directory's name. A fuzzy substring
    match over basenames is the exact anti-pattern check_workload's own
    docstring documents as a real, shipped bug — a guess that could not tell
    "no footprint" from "nobody filled this in", replaced for good reason.
    """
    dir_key = relpath.parent.as_posix()
    if dir_key in _WORKLOAD_DIR_OWNERS:
        return _WORKLOAD_DIR_OWNERS[dir_key]
    if relpath.stem in short_names:
        return short_names[relpath.stem]
    if relpath.parent.name in short_names:
        return short_names[relpath.parent.name]
    return None


def derive_workloads(
    apps: list[dict[str, Any]],
    docs: Iterable[K8sDocument],
    argocd_apps: list[ArgoApp] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Derive `content.workload.objects` for every kubernetes-runtime application.

    ea-metamodel.md §4.2: the manifest path, the ArgoCD ownership, and the
    cluster/namespace/kind/name of each object are all facts the repository
    already states once — in the manifest itself and in
    infrastructure/k8s/argocd/*.yaml — so hand-copying them into the model
    YAML is exactly the kind of drift this file already exists to catch for
    `depends_on`. This applies the same treatment to the workload footprint.

    external/none runtimes are untouched (ea-metamodel.md §4.2's boundary): a
    compose binding or a note is a claim about a system this repo cannot
    see, so only kubernetes objects[] are derived here.
    """
    cluster = _canonical_cluster()
    docs = list(docs)
    if argocd_apps is None:
        argocd_apps = _load_argocd_applications()
    short_names = _short_ref_names(apps)
    argocd_namespaces = _argocd_resource_namespaces(argocd_apps)

    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        kind = doc.body.get("kind")
        if kind not in _WORKLOAD_KINDS:
            continue
        owner = _owning_ref(doc.path, short_names)
        if owner is None:
            continue
        meta = _metadata(doc.body)
        name = meta.get("name")
        if not name:
            continue
        inline_namespace = meta.get("namespace")

        namespaces = argocd_namespaces.get(doc.path)
        if namespaces is not None:
            managed_by = "argocd"
            effective_namespaces = {ns or inline_namespace for ns in namespaces}
        else:
            managed_by = "deploy-workflow"
            effective_namespaces = {inline_namespace}

        for namespace in effective_namespaces:
            if not namespace:
                continue
            result[owner].append({
                "cluster": cluster,
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "manifest": doc.path.as_posix(),
                "managed_by": managed_by,
            })

    for ref, objects in _no_manifest_workloads(cluster).items():
        if ref in short_names.values():
            result[ref] = list(objects) + list(result.get(ref, []))

    return {
        ref: sorted(objs, key=lambda o: (o["namespace"], o["kind"], o["name"]))
        for ref, objs in result.items()
    }


def _metadata(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("metadata") or {}


def _workload_objects(
    app: dict[str, Any], workloads: dict[str, list[dict[str, Any]]] | None
) -> list[dict[str, Any]]:
    """An application's workload objects: authored ones plus derived ones.

    Kubernetes objects[] is shed from the YAML (ea-metamodel.md §4.2), so once
    an application's model entry no longer authors them, `workloads` — the
    output of `derive_workloads()` — is the only source. Tests that still
    build a fixture with literal `content.workload.objects` keep working
    unchanged: this unions rather than replaces.
    """
    workload = app["content"].get("workload") or {}
    objects = list(workload.get("objects") or [])
    if workloads is not None:
        objects = objects + list(workloads.get(app["ref"]) or [])
    return objects


def _manifest_paths(
    app: dict[str, Any], workloads: dict[str, list[dict[str, Any]]] | None = None
) -> set[pathlib.Path]:
    return {
        pathlib.Path(obj["manifest"])
        for obj in _workload_objects(app, workloads)
        if obj.get("manifest")
    }


def _workload_namespaces(
    app: dict[str, Any], workloads: dict[str, list[dict[str, Any]]] | None = None
) -> set[str]:
    return {
        obj["namespace"]
        for obj in _workload_objects(app, workloads)
        if isinstance(obj, dict) and obj.get("namespace")
    }


def _service_index(
    apps: list[dict[str, Any]],
    docs: Iterable[K8sDocument],
    workloads: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[tuple[str | None, str], set[str]]:
    """Map (namespace, Service name) to the application that owns it.

    Several modelled workloads reuse a base manifest in dev and prod namespaces.
    Raw YAML only contains the base namespace, while the portfolio declares both
    workload namespaces. Indexing the declared namespaces keeps service refs
    like `mcp-hub.platform-mcp-prod.svc.cluster.local` derivable from the same
    base service manifest.
    """
    apps_by_manifest: dict[pathlib.Path, set[str]] = defaultdict(set)
    namespaces_by_app: dict[str, set[str]] = {}
    for app in apps:
        ref = app["ref"]
        namespaces_by_app[ref] = _workload_namespaces(app, workloads)
        for path in _manifest_paths(app, workloads):
            apps_by_manifest[path].add(ref)

    index: dict[tuple[str | None, str], set[str]] = defaultdict(set)
    for doc in docs:
        if doc.body.get("kind") != "Service":
            continue
        meta = _metadata(doc.body)
        name = meta.get("name")
        if not name:
            continue
        owners = apps_by_manifest.get(doc.path, set())
        for owner in owners:
            index[(None, name)].add(owner)
            if meta.get("namespace"):
                index[(meta["namespace"], name)].add(owner)
            for namespace in namespaces_by_app[owner]:
                index[(namespace, name)].add(owner)
    return index


def _external_secret_targets(docs: Iterable[K8sDocument]) -> set[tuple[str | None, str]]:
    targets: set[tuple[str | None, str]] = set()
    for doc in docs:
        if doc.body.get("kind") != "ExternalSecret":
            continue
        meta = _metadata(doc.body)
        name = ((doc.body.get("spec") or {}).get("target") or {}).get("name") or meta.get("name")
        if not name:
            continue
        targets.add((None, name))
        if meta.get("namespace"):
            targets.add((meta["namespace"], name))
    return targets


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
    elif isinstance(value, str):
        yield value


def _secret_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        secret_key_ref = value.get("secretKeyRef")
        if isinstance(secret_key_ref, dict) and secret_key_ref.get("name"):
            yield secret_key_ref["name"]
        secret_ref = value.get("secretRef")
        if isinstance(secret_ref, dict) and secret_ref.get("name"):
            yield secret_ref["name"]
        for child in value.values():
            yield from _secret_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _secret_refs(child)


def derive_application_dependencies(
    apps: list[dict[str, Any]],
    docs: Iterable[K8sDocument],
    argocd_apps: list[ArgoApp] | None = None,
) -> dict[str, dict[str, DerivedEdge]]:
    docs = list(docs)
    # Kubernetes objects[] is derived, not authored (ea-metamodel.md §4.2), so
    # the manifest-to-application mapping this function needs has to come from
    # the same derivation `derive_workloads()` performs — otherwise an
    # application whose YAML no longer authors `workload.objects` would look
    # manifest-less here too, and every dependency this function exists to
    # catch would silently stop being derivable.
    workloads = derive_workloads(apps, docs, argocd_apps)
    service_index = _service_index(apps, docs, workloads)
    external_secret_targets = _external_secret_targets(docs)
    docs_by_path: dict[pathlib.Path, list[K8sDocument]] = defaultdict(list)
    for doc in docs:
        docs_by_path[doc.path].append(doc)

    result: dict[str, dict[str, DerivedEdge]] = {}
    for app in apps:
        ref = app["ref"]
        edges: dict[str, DerivedEdge] = {}
        for path in _manifest_paths(app, workloads):
            for doc in docs_by_path.get(path, []):
                namespace = _metadata(doc.body).get("namespace")

                annotations = _metadata(doc.body).get("annotations") or {}
                if ref != APP_RELOADER and "secret.reloader.stakater.com/reload" in annotations:
                    _add_edge(edges, APP_RELOADER, f"{path}: reloader annotation")

                if doc.body.get("kind") == "ExternalSecret" and ref != APP_EXTERNAL_SECRETS:
                    _add_edge(edges, APP_EXTERNAL_SECRETS, f"{path}: ExternalSecret")

                for secret_name in _secret_refs(doc.body):
                    if ref == APP_EXTERNAL_SECRETS:
                        continue
                    if (namespace, secret_name) in external_secret_targets or (
                        None,
                        secret_name,
                    ) in external_secret_targets:
                        _add_edge(edges, APP_EXTERNAL_SECRETS, f"{path}: secret {secret_name}")

                for text in _walk_strings(doc.body):
                    for service_name, service_namespace in _SERVICE_HOST_RE.findall(text):
                        owners = (
                            service_index.get((service_namespace, service_name), set())
                            | service_index.get((None, service_name), set())
                        )
                        for owner in owners:
                            if owner != ref:
                                _add_edge(
                                    edges,
                                    owner,
                                    f"{path}: {service_name}.{service_namespace}",
                                )

        if edges:
            result[ref] = edges
    return result


def _add_edge(edges: dict[str, DerivedEdge], target: str, reason: str) -> None:
    edge = edges.setdefault(target, DerivedEdge(target=target))
    edge.reasons.add(reason)


def missing_dependencies(
    apps: list[dict[str, Any]], derived: dict[str, dict[str, DerivedEdge]]
) -> dict[str, set[str]]:
    missing: dict[str, set[str]] = {}
    by_ref = {app["ref"]: app for app in apps}
    for ref, edges in derived.items():
        authored = set(by_ref[ref]["content"].get("depends_on") or [])
        absent = set(edges) - authored
        if absent:
            missing[ref] = absent
    return missing


def authored_derivable_overlap(
    apps: list[dict[str, Any]], derived: dict[str, dict[str, DerivedEdge]]
) -> dict[str, set[str]]:
    """Authored `depends_on` entries that duplicate an edge the deriver can already compute.

    The inverse of `missing_dependencies`. PC-ASR-007/AC-2 forbids demanding a human hand-copy
    a fact the deriver already knows, so once the deriver writes derivable edges itself, a
    hand-authored copy is the violation -- not, as before, its absence.
    """
    overlap: dict[str, set[str]] = {}
    by_ref = {app["ref"]: app for app in apps}
    for ref, edges in derived.items():
        authored = set(by_ref[ref]["content"].get("depends_on") or [])
        present = authored & set(edges)
        if present:
            overlap[ref] = present
    return overlap


def print_dependencies(derived: dict[str, dict[str, DerivedEdge]]) -> None:
    for ref in sorted(derived):
        print(f"{ref}:")
        for target in sorted(derived[ref]):
            print(f"  - {target}")
            for reason in sorted(derived[ref][target].reasons):
                print(f"      # {reason}")


def check_depends_on() -> int:
    """The CI floor: a YAML `depends_on` entry must not duplicate a manifest-derivable edge.

    scripts/ea-conformance.py's DERIVED check runs the same computation directly (so its
    error text can cite the check name); this CLI entry point exists for a human to run the
    same rule locally.
    """
    apps = _load_model_applications()
    derived = derive_application_dependencies(apps, _load_k8s_documents())
    overlap = authored_derivable_overlap(apps, derived)
    print(f"EA derived depends_on: {sum(len(v) for v in derived.values())} edge(s)")
    if not overlap:
        print("OK - application.depends_on hand-authors no manifest-derivable edge.")
        return 0

    print(f"\n{sum(len(v) for v in overlap.values())} hand-authored derivable edge(s):")
    for ref in sorted(overlap):
        for target in sorted(overlap[ref]):
            reasons = "; ".join(sorted(derived[ref][target].reasons))
            print(
                f"  {ref} depends_on {target} is manifest-derivable ({reasons}) -- "
                "scripts/ea-derive.py owns this edge; remove the authored copy"
            )
    return 1


# --- writing layer: reconcile derived edges into the substrate --------------
#
# Ownership of a derived edge is tracked with the bead_link `created_by` the substrate
# already stores per link (BeadLinkRead.created_by), not a per-edge `content.source_class`:
# bead_link has no such column, and apps/substrate/** is out of scope for this change. See
# .factory/design.md for the full rationale and the alternatives that were rejected.

# scripts/substrate_client.py is the shared client ea-load.py, release-load.py,
# requirements-load.py and arch-source-class-backfill.py all use instead of hand-rolling
# their own X-API-Key request (docs/audits/2026-09-12-architecture-review-modularity-and-
# contracts.md §4). This file was the last hold-out, kept hand-rolled because this exact
# source is also byte-identical-pinned into
# apps/substrate/publish/csdm-on-beads/scripts/ea-derive.py
# (scripts/tests/test_csdm_publish_tree_drift.py) -- a tree whose own promise
# (apps/substrate/publish/csdm-on-beads/README.md) is that none of its files import
# outside the standard library and pydantic/pyyaml, and which carries no
# substrate_client.py of its own.
#
# The import below is deferred into a try/except rather than hoisted to the top of this
# file for that reason: ea-conformance.py's DERIVED/WORKLOAD checks load this whole module
# by path (in both this repository and the published tree) just to call the pure
# derivation functions below, and never touch Substrate -- a module-load-time ImportError
# here would turn the published tree's missing substrate_client.py into a DERIVED/WORKLOAD
# conformance error for every caller, including the ones that only derive and never write.
sys.path.insert(0, str(REPO / "scripts"))
try:
    from substrate_client import Substrate as _Substrate
except ImportError:
    _Substrate = None

DEFAULT_CREATED_BY = "ea-derive"


class DependencyPlan:
    """What a dependency reconcile did, or would do."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.created: list[str] = []
        self.removed: list[str] = []


if _Substrate is not None:

    class Substrate(_Substrate):
        """scripts/substrate_client.py's shared client -- the same pattern
        ea-load.py/release-load.py/requirements-load.py/arch-source-class-backfill.py
        already use, rather than a sixth hand-rolled X-API-Key client.

        `add_link` is kept as a local override: apps/substrate/src/routes.py's
        `create_bead_link` reads `created_by` from the `X-Created-By` header, and this
        deriver picks a writer identity per call (`reconcile_dependencies`'s `created_by`
        argument -- `ea_apply.py`'s in-cluster applier is free to pass a different one)
        rather than fixing one for the life of a `Substrate` instance, which is what the
        shared client's own `add_link` assumes.
        """

        def add_link(
            self, source_id: str, target_id: str, link_type: str, *, created_by: str
        ) -> dict:
            return self._request(
                "POST", f"/beads/{source_id}/links",
                {"target_id": target_id, "link_type": link_type},
                headers={"X-Created-By": created_by} if created_by else None,
            )

else:

    class Substrate:
        """Refuses to guess at a write client scripts/substrate_client.py is not here to
        provide -- reachable only from apps/substrate/publish/csdm-on-beads alone, where
        no substrate_client.py sits beside this file (see the note above). `depends-on
        --apply`/`--dry-run` need a real store; the derive/check commands this published
        copy documents never construct this class.
        """

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "scripts/substrate_client.py is not present next to this file; the "
                "depends-on --apply/--dry-run write path needs it"
            )


def _application_bead_ids(sub: Any) -> dict[str, str]:
    ids: dict[str, str] = {}
    for bead in sub.list_beads("application"):
        ref = (bead.get("content") or {}).get("ref")
        if ref:
            ids[ref] = bead["id"]
    return ids


def reconcile_dependencies(
    sub: Any,
    derived: dict[str, dict[str, Any]],
    *,
    created_by: str = DEFAULT_CREATED_BY,
    dry_run: bool = False,
) -> DependencyPlan:
    """Make the depends_on `bead_link` edges this deriver owns match `derived`, exactly.

    "Owns" means `link_type == "depends_on"` and the link's own `created_by == created_by`
    -- the one signal that lets this reconcile add and remove its own edges without ever
    touching an edge authored (or owned by any other deriver identity). Reconciled, not
    appended: an owned edge no longer present in `derived` is removed, mirroring
    scripts/ea-load.py's `reconcile_edges` for the edges it manages. Idempotent: called
    twice with the same `derived` and no change to the substrate in between, the second
    call issues no writes.

    `derived` only needs to support `.get(ref, {})` yielding something with dependency refs
    as keys -- the DerivedEdge values `derive_application_dependencies` returns are never
    read here, so a plain `{ref: {target_ref: ...}}` mapping works equally well in tests.
    """
    plan = DependencyPlan(dry_run)
    ids = _application_bead_ids(sub)
    ids_by_bead_id = {bead_id: ref for ref, bead_id in ids.items()}

    for ref, source_id in sorted(ids.items()):
        wanted_refs = set(derived.get(ref, {}))
        wanted_ids = {ids[t] for t in wanted_refs if t in ids}
        have: dict[str, str] = {
            link["target_id"]: link["id"]
            for link in sub.links(source_id, "outgoing")
            if link.get("link_type") == "depends_on" and link.get("created_by") == created_by
        }

        for target_ref in sorted(wanted_refs):
            target_id = ids.get(target_ref)
            if target_id is None or target_id in have:
                continue
            plan.created.append(f"{ref} -depends_on-> {target_ref}")
            if not dry_run:
                sub.add_link(source_id, target_id, "depends_on", created_by=created_by)

        for target_id, link_id in sorted(have.items()):
            if target_id in wanted_ids:
                continue
            target_ref = ids_by_bead_id.get(target_id, target_id)
            plan.removed.append(f"{ref} -depends_on-> {target_ref}")
            if not dry_run:
                sub.delete_link(link_id)

    return plan


def build_dependency_plan(
    sub: Any, *, dry_run: bool, created_by: str = DEFAULT_CREATED_BY
) -> DependencyPlan:
    """The reusable core of a dependency-reconcile run.

    CLI `--apply`/`--dry-run` and the in-cluster applier
    (apps/factory-dispatcher/activities/ea_apply.py) both drive this rather than re-deriving
    edges and calling reconcile_dependencies themselves.
    """
    apps = _load_model_applications()
    derived = derive_application_dependencies(apps, _load_k8s_documents())
    return reconcile_dependencies(sub, derived, created_by=created_by, dry_run=dry_run)


def report_dependency_plan(plan: DependencyPlan) -> None:
    head = "DRY RUN — nothing was written" if plan.dry_run else "applied"
    print(f"ea-derive depends-on: {head}\n")
    for label, items in (("created", plan.created), ("removed", plan.removed)):
        if not items:
            continue
        print(f"{label} ({len(items)})")
        for line in items:
            print(f"  {line}")
    if not plan.created and not plan.removed:
        print("unchanged")


def print_workloads(derived: dict[str, list[dict[str, Any]]]) -> None:
    for ref in sorted(derived):
        print(f"{ref}:")
        for obj in derived[ref]:
            print(
                f"  - {obj['namespace']}/{obj['kind']}/{obj['name']} "
                f"(manifest={obj['manifest']}, managed_by={obj['managed_by']})"
            )


def check_workload_derivation() -> int:
    apps = _load_model_applications()
    derived = derive_workloads(apps, _load_k8s_documents(), _load_argocd_applications())
    kube_refs = {
        a["ref"] for a in apps if (a["content"].get("workload") or {}).get("runtime") == "kubernetes"
    }
    missing = sorted(kube_refs - set(derived))
    total = sum(len(v) for v in derived.values())
    print(f"EA derived workload: {total} object(s) across {len(derived)} application(s)")
    if not missing:
        print("OK - every kubernetes-runtime application has a derivable workload.")
        return 0

    print(f"\n{len(missing)} kubernetes-runtime application(s) with no derivable workload:")
    for ref in missing:
        print(f"  {ref}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    depends = sub.add_parser("depends-on", help="derive application.depends_on from manifests")
    depends.add_argument(
        "--check", action="store_true",
        help="fail if the YAML hand-authors a manifest-derivable edge",
    )
    depends.add_argument(
        "--apply", action="store_true",
        help="reconcile derived depends_on edges into the substrate",
    )
    depends.add_argument(
        "--dry-run", action="store_true",
        help="print the substrate reconcile plan; change nothing",
    )
    workload = sub.add_parser("workload", help="derive application.workload.objects from manifests and ArgoCD")
    workload.add_argument("--check", action="store_true", help="fail if a kubernetes application has no derivable workload")
    args = ap.parse_args()

    if args.command == "depends-on":
        if args.check:
            return check_depends_on()
        if args.apply or args.dry_run:
            plan = build_dependency_plan(Substrate(), dry_run=args.dry_run)
            report_dependency_plan(plan)
            return 0
        print_dependencies(derive_application_dependencies(_load_model_applications(), _load_k8s_documents()))
        return 0
    if args.command == "workload":
        if args.check:
            return check_workload_derivation()
        print_workloads(derive_workloads(_load_model_applications(), _load_k8s_documents(), _load_argocd_applications()))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    sys.exit(main())
