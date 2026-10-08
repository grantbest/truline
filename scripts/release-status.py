#!/usr/bin/env python3
"""Measure the backlog against the release it claims to serve.

R26.01 declares a balance — 15% feature, 35% enabling, 15% blocking, 25% risk,
10% security — and once REL-1 landed, tasks carry the edges that would let
that be checked: a `dev.task` reaches its release by a `delivers` edge
(never a string in its own content — see `schemas.py:ArchReleaseContent`),
and names which outcome it satisfies via the bare `content.outcome_ref`. This
is the thing that reads those edges and reports what they say, so "feature
work crowded out security" stops being an impression and becomes a number.

**A task's work class is derived, never read off the task.** `ReleaseOutcome
.work_class` lives on the outcome so a task's class can never disagree with
it — this report looks up `content.outcome_ref` against the release charter
and takes *that* outcome's `work_class`. A task with no `outcome_ref`, or one
naming an outcome the charter does not declare, is `unclassified` — its own
population, never defaulted into one of the five.

**The release-reference count reuses `traceability.classify_population`** —
the same "carries a reference / carries a written waiver / carries neither"
split S29-B1 built for `requirement_refs`, applied here to the `delivers`
edge. One function, two call sites: the two counts cannot independently
drift.

This reports. It does not gate — see `traceability.py`'s docstring for the
discipline this borrows: the exit code reflects whether the tool could run,
never whether the population is balanced.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import pathlib
import sys
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

REPO = pathlib.Path(__file__).resolve().parent.parent
RELEASES_DIR = pathlib.Path("docs") / "releases"
REQUIREMENTS_DIR = pathlib.Path("docs") / "requirements"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import traceability  # noqa: E402
from unknown import Unknown  # noqa: E402

_SUBSTRATE_CLIENT_ALIAS = "_scripts_substrate_client_impl"


def _load_substrate_client_module():
    """Load scripts/substrate_client.py by file path, under an alias
    distinct from the bare name ``substrate_client``.

    apps/substrate/client/src/substrate_client is also importable as the
    bare name ``substrate_client`` and does not export ``SubstrateClient``;
    whichever of the two wins ``sys.modules["substrate_client"]`` first
    decides what a plain ``import substrate_client`` returns for the rest of
    the process (dev.finding d9ae4d93). Loading by path sidesteps that race
    entirely -- the same pattern
    apps/substrate/client/tests/test_public_surface.py already uses for the
    package's own suite. The alias is shared across every scripts/ caller of
    this module so a process that loads more than one of them (this repo's
    own test suite routinely does) executes it, and its apps/substrate/src
    schema imports, once rather than once per caller.
    """
    cached = sys.modules.get(_SUBSTRATE_CLIENT_ALIAS)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        _SUBSTRATE_CLIENT_ALIAS, pathlib.Path(__file__).resolve().parent / "substrate_client.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SUBSTRATE_CLIENT_ALIAS] = module
    spec.loader.exec_module(module)
    return module


substrate_client = _load_substrate_client_module()

WORK_CLASSES = ("feature", "enabling", "blocking", "risk", "security")

#: Below this many classified delivering tasks, a percentage is noise, not a
#: measurement -- a release with four classified tasks must not print 25%
#: steps as though they meant anything. A module constant, not read from any
#: file, until B12 moves it into docs/releases/policy/health-policy.json.
MIN_CLASSIFIED_FOR_PERCENTAGE = 5

#: dev.task states, per apps/substrate/tests/test_transition.py's STATE_MACHINES
#: for ("dev", "task"): pending, doing, review, done, failed, superseded,
#: archived. S29-B1's own AC excludes "completed or done" from the open
#: population; archived and superseded are the same kind of terminal (dead
#: work), and counting either as a live gap would double-report it.
TERMINAL_TASK_STATES = frozenset({"done", "archived", "superseded"})

#: The subset of TERMINAL_TASK_STATES that represents delivered-or-superseded
#: work, not abandoned work. `archived` means "no longer relevant" -- folding
#: it into the population this module treats as "candidate for a missed
#: `delivers` edge" would flag work nobody ever intended to ship.
CLOSED_TASK_STATES = frozenset({"done", "superseded"})

_task_id = traceability._task_id
_content = traceability._content


def _load_release_load_module():
    """`release-load.py` cannot be `import`-ed by name (the hyphen), so it is
    loaded the same way `scripts/tests/test_release_load.py` already does.
    Reusing its `charter_paths`/`load_charters` means a charter that fails
    validation for `release-load.py` fails the same way here — one place
    decides what a loadable charter looks like.
    """
    spec = importlib.util.spec_from_file_location(
        "release_load", pathlib.Path(__file__).resolve().parent / "release-load.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Pure data model — no I/O below this line until main()
# ---------------------------------------------------------------------------


@dataclass
class BalanceEntry:
    work_class: str
    declared_pct: int
    actual_count: int
    # The release's total classified delivering task count -- the same value
    # for every entry in one report, carried per-entry so a caller holding
    # just one BalanceEntry can still tell whether its actual_pct is
    # meaningful.
    sample_size: int
    # None means ABSENT (declared at a non-zero share, nothing delivered) or
    # insufficient sample (fewer than MIN_CLASSIFIED_FOR_PERCENTAGE classified
    # delivering tasks) -- told apart by the two properties below.
    actual_pct: Optional[float]

    @property
    def absent(self) -> bool:
        return self.declared_pct > 0 and self.actual_count == 0

    @property
    def insufficient_sample(self) -> bool:
        return self.sample_size < MIN_CLASSIFIED_FOR_PERCENTAGE


@dataclass
class OutcomeStatus:
    id: str
    statement: str
    work_class: str
    tasks_by_state: dict[str, list[str]] = field(default_factory=dict)
    # Populated only when task_count == 0: closed (done/superseded) dev.task
    # beads with no `delivers` edge to any release, completed inside this
    # release's own window. Not attributed to this outcome specifically --
    # that attribution is exactly the fact nothing recorded -- so every
    # zero-task outcome in the release carries the same candidate set.
    unmeasured_candidates: list[str] = field(default_factory=list)

    @property
    def task_count(self) -> int:
        return sum(len(ids) for ids in self.tasks_by_state.values())


@dataclass(frozen=True)
class Observation:
    value: str
    measured_at: str
    # Optional: requirements-load.py only writes this when the source
    # measurement carried one. release-notes.py cites it; this module's own
    # formatting does not, so its absence changes nothing here.
    measured_revision: Optional[str] = None


@dataclass
class CriterionStatus:
    ref: str
    as_of_opened: Optional[Observation]
    latest: Optional[Observation]
    stale: bool  # latest predates opened_at: "not re-measured", not current
    changed: bool
    # Set only when `ref` itself could not be evaluated at all (fails
    # traceability.REFERENCE_RE) -- distinct from a well-formed ref with
    # genuinely zero recorded conformances, which is a real, verified
    # absence and leaves this None. See unknown.py: this is "could not be
    # computed," not "computed as empty."
    unmeasurable: Optional[Unknown] = None


@dataclass
class UnassignedPopulation:
    open_no_release_ref: list[str] = field(default_factory=list)
    waiver_reason_counts: Counter = field(default_factory=Counter)


@dataclass
class ClosedUnboundPopulation:
    """The sibling population to UnassignedPopulation, for closed rather than
    open work. Kept as its own type -- not a reuse of UnassignedPopulation --
    because `open_no_release_ref` is asserted by name in existing tests and
    its meaning ("open") would be a lie applied to closed beads."""

    closed_no_release_ref: list[str] = field(default_factory=list)
    waiver_reason_counts: Counter = field(default_factory=Counter)


@dataclass
class ReleaseStatusReport:
    ref: str
    name: str
    outcomes: list[OutcomeStatus] = field(default_factory=list)
    # Delivers this release but names no outcome, or names one the charter
    # does not declare.
    unclassified_delivering: list[str] = field(default_factory=list)
    balance: list[BalanceEntry] = field(default_factory=list)
    criteria: list[CriterionStatus] = field(default_factory=list)


def _outcome_value(conformance_content: dict[str, Any]) -> str:
    """`requirements-load.py` normalizes the registry's `conformance`/`verdict`
    keys onto one substrate field, `verdict` — this just reads it."""
    value = conformance_content.get("verdict")
    return str(value) if value else "(no value)"


def _matches_criterion(
    conformance_content: dict[str, Any], requirement_id: str, criterion_id: Optional[str]
) -> bool:
    if conformance_content.get("requirement_id") != requirement_id:
        return False
    return conformance_content.get("acceptance_criterion_id") == criterion_id


def _criterion_status(
    ref: str, conformances: list[dict[str, Any]], opened_at: str
) -> CriterionStatus:
    match = traceability.REFERENCE_RE.match(ref.strip())
    if not match:
        return CriterionStatus(
            ref=ref,
            as_of_opened=None,
            latest=None,
            stale=False,
            changed=False,
            unmeasurable=Unknown(
                f"{ref!r} is not a well-formed requirement/criterion reference, "
                "so no conformance lookup could be attempted for it"
            ),
        )
    requirement_id, criterion_id = match.group(1), match.group(2)

    candidates = sorted(
        (
            Observation(
                value=_outcome_value(_content(o)),
                measured_at=str(_content(o).get("measured_at") or ""),
                measured_revision=_content(o).get("measured_revision"),
            )
            for o in conformances
            if _matches_criterion(_content(o), requirement_id, criterion_id)
            and _content(o).get("measured_at")
        ),
        key=lambda obs: obs.measured_at,
    )

    if not candidates:
        return CriterionStatus(ref=ref, as_of_opened=None, latest=None, stale=False, changed=False)

    as_of_opened_candidates = [o for o in candidates if o.measured_at <= opened_at]
    as_of_opened = as_of_opened_candidates[-1] if as_of_opened_candidates else None
    latest = candidates[-1]
    stale = latest.measured_at < opened_at
    changed = (
        not stale
        and as_of_opened is not None
        and latest.value != as_of_opened.value
    )
    return CriterionStatus(
        ref=ref, as_of_opened=as_of_opened, latest=latest, stale=stale, changed=changed
    )


def _closed_unbound_in_window(
    tasks: list[dict[str, Any]], delivers: dict[str, str], opened_at: str, window_end: str
) -> list[str]:
    """Closed (done/superseded) dev.task beads with no `delivers` edge to any
    release and no written waiver for one, whose `updated_at` falls inside
    `[opened_at, window_end]`.

    `updated_at` is a last-write proxy for completion, not a dedicated
    `closed_at` field -- no such field exists on a dev.task bead (see
    design.md). A bead with no `updated_at` at all cannot be placed in the
    window, so it is excluded rather than guessed into it.

    The waiver exclusion routes through `traceability.classify_population`'s
    `neither` bucket -- the same split `compute_closed_unbound` already
    applies to the whole closed-unbound population -- rather than a second
    hand-rolled check, so a waived bead (dev.finding e1703658) cannot count
    as a candidate here while `compute_closed_unbound` correctly excludes it.
    """
    in_window: list[dict[str, Any]] = []
    for task in tasks:
        if (task.get("state") or "") not in CLOSED_TASK_STATES:
            continue
        completed = str(task.get("updated_at") or "")[:10]
        if not completed:
            continue
        if opened_at <= completed <= window_end:
            in_window.append(task)
    _with_ref, _waived, neither = traceability.classify_population(
        in_window,
        has_reference=lambda t: _task_id(t) in delivers,
        waiver=lambda t: str(_content(t).get("release_ref_waived") or ""),
    )
    return neither


def build_release_report(
    charter: dict[str, Any],
    tasks: list[dict[str, Any]],
    delivers: dict[str, str],
    conformances: list[dict[str, Any]],
    *,
    now: Optional[str] = None,
) -> ReleaseStatusReport:
    """Pure: `tasks` is the whole dev.task population (any release, any
    state); `delivers` maps task id -> release ref, one entry per task since
    REL-1 writes at most one `delivers` edge; `conformances` is the whole
    arch.requirement_conformance population. Fetching all three is the
    caller's problem. `now` is the report-run date (ISO), used only as the
    closed-unbound window's upper bound when the charter declares no
    `target_at` -- defaulted rather than read from the clock in here so this
    function stays deterministic for a caller that supplies it.
    """
    ref = charter["ref"]
    outcomes_by_id = {o["id"]: o for o in charter.get("outcomes", [])}

    delivering_ids = {tid for tid, target in delivers.items() if target == ref}
    delivering_tasks = [t for t in tasks if _task_id(t) in delivering_ids]

    opened_at = str(charter["opened_at"])
    window_end = str(charter.get("target_at") or now or datetime.date.today().isoformat())
    closed_unbound_candidates = _closed_unbound_in_window(tasks, delivers, opened_at, window_end)

    outcomes: list[OutcomeStatus] = []
    for outcome in charter.get("outcomes", []):
        status = OutcomeStatus(
            id=outcome["id"], statement=outcome["statement"], work_class=outcome["work_class"]
        )
        for task in delivering_tasks:
            if _content(task).get("outcome_ref") != outcome["id"]:
                continue
            state = task.get("state") or "(unknown)"
            status.tasks_by_state.setdefault(state, []).append(_task_id(task))
        if status.task_count == 0:
            status.unmeasured_candidates = list(closed_unbound_candidates)
        outcomes.append(status)

    unclassified = [
        _task_id(t)
        for t in delivering_tasks
        if _content(t).get("outcome_ref") not in outcomes_by_id
    ]

    class_counts: dict[str, int] = {cls: 0 for cls in WORK_CLASSES}
    for task in delivering_tasks:
        outcome = outcomes_by_id.get(_content(task).get("outcome_ref"))
        if outcome is not None:
            class_counts[outcome["work_class"]] += 1
    total_classified = sum(class_counts.values())

    declared_balance = charter.get("declared_balance") or {}
    insufficient = total_classified < MIN_CLASSIFIED_FOR_PERCENTAGE
    balance: list[BalanceEntry] = []
    for cls in WORK_CLASSES:
        declared_pct = int(declared_balance.get(cls, 0))
        actual_count = class_counts[cls]
        if insufficient:
            actual_pct = None
        elif declared_pct > 0 and actual_count == 0:
            actual_pct = None
        else:
            actual_pct = round(actual_count / total_classified * 100, 1)
        balance.append(
            BalanceEntry(
                work_class=cls,
                declared_pct=declared_pct,
                actual_count=actual_count,
                sample_size=total_classified,
                actual_pct=actual_pct,
            )
        )

    cited_refs: list[str] = []
    for outcome in charter.get("outcomes", []):
        for r in outcome.get("requirement_refs", []) or []:
            if r not in cited_refs:
                cited_refs.append(r)
    criteria = [_criterion_status(r, conformances, str(charter["opened_at"])) for r in cited_refs]

    return ReleaseStatusReport(
        ref=ref,
        name=charter.get("name", ref),
        outcomes=outcomes,
        unclassified_delivering=unclassified,
        balance=balance,
        criteria=criteria,
    )


def compute_unassigned(open_tasks: list[dict[str, Any]], delivers: dict[str, str]) -> UnassignedPopulation:
    """The backlog-hygiene population: open dev.task beads with no `delivers`
    edge, and the distinct reasons carried by those that waived one instead.

    Reuses `traceability.classify_population` — the same function `audit_tasks`
    uses for `requirement_refs` — rather than a second hand-rolled split.
    """
    _with_ref, waived, neither = traceability.classify_population(
        open_tasks,
        has_reference=lambda t: _task_id(t) in delivers,
        waiver=lambda t: str(_content(t).get("release_ref_waived") or ""),
    )
    return UnassignedPopulation(
        open_no_release_ref=neither,
        waiver_reason_counts=Counter(reason for _, reason in waived),
    )


def compute_closed_unbound(
    closed_tasks: list[dict[str, Any]], delivers: dict[str, str]
) -> ClosedUnboundPopulation:
    """The population release-close.md's failure-modes section names: done or
    superseded dev.task beads with no `delivers` edge -- exactly the shape
    work finished before REL-1 wrote edges would have. Reuses
    `classify_population`, the same split `compute_unassigned` already uses
    for the open population, rather than a second hand-rolled one.
    """
    _with_ref, waived, neither = traceability.classify_population(
        closed_tasks,
        has_reference=lambda t: _task_id(t) in delivers,
        waiver=lambda t: str(_content(t).get("release_ref_waived") or ""),
    )
    return ClosedUnboundPopulation(
        closed_no_release_ref=neither,
        waiver_reason_counts=Counter(reason for _, reason in waived),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_observation(obs: Optional[Observation]) -> str:
    if obs is None:
        return "no observation recorded"
    return f"{obs.value} (measured {obs.measured_at})"


def format_criterion(status: CriterionStatus) -> str:
    if status.unmeasurable is not None:
        return f"    {status.ref}: UNKNOWN — {status.unmeasurable.reason}"
    if status.latest is None:
        return f"    {status.ref}: no arch.requirement_conformance recorded"
    as_of = _format_observation(status.as_of_opened)
    if status.stale:
        return (
            f"    {status.ref}: as at opened_at = {as_of}; "
            f"latest = not re-measured since {status.latest.measured_at}"
        )
    movement = "changed" if status.changed else "unchanged"
    return (
        f"    {status.ref}: as at opened_at = {as_of}; "
        f"latest = {_format_observation(status.latest)} [{movement}]"
    )


def format_outcome(outcome: OutcomeStatus) -> list[str]:
    lines = [f"  {outcome.id} [{outcome.work_class}] ({outcome.task_count} task(s)): {outcome.statement}"]
    for state in sorted(outcome.tasks_by_state):
        ids = ", ".join(outcome.tasks_by_state[state])
        lines.append(f"    {state}: {ids}")
    if outcome.task_count == 0:
        if outcome.unmeasured_candidates:
            ids = ", ".join(outcome.unmeasured_candidates)
            lines.append(
                f"    UNMEASURED, not NOT DELIVERED: {len(outcome.unmeasured_candidates)} closed "
                "dev.task bead(s) with no delivers edge closed inside this release's window "
                f"({ids}) -- inspect and bind, or record why not, before deciding this outcome's fate"
            )
        else:
            lines.append(
                "    NOT DELIVERED: no work bound, and no closed unbound candidate "
                "found in this release's window"
            )
    return lines


def format_release_report(report: ReleaseStatusReport) -> str:
    lines = [f"Release {report.ref} — {report.name}", ""]

    lines.append("Outcomes")
    for outcome in report.outcomes:
        lines.extend(format_outcome(outcome))
    if report.unclassified_delivering:
        lines.append(
            "  unclassified (delivers this release, no valid outcome_ref): "
            + ", ".join(report.unclassified_delivering)
        )
    lines.append("")

    lines.append("Declared vs actual balance")
    for entry in report.balance:
        if entry.insufficient_sample:
            parts = [f"insufficient sample (n={entry.sample_size})"]
            if entry.absent:
                parts.append("ABSENT")
            actual = ", ".join(parts)
        else:
            actual = "ABSENT" if entry.absent else f"{entry.actual_pct}%"
        lines.append(
            f"  {entry.work_class:<10}: declared {entry.declared_pct}%  actual {actual}  ({entry.actual_count} task(s))"
        )
    lines.append("")

    if report.criteria:
        lines.append("Cited criteria — verdict as at opened_at vs latest")
        for status in report.criteria:
            lines.append(format_criterion(status))

    return "\n".join(lines)


def format_unassigned(unassigned: UnassignedPopulation) -> str:
    lines = ["Unassigned population (open dev.task, no delivers edge)", ""]
    lines.append(f"  carrying neither a delivers edge nor a waiver: {len(unassigned.open_no_release_ref)}")
    if unassigned.open_no_release_ref:
        lines.extend(f"    - {tid}" for tid in unassigned.open_no_release_ref)
    lines.append("")
    lines.append("  waiver reasons, counted:")
    if unassigned.waiver_reason_counts:
        for reason, count in sorted(unassigned.waiver_reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {count:>3}  {reason}")
    else:
        lines.append("    (none)")
    return "\n".join(lines)


def format_closed_unbound(closed: ClosedUnboundPopulation) -> str:
    lines = [
        "Closed-but-unbound population (done/superseded dev.task, no delivers edge)",
        "",
        "  This is release-close.md's failure mode: work finished before REL-1 wrote",
        "  `delivers` edges, or finished since without one. Each entry is a candidate for a",
        "  missed edge, not a backlog item -- inspect and bind it, or record why not.",
        "",
    ]
    lines.append(f"  carrying neither a delivers edge nor a waiver: {len(closed.closed_no_release_ref)}")
    if closed.closed_no_release_ref:
        lines.extend(f"    - {tid}" for tid in closed.closed_no_release_ref)
    lines.append("")
    lines.append("  waiver reasons, counted:")
    if closed.waiver_reason_counts:
        for reason, count in sorted(closed.waiver_reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {count:>3}  {reason}")
    else:
        lines.append("    (none)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live substrate access — the only I/O in this module
# ---------------------------------------------------------------------------


class SubstrateReader(Protocol):
    def list_beads(self, namespace: str, type_: str, **params: Any) -> list[dict]: ...

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: Optional[str] = None
    ) -> list[dict]: ...


class GateSubstrateReader:
    """Reads only — never constructs a `create`/`patch` call."""

    def __init__(self, base_url: Optional[str], key: Optional[str]):
        self._client = substrate_client.reader(base_url=base_url, key=key)

    def list_beads(self, namespace: str, type_: str, **params: Any) -> list[dict]:
        query = urllib.parse.urlencode({"namespace": namespace, "type": type_, **params})
        return self._client.get(f"/beads?{query}") or []

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: Optional[str] = None
    ) -> list[dict]:
        params: dict[str, str] = {"direction": direction}
        if link_type is not None:
            params["link_type"] = link_type
        query = urllib.parse.urlencode(params)
        return self._client.get(f"/beads/{bead_id}/links?{query}") or []


def gather_live_data(
    reader: SubstrateReader, charters: list[dict[str, Any]]
) -> tuple[list[dict], dict[str, str], list[dict]]:
    """Everything `build_release_report`/`compute_unassigned` need, fetched
    once. A release whose `arch.release` mirror has not landed yet (REL-1's
    loader has not run) simply delivers nothing — not an error, since the
    charter itself is git-authoritative and always loadable.
    """
    tasks = reader.list_beads("dev", "task", limit=2000)

    delivers: dict[str, str] = {}
    for charter in charters:
        matches = reader.list_beads("arch", "release", content_ref=charter["ref"], limit=1)
        if not matches:
            continue
        release_bead_id = matches[0]["id"]
        for link in reader.list_links(release_bead_id, direction="incoming", link_type="delivers"):
            delivers[link["source_id"]] = charter["ref"]

    conformances = reader.list_beads("arch", "requirement_conformance", limit=5000)
    return tasks, delivers, conformances


def main(
    argv: list[str] | None = None,
    *,
    reader_factory: Callable[[Optional[str], Optional[str]], SubstrateReader] = GateSubstrateReader,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(REPO), help="repository root")
    parser.add_argument("--release", help="report only this release ref")
    args = parser.parse_args(argv)

    repo = pathlib.Path(args.repo)
    release_load = _load_release_load_module()

    try:
        all_charter_items = release_load.load_charters(
            release_load.charter_paths(repo / RELEASES_DIR),
            requirements_dir=repo / REQUIREMENTS_DIR,
        )
    except SystemExit as exc:
        print(f"release-status: could not run: {exc}", file=sys.stderr)
        return 2

    # --release scopes which release *reports* get printed below. It must
    # never scope which charters feed `delivers`: that map backs the
    # unassigned-population count, whose label promises "no delivers edge to
    # any release" -- narrowing it to one release turns "delivers a
    # different release" into a false positive for "delivers nothing."
    charter_items = all_charter_items
    if args.release:
        charter_items = [item for item in all_charter_items if item.ref == args.release]
        if not charter_items:
            print(f"release-status: could not run: no such release {args.release!r}", file=sys.stderr)
            return 2

    reader = reader_factory(os.environ.get("SUBSTRATE_URL"), os.environ.get("SUBSTRATE_API_KEY"))

    try:
        tasks, delivers, conformances = gather_live_data(
            reader, [item.content for item in all_charter_items]
        )
    except RuntimeError as exc:
        print(f"release-status: could not run: {exc}", file=sys.stderr)
        return 2

    open_tasks = [t for t in tasks if (t.get("state") or "") not in TERMINAL_TASK_STATES]
    closed_tasks = [t for t in tasks if (t.get("state") or "") in CLOSED_TASK_STATES]
    unassigned = compute_unassigned(open_tasks, delivers)
    closed_unbound = compute_closed_unbound(closed_tasks, delivers)

    now = datetime.date.today().isoformat()
    sections = [
        format_release_report(
            build_release_report(item.content, tasks, delivers, conformances, now=now)
        )
        for item in charter_items
    ]
    sections.append(format_unassigned(unassigned))
    sections.append(format_closed_unbound(closed_unbound))
    print("\n\n".join(sections))

    # Deliberately 0 for any run that completed: this reports whether the
    # balance holds, it does not gate on it. See traceability.py's docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main())
