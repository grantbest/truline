"""AST-based inventory of every process-spawning call under apps/factory-dispatcher's
non-test source, and of the subset that does not yet take an explicit child
environment (dev.finding a0166920, seam half; SEC-a0166920-2 is the migration that
drives the unfixed count to zero).

Calls are recognised through each file's OWN imports, not by spelling: `import
subprocess as sp; sp.run(...)` and `from subprocess import run as r; r(...)` are both
recognised, exactly as `subprocess.run(...)` is. Importable by both this bead's
test_spawn_inventory.py and SEC-a0166920-2's own meta-assertion, per AC-7.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_TRACKED_MODULES = ("subprocess", "asyncio", "os", "pty")
_SUBPROCESS_SPAWN_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
_ASYNCIO_SPAWN_ATTRS = {"create_subprocess_exec", "create_subprocess_shell"}


def _is_os_spawn_attr(attr: str) -> bool:
    return (
        attr in ("system", "popen")
        or attr.startswith("exec")
        or attr.startswith("spawn")
        or attr.startswith("posix_spawn")
    )


@dataclass(frozen=True)
class SpawnCall:
    path: str  # POSIX-style, relative to apps/factory-dispatcher
    function: str  # enclosing qualified function name ("<module>" at module level)
    line: int
    fixed: bool  # True: an explicit env= keyword, not None and not os.environ, is present


class _ImportTracker:
    """Resolves a Call's `func` to (module, attribute-or-name) via this file's imports."""

    def __init__(self) -> None:
        self.module_alias: dict[str, str] = {}
        self.from_alias: dict[str, tuple[str, str]] = {}

    def visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in _TRACKED_MODULES:
                self.module_alias[alias.asname or alias.name] = alias.name

    def visit_import_from(self, node: ast.ImportFrom) -> None:
        if node.module in _TRACKED_MODULES:
            for alias in node.names:
                self.from_alias[alias.asname or alias.name] = (node.module, alias.name)

    def resolve(self, func: ast.expr) -> tuple[str, str] | None:
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = self.module_alias.get(func.value.id)
            if module is not None:
                return module, func.attr
            return None
        if isinstance(func, ast.Name):
            return self.from_alias.get(func.id)
        return None

    def is_os_environ(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and self.module_alias.get(node.value.id) == "os"
            and node.attr == "environ"
        )


def _has_fixed_env_keyword(call: ast.Call, tracker: _ImportTracker) -> bool:
    for kw in call.keywords:
        if kw.arg != "env":
            continue
        value = kw.value
        if isinstance(value, ast.Constant) and value.value is None:
            return False
        if tracker.is_os_environ(value):
            return False
        return True
    return False


class _SpawnVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.tracker = _ImportTracker()
        self.stack: list[str] = []
        self.calls: list[SpawnCall] = []

    def _qualified(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    def visit_Import(self, node: ast.Import) -> None:
        self.tracker.visit_import(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.tracker.visit_import_from(node)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self.tracker.resolve(node.func)
        if resolved is not None:
            module, attr = resolved
            matched = False
            fixed = False
            if module == "subprocess" and attr in _SUBPROCESS_SPAWN_ATTRS:
                matched = True
                fixed = _has_fixed_env_keyword(node, self.tracker)
            elif module == "asyncio" and attr in _ASYNCIO_SPAWN_ATTRS:
                matched = True
                fixed = _has_fixed_env_keyword(node, self.tracker)
            elif module == "os" and _is_os_spawn_attr(attr):
                matched = True
                fixed = False  # os.* forms cannot take an explicit child env; always count
            elif module == "pty" and attr == "spawn":
                matched = True
                fixed = False
            if matched:
                self.calls.append(
                    SpawnCall(self.rel_path, self._qualified(), node.lineno, fixed)
                )
        self.generic_visit(node)


def _iter_source_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] == "tests":
            continue
        if path.name.startswith("test_"):
            continue
        yield path


def find_spawn_calls(root: Path) -> list[SpawnCall]:
    """List A: every process-spawning call under `root`'s non-test .py files."""
    calls: list[SpawnCall] = []
    for path in _iter_source_files(root):
        rel_path = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _SpawnVisitor(rel_path)
        visitor.visit(tree)
        calls.extend(visitor.calls)
    return calls


def find_unfixed_spawn_calls(root: Path) -> list[SpawnCall]:
    """List B: the subset of `find_spawn_calls` that does not yet take an explicit,
    non-None, non-os.environ child environment."""
    return [call for call in find_spawn_calls(root) if not call.fixed]
