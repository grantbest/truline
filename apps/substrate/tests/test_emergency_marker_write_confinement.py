"""R26.12/B15: the emergency marker is intent recorded on the bead, not
execution state. This bead adds the three fields and their validation only —
no writer and no reader exist yet (B17 writes, B7/B8 read, B16 refuses an
unattested one at intake). This test proves no production module under
apps/factory-dispatcher, apps/mcp-hub/src or apps/lifeops-console/src assigns
``class_of_service``, ``expedite_reason`` or ``expedite_until`` into bead
content. Reads are unrestricted and not scanned for.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NAMES = ("class_of_service", "expedite_reason", "expedite_until")


def _tracked_files(*patterns: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line]


def _is_python_test_file(path: Path) -> bool:
    parts = path.relative_to(REPO).parts
    return "tests" in parts or path.name.startswith("test_")


# Names reused by an unrelated object are not bead content. B7's own
# QueuePosition dataclass (apps/factory-dispatcher/queue_order.py) declares a
# same-named `class_of_service` field that is a *derived read*, not a write
# into a dev.task bead's content — the AC's own text exempts it by name. The
# ast scan below only flags a write whose target base (the dict/object
# written into, or the call constructing it) is textually "content"-shaped,
# so QueuePosition's unrelated field of the same name does not trip it.
_CONTENT_SHAPED = re.compile(r"content", re.IGNORECASE)


def _base_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _python_write_offenses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenses: list[str] = []

    def record(node: ast.AST, detail: str) -> None:
        offenses.append(f"{path.relative_to(REPO)}:{node.lineno}: {detail}")

    def check_target(node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and node.attr in NAMES:
            if _CONTENT_SHAPED.search(_base_text(node.value)):
                record(node, f"attribute write .{node.attr} on a content-shaped target")
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in NAMES
        ):
            if _CONTENT_SHAPED.search(_base_text(node.value)):
                record(node, f"dict-key write [{node.slice.value!r}] on a content-shaped target")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                check_target(target)
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Dict)
                and any(_CONTENT_SHAPED.search(_base_text(t)) for t in node.targets)
            ):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and key.value in NAMES:
                        record(key, f"dict-literal key {key.value!r} assigned to a content-shaped target")
        elif isinstance(node, ast.Call) and _CONTENT_SHAPED.search(_base_text(node.func)):
            for keyword in node.keywords:
                if keyword.arg in NAMES:
                    record(keyword.value, f"keyword {keyword.arg!r} passed to a content constructor call")

    return offenses


# Same "content-shaped target" discipline as the Python scan: only flag an
# attribute/index write or an object-literal key when "content" appears on
# the same line, so an unrelated same-named field (e.g. QueuePosition's) does
# not trip it. A property-signature declaration (`name?: type;`) is a type,
# not a write.
def _typescript_write_offenses(path: Path) -> list[str]:
    offenses: list[str] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _CONTENT_SHAPED.search(line):
            continue
        for name in NAMES:
            if re.search(rf"\.{name}\s*=(?!=)", line) or re.search(
                rf"\[[\"']{name}[\"']\]\s*=(?!=)", line
            ):
                offenses.append(f"{path.relative_to(REPO)}:{lineno}: attribute/index write of {name!r} on a content-shaped target")
            elif re.search(rf"\b{name}\?\s*:", line):
                continue  # property-signature declaration, not a write
            elif re.search(rf"\b{name}\s*:", line):
                offenses.append(f"{path.relative_to(REPO)}:{lineno}: object-literal key {name!r} on a content-shaped line")
    return offenses


def test_no_production_module_assigns_the_emergency_marker_keys():
    offenses: list[str] = []

    for path in _tracked_files("apps/factory-dispatcher/*.py", "apps/mcp-hub/src/**/*.py"):
        if _is_python_test_file(path):
            continue
        offenses.extend(_python_write_offenses(path))

    for path in _tracked_files("apps/lifeops-console/src/**/*.ts", "apps/lifeops-console/src/**/*.tsx"):
        if path.name.endswith(".test.ts") or path.name.endswith(".test.tsx"):
            continue
        offenses.extend(_typescript_write_offenses(path))

    assert offenses == [], "found a write of an emergency-marker key outside this bead's scope:\n" + "\n".join(offenses)
