"""R26.12 B2 (docs/plans/2026-09-23-design-r2612-steerable-and-legible.md, "Queue
order"): the selector's gates and the environmental-fault breaker moved out of
dispatch.py into queue_order.py verbatim. This proves the move by comparing
``ast.dump()`` of each moved unit against the pre-move source recorded in
tests/fixtures/queue-order-move-source-2026-09-25.json -- never by reading the
diff, and never by reading git history (the fixture is the committed,
read-only record of the pre-move source; this test does not regenerate it).

R26.12 B4 extends ``selectable_verdict`` to report every hold reason (not
just the first) and rewrites ``first_selectable`` to delegate to it instead
of reimplementing ``guards.is_runnable`` plus the breaker inline -- B2's own
byte-for-byte proof of ``first_selectable``'s *for-loop body* (the
``pick_task.for`` fixture entry) no longer holds, by design, and was removed
from this file; ``UNITS["pick_task.for"]`` is left unread, orphaned in the
fixture JSON this test may not edit. The weaker structural shape
(``test_first_selectable_body_is_only_docstring_for_and_return_none``:
docstring, one for-loop, ``return None``) still holds and stays.
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import dispatch  # noqa: E402
import queue_order  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "queue-order-move-source-2026-09-25.json"

with open(FIXTURE_PATH) as _f:
    _FIXTURE = json.load(_f)

UNITS = _FIXTURE["units"]

#: Every moved unit's name, excluding the two reference-only entries
#: ("pick_task" is recorded for reference only; "pick_task.for" is compared
#: against first_selectable's body, not a module-level name).
MOVED_NAMES = sorted(name for name in UNITS if name not in ("pick_task", "pick_task.for"))

#: R26.12 B3 / PC-EXE-007 (dev.findings 6706292f, 7e208eea) intentionally
#: changed these two units' source after the move: the worker-finished note
#: joined the outcome prefixes, and the scanner now stops at
#: guards.requeue_barrier_at. They are exempted from the verbatim-AST
#: comparison below BY NAME ONLY -- the identity and non-redefinition checks
#: further down still cover them like every other moved unit.
_B3_CHANGED_NAMES = frozenset({"_DISPATCH_OUTCOME_NOTE_PREFIXES", "trailing_environmental_fault_streak"})


def _queue_order_tree() -> ast.Module:
    return ast.parse(Path(queue_order.__file__).read_text())


def _dispatch_tree() -> ast.Module:
    return ast.parse(Path(dispatch.__file__).read_text())


def _module_level_defs(tree: ast.Module) -> dict[str, ast.AST]:
    by_name: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            by_name[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    by_name[target.id] = node
    return by_name


def _first_selectable_node() -> ast.FunctionDef:
    by_name = _module_level_defs(_queue_order_tree())
    node = by_name.get("first_selectable")
    assert node is not None, "queue_order.py must define first_selectable at module scope"
    return node


def test_every_moved_unit_matches_fixture_source_verbatim():
    by_name = _module_level_defs(_queue_order_tree())
    for name in MOVED_NAMES:
        if name in _B3_CHANGED_NAMES:
            continue
        actual_node = by_name.get(name)
        assert actual_node is not None, f"{name} is missing from queue_order.py"
        fixture_module = ast.parse(textwrap.dedent(UNITS[name]["source"]))
        assert len(fixture_module.body) == 1, name
        assert ast.dump(actual_node) == ast.dump(fixture_module.body[0]), (
            f"{name} in queue_order.py no longer matches its pre-move source"
        )


def test_first_selectable_body_is_only_docstring_for_and_return_none():
    first_selectable = _first_selectable_node()
    body = list(first_selectable.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    assert len(body) == 2, "first_selectable body must be [for, return None] after any docstring"
    for_node, return_node = body
    assert isinstance(for_node, ast.For)
    assert isinstance(return_node, ast.Return)
    assert isinstance(return_node.value, ast.Constant) and return_node.value.value is None


def test_moved_names_are_not_redefined_in_dispatch_py():
    by_name = _module_level_defs(_dispatch_tree())
    for name in MOVED_NAMES:
        assert name not in by_name, (
            f"{name} is still defined by `def`/assignment in dispatch.py; it must "
            "arrive only by import from queue_order"
        )


def test_dispatch_names_are_the_same_objects_as_queue_order():
    for name in MOVED_NAMES:
        assert hasattr(dispatch, name), f"dispatch.{name} no longer resolves"
        assert getattr(dispatch, name) is getattr(queue_order, name), (
            f"dispatch.{name} is not the same object as queue_order.{name}"
        )


#: AC-1's denylist. Checked as a denylist, not an allowlist, so later arc
#: beads (B7: datetime, dataclasses, json, math; the FIFO kill switch: os)
#: can add ordinary standard-library imports without editing this test.
_FORBIDDEN_EXACT_MODULES = {
    "dispatch",
    "substrate_client_loader",
    "beadstore",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "temporalio",
}
_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def test_queue_order_denylist_imports_and_clock_reads():
    tree = _queue_order_tree()
    imported = _imported_module_names(tree)
    for module_name in imported:
        assert module_name not in _FORBIDDEN_EXACT_MODULES, (
            f"queue_order.py imports forbidden module {module_name!r}"
        )
        assert not module_name.startswith("substrate"), (
            f"queue_order.py imports a substrate* module: {module_name!r}"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_NOW_ATTRS:
                raise AssertionError(
                    f"queue_order.py references .{node.attr} — it must not read the clock"
                )
            if (
                node.attr in ("time", "monotonic")
                and isinstance(node.value, ast.Name)
                and node.value.id == "time"
            ):
                raise AssertionError(
                    f"queue_order.py references time.{node.attr} — it must not read the clock"
                )
