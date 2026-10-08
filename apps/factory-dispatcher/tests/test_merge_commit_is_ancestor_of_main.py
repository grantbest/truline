"""Tests for OPS-115: the post-merge ancestry check must ask GitHub, not
whatever `origin` happens to resolve to in `cfg.repo_root`.

`merge_commit_is_ancestor_of_main` (dispatch.py) used to `git fetch origin
<base_ref>` in `cfg.repo_root`. Inside a dispatch clone that is harmless --
`make_clone` (dispatch.py:1261) rewrites `origin` to `cfg.remote` right after
cloning, so the two name the same URL. But `cfg.repo_root` in production is
the worker checkout under launchd, whose `origin` is the operator's local
source mirror (OPS-59's two-hop topology), not GitHub -- and the mirror is
only as fresh as its own last pull. The fix fetches `cfg.remote` by URL
instead, so the check answers against the real trunk regardless of which
remote name the checkout carries.

These tests use real, local-only git repositories under tmp_path, reusing
this suite's own github/mirror/checkout builder (`create_chain_repo_mirror_
canonical` in test_dispatch.py) so the topology under test is the same one
that incident measurement was taken against -- no network, no substrate.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from test_dispatch import (  # noqa: E402
    create_chain_repo_mirror_canonical,
    git,
    push_new_commit_to_canonical,
)


def test_ancestor_check_answers_against_github_even_when_mirror_and_checkout_are_stale(
    tmp_path,
):
    # repo = the worker-checkout role: origin is `mirror`, not `canonical`.
    # mirror = the operator's local working copy: origin is `canonical`.
    # canonical = the bare repo standing in for GitHub.
    repo, mirror, canonical = create_chain_repo_mirror_canonical(tmp_path)
    new_tip = push_new_commit_to_canonical(tmp_path, canonical)

    # Left exactly as create_chain_repo_mirror_canonical built them: mirror on
    # its own feature branch, repo cloned from mirror before canonical's new
    # commit existed. Neither has seen `new_tip`.
    assert git(mirror, "rev-parse", "main").stdout.strip() != new_tip

    cfg = dispatch.Config(remote=str(canonical), repo_root=repo, base_ref="main")

    assert dispatch.merge_commit_is_ancestor_of_main(new_tip, cfg) is True

    # A commit that was never pushed to canonical (or anywhere else) must
    # still come back False -- fetching cfg.remote must not make the check
    # more permissive, only more correct about which remote it asks.
    git(repo, "config", "user.email", "factory@example.test")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "checkout", "-b", "local-only")
    (repo / "local.txt").write_text("never pushed\n")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-m", "local only, on no remote")
    local_only_commit = git(repo, "rev-parse", "HEAD").stdout.strip()

    assert dispatch.merge_commit_is_ancestor_of_main(local_only_commit, cfg) is False


def test_dispatch_clone_path_is_unchanged(tmp_path):
    """A dispatch clone's origin is rewritten to cfg.remote at clone time
    (make_clone, dispatch.py:1261), so origin and cfg.remote already name the
    same URL there -- fetching cfg.remote directly must be behavior-identical
    to the old fetch-origin call for this shape."""
    canonical = tmp_path / "canonical.git"
    git(tmp_path, "init", "--bare", str(canonical))
    git(canonical, "symbolic-ref", "HEAD", "refs/heads/main")

    seed = tmp_path / "seed"
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.email", "factory@example.test")
    git(seed, "config", "user.name", "Factory Test")
    (seed / "README.md").write_text("one\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(canonical))
    git(seed, "push", "-u", "origin", "main")

    dispatch_clone = tmp_path / "dispatch-clone"
    git(tmp_path, "clone", str(canonical), str(dispatch_clone))
    # make_clone's own rewrite (dispatch.py:1261).
    git(dispatch_clone, "remote", "set-url", "origin", str(canonical))

    new_tip = push_new_commit_to_canonical(tmp_path, canonical)

    cfg = dispatch.Config(remote=str(canonical), repo_root=dispatch_clone, base_ref="main")

    assert dispatch.merge_commit_is_ancestor_of_main(new_tip, cfg) is True

    git(dispatch_clone, "config", "user.email", "factory@example.test")
    git(dispatch_clone, "config", "user.name", "Factory Test")
    git(dispatch_clone, "checkout", "-b", "local-only")
    (dispatch_clone / "local.txt").write_text("never pushed\n")
    git(dispatch_clone, "add", "local.txt")
    git(dispatch_clone, "commit", "-m", "local only, on no remote")
    local_only_commit = git(dispatch_clone, "rev-parse", "HEAD").stdout.strip()

    assert dispatch.merge_commit_is_ancestor_of_main(local_only_commit, cfg) is False


def test_ancestor_check_leaves_fetch_head_to_the_tracking_ref_staleness_guard(tmp_path):
    """The #802 gate's F1: a URL fetch writes FETCH_HEAD but updates no
    remote-tracking ref, and _refresh_stale_tracking_ref (dispatch.py) reads
    FETCH_HEAD's age as its proxy for "origin/main was just refreshed". On the
    pass after every merge, reconcile runs the ancestry check before the
    attempt clones, so a FETCH_HEAD-writing check would make the inner hop
    skip its fetch and clone from a base missing the merge (the FA-S29 class).
    The check must therefore not touch FETCH_HEAD: after it runs,
    ensure_base_ref_current must still learn the new tip through the mirror."""
    repo, mirror, canonical = create_chain_repo_mirror_canonical(tmp_path)
    # The checkout fetched its mirror once, earlier -- a real FETCH_HEAD exists,
    # written before anything advanced (the fixture's clone leaves none, and a
    # guard on a missing file exercises nothing: the second #802 gate, F1).
    git(repo, "fetch", "origin", "main")
    fetch_head = repo / ".git" / "FETCH_HEAD"
    assert fetch_head.exists()
    new_tip = push_new_commit_to_canonical(tmp_path, canonical)
    # The operator pulled: the mirror's main is current (its feature branch
    # stays checked out, as the fixture built it), the worker checkout is not.
    git(mirror, "fetch", "origin", "main:main")
    assert git(mirror, "rev-parse", "main").stdout.strip() == new_tip
    assert git(repo, "rev-parse", "origin/main").stdout.strip() != new_tip
    # ...and that fetch was long enough ago that the staleness guard must act.
    stale_at = time.time() - dispatch.BASE_REF_TRACKING_STALENESS_BOUND_S - 60
    os.utime(fetch_head, (stale_at, stale_at))

    cfg = dispatch.Config(remote=str(canonical), repo_root=repo, base_ref="main")
    assert dispatch.merge_commit_is_ancestor_of_main(new_tip, cfg) is True

    # Reconcile ran first; now the attempt asks whether the base is current.
    status = dispatch.ensure_base_ref_current(cfg)
    assert status.local_rev == new_tip
    assert git(repo, "rev-parse", "main").stdout.strip() == new_tip
