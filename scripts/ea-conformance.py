#!/usr/bin/env python3
"""EA model conformance — the reason this model can be a system of record.

`docs/reference/current-state.md` carries an editing rule in its own header:
"update a section ONLY with evidence (kubectl output, CI run, probe) and date
the claim." It says so because it drifted into anti-truth three times — 2026-05-30,
2026-06-13, 2026-07-05 — and it drifted a fourth time anyway, which cost a wrong
finding on 2026-07-29 (monitoring scored dead three weeks after it was migrated).

A rule enforced by good intentions is not a rule. This script is the enforcement.
It runs in CI and fails the build when the model's claims stop matching the repo,
which is the only difference between a system of record and a document.

Checks, in order of how badly a failure misleads a reader:

  STRUCTURE   refs unique; every parent/supports/realizes/depends_on resolves
  CONSISTENCY dispositions and lifecycles that contradict each other
  SUPPORTS    every capability has a supports edge or a dated supports_none_reason
  EVIDENCE    every repo path cited as evidence still exists
  DERIVED     no application.depends_on hand-authors a manifest-derivable edge
  WORKLOAD    every app says what it runs as; kubernetes objects are derived,
              not authored; cited manifests exist; orphans named
  REALITY     custom apps have source; eliminated/eol apps declare no workload
  FRESHNESS   assessed_at not older than --max-age-days (warning, not failure)

Usage:
    python3 scripts/ea-conformance.py            # fail on error, warn on stale
    python3 scripts/ea-conformance.py --strict   # warnings become failures
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import importlib.util
import pathlib
import re
import subprocess
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml is required: pip install pyyaml")


REPO = pathlib.Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "docs" / "architecture" / "model"

# An evidence string is a checkable repo path only if it looks like one. The
# field deliberately also holds PR numbers, doc sections, and prose ("verified
# 2026-07-05"), which are evidence a human can follow but a script cannot.
# Checking only what is mechanically checkable is the honest scope; over-eager
# matching would produce false failures and train people to ignore this script.
#
# Three shapes are checkable, and the first version of this regex only accepted
# one of them — silently skipping 20 of 95 citations, including
# `apps/substrate/` and `.github/workflows/`. Deleting those directories would
# still have reported "cited evidence exists", which is the precise failure this
# script exists to prevent. Found 2026-07-30 by two independent audits, each of
# which found a different half:
#
#   apps/substrate/src/routes.py        a file
#   apps/substrate/                     a directory — trailing slash
#   docs/x.md#L42, docs/x.md#anchor     a deep link into either
#
# The old pattern required every `/` to be followed by at least one character
# and allowed no `#`, so both the trailing-slash and the anchored forms fell
# through to "not a path, don't check it". `_exists` already stripped trailing
# slashes, so the two functions disagreed about what a path was — the check
# looked implemented and was not reached.
#
# Prose stays excluded by the same property as before: it contains spaces, and
# a checkable citation may not. "PR #173/#174/#175" is safe for that reason.
_PATH_RE = re.compile(
    r"^[A-Za-z0-9_.\-]+"          # first segment
    r"(?:/[A-Za-z0-9_.\-*]*)+"    # further segments; * length allows a trailing /
    r"(?:#[A-Za-z0-9_.\-]+)?$"    # optional #Lnn or #anchor
)

# Pillar 10 (Amendment 24): intent and execution are separate stores. A bead
# records what was wanted and what resulted; Temporal records what ran. These
# field names are execution bookkeeping and MUST NOT appear in bead content
# schemas — a store that holds both ends up with an advisory state machine and
# a Python control flow that is the real one (FA-S18).
#
# `max_agent_minutes` and friends are deliberately NOT here: a budget *limit* is
# a statement of intent ("this task may spend 20 minutes"). What's banned is the
# *record of execution* ("this task has been tried 3 times").
_EXECUTION_STATE_FIELDS = frozenset({
    "attempts", "attempt", "attempt_count", "max_attempts", "tries", "try_count",
    "retry", "retries", "retry_count", "retry_after", "backoff",
    "lease", "lease_holder", "lease_expires", "leased_by", "leased_until",
    "claimed_by", "claimed_at", "claim_expires", "owner_pid", "worker_pid",
    "heartbeat", "heartbeat_at", "last_heartbeat", "timeout_at", "deadline_at",
})

# Known violations that Amendment 24 exists to remove, not new debt to tolerate.
# Phase 3 deletes the `dev.*` schemas from the substrate; delete these entries in
# the same PR. Anything NOT listed here is a hard error, so the gate catches
# regressions today without blocking the work that removes the originals.
_PILLAR10_KNOWN: dict[tuple[str, str], str] = {
}

_SUBSTRATE_SCHEMAS = pathlib.Path("apps") / "substrate" / "src" / "schemas.py"

_VALID_MATURITY = {"absent", "emerging", "operating", "optimised"}
_MATURITY_RANK = {maturity: index for index, maturity in enumerate(("absent", "emerging", "operating", "optimised"))}
_VALID_CAP_STATE = {"proposed", "active", "deprecated", "retired"}
_VALID_APP_STATE = {"plan", "build", "operate", "retire", "eol"}
_VALID_TIME = {"invest", "tolerate", "migrate", "eliminate"}
_VALID_LAYER = {"demand", "supply"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, check: str, ref: str, msg: str) -> None:
        self.errors.append(f"  [{check}] {ref}: {msg}")

    def warn(self, check: str, ref: str, msg: str) -> None:
        self.warnings.append(f"  [{check}] {ref}: {msg}")


def _load(name: str, key: str, required: bool = True) -> list[dict]:
    path = MODEL_DIR / name
    if not path.exists():
        if required:
            sys.exit(f"model file missing: {path}")
        return []
    doc = yaml.safe_load(path.read_text())
    return doc[key]


def _is_path(evidence: str) -> bool:
    return bool(_PATH_RE.match(evidence.strip()))


def _exists(rel: str) -> bool:
    """True if the path exists, tolerating a trailing glob, a bare dir, or an anchor.

    The anchor is dropped rather than verified: `#L42` pointing past the end of
    a file is stale evidence too, but line numbers drift on every edit, and a
    check that fails on unrelated changes is a check people learn to ignore.
    Existence of the target is the honest floor.
    """
    rel = rel.strip().split("#", 1)[0].rstrip("/")
    if not rel:
        return False
    if "*" in rel:
        parent = rel.split("*")[0].rstrip("/")
        return (REPO / parent).exists() if parent else True
    return (REPO / rel).exists()


def _parse_date(raw: Any) -> dt.date | None:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


def _baseline_ref_from_env(explicit: str | None) -> str | None:
    if explicit:
        return explicit

    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        # actions/checkout does not guarantee the PR base branch is present in a
        # shallow checkout. Fetching it here keeps the control in the gate rather
        # than making every workflow invocation remember checkout options.
        _git(["fetch", "--quiet", "origin", f"{base_ref}:refs/remotes/origin/{base_ref}"])
        return f"origin/{base_ref}"

    if _git(["rev-parse", "--verify", "--quiet", "origin/main"]).returncode == 0:
        return "origin/main"
    return None


def _load_caps_at(ref: str, r: Report) -> list[dict] | None:
    rel = "docs/architecture/model/business-layer.yaml"
    result = _git(["show", f"{ref}:{rel}"])
    if result.returncode != 0:
        r.error("MATURITY", ref, f"could not read baseline {rel}: {result.stderr.strip()[:300]}")
        return None
    try:
        return yaml.safe_load(result.stdout)["capabilities"]
    except Exception as exc:  # noqa: BLE001 - malformed baseline is a gate failure
        r.error("MATURITY", ref, f"could not parse baseline capabilities: {exc}")
        return None


# --- checks -----------------------------------------------------------------

def check_structure(
    caps: list[dict], apps: list[dict], observations: list[dict], r: Report,
    services: list[dict] = (),
) -> None:
    cap_refs = [c["ref"] for c in caps]
    app_refs = [a["ref"] for a in apps]
    service_refs = [s["ref"] for s in services]
    observation_refs = [o["ref"] for o in observations]

    for label, refs in (
        ("capability", cap_refs),
        ("application", app_refs),
        ("service", service_refs),
        ("observation", observation_refs),
    ):
        seen: set[str] = set()
        for ref in refs:
            if ref in seen:
                r.error("STRUCTURE", ref, f"duplicate {label} ref")
            seen.add(ref)

    cap_set, app_set, service_set = set(cap_refs), set(app_refs), set(service_refs)

    for c in caps:
        if c.get("parent") and c["parent"] not in cap_set:
            r.error("STRUCTURE", c["ref"], f"parent '{c['parent']}' does not exist")
        for t in c["content"].get("supports") or []:
            if t not in cap_set:
                r.error("STRUCTURE", c["ref"], f"supports '{t}' does not exist")

    for a in apps:
        for t in a["content"].get("realizes") or []:
            if t not in cap_set:
                r.error("STRUCTURE", a["ref"], f"realizes '{t}' is not a capability")
        for t in a["content"].get("depends_on") or []:
            if t not in app_set:
                r.error("STRUCTURE", a["ref"], f"depends_on '{t}' is not an application")
        for t in a["content"].get("consumes") or []:
            if t not in service_set:
                r.error("STRUCTURE", a["ref"], f"consumes '{t}' is not a service")

    for s in services:
        for t in s["content"].get("depends_on") or []:
            if t not in app_set:
                r.error("STRUCTURE", s["ref"], f"depends_on '{t}' is not an application")

    for o in observations:
        for t in o["content"].get("measures") or []:
            if t not in app_set:
                r.error("STRUCTURE", o["ref"], f"measures '{t}' is not an application")


_OBSERVATION_CONTENT_KEYS = {
    "ref",
    "observed_at",
    "workload",
    "image_ref",
    "replicas",
    "ready_replicas",
    "argocd_sync_status",
    "argocd_health_status",
    "last_synced_revision",
    "measures",
}
_OBSERVATION_WORKLOAD_KEYS = {"cluster", "namespace", "kind", "name"}
_JUDGMENT_FIELDS = {"state", "lifecycle_state", "technical_health", "business_value", "time_disposition"}


def check_observations(observations: list[dict], r: Report) -> None:
    """Keep observations factual, dated, and linked to what they measure.

    `arch.observation` is the Sprint 6 escape hatch from hand-typed
    assessment, so the gate has to keep the boundary sharp: it may record what
    was seen and link to an application; it may not smuggle portfolio judgments
    into a metric bead.
    """
    for o in observations:
        ref = o["ref"]
        content = o.get("content") or {}
        if content.get("ref") not in {None, ref}:
            r.error("OBSERVATION", ref, f"content.ref {content.get('ref')!r} does not match object ref")
        if "observed_at" not in content:
            r.error("OBSERVATION", ref, "no observed_at")
        elif isinstance(content["observed_at"], str):
            try:
                dt.datetime.fromisoformat(content["observed_at"].replace("Z", "+00:00"))
            except ValueError:
                r.error("OBSERVATION", ref, f"observed_at is not an ISO datetime: {content['observed_at']}")

        unknown = set(content) - _OBSERVATION_CONTENT_KEYS - _JUDGMENT_FIELDS
        for key in sorted(unknown):
            r.error("OBSERVATION", ref, f"unknown observation field '{key}'")
        for key in sorted(set(content) & _JUDGMENT_FIELDS):
            r.error("OBSERVATION", ref, f"judgment field '{key}' belongs on the application, not observation")

        workload = content.get("workload")
        if not isinstance(workload, dict):
            r.error("OBSERVATION", ref, "no workload identity")
        else:
            missing = _OBSERVATION_WORKLOAD_KEYS - set(workload)
            for key in sorted(missing):
                r.error("OBSERVATION", ref, f"workload missing '{key}'")
            for key in _OBSERVATION_WORKLOAD_KEYS & set(workload):
                if not str(workload[key]).strip():
                    r.error("OBSERVATION", ref, f"workload.{key} is blank")

        if content.get("ready_replicas") is not None and content.get("replicas") is not None:
            if int(content["ready_replicas"]) > int(content["replicas"]):
                r.error("OBSERVATION", ref, "ready_replicas exceeds replicas")

        measures = content.get("measures") or []
        if not measures:
            r.error("OBSERVATION", ref, "no measures edge — an unlinked metric cannot support assessment")


def check_consistency(caps: list[dict], apps: list[dict], r: Report) -> None:
    for c in caps:
        ref, state, content = c["ref"], c["state"], c["content"]
        if state not in _VALID_CAP_STATE:
            r.error("CONSISTENCY", ref, f"invalid state '{state}'")
        if content.get("maturity") not in _VALID_MATURITY:
            r.error("CONSISTENCY", ref, f"invalid maturity '{content.get('maturity')}'")
        if content.get("layer") not in _VALID_LAYER:
            r.error("CONSISTENCY", ref, f"invalid layer '{content.get('layer')}'")
        # An absent capability that claims to be active is the single most
        # misleading combination in the model: it reads as "we have this".
        if content.get("maturity") == "absent" and state == "active":
            r.error("CONSISTENCY", ref, "maturity 'absent' but state 'active'")
        if state == "proposed" and content.get("maturity") not in {"absent", None}:
            r.error("CONSISTENCY", ref, "state 'proposed' but maturity is not 'absent'")

    realized = {t for a in apps for t in (a["content"].get("realizes") or [])}
    parents = {c["parent"] for c in caps if c.get("parent")}
    for c in caps:
        ref = c["ref"]
        if c["state"] == "active" and ref not in realized and ref not in parents:
            r.error(
                "CONSISTENCY", ref,
                "state 'active' but no application realizes it and it has no children "
                "— either something realizes it (add the edge) or it is not active",
            )

    for a in apps:
        ref, state, content = a["ref"], a["state"], a["content"]
        if state not in _VALID_APP_STATE:
            r.error("CONSISTENCY", ref, f"invalid lifecycle state '{state}'")
        if content.get("time_disposition") not in _VALID_TIME:
            r.error("CONSISTENCY", ref, f"invalid time_disposition '{content.get('time_disposition')}'")
        if content.get("time_disposition") == "eliminate" and state not in {"retire", "eol", "operate"}:
            r.error("CONSISTENCY", ref, f"disposition 'eliminate' but state '{state}'")
        if state == "eol" and content.get("time_disposition") != "eliminate":
            r.error("CONSISTENCY", ref, "state 'eol' but disposition is not 'eliminate'")


def _validate_roadmap(ref: str, roadmap: Any, r: Report) -> None:
    if not isinstance(roadmap, dict):
        r.error("ROADMAP", ref, "roadmap must be an object with opened_at, target_date, and action")
        return

    opened = _parse_date(roadmap.get("opened_at"))
    target = _parse_date(roadmap.get("target_date"))
    if opened is None:
        r.error("ROADMAP", ref, "roadmap.opened_at must be an ISO date")
    if target is None:
        r.error("ROADMAP", ref, "roadmap.target_date must be an ISO date")
    if opened and target and target < opened:
        r.error("ROADMAP", ref, "roadmap.target_date is before roadmap.opened_at")
    if not str(roadmap.get("action") or "").strip():
        r.error("ROADMAP", ref, "roadmap.action is blank")


def check_roadmap(caps: list[dict], apps: list[dict], r: Report) -> None:
    """Turn EA decisions into dated backlog pressure.

    Sprint 7 made the model visible. Sprint 8 makes the uncomfortable rows
    actionable: an absent capability and a migrate/eliminate application must
    carry the date the pressure opened, the target date, and the next action.
    """
    for c in caps:
        content = c.get("content") or {}
        roadmap = content.get("roadmap")
        if content.get("maturity") == "absent" and roadmap is None:
            r.error("ROADMAP", c["ref"], "absent capability has no dated roadmap action")
        if roadmap is not None:
            _validate_roadmap(c["ref"], roadmap, r)

    for a in apps:
        content = a.get("content") or {}
        roadmap = content.get("roadmap")
        if content.get("time_disposition") in {"migrate", "eliminate"} and roadmap is None:
            r.error("ROADMAP", a["ref"], "migrate/eliminate application has no dated roadmap action")
        if roadmap is not None:
            _validate_roadmap(a["ref"], roadmap, r)


def _measured_capability_refs(caps: list[dict], apps: list[dict], observations: list[dict]) -> set[str]:
    cap_by_ref = {c["ref"]: c for c in caps}
    measured_app_refs = {
        target
        for observation in observations
        for target in ((observation.get("content") or {}).get("measures") or [])
    }
    out: set[str] = set()

    def add_with_parents(cap_ref: str) -> None:
        seen: set[str] = set()
        while cap_ref and cap_ref not in seen:
            seen.add(cap_ref)
            out.add(cap_ref)
            cap_ref = cap_by_ref.get(cap_ref, {}).get("parent")

    for app in apps:
        if app["ref"] not in measured_app_refs:
            continue
        for cap_ref in (app.get("content") or {}).get("realizes") or []:
            add_with_parents(cap_ref)

    return out


def check_maturity_raises(
    caps: list[dict],
    apps: list[dict],
    observations: list[dict],
    baseline_caps: list[dict] | None,
    r: Report,
) -> None:
    """A maturity raise needs measurement, not a nicer assertion.

    The current model still has many asserted assessments. Failing all of them
    would freeze the model. The useful control is change-aware: when a PR raises
    a capability's maturity relative to the base branch, the resulting graph
    must include a measured realizing application for that capability or one of
    its descendants.
    """
    if baseline_caps is None:
        return

    baseline_by_ref = {c["ref"]: c for c in baseline_caps}
    measured_caps = _measured_capability_refs(caps, apps, observations)

    for cap in caps:
        ref = cap["ref"]
        current = (cap.get("content") or {}).get("maturity")
        previous = (baseline_by_ref.get(ref, {}).get("content") or {}).get("maturity", "absent")
        if current not in _MATURITY_RANK or previous not in _MATURITY_RANK:
            continue
        if _MATURITY_RANK[current] <= _MATURITY_RANK[previous]:
            continue
        if ref not in measured_caps:
            r.error(
                "MATURITY", ref,
                f"maturity raised {previous}->{current} without a measured realizing application",
            )


def _validate_supports_none_reason(ref: str, reason: Any, r: Report) -> None:
    if not isinstance(reason, dict):
        r.error("SUPPORTS", ref, "supports_none_reason must be an object with 'dated' and 'reason'")
        return
    if _parse_date(reason.get("dated")) is None:
        r.error("SUPPORTS", ref, "supports_none_reason.dated must be an ISO date")
    if not str(reason.get("reason") or "").strip():
        r.error("SUPPORTS", ref, "supports_none_reason.reason is blank")


def check_supports(caps: list[dict], r: Report) -> None:
    """A capability with no supports edge is a claim resting on nothing until admitted.

    PC-ASR-001/AC-3. `check_structure` has always verified that a *present*
    supports ref resolves; it never checked that one was present at all, so a
    capability with none read as a silent pass — indistinguishable from one
    someone had actually reviewed. Blank is now illegal: every capability must
    carry either a supports edge or a dated admission that it has none, so the
    burn-down (s35-b5) has a real illegal state to fix rather than a
    preference.
    """
    for c in caps:
        ref = c["ref"]
        content = c.get("content") or {}
        if content.get("supports"):
            continue
        reason = content.get("supports_none_reason")
        if reason is None:
            r.error(
                "SUPPORTS", ref,
                "no supports edge and no supports_none_reason — blank is not a legal state",
            )
            continue
        _validate_supports_none_reason(ref, reason, r)


def check_evidence(objects: list[dict], r: Report) -> None:
    for o in objects:
        ref = o["ref"]
        evidence = o["content"].get("evidence") or []
        if not evidence:
            r.error("EVIDENCE", ref, "no evidence — an object with no evidence is an intention")
            continue
        for item in evidence:
            if _is_path(item) and not _exists(item):
                r.error("EVIDENCE", ref, f"cited path does not exist: {item}")


def _load_ea_derive() -> Any:
    path = REPO / "scripts" / "ea-derive.py"
    if not path.exists():
        raise RuntimeError("scripts/ea-derive.py is missing")
    spec = importlib.util.spec_from_file_location("ea_derive_for_conformance", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_derived_dependencies(apps: list[dict], r: Report) -> None:
    """Fail when the YAML hand-authors an edge Kubernetes already proves.

    `depends_on` remains reviewable YAML for what manifests cannot prove: external
    runners, code-default clients, dependencies stored behind a DSN secret. For
    everything manifests *can* prove — service hostnames, ExternalSecret-backed
    secret refs, Reloader annotations — `scripts/ea-derive.py depends-on --apply`
    writes the edge into the substrate itself (PC-ASR-007/AC-2), so a hand-authored
    copy in the YAML is no longer required and is now the violation: it is the hand
    copy AC-2 forbids, and it can drift from what the deriver actually computes.
    """
    try:
        derive = _load_ea_derive()
    except Exception as exc:
        r.error("DERIVED", "scripts/ea-derive.py", f"could not load derivation helper: {exc}")
        return

    orig_repo, orig_model_dir, orig_k8s_dir = derive.REPO, derive.MODEL_DIR, derive.K8S_DIR
    derive.REPO = REPO
    derive.MODEL_DIR = MODEL_DIR
    derive.K8S_DIR = REPO / "infrastructure" / "k8s"
    try:
        derived = derive.derive_application_dependencies(apps, derive._load_k8s_documents())
        overlap = derive.authored_derivable_overlap(apps, derived)
    finally:
        derive.REPO, derive.MODEL_DIR, derive.K8S_DIR = orig_repo, orig_model_dir, orig_k8s_dir

    for ref in sorted(overlap):
        for target in sorted(overlap[ref]):
            reasons = "; ".join(sorted(derived[ref][target].reasons))
            r.error(
                "DERIVED", ref,
                f"depends_on hand-authors manifest-derivable dependency '{target}' ({reasons}) "
                "-- scripts/ea-derive.py owns this edge; remove the authored copy and run "
                "`python3 scripts/ea-derive.py depends-on --apply`",
            )


_VALID_RUNTIME = {"kubernetes", "external", "none"}
_VALID_MANAGED_BY = {"argocd", "deploy-workflow", "helm", "bootstrap", "none"}
_WORKLOAD_OBJECT_KEYS = {"cluster", "namespace", "kind", "name", "manifest", "managed_by"}
_EXTERNAL_BINDING_KEYS = {"host", "compose_path", "service"}


def _validate_kubernetes_objects(ref: str, objects: list[dict], r: Report) -> None:
    """Shape/manifest/GitOps-orphan checks for a kubernetes workload's objects.

    Split out of check_workload so the object-shape checks stay testable
    independent of where the objects came from — production runs this
    against derived objects (ea-derive.py's derive_workloads); tests call it
    directly with literal object dicts.
    """
    for o in objects:
        if not isinstance(o, dict) or not _WORKLOAD_OBJECT_KEYS <= set(o):
            r.error("WORKLOAD", ref, f"object is missing keys: {o!r}")
            continue
        where = f"{o['namespace']}/{o['kind']}/{o['name']}"
        if o["managed_by"] not in _VALID_MANAGED_BY:
            r.error("WORKLOAD", ref,
                    f"{where}: managed_by {o['managed_by']!r} not in {sorted(_VALID_MANAGED_BY)}")
        manifest = o.get("manifest")
        if manifest and not _exists(manifest):
            r.error("WORKLOAD", ref, f"{where}: manifest {manifest} does not exist")
        if manifest and o["managed_by"] == "none":
            r.error("WORKLOAD", ref,
                    f"{where}: managed_by 'none' but a manifest is cited — "
                    f"say which mechanism applies it")
        # The B-103 detector. An object that no mechanism applies is a
        # GitOps orphan: nothing updates it, nothing notices it drift, and
        # nothing will ever remove it. Worse than either keeping it under
        # management or tearing it down.
        if not manifest and o["managed_by"] == "none":
            r.warn("WORKLOAD", ref,
                   f"{where}: GitOps orphan — no manifest in this repo and no mechanism "
                   f"managing it (B-103)")


def _derive_workloads(apps: list[dict], r: Report) -> dict[str, list[dict]]:
    """Self-contained: load ea-derive.py, pointed at this module's REPO/MODEL_DIR,
    and compute workload.objects for every kubernetes-runtime application from
    repo manifests and ArgoCD ownership.

    Follows check_derived_dependencies's pattern of loading a fresh module
    instance and pushing this module's own REPO/MODEL_DIR onto it, so tests
    that monkeypatch `ea.REPO`/`ea.MODEL_DIR` to a tmp_path fixture (as
    test_manifest_derived_dependency_missing_from_model_is_an_error does)
    exercise the deriver against that fixture rather than the real repo.
    """
    try:
        derive = _load_ea_derive()
    except Exception as exc:
        r.error("WORKLOAD", "scripts/ea-derive.py", f"could not load derivation helper: {exc}")
        return {}

    orig = (derive.REPO, derive.MODEL_DIR, derive.K8S_DIR, derive.ARGOCD_DIR)
    derive.REPO, derive.MODEL_DIR = REPO, MODEL_DIR
    derive.K8S_DIR = REPO / "infrastructure" / "k8s"
    derive.ARGOCD_DIR = derive.K8S_DIR / "argocd"
    try:
        docs = derive._load_k8s_documents()
        argocd_apps = derive._load_argocd_applications()
        return derive.derive_workloads(apps, docs, argocd_apps)
    finally:
        derive.REPO, derive.MODEL_DIR, derive.K8S_DIR, derive.ARGOCD_DIR = orig


def check_workload(apps: list[dict], r: Report, derived_workloads: dict[str, list[dict]] | None = None) -> None:
    """Every application must say what it actually is, or say that it is nothing.

    The field this checks did not exist until 2026-07-30, and ``check_reality``
    stood in for it by testing ``short in p.stem`` against manifest basenames —
    a substring guess that could only ever see the repository, and could not
    tell "no footprint" apart from "nobody filled this in".

    That distinction carries more than it sounds. An application with no
    ``workload`` is unassessed; one with ``runtime: none`` has been looked at
    and found to run nowhere. Only the second is a fact, and only the second is
    safe to act on.

    kubernetes objects[] is a further refinement (ea-metamodel.md §4.2): the
    manifest path, the ArgoCD ownership, and each object's cluster/namespace/
    kind/name are derived from the repo (ea-derive.py's derive_workloads), not
    authored. A YAML that still hand-authors objects[] fails here, naming the
    deriver, so the fix is always "delete the block", never "reconcile it by
    hand". external/none runtimes are untouched — a compose binding or a note
    is a claim about a system this repo cannot see.
    """
    if derived_workloads is None:
        derived_workloads = _derive_workloads(apps, r)

    for a in apps:
        ref, content = a["ref"], a["content"]
        wl = content.get("workload")
        if not isinstance(wl, dict):
            r.error("WORKLOAD", ref, "no workload declared — absent is not the same as none")
            continue

        runtime = wl.get("runtime")
        if runtime not in _VALID_RUNTIME:
            r.error("WORKLOAD", ref, f"runtime {runtime!r} not in {sorted(_VALID_RUNTIME)}")
            continue

        if runtime == "kubernetes":
            if wl.get("objects"):
                r.error("WORKLOAD", ref,
                        "workload.objects is hand-authored — kubernetes objects are derived "
                        "by scripts/ea-derive.py (derive_workloads); remove this block from the YAML")
            objects = derived_workloads.get(ref) or []
            if not objects:
                r.error("WORKLOAD", ref,
                        "runtime 'kubernetes' with no derivable objects — no repo manifest or "
                        "ArgoCD ownership names this application")
            _validate_kubernetes_objects(ref, objects, r)
        else:
            objects = wl.get("objects") or []
            if objects:
                r.error("WORKLOAD", ref, f"runtime {runtime!r} must not declare objects")
            binding = wl.get("binding")
            has_binding = isinstance(binding, dict)
            if binding is not None and not has_binding:
                r.error("WORKLOAD", ref, "binding must be an object")
            if not has_binding and not (wl.get("note") or "").strip():
                r.error("WORKLOAD", ref,
                        f"runtime {runtime!r} requires a note saying where it runs, "
                        "or a structured binding")
            if runtime == "none" and binding is not None:
                r.error("WORKLOAD", ref, "runtime 'none' must not declare a binding")
            if runtime == "external" and has_binding:
                missing = _EXTERNAL_BINDING_KEYS - set(binding)
                for key in sorted(missing):
                    r.error("WORKLOAD", ref, f"external binding missing '{key}'")
                for key in sorted(_EXTERNAL_BINDING_KEYS & set(binding)):
                    if not str(binding[key]).strip():
                        r.error("WORKLOAD", ref, f"external binding {key} is blank")
                compose_path = binding.get("compose_path")
                if compose_path and not _exists(str(compose_path)):
                    r.error("WORKLOAD", ref, f"external binding compose_path {compose_path} does not exist")


def _check_app_reality(ref: str, content: dict, state: str, objects: list[dict], r: Report) -> None:
    short = ref.split(".", 1)[1]

    # A custom-built application in build/operate must have source in the tree.
    if content.get("build") == "custom" and state in {"build", "operate"}:
        if not (REPO / "apps" / short).exists():
            r.warn("REALITY", ref, f"build=custom, state={state}, but apps/{short}/ is absent")

    # An eliminated application must not still be deployed. B-103 is the
    # standing lesson: de-managing without tearing down leaves an orphan
    # that is worse than either keeping or killing it.
    if content.get("time_disposition") == "eliminate" and objects:
        live = [f"{o['namespace']}/{o['kind']}/{o['name']}" for o in objects]
        r.warn(
            "REALITY", ref,
            f"disposition 'eliminate' but it still declares a workload: "
            f"{', '.join(live[:3])}",
        )

    # A terminal lifecycle state is a claim that nothing is running. The
    # model scored app.itop 'eol' while both of its objects were 1/1 on the
    # cluster named by EA_CANONICAL_CLUSTER, with 40 days of uptime — the
    # state said gone, the cluster said otherwise, and nothing in this file
    # could tell.
    if state == "eol" and objects:
        r.error(
            "REALITY", ref,
            "state 'eol' asserts nothing is running, but the model declares workload "
            "objects — one of the two is false",
        )


def check_reality(
    apps: list[dict], r: Report, derived_workloads: dict[str, list[dict]] | None = None
) -> None:
    """Probe the repo for what the disposition implies.

    Deliberately narrow. This cannot see the cluster — no kubeconfig in CI for
    the canonical node — so it asserts only what the repository can prove. What
    it can now do is read the declared workload rather than guess at filenames,
    which is the difference between "a file with a similar name exists" and
    "this application says it is these objects".

    kubernetes objects[] is derived (ea-metamodel.md §4.2), so a kubernetes
    application's objects come from `derived_workloads` when the caller
    supplies it (main() always does); a fixture that still authors objects[]
    directly and calls this without that argument keeps working via the
    fallback to the literal declaration.
    """
    derived_workloads = derived_workloads or {}
    for a in apps:
        ref, content, state = a["ref"], a["content"], a["state"]
        wl = content.get("workload") or {}
        if wl.get("runtime") == "kubernetes":
            objects = derived_workloads.get(ref) or (wl.get("objects") or [])
        else:
            objects = wl.get("objects") or []
        _check_app_reality(ref, content, state, objects, r)


def check_pillar10(r: Report) -> None:
    """Fail if a substrate bead-content schema declares execution state.

    Pillar 10 (Amendment 24). This is the one check that reads source rather
    than the model, because the rule it enforces is about code: the EA model can
    say "intent and execution are separate" indefinitely while `schemas.py`
    quietly says otherwise. `current-state.md` rotted precisely because its rule
    was enforced by good intentions — so this one is enforced by the build.

    Parsed with `ast` rather than imported: the script runs in CI without the
    substrate's dependencies installed, and importing pydantic models to inspect
    them would make this check fail for reasons unrelated to what it tests.
    """
    path = REPO / _SUBSTRATE_SCHEMAS
    if not path.exists():
        r.warn("PILLAR10", str(_SUBSTRATE_SCHEMAS), "schema module not found — check skipped")
        return

    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as exc:
        r.error("PILLAR10", str(_SUBSTRATE_SCHEMAS), f"could not parse: {exc}")
        return

    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            field = stmt.target.id
            if field not in _EXECUTION_STATE_FIELDS:
                continue
            key = (node.name, field)
            seen.add(key)
            ref = f"{node.name}.{field}"
            if key in _PILLAR10_KNOWN:
                r.warn("PILLAR10", ref, f"known violation — {_PILLAR10_KNOWN[key]}")
            else:
                r.error(
                    "PILLAR10", ref,
                    "execution state in a bead content schema violates Pillar 10 "
                    "(Amendment 24). Retry counts, leases, and heartbeats belong to "
                    "Temporal, not to bead content.",
                )

    # A known violation that has been fixed must be removed from the allowlist,
    # or the list outlives the debt and starts lying about what the code does.
    for key, why in _PILLAR10_KNOWN.items():
        if key not in seen:
            r.error(
                "PILLAR10", f"{key[0]}.{key[1]}",
                f"listed in _PILLAR10_KNOWN but no longer present — delete the entry ({why})",
            )


_FOUNDATION_APPS = ("app.substrate", "app.temporal")
_FOUNDATION_NOTE_MARKER = "FOUNDATION (Amendment 24"


def check_foundation(apps: list[dict], services: list[dict], r: Report) -> None:
    """The Amendment 24 foundation grouping must be expressed somewhere.

    ea-metamodel.md §8.4 carried the grouping as a `FOUNDATION` clause in
    app.substrate's and app.temporal's `note` fields, as deliberate
    under-modelling with a stated exit: once arch.service lands, the grouping
    becomes a real service object with `depends_on` edges to both
    applications, and the notes collapse into it. This check does not care
    which form is present — it only refuses the state where neither is, which
    is the one state that would let the collapse happen silently and drop the
    grouping instead of migrating it.
    """
    by_ref = {a["ref"]: a for a in apps}
    notes_say_foundation = all(
        _FOUNDATION_NOTE_MARKER in str((by_ref.get(ref, {}).get("content") or {}).get("note") or "")
        for ref in _FOUNDATION_APPS
    )
    service_says_foundation = any(
        set(_FOUNDATION_APPS) <= set((s.get("content") or {}).get("depends_on") or [])
        for s in services
    )
    if not notes_say_foundation and not service_says_foundation:
        r.error(
            "FOUNDATION", "app.substrate/app.temporal",
            "the Amendment 24 foundation grouping is expressed in neither the applications' "
            "notes nor a service object whose depends_on covers app.substrate and app.temporal",
        )


def check_freshness(objects: list[dict], max_age_days: int, r: Report) -> None:
    today = dt.date.today()
    for o in objects:
        raw = o["content"].get("assessed_at")
        if not raw:
            r.error("FRESHNESS", o["ref"], "no assessed_at")
            continue
        assessed = raw if isinstance(raw, dt.date) else dt.date.fromisoformat(str(raw))
        age = (today - assessed).days
        if age > max_age_days:
            r.warn("FRESHNESS", o["ref"], f"last assessed {age} days ago (limit {max_age_days})")


# --- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-age-days", type=int, default=90,
                    help="assessed_at older than this warns (default: 90)")
    ap.add_argument("--baseline-ref",
                    help="git ref to compare capability maturity against (default: PR base or origin/main)")
    ap.add_argument("--no-baseline", action="store_true",
                    help="skip change-aware maturity raise checks")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = ap.parse_args()

    caps = _load("business-layer.yaml", "capabilities")
    apps = _load("application-portfolio.yaml", "applications")
    services = _load("services.yaml", "services", required=False)
    observations = _load("observations.yaml", "observations", required=False)
    everything = caps + apps + services
    baseline_caps = None

    r = Report()
    if not args.no_baseline:
        baseline_ref = _baseline_ref_from_env(args.baseline_ref)
        if baseline_ref:
            baseline_caps = _load_caps_at(baseline_ref, r)
    check_structure(caps, apps, observations, r, services=services)
    check_consistency(caps, apps, r)
    check_roadmap(caps, apps, r)
    check_maturity_raises(caps, apps, observations, baseline_caps, r)
    check_supports(caps, r)
    check_evidence(everything, r)
    check_observations(observations, r)
    check_derived_dependencies(apps, r)
    derived_workloads = _derive_workloads(apps, r)
    check_workload(apps, r, derived_workloads)
    check_reality(apps, r, derived_workloads)
    check_pillar10(r)
    check_foundation(apps, services, r)
    check_freshness(everything, args.max_age_days, r)

    print(
        f"EA model: {len(caps)} capabilities, {len(apps)} applications, "
        f"{len(services)} services, {len(observations)} observations"
    )

    if r.warnings:
        print(f"\n{len(r.warnings)} warning(s):")
        print("\n".join(r.warnings))
    if r.errors:
        print(f"\n{len(r.errors)} error(s):")
        print("\n".join(r.errors))
        print("\nFAIL — the model contradicts itself or the repository.")
        return 1
    if args.strict and r.warnings:
        print("\nFAIL — warnings are errors under --strict.")
        return 1

    print("\nOK — model is internally consistent and its cited evidence exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
