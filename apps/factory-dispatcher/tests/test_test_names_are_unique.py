"""S56-2a0: no test function name may be defined in more than one file under
``apps/factory-dispatcher/tests/``.

pytest tolerates a name defined in two files -- it hides whether the two pin
the same behaviour or two different ones, and selecting by ``-k <name>``
silently runs both. This walks every git-tracked ``apps/factory-dispatcher/
tests/*.py`` with ``ast`` and asserts every ``FunctionDef``/``AsyncFunctionDef``
whose name starts with ``test`` (including methods of test classes) is
defined in exactly one file.

``apps/mcp-hub/tests`` and ``scripts/tests`` are out of this ratchet's scope.
"""

from __future__ import annotations

import ast
import subprocess
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _test_function_names(source: str) -> list[str]:
    tree = ast.parse(source)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def _duplicated_names(sources_by_rel_path: dict[str, str]) -> dict[str, list[str]]:
    name_to_files: dict[str, list[str]] = defaultdict(list)
    for rel_path, source in sorted(sources_by_rel_path.items()):
        for name in _test_function_names(source):
            name_to_files[name].append(rel_path)
    return {name: files for name, files in name_to_files.items() if len(files) > 1}


def _git_tracked_dispatcher_test_sources(root: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-files", "apps/factory-dispatcher/tests/*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    rel_paths = out.stdout.splitlines()
    assert rel_paths, "expected at least one tracked test file"
    return {rel_path: (root / rel_path).read_text() for rel_path in rel_paths}


def test_no_test_function_name_is_defined_in_more_than_one_file():
    sources = _git_tracked_dispatcher_test_sources(REPO_ROOT)
    dupes = _duplicated_names(sources)
    assert dupes == {}, "\n".join(
        f"{name} is defined in more than one file: {', '.join(files)}"
        for name, files in sorted(dupes.items())
    )


def test_the_scan_sees_class_method_names_not_only_module_level_functions(tmp_path):
    """Fixture regression for the scan itself (AC-6): a scan restricted to
    module-level functions would miss a duplicated name that is a method of
    a test class in two different files, since ``ast.walk`` (not a
    module-level-only iteration) is what finds it."""
    (tmp_path / "fixture_a.py").write_text(
        "class TestA:\n    def test_shared(self):\n        pass\n"
    )
    (tmp_path / "fixture_b.py").write_text(
        "class TestB:\n    def test_shared(self):\n        pass\n"
    )
    sources = {
        "fixture_a.py": (tmp_path / "fixture_a.py").read_text(),
        "fixture_b.py": (tmp_path / "fixture_b.py").read_text(),
    }

    dupes = _duplicated_names(sources)

    assert dupes == {"test_shared": ["fixture_a.py", "fixture_b.py"]}
