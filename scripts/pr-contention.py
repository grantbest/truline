#!/usr/bin/env python3
"""Report which open pull requests touch the same files, and which of those
overlaps actually conflict.

The audit behind this script found the collision list a release gate is
handed was assembled from memory, and got it wrong twice in one day: a
dispatch named the wrong file for a real overlap, and a second dispatch
missed a contended pair entirely until a gate found it unprompted. Neither
error changed a verdict, but the input to "merge order is part of the
verdict" (CLAUDE.md) should be a query, not a recollection. This script is
that query.

**Contention is not conflict.** Two open PRs touching one path is
contention -- usually harmless, and reported unconditionally. Whether a
given contended pair's trees actually conflict is a separate, narrower
question this script can also answer, non-destructively, via
``git merge-tree --write-tree`` (no checkout, no ``git merge``, no touching
the operator's working tree -- see ``check_conflict``).

**This tool reports facts. It does not recommend a merge order.** Ordering
depends on the gates' verdicts and on what each PR is for, neither of which
this script sees; it names who touches what and which pairs conflict, and
stops there.

**No network call is made when driven from a fixture.** ``load_fixture_prs``
reads only local files; ``load_gh_prs`` is the sole caller of ``gh``
(one ``subprocess`` invocation per PR number, requesting exactly the fields
this script needs: ``number``, ``files``, ``headRefOid``). This mirrors
``scripts/gate-prepass.py``'s ``load_fixture_prs``/``load_gh_prs`` split
without reusing that module directly -- this tool has no use for bead
resolution, CI rollups, or the substrate client that module also carries,
and reusing it would couple a reporting tool to the gate pre-pass it must
never be wired into (AC-5).

This script performs no merge and is not part of the merge path or the
pre-pass; wiring a contention report into a gate is a separate decision
with its own failure modes, since most contention today merges clean.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Sequence


REPO = pathlib.Path(__file__).resolve().parents[1]


def _normalize_path(path: str) -> str:
    return path.strip().removeprefix("./")


def _coerce_files(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    files: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            files.append(_normalize_path(entry))
        elif isinstance(entry, dict):
            path = entry.get("path") or entry.get("filename") or entry.get("name")
            if path:
                files.append(_normalize_path(str(path)))
    return tuple(files)


@dataclass(frozen=True)
class PRFiles:
    """One open PR's number, changed files, and (optionally) a head ref/sha
    resolvable in some local repo. ``head_ref`` is only used by
    ``check_conflict``; contention reporting never needs it and a fixture or
    live payload that omits it still reports contention normally.
    """

    number: int | str
    files: tuple[str, ...]
    head_ref: str | None = None


def _fixture_pr_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "pr.json").exists():
        return [root]
    direct = sorted(p for p in root.iterdir() if p.is_dir() and (p / "pr.json").exists())
    if direct:
        return direct
    return sorted(p.parent for p in root.rglob("pr.json"))


def _read_fixture_pr(path: pathlib.Path) -> PRFiles:
    payload = json.loads((path / "pr.json").read_text())
    files = _coerce_files(payload.get("files") or payload.get("changed_files"))
    if not files and (path / "files.txt").exists():
        files = tuple(
            _normalize_path(line)
            for line in (path / "files.txt").read_text().splitlines()
            if line.strip()
        )
    head_ref = payload.get("head_ref") or payload.get("headRefOid")
    return PRFiles(number=payload.get("number", path.name), files=files, head_ref=head_ref)


def load_fixture_prs(root: pathlib.Path) -> list[PRFiles]:
    """Read a committed fixture directory. Performs no network or ``gh`` call."""
    dirs = _fixture_pr_dirs(root)
    if not dirs:
        raise ValueError(f"{root} contains no pr.json fixture")
    return [_read_fixture_pr(path) for path in dirs]


def _run_gh(pr_number: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "number,files,headRefOid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view {pr_number} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def load_gh_prs(pr_numbers: Sequence[str]) -> list[PRFiles]:
    """Fetch each PR's files and head sha live via ``gh``. The only source of
    a network call anywhere in this module."""
    prs: list[PRFiles] = []
    for pr_number in pr_numbers:
        payload = _run_gh(pr_number)
        prs.append(
            PRFiles(
                number=payload.get("number", pr_number),
                files=_coerce_files(payload.get("files")),
                head_ref=payload.get("headRefOid"),
            )
        )
    return prs


@dataclass(frozen=True)
class Contention:
    path: str
    prs: tuple[Any, ...]


def find_contention(prs: Sequence[PRFiles]) -> list[Contention]:
    """Every path touched by more than one of ``prs``, each naming every PR
    that touches it. A path touched by exactly one PR is not contention and
    never appears here."""
    by_path: dict[str, list[Any]] = {}
    for pr in prs:
        for path in pr.files:
            by_path.setdefault(path, []).append(pr.number)
    return sorted(
        (Contention(path=path, prs=tuple(numbers)) for path, numbers in by_path.items() if len(numbers) > 1),
        key=lambda c: c.path,
    )


@dataclass(frozen=True)
class Neighbor:
    other: Any
    shared_paths: tuple[str, ...]


def neighbors_of(pr_number: Any, prs: Sequence[PRFiles]) -> list[Neighbor]:
    """Every other PR in ``prs`` that shares at least one path with
    ``pr_number``, and the shared paths -- the per-PR view of ``find_contention``."""
    target = next((pr for pr in prs if str(pr.number) == str(pr_number)), None)
    if target is None:
        raise ValueError(f"PR #{pr_number} is not among the PRs supplied")
    target_files = set(target.files)

    neighbors: list[Neighbor] = []
    for pr in prs:
        if str(pr.number) == str(pr_number):
            continue
        shared = tuple(sorted(target_files & set(pr.files)))
        if shared:
            neighbors.append(Neighbor(other=pr.number, shared_paths=shared))
    return sorted(neighbors, key=lambda n: str(n.other))


def contended_pairs(prs: Sequence[PRFiles]) -> dict[tuple[Any, Any], tuple[str, ...]]:
    """Every unordered pair of PRs sharing at least one path, mapped to the
    shared paths. Used to scope which pairs ``--check-conflicts`` bothers
    running ``git merge-tree`` on -- a pair with no shared path is never a
    merge-tree candidate here."""
    pairs: dict[tuple[Any, Any], tuple[str, ...]] = {}
    for i, pr_a in enumerate(prs):
        for pr_b in prs[i + 1 :]:
            shared = tuple(sorted(set(pr_a.files) & set(pr_b.files)))
            if shared:
                pairs[(pr_a.number, pr_b.number)] = shared
    return pairs


def check_conflict(
    repo_root: pathlib.Path, ref_a: str, ref_b: str
) -> tuple[bool | None, tuple[str, ...]]:
    """Whether ``ref_a`` and ``ref_b`` actually conflict, via
    ``git merge-tree --write-tree`` -- non-mutating: no checkout, no
    ``git merge``, no change to the working tree, the index, or any branch.
    ``--write-tree`` still writes a merged tree object into the object
    database (as any ``merge-tree`` invocation does); it never updates a ref
    or a working tree.

    Returns ``(True, conflicted_paths)`` if the merge conflicts,
    ``(False, ())`` if it merges clean, and ``(None, ())`` if the merge could
    not be attempted at all (e.g. a ref unresolvable in ``repo_root`` --
    never guessed into a clean or conflicting verdict).
    """
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", ref_a, ref_b],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return False, ()
    # Exit status 1 covers both "merged, but with conflicts" (tree oid on the
    # first stdout line, conflicted paths after it) and "could not even start
    # the merge" (e.g. an unresolvable ref -- git writes nothing to stdout,
    # only a fatal message to stderr). Only the former is a real conflict.
    if result.returncode == 1 and result.stdout.strip():
        conflicted = tuple(line for line in result.stdout.splitlines()[1:] if line.strip())
        return True, conflicted
    return None, ()


def render_pr_report(
    pr_number: Any,
    prs: Sequence[PRFiles],
    conflicts: dict[tuple[Any, Any], tuple[bool | None, tuple[str, ...]]] | None = None,
) -> str:
    neighbors = neighbors_of(pr_number, prs)
    lines = [f"Contention report for PR #{pr_number}"]
    if not neighbors:
        # Scope the claim to the population this call was HANDED, and name it.
        # There is no estate discovery here -- the PR list is supplied by the
        # caller -- so "shares no files with any other open PR" asserts
        # something about the world that only holds if the caller enumerated
        # it correctly. The originating incidents for this tool were
        # enumeration errors (a PR omitted from another's neighbour list), and
        # a silent omission renders identically to a genuine all-clear. A gate
        # is told to trust this line, so it must say what it was measured over.
        # str() on both sides: --pr supplies a string while a gh payload's
        # `number` is an int, which is why neighbors_of compares them this way
        # too. Without the coercion the target never matches itself and the
        # report lists the PR among its own neighbours.
        others = [
            str(other.number)
            for other in prs
            if str(other.number) != str(pr_number)
        ]
        if others:
            supplied = " ".join(f"#{number}" for number in others)
            lines.append(
                f"PR #{pr_number} shares no files with any of the "
                f"{len(others)} pull request(s) supplied: {supplied}"
            )
        else:
            lines.append(
                f"PR #{pr_number} was the only pull request supplied; "
                "no comparison was made."
            )
        return "\n".join(lines)

    for neighbor in neighbors:
        paths = ", ".join(neighbor.shared_paths)
        lines.append(f"PR #{pr_number} also touches, with PR #{neighbor.other}: {paths}")
        conflict = _lookup_conflict(conflicts, pr_number, neighbor.other)
        if conflict is not None:
            lines.append(_render_conflict_line(pr_number, neighbor.other, conflict))
    return "\n".join(lines)


def render_estate_report(
    prs: Sequence[PRFiles],
    conflicts: dict[tuple[Any, Any], tuple[bool | None, tuple[str, ...]]] | None = None,
) -> str:
    contention = find_contention(prs)
    # Name the population, not just its size: a PR that contends with nothing
    # never appears in the body below, so an omitted PR is otherwise invisible
    # to the reader of this report.
    considered = " ".join(f"#{entry.number}" for entry in prs)
    lines = [f"Contended paths across {len(prs)} open PR(s): {len(contention)}"]
    lines.append(f"Pull requests considered: {considered or '(none)'}")
    for entry in contention:
        names = " ".join(f"#{number}" for number in entry.prs)
        lines.append(f"{entry.path}: {names}")

    if conflicts:
        lines.append("")
        lines.append("Conflict checks:")
        for (pr_a, pr_b), result in sorted(conflicts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
            lines.append(_render_conflict_line(pr_a, pr_b, result))
    return "\n".join(lines)


def _lookup_conflict(
    conflicts: dict[tuple[Any, Any], tuple[bool | None, tuple[str, ...]]] | None,
    a: Any,
    b: Any,
) -> tuple[bool | None, tuple[str, ...]] | None:
    if conflicts is None:
        return None
    for key in ((a, b), (b, a)):
        if key in conflicts:
            return conflicts[key]
    return None


def _render_conflict_line(a: Any, b: Any, result: tuple[bool | None, tuple[str, ...]]) -> str:
    conflicts, paths = result
    if conflicts is None:
        return f"#{a} x #{b}: conflict check could not run"
    if conflicts:
        return f"#{a} x #{b}: CONFLICTS ({', '.join(paths)})"
    return f"#{a} x #{b}: merges clean"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prs", nargs="*", help="open PR numbers to compare, fetched live via gh")
    parser.add_argument(
        "--fixture-dir",
        type=pathlib.Path,
        help="offline fixture directory (one pr.json per PR); performs no gh/network call",
    )
    parser.add_argument(
        "--pr",
        help="report only this PR's contended neighbours; omit for the whole-estate report",
    )
    parser.add_argument(
        "--check-conflicts",
        action="store_true",
        help="also run `git merge-tree --write-tree` on every contended pair with a resolvable head ref",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=REPO,
        help="repo in which head refs are resolvable, for --check-conflicts",
    )
    args = parser.parse_args(argv)

    if bool(args.fixture_dir) == bool(args.prs):
        parser.error("provide either --fixture-dir or one or more PR numbers")

    try:
        prs = load_fixture_prs(args.fixture_dir) if args.fixture_dir else load_gh_prs(args.prs)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"pr-contention error: {exc}", file=sys.stderr)
        return 2

    conflicts: dict[tuple[Any, Any], tuple[bool | None, tuple[str, ...]]] = {}
    if args.check_conflicts:
        by_number = {pr.number: pr for pr in prs}
        for (a, b) in contended_pairs(prs):
            ref_a = by_number[a].head_ref
            ref_b = by_number[b].head_ref
            if ref_a and ref_b:
                conflicts[(a, b)] = check_conflict(args.repo_root, ref_a, ref_b)

    if args.pr is not None:
        print(render_pr_report(args.pr, prs, conflicts))
    else:
        print(render_estate_report(prs, conflicts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
