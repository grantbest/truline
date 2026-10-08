#!/usr/bin/env python3
"""A `PRIN-` id that resolves to nothing reads as doctrine and is not.

`docs/architecture/principles.md` (merged #425) is the principle registry: every
``### PRIN-NNN`` heading in it is a real, citable entry. Docs and plans cite those
ids freely — sprint plans open with an ``Applies:`` line, findings name the
principle they apply — but until this script existed nothing checked that a cited
id actually resolves. That is the exact failure `scripts/traceability.py` already
names for `docs/requirements/`: "an unresolvable reference is worse than none,
because it reads as traceability while pointing nowhere."

**Runs as pytest, not a new CI job.** `.github/workflows/**` is out of scope for a
factory task — the factory may not write the gates that judge it (F-DCE-2,
`docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md`). `lint.yml`
already runs ``python -m pytest scripts/tests/ -q`` in its "Test the checkers and
the guard meta-tests" step, so a pytest here is a gate with no workflow edit.
`scripts/tests/test_citations.py` is that gate; this module is also runnable
standalone for local use.

**Fences are respected, matching the change-kind job's awk.** ``lint.yml``'s
change-kind check toggles an ``in_fence`` flag on any line starting with ` ``` `
and skips content inside it; a `PRIN-` token inside a fenced code example (a
sample citation line, an error message) is not a real citation and must not be
flagged as one.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass, field

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_PATH = pathlib.Path("docs/architecture/principles.md")
SCAN_DIRS = (pathlib.Path("docs/architecture"), pathlib.Path("docs/plans"), pathlib.Path("docs/audits"))

# The negative lookahead keeps a 4+ digit run (e.g. "PRIN-0013") from being
# read as "PRIN-001" plus a stray digit.
CITATION_RE = re.compile(r"PRIN-\d{3}(?!\d)")
HEADING_RE = re.compile(r"^### (PRIN-\d{3})\b")
FENCE_RE = re.compile(r"^```")


@dataclass(frozen=True)
class Citation:
    """One `PRIN-NNN` token, where it was found."""

    path: pathlib.Path
    line: int
    ref: str


class RegistryUnavailable(RuntimeError):
    """The registry file could not be read, so the id population is unknown —
    not empty. Raised rather than swallowed into ``frozenset()``: a caller
    that could not tell "no registry" from "registry with zero ids" would
    resolve every real citation against nothing and report the whole
    population as dangling, which is exactly the failure PRIN-015 names
    (a fault must announce itself with its cause attached, not masquerade as
    the thing it prevented from being checked). This used to return
    ``frozenset()`` here on the same reasoning `traceability.load_registries`
    once used for a missing `docs/requirements/` directory; this was the
    third, unfixed copy of that choice.

    ATTRIBUTIONS CHECKED AGAINST MERGE STATE, NOT RECOLLECTION (gate finding
    on #934, which found this paragraph crediting an unmerged PR):
    the `scan_error` shape reused above came from **#714** (`3d1172a2`), which
    introduced `ScanUnavailable` and the `scan_error` field in
    `requirement_citations.py`. **#838** (`c41c9000`) made a missing
    `docs/requirements/` directory visible in `traceability.py` via
    `Registry.directory_missing` rather than silently empty — note it made the
    absence VISIBLE, it did not make the sibling raise. **#918 is still OPEN**
    as of 2026-09-18 and wires those two together; it is NOT completed
    remediation and must not be cited as though it were.

    That precision is the point of this paragraph rather than pedantry: this
    defect propagated BY DOCSTRING, and a comment recording an unmerged PR as
    done would hand the next reader a false precedent — the same mechanism,
    one turn later.
    """


def load_registry(repo: pathlib.Path) -> frozenset[str]:
    """Every id with a `### PRIN-NNN` heading in the registry.

    Raises :class:`RegistryUnavailable` when the registry file itself is
    missing — see that class for why this no longer returns an empty set.
    """
    path = repo / REGISTRY_PATH
    if not path.exists():
        raise RegistryUnavailable(f"{REGISTRY_PATH} not found")
    ids = set()
    for line in path.read_text().splitlines():
        match = HEADING_RE.match(line)
        if match:
            ids.add(match.group(1))
    return frozenset(ids)


def find_markdown_files(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every `*.md` under the scanned directories, deduplicated and sorted."""
    files: set[pathlib.Path] = set()
    for rel_dir in SCAN_DIRS:
        directory = repo / rel_dir
        if directory.exists():
            files.update(directory.rglob("*.md"))
    return sorted(files)


def find_citations(path: pathlib.Path) -> list[Citation]:
    """Every `PRIN-NNN` token in `path`, outside fenced code blocks."""
    citations: list[Citation] = []
    in_fence = False
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in CITATION_RE.finditer(line):
            citations.append(Citation(path=path, line=lineno, ref=match.group(0)))
    return citations


@dataclass
class Report:
    files_scanned: int = 0
    citations_checked: int = 0
    dangling: list[Citation] = field(default_factory=list)
    #: Set when the registry itself could not be read — distinguishes "the
    #: registry was unreachable" from "found zero violations" (PRIN-015).
    #: `dangling` stays empty in this case: an unresolvable environment is
    #: not evidence that any citation is wrong.
    scan_error: str | None = None


def audit(repo: pathlib.Path) -> Report:
    """Pure aside from the filesystem reads it must do to find its own input —
    no network, no substrate."""
    report = Report()
    try:
        registry = load_registry(repo)
    except RegistryUnavailable as exc:
        report.scan_error = str(exc)
        return report
    for path in find_markdown_files(repo):
        report.files_scanned += 1
        for citation in find_citations(path):
            report.citations_checked += 1
            if citation.ref not in registry:
                report.dangling.append(citation)
    return report


def format_report(report: Report, repo: pathlib.Path) -> str:
    if report.scan_error is not None:
        return f"principle citations: registry unreachable, cannot evaluate ({report.scan_error})"
    lines = [
        f"Files scanned:     {report.files_scanned}",
        f"Citations checked: {report.citations_checked}",
        f"Dangling:          {len(report.dangling)}",
    ]
    if report.dangling:
        lines.append("")
        lines.append("Citations that resolve to no registry entry:")
        for citation in report.dangling:
            try:
                rel = citation.path.relative_to(repo)
            except ValueError:
                rel = citation.path
            lines.append(f"  {rel}:{citation.line}: {citation.ref}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO), help="repository root")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.repo)
    report = audit(repo)
    print(format_report(report, repo))
    if report.scan_error is not None:
        return 1
    return 1 if report.dangling else 0


if __name__ == "__main__":
    sys.exit(main())
