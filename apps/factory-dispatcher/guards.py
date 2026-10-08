#!/usr/bin/env python3
"""Pure decision logic for the factory dispatcher.

Everything here is side-effect free so the safety-critical rules — what a worker
was allowed to touch, whether a task is waiting on a human — are testable
without a repo, a network, or an agent.

The three rules that matter, and why:

1.  **Scope is verified after the run, not enforced during it.** The
    dispatcher's own sandbox-exec containment (``containment.py``) confines
    writes to the clone, not to ``scope.paths`` within it. So the dispatcher
    inspects the diff and refuses to open a PR when the worker strayed. An
    out-of-scope change is a failed run, not a reviewable one.

2.  **Allow-matching fails closed.** ``scope.paths`` entries are path prefixes.
    An entry the matcher does not understand matches *nothing* rather than
    guessing, so a malformed scope blocks the run instead of widening it.
    ``forbidden_paths`` matching deliberately over-matches for the same reason
    in the other direction — over-denial is the safe failure.

3.  **A task with an unanswered blocking question is not runnable — and an
    answer only counts once it says so.** The worker already stopped and
    asked; re-dispatching it would produce exactly the guess the question
    exists to prevent (see docs/plans/dev-collaboration-primitives.md §4).
    An ``answer`` note existing is not the same as an answer that releases
    the work: only ``releases_work: true`` closes the question. Nothing about
    the answer's free-text body is classified to decide this.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Iterable

import retry_policy


def slugify(text: str, limit: int = 48) -> str:
    """Reduce a task title to a git-branch-safe fragment."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "task"


def porcelain_paths(status: str) -> tuple[str, ...]:
    """Extract the path from each `git status --porcelain` line.

    Handles the two-char status prefix and `old -> new` rename entries, keeping
    the destination. Quoted paths (non-ASCII) are returned as git printed them —
    good enough for set comparison, since both sides come from the same git.
    """
    paths: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        entry = line[3:] if len(line) > 3 and line[2] == " " else line.strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip().strip('"'))
    return tuple(paths)


def containment_breach(
    worker_changed: Iterable[str], before_status: str, after_status: str
) -> tuple[str, ...]:
    """Paths the worker edited in its clone that ALSO changed in the real tree.

    This is the precise signature of the FINDINGS #11 escape: the worker was told
    to edit file X, followed ``.git`` home, and edited the *real* X. If the real
    copy of every file the worker touched is unchanged, containment held —
    whatever else happened in the tree.

    Why not compare the whole tree, which is what this did first: that guard
    tripped three times in one day and was wrong all three times. Twice it caught
    the operator committing mid-run; once it caught a *different agent session*
    implementing an unrelated story in the same checkout. Zero true positives,
    two discarded runs of correct work. A shared dev machine simply is not
    quiescent, and a guard that cannot attribute a change will keep blaming the
    worker for the room's noise.

    Narrowing to the intersection makes the check attributable. It still
    false-positives if another writer edits the *exact same file* in the same
    window, which is rare and — unlike the old behaviour — genuinely worth
    stopping for.
    """
    touched = set(worker_changed)
    if not touched:
        return ()
    before = set(porcelain_paths(before_status))
    after = set(porcelain_paths(after_status))
    return tuple(sorted((after - before) & touched))


def tree_delta(before_status: str, after_status: str) -> tuple[str, ...]:
    """Which `git status --porcelain` entries appeared or changed.

    Takes the porcelain status *only* — never a fingerprint with the HEAD sha
    prepended, or a moved HEAD gets reported as though it were a changed path.

    The containment guard originally compared two fingerprints and reported only
    that they differed. That is nearly useless in practice: on 2026-07-29 the
    very first real run tripped it because a human edited an unrelated file while
    the worker was running, and the message named no path, so "CONTAINMENT
    BREACH" read as the worker escaping when it had not. Naming the paths turns
    an alarming dead end into a two-second diagnosis.
    """
    before_entries = {line.strip() for line in before_status.splitlines() if line.strip()}
    after_entries = {line.strip() for line in after_status.splitlines() if line.strip()}
    return tuple(sorted(after_entries - before_entries))


# ---------------------------------------------------------------------------
# scope checking
# ---------------------------------------------------------------------------


def _normalize_prefix(pattern: str) -> str | None:
    """Reduce a scope entry to a comparable path prefix, or None if unsupported.

    Understood: ``apps/foo``, ``apps/foo/``, ``apps/foo/**``. Anything with a
    wildcard elsewhere returns None and therefore matches nothing — see rule 2.
    """
    pattern = pattern.strip()
    if not pattern:
        return None
    if pattern.endswith("/**"):
        pattern = pattern[:-3]
    elif pattern.endswith("/*"):
        pattern = pattern[:-2]
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return None
    return pattern.rstrip("/")


def path_allowed(path: str, allow_patterns: Iterable[str]) -> bool:
    """True when ``path`` sits under one of the allowed prefixes.

    The ``+ "/"`` is load-bearing: a bare ``startswith(prefix)`` would let a
    scope of ``apps/mcp-hub`` authorize writes to ``apps/mcp-hub-evil/``.
    """
    for pattern in allow_patterns:
        prefix = _normalize_prefix(pattern)
        if prefix is None:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def path_forbidden(path: str, forbid_patterns: Iterable[str]) -> bool:
    """True when ``path`` matches any forbidden pattern.

    Uses prefix matching *and* fnmatch. fnmatch's ``*`` spans ``/``, which
    over-matches compared to real glob semantics; that is kept on purpose
    because a false positive here blocks a PR, while a false negative would let
    a worker edit something it must not.
    """
    for pattern in forbid_patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        prefix = _normalize_prefix(pattern)
        if prefix is not None and (path == prefix or path.startswith(prefix + "/")):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


@dataclass(frozen=True)
class ScopeVerdict:
    out_of_scope: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    unsupported_patterns: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.out_of_scope and not self.forbidden

    def describe(self) -> str:
        parts: list[str] = []
        if self.forbidden:
            parts.append(f"touched forbidden paths: {', '.join(self.forbidden)}")
        if self.out_of_scope:
            parts.append(f"wrote outside declared scope: {', '.join(self.out_of_scope)}")
        if self.unsupported_patterns:
            parts.append(
                "scope patterns not understood (treated as matching nothing): "
                + ", ".join(self.unsupported_patterns)
            )
        return "; ".join(parts) or "all changes within scope"


