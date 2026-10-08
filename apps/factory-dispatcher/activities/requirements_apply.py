"""In-cluster requirements-registry applier (PC-ASR-002/AC-2; PRIN-014; PRIN-008).

`scripts/requirements-load.py` has always been able to mirror the two requirement
registries into `arch.requirement` and dated `arch.requirement_conformance` beads;
nothing has ever called it on a schedule — the one arch loader left hand-cranked
(`tasks/done/OPS-37`'s named defect: "the requirements registries mirror only when
a human remembers"). The consequence was live on 2026-09-07: three re-measured
verdicts merged in #683 sat in git while release-status, release-notes and the
console's cited-criteria panel reported the prior ones. This activity is the
scheduled caller, running beside the dispatcher on the same worker so it reuses
the `SUBSTRATE_API_KEY` write credential already there.

Unlike `release_apply` (a thin wrapper — see its docstring for why it carries no
revision gate and no status bead), this applier carries both, sharing
`activities/status_bead.py`'s landing helpers with `activities/ea_apply.py`:

* **PRIN-014 revision short-circuit.** `reconcile()` already content-diffs, but a
  full reconcile still lists every requirement and conformance bead (~280 today)
  to discover nothing changed. The standing status bead's
  `context.applied_revision` is compared against the last commit touching
  `docs/requirements` first, so the ~96 idle ticks a day cost one `git log` and
  one observation lookup.
* **PRIN-008 standing failure record.** One `arch.observation` with
  `content.ref == STATUS_REF` records success revision or failure context
  (`previous_failure` / `recovered_from` / `repeat_of_previous_failure` /
  `consecutive_failures` — verbatim ea_apply semantics), then the error still
  propagates so the Temporal run shows failed.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

import dispatch  # noqa: E402
from activities import status_bead  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]
REQUIREMENTS_LOAD_PATH = REPO_ROOT / "scripts" / "requirements-load.py"
REQUIREMENTS_RELATIVE_DIR = Path("docs/requirements")

STATUS_REF = "obs.requirements-apply-status"
CREATED_BY = "factory-dispatcher/requirements-apply"
# "factory-dispatcher/requirements-apply" is enrolled in
# apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119,
# #822) -- declaring "derived" here depends on that enrolment being DEPLOYED,
# not just merged (unenrolled, this would be a 409). See .factory/design.md.
SOURCE_CLASS = "derived"


class RequirementsApplyError(RuntimeError):
    """The reconcile recorded errors; recorded on the status bead, then propagated."""


def _load_requirements_load_module():
    """Import scripts/requirements-load.py by path — the filename is not a module name."""
    spec = importlib.util.spec_from_file_location("requirements_load", REQUIREMENTS_LOAD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_registries_revision(
    cfg: "dispatch.Config", *, relative_path: Path = REQUIREMENTS_RELATIVE_DIR
) -> str:
    """The SHA of the last commit under ``docs/requirements`` on ``cfg.base_ref``.

    Changes exactly when a commit touches the registries — not on every commit —
    so "nothing changed" is a single string comparison (PRIN-014).
    """
    result = dispatch.run(
        [dispatch.GIT, "log", "-1", "--format=%H", cfg.base_ref, "--", str(relative_path)],
        cwd=cfg.repo_root,
    )
    revision = result.stdout.strip()
    if not revision:
        raise RequirementsApplyError(
            f"no commit under {relative_path} on {cfg.base_ref} — the registries would not exist"
        )
    return revision


def _status_workload() -> dict[str, Any]:
    return {
        "cluster": "repository",
        "namespace": "requirements",
        "kind": "RequirementRegistry",
        "name": "docs/requirements",
    }


def _status_content(revision: str | None, observed_at: datetime) -> dict[str, Any]:
    return status_bead.status_content(
        STATUS_REF, _status_workload(), revision, status_bead.iso(observed_at),
        source_class=SOURCE_CLASS,
    )


def find_status(sub: Any) -> dict[str, Any] | None:
    return status_bead.find_status(sub, STATUS_REF)


def _write_status(
    sub: Any, existing: dict[str, Any] | None, content: dict[str, Any],
    context: dict[str, Any],
) -> None:
    try:
        status_bead.write_status(sub, existing, content, context, created_by=CREATED_BY)
    except status_bead.DuplicateRefRefusal as dup:
        _record_duplicate_ref_refusal(sub, dup)
        raise RequirementsApplyError(str(dup)) from dup


def _record_duplicate_ref_refusal(sub: Any, dup: "status_bead.DuplicateRefRefusal") -> None:
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
    return status_bead.prior_failure(context, "registries_revision")


def apply_requirement_registries(
    sub: Any,
    *,
    cfg: "dispatch.Config",
    requirements_load: Any = None,
    now_fn: Any = lambda: datetime.now(timezone.utc),
    revision_fn: Any = current_registries_revision,
) -> dict[str, Any]:
    """Mirror the requirement registries into the substrate iff their revision moved.

    Returns ``{"status": "unchanged"|"applied", "revision": ..., "summary": {...}}``.
    Raises :class:`RequirementsApplyError` after writing the standing failure
    record; the Temporal activity wrapper lets that propagate so the workflow run
    itself fails loudly (PRIN-008: the failure is recorded AND announced, never
    swallowed into a green run).
    """
    requirements_load = requirements_load or _load_requirements_load_module()
    revision = revision_fn(cfg)
    now = now_fn()

    existing = find_status(sub)
    prior_context = (existing or {}).get("context") or {}
    applied_revision = prior_context.get("applied_revision")

    if revision == applied_revision:
        # R2605-8 DEFECT 2 (sibling reconciler to activities/ea_apply.py's
        # own fix): the status record must be refreshed even when nothing
        # was applied, so a frozen observed_at can only mean the reconciler
        # stopped. "unchanged" is a distinct outcome value from "ok".
        context = {
            "status": "unchanged",
            "registries_revision": revision,
            "applied_revision": applied_revision,
            "checked_at": status_bead.iso(now),
        }
        _write_status(sub, existing, _status_content(applied_revision, now), context)
        return {"status": "unchanged", "revision": revision}

    try:
        requirements, conformances = requirements_load.load_registries()
        plan = requirements_load.reconcile(sub, requirements, conformances, apply=True)
        if plan.errors:
            raise RequirementsApplyError(
                f"requirements-load reconcile recorded {len(plan.errors)} error(s): "
                + "; ".join(plan.errors)
            )
    except (Exception, SystemExit) as exc:
        # SystemExit included deliberately: scripts/requirements-load.py's
        # load_registries() sys.exit()s on a malformed registry (duplicate id),
        # and SystemExit derives from BaseException — without this it would
        # bypass the PRIN-008 standing record while the bead kept saying ok
        # (release-gate finding F1 on the PR that landed this).
        reason = str(exc)
        previous_failure = _prior_failure(prior_context)
        repeated = previous_failure is not None and previous_failure.get("reason") == reason
        consecutive_failures = (
            prior_context.get("consecutive_failures", 1) + 1 if repeated else 1
        )
        context: dict[str, Any] = {
            "status": "failed",
            "registries_revision": revision,
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
        if isinstance(exc, RequirementsApplyError):
            raise
        raise RequirementsApplyError(
            f"requirements-load reconcile failed at revision {revision}: {reason}"
        ) from exc

    summary = {
        "requirement_creates": len(plan.requirement_creates),
        "requirement_updates": len(plan.requirement_updates),
        "requirement_unchanged": len(plan.requirement_unchanged),
        "conformance_creates": len(plan.conformance_creates),
        "conformance_updates": len(plan.conformance_updates),
        "conformance_unchanged": len(plan.conformance_unchanged),
    }
    context = {
        "status": "ok",
        "registries_revision": revision,
        "applied_revision": revision,
        "summary": summary,
        "applied_at": status_bead.iso(now),
    }
    recovered_from = _prior_failure(prior_context)
    if recovered_from is not None:
        context["recovered_from"] = recovered_from
    _write_status(sub, existing, _status_content(revision, now), context)
    return {"status": "applied", "revision": revision, "summary": summary}


@activity.defn(name="apply_requirement_registries")
def apply_requirement_registries_activity(
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = request or {}
    requirements_load = _load_requirements_load_module()
    cfg = dispatch.Config.from_env()
    return apply_requirement_registries(
        requirements_load.Substrate(), cfg=cfg, requirements_load=requirements_load
    )


ACTIVITIES = [apply_requirement_registries_activity]
