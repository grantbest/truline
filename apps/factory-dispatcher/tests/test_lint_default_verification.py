"""The worker verifies everything except the lint that gates every merge.

Declared verification (preflight before the worker runs, verify after) ran
whatever a spec named and nothing else. The CI required status check
(`.github/workflows/lint.yml`'s `python-lint` job) is a separate, independent
gate, so a spec that never thought to declare a `ruff` command shipped
lint-red work that passed its own verification and was refused by the merge
gate anyway -- an SRE last-mile per instance.

These tests hold three things true:

  1. a spec that declares no lint command still gets the pinned ruff check,
     scoped to the paths the task actually changed, added at the dispatcher
     level (`dispatch.effective_verification_commands`, wired into
     `verify_activity`) rather than something every future spec must
     remember;
  2. a spec that already declares its own ruff invocation does not get a
     second one;
  3. a genuinely lint-red file fails the same execution path
     (`dispatch.verify_declared_commands`) `verify_activity` uses, before a
     PR would ever open.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import containment  # noqa: E402
import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def _task(commands: list[str]) -> dict:
    return {
        "id": "task-1",
        "content": {
            "lane": "bug-triage",
            "title": "t",
            "verification": {"commands": commands},
        },
    }


# ---------------------------------------------------------------------------
# dispatch.effective_verification_commands: the composition itself
# ---------------------------------------------------------------------------


def test_adds_pinned_ruff_check_when_spec_declares_no_lint_command(tmp_path):
    clone = tmp_path / "repo"
    pkg = clone / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n")

    commands = dispatch.effective_verification_commands(
        _task(["python -m pytest tests/ -q"]), clone, ["pkg/mod.py"]
    )

    assert "python -m pytest tests/ -q" in commands
    lint_commands = [c for c in commands if "ruff" in c]
    assert len(lint_commands) == 1
    assert lint_commands[0].startswith("python -m ruff check ")
    assert "pkg/mod.py" in lint_commands[0]


def test_declared_ruff_invocation_is_not_run_twice(tmp_path):
    clone = tmp_path / "repo"
    pkg = clone / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n")
    declared = ["python -m ruff check apps/", "python -m pytest tests/ -q"]

    commands = dispatch.effective_verification_commands(
        _task(declared), clone, ["pkg/mod.py"]
    )

    assert commands == declared
    assert sum("ruff" in c for c in commands) == 1


def test_no_lint_command_added_when_nothing_python_changed(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "README.md").write_text("hello\n")

    commands = dispatch.effective_verification_commands(
        _task(["python -m pytest tests/ -q"]), clone, ["README.md"]
    )

    assert commands == ["python -m pytest tests/ -q"]


def test_deleted_python_path_is_not_a_lint_target(tmp_path):
    """A path git reports as changed (a deletion) that no longer exists in
    the clone must not be handed to ruff -- mirrors smoke_check_python's own
    filter one function up in dispatch.py."""
    clone = tmp_path / "repo"
    clone.mkdir()

    assert dispatch.default_lint_command(clone, ["gone.py"]) is None


def test_declares_lint_command_is_word_bounded():
    """A command that happens to contain 'ruff' as a substring of another
    word must not be mistaken for a declared lint invocation."""
    assert not dispatch.declares_lint_command(["python -m pytest truffle_tests/ -q"])
    assert dispatch.declares_lint_command(["ruff check apps/"])


# ---------------------------------------------------------------------------
# activities.dispatch_steps.verify_activity: the wiring
# ---------------------------------------------------------------------------


def _capturing_verify(received: dict):
    def fake_verify_declared_commands(_clone, commands):
        received["commands"] = list(commands)
        return dispatch.VerificationReport(
            tuple(
                dispatch.VerificationCommandResult(
                    command=c, outcome="passed", exit_code=0, output="", duration_s=0.01
                )
                for c in commands
            ),
            bootstrap_note="stub bootstrap",
        )

    return fake_verify_declared_commands


def test_verify_activity_composes_the_lint_check_for_a_spec_with_none_declared(
    monkeypatch, tmp_path
):
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "changed.py").write_text("x = 1\n")
    received: dict = {}
    monkeypatch.setattr(
        dispatch, "verify_declared_commands", _capturing_verify(received)
    )

    dispatch_steps.verify_activity(
        {
            "clone": str(clone),
            "task": _task(["python -m pytest tests/ -q"]),
            "paths": ["changed.py"],
        }
    )

    assert "python -m pytest tests/ -q" in received["commands"]
    assert any(
        c.startswith("python -m ruff check ") and "changed.py" in c
        for c in received["commands"]
    )


def test_verify_activity_does_not_double_run_a_declared_ruff_command(
    monkeypatch, tmp_path
):
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "changed.py").write_text("x = 1\n")
    received: dict = {}
    monkeypatch.setattr(
        dispatch, "verify_declared_commands", _capturing_verify(received)
    )
    declared = ["python -m ruff check apps/", "python -m pytest tests/ -q"]

    dispatch_steps.verify_activity(
        {"clone": str(clone), "task": _task(declared), "paths": ["changed.py"]}
    )

    assert received["commands"] == declared


# ---------------------------------------------------------------------------
# a genuinely lint-red fixture, run through the same execution path
# verify_activity uses
# ---------------------------------------------------------------------------

RUFF_BINARY = shutil.which("ruff")


@pytest.mark.skipif(RUFF_BINARY is None, reason="ruff not installed on PATH")
def test_lint_red_fixture_fails_before_a_pr_would_open(tmp_path, monkeypatch):
    """dispatch.verify_declared_commands is the exact function verify_activity
    calls (through the heartbeat wrapper). bootstrap_verification_env is
    stubbed out so this stays hermetic -- no pip install, no network -- while
    still running a real ruff against a real lint violation, proving the
    execution path (not just the composition) catches it."""
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "bad.py").write_text("import os\n")  # F401: imported, never used
    monkeypatch.setattr(
        dispatch, "bootstrap_verification_env", lambda _clone: "stub bootstrap"
    )
    # Test seam only (dev.finding 6c19f60f AC-2/AC-7): this suite may itself
    # be running nested inside the dispatcher's own sandbox, where a second
    # sandbox_apply always fails (rc 71) regardless of the inner profile --
    # skip only the wrap, so the real ruff execution this test cares about
    # stays real.
    monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)

    report = dispatch.verify_declared_commands(
        clone, [f"{RUFF_BINARY} check bad.py"]
    )

    assert not report.ok
    assert report.failed


@pytest.mark.skipif(RUFF_BINARY is None, reason="ruff not installed on PATH")
def test_lint_clean_fixture_passes_the_same_path(tmp_path, monkeypatch):
    """Contrast case: the same execution path passes clean code, so the red
    fixture above is failing on the lint violation and not on the wiring."""
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "good.py").write_text("import os\n\nprint(os.getcwd())\n")
    monkeypatch.setattr(
        dispatch, "bootstrap_verification_env", lambda _clone: "stub bootstrap"
    )
    monkeypatch.setattr(containment, "contained_argv", lambda argv, _profile: argv)

    report = dispatch.verify_declared_commands(
        clone, [f"{RUFF_BINARY} check good.py"]
    )

    assert report.ok


def test_composed_lint_respects_ruff_toml_exclusions(tmp_path):
    """A task touching a path ruff.toml extend-excludes (the deliberately
    malformed scan fixtures) must not be refused for lint CI never imposes:
    explicitly-passed paths bypass exclusions unless --force-exclude rides
    the composed command. The exact check CI runs, not a stricter cousin.
    """
    (tmp_path / "scripts" / "tests" / "fixtures").mkdir(parents=True)
    fixture = tmp_path / "scripts" / "tests" / "fixtures" / "leaky.py"
    fixture.write_text("undefined_name_on_purpose\n")
    (tmp_path / "ruff.toml").write_text(
        'extend-exclude = ["scripts/tests/fixtures"]\n'
    )

    command = dispatch.default_lint_command(
        tmp_path, ["scripts/tests/fixtures/leaky.py"]
    )

    assert command is not None
    assert "--force-exclude" in command

    import shutil
    import subprocess

    if shutil.which("ruff") is None:
        pytest.skip("ruff not on PATH")
    completed = subprocess.run(
        ["ruff", *command.split()[3:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
