"""Tests for the merge-pr helper. The gh interface is faked; no network."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]


def _load_merge_pr():
    path = REPO / "scripts" / "merge-pr.py"
    spec = importlib.util.spec_from_file_location("merge_pr", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


merge_pr = _load_merge_pr()


# --- pure logic: extract_change_kind ---------------------------------------


def test_extract_change_kind_single_declaration():
    body = "Some PR description.\n\nChange kind: behavioral\n\nmore text"
    assert merge_pr.extract_change_kind(body) == "Change kind: behavioral"


def test_extract_change_kind_ignores_fenced_examples():
    body = "```\nChange kind: structural\n```\nChange kind: behavioral"
    assert merge_pr.extract_change_kind(body) == "Change kind: behavioral"


def test_extract_change_kind_zero_declarations_refuses():
    with pytest.raises(merge_pr.Refusal, match="found 0"):
        merge_pr.extract_change_kind("no declaration here")


def test_extract_change_kind_multiple_declarations_refuses():
    body = "Change kind: structural\nChange kind: behavioral"
    with pytest.raises(merge_pr.Refusal, match="found 2"):
        merge_pr.extract_change_kind(body)


# --- pure logic: extract_provenance -----------------------------------------


def test_extract_provenance_matches_both_kinds():
    body = "Summary.\n\nOuter-loop: claude/foo\nDispatched-bead id: bead-123\n\nmore text"
    assert merge_pr.extract_provenance(body) == [
        "Outer-loop: claude/foo",
        "Dispatched-bead id: bead-123",
    ]


def test_extract_provenance_is_case_insensitive():
    body = "outer-loop: claude/foo"
    assert merge_pr.extract_provenance(body) == ["outer-loop: claude/foo"]


def test_extract_provenance_limits_to_two_lines():
    body = (
        "Outer-loop: a\n"
        "Dispatched-bead id: b\n"
        "Outer-loop: c\n"
    )
    assert merge_pr.extract_provenance(body) == ["Outer-loop: a", "Dispatched-bead id: b"]


def test_extract_provenance_empty_when_absent():
    assert merge_pr.extract_provenance("Change kind: structural\n\nno provenance here") == []


# --- pure logic: compose_merge_body -----------------------------------------


def test_compose_merge_body_without_provenance():
    assert merge_pr.compose_merge_body("Change kind: structural", []) == "Change kind: structural"


def test_compose_merge_body_with_provenance_matches_shell_composition():
    body = merge_pr.compose_merge_body(
        "Change kind: behavioral", ["Outer-loop: claude/foo", "Dispatched-bead id: bead-123"]
    )
    assert body == "Change kind: behavioral\nOuter-loop: claude/foo\nDispatched-bead id: bead-123"


# --- pure logic: find_dependent_pr ------------------------------------------


def test_find_dependent_pr_matches_base_ref():
    open_prs = [{"number": 329, "baseRefName": "lifeops/stack-a"}]
    dependent = merge_pr.find_dependent_pr("lifeops/stack-a", open_prs)
    assert dependent == {"number": 329, "baseRefName": "lifeops/stack-a"}


def test_find_dependent_pr_none_when_no_match():
    open_prs = [{"number": 400, "baseRefName": "main"}]
    assert merge_pr.find_dependent_pr("lifeops/stack-a", open_prs) is None


# --- merge_pr(): the happy path and both refusals, gh interface faked ------


def test_merge_pr_happy_path(monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": "Summary.\n\nChange kind: structural\n",
            "headRefName": "lifeops/fix-500",
        },
    )
    monkeypatch.setattr(merge_pr, "_open_prs", lambda: [{"number": 501, "baseRefName": "main"}])
    monkeypatch.setattr(
        merge_pr, "_merge", lambda pr_number, body: calls.append((pr_number, body))
    )
    # Runs strictly after `_merge` above; faked here so this test stays gh/network-free the
    # way this module's own docstring promises -- the wiring itself is covered by
    # test_run_post_merge_health_check_* below.
    monkeypatch.setattr(
        merge_pr, "run_post_merge_health_check", lambda: "post-merge health check: GREEN"
    )

    output = merge_pr.merge_pr("500")

    assert calls == [("500", "Change kind: structural")]
    assert "Change kind: structural" in output
    assert "release-gate verdict" in output
    assert "operator" in output
    assert "branch not deleted" in output
    assert "post-merge health check: GREEN" in output


def test_merge_pr_happy_path_carries_provenance_after_change_kind(monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": (
                "Summary.\n\nChange kind: behavioral\n\n"
                "Outer-loop: claude/foo\nDispatched-bead id: bead-123\n"
            ),
            "headRefName": "lifeops/fix-500",
        },
    )
    monkeypatch.setattr(merge_pr, "_open_prs", lambda: [])
    monkeypatch.setattr(
        merge_pr, "_merge", lambda pr_number, body: calls.append((pr_number, body))
    )
    monkeypatch.setattr(
        merge_pr, "run_post_merge_health_check", lambda: "post-merge health check: GREEN"
    )

    merge_pr.merge_pr("500")

    assert calls == [
        (
            "500",
            "Change kind: behavioral\nOuter-loop: claude/foo\nDispatched-bead id: bead-123",
        )
    ]


def test_merge_pr_refuses_on_zero_change_kind_lines(monkeypatch):
    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {"number": 500, "body": "no declaration", "headRefName": "lifeops/fix-500"},
    )

    def _open_prs_should_not_be_called():
        raise AssertionError("must refuse before checking dependent PRs")

    def _merge_should_not_be_called(pr_number, body):
        raise AssertionError("must refuse before any merge call")

    monkeypatch.setattr(merge_pr, "_open_prs", lambda: _open_prs_should_not_be_called())
    monkeypatch.setattr(merge_pr, "_merge", _merge_should_not_be_called)

    with pytest.raises(merge_pr.Refusal, match="found 0"):
        merge_pr.merge_pr("500")


def test_merge_pr_refuses_on_multiple_change_kind_lines(monkeypatch):
    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": "Change kind: structural\nChange kind: behavioral",
            "headRefName": "lifeops/fix-500",
        },
    )

    def _merge_should_not_be_called(pr_number, body):
        raise AssertionError("must refuse before any merge call")

    monkeypatch.setattr(merge_pr, "_merge", _merge_should_not_be_called)

    with pytest.raises(merge_pr.Refusal, match="found 2"):
        merge_pr.merge_pr("500")


def test_merge_pr_refuses_when_pr_is_base_of_open_pr(monkeypatch):
    """The #328/#329 trap: PR #500 is the base of open PR #501."""
    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": "Change kind: behavioral\n",
            "headRefName": "lifeops/fix-500",
        },
    )
    monkeypatch.setattr(
        merge_pr,
        "_open_prs",
        lambda: [{"number": 501, "baseRefName": "lifeops/fix-500"}],
    )

    def _merge_should_not_be_called(pr_number, body):
        raise AssertionError("must refuse before any merge call")

    monkeypatch.setattr(merge_pr, "_merge", _merge_should_not_be_called)

    with pytest.raises(merge_pr.Refusal, match="#501") as excinfo:
        merge_pr.merge_pr("500")
    assert "#500" in str(excinfo.value)


