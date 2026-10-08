#!/usr/bin/env python3
"""GitHub Actions workflow-run health checker.

`build-mcp-hub.yml` failed on every run from 2026-09-03 to 2026-09-11 -- at least five
consecutive red builds across eight days, each deploying a broken image to the canonical node
before failing -- and nobody noticed until an unrelated investigation opened the run history by
hand. The pipeline reported failure correctly every time; a failed workflow run in this repository
was announced to nobody (`grep -rln 'if: failure()'` over `.github/workflows/` matches only
`backup-offsite-sync.yml` and `deploy.yml`; nothing in `scripts/` or `apps/factory-dispatcher/`
polled `gh run list` or the Actions API).

Adding an `if: failure()` step to `build-mcp-hub.yml` was considered and rejected: (1)
`.github/workflows/**` is `forbidden_always` in the task intake contract and is appended to every
bead's `forbidden_paths` at filing time regardless of declared scope, so no bead can ever carry a
workflow edit; (2) a per-workflow notification step has to be remembered for every workflow ever
added -- the same one-layer-up gap this checker exists to close. This module instead reads
`gh run list`'s own output, grouped by the `workflowName` GitHub itself reports, so a workflow
added tomorrow is covered without a code change here. See `.factory/design.md`.

The checking functions below take already-fetched `gh run list --json ...` output and return
`WorkflowRunFinding` objects. They run no subprocess and touch no network, so they can be driven
entirely from fixtures. `main()` is the thin, separately runnable wiring that shells out to `gh`
and posts through the dispatcher's declared alert inventory.

Reworked after the 2026-09-16 release gate on an earlier version of this module (PR #892,
dfc0d827): that version bounded its lookback with a single global `gh run list --limit N`
call, sorted across every workflow together. A low-frequency, path-filtered workflow's one red
run can be pushed out of that shared window entirely by other workflows running more often --
measured live at the shipped default (`limit=100`): 4 workflows visible across an 11h56m
window, zero occurrences of the workflow this bead was written about. Worse, the version that
shipped then read that absence as recovery and cleared the alert state, which is this bead's
own failure recurring through the front door: a build stays red and the checker reports green.
This version instead enumerates the workflows this repo currently defines via `gh workflow
list` and fetches each one's own most recent runs independently, so a workflow's coverage
depends only on itself, not on how often its siblings run; and it only ever clears an alert for
a workflow this run actually observed a completed run for -- absence now reads as unknown, not
recovered.

Branch-scoped since the follow-up bead raised as F2 at that same gate: every `gh run list` call
now passes `--branch <default branch>` (resolved via `gh repo view`, not a hardcoded `main`), so a
run on a feature branch or a pull request can never be read as the latest completed run that
decides a workflow's health. Before this, `lint.yml`'s `pull_request` trigger meant `Lint &
Validate`'s history was dominated by feature-branch runs -- a green PR run could be, and measured
live in this repository was, the latest completed run for a workflow whose most recent
default-branch run was red.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import process_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Same sys.path/reuse reasoning as cluster_health.py and failure_diagnosis.py: the dispatcher
# runs host-local against its own working copy of this monorepo, so this is source reuse of
# mcp-hub's alert-inventory/policy machinery, not a network call to the deployed mcp-hub service.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)

from tools import notify  # noqa: E402

import failure_diagnosis  # noqa: E402

#: Fields requested from `gh run list --json`. `workflowName` is GitHub's own name for the
#: workflow file (the yaml `name:` key) -- grouping by it is what lets a workflow added tomorrow
#: get covered with no code change here, rather than a hand-maintained list of workflow names.
GH_RUN_LIST_FIELDS = ("databaseId", "workflowName", "status", "conclusion", "updatedAt", "url")

#: Per-workflow, not global (see module docstring): each workflow enumerated via `gh workflow
#: list` gets its own `gh run list --workflow <name> --limit <this>` call, so this bounds how
#: many of one workflow's own runs we look through to find its latest *completed* one -- it does
#: not bound how many workflows are covered, which is however many `gh workflow list` returns.
GH_RUN_LIST_LIMIT_DEFAULT = 5
GH_RUN_LIST_LIMIT_ENV = "FACTORY_WORKFLOW_HEALTH_RUN_LIST_LIMIT"

#: How many workflow definitions `gh workflow list` itself is asked for. Comfortably above any
#: repo this dispatcher runs against today; not the knob that bounded the SRE-finding defect
#: (that was the per-workflow run count), so no per-repo tuning is expected here.
GH_WORKFLOW_LIST_LIMIT = 200

#: `conclusion` values (per `gh run list --json conclusion`) this checker treats as a red build.
#: Deliberately excludes `cancelled`/`skipped`/`neutral`/`action_required`/`stale`: those are not
#: "the pipeline reported failure" the way the SRE finding describes -- a cancelled run is not
#: evidence the code is broken.
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out"})

WORKFLOW_RUN_FAILING_ALERT_ID = "factory_dispatcher.workflow_run_failing"
WORKFLOW_RUN_CHECK_UNREACHABLE_ALERT_ID = "factory_dispatcher.workflow_run_check_unreachable"

#: Alert "kind" prefix for one failing workflow's finding -- distinguishes this checker's tracked
#: kinds from every other alert family sharing failure_diagnosis's state file when clearing.
FINDING_KIND_PREFIX = "workflow_run_failing:"


class MissingGhRunOutputError(RuntimeError):
    """`gh run list` itself could not be read; the check was not able to run at all."""


@dataclass(frozen=True)
class WorkflowRunFinding:
    workflow_name: str
    conclusion: str
    run_url: str
    updated_at: str
    run_id: str


# ---------------------------------------------------------------------------
# collect -- shells out to `gh`, or is driven from a fixture via `runner`
# ---------------------------------------------------------------------------


def run_list_limit_from_env(environ: Any = None) -> int:
    values = os.environ if environ is None else environ
    raw = values.get(GH_RUN_LIST_LIMIT_ENV)
    if not raw:
        return GH_RUN_LIST_LIMIT_DEFAULT
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(f"{GH_RUN_LIST_LIMIT_ENV} must be an integer") from exc
    if limit <= 0:
        raise ValueError(f"{GH_RUN_LIST_LIMIT_ENV} must be positive")
    return limit


def _gh_workflow_names(*, repo: str) -> list[str] | None:
    """Run `gh workflow list --repo <repo> --json name` and parse it, or None if unavailable.

    This is what makes coverage derive from what exists rather than a hand-maintained list:
    every workflow this call returns gets its own `gh run list --workflow` query below, so a
    workflow added tomorrow is covered with no code change here.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "workflow", "list",
                "--repo", repo,
                "--limit", str(GH_WORKFLOW_LIST_LIMIT),
                "--json", "name",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("gh workflow list --repo %s failed: %s", repo, exc)
        return None
    try:
        return [str(item["name"]) for item in json.loads(proc.stdout)]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("gh workflow list --repo %s returned unparseable JSON: %s", repo, exc)
        return None


