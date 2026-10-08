"""R2605-6/AC-3: the published CSDM-on-beads tree imports nothing household-specific.

`apps/substrate/publish/csdm-on-beads/` is meant to be installable by a
stranger — a fresh checkout of that tree alone, with none of this
platform's other applications anywhere on disk. The only way to make that a
checked fact rather than a claim is to actually import every module in the
tree from an interpreter that cannot see this platform's other apps: no
`apps/factory-dispatcher`, no `apps/mcp-hub`, no `apps/lifeops-console`, no
`apps/finance-reporting`, and no `finance_schemas` module, anywhere on
`sys.path`.
"""

from __future__ import annotations

import os

import pathlib
import subprocess
import sys

import pytest


_INTERPRETER_PASSTHROUGH = ("LD_LIBRARY_PATH", "PATH", "SYSTEMROOT")


def _interpreter_env(base: dict[str, str]) -> dict[str, str]:
    """``base`` plus only what the interpreter needs to load itself.

    These tests scrub the environment to prove isolation. On the CI runner
    ``sys.executable`` is actions/setup-python's tool-cache CPython, which finds
    ``libpython3.11.so`` only through the ``LD_LIBRARY_PATH`` setup-python
    exports -- scrubbing it fails every subprocess with "error while loading
    shared libraries" before the test's script runs (#794, 2026-09-12). The
    passthrough is the interpreter's loader inputs, never PYTHONPATH or HOME,
    so the isolation being asserted is unchanged.
    """
    env = dict(base)
    for name in _INTERPRETER_PASSTHROUGH:
        if name not in env and name in os.environ:
            env[name] = os.environ[name]
    return env

REPO = pathlib.Path(__file__).resolve().parents[2]
TREE = REPO / "apps" / "substrate" / "publish" / "csdm-on-beads"

# Every importable Python module the tree carries. Listed explicitly rather
# than globbed so a new file added to the tree without updating this list
# fails loudly (a silent gap here is a module nobody ever proved isolated).
_MODULES = ("schemas", "bead_rules")

_FORBIDDEN_SUBSTRINGS = (
    "factory-dispatcher",
    "factory_dispatcher",
    "mcp-hub",
    "mcp_hub",
    "lifeops-console",
    "lifeops_console",
    "finance-reporting",
    "finance_reporting",
    "finance_schemas",
)


def test_tree_exists():
    assert TREE.is_dir(), f"expected the publishable tree at {TREE}"
    for name in _MODULES:
        assert (TREE / f"{name}.py").is_file()


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_source_names_no_forbidden_package(module_name: str):
    """Static floor: the source text itself never spells a forbidden package."""
    source = (TREE / f"{module_name}.py").read_text()
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in source, f"{module_name}.py names forbidden package {forbidden!r}"


def test_tree_modules_import_in_a_fresh_interpreter_without_the_household_apps():
    """Dynamic floor: actually import the tree with those packages absent from sys.path.

    A fresh subprocess (python -I, which ignores PYTHON* variables) is launched;
    the child's own sys.path assignment below is what confines imports to the tree's
    directory only (not this repository's root, not `apps/`), and `cwd` set
    outside the repository entirely, so nothing about the household's other
    applications is reachable by accident — the same property a stranger's
    checkout of this tree alone would have.
    """
    script = (
        "import sys\n"
        f"sys.path[:] = [{str(TREE)!r}] + [p for p in sys.path if p]\n"
        "import schemas\n"
        "import bead_rules\n"
        "assert len(schemas.ARCH_TYPE_SCHEMAS) == 14, schemas.ARCH_TYPE_SCHEMAS\n"
        "assert len(bead_rules.BEAD_LINK_TYPES) == 19, bead_rules.BEAD_LINK_TYPES\n"
        "assert bead_rules.STATE_MACHINES, 'no state machines registered'\n"
        "forbidden = "
        + repr(_FORBIDDEN_SUBSTRINGS)
        + "\n"
        "for name, mod in list(sys.modules.items()):\n"
        "    path = getattr(mod, '__file__', '') or ''\n"
        "    for bad in forbidden:\n"
        "        assert bad not in name and bad not in path, (name, path, bad)\n"
        "print('ISOLATED-IMPORT-OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(REPO.parent),  # outside this repository entirely
        env=_interpreter_env({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "ISOLATED-IMPORT-OK" in result.stdout
