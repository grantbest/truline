from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "pr-contention"
MODULE_PATH = REPO / "scripts" / "pr-contention.py"

sys.path.insert(0, str(REPO / "scripts"))
import feature_probe  # noqa: E402


def _load_pr_contention():
    spec = importlib.util.spec_from_file_location("pr_contention", MODULE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# `--write-tree` arrived in git 2.38. THE CI RUNNER IS OLDER THAN THAT --
# measured 2026-09-18: GitHub's runner reports `git version 2.34.1` while a
# developer machine here reports 2.50.1. On 2.34 the flag does not exist, so
# `check_conflict` correctly returns its "could not run" answer (None) and
# the three behaviour tests below asserted True/False against it and failed
# in CI while passing locally.
#
# Guarded via scripts/feature_probe.py's FEATURE PROBE rather than by parsing
# `git --version`: the question is "does this invocation work here", and a
# version string is a proxy for it that can be wrong (vendor builds,
# backports). Probing answers the question actually being asked. The two
# tests named below stub subprocess.run to pin check_conflict's True/False
# branches independently of the local git's --write-tree support, so they
# run -- and cover the degraded contract -- everywhere, including CI's 2.34.1.
REQUIRES_MERGE_TREE = feature_probe.skip_unless(
    ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", "HEAD"],
    requirement="single-invocation write-tree merge probe",
    capability="git >= 2.38 (git merge-tree --write-tree)",
    substitute_test=(
        "test_check_conflict_returns_true_and_conflicted_paths_when_git_reports_conflict "
        "and test_check_conflict_returns_false_when_git_reports_a_clean_merge"
    ),
    cwd=str(REPO),
)


# ---------------------------------------------------------------------------
# AC-2: no network call when driven from a fixture, and the source is
# injectable (fixture vs. live gh).
# ---------------------------------------------------------------------------


def test_load_fixture_prs_calls_no_subprocess(monkeypatch):
    pr_contention = _load_pr_contention()

    def _forbidden(*args, **kwargs):
        raise AssertionError("load_fixture_prs must never shell out")

    monkeypatch.setattr(subprocess, "run", _forbidden)

    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")
    assert {pr.number for pr in prs} == {925, 926, 929, 923, 921, 930, 916}


def test_main_with_fixture_dir_calls_no_subprocess(monkeypatch, capsys):
    pr_contention = _load_pr_contention()

    def _forbidden(*args, **kwargs):
        raise AssertionError("a fixture-driven run must never shell out")

    monkeypatch.setattr(subprocess, "run", _forbidden)

    code = pr_contention.main(["--fixture-dir", str(FIXTURES / "estate")])
    assert code == 0
    assert "Contended paths" in capsys.readouterr().out


def test_load_gh_prs_is_the_only_source_of_a_subprocess_call(monkeypatch):
    """AC-2's injectability: live mode calls gh exactly once per PR number,
    and it is the sole subprocess call site in the module."""
    pr_contention = _load_pr_contention()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stdout = '{"number": 925, "files": ["a.py"], "headRefOid": "deadbeef"}'
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    prs = pr_contention.load_gh_prs(["925"])

    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "pr", "view"]
    assert prs == [pr_contention.PRFiles(number=925, files=("a.py",), head_ref="deadbeef")]


# ---------------------------------------------------------------------------
# AC-1 / AC-4(i,ii): contention is reported with paths named; disjoint PRs
# are not reported as contended (the known-bad control).
# ---------------------------------------------------------------------------


def test_shared_path_is_reported_with_the_path_named():
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    neighbors = pr_contention.neighbors_of(925, prs)
    by_other = {n.other: n.shared_paths for n in neighbors}

    assert by_other[926] == (
        "apps/factory-dispatcher/dispatch.py",
        "apps/factory-dispatcher/tests/test_dispatch.py",
    )
    assert by_other[929] == ("apps/factory-dispatcher/dispatch.py",)
    assert by_other[923] == ("apps/factory-dispatcher/schedule_status.py",)

    report = pr_contention.render_pr_report(925, prs)
    assert "PR #925 also touches, with PR #926: apps/factory-dispatcher/dispatch.py" in report
    assert "apps/factory-dispatcher/schedule_status.py" in report


def test_disjoint_prs_are_not_reported():
    """Known-bad control (AC-4 ii): #923 and #926 share no path. A tool that
    reported every pair as contended would fail this test even though it
    would pass the shared-path test above."""
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    neighbors = pr_contention.neighbors_of(923, prs)
    assert {n.other for n in neighbors} == {925}

    report = pr_contention.render_pr_report(926, prs)
    assert "923" not in report
    assert "guards.py" not in [p for n in pr_contention.neighbors_of(925, prs) for p in n.shared_paths]


