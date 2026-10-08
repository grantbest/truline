"""Scheduled-path workdir hygiene parity with the CLI path.

The CLI path (dispatch_once) marks a workdir's owner right after mkdtemp,
sweeps orphaned workdirs and refuses a full disk before claiming, and cleans
up loudly rather than with shutil.rmtree(ignore_errors=True). The scheduled
(Temporal activity) path had none of that: isolate_activity minted a bare
mkdtemp with no owner marker, cleanup_activity discarded rmtree failures, and
neither claim_activity nor isolate_activity ever swept orphans or checked the
disk floor. That let a launchd restart leak a clone forever, and let a CLI
sweep run concurrently with a scheduled run treat the scheduled run's
unmarked, in-flight workdir as dead and rmtree it out from under the worker.

These tests drive activities.dispatch_steps directly -- no substrate, no
network, no Temporal server -- exactly as test_claim_atomicity.py does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402
from test_claim_atomicity import FakeSubstrate, make_task  # noqa: E402
from test_dispatch import _age, _dead_pid  # noqa: E402


def test_current_workflow_run_identity_reads_activity_info_inside_a_context(monkeypatch):
    monkeypatch.setattr(dispatch_steps.activity, "in_activity", lambda: True)
    monkeypatch.setattr(
        dispatch_steps.activity,
        "info",
        lambda: SimpleNamespace(workflow_id="wf-1", workflow_run_id="run-1"),
    )
    assert dispatch_steps._current_workflow_run_identity() == ("wf-1", "run-1")


def test_current_workflow_run_identity_is_none_outside_a_context():
    # The real temporalio.activity.in_activity(): no worker ever established a
    # context for this direct call, matching every other test in this module.
    assert dispatch_steps._current_workflow_run_identity() == (None, None)


def test_isolate_activity_marks_workdir_owner_before_clone(tmp_path, monkeypatch):
    """Driven without a real clone: make_clone and fingerprint_tree are
    stubbed, so this exercises only isolate_activity's own workdir handling."""
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_make_clone(_cfg, dest):
        dest.mkdir(parents=True)

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)
    monkeypatch.setattr(
        dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", "")
    )

    cfg = dispatch.Config(repo_root=tmp_path)
    state = {"cfg": dispatch_steps._cfg_to_state(cfg)}

    result = dispatch_steps.isolate_activity(state)

    workdir = Path(result["workdir"])
    assert workdir.parent == tmp_path
    assert workdir.name.startswith("factory-")
    assert dispatch.workdir_owner_is_alive(workdir), (
        "isolate_activity must write the same owner marker mark_workdir_owner "
        "writes, or the sweep's liveness check has nothing to read for a "
        "scheduled run's workdir"
    )


def test_isolate_activity_stamps_the_workflow_run_when_run_inside_an_activity_context(
    tmp_path, monkeypatch
):
    """OPS-60: the owner marker must name the RUN, not merely the daemon process
    that happens to host it. Inside a real Temporal activity context,
    isolate_activity reads activity.info() (via _current_workflow_run_identity)
    and passes the workflow_id/run_id through to mark_workdir_owner. Stubbing
    _current_workflow_run_identity directly, rather than temporalio's own
    activity.in_activity()/activity.info(), keeps this test from also needing a
    real activity Context for activity.heartbeat() to find (_run_worker_with_
    heartbeat's own heartbeat calls are exercised elsewhere)."""
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_make_clone(_cfg, dest):
        dest.mkdir(parents=True)

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)
    monkeypatch.setattr(
        dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch_steps,
        "_current_workflow_run_identity",
        lambda: ("wf-123", "run-456"),
    )

    cfg = dispatch.Config(repo_root=tmp_path)
    state = {"cfg": dispatch_steps._cfg_to_state(cfg)}

    result = dispatch_steps.isolate_activity(state)

    workdir = Path(result["workdir"])
    marker = json.loads((workdir / ".owner-pid").read_text())
    assert marker["workflow_id"] == "wf-123"
    assert marker["workflow_run_id"] == "run-456"


