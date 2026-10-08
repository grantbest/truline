"""Nothing compares a deployed app's reported revision against main.

2026-09 measured instance: mcp-hub's build failed on every commit from 09-03 to 09-12 while
platform-mcp-prod/mcp-hub reported 1/1 Running, Available, zero restarts, serving an image nine
days stale. These tests exercise the read (kubectl exec into the pod, never the public,
Cloudflare-fronted URL) and the comparison (ancestor-of-FETCH_HEAD, restricted to the build's
own trigger pathspec) with git and kubectl both injected -- no live cluster, no network, and (for
the two real-git tests) no local `main` that has to be fast-forwarded first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deployed_revision as dr  # noqa: E402

NAMESPACE = "platform-mcp-prod"
DEPLOYMENT = "mcp-hub"
OLD_SHA = "aaaaaaa"
TIP_SHA = "ccccccc"


def fake_git_runner(calls, *, responses):
    """`responses` maps the git subcommand (cmd[1]) to a SimpleNamespace result, or a callable
    taking the full cmd list for cases that need to branch on arguments."""

    def runner(cmd, cwd=None, check=True, timeout=None):
        calls.append({"cmd": cmd, "cwd": cwd, "check": check, "timeout": timeout})
        key = cmd[1]
        resp = responses[key]
        if callable(resp):
            return resp(cmd)
        return resp

    return runner


def fake_kubectl_probe(sha="", error=""):
    def probe(namespace, deployment, *, timeout=None):
        assert namespace == NAMESPACE
        assert deployment == DEPLOYMENT
        return (sha if not error else None), error

    return probe


# ---------------------------------------------------------------------------
# the read -- kubectl exec, never the public URL, never collapsed to bare None
# ---------------------------------------------------------------------------


def test_kubectl_probe_returns_sha_on_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:4] == ["kubectl", "-n", NAMESPACE, "exec"]
        assert cmd[4] == f"deploy/{DEPLOYMENT}"
        assert kwargs["timeout"] == dr.KUBECTL_TIMEOUT_S
        return SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr="")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    sha, error = dr.default_kubectl_health_probe(NAMESPACE, DEPLOYMENT)
    assert sha == TIP_SHA
    assert error == ""


def test_kubectl_probe_reports_reason_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="pods \"mcp-hub\" not found")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    sha, error = dr.default_kubectl_health_probe(NAMESPACE, DEPLOYMENT)
    assert sha is None
    assert "not found" in error  # never collapsed to a bare None with no reason


def test_kubectl_probe_reports_reason_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    sha, error = dr.default_kubectl_health_probe(NAMESPACE, DEPLOYMENT, timeout=30)
    assert sha is None
    assert "failed" in error.lower()


def test_kubectl_probe_reports_reason_on_empty_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    sha, error = dr.default_kubectl_health_probe(NAMESPACE, DEPLOYMENT)
    assert sha is None
    assert "no output" in error


# ---------------------------------------------------------------------------
# describe_deployed_revision_drift -- the three-way split
# ---------------------------------------------------------------------------


def test_kubectl_failure_reports_unreachable_never_drift_or_current():
    calls = []
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/unused"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(error="kubectl exec failed: connection refused"),
        git_runner=fake_git_runner(calls, responses={}),
    )
    assert status.cannot_determine_reason == "unreachable"
    assert status.could_not_determine is True
    assert status.drifted is False  # unknown must never read as safe
    assert "connection refused" in status.error
    assert calls == []  # never reaches git at all


def test_health_reporting_unknown_sha_is_cannot_determine_not_current():
    calls = []
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/unused"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha="unknown"),
        git_runner=fake_git_runner(calls, responses={}),
    )
    assert status.cannot_determine_reason == "sha_unknown"
    assert status.could_not_determine is True
    assert status.drifted is False
    assert status.deployed_sha == "unknown"
    assert calls == []  # never reaches git -- nothing to compare


def test_not_an_ancestor_is_drifted():
    calls = []
    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=1, stdout="", stderr=""),
        "rev-parse": SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr=""),
    }
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    assert status.could_not_determine is False
    assert status.is_ancestor is False
    assert status.drifted is True
    assert status.pathspec_commits is None  # not meaningful off main entirely


def test_ancestor_with_trigger_path_commits_is_drifted():
    calls = []

    def rev_parse(cmd):
        return SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr="")

    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "rev-parse": rev_parse,
        "rev-list": SimpleNamespace(returncode=0, stdout="2\n", stderr=""),
    }
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    assert status.is_ancestor is True
    assert status.pathspec_commits == 2
    assert status.drifted is True
    assert status.tracking_revision == TIP_SHA
    # the rev-list call is restricted to the build's own trigger pathspec
    rev_list_call = next(c for c in calls if c["cmd"][1] == "rev-list")
    assert dr.TRIGGER_PATHSPEC[0] in rev_list_call["cmd"]
    assert dr.TRIGGER_PATHSPEC[1] in rev_list_call["cmd"]


def test_ancestor_behind_by_unrelated_commits_is_not_drifted():
    """The ordinary case after any merge that never touches apps/mcp-hub: a bare
    commits-behind count must not alarm (the #772 gate correction)."""
    calls = []
    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "rev-parse": SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr=""),
        "rev-list": SimpleNamespace(returncode=0, stdout="0\n", stderr=""),
    }
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    assert status.is_ancestor is True
    assert status.pathspec_commits == 0
    assert status.drifted is False


