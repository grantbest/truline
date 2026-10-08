"""The selector's gates and the environmental-fault breaker.

R26.12 build item B2 (docs/plans/2026-09-23-design-r2612-steerable-and-legible.md,
"Mechanism" > "Queue order", the MODULE / SELECTABLE paragraph): this module is
the home the design requires for the one selector the arc builds, outside the
dispatch.py/guards.py pair. This bead is a MOVE, nothing else — the units below
are pick_task's old loop body and the module state it already needed, moved
verbatim out of dispatch.py. No behaviour changes, no names change, no output
changes; see tests/test_queue_order_move.py for the AST proof and
tests/test_queue_order_parity.py for the pick-equivalence proof.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal, Mapping

import guards
import retry_policy

#: Prefixes each dispatch-outcome-recording function writes its status note
#: with. Shared constants, not inline literals, because
#: trailing_environmental_fault_streak below has to recognise every one of
#: them to tell "consecutive" apart from "merely frequent" — a copy of these
#: strings living only in an f-string is a copy that can drift silently.
RUN_FAILED_NOTE_PREFIX = guards.FAILURE_NOTE_PREFIX
ENVIRONMENTAL_FAULT_NOTE_PREFIX = "Environmental fault:"
CAPACITY_BACKPRESSURE_NOTE_PREFIX = "Capacity backpressure:"
ALREADY_SATISFIED_CLAIM_NOTE_PREFIX = "Already-satisfied claim:"
#: R26.12 B3 (PC-EXE-007): a successful run is an outcome too -- without it,
#: a bead latched by a since-fixed fault stays latched after it has visibly
#: since worked. activities/dispatch_steps.py's success note builds its body
#: from this constant rather than a bare literal, so the writer and this
#: reader cannot drift apart the way _DISPATCH_OUTCOME_NOTE_PREFIXES already
#: guards against for the other four.
WORKER_FINISHED_NOTE_PREFIX = "Worker finished in"

_DISPATCH_OUTCOME_NOTE_PREFIXES = (
    RUN_FAILED_NOTE_PREFIX,
    ENVIRONMENTAL_FAULT_NOTE_PREFIX,
    CAPACITY_BACKPRESSURE_NOTE_PREFIX,
    ALREADY_SATISFIED_CLAIM_NOTE_PREFIX,
    WORKER_FINISHED_NOTE_PREFIX,
)

#: Extra ``dev.note`` content field an environmental-fault note carries: a
#: stable identity for the fault (see environmental_fault_signature), so a
#: later dispatch can tell "the same fault, again" from "a different one this
#: time" without re-parsing prose.
ENVIRONMENTAL_FAULT_SIGNATURE_FIELD = "environmental_fault_signature"


def _is_dispatch_outcome_note(note: dict[str, Any]) -> bool:
    """Whether ``note`` records the terminal outcome of one dispatch attempt.

    Excludes pure narration — a claim note, a PR-opened note, an operator
    action — so trailing_environmental_fault_streak walks only the notes that
    actually decide "consecutive."
    """
    content = note.get("content") or {}
    if content.get("kind") != "status":
        return False
    body = str(content.get("body") or "")
    return any(body.startswith(prefix) for prefix in _DISPATCH_OUTCOME_NOTE_PREFIXES)


def trailing_environmental_fault_streak(
    notes: Iterable[dict[str, Any]],
) -> tuple[str, int]:
    """The most recent environmental fault's signature and its consecutive run.

    Scans outcome notes newest-first (skipping narration notes entirely, per
    ``_is_dispatch_outcome_note``) and counts how many of the most recent ones
    in a row are environmental faults carrying the SAME signature. Returns
    ``("", 0)`` when there is no recorded outcome, or the most recent one was
    not an environmental fault: a real work failure, a capacity backpressure,
    a successful run (WORKER_FINISHED_NOTE_PREFIX), or a differently-signed
    fault landing in between two occurrences of this one means the fault did
    not recur on every consecutive dispatch, which is exactly the case that
    must not trip the bound.

    R26.12 B3 (PC-EXE-007): the scan also stops dead at
    ``guards.requeue_barrier_at`` -- an outcome note older than the most
    recent recorded requeue is never counted, the identical comparison
    ``guards.prior_failures`` already applies to the retry budget. A
    recorded requeue therefore ends a latched streak exactly as a later
    success does, without this function writing, deleting, or reading
    anything beyond the notes it was given.
    """
    notes = list(notes)
    barrier = guards.requeue_barrier_at(notes)
    ordered = sorted(notes, key=lambda n: n.get("created_at") or "", reverse=True)
    signature = ""
    count = 0
    for note in ordered:
        if barrier is not None and (note.get("created_at") or "") < barrier:
            break
        if not _is_dispatch_outcome_note(note):
            continue
        content = note.get("content") or {}
        note_signature = content.get(ENVIRONMENTAL_FAULT_SIGNATURE_FIELD)
        if count == 0:
            if not note_signature:
                return "", 0
            signature = note_signature
            count = 1
            continue
        if note_signature != signature:
            break
        count += 1
    return signature, count


def _next_environmental_fault_streak(
    notes: Iterable[dict[str, Any]], signature: str
) -> int:
    """The streak length THIS occurrence would extend to, if recorded now."""
    prior_signature, prior_streak = trailing_environmental_fault_streak(notes)
    return prior_streak + 1 if prior_signature == signature else 1


def first_selectable(
    sub: Any,
    pending: list[dict],
    all_tasks: list[dict],
    release_by_task_id: dict[str, guards.ReleaseState],
) -> dict | None:
    """The first task in ``pending`` order left selectable by
    ``selectable_verdict``, or ``None`` if none is."""
    for task in pending:
        notes = sub.list_notes(task["id"])
        verdict = selectable_verdict(task, notes, all_tasks, release_by_task_id)
        if not verdict.selectable:
            print(f"  skip {task['id'][:8]} — {verdict.hold_reasons[0]}")
            continue
        return task
    return None


# ---------------------------------------------------------------------------
# R26.12 B7: the declared queue order
#
# Everything below is new -- B2 moved only first_selectable and the state it
# needs above. rank_pending computes the order the design declares (urgency x
# impact, predecessor inheritance, bounded aging) as a pure function of
# injected inputs; nothing in production calls it yet (see this module's
# acceptance criteria -- B8 wires it into pick_task later). selectable_verdict
# is the per-task predicate first_selectable's loop runs inline, pulled out
# so this function can call it without a store.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeBox:
    """The release-level window urgency is measured against."""

    opened_at: datetime | None
    target_at: datetime | None


@dataclass(frozen=True)
class RankPolicy:
    """The policy values the urgency/aging arithmetic reads, injected rather
    than read from a file (2026-09-25 the Operator decision P4): the design's own
    values are 0.33/0.66 urgency bands, one aging tier per 14 days, at most 3
    steps."""

    urgency_band_medium: float
    urgency_band_high: float
    aging_days: int
    max_aging_steps: int = 3
    policy_revision: str | None = None

    @staticmethod
    def from_mapping(mapping: Mapping[str, Any], policy_revision: str | None = None) -> "RankPolicy":
        """Read exactly ``urgency_bands.{medium,high}``, ``aging_days`` and an
        optional ``max_aging_steps`` from ``mapping``, ignoring every other
        key -- so a caller (B8) can pass the whole governed health-policy.json
        straight through without this module knowing its other dozen keys."""
        bands = mapping.get("urgency_bands") or {}
        medium = bands.get("medium")
        high = bands.get("high")
        aging_days = mapping.get("aging_days")
        max_aging_steps = mapping.get("max_aging_steps", 3)

        if medium is None or high is None:
            raise ValueError("policy.urgency_bands must declare both 'medium' and 'high'")
        medium = float(medium)
        high = float(high)
        if not (0 < medium < high <= 1):
            raise ValueError(
                f"policy.urgency_bands must satisfy 0 < medium < high <= 1, got medium={medium}, high={high}"
            )
        if not isinstance(aging_days, int) or isinstance(aging_days, bool) or aging_days <= 0:
            raise ValueError(f"policy.aging_days must be a positive int, got {aging_days!r}")
        if (
            not isinstance(max_aging_steps, int)
            or isinstance(max_aging_steps, bool)
            or not (0 <= max_aging_steps <= 3)
        ):
            raise ValueError(f"policy.max_aging_steps must be an int in 0..3, got {max_aging_steps!r}")

        return RankPolicy(
            urgency_band_medium=medium,
            urgency_band_high=high,
            aging_days=aging_days,
            max_aging_steps=max_aging_steps,
            policy_revision=policy_revision,
        )


def _iso_z(value: datetime) -> str:
    """Render a timezone-aware datetime as ISO-8601 UTC with a 'Z' suffix."""
    utc = value.astimezone(timezone.utc)
    rendered = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        rendered += f".{utc.microsecond:06d}"
    return rendered + "Z"


def _render_explanation_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, datetime):
        return _iso_z(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(value) if value else "none"
    return str(value)


@dataclass(frozen=True)
class QueuePosition:
    """One task's place in the declared order, with every field the
    arithmetic that put it there read from -- see ``explanation``."""

    task_id: str
    position: int | None
    selectable: bool
    latched: bool
    hold_reasons: tuple[str, ...]
    class_of_service: str
    class_source: str
    urgency: str
    urgency_source: str
    impact: str
    impact_source: str
    priority: int
    effective_priority: int
    inherited_from: str | None
    unblocks: tuple[str, ...]
    waiting_since: datetime
    waiting_since_source: str
    age_days: int
    aged_steps: int
    release_ref: str | None
    release_target_at: datetime | None
    outcome_ref: str | None
    explanation: str


def _position_to_jsonable(position: QueuePosition) -> dict[str, Any]:
    return {
        "task_id": position.task_id,
        "position": position.position,
        "selectable": position.selectable,
        "latched": position.latched,
        "hold_reasons": list(position.hold_reasons),
        "class_of_service": position.class_of_service,
        "class_source": position.class_source,
        "urgency": position.urgency,
        "urgency_source": position.urgency_source,
        "impact": position.impact,
        "impact_source": position.impact_source,
        "priority": position.priority,
        "effective_priority": position.effective_priority,
        "inherited_from": position.inherited_from,
        "unblocks": list(position.unblocks),
        "waiting_since": _iso_z(position.waiting_since),
        "waiting_since_source": position.waiting_since_source,
        "age_days": position.age_days,
        "aged_steps": position.aged_steps,
        "release_ref": position.release_ref,
        "release_target_at": _iso_z(position.release_target_at) if position.release_target_at is not None else None,
        "outcome_ref": position.outcome_ref,
        "explanation": position.explanation,
    }


@dataclass(frozen=True)
class QueueOrder:
    """The whole declared order: ``ranked`` (selectable, in position order)
    and ``held`` (not selectable right now, with why)."""

    ranked: tuple[QueuePosition, ...]
    held: tuple[QueuePosition, ...]
    computed_at: datetime
    computed_by_revision: str | None
    policy_revision: str | None
    inputs_unavailable: tuple[str, ...]
    fallback_reason: str | None = None

    def to_json(self) -> str:
        payload = {
            "ranked": [_position_to_jsonable(p) for p in self.ranked],
            "held": [_position_to_jsonable(p) for p in self.held],
            "computed_at": _iso_z(self.computed_at),
            "computed_by_revision": self.computed_by_revision,
            "policy_revision": self.policy_revision,
            "inputs_unavailable": list(self.inputs_unavailable),
            "fallback_reason": self.fallback_reason,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def rank_key(position: QueuePosition) -> tuple:
    """The sort key this bead's queue order is sorted by: k0 class of
    service, k1a effective priority, k1b the task's OWN release target_at
    (missing sorts last), k3 waiting_since, then task id."""
    k0 = 0 if position.class_of_service == "emergency" else 1
    target_at = position.release_target_at
    k1b = (0, target_at) if target_at is not None else (1, None)
    return (k0, position.effective_priority, k1b, position.waiting_since, position.task_id)


@dataclass(frozen=True)
class SelectableVerdict:
    """Whether a task may be claimed right now, and EVERY reason it is not.

    ``hold_reasons[0]`` is always exactly ``guards.is_runnable``'s own
    ``.reason`` when it is not ok -- the literal first-failing-gate text,
    never a re-derivation of it -- so it can never drift from "today's
    first-return gate" no matter how many more reasons get appended after
    it. ``latched`` is true exactly when the environmental-fault streak has
    reached ``retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT``.
    """

    selectable: bool
    hold_reasons: tuple[str, ...]
    latched: bool


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _full_predecessor_walk_reasons(
    task: dict[str, Any], all_tasks: list[dict[str, Any]]
) -> list[str]:
    """Every declared predecessor that is missing, superseded, or not landed
    -- not just the first one ``guards.ordering_block_reason`` stops at.

    Cycle detection is deliberately not repeated here: that case is already
    covered by re-running ``guards.ordering_block_reason`` itself, which
    ``selectable_verdict`` also does.
    """
    tasks_by_id = {
        str(t.get("id") or "").strip(): t for t in all_tasks if str(t.get("id") or "").strip()
    }
    reasons: list[str] = []
    for predecessor_id in guards.predecessor_bead_ids(task):
        predecessor = tasks_by_id.get(predecessor_id)
        if predecessor is None:
            reasons.append(f"predecessor bead {predecessor_id} not found")
            continue
        if str(predecessor.get("state") or "").strip() in guards.SUCCESSFUL_TERMINAL_STATES:
            continue
        replacements = guards.superseding_task_ids(predecessor, all_tasks)
        if replacements:
            reasons.append(
                f"predecessor {predecessor_id} is held ("
                + guards.superseded_reason(replacements)
                + ") and can never land"
            )
            continue
        state = str(predecessor.get("state") or "(missing state)").strip()
        reasons.append(f"waiting for predecessor bead {predecessor_id} to land (state={state})")
    return reasons


def _blocking_question_reason(notes: list[dict[str, Any]]) -> str:
    pending = guards.blocking_questions(notes)
    if not pending:
        return ""
    question_id = pending[0].get("id") or "(missing id)"
    body = ((pending[0].get("content") or {}).get("body") or "").strip()
    answers = [
        n
        for n in notes
        if (n.get("content") or {}).get("kind") == "answer"
        and (n.get("content") or {}).get("answers_ref") == pending[0].get("id")
    ]
    if answers:
        return (
            f"blocking question {question_id} has an answer that does not "
            f"declare release ({guards.RELEASES_WORK_FIELD}=true required): {body[:120]}"
        )
    return f"waiting on blocking question {question_id}: {body[:120]}"


def _attempts_exhausted_reason(notes: list[dict[str, Any]]) -> str:
    attempts = len(guards.prior_failures(notes))
    max_attempts = retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS
    if attempts >= max_attempts:
        return f"attempts exhausted ({attempts}/{max_attempts}, per recorded failures)"
    return ""


def _scope_paths_reason(task: dict[str, Any]) -> str:
    scope = (task.get("content") or {}).get("scope") or {}
    if not (scope.get("paths") or []):
        return "task declares no scope.paths"
    return ""


def selectable_verdict(
    task: dict[str, Any],
    notes: Iterable[dict[str, Any]],
    all_tasks: list[dict[str, Any]],
    release_by_task_id: dict[str, guards.ReleaseState] | None,
    *,
    apply_breaker: bool = True,
) -> SelectableVerdict:
    """Whether ``task`` may be claimed right now, and EVERY reason it is
    not, not just the first.

    ``hold_reasons[0]`` is pinned to ``guards.is_runnable``'s own
    first-failing-gate reason (see ``SelectableVerdict``'s docstring);
    because ``is_runnable`` evaluates supersede -> ordering -> release ->
    attempts -> blocking -> scope in that exact sequence and stops at the
    first failure, whichever gate produced that reason tells us every gate
    before it already passed -- so independently re-evaluating the earlier
    gates below can only add strictly new information, never a different
    first answer. Every other applicable hold the design's SELECTABLE list
    names is appended after it, in that list's order, deduplicated against
    what is already present.

    ``apply_breaker`` gates the environmental-fault latch alone (the
    operator-force rule): with ``apply_breaker=False`` the streak is not
    even computed, ``latched`` is ``False`` and no latch hold is added.
    """
    notes = list(notes)

    reasons: list[str] = []
    verdict = guards.is_runnable(task, notes, all_tasks, release_by_task_id=release_by_task_id)
    if not verdict.ok:
        reasons.append(verdict.reason)

    release_reason = guards.release_block_reason(task, release_by_task_id)
    attempts_reason = _attempts_exhausted_reason(notes)
    blocking_reason = _blocking_question_reason(notes)
    scope_reason = _scope_paths_reason(task)

    # guards.is_runnable evaluates supersede -> ordering -> release ->
    # attempts -> blocking -> scope in that fixed order and stops at the
    # first failure. The four reasons above are byte-for-byte mirrors of
    # what is_runnable itself computes for those same gates from the same
    # inputs, so if verdict.reason equals one of them -- or verdict.ok is
    # true -- is_runnable necessarily reached and passed the ordering gate
    # already (every predecessor landed, no cycle): re-walking predecessors
    # or re-running guards.ordering_block_reason can only reproduce "no
    # issue", a second whole-population scan for nothing. Symmetrically,
    # verdict.reason can only carry guards.superseded_reason's fixed
    # "superseded by bead " prefix when the supersede gate itself failed, or
    # guards.ordering_block_reason's "predecessor cycle detected" prefix when
    # a cycle is what is_runnable found -- the two cases where is_runnable
    # never reached (or never passed) the ordering gate, so the walk below is
    # not redundant for either. With at most one declared predecessor AND no
    # cycle, the walk's loop can only ever reproduce the single reason
    # is_runnable's own ordering gate already found, so it is skipped only
    # then. This is what keeps a 500-task sweep (rank_pending,
    # _fifo_positions) from repeating an O(n) scan per task per gate it
    # already settled (dev.finding recorded against PR #1201's CI-red
    # timing).
    ordering_already_passed = verdict.ok or verdict.reason in (
        release_reason,
        attempts_reason,
        blocking_reason,
        scope_reason,
    )
    task_superseded = not verdict.ok and verdict.reason.startswith("superseded by bead ")
    task_cycle = not verdict.ok and verdict.reason.startswith("predecessor cycle detected")

    if not ordering_already_passed:
        if task_superseded or task_cycle or len(guards.predecessor_bead_ids(task)) > 1:
            for reason in _full_predecessor_walk_reasons(task, all_tasks):
                _append_reason(reasons, reason)
        if task_superseded:
            _append_reason(reasons, guards.ordering_block_reason(task, all_tasks))

    _append_reason(reasons, release_reason)
    _append_reason(reasons, attempts_reason)
    _append_reason(reasons, blocking_reason)
    _append_reason(reasons, scope_reason)

    latched = False
    if apply_breaker:
        signature, streak = trailing_environmental_fault_streak(notes)
        limit = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
        if signature and streak >= limit:
            reasons.append(
                f"same environmental fault recorded "
                f"{streak} consecutive times (bound {limit}); this is a stop, not "
                "a verdict on the work — use --task to force a run"
            )
            latched = True

    return SelectableVerdict(selectable=not reasons, hold_reasons=tuple(reasons), latched=latched)


#: (urgency, impact) -> priority tier, before aging and inheritance (AC-4).
#: Every pair not named here is P4.
_PRIORITY_MATRIX: dict[tuple[str, str], int] = {
    ("high", "high"): 1,
    ("high", "medium"): 2,
    ("medium", "high"): 2,
    ("medium", "medium"): 3,
    ("high", "low"): 3,
    ("low", "high"): 3,
}


def _urgency(
    release: guards.ReleaseState | None,
    time_box: TimeBox | None,
    policy: RankPolicy,
    now: datetime,
) -> tuple[str, str]:
    if release is None:
        return "low", "no_release"
    if (
        time_box is None
        or time_box.opened_at is None
        or time_box.target_at is None
        or time_box.target_at <= time_box.opened_at
    ):
        return "low", "time_unmeasurable"
    elapsed = (now - time_box.opened_at) / (time_box.target_at - time_box.opened_at)
    if elapsed >= policy.urgency_band_high or now > time_box.target_at:
        return "high", "time_box"
    if elapsed >= policy.urgency_band_medium:
        return "medium", "time_box"
    return "low", "time_box"


def _impact(
    release: guards.ReleaseState | None,
    release_ref: str | None,
    outcome_ref: str | None,
    impact_by_outcome: Mapping[str, str] | None,
) -> tuple[str, str]:
    if impact_by_outcome is None or release is None or not outcome_ref:
        return "medium", "default"
    key = f"{release_ref}/{outcome_ref}"
    if key not in impact_by_outcome:
        return "medium", "default"
    value = impact_by_outcome[key]
    if value not in ("high", "medium", "low"):
        raise ValueError(f"impact_by_outcome[{key!r}] must be 'high', 'medium' or 'low', got {value!r}")
    return value, "outcome"


def _parse_task_created_at(value: Any) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_dependents_map(
    pending_tasks: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Reverse predecessor adjacency over ``pending_tasks``, built once per
    pending set rather than rescanned per id: guards.dependents_of itself
    scans every task for every query, which is what made the per-id lookups
    below O(N^2) over a pending set. guards.dependents_of's own semantics --
    self-exclusion, the stripped-id normalisation -- are preserved by
    building the map from guards.predecessor_bead_ids directly, called once
    per task."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for candidate in pending_tasks:
        candidate_id = str(candidate.get("id") or "").strip()
        for predecessor_id in dict.fromkeys(guards.predecessor_bead_ids(candidate)):
            if predecessor_id == candidate_id:
                continue
            buckets.setdefault(predecessor_id, []).append(candidate)
    return {bead_id: tuple(dependents) for bead_id, dependents in buckets.items()}


def _transitive_pending_dependents(
    task_id: str, dependents_of_id: Callable[[str], tuple[dict[str, Any], ...]]
) -> list[str]:
    """Every pending task reachable by following 'names this id as a
    predecessor' edges forward, cycle-safe: an id already seen on this walk
    is never re-queued, so a predecessor cycle terminates instead of looping."""
    visited: set[str] = set()
    stack = [task_id]
    reachable: list[str] = []
    while stack:
        current = stack.pop()
        for dependent in dependents_of_id(current):
            dependent_id = dependent["id"]
            if dependent_id in visited:
                continue
            visited.add(dependent_id)
            reachable.append(dependent_id)
            stack.append(dependent_id)
    return reachable


def _explain(
    *,
    release_ref: str | None,
    release_target_at: datetime | None,
    outcome_ref: str | None,
    urgency: str,
    urgency_source: str,
    impact: str,
    impact_source: str,
    priority: int,
    effective_priority: int,
    inherited_from: str | None,
    unblocks: tuple[str, ...],
    waiting_since: datetime,
    waiting_since_source: str,
    age_days: int,
    aged_steps: int,
    hold_reasons: tuple[str, ...],
) -> str:
    fields = (
        ("release_ref", release_ref),
        ("release_target_at", release_target_at),
        ("outcome_ref", outcome_ref),
        ("urgency", urgency),
        ("urgency_source", urgency_source),
        ("impact", impact),
        ("impact_source", impact_source),
        ("priority", priority),
        ("effective_priority", effective_priority),
        ("inherited_from", inherited_from),
        ("unblocks", list(unblocks)),
        ("waiting_since", waiting_since),
        ("waiting_since_source", waiting_since_source),
        ("age_days", age_days),
        ("aged_steps", aged_steps),
        ("hold_reasons", list(hold_reasons)),
    )
    rendered = [f"{name}={_render_explanation_value(value)}" for name, value in fields]
    return "Queue position: " + "; ".join(rendered) + "."


def rank_pending(
    tasks: Iterable[dict[str, Any]],
    *,
    notes_by_id: Mapping[str, Iterable[dict[str, Any]]],
    release_by_task_id: Mapping[str, guards.ReleaseState] | None,
    time_box_by_release_ref: Mapping[str, TimeBox] | None,
    policy: RankPolicy,
    now: datetime,
    claimable_since_by_task_id: Mapping[str, datetime | None],
    impact_by_outcome: Mapping[str, str] | None = None,
    claimable_since_source: str = "injected",
    computed_by_revision: str | None = None,
) -> QueueOrder:
    """The declared queue order (design "Mechanism" > "Queue order"): pure,
    unwired, explains every position. See this module's acceptance criteria
    for the full contract; nothing in production calls this yet."""
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    inputs_unavailable: list[str] = []
    if release_by_task_id is None:
        inputs_unavailable.append("release_by_task_id")
    if time_box_by_release_ref is None:
        inputs_unavailable.append("time_box_by_release_ref")
    if inputs_unavailable:
        return QueueOrder(
            ranked=(),
            held=(),
            computed_at=now,
            computed_by_revision=computed_by_revision,
            policy_revision=policy.policy_revision,
            inputs_unavailable=tuple(inputs_unavailable),
        )

    all_tasks = list(tasks)
    pending_tasks = [t for t in all_tasks if t.get("state") == "pending"]

    dependents_map = _build_dependents_map(pending_tasks)

    def dependents_of_id(task_id: str) -> tuple[dict[str, Any], ...]:
        return dependents_map.get(task_id, ())

    own_aged_tier: dict[str, int] = {}
    own_fields: dict[str, dict[str, Any]] = {}

    for task in pending_tasks:
        task_id = task["id"]
        content = task.get("content") or {}

        if task_id not in notes_by_id:
            selectable = False
            hold_reasons: tuple[str, ...] = ("notes unavailable for this bead",)
            latched = False
        else:
            notes = list(notes_by_id[task_id])
            verdict = selectable_verdict(task, notes, all_tasks, release_by_task_id)
            selectable, hold_reasons, latched = verdict.selectable, verdict.hold_reasons, verdict.latched

        release = release_by_task_id.get(task_id)
        release_ref = release.ref if release is not None else None
        time_box = time_box_by_release_ref.get(release_ref) if release is not None else None
        release_target_at = time_box.target_at if time_box is not None else None

        urgency, urgency_source = _urgency(release, time_box, policy, now)
        outcome_ref = content.get("outcome_ref")
        impact, impact_source = _impact(release, release_ref, outcome_ref, impact_by_outcome)

        own_priority = _PRIORITY_MATRIX.get((urgency, impact), 4)

        claimable = claimable_since_by_task_id.get(task_id)
        if claimable is not None:
            waiting_since = claimable
            waiting_since_source = claimable_since_source
        else:
            waiting_since = _parse_task_created_at(task.get("created_at"))
            waiting_since_source = "created_at"

        age_days = max(0, math.floor((now - waiting_since).total_seconds() / 86400))
        aged_steps = min(policy.max_aging_steps, age_days // policy.aging_days)
        own_aged_tier[task_id] = max(1, own_priority - aged_steps)

        own_fields[task_id] = {
            "selectable": selectable,
            "hold_reasons": hold_reasons,
            "latched": latched,
            "release_ref": release_ref,
            "release_target_at": release_target_at,
            "outcome_ref": outcome_ref,
            "urgency": urgency,
            "urgency_source": urgency_source,
            "impact": impact,
            "impact_source": impact_source,
            "priority": own_priority,
            "waiting_since": waiting_since,
            "waiting_since_source": waiting_since_source,
            "age_days": age_days,
            "aged_steps": aged_steps,
        }

    positions: list[QueuePosition] = []
    for task_id, fields in own_fields.items():
        own_tier = own_aged_tier[task_id]
        reachable = _transitive_pending_dependents(task_id, dependents_of_id)
        candidates = [(own_aged_tier[dep_id], dep_id) for dep_id in reachable if dep_id in own_aged_tier]

        effective_priority = own_tier
        inherited_from: str | None = None
        if candidates:
            best_tier = min(tier for tier, _ in candidates)
            if best_tier < own_tier:
                effective_priority = best_tier
                inherited_from = min(dep_id for tier, dep_id in candidates if tier == best_tier)

        unblocks = tuple(sorted(d["id"] for d in dependents_of_id(task_id)))

        explanation = _explain(
            release_ref=fields["release_ref"],
            release_target_at=fields["release_target_at"],
            outcome_ref=fields["outcome_ref"],
            urgency=fields["urgency"],
            urgency_source=fields["urgency_source"],
            impact=fields["impact"],
            impact_source=fields["impact_source"],
            priority=fields["priority"],
            effective_priority=effective_priority,
            inherited_from=inherited_from,
            unblocks=unblocks,
            waiting_since=fields["waiting_since"],
            waiting_since_source=fields["waiting_since_source"],
            age_days=fields["age_days"],
            aged_steps=fields["aged_steps"],
            hold_reasons=fields["hold_reasons"],
        )

        positions.append(
            QueuePosition(
                task_id=task_id,
                position=None,
                selectable=fields["selectable"],
                latched=fields["latched"],
                hold_reasons=fields["hold_reasons"],
                class_of_service="normal",
                class_source="none",
                urgency=fields["urgency"],
                urgency_source=fields["urgency_source"],
                impact=fields["impact"],
                impact_source=fields["impact_source"],
                priority=fields["priority"],
                effective_priority=effective_priority,
                inherited_from=inherited_from,
                unblocks=unblocks,
                waiting_since=fields["waiting_since"],
                waiting_since_source=fields["waiting_since_source"],
                age_days=fields["age_days"],
                aged_steps=fields["aged_steps"],
                release_ref=fields["release_ref"],
                release_target_at=fields["release_target_at"],
                outcome_ref=fields["outcome_ref"],
                explanation=explanation,
            )
        )

    ranked_positions = sorted((p for p in positions if p.selectable), key=rank_key)
    held_positions = sorted((p for p in positions if not p.selectable), key=rank_key)
    ranked = tuple(
        QueuePosition(
            task_id=p.task_id,
            position=i,
            selectable=p.selectable,
            latched=p.latched,
            hold_reasons=p.hold_reasons,
            class_of_service=p.class_of_service,
            class_source=p.class_source,
            urgency=p.urgency,
            urgency_source=p.urgency_source,
            impact=p.impact,
            impact_source=p.impact_source,
            priority=p.priority,
            effective_priority=p.effective_priority,
            inherited_from=p.inherited_from,
            unblocks=p.unblocks,
            waiting_since=p.waiting_since,
            waiting_since_source=p.waiting_since_source,
            age_days=p.age_days,
            aged_steps=p.aged_steps,
            release_ref=p.release_ref,
            release_target_at=p.release_target_at,
            outcome_ref=p.outcome_ref,
            explanation=p.explanation,
        )
        for i, p in enumerate(ranked_positions)
    )

    return QueueOrder(
        ranked=ranked,
        held=tuple(held_positions),
        computed_at=now,
        computed_by_revision=computed_by_revision,
        policy_revision=policy.policy_revision,
        inputs_unavailable=(),
    )


# ---------------------------------------------------------------------------
# R26.12 S55 rail: the order can be read and switched off before it drives a
# pick. order_mode reads the FACTORY_QUEUE_ORDER kill switch from an explicit
# environ (never os.environ itself -- B7 AC-12's import/IO guard extends to
# this function too); rank_or_fifo picks rank_pending or a dead-simple FIFO
# order accordingly, and falls back to FIFO on any rank_pending failure so
# the selector is never left with neither. Nothing in pick_task calls either
# of these yet -- see this bead's acceptance criteria.
# ---------------------------------------------------------------------------


def order_mode(environ: Mapping[str, str]) -> Literal["rank", "fifo"]:
    """'fifo' when FACTORY_QUEUE_ORDER is exactly 'fifo' (case-insensitive,
    whitespace-stripped), 'rank' when unset or 'rank', else a ValueError
    naming the value -- a typo must not silently pick either mode."""
    raw = environ.get("FACTORY_QUEUE_ORDER")
    if raw is None:
        return "rank"
    normalized = raw.strip().lower()
    if normalized == "fifo":
        return "fifo"
    if normalized == "rank":
        return "rank"
    raise ValueError(f"FACTORY_QUEUE_ORDER must be 'rank' or 'fifo', got {raw!r}")


#: The fixed explanation every FIFO position carries (AC-2) -- FIFO never
#: computes urgency/impact/aging, so there is nothing per-position to render.
FIFO_EXPLANATION = "FIFO (FACTORY_QUEUE_ORDER=fifo)"

#: What an unparsable (or missing) ``created_at`` falls back to in FIFO mode
#: -- FIFO is itself the fallback a failing rank_pending lands on, so it must
#: never raise on the exact inputs that made rank_pending raise.
_FIFO_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _fifo_waiting_since(value: Any) -> tuple[datetime, str]:
    """``_parse_task_created_at``, tolerant: a missing or unparsable
    ``created_at`` becomes the epoch rather than raising."""
    try:
        return _parse_task_created_at(value), "created_at"
    except (ValueError, TypeError):
        return _FIFO_EPOCH, "unparsable_created_at"


def _fifo_age_days(now: datetime, waiting_since: datetime) -> int:
    """Same arithmetic as rank_pending's age_days, tolerant of a naive
    ``now`` reaching here from a ``rank_pending`` failure it raised on."""
    try:
        return max(0, math.floor((now - waiting_since).total_seconds() / 86400))
    except TypeError:
        return 0


def _fifo_positions(
    tasks: Iterable[dict[str, Any]],
    *,
    notes_by_id: Mapping[str, Iterable[dict[str, Any]]],
    release_by_task_id: Mapping[str, guards.ReleaseState] | None,
    time_box_by_release_ref: Mapping[str, TimeBox] | None,
    now: datetime,
) -> list[QueuePosition]:
    """Every pending task as a QueuePosition, selectable exactly as
    ``rank_pending`` would (same ``selectable_verdict`` call, same "notes
    unavailable" special case -- this is what keeps the selectable+held id
    set identical between rank and FIFO mode), but with none of the
    urgency/impact/aging arithmetic that can fail: that is the whole point
    of a kill switch."""
    all_tasks = list(tasks)
    pending_tasks = [t for t in all_tasks if t.get("state") == "pending"]

    positions: list[QueuePosition] = []
    for task in pending_tasks:
        task_id = task["id"]

        if task_id not in notes_by_id:
            selectable = False
            hold_reasons: tuple[str, ...] = ("notes unavailable for this bead",)
            latched = False
        else:
            notes = list(notes_by_id[task_id])
            verdict = selectable_verdict(task, notes, all_tasks, release_by_task_id)
            selectable, hold_reasons, latched = verdict.selectable, verdict.hold_reasons, verdict.latched

        release = release_by_task_id.get(task_id) if release_by_task_id else None
        release_ref = release.ref if release is not None else None
        time_box = (
            time_box_by_release_ref.get(release_ref)
            if (time_box_by_release_ref and release_ref is not None)
            else None
        )
        release_target_at = time_box.target_at if time_box is not None else None
        outcome_ref = (task.get("content") or {}).get("outcome_ref")

        waiting_since, waiting_since_source = _fifo_waiting_since(task.get("created_at"))
        if waiting_since_source == "unparsable_created_at":
            age_days = 0
        else:
            age_days = _fifo_age_days(now, waiting_since)

        positions.append(
            QueuePosition(
                task_id=task_id,
                position=None,
                selectable=selectable,
                latched=latched,
                hold_reasons=hold_reasons,
                class_of_service="normal",
                class_source="none",
                urgency="unknown",
                urgency_source="fifo",
                impact="unknown",
                impact_source="fifo",
                priority=0,
                effective_priority=0,
                inherited_from=None,
                unblocks=(),
                waiting_since=waiting_since,
                waiting_since_source=waiting_since_source,
                age_days=age_days,
                aged_steps=0,
                release_ref=release_ref,
                release_target_at=release_target_at,
                outcome_ref=outcome_ref,
                explanation=FIFO_EXPLANATION,
            )
        )
    return positions


def _fifo_order(
    tasks: Iterable[dict[str, Any]],
    *,
    notes_by_id: Mapping[str, Iterable[dict[str, Any]]],
    release_by_task_id: Mapping[str, guards.ReleaseState] | None,
    time_box_by_release_ref: Mapping[str, TimeBox] | None,
    policy: RankPolicy,
    now: datetime,
    computed_by_revision: str | None,
    fallback_reason: str | None,
) -> QueueOrder:
    all_tasks = list(tasks)
    positions = _fifo_positions(
        all_tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=release_by_task_id,
        time_box_by_release_ref=time_box_by_release_ref,
        now=now,
    )
    #: Oldest first by the raw, unparsed created_at string, then task id --
    #: the same tie-break dispatch.pick_task's own FIFO sort uses, and one
    #: that (unlike sorting on the parsed waiting_since) can never raise.
    raw_created_at_by_id = {t["id"]: str(t.get("created_at") or "") for t in all_tasks}
    sort_key = lambda p: (raw_created_at_by_id[p.task_id], p.task_id)  # noqa: E731
    selectable_sorted = sorted((p for p in positions if p.selectable), key=sort_key)
    held_sorted = sorted((p for p in positions if not p.selectable), key=sort_key)
    ranked = tuple(replace(p, position=i) for i, p in enumerate(selectable_sorted))

    return QueueOrder(
        ranked=ranked,
        held=tuple(held_sorted),
        computed_at=now,
        computed_by_revision=computed_by_revision,
        policy_revision=policy.policy_revision,
        inputs_unavailable=(),
        fallback_reason=fallback_reason,
    )


def rank_or_fifo(
    mode: Literal["rank", "fifo"],
    tasks: Iterable[dict[str, Any]],
    *,
    notes_by_id: Mapping[str, Iterable[dict[str, Any]]],
    release_by_task_id: Mapping[str, guards.ReleaseState] | None,
    time_box_by_release_ref: Mapping[str, TimeBox] | None,
    policy: RankPolicy,
    now: datetime,
    claimable_since_by_task_id: Mapping[str, datetime | None],
    impact_by_outcome: Mapping[str, str] | None = None,
    claimable_since_source: str = "injected",
    computed_by_revision: str | None = None,
) -> QueueOrder:
    """``rank_pending``'s order in 'rank' mode, or the dead-simple FIFO order
    in 'fifo' mode -- the ``FACTORY_QUEUE_ORDER`` kill switch (``order_mode``)
    picks which. Never raises: a ``rank_pending`` failure in 'rank' mode
    falls back to FIFO with the exception recorded in ``fallback_reason``
    rather than propagating, so the selector is never left with neither."""
    if mode == "fifo":
        return _fifo_order(
            tasks,
            notes_by_id=notes_by_id,
            release_by_task_id=release_by_task_id,
            time_box_by_release_ref=time_box_by_release_ref,
            policy=policy,
            now=now,
            computed_by_revision=computed_by_revision,
            fallback_reason=None,
        )
    if mode != "rank":
        raise ValueError(f"mode must be 'rank' or 'fifo', got {mode!r}")

    try:
        return rank_pending(
            tasks,
            notes_by_id=notes_by_id,
            release_by_task_id=release_by_task_id,
            time_box_by_release_ref=time_box_by_release_ref,
            policy=policy,
            now=now,
            claimable_since_by_task_id=claimable_since_by_task_id,
            impact_by_outcome=impact_by_outcome,
            claimable_since_source=claimable_since_source,
            computed_by_revision=computed_by_revision,
        )
    except Exception as exc:  # noqa: BLE001 - any rank failure falls back to FIFO, never propagates
        return _fifo_order(
            tasks,
            notes_by_id=notes_by_id,
            release_by_task_id=release_by_task_id,
            time_box_by_release_ref=time_box_by_release_ref,
            policy=policy,
            now=now,
            computed_by_revision=computed_by_revision,
            fallback_reason=f"{type(exc).__name__}: {exc}",
        )
