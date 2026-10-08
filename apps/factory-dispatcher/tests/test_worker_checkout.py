"""Tests for advancing the factory worker's own controlled checkout.

2026-08-25: the launchd worker loaded whatever branch the operator's shared
working tree happened to have checked out at restart -- once a PR branch
tip, observed by OPS-11's drift report as `worker_revision_drift: true`.
`worker_checkout.advance` is this fix's writable half: the one documented
command that moves the worker's own checkout, refusing before writing
anything when the target revision is not on main.

These tests use real, local-only git repositories under tmp_path -- no
network, no cluster, no substrate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker_checkout  # noqa: E402
import worker_revision  # noqa: E402


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [worker_checkout.GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def build_source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    git(tmp_path, "init", str(repo))
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("one\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# advance() -- the one documented command
# ---------------------------------------------------------------------------


def test_advance_clones_a_fresh_checkout_at_mains_tip_by_default(tmp_path):
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"

    target = worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    main_rev = git(repo, "rev-parse", "main").stdout.strip()
    assert target == main_rev
    assert (checkout_root / ".git").exists()
    assert git(checkout_root, "rev-parse", "HEAD").stdout.strip() == main_rev
    assert worker_checkout.is_factory_controlled_checkout(checkout_root)


def test_advance_refuses_a_revision_that_is_not_an_ancestor_of_main(tmp_path):
    repo = build_source_repo(tmp_path)
    git(repo, "checkout", "-b", "feature/not-merged")
    (repo / "note.txt").write_text("side quest\n")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-m", "unmerged work")
    off_main = git(repo, "rev-parse", "feature/not-merged").stdout.strip()
    checkout_root = tmp_path / "checkout"

    with pytest.raises(worker_checkout.WorkerCheckoutError) as excinfo:
        worker_checkout.advance(off_main, checkout_root=checkout_root, source_repo_root=repo)

    assert off_main in str(excinfo.value)
    assert "not an ancestor" in str(excinfo.value)
    assert not checkout_root.exists()


def test_advance_updates_an_existing_checkout_to_a_new_main_commit(tmp_path):
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"
    worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    (repo / "README.md").write_text("two\n")
    git(repo, "commit", "-am", "advance main")
    new_main = git(repo, "rev-parse", "main").stdout.strip()

    target = worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    assert target == new_main
    assert git(checkout_root, "rev-parse", "HEAD").stdout.strip() == new_main


def test_advance_to_an_explicit_older_revision_still_on_main(tmp_path):
    repo = build_source_repo(tmp_path)
    first_commit = git(repo, "rev-parse", "main").stdout.strip()
    (repo / "README.md").write_text("two\n")
    git(repo, "commit", "-am", "second commit")
    checkout_root = tmp_path / "checkout"

    target = worker_checkout.advance(
        first_commit, checkout_root=checkout_root, source_repo_root=repo
    )

    assert target == first_commit
    assert git(checkout_root, "rev-parse", "HEAD").stdout.strip() == first_commit


def test_advance_does_not_follow_whatever_branch_the_source_working_tree_has_checked_out(
    tmp_path,
):
    # The bug this whole change exists to fix, reproduced against advance()
    # itself: an ordinary local `git clone` follows the source's checked-out
    # HEAD, not necessarily main. advance() must clone `main_ref` by name.
    repo = build_source_repo(tmp_path)
    git(repo, "checkout", "-b", "operator-is-mid-edit")
    (repo / "note.txt").write_text("mid-edit\n")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-m", "operator's in-progress lint fix")
    checkout_root = tmp_path / "checkout"

    target = worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    main_rev = git(repo, "rev-parse", "main").stdout.strip()
    assert target == main_rev
    assert git(checkout_root, "rev-parse", "HEAD").stdout.strip() == main_rev


def test_is_factory_controlled_checkout_is_false_for_an_ordinary_clone(tmp_path):
    repo = build_source_repo(tmp_path)
    plain_clone = tmp_path / "plain-clone"
    git(tmp_path, "clone", str(repo), str(plain_clone))

    assert not worker_checkout.is_factory_controlled_checkout(plain_clone)


# ---------------------------------------------------------------------------
# advance() refuses the self-referential invocation (2026-09-02)
#
# The footgun from the 2026-09-02 incident: an operator ran `worker_checkout.py
# advance` from inside the worker checkout itself, so --source-repo-root's
# Path(__file__)-derived default resolved to the checkout being advanced. The
# command fetched main from itself and reported success having changed
# nothing. advance() must refuse before any git call, not silently no-op.
# ---------------------------------------------------------------------------


def test_advance_refuses_when_source_repo_root_is_the_checkout_being_advanced(tmp_path):
    checkout_root = tmp_path / "checkout"
    checkout_root.mkdir()

    with pytest.raises(worker_checkout.WorkerCheckoutError) as excinfo:
        worker_checkout.advance(checkout_root=checkout_root, source_repo_root=checkout_root)

    message = str(excinfo.value)
    assert "refusing to advance" in message
    assert "self-referential" in message.lower() or "same path" in message.lower()


def test_advance_refuses_the_self_referential_invocation_via_equivalent_but_unnormalized_paths(
    tmp_path,
):
    # The bug as it actually happened: --source-repo-root defaults to
    # Path(__file__).resolve().parents[2], which resolves to the same
    # directory as checkout_root even though the two path *strings* the
    # caller passed in were never compared for equality anywhere.
    checkout_root = tmp_path / "checkout"
    checkout_root.mkdir()
    equivalent_but_unresolved = checkout_root / "sub" / ".."

    with pytest.raises(worker_checkout.WorkerCheckoutError):
        worker_checkout.advance(
            checkout_root=checkout_root, source_repo_root=equivalent_but_unresolved
        )


def test_advance_refuses_self_referential_invocation_before_the_checkout_exists(tmp_path):
    # Path.resolve() must not require either side to exist yet -- a checkout's
    # very first advance() call is exactly the not-yet-cloned case.
    not_yet_cloned = tmp_path / "checkout"

    with pytest.raises(worker_checkout.WorkerCheckoutError):
        worker_checkout.advance(checkout_root=not_yet_cloned, source_repo_root=not_yet_cloned)

    assert not not_yet_cloned.exists()


def test_advance_does_not_refuse_two_genuinely_different_checkouts(tmp_path):
    # The guard must not become a second, over-broad refusal: two distinct
    # directories that merely share a common ancestor must still advance.
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"

    target = worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    assert worker_checkout.is_factory_controlled_checkout(checkout_root)
    assert target == git(repo, "rev-parse", "main").stdout.strip()


# ---------------------------------------------------------------------------
# declared_source_repo_root() -- reads the declared source back from `origin`,
# for a caller (running from inside checkout_root) that cannot derive it from
# its own __file__ without hitting the self-referential case above.
# ---------------------------------------------------------------------------


def test_declared_source_repo_root_reads_back_the_origin_a_clone_recorded(tmp_path):
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"
    worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)

    declared = worker_checkout.declared_source_repo_root(checkout_root)

    assert declared == repo


# ---------------------------------------------------------------------------
# sync_source_mirror_main() -- OPS-59's outer hop: the source mirror's own
# `main` fetched from its canonical remote (GitHub in production), never
# touching the mirror's working tree or whatever branch the operator has
# checked out.
# ---------------------------------------------------------------------------


def build_mirror_tracking_canonical(tmp_path: Path) -> tuple[Path, Path]:
    """A bare "canonical remote" plus a mirror cloned from it -- standing in
    for GitHub and an operator's interactive working copy."""
    canonical = tmp_path / "canonical.git"
    git(tmp_path, "init", "--bare", str(canonical))
    # The bare canonical's HEAD must name main explicitly: a bare init leaves
    # HEAD at the runner git's default branch, and a clone of a bare repo
    # whose HEAD dangles checks out no branch at all on older gits (the CI
    # runner), while newer gits guess -- which is why this failed only in CI.
    git(canonical, "symbolic-ref", "HEAD", "refs/heads/main")

    seed = tmp_path / "seed"
    # -b main: the CI runner's git has no init.defaultBranch configured, so a
    # bare `git init` creates `master` there and every rev-parse of `main`
    # fails; the branch this chain synchronizes must exist by construction.
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "factory@example.test")
    git(seed, "config", "user.name", "Factory Test")
    git(seed, "checkout", "-b", "main")
    (seed / "README.md").write_text("one\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(canonical))
    git(seed, "push", "-u", "origin", "main")

    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", str(canonical), str(mirror))
    git(mirror, "config", "user.email", "factory@example.test")
    git(mirror, "config", "user.name", "Factory Test")
    # The constraint OPS-59 exists to respect: an operator's working copy is
    # not left on main day to day -- it was on a feature branch in the
    # 2026-09-04 incident.
    git(mirror, "checkout", "-b", "operator-feature-branch")
    return mirror, canonical


def push_new_commit_to_canonical(tmp_path: Path, canonical: Path) -> str:
    updater = tmp_path / "canonical-updater"
    git(tmp_path, "clone", str(canonical), str(updater))
    git(updater, "checkout", "main")
    git(updater, "config", "user.email", "factory@example.test")
    git(updater, "config", "user.name", "Factory Test")
    (updater / "README.md").write_text("two\n")
    git(updater, "commit", "-am", "merged to canonical")
    git(updater, "push", "origin", "main")
    return git(updater, "rev-parse", "main").stdout.strip()


def test_sync_source_mirror_main_fetches_a_fast_forward_and_moves_only_the_ref(tmp_path):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    new_tip = push_new_commit_to_canonical(tmp_path, canonical)
    checked_out_before = git(mirror, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    result = worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert result.updated is True
    assert result.revision == new_tip
    assert result.error is None
    assert git(mirror, "rev-parse", "main").stdout.strip() == new_tip
    # The operator's checked-out branch and working tree are untouched.
    assert git(mirror, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == checked_out_before
    assert git(mirror, "status", "--porcelain").stdout.strip() == ""


def test_sync_source_mirror_main_is_a_noop_when_already_current(tmp_path):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    current = git(mirror, "rev-parse", "main").stdout.strip()

    result = worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert result.updated is False
    assert result.error is None
    assert result.revision == current


def test_sync_source_mirror_main_is_ref_only_no_checkout_merge_or_reset(tmp_path, monkeypatch):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    push_new_commit_to_canonical(tmp_path, canonical)

    calls: list[list[str]] = []
    real_run = worker_checkout.run

    def spy(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(worker_checkout, "run", spy)

    worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    forbidden = {"checkout", "merge", "reset"}
    assert not any(set(c) & forbidden for c in calls), calls
    verbs = {c[1] for c in calls}
    assert verbs <= {"rev-parse", "fetch", "rev-list", "branch"}


def test_sync_source_mirror_main_refuses_local_only_commits(tmp_path):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    push_new_commit_to_canonical(tmp_path, canonical)

    git(mirror, "checkout", "main")
    (mirror / "local.txt").write_text("committed straight to main in the mirror\n")
    git(mirror, "add", "local.txt")
    git(mirror, "commit", "-m", "local-only commit on the mirror's main")
    local_only_rev = git(mirror, "rev-parse", "main").stdout.strip()

    with pytest.raises(worker_checkout.SourceMirrorLocalCommitsError) as excinfo:
        worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert excinfo.value.local_only == 1
    assert excinfo.value.local_rev == local_only_rev
    # Refused before writing anything -- main is untouched.
    assert git(mirror, "rev-parse", "main").stdout.strip() == local_only_rev


def test_sync_source_mirror_main_degrades_gracefully_when_remote_is_unreachable(tmp_path):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    git(mirror, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    before = git(mirror, "rev-parse", "main").stdout.strip()

    result = worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert result.updated is False
    assert result.error is not None
    assert result.revision == before
    assert git(mirror, "rev-parse", "main").stdout.strip() == before


def test_sync_source_mirror_main_leaves_main_alone_when_it_is_checked_out(tmp_path):
    mirror, canonical = build_mirror_tracking_canonical(tmp_path)
    new_tip = push_new_commit_to_canonical(tmp_path, canonical)
    git(mirror, "checkout", "main")
    before = git(mirror, "rev-parse", "main").stdout.strip()

    result = worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert result.updated is False
    assert result.error is not None
    assert git(mirror, "rev-parse", "main").stdout.strip() == before
    assert before != new_tip
    assert git(mirror, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"


def test_sync_source_mirror_main_creates_main_when_the_mirror_never_had_it_locally(tmp_path):
    canonical = tmp_path / "canonical.git"
    git(tmp_path, "init", "--bare", str(canonical))
    # The bare canonical's HEAD must name main explicitly: a bare init leaves
    # HEAD at the runner git's default branch, and a clone of a bare repo
    # whose HEAD dangles checks out no branch at all on older gits (the CI
    # runner), while newer gits guess -- which is why this failed only in CI.
    git(canonical, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = tmp_path / "seed"
    # -b main: the CI runner's git has no init.defaultBranch configured, so a
    # bare `git init` creates `master` there and every rev-parse of `main`
    # fails; the branch this chain synchronizes must exist by construction.
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "factory@example.test")
    git(seed, "config", "user.name", "Factory Test")
    git(seed, "checkout", "-b", "main")
    (seed / "README.md").write_text("one\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "initial")
    git(seed, "checkout", "-b", "other")
    git(seed, "remote", "add", "origin", str(canonical))
    git(seed, "push", "origin", "main")

    # A single-branch clone of a non-main branch never creates a local `main`.
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "--branch", "other", "--single-branch", str(seed), str(mirror))
    git(mirror, "remote", "set-url", "origin", str(canonical))
    canonical_tip = git(seed, "rev-parse", "main").stdout.strip()

    result = worker_checkout.sync_source_mirror_main(source_repo_root=mirror)

    assert result.updated is True
    assert result.revision == canonical_tip
    assert git(mirror, "rev-parse", "main").stdout.strip() == canonical_tip


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_advances_using_env_checkout_root_and_prints_target(
    tmp_path, monkeypatch, capsys
):
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"
    monkeypatch.setenv(worker_checkout.CHECKOUT_DIR_ENV, str(checkout_root))

    exit_code = worker_checkout.main(["advance", "--source-repo-root", str(repo)])

    assert exit_code == 0
    assert worker_checkout.is_factory_controlled_checkout(checkout_root)
    assert str(checkout_root) in capsys.readouterr().out


def test_main_exits_nonzero_and_prints_to_stderr_on_refusal(tmp_path, capsys):
    repo = build_source_repo(tmp_path)
    git(repo, "checkout", "-b", "feature/x")
    (repo / "note.txt").write_text("x\n")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-m", "off main")
    off_main = git(repo, "rev-parse", "feature/x").stdout.strip()
    checkout_root = tmp_path / "checkout"

    exit_code = worker_checkout.main(
        [
            "advance",
            off_main,
            "--source-repo-root",
            str(repo),
            "--checkout-root",
            str(checkout_root),
        ]
    )

    assert exit_code == 1
    assert "refusing" in capsys.readouterr().err
    assert not checkout_root.exists()


# ---------------------------------------------------------------------------
# AC4: OPS-11's drift reporting must keep working against the new arrangement
# ---------------------------------------------------------------------------


def test_ops11_drift_reporting_works_against_a_factory_controlled_checkout_started_current(
    tmp_path,
):
    repo = build_source_repo(tmp_path)
    checkout_root = tmp_path / "checkout"
    worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)
    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(repo_root=checkout_root, state_path=state_path)

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path, repo_root=repo
    )

    assert status.error == ""
    assert status.drifted is False


def test_ops11_drift_reporting_flags_a_worker_checkout_advanced_off_main(tmp_path):
    # This fix's startup guard (worker_revision.ensure_checkout_is_on_main_ancestor)
    # is defense in depth, not a replacement for OPS-11's reporting: even a
    # checkout that ends up off main -- an operator bypassing advance() by
    # fetching and checking out unmerged work directly inside the controlled
    # checkout, the same shape as the original 2026-08-25 incident -- must
    # still be caught by the existing drift check schedule_status.py relies
    # on. describe_worker_revision_drift reads repo_root=repo (the source
    # tree, standing in for wherever an operator runs schedule_status.py
    # from) rather than checkout_root, so the compared revision must be an
    # object repo actually has -- exactly the PR-branch-tip case OPS-11 was
    # built to catch.
    repo = build_source_repo(tmp_path)
    git(repo, "checkout", "-b", "manual-bypass")
    (repo / "note.txt").write_text("bypass\n")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-m", "unmerged work")
    off_main = git(repo, "rev-parse", "manual-bypass").stdout.strip()
    git(repo, "checkout", "main")

    checkout_root = tmp_path / "checkout"
    worker_checkout.advance(checkout_root=checkout_root, source_repo_root=repo)
    git(checkout_root, "fetch", str(repo), "manual-bypass")
    git(checkout_root, "checkout", "--detach", off_main)

    state_path = tmp_path / "worker-revision.json"
    worker_revision.record_worker_start(repo_root=checkout_root, state_path=state_path)

    status = worker_revision.describe_worker_revision_drift(
        state_path=state_path, repo_root=repo
    )

    assert status.error == ""
    assert status.drifted is True