def _gh_default_branch(*, repo: str) -> str | None:
    """Run `gh repo view <repo> --json defaultBranchRef` and return its name, or None if
    unavailable.

    This is what keeps the health signal scoped to the branch that matters without a hardcoded
    `main`: a repo whose default branch is named something else -- or that renames it later -- is
    still scoped correctly with no code change here, the same "derived from what exists" reasoning
    `_gh_workflow_names` already applies to workflow enumeration.
    """
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "defaultBranchRef"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("gh repo view %s failed: %s", repo, exc)
        return None
    try:
        name = json.loads(proc.stdout)["defaultBranchRef"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("gh repo view %s returned unparseable JSON: %s", repo, exc)
        return None
    return str(name) if name else None


def _gh_run_list_json(
    *, repo: str, workflow: str, limit: int, branch: str
) -> list[dict[str, Any]] | None:
    """Run `gh run list --repo <repo> --workflow <workflow> --branch <branch> --json ...` and
    parse it, or None if unavailable.

    `--branch` is mandatory here, not an optional filter: raised as F2 at the PR #895 gate, a
    `gh run list` with no branch constraint returns runs across every branch, so a workflow
    carrying a `pull_request` trigger (`lint.yml` is one) can have its latest completed run be a
    green PR run while its most recent default-branch run is still red. Requiring `branch` on this
    signature is what makes that mistake a `TypeError` at every call site instead of a silent
    cross-branch leak.

    Unavailable (gh missing, unauthenticated, GitHub unreachable) is logged and treated as "this
    check did not run", never as "this check found nothing" -- see `collect_workflow_runs_snapshot`.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "run", "list",
                "--repo", repo,
                "--workflow", workflow,
                "--branch", branch,
                "--limit", str(limit),
                "--json", ",".join(GH_RUN_LIST_FIELDS),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("gh run list --repo %s --workflow %s failed: %s", repo, workflow, exc)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "gh run list --repo %s --workflow %s returned unparseable JSON: %s",
            repo, workflow, exc,
        )
        return None


def collect_workflow_runs_snapshot(
    repo: str,
    *,
    limit: int | None = None,
    list_workflows: Any = _gh_workflow_names,
    runner: Any = _gh_run_list_json,
    default_branch: Any = _gh_default_branch,
) -> list[dict[str, Any]]:
    """Fetch this repo's currently-defined workflows via `list_workflows`, then each one's own
    recent runs on the repo's default branch via `runner` -- bounding lookback per workflow rather
    than by one global count shared across all of them (see module docstring for why: a
    low-frequency workflow's one red run must never be pushed out of the window by other workflows
    running more often), and scoping every lookup to `default_branch`'s answer so a run on a
    feature branch or a pull request can never be read as the workflow's own health signal.

    Raises `MissingGhRunOutputError` when any of the three `gh` calls (workflow enumeration,
    default-branch lookup, or a per-workflow run list) produced no parseable output at all -- the
    signature of `gh` itself being unreachable (binary missing, unauthenticated, GitHub down), as
    opposed to a successful call that legitimately found no workflows or no runs (a brand new
    repository), which returns `[]` and is not an error. A checker that cannot distinguish "no
    failures" from "could not look" reproduces the defect it exists to close -- and that applies to
    the default branch too: querying every branch because the default couldn't be determined would
    silently reopen the F2 defect this scoping exists to close.
    """
    names = list_workflows(repo=repo)
    if names is None:
        raise MissingGhRunOutputError(
            f"gh produced no output for `gh workflow list --repo {repo}`. gh may be missing, "
            "unauthenticated, or GitHub Actions may be unreachable."
        )
    per_workflow_limit = limit if limit is not None else run_list_limit_from_env()
    runs: list[dict[str, Any]] = []
    if not names:
        return runs
    branch = default_branch(repo=repo)
    if not branch:
        raise MissingGhRunOutputError(
            f"gh produced no usable output for `gh repo view {repo} --json defaultBranchRef`. gh "
            "may be missing, unauthenticated, or GitHub may be unreachable, and the health signal "
            "must not fall back to querying every branch."
        )
    for name in names:
        result = runner(repo=repo, workflow=name, limit=per_workflow_limit, branch=branch)
        if result is None:
            raise MissingGhRunOutputError(
                f"gh produced no output for `gh run list --repo {repo} --workflow {name} "
                f"--branch {branch}`. gh may be missing, unauthenticated, or GitHub Actions may "
                "be unreachable."
            )
        runs.extend(result)
    return runs


# ---------------------------------------------------------------------------
# checking -- pure functions over already-fetched `gh run list` output
# ---------------------------------------------------------------------------


def _latest_completed_run_per_workflow(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each workflow's most recently *completed* run, by `updatedAt`.

    In-progress/queued runs are ignored when picking "the latest" -- a workflow mid-run must not
    read as though its still-unknown outcome were a finding either way.
    """
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if str(run.get("status") or "").lower() != "completed":
            continue
        name = str(run.get("workflowName") or "")
        if not name:
            continue
        updated_at = str(run.get("updatedAt") or "")
        current = latest.get(name)
        if current is None or updated_at > str(current.get("updatedAt") or ""):
            latest[name] = run
    return latest


def failing_workflow_findings(runs: list[dict[str, Any]]) -> list[WorkflowRunFinding]:
    """Every workflow whose most recently completed run concluded in `FAILING_CONCLUSIONS`."""
    findings: list[WorkflowRunFinding] = []
    for name, run in sorted(_latest_completed_run_per_workflow(runs).items()):
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion not in FAILING_CONCLUSIONS:
            continue
        findings.append(
            WorkflowRunFinding(
                workflow_name=name,
                conclusion=conclusion,
                run_url=str(run.get("url") or ""),
                updated_at=str(run.get("updatedAt") or ""),
                run_id=str(run.get("databaseId") or ""),
            )
        )
    return findings


#: Conclusions that are POSITIVE EVIDENCE a workflow recovered. Deliberately just `success`:
#: clearing an alert is an assertion that the thing is fixed, and only a green run asserts that.
RECOVERED_CONCLUSIONS = frozenset({"success"})


def observed_workflow_names(runs: list[dict[str, Any]]) -> set[str]:
    """Every workflow this snapshot saw finish GREEN -- the only positive evidence of recovery.

    `notify_findings` uses this to bound clearing, so membership here means "this run proved the
    workflow is healthy", never merely "this run looked at it".

    NARROWED 2026-09-16 AT THE PR #895 GATE (F1). An earlier version returned every workflow with
    a *completed* run regardless of conclusion, which made a CANCELLED latest run read as
    recovery: `FAILING_CONCLUSIONS` excludes `cancelled` (rightly -- a cancelled run is not
    evidence the code is broken), so such a workflow was observed-and-not-failing, and its alert
    state was deleted while the build was still red. That is not hypothetical in this repository:
    across the last 300 runs there were 284 success, 14 cancelled and 2 failure, and ALL sixteen
    cancelled-or-failed runs belonged to `Lint & Validate` and `Secret Scan` -- the two workflows
    carrying `concurrency: cancel-in-progress`. So the workflow that actually goes red is also the
    one most often superseded, and a red main followed by a cancelled run would have silently
    cleared its own alert.

    The rule this restores is PRIN-015's: an undecided outcome is UNKNOWN, not healthy. A
    cancelled, skipped, neutral, action_required or stale latest run now leaves the alert standing,
    exactly as an absent one does.
    """
    return {
        name
        for name, run in _latest_completed_run_per_workflow(runs).items()
        if str(run.get("conclusion") or "").lower() in RECOVERED_CONCLUSIONS
    }


# ---------------------------------------------------------------------------
# alerting -- declared ALERT_INVENTORY entries, dispatcher's own alert-policy state
# ---------------------------------------------------------------------------


def _finding_kind(workflow_name: str) -> str:
    return f"{FINDING_KIND_PREFIX}{workflow_name}"


def _finding_fingerprint(finding: WorkflowRunFinding) -> str:
    # Deliberately excludes run_id/updated_at/run_url: a workflow rerunning on every push would
    # otherwise mint a "new" fingerprint every single run even though the underlying failure is
    # identical, defeating cross-run dedup -- the same reasoning as
    # cluster_health._job_failure_category excluding the CronJob's Job name/message. A *different*
    # failure category (e.g. timed_out replacing failure) changes the fingerprint and re-alerts
    # immediately, independent of the re-alert interval.
    return notify.alert_content_fingerprint(f"{finding.workflow_name}:{finding.conclusion}")


async def _announce_finding_async(finding: WorkflowRunFinding, policy: "notify.AlertPolicy") -> bool:
    content = (
        f"{finding.workflow_name} is failing: its most recent completed run concluded "
        f"{finding.conclusion} ({finding.run_url})."
    )
    return await notify.send_alert(
        policy,
        WORKFLOW_RUN_FAILING_ALERT_ID,
        _finding_fingerprint(finding),
        content,
        template_values={
            "workflow_name": finding.workflow_name,
            "run_url": finding.run_url,
        },
        re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_finding(
    finding: WorkflowRunFinding, *, policy: "notify.AlertPolicy | None" = None
) -> bool:
    """Raise the declared alert for one failing workflow.

    Best-effort, matching every other declared-alert call site in this app
    (`stranded_alerts.py`, `activities/deployed_revision_drift.py`): a bad webhook or a corrupt
    local alert-state file must never turn a read-only detection pass into a failed check.
    """
    try:
        return asyncio.run(
            _announce_finding_async(finding, policy or failure_diagnosis.dev_task_alert_policy())
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the checker.
        logger.exception(
            "could not announce workflow run failing for %s; detection is unaffected",
            finding.workflow_name,
        )
        return False


def notify_findings(
    findings: list[WorkflowRunFinding],
    *,
    observed_workflow_names: "set[str] | frozenset[str]",
    policy: "notify.AlertPolicy | None" = None,
) -> list[WorkflowRunFinding]:
    """Post the declared alert for every currently-failing workflow, and clear what recovered.

    `observed_workflow_names` is required, not optional: clearing must only ever consider a
    workflow this run actually observed a completed run for (see `observed_workflow_names()`
    above). A workflow absent from it -- out of this run's per-workflow lookback, still
    in-progress, or simply not returned by `gh` -- is unknown, not recovered, and its prior
    alert state (if any) must survive untouched. This is the fix for the release gate's F1: an
    earlier version of this function cleared every kind not in the *failing* set, so a
    workflow that scrolled out of a shared, count-bounded lookback window read as "recovered"
    and lost its alert state while still actually red.

    Clearing runs first, against this run's own active set, so a workflow that resolved and later
    recurs with byte-identical content (the same failure category) posts again immediately instead
    of inheriting a suppression window recorded for the earlier, now-resolved episode -- the
    transition-must-clear requirement this checker exists to satisfy (see
    `failure_diagnosis.clear_stale_dev_task_alert_kinds`).
    """
    active_policy = policy or failure_diagnosis.dev_task_alert_policy()
    try:
        failure_diagnosis.clear_stale_dev_task_alert_kinds(
            FINDING_KIND_PREFIX,
            {_finding_kind(f.workflow_name) for f in findings},
            observed_kinds={_finding_kind(name) for name in observed_workflow_names},
        )
    except Exception:  # noqa: BLE001 - clearing is best-effort, like every alert path here.
        logger.exception("workflow run health: could not clear resolved alert state")
    return [finding for finding in findings if announce_finding(finding, policy=active_policy)]


def announce_unreachable(
    error: "MissingGhRunOutputError", *, policy: "notify.AlertPolicy | None" = None
) -> bool:
    """Raise the declared alert when the check could not reach `gh`/GitHub at all (PRIN-015)."""
    content = f"the workflow run health check could not reach GitHub Actions: {error}"
    try:
        return asyncio.run(
            notify.send_alert(
                policy or failure_diagnosis.dev_task_alert_policy(),
                WORKFLOW_RUN_CHECK_UNREACHABLE_ALERT_ID,
                notify.alert_content_fingerprint(str(error)),
                content,
                template_values={"error": str(error)},
                re_alert_interval_hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS,
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the checker.
        logger.exception("could not announce workflow run health check unreachable (%s)", error)
        return False


# ---------------------------------------------------------------------------
# main -- separately runnable CLI wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    repo = os.environ.get("FACTORY_REPO")
    if not repo:
        print("FACTORY_REPO is not set; cannot determine which repository to check.", file=sys.stderr)
        return 2

    try:
        runs = collect_workflow_runs_snapshot(repo)
    except MissingGhRunOutputError as exc:
        announce_unreachable(exc)
        print(f"workflow run health check could not run: {exc}", file=sys.stderr)
        return 2

    findings = failing_workflow_findings(runs)
    for finding in findings:
        print(f"[{finding.workflow_name}] {finding.conclusion} ({finding.run_url})")

    notify_findings(findings, observed_workflow_names=observed_workflow_names(runs))

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
