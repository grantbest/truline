from __future__ import annotations

import importlib.util
import pathlib
import re
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "check-fast-path"
MODULE_PATH = REPO / "scripts" / "check-fast-path.py"


def _load_check_fast_path():
    spec = importlib.util.spec_from_file_location("check_fast_path", MODULE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_fixture(name: str, capsys):
    check_fast_path = _load_check_fast_path()
    code = check_fast_path.main(["--fixture-dir", str(FIXTURES / name)])
    output = capsys.readouterr().out
    return code, output


def test_all_conditions_passing_ends_eligible_with_exit_zero(capsys):
    code, output = _run_fixture("all-pass", capsys)

    assert code == 0, output
    for check in (
        "bead",
        "lane-autonomy",
        "change-kind",
        "scope",
        "ci",
        "ci-coverage",
        "verification-evidence",
    ):
        assert f"PR #201 PASS {check}:" in output
    assert output.rstrip("\n").splitlines()[-1] == "PR #201: ELIGIBLE"


def test_known_bad_fixtures_prove_each_condition_can_fail(capsys):
    cases = [
        ("fail-bead", "202", "bead", "no dispatched-bead id line"),
        ("fail-lane-autonomy", "203", "lane-autonomy", "autonomy=propose"),
        ("fail-change-kind", "204", "change-kind", "declared 'behavioral'"),
        ("fail-scope", "205", "scope", ".github/workflows/build.yml"),
        ("fail-ci", "206", "ci", "failure"),
        ("fail-verification-evidence", "207", "verification-evidence", "missing"),
    ]

    for fixture, pr, check, evidence in cases:
        code, output = _run_fixture(fixture, capsys)
        assert code == 1, output
        assert f"PR #{pr} FAIL {check}:" in output, output
        assert evidence in output, output
        assert output.rstrip("\n").splitlines()[-1] == f"PR #{pr}: INELIGIBLE"


def test_autonomy_propose_is_ineligible_even_though_everything_else_passes(capsys):
    """Filing defaults autonomy to 'propose' (file_task.py); eligibility is a
    deliberate per-bead escalation, never inferred by this checker."""
    code, output = _run_fixture("fail-lane-autonomy", capsys)

    assert code == 1
    for check in ("bead", "change-kind", "scope", "ci", "verification-evidence"):
        assert f"PR #203 PASS {check}:" in output
    assert "PR #203 FAIL lane-autonomy:" in output
    assert "PR #203: INELIGIBLE" in output


def test_autonomy_absent_is_ineligible_even_though_everything_else_passes(capsys):
    code, output = _run_fixture("fail-autonomy-absent", capsys)

    assert code == 1
    for check in ("bead", "change-kind", "scope", "ci", "verification-evidence"):
        assert f"PR #210 PASS {check}:" in output
    assert "PR #210 FAIL lane-autonomy: lane=code-health autonomy=(missing)" in output
    assert "PR #210: INELIGIBLE" in output


def test_combined_fixture_directory_runs_each_pr_and_exits_nonzero(capsys):
    code, output = _run_fixture(".", capsys)

    assert code == 1
    assert "PR #201: ELIGIBLE" in output
    for pr in ("202", "203", "204", "205", "206", "207", "210", "863"):
        assert f"PR #{pr}: INELIGIBLE" in output


def test_fast_path_refuses_pr_863s_shape_one_unrequired_context_only(capsys):
    """OPS-147 AC-5: the motivating incident, replayed through the fast path.

    PR #863's real rollup held one entry, GitGuardian Security Checks,
    conclusion SUCCESS -- nothing this repo requires. Condition 5 (``ci``)
    alone reads that as green, exactly like the release-gate pre-pass did
    before this fix; only the added ``ci-coverage`` condition can tell
    "nothing failed" from "nothing required ran", and the 19.3 auto-merge
    amendment's own condition 5 stands on being able to make that call.
    """
    code, output = _run_fixture("fail-ci-coverage", capsys)

    assert code == 1, output
    assert "PR #863 PASS ci:" in output
    assert "PR #863 FAIL ci-coverage: missing required context(s):" in output
    assert "PR #863: INELIGIBLE" in output


def test_module_shares_pr_loading_and_bead_resolution_with_gate_prepass():
    """A second recogniser/substrate-client implementation is exactly the
    drift the shared gate_markers/substrate_client modules were built to
    prevent (see their docstrings) — check-fast-path.py must not reintroduce
    it one layer up."""
    check_fast_path = _load_check_fast_path()
    # The very module object check-fast-path.py itself loaded (see its
    # _load_gate_prepass), not a second independently-loaded copy — identity
    # against a fresh import would be a coincidence, not a guarantee.
    gate_prepass = check_fast_path._prepass

    assert check_fast_path.load_fixture_prs is gate_prepass.load_fixture_prs
    assert check_fast_path.load_gh_prs is gate_prepass.load_gh_prs
    assert check_fast_path.find_bead_id is gate_prepass.find_bead_id
    assert check_fast_path.PullRequest is gate_prepass.PullRequest
    assert check_fast_path.CheckResult is gate_prepass.CheckResult


def test_module_defines_no_second_recogniser_pattern():
    source = MODULE_PATH.read_text()
    # Fragments of gate_markers.py's UUID / bead-line / dev.task regexes. Their
    # presence here would mean a second implementation was written instead of
    # reusing gate-prepass's.
    forbidden_fragments = (
        "[0-9a-fA-F]{8}",
        "BEAD_LINE_RE = re.compile",
        "DEV_TASK_REF_RE = re.compile",
        "UUID_RE = re.compile",
    )
    for fragment in forbidden_fragments:
        assert fragment not in source, f"found a reimplemented recogniser fragment: {fragment}"


def test_module_contains_no_merge_or_review_call_path():
    """Structural incapability, proved mechanically: no gh pr merge/review/
    approve/comment invocation, and no subprocess use of its own at all — every
    gh/network call is delegated to gate-prepass.py's already-tested loaders."""
    source = MODULE_PATH.read_text()

    assert "import subprocess" not in source
    assert "subprocess.run" not in source
    assert "subprocess.Popen" not in source

    mutating_gh_pr = re.compile(
        r"gh[^\n]{0,40}pr[^\n]{0,20}(merge|review|approve|comment)", re.IGNORECASE
    )
    assert not mutating_gh_pr.search(source), "found a PR-mutating gh invocation"

    for literal in ("merge", "review", "approve"):
        assert f'"{literal}"' not in source
        assert f"'{literal}'" not in source