def check_scope(changed_paths: Iterable[str], scope: dict[str, Any]) -> ScopeVerdict:
    """Grade a worker's diff against the task's declared scope."""
    allow = list(scope.get("paths") or [])
    forbid = list(scope.get("forbidden_paths") or [])

    unsupported = tuple(p for p in allow if _normalize_prefix(p) is None)

    out_of_scope: list[str] = []
    forbidden: list[str] = []
    for path in changed_paths:
        if path_forbidden(path, forbid):
            forbidden.append(path)
        elif not path_allowed(path, allow):
            out_of_scope.append(path)

    return ScopeVerdict(
        out_of_scope=tuple(out_of_scope),
        forbidden=tuple(forbidden),
        unsupported_patterns=unsupported,
    )


# ---------------------------------------------------------------------------
# premise-path check
# ---------------------------------------------------------------------------


def _path_present(path: str, present_paths: frozenset[str]) -> bool:
    """True when ``path`` is on the ref, either as a file or as a directory
    containing at least one present file.

    The directory case matters for ``scope.paths`` entries: a scope of
    ``apps/mcp-hub/`` should read as present against a flat file listing
    that never names the directory itself, only files under it.
    """
    if path in present_paths:
        return True
    prefix = path.rstrip("/") + "/"
    return any(entry.startswith(prefix) for entry in present_paths)


def absent_premise_paths(
    content: dict[str, Any], present_paths: Iterable[str]
) -> tuple[str, ...]:
    """Spec-named paths that do not exist on the base ref, first-seen order.

    Reads exactly two structured fields: ``context_refs`` and
    ``scope["paths"]``. Both are filer-declared, single-purpose fields — the
    "read these first" list and the declared write scope — populated by
    ``file_task.py`` from structured spec fields, never by parsing free text.
    ``acceptance`` is deliberately NOT read: it is free-text prose, and a
    2026-09-16 sweep of the live queue found that scanning acceptance-criterion
    text for paths flagged 4 illustrative fixture paths
    (``apps/empty/main.py``, ``apps/empty/src/thing.test.ts``,
    ``apps/empty/tests/test_thing.py``, ``scripts/tests/``) that a human filer
    wrote to describe a scenario, not a path the task depends on existing.
    Restricting to ``context_refs``/``scope.paths`` produced zero false
    positives against that sweep. The cost: a premise artifact named only
    inside acceptance-criterion prose, and nowhere in ``context_refs`` or
    ``scope.paths``, is invisible to this check.

    ``present_paths`` is taken as an explicit argument — a flat listing of
    paths on the base ref (e.g. ``git ls-tree -r --name-only``) — so this
    performs no I/O of its own: no git, no network, no live store. A
    directory-shaped entry (a ``scope.paths`` prefix) counts as present if any
    entry in ``present_paths`` is nested under it.

    Advisory only: this returns data and raises nothing. It does not affect
    ``is_runnable`` or any dispatch decision.
    """
    present = frozenset(present_paths)

    named: list[str] = []
    for ref in content.get("context_refs") or []:
        ref = str(ref).strip()
        if ref:
            named.append(ref)

    scope = content.get("scope") or {}
    for raw in scope.get("paths") or []:
        raw = str(raw).strip()
        if not raw:
            continue
        normalized = _normalize_prefix(raw)
        named.append(normalized if normalized is not None else raw)

    absent: list[str] = []
    seen: set[str] = set()
    for path in named:
        if path in seen:
            continue
        seen.add(path)
        if not _path_present(path, present):
            absent.append(path)
    return tuple(absent)


#: Body prefix for the advisory note isolate_activity writes when
#: absent_premise_paths finds something. Its own "status" kind and a distinct
#: prefix, the same shape CLAIM_NOTE_PREFIX/FAILURE_NOTE_PREFIX already use, so
#: no schema change and no new note kind for a reader to learn.
ABSENT_PREMISE_NOTE_PREFIX = "Premise paths absent from the base ref: "


def render_absent_premise_note(absent: Iterable[str]) -> str:
    """Body for the advisory note surfacing an ``absent_premise_paths`` finding.

    ``absent_premise_paths`` itself raises nothing and gates nothing (see its
    own docstring); this renders what it found so a person reading the bead —
    or a later dispatch attempt re-reading its notes — sees that dispatch
    already knew a named path was missing from the base ref, without the run
    having been refused over it.
    """
    paths = ", ".join(absent)
    return (
        f"{ABSENT_PREMISE_NOTE_PREFIX}{paths}. Advisory only: dispatch "
        "continued and the worker was not stopped."
    )


# ---------------------------------------------------------------------------
# collaboration gating (dev.note) — mirrors src/lib/dev-board.ts
# ---------------------------------------------------------------------------


def _note_content(note: dict[str, Any]) -> dict[str, Any]:
    return note.get("content") or {}


#: The field an ``answer`` note must set to ``True`` for its ``answers_ref``
#: question to close. Deliberately boolean and deliberately opt-in: an answer
#: existing is not consent, only this field is. Nothing about the answer's
#: free-text ``body`` is read to make this decision, on purpose — classifying
#: prose (by keyword, sentiment, or any other heuristic) would reintroduce the
#: exact failure this field exists to close under a subtler trigger. See the
#: incident on 2026-08-29: two answers reading "wait for rest of work to
#: merge/clear" and "hold until things clear" both closed their questions
#: anyway, because the old code only checked that an answer existed.
RELEASES_WORK_FIELD = "releases_work"


def _releases_work(note: dict[str, Any]) -> bool:
    return _note_content(note).get(RELEASES_WORK_FIELD) is True


