"""Tests for scripts/feature_probe.py -- the reusable helper that answers
"does this invocation work HERE" by running it, never by parsing a version
string. Background: dev.finding, PR #941 (2026-09-18) -- a GitHub Actions
runner reported ``git version 2.34.1`` while a developer machine reported
2.50.1; ``git merge-tree --write-tree`` (arrived git 2.38) did not exist on
the runner, and a test written against the developer's git went red in CI
with both directions of its guarded assertion failing identically -- the
signature of a command that never ran, not of a classification bug.

This file has two halves. The first is ordinary unit coverage of
``probe_invocation``, ``missing_capability_reason``, and ``skip_unless``.
The second (``test_skip_fires_absent_and_runs_present_...``) is the proof
AC-3 (re-specified 2026-09-18) asks for: BOTH arms deterministic on every
machine, CI's included, using a capability the test itself creates and
removes -- a fixture executable on a tmp_path PATH that the test makes
succeed for the PRESENT arm and fail for the ABSENT arm. It drives a
*real* nested pytest run of a sample suite built with
``feature_probe.skip_unless`` -- so the skip is observed firing, not
merely asserted to fire in the abstract -- and deliberately does NOT gate
on any real-git version-dependent feature: the first attempt at this proof
(PR #943) used ``git merge-tree --write-tree`` as the probed capability and
asserted the unshimmed control arm ran and passed, which silently required
the HOST to already have that flag. CI's git is 2.34.1, so that arm skipped
there too, both arms became identical, and the fixed count assertion failed
in CI while passing locally -- reproducing this bead's own subject inside
its proof. A self-controlled fixture capability cannot skew that way on any
host.

AC-4's concrete application of the helper to a real, git-version-gated case
lives in scripts/tests/test_pr_contention.py's ``REQUIRES_MERGE_TREE``
guard, not here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import feature_probe  # noqa: E402


def _seed_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("config", "user.email", "probe@example.invalid")
    run("config", "user.name", "probe")
    run("commit", "-q", "--allow-empty", "-m", "seed")
    return root


# --------------------------------------------------------------------------
# probe_invocation
# --------------------------------------------------------------------------


def test_probe_invocation_reports_available_for_a_working_invocation(tmp_path):
    repo = _seed_git_repo(tmp_path / "repo")
    result = feature_probe.probe_invocation(["git", "-C", str(repo), "rev-parse", "HEAD"])
    assert result.available is True


def test_probe_invocation_reports_unavailable_for_a_nonzero_exit(tmp_path):
    repo = _seed_git_repo(tmp_path / "repo")
    result = feature_probe.probe_invocation(
        ["git", "-C", str(repo), "this-is-not-a-git-subcommand"]
    )
    assert result.available is False
    assert result.detail  # nonempty: stderr carries the diagnostic


def test_probe_invocation_reports_unavailable_for_a_missing_executable():
    result = feature_probe.probe_invocation(["definitely-not-a-real-executable-xyz"])
    assert result.available is False
    assert result.detail  # nonempty: the OSError string carries the diagnostic


def test_probe_invocation_does_not_raise_on_timeout(tmp_path):
    result = feature_probe.probe_invocation(["sleep", "5"], timeout=0.05)
    assert result.available is False


# --------------------------------------------------------------------------
# missing_capability_reason
# --------------------------------------------------------------------------


def test_missing_capability_reason_names_requirement_capability_and_substitute():
    reason = feature_probe.missing_capability_reason(
        requirement="single-invocation write-tree merge probe",
        capability="git >= 2.38 (git merge-tree --write-tree)",
        substitute_test="test_conflict_probe_via_working_tree_merge",
    )
    assert "single-invocation write-tree merge probe" in reason
    assert "git >= 2.38 (git merge-tree --write-tree)" in reason
    assert "test_conflict_probe_via_working_tree_merge" in reason


def test_missing_capability_reason_folds_in_detail_when_present():
    reason = feature_probe.missing_capability_reason(
        requirement="req",
        capability="cap",
        substitute_test="sub",
        detail="fatal: ambiguous argument '--write-tree'",
    )
    assert "fatal: ambiguous argument '--write-tree'" in reason
    assert "req" in reason and "cap" in reason and "sub" in reason


# --------------------------------------------------------------------------
# skip_unless
# --------------------------------------------------------------------------


def test_skip_unless_builds_a_true_condition_when_the_invocation_fails(tmp_path):
    repo = _seed_git_repo(tmp_path / "repo")
    marker = feature_probe.skip_unless(
        ["git", "-C", str(repo), "this-is-not-a-git-subcommand"],
        requirement="req",
        capability="cap",
        substitute_test="sub",
    )
    condition, reason = marker.mark.args[0], marker.mark.kwargs["reason"]
    assert condition is True  # skipif condition True => the test is skipped
    assert "req" in reason and "cap" in reason and "sub" in reason


def test_skip_unless_builds_a_false_condition_when_the_invocation_succeeds(tmp_path):
    repo = _seed_git_repo(tmp_path / "repo")
    marker = feature_probe.skip_unless(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        requirement="req",
        capability="cap",
        substitute_test="sub",
    )
    assert marker.mark.args[0] is False  # skipif condition False => the test runs


# --------------------------------------------------------------------------
# AC-3 (re-specified 2026-09-18): prove the skip fires when the probed
# capability is absent, and that the guarded test runs when it is present,
# by actually running a sample suite through nested pytest. The probed
# capability is a fixture executable ("widget-probe") that this test writes
# to a tmp_path bin directory and prepends to PATH itself -- it is not git,
# and its success/failure is set directly by the test, not inferred from
# any tool already installed on the host. Both arms are therefore
# deterministic on every machine, CI's included.
# --------------------------------------------------------------------------

_SAMPLE_SUITE = '''
import subprocess
import sys

sys.path.insert(0, {scripts_dir!r})
import feature_probe

_requires_widget_probe = feature_probe.skip_unless(
    ["widget-probe", "--check"],
    requirement={requirement!r},
    capability={capability!r},
    substitute_test={substitute_test!r},
)


@_requires_widget_probe
def test_guarded_by_widget_probe():
    result = subprocess.run(["widget-probe", "--check"], capture_output=True, text=True)
    assert result.returncode == 0


def test_widget_substitute_always_runs():
    assert True
'''

_REQUIREMENT = "widget-probe fast path"
_CAPABILITY = "a widget-probe executable on PATH that exits 0 for --check"
_SUBSTITUTE_TEST = "test_widget_substitute_always_runs"


def _write_sample_suite(sample_dir: Path) -> None:
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "test_guarded.py").write_text(
        _SAMPLE_SUITE.format(
            scripts_dir=str(SCRIPTS_DIR),
            requirement=_REQUIREMENT,
            capability=_CAPABILITY,
            substitute_test=_SUBSTITUTE_TEST,
        )
    )


def _write_widget_probe_fixture(bin_dir: Path, *, succeeds: bool) -> None:
    """The self-controlled capability AC-3 requires: a tiny executable this
    test creates and removes itself (via tmp_path), made to succeed for the
    PRESENT arm and fail for the ABSENT arm -- never a real-git
    version-gated feature."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "widget-probe"
    script.write_text("#!/bin/sh\nexit 0\n" if succeeds else "#!/bin/sh\nexit 1\n")
    os.chmod(script, 0o755)


