"""Tests for scripts/post-merge-health-check.py -- the merge path's own tripwire for a batch of
individually-green PRs turning main red as a SET (OBSERVED 2026-09-17).

No test here mocks `workflow_run_health.notify.get_alert_definition` or `ALERT_INVENTORY`: every
alert-id-resolution assertion runs against the real registry. Only the delivery TRANSPORT (a
fake ``policy.send``) and the dispatcher's on-disk alert-state file (redirected via
`FACTORY_ALERT_STATE_PATH`, never `$HOME`, since `scripts/tests/` carries no autouse `$HOME`
isolation fixture the way `apps/factory-dispatcher/tests/conftest.py` does) are faked -- exactly
`apps/factory-dispatcher/tests/test_workflow_run_health.py`'s own `RecordingPolicy` idiom, applied
here because that module's `announce_finding`/`announce_unreachable`/`notify_findings` are reused
unmodified rather than re-implemented.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load_post_merge_health_check():
    path = REPO / "scripts" / "post-merge-health-check.py"
    spec = importlib.util.spec_from_file_location("post_merge_health_check", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_post_merge_health_check()

# checker.py's own sys.path.insert makes this importable now.
import workflow_run_health  # noqa: E402


def _run(
    workflow_name: str,
    *,
    conclusion: str = "success",
    status: str = "completed",
    updated_at: str = "2026-09-17T00:00:00Z",
    run_id: str = "1",
    url: str = "https://github.com/example/repo/actions/runs/1",
) -> dict:
    return {
        "databaseId": run_id,
        "workflowName": workflow_name,
        "status": status,
        "conclusion": conclusion,
        "updatedAt": updated_at,
        "url": url,
    }


class RecordingPolicy:
    """Fakes only the outbound transport -- `send_alert` still resolves the alert id against the
    real `ALERT_INVENTORY` before ever reaching this object."""

    def __init__(self, send_result: bool = True):
        self.sent: list[tuple[str, str]] = []
        self._send_result = send_result

    async def send(self, kind, fingerprint, content, *, severity=None, min_interval_hours=0.0,
                    re_alert_interval_hours=0.0, members=None):
        self.sent.append((kind, content))
        return self._send_result


# ---------------------------------------------------------------------------
# classify_main_health: the tri-state read, pure function over already-fetched runs
# ---------------------------------------------------------------------------


def test_a_failed_completed_run_is_red():
    runs = [_run("Lint & Validate", status="completed", conclusion="failure")]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate"])
    assert status == checker.RED
    assert [f.workflow_name for f in findings] == ["Lint & Validate"]


def test_an_in_progress_run_with_nothing_completed_is_unmeasured():
    runs = [_run("Lint & Validate", status="in_progress", conclusion="")]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate"])
    assert status == checker.UNMEASURED
    assert findings == []


def test_a_queued_run_with_nothing_completed_is_unmeasured():
    runs = [_run("Lint & Validate", status="queued", conclusion="")]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate"])
    assert status == checker.UNMEASURED


def test_a_workflow_with_no_runs_at_all_is_unmeasured_not_green():
    """The 'absent' case: gh returned zero runs for a workflow this repo defines. A checker
    that judges only `runs` would never even see this workflow's name and could default to
    calling it green -- fail closed instead."""
    status, findings = checker.classify_main_health([], ["Lint & Validate"])
    assert status == checker.UNMEASURED
    assert findings == []


def test_all_defined_workflows_confirmed_green_is_green():
    runs = [_run("Lint & Validate", status="completed", conclusion="success")]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate"])
    assert status == checker.GREEN
    assert findings == []


def test_one_green_and_one_never_observed_workflow_is_unmeasured_not_green():
    """A single confirmed-green workflow must not paper over a sibling this run never
    confirmed either way -- 'main is green' means every declared workflow is, not just one."""
    runs = [_run("Lint & Validate", status="completed", conclusion="success")]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate", "Secret Scan"])
    assert status == checker.UNMEASURED


def test_red_wins_over_a_confirmed_green_sibling():
    runs = [
        _run("Lint & Validate", status="completed", conclusion="failure"),
        _run("Secret Scan", status="completed", conclusion="success"),
    ]
    status, findings = checker.classify_main_health(runs, ["Lint & Validate", "Secret Scan"])
    assert status == checker.RED
    assert [f.workflow_name for f in findings] == ["Lint & Validate"]


def test_no_workflow_names_available_degrades_to_judging_only_what_runs_mentions():
    """`workflow_names=None` (the enumeration call itself failed) must not crash; it degrades to
    the same information workflow_run_health.py's own scheduled check already accepts."""
    runs = [_run("Lint & Validate", status="completed", conclusion="success")]
    status, findings = checker.classify_main_health(runs, None)
    assert status == checker.GREEN


