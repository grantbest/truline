"""AC-2 (2026-09-17, PR #887, bead 766f103e): a scope grader that measures
from the clone's own HEAD at clone time mis-certifies a PR whose HEAD was
itself already off trunk when the clone was made.

``scope_activity`` used to grade ``state["paths"]`` (the diff since HEAD) or,
when a preserved baseline had been applied, the diff since ``verified_revision``
-- the clone's HEAD right after cloning. Both are a diff since *something the
clone already carried*, not since the true merge-base with trunk. On #887 the
operator repo's ``main`` had been force-moved onto a revision carrying other
pull requests' unmerged commits, so the clone's own HEAD at clone time was
already several commits off real trunk. Every path between real trunk and
that HEAD sat below the diff base and was structurally invisible to the old
check, while GitHub attributed all of it to the PR.

This file builds that exact topology from three separate local-only git
repositories (AC-4: no ambient ``main``/``origin/main``/``HEAD^`` -- the
trunk reference is data the fixture builds and hands to ``dispatch.Config``
explicitly, never discovered from whatever repository happens to be checked
out) and drives the real ``dispatch_steps.scope_activity`` and
``guards.check_scope`` against it, no mocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402
from test_dispatch import git, task_with_id  # noqa: E402
from _clone_fixtures import recorded  # noqa: E402


def _build_887_topology(tmp_path: Path):
    """(clone, cfg, verified_revision, worker_paths) for the #887 shape.

    trunk_remote: a bare repo standing in for GitHub's real ``main`` --
    never advanced past the plain trunk commit.

    operator_repo: stands in for the operator checkout (``cfg.repo_root``)
    whose local ``main`` gets force-advanced onto a commit ON TOP of trunk
    that carries a file no PR declared scope for -- the contamination.
    ``make_clone`` clones from THIS repo, not from ``trunk_remote``.

    clone: made with the exact same ``git clone --branch <base_ref>
    --single-branch <repo_root> <dest>`` shape ``dispatch.make_clone`` uses,
    so its HEAD at clone time is the contaminated commit, not trunk.
    """
    trunk_remote = tmp_path / "trunk-remote.git"
    git(tmp_path, "init", "--bare", str(trunk_remote))
    git(trunk_remote, "symbolic-ref", "HEAD", "refs/heads/main")

    seed = tmp_path / "seed"
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "factory@example.test")
    git(seed, "config", "user.name", "Factory Test")
    (seed / "README.md").write_text("trunk\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "trunk commit")
    git(seed, "remote", "add", "origin", str(trunk_remote))
    git(seed, "push", "-u", "origin", "main")
    trunk_sha = git(seed, "rev-parse", "HEAD").stdout.strip()

    operator_repo = tmp_path / "operator-repo"
    git(tmp_path, "clone", str(trunk_remote), str(operator_repo))
    git(operator_repo, "checkout", "main")
    git(operator_repo, "config", "user.email", "factory@example.test")
    git(operator_repo, "config", "user.name", "Factory Test")
    recorded(operator_repo)
    contaminant_dir = operator_repo / "apps" / "factory-dispatcher" / "tasks"
    contaminant_dir.mkdir(parents=True)
    (contaminant_dir / "contaminant.json").write_text('{"unmerged": true}\n')
    git(operator_repo, "add", "apps/factory-dispatcher/tasks/contaminant.json")
    git(
        operator_repo,
        "commit",
        "-m",
        "another PR's unmerged commit, force-moved onto local main",
    )
    contaminated_base_sha = git(operator_repo, "rev-parse", "HEAD").stdout.strip()
    # The contamination is local to the operator checkout only -- GitHub's
    # real main (trunk_remote) never saw it. This is the load-bearing fact:
    # a scope grader that asks trunk_remote for the truth gets the right
    # answer; one that trusts the clone's own history does not.
    assert git(trunk_remote, "rev-parse", "main").stdout.strip() == trunk_sha
    assert contaminated_base_sha != trunk_sha

    clone = tmp_path / "clone"
    git(
        tmp_path,
        "clone",
        "--branch",
        "main",
        "--single-branch",
        str(operator_repo),
        str(clone),
    )
    git(clone, "config", "user.email", "factory@example.test")
    git(clone, "config", "user.name", "Factory Test")
    recorded(clone)
    verified_revision = git(clone, "rev-parse", "HEAD").stdout.strip()
    assert verified_revision == contaminated_base_sha

    allowed_dir = clone / "apps" / "factory-dispatcher"
    (allowed_dir / "guards.py").write_text("# worker's own change\n")
    (allowed_dir / "tests").mkdir(exist_ok=True)
    (allowed_dir / "tests" / "test_guards.py").write_text("# worker's own test\n")
    git(
        clone,
        "add",
        "apps/factory-dispatcher/guards.py",
        "apps/factory-dispatcher/tests/test_guards.py",
    )
    git(clone, "commit", "-m", "worker's own commit")

    worker_paths = dispatch.changed_paths(clone)
    assert worker_paths == [
        "apps/factory-dispatcher/guards.py",
        "apps/factory-dispatcher/tests/test_guards.py",
    ]

    cfg = dispatch.Config(
        repo="example/repo",
        remote=str(trunk_remote),
        repo_root=operator_repo,
        base_ref="main",
    )
    return clone, cfg, verified_revision, worker_paths


def test_scope_activity_catches_contamination_below_the_clones_own_head(tmp_path):
    """MEASURED (PR #887, bead 766f103e): a scope declaring
    ``['apps/factory-dispatcher/guards.py', 'apps/factory-dispatcher/tests/']``
    must refuse a run whose full merge-base diff also carries
    ``apps/factory-dispatcher/tasks/contaminant.json`` -- even though that
    file sits below the clone's own HEAD at clone time and the worker's own
    turn never touched it. Before this fix, ``scope_activity`` graded only
    the diff since the clone's HEAD (``state["paths"]``, unconditionally, or
    ``verified_revision`` when a preserved baseline applied -- neither of
    which reaches below the clone's own history) and returned a passing
    verdict over this exact shape.
    """
    clone, cfg, verified_revision, worker_paths = _build_887_topology(tmp_path)

    bead = task_with_id(
        "task-887",
        state="pending",
        scope={
            "paths": [
                "apps/factory-dispatcher/guards.py",
                "apps/factory-dispatcher/tests/",
            ],
            "forbidden_paths": [],
        },
    )
    state = {
        "cfg": dispatch_steps._cfg_to_state(cfg),
        "task": bead,
        "clone": str(clone),
        "verified_revision": verified_revision,
        "preserved_baseline_refs": [],
        "paths": worker_paths,
    }

    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch_steps.scope_activity(state)

    message = str(excinfo.value)
    assert "apps/factory-dispatcher/tasks/contaminant.json" in message


def test_merge_base_diff_paths_asks_the_real_remote_not_the_clones_own_history(
    tmp_path,
):
    """Same topology, driven at the ``_merge_base_diff_paths`` layer directly:
    the full diff it returns must include the contaminant even though the
    clone's own git history never diverges from it locally (there is no
    other ref inside the clone to diff against -- only the fetch from
    ``cfg.remote`` can surface it)."""
    clone, cfg, verified_revision, worker_paths = _build_887_topology(tmp_path)

    full_paths = dispatch_steps._merge_base_diff_paths(clone, cfg.remote, cfg.base_ref)

    assert full_paths == [
        "apps/factory-dispatcher/guards.py",
        "apps/factory-dispatcher/tasks/contaminant.json",
        "apps/factory-dispatcher/tests/test_guards.py",
    ]
    # And the old, narrower check reaches only the worker's own diff --
    # the exact gap this bead closes.
    assert dispatch.diff_paths_since(clone, verified_revision) == worker_paths


def test_merge_base_diff_paths_refuses_a_tampered_clone(tmp_path):
    """dev.finding 79db3113 part c2, AC-2: _merge_base_diff_paths' own fetch
    and merge-base calls (dispatch.git_in_clone) go through the live hook --
    a tamper to the clone must be refused there too, naming the path."""
    clone, cfg, _verified_revision, _worker_paths = _build_887_topology(tmp_path)
    config = clone / ".git" / "config"
    config.write_text(config.read_text() + "\n[test]\n\tplanted = 1\n")

    with pytest.raises(dispatch.CloneGitControlTampered) as excinfo:
        dispatch_steps._merge_base_diff_paths(clone, cfg.remote, cfg.base_ref)

    assert str(config) in str(excinfo.value)