# --- run_post_merge_health_check(): best-effort wrapper over the sibling script ------------


def test_run_post_merge_health_check_delegates_to_the_loaded_checker(monkeypatch):
    class FakeChecker:
        RED = "red"
        UNREACHABLE = "unreachable"
        UNMEASURED = "unmeasured"

        def resolve_repo(self):
            return "example/repo"

        def check_and_announce(self, repo):
            assert repo == "example/repo"
            return {"status": "red", "failing_workflows": ["Lint & Validate"]}

    monkeypatch.setattr(merge_pr, "_load_post_merge_health_check", lambda: FakeChecker())

    assert merge_pr.run_post_merge_health_check() == (
        "post-merge health check: RED (Lint & Validate)"
    )


def test_run_post_merge_health_check_reports_skip_when_repo_unresolvable(monkeypatch):
    class FakeChecker:
        def resolve_repo(self):
            return None

    monkeypatch.setattr(merge_pr, "_load_post_merge_health_check", lambda: FakeChecker())

    assert "skipped" in merge_pr.run_post_merge_health_check()
    assert "OWNER/REPO" in merge_pr.run_post_merge_health_check()


def test_run_post_merge_health_check_never_raises_on_failure(monkeypatch):
    """A `gh` hiccup or import error here must never turn a successful merge into a raised
    exception -- `merge_pr()` has already committed the merge by the time this runs."""

    def _boom():
        raise RuntimeError("could not load module")

    monkeypatch.setattr(merge_pr, "_load_post_merge_health_check", _boom)

    result = merge_pr.run_post_merge_health_check()

    assert "skipped" in result
    assert "could not load module" in result