# ---------------------------------------------------------------------------
# real-registry resolution: known-good and known-bad controls, never monkeypatched
# ---------------------------------------------------------------------------


def test_the_failing_alert_id_resolves_against_the_real_registry():
    definition = workflow_run_health.notify.get_alert_definition(
        workflow_run_health.WORKFLOW_RUN_FAILING_ALERT_ID
    )
    assert not definition.is_removed


def test_the_check_unreachable_alert_id_resolves_against_the_real_registry():
    definition = workflow_run_health.notify.get_alert_definition(
        workflow_run_health.WORKFLOW_RUN_CHECK_UNREACHABLE_ALERT_ID
    )
    assert not definition.is_removed


def test_known_bad_control_an_unregistered_alert_id_fails_resolution():
    """Proves the test above is not vacuous: a deliberately-mutated id must still fail. An
    unregistered alert id is a defect this repository has shipped twice."""
    bogus = workflow_run_health.WORKFLOW_RUN_FAILING_ALERT_ID + "_typo_never_registered"
    with pytest.raises(KeyError):
        workflow_run_health.notify.get_alert_definition(bogus)


# ---------------------------------------------------------------------------
# check_and_announce: end-to-end wiring, real registry + real notify_findings/
# announce_unreachable, fake transport and a redirected on-disk alert-state file
# ---------------------------------------------------------------------------


def test_red_finding_is_announced_through_the_real_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    runs = [_run("Lint & Validate", status="completed", conclusion="failure")]
    policy = RecordingPolicy()

    result = checker.check_and_announce(
        "example/repo",
        collect=lambda repo: runs,
        list_workflow_names=lambda *, repo: ["Lint & Validate"],
        policy=policy,
    )

    assert result["status"] == checker.RED
    assert result["failing_workflows"] == ["Lint & Validate"]
    assert len(policy.sent) == 1
    kind, content = policy.sent[0]
    assert kind == "workflow_run_failing:Lint & Validate"
    assert "Lint & Validate" in content


def test_green_finding_announces_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    runs = [_run("Lint & Validate", status="completed", conclusion="success")]
    policy = RecordingPolicy()

    result = checker.check_and_announce(
        "example/repo",
        collect=lambda repo: runs,
        list_workflow_names=lambda *, repo: ["Lint & Validate"],
        policy=policy,
    )

    assert result["status"] == checker.GREEN
    assert policy.sent == []


def test_unmeasured_announces_nothing():
    """Deliberate: paging on every merge for CI that simply has not finished yet -- the ordinary
    state seconds after any merge -- would drown the one alert that matters (see the module
    docstring's tradeoff note and .factory/design.md)."""
    policy = RecordingPolicy()

    result = checker.check_and_announce(
        "example/repo",
        collect=lambda repo: [],
        list_workflow_names=lambda *, repo: ["Lint & Validate"],
        policy=policy,
    )

    assert result["status"] == checker.UNMEASURED
    assert policy.sent == []


def test_unreachable_is_announced_through_the_real_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))
    policy = RecordingPolicy()
    error = workflow_run_health.MissingGhRunOutputError("gh: command not found")

    def _raise(repo):
        raise error

    result = checker.check_and_announce("example/repo", collect=_raise, policy=policy)

    assert result["status"] == checker.UNREACHABLE
    assert len(policy.sent) == 1
    kind, content = policy.sent[0]
    assert kind == "workflow_run_check_unreachable"
    assert "gh: command not found" in content


