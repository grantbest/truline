"""A red build must not be announced to nobody.

`build-mcp-hub.yml` failed on every run from 2026-09-03 to 2026-09-11 -- at least five consecutive
red builds across eight days, each deploying a broken image to the canonical node before failing
-- and nobody noticed until an unrelated investigation opened the run history by hand. This module
fails against today's repo before this change lands: `workflow_run_health` does not exist yet.

tests/conftest.py's autouse `_no_test_writes_to_the_real_home` fixture fakes $HOME for every test
in this module, so the shared dispatcher alert-state file (`failure_diagnosis`'s, reused here) is
fresh and isolated per test with no extra plumbing needed.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failure_diagnosis  # noqa: E402
import workflow_run_health  # noqa: E402


def _run(
    workflow_name: str,
    *,
    conclusion: str = "success",
    status: str = "completed",
    updated_at: str = "2026-09-11T00:00:00Z",
    run_id: str = "1",
    url: str = "https://github.com/example/repo/actions/runs/1",
) -> dict:
    return {
        "databaseId": run_id,
        "workflowName": workflow_name,
        "status": status,
        "conclusion": conclusion,
        "updatedAt": updated_at,
        "url": url,
    }


class RecordingPolicy:
    def __init__(self, send_result: bool = True):
        self.sent: list[tuple[str, dict]] = []
        self._send_result = send_result

    async def send(self, kind, fingerprint, content, *, severity=None, re_alert_interval_hours=0.0, **kwargs):
        self.sent.append(
            (kind, {"fingerprint": fingerprint, "content": content, "re_alert_interval_hours": re_alert_interval_hours, **kwargs})
        )
        return self._send_result


class ExplodingPolicy:
    async def send(self, *args, **kwargs):
        raise RuntimeError("webhook exploded")


# ---------------------------------------------------------------------------
# derived from what gh actually reports -- no hand-maintained workflow list
# ---------------------------------------------------------------------------


def test_a_workflow_never_seen_before_is_still_detected_when_its_latest_run_failed():
    """A workflow added tomorrow must be covered with no code change here."""
    runs = [_run("a-workflow-nobody-wrote-yet", conclusion="failure")]

    findings = workflow_run_health.failing_workflow_findings(runs)

    assert [f.workflow_name for f in findings] == ["a-workflow-nobody-wrote-yet"]


def test_only_the_most_recently_completed_run_per_workflow_counts():
    runs = [
        _run("build-mcp-hub", conclusion="failure", updated_at="2026-09-03T00:00:00Z", run_id="1"),
        _run("build-mcp-hub", conclusion="success", updated_at="2026-09-11T00:00:00Z", run_id="5"),
    ]

    findings = workflow_run_health.failing_workflow_findings(runs)

    assert findings == []  # the older failure must not outrank the newer success


def test_in_progress_runs_are_ignored_when_finding_the_latest():
    runs = [
        _run("build-mcp-hub", conclusion="failure", status="completed", updated_at="2026-09-10T00:00:00Z"),
        _run("build-mcp-hub", conclusion="", status="in_progress", updated_at="2026-09-11T00:00:00Z"),
    ]

    findings = workflow_run_health.failing_workflow_findings(runs)

    assert len(findings) == 1
    assert findings[0].conclusion == "failure"


def test_cancelled_and_skipped_conclusions_do_not_count_as_failing():
    runs = [
        _run("release-status-check", conclusion="cancelled"),
        _run("doctrine-staleness", conclusion="skipped"),
        _run("ea-observation", conclusion="neutral"),
    ]

    findings = workflow_run_health.failing_workflow_findings(runs)

    assert findings == []


def test_the_build_mcp_hub_incident_is_detected_and_a_healthy_sibling_is_not():
    """The SRE finding, reproduced: build-mcp-hub red, backup-offsite-sync green."""
    runs = [
        _run("build-mcp-hub", conclusion="failure", updated_at="2026-09-11T00:00:00Z"),
        _run("backup-offsite-sync", conclusion="success", updated_at="2026-09-11T01:00:00Z"),
    ]

    findings = workflow_run_health.failing_workflow_findings(runs)

    assert [f.workflow_name for f in findings] == ["build-mcp-hub"]


# ---------------------------------------------------------------------------
# collect -- degrading honestly (PRIN-015): "no failures" vs "could not look"
# ---------------------------------------------------------------------------


def test_collect_raises_when_gh_workflow_list_produces_no_output_at_all():
    def broken_list_workflows(*, repo):
        return None

    try:
        workflow_run_health.collect_workflow_runs_snapshot(
            "example/repo", list_workflows=broken_list_workflows
        )
        raised = False
    except workflow_run_health.MissingGhRunOutputError:
        raised = True
    assert raised


def test_collect_raises_when_a_per_workflow_run_list_produces_no_output_at_all():
    """Bounding lookback per workflow (F1's lookback-strategy fix) must not weaken the honest
    degradation guarantee: any one workflow's `gh run list` failing to answer is still `gh`
    itself being unreachable, not "that workflow has no runs"."""

    def list_workflows(*, repo):
        return ["build-mcp-hub"]

    def broken_runner(*, repo, workflow, limit, branch):
        return None

    try:
        workflow_run_health.collect_workflow_runs_snapshot(
            "example/repo",
            list_workflows=list_workflows,
            runner=broken_runner,
            default_branch=lambda **_: "main",
        )
        raised = False
    except workflow_run_health.MissingGhRunOutputError:
        raised = True
    assert raised


def test_collect_raises_when_the_default_branch_cannot_be_determined():
    """Branch scoping must degrade the same honest way as workflow/run enumeration: if `gh repo
    view` can't answer, this must not silently fall back to querying every branch -- that would
    reopen the exact defect branch scoping exists to close."""

    def list_workflows(*, repo):
        return ["build-mcp-hub"]

    def broken_default_branch(*, repo):
        return None

    try:
        workflow_run_health.collect_workflow_runs_snapshot(
            "example/repo", list_workflows=list_workflows, default_branch=broken_default_branch
        )
        raised = False
    except workflow_run_health.MissingGhRunOutputError:
        raised = True
    assert raised


def test_collect_does_not_raise_on_a_repo_with_no_workflows():
    def empty_list_workflows(*, repo):
        return []

    runs = workflow_run_health.collect_workflow_runs_snapshot(
        "example/repo", list_workflows=empty_list_workflows
    )

    assert runs == []


def test_collect_queries_each_enumerated_workflow_independently_with_repo_and_limit():
    seen = []

    def list_workflows(*, repo):
        assert repo == "example/repo"
        return ["build-mcp-hub", "backup-offsite-sync"]

    def runner(*, repo, workflow, limit, branch):
        seen.append({"repo": repo, "workflow": workflow, "limit": limit, "branch": branch})
        return []

    workflow_run_health.collect_workflow_runs_snapshot(
        "example/repo",
        limit=42,
        list_workflows=list_workflows,
        runner=runner,
        default_branch=lambda **_: "main",
    )

    assert seen == [
        {"repo": "example/repo", "workflow": "build-mcp-hub", "limit": 42, "branch": "main"},
        {"repo": "example/repo", "workflow": "backup-offsite-sync", "limit": 42, "branch": "main"},
    ]


def test_collect_scopes_every_run_list_call_to_the_resolved_default_branch():
    """The acceptance criterion, pinned directly: `default_branch` need not answer `main` --
    whatever it reports is what every `runner` call is scoped to, so this is not a hardcoded
    literal."""
    seen_branches = []

    def list_workflows(*, repo):
        return ["build-mcp-hub"]

    def runner(*, repo, workflow, limit, branch):
        seen_branches.append(branch)
        return []

    workflow_run_health.collect_workflow_runs_snapshot(
        "example/repo",
        list_workflows=list_workflows,
        runner=runner,
        default_branch=lambda **_: "trunk",
    )

    assert seen_branches == ["trunk"]


def test_a_green_pr_run_does_not_mask_a_red_run_on_the_default_branch():
    """The measured scenario from the module docstring, reproduced directly: `Lint & Validate`
    goes red on main; a PR's lint run then goes green and updates *after* the red main run. An
    unscoped `gh run list` (no `--branch`) cannot tell that PR run apart from a main-branch run,
    so it becomes "the latest completed run" and the checker reads the workflow as healthy while
    main is still red -- OPS-103's own defect shape, arriving through a different door.
    """
    runs_by_branch = {
        "main": [
            _run("Lint & Validate", conclusion="failure", updated_at="2026-09-16T01:00:00Z", run_id="1"),
        ],
        "feature/some-pr": [
            _run("Lint & Validate", conclusion="success", updated_at="2026-09-16T05:00:00Z", run_id="2"),
        ],
    }

    def list_workflows(*, repo):
        return ["Lint & Validate"]

    def runner(*, repo, workflow, limit, branch=None):
        if branch is None:
            return [run for branch_runs in runs_by_branch.values() for run in branch_runs]
        return list(runs_by_branch.get(branch, []))

    runs = workflow_run_health.collect_workflow_runs_snapshot(
        "example/repo",
        list_workflows=list_workflows,
        runner=runner,
        default_branch=lambda **_: "main",
    )
    findings = workflow_run_health.failing_workflow_findings(runs)
    observed = workflow_run_health.observed_workflow_names(runs)

    assert [f.workflow_name for f in findings] == ["Lint & Validate"], (
        "the red main run must be found even though a later PR run went green"
    )
    assert "Lint & Validate" not in observed, (
        "a green PR run must not be read as recovery for the workflow's main-branch health"
    )


def test_a_low_frequency_workflows_failure_is_not_crowded_out_by_a_high_frequency_sibling():
    """The release gate's F1 lookback finding, reproduced directly: a single global run-count
    limit sorts every workflow's runs together, so a rarely-run workflow's one red run can be
    pushed out of the window entirely by a sibling that runs far more often. Querying each
    workflow independently must not have this problem, regardless of how many runs the other
    workflow contributes."""

    def list_workflows(*, repo):
        return ["frequent-workflow", "rare-workflow"]

    def runner(*, repo, workflow, limit, branch):
        if workflow == "frequent-workflow":
            return [
                _run(
                    "frequent-workflow",
                    conclusion="success",
                    updated_at=f"2026-09-{10 + i:02d}T00:00:00Z",
                )
                for i in range(limit)
            ]
        return [_run("rare-workflow", conclusion="failure", updated_at="2026-07-01T00:00:00Z")]

    runs = workflow_run_health.collect_workflow_runs_snapshot(
        "example/repo",
        limit=5,
        list_workflows=list_workflows,
        runner=runner,
        default_branch=lambda **_: "main",
    )
    findings = workflow_run_health.failing_workflow_findings(runs)

    assert [f.workflow_name for f in findings] == ["rare-workflow"]


# ---------------------------------------------------------------------------
# declared alerts -- ALERT_INVENTORY, not a bespoke send path
# ---------------------------------------------------------------------------


def _finding(workflow_name: str = "build-mcp-hub", conclusion: str = "failure") -> workflow_run_health.WorkflowRunFinding:
    return workflow_run_health.WorkflowRunFinding(
        workflow_name=workflow_name,
        conclusion=conclusion,
        run_url="https://github.com/example/repo/actions/runs/1",
        updated_at="2026-09-11T00:00:00Z",
        run_id="1",
    )


def test_announce_finding_raises_the_declared_alert():
    policy = RecordingPolicy()
    posted = workflow_run_health.announce_finding(_finding(), policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == "workflow_run_failing:build-mcp-hub"
    assert "build-mcp-hub" in payload["content"]
    assert payload["re_alert_interval_hours"] == failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_announce_finding_uses_the_declared_inventory_entry():
    definition = workflow_run_health.notify.get_alert_definition(
        workflow_run_health.WORKFLOW_RUN_FAILING_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_announce_finding_is_best_effort_and_never_raises():
    assert workflow_run_health.announce_finding(_finding(), policy=ExplodingPolicy()) is False


def test_workflow_run_health_announce_unreachable_raises_the_declared_alert():
    policy = RecordingPolicy()
    error = workflow_run_health.MissingGhRunOutputError("gh: command not found")

    posted = workflow_run_health.announce_unreachable(error, policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == "workflow_run_check_unreachable"
    assert "gh: command not found" in payload["content"]
    assert payload["re_alert_interval_hours"] == failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_workflow_run_health_announce_unreachable_uses_the_declared_inventory_entry():
    definition = workflow_run_health.notify.get_alert_definition(
        workflow_run_health.WORKFLOW_RUN_CHECK_UNREACHABLE_ALERT_ID
    )
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_workflow_run_health_announce_unreachable_is_best_effort_and_never_raises():
    error = workflow_run_health.MissingGhRunOutputError("boom")
    assert workflow_run_health.announce_unreachable(error, policy=ExplodingPolicy()) is False


def test_finding_fingerprint_excludes_run_id_and_timestamp():
    """A workflow rerunning on every push must dedup across runs of the identical failure."""
    first = workflow_run_health._finding_fingerprint(
        workflow_run_health.WorkflowRunFinding(
            workflow_name="build-mcp-hub", conclusion="failure",
            run_url="https://x/runs/1", updated_at="2026-09-03T00:00:00Z", run_id="1",
        )
    )
    second = workflow_run_health._finding_fingerprint(
        workflow_run_health.WorkflowRunFinding(
            workflow_name="build-mcp-hub", conclusion="failure",
            run_url="https://x/runs/9", updated_at="2026-09-11T00:00:00Z", run_id="9",
        )
    )
    assert first == second


def test_finding_fingerprint_changes_when_the_conclusion_category_changes():
    first = workflow_run_health._finding_fingerprint(_finding(conclusion="failure"))
    second = workflow_run_health._finding_fingerprint(_finding(conclusion="timed_out"))
    assert first != second


# ---------------------------------------------------------------------------
# the OPS-69 trap: a persistently red workflow must post once, then fall
# silent until the declared re-alert interval elapses -- reusing
# failure_diagnosis's own dedup/interval mechanism, not a new one.
# ---------------------------------------------------------------------------


def test_a_persistently_failing_workflow_posts_once_then_falls_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    finding = _finding()

    for _ in range(3):
        workflow_run_health.notify_findings(
            [finding], observed_workflow_names={finding.workflow_name}, policy=policy
        )

    assert fake_post.calls == 1


def test_after_the_declared_interval_a_still_failing_workflow_re_alerts(tmp_path, monkeypatch):
    state_path = tmp_path / "alert-state.json"
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(state_path))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    finding = _finding()

    workflow_run_health.notify_findings(
        [finding], observed_workflow_names={finding.workflow_name}, policy=policy
    )
    assert fake_post.calls == 1

    # Back-date the recorded post past the declared re-alert interval, simulating the same
    # workflow still red a day later.
    doc = json.loads(state_path.read_text())
    stale_time = datetime.now(timezone.utc) - timedelta(
        hours=failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS + 1
    )
    doc["workflow_run_failing:build-mcp-hub"]["last_posted_at"] = stale_time.isoformat()
    state_path.write_text(json.dumps(doc))

    workflow_run_health.notify_findings(
        [finding], observed_workflow_names={finding.workflow_name}, policy=policy
    )

    assert fake_post.calls == 2


def test_a_healthy_run_posts_nothing():
    policy = RecordingPolicy()

    gated = workflow_run_health.notify_findings(
        [], observed_workflow_names={"build-mcp-hub"}, policy=policy
    )

    assert gated == []
    assert policy.sent == []


# ---------------------------------------------------------------------------
# red -> green must clear the finding, so it cannot strand the dedup window
# ---------------------------------------------------------------------------


def test_a_workflow_that_recovers_and_later_recurs_posts_again_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    finding = _finding()

    first = workflow_run_health.notify_findings(
        [finding], observed_workflow_names={finding.workflow_name}, policy=policy
    )
    # A green run in between -- build-mcp-hub was actually observed this pass, and it passed.
    recovered = workflow_run_health.notify_findings(
        [], observed_workflow_names={finding.workflow_name}, policy=policy
    )
    recurred = workflow_run_health.notify_findings(
        [finding], observed_workflow_names={finding.workflow_name}, policy=policy
    )

    assert first == [finding]
    assert recovered == []
    assert recurred == [finding]
    assert fake_post.calls == 2


def test_a_cancelled_latest_run_keeps_its_alert_state(tmp_path, monkeypatch):
    """Release-gate F1 (#895): a cancelled latest run is not recovery.

    `FAILING_CONCLUSIONS` rightly excludes `cancelled` -- a cancelled run is not evidence the code
    is broken -- so before this narrowing such a workflow was observed-and-not-failing, i.e.
    recovered, and its alert state was deleted while the build was still red.

    Live, not hypothetical: across the last 300 runs of this repository there were 284 success,
    14 cancelled and 2 failure, and ALL sixteen cancelled-or-failed runs belonged to
    `Lint & Validate` and `Secret Scan` -- the two workflows carrying `cancel-in-progress`. The
    workflow that actually goes red is also the one most often superseded.
    """
    state_path = tmp_path / "alert-state.json"
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(state_path))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    # pass 1: the workflow is red, so its alert posts and its kind is tracked.
    workflow_run_health.notify_findings(
        [_finding(workflow_name="lint")], observed_workflow_names={"lint"}, policy=policy
    )
    assert fake_post.calls == 1

    # pass 2: the next run was superseded and CANCELLED. It is present in the snapshot but
    # nothing about it proves recovery, so observed_workflow_names must not include it.
    cancelled = {
        "workflowName": "lint",
        "status": "completed",
        "conclusion": "cancelled",
        "updatedAt": "2026-09-16T02:00:00Z",
    }
    observed = workflow_run_health.observed_workflow_names([cancelled])
    assert observed == set(), "a cancelled run is not positive evidence of recovery"

    workflow_run_health.notify_findings(
        workflow_run_health.failing_workflow_findings([cancelled]),
        observed_workflow_names=observed,
        policy=policy,
    )

    doc = json.loads(state_path.read_text())
    assert "workflow_run_failing:lint" in doc, (
        "a cancelled latest run must not clear the alert -- it is presence with an undecided "
        "outcome, which is unknown, not healthy"
    )


def test_a_workflow_absent_from_the_snapshot_keeps_its_alert_state(tmp_path, monkeypatch):
    """Release-gate F1: absence from this run's observed set must never read as recovery.

    Shown failing against the pre-fix implementation (git-stashed and re-run by hand against
    this exact scenario, outside this test file): calling the old two-argument
    `notify_findings(findings, policy=policy)` with an empty `findings` list on pass 2 cleared
    `workflow_run_failing:build-mcp-hub` even though the workflow was never re-observed as
    healthy -- it simply wasn't in that pass's `gh run list` output, e.g. because it scrolled
    out of a shared, count-bounded lookback window while still red.

    THIS TEST PINS ABSENCE ONLY. Corrected at the #895 gate (F1): an earlier version of this
    docstring claimed it also pinned "latest run cancelled", which it does not -- a cancelled
    latest run is PRESENCE with a non-failing conclusion, and it took a separate narrowing of
    `observed_workflow_names` to stop that reading as recovery. See
    `test_a_cancelled_latest_run_keeps_its_alert_state` below, which pins that case.
    """
    state_path = tmp_path / "alert-state.json"
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(state_path))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    finding = _finding(workflow_name="build-mcp-hub")

    workflow_run_health.notify_findings(
        [finding], observed_workflow_names={"build-mcp-hub"}, policy=policy
    )
    assert fake_post.calls == 1

    # This run's snapshot did not include build-mcp-hub at all -- neither failing nor observed.
    gated = workflow_run_health.notify_findings([], observed_workflow_names=set(), policy=policy)

    assert gated == []
    doc = json.loads(state_path.read_text())
    assert "workflow_run_failing:build-mcp-hub" in doc, (
        "a workflow absent from this run's observed set must not have its alert state cleared"
    )


def test_clearing_only_touches_this_checkers_own_kind_prefix(tmp_path, monkeypatch):
    """Clearing on green must never disturb an unrelated alert family sharing the state file."""
    state_path = tmp_path / "alert-state.json"
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(state_path))
    state_path.write_text(json.dumps({
        "dev_task_failed:abc123": {"fingerprint": "x", "last_posted_at": "2026-09-01T00:00:00Z"},
    }))

    workflow_run_health.notify_findings(
        [], observed_workflow_names={"build-mcp-hub"}, policy=RecordingPolicy()
    )

    doc = json.loads(state_path.read_text())
    assert "dev_task_failed:abc123" in doc


def test_workflow_run_health_unrelated_finding_does_not_suppress_or_get_suppressed_by_another(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        return True

    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    build_finding = _finding(workflow_name="build-mcp-hub")
    deploy_finding = _finding(workflow_name="deploy")
    observed = {"build-mcp-hub", "deploy"}

    workflow_run_health.notify_findings(
        [build_finding], observed_workflow_names=observed, policy=policy
    )
    gated = workflow_run_health.notify_findings(
        [build_finding, deploy_finding], observed_workflow_names=observed, policy=policy
    )

    assert gated == [deploy_finding]


# ---------------------------------------------------------------------------
# quiet degradation must not read as "everything is green" (caplog sanity)
# ---------------------------------------------------------------------------


def test_dedup_clear_failure_is_logged_and_does_not_block_delivery(monkeypatch, caplog):
    def broken_clear(prefix, active_kinds, *, observed_kinds):
        raise RuntimeError("state file is corrupt")

    monkeypatch.setattr(failure_diagnosis, "clear_stale_dev_task_alert_kinds", broken_clear)
    caplog.set_level(logging.WARNING, logger=workflow_run_health.logger.name)
    policy = RecordingPolicy()

    gated = workflow_run_health.notify_findings(
        [_finding()], observed_workflow_names={_finding().workflow_name}, policy=policy
    )

    assert gated == [_finding()]
    assert "could not clear resolved alert state" in caplog.text


# ---------------------------------------------------------------------------
# the argv itself, not just the signature (F1 at the #906 gate)
#
# Every other test in this file injects a fake `runner`, so `_gh_run_list_json`
# -- the one function that actually builds the `gh` command line -- was executed
# by nothing. Deleting `"--branch", branch,` from its argv left all 30 tests
# green. A mandatory keyword parameter makes dropping the argument a TypeError
# at the CALL SITE; it says nothing about whether the value reaches the command.
# "Does the collaborator receive the argument" is not "does the command carry
# the flag", and this module's defect has now recurred three times, each through
# a door the tests did not watch: #892 shared lookback, #895 cancelled-as-
# recovery, #906 branch scope.
# ---------------------------------------------------------------------------


def _capture_gh_argv(monkeypatch, stdout="[]"):
    """Run _gh_run_list_json against a fake subprocess and return the argv it built."""
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(workflow_run_health.subprocess, "run", fake_run)
    result = workflow_run_health._gh_run_list_json(
        repo="grantbest/homelabv2", workflow="lint.yml", limit=7, branch="main"
    )
    return captured["argv"], result


def test_gh_run_list_argv_carries_branch_scoping(monkeypatch):
    argv, result = _capture_gh_argv(monkeypatch)

    assert result == []
    assert "--branch" in argv, f"--branch absent from the built command line: {argv}"
    # adjacency, not mere presence: a flag separated from its value is a different command
    assert argv[argv.index("--branch") + 1] == "main"


def test_gh_run_list_argv_is_exactly_the_documented_command(monkeypatch):
    argv, _ = _capture_gh_argv(monkeypatch)

    assert argv == [
        "gh", "run", "list",
        "--repo", "grantbest/homelabv2",
        "--workflow", "lint.yml",
        "--branch", "main",
        "--limit", "7",
        "--json", ",".join(workflow_run_health.GH_RUN_LIST_FIELDS),
    ]


def test_gh_default_branch_argv_is_exactly_the_documented_command(monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"defaultBranchRef": {"name": "main"}}', stderr=""
        )

    monkeypatch.setattr(workflow_run_health.subprocess, "run", fake_run)
    assert workflow_run_health._gh_default_branch(repo="grantbest/homelabv2") == "main"
    assert captured["argv"] == [
        "gh", "repo", "view", "grantbest/homelabv2", "--json", "defaultBranchRef",
    ]
