"""OPS-181/OPS-182 regression: the standing status bead is found by content_ref
through the store's own indexed lookup, never by scanning a page of `list_beads`.

`find_status` used to page `list_beads("observation")` and scan for a matching
`content.ref`. `arch.observation` grows by ~45-52 dated per-night mints, newest
first, so a caller's fixed page size (1000) is a countdown: three days after the
population crossed it, `obs.ea-apply-status` sat at page position 1116 and every
tick treated the record as absent, attempting a `create` the store's own
unique-ref constraint (migration 0006_unique_arch_ref) refused with 409 -- which
a separate finance-integrity mapper then relabelled as a plaid duplicate
(OPS-182). `requirements_apply` shares `find_status` and sat at position 673 of
the same page, closing on the same failure from a different starting point.

Everything here runs against the committed snapshot
(`fixtures/arch-observation-page-2026-09-19.json`, decision record 2026-09-13
D7: no test in this bead reads the live store) -- ordering, ids and the two
status positions are the load-bearing facts it preserves; every other `ref` is
redacted.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import activities as activities_pkg  # noqa: E402
from activities import ea_apply, requirements_apply, status_bead  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "arch-observation-page-2026-09-19.json"

#: The callers' historical page size (status_bead.py used to default `list_beads`'s
#: `limit` to this) -- not reused as a fix, only as the boundary the regression
#: pins against.
HISTORICAL_PAGE_SIZE = 1000


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _as_bead(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": "observation",
        "state": row["state"],
        "content": {"ref": row["ref"]},
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


class FixtureSubstrate:
    """Doubles the store's two observation-read routes against the committed
    OPS-181 snapshot.

    `list_beads` mirrors the live route's own behaviour (measured 2026-09-19):
    it returns up to the first `limit` rows in snapshot (newest-first) order and
    never refuses a large `limit` -- the fix here is not "ask for more rows".
    `find_bead` mirrors the store's own indexed `content_ref` lookup: it
    resolves the match directly, at any position, without walking a page.
    """

    def __init__(self) -> None:
        fixture = _load_fixture()
        self._observations: list[dict[str, Any]] = fixture["observations"]
        self.list_calls = 0
        self.find_calls = 0

    def list_beads(self, bead_type: str, limit: int = 1000) -> list[dict[str, Any]]:
        self.list_calls += 1
        assert bead_type == "observation"
        return [_as_bead(row) for row in self._observations[:limit]]

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict[str, Any] | None:
        self.find_calls += 1
        assert namespace == "arch"
        assert type == "observation"
        for row in self._observations:
            if row["ref"] == content_ref:
                return _as_bead(row)
        return None


def _old_scan_find_status(sub: FixtureSubstrate, status_ref: str) -> dict[str, Any] | None:
    """The pre-fix shape `status_bead.find_status` used to have: a page of
    `list_beads`, scanned linearly for a matching `content.ref`. Reproduced here,
    not reimported, purely to pin the failure this bead fixes -- the shipped
    module no longer contains this code path at all."""
    for bead in sub.list_beads("observation"):
        if (bead.get("content") or {}).get("ref") == status_ref:
            return bead
    return None


def test_fixture_positions_the_ea_apply_status_bead_beyond_the_historical_page():
    fixture = _load_fixture()
    assert fixture["ea_apply_status_index"] >= HISTORICAL_PAGE_SIZE
    assert fixture["observations"][fixture["ea_apply_status_index"]]["ref"] == ea_apply.STATUS_REF


def test_fixture_positions_the_requirements_apply_status_bead():
    fixture = _load_fixture()
    idx = fixture["requirements_apply_status_index"]
    assert fixture["observations"][idx]["ref"] == requirements_apply.STATUS_REF


def test_old_scan_shape_missed_the_ea_apply_status_bead_beyond_the_page():
    sub = FixtureSubstrate()
    assert _old_scan_find_status(sub, ea_apply.STATUS_REF) is None


def test_new_lookup_finds_the_ea_apply_status_bead_the_old_scan_missed():
    sub = FixtureSubstrate()
    found = status_bead.find_status(sub, ea_apply.STATUS_REF)
    assert found is not None
    assert found["content"]["ref"] == ea_apply.STATUS_REF


def test_new_lookup_finds_the_requirements_apply_status_bead():
    sub = FixtureSubstrate()
    found = status_bead.find_status(sub, requirements_apply.STATUS_REF)
    assert found is not None
    assert found["content"]["ref"] == requirements_apply.STATUS_REF


def test_find_status_makes_no_list_call_at_all():
    """The fix is not a larger `limit` -- it is not listing at all."""
    sub = FixtureSubstrate()

    status_bead.find_status(sub, ea_apply.STATUS_REF)

    assert sub.list_calls == 0
    assert sub.find_calls == 1


def test_double_returns_at_most_limit_rows_and_never_refuses_a_large_one():
    """The live route accepts limit=1001 and limit=2000 and returns that many rows
    (measured 2026-09-19) -- the double must not refuse a large limit either, so a
    fix that just raised `limit` would be visibly not this fix (it would still pass
    the old scan against a bigger page, for a while)."""
    sub = FixtureSubstrate()
    total = len(sub._observations)

    assert len(sub.list_beads("observation", limit=1001)) == 1001
    assert len(sub.list_beads("observation", limit=2000)) == total


# --- neither caller of status_bead may scan list_beads to find its status record ----------------


def _activity_modules_importing_status_bead():
    pkg_dir = Path(activities_pkg.__file__).resolve().parent
    modules = []
    for _, name, _ in pkgutil.iter_modules([str(pkg_dir)]):
        if name == "status_bead":
            continue
        module = importlib.import_module(f"activities.{name}")
        if getattr(module, "status_bead", None) is status_bead:
            modules.append(module)
    return modules


def test_status_bead_find_status_source_contains_no_list_beads_call():
    body = inspect.getsource(status_bead.find_status).split('"""', 2)[-1]
    assert "list_beads" not in body


def test_every_module_importing_status_bead_resolves_its_status_without_listing():
    modules = _activity_modules_importing_status_bead()
    names = {module.__name__ for module in modules}
    assert "activities.ea_apply" in names
    assert "activities.requirements_apply" in names

    for module in modules:
        find_status_fn = getattr(module, "find_status", None)
        if find_status_fn is None:
            continue
        sub = FixtureSubstrate()
        find_status_fn(sub)
        assert sub.list_calls == 0, f"{module.__name__}.find_status scanned list_beads"