# ---------------------------------------------------------------------------
# resolve_repo: explicit, then env, then `gh repo view`
# ---------------------------------------------------------------------------


def test_resolve_repo_prefers_explicit_argument():
    assert checker.resolve_repo("explicit/repo", environ={"FACTORY_REPO": "env/repo"}) == "explicit/repo"


def test_resolve_repo_falls_back_to_env():
    assert checker.resolve_repo(None, environ={"FACTORY_REPO": "env/repo"}) == "env/repo"


def test_resolve_repo_returns_none_when_gh_is_unavailable(monkeypatch):
    def _fake_run(*args, **kwargs):
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(checker.subprocess, "run", _fake_run)
    assert checker.resolve_repo(None, environ={}) is None


# ---------------------------------------------------------------------------
# main(): CLI wiring and exit codes
# ---------------------------------------------------------------------------


def test_main_returns_2_when_repo_cannot_be_resolved(monkeypatch, capsys):
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: None)
    assert checker.main([]) == 2
    assert "could not determine OWNER/REPO" in capsys.readouterr().err


def test_main_returns_0_on_green(monkeypatch, capsys):
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: "example/repo")
    monkeypatch.setattr(
        checker, "check_and_announce", lambda repo: {"status": checker.GREEN, "failing_workflows": []}
    )
    assert checker.main([]) == 0
    assert "GREEN" in capsys.readouterr().err


def test_main_returns_1_on_red(monkeypatch, capsys):
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: "example/repo")
    monkeypatch.setattr(
        checker,
        "check_and_announce",
        lambda repo: {"status": checker.RED, "failing_workflows": ["Lint & Validate"]},
    )
    assert checker.main([]) == 1
    err = capsys.readouterr().err
    assert "RED" in err
    assert "Lint & Validate" in err


def test_main_returns_3_on_unmeasured(monkeypatch, capsys):
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: "example/repo")
    monkeypatch.setattr(
        checker,
        "check_and_announce",
        lambda repo: {"status": checker.UNMEASURED, "failing_workflows": []},
    )
    assert checker.main([]) == 3
    assert "UNMEASURED" in capsys.readouterr().err


def test_main_returns_2_on_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: "example/repo")
    monkeypatch.setattr(
        checker,
        "check_and_announce",
        lambda repo: {"status": checker.UNREACHABLE, "failing_workflows": [], "error": "boom"},
    )
    assert checker.main([]) == 2
    assert "UNREACHABLE" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# GATE FINDING 1 (#924): the GREEN line must state the bound of what it measured
# ---------------------------------------------------------------------------


def test_the_green_line_says_it_did_not_measure_this_merges_own_runs(monkeypatch, capsys):
    """The check is branch-scoped, so a bare "GREEN" printed under someone's merge would claim
    something about THAT merge which nothing here established.

    This asserts the QUALIFICATION, not merely the word "GREEN" --
    ``test_main_returns_0_on_green`` above already passes on the bare label, which is exactly
    why it could not catch this. The two tests are deliberately not merged: one pins the exit
    code and the state, this one pins the claim.
    """
    monkeypatch.setattr(checker, "resolve_repo", lambda explicit: "example/repo")
    monkeypatch.setattr(
        checker,
        "check_and_announce",
        lambda repo: {"status": checker.GREEN, "failing_workflows": []},
    )

    assert checker.main([]) == 0
    err = capsys.readouterr().err

    assert "GREEN" in err
    assert "last completed runs on main" in err
    assert "have not completed" in err
    # The qualification is the whole point: an unqualified line must not survive a refactor.
    assert "[main-health] GREEN\n" not in err


def test_the_green_line_is_declared_once_and_not_respelled_per_call_site():
    """`merge-pr.py` renders the same claim into the merge body. It reads this constant rather
    than repeating the sentence, so the two cannot drift into saying different things about the
    same measurement."""
    assert checker.GREEN_LINE.startswith("GREEN (")
    assert "last completed runs on main" in checker.GREEN_LINE
