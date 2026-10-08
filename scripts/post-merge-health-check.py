#!/usr/bin/env python3
"""Per-merge tripwire: after each merge, ask whether main is still green.

OBSERVED 2026-09-17: an attended session squash-merged 21 PRs in dependency order. Every one
carried a recorded Release-gate verdict, green CI on its own branch, and a CLEAN `git
merge-tree` result against every other open PR. main went RED anyway -- a SET-level collision
(several PRs each widened a tree-wide count the same way) that no pairwise conflict check and
no per-PR CI run can see, because neither ever evaluates the merged result. The window was about
forty minutes and was found by a human reading a CI list, not by anything that announces itself.

`apps/factory-dispatcher/workflow_run_health.py` already detects exactly this shape of failure
(a workflow's most recently completed run on the default branch concluding red) and already
posts through the dispatcher's declared alert inventory -- but only on the 15-minute
`ClusterHealthWorkflow` schedule (see `apps/factory-dispatcher/activities/workflow_run_health.py`).
Fifteen minutes -- or, in a fast attended batch, however many further merges land before the next
tick -- is "only at the end" relative to a 40-minute, 21-PR batch. This module adds no new
detection logic of its own: it is a thin per-merge trigger over that module's own already-tested
`collect_workflow_runs_snapshot` / `failing_workflow_findings` / `observed_workflow_names` /
`notify_findings` / `announce_unreachable`, so the scheduled check and this merge-path check can
never independently drift on what counts as a finding (the same "thin driver, no re-logic" idiom
`activities/workflow_run_health.py` itself documents). It reuses that module's own declared alert
ids (`WORKFLOW_RUN_FAILING_ALERT_ID`, `WORKFLOW_RUN_CHECK_UNREACHABLE_ALERT_ID`) rather than
minting new ones: an unregistered alert id is a defect this repository has shipped twice, and the
surest way to never ship it a third time is to never declare a third id.

THREE-STATE, NOT TWO. `failing_workflow_findings`/`observed_workflow_names` already distinguish "a
workflow's latest completed run failed" from "a workflow's latest completed run succeeded" --  but
neither says anything about a workflow whose only recent runs are still queued/in-progress, or
that this checker's own lookback window returned nothing recent for at all. Both of those are
"not yet measured", not "healthy", and reporting them as green is exactly the failure mode this
bead exists to close (see `classify_main_health`). Announcing is reserved for the RED state: an
"unmeasured" reading is the ordinary state of a workflow whose CI simply has not finished yet
(the overwhelmingly common case in the few seconds after any merge), and paging on every merge for
a condition that resolves itself within minutes would drown the one alert that matters under noise
that doesn't. `.factory/design.md` records this tradeoff and the alternative (blocking each merge
until CI completes) that was rejected because it would turn "notice" into a de facto merge queue.

Every function below that talks to `gh` is one already defined and tested in
`workflow_run_health.py`; nothing here shells out on its own. `main()` is the thin, separately
runnable CLI wiring that `scripts/merge-pr.sh` and `scripts/merge-pr.py` both call, best-effort,
right after a successful merge -- see either file's own comment at the call site for why a failure
here must never turn a successful merge into a reported one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# Same sys.path/reuse idiom as scripts/gate-verify.py and scripts/factory-redeploy.py: this is
# source reuse of the dispatcher's own already-tested checker and alert-inventory machinery, not a
# network call to a deployed service.
_DISPATCHER_DIR = str(REPO_ROOT / "apps" / "factory-dispatcher")
if _DISPATCHER_DIR not in sys.path:
    sys.path.insert(0, _DISPATCHER_DIR)

GREEN = "green"
RED = "red"
UNMEASURED = "unmeasured"
UNREACHABLE = "unreachable"

EXIT_BY_STATUS = {GREEN: 0, RED: 1, UNREACHABLE: 2, UNMEASURED: 3}

# GATE FINDING 1 (#924): SAY WHAT WAS MEASURED, NOT WHAT THE OPERATOR HOPES WAS MEASURED.
#
# This check is BRANCH-scoped, not COMMIT-scoped: `workflow_run_health.GH_RUN_LIST_FIELDS` does
# not request `headSha`, so `classify_main_health` reads the latest COMPLETED run per workflow on
# main and cannot tell which commit produced it. Seconds after a merge -- the only moment this
# code ever runs -- the merge's own runs are still queued or in progress and the latest completed
# runs belong to the PREVIOUS commit. The read is then truthfully "nothing known-red on main",
# and a bare "GREEN" printed right under someone's merge reads as "your merge is fine", which is
# a claim nothing here established.
#
# The label is qualified rather than the classifier changed, deliberately, for two reasons.
# (1) Downgrading to UNMEASURED whenever no completed run is newer than the merge would make the
#     check return UNMEASURED essentially always and RED essentially never, which destroys AC-3 --
#     the whole point is that an operator mid-batch learns main went red WITHOUT waiting.
# (2) Commit-scoping needs `headSha` in GH_RUN_LIST_FIELDS, which lives under
#     apps/factory-dispatcher/ -- a path this bead's declared scope FORBIDS.
# PRIN-015: state the bound of what was observed.
GREEN_LINE = "GREEN (last completed runs on main; this merge's own runs have not completed)"


def classify_main_health(
    runs: Sequence[dict[str, Any]],
    workflow_names: Sequence[str] | None,
) -> tuple[str, list[Any]]:
    """Tri-state read of ``runs`` (the shape ``workflow_run_health.collect_workflow_runs_snapshot``
    returns): ``"red"``, ``"green"``, or ``"unmeasured"`` -- never a plain boolean, because a
    plain boolean has nowhere to put "not yet known" except inside one of the other two.

    ``workflow_names`` is every workflow this repo currently defines (``gh workflow list``'s own
    answer, via ``workflow_run_health._gh_workflow_names``), independent of ``runs``. It is what
    lets a workflow with NO runs in ``runs`` at all -- absent from the lookback window entirely,
    not merely mid-run -- still read as unmeasured instead of silently not counting against
    green: a workflow present only in ``runs`` (queued/in-progress, no completed entry within the
    lookback) and a workflow entirely absent from ``runs`` are the same fact for this purpose, and
    both must fail the "confirmed green" bar the same way. Pass ``None`` when that enumeration is
    unavailable; the read then degrades to judging only what ``runs`` itself mentions, which is
    the same information ``workflow_run_health.py``'s own scheduled check already accepts as
    sufficient.

    RED wins over everything: even one workflow whose latest completed run failed makes main red,
    regardless of how many others are confirmed green.
    """
    import workflow_run_health

    runs = list(runs)
    findings = workflow_run_health.failing_workflow_findings(runs)
    if findings:
        return RED, findings

    observed = workflow_run_health.observed_workflow_names(runs)
    mentioned = {str(r.get("workflowName") or "") for r in runs if r.get("workflowName")}
    known = set(workflow_names) if workflow_names is not None else mentioned

    if known - observed:
        # Something this repo defines (or that appeared in the lookback window at all) has not
        # been confirmed green -- still queued/in-progress, or outside the lookback entirely.
        return UNMEASURED, []
    if observed:
        return GREEN, []
    return UNMEASURED, []


def check_and_announce(
    repo: str,
    *,
    collect: Callable[[str], list[dict[str, Any]]] | None = None,
    list_workflow_names: Callable[..., list[str] | None] | None = None,
    notify_findings: Callable[..., list[Any]] | None = None,
    announce_unreachable: Callable[..., bool] | None = None,
    policy: "Any | None" = None,
) -> dict[str, Any]:
    """Run the check end-to-end against ``repo`` and announce a RED finding, if any.

    Every collaborator defaults to the real ``workflow_run_health`` function of the same purpose,
    resolved lazily so tests can inject fixtures without touching `gh`, `sys.path`, or the
    dispatcher's real alert-state file. A ``policy`` is passed straight through to
    ``notify_findings``/``announce_unreachable``, which themselves default to the dispatcher's own
    declared alert policy (``failure_diagnosis.dev_task_alert_policy()``) when ``None``.
    """
    import workflow_run_health

    collect = collect or workflow_run_health.collect_workflow_runs_snapshot
    list_workflow_names = list_workflow_names or workflow_run_health._gh_workflow_names
    notify_findings_fn = notify_findings or workflow_run_health.notify_findings
    announce_unreachable_fn = announce_unreachable or workflow_run_health.announce_unreachable

    try:
        runs = collect(repo)
    except workflow_run_health.MissingGhRunOutputError as exc:
        announce_unreachable_fn(exc, policy=policy)
        return {"status": UNREACHABLE, "error": str(exc), "failing_workflows": []}

    workflow_names = list_workflow_names(repo=repo)
    status, findings = classify_main_health(runs, workflow_names)

    if status == RED:
        observed = workflow_run_health.observed_workflow_names(runs)
        notify_findings_fn(findings, observed_workflow_names=observed, policy=policy)

    return {
        "status": status,
        "failing_workflows": sorted({f.workflow_name for f in findings}),
    }


def resolve_repo(explicit: str | None = None, *, environ: Any = None) -> str | None:
    """``explicit``, else ``$FACTORY_REPO``, else ``gh repo view``'s own answer -- the same
    fallback order ``workflow_run_health.py``'s ``main()`` uses for the first two, extended with
    a `gh` lookup so this script also works from a plain checkout with no env var set, the way
    `scripts/merge-pr.sh` and `scripts/merge-pr.py` are already run.
    """
    if explicit:
        return explicit
    values = os.environ if environ is None else environ
    env_repo = values.get("FACTORY_REPO")
    if env_repo:
        return env_repo
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = proc.stdout.strip()
    return name or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=None,
        help="OWNER/REPO to check; defaults to $FACTORY_REPO, else `gh repo view`",
    )
    args = parser.parse_args(argv)

    repo = resolve_repo(args.repo)
    if not repo:
        print(
            "post-merge health check: could not determine OWNER/REPO "
            "(set FACTORY_REPO or run where `gh repo view` resolves one); skipping.",
            file=sys.stderr,
        )
        return EXIT_BY_STATUS[UNREACHABLE]

    result = check_and_announce(repo)
    status = result["status"]
    if status == RED:
        print(f"[main-health] RED: {', '.join(result['failing_workflows'])}", file=sys.stderr)
    elif status == UNREACHABLE:
        print(f"[main-health] UNREACHABLE: {result.get('error')}", file=sys.stderr)
    elif status == UNMEASURED:
        print("[main-health] UNMEASURED: not yet confirmed green", file=sys.stderr)
    else:
        print(f"[main-health] {GREEN_LINE}", file=sys.stderr)
    return EXIT_BY_STATUS.get(status, EXIT_BY_STATUS[UNREACHABLE])


if __name__ == "__main__":
    raise SystemExit(main())
