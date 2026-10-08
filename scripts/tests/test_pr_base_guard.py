"""Meta-tests for the PR-base guard.

Each case is fed a state the guard MUST reject or accept. The 2026-08-02 case is
reproduced verbatim: #253 based on ``lifeops/gemini-reviewer-orientation``, whose
own PR #251 had merged 32 seconds earlier.
"""

from __future__ import annotations

import pathlib
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import pr_base_guard as guard  # noqa: E402


def test_base_main_passes():
    result = guard.evaluate("main", None)
    assert result.ok
    assert "advances trunk" in result.message


def test_base_is_open_pr_passes_with_ordering_warning():
    result = guard.evaluate("lifeops/some-stack", "OPEN", 300)
    assert result.ok
    # The stack is legal, but merge order is the thing that bit us.
    assert "LAST" in result.message


def test_the_2026_08_02_incident_is_rejected():
    """#253 based on a branch whose PR (#251) had already merged."""
    result = guard.evaluate("lifeops/gemini-reviewer-orientation", "MERGED", 251)
    assert not result.ok
    assert "ALREADY been merged" in result.message
    assert "#251" in result.message


def test_closed_unmerged_base_is_rejected():
    result = guard.evaluate("lifeops/abandoned", "CLOSED", 99)
    assert not result.ok
    assert "stranded" in result.message


def test_base_with_no_pr_at_all_is_rejected():
    result = guard.evaluate("lifeops/orphan", None)
    assert not result.ok
    assert "no pull request of its own" in result.message


def test_state_casing_does_not_matter():
    """REST returns 'open'; GraphQL returns 'OPEN'. Callers should not care."""
    assert guard.evaluate("x", "open", 1).ok
    assert not guard.evaluate("x", "merged", 1).ok
    assert not guard.evaluate("x", "closed", 1).ok


def test_lookup_prefers_the_newest_pr(monkeypatch):
    """A branch reused across PRs must resolve to its most recent one."""
    payload = [
        {"number": 10, "state": "closed", "merged_at": None, "created_at": "2026-01-01T00:00:00Z"},
        {"number": 20, "state": "closed", "merged_at": "2026-08-02T22:07:45Z", "created_at": "2026-08-02T00:00:00Z"},
    ]
    monkeypatch.setattr(guard, "_api", lambda path, token: payload)
    state, number = guard.lookup_base_pr("grantbest/homelabv2", "some-branch", "t")
    assert (state, number) == ("MERGED", 20)


def test_lookup_reports_absence():
    guard_api = guard._api
    try:
        guard._api = lambda path, token: []
        assert guard.lookup_base_pr("grantbest/homelabv2", "nope", "t") == (None, None)
    finally:
        guard._api = guard_api


def test_guard_can_actually_fail():
    """The gate must be capable of returning a non-ok verdict at all."""
    verdicts = [
        guard.evaluate("b", "MERGED", 1).ok,
        guard.evaluate("b", "CLOSED", 1).ok,
        guard.evaluate("b", None).ok,
    ]
    assert verdicts == [False, False, False]