def test_isolate_activity_marks_pid_identity_only_outside_a_workflow_context(
    tmp_path, monkeypatch
):
    """The direct-call shape every other test in this module exercises: no
    Temporal worker ever established an activity context, so the marker must
    carry no workflow identity -- a workflow_id/run_id that was never real
    would make workdir_owner_is_alive ask Temporal about a run that does not
    exist."""
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_make_clone(_cfg, dest):
        dest.mkdir(parents=True)

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)
    monkeypatch.setattr(
        dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", "")
    )

    cfg = dispatch.Config(repo_root=tmp_path)
    state = {"cfg": dispatch_steps._cfg_to_state(cfg)}

    result = dispatch_steps.isolate_activity(state)

    marker = json.loads((Path(result["workdir"]) / ".owner-pid").read_text())
    assert "workflow_id" not in marker
    assert "workflow_run_id" not in marker


def test_activity_created_workdir_becomes_sweepable_once_its_run_is_gone_even_if_cleanup_never_ran(
    tmp_path, monkeypatch
):
    """The 'worker restart recovery' ending named in the acceptance criteria: a
    workflow that never resumes far enough to run cleanup_activity (the
    process hosting it died, or an operator terminated it) leaves its workdir
    on disk with the daemon's pid still recorded as its owner. The run-identity
    backstop is what makes that workdir sweepable anyway, once Temporal
    confirms the run itself is no longer executing -- no cleanup_activity call
    involved at all."""
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))

    def fake_make_clone(_cfg, dest):
        dest.mkdir(parents=True)

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)
    monkeypatch.setattr(
        dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", "")
    )
    monkeypatch.setattr(
        dispatch_steps,
        "_current_workflow_run_identity",
        lambda: ("wf-restart", "run-restart"),
    )

    cfg = dispatch.Config(repo_root=tmp_path)
    state = {"cfg": dispatch_steps._cfg_to_state(cfg)}
    result = dispatch_steps.isolate_activity(state)
    workdir = Path(result["workdir"])

    # cleanup_activity is deliberately never called -- this is the ending
    # where the worker restarted before it could run.
    monkeypatch.setattr(dispatch, "temporal_workflow_run_is_alive", lambda *_a, **_k: True)
    assert dispatch.workdir_owner_is_alive(workdir), (
        "while Temporal still reports the run RUNNING, cleanup having never run "
        "must not make the workdir look dead"
    )

    monkeypatch.setattr(dispatch, "temporal_workflow_run_is_alive", lambda *_a, **_k: False)
    assert not dispatch.workdir_owner_is_alive(workdir), (
        "once Temporal reports the run over, the workdir must read as dead -- "
        "sweepable -- regardless of whether cleanup_activity ever ran"
    )

    _age(workdir, dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES + 10)
    result = dispatch.sweep_orphan_workdirs(tmp_path)
    assert not workdir.exists()
    assert str(workdir) in result.removed


def test_isolate_activity_cleans_up_loudly_when_clone_fails(tmp_path, monkeypatch):
    """A failure before the state carries a 'workdir' key back to
    workflow_core means the outer cleanup activity never sees this workdir --
    isolate_activity's own except-block cleanup is the only thing that will
    ever remove it, so it must use the loud cleanup_workdir, not a silent
    ignore_errors=True rmtree."""
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        dispatch, "make_clone", lambda _cfg, _dest: (_ for _ in ()).throw(RuntimeError("clone failed"))
    )

    cleaned = []
    monkeypatch.setattr(dispatch, "cleanup_workdir", lambda workdir: cleaned.append(workdir))

    cfg = dispatch.Config(repo_root=tmp_path)
    state = {"cfg": dispatch_steps._cfg_to_state(cfg)}

    try:
        dispatch_steps.isolate_activity(state)
    except RuntimeError:
        pass
    else:
        raise AssertionError("isolate_activity must re-raise the clone failure")

    assert len(cleaned) == 1
    assert cleaned[0].parent == tmp_path


