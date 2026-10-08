"""OPS-89: every dispatcher substrate store merges per-call headers, by signature.

Six stores shipped the same latent collision (`headers=self._headers` passed
alongside `**kwargs`), and #727 proved the shape crashes on the live path the
FakeStore never walks -- the reconciler's first sweep died on
`httpx.request() got multiple values for keyword argument 'headers'`.

Six hand-written seam tests fix the six. They do not close the class: store
number seven lands with the collision shape and nothing fails. This is that
check, in the shape `tests/test_store_call_surface.py` already established for
the store call surface -- derived from the source by ast, so a new store is
covered the moment it exists rather than when somebody remembers to clone a
test.

The rule: a class in `activities/` or at the dispatcher root that defines
`_request` and merges standing auth headers MUST declare `headers` as an
explicit parameter. An implementation that pops it out of `**kwargs` works
identically at runtime and is invisible to this check, which is exactly why
the check demands the explicit form (`change_apply.py` was converged for this
reason rather than left as a third idiom).
"""

from __future__ import annotations

import ast
import pathlib

DISPATCHER = pathlib.Path(__file__).resolve().parents[1]

# Modules that hold a substrate store of their own. One level per root, not a
# recursive walk: every store lives directly in `activities/` or at the
# dispatcher root today, so a new sibling needs no edit here -- but a store
# placed in a NEW SUBPACKAGE would not be scanned, which the population pin
# below cannot detect either. Widen SCAN_ROOTS when a subpackage appears.
SCAN_ROOTS = (DISPATCHER / "activities", DISPATCHER)

AUTH_HEADER_MARKER = "_headers"


def _python_files() -> list[pathlib.Path]:
    seen: dict[pathlib.Path, None] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            seen.setdefault(path.resolve(), None)
    return list(seen)


def _merges_auth_headers(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body reads ``self._headers`` -- i.e. it is a store request
    helper that carries standing auth, not an unrelated ``_request``."""
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == AUTH_HEADER_MARKER
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            return True
    return False


def _declares_headers_param(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = fn.args
    named = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return "headers" in named


def request_helpers_missing_explicit_headers() -> list[str]:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in (
                n
                for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                if fn.name != "_request" or not _merges_auth_headers(fn):
                    continue
                if not _declares_headers_param(fn):
                    offenders.append(
                        f"{path.relative_to(DISPATCHER.parent.parent)}"
                        f"::{cls.name}._request"
                    )
    return offenders


def test_every_store_request_helper_declares_an_explicit_headers_parameter():
    assert request_helpers_missing_explicit_headers() == []


def test_the_scan_actually_finds_the_stores_it_claims_to_cover():
    """A rule that silently matches nothing passes forever. Pin the population:
    the six OPS-89 stores plus change_apply plus the shared client."""
    found = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in (
                n
                for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                if fn.name == "_request" and _merges_auth_headers(fn):
                    found.add(path.name)

    expected = {
        "substrate.py",
        "change_apply.py",
        "doctrine_registry_view.py",
        "doctrine_staleness.py",
        "ea_observation.py",
        "knowledge_ingestion.py",
        "staleness_report.py",
        "worker_revision_drift.py",
    }
    missing = expected - found
    assert missing == set(), f"the scan stopped seeing known stores: {sorted(missing)}"
    assert len(found) >= len(expected)
