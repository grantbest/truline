"""Resolved-bug test citation checker.

`known-bugs.md` is allowed to carry historical resolved bugs with no test
citation only when the bug is explicitly listed in the grandfather file. The
grandfather file is a ratchet: it can shrink, but entries may not be added back.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence


REPO = pathlib.Path(__file__).resolve().parent.parent
KNOWN_BUGS = pathlib.Path("docs/reference/known-bugs.md")
GRANDFATHER = pathlib.Path("scripts/bug-citations-grandfathered.txt")
CUTOFF = dt.date(2026, 8, 1)

_ENTRY_RE = re.compile(r"^(?:#{3,6}\s+|-\s+\*\*)\s*(B-\d{3})\b")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_BUG_ID_RE = re.compile(r"^B-\d{3}$")
_MARKDOWN_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_RAW_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+tests/[A-Za-z0-9_./:-]+")


@dataclasses.dataclass(frozen=True)
class BugEntry:
    bug_id: str
    start_line: int
    heading: str
    text: str
    section_resolved: bool
    section_date: dt.date | None

    @property
    def resolved(self) -> bool:
        return self.section_resolved or has_resolved_marker(self.text)

    @property
    def resolved_date(self) -> dt.date | None:
        return resolution_date(self.text, self.section_date)


@dataclasses.dataclass(frozen=True)
class TestCitation:
    raw: str
    path: pathlib.Path


@dataclasses.dataclass
class CheckResult:
    errors: list[str] = dataclasses.field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def has_resolved_marker(text: str) -> bool:
    """True for the resolved spellings in known-bugs.md, but not partials."""
    upper = text.upper()
    for match in re.finditer(r"\bRESOLVED\b", upper):
        before = upper[max(0, match.start() - 24):match.start()]
        if re.search(r"(PARTIALLY|MOSTLY)\s+$", before):
            continue
        return True
    return False


def _first_date(text: str) -> dt.date | None:
    match = _DATE_RE.search(text)
    return dt.date.fromisoformat(match.group(1)) if match else None


def resolution_date(text: str, inherited: dt.date | None = None) -> dt.date | None:
    for line in text.splitlines():
        stripped = line.strip()
        if (
            has_resolved_marker(stripped)
            or stripped.startswith("**Resolution")
            or stripped.startswith("**Fix")
        ):
            found = _first_date(stripped)
            if found:
                return found
    return inherited


def parse_known_bugs(path: pathlib.Path) -> list[BugEntry]:
    lines = path.read_text().splitlines()
    entries: list[BugEntry] = []
    current: tuple[str, int, str, bool, dt.date | None, list[str]] | None = None
    section_resolved = False
    section_date: dt.date | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        bug_id, start_line, heading, inherited_resolved, inherited_date, body = current
        entries.append(
            BugEntry(
                bug_id=bug_id,
                start_line=start_line,
                heading=heading,
                text="\n".join(body),
                section_resolved=inherited_resolved,
                section_date=inherited_date,
            )
        )
        current = None

    for idx, line in enumerate(lines, start=1):
        if line.startswith("## ") and not line.startswith("### "):
            flush()
            section_resolved = has_resolved_marker(line)
            section_date = _first_date(line)
            continue

        match = _ENTRY_RE.match(line)
        if match:
            flush()
            current = (
                match.group(1),
                idx,
                line,
                section_resolved,
                section_date,
                [line],
            )
            continue

        if current is not None:
            current[-1].append(line)

    flush()
    return entries


def _clean_candidate(raw: str) -> str:
    token = raw.strip().strip("<>()[]{}")
    token = token.rstrip(".,;")
    return token.split("#", 1)[0]


def find_test_citations(text: str) -> list[TestCitation]:
    raw_candidates: list[str] = []
    raw_candidates.extend(_MARKDOWN_LINK_RE.findall(text))
    raw_candidates.extend(_BACKTICK_RE.findall(text))
    raw_candidates.extend(_RAW_PATH_RE.findall(text))

    citations: list[TestCitation] = []
    seen: set[tuple[str, pathlib.Path]] = set()
    for raw in raw_candidates:
        cleaned = _clean_candidate(raw)
        if "://" in cleaned:
            continue
        path_part = cleaned.split("::", 1)[0]
        path = pathlib.PurePosixPath(path_part)
        if path.is_absolute():
            continue
        if "tests" not in path.parts:
            continue
        key = (raw, pathlib.Path(path))
        if key in seen:
            continue
        seen.add(key)
        citations.append(TestCitation(raw=raw, path=pathlib.Path(path)))
    return citations


def existing_test_citations(repo: pathlib.Path, text: str) -> tuple[list[TestCitation], list[TestCitation]]:
    existing: list[TestCitation] = []
    missing: list[TestCitation] = []
    for citation in find_test_citations(text):
        if (repo / citation.path).exists():
            existing.append(citation)
        else:
            missing.append(citation)
    return existing, missing


def parse_grandfather_text(text: str) -> list[str]:
    bug_ids: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _BUG_ID_RE.match(line):
            raise ValueError(f"line {line_no}: expected B-###, got {line!r}")
        bug_ids.append(line)
    return bug_ids


def read_grandfather(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    return parse_grandfather_text(path.read_text())


def _git_show(repo: pathlib.Path, spec: str) -> str | None:
    result = subprocess.run(
        ["git", "show", spec],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


def git_baseline_sets(repo: pathlib.Path, grandfather_relpath: pathlib.Path) -> list[set[str]]:
    rel = grandfather_relpath.as_posix()
    baselines: list[set[str]] = []
    for rev in ("HEAD", "HEAD^"):
        text = _git_show(repo, f"{rev}:{rel}")
        if text is None:
            continue
        try:
            baselines.append(set(parse_grandfather_text(text)))
        except ValueError:
            continue
    return baselines


def grandfather_candidates(entries: Iterable[BugEntry], repo: pathlib.Path) -> list[str]:
    ids: list[str] = []
    for entry in entries:
        if not entry.resolved:
            continue
        date = entry.resolved_date
        if date is not None and date >= CUTOFF:
            continue
        existing, _missing = existing_test_citations(repo, entry.text)
        if not existing:
            ids.append(entry.bug_id)
    return sorted(set(ids))


def render_grandfather(ids: Sequence[str]) -> str:
    body = "\n".join(ids)
    return (
        "# Resolved known-bugs entries grandfathered from the test-citation rule.\n"
        "#\n"
        "# APPEND-NEVER: this file is a ratchet. Entries may be removed after a\n"
        "# resolved bug cites an existing test, but new identifiers must not be\n"
        "# added and removed identifiers must not come back.\n"
        "# Generated from docs/reference/known-bugs.md on 2026-08-01.\n"
        "\n"
        f"{body}\n"
    )


def check(
    repo: pathlib.Path,
    known_bugs_relpath: pathlib.Path = KNOWN_BUGS,
    grandfather_relpath: pathlib.Path = GRANDFATHER,
    baseline_sets: Sequence[set[str]] | None = None,
) -> CheckResult:
    result = CheckResult()
    known_bugs_path = repo / known_bugs_relpath
    grandfather_path = repo / grandfather_relpath

    entries = parse_known_bugs(known_bugs_path)
    entries_by_id = {entry.bug_id: entry for entry in entries}
    if len(entries_by_id) != len(entries):
        seen: set[str] = set()
        for entry in entries:
            if entry.bug_id in seen:
                result.error(f"{entry.bug_id}: duplicate known-bugs entry")
            seen.add(entry.bug_id)

    try:
        grandfather_ids = read_grandfather(grandfather_path)
    except ValueError as exc:
        result.error(f"{grandfather_relpath}: {exc}")
        grandfather_ids = []
    grandfather = set(grandfather_ids)

    for bug_id in sorted(grandfather - set(entries_by_id)):
        result.error(f"{grandfather_relpath}: {bug_id} is not present in {known_bugs_relpath}")

    if baseline_sets is None:
        baseline_sets = git_baseline_sets(repo, grandfather_relpath)
    for baseline in baseline_sets:
        added = sorted(grandfather - baseline)
        if added:
            result.error(
                f"{grandfather_relpath}: grandfather list grew; remove added identifier(s): "
                + ", ".join(added)
            )

    for entry in entries:
        if not entry.resolved or entry.bug_id in grandfather:
            continue

        date = entry.resolved_date
        existing, missing = existing_test_citations(repo, entry.text)
        for citation in missing:
            result.error(
                f"{entry.bug_id} line {entry.start_line}: cited test path does not exist: "
                f"{citation.raw}"
            )
        if not existing and not missing:
            date_text = date.isoformat() if date else "no resolution date"
            result.error(
                f"{entry.bug_id} line {entry.start_line}: resolved entry ({date_text}) "
                "must cite an existing test path under a tests/ directory"
            )

    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=REPO)
    parser.add_argument("--known-bugs", type=pathlib.Path, default=KNOWN_BUGS)
    parser.add_argument("--grandfather", type=pathlib.Path, default=GRANDFATHER)
    parser.add_argument(
        "--write-grandfather-list",
        action="store_true",
        help="regenerate the current historical grandfather list and exit",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    if args.write_grandfather_list:
        entries = parse_known_bugs(repo / args.known_bugs)
        ids = grandfather_candidates(entries, repo)
        (repo / args.grandfather).write_text(render_grandfather(ids))
        print(f"Wrote {len(ids)} grandfathered bug identifier(s) to {args.grandfather}")
        return 0

    result = check(repo, args.known_bugs, args.grandfather)
    if result.errors:
        print("known-bugs test citations: FAIL")
        print("\n".join(f"  {error}" for error in result.errors))
        return 1

    print("known-bugs test citations: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
