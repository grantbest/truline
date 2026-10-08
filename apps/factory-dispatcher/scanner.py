#!/usr/bin/env python3
"""Lane scanner skeleton — reports what it WOULD file, and files nothing.

    python scanner.py                    # every scanner
    python scanner.py --scanner requirement-gaps
    python scanner.py file               # distinct live filing invocation

Sprint 12.1. There is deliberately no flag that files anything: the ability to
write is absent from this module rather than defaulted off, because "the first
thing a work-generating agent does wrong is generate work" and a dry-run you can
turn off is not a dry-run. Filing, added later, is therefore a separate command,
not a switch on the scan path.

Two properties make the report worth reading rather than a list of guesses.

**Candidates are assessed through the real filing path.** ``file_task
.build_content`` is called on each one, so a candidate reported as fileable is
one that filing would actually accept — including the traceability refusal that
shipped on 2026-08-09, which rejects work carrying no resolving requirement
reference. A second copy of "would this file" would drift from the first, and
the copy that drifts is always the one doing the gating.

**Well-formed is not the same as doable.** A scanner can emit a correctly
scoped, schema-valid task proposing work the factory is forbidden to perform —
CI-scoped requirements are the obvious case. Those are counted separately,
because a queue full of them looks healthy right up until every one fails.

**A candidate is never produced from a scope with nowhere permitted to work.**
When every path a requirement names is one the factory may not touch, that is
not a malformed spec — it is a real gap no factory task can close, the same
class ``scanner-suppressions.yaml`` already names by hand for PC-TRU-001, just
recognised mechanically from scope alone. Such a finding is reported as
suppressed and never reaches ``build_content``, so it cannot masquerade as
"would be refused for a fixable reason" and cannot drag down the well-formed
count for a reason that will not change on its own. A finding with no derived
scope at all is a different claim again — "could not work out what to do", not
"nothing to do" — and is reported as underivable instead.

What this does NOT do, by design: dedupe against open tasks, rate-limit, or
honour suppression. That is 12.3, and it is rated high risk on its own because
the queue now drains on a schedule — a scanner that re-files the same finding
nightly starves every other lane automatically. The ``dedupe_key`` seam exists
here so 12.3 has something to attach to; nothing consumes it yet.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import guards
import process_env
from file_task import (
    CLOSED_TASK_STATES,
    FORBIDDEN_ALWAYS,
    _matches_spec_identity,
    _spec_identity,
    build_content,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES_DIR = REPO_ROOT / "docs" / "requirements"
TASKS_DIR = Path(__file__).resolve().parent / "tasks"
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"

#: Every directory the spec-record check reads as a queue. Declared once here
#: so a third queue directory is added by extending this tuple rather than by
#: touching scan_spec_record, which stays a per-directory classifier. Before
#: this, TASKS_DIR was the only entry and docs/plans/specs/ — a second queue
#: `file_task.py` never wrote to but specs still land in — was never scanned.
SPEC_QUEUE_DIRS: tuple[Path, ...] = (
    TASKS_DIR,
    REPO_ROOT / "docs" / "plans" / "specs",
)

#: Scope a generated task may claim, and what it may never touch. Sourced from
#: the same intake contract file_task uses, so a scanner cannot propose a task
#: that widens its own boundary past what a hand-written one could.
SCANNER_FORBIDDEN_PATHS = (
    ".github/**",
    "infrastructure/**",
    "docs/requirements/**",
    "docs/releases/**",
    "ARCHITECTURE.md",
    "apps/factory-dispatcher/tasks/**",
)

#: Content field recording which scanner candidate produced a task. Exact, so a
#: scanner never re-files its own work. ``DevTaskContent`` sets extra="allow"
#: (schemas.py:205), so this needs no schema change.
SCANNER_KEY_FIELD = "scanner_key"

#: How many tasks one scanner pass may file. The queue drains on a schedule, so
#: this is a denial-of-service control on the factory's own backlog rather than
#: tidiness: a scanner that files faster than the queue drains starves every
#: other lane, and sprint 12 named the shape — the first thing a work-generating
#: agent does wrong is generate work.
MAX_FILINGS_PER_RUN = 3
FILING_LANE = "code-health"
SCANNER_CREATED_BY = "factory-scanner"

#: Filing now refuses a spec naming neither a release nor a waiver (release
#: traceability, 2026-08-26). An automated filer that picked an open release
#: for every candidate would recreate the exact pile that mechanism exists to
#: end — the scanner's own findings are unattended and unreviewed at
#: generation time, which is precisely the population that "which release is
#: this actually for" cannot be guessed for. Defaulting to a written waiver
#: keeps the scanner's output a counted triage queue instead of a silent fill
#: of whatever release happens to be open.
SCANNER_RELEASE_WAIVER = (
    "filed by the lane scanner (Sprint 12.1); auto-filed candidates are "
    "unreviewed at generation time and default to a waiver rather than a "
    "guessed release, so they land as a counted triage queue"
)

# A top-level spec may reasonably exist briefly before it is filed. Past this,
# no exact-title bead means the reviewable queue and the bead queue have drifted.
SPEC_RECORD_UNFILED_GRACE = timedelta(days=7)

#: Suppressed candidates, by dedupe key, with the reason. A suppression is a
#: standing decision and must state why, so it can be revisited rather than
#: inherited.
SUPPRESSIONS_PATH = Path(__file__).resolve().parent / "scanner-suppressions.yaml"


@dataclass(frozen=True)
class Finding:
    """One thing a scanner noticed, before anyone decides it is worth doing."""

    scanner: str
    lane: str
    dedupe_key: str
    title: str
    intent: str
    requirement_refs: tuple[str, ...] = ()
    scope_paths: tuple[str, ...] = ()
    #: Set when the verdict this came from may no longer describe the code.
    #: Non-empty means "do not propose", and the reason is reported rather than
    #: swallowed — a silent skip is a hole, the same way a silent waiver is.
    stale_reason: str = ""


@dataclass(frozen=True)
class Candidate:
    """A finding, rendered as a spec, and what filing would say about it."""

    finding: Finding
    spec: dict[str, Any] = field(default_factory=dict)
    refusal: str = ""
    blocked_paths: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    #: Set when the queue already covers this. Distinct from ``refusal``: the
    #: spec is well-formed and would file, and the reason not to is the state of
    #: the queue rather than the candidate.
    duplicate_of: str = ""
    #: Set when a standing decision says do not file this, with the reason.
    suppressed_by: str = ""

    @property
    def fileable(self) -> bool:
        return not self.refusal

    @property
    def would_file(self) -> bool:
        """Passes every control, so a live run would actually write this."""
        return (
            self.fileable
            and not self.duplicate_of
            and not self.suppressed_by
            and not self.finding.stale_reason
        )

    @property
    def doable(self) -> bool:
        """Fileable, current, and with a path the factory may actually touch."""
        return self.fileable and bool(self.allowed_paths) and not self.finding.stale_reason

    @property
    def narrowed(self) -> bool:
        """Some of the requirement's paths were dropped to make this fileable.

        Worth its own flag rather than folding into `doable`: a silently
        narrowed scope is the most dangerous candidate this scanner can emit.
        It looks doable, files cleanly, and may have had the actual work
        removed from it — leaving a task that can be completed without
        addressing the requirement it cites.
        """
        return bool(self.blocked_paths) and bool(self.allowed_paths)


@dataclass(frozen=True)
class UngeneratedFinding:
    """A finding that never became a candidate — decided from scope alone,
    before ``build_content`` would have run.

    Two disjoint reasons this happens, and they must not be conflated: a
    finding whose derived scope is every path the factory may not touch is a
    real gap that no factory task can close — the same class PC-TRU-001's
    standing suppression already names by hand, just recognised mechanically
    from scope alone instead of decided by a person (``kind="scope-forbidden"``).
    A finding with no derived scope at all is the opposite claim — not "there
    is nothing to do" but "the generator could not work out what to do"
    (``kind="underivable"``). Reporting one as the other would state a claim
    nobody made.
    """

    finding: Finding
    kind: str
    reason: str


@dataclass(frozen=True)
class SpecRecordIssue:
    """One disagreement between ``tasks/`` as a record and dev.task beads."""

    kind: str
    path: Path
    title: str
    bead_id: str = ""
    bead_state: str = ""
    age_days: int | None = None
    #: Set when a ``_filing_note`` was present but did not exempt this spec —
    #: ``classify_filing_note``'s category, kept for diagnostics so a reader
    #: sees WHY the note did not save the filing (PRIN-008), not just that it
    #: didn't. Empty when no note was present at all.
    hold_note_category: str = ""

    @property
    def display_path(self) -> str:
        try:
            return str(self.path.relative_to(REPO_ROOT))
        except ValueError:
            return str(self.path)


# ---------------------------------------------------------------------------
# Hold-note classification — a _filing_note exempts a spec from being reported
# as a dropped filing only when it STATES a hold condition; mere presence or
# provenance never exempts (OPS-14's own note is the real no-hold example).
#
# Every stated condition is classified into exactly one of five categories.
# Two exempt (a live human hold, or a machine condition that has not yet
# occurred); three do not (no hold was ever stated; the condition already
# occurred, so the hold expired; or the note is closed-set-shaped but names no
# resolvable referent — unresolvability must never exempt, the same failure
# mode as an unresolvable requirement reference).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteVerdict:
    category: str
    reason: str

    @property
    def exempts(self) -> bool:
        return self.category in ("operator-decision", "machine-resolvable-pending")


#: Cue phrases that mark a note as STATING a hold, even when none of its
#: clauses resolves to anything nameable. Without this cue check, "the next
#: structural PR merges" (no PR number) would be indistinguishable from
#: OPS-14's pure provenance — both parse to zero clauses — but they are not
#: the same claim: one reads as a hold and names nothing, the other never
#: claimed to be a hold at all.
_HOLD_INTENT_CUES = (
    "do not file", "don't file", "file after", "file until", "hold until",
    "conditional", "moot", "not yet", "pending on", "wait for", "blocked on",
    "blocking",
)


def _has_hold_intent(note: str) -> bool:
    lowered = note.lower()
    return any(cue in lowered for cue in _HOLD_INTENT_CUES)


#: Decider + decision: "the Operator directs ..." / "has the Operator directed ...". Matched
#: before every other clause type -- a human still deciding governs the note
#: regardless of what any machine-resolvable sub-condition shows (A34-1's own
#: note: the amendment-status half has occurred, and the note is still a live
#: hold because the decision half has not).
_OPERATOR_DECISION_RE = re.compile(r"\b([A-Z][a-z]+)\s+direct(?:s|ed)\b([^.?!]*)")


def _operator_decision_clauses(note: str) -> list[tuple[str, str]]:
    clauses = []
    for m in _OPERATOR_DECISION_RE.finditer(note):
        decision = m.group(2).strip(" ?!.")
        if decision:
            clauses.append((m.group(1), decision))
    return clauses


#: "PR #NNN is merged" / "merges #NNN" -- any `#NNN` with "merge" in a small
#: window around it. A bare `#NNN` with no such window (OPS-14's own "#509
#: shipped the checker" is provenance, not a condition) is not a clause at
#: all.
def _pr_clauses(note: str) -> list[int]:
    clauses = []
    for m in re.finditer(r"#(\d+)", note):
        window = note[max(0, m.start() - 25): m.end() + 25].lower()
        if "merge" in window:
            clauses.append(int(m.group(1)))
    return clauses


#: "File AFTER R2605-1" / "until OPS-14" -- a spec-id shorthand this corpus
#: already uses for its own filenames. Case-sensitive by design: only the
#: keyword itself is matched case-insensitively (the scoped inline flag),
#: because a real id is always written in caps in this corpus and a
#: lowercase match would just as happily catch "after 13 days".
_BEAD_CLAUSE_RE = re.compile(r"\b(?i:after|until)\s+([A-Z]{1,6}\d*-\d+)\b")


def _bead_clauses(note: str) -> list[str]:
    return [m.group(1) for m in _BEAD_CLAUSE_RE.finditer(note)]


#: "Amendment 34 is PROPOSED" -- direct form.
_AMENDMENT_DIRECT_RE = re.compile(
    r"\bAmendment\s+(\d+)\s+(?:is|reaches|becomes)\s+`?([A-Za-z]+(?:\s+[A-Za-z]+)?)`?"
)
#: "A35 vote branches (withdraw: ...; ratify: ...)" -- OPS-70's own shape for
#: "resolves regardless of which way the vote goes": each semicolon-separated
#: label inside the parens is a candidate status word.
_AMENDMENT_BRANCHES_RE = re.compile(r"\bA(?:mendment)?\s*(\d+)\s+vote branches\s*\(([^)]*)\)")


def _amendment_clauses(note: str) -> list[tuple[int, list[str]]]:
    clauses: list[tuple[int, list[str]]] = []
    for m in _AMENDMENT_DIRECT_RE.finditer(note):
        clauses.append((int(m.group(1)), [m.group(2)]))
    for m in _AMENDMENT_BRANCHES_RE.finditer(note):
        words = []
        for segment in m.group(2).split(";"):
            segment = segment.strip()
            if not segment:
                continue
            head = re.split(r"[:\s]", segment, maxsplit=1)[0]
            if head:
                words.append(head)
        if words:
            clauses.append((int(m.group(1)), words))
    return clauses


def _stems_match(actual: str, candidate: str) -> bool:
    """Whether `candidate` (a word from a note, e.g. "withdraw") names the
    same status as `actual` (ARCHITECTURE.md's own word, e.g. "WITHDRAWN").

    Compared on a short letters-only prefix rather than full equality: notes
    use verb/label forms ("withdraw", "ratify") for the noun forms the
    document's Status line carries ("WITHDRAWN", "RATIFIED").
    """
    a = re.sub(r"[^A-Za-z]", "", actual).upper()
    b = re.sub(r"[^A-Za-z]", "", candidate).upper()
    if not a or not b:
        return False
    n = min(5, len(a), len(b))
    return a[:n] == b[:n]


def classify_filing_note(
    note: str,
    *,
    pr_merged_fn: Callable[[int], bool] = lambda n: False,
    bead_terminal_fn: Callable[[str], bool | None] = lambda spec_id: None,
    amendment_status_fn: Callable[[int], str | None] = lambda n: None,
) -> NoteVerdict:
    """Classify a ``_filing_note`` into exactly one of five categories.

    Two exempt a spec from being reported as a dropped filing:

    - ``operator-decision``: names a decider and a decision awaited. A live
      manual hold; never auto-failed, never silently passed.
    - ``machine-resolvable-pending``: names a PR-merged / bead-terminal /
      amendment-status condition that has not happened yet.

    Three do not:

    - ``no-hold``: no hold language at all -- mere presence or provenance
      (OPS-14's own note) never exempts.
    - ``machine-resolvable-expired``: the named condition has already
      occurred, so the hold is stale.
    - ``defective``: reads as a hold (closed-set-shaped or explicit hold
      language) but names no resolvable referent. Unresolvability must never
      exempt.

    Priority when a note carries more than one clause: an operator-decision
    clause always wins over any machine-resolvable clause in the same note,
    occurred; otherwise an OCCURRED machine-resolvable clause governs over a
    merely pending or unresolvable one in the same note (OPS-70's own note:
    an occurred amendment-status clause sits beside a volume-judgment clause
    naming no decider -- not a clause of any kind -- and the occurred clause
    governs, so the note fails as expired).
    """
    if not note.strip():
        return NoteVerdict("no-hold", "no _filing_note")

    operator = _operator_decision_clauses(note)
    if operator:
        decider, decision = operator[0]
        return NoteVerdict("operator-decision", f"{decider} decides: {decision}")

    outcomes: list[str] = []  # "occurred" | "pending" | "unresolvable"

    for amendment_id, candidates in _amendment_clauses(note):
        status = amendment_status_fn(amendment_id)
        if status is None:
            outcomes.append("unresolvable")
        elif any(_stems_match(status, word) for word in candidates):
            outcomes.append("occurred")
        else:
            outcomes.append("pending")

    for spec_id in _bead_clauses(note):
        terminal = bead_terminal_fn(spec_id)
        if terminal is None:
            outcomes.append("unresolvable")
        elif terminal:
            outcomes.append("occurred")
        else:
            outcomes.append("pending")

    for pr_number in _pr_clauses(note):
        outcomes.append("occurred" if pr_merged_fn(pr_number) else "pending")

    if "occurred" in outcomes:
        return NoteVerdict("machine-resolvable-expired", "a named condition has already occurred")
    if outcomes:
        if "pending" in outcomes:
            return NoteVerdict("machine-resolvable-pending", "named condition(s) not yet occurred")
        return NoteVerdict("defective", "closed-set-shaped condition names an unresolvable referent")

    if _has_hold_intent(note):
        return NoteVerdict("defective", "reads as a hold but names no resolvable condition")

    return NoteVerdict("no-hold", "no hold condition stated")


#: "### Amendment N" -- the only heading shape carrying a Status line this
#: parser reads; every other "### "/"## " heading resets tracking to None.
_AMENDMENT_HEADER_RE = re.compile(r"^### Amendment (\d+)\b")


def parse_amendment_statuses(text: str) -> dict[int, str]:
    """Amendment number -> its first ``**Status:**`` line's status word.

    Handles the three textual shapes ARCHITECTURE.md's §9 body actually
    carries: backticked (`` **Status:** `RATIFIED` ``), bare
    (``**Status:** RATIFIED -- in force...``), and absent (an amendment with
    no ``**Status:**`` line at all -- Amendments 1-24 carry Summary/
    Rationale/Diff and nothing else). Absent means the amendment simply has
    no status recorded, not a parse failure: it is missing from the returned
    dict, the same "unresolvable" outcome ``classify_filing_note`` gives a
    bead id that names no known spec.
    """
    statuses: dict[int, str] = {}
    current: int | None = None
    awaiting_status = False
    for line in text.splitlines():
        if line.startswith("### ") or line.startswith("## "):
            header = _AMENDMENT_HEADER_RE.match(line)
            current = int(header.group(1)) if header else None
            awaiting_status = current is not None
            continue
        if not awaiting_status or current is None:
            continue
        stripped = line.strip()
        if not stripped.startswith("**Status:**"):
            continue
        value = stripped[len("**Status:**"):].strip()
        word_match = re.match(r"^`?([A-Z]+(?:\s+[A-Z]+)*)`?", value)
        if word_match:
            statuses[current] = word_match.group(1)
        awaiting_status = False  # only the first Status line per section counts
    return statuses


def merged_pr_numbers(commit_subjects: Iterable[str]) -> frozenset[int]:
    """PR numbers recoverable from commit subjects via the same trailing
    ``(#NNN)`` the dispatcher's squash-merge always appends."""
    numbers = set()
    for subject in commit_subjects:
        m = re.search(r"\(#(\d+)\)$", subject)
        if m:
            numbers.add(int(m.group(1)))
    return frozenset(numbers)


def _bead_terminal_for_spec_id(
    spec_id: str,
    tasks: list[dict[str, Any]],
    queue_dirs: tuple[Path, ...] = SPEC_QUEUE_DIRS,
) -> bool | None:
    """Whether the bead for the spec whose filename starts with `spec_id`
    has reached a terminal state.

    None when no spec file's name starts with `spec_id` at all -- an
    unresolvable referent, same as an id typo. False when the spec is known
    but no bead matches its title yet (not started, or not yet filed) --
    "not occurred", not "unknown". True only once a matching bead is
    terminal.
    """
    for tasks_dir in queue_dirs:
        for path, _in_done, spec in _specs_under(tasks_dir):
            if path.stem.startswith(spec_id):
                title = str(spec.get("title") or "")
                matches = [
                    t for t in tasks if (t.get("content") or {}).get("title") == title
                ]
                if not matches:
                    return False
                return any(t.get("state") in CLOSED_TASK_STATES for t in matches)
    return None


def amendment_statuses(path: Path = ARCHITECTURE_PATH) -> dict[int, str]:
    return parse_amendment_statuses(path.read_text())


def default_note_verdict(
    note: str,
    tasks: list[dict[str, Any]],
    commit_subjects: tuple[str, ...] = (),
) -> NoteVerdict:
    """The real resolvers: ARCHITECTURE.md for amendment status, the spec/bead
    population for bead-terminal, merged commit subjects for PR-merged."""
    merged_prs = merged_pr_numbers(commit_subjects)
    statuses = amendment_statuses()
    return classify_filing_note(
        note,
        pr_merged_fn=lambda n: n in merged_prs,
        bead_terminal_fn=lambda spec_id: _bead_terminal_for_spec_id(spec_id, tasks),
        amendment_status_fn=lambda n: statuses.get(n),
    )


def _implementation_paths(requirement: dict[str, Any]) -> tuple[str, ...]:
    """Repo paths a requirement names, with any :line suffix dropped."""
    paths = []
    for entry in requirement.get("implementation") or []:
        text = str(entry).split(" ")[0].strip()
        if ":" in text:
            text = text.split(":", 1)[0]
        if text and ("/" in text or text.endswith(".md")):
            paths.append(text)
    return tuple(dict.fromkeys(paths))


def last_changed(paths: tuple[str, ...], repo_root: Path = REPO_ROOT) -> str | None:
    """ISO date of the most recent commit touching any of `paths`, or None.

    None means "git could not say" — no matching path, or no repository. That
    is treated as *not* stale, because refusing to propose anything when the
    lookup is unavailable would silently empty the scan.
    """
    if not paths:
        return None
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", *paths],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return None
    return proc.stdout.strip() or None


def last_changed_revision(paths: tuple[str, ...], repo_root: Path = REPO_ROOT) -> str | None:
    """Commit of the most recent change touching any of `paths`, or None."""
    if not paths:
        return None
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", *paths],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return None
    return proc.stdout.strip() or None


def revision_exists(revision: str, repo_root: Path = REPO_ROOT) -> bool:
    """Whether `revision` resolves to a commit in this repository."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return False
    return proc.returncode == 0


def is_ancestor(ancestor: str, descendant: str, repo_root: Path = REPO_ROOT) -> bool:
    """Whether `ancestor` is at or before `descendant` in git history."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return False
    return proc.returncode == 0


def _date_stale_reason(
    measured_at: str, changed_at: str | None, paths: tuple[str, ...]
) -> str:
    """Why a verdict cannot be trusted by the historical date rule.

    Same-day counts as stale. At date granularity the order of a measurement
    and a commit on the same day is unknowable, and the two errors are not
    symmetrical: proposing stale work burns a full attempt discovering there is
    nothing to do — the exact waste G-4 exists to stop — while skipping a fresh
    finding costs only that a human notices it later.
    """
    if not measured_at:
        return (
            "date rule: the verdict carries no measured_at, so its age cannot "
            "be established"
        )
    if changed_at is None:
        return ""
    if changed_at >= measured_at:
        return (
            f"date rule: measured {measured_at}, but "
            f"{', '.join(paths)} changed {changed_at} — "
            "the verdict may already be burnt down"
        )
    return ""


def _revision_stale_reason(
    measured_revision: str,
    last_touching_revision: str | None,
    paths: tuple[str, ...],
    revision_exists_fn: Callable[[str], bool],
    is_ancestor_fn: Callable[[str, str], bool],
) -> str:
    """Why a verdict cannot be trusted by the revision rule."""
    if not revision_exists_fn(measured_revision):
        return (
            f"revision rule: measured_revision {measured_revision} cannot be "
            "resolved in this repository, so it is not evidence of freshness"
        )
    if last_touching_revision is None:
        return ""
    if not is_ancestor_fn(last_touching_revision, measured_revision):
        return (
            f"revision rule: measured at revision {measured_revision}, but "
            f"{', '.join(paths)} last changed at revision {last_touching_revision}, "
            "which is not an ancestor of the measured revision"
        )
    return ""


def _stale_reason(
    failing_criteria: list[dict[str, Any]],
    changed_at: str | None,
    last_touching_revision: str | None,
    paths: tuple[str, ...],
    revision_exists_fn: Callable[[str], bool],
    is_ancestor_fn: Callable[[str, str], bool],
) -> str:
    """Why the grouped finding may no longer describe the code."""
    criteria_with_revision = [
        ac for ac in failing_criteria if str(ac.get("measured_revision") or "").strip()
    ]
    for ac in criteria_with_revision:
        reason = _revision_stale_reason(
            str(ac.get("measured_revision")).strip(),
            last_touching_revision,
            paths,
            revision_exists_fn,
            is_ancestor_fn,
        )
        if reason:
            return reason

    criteria_without_revision = [
        ac for ac in failing_criteria if not str(ac.get("measured_revision") or "").strip()
    ]
    if not criteria_without_revision:
        return ""
    measured = min(
        (str(ac.get("measured_at") or "") for ac in criteria_without_revision),
        default="",
    )
    return _date_stale_reason(measured, changed_at, paths)


def scan_requirement_gaps(
    registries_dir: Path = REGISTRIES_DIR,
    last_changed_fn: Callable[[tuple[str, ...]], str | None] = last_changed,
    last_changed_revision_fn: Callable[
        [tuple[str, ...]], str | None
    ] = last_changed_revision,
    revision_exists_fn: Callable[[str], bool] = revision_exists,
    is_ancestor_fn: Callable[[str, str], bool] = is_ancestor,
) -> list[Finding]:
    """Requirements carrying at least one acceptance criterion marked `fail`.

    Chosen as the skeleton's first source because it needs no new measurement
    and no judgement: the verdicts are dated snapshots someone already made, and
    each one carries its own citation — a gap in PC-SUB-001/AC-1 cites
    PC-SUB-001/AC-1. That sidesteps the trap the traceability refusal was built
    to catch, where a generator invents a plausible-looking reference.

    Grouped per requirement rather than per criterion: three failing criteria on
    one requirement are one piece of work, not three.

    LifeOps (LO-*) requirements are deliberately skipped. They are product
    behaviour, and which lane owns them and what scope they touch is a judgement
    a scanner reading a registry does not have. Emitting them would be the
    "well-formed, correctly-scoped, pointless" failure this dry-run exists to
    surface, so the skeleton declines rather than guesses.
    """
    findings: list[Finding] = []
    for path in sorted(registries_dir.glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        registry = json.loads(path.read_text())
        for requirement in registry.get("requirements") or []:
            rid = str(requirement.get("id") or "")
            if not rid.startswith("PC-"):
                continue
            failing_criteria = [
                ac
                for ac in requirement.get("acceptance_criteria") or []
                if ac.get("conformance") == "fail"
            ]
            if not failing_criteria:
                continue
            failing = [str(ac.get("id")) for ac in failing_criteria]
            title = str(requirement.get("title") or rid)
            paths = _implementation_paths(requirement)
            uses_revision = any(
                str(ac.get("measured_revision") or "").strip() for ac in failing_criteria
            )
            uses_date = any(
                not str(ac.get("measured_revision") or "").strip()
                for ac in failing_criteria
            )
            stale = _stale_reason(
                failing_criteria,
                last_changed_fn(paths) if uses_date else None,
                last_changed_revision_fn(paths) if uses_revision else None,
                paths,
                revision_exists_fn,
                is_ancestor_fn,
            )
            findings.append(
                Finding(
                    scanner="requirement-gaps",
                    lane="code-health",
                    dedupe_key=f"requirement-gaps:{rid}",
                    title=f"{rid} does not hold: {title}",
                    intent=(
                        f"{rid} — \"{title}\" — is recorded as {requirement.get('status')} "
                        f"with {len(failing)} acceptance criterion/criteria marked fail: "
                        f"{', '.join(failing)}.\n\n"
                        f"Rationale of record: {requirement.get('rationale') or '(none recorded)'}\n\n"
                        "This candidate was produced by a scanner from a dated conformance "
                        "snapshot, not from reading the code. The verdict may have been burnt "
                        "down since it was recorded. Confirm the criterion still fails at HEAD "
                        "before doing any work, and if it now passes, say so and change nothing."
                    ),
                    requirement_refs=tuple(f"{rid}/{ac}" for ac in failing),
                    scope_paths=paths,
                    stale_reason=stale,
                )
            )
    return findings


SCANNERS: dict[str, Callable[..., list[Finding]]] = {
    "requirement-gaps": scan_requirement_gaps,
}


def finding_to_spec(
    finding: Finding, scope_paths: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Render a finding as the spec dict file_task would be handed.

    `scope_paths` overrides the finding's own, so a candidate can be assessed
    with forbidden entries removed rather than being reported as unfileable for
    naming one.
    """
    paths = finding.scope_paths if scope_paths is None else scope_paths
    return {
        "lane": finding.lane,
        "title": finding.title,
        "intent": finding.intent,
        "acceptance": [
            f"THE criterion/criteria {', '.join(finding.requirement_refs)} SHALL hold, "
            "or the task SHALL report why the recorded verdict no longer applies.",
        ],
        "verification": {
            "commands": [
                "cd apps/factory-dispatcher && python -m pytest tests/ -q",
                "venv/bin/python scripts/check-repo-invariants.py",
            ],
            "must_report_unverified": True,
        },
        "scope": {
            "paths": list(paths),
            "forbidden_paths": list(SCANNER_FORBIDDEN_PATHS),
        },
        "risk_class": "behavioral",
        "requirement_refs": list(finding.requirement_refs),
        "release_ref_waived": SCANNER_RELEASE_WAIVER,
        "worker_hint": "claude",
    }


def blocked_paths(scope_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Scope entries the factory is forbidden to touch.

    A task scoped entirely to forbidden paths is well-formed and impossible.
    PC-DEL-001 and PC-TRU-001 are the live examples: both are CI-scoped, both
    are the Operator's alone, and a scanner reading only the registry would happily
    propose them.
    """
    forbid = list(SCANNER_FORBIDDEN_PATHS) + list(FORBIDDEN_ALWAYS)
    return tuple(p for p in scope_paths if guards.path_forbidden(p, forbid))


def ungenerated_reason(finding: Finding) -> tuple[str, str]:
    """("", "") when `finding` should be assessed as a candidate; otherwise
    (kind, reason) for why no candidate is produced for it at all.

    Checked before ``assess`` runs ``build_content``, so a finding this
    catches never reaches it: giving ``build_content`` an empty scope.paths
    would report "scope.paths must list at least one path", which reads as a
    fixable spec defect. It is not — the gap is real and no factory task can
    close it, and that is a different claim from "malformed", so it is decided
    here instead of let fall through to that refusal.
    """
    if not finding.scope_paths:
        return (
            "underivable",
            "the requirement names no repo path to work in — a scope could "
            "not be derived for it at all",
        )
    blocked = blocked_paths(finding.scope_paths)
    if set(blocked) >= set(finding.scope_paths):
        return (
            "scope-forbidden",
            "every path this requirement names is one the factory may not "
            "touch (" + ", ".join(blocked) + ") — a real gap, not a factory task",
        )
    return ("", "")


def load_suppressions(path: Path = SUPPRESSIONS_PATH) -> dict[str, str]:
    """Suppressed dedupe keys mapped to the reason. Absent file means none."""
    if not path.exists():
        return {}
    import yaml  # imported here so the dry run works without PyYAML installed

    loaded = yaml.safe_load(path.read_text()) or {}
    entries = loaded.get("suppress") or {}
    return {str(k): str(v) for k, v in entries.items()}


# ---------------------------------------------------------------------------
# Doctrine staleness (F-DCE-5) — docs/architecture/doctrine.md, PRIN-011.
#
# An `adopted`/`enforced` arch.principle bead with no incoming `applies` link
# and no incoming `enforced_by` link, past a configurable window since its
# last dated status transition, governs nothing and the system should say so.
# This rule does not file a dev.task — a stale citation is a measurement, not
# work — so it does not join SCANNERS/Finding/Candidate below. It is emitted
# as an arch.observation by activities/doctrine_staleness.py, the same shape
# activities/staleness_report.py already writes, so it surfaces wherever
# those already surface. The pure decision lives here so the one suppression
# file (load_suppressions, above) governs this rule exactly like every other.
# ---------------------------------------------------------------------------

#: Default gap between a principle's last dated status transition and the
#: point an unapplied adopted/enforced principle is worth a verdict. "A
#: configurable window" per F-DCE-5 — callers of principle_staleness_reason
#: may pass a different timedelta; this is only the default.
DOCTRINE_STALENESS_WINDOW = timedelta(days=14)

#: Dedupe-key / suppression-key prefix for this rule, in the same
#: `scanner:key` shape every other scanner-suppressions.yaml entry uses.
DOCTRINE_STALENESS_SCANNER = "doctrine-staleness"


def doctrine_staleness_dedupe_key(principle_id: str) -> str:
    """The scanner-suppressions.yaml key for one principle's staleness verdict."""
    return f"{DOCTRINE_STALENESS_SCANNER}:{principle_id}"


def principle_last_transition_date(content: dict[str, Any]) -> str | None:
    """ISO date of the most recent ``status_history`` entry, or None.

    None means the principle carries no dated transition to measure from —
    malformed for an adopted/enforced principle, but repairing that is not
    this rule's job. Absent a date the window cannot be established, so the
    caller treats the principle as not (yet provably) stale rather than
    guessing at one.
    """
    history = content.get("status_history") or []
    dates = [str(entry.get("date")) for entry in history if entry.get("date")]
    return max(dates) if dates else None


def principle_staleness_reason(
    principle_id: str,
    content: dict[str, Any],
    has_governing_link: bool,
    now: datetime,
    window: timedelta = DOCTRINE_STALENESS_WINDOW,
) -> str:
    """Why `principle_id` is an unapplied-doctrine verdict, or "" if it is not.

    Three ways out before the window is even checked: not adopted/enforced
    (proposed and retired principles carry no promise to be applied, so this
    rule ignores them entirely — a Tier 3 conjecture or a demoted principle is
    not doctrine rot); already governed by an `applies` or `enforced_by` edge;
    or carrying no dated status transition to measure the window from.
    """
    status = str(content.get("status") or "")
    if status not in ("adopted", "enforced"):
        return ""
    if has_governing_link:
        return ""
    last_transition = principle_last_transition_date(content)
    if last_transition is None:
        return ""
    try:
        transition_dt = datetime.fromisoformat(last_transition)
    except ValueError:
        return ""
    if transition_dt.tzinfo is None:
        transition_dt = transition_dt.replace(tzinfo=timezone.utc)
    elapsed = now - transition_dt
    if elapsed < window:
        return ""
    return (
        f"{principle_id} has been {status} since {last_transition} "
        f"({elapsed.days} days, window {window.days} days) with no applies "
        "citation and no enforced_by edge"
    )


def open_task_state(store) -> tuple[frozenset[str], frozenset[str], int]:
    """What the queue already covers: requirement refs, scanner keys, and size.

    ``store`` is passed in rather than constructed, so a dry run against a
    reachable substrate and a unit test share one path.

    Closed states are ``file_task.CLOSED_TASK_STATES`` rather than a local
    literal — a superseded bead is dead, not merely quiet, and must not keep
    suppressing re-filing of the requirement it once cited (mirrors
    dispatch.py:CLOSED_TASK_STATES; the agreement is asserted in
    tests/test_scanner.py).
    """
    refs: set[str] = set()
    keys: set[str] = set()
    open_tasks = 0
    for task in store.list_tasks():
        if task.get("state") in CLOSED_TASK_STATES:
            continue
        open_tasks += 1
        content = task.get("content") or {}
        refs.update(content.get("requirement_refs") or [])
        key = content.get(SCANNER_KEY_FIELD)
        if key:
            keys.add(str(key))
    return frozenset(refs), frozenset(keys), open_tasks


def duplicate_reason(
    finding: Finding, covered_refs: frozenset[str], filed_keys: frozenset[str]
) -> str:
    """Why this candidate is already in the queue, or "" if it is not.

    Two rules, because they catch different things.

    The scanner key is exact and catches the scanner re-filing its own work.

    Requirement coverage catches work a human already filed, and it is a
    **union** across open tasks rather than a per-task containment. Measured on
    2026-08-12: the ``PC-SUB-001`` candidate is covered by two open beads —
    ``71fb26bb`` holds AC-1 and AC-3, ``a2e8468e`` holds AC-2 — and no single
    task covers it. A per-task rule would have filed a fourth bead for work
    already twice claimed.

    Coverage must be **total**. A candidate sharing one ref with an open task is
    not a duplicate: ``d6d3cee7`` is ARCHITECTURE.md drift work that cites
    ``PC-SUB-001/AC-1`` in passing, and treating that as a match would suppress
    unrelated candidates for as long as it stays open.
    """
    if finding.dedupe_key in filed_keys:
        return f"already filed by this scanner as {finding.dedupe_key}"
    refs = set(finding.requirement_refs)
    if refs and refs <= covered_refs:
        return (
            "every requirement it cites is already covered by open work: "
            + ", ".join(sorted(refs))
        )
    return ""


def git_mtime(path: Path, repo_root: Path = REPO_ROOT) -> datetime | None:
    """Commit time of the most recent git change to ``path``, or None."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        rel = path
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(rel)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except ValueError:
        return None


#: The dispatcher composes a PR title as ``{kind}({lane}): {spec title}``
#: (dispatch.py) and GitHub's squash merge appends `` (#<pr>)`` to make the
#: commit subject. Extracting the middle group is what makes the match exact:
#: comparing a spec title against the raw subject would never match anything
#: (the wrapper is always present), and matching by substring/grep would match
#: too much — any subject that happens to *contain* the title. This regex
#: recovers exactly the title component, so equality against it is both exact
#: and no looser than the wrapper the dispatcher actually writes.
_COMMIT_SUBJECT_TITLE_RE = re.compile(r"^[\w.-]+\([^)]*\): (?P<title>.*) \(#\d+\)$")


def merged_commit_subjects(repo_root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Commit subjects reachable from main, or from HEAD if main cannot be
    resolved. Empty tuple, never an exception, if git is unavailable — this
    check needs no substrate and must still answer when the store is not.
    """
    for rev in ("main", "HEAD"):
        try:
            proc = subprocess.run(
                ["git", "log", "--format=%s", rev],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                env=process_env.child_env(),
            )
        except OSError:
            return ()
        if proc.returncode == 0:
            return tuple(line for line in proc.stdout.splitlines() if line)
    return ()


def merged_titles(commit_subjects: Iterable[str]) -> frozenset[str]:
    """Spec titles recoverable from ``commit_subjects`` by exact extraction.

    A subject that does not match the dispatcher's ``kind(lane): title
    (#pr)`` template contributes nothing — it is not evidence either way.
    """
    titles = set()
    for subject in commit_subjects:
        match = _COMMIT_SUBJECT_TITLE_RE.match(subject)
        if match:
            titles.add(match.group("title"))
    return frozenset(titles)


def _specs_under(tasks_dir: Path) -> tuple[tuple[Path, bool, dict[str, Any]], ...]:
    """Spec JSON files under ``tasks/``; bool is True for ``done/`` specs."""
    specs: list[tuple[Path, bool, dict[str, Any]]] = []
    for path in sorted(tasks_dir.glob("*.json")):
        specs.append((path, False, json.loads(path.read_text())))
    done_dir = tasks_dir / "done"
    for path in sorted(done_dir.glob("*.json")):
        specs.append((path, True, json.loads(path.read_text())))
    return tuple(specs)


def _age_days(since: datetime | None, now: datetime) -> int | None:
    if since is None:
        return None
    seconds = max(0.0, (now - since).total_seconds())
    return int(seconds // 86400)


def scan_spec_record(
    store: Any,
    tasks_dir: Path = TASKS_DIR,
    mtime_fn: Callable[[Path], datetime | None] = git_mtime,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    unfiled_grace: timedelta = SPEC_RECORD_UNFILED_GRACE,
    commit_subjects_fn: Callable[[], Iterable[str]] = merged_commit_subjects,
    note_verdict_fn: Callable[
        [str, list[dict[str, Any]], tuple[str, ...]], NoteVerdict
    ] = default_note_verdict,
) -> tuple[SpecRecordIssue, ...]:
    """Find drift between checked-in task specs and dev.task bead state.

    Report-only by construction: this reads specs and tasks, and never calls a
    store write method or edits files.
    """
    tasks = list(store.list_tasks())
    now = now_fn()
    subjects = tuple(commit_subjects_fn())
    merged = merged_titles(subjects)
    issues: list[SpecRecordIssue] = []

    for path, in_done, spec in _specs_under(tasks_dir):
        title = str(spec.get("title") or "")
        # Bead matching is match-first-then-state: the reused matcher (path
        # or title) decides whether ANY bead matches, in any state, before a
        # _filing_note is ever consulted. A `done/` spec's recorded identity
        # is the path it was filed at (top-level), never updated when the
        # file later moves here -- so a `done/` spec still matches by title
        # only, unchanged from before.
        spec_identity = _spec_identity(str(path))
        matches = [
            task for task in tasks
            if _matches_spec_identity(task, spec_identity, title)
        ]
        if in_done:
            # "done" and "superseded" are both closed — either belongs in done/.
            # Only a bead that reopened (review, pending, doing, failed, ...)
            # makes a done/ spec disagree with the record.
            not_done = [
                task for task in matches if task.get("state") not in ("done", "superseded")
            ]
            if not_done:
                task = not_done[0]
                issues.append(
                    SpecRecordIssue(
                        kind="done-spec-not-done",
                        path=path,
                        title=title,
                        bead_id=str(task.get("id") or ""),
                        bead_state=str(task.get("state") or "(missing)"),
                    )
                )
            elif not matches:
                # No bead at all is not the same disagreement as a reopened
                # bead: it is a spec whose work shipped before dev.task beads
                # were the record, not a reopening. See done-spec-no-bead
                # handling in render_report — it must never be reported as
                # drift an operator has to act on.
                issues.append(
                    SpecRecordIssue(
                        kind="done-spec-no-bead",
                        path=path,
                        title=title,
                        bead_state="(no matching bead)",
                    )
                )
            continue

        # A closed bead (done/superseded/archived) sharing this identity is
        # only drift if EVERY matching bead is closed. A live match
        # (pending/doing/review/failed) means the spec was re-filed onto a
        # fresh bead -- via --supersede or a bare regression re-file -- and
        # is correctly queued again, not disagreeing with the record.
        all_closed = bool(matches) and all(
            task.get("state") in CLOSED_TASK_STATES for task in matches
        )

        done = [task for task in matches if task.get("state") == "done"]
        if all_closed and done:
            task = done[0]
            issues.append(
                SpecRecordIssue(
                    kind="queued-spec-done",
                    path=path,
                    title=title,
                    bead_id=str(task.get("id") or ""),
                    bead_state="done",
                )
            )
            continue

        superseded = [task for task in matches if task.get("state") == "superseded"]
        if all_closed and superseded:
            task = superseded[0]
            issues.append(
                SpecRecordIssue(
                    kind="queued-spec-superseded",
                    path=path,
                    title=title,
                    bead_id=str(task.get("id") or ""),
                    bead_state="superseded",
                )
            )
            continue

        if not matches:
            # Only match-none reaches the note. Presence alone never exempts
            # — classify_filing_note must find a STATED hold condition
            # (operator-decision, or machine-resolvable and not yet
            # occurred). Everything else (no note, an expired condition, a
            # defective one) falls through to the existing drift reporting
            # exactly as if no note were there at all.
            note = str(spec.get("_filing_note") or "")
            verdict = note_verdict_fn(note, tasks, subjects) if note else None
            if verdict is not None and verdict.exempts:
                continue
            hold_note_category = verdict.category if verdict is not None else ""

            if title in merged:
                # Opposite condition from "not filed yet": the work is
                # already on main, so the unfiled grace period — built for
                # "written this morning" — does not apply.
                issues.append(
                    SpecRecordIssue(
                        kind="queued-spec-merged-no-bead",
                        path=path,
                        title=title,
                        hold_note_category=hold_note_category,
                    )
                )
                continue
            changed_at = mtime_fn(path)
            if changed_at is None or now - changed_at > unfiled_grace:
                issues.append(
                    SpecRecordIssue(
                        kind="queued-spec-no-bead",
                        path=path,
                        title=title,
                        age_days=_age_days(changed_at, now),
                        hold_note_category=hold_note_category,
                    )
                )

    return tuple(issues)


#: Issue kinds that mean the spec belongs in ``done/`` — its bead has closed.
_CLOSED_AT_TOP_LEVEL = ("queued-spec-done", "queued-spec-superseded")


def reconcile_spec_record(
    issues: tuple[SpecRecordIssue, ...],
) -> tuple[SpecRecordIssue, ...]:
    """Move spec files so a queue directory agrees with the bead state ``scan_spec_record`` read.

    Acts only on an issue already resolved to a bead by exact title match: a
    top-level spec whose bead is done or superseded moves into that queue's
    ``done/``; a ``done/`` spec whose bead is resolved but not done (reopened)
    moves back to the top level. An issue with no resolving bead — ``bead_id``
    empty — is left exactly where it is: REC-1's rule that a check may not act
    on a spec it cannot resolve applies to moving files too, not only to
    reporting. That includes ``queued-spec-merged-no-bead``: a merged spec
    with no bead is not "resolved to a bead", so it is reported and left
    alone here too — moving it is the absorption step, not detection.

    The destination is derived from ``issue.path`` rather than a passed-in
    directory, because SPEC_QUEUE_DIRS now names more than one queue and each
    issue must move within the queue it came from.

    Takes the issue tuple, not the store, so it cannot create, modify, or
    transition a bead even by accident — that capability is simply absent
    from this function's inputs. It moves the file only; the spec's content
    is never read for anything but its path.
    """
    moved: list[SpecRecordIssue] = []
    for issue in issues:
        if issue.kind in _CLOSED_AT_TOP_LEVEL:
            done_dir = issue.path.parent / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            issue.path.rename(done_dir / issue.path.name)
            moved.append(issue)
        elif issue.kind == "done-spec-not-done" and issue.bead_id:
            issue.path.rename(issue.path.parent.parent / issue.path.name)
            moved.append(issue)
    return tuple(moved)


def reconcile_report(moved: tuple[SpecRecordIssue, ...]) -> str:
    """One-line "nothing to do" when ``moved`` is empty, else what moved where.

    "Nothing to do" and "did not run" must stay distinguishable — the same
    requirement REC-1 put on the report applies to the mechanism that acts.
    """
    if not moved:
        return (
            "spec record reconcile: tasks/ already agrees with the bead "
            "record; nothing to move."
        )
    lines = ["spec record reconcile: moved the following to agree with bead state:"]
    for issue in moved:
        destination = "done/" if issue.kind in _CLOSED_AT_TOP_LEVEL else "tasks/"
        bead = issue.bead_id or "?"
        lines.append(
            f"- {issue.title} -> {destination}{issue.path.name} "
            f"(bead {bead} state={issue.bead_state})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mirror direction — a dev.task whose spec_identity names a tasks/ spec that
# is present nowhere on merged main is an ahead-of-gate filing: it was filed
# from a working tree main never saw. The remedy differs by bead state, so
# the labels differ: a live bead's spec wants routing to review; a terminal
# bead's spec wants committing to done/ as the record, before a git clean
# erases the only copy of a shipped work's intent.
# ---------------------------------------------------------------------------

#: Kept separate from ``TASKS_DIR``'s repo-relative form so the git plumbing
#: below can build a path argument without a live filesystem round trip.
_TASKS_DIR_REL = "apps/factory-dispatcher/tasks"


def merged_main_tasks_basenames(repo_root: Path = REPO_ROOT) -> frozenset[str] | None:
    """Basenames of every ``*.json`` spec present in ``tasks/`` or
    ``tasks/done/`` on the resolved ``main`` ref, or None when ``main``
    cannot be resolved at all.

    Deliberately NOT ``merged_commit_subjects``: that helper falls back to
    HEAD when ``main`` is unresolvable (scanner.py:749), which is correct for
    the ``queued-spec-merged-no-bead`` kind it already serves (a merge this
    worktree cannot yet see main for is still real, shipped work) and wrong
    here — it would make a spec filed only on a worker branch look present on
    main. This check must report cannot-evaluate instead, never "filed".
    """
    try:
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "main^{commit}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return None
    if verify.returncode != 0:
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "main", "--", _TASKS_DIR_REL],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=process_env.child_env(),
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return frozenset(
        Path(line).name for line in proc.stdout.splitlines() if line.endswith(".json")
    )


def scan_ahead_of_gate_filings(
    tasks: list[dict[str, Any]],
    main_basenames_fn: Callable[[], frozenset[str] | None] = merged_main_tasks_basenames,
) -> tuple[SpecRecordIssue, ...] | None:
    """Every dev.task, in any state, whose ``content.spec_identity`` names a
    ``tasks/`` spec absent — by basename, checked against both ``tasks/`` and
    ``tasks/done/`` — from merged main.

    None (cannot-evaluate) when ``main`` cannot be resolved; never an empty
    tuple in that case, which would read as "checked, and clean".

    Labels key on the bead's own ``state`` field, never on commit-subject
    extraction: OPS-82's own filing commit does not match
    ``_COMMIT_SUBJECT_TITLE_RE``, so that signal is lossy for exactly the
    population this check exists to find.
    """
    basenames = main_basenames_fn()
    if basenames is None:
        return None
    issues: list[SpecRecordIssue] = []
    for task in tasks:
        content = task.get("content") or {}
        spec_identity = str(content.get("spec_identity") or "")
        if not spec_identity.startswith(_TASKS_DIR_REL + "/"):
            continue
        if Path(spec_identity).name in basenames:
            continue
        state = str(task.get("state") or "")
        kind = "ahead-of-gate-terminal" if state in CLOSED_TASK_STATES else "ahead-of-gate-live"
        issues.append(
            SpecRecordIssue(
                kind=kind,
                path=Path(spec_identity),
                title=str(content.get("title") or ""),
                bead_id=str(task.get("id") or ""),
                bead_state=state,
            )
        )
    return tuple(issues)


def assess(finding: Finding) -> Candidate:
    """Run a finding through the real filing path and record the verdict."""
    blocked = blocked_paths(finding.scope_paths)
    allowed = tuple(p for p in finding.scope_paths if p not in set(blocked))
    spec = finding_to_spec(finding, allowed)
    common = {"finding": finding, "spec": spec, "blocked_paths": blocked, "allowed_paths": allowed}
    try:
        build_content(spec)
    except SystemExit as exc:
        return Candidate(**common, refusal=str(exc))
    except Exception as exc:  # noqa: BLE001 - report, never raise, during a dry run
        return Candidate(**common, refusal=f"{type(exc).__name__}: {exc}")
    return Candidate(**common)


@dataclass(frozen=True)
class Report:
    candidates: tuple[Candidate, ...]
    #: False when no substrate was reachable, so "no duplicates" means "not
    #: checked" rather than "the queue is clear".
    queue_known: bool = False
    spec_record_issues: tuple[SpecRecordIssue, ...] = ()
    #: False when no substrate was reachable, so record agreement cannot be
    #: asserted. The spec files alone are only half of this invariant.
    spec_record_known: bool = False
    ahead_of_gate_issues: tuple[SpecRecordIssue, ...] = ()
    #: False when no substrate was reachable OR ``main`` could not be
    #: resolved — either way the mirror direction cannot be asserted, and
    #: must report cannot-evaluate rather than implying clean.
    ahead_of_gate_known: bool = False
    #: Findings decided from scope alone, before ``assess`` ever ran. Not
    #: ``Candidate``s and not counted in ``candidates`` — a finding here was
    #: never produced as a candidate at all, which is the point.
    ungenerated: tuple[UngeneratedFinding, ...] = ()

    @property
    def may_file_live(self) -> bool:
        """Whether a live run is allowed to write at all.

        Dedup is not advisory. With no substrate reachable the queue cannot be
        consulted, and this report would file duplicates it simply could not
        see — on 2026-08-12 that is 1 of the 3 it would write. A live filer must
        refuse the whole pass rather than file the part it can still justify.
        """
        return self.queue_known

    @property
    def would_file(self) -> tuple[Candidate, ...]:
        """What a live run would write, in order, after the rate limit."""
        return tuple(
            c for c in self.candidates if c.would_file and c.finding.lane == FILING_LANE
        )[:MAX_FILINGS_PER_RUN]

    @property
    def held_by_rate_limit(self) -> tuple[Candidate, ...]:
        return tuple(
            c for c in self.candidates if c.would_file and c.finding.lane == FILING_LANE
        )[MAX_FILINGS_PER_RUN:]

    @property
    def wrong_lane(self) -> tuple[Candidate, ...]:
        return tuple(
            c for c in self.candidates if c.would_file and c.finding.lane != FILING_LANE
        )

    @property
    def duplicates(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.duplicate_of)

    @property
    def suppressed(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.suppressed_by)

    @property
    def fileable(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.fileable)

    @property
    def refused(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if not c.fileable)

    @property
    def doable(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.doable)

    @property
    def narrowed(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.narrowed and c.doable)

    @property
    def stale(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.finding.stale_reason)

    @property
    def scope_forbidden(self) -> tuple[UngeneratedFinding, ...]:
        """Never produced: every derived path is one the factory may not touch."""
        return tuple(u for u in self.ungenerated if u.kind == "scope-forbidden")

    @property
    def underivable(self) -> tuple[UngeneratedFinding, ...]:
        """Never produced: no scope could be derived for the finding at all."""
        return tuple(u for u in self.ungenerated if u.kind == "underivable")

    @property
    def duplicate_keys(self) -> tuple[str, ...]:
        seen: dict[str, int] = {}
        for candidate in self.candidates:
            key = candidate.finding.dedupe_key
            seen[key] = seen.get(key, 0) + 1
        return tuple(sorted(k for k, n in seen.items() if n > 1))


def dry_run(
    scanners: dict[str, Callable[..., list[Finding]]] | None = None,
    store: Any = None,
    suppressions: dict[str, str] | None = None,
    queue_dirs: tuple[Path, ...] = SPEC_QUEUE_DIRS,
    spec_mtime_fn: Callable[[Path], datetime | None] = git_mtime,
    spec_now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    spec_commit_subjects_fn: Callable[[], Iterable[str]] = merged_commit_subjects,
    ahead_of_gate_main_basenames_fn: Callable[
        [], frozenset[str] | None
    ] = merged_main_tasks_basenames,
) -> Report:
    """Run every scanner and assess what it would file. Writes nothing.

    ``store`` is optional so the dry run still works with no substrate
    reachable. Without it the queue cannot be consulted, so nothing is marked
    duplicate and the report says so rather than implying the queue is empty.
    """
    selected = SCANNERS if scanners is None else scanners
    suppressed = load_suppressions() if suppressions is None else suppressions
    if store is None:
        covered_refs, filed_keys, queue_known = frozenset(), frozenset(), False
        spec_record_issues: tuple[SpecRecordIssue, ...] = ()
        spec_record_known = False
        ahead_of_gate_issues: tuple[SpecRecordIssue, ...] = ()
        ahead_of_gate_known = False
    else:
        covered_refs, filed_keys, _ = open_task_state(store)
        queue_known = True
        spec_record_issues = tuple(
            issue
            for tasks_dir in queue_dirs
            for issue in scan_spec_record(
                store,
                tasks_dir=tasks_dir,
                mtime_fn=spec_mtime_fn,
                now_fn=spec_now_fn,
                commit_subjects_fn=spec_commit_subjects_fn,
            )
        )
        spec_record_known = True
        ahead_of_gate_result = scan_ahead_of_gate_filings(
            store.list_tasks(), main_basenames_fn=ahead_of_gate_main_basenames_fn
        )
        ahead_of_gate_known = ahead_of_gate_result is not None
        ahead_of_gate_issues = ahead_of_gate_result or ()

    candidates: list[Candidate] = []
    ungenerated: list[UngeneratedFinding] = []
    for name in sorted(selected):
        for finding in selected[name]():
            kind, reason = ungenerated_reason(finding)
            if kind:
                ungenerated.append(
                    UngeneratedFinding(finding=finding, kind=kind, reason=reason)
                )
                continue
            candidate = assess(finding)
            suppression = suppressed.get(finding.dedupe_key, "")
            if suppression:
                candidate = replace(candidate, suppressed_by=suppression)
            elif queue_known:
                duplicate = duplicate_reason(finding, covered_refs, filed_keys)
                if duplicate:
                    candidate = replace(candidate, duplicate_of=duplicate)
            candidates.append(candidate)
    return Report(
        candidates=tuple(candidates),
        queue_known=queue_known,
        spec_record_issues=spec_record_issues,
        spec_record_known=spec_record_known,
        ahead_of_gate_issues=ahead_of_gate_issues,
        ahead_of_gate_known=ahead_of_gate_known,
        ungenerated=tuple(ungenerated),
    )


def render_report(report: Report) -> str:
    lines = [
        "Lane scanner — DRY RUN. Nothing was filed and nothing can be.",
        "",
        f"candidates:        {len(report.candidates)}",
        f"a live run files:  {len(report.would_file)}   (cap {MAX_FILINGS_PER_RUN} per run)",
        f"held by the cap:   {len(report.held_by_rate_limit)}",
        f"already in queue:  {len(report.duplicates)}"
        + ("" if report.queue_known else "   (NOT CHECKED — no substrate reachable)"),
        f"suppressed:        {len(report.suppressed)}",
        f"skipped as stale:  {len(report.stale)}",
        f"would be refused:  {len(report.refused)}",
        f"well-formed:       {len(report.fileable)} of {len(report.candidates)}",
        f"duplicate keys:    {len(report.duplicate_keys)}   (within this run)",
        f"never produced — no permitted path in scope: {len(report.scope_forbidden)}",
        f"never produced — scope underivable:          {len(report.underivable)}",
        "",
    ]

    if not report.may_file_live:
        lines.append(
            "A LIVE RUN WOULD REFUSE THIS PASS: the queue could not be consulted, "
            "so dedup did not run and the list below is not trustworthy."
        )
        lines.append("")

    if not report.spec_record_known:
        lines.append(
            "SPEC RECORD: NOT CHECKED — no substrate reachable, so tasks/ could "
            "not be compared with dev.task beads."
        )
        lines.append("")
    else:
        # done-spec-no-bead is not drift: a done/ spec with no bead at all
        # predates dev.task beads as the record, and is not a reopening.
        # It is reported below, separately, and never counts toward the
        # drift section's findings or its empty/non-empty decision — folding
        # it back in would recreate exactly the noise this split exists to
        # remove.
        predates_beads = [
            issue
            for issue in report.spec_record_issues
            if issue.kind == "done-spec-no-bead"
        ]
        real_drift = [
            issue
            for issue in report.spec_record_issues
            if issue.kind != "done-spec-no-bead"
        ]
        if not real_drift:
            lines.append(
                "SPEC RECORD: checked tasks/ against dev.task beads; no drift found."
            )
        else:
            lines.append("SPEC RECORD DRIFT — tasks/ and dev.task beads disagree:")
            queued_done = [
                issue for issue in real_drift if issue.kind == "queued-spec-done"
            ]
            queued_superseded = [
                issue for issue in real_drift if issue.kind == "queued-spec-superseded"
            ]
            done_not_done = [
                issue for issue in real_drift if issue.kind == "done-spec-not-done"
            ]
            no_bead = [
                issue for issue in real_drift if issue.kind == "queued-spec-no-bead"
            ]
            merged_no_bead = [
                issue
                for issue in real_drift
                if issue.kind == "queued-spec-merged-no-bead"
            ]
            if queued_done:
                lines.append("- top-level specs whose bead is done:")
                for issue in queued_done:
                    bead = f" bead={issue.bead_id}" if issue.bead_id else ""
                    lines.append(f"    {issue.display_path}{bead} title={issue.title}")
            if queued_superseded:
                lines.append("- top-level specs whose bead is superseded:")
                for issue in queued_superseded:
                    bead = f" bead={issue.bead_id}" if issue.bead_id else ""
                    lines.append(f"    {issue.display_path}{bead} title={issue.title}")
            if done_not_done:
                lines.append("- done/ specs whose bead is not done:")
                for issue in done_not_done:
                    bead = f" bead={issue.bead_id}" if issue.bead_id else ""
                    lines.append(
                        f"    {issue.display_path}{bead} state={issue.bead_state} "
                        f"title={issue.title}"
                    )
            if no_bead:
                lines.append(
                    "- top-level specs with no exact-title bead "
                    f"after {SPEC_RECORD_UNFILED_GRACE.days} days:"
                )
                for issue in no_bead:
                    age = (
                        "age=unknown"
                        if issue.age_days is None
                        else f"age_days={issue.age_days}"
                    )
                    hold = (
                        f" hold_note={issue.hold_note_category}"
                        if issue.hold_note_category
                        else ""
                    )
                    lines.append(f"    {issue.display_path} {age} title={issue.title}{hold}")
            if merged_no_bead:
                lines.append(
                    "- top-level specs whose title matches a merged commit subject "
                    "and have no bead at all — already shipped, never filed, no "
                    "grace period applies:"
                )
                for issue in merged_no_bead:
                    hold = (
                        f" hold_note={issue.hold_note_category}"
                        if issue.hold_note_category
                        else ""
                    )
                    lines.append(f"    {issue.display_path} title={issue.title}{hold}")
        if predates_beads:
            lines.append(
                "SPEC RECORD (informational, not drift, no action needed): "
                f"{len(predates_beads)} done/ spec(s) with no matching bead — "
                "these predate dev.task beads as the record, not reopenings:"
            )
            for issue in predates_beads:
                lines.append(f"    {issue.display_path} title={issue.title}")
        lines.append("")

    if not report.ahead_of_gate_known:
        lines.append(
            "AHEAD-OF-GATE: NOT CHECKED — either no substrate was reachable, or "
            "git could not resolve main, so no bead's spec_identity could be "
            "verified against merged main."
        )
        lines.append("")
    elif not report.ahead_of_gate_issues:
        lines.append("AHEAD OF GATE: no dev.task names a tasks/ spec absent from main.")
        lines.append("")
    else:
        lines.append(
            "AHEAD OF GATE — a dev.task names a tasks/ spec present nowhere on "
            "merged main:"
        )
        live = [i for i in report.ahead_of_gate_issues if i.kind == "ahead-of-gate-live"]
        terminal = [
            i for i in report.ahead_of_gate_issues if i.kind == "ahead-of-gate-terminal"
        ]
        if live:
            lines.append("- live bead, spec wants routing to review:")
            for issue in live:
                lines.append(
                    f"    {issue.display_path} bead={issue.bead_id} "
                    f"state={issue.bead_state} title={issue.title}"
                )
        if terminal:
            lines.append(
                "- terminal bead, spec wants committing to done/ as the record "
                "before it is lost:"
            )
            for issue in terminal:
                lines.append(
                    f"    {issue.display_path} bead={issue.bead_id} "
                    f"state={issue.bead_state} title={issue.title}"
                )
        lines.append("")

    if report.would_file:
        lines.append("A LIVE RUN WOULD FILE THESE:")
        for candidate in report.would_file:
            lines.append(f"- [{candidate.finding.lane}] {candidate.finding.title}")
            lines.append(f"    key={candidate.finding.dedupe_key}")
        lines.append("")

    if report.held_by_rate_limit:
        lines.append(
            f"HELD BY THE RATE LIMIT — eligible, over the {MAX_FILINGS_PER_RUN}-per-run cap:"
        )
        for candidate in report.held_by_rate_limit:
            lines.append(f"- {candidate.finding.dedupe_key}")
        lines.append("")

    if report.wrong_lane:
        lines.append(f"NOT FILED — scanner filing is {FILING_LANE} lane only:")
        for candidate in report.wrong_lane:
            lines.append(f"- [{candidate.finding.lane}] {candidate.finding.title}")
            lines.append(f"    key={candidate.finding.dedupe_key}")
        lines.append("")

    if report.duplicates:
        lines.append("ALREADY IN THE QUEUE — not filed:")
        for candidate in report.duplicates:
            lines.append(f"- {candidate.finding.dedupe_key}")
            lines.append(f"    {candidate.duplicate_of}")
        lines.append("")

    if report.suppressed:
        lines.append("SUPPRESSED by a standing decision:")
        for candidate in report.suppressed:
            lines.append(f"- {candidate.finding.dedupe_key}")
            lines.append(f"    {candidate.suppressed_by.strip()[:200]}")
        lines.append("")

    if report.scope_forbidden:
        # Same idea as a standing-decision suppression — do not file this,
        # the gap is real and no factory task can close it — just reached
        # mechanically from scope alone rather than decided by a person. Kept
        # as its own header so the two are never read as the same kind of
        # decision: one is inspectable and revisitable (a line in
        # scanner-suppressions.yaml); the other follows from the paths a
        # requirement names and changes only if those paths do.
        lines.append(
            "SUPPRESSED — no path in scope the factory may touch (computed "
            "from scope, not a standing decision):"
        )
        for ungenerated in report.scope_forbidden:
            lines.append(f"- {ungenerated.finding.dedupe_key}")
            lines.append(f"    {ungenerated.reason}")
        lines.append("")

    if report.underivable:
        lines.append(
            "UNDERIVABLE — the scanner could not work out where the fix "
            "belongs, which is not the same claim as \"nothing to do\":"
        )
        for ungenerated in report.underivable:
            lines.append(f"- {ungenerated.finding.dedupe_key}")
            lines.append(f"    {ungenerated.reason}")
        lines.append("")

    clean = [c for c in report.doable if not c.narrowed]
    if clean:
        lines.append("WELL-FORMED AND FACTORY-DOABLE (before the controls above):")
        for candidate in clean:
            lines.append(f"- [{candidate.finding.lane}] {candidate.finding.title}")
            lines.append(
                f"    refs={', '.join(candidate.finding.requirement_refs)} "
                f"scope={', '.join(candidate.allowed_paths)}"
            )
        lines.append("")

    if report.narrowed:
        lines.append(
            "WOULD FILE, but SCOPE WAS NARROWED — check the dropped paths were not "
            "where the work is:"
        )
        for candidate in report.narrowed:
            lines.append(f"- {candidate.finding.title}")
            lines.append(f"    kept:    {', '.join(candidate.allowed_paths)}")
            lines.append(f"    dropped: {', '.join(candidate.blocked_paths)}")
        lines.append("")

    if report.stale:
        lines.append(
            "SKIPPED — the verdict may no longer describe the code. Re-measure "
            "before proposing these:"
        )
        for candidate in report.stale:
            lines.append(f"- {candidate.finding.title}")
            lines.append(f"    {candidate.finding.stale_reason}")
        lines.append("")

    impossible = [c for c in report.fileable if not c.doable and not c.finding.stale_reason]
    if impossible:
        lines.append(
            "WOULD FILE, but the factory CANNOT do them — well-formed and impossible:"
        )
        for candidate in impossible:
            reason = (
                f"every path the requirement names is forbidden "
                f"({', '.join(candidate.blocked_paths)})"
                if candidate.blocked_paths
                else "the requirement names no repo path to work in"
            )
            lines.append(f"- {candidate.finding.title}")
            lines.append(f"    {reason}")
        lines.append("")

    if report.refused:
        lines.append("WOULD BE REFUSED AT FILING:")
        for candidate in report.refused:
            lines.append(f"- {candidate.finding.title}")
            lines.append(f"    {candidate.refusal.splitlines()[0]}")
        lines.append("")

    lines.append(
        "This report is the whole point of 12.1. Before any of it files, 12.3 owes "
        "dedup against open tasks, a rate limit, and a suppression the scanner "
        "honours — the queue drains on a schedule, so an unbounded scanner starves "
        "every other lane."
    )
    return "\n".join(lines)


def scanner_provenance(candidate: Candidate) -> dict[str, Any]:
    """Complete BeadProvenance record for scanner-filed work."""
    return {
        "worker": SCANNER_CREATED_BY,
        "model": "none",
        "prompt_ref": f"scanner/{candidate.finding.scanner}/{candidate.finding.dedupe_key}",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }


def filing_spec(candidate: Candidate) -> dict[str, Any]:
    """Spec shape used for the live write.

    The scan report assesses the richer scanner spec so it can say exactly what
    would be proposed. The live write deliberately lets ``file_task`` provide
    budget, verification, and forbidden-path defaults, matching hand-filed work.
    """
    spec = dict(candidate.spec)
    spec.pop("budget", None)
    spec.pop("verification", None)
    spec["scope"] = {"paths": list(candidate.allowed_paths)}
    return spec


def content_for_filing(candidate: Candidate) -> dict[str, Any]:
    spec = filing_spec(candidate)
    content = build_content(spec)
    content[SCANNER_KEY_FIELD] = candidate.finding.dedupe_key
    # build_content never touches release fields — that resolution needs a
    # live substrate and is enforced at the file_task.py filing boundary
    # (_check_release_traceability), which this direct POST bypasses. The
    # scanner's own release traceability is satisfied upstream instead: every
    # generated spec already carries SCANNER_RELEASE_WAIVER (finding_to_spec),
    # so it is copied straight into content here, the same way SCANNER_KEY_
    # FIELD is set above.
    if spec.get("release_ref_waived"):
        content["release_ref_waived"] = spec["release_ref_waived"]
    return content


def post_task(store: Any, candidate: Candidate) -> dict[str, Any]:
    """Create one dev.task bead through the same endpoint file_task uses."""
    return store._request(
        "POST",
        "/beads",
        json={
            "namespace": "dev",
            "type": "task",
            "state": "pending",
            "trust_tier": "user",
            "created_by": SCANNER_CREATED_BY,
            "provenance": scanner_provenance(candidate),
            "content": content_for_filing(candidate),
        },
    )


def file_report(report: Report, store: Any) -> tuple[dict[str, Any], ...]:
    """File the report's eligible candidates. ``dry_run`` remains read-only."""
    if not report.may_file_live:
        raise SystemExit(
            "refusing to file: the queue could not be consulted, so dedup did not run"
        )
    return tuple(post_task(store, candidate) for candidate in report.would_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report what the lane scanners would file. Files nothing."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("scan", "file", "reconcile"),
        default="scan",
        help=(
            "scan reports only; file performs the distinct live filing "
            "invocation; reconcile moves tasks/ spec files to agree with "
            "bead state, and touches no bead"
        ),
    )
    parser.add_argument(
        "--scanner",
        action="append",
        choices=sorted(SCANNERS),
        help="run only the named scanner; repeatable. Default: all.",
    )
    parser.add_argument(
        "--no-queue",
        action="store_true",
        help=(
            "do not consult the substrate for what is already open; the report "
            "then says duplicates were not checked rather than implying none"
        ),
    )
    args = parser.parse_args(argv)

    selected = (
        {name: SCANNERS[name] for name in args.scanner} if args.scanner else None
    )
    store = None
    if not args.no_queue:
        try:
            from substrate import default_store

            store = default_store()
            store.list_tasks()
        except Exception as exc:  # noqa: BLE001 - the dry run must survive this
            print(
                f"note: substrate unreachable ({type(exc).__name__}: {exc}); "
                "duplicates were NOT checked.\n",
                file=sys.stderr,
            )
            store = None
    report = dry_run(selected, store=store)
    if args.command == "file":
        filed = file_report(report, store)
        for bead in filed:
            print(f"filed dev.task {bead['id']}")
        if not filed:
            print("filed dev.task: 0")
        return 0

    if args.command == "reconcile":
        if not report.spec_record_known:
            print(
                "refusing to reconcile: the queue could not be consulted, so "
                "bead state is unknown and no file was moved",
                file=sys.stderr,
            )
            return 1
        moved = reconcile_spec_record(report.spec_record_issues)
        print(reconcile_report(moved))
        return 0

    print(render_report(report))
    # Exit code reflects whether the scan RAN, never what it found. A scanner
    # that fails the build because the platform has gaps would be turned off
    # within a week.
    return 0


if __name__ == "__main__":
    sys.exit(main())
