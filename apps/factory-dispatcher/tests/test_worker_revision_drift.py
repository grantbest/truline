"""The running worker must be able to say which revision it loaded.

2026-08-23: a launchd worker started 08-22 13:24, before #500 (the
consecutive-environmental-fault breaker) merged 08-23 08:55. Python had
loaded dispatch.py once, at process start, so the merge changed nothing
about the process already running. Bead f9fc8ff9 faulted eight consecutive
times and was never stopped, and nothing about the factory's status output
said the running worker was not running the code on main.

These tests assert two things that recorded nothing before this change:
that a worker's loaded revision is durably readable without restarting or
interrupting it, and that a revision behind main is reported as drift while
a current one is not. They require no Temporal server, no substrate, no
network and no real git repository -- git itself is injected as a fake.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
import worker_revision  # noqa: E402
from activities import worker_revision_drift as wrd  # noqa: E402
from schedule_runtime import FactoryScheduleStatus  # noqa: E402
from worker_revision import WorkerCheckoutDriftError, WorkerRevisionStatus  # noqa: E402

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
SCHEDULE_ID = "factory-dispatcher-dev"
OLD_REVISION = "aaaaaaa"
CURRENT_REVISION = "ccccccc"
MAIN_REVISION = "ccccccc"


def quiet_status():
    return FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=True,
        in_flight=(),
        recent=(),
    )


def fake_git_runner(*, stdout: str = "", returncode: int = 0):
    def runner(cmd, cwd=None, check=True, timeout=None):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    return runner


# ---------------------------------------------------------------------------
# capture, record and read -- the "readable without restarting" half
# ---------------------------------------------------------------------------


def test_recorded_revision_is_readable_from_a_separate_read_without_touching_the_writer(
    tmp_path,
):
    state_path = tmp_path / "worker-revision.json"
    git_runner = fake_git_runner(stdout=f"{OLD_REVISION}\n")

    written = worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=git_runner,
    )
    assert written is not None
    assert written.revision == OLD_REVISION

    # A wholly separate read call, as schedule_status.py would make it later,
    # with no interaction with whatever wrote the file.
    read_back = worker_revision.read_worker_revision_record(state_path)
    assert read_back == written


def test_no_record_reads_as_none_not_a_crash(tmp_path):
    assert worker_revision.read_worker_revision_record(tmp_path / "missing.json") is None


def test_record_worker_start_is_best_effort_and_never_raises(tmp_path):
    def exploding_git_runner(cmd, cwd=None, check=True, timeout=None):
        raise RuntimeError("git not on PATH")

    result = worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=tmp_path / "worker-revision.json",
        now=NOW,
        git_runner=exploding_git_runner,
    )
    assert result is None
    assert not (tmp_path / "worker-revision.json").exists()


# ---------------------------------------------------------------------------
# drift detection -- captured from the checkout, not an env var, no network
# ---------------------------------------------------------------------------


def test_drift_status_is_true_when_worker_revision_is_not_an_ancestor_of_main(tmp_path):
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{OLD_REVISION}\n"),
    )

    calls = []

    def git_runner(cmd, cwd=None, check=True, timeout=None):
        calls.append(cmd)
        if cmd[1] == "rev-parse":
            return SimpleNamespace(stdout=f"{MAIN_REVISION}\n", stderr="", returncode=0)
        assert cmd[1] == "merge-base"
        return SimpleNamespace(stdout="", stderr="", returncode=1)  # not an ancestor

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=git_runner,
    )

    assert status.error == ""
    assert status.is_ancestor is False
    assert status.drifted is True
    assert status.commits_behind is None  # not meaningful off main entirely
    assert status.worker_revision == OLD_REVISION
    assert status.main_revision == MAIN_REVISION
    # No `git fetch`: the check must read local refs only, never the network.
    assert not any(cmd[1] == "fetch" for cmd in calls)


def test_drift_status_is_true_when_worker_revision_is_mains_parent(tmp_path):
    # This is OPS-11's actual bug, reproduced: a worker sitting on an
    # ancestor of main -- the ordinary, every-merge case -- is exactly the
    # condition "Behavioral changes merged to main ... are not in effect
    # until the worker is restarted" describes. It must report drift.
    repo = build_repo_on_branch(tmp_path, "main")
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "README.md").write_text("two\n")
    git(repo, "commit", "-am", "second commit")
    (repo / "README.md").write_text("three\n")
    git(repo, "commit", "-am", "third commit")
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()

    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=repo,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{parent}\n"),
    )

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=repo,
    )

    assert status.error == ""
    assert status.is_ancestor is True
    assert status.drifted is True
    assert status.commits_behind == 2
    assert status.worker_revision == parent
    assert status.main_revision == tip


def test_drift_status_is_false_when_worker_revision_is_current(tmp_path):
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{CURRENT_REVISION}\n"),
    )

    def git_runner(cmd, cwd=None, check=True, timeout=None):
        if cmd[1] == "rev-parse":
            return SimpleNamespace(stdout=f"{MAIN_REVISION}\n", stderr="", returncode=0)
        assert cmd[1] == "merge-base"
        return SimpleNamespace(stdout="", stderr="", returncode=0)  # is an ancestor

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=git_runner,
    )

    assert status.error == ""
    assert status.is_ancestor is True
    assert status.drifted is False
    assert status.commits_behind == 0


def test_no_record_reports_could_not_determine_not_current(tmp_path):
    status = worker_revision.describe_worker_revision_drift(
        state_path=tmp_path / "missing.json",
        repo_root=tmp_path,
        git_runner=fake_git_runner(stdout="unused\n"),
    )

    assert status.error != ""
    assert status.is_ancestor is None
    assert status.drifted is False  # unknown must not silently read as safe


def test_git_failure_reports_could_not_determine(tmp_path):
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{OLD_REVISION}\n"),
    )

    def exploding_git_runner(cmd, cwd=None, check=True, timeout=None):
        raise RuntimeError("main ref does not exist locally")

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=exploding_git_runner,
    )

    assert status.error != ""
    assert status.is_ancestor is None


# ---------------------------------------------------------------------------
# pid correspondence -- the record must name a writer still running, or the
# status must say it cannot tell, not report currency or drift on trust.
#
# 2026-08-27: two production incidents in one day, both the dispatcher test
# suite overwriting a real launchd worker's revision record with whatever
# checkout pytest ran in. First with a revision that happened to parse as
# main's own tip -- reported commits_behind=0, i.e. falsely "current" for a
# worker 17 commits behind. Second with a revision only a PR branch ever
# held -- reported "could not determine" for the right question asked of the
# wrong reason (`git merge-base` simply couldn't resolve an object it never
# had). Both writer processes had already exited by the time anyone ran
# schedule_status.py. Neither shape should ever be trusted, and a check that
# only asked "is this an ancestor of main" cannot tell the difference between
# a stale-but-legitimate worker record and a foreign one -- only checking who
# wrote it can.
# ---------------------------------------------------------------------------


def test_drift_status_could_not_determine_when_writer_process_is_not_running(tmp_path):
    # Shape one: the foreign record names a revision that IS main's own tip
    # (MAIN_REVISION), which is exactly what made this incident read as
    # falsely "current" rather than merely wrong.
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{MAIN_REVISION}\n"),
        pid=999999,
    )

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=fake_git_runner(stdout=f"{MAIN_REVISION}\n"),
        pid_is_running=lambda pid: False,
    )

    assert status.error != ""
    assert "999999" in status.error
    assert status.is_ancestor is None
    assert status.drifted is False  # unknown must not silently read as safe
    assert status.commits_behind is None


def test_drift_status_could_not_determine_when_writer_is_gone_and_revision_is_unresolvable(
    tmp_path,
):
    # Shape two: the foreign record names a revision this checkout's git
    # cannot resolve at all -- a PR branch tip only the writer's checkout
    # ever held. The pid check must catch this before git is even asked,
    # so both 2026-08-27 shapes are reported the same untrustworthy way
    # rather than one reading as "current" and the other as a git error.
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout="2e599c97deadbeef\n"),
        pid=999999,
    )

    def unreachable_git_runner(cmd, cwd=None, check=True, timeout=None):
        raise AssertionError("must not query git before pid correspondence is confirmed")

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=unreachable_git_runner,
        pid_is_running=lambda pid: False,
    )

    assert status.error != ""
    assert status.is_ancestor is None
    assert status.drifted is False


def test_drift_status_could_not_determine_for_a_record_written_before_pid_tracking_existed(
    tmp_path,
):
    state_path = tmp_path / "worker-revision.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"revision": "%s", "started_at": "%s"}' % (OLD_REVISION, NOW.isoformat()),
        encoding="utf-8",
    )

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=fake_git_runner(stdout=f"{MAIN_REVISION}\n"),
    )

    assert status.error != ""
    assert status.is_ancestor is None
    assert status.drifted is False


def test_drift_status_still_reports_commits_behind_when_the_writer_is_confirmed_running(
    tmp_path,
):
    # OPS-18's case, proven against the new correspondence check explicitly
    # (rather than relying on the recording call sharing the test's own live
    # pid): a record whose writer IS still running must be compared against
    # main exactly as before.
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(
        repo_root=tmp_path,
        state_path=state_path,
        now=NOW,
        git_runner=fake_git_runner(stdout=f"{OLD_REVISION}\n"),
        pid=4242,
    )

    def git_runner(cmd, cwd=None, check=True, timeout=None):
        if cmd[1] == "rev-parse":
            return SimpleNamespace(stdout=f"{MAIN_REVISION}\n", stderr="", returncode=0)
        if cmd[1] == "merge-base":
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        assert cmd[1] == "rev-list"
        return SimpleNamespace(stdout="3\n", stderr="", returncode=0)

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path,
        repo_root=tmp_path,
        git_runner=git_runner,
        pid_is_running=lambda pid: pid == 4242,
    )

    assert status.error == ""
    assert status.is_ancestor is True
    assert status.drifted is True
    assert status.commits_behind == 3
    assert status.worker_revision == OLD_REVISION


# ---------------------------------------------------------------------------
# operator-facing report -- the acceptance criteria's two required scenarios
# ---------------------------------------------------------------------------


def test_status_output_reports_drift_for_a_worker_whose_revision_is_behind():
    status = WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-08-22T13:24:00Z",
        main_ref="main",
        main_revision=MAIN_REVISION,
        is_ancestor=False,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        worker_revision_status=status,
        now=NOW,
    )

    assert "worker_revision_drift: true" in rendered
    assert "WORKER REVISION DRIFT" in rendered
    assert OLD_REVISION in rendered
    assert MAIN_REVISION in rendered
    assert "running for" in rendered.lower() or "worker_running_for" in rendered
    assert "22h 36m" in rendered  # NOW - worker_started_at


def test_status_output_names_commits_behind_and_calls_for_a_restart_when_worker_is_an_ancestor():
    status = WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-08-22T13:24:00Z",
        main_ref="main",
        main_revision=MAIN_REVISION,
        is_ancestor=True,
        commits_behind=2,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        worker_revision_status=status,
        now=NOW,
    )

    assert "worker_revision_drift: true" in rendered
    assert "2 commits behind" in rendered
    assert "restart" in rendered.lower()
    assert "unreviewed branch" not in rendered.lower()


def test_status_output_names_unreviewed_branch_not_a_commit_count_when_worker_is_not_an_ancestor():
    status = WorkerRevisionStatus(
        worker_revision=OLD_REVISION,
        worker_started_at="2026-08-22T13:24:00Z",
        main_ref="main",
        main_revision=MAIN_REVISION,
        is_ancestor=False,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        worker_revision_status=status,
        now=NOW,
    )

    assert "worker_revision_drift: true" in rendered
    assert "unreviewed branch" in rendered.lower()
    assert "commits behind" not in rendered
    assert "commit behind" not in rendered


def test_status_output_reports_no_drift_for_a_worker_that_is_current():
    status = WorkerRevisionStatus(
        worker_revision=CURRENT_REVISION,
        worker_started_at="2026-08-23T11:00:00Z",
        main_ref="main",
        main_revision=MAIN_REVISION,
        is_ancestor=True,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        worker_revision_status=status,
        now=NOW,
    )

    assert "worker_revision_drift: false" in rendered
    assert "WORKER REVISION DRIFT" not in rendered


def test_status_output_omits_worker_revision_block_when_not_supplied():
    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        now=NOW,
    )

    assert "worker_revision" not in rendered


def test_status_output_reports_could_not_determine_distinctly_from_current():
    status = WorkerRevisionStatus.could_not_determine("main", "no worker revision record found")

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        worker_revision_status=status,
        now=NOW,
    )

    assert "worker_revision: could-not-determine" in rendered
    assert "worker_revision_drift: could-not-determine" in rendered
    assert "worker_revision_drift: false" not in rendered
    assert "WORKER REVISION DRIFT" not in rendered


# End-to-end wiring -- proof that worker.py itself calls record_worker_start()
# at startup, from the real checkout, before any other guard -- lives in
# test_worker_config_guard.py's test_main_proceeds_past_the_guard_when_config_is_complete,
# alongside the rest of the startup-ordering tests it belongs with.


# ---------------------------------------------------------------------------
# ensure_checkout_is_on_main_ancestor -- the hard startup gate (2026-08-25).
#
# Unlike the best-effort record above, this must refuse to return. It needs
# a real git repository (branch names, not just a stdout string), so these
# use real local-only git commands -- no network, no cluster, no substrate.
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [worker_revision.GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def build_repo_on_branch(tmp_path: Path, branch: str) -> Path:
    repo = tmp_path / "repo"
    git(tmp_path, "init", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("one\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    if branch != "main":
        git(repo, "checkout", "-b", branch)
        (repo / "note.txt").write_text("side quest\n")
        git(repo, "add", "note.txt")
        git(repo, "commit", "-m", "feature work")
    return repo


def test_ensure_checkout_refuses_when_on_a_feature_branch_and_names_it(tmp_path):
    # This is today's bug, reproduced: the launchd worker restarted while a
    # PR branch (2026-08-25: PR #518's tip) was checked out in the shared
    # working tree it runs from. This must fail against today's behaviour --
    # nothing currently stops a worker starting on any branch -- and the
    # failure must name the branch, the way the operator would need to know.
    repo = build_repo_on_branch(tmp_path, "fix/drop-two-dead-imports")

    with pytest.raises(WorkerCheckoutDriftError) as excinfo:
        worker_revision.ensure_checkout_is_on_main_ancestor(repo_root=repo)

    message = str(excinfo.value)
    assert "fix/drop-two-dead-imports" in message
    assert "not an ancestor" in message
    assert "refusing to start" in message


def test_ensure_checkout_passes_when_on_main(tmp_path):
    repo = build_repo_on_branch(tmp_path, "main")

    revision = worker_revision.ensure_checkout_is_on_main_ancestor(repo_root=repo)

    assert revision == git(repo, "rev-parse", "HEAD").stdout.strip()


def test_ensure_checkout_passes_for_a_detached_head_at_an_old_ancestor_of_main(tmp_path):
    # Stale-but-on-main must not read as drift, matching describe_worker_revision_drift's
    # existing semantics: "ancestor of main" is the safety property, not "current".
    repo = build_repo_on_branch(tmp_path, "main")
    first_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "README.md").write_text("two\n")
    git(repo, "commit", "-am", "second commit")
    git(repo, "checkout", first_commit)

    revision = worker_revision.ensure_checkout_is_on_main_ancestor(repo_root=repo)

    assert revision == first_commit


def test_ensure_checkout_reads_repo_root_from_the_module_global_when_not_passed(
    tmp_path, monkeypatch
):
    # worker.py calls this with no repo_root override; tests must be able to
    # point it at a fixture by monkeypatching worker_revision.REPO_ROOT, the
    # same shape the existing config guards already use for
    # dispatch.required_worker_executables.
    repo = build_repo_on_branch(tmp_path, "fix/drop-two-dead-imports")
    monkeypatch.setattr(worker_revision, "REPO_ROOT", repo)

    with pytest.raises(WorkerCheckoutDriftError) as excinfo:
        worker_revision.ensure_checkout_is_on_main_ancestor()

    assert "fix/drop-two-dead-imports" in str(excinfo.value)


def test_worker_revision_drift_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
    """#727: the change reconciler's live `_request` passed `headers=self._headers`
    unconditionally, so a caller supplying its own `headers=` kwarg collided with it
    (`httpx.request() got multiple values for keyword argument 'headers'`) -- a
    live-only shape the FakeStore, which replaces the whole store, never walks. This
    store shares the same `_request` shape. Pins: one merged headers dict carrying
    BOTH the standing auth header and a per-call header, per-call winning on
    collision.
    """
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(wrd.httpx, "request", fake_request)

    store = wrd.SubstrateWorkerRevisionDriftStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": wrd.CREATED_BY, "Content-Type": "text/plain"},
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == wrd.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"