def test_deployed_sha_equal_to_tracking_revision_skips_rev_list():
    calls = []
    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "rev-parse": SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr=""),
    }
    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=TIP_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    assert status.is_ancestor is True
    assert status.pathspec_commits == 0
    assert status.drifted is False
    assert not any(c["cmd"][1] == "rev-list" for c in calls)


def test_git_failure_reports_unreachable_not_current():
    calls = []

    def exploding_fetch(cmd):
        raise RuntimeError("could not resolve origin")

    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses={"fetch": exploding_fetch}),
    )
    assert status.cannot_determine_reason == "unreachable"
    assert status.drifted is False
    assert "could not resolve origin" in status.error


def test_fetch_is_bounded_by_the_declared_timeout():
    calls = []
    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "rev-parse": SimpleNamespace(returncode=0, stdout=f"{OLD_SHA}\n", stderr=""),
    }
    dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    fetch_call = next(c for c in calls if c["cmd"][1] == "fetch")
    assert fetch_call["timeout"] == dr.FETCH_TIMEOUT_S
    assert fetch_call["cmd"] == [dr.GIT, "fetch", "--quiet", "origin", dr.DEFAULT_BASE_REF]


def test_comparison_never_reads_a_local_branch():
    """Every comparison runs against FETCH_HEAD; nothing in this module ever names a local
    branch as the thing to compare against (dispatch.py's own exemplars' shape)."""
    calls = []
    responses = {
        "fetch": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "merge-base": SimpleNamespace(returncode=0, stdout="", stderr=""),
        "rev-parse": SimpleNamespace(returncode=0, stdout=f"{TIP_SHA}\n", stderr=""),
        "rev-list": SimpleNamespace(returncode=0, stdout="0\n", stderr=""),
    }
    dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=Path("/repo"),
        remote="origin",
        kubectl_probe=fake_kubectl_probe(sha=OLD_SHA),
        git_runner=fake_git_runner(calls, responses=responses),
    )
    for call in calls:
        cmd = call["cmd"]
        if cmd[1] in ("merge-base", "rev-parse", "rev-list"):
            assert any("FETCH_HEAD" in arg for arg in cmd)


# ---------------------------------------------------------------------------
# real git -- proving the local `main` ref's position is irrelevant
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([dr.GIT, *args], cwd=cwd, check=True, capture_output=True, text=True)


