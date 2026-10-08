"""The published CSDM-on-beads tree is a copy of the layer; this test is what keeps it one.

The tree under ``apps/substrate/publish/csdm-on-beads/`` was copied from the live
layer (R2605-6, #794). A copy with no guard drifts the first time its source moves
while every check stays green -- PRIN-005's failure mode, in the one tree that
exists to leave this repository. The rule this test enforces: never edit the copy,
generalise the source. Byte identity for the scripts and LICENSE; value equality
for the rule tables; structural equality for the twelve arch models (pydantic
folds docstrings into ``description``, so docstring edits are not drift).
"""

from __future__ import annotations

import filecmp
import importlib.util
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
TREE = REPO / "apps" / "substrate" / "publish" / "csdm-on-beads"
SRC = REPO / "apps" / "substrate" / "src"

BYTE_IDENTICAL = {
    TREE / "scripts" / "ea-conformance.py": REPO / "scripts" / "ea-conformance.py",
    TREE / "scripts" / "ea-derive.py": REPO / "scripts" / "ea-derive.py",
    TREE / "scripts" / "ea_reflect.py": REPO / "scripts" / "ea_reflect.py",
    TREE / "LICENSE": REPO / "LICENSE",
}


def _load_copy(name: str, path: pathlib.Path):
    """The published tree is self-contained: each module loads by file path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_live(module: str):
    """The live layer is a package (`from .bead_rules import ...`): import it the
    way the server does, as ``src.<module>`` with ``apps/substrate`` on sys.path."""
    root = str(SRC.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"src.{module}")


def _strip_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(v) for v in node]
    return node


def test_copied_scripts_and_licence_are_byte_identical_to_their_sources():
    drifted = [
        f"{copy.relative_to(REPO)} != {source.relative_to(REPO)}"
        for copy, source in BYTE_IDENTICAL.items()
        if not filecmp.cmp(copy, source, shallow=False)
    ]
    assert not drifted, "published copy drifted from its source -- edit the source, then re-copy:\n" + "\n".join(drifted)


def test_copied_rule_tables_equal_the_live_ones_by_value():
    copy = _load_copy("csdm_publish_bead_rules", TREE / "bead_rules.py")
    live = _load_live("bead_rules")
    assert copy.STATE_MACHINES == live.STATE_MACHINES
    assert copy.STATE_MACHINE_ENTRY_STATES == live.STATE_MACHINE_ENTRY_STATES
    assert copy.BEAD_LINK_TYPES == live.BEAD_LINK_TYPES


def test_copied_arch_models_are_structurally_identical_to_the_live_ones():
    copy = _load_copy("csdm_publish_schemas", TREE / "schemas.py")
    live = _load_live("schemas")
    assert set(copy.ARCH_TYPE_SCHEMAS) == set(live.ARCH_TYPE_SCHEMAS)
    for type_name, copy_model in copy.ARCH_TYPE_SCHEMAS.items():
        live_model = live.ARCH_TYPE_SCHEMAS[type_name]
        assert _strip_descriptions(copy_model.model_json_schema()) == _strip_descriptions(
            live_model.model_json_schema()
        ), f"arch.{type_name}: published model diverges from the live one"
