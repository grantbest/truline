"""R26.12 B4 AC-3: the callers ratchet.

``guards.is_runnable`` and ``queue_order.trailing_environmental_fault_streak``
compose the full answer only inside ``queue_order.selectable_verdict`` now --
every other reader (``dispatch.pick_task``, the claim-time re-check,
``schedule_status``, the hub's ``task_runnability``) gets its verdict from
``selectable_verdict`` and computes nothing of its own (AC-2). This walks the
git-tracked ``*.py`` under ``apps/factory-dispatcher`` and
``apps/mcp-hub/src`` with ``ast`` and asserts the only modules calling either
name are ``queue_order.py``, ``guards.py``, and files under a ``tests/``
directory. A planted caller in a synthetic tmp tree proves the walker
actually detects a violation, not merely that none exist today.

No substrate, no network: this reads source text from disk (the real
checkout, or a tmp_path fixture) and parses it with ``ast`` -- nothing is
imported or executed.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two names the ratchet watches -- see queue_order.py's own module
#: docstring and this bead's design record for why only these two.
_WATCHED_ATTRS = {"is_runnable", "trailing_environmental_fault_streak"}

#: Relative-to-root paths exempt outright, regardless of directory.
_ALLOWED_EXACT = {
    "apps/factory-dispatcher/queue_order.py",
    "apps/factory-dispatcher/guards.py",
}


def _is_allowed_caller(rel_path: str) -> bool:
    if rel_path in _ALLOWED_EXACT:
        return True
    return "tests" in Path(rel_path).parts


def _violating_lines(source: str) -> list[int]:
    """Line numbers of every call of a watched name in ``source``, through an
    attribute access, a bare name, or a dynamic ``getattr`` lookup.

    A bare-name call (``ast.Call`` on an ``ast.Name``) is what
    ``from guards import is_runnable`` followed by ``is_runnable(...)``
    looks like -- just as much a caller as ``guards.is_runnable(...)``, and
    not caught by the attribute-only check this started as.
    ``getattr(<x>, "<watched>")`` is the same dodge through a string-keyed
    dynamic lookup instead of a name.

    The ``ImportFrom`` that brings the bare name into scope is deliberately
    NOT itself a violation: ``dispatch.py`` re-exports
    ``trailing_environmental_fault_streak`` from ``queue_order`` (imported,
    never called -- every call site below is a comment) for callers that
    reach it as ``dispatch.trailing_environmental_fault_streak``, and that
    import predates this bead. Flagging the import itself would make the
    real checkout fail its own ratchet; flagging the bare-name *call* below
    still catches the case this bead cares about (a reader that imports the
    name specifically to invoke it)."""
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _WATCHED_ATTRS:
            lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in _WATCHED_ATTRS:
            lines.append(node.lineno)
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _WATCHED_ATTRS
        ):
            lines.append(node.lineno)
    return lines


def _find_violations(files_by_rel_path: dict[str, str]) -> dict[str, list[int]]:
    """``files_by_rel_path`` maps a root-relative path to its source text.

    Returns the subset not already allowed that contains at least one
    watched call, each with its offending line numbers.
    """
    violations: dict[str, list[int]] = {}
    for rel_path, source in files_by_rel_path.items():
        if _is_allowed_caller(rel_path):
            continue
        lines = _violating_lines(source)
        if lines:
            violations[rel_path] = lines
    return violations


def _git_tracked_python_sources(root: Path, prefixes: tuple[str, ...]) -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, str] = {}
    for rel_path in out.stdout.splitlines():
        if not rel_path.endswith(".py") or not rel_path.startswith(prefixes):
            continue
        result[rel_path] = (root / rel_path).read_text()
    return result


def _tmp_tree_python_sources(root: Path, prefixes: tuple[str, ...]) -> dict[str, str]:
    """Same shape as ``_git_tracked_python_sources`` but a plain filesystem
    walk -- used only against a synthetic ``tmp_path`` tree, never git."""
    result: dict[str, str] = {}
    for path in root.rglob("*.py"):
        rel_path = str(path.relative_to(root))
        if not rel_path.startswith(prefixes):
            continue
        result[rel_path] = path.read_text()
    return result


# ---------------------------------------------------------------------------
# The real ratchet: no violations in the actual checkout today.
# ---------------------------------------------------------------------------


def test_no_module_outside_queue_order_guards_and_tests_calls_the_watched_names():
    sources = _git_tracked_python_sources(
        REPO_ROOT, ("apps/factory-dispatcher/", "apps/mcp-hub/src/")
    )
    assert sources, "expected at least one tracked .py file under the watched roots"
    violations = _find_violations(sources)
    assert violations == {}, f"forbidden callers of {sorted(_WATCHED_ATTRS)}: {violations}"


def test_hub_loader_may_import_queue_order_but_not_call_the_watched_names():
    """``apps/mcp-hub/src/tools/factory_status.py`` imports ``queue_order``
    (AC-2) -- that import must not itself be mistaken for a call, and the
    module must still carry none of the two watched calls."""
    source = (REPO_ROOT / "apps/mcp-hub/src/tools/factory_status.py").read_text()
    tree = ast.parse(source)
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "queue_order" in imported_names or any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "queue_order"
        for node in ast.walk(tree)
    ), "factory_status.py must import queue_order (directly or via importlib)"
    assert _violating_lines(source) == []


# ---------------------------------------------------------------------------
# Proof the walker actually catches a violation, against a synthetic tree.
# ---------------------------------------------------------------------------


def test_planted_violation_in_a_tmp_tree_is_detected(tmp_path):
    (tmp_path / "apps" / "factory-dispatcher").mkdir(parents=True)
    (tmp_path / "apps" / "factory-dispatcher" / "queue_order.py").write_text(
        "def selectable_verdict():\n    return guards.is_runnable(1, 2, 3)\n"
    )
    (tmp_path / "apps" / "factory-dispatcher" / "some_other_reader.py").write_text(
        "def read():\n    return guards.is_runnable(task, notes, tasks)\n"
    )

    sources = _tmp_tree_python_sources(tmp_path, ("apps/factory-dispatcher/",))
    violations = _find_violations(sources)

    assert "apps/factory-dispatcher/some_other_reader.py" in violations
    assert "apps/factory-dispatcher/queue_order.py" not in violations


def test_planted_bare_name_caller_in_a_tmp_tree_is_detected(tmp_path):
    """RC-3 (AC-3, kills M9b/M9c): a bare name brought in by ``from X import
    Y`` is just as much a caller as ``guards.is_runnable(...)`` -- dodging
    the attribute-access shape must not dodge the ratchet."""
    (tmp_path / "apps" / "factory-dispatcher").mkdir(parents=True)
    (tmp_path / "apps" / "factory-dispatcher" / "reader_a.py").write_text(
        "from guards import is_runnable\n\n\ndef read(t):\n    return is_runnable(t, [], [])\n"
    )
    (tmp_path / "apps" / "factory-dispatcher" / "reader_b.py").write_text(
        "from queue_order import trailing_environmental_fault_streak\n\n\n"
        "def read(notes):\n    return trailing_environmental_fault_streak(notes)\n"
    )

    sources = _tmp_tree_python_sources(tmp_path, ("apps/factory-dispatcher/",))
    violations = _find_violations(sources)

    assert "apps/factory-dispatcher/reader_a.py" in violations
    assert "apps/factory-dispatcher/reader_b.py" in violations


def test_planted_violation_under_a_tests_directory_is_exempt(tmp_path):
    (tmp_path / "apps" / "factory-dispatcher" / "tests").mkdir(parents=True)
    (tmp_path / "apps" / "factory-dispatcher" / "tests" / "test_something.py").write_text(
        "def test_x():\n    guards.is_runnable(1, 2, 3)\n"
    )

    sources = _tmp_tree_python_sources(tmp_path, ("apps/factory-dispatcher/",))
    violations = _find_violations(sources)

    assert violations == {}