def test_925_report_never_names_guards_py():
    """The exact regression the audit measured: a dispatch once claimed #925
    touches guards.py. #925 does not touch it; only #926 does, and #926
    touching it alone is not contention."""
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    report = pr_contention.render_pr_report(925, prs)
    assert "guards.py" not in report


def test_whole_estate_reports_exactly_the_seven_measured_paths():
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    contention = pr_contention.find_contention(prs)
    paths = {c.path for c in contention}

    assert paths == {
        "apps/factory-dispatcher/dispatch.py",
        "apps/factory-dispatcher/activities/dispatch_steps.py",
        "apps/factory-dispatcher/schedule_status.py",
        "apps/factory-dispatcher/tests/test_dispatch.py",
        "apps/factory-dispatcher/tests/test_store_double_call_surface.py",
        "scripts/gate-prepass.py",
        "scripts/tests/test_gate_prepass.py",
    }

    by_path = {c.path: set(c.prs) for c in contention}
    assert by_path["apps/factory-dispatcher/dispatch.py"] == {925, 926, 929}
    assert by_path["scripts/gate-prepass.py"] == {921, 930}
    assert by_path["apps/factory-dispatcher/schedule_status.py"] == {925, 923}

    report = pr_contention.render_estate_report(prs)
    assert report.splitlines()[0] == "Contended paths across 7 open PR(s): 7"


def test_930_neighbours_include_921_the_dispatch_missed():
    """The second measured error: a dispatch named #924/#927 as #930's
    scripts/tests neighbours and missed #921, which touches both files #930
    changes."""
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    neighbors = pr_contention.neighbors_of(930, prs)
    assert [n.other for n in neighbors] == [921]
    assert set(neighbors[0].shared_paths) == {
        "scripts/gate-prepass.py",
        "scripts/tests/test_gate_prepass.py",
    }


def test_unknown_pr_number_raises_rather_than_reporting_nothing():
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    try:
        pr_contention.neighbors_of("999", prs)
    except ValueError as exc:
        assert "999" in str(exc)
    else:
        raise AssertionError("expected ValueError for a PR not in the supplied set")


# ---------------------------------------------------------------------------
# AC-3 / AC-4(iii): conflict is distinguished from contention, using
# `git merge-tree --write-tree` against a real, local-only repo.
# ---------------------------------------------------------------------------


def _build_conflict_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Two branches off one base, both editing shared.py incompatibly --
    the real shape of #916+#929 (both touch
    test_store_double_call_surface.py, and the merge conflicts)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "contention@example.test")
    _git(repo, "config", "user.name", "Contention Test")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "shared.py").write_text("one\n")
    (repo / "other.py").write_text("one\n")
    _git(repo, "add", "shared.py", "other.py")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "pr-916")
    (repo / "shared.py").write_text("branch A change\n")
    _git(repo, "commit", "-q", "-am", "pr-916 edits shared.py")

    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "pr-929")
    (repo / "shared.py").write_text("branch B change\n")
    _git(repo, "commit", "-q", "-am", "pr-929 edits shared.py incompatibly")

    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "pr-921")
    (repo / "other.py").write_text("branch C change\n")
    _git(repo, "commit", "-q", "-am", "pr-921 edits a different file")

    _git(repo, "checkout", "-q", "main")
    return repo


@REQUIRES_MERGE_TREE
def test_conflicting_pair_is_reported_as_conflicting(tmp_path):
    pr_contention = _load_pr_contention()
    repo = _build_conflict_repo(tmp_path)

    conflicts, paths = pr_contention.check_conflict(repo, "pr-916", "pr-929")

    assert conflicts is True
    assert paths == ("shared.py",)


@REQUIRES_MERGE_TREE
def test_clean_pair_is_reported_as_not_conflicting(tmp_path):
    pr_contention = _load_pr_contention()
    repo = _build_conflict_repo(tmp_path)

    conflicts, paths = pr_contention.check_conflict(repo, "pr-916", "pr-921")

    assert conflicts is False
    assert paths == ()


def test_check_conflict_does_not_touch_branches_or_working_tree(tmp_path):
    pr_contention = _load_pr_contention()
    repo = _build_conflict_repo(tmp_path)

    before_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    before_status = _git(repo, "status", "--porcelain").stdout
    before_shas = {
        ref: _git(repo, "rev-parse", ref).stdout.strip() for ref in ("main", "pr-916", "pr-929", "pr-921")
    }

    pr_contention.check_conflict(repo, "pr-916", "pr-929")
    pr_contention.check_conflict(repo, "pr-916", "pr-921")

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == before_branch
    assert _git(repo, "status", "--porcelain").stdout == before_status
    after_shas = {
        ref: _git(repo, "rev-parse", ref).stdout.strip() for ref in ("main", "pr-916", "pr-929", "pr-921")
    }
    assert after_shas == before_shas


