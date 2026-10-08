#!/usr/bin/env python3
"""Count the work that carries no intent.

The traceability contract shipped on 2026-08-02 and is deliberately unenforced.
The comment above the fields in ``schemas.py`` says exactly why:

    Optional by decision, not by oversight: the rollout warns for one sprint
    before it refuses, because the in-flight non-conforming population cannot
    currently be counted.

That sprint passed. The count was never built, so the refusal could not be
scheduled — not because anyone decided against it, but because the precondition
it was made conditional on did not exist. This is that precondition.

**Well-formed is not the same as resolvable.** ``DevTaskContent`` validates the
SHAPE of a reference — ``LO-CAT-004`` and ``LO-CAT-004/AC-1`` both pass whether
or not the requirement exists. Its own comment names the risk: "an unresolvable
reference is worse than none, because it reads as traceability while pointing
nowhere." So three populations are counted separately, and a reference that
points nowhere is never folded in with a task that honestly carries none.

**Registries are discovered, not named.** Until 2026-08-08 there was one, and its
scope line put "Platform substrate, the factory/dispatcher, and homelab
infrastructure" explicitly OUT of scope — so every factory task was platform work
with no requirement it could legally cite. ``REG-PLATFORM`` closed that. Reading
the directory rather than a filename means the next registry is counted with no
code change and no second place to update.

This reports. It does not gate: the exit code reflects whether the tool could
run, never whether the population is clean. Turning warning into refusal is a
separate decision that only becomes available once the number is known.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

REPO = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS_DIR = pathlib.Path("docs/requirements")

# The same shape DevTaskContent enforces. Kept here rather than imported so this
# tool can audit exported records without importing the substrate.
REFERENCE_RE = re.compile(r"^([A-Z]{2,}-[A-Z]{2,}-\d{3})(?:/(AC-\d+))?$")

TRACEABILITY_FIELDS = ("requirement_refs", "nfrs", "arch_impact")


@dataclass(frozen=True)
class Registry:
    """Requirement ids and criterion ids, from every registry on disk.

    ``directory_missing`` carries the one fact ``sources``/``requirements`` alone
    cannot: whether the directory this registry was loaded from existed at all.
    A directory that exists but holds no ``*.json`` registries and a directory
    that does not exist both leave every other field empty — ``sources == ()``,
    zero requirements, ``resolves()`` false for everything — so without this
    field the two are indistinguishable to a caller, and a caller re-``.exists()``
    ing the path itself would let its answer drift from what the loader actually
    observed. A caller that cannot tell "unreachable" from "genuinely empty"
    collapses an Unknown into a fixed value — exactly what PRIN-015 forbids.
    Set only by :func:`load_registries`.
    """

    requirements: frozenset[str] = frozenset()
    criteria: frozenset[str] = frozenset()
    sources: tuple[str, ...] = ()
    directory_missing: bool = False

    def resolves(self, reference: str) -> bool:
        match = REFERENCE_RE.match(reference.strip())
        if not match:
            return False
        requirement, criterion = match.group(1), match.group(2)
        if criterion:
            return f"{requirement}/{criterion}" in self.criteria
        return requirement in self.requirements


def load_registries(directory: pathlib.Path) -> Registry:
    """Every ``*.json`` in the requirements directory is a registry.

    Enumerated rather than named. A file that does not parse, or that carries no
    ``requirements`` list, is skipped rather than fatal — a malformed sibling
    must not take down the count of everything else.
    """
    requirements: set[str] = set()
    criteria: set[str] = set()
    sources: list[str] = []

    if not directory.exists():
        return Registry(directory_missing=True)

    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        entries = data.get("requirements")
        if not isinstance(entries, list):
            continue
        sources.append(path.name)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            requirement_id = entry.get("id")
            if not requirement_id:
                continue
            requirements.add(str(requirement_id))
            for criterion in entry.get("acceptance_criteria") or []:
                if isinstance(criterion, dict) and criterion.get("id"):
                    criteria.add(f"{requirement_id}/{criterion['id']}")

    return Registry(frozenset(requirements), frozenset(criteria), tuple(sources))


@dataclass
class FieldCoverage:
    """One traceability field, counted across the population."""

    name: str
    present: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.present) + len(self.absent)


@dataclass
class TraceabilityReport:
    tasks_audited: int = 0
    registry_sources: tuple[str, ...] = ()
    #: Set from ``Registry.directory_missing``. When true, every reference
    #: below reads as dangling only because the registry could not be read —
    #: not because any citation is wrong — and the report must say so rather
    #: than let a real citation be blamed for an unreachable directory.
    registry_directory_missing: bool = False
    # Three populations, deliberately distinct.
    no_references: list[str] = field(default_factory=list)
    # An escape hatch that cannot be counted becomes the default path within a
    # fortnight. Waived work is its own population — visible, and therefore a
    # backlog item rather than a hole.
    waived: list[tuple[str, str]] = field(default_factory=list)
    references_resolve: list[str] = field(default_factory=list)
    references_dangling: list[tuple[str, list[str]]] = field(default_factory=list)
    fields: dict[str, FieldCoverage] = field(default_factory=dict)

    @property
    def dangling_reference_count(self) -> int:
        return sum(len(refs) for _, refs in self.references_dangling)


def _task_id(task: dict[str, Any]) -> str:
    """Prefer the bead id; fall back to a title so a record is never anonymous."""
    for key in ("id", "task_id"):
        if task.get(key):
            return str(task[key])
    content = task.get("content") or task
    return str(content.get("title") or "(unidentified task)")


def _content(task: dict[str, Any]) -> dict[str, Any]:
    """Accept a bead or a bare content dict — both are handed around."""
    inner = task.get("content")
    return inner if isinstance(inner, dict) else task


def classify_population(
    tasks: Iterable[dict[str, Any]],
    *,
    has_reference: Callable[[dict[str, Any]], bool],
    waiver: Callable[[dict[str, Any]], str],
    identifier: Callable[[dict[str, Any]], str] = _task_id,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """The three-population split, generalized past ``requirement_refs``.

    "Carries a reference, carries a written reason it doesn't, or carries
    neither" is the same question for the release ``delivers`` edge that it is
    for a requirement reference — only what counts as "the reference" differs.
    One function answers it so the two counts cannot independently drift, which
    is the failure mode a second hand-rolled counter would reintroduce.
    """
    with_reference: list[str] = []
    waived: list[tuple[str, str]] = []
    neither: list[str] = []
    for task in tasks:
        ident = identifier(task)
        if has_reference(task):
            with_reference.append(ident)
            continue
        reason = waiver(task).strip()
        if reason:
            waived.append((ident, reason))
        else:
            neither.append(ident)
    return with_reference, waived, neither


def audit_tasks(tasks: Iterable[dict[str, Any]], registry: Registry) -> TraceabilityReport:
    """Pure: no I/O, no network, no substrate. Fetching is the caller's problem."""
    tasks = list(tasks)
    report = TraceabilityReport(
        registry_sources=registry.sources,
        registry_directory_missing=registry.directory_missing,
    )
    for name in TRACEABILITY_FIELDS:
        report.fields[name] = FieldCoverage(name)

    for task in tasks:
        report.tasks_audited += 1
        identifier = _task_id(task)
        content = _content(task)

        for name in TRACEABILITY_FIELDS:
            value = content.get(name)
            coverage = report.fields[name]
            (coverage.present if value else coverage.absent).append(identifier)

    _with_refs, report.waived, report.no_references = classify_population(
        tasks,
        has_reference=lambda t: bool(_content(t).get("requirement_refs")),
        waiver=lambda t: str(_content(t).get("requirement_refs_waived") or ""),
    )

    # Re-filtered directly, not looked up by identifier: two tasks can share an
    # identifier (both bare content with the same fallback title), and a dict
    # keyed by identifier would let one silently overwrite the other's refs.
    for task in tasks:
        content = _content(task)
        references = [str(r) for r in (content.get("requirement_refs") or [])]
        if not references:
            continue
        identifier = _task_id(task)
        dangling = [r for r in references if not registry.resolves(r)]
        if dangling:
            report.references_dangling.append((identifier, dangling))
        else:
            report.references_resolve.append(identifier)

    return report


