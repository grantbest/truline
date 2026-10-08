#!/usr/bin/env python3
"""A requirement reference that resolves to nothing is free everywhere but filing.

``file_task.py``'s ``_check_traceability`` refuses to file a ``dev.task`` whose
``requirement_refs`` point nowhere, for a reason its own docstring gives: "an
unresolvable reference is worse than none, because it reads as traceability
while pointing nowhere." That check runs at exactly one boundary. Everywhere
else — a module docstring, an inline comment, a test's own description of what
it exercises — a reference with numbered acceptance criteria reads exactly like
a real, registered requirement, and nothing has ever checked whether it is one.

PC-ASR-007 was cited that way in 14 files, with per-criterion specificity
(``/AC-2`` for one subsystem, ``/AC-3`` for another), and was never in either
registry. It was found because a spec author did exactly what a reference in a
docstring invites: read it and cited it onward.

This module builds the detector, not the fix. It resolves every citation it
finds against :func:`traceability.load_registries` — the same resolver
``file_task.py`` gates filing on, not a second implementation that can drift —
and reports what does not resolve. It does not decide whether PC-ASR-006 and
PC-ASR-007 should be registered or whether the 14 files should be corrected:
that is a product-owner judgement about intent, and a checker that made it
mechanically would be inventing the traceability this exists to protect.

**This module detects; the invariant gates.** The report-only era ended
2026-09-07: #692 registered PC-ASR-006/007 from the behavior that cited them,
closing the product-owner call the stance deferred to, and
``check-repo-invariants.py``'s ``requirement-citations`` rule now FAILS a
dangling citation — except the deliberate examples and fixtures frozen by
(path, ref) pair in ``scripts/requirement-citations-grandfathered.txt``. This
module itself still only reports (its CLI stays a reader); the gate lives in
the invariant, where every other ratchet lives. Gating on ``requirement_refs`` inside a task spec under
``apps/factory-dispatcher/tasks/`` is a separate, narrower check
(``check_task_spec_requirement_refs_resolve`` in ``check-repo-invariants.py``):
that field is filed content, not prose, and the same boundary ``file_task.py``
already enforces for new specs can reasonably enforce it for every spec on
disk.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field

REPO = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS_DIR = pathlib.Path("docs/requirements")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import traceability  # noqa: E402

# Unanchored twin of traceability.REFERENCE_RE: that one matches a whole field
# value, this one finds the same shape inside a line of prose or code.
CITATION_RE = re.compile(r"\b([A-Z]{2,}-[A-Z]{2,}-\d{3})(?:/(AC-\d+))?\b")

# Extensions worth reading as source or documentation. Deliberately excludes
# lockfiles, generated bundles and binaries, which cannot carry a designed
# citation and would only cost read time.
TEXT_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".md", ".json", ".yaml", ".yml", ".sh", ".toml"}
)


@dataclass(frozen=True)
class Citation:
    """One requirement-shaped token, where it was found."""

    path: pathlib.Path
    line: int
    ref: str


class ScanUnavailable(RuntimeError):
    """``git ls-files`` could not be asked, so the tracked-file population is
    unknown — not zero. Raised rather than swallowed into ``[]``: a caller
    that treated "git failed" the same as "this repo has no tracked files"
    is exactly how a ``git archive`` export made the citations ratchet report
    clean after scanning nothing (PRIN-015)."""


def tracked_files(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every git-tracked file, relative to ``repo``.

    Tracked rather than walked: a build artefact or an untracked scratch file
    was never reviewed and citing it would report noise nobody can act on.
    Raises :class:`ScanUnavailable` when git cannot answer at all (no
    ``.git``, e.g. a tree produced by ``git archive``) — that is a different
    state from a real, empty repository, and a caller that cannot tell them
    apart cannot tell "clean" from "never ran."
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ScanUnavailable(
            (result.stderr or f"git ls-files exited {result.returncode}").strip()
        )
    return [pathlib.Path(line) for line in result.stdout.splitlines() if line]


def find_citations(path: pathlib.Path) -> list[Citation]:
    """Every requirement-shaped token in ``path``, line by line.

    A file that is not valid UTF-8 text is skipped rather than fatal — the
    same survivable-absence choice the rest of this checker family makes for a
    missing registry or a missing repository.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    citations: list[Citation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in CITATION_RE.finditer(line):
            requirement, criterion = match.group(1), match.group(2)
            ref = f"{requirement}/{criterion}" if criterion else requirement
            citations.append(Citation(path=path, line=lineno, ref=ref))
    return citations


@dataclass
class Report:
    files_scanned: int = 0
    citations_checked: int = 0
    dangling: list[Citation] = field(default_factory=list)
    #: Set when ``tracked_files`` could not ask git at all — distinguishes
    #: "scanned zero files" from "found zero violations" (PRIN-015). None of
    #: the counters above can be trusted when this is set.
    scan_error: str | None = None


def audit(repo: pathlib.Path) -> Report:
    """Pure aside from the filesystem and ``git`` reads needed to find its own
    input — no network, no substrate, no cluster."""
    registry = traceability.load_registries(repo / REQUIREMENTS_DIR)
    report = Report()
    if registry.directory_missing:
        report.scan_error = (
            f"requirements registry not found: {repo / REQUIREMENTS_DIR}"
        )
        return report
    try:
        files = tracked_files(repo)
    except ScanUnavailable as exc:
        report.scan_error = str(exc)
        return report
    for rel in files:
        if rel.suffix not in TEXT_SUFFIXES:
            continue
        path = repo / rel
        if not path.is_file():
            continue
        report.files_scanned += 1
        for citation in find_citations(path):
            report.citations_checked += 1
            if not registry.resolves(citation.ref):
                report.dangling.append(citation)
    return report


def format_report(report: Report, repo: pathlib.Path) -> str:
    """One line when nothing dangles, so 'nothing dangling' stays
    distinguishable from 'did not run' — otherwise a clean population and an
    audit that silently never executed look identical."""
    if report.scan_error is not None:
        return (
            "requirement citations: scan unavailable, cannot evaluate "
            f"({report.scan_error})"
        )
    header = (
        f"requirement citations: {report.citations_checked} checked across "
        f"{report.files_scanned} tracked file(s), {len(report.dangling)} dangling"
    )
    if not report.dangling:
        return header

    lines = [header + ":"]
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
    # Deliberately 0 regardless of what the population contains: this reports
    # a list an operator can read; deciding each entry's fate is theirs.
    return 0


if __name__ == "__main__":
    sys.exit(main())