# --- _merge(): the actual gh invocation, subprocess faked -------------------


def test_merge_invokes_gh_without_delete_branch(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run(args, check):
        calls.append(args)

    monkeypatch.setattr(merge_pr.subprocess, "run", _fake_run)

    merge_pr._merge("500", "Change kind: structural")

    assert len(calls) == 1
    args = calls[0]
    assert "--delete-branch" not in args
    assert args == ["gh", "pr", "merge", "500", "--squash", "--body", "Change kind: structural"]


# --- main(): CLI wiring, output content, and exit codes --------------------


def test_main_happy_path_prints_verdict_notice_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": "Change kind: structural\n",
            "headRefName": "lifeops/fix-500",
        },
    )
    monkeypatch.setattr(merge_pr, "_open_prs", lambda: [])
    monkeypatch.setattr(merge_pr, "_merge", lambda pr_number, body: None)
    monkeypatch.setattr(
        merge_pr, "run_post_merge_health_check", lambda: "post-merge health check: GREEN"
    )

    exit_code = merge_pr.main(["500"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "release-gate verdict" in out
    assert "operator" in out


def test_main_refusal_returns_nonzero_and_names_dependent_pr(monkeypatch, capsys):
    monkeypatch.setattr(
        merge_pr,
        "_pr_view",
        lambda pr_number: {
            "number": 500,
            "body": "Change kind: behavioral\n",
            "headRefName": "lifeops/fix-500",
        },
    )
    monkeypatch.setattr(
        merge_pr,
        "_open_prs",
        lambda: [{"number": 501, "baseRefName": "lifeops/fix-500"}],
    )

    exit_code = merge_pr.main(["500"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "#501" in err
    assert "release-gate verdict" in err


# ---------------------------------------------------------------------------
# GATE FINDING 1 (#924): the merge body must not claim more than was measured
# ---------------------------------------------------------------------------


def test_the_green_health_line_in_the_merge_body_is_the_qualified_one(monkeypatch):
    """The line this returns is written into the squash-merge body, where it outlives the
    terminal session -- so it is the copy that most needs to state its own bound.

    Driven against the REAL checker module rather than a hand-rolled double, deliberately. The
    doubles in this file (``FakeChecker`` above) define only the attributes their own case
    touches; one of them would happily not have ``GREEN_LINE`` at all and this test would then
    pass while the real merge path raised ``AttributeError`` on the green branch -- a double
    accepting less than the live contract, which is the trap this repo has already paid for.
    """
    checker = merge_pr._load_post_merge_health_check()
    monkeypatch.setattr(checker, "resolve_repo", lambda: "example/repo", raising=False)
    monkeypatch.setattr(
        checker,
        "check_and_announce",
        lambda repo: {"status": checker.GREEN, "failing_workflows": []},
        raising=False,
    )
    monkeypatch.setattr(merge_pr, "_load_post_merge_health_check", lambda: checker)

    line = merge_pr.run_post_merge_health_check()

    assert line.startswith("post-merge health check: GREEN (")
    assert "last completed runs on main" in line
    assert "have not completed" in line
    assert line != "post-merge health check: GREEN"