def format_report(report: TraceabilityReport) -> str:
    lines: list[str] = []
    sources = ", ".join(report.registry_sources) or "(none found)"
    lines.append(f"Registries read: {sources}")
    lines.append(f"Tasks audited:   {report.tasks_audited}")
    if report.registry_directory_missing:
        lines.append(
            "WARNING: the requirements registry directory was not found. "
            "Every reference below reads as dangling because the registry "
            "was unreachable, not because any citation is wrong — this "
            "report is not meaningful until the directory exists again."
        )
    lines.append("")

    lines.append("Requirement references")
    lines.append(f"  resolve cleanly : {len(report.references_resolve)}")
    lines.append(f"  none carried    : {len(report.no_references)}")
    lines.append(f"  waived, with a reason : {len(report.waived)}")
    lines.append(
        f"  point nowhere   : {len(report.references_dangling)} "
        f"({report.dangling_reference_count} reference(s))"
    )

    if report.no_references:
        lines.append("")
        lines.append("  Carrying no requirement reference:")
        lines.extend(f"    - {identifier}" for identifier in report.no_references)

    if report.waived:
        lines.append("")
        lines.append("  Waived, with a stated reason:")
        for identifier, reason in report.waived:
            lines.append(f"    - {identifier}: {reason}")

    if report.references_dangling:
        lines.append("")
        lines.append("  Referencing requirements that do not exist:")
        for identifier, refs in report.references_dangling:
            lines.append(f"    - {identifier}: {', '.join(refs)}")

    lines.append("")
    lines.append("Field coverage")
    for name in TRACEABILITY_FIELDS:
        coverage = report.fields[name]
        lines.append(f"  {name:<17}: {len(coverage.present)}/{coverage.total} carried")

    return "\n".join(lines)


def _load_tasks(path: pathlib.Path | None) -> list[dict[str, Any]]:
    """Records from a JSON file, or stdin when no path is given.

    Fetching is deliberately not this tool's job: keeping it out means the whole
    of the logic above is testable with no substrate, no network and no
    credentials.
    """
    raw = path.read_text() if path else sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("tasks", "items", "beads"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return list(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks", nargs="?", help="JSON file of dev.task records; stdin if omitted")
    parser.add_argument("--repo", default=str(REPO), help="repository root")
    args = parser.parse_args(argv)

    try:
        registry = load_registries(pathlib.Path(args.repo) / REQUIREMENTS_DIR)
        tasks = _load_tasks(pathlib.Path(args.tasks) if args.tasks else None)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"traceability: could not run: {exc}", file=sys.stderr)
        return 2

    print(format_report(audit_tasks(tasks, registry)))
    # Deliberately 0 even when the population is non-conforming. This reports a
    # number; it does not yet gate on one. Making it gate is a separate decision
    # that the number itself is the precondition for.
    return 0


if __name__ == "__main__":
    sys.exit(main())
