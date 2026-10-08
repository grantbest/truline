"""Merge-by-verdict, the credentialed half (R26.09/O-2, this bead's own record).

The gateway (``apps/mcp-hub/src/tools/factory_merge.py``) holds no GitHub
credential and starts ``MergeOnVerdictWorkflow`` without ever reading GitHub
or merging. This module is that workflow's one activity, on the worker that
already holds ``gh auth``: it fetches the PR's body/comments/head with ``gh``,
resolves the bead id and release-gate verdict with ``scripts/gate_markers.py``
(the same recognizer ``scripts/merge-pr.sh`` itself consults, never
reimplemented), and -- only when every condition below holds -- runs
``scripts/merge-pr.sh <number>``. See .factory/design.md for the full design
record, including why this is deliberately STRICTER than ``merge-pr.sh``
itself about a missing revision line.

Two independent controls gate this end to end (R26.09/O-2's dark-until-
granted shape): the ``factory.merge`` scope, checked at the gateway before
this ever runs, and ``FACTORY_MERGE_CAPABILITY_ENABLED`` here -- checked
FIRST, before any ``gh`` call, so a disabled capability never reads GitHub at
all.

Every request that resolves a ``Dispatched-bead id`` gets a ``dev.note``
(``kind: status``) recording the requesting identity, the scope, the PR head,
the verdict found, and the disposition -- best-effort, the same way
``activities/dispatch_steps.py``'s ``_lift_design_note`` treats its own note
write: a rejected write is a lost comment, never a failed merge or a failed
refusal. When no bead id resolves, there is nothing to attach a note to; the
disposition still lands in the return value, which becomes the workflow's
queryable result either way.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import dispatch  # noqa: E402
import gate_markers  # noqa: E402
from substrate import default_store  # noqa: E402

logger = logging.getLogger(__name__)

CREATED_BY = "factory-dispatcher/merge-on-verdict"
CAPABILITY_ENV = "FACTORY_MERGE_CAPABILITY_ENABLED"
MERGE_SCRIPT = _REPO_ROOT / "scripts" / "merge-pr.sh"
MERGE_SCRIPT_TIMEOUT_S = 900.0


def capability_enabled() -> bool:
    """The second, independent control (see the module docstring). Absent or
    anything other than the literal ``'true'`` is closed -- the same
    fail-closed convention ``tools/schedules.py``'s ``schedules_enabled``
    uses for its own environment gate."""
    return os.environ.get(CAPABILITY_ENV, "").strip().lower() == "true"


def gh_pr_view(number: int, cfg: "dispatch.Config") -> dict[str, Any]:
    proc = dispatch.run(
        [
            "gh", "pr", "view", str(number),
            "--repo", cfg.repo,
            "--json", "body,comments,headRefOid,headRefName",
        ],
        cwd=cfg.repo_root,
        timeout=60,
    )
    return json.loads(proc.stdout or "{}")


def gh_open_prs_with_base(base_branch: str, cfg: "dispatch.Config") -> list[dict[str, Any]]:
    proc = dispatch.run(
        [
            "gh", "pr", "list",
            "--repo", cfg.repo,
            "--state", "open",
            "--base", base_branch,
            "--json", "number",
        ],
        cwd=cfg.repo_root,
        timeout=60,
    )
    return json.loads(proc.stdout or "[]")


def run_merge_script(number: int, cfg: "dispatch.Config") -> subprocess.CompletedProcess:
    # merge-pr.sh runs scripts/post-merge-health-check.py, whose RED-alert path
    # posts through notify.post_discord, which reads DISCORD_WEBHOOK_URL from
    # the environment (dev.finding a0166920). No other credential reaches this
    # child: gh authenticates from its own config/keychain.
    return dispatch.run(
        [str(MERGE_SCRIPT), str(number)],
        cwd=cfg.repo_root,
        timeout=MERGE_SCRIPT_TIMEOUT_S,
        check=False,
        needs=("DISCORD_WEBHOOK_URL",),
    )


def _comment_bodies(comments: Any) -> list[str]:
    bodies: list[str] = []
    for comment in comments or []:
        if isinstance(comment, dict):
            bodies.append(str(comment.get("body") or ""))
        else:
            bodies.append(str(comment or ""))
    return bodies


def _note_body(
    *,
    number: int,
    requested_by: str,
    scope: str,
    head: str | None,
    verdict: str | None,
    disposition: str,
    reason: str | None,
) -> str:
    lines = [
        f"Merge-by-verdict request for PR #{number}.",
        f"Requested by: {requested_by} (scope: {scope}).",
        f"PR head: {head or 'unknown'}.",
        f"Release-gate verdict found: {verdict or 'none'}.",
        f"Disposition: {disposition}" + (f" ({reason})" if reason else "."),
    ]
    return "\n".join(lines)


def _record_note(
    store: Any,
    *,
    bead_id: str,
    number: int,
    requested_by: str,
    scope: str,
    head: str | None,
    verdict: str | None,
    disposition: str,
    reason: str | None,
) -> None:
    body = _note_body(
        number=number, requested_by=requested_by, scope=scope, head=head,
        verdict=verdict, disposition=disposition, reason=reason,
    )
    try:
        store.add_note(
            bead_id,
            "status",
            body,
            CREATED_BY,
            provenance=dispatch.provenance_for_operator_action(bead_id, "merge-on-verdict"),
        )
    except Exception:  # noqa: BLE001 -- a rejected write is a lost comment, never a failed run
        logger.exception(
            "merge-on-verdict: failed to record dev.note on bead %s (PR #%s) -- "
            "the disposition itself is unaffected.",
            bead_id, number,
        )


def run_merge_on_verdict(
    request: dict[str, Any],
    *,
    cfg: "dispatch.Config | None" = None,
    store: Any = None,
    gh_view: Callable[[int, "dispatch.Config"], dict[str, Any]] = gh_pr_view,
    gh_open_prs_with_base_fn: Callable[[str, "dispatch.Config"], list[dict[str, Any]]] = gh_open_prs_with_base,
    run_merge_script_fn: Callable[[int, "dispatch.Config"], subprocess.CompletedProcess] = run_merge_script,
    capability_enabled_fn: Callable[[], bool] = capability_enabled,
) -> dict[str, Any]:
    """Decide, and if warranted execute, one merge-by-verdict request.

    Returns a plain, JSON-serializable dict -- this crosses the Temporal
    activity boundary and becomes the workflow's own recorded disposition.
    """
    number = int(request["pr_number"])
    requested_by = str(request.get("requested_by") or "unknown")
    scope = str(request.get("scope") or "")
    cfg = cfg or dispatch.Config.from_env()
    store = store if store is not None else default_store()

    def _result(disposition: str, reason: str | None, *, verdict: str | None, head: str | None, bead_id: str | None) -> dict[str, Any]:
        return {
            "pr": number,
            "requested_by": requested_by,
            "scope": scope,
            "verdict": verdict,
            "head": head,
            "bead_id": bead_id,
            "disposition": disposition,
            "reason": reason,
        }

    if not capability_enabled_fn():
        return _result("refused:capability-disabled", "capability-disabled", verdict=None, head=None, bead_id=None)

    try:
        payload = gh_view(number, cfg)
    except dispatch.DispatchError as exc:
        reason = f"gh-error:{str(exc)[:200]}"
        return _result(f"refused:{reason}", reason, verdict=None, head=None, bead_id=None)

    body = str(payload.get("body") or "")
    comments = _comment_bodies(payload.get("comments"))
    head = payload.get("headRefOid")
    head_ref_name = payload.get("headRefName")

    bead_id = gate_markers.find_bead_id(body)
    verdict = gate_markers.find_release_gate_verdict(body, comments)
    record = gate_markers.find_release_gate_verdict_record(body, comments)

    def _refuse(reason: str) -> dict[str, Any]:
        result = _result(f"refused:{reason}", reason, verdict=verdict, head=head, bead_id=bead_id)
        if bead_id:
            _record_note(
                store, bead_id=bead_id, number=number, requested_by=requested_by,
                scope=scope, head=head, verdict=verdict,
                disposition=result["disposition"], reason=reason,
            )
        return result

    if verdict is None:
        return _refuse("no-verdict")
    if verdict == "DO-NOT-MERGE":
        return _refuse("verdict-do-not-merge")
    if verdict == "MERGE-WITH-CHANGES":
        return _refuse("verdict-merge-with-changes")

    # verdict == "MERGE" from here: intentionally STRICTER than merge-pr.sh
    # itself, which treats a missing revision line as the pre-convention
    # compatibility case and proceeds -- an unattended-triggered merge should
    # not inherit that leniency (see .factory/design.md).
    if not record.revision:
        return _refuse("no-revision")
    if not gate_markers.revisions_match(record.revision, head or ""):
        return _refuse("revision-mismatch")
    if not bead_id:
        # "attended PRs merge from the CLI" -- CLAUDE.md's existing rule;
        # this activity has no bead to note the refusal against either.
        return _refuse("no-bead")

    try:
        open_with_base = gh_open_prs_with_base_fn(head_ref_name or "", cfg)
    except dispatch.DispatchError as exc:
        return _refuse(f"gh-error:{str(exc)[:200]}")
    if open_with_base:
        return _refuse("stacked-base")

    proc = run_merge_script_fn(number, cfg)
    if proc.returncode != 0:
        reason = f"merge-script-failed:{proc.returncode}:{(proc.stderr or proc.stdout or '').strip()[:300]}"
        return _refuse(reason)

    result = _result("merged", None, verdict=verdict, head=head, bead_id=bead_id)
    _record_note(
        store, bead_id=bead_id, number=number, requested_by=requested_by,
        scope=scope, head=head, verdict=verdict, disposition="merged", reason=None,
    )
    return result


@activity.defn(name="run_merge_on_verdict")
def run_merge_on_verdict_activity(request: dict[str, Any]) -> dict[str, Any]:
    return run_merge_on_verdict(request)


ACTIVITIES = [run_merge_on_verdict_activity]
