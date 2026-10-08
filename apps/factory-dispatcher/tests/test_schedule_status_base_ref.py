"""A stale base ref must be an operator-visible, factory-level signal.

2026-08-26: a merge to main left the dispatcher's local `main` branch behind
`origin/main`. `dispatch.ensure_base_ref_current` correctly refused to clone
from it, but the refusal lived only in one bead's own notes -- the Temporal
schedule kept firing every 15 minutes, `DispatchTaskWorkflow` completed
rather than failed, and the board just showed pending work. That state
lasted ten hours before anyone noticed.

These tests cover `schedule_status.describe_base_ref_status` (a live,
local-only git read, mirroring `worker_revision.describe_worker_revision_drift`),
its rendering into `render_schedule_status` (naming both revisions and the
commit count), and its wiring into `factory_health_notifications` (one
factory-wide notification, not per-bead noise) -- against the exact incident
shape: four commits behind, a clean tree, no local-only commits.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cluster_health  # noqa: E402
import dispatch  # noqa: E402
import schedule_status  # noqa: E402
from schedule_runtime import FactoryScheduleStatus  # noqa: E402
from schedule_status import BaseRefLiveStatus  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SCHEDULE_ID = "factory-dispatcher-dev"
INTERVAL_SECONDS = 15 * 60


def quiet_status():
    return FactoryScheduleStatus(
        schedule_id=SCHEDULE_ID,
        paused=False,
        in_flight=(),
        recent=(),
    )


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [dispatch.GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def build_repo_behind_by(tmp_path: Path, commits: int) -> tuple[Path, str, str]:
    """A local `main` tracking `origin/main`, `commits` commits behind, clean tree.

    Mirrors test_dispatch.py's create_repo_with_tracking_main, extended to
    push a declared number of commits from a second clone so the exact
    incident shape (four commits behind) is reproducible.
    """
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("zero\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    local_rev = git(repo, "rev-parse", "main").stdout.strip()

    updater = tmp_path / "updater"
    git(tmp_path, "clone", str(remote), str(updater))
    git(updater, "checkout", "main")
    git(updater, "config", "user.email", "factory@example.test")
    git(updater, "config", "user.name", "Factory Test")
    for i in range(commits):
        (updater / "README.md").write_text(f"commit-{i}\n")
        git(updater, "commit", "-am", f"advance remote {i}")
    git(updater, "push", "origin", "main")
    git(repo, "fetch", "origin", "main")
    upstream_rev = git(repo, "rev-parse", "origin/main").stdout.strip()

    return repo, local_rev, upstream_rev


# ---------------------------------------------------------------------------
# describe_base_ref_status -- live, local-only, best-effort
# ---------------------------------------------------------------------------


def test_describe_base_ref_status_reports_stale_with_both_revisions_and_count(tmp_path):
    repo, local_rev, upstream_rev = build_repo_behind_by(tmp_path, 4)

    status = schedule_status.describe_base_ref_status(
        dispatch.Config(repo_root=repo, base_ref="main")
    )

    assert status.error == ""
    assert status.stale is True
    assert status.commits_behind == 4
    assert status.local_only == 0
    assert status.local_rev == local_rev
    assert status.upstream_rev == upstream_rev
    assert status.upstream_ref == "origin/main"


def test_describe_base_ref_status_reports_not_stale_when_current(tmp_path):
    repo, local_rev, upstream_rev = build_repo_behind_by(tmp_path, 0)

    status = schedule_status.describe_base_ref_status(
        dispatch.Config(repo_root=repo, base_ref="main")
    )

    assert status.error == ""
    assert status.stale is False
    assert status.commits_behind == 0


def test_describe_base_ref_status_never_writes_to_the_checkout(tmp_path):
    # PRIN-004 (#525): the operator's checked-out branch is a frozen contract.
    # A stale-base-ref check must be pure read: same branch, same HEAD, same
    # working tree, before and after.
    repo, local_rev, _upstream_rev = build_repo_behind_by(tmp_path, 4)
    branch_before = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    status_before = git(repo, "status", "--porcelain").stdout

    schedule_status.describe_base_ref_status(dispatch.Config(repo_root=repo, base_ref="main"))

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch_before
    assert git(repo, "rev-parse", "main").stdout.strip() == local_rev
    assert git(repo, "status", "--porcelain").stdout == status_before


def test_describe_base_ref_status_is_best_effort_not_a_crash(tmp_path):
    # No git repo at all at this path -- base_ref_status must raise, and this
    # must turn that into "could not determine", never "current".
    status = schedule_status.describe_base_ref_status(
        dispatch.Config(repo_root=tmp_path, base_ref="main")
    )

    assert status.error != ""
    assert status.stale is False


# ---------------------------------------------------------------------------
# render_schedule_status -- the operator-visible signal, naming both revisions
# ---------------------------------------------------------------------------


def test_status_output_names_both_revisions_and_commit_count_when_stale():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        commits_behind=4,
        local_only=0,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        base_ref_status=status,
        now=NOW,
    )

    assert "base_ref_stale: true" in rendered
    assert "base_ref_state: stale" in rendered
    assert "STALE BASE REF" in rendered
    assert "a" * 40 in rendered
    assert "b" * 40 in rendered
    assert "4 commits behind" in rendered


def test_status_output_reports_no_staleness_when_current():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="c" * 40,
        upstream_ref="origin/main",
        upstream_rev="c" * 40,
        commits_behind=0,
        local_only=0,
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        base_ref_status=status,
        now=NOW,
    )

    assert "base_ref_stale: false" in rendered
    assert "base_ref_state: healthy" in rendered
    assert "STALE BASE REF" not in rendered


# ---------------------------------------------------------------------------
# base_ref_state -- the third, closed-vocabulary value AC-2 of the bead that
# split the rendering half of dev.task 05a884ff out adds: "not checked" must
# be reachable, and distinct from "healthy", "stale" and "wrong", whenever the
# last drain cycle did not confirm this checkout's tracking ref against
# GitHub's main -- independent of what the local stale/wrong bits happen to
# read, since neither is trustworthy against unconfirmed data.
# ---------------------------------------------------------------------------


def test_base_ref_live_status_state_is_not_checked_when_unconfirmed_even_if_locally_healthy():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="c" * 40,
        upstream_ref="origin/main",
        upstream_rev="c" * 40,
        commits_behind=0,
        local_only=0,
        checked=False,
        check_reason="mirror ahead of GitHub; refused to fast-forward",
    )

    assert status.stale is False
    assert status.wrong is False
    assert status.state == "not_checked"


def test_base_ref_live_status_state_is_healthy_only_when_checked_and_clean():
    checked_and_clean = BaseRefLiveStatus(
        base_ref="main",
        local_rev="c" * 40,
        upstream_ref="origin/main",
        upstream_rev="c" * 40,
        commits_behind=0,
        local_only=0,
        checked=True,
    )
    assert checked_and_clean.state == "healthy"

    not_checked = BaseRefLiveStatus(
        base_ref="main",
        local_rev="c" * 40,
        upstream_ref="origin/main",
        upstream_rev="c" * 40,
        commits_behind=0,
        local_only=0,
        checked=False,
    )
    assert not_checked.state != "healthy"


def test_status_output_names_the_reason_when_not_checked():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="a" * 40,
        commits_behind=0,
        local_only=0,
        checked=False,
        check_reason="source mirror main not updated from its canonical remote: boom",
    )

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        base_ref_status=status,
        now=NOW,
    )

    assert "base_ref_state: not_checked" in rendered
    assert "BASE REF NOT CHECKED AGAINST GITHUB" in rendered
    assert "source mirror main not updated from its canonical remote: boom" in rendered
    # An operator scanning for the well-known STALE/WRONG banners must not
    # mistake "not checked" for either -- it needs its own distinct signal.
    assert "STALE BASE REF" not in rendered
    assert "WRONG BASE REF" not in rendered


def test_describe_base_ref_status_reads_checked_from_the_recorded_state(tmp_path, monkeypatch):
    # describe_base_ref_status itself never talks to the mirror or GitHub --
    # `checked` comes from dispatch.read_base_ref_check_record, host-local
    # state ensure_base_ref_current records each drain cycle. This proves the
    # wiring end to end: a local read that is perfectly healthy still renders
    # not_checked when nothing has ever recorded confirming it.
    repo, _local_rev, _upstream_rev = build_repo_behind_by(tmp_path, 0)
    monkeypatch.setenv(
        "FACTORY_BASE_REF_CHECK_STATE_PATH", str(tmp_path / "base-ref-check-state.json")
    )

    never_recorded = schedule_status.describe_base_ref_status(
        dispatch.Config(repo_root=repo, base_ref="main")
    )
    assert never_recorded.checked is False
    assert never_recorded.state == "not_checked"

    dispatch._write_base_ref_check_record(checked=True, reason="")
    recorded_healthy = schedule_status.describe_base_ref_status(
        dispatch.Config(repo_root=repo, base_ref="main")
    )
    assert recorded_healthy.checked is True
    assert recorded_healthy.state == "healthy"


def test_status_output_omits_base_ref_block_when_not_supplied():
    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        now=NOW,
    )

    assert "base_ref_stale" not in rendered


def test_status_output_reports_could_not_determine_distinctly():
    status = BaseRefLiveStatus.could_not_determine("main", "no upstream configured")

    rendered = schedule_status.render_schedule_status(
        quiet_status(),
        namespace="dev",
        base_ref_status=status,
        now=NOW,
    )

    assert "base_ref_stale: could-not-determine" in rendered
    assert "base_ref_stale: false" not in rendered
    assert "STALE BASE REF" not in rendered


# ---------------------------------------------------------------------------
# factory_health_notifications -- one factory-wide condition, not N per-bead
# ---------------------------------------------------------------------------


def test_stale_base_ref_produces_one_urgent_notification_via_cluster_health():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        commits_behind=4,
        local_only=0,
    )

    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        base_ref_status=status,
        now=NOW,
    )

    matching = [n for n in notifications if n.source == "base-ref-stale"]
    assert len(matching) == 1
    assert matching[0].severity == "urgent"
    assert "a" * 40 in matching[0].detail
    assert "b" * 40 in matching[0].detail
    assert "4" in matching[0].detail

    payload = cluster_health.build_payload(notifications)
    assert "base-ref-stale" in payload


def test_a_current_base_ref_produces_no_notification():
    status = BaseRefLiveStatus(
        base_ref="main",
        local_rev="c" * 40,
        upstream_ref="origin/main",
        upstream_rev="c" * 40,
        commits_behind=0,
        local_only=0,
    )

    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        base_ref_status=status,
        now=NOW,
    )

    assert [n for n in notifications if n.source == "base-ref-stale"] == []


def test_no_base_ref_status_supplied_produces_no_notification_and_no_crash():
    notifications = schedule_status.factory_health_notifications(
        quiet_status(),
        interval_seconds=INTERVAL_SECONDS,
        now=NOW,
    )

    assert notifications == []
