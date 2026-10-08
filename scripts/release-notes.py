#!/usr/bin/env python3
"""Generate docs/releases/<ref>-notes.md from what merged and what was measured.

Five tags, no changelog, no version: "what did we tangibly get" from a release
has been answered by reading merge subjects, which is why it went unanswered.
This renders the answer from bead state instead of prose anyone typed:

* the charter's objective, verbatim (docs/releases/<ref>.json, validated the
  same way release-load.py validates it before mirroring);
* one section per outcome — its statement, the *merged* PRs delivering it
  (resolved from the release's own `delivers` edges, the way
  release-manifest.py resolves a release's PR set), and the criteria it cited;
* measured movement per cited criterion: the arch.requirement_conformance
  verdict at the release's opened_at against the latest one, dated and
  revisioned;
* declared-versus-actual work-class balance;
* work delivering the release with no outcome, and what was never delivered;
* the Release-gate verdict each PR carries.

**No second implementation of REL-2's arithmetic.** The balance and the
opened_at-vs-latest movement are computed once, by release-status.py's
`build_release_report` — this module calls it and renders its output. See
scripts/release-status.py's own docstring for why a second implementation of
the same rule is the specific failure mode this avoids.

**Never from prose.** The only positional argument is the release ref. There
is no flag for a body, a summary, or free text — the CLI cannot accept it.

**Fails rather than under-reports.** Every provider call that reaches the
substrate or `gh` can raise; `main()` catches once, at the top, and neither
writes nor partially writes docs/releases/<ref>-notes.md on failure. An
outcome with no merged PR renders as NOT DELIVERED and is never dropped; a
criterion whose newest observation predates opened_at renders as not
re-measured, never as a current verdict.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional, Protocol

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_module(name: str, filename: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = pathlib.Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_substrate = _load_module("substrate_client", "substrate_client.py")
_markers = _load_module("gate_markers", "gate_markers.py")
_release_load = _load_module("release_load", "release-load.py")
_release_status = _load_module("release_status", "release-status.py")

SECRET_ENV_VARS = (_substrate.URL_ENV, _substrate.KEY_ENV)
find_release_gate_verdict = _markers.find_release_gate_verdict
build_release_report = _release_status.build_release_report

GH_PR_FIELDS = "number,title,body,url,state,mergedAt,comments"

# A PR reference the way DevTaskContent.pr_url carries it — the same shape
# release-manifest.py's _pr_number_from_bead reads, kept here as its own copy
# rather than an import: a leading-underscore helper in another module is
# that module's own business, and this regex is one line (traceability.py's
# REFERENCE_RE sets the same precedent for a small, stable shared shape).
PR_URL_RE = re.compile(r"/pull/(\d+)(?:[/?#]|$)")


@dataclass(frozen=True)
class PrRecord:
    number: int
    title: str
    body: str
    url: str = ""
    state: str = ""
    merged_at: str = ""
    comments: tuple[str, ...] = ()

    @property
    def merged(self) -> bool:
        return bool(self.merged_at) or self.state.upper() == "MERGED"


def _pr_from_gh(data: dict[str, Any]) -> PrRecord:
    raw_comments = data.get("comments") or []
    comments = tuple(
        str(c.get("body") or "") if isinstance(c, dict) else str(c or "")
        for c in raw_comments
    )
    return PrRecord(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        url=str(data.get("url") or ""),
        state=str(data.get("state") or ""),
        merged_at=str(data.get("mergedAt") or ""),
        comments=comments,
    )


@dataclass(frozen=True)
class LinkRecord:
    link_type: str
    source_id: str
    target_id: str

    def other_id(self, bead_id: str) -> Optional[str]:
        if self.source_id == bead_id:
            return self.target_id
        if self.target_id == bead_id:
            return self.source_id
        return None


def _link_from_dict(row: dict[str, Any]) -> LinkRecord:
    return LinkRecord(
        link_type=str(row.get("link_type") or row.get("type") or "linked"),
        source_id=str(row.get("source_id") or ""),
        target_id=str(row.get("target_id") or ""),
    )


class Provider(Protocol):
    def charter(self, ref: str) -> dict[str, Any]:
        ...

    def release(self, ref: str) -> Optional[dict[str, Any]]:
        ...

    def links(self, bead_id: str) -> list[LinkRecord]:
        ...

    def bead(self, bead_id: str) -> dict[str, Any]:
        ...

    def conformances(self) -> list[dict[str, Any]]:
        ...

    def pr(self, number: int) -> PrRecord:
        ...

    def population(self, ref: str) -> Optional[tuple[list[dict[str, Any]], dict[str, str]]]:
        """The whole dev.task population and a delivers map spanning every
        charter -- what release-status.py's own main() feeds
        build_release_report (see gather_live_data), reused here rather than
        gather_notes handing it only the tasks that deliver this one release
        (which makes unmeasured_candidates dead code: see build_release_report
        -- a zero-task outcome can only ever look at the closed-unbound
        population, and that population is empty by construction when
        `tasks` is scoped to one release's own delivering tasks).

        None means "no whole-population data available for this ref" -- a
        real, meaningful answer distinct from "fetched and empty" -- and
        signals the caller to fall back to the delivering-tasks-only shape
        this module used before this method existed.
        """
        ...


def _load_charter_file(path: pathlib.Path, requirements_dir: pathlib.Path) -> dict[str, Any]:
    """Read one charter through release-load.py's own validation.

    A charter that fails validation there (dangling requirement ref,
    measurement keys, malformed JSON) must fail here the same way — the notes
    would otherwise render a citation the substrate mirror would have
    refused.
    """
    if not path.exists():
        raise RuntimeError(f"no charter at {path}")
    try:
        items = _release_load.load_charters([path], requirements_dir=requirements_dir)
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from None
    return items[0].content


class FixtureProvider:
    def __init__(self, root: pathlib.Path, repo: pathlib.Path = REPO) -> None:
        self.root = root
        self.repo = repo

    def charter(self, ref: str) -> dict[str, Any]:
        return _load_charter_file(
            self.root / "charters" / f"{ref}.json", self.repo / "docs" / "requirements"
        )

    def release(self, ref: str) -> Optional[dict[str, Any]]:
        path = self.root / "releases" / f"{ref}.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def links(self, bead_id: str) -> list[LinkRecord]:
        path = self.root / "links" / f"{bead_id}.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("links") or [])
        return [_link_from_dict(row) for row in rows]

    def bead(self, bead_id: str) -> dict[str, Any]:
        return self._read_json(self.root / "beads" / f"{bead_id}.json")

    def conformances(self) -> list[dict[str, Any]]:
        path = self.root / "conformances.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        return data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])

    def pr(self, number: int) -> PrRecord:
        return _pr_from_gh(self._read_json(self.root / "prs" / f"{number}.json"))

    def population(self, ref: str) -> Optional[tuple[list[dict[str, Any]], dict[str, str]]]:
        """Read `population/<ref>.json` ({"tasks": [...], "delivers": {...}})
        when the fixture root carries one for this ref, else None.

        Keyed per release ref rather than one shared file at the fixtures
        root -- a single shared population would be read by every test
        against this fixture root (R90.02 included) and could silently flip
        its zero-task outcome from NOT DELIVERED to UNMEASURED.
        """
        path = self.root / "population" / f"{ref}.json"
        if not path.exists():
            return None
        data = self._read_json(path)
        return list(data.get("tasks") or []), dict(data.get("delivers") or {})

    def _read_json(self, path: pathlib.Path) -> Any:
        return json.loads(path.read_text())


class LiveProvider:
    def __init__(
        self,
        repo: pathlib.Path = REPO,
        *,
        reader_factory: Any = None,
    ) -> None:
        self.repo = repo
        self._client = _substrate.reader(
            base_url=os.environ.get(_substrate.URL_ENV),
            key=os.environ.get(_substrate.KEY_ENV),
        )
        # The seam release-status.py's own main() exposes (its
        # `reader_factory` parameter, defaulting to GateSubstrateReader) --
        # reused rather than monkeypatching a substrate client singleton,
        # which would not work here: release-notes.py's `_substrate` and
        # release-status.py's loaded `substrate_client` are two distinct
        # module objects (see _load_substrate_client_module's docstring),
        # so a patch on one does not intercept the reader the other builds.
        # Not constructed eagerly: building the real GateSubstrateReader is
        # itself just an object construction (no network), but keeping it
        # lazy, built only inside population(), means a caller that never
        # calls population() -- e.g. every existing LiveProvider caller
        # before this method existed -- triggers no new code path at all.
        self._reader_factory = reader_factory or _release_status.GateSubstrateReader

    def charter(self, ref: str) -> dict[str, Any]:
        return _load_charter_file(
            self.repo / "docs" / "releases" / f"{ref}.json", self.repo / "docs" / "requirements"
        )

    def release(self, ref: str) -> Optional[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "namespace": "arch",
            "type": "release",
            "content_ref": ref,
            "limit": 1,
        })
        data = self._request(f"/beads?{query}")
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])
        return rows[0] if rows else None

    def links(self, bead_id: str) -> list[LinkRecord]:
        data = self._request(f"/beads/{urllib.parse.quote(bead_id)}/links?direction=both")
        rows = data if isinstance(data, list) else list(data.get("items") or data.get("links") or [])
        return [_link_from_dict(row) for row in rows]

    def bead(self, bead_id: str) -> dict[str, Any]:
        return self._request(f"/beads/{urllib.parse.quote(bead_id)}")

    def conformances(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({
            "namespace": "arch",
            "type": "requirement_conformance",
            "limit": 5000,
        })
        data = self._request(f"/beads?{query}")
        return data if isinstance(data, list) else list(data.get("items") or data.get("beads") or [])

    def pr(self, number: int) -> PrRecord:
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", GH_PR_FIELDS],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"gh failed for PR #{number}"
            raise RuntimeError(message)
        return _pr_from_gh(json.loads(result.stdout))

    def _request(self, path: str) -> Any:
        return self._client.get(path)

    def population(self, ref: str) -> Optional[tuple[list[dict[str, Any]], dict[str, str]]]:
        """The whole dev.task population and a delivers map over every
        charter on disk -- release-status.py's own gather_live_data, fed
        every charter the same way its main() does, through a reader built
        by self._reader_factory (injectable; defaults to GateSubstrateReader).
        """
        try:
            charter_items = _release_load.load_charters(
                _release_load.charter_paths(self.repo / "docs" / "releases"),
                requirements_dir=self.repo / "docs" / "requirements",
            )
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from None
        reader = self._reader_factory(
            os.environ.get(_substrate.URL_ENV), os.environ.get(_substrate.KEY_ENV)
        )
        tasks, delivers, _conformances = _release_status.gather_live_data(
            reader, [item.content for item in charter_items]
        )
        return tasks, delivers


# ---------------------------------------------------------------------------
# Pure assembly — everything below takes already-fetched data
# ---------------------------------------------------------------------------


@dataclass
class OutcomeNotes:
    id: str
    statement: str
    work_class: str
    requirement_refs: tuple[str, ...]
    merged_prs: tuple[PrRecord, ...]
    # Populated only when this outcome has zero delivering tasks and the
    # release-wide closed-unbound scan (build_release_report) found a
    # candidate in this release's window -- see OutcomeStatus. Non-empty
    # here means UNMEASURED, never NOT DELIVERED.
    unmeasured_candidates: tuple[str, ...] = ()

    @property
    def delivered(self) -> bool:
        return bool(self.merged_prs)


@dataclass
class ReleaseNotes:
    ref: str
    name: str
    objective: str
    outcomes: list[OutcomeNotes]
    balance: list[Any]  # release_status.BalanceEntry
    criteria: list[Any]  # release_status.CriterionStatus
    unclassified: list[str]
    prs: list[PrRecord]


def gather_notes(ref: str, provider: Provider) -> ReleaseNotes:
    """Fetch everything the notes need and hand back a plain data object.

    Every provider call here is allowed to raise (RuntimeError from a
    substrate 4xx/5xx or missing env, OSError/subprocess.SubprocessError from
    a failing `gh`) and is deliberately not caught: a run that cannot fetch
    one section must not write notes with the rest, silently missing it.
    """
    charter = provider.charter(ref)

    release_bead = provider.release(ref)
    if release_bead is None:
        raise RuntimeError(f"release {ref} not found in the substrate")
    release_id = str(release_bead.get("id") or "")

    task_ids = sorted({
        other
        for link in provider.links(release_id)
        if link.link_type == "delivers"
        for other in [link.other_id(release_id)]
        if other
    })
    delivering_tasks = [provider.bead(tid) for tid in task_ids]

    conformances = provider.conformances()

    # The whole dev.task population and a delivers map over every charter --
    # not just the tasks that deliver this one release -- so a zero-task
    # outcome's UNMEASURED candidates (build_release_report's own
    # closed-unbound scan) are reachable here the same way release-status.py
    # main() reaches them. Falls back to the pre-existing delivering-tasks-
    # only shape when the provider has no whole-population data for this ref
    # (every FixtureProvider release with no population/<ref>.json fixture).
    population = provider.population(ref)
    if population is not None:
        all_tasks, delivers = population
    else:
        all_tasks, delivers = delivering_tasks, {tid: ref for tid in task_ids}

    report = build_release_report(charter, all_tasks, delivers, conformances)

    pr_by_task: dict[str, PrRecord] = {}
    for tid, task in zip(task_ids, delivering_tasks):
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        match = PR_URL_RE.search(str(content.get("pr_url") or ""))
        if not match:
            continue
        pr_by_task[tid] = provider.pr(int(match.group(1)))

    outcomes_by_id = {o["id"]: o for o in charter.get("outcomes", [])}
    outcomes: list[OutcomeNotes] = []
    for status in report.outcomes:
        outcome_def = outcomes_by_id.get(status.id, {})
        delivering_task_ids = [tid for ids in status.tasks_by_state.values() for tid in ids]
        merged_prs = tuple(
            pr_by_task[tid] for tid in delivering_task_ids if pr_by_task.get(tid) and pr_by_task[tid].merged
        )
        outcomes.append(
            OutcomeNotes(
                id=status.id,
                statement=status.statement,
                work_class=status.work_class,
                requirement_refs=tuple(outcome_def.get("requirement_refs") or []),
                merged_prs=merged_prs,
                unmeasured_candidates=tuple(status.unmeasured_candidates),
            )
        )

    all_prs = [pr_by_task[tid] for tid in task_ids if tid in pr_by_task]

    return ReleaseNotes(
        ref=charter["ref"],
        name=charter.get("name", ref),
        objective=str(charter.get("objective") or ""),
        outcomes=outcomes,
        balance=report.balance,
        criteria=report.criteria,
        unclassified=report.unclassified_delivering,
        prs=all_prs,
    )


def _format_observation(obs: Any) -> str:
    if obs is None:
        return "no observation recorded"
    revision = f", rev {obs.measured_revision}" if obs.measured_revision else ""
    return f"{obs.value} (measured {obs.measured_at}{revision})"


def _format_criterion(status: Any) -> str:
    if status.latest is None:
        return f"- {status.ref}: no arch.requirement_conformance recorded"
    as_of = _format_observation(status.as_of_opened)
    if status.stale:
        revision = f", rev {status.latest.measured_revision}" if status.latest.measured_revision else ""
        return (
            f"- {status.ref}: as at opened_at = {as_of}; "
            f"latest = not re-measured since {status.latest.measured_at}{revision}"
        )
    movement = "changed" if status.changed else "unchanged"
    return (
        f"- {status.ref}: as at opened_at = {as_of}; "
        f"latest = {_format_observation(status.latest)} [{movement}]"
    )


def render_notes(notes: ReleaseNotes) -> str:
    lines: list[str] = [f"# Release {notes.ref} — {notes.name}", ""]

    lines.extend(["## Objective", "", notes.objective, ""])

    lines.extend(["## Outcomes", ""])
    not_delivered: list[OutcomeNotes] = []
    unmeasured: list[OutcomeNotes] = []
    for outcome in notes.outcomes:
        lines.append(f"### {outcome.id} [{outcome.work_class}]")
        lines.append("")
        lines.append(outcome.statement)
        lines.append("")
        refs = ", ".join(outcome.requirement_refs) if outcome.requirement_refs else "(none)"
        lines.append(f"- Criteria cited: {refs}")
        if outcome.delivered:
            lines.append("- Merged PRs:")
            for pr in outcome.merged_prs:
                lines.append(f"  - #{pr.number} {pr.title} ({pr.url})")
        elif outcome.unmeasured_candidates:
            ids = ", ".join(outcome.unmeasured_candidates)
            lines.append(
                f"- Merged PRs: UNMEASURED, not NOT DELIVERED -- {len(outcome.unmeasured_candidates)} "
                "closed dev.task bead(s) with no delivers edge closed inside this release's window "
                f"({ids}); inspect the candidate(s) and bind it with --bind-release, or record why "
                "not, before deciding this outcome's fate."
            )
            unmeasured.append(outcome)
        else:
            lines.append("- Merged PRs: NOT DELIVERED — no merged PR delivers this outcome.")
            not_delivered.append(outcome)
        lines.append("")

    lines.extend(["## Measured Movement", ""])
    if notes.criteria:
        for status in notes.criteria:
            lines.append(_format_criterion(status))
    else:
        lines.append("(no criteria cited by this release)")
    lines.append("")

    lines.extend(["## Declared vs Actual Balance", ""])
    for entry in notes.balance:
        if entry.absent:
            actual = "ABSENT"
        elif entry.insufficient_sample:
            actual = f"insufficient sample (n={entry.sample_size})"
        else:
            actual = f"{entry.actual_pct}%"
        line = f"- {entry.work_class}: declared {entry.declared_pct}%, actual {actual} ({entry.actual_count} task(s))"
        # ABSENT and insufficient_sample are independent facts (a class can be
        # both at once) -- when both hold, the ABSENT substring above is kept
        # exactly as rendered elsewhere, with the sample-size caveat appended
        # rather than replacing it, so neither fact is lost.
        if entry.insufficient_sample and entry.absent:
            line += f" insufficient sample (n={entry.sample_size})"
        lines.append(line)
    lines.append("")

    lines.extend(["## Work With No Outcome", ""])
    if notes.unclassified:
        for task_id in notes.unclassified:
            lines.append(f"- {task_id}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.extend(["## What Was Not Delivered", ""])
    if not_delivered:
        for outcome in not_delivered:
            lines.append(f"- {outcome.id} NOT DELIVERED: {outcome.statement}")
    elif not unmeasured:
        lines.append("(all outcomes delivered)")
    lines.append("")

    lines.extend(["## Unmeasured Outcomes", ""])
    if unmeasured:
        for outcome in unmeasured:
            ids = ", ".join(outcome.unmeasured_candidates)
            lines.append(
                f"- {outcome.id} UNMEASURED, not NOT DELIVERED: {outcome.statement} -- "
                f"candidate(s) {ids}; inspect the candidate(s) and bind, or record why not, "
                "before deciding this outcome's fate."
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.extend(["## Release-Gate Verdicts", ""])
    if notes.prs:
        for pr in notes.prs:
            verdict = find_release_gate_verdict(pr.body, pr.comments)
            lines.append(f"- #{pr.number} {pr.title}: {verdict or '(no Release-gate verdict recorded)'}")
    else:
        lines.append("(no PRs delivered this release)")
    lines.append("")

    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref", help="release ref, e.g. R26.01")
    parser.add_argument("--fixture-dir", type=pathlib.Path, help="read release data from fixtures")
    parser.add_argument("--repo", type=pathlib.Path, default=REPO, help="repository root")
    parser.add_argument(
        "--out-dir", type=pathlib.Path, help="directory to write <ref>-notes.md into (default docs/releases)"
    )
    args = parser.parse_args(argv)

    provider: Provider = (
        FixtureProvider(args.fixture_dir, repo=args.repo) if args.fixture_dir else LiveProvider(repo=args.repo)
    )

    try:
        notes = gather_notes(args.ref, provider)
        rendered = render_notes(notes)
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as exc:
        message = str(exc)
        for value in (os.environ.get(name) for name in SECRET_ENV_VARS):
            if value:
                message = message.replace(value, "")
        print(f"release-notes: could not run: {message}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or (args.repo / "docs" / "releases")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.ref}-notes.md"
    out_path.write_text(rendered)
    print(f"release-notes: wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
