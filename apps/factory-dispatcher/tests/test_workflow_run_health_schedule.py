"""Tests for the scheduled GitHub Actions workflow-run health check.

An observer nothing runs is exactly the shape this bead exists to end -- `cluster_health.py`
shipped working and unscheduled for weeks (OPS-14) before anyone noticed. `workflow_run_health`
rides the existing 15-minute `ClusterHealthWorkflow`/cluster-health schedule as a second leg
(see `workflows/cluster_health.py`'s module docstring for why this is not
a dedicated new schedule: declaring one trips `readme-model-conformance`, which requires
`docs/architecture/README.md` to name every schedule `schedule_runtime.py` declares, and that file
is out of this bead's scope). This module asserts the two things that fail against today's repo
before this change lands: that the checker's activity is reachable from `ClusterHealthWorkflow`
(which is already schedule-registered -- `tests/test_cluster_health_schedule.py` covers that half),
and that a scheduled run posts exactly the findings the checker itself produced -- nothing more,
nothing less, and nothing when every workflow is green.

No Temporal server, no network: the activity is tested by injecting a fake `collect` and a
recording `notify`, the same technique `test_cluster_health_schedule.py` uses for
`run_cluster_health_check`. `tests/test_schedule_wiring.py`'s static AST-based check separately
proves `report_workflow_run_health` is reachable from a worker-registered, schedule-registered
workflow (or must carry a declared exemption) -- the general "wired to a schedule" invariant this
module does not need to re-derive.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow_run_health  # noqa: E402
from activities import workflow_run_health as activity  # noqa: E402


def _run(workflow_name: str, *, conclusion: str = "success", updated_at: str = "2026-09-11T00:00:00Z") -> dict:
    return {
        "databaseId": "1",
        "workflowName": workflow_name,
        "status": "completed",
        "conclusion": conclusion,
        "updatedAt": updated_at,
        "url": "https://github.com/example/repo/actions/runs/1",
    }


# -- the checker rides the existing, already-scheduled ClusterHealthWorkflow ------------------


def test_workflow_run_health_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_workflow_run_health_activity" in names


def test_cluster_health_workflow_calls_the_workflow_run_health_activity():
    """`ClusterHealthWorkflow` rides the checker as a second leg -- not a dedicated schedule
    (see workflows/cluster_health.py's module docstring)."""
    import inspect

    from workflows.cluster_health import ClusterHealthWorkflow

    source = inspect.getsource(ClusterHealthWorkflow)
    assert "report_workflow_run_health" in source
    assert "report_cluster_health" in source  # the original leg must still be present


def test_a_workflow_run_health_leg_failure_does_not_skip_the_cluster_health_leg():
    """Mirrors DoctrineStalenessReportWorkflow's four-leg shape: one leg's failure must not
    silence the other leg's own alerting, and the first failure must still surface afterward."""
    import ast
    import inspect

    from workflows.cluster_health import ClusterHealthWorkflow

    tree = ast.parse(inspect.getsource(ClusterHealthWorkflow))
    calls = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_activity"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert calls == ["report_cluster_health", "report_workflow_run_health"]

    # Each call must be inside its own try/except ActivityError, per DoctrineStalenessReportWorkflow's
    # established shape -- not one try wrapping both, which would skip the second leg entirely.
    try_blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert len(try_blocks) == 2


# -- a run posts exactly what the checker produced -------------------------------------------


def test_a_healthy_run_list_posts_nothing():
    runs = [_run("build-mcp-hub"), _run("backup-offsite-sync")]
    calls = {"notify": 0}

    def recording_notify(findings, *, observed_workflow_names):
        calls["notify"] += 1
        assert findings == []
        assert observed_workflow_names == {"build-mcp-hub", "backup-offsite-sync"}
        return findings

    result = activity.run_workflow_run_health_check(
        repo_fn=lambda: "example/repo",
        collect=lambda repo: runs,
        notify=recording_notify,
    )

    assert result["finding_count"] == 0
    assert result["failing_workflows"] == []
    assert calls["notify"] == 1


def test_a_failing_workflow_posts_exactly_the_checkers_own_finding():
    runs = [_run("build-mcp-hub", conclusion="failure"), _run("backup-offsite-sync")]
    posted = []

    def recording_notify(findings, *, observed_workflow_names):
        posted.extend(findings)
        return findings

    result = activity.run_workflow_run_health_check(
        repo_fn=lambda: "example/repo",
        collect=lambda repo: runs,
        notify=recording_notify,
    )

    expected = workflow_run_health.failing_workflow_findings(runs)
    assert result["finding_count"] == len(expected) == 1
    assert result["failing_workflows"] == ["build-mcp-hub"]
    assert posted == expected


def test_the_activity_never_reimplements_a_check_it_calls_find_observe_and_notify_exactly_once():
    """Non-duplication: the scheduled path must drive the checker's own functions, not new logic."""
    runs = [_run("build-mcp-hub", conclusion="failure")]
    calls = {"find": 0, "observe": 0, "notify": 0}
    sentinel_findings = [
        workflow_run_health.WorkflowRunFinding(
            workflow_name="build-mcp-hub",
            conclusion="failure",
            run_url="https://x/runs/1",
            updated_at="2026-09-11T00:00:00Z",
            run_id="1",
        )
    ]
    sentinel_observed = {"build-mcp-hub"}

    def fake_find(fetched_runs):
        calls["find"] += 1
        assert fetched_runs is runs
        return sentinel_findings

    def fake_observe(fetched_runs):
        calls["observe"] += 1
        assert fetched_runs is runs
        return sentinel_observed

    def fake_notify(findings, *, observed_workflow_names):
        calls["notify"] += 1
        assert findings is sentinel_findings
        assert observed_workflow_names is sentinel_observed
        return findings

    result = activity.run_workflow_run_health_check(
        repo_fn=lambda: "example/repo",
        collect=lambda repo: runs,
        find=fake_find,
        observe=fake_observe,
        notify=fake_notify,
    )

    assert calls == {"find": 1, "observe": 1, "notify": 1}
    assert result["finding_count"] == 1
    assert result["failing_workflows"] == ["build-mcp-hub"]


# -- the checker's own failure must not read as a quiet, healthy run --------------------------


def test_gh_being_unreachable_fails_the_activity_rather_than_reporting_zero_findings():
    def broken_collect(repo):
        raise workflow_run_health.MissingGhRunOutputError("gh: command not found")

    calls = {"notify": 0}

    def counting_notify(findings):
        calls["notify"] += 1
        return findings

    try:
        activity.run_workflow_run_health_check(
            repo_fn=lambda: "example/repo",
            collect=broken_collect,
            notify=counting_notify,
        )
        raised = False
    except workflow_run_health.MissingGhRunOutputError:
        raised = True

    assert raised, "a checker that never ran must fail the activity, not report a clean run"
    assert calls["notify"] == 0


def test_gh_unreachable_posts_a_deduped_notice_through_the_declared_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    import failure_diagnosis

    policy = workflow_run_health.notify.AlertPolicy(
        load_alert_state=failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )
    monkeypatch.setattr(failure_diagnosis, "dev_task_alert_policy", lambda post=None: policy)

    def broken_collect(repo):
        raise workflow_run_health.MissingGhRunOutputError("gh: command not found")

    raised_count = 0
    for _ in range(3):
        try:
            activity.run_workflow_run_health_check(
                repo_fn=lambda: "example/repo", collect=broken_collect
            )
        except workflow_run_health.MissingGhRunOutputError:
            raised_count += 1

    assert raised_count == 3
    assert fake_post.calls == 1  # deduped: the standing outage posts once, not on every tick
