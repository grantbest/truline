#!/usr/bin/env python3
"""Mechanical eligibility predicate for the code-health auto-merge fast path.

D2 precondition from docs/plans/2026-08-18-decision-record-next-arc.md. The
19.3 flip — a code-health PR auto-merging — is amendment-class per PRIN-001
and does NOT happen here. This script only decides and reports, given a PR
number, whether it is mechanically eligible for the fast path that amendment
will stand on. It performs no merge, approval, or review, and it must remain
structurally unable to: it imports no ``subprocess`` and calls no ``gh``
subcommand of its own, delegating every ``gh``/substrate call to
``gate-prepass.py``'s already-tested loading machinery (see
``_load_gate_prepass`` below). A meta-test greps this file for merge/review
call paths and fails the suite if any is found.

Eligibility is seven independent conditions, all reported, any FAIL making
the PR INELIGIBLE:

1.  ``bead`` — the PR resolves to exactly one dev.task bead via the #432
    recogniser convention (``gate-prepass.py``'s ``find_bead_id``).
2.  ``lane-autonomy`` — the bead's lane is ``code-health`` and its
    ``autonomy`` field is exactly ``"auto"``. Absent or ``"propose"`` (the
    filing default — see ``file_task.py``) is INELIGIBLE, never rescued by
    any other condition passing.
3.  ``change-kind`` — the PR body carries exactly one bare ``Change kind:``
    declaration, and it matches the bead's ``risk_class``.
4.  ``scope`` — the diff touches only paths inside the bead's
    ``scope.paths`` and none inside ``forbidden_paths``.
5.  ``ci`` — CI conclusion on the head SHA is green. A precondition, never a
    verdict (CLAUDE.md).
6.  ``ci-coverage`` — every required CI context actually ran (OPS-147): a
    green ``ci`` from one trivial check and a green ``ci`` from every
    required suite are indistinguishable to condition 5 alone (PR #863's
    shape), and the 19.3 auto-merge amendment stands on this predicate, so
    this fast path must be able to tell them apart too. Unlike
    ``gate-prepass.py``'s pre-pass, this check is NOT advisory here: a
    SKIP (coverage undetermined) is treated the same as a FAIL, because
    auto-merge eligibility must never be granted on unmeasured data.
7.  ``verification-evidence`` — verification evidence is present in the PR
    body per the gate-prepass convention.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
from typing import Any, Sequence


REPO = pathlib.Path(__file__).resolve().parents[1]

CHECK_ORDER = (
    "bead",
    "lane-autonomy",
    "change-kind",
    "scope",
    "ci",
    "ci-coverage",
    "verification-evidence",
)


def _load_gate_prepass():
    """Load ``gate-prepass.py`` the same way it loads its own shared modules.

    The filename is hyphenated and not import-safe, so this mirrors
    ``gate-prepass.py``'s own ``_load_shared`` helper rather than
    reimplementing PR loading, bead resolution, or the substrate client here.
    """
    name = "gate_prepass"
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = pathlib.Path(__file__).resolve().with_name("gate-prepass.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load gate-prepass module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_prepass = _load_gate_prepass()

# Reused, not reimplemented — see module docstring and .factory/design.md.
PullRequest = _prepass.PullRequest
CheckResult = _prepass.CheckResult
load_fixture_prs = _prepass.load_fixture_prs
load_gh_prs = _prepass.load_gh_prs
find_bead_id = _prepass.find_bead_id


def _content_field(bead: dict[str, Any] | None, field: str) -> Any:
    """Read ``field`` off a bead dict that may or may not nest it under ``content``.

    ``gate-prepass.py``'s fixtures already exercise both shapes (flat, as
    written by hand-authored fixtures, and ``content``-nested, as the
    substrate actually returns); ``_scope_from_bead`` there does the same
    thing for ``scope`` specifically. This generalises it to any field
    without touching that function's contract.
    """
    if not bead:
        return None
    if field in bead:
        return bead[field]
    content = bead.get("content")
    if isinstance(content, dict):
        return content.get(field)
    return None


def _fmt(value: Any) -> str:
    return str(value) if value not in (None, "") else "(missing)"


def _bead_check(pr: PullRequest) -> CheckResult:
    bead_id = find_bead_id(pr.body)
    if bead_id is None:
        return CheckResult(
            pr.number,
            "bead",
            False,
            "no dispatched-bead id line and no dev.task reference recognised",
        )
    if pr.bead is None:
        return CheckResult(
            pr.number,
            "bead",
            False,
            f"recognised bead id {bead_id} but bead content unavailable",
        )
    return CheckResult(
        pr.number, "bead", True, f"resolved to exactly one dev.task bead {bead_id}"
    )


def _lane_autonomy_check(pr: PullRequest) -> CheckResult:
    if pr.bead is None:
        return CheckResult(
            pr.number,
            "lane-autonomy",
            False,
            "bead content unavailable; cannot check lane or autonomy",
        )
    lane = _content_field(pr.bead, "lane")
    autonomy = _content_field(pr.bead, "autonomy")
    ok = lane == "code-health" and autonomy == "auto"
    return CheckResult(
        pr.number, "lane-autonomy", ok, f"lane={_fmt(lane)} autonomy={_fmt(autonomy)}"
    )


def _change_kind_check(pr: PullRequest) -> CheckResult:
    if pr.bead is None:
        return CheckResult(
            pr.number,
            "change-kind",
            False,
            "bead content unavailable; cannot check change kind against risk_class",
        )
    risk_class = _content_field(pr.bead, "risk_class")
    matches = _prepass.CHANGE_KIND_RE.findall(pr.body)
    if len(matches) != 1:
        return CheckResult(
            pr.number,
            "change-kind",
            False,
            f"expected exactly one Change kind line, found {len(matches)}",
        )
    declared = matches[0]
    ok = declared == risk_class
    return CheckResult(
        pr.number,
        "change-kind",
        ok,
        f"declared '{declared}' vs bead risk_class '{_fmt(risk_class)}'",
    )


def _scope_check(pr: PullRequest, guards: Any) -> CheckResult:
    if pr.bead is None:
        return CheckResult(
            pr.number,
            "scope",
            False,
            "bead content unavailable; cannot check changed files against scope",
        )
    scope = _prepass._scope_from_bead(pr.bead)
    if not scope:
        return CheckResult(pr.number, "scope", False, "bead content supplied no scope")

    scope = dict(scope)
    forbidden = list(scope.get("forbidden_paths") or [])
    forbidden.extend(_prepass.FACTORY_ALWAYS_FORBIDDEN)
    scope["forbidden_paths"] = forbidden

    verdict = guards.check_scope(pr.changed_files, scope)
    if verdict.ok:
        return CheckResult(
            pr.number,
            "scope",
            True,
            f"{len(pr.changed_files)} changed file(s) checked within bead scope",
        )
    return CheckResult(pr.number, "scope", False, verdict.describe())


def evaluate_prs(
    prs: Sequence[PullRequest], required_contexts: Sequence[str] | None = None
) -> list[CheckResult]:
    guards = _prepass._load_dispatcher_guards()
    results: list[CheckResult] = []
    for pr in prs:
        checks = {
            "bead": _bead_check(pr),
            "lane-autonomy": _lane_autonomy_check(pr),
            "change-kind": _change_kind_check(pr),
            "scope": _scope_check(pr, guards),
            "ci": _prepass._ci_check(pr),
            "ci-coverage": _prepass._ci_coverage_check(pr, required_contexts),
            "verification-evidence": _prepass._verification_evidence_check(pr),
        }
        results.extend(checks[name] for name in CHECK_ORDER)
    return results


def render_report(results: Sequence[CheckResult]) -> str:
    lines = [
        "Fast-path eligibility check",
        "ELIGIBLE is a mechanical predicate only; it merges, approves, and "
        "comments on nothing. The attended buffer this feeds is a detail of "
        "the 19.3 amendment (PRIN-007), not this script.",
        "",
    ]
    by_pr: dict[Any, list[CheckResult]] = {}
    order: list[Any] = []
    for result in results:
        if result.pr not in by_pr:
            by_pr[result.pr] = []
            order.append(result.pr)
        by_pr[result.pr].append(result)

    blocks: list[str] = []
    for pr_number in order:
        pr_results = by_pr[pr_number]
        block = [
            f"PR #{pr_number} "
            f"{'SKIP' if not result.ran else 'PASS' if result.ok else 'FAIL'} "
            f"{result.check}: {result.evidence}"
            for result in pr_results
        ]
        # A SKIP is treated the same as a FAIL for eligibility (CheckResult.ok
        # is already False whenever ran=False): auto-merge eligibility must
        # never be granted on data this checker could not measure.
        eligible = all(result.ok for result in pr_results)
        block.append(f"PR #{pr_number}: {'ELIGIBLE' if eligible else 'INELIGIBLE'}")
        blocks.append("\n".join(block))

    return "\n".join(lines) + "\n\n".join(blocks)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prs", nargs="*", help="PR numbers to fetch with gh")
    parser.add_argument("--fixture-dir", type=pathlib.Path, help="offline fixture directory")
    parser.add_argument("--bead-dir", type=pathlib.Path, help="directory of bead JSON files for gh PRs")
    parser.add_argument("--substrate-url", default=os.environ.get(_prepass._substrate.URL_ENV))
    parser.add_argument("--substrate-key", default=os.environ.get(_prepass._substrate.KEY_ENV))
    parser.add_argument(
        "--required-contexts",
        default=os.environ.get("GATE_PREPASS_REQUIRED_CONTEXTS"),
        help=(
            "comma-separated list of CI context names the ci-coverage "
            "condition requires (or set GATE_PREPASS_REQUIRED_CONTEXTS); "
            "same source and default as gate-prepass.py's own flag"
        ),
    )
    args = parser.parse_args(argv)

    if bool(args.fixture_dir) == bool(args.prs):
        parser.error("provide either --fixture-dir or one or more PR numbers")

    required_contexts = _prepass.resolve_required_contexts(args.required_contexts)

    try:
        prs = (
            load_fixture_prs(args.fixture_dir)
            if args.fixture_dir
            else load_gh_prs(args.prs, args.bead_dir, args.substrate_url, args.substrate_key)
        )
        results = evaluate_prs(prs, required_contexts)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"check-fast-path error: {exc}", file=sys.stderr)
        return 2

    print(render_report(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
