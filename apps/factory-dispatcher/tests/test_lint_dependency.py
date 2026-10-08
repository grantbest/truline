"""The clone-local verification venv must be able to run the same linter CI runs.

OPS-30 (dev.task): a spec declared `python -m ruff check apps/`, and the
pristine-verification preflight refused to file it — the isolated clone
bootstrap_verification_env() builds installs from apps/*/requirements.txt,
and ruff was in none of them. #566 then merged, passed its declared
verification, and was refused by the required lint status check anyway,
because the version that gates the merge (.github/workflows/lint.yml's
`pip install ruff==...`) was never installed anywhere the work is verified.

These tests hold two things true at once, driven by the existing
verification_dependency_args()/bootstrap_verification_env() test seams
(tmp_path fake clones + a monkeypatched `run`), not a real clone or network
access:

  1. the version apps/factory-dispatcher/requirements.txt pins and the
     version .github/workflows/lint.yml pins are the same value, and a bump
     to one without the other fails loudly instead of drifting silently;
  2. adding that pin to requirements.txt does not break the existing
     clone-local venv construction for tasks that declare no lint command.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER_REQUIREMENTS = REPO_ROOT / "apps" / "factory-dispatcher" / "requirements.txt"
LINT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "lint.yml"


def _ruff_pin_in_requirements(text: str) -> str:
    match = re.search(r"^ruff==([^\s#]+)\s*$", text, re.MULTILINE)
    assert match, "apps/factory-dispatcher/requirements.txt has no `ruff==<version>` pin"
    return match.group(1)


def _ruff_pin_in_lint_workflow(text: str) -> str:
    match = re.search(r"pip install ruff==([^\s#]+)", text)
    assert match, ".github/workflows/lint.yml has no `pip install ruff==<version>` pin"
    return match.group(1)


def test_dispatcher_ruff_pin_matches_ci_ruff_pin():
    """The clone installs the exact ruff CI's required status check enforces.

    A linter in the verification clone that disagrees with the one CI runs
    is worse than no linter at all: it lets an agent believe a lint command
    passed while the merge-gating check still refuses the PR (or the
    reverse — refuses locally-clean work). This is the regression test for
    that: bump one pin without the other and it fails.
    """
    dispatcher_version = _ruff_pin_in_requirements(DISPATCHER_REQUIREMENTS.read_text())
    ci_version = _ruff_pin_in_lint_workflow(LINT_WORKFLOW.read_text())
    assert dispatcher_version == ci_version, (
        f"apps/factory-dispatcher/requirements.txt pins ruff=={dispatcher_version} "
        f"but .github/workflows/lint.yml pins ruff=={ci_version} — the clone-local "
        "venv would install a linter that disagrees with the required status check"
    )


def test_verification_dependency_args_includes_dispatcher_requirements(tmp_path):
    """The real requirements file (ruff pin included) reaches the pip args.

    verification_dependency_args() globs apps/*/requirements.txt and passes
    each one to pip via -r; it does not parse ruff out specifically, so this
    confirms the file the ruff pin lives in is actually picked up.
    """
    clone = tmp_path / "repo"
    app_dir = clone / "apps" / "factory-dispatcher"
    app_dir.mkdir(parents=True)
    (app_dir / "requirements.txt").write_text(DISPATCHER_REQUIREMENTS.read_text())

    args = dispatch.verification_dependency_args(clone)

    assert "-r" in args
    idx = args.index("-r")
    assert args[idx + 1] == "apps/factory-dispatcher/requirements.txt"


def test_bootstrap_verification_env_succeeds_with_ruff_pin(tmp_path, monkeypatch):
    """The clone-local venv build still succeeds with ruff added.

    Same seam as test_dispatch.py's bootstrap coverage: `run` is
    monkeypatched so no interpreter, network, or real pip install is
    involved. This is the test for the 'existing clone venv construction
    keeps working' acceptance criterion — it fails if adding the ruff pin
    ever breaks bootstrap_verification_env() for a task that declares no
    lint command at all.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    clone = tmp_path / "repo"
    app_dir = clone / "apps" / "factory-dispatcher"
    app_dir.mkdir(parents=True)
    (app_dir / "requirements.txt").write_text(DISPATCHER_REQUIREMENTS.read_text())
    (clone / "venv" / "bin").mkdir(parents=True)
    (clone / "venv" / "bin" / "python").write_text("")

    monkeypatch.setattr(dispatch, "run", fake_run)
    # dispatch.interpreter_version now shares this module's `run()` (dev.finding
    # a0166920, AC-7's size cap); this test is about venv/pip construction, not
    # interpreter discovery, so short-circuit the latter instead of teaching
    # the double to impersonate real python version checks.
    monkeypatch.setattr(dispatch, "resolve_verification_python", lambda: sys.executable)

    dispatch.bootstrap_verification_env(clone)

    pip_installs = [c for c in calls if "pip" in c and "install" in c]
    assert pip_installs, "bootstrap did not invoke pip install"
    assert any(
        "apps/factory-dispatcher/requirements.txt" in " ".join(cmd) for cmd in pip_installs
    ), "bootstrap did not request the requirements file carrying the ruff pin"
