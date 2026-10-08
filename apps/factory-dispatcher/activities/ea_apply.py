"""In-cluster EA-model applier (PC-ASR-007/AC-3; PC-ASR-001/AC-2).

``ea-load.py`` has always been able to write ``docs/architecture/model/**`` into the
substrate; nothing has ever called it on a schedule (PC-ASR-001/AC-2, failing live since
2026-08-09: "ea-load.py appears in no workflow"). This activity is that caller, running
beside the dispatcher on the same worker so it reuses the ``SUBSTRATE_API_KEY`` write
credential already there — no new credential is minted, and none reaches CI or the repo
(the NFR this task carries).

Two rules, both mechanized here rather than left to the reconcile loop's own idempotency:

**PRIN-014 — the same revision twice is zero writes.** "Revision" is the SHA of the last
commit under ``docs/architecture/model`` on ``cfg.base_ref`` (:func:`current_model_revision`),
not the branch tip -- it only changes when a commit actually touches that path. The standing
status bead's ``context.applied_revision`` is compared against it *before* anything else runs;
on a match this function returns without calling ``ea-load.py`` or touching the substrate again.

**PRIN-008 — a failure halts loudly and a retry carries its predecessor's reason.** On a
reconcile failure the standing bead is updated with the failure and the revision that produced
it *before* the exception is re-raised, so the record is visible without querying Temporal (the
activity/workflow failing is true too, but that is not what the acceptance clause asks for). The
next attempt reads that record and, whichever way it resolves, carries the prior reason forward
in its own (``previous_failure`` or ``recovered_from``) rather than silently overwriting it.

One standing ``arch.observation`` bead, ``content.ref == STATUS_REF`` -- the same "one record
per condition, not one run" idiom ``activities/doctrine_staleness.py`` already uses, for the same
reason: a re-run finds and updates the same bead instead of accumulating one per firing.
``ArchObservationContent`` (apps/substrate/src/schemas.py) is a closed schema shaped for exactly
this: ``last_synced_revision`` already means "the revision most recently synced," and
``workload`` follows doctrine_staleness's precedent of naming a repository artifact
(``cluster: "repository"``) rather than a cluster workload. The free-form half of the bead --
``context``, orthogonal to the typed ``content`` -- carries status/reason/summary detail no
closed schema needs to grow a field for.

**The dependency reconcile (PC-ASR-007/AC-2) rides the same cycle, ungated by PRIN-014.**
``scripts/ea-derive.py``'s ``reconcile_dependencies`` writes manifest-derived ``depends_on``
edges into the substrate as ``bead_link`` rows it owns (``created_by="ea-derive"``), so a human
never hand-copies one into the YAML. It runs on every invocation of this activity -- not only
when the model revision moves -- because a derived edge can go stale from a change under
``infrastructure/k8s/**`` alone, a path PRIN-014's revision gate does not watch. This is safe
unconditionally: the reconcile is idempotent (unchanged inputs, zero writes), so running it every
tick costs a few extra read calls, not meaningful work. It is wired in via ``dependency_sub`` and
``derived_dependencies`` -- both default to ``None``, so every existing caller of
``apply_ea_model`` that does not pass them (all of today's tests) is unaffected; the step is
simply skipped.

**A kubernetes application's workload objects are resolved before ``build_plan`` ever sees
them.** #514 made ``content.workload.objects`` derived from the manifests rather than
hand-authored (ea-metamodel.md §4.2): every kubernetes-runtime record in
``application-portfolio.yaml`` now carries ``workload: {runtime: kubernetes}`` and nothing else.
``ea-load.py``'s ``build_plan`` reads that YAML from disk unchanged -- it has no hook to accept
an already-resolved object list, and it is out of scope to give it one -- so
:func:`_resolve_kubernetes_workloads` writes a throwaway copy of ``model_dir`` whose
``application-portfolio.yaml`` has each kubernetes application's ``workload.objects`` filled in
from ``derived_workloads`` (``scripts/ea-derive.py``'s ``derive_workloads``), and passes that copy
to ``build_plan`` instead. A kubernetes application the derivation resolves to nothing is refused
by name before any request reaches the substrate -- the 422 stops being the thing that catches it.
Like the dependency reconcile, this is wired in via an optional ``derived_workloads`` parameter
that defaults to ``None``; every existing caller that does not pass it is unaffected.

**A failure that repeats its predecessor's exact reason is marked as a standing condition.**
PRIN-008's failure record already carries ``previous_failure`` forward; when the new failure's
``reason`` matches it verbatim, the record also carries ``repeat_of_previous_failure: true`` and
a running ``consecutive_failures`` count, reset to 1 the moment the reason changes. This is
visible on the standing bead itself -- readable without a notification transport -- so a reconcile
wedged on the same cause cannot repeat silently.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml
from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

import dispatch  # noqa: E402
from activities import status_bead  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]
EA_LOAD_PATH = REPO_ROOT / "scripts" / "ea-load.py"
EA_DERIVE_PATH = REPO_ROOT / "scripts" / "ea-derive.py"
MODEL_RELATIVE_DIR = Path("docs") / "architecture" / "model"
APPLICATION_PORTFOLIO_FILENAME = "application-portfolio.yaml"
STATUS_REF = "obs.ea-apply-status"
CREATED_BY = "factory-dispatcher/ea-apply"
# "factory-dispatcher/ea-apply" is enrolled in apps/substrate/src/bead_rules.py's
# SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822) -- declaring "derived" here depends
# on that enrolment being DEPLOYED, not just merged (unenrolled, this would be a
# 409). See .factory/design.md.
SOURCE_CLASS = "derived"


class EaApplyError(RuntimeError):
    """A reconcile failure, wrapped with the model revision it was attempting."""


class ModelSubstrate(Protocol):
    """Exactly the seven calls ``ea-load.py``'s own ``Substrate`` class makes.

    The same object drives both the model reconcile (via ``ea-load.py``'s ``build_plan``)
    and the standing status bead read/write below -- both are ``arch`` namespace beads, and a
    second HTTP client here would be a second place for the schema to drift. The seventh,
    ``find_bead``, is the standing-status lookup (OPS-181/OPS-182): resolving
    ``content.ref`` through the store's own indexed lookup rather than paging
    ``list_beads`` toward a bead a busy night can push past any fixed page size.

    ``list_beads`` carries ``offset`` (OPS-191 round 4) so ``ea-load.py``'s own paging walk
    (``_list_all``, dev.finding 2bf3c19b) works unchanged through this Protocol too -- the
    orphan/prune pass in ``reconcile_objects`` needs the full population regardless of the
    ``find_bead`` lookup above, matching ``scripts/substrate_client.py``'s ``Substrate``.
    """

    def list_beads(
        self, bead_type: str, limit: int = 1000, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    def find_bead(
        self, namespace: str, type: str, content_ref: str
    ) -> dict[str, Any] | None: ...

    def create(
        self, bead_type: str, state: str, content: dict[str, Any], parent_id: str | None,
        *, created_by: str = "",
    ) -> dict[str, Any]: ...

    def patch(
        self, bead_id: str, body: dict[str, Any], *, created_by: str = ""
    ) -> dict[str, Any]: ...

    def links(self, bead_id: str, direction: str = "outgoing") -> list[dict[str, Any]]: ...

    def add_link(self, source_id: str, target_id: str, link_type: str) -> dict[str, Any]: ...

    def delete_link(self, link_id: str) -> None: ...

    def delete_bead(self, bead_id: str) -> None: ...


class DependencySubstrate(Protocol):
    """The substrate calls ``scripts/ea-derive.py``'s dependency reconcile needs.

    Distinct from :class:`ModelSubstrate`: writing an edge this deriver owns needs to tag a
    writer identity (``created_by``) per link, which ``ea-load.py``'s ``Substrate.add_link``
    has no parameter for and cannot be given one without editing ``ea-load.py`` (out of
    scope). ``scripts/ea-derive.py``'s own ``Substrate`` class implements this surface.
    """

    def list_beads(self, bead_type: str, limit: int = 1000) -> list[dict[str, Any]]: ...

    def links(self, bead_id: str, direction: str = "outgoing") -> list[dict[str, Any]]: ...

    def add_link(
        self, source_id: str, target_id: str, link_type: str, *, created_by: str
    ) -> dict[str, Any]: ...

    def delete_link(self, link_id: str) -> None: ...


def _load_ea_load_module():
    """Import scripts/ea-load.py by path -- the filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("ea_load", EA_LOAD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ea_derive_module():
    """Import scripts/ea-derive.py by path, the same way ``_load_ea_load_module`` does."""
    spec = importlib.util.spec_from_file_location("ea_derive", EA_DERIVE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_model_revision(
    cfg: "dispatch.Config", *, relative_path: Path = MODEL_RELATIVE_DIR
) -> str:
    """The SHA of the last commit under ``docs/architecture/model`` on ``cfg.base_ref``.

    Changes exactly when a commit touches that path -- not on every commit to
    ``cfg.base_ref`` -- so comparing it is what makes "nothing under
    docs/architecture/model/** changed" a single string comparison.
    """
    result = dispatch.run(
        [dispatch.GIT, "log", "-1", "--format=%H", cfg.base_ref, "--", str(relative_path)],
        cwd=cfg.repo_root,
    )
    revision = result.stdout.strip()
    if not revision:
        raise EaApplyError(
            f"no commit under {relative_path} on {cfg.base_ref} -- the model would not exist"
        )
    return revision


def _status_workload() -> dict[str, str]:
    return {
        "cluster": "repository",
        "namespace": "architecture",
        "kind": "EaModel",
        "name": "docs/architecture/model",
    }


def _status_content(revision: str | None, observed_at: datetime) -> dict[str, Any]:
    return status_bead.status_content(
        STATUS_REF, _status_workload(), revision, status_bead.iso(observed_at),
        source_class=SOURCE_CLASS,
    )


def find_status(sub: ModelSubstrate) -> dict[str, Any] | None:
    return status_bead.find_status(sub, STATUS_REF)


def _write_status(
    sub: ModelSubstrate, existing: dict[str, Any] | None, content: dict[str, Any],
    context: dict[str, Any],
) -> None:
    try:
        status_bead.write_status(sub, existing, content, context, created_by=CREATED_BY)
    except status_bead.DuplicateRefRefusal as dup:
        _record_duplicate_ref_refusal(sub, dup)
        raise EaApplyError(str(dup)) from dup


def _record_duplicate_ref_refusal(
    sub: ModelSubstrate, dup: "status_bead.DuplicateRefRefusal"
) -> None:
    """The create/create race ``find_status`` ordinarily prevents happened anyway: the
    bead now exists (that is what the 409 means), so record the refusal on it by patch
    -- never another create -- using the status code and ref, never the response's
    detail text (OPS-182's mislabel)."""
    refreshed = find_status(sub)
    if refreshed is None:
        return
    status_bead.write_status(
        sub, refreshed, refreshed.get("content") or {},
        {
            "status": "failed",
            "reason": str(dup),
            "duplicate_ref_refusal": {"status": dup.status, "ref": dup.ref},
        },
        created_by=CREATED_BY,
    )


def _prior_failure(context: dict[str, Any]) -> dict[str, Any] | None:
    return status_bead.prior_failure(context, "model_revision")


def _resolve_kubernetes_workloads(
    model_dir: Path, derived_workloads: dict[str, list[dict[str, Any]]]
) -> Path:
    """A throwaway copy of ``model_dir`` with kubernetes workload objects filled in.

    ``build_plan`` (``scripts/ea-load.py``) reads ``application-portfolio.yaml`` from disk
    itself and has no parameter for an already-resolved object list, so this writes a copy of
    the model directory with each kubernetes-runtime application's ``workload.objects`` set to
    its authored objects (normally none, post-#514) plus whatever ``derived_workloads`` -- keyed
    by application ``ref`` -- resolved for it. The caller is responsible for deleting the
    returned directory.

    Raises :class:`EaApplyError`, naming every kubernetes-runtime application the derivation
    left with no objects at all, instead of writing an empty ``workload`` and letting the
    substrate's ``workload_shape_matches_runtime`` 422 be what catches it.
    """
    portfolio_path = model_dir / APPLICATION_PORTFOLIO_FILENAME
    portfolio = yaml.safe_load(portfolio_path.read_text()) or {}
    missing: list[str] = []

    for app in portfolio.get("applications", []):
        content = app.get("content") or {}
        workload = content.get("workload") or {}
        if workload.get("runtime") != "kubernetes":
            continue
        objects = list(workload.get("objects") or []) + list(
            derived_workloads.get(app["ref"]) or []
        )
        if not objects:
            missing.append(app["ref"])
            continue
        workload["objects"] = objects
        content["workload"] = workload
        app["content"] = content

    if missing:
        raise EaApplyError(
            "kubernetes-runtime application(s) with no derivable workload -- refusing rather "
            "than sending an empty workload: " + ", ".join(sorted(missing))
        )

    resolved_dir = Path(tempfile.mkdtemp(prefix="ea-apply-resolved-"))
    for path in model_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == APPLICATION_PORTFOLIO_FILENAME:
            (resolved_dir / path.name).write_text(yaml.safe_dump(portfolio, sort_keys=False))
        else:
            shutil.copy2(path, resolved_dir / path.name)
    return resolved_dir


def _reconcile_dependencies_if_wired(
    result: dict[str, Any],
    *,
    dependency_sub: "DependencySubstrate | None",
    derived_dependencies: dict[str, Any] | None,
    reconcile_dependencies_fn: Any,
) -> dict[str, Any]:
    """Run the PC-ASR-007/AC-2 dependency reconcile, ungated by PRIN-014, if wired in.

    Both `dependency_sub` and `derived_dependencies` must be supplied for this to run --
    the default (`None`, `None`) makes it a no-op, so every existing caller of
    `apply_ea_model` that predates this step is unaffected. See the module docstring for
    why this runs on every invocation rather than only when the model revision moves.
    """
    if dependency_sub is None or derived_dependencies is None:
        return result
    plan = reconcile_dependencies_fn(dependency_sub, derived_dependencies)
    return {**result, "dependencies": {"created": len(plan.created), "removed": len(plan.removed)}}


def apply_ea_model(
    sub: ModelSubstrate,
    *,
    cfg: "dispatch.Config",
    build_plan: Any,
    model_dir: Path | None = None,
    now_fn: Any = lambda: datetime.now(timezone.utc),
    revision_fn: Any = current_model_revision,
    dependency_sub: "DependencySubstrate | None" = None,
    derived_dependencies: dict[str, Any] | None = None,
    reconcile_dependencies_fn: Any = None,
    derived_workloads: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Reconcile ``docs/architecture/model/**`` into ``sub`` iff its revision moved.

    Returns ``{"status": "unchanged"|"applied", "revision": ..., "summary": {...}}`` on success,
    plus ``"dependencies": {"created": ..., "removed": ...}`` when the dependency reconcile ran
    (``dependency_sub`` and ``derived_dependencies`` both given -- see the module docstring).
    Raises :class:`EaApplyError` on a reconcile failure, after writing the standing failure
    record -- callers (the Temporal activity wrapper) let that propagate so the workflow itself
    also fails loudly. A build_plan failure skips the dependency reconcile for this call; the
    next tick still resolves it once the authored reconcile succeeds again.

    When ``derived_workloads`` is given (``{application ref: [object, ...]}``, the output of
    ``scripts/ea-derive.py``'s ``derive_workloads``), every kubernetes-runtime application's
    ``workload.objects`` is resolved from it before ``build_plan`` runs -- see
    :func:`_resolve_kubernetes_workloads`. Defaulting to ``None`` skips this step entirely, so
    every existing caller that predates it is unaffected.
    """
    model_dir = model_dir or (cfg.repo_root / MODEL_RELATIVE_DIR)
    revision = revision_fn(cfg)
    now = now_fn()

    existing = find_status(sub)
    prior_context = (existing or {}).get("context") or {}
    applied_revision = prior_context.get("applied_revision")

    if revision == applied_revision:
        # R2605-8 DEFECT 2: PRIN-014's short-circuit must skip the model
        # reconcile, not the standing status record -- the record is the
        # report that this run happened, not a model write. Without this
        # write a reconciler idle for 12 days and one that stopped 12 days
        # ago produce the identical bead; "unchanged" is a distinct outcome
        # value from "ok" (applied) precisely so a reader never has to guess
        # which one they are looking at.
        context = {
            "status": "unchanged",
            "model_revision": revision,
            "applied_revision": applied_revision,
            "checked_at": status_bead.iso(now),
        }
        _write_status(sub, existing, _status_content(applied_revision, now), context)
        result = {"status": "unchanged", "revision": revision}
        return _reconcile_dependencies_if_wired(
            result, dependency_sub=dependency_sub, derived_dependencies=derived_dependencies,
            reconcile_dependencies_fn=reconcile_dependencies_fn,
        )

    try:
        resolved_model_dir = model_dir
        resolved_tmp_dir: Path | None = None
        if derived_workloads is not None:
            resolved_tmp_dir = _resolve_kubernetes_workloads(model_dir, derived_workloads)
            resolved_model_dir = resolved_tmp_dir
        try:
            plan = build_plan(sub, dry_run=False, model_dir=resolved_model_dir)
        finally:
            if resolved_tmp_dir is not None:
                shutil.rmtree(resolved_tmp_dir, ignore_errors=True)
    except Exception as exc:
        reason = str(exc)
        previous_failure = _prior_failure(prior_context)
        repeated = previous_failure is not None and previous_failure.get("reason") == reason
        consecutive_failures = (
            prior_context.get("consecutive_failures", 1) + 1 if repeated else 1
        )
        context: dict[str, Any] = {
            "status": "failed",
            "model_revision": revision,
            "applied_revision": applied_revision,
            "reason": reason,
            "failed_at": status_bead.iso(now),
            "consecutive_failures": consecutive_failures,
        }
        if repeated:
            context["repeat_of_previous_failure"] = True
        if previous_failure is not None:
            context["previous_failure"] = previous_failure
        _write_status(sub, existing, _status_content(applied_revision, now), context)
        raise EaApplyError(
            f"ea-load reconcile failed at revision {revision}: {reason}"
        ) from exc

    summary = {
        "created": len(plan.created),
        "updated": len(plan.updated),
        "links_added": len(plan.links_added),
        "links_removed": len(plan.links_removed),
        "orphans": len(plan.orphans),
    }
    context = {
        "status": "ok",
        "model_revision": revision,
        "applied_revision": revision,
        "summary": summary,
        "applied_at": status_bead.iso(now),
    }
    recovered_from = _prior_failure(prior_context)
    if recovered_from is not None:
        context["recovered_from"] = recovered_from
    _write_status(sub, existing, _status_content(revision, now), context)
    result = {"status": "applied", "revision": revision, "summary": summary}
    return _reconcile_dependencies_if_wired(
        result, dependency_sub=dependency_sub, derived_dependencies=derived_dependencies,
        reconcile_dependencies_fn=reconcile_dependencies_fn,
    )


@activity.defn(name="apply_ea_model")
def apply_ea_model_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    ea_load = _load_ea_load_module()
    ea_derive = _load_ea_derive_module()
    cfg = dispatch.Config.from_env()
    apps = ea_derive._load_model_applications()
    docs = ea_derive._load_k8s_documents()
    derived_dependencies = ea_derive.derive_application_dependencies(apps, docs)
    derived_workloads = ea_derive.derive_workloads(apps, docs)
    return apply_ea_model(
        ea_load.Substrate(), cfg=cfg, build_plan=ea_load.build_plan,
        dependency_sub=ea_derive.Substrate(), derived_dependencies=derived_dependencies,
        reconcile_dependencies_fn=ea_derive.reconcile_dependencies,
        derived_workloads=derived_workloads,
    )


ACTIVITIES = [apply_ea_model_activity]
