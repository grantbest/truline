#!/usr/bin/env python3
"""Emit which test suites a change obliges, reading .github/test-map.yaml.

R3.2 of docs/plans/2026-08-18-gitops-resilience-sprints.md: the changes-gate
job in lint.yml pipes `git diff --name-only` into this script and suite jobs
run on its outputs. Filtering happens at the JOB level (`if:` against these
outputs) and never at the workflow level — a required check whose workflow
never triggers wedges the PR, while a skipped job reports and satisfies.

Precedence per changed file, most conservative rule first:

  1. a `run-everything` path -> run_all=true (a change to CI itself
     invalidates every selection this script could make);
  2. any suite `paths` match -> those suites are obliged (a file may oblige
     several);
  3. an `exempt` path -> obliges nothing beyond the universal tier;
  4. no match at all -> run_all=true. Over-matching is the safe direction:
     an unnecessary suite costs minutes, an untested change costs an
     incident. Unmapped paths are reported on stderr so the map gets fixed.

Any event other than pull_request also emits run_all=true — the
merge-to-main run is the T1 safety net that makes T0 selection trustworthy.

Output (stdout, $GITHUB_OUTPUT format): `run_all=true|false` plus one
`<suite>=true|false` line per suite, kebab-case keys emitted as snake_case
because hyphens need bracket syntax in workflow expressions.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MAP = REPO / ".github" / "test-map.yaml"

# Sentinels survive re.escape so `**` semantics can be restored afterwards.
_DS_SLASH = "\x00"  # `**/` — zero or more whole directories
_DS = "\x01"        # `**`  — anything, across slashes


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a test-map glob to a full-path regex.

    `**` crosses directory boundaries, `*` stays within one segment —
    fnmatch treats `*` like `**`, which would over-match `*.md` into
    `docs/x.md` and silently exempt documentation-adjacent code paths.
    """
    tokened = pattern.replace("**/", _DS_SLASH).replace("**", _DS)
    escaped = re.escape(tokened)
    escaped = escaped.replace(re.escape(_DS_SLASH), r"(?:[^/]+/)*")
    escaped = escaped.replace(re.escape(_DS), r".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    return re.compile(rf"^{escaped}$")


def _regexes(paths: list[str] | None) -> list[re.Pattern[str]]:
    return [glob_to_regex(p) for p in (paths or [])]


def classify(
    files: list[str], test_map: dict
) -> tuple[dict[str, bool], bool, list[str]]:
    """Return (suite -> obliged, run_all, unmapped files)."""
    suites: dict[str, bool] = {key: False for key in test_map.get("suites", {})}
    suite_res = {
        key: _regexes(spec.get("paths"))
        for key, spec in (test_map.get("suites") or {}).items()
    }
    exempt_res = _regexes((test_map.get("exempt") or {}).get("paths"))
    run_all_res = _regexes((test_map.get("run-everything") or {}).get("paths"))

    run_all = False
    unmapped: list[str] = []
    for name in files:
        name = name.strip()
        if not name:
            continue
        if any(r.match(name) for r in run_all_res):
            run_all = True
            continue
        obliged = False
        for key, res in suite_res.items():
            if any(r.match(name) for r in res):
                suites[key] = True
                obliged = True
        if obliged:
            continue
        if any(r.match(name) for r in exempt_res):
            continue
        unmapped.append(name)
        run_all = True
    return suites, run_all, unmapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="GITHUB_EVENT_NAME")
    parser.add_argument(
        "--files-from",
        default="-",
        help="newline-separated changed files; '-' reads stdin",
    )
    parser.add_argument("--map", type=pathlib.Path, default=DEFAULT_MAP)
    args = parser.parse_args(argv)

    test_map = yaml.safe_load(args.map.read_text())

    if args.event != "pull_request":
        suites = {key: False for key in test_map.get("suites", {})}
        run_all, unmapped = True, []
    else:
        if args.files_from == "-":
            raw = sys.stdin.read()
        else:
            raw = pathlib.Path(args.files_from).read_text()
        names = [line for line in raw.splitlines() if line.strip()]
        if not names:
            # A real PR always changes at least one file; an empty list here
            # means the upstream diff failed or mis-targeted. Failing open
            # (skip everything, PR reads green) is the one outcome this
            # design must never produce.
            print("empty changed-file list on a pull_request forces run_all", file=sys.stderr)
            suites = {key: False for key in test_map.get("suites", {})}
            run_all, unmapped = True, []
        else:
            suites, run_all, unmapped = classify(names, test_map)

    for name in unmapped:
        print(f"unmapped path forces run_all: {name}", file=sys.stderr)

    print(f"run_all={str(run_all).lower()}")
    for key in sorted(suites):
        print(f"{key.replace('-', '_')}={str(suites[key]).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