def test_sweep_preserves_marked_live_workdir_and_removes_unmarked_stale_one(tmp_path):
    """The exact destructive-sweep scenario the bug describes: a scheduled
    workdir that never got a marker looks identical, to the sweep, to a
    genuinely abandoned one. Once marked (as isolate_activity now does), a
    live scheduled run survives the same sweep that reclaims an unmarked
    stale directory next to it."""
    live_scheduled = tmp_path / "factory-scheduled-live"
    live_scheduled.mkdir()
    dispatch.mark_workdir_owner(live_scheduled)  # owned by this test process: alive
    _age(live_scheduled, dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES + 10)

    unmarked_stale = tmp_path / "factory-scheduled-unmarked"
    unmarked_stale.mkdir()
    _age(unmarked_stale, dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES + 10)

    result = dispatch.sweep_orphan_workdirs(tmp_path)

    assert live_scheduled.exists()
    assert not unmarked_stale.exists()
    assert str(unmarked_stale) in result.removed


def test_cleanup_activity_reports_rmtree_failure_instead_of_swallowing_it(
    tmp_path, monkeypatch, capsys
):
    workdir = tmp_path / "factory-scheduled-cleanup-fail"
    workdir.mkdir()
    monkeypatch.setattr(
        dispatch.shutil,
        "rmtree",
        lambda _p: (_ for _ in ()).throw(OSError("no space left on device")),
    )

    result = dispatch_steps.cleanup_activity({"workdir": str(workdir)})

    assert result == {"status": "cleaned", "exit_code": 0}
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "no space left on device" in out


def test_claim_activity_sweeps_orphan_workdir_before_claiming(tmp_path, monkeypatch):
    orphan = tmp_path / "factory-scheduled-orphan"
    orphan.mkdir()
    (orphan / ".owner-pid").write_text(str(_dead_pid()))
    _age(orphan, dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES + 10)

    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task_a])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({})

    assert result["status"] == "claimed"
    assert not orphan.exists(), (
        "the scheduled claim path must reclaim what a prior scheduled run's "
        "kill stranded, exactly as dispatch_once does before its claim"
    )


def test_claim_activity_refuses_claim_below_disk_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        dispatch.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(
            total=10 * 1024**3, used=int(9.9 * 1024**3), free=int(0.1 * 1024**3)
        ),
    )
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task_a])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({})

    assert result["status"] == "disk_floor"
    assert result["exit_code"] == 1
    assert sub.transitions == [], "a full disk must never be recorded as a verdict on the bead"
    assert task_a["state"] == "pending"


def test_max_agent_minutes_ceiling_is_below_orphan_sweep_threshold():
    """The relation itself, asserted against the live constants -- not two
    literals that happen to agree today."""
    assert dispatch.MAX_AGENT_MINUTES_CEILING < dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES


def test_effective_budget_minutes_clamps_an_oversized_declared_budget():
    oversized = dispatch.MAX_AGENT_MINUTES_CEILING + 1000
    content = {"budget": {"max_agent_minutes": oversized}}
    assert dispatch.effective_budget_minutes(content) == dispatch.MAX_AGENT_MINUTES_CEILING


def test_effective_budget_minutes_passes_through_a_budget_under_the_ceiling():
    modest = dispatch.MAX_AGENT_MINUTES_CEILING - 1
    content = {"budget": {"max_agent_minutes": modest}}
    assert dispatch.effective_budget_minutes(content) == modest


def test_effective_budget_minutes_defaults_when_undeclared():
    assert dispatch.effective_budget_minutes({}) == dispatch.DEFAULT_BUDGET_MINUTES


def test_claim_activity_clamps_an_oversized_declared_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    task_a["content"]["budget"] = {"max_agent_minutes": dispatch.DEFAULT_ORPHAN_WORKDIR_MINUTES * 10}
    sub = FakeSubstrate([task_a])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({})

    assert result["status"] == "claimed"
    assert result["budget"] == dispatch.MAX_AGENT_MINUTES_CEILING
