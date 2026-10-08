"""Freshness is not ancestry, and the ancestor guard was never asked for it.

2026-08-29: PR #583 was cut from a checkout nine commits behind main
(`76da5019` while main was at `87b45db`).
`worker_revision.ensure_checkout_is_on_main_ancestor` passed -- `76da5019`
IS an ancestor of main, exactly as designed -- and nothing else on the path
asked whether the checkout was current, so the PR's declared-verification
evidence reached the release gate with no revision recorded anywhere, and no
way to tell a stale run from a current one without computing `git
merge-base` by hand.

These tests assert the freshness check this incident was missing: a valid
ancestor that is not main's tip is detected as stale, a checkout at main's
tip is not, and the existing ancestor guard still refuses a diverged
checkout unchanged. No substrate, no network, no Temporal server -- the
first two are driven entirely by an injected git runner; the third uses a
real local-only git repository, the same shape
test_worker_revision_drift.py's own ancestor-guard tests already use.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_revision  # noqa: E402
from worker_revision import (  # noqa: E402
    CheckoutFreshness,
    WorkerCheckoutDriftError,
    WorkerCheckoutStaleError,
)

OLD_REVISION = "aaaaaaa"
MAIN_REVISION = "ccccccc"


def fake_git_runner_for_freshness(*, ancestor_returncode: int, commits_behind: str = "0"):
    """A git runner that answers exactly the three calls freshness checking makes."""

    def runner(cmd, cwd=None, check=True, timeout=None):
        if cmd[1] == "rev-parse" and cmd[-1] == "HEAD":
            return SimpleNamespace(stdout=f"{OLD_REVISION}\n", stderr="", returncode=0)
        if cmd[1] == "rev-parse":
            return SimpleNamespace(stdout=f"{MAIN_REVISION}\n", stderr="", returncode=0)
        if cmd[1] == "merge-base":
            return SimpleNamespace(stdout="", stderr="", returncode=ancestor_returncode)
        assert cmd[1] == "rev-list"
        return SimpleNamespace(stdout=f"{commits_behind}\n", stderr="", returncode=0)

    return runner


# ---------------------------------------------------------------------------
# check_checkout_freshness -- pure, injected git runner, no real repository
# ---------------------------------------------------------------------------


def test_old_but_valid_ancestor_of_main_is_detected_as_stale(tmp_path):
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=0, commits_behind="9")

    freshness = worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert freshness.is_ancestor is True
    assert freshness.commits_behind == 9
    assert freshness.stale is True
    assert freshness.revision == OLD_REVISION
    assert freshness.main_revision == MAIN_REVISION
    assert freshness.error == ""


def test_checkout_at_mains_tip_is_not_reported_stale(tmp_path):
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=0, commits_behind="0")

    freshness = worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert freshness.is_ancestor is True
    assert freshness.commits_behind == 0
    assert freshness.stale is False


def test_checkout_at_exactly_mains_revision_is_not_stale_without_a_rev_list_call(tmp_path):
    calls = []

    def git_runner(cmd, cwd=None, check=True, timeout=None):
        calls.append(cmd)
        # Both HEAD and main resolve to the same revision -- no daylight to measure.
        return SimpleNamespace(stdout=f"{MAIN_REVISION}\n", stderr="", returncode=0)

    freshness = worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert freshness.commits_behind == 0
    assert freshness.stale is False
    assert not any(cmd[1] == "rev-list" for cmd in calls)


def test_diverged_checkout_reports_not_ancestor_and_never_reads_as_stale(tmp_path):
    # A diverged checkout is ensure_checkout_is_on_main_ancestor's refusal to
    # make, not this function's -- "not stale" here must never be mistaken
    # for "safe."
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=1)

    freshness = worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert freshness.is_ancestor is False
    assert freshness.commits_behind is None
    assert freshness.stale is False


def test_freshness_check_issues_no_fetch_and_no_network(tmp_path):
    calls = []

    def git_runner(cmd, cwd=None, check=True, timeout=None):
        calls.append(cmd)
        return fake_git_runner_for_freshness(ancestor_returncode=0, commits_behind="4")(
            cmd, cwd=cwd, check=check, timeout=timeout
        )

    worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert not any(cmd[1] == "fetch" for cmd in calls)


def test_freshness_check_reports_error_rather_than_raising_on_git_failure(tmp_path):
    def exploding_git_runner(cmd, cwd=None, check=True, timeout=None):
        raise RuntimeError("git not on PATH")

    freshness = worker_revision.check_checkout_freshness(
        repo_root=tmp_path, main_ref="main", git_runner=exploding_git_runner
    )

    assert freshness.error != ""
    assert freshness.is_ancestor is None
    assert freshness.stale is False


# ---------------------------------------------------------------------------
# ensure_checkout_is_current_with_main -- the hard gate
# ---------------------------------------------------------------------------


def test_ensure_checkout_is_current_raises_naming_both_revisions_and_the_distance(tmp_path):
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=0, commits_behind="9")

    with pytest.raises(WorkerCheckoutStaleError) as excinfo:
        worker_revision.ensure_checkout_is_current_with_main(
            repo_root=tmp_path, main_ref="main", git_runner=git_runner
        )

    message = str(excinfo.value)
    assert OLD_REVISION in message
    assert MAIN_REVISION in message
    assert "9 commit" in message


def test_ensure_checkout_is_current_returns_quietly_at_mains_tip(tmp_path):
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=0, commits_behind="0")

    freshness = worker_revision.ensure_checkout_is_current_with_main(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert isinstance(freshness, CheckoutFreshness)
    assert freshness.stale is False


def test_ensure_checkout_is_current_does_not_raise_for_a_diverged_checkout(tmp_path):
    # Not this function's refusal to make -- ensure_checkout_is_on_main_ancestor
    # owns that, unchanged, tested below.
    git_runner = fake_git_runner_for_freshness(ancestor_returncode=1)

    freshness = worker_revision.ensure_checkout_is_current_with_main(
        repo_root=tmp_path, main_ref="main", git_runner=git_runner
    )

    assert freshness.is_ancestor is False


# ---------------------------------------------------------------------------
# ensure_checkout_is_on_main_ancestor -- unchanged: a diverged checkout is
# still refused after this change. Real local-only git repository, matching
# test_worker_revision_drift.py's own ancestor-guard tests.
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


def test_diverged_checkout_is_still_refused_by_the_existing_ancestor_guard(tmp_path):
    repo = build_repo_on_branch(tmp_path, "fix/drop-two-dead-imports")

    with pytest.raises(WorkerCheckoutDriftError) as excinfo:
        worker_revision.ensure_checkout_is_on_main_ancestor(repo_root=repo)

    message = str(excinfo.value)
    assert "fix/drop-two-dead-imports" in message
    assert "not an ancestor" in message

    # And the new freshness gate does not relax that refusal into a pass:
    # a diverged checkout must never come out of the freshness gate as
    # merely "not stale."
    freshness = worker_revision.ensure_checkout_is_current_with_main(repo_root=repo)
    assert freshness.is_ancestor is False
    assert freshness.stale is False


def test_old_but_valid_ancestor_on_a_real_repo_is_refused_by_the_freshness_gate(tmp_path):
    # The exact shape ensure_checkout_is_on_main_ancestor is designed to let
    # through (see test_ensure_checkout_passes_for_a_detached_head_at_an_old_ancestor_of_main
    # in test_worker_revision_drift.py) must be refused here instead.
    repo = build_repo_on_branch(tmp_path, "main")
    first_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "README.md").write_text("two\n")
    git(repo, "commit", "-am", "second commit")
    git(repo, "checkout", first_commit)

    revision = worker_revision.ensure_checkout_is_on_main_ancestor(repo_root=repo)
    assert revision == first_commit

    with pytest.raises(WorkerCheckoutStaleError) as excinfo:
        worker_revision.ensure_checkout_is_current_with_main(repo_root=repo)

    message = str(excinfo.value)
    assert first_commit in message
    assert "1 commit" in message
