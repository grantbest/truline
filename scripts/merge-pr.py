#!/usr/bin/env python3
"""Squash-merge a PR while preserving its durable commit-message provenance.

``gh pr merge --squash`` without ``--body`` drops the PR body, and with it the
Tidy-First ``Change kind:`` line that ``scripts/repo_invariants.py`` RULE 7
reads back out of merge commit history. ``scripts/merge-pr.sh`` already fixes
this for the shell path; this module composes the identical squash-commit
body (``Change kind:`` line, then any ``Outer-loop:``/``Dispatched-bead id:``
provenance lines) so its refusal logic can be exercised without a network in
``scripts/tests/`` while producing the same durable history the shell path
does — either can be the one an operator reaches for.

Two refusals happen before any merge call is made:

1. The PR body must carry exactly one bare ``Change kind: structural`` or
   ``Change kind: behavioral`` line, outside code fences. Zero or many is a
   refusal, not a guess.
2. The PR's head branch must not be the base of any other *open* PR. This
   helper does not pass ``--delete-branch`` — deletion is an explicit
   post-step an operator takes after confirming nothing stacks — but a repo
   with "automatically delete head branches" enabled can still strand a
   stacked PR on merge (the #328/#329 incident) without that flag, so the
   refusal stays in force regardless.

This helper never decides whether a release-gate verdict exists. That
judgment, and the accountability for it, stays with the operator running the
command — the script says so in every run, success or refusal.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


CHANGE_KIND_RE = re.compile(r"^[ \t]*Change kind:[ \t]*(structural|behavioral)[ \t]*$")
PROVENANCE_RE = re.compile(r"^[ \t]*(Outer-loop:|Dispatched-bead id:)", re.IGNORECASE)

VERDICT_NOTICE = (
    "This helper does not decide whether a release-gate verdict exists. A "
    "merge still requires one, and that judgment — and the accountability "
    "for it — stays with the operator running this command."
)


class Refusal(Exception):
    """Raised when the helper must stop before making any merge call."""


def extract_change_kind(body: str) -> str:
    """Return the single bare ``Change kind:`` line in ``body``.

    Fenced code blocks are excluded so a quoted example cannot count. Raises
    :class:`Refusal` unless exactly one declaration is present.
    """
    declarations: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if CHANGE_KIND_RE.match(line):
            declarations.append(line.strip())
    if len(declarations) != 1:
        raise Refusal(
            "PR body must carry exactly one bare 'Change kind: structural' or "
            f"'Change kind: behavioral' line; found {len(declarations)}."
        )
    return declarations[0]


def extract_provenance(body: str) -> list[str]:
    """Return up to two ``Outer-loop:``/``Dispatched-bead id:`` lines from ``body``.

    Mirrors ``scripts/merge-pr.sh``'s
    ``grep -iE '^\\s*(Outer-loop:|Dispatched-bead id:)' | head -2``: case-insensitive,
    first two matches in order, no fence exclusion (the shell script doesn't
    fence-exclude provenance either — only the Change kind declaration).
    """
    matches = [line for line in body.splitlines() if PROVENANCE_RE.match(line)]
    return matches[:2]


def compose_merge_body(change_kind: str, provenance: Sequence[str]) -> str:
    """Compose the squash commit body: ``change_kind`` then any ``provenance`` lines.

    Matches ``scripts/merge-pr.sh``'s composition exactly, so the two paths
    produce identical durable commits for the same PR body.
    """
    if not provenance:
        return change_kind
    return "\n".join([change_kind, *provenance])


def find_dependent_pr(head_ref: str, open_prs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first open PR based on ``head_ref``, or ``None``.

    ``open_prs`` is the shape of ``gh pr list --state open --json
    number,baseRefName``.
    """
    for pr in open_prs:
        if pr.get("baseRefName") == head_ref:
            return pr
    return None


def _run_gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _pr_view(pr_number: str) -> dict[str, Any]:
    return json.loads(_run_gh(["pr", "view", pr_number, "--json", "number,body,headRefName"]))


def _open_prs() -> list[dict[str, Any]]:
    return json.loads(_run_gh(["pr", "list", "--state", "open", "--json", "number,baseRefName"]))


def _merge(pr_number: str, merge_body: str) -> None:
    subprocess.run(
        ["gh", "pr", "merge", pr_number, "--squash", "--body", merge_body],
        check=True,
    )


def _load_post_merge_health_check() -> Any:
    """Load scripts/post-merge-health-check.py by file path.

    Its filename carries the hyphens every other script in this directory does, so it cannot be
    named in an ``import`` statement; ``importlib.util.spec_from_file_location`` loads it from its
    path instead, the same trick ``scripts/unattended-merge-metrics.py``'s ``_load_shared`` uses
    for a sibling module.
    """
    path = Path(__file__).resolve().with_name("post-merge-health-check.py")
    spec = importlib.util.spec_from_file_location("post_merge_health_check", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load post-merge health check from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_post_merge_health_check() -> str:
    """Best-effort post-merge tripwire (see post-merge-health-check.py's module docstring for the
    2026-09-17 incident this exists to close). Called only after ``_merge`` has already succeeded,
    so any failure here -- an import error, a `gh` hiccup, an unreachable network -- is caught and
    reported as a skip, never raised: a health-check failure must not turn a successful merge into
    a reported one.
    """
    try:
        checker = _load_post_merge_health_check()
        repo = checker.resolve_repo()
        if not repo:
            return "post-merge health check skipped: could not determine OWNER/REPO"
        result = checker.check_and_announce(repo)
        status = result["status"]
        if status == checker.RED:
            return f"post-merge health check: RED ({', '.join(result['failing_workflows'])})"
        if status == checker.UNREACHABLE:
            return f"post-merge health check: UNREACHABLE ({result.get('error')})"
        if status == checker.UNMEASURED:
            return "post-merge health check: UNMEASURED (not yet confirmed green)"
        # See post-merge-health-check.py's GREEN_LINE: this read is branch-scoped, so an
        # unqualified "GREEN" here would claim something about THIS merge that was not measured.
        return f"post-merge health check: {checker.GREEN_LINE}"
    except Exception as exc:  # noqa: BLE001 - best-effort, must never fail an already-succeeded merge
        return f"post-merge health check skipped: {exc}"


def merge_pr(pr_number: str) -> str:
    """Merge ``pr_number``, refusing before any merge call when unsafe.

    Raises :class:`Refusal` for either mechanical trap; the caller decides
    how to report that.
    """
    pr = _pr_view(pr_number)
    body = pr.get("body") or ""
    change_kind = extract_change_kind(body)
    provenance = extract_provenance(body)
    merge_body = compose_merge_body(change_kind, provenance)

    head_ref = pr.get("headRefName")
    dependent = find_dependent_pr(head_ref, _open_prs())
    if dependent is not None:
        raise Refusal(
            f"PR #{pr_number} is the base of open PR #{dependent.get('number')}; "
            "deleting its branch would strand that PR the way #328 stranded #329. "
            f"Retarget or merge PR #{dependent.get('number')} first."
        )

    _merge(pr_number, merge_body)
    health_line = run_post_merge_health_check()
    return (
        f"Merged PR #{pr_number} (squash, branch not deleted) with commit body:\n"
        f"{merge_body}\n\n{VERDICT_NOTICE}\n\n{health_line}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr_number", help="PR number to merge")
    args = parser.parse_args(argv)

    try:
        print(merge_pr(args.pr_number))
        return 0
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        print(VERDICT_NOTICE, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