def _run_sample_suite(sample_dir: Path, *, path: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=path)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "test_guarded.py"],
        cwd=str(sample_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for n, word in re.findall(r"(\d+) (passed|skipped|failed)", output):
        counts[word] = int(n)
    return counts


def test_skip_fires_absent_and_runs_present_on_a_self_controlled_fixture_capability(tmp_path):
    sample_dir = tmp_path / "sample"
    _write_sample_suite(sample_dir)

    # ABSENT arm: the fixture executable exists but fails deterministically
    # -- the guarded test skips, and only the substitute runs.
    absent_bin = tmp_path / "bin-absent"
    _write_widget_probe_fixture(absent_bin, succeeds=False)
    absent = _run_sample_suite(sample_dir, path=f"{absent_bin}{os.pathsep}{os.environ['PATH']}")
    absent_counts = _counts(absent.stdout)
    assert absent_counts.get("passed") == 1, absent.stdout
    assert absent_counts.get("skipped") == 1, absent.stdout

    # PRESENT arm: same fixture executable, now made to succeed -- both the
    # guarded test and the substitute run and pass.
    present_bin = tmp_path / "bin-present"
    _write_widget_probe_fixture(present_bin, succeeds=True)
    present = _run_sample_suite(sample_dir, path=f"{present_bin}{os.pathsep}{os.environ['PATH']}")
    present_counts = _counts(present.stdout)
    assert present_counts.get("passed") == 2, present.stdout
    assert present_counts.get("skipped", 0) == 0, present.stdout

    # The skip reason is the point: it must name what's unavailable, what
    # would provide it, and which test covers the degraded contract.
    assert _REQUIREMENT in absent.stdout
    assert _CAPABILITY in absent.stdout
    assert _SUBSTITUTE_TEST in absent.stdout