def build_origin_and_stale_clone(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """An `origin` repo that gets commits added after cloning, and a `clone` whose local
    `main` is never fast-forwarded afterward -- the every-day "operator hasn't advanced the
    shared checkout" shape. Returns (origin, clone, sha_right_after_clone, origin_tip)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    git(origin, "config", "user.email", "factory@example.test")
    git(origin, "config", "user.name", "Factory Test")
    git(origin, "checkout", "-b", "main")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", "README.md")
    git(origin, "commit", "-m", "initial")
    sha_at_clone = git(origin, "rev-parse", "HEAD").stdout.strip()

    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.email", "factory@example.test")
    git(clone, "config", "user.name", "Factory Test")

    # origin advances with a commit that touches apps/mcp-hub -- the clone's local `main`
    # is deliberately left exactly where it was at clone time (never fast-forwarded).
    (origin / "apps").mkdir()
    (origin / "apps" / "mcp-hub").mkdir()
    (origin / "apps" / "mcp-hub" / "note.txt").write_text("feature\n")
    git(origin, "add", "apps/mcp-hub/note.txt")
    git(origin, "commit", "-m", "mcp-hub change")
    origin_tip = git(origin, "rev-parse", "HEAD").stdout.strip()

    return origin, clone, sha_at_clone, origin_tip


def test_local_main_position_is_irrelevant_to_the_verdict(tmp_path):
    origin, clone, sha_at_clone, origin_tip = build_origin_and_stale_clone(tmp_path)
    local_main_before = git(clone, "rev-parse", "main").stdout.strip()
    assert local_main_before == sha_at_clone  # confirm the clone's main really is stale

    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=clone,
        remote=str(origin),
        trigger_pathspec=("apps/mcp-hub",),
        kubectl_probe=fake_kubectl_probe(sha=sha_at_clone),
    )

    assert status.is_ancestor is True
    assert status.tracking_revision == origin_tip
    assert status.pathspec_commits == 1
    assert status.drifted is True

    # Now move the clone's local `main` to something else entirely -- an unrelated branch,
    # not even reachable from origin's history -- and confirm the verdict is unchanged,
    # because the comparison never reads local `main` at all.
    git(clone, "checkout", "-b", "side-quest")
    (clone / "unrelated.txt").write_text("local work\n")
    git(clone, "add", "unrelated.txt")
    git(clone, "commit", "-m", "local-only work never pushed anywhere")
    git(clone, "branch", "-f", "main", "HEAD")  # local main now points somewhere origin never had

    status_again = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=clone,
        remote=str(origin),
        trigger_pathspec=("apps/mcp-hub",),
        kubectl_probe=fake_kubectl_probe(sha=sha_at_clone),
    )

    assert status_again.is_ancestor is True
    assert status_again.tracking_revision == origin_tip
    assert status_again.pathspec_commits == 1
    assert status_again.drifted is True


def test_unrelated_commits_between_deployed_sha_and_tip_do_not_drift(tmp_path):
    """A tip that only advanced by commits outside the build's trigger pathspec must not
    alarm -- the ordinary, harmless "unrelated merge" shape, proven against a real
    repository rather than a fake git runner (the #772 gate correction, generalized)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    git(origin, "config", "user.email", "factory@example.test")
    git(origin, "config", "user.name", "Factory Test")
    git(origin, "checkout", "-b", "main")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", "README.md")
    git(origin, "commit", "-m", "initial")
    deployed_sha = git(origin, "rev-parse", "HEAD").stdout.strip()

    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))

    # origin advances, but only with a commit that touches neither apps/mcp-hub nor the
    # build workflow file -- exactly the state after most merges, since build-mcp-hub.yml
    # only builds on pushes touching those paths.
    (origin / "docs.txt").write_text("unrelated docs change\n")
    git(origin, "add", "docs.txt")
    git(origin, "commit", "-m", "unrelated docs change")
    new_tip = git(origin, "rev-parse", "HEAD").stdout.strip()

    status = dr.describe_deployed_revision_drift(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        repo_root=clone,
        remote=str(origin),
        trigger_pathspec=("apps/mcp-hub", ".github/workflows/build-mcp-hub.yml"),
        kubectl_probe=fake_kubectl_probe(sha=deployed_sha),
    )

    assert status.is_ancestor is True
    assert status.tracking_revision == new_tip
    assert status.pathspec_commits == 0
    assert status.drifted is False


# ---------------------------------------------------------------------------
# fresh-interpreter import -- this module must import cleanly first (F5)
# ---------------------------------------------------------------------------


def test_deployed_revision_module_imports_cleanly_in_a_fresh_interpreter():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import deployed_revision"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
