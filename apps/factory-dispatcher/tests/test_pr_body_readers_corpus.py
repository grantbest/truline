"""AC-7 (dispatcher side): change_apply.has_emergency_marker and classify_change_kind must read
the 15 real, previously-merged PR bodies in scripts/tests/fixtures/pr-body-readers/corpus/ exactly
as they did on main (corpus/expected.json). No `gh`, no network: everything here is a committed
fixture, read by its repo path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))

from activities import change_apply as ca  # noqa: E402

MERGE_PR_MODULE = ca._load_merge_pr_module()
FIXTURES = _REPO_ROOT / "scripts" / "tests" / "fixtures" / "pr-body-readers" / "corpus"


def _expected() -> dict:
    return json.loads((FIXTURES / "expected.json").read_text())


def _pr_numbers() -> list[int]:
    return sorted(int(n) for n in _expected()["records"].keys())


def _body(number: int) -> str:
    return json.loads((FIXTURES / f"{number}.body.json").read_text())["body"]


@pytest.mark.parametrize("number", _pr_numbers())
def test_has_emergency_marker_matches_recorded(number):
    record = _expected()["records"][str(number)]
    assert ca.has_emergency_marker(_body(number)) == record["emergency_marker"]


@pytest.mark.parametrize("number", _pr_numbers())
def test_classify_change_kind_matches_recorded(number):
    record = _expected()["records"][str(number)]
    body = _body(number)

    change_type, _gap = ca.classify_change_kind(
        body,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )

    if record["emergency_marker"]:
        assert change_type == "emergency"
    else:
        declaration = record["merge_pr_py_change_kind"].get("declaration")
        assert declaration is not None, "corpus record carries a change-kind refusal, not a declaration"
        kind = declaration.rsplit(":", 1)[1].strip()
        assert change_type == ca.CHANGE_TYPE_BY_KIND[kind]
