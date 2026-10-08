"""R26.11/O-5 (S56-1): the dispatcher's coverage ratchet is wired the way the
re-spec requires -- the baseline's threshold is half a point (at two-decimal
precision) below its own measurement, the full-run command carries both the
precision flag and that threshold, the suite's own pytest config never
measures coverage at all (so every subset run pays nothing), and CI's run
line for this suite carries no --cov option either.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO_ROOT = APP.parents[1]

BASELINE = json.loads((APP / "coverage-baseline.json").read_text())


def test_coverage_ratchet_is_wired_correctly():
    assert re.fullmatch(r"[0-9a-f]{40}", BASELINE["measured_revision"])
    assert BASELINE["fail_under_pct"] == round(BASELINE["line_coverage_pct"] - 0.5, 2)

    command = BASELINE["command"]
    assert "--cov-precision=2" in command
    assert f"--cov-fail-under={BASELINE['fail_under_pct']}" in command

    assert not (APP / "pytest.ini").exists()

    lint_yml = (REPO_ROOT / ".github" / "workflows" / "lint.yml").read_text()
    for line in lint_yml.splitlines():
        if "cd apps/factory-dispatcher && python -m pytest tests/ -q" in line:
            assert "--cov" not in line
            break
    else:
        raise AssertionError("dispatcher's CI run line not found in lint.yml")
