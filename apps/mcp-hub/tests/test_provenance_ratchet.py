"""AC-4: refuse any provenance dict built outside tools/provenance.py.

The checker lives here, in the test, not under src/ -- it is test
infrastructure asserting a property of production code, not itself
production code the ratchet needs to police.
"""

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
_EXCLUDED = {_SRC_ROOT / "tools" / "provenance.py"}


def _is_dict_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"


def _target_names_provenance(target: ast.AST) -> bool:
    if isinstance(target, ast.Name):
        return "provenance" in target.id
    if isinstance(target, ast.Attribute):
        return "provenance" in target.attr
    if isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return "provenance" in key.value
    return False


def find_provenance_violations(source: str) -> list:
    """Return every place ``source`` builds a provenance-shaped dict outside
    ``build_provenance``: as a ``provenance=`` keyword, as a ``'provenance'``
    dict key, as the RHS of an assignment whose target name/attribute/
    subscript-key contains ``provenance``, or any dict literal keyed
    ``'prompt_ref'``."""
    tree = ast.parse(source)
    violations = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "provenance" and _is_dict_expr(kw.value):
                    violations.append({"lineno": kw.value.lineno, "shape": "keyword_provenance"})

        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value == "provenance" and _is_dict_expr(value):
                        violations.append({"lineno": value.lineno, "shape": "dict_key_provenance"})
                    if key.value == "prompt_ref":
                        violations.append({"lineno": node.lineno, "shape": "dict_key_prompt_ref"})

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and _is_dict_expr(value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(_target_names_provenance(t) for t in targets):
                    violations.append({"lineno": value.lineno, "shape": "assign_provenance"})

    return violations


def _src_files():
    return [p for p in sorted(_SRC_ROOT.rglob("*.py")) if p not in _EXCLUDED]


def test_no_provenance_dict_built_outside_the_builder():
    offenders = {}
    for path in _src_files():
        found = find_provenance_violations(path.read_text())
        if found:
            offenders[str(path.relative_to(_SRC_ROOT))] = found
    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "create_bead('alert', {}, 'active', 'x', provenance={'generator': 'budget-pulse/persist'})",
        "payload = {'provenance': {'chain': []}}",
        "provenance_chain: dict = {'agent': 'a'}",
        "x = {'worker': 'w', 'prompt_ref': 'p'}",
    ],
)
def test_checker_flags_each_known_shape_exactly_once(source):
    assert len(find_provenance_violations(source)) == 1
