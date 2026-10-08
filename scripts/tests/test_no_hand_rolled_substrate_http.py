"""No script under scripts/ hand-rolls the substrate's `X-API-Key` header.

ea-derive.py (scripts/ea-derive.py:650, before this file's own change) was the last
hand-rolled construction site: every other script that reaches the substrate now goes
through scripts/substrate_client.py, the one place this repository's write path is
allowed to build that header. This is the repo-wide invariant pinning that, filed once
it could actually be born green (dev.task ea-derive.py migration, successor to
0b219958).

Its own predecessor's failure is exactly why this is an AST-based construction-site
scan, not a text search for the string "X-API-Key": a 2026-09-15 audit
(docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md) named four
scripts by grepping for that string, and two of the four named scripts turned out to be
a docstring mention (principles_sync.py, arch-source-class-backfill.py) rather than a
live client -- the audit's own methodology caveat, line 295, said as much. A text search
run today would repeat that mistake on release-manifest.py, whose two hits are GENERATED
DOCUMENT TEXT (a curl snippet it prints for an operator), not a header it sends. Parsing
each file and asking specifically "is this string literal the header-name argument to an
`.add_header(...)` call, or a dict key passed as a header mapping" is what tells a real
construction site apart from a docstring, a comment (never part of the AST at all), or a
string a script merely prints or returns.

Scoped to scripts/*.py (`git ls-files`, not a filesystem walk -- the latter would also
reach .claude/worktrees/** and count whole nested checkouts of this repository) and
excludes scripts/tests/: this rule is about the tools in scripts/, not about a test
fixture or double that legitimately constructs an X-API-Key-shaped example to exercise
one of them.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The one legitimate owner of this header's construction.
SHARED_CLIENT = "scripts/substrate_client.py"

_HEADER_NAME = "X-API-Key"


def _tracked_top_level_scripts() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "scripts/*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        line
        for line in output.splitlines()
        if line and not line.startswith("scripts/tests/")
    )


def _x_api_key_construction_sites(source: str) -> list[int]:
    """Line numbers of a real `X-API-Key` header construction: either
    `<expr>.add_header("X-API-Key", ...)`, or a dict literal keyed
    `"X-API-Key"` (the `headers={...}` shape scripts/substrate_client.py's own
    `_request` accepts). AST-based, so a docstring, a `#` comment (never part
    of the AST), or a plain string a script prints/returns (generated
    document text) can never match either shape -- they parse as a string
    constant, never as a call or a dict literal.
    """
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_header"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == _HEADER_NAME
        ):
            hits.append(node.lineno)
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == _HEADER_NAME:
                    hits.append(node.lineno)
    return hits


def test_no_script_outside_the_shared_client_hand_rolls_the_x_api_key_header():
    violations = []
    for relpath in _tracked_top_level_scripts():
        if relpath == SHARED_CLIENT:
            continue
        source = (REPO / relpath).read_text(encoding="utf-8")
        for lineno in _x_api_key_construction_sites(source):
            violations.append(f"{relpath}:{lineno}")

    assert violations == [], (
        "hand-rolled substrate X-API-Key construction found outside "
        f"{SHARED_CLIENT}: {violations}. Use scripts/substrate_client.py instead."
    )


def test_the_shared_client_itself_is_the_one_real_construction_site():
    """The scan finds exactly what it should: not zero real sites in the whole
    tree (which would mean the AST match is too narrow to ever fire), just
    zero *outside* the one file allowed to have it."""
    source = (REPO / SHARED_CLIENT).read_text(encoding="utf-8")
    assert _x_api_key_construction_sites(source), (
        f"{SHARED_CLIENT} itself no longer constructs the X-API-Key header -- "
        "this scan's positive case has gone stale"
    )


def test_a_docstring_mention_of_x_api_key_is_not_a_violation():
    source = '"""This client used to hand-roll the X-API-Key header by hand."""\n'
    assert _x_api_key_construction_sites(source) == []


def test_a_comment_mention_of_x_api_key_is_not_a_violation():
    source = "x = 1  # this used to send X-API-Key without the shared client\n"
    assert _x_api_key_construction_sites(source) == []


def test_generated_document_text_naming_x_api_key_is_not_a_violation():
    """release-manifest.py's own shape: a curl snippet it prints for an
    operator, which names the header as a string an operator will type --
    never a header this process itself sends."""
    source = textwrap.dedent(
        '''
        def emit_curl_snippet():
            return \'curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:18001/beads"\'
        '''
    )
    assert _x_api_key_construction_sites(source) == []


def test_a_real_add_header_call_is_a_violation():
    source = textwrap.dedent(
        """
        def _request(req, key):
            req.add_header("X-API-Key", key)
        """
    )
    assert _x_api_key_construction_sites(source) == [3]


def test_a_real_headers_dict_literal_is_a_violation():
    source = textwrap.dedent(
        """
        def build(key):
            return {"X-API-Key": key}
        """
    )
    assert _x_api_key_construction_sites(source) == [3]