def open_questions(notes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Question notes with no answer that explicitly declares release.

    An answer referencing a question via ``answers_ref`` does not by itself
    close it — only ``releases_work: true`` does. An answer missing the
    field, or carrying any other value, leaves the question open: silence is
    read as "not released," never as consent, matching this module's existing
    fail-closed posture (see ``_normalize_prefix``).
    """
    notes = list(notes)
    released = {
        _note_content(n).get("answers_ref")
        for n in notes
        if _note_content(n).get("kind") == "answer" and _releases_work(n)
    }
    released.discard(None)
    return [
        n
        for n in notes
        if _note_content(n).get("kind") == "question" and n.get("id") not in released
    ]


def orphaned_answers(notes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Answer notes whose ``answers_ref`` points at no note in this thread."""
    notes = list(notes)
    note_ids = {n.get("id") for n in notes}
    return [
        n
        for n in notes
        if _note_content(n).get("kind") == "answer"
        and _note_content(n).get("answers_ref") is not None
        and _note_content(n).get("answers_ref") not in note_ids
    ]


def blocking_questions(notes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open questions the worker is actually halted on."""
    return [n for n in open_questions(notes) if _note_content(n).get("blocking") is True]


#: The body prefix both claim paths write: the dispatcher's own
#: activities.dispatch_steps.claim_activity ("Claimed by <worker>...") and an
#: operator's ``--start`` ("Claimed for ATTENDED work by <operator>..."). A
#: ``doing`` bead with no note starting this way carries no record of who put
#: it there or why — the exact shape claim_activity's pending->doing
#: transition leaves behind when the note write after it fails and is not
#: compensated. See :func:`has_claim_note`.
CLAIM_NOTE_PREFIX = "Claimed "


def has_claim_note(notes: Iterable[dict[str, Any]]) -> bool:
    """Whether any status note records this bead having been claimed into doing."""
    for note in notes:
        content = _note_content(note)
        if content.get("kind") != "status":
            continue
        if (content.get("body") or "").strip().startswith(CLAIM_NOTE_PREFIX):
            return True
    return False


#: The body prefix fail_task writes. Capacity backpressure writes the SAME note
#: kind ("status") with a different prefix, and must never appear here: FA-S27
#: returns a capacity-blocked task to pending WITHOUT spending an attempt, so
#: telling a worker it "failed" for running out of subscription is a lie that
#: would push it to change something to avoid a failure it did not cause.
FAILURE_NOTE_PREFIX = "Run failed:"

#: Bounds. A single reason can carry a 400-char worker output tail
#: (dispatch.WORKER_OUTPUT_TAIL_CHARS), and beads have reached seven attempts,
#: so an unbounded history is a prompt that grows without limit. #319 is the
#: precedent: unbounded worker output exceeded Temporal's transport limit and
#: one task wedged the whole queue. This edge must not reintroduce that.
MAX_FAILURE_REASONS_IN_PROMPT = 3
FAILURE_REASON_CHARS = 600


@dataclass(frozen=True)
class PriorFailure:
    ordinal: int
    reason: str


#: A status note carrying this field is a recorded requeue: an operator judged
#: the attempts before it not to have been spent on this task's work, and the
#: retry budget restarts from that point. dev.note's ``kind`` is a closed
#: Literal (schemas.py:254) but its content allows extra fields, so this needs
#: no schema change.
#:
#: This field alone is not a requeue. A ``failed`` bead also needs the
#: ``failed -> pending`` compare-and-set transition; a note without it resets a
#: budget on a bead that stays out of the pending pool, which nothing refuses
#: or warns about and a drain cannot explain. ``dispatch.py --requeue
#: <bead-id> --reason '...'`` is the one command that does both together — it
#: performs the transition when the bead is failed and always writes this
#: note. Do not write this field by hand outside that command.
REQUEUE_MARKER_FIELD = "resets_attempts"

#: The 2026-08-07 requeue predates the marker and is recorded in prose on 12
#: beads. It is honoured so those beads are not stranded by a convention
#: introduced after them. New requeues carry the field; this stays only for the
#: historical records, which are a fixed and closed set.
LEGACY_REQUEUE_PREFIX = "Returned to pending by the SRE"


def requeue_barrier_at(notes: Iterable[dict[str, Any]]) -> str | None:
    """Timestamp of the most recent recorded requeue, or None.

    Failures before a recorded requeue are not the task's. On 2026-08-07 twelve
    beads burned their whole budget against a stale local ``main``: the clone
    carried a Change-kind guard from before #313, so
    ``check-repo-invariants.py`` exited 1 on every run regardless of what the
    worker changed. Returning them to pending with the counter reset was
    correct, and it was recorded on every bead.
    """
    barrier: str | None = None
    for note in notes:
        content = _note_content(note)
        if content.get("kind") != "status":
            continue
        body = (content.get("body") or "").strip()
        recorded = bool(content.get(REQUEUE_MARKER_FIELD)) or body.startswith(
            LEGACY_REQUEUE_PREFIX
        )
        if not recorded:
            continue
        created_at = note.get("created_at") or ""
        if barrier is None or created_at > barrier:
            barrier = created_at
    return barrier


def prior_failures(notes: Iterable[dict[str, Any]]) -> list[PriorFailure]:
    """Recorded reasons earlier attempts failed, oldest first.

    fail_task writes these and nothing has ever read them back, so before this
    existed every retry began as attempt #1. cd5e27ce failed six times against
    a spec that was unsatisfiable by construction, each attempt rediscovering
    the same impossibility from a standing start.

    Ordinals count ALL recorded failures, not the ones that survive the cap, so
    a worker shown attempts 5-7 is not told it is on attempt 1.

    Failures before a recorded requeue are excluded. They were judged not to be
    this task's, so counting them against its budget overrides that judgement,
    and showing them to a retrying worker describes an environment that no
    longer exists.
    """
    notes = list(notes)
    barrier = requeue_barrier_at(notes)
    ordered = sorted(notes, key=lambda n: n.get("created_at") or "")
    failures: list[PriorFailure] = []
    for note in ordered:
        if barrier is not None and (note.get("created_at") or "") < barrier:
            continue
        content = _note_content(note)
        if content.get("kind") != "status":
            continue
        body = (content.get("body") or "").strip()
        if not body.startswith(FAILURE_NOTE_PREFIX):
            continue
        reason = body[len(FAILURE_NOTE_PREFIX) :].strip()
        if not reason:
            continue
        failures.append(PriorFailure(ordinal=len(failures) + 1, reason=reason))
    return failures


def _pre_barrier_failures(notes: list[dict[str, Any]]) -> list[PriorFailure]:
    """Recorded failures from before the current requeue barrier, if any.

    ``prior_failures`` deliberately excludes these once a requeue draws a
    barrier -- they were judged not to be this task's, see its docstring. But
    that reset also empties the failure history ``build_prompt`` renders, so a
    requeue whose note carries no parseable ``Reason:`` (a legacy requeue, or
    a bare ``resets_attempts`` marker) leaves a resumed worker with *less*
    than an unrequeued bead would have shown it, not merely a reset budget.
    This recovers that pre-barrier history so a fallback renderer can show it,
    clearly labelled as pre-barrier so it is never mistaken for the (reset)
    attempt count ``prior_failures``/``is_runnable`` use.

    Returns ``[]`` when there is no barrier at all -- a plain first attempt or
    unrequeued retry has nothing to recover.
    """
    barrier = requeue_barrier_at(notes)
    if barrier is None:
        return []
    ordered = sorted(notes, key=lambda n: n.get("created_at") or "")
    failures: list[PriorFailure] = []
    for note in ordered:
        if (note.get("created_at") or "") >= barrier:
            continue
        content = _note_content(note)
        if content.get("kind") != "status":
            continue
        body = (content.get("body") or "").strip()
        if not body.startswith(FAILURE_NOTE_PREFIX):
            continue
        reason = body[len(FAILURE_NOTE_PREFIX) :].strip()
        if not reason:
            continue
        failures.append(PriorFailure(ordinal=len(failures) + 1, reason=reason))
    return failures


def _render_failure_history(failures: list[PriorFailure]) -> list[str]:
    """Render prior failures for the prompt, most recent kept, oldest first."""
    if not failures:
        return []

    kept = failures[-MAX_FAILURE_REASONS_IN_PROMPT:]
    omitted = len(failures) - len(kept)

    lines = ["## Why previous attempts failed"]
    lines.append(
        "This task has been attempted before and the attempt was rejected. "
        "These are the recorded reasons, oldest first."
    )
    if omitted:
        lines.append(
            f"{omitted} earlier failure(s) are not shown; the most recent "
            f"{len(kept)} are."
        )
    lines.append("")
    for failure in kept:
        reason = failure.reason
        if len(reason) > FAILURE_REASON_CHARS:
            reason = reason[:FAILURE_REASON_CHARS] + " […truncated]"
        lines.append(f"- **Attempt {failure.ordinal}:** {reason}")
    lines.append("")
    lines.append(
        "Read these before starting. If your plan would fail the same way "
        "again, or if the reasons show the task cannot be completed inside "
        "its declared scope, STOP and say so instead of attempting it again. "
        "Repeating a rejected approach spends the retry budget without "
        "producing anything."
    )
    lines.append("")
    return lines


def _render_pre_barrier_summary(failures: list[PriorFailure]) -> str:
    """Render pre-barrier failures as a labelled fallback, or "" when there are none.

    Only ever called when nothing else has already carried this history
    forward (see ``render_requeue_fallback_section``): a requeue's retry
    budget reset is correct and untouched by this -- these ordinals are
    informational, never the attempt count ``is_runnable`` checks.
    """
    if not failures:
        return ""

    kept = failures[-MAX_FAILURE_REASONS_IN_PROMPT:]
    omitted = len(failures) - len(kept)

    lines = [
        "## Why earlier attempts failed, before this bead was requeued",
        "",
        (
            "This bead was requeued, which restarted its retry budget. That "
            "reset is correct and stays in place -- but a diagnosis from "
            "before the requeue is not the same as no diagnosis. These are "
            "the pre-requeue reasons, oldest first."
        ),
    ]
    if omitted:
        lines.append(
            f"{omitted} earlier failure(s) are not shown; the most recent "
            f"{len(kept)} are."
        )
    lines.append("")
    for failure in kept:
        reason = failure.reason
        if len(reason) > FAILURE_REASON_CHARS:
            reason = reason[:FAILURE_REASON_CHARS] + " […truncated]"
        lines.append(f"- **Pre-requeue attempt {failure.ordinal}:** {reason}")
    lines.append("")
    lines.append(
        "Read these before starting. If your plan would fail the same way "
        "again, STOP and say so instead of attempting it again."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class AnsweredPair:
    question: str
    answer: str


def answered_pairs(notes: Iterable[dict[str, Any]]) -> list[AnsweredPair]:
    """Resolved Q&A, oldest first — the context that unblocks a resumed worker.

    This is the payoff of the question round-trip: the human's decision travels
    back into the prompt attached to the question it settled, rather than being
    lost in a chat log.
    """
    notes = sorted(list(notes), key=lambda n: n.get("created_at") or "")
    by_id = {n.get("id"): n for n in notes}
    pairs: list[AnsweredPair] = []
    for note in notes:
        content = _note_content(note)
        if content.get("kind") != "answer":
            continue
        question = by_id.get(content.get("answers_ref"))
        if question is None:
            continue
        pairs.append(
            AnsweredPair(
                question=(_note_content(question).get("body") or "").strip(),
                answer=(content.get("body") or "").strip(),
            )
        )
    return pairs


@dataclass(frozen=True)
class Runnable:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class ReleaseState:
    """The release a ``dev.task`` delivers, resolved from its ``delivers`` edge.

    ``ref`` is the human-readable release ref (``R26.01``); ``state`` is the
    ``arch.release`` state machine value (``planned``, ``in_flight``,
    ``closing``, ``released``, ``abandoned`` — routes.py:379).
    """

    ref: str
    state: str


#: Release states whose work may be claimed. Only ``in_flight`` — a release
#: still ``planned`` has not started, and a bead behind it jumping the queue
#: is exactly the 2026-08-30 finding ("the backlog is filed in reverse", D3):
#: R26.06/O-1 delivered sprint-42 work in sprint 35 because nothing checked
#: this. ``closing``/``released``/``abandoned`` are also not in_flight, so
#: they refuse too, which is the conservative direction to fail in.
RUNNABLE_RELEASE_STATES = frozenset({"in_flight"})


def release_block_reason(
    task: dict[str, Any],
    release_by_task_id: dict[str, ReleaseState] | None,
) -> str:
    """Why the release this task delivers keeps it from running, if it does.

    ``release_by_task_id`` is keyed only by tasks with a *resolved* ``delivers``
    edge. A task absent from it — no edge at all, e.g. a bead filed with
    ``release_ref_waived`` and no release to point at — is not this gate's
    concern: file_task.py's REL-1 already refuses filing that names neither a
    release_ref nor a waiver, so absence here reads as "nothing to check," not
    "check failed." Passing ``None`` for ``release_by_task_id`` itself means
    the caller did not evaluate this gate at all — existing behaviour,
    unchanged, which is what keeps every caller that predates this filter
    byte-identical.
    """
    if not release_by_task_id:
        return ""
    task_id = str(task.get("id") or "").strip()
    release = release_by_task_id.get(task_id)
    if release is None or release.state in RUNNABLE_RELEASE_STATES:
        return ""
    return f"delivers {release.ref} ({release.state}), not in_flight"


SUCCESSFUL_TERMINAL_STATES = frozenset({"done"})
PREDECESSOR_BEAD_IDS_FIELD = "predecessor_bead_ids"


def predecessor_bead_ids(task: dict[str, Any]) -> tuple[str, ...]:
    """Structured ordering constraints declared on this task.

    Historical beads do not carry the field; they read as having no declared
    predecessors. A stray string is normalized the same way ``source_bead_ids``
    is, so one hand-filed malformed record does not become an iterable of
    characters.
    """
    raw = (task.get("content") or {}).get(PREDECESSOR_BEAD_IDS_FIELD) or []
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(bead_id).strip() for bead_id in raw if str(bead_id).strip())


def _task_landed(task: dict[str, Any]) -> bool:
    return str(task.get("state") or "").strip() in SUCCESSFUL_TERMINAL_STATES


def _predecessor_cycle(
    task_id: str,
    tasks_by_id: dict[str, dict[str, Any]],
    path: tuple[str, ...],
) -> tuple[str, ...]:
    if task_id in path:
        return path[path.index(task_id) :] + (task_id,)

    task = tasks_by_id.get(task_id)
    if task is None or _task_landed(task):
        return ()

    next_path = path + (task_id,)
    for predecessor_id in predecessor_bead_ids(task):
        cycle = _predecessor_cycle(predecessor_id, tasks_by_id, next_path)
        if cycle:
            return cycle
    return ()


def ordering_block_reason(
    task: dict[str, Any],
    tasks: Iterable[dict[str, Any]] | None,
) -> str:
    """Why declared predecessors prevent this task from running, if they do."""
    predecessors = predecessor_bead_ids(task)
    if not predecessors or tasks is None:
        return ""

    task_id = str(task.get("id") or "").strip()
    tasks_by_id = {
        str(candidate.get("id") or "").strip(): candidate
        for candidate in tasks
        if str(candidate.get("id") or "").strip()
    }
    if task_id:
        tasks_by_id[task_id] = task

    for predecessor_id in predecessors:
        predecessor = tasks_by_id.get(predecessor_id)
        if predecessor is None:
            return f"predecessor bead {predecessor_id} not found"
        if _task_landed(predecessor):
            continue

        cycle = _predecessor_cycle(
            predecessor_id,
            tasks_by_id,
            (task_id,) if task_id else (),
        )
        if cycle:
            return "predecessor cycle detected: " + " -> ".join(cycle)

        # A predecessor that is itself superseded is not "waiting" — it is
        # structurally incapable of ever reaching done (2026-08-20: S24-B1 and
        # FA-S33 named held originals as predecessors and every drain read
        # "waiting for predecessor ... (state=pending)" as though the queue
        # were merely slow). Say so loudly and name the live replacement.
        predecessor_superseded_by = superseding_task_ids(
            predecessor, tasks_by_id.values()
        )
        if predecessor_superseded_by:
            return (
                f"predecessor {predecessor_id} is held ("
                + superseded_reason(predecessor_superseded_by)
                + ") and can never land"
            )

        state = str(predecessor.get("state") or "(missing state)").strip()
        return f"waiting for predecessor bead {predecessor_id} to land (state={state})"

    return ""


def superseding_task_ids(
    task: dict[str, Any], tasks: Iterable[dict[str, Any]]
) -> tuple[str, ...]:
    """Replacement dev.task ids that name ``task`` as their source.

    ``source_bead_ids`` points from the re-filed task back to the bead it came
    from. A dispatcher holding the old bead therefore has to reverse-index the
    board to discover that it was superseded. Historical beads without the field
    read as an empty list and keep their previous behaviour.
    """
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return ()

    replacements: list[str] = []
    for candidate in tasks:
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id or candidate_id == task_id:
            continue
        source_bead_ids = (candidate.get("content") or {}).get("source_bead_ids") or []
        if isinstance(source_bead_ids, str):
            source_bead_ids = [source_bead_ids]
        sources = {str(source).strip() for source in source_bead_ids if str(source).strip()}
        if task_id in sources:
            replacements.append(candidate_id)
    return tuple(sorted(replacements))


def superseded_reason(replacement_ids: Iterable[str]) -> str:
    replacements = tuple(replacement_ids)
    if not replacements:
        return ""
    return "superseded by bead " + ", ".join(replacements)


def dependents_of(
    bead_id: str, tasks: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Tasks naming ``bead_id`` in their own ``predecessor_bead_ids``.

    Read side of the OPS-13 fix: supersession is a graph operation, so
    finding who points at a bead is as much a graph query as finding what
    replaced it (``superseding_task_ids``, above).
    """
    return tuple(
        candidate
        for candidate in tasks
        if str(candidate.get("id") or "").strip() != bead_id
        and bead_id in predecessor_bead_ids(candidate)
    )


def repoint_predecessor_ids(
    predecessor_ids: Iterable[str], superseded_id: str, replacement_id: str
) -> tuple[str, ...]:
    """``predecessor_ids`` with ``superseded_id`` replaced by ``replacement_id``.

    Order-preserving and deduplicating: a dependent that already names both
    the superseded bead and its replacement (e.g. filed against the
    replacement after the fact, while still also carrying the old id)
    collapses to naming the replacement once, not twice.
    """
    result: list[str] = []
    for predecessor_id in predecessor_ids:
        replaced = replacement_id if predecessor_id == superseded_id else predecessor_id
        if replaced not in result:
            result.append(replaced)
    return tuple(result)


def superseded_predecessors(
    task: dict[str, Any], tasks: Iterable[dict[str, Any]]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Every predecessor of ``task`` that is itself superseded, with its replacement(s).

    ``ordering_block_reason`` answers "why can't THIS task run right now" and
    stops at the first blocking predecessor. This answers a different
    question — "does this bead's graph contain a stranded edge at all" — for
    every predecessor, so a status report can flag each one rather than only
    the first is_runnable() would trip over. It exists so a supersession that
    reaches a dependent by any path other than --supersede's own repointing
    (a hand-edit, a partial repoint that failed halfway) is still visible.
    """
    tasks = list(tasks)
    tasks_by_id = {
        str(candidate.get("id") or "").strip(): candidate
        for candidate in tasks
        if str(candidate.get("id") or "").strip()
    }
    faults: list[tuple[str, tuple[str, ...]]] = []
    for predecessor_id in predecessor_bead_ids(task):
        predecessor = tasks_by_id.get(predecessor_id)
        if predecessor is None:
            continue
        replacements = superseding_task_ids(predecessor, tasks)
        if replacements:
            faults.append((predecessor_id, replacements))
    return tuple(faults)


def is_runnable(
    task: dict[str, Any],
    notes: Iterable[dict[str, Any]],
    tasks: Iterable[dict[str, Any]] | None = None,
    release_by_task_id: dict[str, ReleaseState] | None = None,
) -> Runnable:
    """Whether the dispatcher may claim this task right now."""
    content = task.get("content") or {}
    # Read once: notes is an Iterable and both checks below consume it.
    notes = list(notes)

    if tasks is not None:
        tasks = list(tasks)
        superseded_by = superseding_task_ids(task, tasks)
        if superseded_by:
            return Runnable(False, superseded_reason(superseded_by))

        ordering_reason = ordering_block_reason(task, tasks)
        if ordering_reason:
            return Runnable(False, ordering_reason)

    release_reason = release_block_reason(task, release_by_task_id)
    if release_reason:
        return Runnable(False, release_reason)

    # Failure notes are the local audit trail; Temporal owns the retry execution
    # policy. A recorded requeue creates the barrier used by prior_failures(), so
    # old failures do not strand a task after an operator explicitly restarts
    # its budget.
    attempts = len(prior_failures(notes))
    max_attempts = retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS
    if attempts >= max_attempts:
        return Runnable(
            False, f"attempts exhausted ({attempts}/{max_attempts}, per recorded failures)"
        )

    pending = blocking_questions(notes)
    if pending:
        question_id = pending[0].get("id") or "(missing id)"
        body = (_note_content(pending[0]).get("body") or "").strip()
        answers = [
            n
            for n in notes
            if _note_content(n).get("kind") == "answer"
            and _note_content(n).get("answers_ref") == pending[0].get("id")
        ]
        if answers:
            return Runnable(
                False,
                f"blocking question {question_id} has an answer that does not "
                f"declare release ({RELEASES_WORK_FIELD}=true required): {body[:120]}",
            )
        return Runnable(False, f"waiting on blocking question {question_id}: {body[:120]}")

    scope = content.get("scope") or {}
    if not (scope.get("paths") or []):
        return Runnable(False, "task declares no scope.paths")

    return Runnable(True)


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------


#: Mirrors dispatch.PRESERVED_ATTEMPTS_FIELD. guards.py cannot import dispatch
#: (dispatch imports guards), so this is a convention shared by string value
#: only, exactly like REQUEUE_MARKER_FIELD above -- entries are
#: ``{"pr": <ref>, "head_sha": <sha>}``, oldest first, and carry no timestamp.
_PRESERVED_ATTEMPTS_FIELD = "preserved_attempts"


def _preserved_pr_refs(content: dict[str, Any]) -> set[str]:
    preserved = content.get(_PRESERVED_ATTEMPTS_FIELD) or []
    return {str(entry.get("pr")) for entry in preserved if entry.get("pr")}


def _request_changes_review_findings(
    notes: list[dict[str, Any]], pr_refs: set[str]
) -> str | None:
    """Body of the most recent request-changes review bound to a preserved PR.

    Bound by the review note's own ``pr_url`` extra field when it carries one.
    When it does not, fall back to timing: the nearest preceding ``attachment``
    note whose ``url`` names a preserved PR binds every review note after it to
    that PR, exactly as a review filed on GitHub and then attached to the bead
    would read without a pr_url of its own.
    """
    ordered = sorted(notes, key=lambda n: n.get("created_at") or "")
    matches: list[tuple[str, dict[str, Any]]] = []
    last_attachment_pr: str | None = None
    for note in ordered:
        content = _note_content(note)
        kind = content.get("kind")
        if kind == "attachment":
            url = str(content.get("url") or "").strip()
            # A later attachment naming some other PR ends the binding window:
            # a review filed after it reads as that PR's, not the preserved
            # one's (the #805 gate, F3).
            last_attachment_pr = url if url in pr_refs else None
            continue
        if kind != "review" or content.get("verdict") != "request-changes":
            continue
        pr_url = str(content.get("pr_url") or "").strip()
        bound = pr_url if pr_url in pr_refs else (None if pr_url else last_attachment_pr)
        if bound:
            matches.append((note.get("created_at") or "", note))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    body = (_note_content(matches[-1][1]).get("body") or "").strip()
    return body or None


#: The prose ``requeue_task`` appends after the reason it records, when the
#: bead also carries preserved work -- stripped back off here so the extracted
#: Reason is exactly the operator's own words, not the bookkeeping sentence
#: bolted on after it.
_REQUEUE_PRESERVED_SUFFIX_MARKER = " Preserved work from"
_REQUEUE_REASON_MARKER = "Reason:"


def _barrier_requeue_reason(notes: list[dict[str, Any]]) -> str | None:
    """The Reason text of the resets_attempts note that set the current attempt barrier.

    Reuses ``requeue_barrier_at`` rather than re-deriving "the latest requeue"
    independently, so this can never fire on a note older than the barrier
    ``prior_failures`` already uses -- one barrier, read the same way
    everywhere. A legacy pre-marker requeue (prose prefix only, see
    ``LEGACY_REQUEUE_PREFIX``) carries no structured field and no parseable
    Reason, so it yields nothing here, same as before this function existed.
    """
    barrier = requeue_barrier_at(notes)
    if barrier is None:
        return None
    at_barrier = [
        note
        for note in notes
        if _note_content(note).get("kind") == "status"
        and bool(_note_content(note).get(REQUEUE_MARKER_FIELD))
        and (note.get("created_at") or "") == barrier
    ]
    if not at_barrier:
        return None
    body = (_note_content(at_barrier[-1]).get("body") or "").strip()
    idx = body.find(_REQUEUE_REASON_MARKER)
    if idx == -1:
        return None
    reason = body[idx + len(_REQUEUE_REASON_MARKER) :].strip()
    suffix_idx = reason.find(_REQUEUE_PRESERVED_SUFFIX_MARKER)
    if suffix_idx != -1:
        reason = reason[:suffix_idx].strip()
    return reason or None


@dataclass(frozen=True)
class ResumeContext:
    """Why a dispatch with preserved_attempts is a resume worth acting on, not just re-verifying."""

    pr: str
    head_sha: str
    findings: str | None
    requeue_reason: str | None


def resume_context(
    content: dict[str, Any], notes: Iterable[dict[str, Any]]
) -> ResumeContext | None:
    """Whether this dispatch resumes preserved work with something specific to act on.

    ``None`` when there is nothing to act on: no ``preserved_attempts``, no
    usable ``pr`` on any entry, or neither a bound request-changes review nor
    a reasoned requeue at the current attempt barrier. That covers both a
    first attempt and a resume after a plain environmental failure -- the two
    cases the acceptance criteria requires stay byte-identical.
    """
    notes = list(notes)
    preserved = list(content.get(_PRESERVED_ATTEMPTS_FIELD) or [])
    if not preserved:
        return None
    pr_refs = _preserved_pr_refs(content)
    if not pr_refs:
        return None

    findings = _request_changes_review_findings(notes, pr_refs)
    requeue_reason = _barrier_requeue_reason(notes)
    if findings is None and requeue_reason is None:
        return None

    baseline = preserved[-1]
    return ResumeContext(
        pr=str(baseline.get("pr") or ""),
        head_sha=str(baseline.get("head_sha") or ""),
        findings=findings,
        requeue_reason=requeue_reason,
    )


def render_resume_section(
    content: dict[str, Any], notes: Iterable[dict[str, Any]]
) -> str:
    """Render the resume-from-preserved-work section, or "" when this is not one.

    Placed by ``build_prompt`` right after the title/intent block and before
    ``## Acceptance criteria``, so a resumed worker reads what its task
    actually is before it reads criteria a completed-looking baseline would
    otherwise satisfy on sight (2026-09-13: FA-S49-1 and FA-S49-2 both
    re-audited an applied baseline against acceptance criteria alone, found it
    complete, and burned the attempt while a request-changes review sat
    unread on the bead).
    """
    context = resume_context(content, notes)
    if context is None:
        return ""

    head = context.head_sha[:8] if context.head_sha else "(unknown head)"
    lines = [
        "## Resuming from applied preserved work",
        "",
        (
            f"This attempt resumes from preserved work already applied as its "
            f"starting point: {context.pr or '(unknown PR)'} at {head}. Read its "
            f"discussion with `gh pr view {context.pr} --json comments` if you "
            "need more than what is quoted below."
        ),
        "",
        (
            "Re-verifying that baseline against the acceptance criteria is NOT "
            "the task, and reporting it as already satisfied without making a "
            "change is a refused outcome. The task is:"
        ),
        "",
    ]
    if context.findings is not None:
        lines.append("**The review's request-changes findings:**")
        lines.append("")
        lines.append(context.findings)
        lines.append("")
    if context.requeue_reason is not None:
        lines.append("**The reason this bead was requeued:**")
        lines.append("")
        lines.append(context.requeue_reason)
        lines.append("")
    lines.append(
        "The acceptance criteria below are the frame for that work, not a "
        "checklist to re-verify the baseline against."
    )
    return "\n".join(lines)


def render_requeue_fallback_section(
    content: dict[str, Any], notes: Iterable[dict[str, Any]]
) -> str:
    """A requeue's diagnosis, rendered when ``render_resume_section`` had nothing.

    ``resume_context``/``render_resume_section`` only construct when a
    preserved attempt carries a usable ``pr`` -- so a bead that dies before it
    ever opens one (the exact shape that gets requeued with a diagnosis to
    break a retry loop, see the 2026-09-17/18 measurement on bead 5bb23acc)
    never reaches that channel, and the operator's reason is silently dropped.
    This is a second, independent channel for exactly that gap.

    Call this ONLY when ``render_resume_section(content, notes)`` already
    returned "" -- a bead with a preserved PR and a bound requeue reason or
    review already renders correctly through that path, byte-identically to
    before this function existed, and must not be touched by this one.

    Preference order once here: a requeue's own ``Reason:`` text outranks a
    pre-barrier failure summary, since it is the operator's own diagnosis
    rather than a raw log of what a worker reported. Falls back to the
    pre-barrier summary only when no reason was recorded (a legacy requeue,
    or a bare ``resets_attempts`` marker) -- so a requeue never leaves the
    next worker with less than an unrequeued retry would have shown it (see
    ``_pre_barrier_failures``). Renders "" when neither applies: a first
    attempt, or a plain environmental re-dispatch with no requeue at all,
    must stay exactly as empty as before.
    """
    notes = list(notes)
    reason = _barrier_requeue_reason(notes)
    if reason is not None:
        return "\n".join(
            [
                "## Why this bead was requeued",
                "",
                (
                    "There is no prior pull request to resume from. An "
                    "operator requeued this bead with a diagnosis:"
                ),
                "",
                reason,
                "",
                (
                    "Treat this as the reason the previous approach was "
                    "rejected. If your plan would repeat it, STOP and say so "
                    "instead of attempting it again."
                ),
            ]
        )
    return _render_pre_barrier_summary(_pre_barrier_failures(notes))


def render_doctrine_section(principles: Iterable[tuple[str, str]]) -> str:
    """Render adopted/enforced principle citations for a worker prompt (F-DCE-3).

    Returns ``""`` when ``principles`` is empty, so a caller that appends this
    unconditionally produces byte-identical output for a lane with nothing to cite.
    Parsing and status filtering happen upstream (doctrine.py); this only renders
    whatever (id, statement) pairs it is given.
    """
    principles = list(principles)
    if not principles:
        return ""
    lines = [
        "## Doctrine",
        "",
        "These architecture principles apply to this lane's work. Treat them as "
        "binding context for the approach you choose, not suggestions.",
        "",
    ]
    for prin_id, statement in principles:
        lines.append(f"- **{prin_id}:** {statement}")
    lines.append("")
    return "\n".join(lines)


#: The lane whose beads carry a requirement/NFR contract worth naming in the
#: prompt itself. Gated on the lane, not merely on the fields being present, so
#: a code-health/drift/bug-triage bead that happens to carry requirement_refs
#: renders byte-identically to before this feature existed (sprint 24 spec).
FEATURE_LANE = "feature"


def render_requirement_contract(content: dict[str, Any]) -> str:
    """Requirement refs and NFRs as the contract a feature-lane change satisfies.

    Both already reach a PR body via ``dispatch.render_traceability``, but that
    renderer runs after the worker has already written the change — too late to
    shape it. A feature bead's requirement_refs and nfrs are what the change is
    *for*, so the worker prompt names them directly rather than leaving them to
    be discovered only in the PR body.
    """
    if content.get("lane") != FEATURE_LANE:
        return ""
    requirement_refs = [str(ref) for ref in (content.get("requirement_refs") or [])]
    nfrs = [nfr for nfr in (content.get("nfrs") or []) if isinstance(nfr, dict)]
    if not requirement_refs and not nfrs:
        return ""

    lines = ["## Requirement contract", ""]
    if requirement_refs:
        lines.append("This change SHALL satisfy:")
        lines.extend(f"- {ref}" for ref in requirement_refs)
        lines.append("")
    if nfrs:
        lines.append("Non-functional requirements this change SHALL meet:")
        for nfr in nfrs:
            statement = str(nfr.get("statement") or "").strip()
            if not statement:
                continue
            threshold = str(nfr.get("threshold") or "").strip()
            line = f"- {statement}"
            if threshold:
                line += f" (threshold: {threshold})"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


def build_prompt(
    task: dict[str, Any],
    notes: Iterable[dict[str, Any]],
    principles: Iterable[tuple[str, str]] = (),
) -> str:
    """Render a dev.task bead into a worker instruction.

    Deliberately restates the scope boundary and asks the worker to run checks
    when it can. The dispatcher also runs declared verification mechanically
    after the worker exits, because a worker report is not a gate.
    """
    content = task.get("content") or {}
    scope = content.get("scope") or {}
    verification = content.get("verification") or {}
    # Materialised once: notes is declared Iterable and is now read twice, so a
    # generator would leave the second reader with nothing.
    notes = list(notes)

    lines: list[str] = []
    lines.append(f"# Task: {content.get('title', '(untitled)')}")
    lines.append("")
    lines.append(str(content.get("intent") or "").strip())
    lines.append("")

    resume_section = render_resume_section(content, notes)
    if not resume_section:
        resume_section = render_requeue_fallback_section(content, notes)
    if resume_section:
        lines.append(resume_section)
        lines.append("")

    acceptance = content.get("acceptance") or []
    if acceptance:
        lines.append("## Acceptance criteria")
        lines.extend(f"- {item}" for item in acceptance)
        lines.append("")

    contract_section = render_requirement_contract(content)
    if contract_section:
        lines.append(contract_section)
        lines.append("")

    context_refs = content.get("context_refs") or []
    if context_refs:
        lines.append("## Read these first")
        lines.extend(f"- {ref}" for ref in context_refs)
        lines.append("")

    doctrine_section = render_doctrine_section(principles)
    if doctrine_section:
        lines.append(doctrine_section)
        lines.append("")

    pairs = answered_pairs(notes)
    if pairs:
        lines.append("## Decisions already made about this task")
        lines.append(
            "These questions were asked during earlier work and answered by a human. "
            "Treat the answers as binding."
        )
        lines.append("")
        for pair in pairs:
            lines.append(f"- **Q:** {pair.question}")
            lines.append(f"  **A:** {pair.answer}")
        lines.append("")

    lines.extend(_render_failure_history(prior_failures(notes)))

    lines.append("## Hard boundaries")
    allow = scope.get("paths") or []
    forbid = scope.get("forbidden_paths") or []
    lines.append(
        "You may only create or modify files under: " + ", ".join(allow)
        if allow
        else "No scope declared — stop and change nothing."
    )
    lines.append("You must not touch: " + ", ".join(forbid))
    lines.append(
        "A change outside those paths fails the whole run and is discarded, "
        "however good it is. If the task appears to require touching something "
        "out of scope, stop and say so instead of doing it."
    )
    lines.append("")

    risk = content.get("risk_class")
    if risk == "structural":
        lines.append(
            "This is a **structural** change: behaviour must be identical afterwards. "
            "Do not fix unrelated inconsistencies you notice along the way, do not "
            "add defaults to required configuration, and do not weaken or rewrite "
            "existing assertions to make the change pass. You may add new test "
            "coverage when the task asks for it."
        )
        lines.append("")

    commands = verification.get("commands") or []
    if commands:
        lines.append("## Verification")
        lines.append("Run what you can of:")
        lines.extend(f"- `{cmd}`" for cmd in commands)
        lines.append(
            "State plainly which checks you ran, which failed, and which could "
            "not run. The dispatcher will run the declared verification again "
            "inside its isolated clone before it opens a PR."
        )
        lines.append("")

    return "\n".join(lines).strip() + "\n"