def test_unresolvable_ref_reports_could_not_run_not_a_guessed_verdict(tmp_path):
    pr_contention = _load_pr_contention()
    repo = _build_conflict_repo(tmp_path)

    conflicts, paths = pr_contention.check_conflict(repo, "pr-916", "no-such-ref")

    assert conflicts is None
    assert paths == ()


@REQUIRES_MERGE_TREE
def test_end_to_end_check_conflicts_via_cli_names_the_conflicting_pair(tmp_path, capsys):
    """Ties the fixture-driven contention report to a real conflict check:
    fixture head_refs point at real branches in a throwaway repo, and
    --check-conflicts runs `git merge-tree` against it."""
    pr_contention = _load_pr_contention()
    repo = _build_conflict_repo(tmp_path)

    fixture_dir = tmp_path / "fixtures"
    for number, ref in (("916", "pr-916"), ("929", "pr-929"), ("921", "pr-921")):
        pr_dir = fixture_dir / number
        pr_dir.mkdir(parents=True)
        files = ["shared.py"] if number != "921" else ["other.py"]
        (pr_dir / "pr.json").write_text(
            f'{{"number": {number}, "files": {files!r}, "head_ref": "{ref}"}}'.replace("'", '"')
        )

    code = pr_contention.main(
        [
            "--fixture-dir",
            str(fixture_dir),
            "--check-conflicts",
            "--repo-root",
            str(repo),
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "#916 x #929: CONFLICTS (shared.py)" in output
    # #921 touches a different file (other.py) from #916/#929 (shared.py), so
    # it is not a contended pair at all and is never sent to merge-tree.
    #
    # Asserted against the lines that make a CLAIM about #921, not against the
    # whole output: the estate report now names every pull request it was
    # given ("Pull requests considered: ..."), precisely so that a PR which
    # contends with nothing is still visible to the reader rather than being
    # indistinguishable from one that was omitted from the input. A bare
    # `"921" not in output` would now forbid that disclosure.
    claim_lines = [
        line
        for line in output.splitlines()
        if "921" in line and not line.startswith("Pull requests considered:")
    ]
    assert claim_lines == [], claim_lines


# ---------------------------------------------------------------------------
# AC-6: no claim about merge order is ever emitted.
# ---------------------------------------------------------------------------


def test_no_output_ever_recommends_a_merge_order():
    pr_contention = _load_pr_contention()
    prs = pr_contention.load_fixture_prs(FIXTURES / "estate")

    outputs = [pr_contention.render_estate_report(prs)]
    for pr in prs:
        outputs.append(pr_contention.render_pr_report(pr.number, prs))

    banned = ("merge order", "should merge", "merge first", "merge before", "recommend")
    for output in outputs:
        lowered = output.lower()
        for phrase in banned:
            assert phrase not in lowered, f"{phrase!r} found in: {output}"


# ---------------------------------------------------------------------------
# The True/False branches, pinned on EVERY git including CI's 2.34.1, by
# stubbing subprocess.run rather than depending on a real --write-tree.
#
# The None branch (an unattemptable merge, e.g. an unresolvable ref) is
# already covered unconditionally by
# test_unresolvable_ref_reports_could_not_run_not_a_guessed_verdict above --
# it runs on every git today, so it needs no stubbed counterpart here.
# What was missing in CI was the OTHER two answers: on 2.34.1 the
# REQUIRES_MERGE_TREE tests above (test_conflicting_pair_is_reported_as_conflicting,
# test_clean_pair_is_reported_as_not_conflicting) skip, and nothing else
# exercised True or False there. An inversion mutation swapping check_conflict's
# True and False returns is invisible in that CI run without the two tests
# below: they stub subprocess.run the same way
# test_load_gh_prs_is_the_only_source_of_a_subprocess_call does, so they pin
# the branches independently of whether this git's merge-tree supports
# --write-tree at all.


def test_check_conflict_returns_true_and_conflicted_paths_when_git_reports_conflict(monkeypatch):
    pr_contention = _load_pr_contention()

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 1
            stdout = "deadbeef\nshared.py\n"
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    conflicts, paths = pr_contention.check_conflict(pathlib.Path("/repo"), "ref-a", "ref-b")

    assert conflicts is True
    assert paths == ("shared.py",)


def test_check_conflict_returns_false_when_git_reports_a_clean_merge(monkeypatch):
    pr_contention = _load_pr_contention()

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    conflicts, paths = pr_contention.check_conflict(pathlib.Path("/repo"), "ref-a", "ref-b")

    assert conflicts is False
    assert paths == ()
