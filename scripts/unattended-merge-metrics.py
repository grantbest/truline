#!/usr/bin/env python3
"""Measure, from records the merge path already produces, whether unattended
merging is safe — PC-FAC-005/AC-1's denominator.

This module counts three things and reports nothing else:

1.  How many merges happened with no human executing the merge (``unattended``,
    keyed on the *recorded* merge actor type and a resolvable ``dev.task``
    reference — never on a bead's filed ``autonomy`` field: see
    ``.factory/design.md`` for why the flag and the outcome are not the same
    fact).
2.  How many of those merges were later undone by a recognised ``git revert``
    (never by "a later fix landed nearby" — see ``REVERT_TRAILER_RE``).
3.  How long undoing one took — reported as ``unknown``, never ``0``, until a
    rollback-timing source exists (R26.02/O-11, gated on O-10). Reporting
    ``0`` here would assert an undo took no time when the truth is that
    nothing measured it.

Every function here is a pure computation over records the caller supplies —
no substrate, no network, no GitHub API, no clock read. This script does not
merge, gate, or otherwise participate in the merge path; it only reads what
that path already recorded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence


REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_shared(name: str):
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = pathlib.Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load shared module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_markers = _load_shared("gate_markers")
find_bead_id = _markers.find_bead_id

# git's own revert-commit trailer (also what GitHub's "Revert" button writes
# into the revert PR body): "This reverts commit <sha>." — the mechanical
# fact that a merge was undone, distinct from a PR that merely mentions or
# fixes a prior change in prose.
REVERT_TRAILER_RE = re.compile(
    r"(?im)^\s*This reverts commit\s+([0-9a-fA-F]{7,40})\.?\s*$"
)

TIME_TO_UNDO_UNKNOWN_REASON = (
    "no rollback-timing source exists yet (R26.02/O-11, gated on O-10 image "
    "digests); a git revert timestamp says when the repo changed, not how "
    "long the undone change was live"
)


@dataclass(frozen=True)
class MergeRecord:
    """One merge, as GitHub and the merge path already recorded it.

    ``merged_by_type`` is GitHub's own actor typing for whoever executed the
    merge (``"User"`` or ``"Bot"``) — a fact about what happened, not a flag
    either side declared beforehand. ``autonomy`` is carried through from the
    originating bead for cross-tabulation only; it never enters the
    unattended predicate (see ``.factory/design.md``).
    """

    pr_number: int | str
    merge_commit: str
    merged_at: str
    body: str
    merged_by_login: str
    merged_by_type: str
    autonomy: str | None = None


@dataclass(frozen=True)
class Window:
    since: str | None
    until: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"since": self.since, "until": self.until}


def record_from_dict(data: dict[str, Any]) -> MergeRecord:
    merged_by = data.get("merged_by") or {}
    return MergeRecord(
        pr_number=data["pr_number"],
        merge_commit=str(data["merge_commit"]),
        merged_at=str(data["merged_at"]),
        body=str(data.get("body") or ""),
        merged_by_login=str(merged_by.get("login") or ""),
        merged_by_type=str(merged_by.get("type") or ""),
        autonomy=data.get("autonomy"),
    )


def is_unattended(record: MergeRecord) -> bool:
    """A merge nobody executed by hand: a Bot-typed merge actor, on a PR that
    resolves to a ``dev.task`` bead (an ``Outer-loop: true`` PR carries no
    such reference and is attended by construction — see gate_markers.py)."""
    return record.merged_by_type == "Bot" and find_bead_id(record.body) is not None


def reverted_merge_commits(records: Sequence[MergeRecord]) -> set[str]:
    """Merge commits any record's body names as reverted, via the recognised
    ``git revert`` trailer only — a later, unrelated fix does not match."""
    reverted: set[str] = set()
    for record in records:
        for sha in REVERT_TRAILER_RE.findall(record.body):
            reverted.add(sha.lower())
    return {
        record.merge_commit.lower()
        for record in records
        if record.merge_commit.lower() in reverted
    }


def _window(records: Sequence[MergeRecord], since: str | None, until: str | None) -> Window:
    if since is not None or until is not None:
        return Window(since=since, until=until)
    if not records:
        return Window(since=None, until=None)
    timestamps = sorted(r.merged_at for r in records)
    return Window(since=timestamps[0], until=timestamps[-1])


def compute_metrics(
    records: Sequence[MergeRecord],
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Pure function: identical ``records`` (and ``since``/``until``) always
    produce an identical report. No I/O, no clock read.

    ``since``/``until``, if given, are compared lexicographically against
    ``merged_at`` (ISO-8601 sorts correctly this way) to select the window;
    callers wanting a rolling window pass this call's own boundaries in.
    """
    windowed = [
        r for r in records if (since is None or r.merged_at >= since) and (until is None or r.merged_at <= until)
    ]

    unattended = [r for r in windowed if is_unattended(r)]
    undone_shas = reverted_merge_commits(windowed)
    unattended_undone = [r for r in unattended if r.merge_commit.lower() in undone_shas]

    return {
        "window": _window(windowed, since, until).to_dict(),
        "total_merges": len(windowed),
        "unattended_merges": len(unattended),
        "unattended_merges_undone": len(unattended_undone),
        "time_to_undo": {
            "status": "unknown",
            "duration_seconds": None,
            "reason": TIME_TO_UNDO_UNKNOWN_REASON,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records-json",
        required=True,
        type=pathlib.Path,
        help="path to a JSON array of already-fetched merge records "
        "(this script performs no gh/git/network calls itself)",
    )
    parser.add_argument("--since", default=None, help="ISO-8601 lower bound on merged_at")
    parser.add_argument("--until", default=None, help="ISO-8601 upper bound on merged_at")
    args = parser.parse_args(argv)

    raw = json.loads(args.records_json.read_text())
    records = [record_from_dict(item) for item in raw]
    report = compute_metrics(records, since=args.since, until=args.until)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
