"""Tests for scripts/coverage_ratchet.py: the nightly ratchet runs exactly one
command shape per committed baseline file, from an argument allow-list and
without a shell, so a baseline file cannot switch the ratchet off or run
anything else (PR 1147 gate findings, rounds 1 and 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import coverage_ratchet as cr

REPO = Path(__file__).resolve().parents[2]
COV = "--cov=scripts --cov-precision=2 --cov-fail-under=75.64"
BASE = f"pytest scripts/tests {COV}"


def ok(cmd: str, fail_under="75.64"):
    return cr.parse(cmd, fail_under, REPO)


def refused(cmd: str, fail_under="75.64"):
    with pytest.raises(cr.RefusedCommand):
        cr.parse(cmd, fail_under, REPO)


def test_the_three_committed_baselines_are_accepted():
    for rel in ("apps/factory-dispatcher/coverage-baseline.json",
                "apps/mcp-hub/coverage-baseline.json",
                "scripts/coverage-baseline.json"):
        doc = json.loads((REPO / rel).read_text())
        cwd, argv = cr.parse(doc["command"], doc["fail_under_pct"], REPO)
        assert argv[1:3] == ["-m", "pytest"]
        assert f"--cov-fail-under={doc['fail_under_pct']}" in argv


def test_the_base_shape_and_allowed_extras_are_accepted():
    ok(BASE)
    ok(f"python -m pytest scripts/tests/ -q {COV}")
    ok(f"pytest scripts/tests -q --deselect scripts/tests/test_coverage_ratchet.py::test_x {COV}")
    ok("pytest scripts/tests --cov=scripts --cov-precision=2 --cov-fail-under=75.640")


def test_leading_cd_sets_cwd_and_is_not_passed_to_pytest():
    cwd, argv = ok("cd apps/mcp-hub && python -m pytest tests/ -q --cov=src --cov-precision=2 --cov-fail-under=75.64")
    assert cwd == (REPO / "apps/mcp-hub").resolve()
    assert "cd" not in argv and "&&" not in argv


@pytest.mark.parametrize("cmd", [
    # round 1: chaining and shell operators
    f"{BASE} && rm -rf /tmp/x",
    f"{BASE} & echo bg",
    f"{BASE}\necho second",
    f"{BASE} ; true",
    f"{BASE} | tee out",
    f"{BASE} > out",
    f"{BASE} $(true)",
    # round 1: threshold games
    f"{BASE} --cov-fail-under=0",
    "pytest scripts/tests --cov=scripts --cov-precision=2 --cov-fail-under=75.641",
    "pytest scripts/tests --cov=scripts --cov-precision=2 --cov-fail-under=7",
    # round 2: one extra flag that skips or disables measurement
    f"{BASE} --no-cov",
    f"{BASE} --cov-reset",
    f"{BASE} -o addopts=--no-cov",
    f"{BASE} --override-ini=addopts=--no-cov",
    f"{BASE} --co",
    f"{BASE} --collect-only",
    f"{BASE} --help",
    f"{BASE} --version",
    f"{BASE} -p no:cov",
    f"{BASE} -p some_plugin",
    f"{BASE} --cov-config=/tmp/x.cfg",
    f"{BASE} -c /tmp/pytest.ini",
    f"{BASE} -k nothing_matches",
    f"{BASE} --fail-under=0",
    # paths outside the repo or that do not exist
    f"pytest /etc {COV}",
    f"pytest ../.. {COV}",
    f"pytest no/such/dir {COV}",
    "pytest scripts/tests --cov=/ --cov-precision=2 --cov-fail-under=75.64",
    f"pytest scripts/tests --deselect /etc/x::y {COV}",
    # missing or duplicated required options
    "pytest scripts/tests --cov-precision=2 --cov-fail-under=75.64",
    "pytest scripts/tests --cov=scripts --cov=apps --cov-precision=2 --cov-fail-under=75.64",
    "pytest scripts/tests --cov=scripts --cov-fail-under=75.64",
    "pytest scripts/tests --cov=scripts --cov-precision=0 --cov-fail-under=75.64",
    "pytest scripts/tests --cov=scripts --cov-precision=2",
    # not pytest, or a cd in the wrong shape
    f"bash -c pytest {COV}",
    f"cd apps/mcp-hub pytest {COV}",
    f"cd /etc && pytest {COV}",
    f"cd ../.. && pytest {COV}",
    f"cd apps/mcp-hub && cd .. && pytest {COV}",
])
def test_refuses_every_other_shape(cmd):
    refused(cmd)


def test_cd_through_a_symlink_that_escapes_the_repo_is_refused(tmp_path):
    link = REPO / "scripts" / "tests" / ".tmp_escape_link_for_test"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(cr.RefusedCommand, match="resolves outside the repo"):
            cr.parse(f"cd scripts/tests/.tmp_escape_link_for_test && pytest . {COV}", "75.64", REPO)
    finally:
        if link.is_symlink():
            link.unlink()


def test_check_only_cli_refuses_a_bad_file_with_exit_3(tmp_path, capsys, monkeypatch):
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"command": f"{BASE} --no-cov", "fail_under_pct": 75.64}))
    monkeypatch.chdir(REPO)
    assert cr.main(["coverage_ratchet.py", str(bad), "--check-only"]) == 3
    assert "refused" in capsys.readouterr().err
