#!/usr/bin/env python3
"""File a dev.task bead from a JSON spec.

    python file_task.py tasks/my-task.json
    cat spec.json | python file_task.py -

The spec only needs the fields that require judgement; everything mechanical is
defaulted here so filing a task is a few lines rather than a schema exercise:

    {
      "lane": "bug-triage",
      "title": "Meter vision inference",
      "intent": "vision.py never calls log_llm_cost, so...",
      "acceptance": ["WHEN extract runs, THE cost SHALL be logged"],
      "scope": {"paths": ["apps/mcp-hub/src/tools/vision.py"]},
      "risk_class": "behavioral"
    }

``forbidden_paths`` always gains ``.github/workflows/**`` — the substrate
rejects a dev.task without it, because the factory may not write the gates that
judge it. Defaulting it here means a hand-written spec cannot accidentally
propose a task that widens its own boundary.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import dispatch
import guards
import retry_policy
from substrate import Substrate

# The resolver lives in scripts/traceability.py and is reused rather than
# reimplemented: two copies of "does this reference resolve" drift apart, and the
# one that drifts is always the one doing the gating.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import traceability  # noqa: E402

CREATED_BY = "factory-dispatcher/file-task"
_CONTRACT_PATH = (
    Path(__file__).resolve().parent / "contracts" / "task-intake-contract.json"
)
_CONTRACT = json.loads(_CONTRACT_PATH.read_text())
REQUIRED = tuple(_CONTRACT["required"])


def _coerce_forbidden_always(value: Any) -> list[str]:
    """``forbidden_always`` must be a list of strings (R26.12 B16).

    The earlier shape was the bare string ``".github/workflows/**"``, and
    ``list(value)`` on a string silently iterates its characters rather than
    raising -- the exact "shape cannot silently regress" failure this guards
    against. A non-list value is refused outright instead.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SystemExit(
            "task-intake-contract.json's forbidden_always must be a list of "
            f"strings; got {value!r}. The old bare-string shape no longer "
            "validates -- update the contract to a list."
        )
    return list(value)


FORBIDDEN_ALWAYS = _coerce_forbidden_always(_CONTRACT["forbidden_always"])

#: Marker fields reserved for the operator verb B17 adds -- a filed spec may
#: never carry one directly (R26.12 B16; see .factory/design.md). Mirrors
#: apps/substrate/src/schemas.py:204-206's DevTaskContent field names exactly.
MARKER_FIELDS = ("class_of_service", "expedite_reason", "expedite_until")

DEFAULT_BUDGET = {"max_agent_minutes": 30, "max_usd": 2.0, "max_tokens": 250000}
DEFAULT_VERIFICATION = {
    "commands": ["cd apps/mcp-hub && python -m pytest tests/ -q"],
    "must_report_unverified": True,
}


REQUIREMENTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "requirements"
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _check_traceability(spec: dict[str, Any]) -> None:
    """Newly filed work must cite a requirement that exists, or say why not.

    the Operator's decision 2026-08-08, taken on the count from TF-1: 12 of 64 dev.task
    records carried a requirement reference, 52 carried none, 0 pointed nowhere.

    The refusal lives HERE and nowhere else. Putting it in DevTaskContent would
    make those 52 historical beads unreadable, and their content records what was
    true when they ran. Gating on the whole population would block today's work on
    July's debt and be abandoned inside a week. Filing is the only boundary that
    separates what we do from now on from what we already did, and it needs no
    migration because the historical records are already filed.

    Resolution, not shape. The schema validator accepts PC-SUB-999 because it
    LOOKS like a reference; its own comment names the risk — "an unresolvable
    reference is worse than none, because it reads as traceability while pointing
    nowhere." A refusal that accepted one would only teach people to type
    something that passes.
    """
    waiver = str(spec.get("requirement_refs_waived") or "").strip()
    refs = [str(r) for r in (spec.get("requirement_refs") or [])]

    if refs:
        registry = traceability.load_registries(REQUIREMENTS_DIR)
        if registry.directory_missing:
            raise SystemExit(
                "could not check requirement reference(s) "
                + ", ".join(refs)
                + f": no requirements registry was found at {REQUIREMENTS_DIR} "
                "(the directory does not exist here).\n"
                "This is an environment fault, not a bad citation — the registry "
                "this check needs to resolve against was not reachable. Confirm "
                f"{REQUIREMENTS_DIR} is present wherever this runs, then refile."
            )
        dangling = [r for r in refs if not registry.resolves(r)]
        if dangling:
            raise SystemExit(
                "requirement reference(s) resolve to nothing: "
                + ", ".join(dangling)
                + f"\nsearched: {', '.join(registry.sources) or '(no registries found)'}"
                + "\nA reference that points nowhere reads as traceability and is not."
            )
        return

    if waiver:
        return

    registry = traceability.load_registries(REQUIREMENTS_DIR)
    if registry.directory_missing:
        raise SystemExit(
            "this task carries no requirement_refs.\n"
            f"the requirements registry at {REQUIREMENTS_DIR} does not exist here, "
            "so this is an environment fault, not a measured count of zero "
            "requirements — whether a citation would even resolve could not be "
            "checked.\n"
            "Cite the requirement this work satisfies, as LO-CAT-004 or LO-CAT-004/AC-1.\n"
            "If none applies yet, set requirement_refs_waived to a written reason — it is "
            "recorded on the bead and counted as its own population, so a waiver is a "
            "backlog item rather than a hole."
        )
    raise SystemExit(
        "this task carries no requirement_refs.\n"
        f"searched: {', '.join(registry.sources) or '(no registries found)'} "
        f"({len(registry.requirements)} requirements)\n"
        "Cite the requirement this work satisfies, as LO-CAT-004 or LO-CAT-004/AC-1.\n"
        "If none applies yet, set requirement_refs_waived to a written reason — it is "
        "recorded on the bead and counted as its own population, so a waiver is a "
        "backlog item rather than a hole."
    )


def _check_release_traceability(
    spec: dict[str, Any], sub: Substrate
) -> dispatch.ReleaseBinding | None:
    """Newly filed work must name the release it serves, or say why not.

    Cloned from ``_check_traceability`` one field over, but not folded into
    ``build_content``: that check is a pure shape/local-file read
    (``traceability.load_registries``), while this one is resolution — it
    needs a live substrate to know which releases exist, which are open, and
    what outcomes a charter declares (2026-08-25's release-traceability task:
    "resolution, not shape" is the whole point, the same lesson
    ``_check_traceability``'s own docstring already draws for PC-SUB-999).
    That is why this lives at the same tier as ``_check_predecessor_
    supersession``/``_check_not_a_duplicate`` — called from ``main()`` with
    ``sub``, not from ``build_content`` — rather than requiring every
    ``build_content`` caller (``scanner.assess()`` in particular, which must
    stay substrate-optional for its dry-run report) to thread one through.

    Returns the resolved :class:`dispatch.ReleaseBinding` so ``main()`` can
    write ``content.outcome_ref`` and, after the bead exists, the
    ``delivers`` edge — or ``None`` when the work is waived.
    """
    ref = str(spec.get("release_ref") or "").strip()
    waiver = str(spec.get("release_ref_waived") or "").strip()

    if ref:
        try:
            return dispatch.resolve_release_ref(ref, sub)
        except dispatch.ReleaseResolutionError as exc:
            raise SystemExit(str(exc)) from exc

    if waiver:
        return None

    try:
        open_refs = dispatch.list_open_release_refs(sub)
    except Exception as exc:  # noqa: BLE001 - unreachable substrate must still refuse
        raise SystemExit(
            "this task carries no release_ref and no release_ref_waived, and "
            f"open releases could not be listed ({type(exc).__name__}: {exc}). "
            "Refusing rather than filing a task with no delivers edge."
        ) from exc

    raise SystemExit(
        "this task carries no release_ref.\n"
        f"open releases: {', '.join(open_refs) or '(none open)'}\n"
        "Cite the release this work serves, as R26.01 or R26.01/O-2.\n"
        "If none applies yet, set release_ref_waived to a written reason — it "
        "is recorded on the bead and inspectable after the fact, exactly like "
        "requirement_refs_waived."
    )


def _check_worker_hint(spec: dict[str, Any]) -> None:
    """Refuse a spec naming a worker that filing already knows cannot run it.

    B-FRESH-1's spec carried worker_hint='codex' after the Operator retired codex
    2026-08-22 (OPS-4, dispatch.WORKER_REGISTRY['codex'].retired). Nothing
    rejected it at filing: dispatch.select_worker only sees the hint at
    dispatch time, and there it deliberately falls back to the default worker
    rather than refusing (retirement is permanent, and stranding the bead over
    a worker that is never coming back serves no one). That fallback means an
    author who typed a stale hint is never told their choice was overridden
    until well after a worker budget has been spent under a name nobody
    actually chose. Filing is the earlier, louder place to say so.

    Reuses dispatch.WORKER_REGISTRY -- the one declared roster -- rather than
    hard-coding worker names, so retiring the next worker needs no edit here.
    """
    hint = str(spec.get("worker_hint") or "").strip()
    if not hint:
        return

    live = sorted(
        name
        for name, entry in dispatch.WORKER_REGISTRY.items()
        if not entry.retired and not entry.quarantined
    )
    worker = dispatch.WORKER_REGISTRY.get(hint)
    if worker is None:
        raise SystemExit(
            f"worker_hint {hint!r} names no registered worker.\n"
            f"live workers: {', '.join(live) or '(none)'}\n"
            "Set worker_hint to one of these, or drop the field to use the "
            "default worker."
        )
    if worker.retired:
        reason = worker.retirement_reason or "no retirement reason recorded"
        raise SystemExit(
            f"worker_hint {hint!r} names a retired worker: {reason}\n"
            f"live workers: {', '.join(live) or '(none)'}\n"
            "Set worker_hint to one of these, or drop the field to use the "
            "default worker."
        )


#: The shape D7 exists to catch: a reference to the live/production store, to
#: "prod" adjacent to a store noun, or to a live read/write. Matched
#: case-insensitively, word-bounded so "production" alone (e.g. "production
#: incident") does not fire and "prod" does not match inside "product" or
#: "production".
#:
#: RECALIBRATED 2026-09-16 (release gate F3 on PR #883, head 9c43521c): bare
#: `prod` fired on prose that only names production without directing a
#: worker at it -- a k8s namespace ("platform-mcp-prod"), a symptom ("is
#: inert in prod"), a measurement already taken by the outer loop ("carries
#: source_class None in prod"), a past incident narrated in prose ("measured
#: ... against prod"). Measured across the committed corpus 2026-09-16: of
#: the specs the old bare-`prod` alternative refused, the ones above turned
#: out to be false positives once read in full, because the word next to
#: `prod` was never a store noun. Requiring `prod` to sit next to `store` or
#: `substrate` keeps the true positives (OPS-97 "live prod store", the
#: R2605-8/9 "prod substrate" measurements) while dropping the false ones --
#: a field that must routinely be declared against for prose that never
#: touches the store stops carrying signal for the case it exists for.
#:
#: RE-DESIGNED 2026-09-17 (release gate DO-NOT-MERGE on PR #888, findings
#: F1/F3/F4): the two entries that used to live in THIS alternation for the
#: ad0a0f95 shape -- a literal "SHALL be run through scripts/*.py" and a bare
#: "at execution" -- are gone from here. Both were keyword literals fitted to
#: one spec's exact wording and measured to miss natural paraphrases (F1) or
#: over-fire on unrelated prose (F3); see ``_script_run_directive_hit`` and
#: ``_execution_time_measurement_hit`` below for their structural/narrowed
#: replacements. `substrate[_ ]?url` is gone from here for the same reason
#: (F4) and lives in ``_substrate_url_hit`` instead, narrowed rather than
#: dropped.
_LIVE_STORE_KEYWORD_RE = re.compile(
    r"\b(live\s+store|production\s+store|prod\s+(?:store|substrate)"
    r"|live\s+read|live\s+write)\b",
    re.IGNORECASE,
)

#: F4 (release gate on PR #888): `substrate[_ ]?url` alone has no polarity --
#: it refused M7a's acceptance criterion about error handling when the
#: variable is UNSET ("With SUBSTRATE_URL or SUBSTRATE_API_KEY unset ... the
#: script SHALL exit with the same code as before"), the inverse of a
#: direction at the store. Requiring a read/imperative verb in the same
#: clause keeps the true positive (`curl $SUBSTRATE_URL/beads`) and drops the
#: "unset" false positive, which names no verb that reaches the store at all.
_SUBSTRATE_URL_RE = re.compile(r"\bsubstrate[_ ]?url\b", re.IGNORECASE)
_URL_READ_VERB_RE = re.compile(
    r"\b(?:read|curl|fetch|query|poll|hit|call|request|GET)\b", re.IGNORECASE
)

#: F3 (release gate on PR #888): bare `at execution` fired on prose that has
#: nothing to do with a measurement -- "THE lease SHALL be acquired at
#: execution time by the Temporal activity", "The registry digest SHALL be
#: pinned at execution, not at build" -- vocabulary pervasive in this repo's
#: dispatcher specs. Requiring a measurement word in the same clause keeps
#: ad0a0f95's real AC-5 ("THE COUNT SHALL BE RE-MEASURED AT EXECUTION") and
#: drops both measured false positives.
_MEASUREMENT_WORD_RE = re.compile(
    r"\b(?:re-?measured|measured|counted|recounted|the\s+count)\b", re.IGNORECASE
)
_EXECUTION_TIME_RE = re.compile(r"\bat\s+execution(?:\s+time)?\b", re.IGNORECASE)

#: PRIMARY, STRUCTURAL SIGNAL (release gate DO-NOT-MERGE on PR #888, finding
#: F1). ad0a0f95's real AC-2 -- "THE backfill SHALL be run through
#: scripts/arch-source-class-backfill.py" -- named a script and directed the
#: worker to run it, without ever saying "live store" or "prod store". The
#: #883/#888 keyword literal for this ("SHALL be run through scripts/*.py")
#: was fitted to that exact phrasing: re-derived by the outer loop against
#: four natural paraphrases ("executed using", "SHALL run", "performed by",
#: "run using"), eight of nine slipped through, and AC-2's own criterion
#: without "through" would have too. AC-3 named the fix in terms: which
#: scripts reach the substrate is knowable from the repository, not from the
#: prose. ``_substrate_reaching_script_names`` reads scripts/*.py (a read,
#: which scope.forbidden_paths containing scripts/** does not forbid -- that
#: field governs writes this bead makes, not reads its own check performs)
#: and this regex only needs to recognise the DIRECTIVE shape (SHALL, an
#: optional "be", then a run-family verb) -- the verb list can stay generous
#: per AC-1 because co-occurrence with an actual script name is what carries
#: the signal, not the verb choice. M7's "arch-source-class-backfill.py ...
#: SHALL drop their own HTTP" names the same script with no run-family verb
#: immediately after a SHALL, so it stays clean.
_SCRIPT_RUN_DIRECTIVE_RE = re.compile(
    r"\bSHALL\s+(?:be\s+)?"
    r"(run|executed?|performed|invoked|called|triggered|kicked\s+off)\b",
    re.IGNORECASE,
)


def _substrate_reaching_script_names() -> frozenset[str]:
    """Bare filenames of scripts/*.py that import substrate_client or build
    a Substrate() client -- computed from the repository, not hand-listed.

    Only the top-level scripts/ directory is scanned, not scripts/tests/:
    a spec directs a worker at a script by naming it as it would appear in
    an acceptance criterion (e.g. "arch-source-class-backfill.py"), which is
    always a top-level script in this repo.
    """
    names: set[str] = set()
    if not SCRIPTS_DIR.is_dir():
        return frozenset()
    for path in SCRIPTS_DIR.glob("*.py"):
        try:
            text = path.read_text()
        except OSError:
            continue
        if "substrate_client" in text or "Substrate()" in text:
            names.add(path.name)
    return frozenset(names)


#: Negation cues that flip a live-store reference from a direction into a
#: prohibition or a statement of what was NOT done — "SHALL NOT read the live
#: store" and "read the live store" use the same vocabulary; only these words
#: (and where they fall) tell the two apart.
#:
#: nothing/none/without/neither/nor joined 2026-09-16 (release gate F2 on PR
#: #883): \bnot\b and \bno\b are word-bounded and do not match inside
#: "Nothing" or "None" -- the most natural English form of the D7 prohibition
#: ("Nothing in this bead reads the live store", OPS-121 acceptance[3]) was
#: invisible to the check.
_NEGATION_WORD_RE = re.compile(
    r"\b(not|never|no|nothing|none|without|neither|nor|cannot|won't|wont)\b|n't\b",
    re.IGNORECASE,
)
#: How many words back from a hit to look for a negation cue within the same
#: clause. 8 covers every control case measured 2026-09-16 -- "do not add a
#: fixture that resolves against prod" puts 6 words between "not" and "prod"
#: -- with room to spare.
#:
#: RE-DERIVED 2026-09-17 (release gate DO-NOT-MERGE on PR #888, finding F2):
#: an 8-word window is NOT, on its own, short enough to avoid laundering an
#: unrelated negation -- "This is not a drill, so read the live store and
#: report the count." puts "not" and "live" 6 words apart in the same
#: sentence, and the negation governs "a drill", not "read". No window width
#: fixes this, because the two clauses are grammatically independent; the
#: real bug was ``_clauses`` treating them as one clause. See its docstring
#: below for the fix.
_NEGATION_WINDOW_WORDS = 8


def _clauses(text: str) -> list[str]:
    """Split ``text`` into sentence/clause-sized units.

    A negation and the reference it governs are expected to share a clause —
    splitting on '.', ';', '!', '?' and newlines keeps "THE WORKER SHALL NOT
    ... THE LIVE STORE to satisfy any criterion here" whole while still
    separating "do not add a fixture ...; and no live store" style lists.

    RE-DERIVED 2026-09-17 (release gate DO-NOT-MERGE on PR #888, finding F2):
    that punctuation set missed two everyday ways English opens a new,
    grammatically independent clause that a preceding negation does not
    reach into -- a colon ("No shortcuts: the worker SHALL read the live
    production store directly.") and ", so " ("This is not a drill, so read
    the live store and report the count." / "There is no fixture, so
    measure against the prod store yourself."). In all three, the negation
    modifies a noun in the FIRST clause ("a drill", "shortcuts", "a
    fixture") and the live-store direction is a fresh, unnegated clause
    after the break -- splitting there, rather than widening or narrowing
    the word-count window, is what tells them apart, because no window
    width can distinguish "6 words back, same clause" from "6 words back,
    across a clause boundary".

    ", so " always splits: it is English coordinating-conjunction grammar
    for "here begins a new independent clause", so it carries no ambiguity
    a heuristic needs to resolve. A bare colon is NOT unambiguous the same
    way -- it also introduces an elaboration still governed by what came
    before it, and the corpus has exactly that shape (S53-2's intent:
    "FILING THE FIRST THREE RISKS IS NOT THIS BEAD'S: writing beads to the
    prod store is an operator act ..." -- the colon here explains what "NOT
    THIS BEAD'S" refers to, and an unconditional colon split broke that
    negation's reach, newly refusing a spec that was never in violation).
    A colon only opens a fresh, negation-blocking clause here when what
    follows it — up to the next hard stop — states its OWN mandate (a
    "SHALL"), which is true for "No shortcuts: the worker SHALL read ..."
    and false for S53-2's elaboration.
    """
    return [
        c.strip()
        for c in re.split(
            r"(?<=[.;!?])\s+|:\s+(?=[^.!?;]*?\bSHALL\b)|,\s+so\b\s*|\n+",
            text,
            flags=re.IGNORECASE,
        )
        if c.strip()
    ]


def _is_negated(clause: str, match_start: int) -> bool:
    words = re.findall(r"\S+", clause[:match_start])
    return any(_NEGATION_WORD_RE.search(w) for w in words[-_NEGATION_WINDOW_WORDS:])


def _keyword_hit(clause: str) -> int | None:
    match = _LIVE_STORE_KEYWORD_RE.search(clause)
    return match.start() if match else None


def _substrate_url_hit(clause: str) -> int | None:
    match = _SUBSTRATE_URL_RE.search(clause)
    if match and _URL_READ_VERB_RE.search(clause):
        return match.start()
    return None


def _execution_time_measurement_hit(clause: str) -> int | None:
    # ANCHOR ON THE EXECUTION-TIME TOKEN, NOT THE MEASUREMENT WORD (#911 gate, F1).
    # _MEASUREMENT_WORD_RE includes `the\s+count`, which matches at index 0 of a
    # clause beginning "THE COUNT SHALL NOT be re-measured at execution" -- so
    # _is_negated(clause, 0) inspected zero preceding words and never saw the NOT,
    # which sits AFTER the anchor. Three D7-COMPLIANT prohibitions therefore
    # refused: the sentences a filer writes when they are obeying the rule this
    # check exists to enforce. At #888's head the anchor was the execution-time
    # token, which sits after the NOT, so negation was seen correctly; this is a
    # regression introduced by the narrowing, not a pre-existing gap. The gating
    # pair is unchanged, so the F3 narrowing (a measurement word must also be
    # present) is preserved -- only the reported position moves.
    match = _EXECUTION_TIME_RE.search(clause)
    if match and _MEASUREMENT_WORD_RE.search(clause):
        return match.start()
    return None


def _script_run_directive_hit(clause: str) -> int | None:
    match = _SCRIPT_RUN_DIRECTIVE_RE.search(clause)
    if not match:
        return None
    if any(name in clause for name in _substrate_reaching_script_names()):
        return match.start(1)
    return None


#: Run in order for each clause; the first detector to fire wins (only one
#: hit per clause is recorded, same as before this bead's changes).
_HIT_DETECTORS = (
    _keyword_hit,
    _substrate_url_hit,
    _execution_time_measurement_hit,
    _script_run_directive_hit,
)


def _live_store_hits(field: str, value: str) -> list[tuple[str, str]]:
    """(field, clause) pairs where ``value`` points at the live store with no
    negation cue governing the hit within the same clause."""
    hits: list[tuple[str, str]] = []
    for clause in _clauses(value):
        for detector in _HIT_DETECTORS:
            start = detector(clause)
            if start is not None and not _is_negated(clause, start):
                hits.append((field, clause))
                break
    return hits


def _check_live_store_access(spec: dict[str, Any]) -> None:
    """Refuse a spec whose acceptance/verification/intent text points a
    worker at the live store, unless it explicitly declares why (D7).

    dev.task ad0a0f95 (dev.finding 9c9ccd9f, MEASURED 2026-09-16): its AC-1,
    AC-3 and AC-5 directed the worker at the live production store, and AC-1
    required mutating it. The worker complied — 1019 production arch beads
    were rewritten to source_class='derived' forty-six minutes before the
    commit implementing that code was even authored, because the worker
    inherits the launchd environment and therefore holds the production
    substrate write key (decision record D7, CLAUDE.md). D7 already says a
    spec needing store-shaped evidence must name a committed fixture or
    snapshot instead, and that the outer loop runs any live comparison itself
    at the gate — but that rule lived only in CLAUDE.md, carried by the outer
    loop remembering it at filing time. This is the mechanical carrier.

    THIS IS PROSE CLASSIFICATION, AND IT STILL REFUSES (unlike
    ``structural_behavior_warnings``, which only ever warns). The naive
    version of this check -- a bare keyword match -- was run by hand over
    every pending/doing bead 2026-09-16 and over-fired on all four beads that
    use this vocabulary to PROHIBIT live-store access rather than direct it:
    "no live store ... is needed" (OPS-134), "THE WORKER SHALL NOT READ OR
    WRITE THE LIVE STORE" (OPS-139), "not by a fresh live read" (OPS-140), and
    "do not add a fixture that resolves against prod" (this bead itself). A
    prohibition and a direction share the same words; only where the negation
    falls tells them apart, so this checks for a negation cue governing the
    same clause as the keyword (``_is_negated``) before refusing on it — a
    heuristic, not a parse, but one driven against exactly the specs it must
    not misfire on.

    RECALIBRATED 2026-09-16 (release gate F1-F5 on PR #883, head 9c43521c),
    against ad0a0f95's REAL acceptance text rather than a paraphrase: its
    AC-2 named a substrate-writing script and directed the worker to run it
    ("SHALL be run through scripts/arch-source-class-backfill.py"), and its
    AC-5 demanded a count be "RE-MEASURED AT EXECUTION, NOT INHERITED FROM
    THIS SPEC" -- NEITHER contains "live store", "prod store" or "against
    prod", so #883's keyword list scored zero hits on the two criteria that
    actually caused the incident. The same gate also found bare `prod` firing
    on prose that only names production without directing anyone at it (a
    k8s namespace, a symptom, a past measurement already taken by the outer
    loop) and \bnot\b/\bno\b missing the negation in "Nothing ... reads the
    live store" (OPS-121) -- `prod` now must sit next to a store noun, and
    nothing/none/without/neither/nor joined the negation words.

    RE-DESIGNED 2026-09-17 (release gate DO-NOT-MERGE on PR #888): #883's
    literal "SHALL be run through scripts/*.py" and bare "at execution"
    keyword entries were fitted to ad0a0f95's exact wording -- re-derived by
    the outer loop, eight of nine natural paraphrases of those two criteria
    slipped through, and bare "at execution" and bare `substrate[_ ]?url`
    each fired on unrelated or inverted prose (F1/F3/F4). AC-3 forbids
    chasing this with a bigger keyword list. ``_HIT_DETECTORS`` replaces the
    single alternation with: the STRUCTURAL script-run-directive check
    (``_script_run_directive_hit``, keyed on a script this repo's own
    scripts/*.py shows reaches the substrate, not on prose), and two
    narrowed keyword checks that require a second, co-occurring word in the
    same clause (``_substrate_url_hit``, ``_execution_time_measurement_hit``)
    instead of firing on the bare token alone. ``_clauses`` also now splits
    on ":" and ", so " (finding F2): an 8-word negation window cannot tell
    "6 words back, same clause" from "6 words back, across an independent
    clause a negation doesn't reach into" -- see its docstring.

    The escape hatch is ``live_store_access``: a filer who sets it is
    declaring, in the same spot D7 asks for, that the outer loop runs this
    comparison at the gate rather than the worker. An unsatisfiable check
    gets routed around by rewording rather than fixed, so this one always has
    a way through that does not depend on the prose passing a heuristic. A
    spec that DESCRIBES this check -- quoting the vocabulary it matches on,
    as this bead's own spec does -- is exactly the case no prose heuristic
    can resolve by tuning: describing a prohibition and issuing a direction
    are, again, the same words. Such a spec is expected to use the
    declaration, not to be made to pass by widening the heuristic further.
    """
    if str(spec.get("live_store_access") or "").strip():
        return

    hits: list[tuple[str, str]] = []
    for index, item in enumerate(spec.get("acceptance") or []):
        hits.extend(_live_store_hits(f"acceptance[{index}]", str(item)))

    verification = spec.get("verification") or {}
    for index, command in enumerate(verification.get("commands") or []):
        hits.extend(_live_store_hits(f"verification.commands[{index}]", str(command)))
    if verification.get("description"):
        hits.extend(
            _live_store_hits("verification.description", str(verification["description"]))
        )

    if spec.get("intent"):
        hits.extend(_live_store_hits("intent", str(spec["intent"])))

    if not hits:
        return

    quoted = "\n".join(f"  {field}: {clause!r}" for field, clause in hits)
    raise SystemExit(
        "Refusing to file: this spec's acceptance/verification/intent text "
        "points at the live/production store (decision record D7, CLAUDE.md).\n"
        + quoted
        + "\n\nA dev.task worker inherits the launchd environment and holds the "
        "production substrate write key, so a spec that needs store-shaped "
        "evidence must not send it there (dev.finding 9c9ccd9f). Satisfy this "
        "one of two ways:\n"
        "  1. Point the text above at a committed fixture or snapshot file the "
        "outer loop supplies instead of the live store, or\n"
        "  2. Set live_store_access to a written reason declaring that the "
        "outer loop runs this comparison itself at the gate, not the worker."
    )


#: Heuristic-only vocabulary for structural_behavior_warnings: verbs that read
#: as a NEW gate or refusal path rather than a description of unchanged
#: behaviour. Deliberately small and deliberately over-triggers on prose like
#: "SHALL reject what the live client rejects" (a parity assertion, not a new
#: behaviour) -- that false positive is the cheap, intended kind: a human
#: reads the named criterion and dismisses it in seconds, which is the whole
#: point of a warning that must never block filing.
_BEHAVIOR_MANDATE_VERBS_RE = re.compile(r"\b(refuse|reject|deny|block)\b", re.IGNORECASE)
_SHALL_RE = re.compile(r"\bSHALL\b", re.IGNORECASE)
_SHALL_NOT_RE = re.compile(r"\bSHALL\s+NOT\b", re.IGNORECASE)


def _mandates_new_behavior(criterion: str) -> bool:
    return bool(
        _SHALL_RE.search(criterion)
        and _BEHAVIOR_MANDATE_VERBS_RE.search(criterion)
        and not _SHALL_NOT_RE.search(criterion)
    )


def structural_behavior_warnings(content: dict[str, Any]) -> list[str]:
    """Acceptance criteria that read as a behaviour mandate on a structural bead.

    R2605-5's AC-4 required a duplicate-registration REFUSAL in a bead whose
    own risk_class was 'structural' -- unsatisfiable under Tidy-First, which
    admits no carve-out for a small amount of behaviour change
    (constitution-DRAFT.md:9). The worker complied anyway and the gate
    correctly refused the resulting PR, burning a whole attempt against
    retry_policy's cap of three.

    This is prose classification, which guards.py's own doctrine (see its
    module docstring on RELEASES_WORK_FIELD) treats as unreliable enough to
    never gate on alone -- so, unlike every other check in this file, this one
    is NEVER raised as a refusal. It only returns the flagged criteria, for
    the caller to print as a warning naming exactly what a human should judge.
    """
    if content.get("risk_class") != "structural":
        return []
    return [
        str(item) for item in content.get("acceptance") or [] if _mandates_new_behavior(str(item))
    ]


#: Mirrors dispatch.py:CLOSED_TASK_STATES. Reproduced locally rather than
#: imported — dispatch.py pulls in Temporal client dependencies this filing
#: script has never needed, and the value is three literals, not a moving
#: target. Anything NOT in this set counts as "open" for duplicate-filing
#: purposes, including states this codebase has no literal enum for yet
#: (the bead's "held" language names a concept, not an observed state — a
#: grep across the repo turns up no such state today), so a new non-terminal
#: state added later stays open by default instead of silently falling
#: through an enumerated allowlist. "superseded" joined "done"/"archived" when
#: the dev.task machine gained it as a terminal state: a superseded bead is
#: dead, not merely quiet, and re-filing against its identity is not a
#: duplicate of live work.
CLOSED_TASK_STATES = frozenset({"done", "archived", "superseded"})

#: A GitHub PR (or issue -- the two share one number sequence on GitHub, which
#: is exactly why a match here is never trusted on its own) named in prose,
#: e.g. "present at #834's head". Requires 2+ digits: this repo's PR numbers
#: are already in the many hundreds (see the commit log), so a lone "#3"/"#7"
#: reads far more often as an ordinal ("step #3 of the migration") than a
#: GitHub reference, and admitting single digits would turn this into exactly
#: the "bare '#NNN' substring sweep" the bead this check derives from
#: (dev.finding 30784136) says must not happen.
#:
#: KNOWN FALSE POSITIVES, and what actually happens to each -- corrected
#: 2026-09-14 after the #853 release gate measured them, because the earlier
#: wording here claimed more than the code does:
#:
#:   1. A bead quoting a past, already-landed PR ("... fixed the leak
#:      (#822)"). RESOLVED SILENTLY: gh answers, _lookup_pr_state returns
#:      MERGED/CLOSED, and no warning is emitted. This one the comment got
#:      right.
#:   2. AN ISSUE NUMBER. NOT silent. GitHub shares one number sequence
#:      between issues and PRs, and `gh pr view <issue>` EXITS 1
#:      ("Could not resolve to a PullRequest"), so _lookup_pr_state raises,
#:      the caller catches it, and a "could not be evaluated" WARNING is
#:      emitted naming that number. This repo has real open two-digit issues
#:      -- #35, #36, #37, #38 (the SUBSTRATE_API_KEY scoping set) and #94 --
#:      and a spec citing one of them as context WILL produce a spurious
#:      warning. Failing loud rather than silent is PRIN-015-shaped and is
#:      why this is tolerable, but it IS noise, and a warn-only control dies
#:      when its operator learns the warnings are unreliable.
#:   3. Any other "#NN+" that is neither: an RFC number, a hex colour
#:      (#123456), a numbered step. Same path as (2) -- a
#:      "could not be evaluated" warning, not silence.
#:
#: The check warns and never blocks (see pr_prerequisite_warnings), so every
#: case above costs attention, not a refused filing. If that noise ever
#: matters, the fix is to distinguish "GitHub answered: no such PR" (should be
#: silent) from "GitHub could not be reached" (should warn) -- they are the
#: same branch today.
_PR_REFERENCE_RE = re.compile(r"(?<![\w#])#(\d{2,})(?!\w)")


def _referenced_pr_numbers(spec: dict[str, Any]) -> list[int]:
    """PR numbers named in a spec's own intent/acceptance prose, first-seen order."""
    text = "\n".join(
        [str(spec.get("intent") or ""), *[str(item) for item in (spec.get("acceptance") or [])]]
    )
    numbers: list[int] = []
    for match in _PR_REFERENCE_RE.finditer(text):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return numbers


_PR_URL_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _bead_id_for_pr(number: int, tasks: list[dict[str, Any]]) -> str | None:
    """The dev.task bead whose recorded pr_url names PR ``number``, if any."""
    for task in tasks:
        pr_url = str((task.get("content") or {}).get("pr_url") or "")
        match = _PR_URL_NUMBER_RE.search(pr_url)
        if match and int(match.group(1)) == number:
            return str(task.get("id") or "") or None
    return None


def _lookup_pr_state(number: int) -> str:
    """Ask GitHub whether PR ``number`` is OPEN, CLOSED, or MERGED.

    Reuses dispatch.lookup_pull_request (``gh pr view``) rather than a second
    implementation of "ask GitHub about a PR" (PRIN-005; dispatch.py already
    has one, used by pull_requests_for_branch and clear_stale_branch_for_retry).
    Raises whatever dispatch/gh raises -- missing FACTORY_REPO
    (dispatch.Config.from_env()), gh unreachable, a number naming no PR at
    all. The caller treats every raise identically, as "could not evaluate"
    (PRIN-015: a control that cannot evaluate its question fails closed, and
    this check's closed state is silence, never a block).
    """
    cfg = dispatch.Config.from_env()
    status = dispatch.lookup_pull_request(str(number), cfg)
    return status.state.upper()


def pr_prerequisite_warnings(
    spec: dict[str, Any], content: dict[str, Any], tasks: list[dict[str, Any]]
) -> list[str]:
    """A spec names an open PR as its prerequisite in prose but declares no
    predecessor_bead_ids fence -- guards.is_runnable reads only that field,
    never this prose, so a bead in this shape reads as dispatchable against a
    main that may still lack the symbol its own work needs (dev.finding
    30784136: 3b884fe3, ad0a0f95, 5bb23acc -- three beads, three 60-minute
    worker budgets spent on work that could not have succeeded, one caught
    only by a hand sweep before it ran at all).

    Warns only, never refuses -- the same posture as this module's
    structural_behavior_warnings, and for the same reason: of the three
    pending beads matching this signature when the finding was measured, two
    were benign (a deliberate cross-reference to another bead's work, and a
    self-reference to the bead's own already-closed PR). A refusal here would
    have blocked both correct filings to catch the one error a human
    dismisses in seconds.
    """
    if content.get("predecessor_bead_ids"):
        return []

    warnings: list[str] = []
    for number in _referenced_pr_numbers(spec):
        try:
            state = _lookup_pr_state(number)
        except Exception as exc:  # noqa: BLE001 - PRIN-015: degrade to silence, never block or crash
            warnings.append(
                f"WARNING: this spec names #{number}; whether it is an open "
                "pull request could not be evaluated "
                f"({type(exc).__name__}: {exc}). Filing continues -- this "
                "check degrades to silence rather than blocking. If "
                f"#{number} is a prerequisite PR, set "
                "content.predecessor_bead_ids to the bead it belongs to once "
                "you can confirm its state."
            )
            continue

        if state != "OPEN":
            continue

        bead_id = _bead_id_for_pr(number, tasks)
        bead_clause = (
            f"the bead it belongs to is {bead_id!r}"
            if bead_id
            else "the bead it belongs to could not be resolved from any "
            "recorded pr_url"
        )
        warnings.append(
            f"WARNING: this spec names #{number}, which is currently OPEN, "
            "but carries no predecessor_bead_ids -- guards.is_runnable reads "
            "only that field, never this prose, so this bead would be "
            f"dispatchable against a main that may still lack #{number}'s "
            f"work. If #{number} is a prerequisite, set "
            f"content.predecessor_bead_ids ({bead_clause}). This is a "
            "warning, not a refusal -- ignore it if the reference is "
            "unrelated to ordering (e.g. naming a different bead's work)."
        )
    return warnings


#: States CLOSED_TASK_STATES contains that are NOT also a successful landing
#: (guards.SUCCESSFUL_TERMINAL_STATES) -- reusing the guard's own definition
#: of "landed" rather than restating it here (PRIN-005: one rule, no copy). A
#: predecessor found in one of these is not "waiting", it is dead:
#: dev_task_contract.DEV_TASK_STATE_MACHINE has no outbound edge from either
#: state, so guards.ordering_block_reason can never see it reach "done" and
#: will report "waiting for predecessor ... to land" forever -- the exact
#: failure mode 2026-08-20's fix at guards.py:609-621 exists to catch, but
#: only when a reverse pointer (some other task's source_bead_ids) happens to
#: exist. OPS-107's own predecessor was hand-superseded with source_bead_ids=[]
#: recorded on the note instead, so that reverse index found nothing and the
#: guard fell through to the generic "waiting" message on every drain for
#: roughly twelve hours.
_NEVER_LANDS_STATES = CLOSED_TASK_STATES - guards.SUCCESSFUL_TERMINAL_STATES


def _check_predecessor_supersession(
    predecessor_ids: list[str], tasks: list[dict[str, Any]]
) -> None:
    """Refuse a predecessor id that can never land, whether or not a reverse
    pointer to its replacement was ever recorded.

    A dependency edge onto a bead that is structurally incapable of landing is
    a contradiction filed in advance. 2026-08-20: the #457 batch pointed
    S24-B1 and FA-S33 at held originals instead of their landed replacements,
    and nothing caught it until six pending beads sat behind a wait that could
    never clear. Catching it here, at filing time, means the author is told to
    point at the live work instead of discovering it a day later in a drain
    log.

    The original version of this check only ever consulted
    ``guards.superseding_task_ids`` -- the reverse index over
    ``source_bead_ids`` -- and never looked at the predecessor's own state.
    That is exactly the shape that let OPS-107 through: its predecessor
    (79a6fba7) was superseded by hand 39 seconds after OPS-107 was filed,
    recording no reverse pointer, so the reverse index found nothing and this
    check fired clean while the bead it passed sat unrunnable for the rest of
    its life. This version resolves each predecessor id against ``tasks``
    directly and refuses on its own recorded state, using the reverse index
    only to name a replacement when one happens to exist.
    """
    tasks_by_id = {
        str(candidate.get("id") or "").strip(): candidate
        for candidate in tasks
        if str(candidate.get("id") or "").strip()
    }
    for predecessor_id in predecessor_ids:
        predecessor = tasks_by_id.get(predecessor_id)
        if predecessor is None:
            raise SystemExit(
                f"predecessor_bead_ids names {predecessor_id}, which names no "
                "dev.task.\n"
                "Point predecessor_bead_ids at a bead that actually exists, or "
                "drop it if the ordering constraint no longer applies."
            )

        state = str(predecessor.get("state") or "").strip()
        if state not in _NEVER_LANDS_STATES:
            continue

        superseded_by = guards.superseding_task_ids(predecessor, tasks)
        if superseded_by:
            raise SystemExit(
                f"predecessor_bead_ids names {predecessor_id}, which is "
                f"{state} and can never land.\n"
                "Point predecessor_bead_ids at its replacement instead: "
                + ", ".join(superseded_by)
            )
        raise SystemExit(
            f"predecessor_bead_ids names {predecessor_id}, which is {state} "
            "and can never land, with no replacement recorded against it.\n"
            "Drop it from predecessor_bead_ids, or point predecessor_bead_ids "
            "at a different bead if the ordering dependency is on something "
            "else."
        )


def _check_not_own_supersession_target(
    predecessor_ids: list[str], superseded_task: dict[str, Any] | None
) -> None:
    """Refuse a predecessor id that this very filing is about to supersede.

    This guards a shape, not a replay of OPS-107's actual history: OPS-107
    (bead 70b01c44) named 79a6fba7 in predecessor_bead_ids -- 'must LAND
    before I can run' -- while 79a6fba7 was still pending, which was fine on
    its own. What actually happened is that 79a6fba7 was superseded by hand
    roughly 39 seconds later, outside this script, recording no reverse
    pointer (source_bead_ids=[] on the note) -- the gap
    _check_predecessor_supersession, above, now closes by consulting the
    predecessor's own state directly instead of only the reverse index.

    This check exists for the narrower contradiction that fix still cannot
    see coming: a SINGLE filing that names a bead in predecessor_bead_ids
    ('must land first') and also passes that same bead to --supersede
    ('replaces it'). _check_predecessor_supersession cannot catch this shape
    because it runs against ``tasks`` read before this filing's own supersede
    transition happens, so at that moment the named predecessor is still
    merely pending, not yet superseded, and its check correctly finds nothing
    wrong. The contradiction only exists once ``_check_not_a_duplicate`` has
    resolved which bead this filing supersedes, which is why this check runs
    after that resolution rather than being folded into the earlier one.
    """
    if superseded_task is None:
        return
    target_id = str(superseded_task.get("id") or "").strip()
    if target_id and target_id in predecessor_ids:
        raise SystemExit(
            f"predecessor_bead_ids names {target_id}, but --supersede "
            f"{target_id} means this filing REPLACES it -- a bead cannot "
            "require its own replacement to land first.\n"
            f"Drop {target_id} from predecessor_bead_ids (its work is being "
            "superseded, not waited on), or supply --supersede for a "
            "different bead if the ordering dependency is on something else."
        )


def _check_source_bead_ids_not_live_tasks(
    source_ids: list[str],
    predecessor_ids: list[str],
    tasks: list[dict[str, Any]],
    supersede: str | None = None,
) -> None:
    """Refuse a spec-supplied source_bead_ids entry that names a live dev.task.

    guards.superseding_task_ids reverse-indexes the board on this field alone:
    any task naming bead X in its source_bead_ids makes X read as superseded,
    which guards.is_runnable then refuses and dispatch.py --requeue reports as
    refused reason=superseded_by — regardless of how that value got written.
    --supersede earns that claim through three checks (identity match in
    _check_not_a_duplicate, the SUPERSEDABLE_STATES check, and
    _check_not_own_supersession_target's predecessor-contradiction check); a
    spec's own source_bead_ids field (build_content, above) runs none of
    them. Measured 2026-09-16: a failed, unrequeueable bead falsely read as
    superseded, two beads with open merge-ready PRs falsely read as
    superseded, and the exact contradiction
    _check_not_own_supersession_target exists to catch — because that guard
    is only ever reached from the --supersede resolution path, which this
    field bypasses entirely.

    Rather than re-deriving --supersede's three checks against an id list
    that carries no claim about the filing's own identity, a live dev.task id
    is refused here outright: supersession is an operator act that belongs on
    the command line where it is auditable (--supersede), not a value that
    can arrive silently embedded in a hand-written spec. An id that resolves
    to nothing in ``tasks`` — a dev.finding id, or an already-closed dev.task
    — is unaffected; ``sub.list_tasks()`` only ever returns dev.task beads, so
    non-task provenance can never collide with this check.

    ``supersede`` narrows this by exactly one id: the one named on the same
    filing's ``--supersede`` flag, which is already destined to become
    ``source_bead_ids``' own auditable path (``_check_not_a_duplicate`` and
    friends validate it, and ``file_spec`` appends it after this check
    returns). Naming that same id up front in the spec's own
    ``source_bead_ids`` is not the silent, unaudited write this check exists
    to catch -- it is the operator declaring in the spec what --supersede is
    about to make true anyway. Every OTHER id in ``source_ids`` is still
    checked exactly as before, including a different live dev.task named
    alongside a --supersede for something else.
    """
    tasks_by_id = {
        str(candidate.get("id") or "").strip(): candidate
        for candidate in tasks
        if str(candidate.get("id") or "").strip()
    }
    for source_id in source_ids:
        if supersede and source_id == supersede:
            continue
        task = tasks_by_id.get(source_id)
        if task is None:
            continue
        state = str(task.get("state") or "").strip()
        if state in CLOSED_TASK_STATES:
            continue

        contradiction = ""
        if source_id in predecessor_ids:
            contradiction = (
                f"\nIt is also named in predecessor_bead_ids -- 'must land "
                f"before I can run' and 'this filing replaces it' cannot "
                f"both be true of {source_id}."
            )
        raise SystemExit(
            f"source_bead_ids names {source_id}, a live dev.task (state="
            f"{state or '(missing state)'})." + contradiction + "\n"
            "guards.superseding_task_ids reverse-indexes source_bead_ids: "
            f"naming a live dev.task there marks {source_id} as superseded "
            "on every future read, refused by guards.is_runnable and by "
            "dispatch.py --requeue (reason=superseded_by), with none of the "
            "checks --supersede runs (identity match, legal-source-state, "
            "predecessor contradiction).\n"
            f"If this filing replaces {source_id}, file with --supersede "
            f"{source_id} instead -- it runs those checks and writes "
            "source_bead_ids itself once they pass.\n"
            "If it is not a replacement, drop it from source_bead_ids and "
            "record the relationship as a derived_from or found_by link "
            "instead (BEAD_LINK_TYPES, apps/substrate/src/bead_rules.py) -- "
            "or, if it names a dev.finding rather than a dev.task, "
            "source_bead_ids already accepts it unchanged."
        )


def _check_derived_from_findings(ids: list[str], sub: Substrate) -> None:
    """Refuse a derived_from_finding_ids entry absent from the newest page of
    dev.finding beads.

    ``BeadStore`` has no read of one bead by id, and ``substrate.py`` is out
    of scope for this check, so each id is resolved by membership in a
    single newest-first page of 500 dev.finding beads -- a page read, to be
    replaced by a by-id read when one exists (R26.13/O-2's ratchet will list
    it). The store's own existence check on ``add_link``'s target (file_spec,
    below) is the backstop for a page that has gone stale between this read
    and that write -- including the case where ``ids`` names a dev.task
    rather than a dev.finding, which is simply never a member of this page.

    Callers guard the empty-list case themselves (mirroring
    ``_check_predecessor_supersession``'s call site, above) rather than this
    function doing so internally, so that the one ``sub.list_beads`` call
    stays visibly conditional to a reader -- and to
    ``tests/test_store_double_call_surface.py``'s static reachability
    analysis -- on the call site, not hidden behind an early return here.
    """
    findings = sub.list_beads("dev", "finding", limit=500)
    known = {str(f.get("id") or "") for f in findings}
    for finding_id in ids:
        if finding_id not in known:
            raise SystemExit(
                f"derived_from_finding_ids names {finding_id!r}, which is "
                "not among the newest 500 dev.findings.\n"
                "Point derived_from_finding_ids at a finding that exists, or "
                "drop it if the relationship no longer applies."
            )


#: States from which a bead may legally become "superseded" (mirrors the
#: substrate's declared edges, apps/substrate/src/routes.py:STATE_MACHINES).
#: A bead already "doing" or in "review" is live work, not dead work — the
#: machine has no edge for it, and --supersede must refuse before filing a
#: new bead against it rather than leave a note claiming death for work that
#: might still land.
SUPERSEDABLE_STATES = frozenset({"pending", "failed"})


def _spec_identity(spec_arg: str) -> str | None:
    """Stable identity for the spec being filed, or None when it has no path.

    Specs live at fixed repo paths (``apps/factory-dispatcher/tasks/*.json``),
    which is exactly the identity a duplicate filing shares with the bead it
    duplicates — refiling the same file twice is the incident this guards
    against (2026-08-21: #477/#478 duplicated S24-P1/P2). A spec piped via
    stdin carries no such anchor and is deduplicated on title alone.
    """
    if spec_arg == "-":
        return None
    repo_root = Path(__file__).resolve().parents[2]
    resolved = Path(spec_arg).resolve()
    try:
        return str(resolved.relative_to(repo_root))
    except ValueError:
        return str(resolved)


def _attempts_remaining(task_id: str, sub: Substrate) -> bool:
    notes = sub.list_notes(task_id)
    attempts = len(guards.prior_failures(notes))
    return attempts < retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS


def _is_open(task: dict[str, Any], sub: Substrate) -> bool:
    """Whether ``task`` is live enough to make a same-identity filing a duplicate.

    A failed task is only open while it still has a retry attempt left — one
    that exhausted its budget never landed and nothing is retrying it, so a
    fresh filing against the same identity is a new attempt, not a duplicate
    of a live one.
    """
    state = str(task.get("state") or "").strip()
    if state in CLOSED_TASK_STATES:
        return False
    if state == "failed":
        return _attempts_remaining(str(task.get("id") or ""), sub)
    return True


def _matches_spec_identity(
    task: dict[str, Any], spec_identity: str | None, title: str
) -> bool:
    """Path and/or title — either signal alone is enough to call it the same spec.

    Path is the primary signal (see ``_spec_identity``); title is checked
    unconditionally, not only as a fallback, because it also catches beads
    filed before ``spec_identity`` existed as a field and specs filed via
    stdin, and it is exactly the signal that would have caught the actual
    2026-08-21 incident even without a recorded path.
    """
    content = task.get("content") or {}
    if spec_identity and content.get("spec_identity") == spec_identity:
        return True
    return bool(title) and content.get("title") == title


def find_duplicate_tasks(
    tasks: Iterable[dict[str, Any]],
    spec_identity: str | None,
    title: str,
    sub: Substrate,
) -> list[dict[str, Any]]:
    """Open dev.task beads that already carry this spec's identity."""
    return [
        task
        for task in tasks
        if _matches_spec_identity(task, spec_identity, title) and _is_open(task, sub)
    ]


def _check_not_a_duplicate(
    tasks: list[dict[str, Any]],
    spec_identity: str | None,
    title: str,
    sub: Substrate,
    supersede: str | None,
) -> dict[str, Any] | None:
    """Refuse filing when this spec's identity already has a live dev.task.

    2026-08-21: S24-P1 and S24-P2 were each filed twice; two workers spent
    full budgets producing PRs the gate then had to read, judge, and kill
    (#477/#478, superseded by #479/#480). The refusal belongs here, at the
    mouth of the pipeline (PRIN-008), not two worker-budgets later at the
    gate.

    Returns the bead being superseded when ``--supersede`` validates, so the
    caller can record both the ordering edge and the note; returns None when
    there is nothing to supersede (no duplicate found, no flag given).
    """
    duplicates = find_duplicate_tasks(tasks, spec_identity, title, sub)

    if not duplicates:
        if supersede:
            raise SystemExit(
                f"--supersede {supersede} was given, but no live dev.task matches "
                "this spec's identity — nothing to supersede."
            )
        return None

    if not supersede:
        named = "; ".join(
            f"{task.get('id')} (state={task.get('state') or '(missing state)'})"
            for task in duplicates
        )
        raise SystemExit(
            "this spec's identity already has a live dev.task: " + named + ".\n"
            "Filing it again would spend a second worker's budget on the same "
            "intent (PRIN-008). If this is a deliberate re-file, name the bead "
            "it replaces with --supersede <bead-id>."
        )

    target = next(
        (task for task in duplicates if str(task.get("id") or "") == supersede), None
    )
    if target is None:
        raise SystemExit(
            f"--supersede {supersede} does not match this spec's identity. "
            "Live duplicates found: "
            + "; ".join(str(task.get("id")) for task in duplicates)
        )

    target_state = str(target.get("state") or "").strip()
    if target_state not in SUPERSEDABLE_STATES:
        raise SystemExit(
            f"--supersede {supersede} is in state={target_state or '(missing state)'}; "
            "only a pending or failed dev.task can be marked superseded — a bead "
            "already doing or in review is still live work, not dead work. Let "
            "it land or fail first."
        )
    return target


def _is_spec_identity_conflict(exc: Exception) -> bool:
    """Whether ``exc`` is the store's 409 for migration 0008's uniqueness
    guarantee on ``content.spec_identity`` (OPS-68) — routes.py's
    ``_is_spec_identity_duplicate_error`` raised this server-side when a
    second filing's create landed while ours was still in flight, in the gap
    ``_check_not_a_duplicate``'s own read-then-check cannot close alone.
    """
    if getattr(exc, "status", None) != 409:
        return False
    body = getattr(exc, "body", "") or ""
    return "spec_identity_duplicate" in str(body)


def _repoint_dependents(
    superseded_id: str,
    replacement_id: str,
    tasks: list[dict[str, Any]],
    sub: Substrate,
) -> tuple[str, ...]:
    """Repoint every live dependent of ``superseded_id`` at ``replacement_id``.

    Observed 2026-08-24 (OPS-13): re-filing S33-B2 with --supersede replaced
    f9fc8ff9 with 658a1d8a and recorded the supersession correctly on both
    beads, but four dependents still named f9fc8ff9 in predecessor_bead_ids —
    each read by is_runnable as "predecessor is held (superseded)" forever,
    with nothing saying so, until they were repointed by hand.

    Supersession is a graph operation, not a bead operation: the replacement
    inherits the dependents automatically, here, rather than leaving them
    silently pointed at a bead that will never land.
    """
    dependents = guards.dependents_of(superseded_id, tasks)
    if not dependents:
        return ()

    repointed: list[str] = []
    failed: list[str] = []
    for dependent in dependents:
        content = dict(dependent.get("content") or {})
        content[guards.PREDECESSOR_BEAD_IDS_FIELD] = list(
            guards.repoint_predecessor_ids(
                guards.predecessor_bead_ids(dependent), superseded_id, replacement_id
            )
        )
        try:
            sub.patch_content(dependent["id"], content, CREATED_BY)
        except Exception as exc:
            failed.append(f"{dependent['id']} ({exc})")
        else:
            repointed.append(dependent["id"])

    if failed:
        raise SystemExit(
            f"superseded {superseded_id} and repointed {len(repointed)}/"
            f"{len(dependents)} dependent(s), but failed to repoint: "
            + ", ".join(failed)
            + ". These beads still name a dead predecessor and must be "
            f"repointed by hand: point predecessor_bead_ids at {replacement_id} "
            f"instead of {superseded_id}."
        )
    return tuple(repointed)


def _check_pristine_verification(content: dict[str, Any]) -> None:
    """Refuse to file a spec whose declared verification cannot be dispatched.

    Observed 2026-08-23 (OPS-6): dev.task 05262e55's declared verification
    could never pass — apps/substrate/src/database.py raised at import for
    want of environment variables the spec never set — and nothing caught it
    at filing. The dispatcher's own preflight step runs exactly this check
    (dispatch.verify_pristine_commands calls the same
    dispatch.verify_declared_commands preflight_activity does), it just runs
    three hours and twelve clone bootstraps too late. Filing is where every
    other structural defect in a spec is already caught (PRIN-008); this one
    belongs here too.

    ``verification.expect_pristine_failure`` changes what "cannot be
    dispatched" means for a red-first command (a reproduction, or a red test
    for a fix this same task's work will make): the dispatcher's own
    preflight (dispatch.dispatch_once, dispatch.expects_pristine_failure) now
    requires such a command to FAIL before the worker runs and refuses it as
    already-satisfied if it already passes. Filing enforces the identical
    expectation, so a spec is never accepted into a state dispatch can only
    ever refuse.

    OPS-7's ``--waive-pristine-verification`` is gone. It recorded a reason
    and let filing through unconditionally, but the dispatcher's preflight
    never read that reason — the escape hatch minted beads filing accepted
    that dispatch could never run (this file's own history, 2026-08-23). Its
    one legitimate use, a red-first command, now has a real mechanism instead
    of an unread waiver.
    """
    commands = list((content.get("verification") or {}).get("commands") or [])
    if not commands:
        return

    report = dispatch.verify_pristine_commands(commands)
    expect_failure = bool(
        (content.get("verification") or {}).get("expect_pristine_failure")
    )

    if expect_failure:
        if report.could_not_start:
            raise SystemExit(
                "Refusing to file: declared verification could not even start "
                "against an unmodified clone of main.\n"
                + dispatch.pristine_verification_failure_reason(report)
            )
        if report.ok:
            raise SystemExit(
                "Refusing to file: verification.expect_pristine_failure is set, but "
                "declared verification already passes against an unmodified clone of "
                "main.\n"
                + dispatch.pristine_already_satisfied_reason(report)
                + "\n\nThe work this task exists to do already appears done — file "
                "against the PR/commit that did it instead, or drop "
                "expect_pristine_failure if this task is not actually red-first."
            )
        return

    if report.ok:
        return
    raise SystemExit(
        "Refusing to file: declared verification does not pass against an "
        "unmodified clone of main.\n"
        + dispatch.pristine_verification_failure_reason(report)
        + "\n\nIf this is deliberate (e.g. a reproduction test for a bug this "
        "task's own work will fix), set verification.expect_pristine_failure to "
        "true instead — the dispatcher's preflight enforces the same expectation "
        "at claim time, and the note above already fired against the same failure."
    )


#: Root-cause track control C-4 (docs/plans/2026-09-30-root-cause-track-
#: security-findings-2026-09-26.md): the one declared list a spec's
#: scope.paths is checked against (``_paths_intersect``) to decide whether it
#: is a "boundary spec" that must name its principal and reach. This file is
#: the sole copy; it is not itself a boundary spec under its own list.
BOUNDARY_PATHS: tuple[str, ...] = (
    "apps/factory-dispatcher/dispatch.py",  # spawns workers; holds Config/credentials
    "apps/factory-dispatcher/containment.py",  # clone-content containment (RC-3)
    "apps/factory-dispatcher/process_env.py",  # builds every spawned child's env (RC-2)
    "apps/factory-dispatcher/launchd_agent.py",  # rendered launch commands; a0166920's route
    "apps/factory-dispatcher/scanner.py",  # spawn site
    "apps/factory-dispatcher/tunnel_keeper.py",  # spawn site
    "apps/factory-dispatcher/workflow_run_health.py",  # spawn site
    "apps/factory-dispatcher/deployed_revision.py",  # spawn site
    "apps/factory-dispatcher/cluster_health.py",  # spawn site
    "apps/factory-dispatcher/probe_console_surface.py",  # spawn site
    "apps/factory-dispatcher/activities/dispatch_steps.py",  # spawn site; dd648709's narrowing lived here
    "apps/factory-dispatcher/activities/ea_observation.py",  # spawn site
    "apps/factory-dispatcher/launchd/**",  # launchd templates rendered into spawn commands
    "apps/factory-dispatcher/activities/merge_on_verdict.py",  # merge authority
    "apps/factory-dispatcher/worker.py",  # holds the env every spawned child inherits
    "apps/factory-dispatcher/worker_identity.py",  # builds the sudo launch call and the worker's credential payload (part B, design §8 row B1)
    "apps/factory-dispatcher/worker_launcher.py",  # runs as the worker user; Invariant L lives here (part B, design §8 row B2)
    "apps/factory-dispatcher/handoff.py",  # the only dispatcher-side git boundary onto W/repo in split mode (dev.finding 79db3113, design §5)
    "apps/mcp-hub/src/openapi_app.py",  # caller authentication; 52deaf69 lived here
    "apps/mcp-hub/src/access_auth.py",  # caller authentication (check_scope); 4208f26f lived here
    "apps/substrate/src/routes.py",  # caller authentication; substrate API-key check
    "infrastructure/**",  # stands in for ExternalSecret/Service/Ingress manifests
)


def _normalize_boundary_token(token: str) -> str:
    """Strip a leading ``./`` and a trailing ``/``, ``/**`` or ``**``."""
    token = str(token).strip()
    if token.startswith("./"):
        token = token[2:]
    if token.endswith("/**"):
        token = token[:-3]
    elif token.endswith("**"):
        token = token[:-2]
    if token.endswith("/"):
        token = token[:-1]
    return token


def _is_component_prefix(prefix: str, path: str) -> bool:
    """``prefix`` is ``path`` itself or an ancestor on WHOLE path components --
    a raw string prefix would wrongly match ``dispatch.py.bak`` against
    ``dispatch.py``, which must stay a miss."""
    return path == prefix or path.startswith(prefix + "/")


def _paths_intersect(scope_path: str, pattern: str) -> bool:
    """Equal, a component-wise prefix either way, or an fnmatch either way
    (a scope path may itself carry a glob, e.g. ``apps/mcp-hub/src/*.py``)."""
    a = _normalize_boundary_token(scope_path)
    b = _normalize_boundary_token(pattern)
    if a == b or _is_component_prefix(a, b) or _is_component_prefix(b, a):
        return True
    return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def _first_boundary_intersection(scope_paths: Iterable[Any]) -> tuple[str, str] | None:
    """First (scope path, pattern) hit, scope.paths order then BOUNDARY_PATHS
    order -- deterministic, so the refusal always names the same pair."""
    for scope_path in scope_paths:
        for pattern in BOUNDARY_PATHS:
            if _paths_intersect(str(scope_path), pattern):
                return str(scope_path), pattern
    return None


def _check_boundary_principal(spec: dict[str, Any]) -> None:
    """Refuse a boundary spec with no declared principal/reach (C-4): the
    root-cause track's eleven findings share a process cause, that no spec had
    to state which identity a change runs as or which credentials it can
    reach, so each reach was found by an audit after the spec that created it
    was already filed. ``principal``: a non-empty string naming the identity
    the changed code runs as. ``reaches``: a non-empty list of non-empty
    strings naming the credentials it can use -- ``["none"]`` says none."""
    scope_paths = (spec.get("scope") or {}).get("paths") or []
    hit = _first_boundary_intersection(scope_paths)
    if hit is None:
        return
    scope_path, pattern = hit

    principal = spec.get("principal")
    reaches = spec.get("reaches")

    missing: list[str] = []
    if not isinstance(principal, str) or not principal.strip():
        missing.append("principal")
    if (
        not isinstance(reaches, list)
        or not reaches
        or not all(isinstance(r, str) and r.strip() for r in reaches)
    ):
        missing.append("reaches")

    if not missing:
        return

    raise SystemExit(
        f"Refusing to file: scope.paths entry {scope_path!r} touches boundary "
        f"path {pattern!r} (root-cause track control C-4), so this spec must "
        "declare " + " and ".join(missing) + ".\n"
        "Add principal: a non-empty string naming the OS user or service "
        "identity the changed code runs as.\n"
        "Add reaches: a non-empty list of non-empty strings naming the "
        'credentials that principal can use -- use ["none"] to say explicitly '
        "that it reaches none."
    )


def _check_no_marker_fields(spec: dict[str, Any]) -> None:
    """Refuse a spec carrying class_of_service, expedite_reason or
    expedite_until (R26.12 B16).

    These three mark a dev.task's class of service -- the emergency lane
    B17's ``--expedite`` operator verb will set after filing, and B7's rank
    key honours only when an operator note attests it. Nothing the factory
    files may set its own class of service: a spec is unattended machine
    output (hand-written, scanner-generated, or board-submitted) all the
    same, and none of those are the operator. ``build_content`` never copies
    these keys into ``content`` -- they are absent from its field whitelist
    below -- so a spec carrying one at the top level is the only way one
    could ever reach a bead, which is exactly what this refuses.
    """
    for field in MARKER_FIELDS:
        if field in spec:
            raise SystemExit(
                f"spec carries {field!r}, which is set only by the operator "
                "verb that marks a task's class of service (B17), never by "
                "a filed spec.\n"
                f"Drop {field!r} from the spec and file normally; an "
                "operator can mark the resulting bead emergency after it "
                "exists, if that is warranted."
            )


def build_content(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise SystemExit(f"spec must be a JSON object; got {type(spec).__name__}")

    _check_no_marker_fields(spec)

    missing = [key for key in REQUIRED if not spec.get(key)]
    if missing:
        raise SystemExit(f"spec is missing required field(s): {', '.join(missing)}")

    _check_boundary_principal(spec)
    _check_traceability(spec)
    _check_worker_hint(spec)
    _check_live_store_access(spec)

    scope = dict(spec["scope"])
    if not scope.get("paths"):
        raise SystemExit("scope.paths must list at least one path")
    forbidden = list(scope.get("forbidden_paths") or [])
    for always in FORBIDDEN_ALWAYS:
        if always not in forbidden:
            forbidden.append(always)
    scope["forbidden_paths"] = forbidden

    # Checked against FORBIDDEN_ALWAYS specifically, not the full merged
    # `forbidden` list: a spec author's own forbidden_paths declaration (an
    # intentionally over-broad safety net, e.g. PROBE-1's scope narrowed to
    # one file while forbidding all of apps/**) is not the charter fence this
    # refusal exists to enforce, and must not be refused for overlapping
    # itself.
    for scope_path in scope["paths"]:
        if guards.path_forbidden(str(scope_path), FORBIDDEN_ALWAYS):
            raise SystemExit(
                f"scope.paths entry {scope_path!r} is forbidden_always "
                f"({', '.join(FORBIDDEN_ALWAYS)}) -- the factory may never "
                "propose writes there, regardless of declared scope.\n"
                "Drop it from scope.paths."
            )

    content: dict[str, Any] = {
        "lane": spec["lane"],
        "title": spec["title"],
        "intent": spec["intent"],
        "context_refs": spec.get("context_refs") or [],
        "source_bead_ids": spec.get("source_bead_ids") or [],
        "predecessor_bead_ids": spec.get("predecessor_bead_ids") or [],
        "acceptance": spec["acceptance"],
        "verification": spec.get("verification") or DEFAULT_VERIFICATION,
        "scope": scope,
        "risk_class": spec["risk_class"],
        "budget": spec.get("budget") or DEFAULT_BUDGET,
    }
    # The traceability contract (schemas.py DevTaskContent, 2026-08-02).
    #
    # This dict is a whitelist, so a field absent from it is dropped silently
    # with exit code 0 — which is exactly what happened to these three from the
    # day they were added until 2026-08-03. dev.task 93a1ab40 was filed from a
    # spec carrying two NFRs and an arch_impact, and came back with
    # requirement_refs=None, nfrs=[], arch_impact=None, while
    # scope.forbidden_paths showed the auto-appended .github/workflows/**
    # proving this function had run. The contract had no producer, so the
    # renderer that puts it in the PR body was unreachable in practice.
    #
    # Passed through only when present: the substrate defaults them, and
    # writing an empty list is not the same claim as writing nothing.
    for field in ("requirement_refs", "nfrs", "arch_impact"):
        if spec.get(field):
            content[field] = spec[field]
    # Inspectable after the fact rather than only at the moment of filing.
    if str(spec.get("requirement_refs_waived") or "").strip():
        content["requirement_refs_waived"] = spec["requirement_refs_waived"]

    if spec.get("worker_hint"):
        content["worker_hint"] = spec["worker_hint"]
    if str(spec.get("live_store_access") or "").strip():
        content["live_store_access"] = spec["live_store_access"]
    if str(spec.get("principal") or "").strip():
        content["principal"] = spec["principal"]
    if spec.get("reaches"):
        content["reaches"] = spec["reaches"]
    derived_from_finding_ids = spec.get("derived_from_finding_ids")
    if derived_from_finding_ids is not None:
        if not isinstance(derived_from_finding_ids, list):
            raise SystemExit(
                "derived_from_finding_ids must be a list of dev.finding ids; "
                f"got {type(derived_from_finding_ids).__name__}"
            )
        for index, item in enumerate(derived_from_finding_ids):
            if not isinstance(item, str):
                raise SystemExit(
                    f"derived_from_finding_ids[{index}] must be a non-empty "
                    f"dev.finding id; got {type(item).__name__}"
                )
            if not item:
                raise SystemExit(
                    f"derived_from_finding_ids[{index}] must be a non-empty "
                    "dev.finding id; got the empty string"
                )
        if derived_from_finding_ids:
            content["derived_from_finding_ids"] = derived_from_finding_ids
    # Never defaulted to auto-merge: that is a deliberate escalation, and G1
    # (registry digest pinning + measured rollback) gates it regardless.
    content["autonomy"] = spec.get("autonomy", "propose")
    return content


@dataclass
class FiledTask:
    """Everything a caller of :func:`file_spec` needs to report what happened."""

    bead: dict[str, Any]
    content: dict[str, Any]
    release_binding: dispatch.ReleaseBinding | None
    superseded_task: dict[str, Any] | None
    repointed: tuple[str, ...]


def file_spec(
    spec: dict[str, Any],
    created_by: str,
    *,
    spec_identity: str | None = None,
    supersede: str | None = None,
    run_pristine_verification: bool,
    sub: Substrate | None = None,
) -> FiledTask:
    """File ``spec`` as a dev.task bead. The one filing path — CLI and HTTP both
    call this rather than each re-deriving the refusal checks (PRIN-005).

    ``run_pristine_verification`` has no default: every caller must state its
    intent explicitly, because the property it gates (the gateway pod must
    never clone or execute a client-supplied command) must not be reachable
    by a caller merely forgetting to pass ``False`` — see ``.factory/design.md``.
    Omitting it is a ``TypeError`` at the call site, not a silently-inherited
    policy.

    Which value each caller passes is the Operator's 2026-09-17 Option A decision
    made concrete: the CLI (``main``, below) passes ``True`` — an operator's own
    machine legitimately has a repo and a git binary, and OPS-6 is why that
    check exists at filing at all. The HTTP route passes ``False`` — the
    gateway pod must never clone or execute a client-supplied command
    (mcp-hub.es.yaml:42's SUBSTRATE_API_KEY, Plaid credentials and Cloudflare
    Access configuration all live in that pod). Dropping the check here does
    not drop it from the system: ``activities.dispatch_steps.preflight_activity``
    runs the identical check at claim time, in the dispatcher's own isolated
    clone, and a bead whose declared verification cannot pass reaches
    ``dispatch.record_environment_failure``'s consecutive-fault bound (bound
    3) instead of stranding silently or re-dispatching forever — see
    ``.factory/design.md``'s "AC-3's mechanism" section.

    ``spec_identity``/``supersede`` are ``None`` for every caller but the CLI
    with a file-path spec: an HTTP-filed spec has no repo-relative path to
    anchor an identity to (the same shape ``main`` already gives a stdin-piped
    spec), and HTTP filing does not support ``--supersede`` — killing a live
    bead over HTTP is a different decision than filing a new one.
    """
    content = build_content(spec)
    for criterion in structural_behavior_warnings(content):
        print(
            "WARNING: risk_class is 'structural' but this acceptance criterion "
            f"appears to mandate new behaviour: {criterion!r}\n"
            "  Tidy-First (constitution-DRAFT.md:9) admits no behavioural "
            "carve-out inside a structural change. This is a heuristic over "
            "prose, not a resolved fact, and does not block filing -- confirm "
            "this criterion is really about preserving existing behaviour, or "
            "move it (and its risk) into a separate behavioral-class task.",
            file=sys.stderr,
        )

    sub = sub if sub is not None else Substrate()
    tasks = sub.list_tasks()

    for warning in pr_prerequisite_warnings(spec, content, tasks):
        print(warning, file=sys.stderr)

    release_binding = _check_release_traceability(spec, sub)
    if release_binding is not None:
        if release_binding.outcome_id:
            content["outcome_ref"] = release_binding.outcome_id
    else:
        waiver = str(spec.get("release_ref_waived") or "").strip()
        if waiver:
            content["release_ref_waived"] = spec["release_ref_waived"]

    predecessor_ids = content.get("predecessor_bead_ids") or []
    if predecessor_ids:
        _check_predecessor_supersession(predecessor_ids, tasks)

    _check_source_bead_ids_not_live_tasks(
        content.get("source_bead_ids") or [], predecessor_ids, tasks, supersede
    )

    derived_from_finding_ids = content.get("derived_from_finding_ids") or []
    if derived_from_finding_ids:
        _check_derived_from_findings(derived_from_finding_ids, sub)

    superseded_task = _check_not_a_duplicate(
        tasks, spec_identity, content["title"], sub, supersede
    )
    _check_not_own_supersession_target(predecessor_ids, superseded_task)

    if spec_identity:
        content["spec_identity"] = spec_identity
    if superseded_task is not None:
        source_bead_ids = list(content.get("source_bead_ids") or [])
        if superseded_task["id"] not in source_bead_ids:
            source_bead_ids.append(superseded_task["id"])
        content["source_bead_ids"] = source_bead_ids

    # Last, and deliberately expensive (a throwaway clone + a verification
    # run): every cheaper refusal above should fire first so a spec with
    # several problems is told about the cheap ones without paying for a
    # clone bootstrap first.
    if run_pristine_verification:
        _check_pristine_verification(content)

    # --supersede transitions the OLD bead to "superseded" BEFORE the new bead
    # is created, not after (OPS-68). The store now refuses a second row in any
    # live state (pending/doing/review) for this identity (migration 0008) —
    # while the old bead sat "pending"/"failed" a moment longer, its own row would still carry
    # this identity and the create below would trip that guarantee itself.
    # Transitioning first also closes a smaller version of the same bug:
    # under the old note-then-transition order, two rows briefly carried one
    # identity even when nothing was racing.
    if superseded_task is not None:
        try:
            dispatch.transition_state_checked(
                sub,
                superseded_task["id"],
                str(superseded_task.get("state") or "pending"),
                "superseded",
                created_by,
            )
        except Exception as exc:
            raise SystemExit(
                f"--supersede {superseded_task['id']} could not be transitioned "
                f"to superseded: {exc}. Nothing was filed — retry once the "
                "underlying conflict (likely a concurrent transition of the "
                "same bead) clears."
            ) from exc

    try:
        bead = sub.create_task(content, created_by, trust_tier="user")
    except Exception as exc:
        if not _is_spec_identity_conflict(exc):
            if superseded_task is not None:
                # Any create failure here -- a network blip, a 500, a timeout,
                # not just the exotic third-filer conflict below -- leaves the
                # predecessor already superseded with nothing recorded to
                # replace it. Under the pre-#673 order a failed create left
                # the old bead untouched; the reorder moved the danger window,
                # so the guidance moves with it (release-gate blocking
                # finding). A timeout is ambiguous: the create may actually
                # have landed -- inspect before refiling.
                raise SystemExit(
                    f"{superseded_task['id']} was transitioned to superseded, "
                    f"but filing its replacement FAILED: {exc}. "
                    f"{superseded_task['id']} is now dead with no replacement "
                    "bead. If this was a timeout, the create may have landed "
                    "anyway -- check the board for the new bead before "
                    "re-filing; otherwise re-file the same spec without "
                    "--supersede (the predecessor is no longer live)."
                ) from exc
            raise
        if superseded_task is not None:
            # Vanishingly unlikely — freeing the identity above is what makes
            # this create ordinarily safe — but if a THIRD filer claimed it in
            # the gap, say so plainly: the old bead is already dead with
            # nothing recorded to replace it.
            raise SystemExit(
                f"{superseded_task['id']} was transitioned to superseded, but "
                f"filing its replacement was refused as a duplicate "
                f"spec_identity: {exc}. {superseded_task['id']} is now dead "
                "with no replacement bead — re-file the same spec (without "
                "--supersede, since that predecessor is no longer live) once "
                "the conflict clears."
            ) from exc
        # The mouth's own uniqueness guarantee caught what the read-then-check
        # above could not: another filer's create landed in the gap between
        # our read and our write. Refuse exactly as a sequential duplicate
        # would — same message, naming whichever bead actually won the race.
        _check_not_a_duplicate(sub.list_tasks(), spec_identity, content["title"], sub, None)
        raise SystemExit(
            "the store refused this filing as a duplicate spec_identity "
            "(PRIN-014's guarantee — the check-then-act window OPS-68 closes), "
            "but no matching live dev.task was found on re-read. Another "
            "filer won the race and its bead may have since changed state; "
            "re-list and inspect manually before refiling."
        ) from exc

    if release_binding is not None:
        # After the POST, not before: the edge needs the new bead's id as its
        # source. If it fails here the bead already exists — the acceptance
        # criterion this closes ("no bead with no edge") is best-effort past
        # this point, so the failure is loud and names the exact fix rather
        # than leaving the bead silently unbound.
        try:
            sub.add_link(bead["id"], release_binding.release_id, "delivers", created_by)
        except Exception as exc:
            outcome_suffix = (
                f"/{release_binding.outcome_id}" if release_binding.outcome_id else ""
            )
            raise SystemExit(
                f"filed dev.task {bead['id']} but failed to write its delivers "
                f"edge to release {release_binding.release_ref}: {exc}. The bead "
                "exists with no release binding — bind it by hand: "
                f"dispatch.py --task {bead['id']} --bind-release "
                f"{release_binding.release_ref}{outcome_suffix}"
            ) from exc

    for finding_id in content.get("derived_from_finding_ids") or []:
        # Same reasoning as the delivers edge above: after the POST, not
        # before, because the edge needs the new bead's id as its source.
        try:
            sub.add_link(bead["id"], finding_id, "derived_from", created_by)
        except Exception as exc:
            raise SystemExit(
                f"filed dev.task {bead['id']} but failed to write its "
                f"derived_from edge to dev.finding {finding_id}: {exc}. The "
                "bead exists with no edge to this finding — add it by hand: "
                f"BeadStore.add_link({bead['id']!r}, {finding_id!r}, "
                "'derived_from', created_by)."
            ) from exc

    repointed: tuple[str, ...] = ()
    if superseded_task is not None:
        # The transition already landed above (OPS-68 reordered it ahead of
        # the create, to free the identity the store now enforces
        # uniqueness on). Both of these run only once that transition — and
        # now the new bead itself — are known-good: the new bead already
        # carries the ordering edge (source_bead_ids, above — the existing
        # convention guards.py reads back via superseding_task_ids), and this
        # note is the durable, directly-readable trail on the OLD bead
        # itself, so the record survives without anyone having to
        # reverse-index the population to find it (exactly what #477's
        # close-out comment had to do by hand).
        sub.add_note(
            parent_id=superseded_task["id"],
            kind="status",
            body=f"Superseded by dev.task {bead['id']}, filed with --supersede.",
            created_by=created_by,
        )

        # Only after the transition has actually landed (OPS-13): repointing
        # dependents at a bead whose supersession failed to record would
        # strand them on a replacement that the board does not yet agree is
        # live.
        repointed = _repoint_dependents(superseded_task["id"], bead["id"], tasks, sub)

    return FiledTask(
        bead=bead,
        content=content,
        release_binding=release_binding,
        superseded_task=superseded_task,
        repointed=repointed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="File a dev.task bead.")
    parser.add_argument("spec", help="path to a JSON spec, or - for stdin")
    parser.add_argument(
        "--supersede",
        metavar="BEAD_ID",
        default=None,
        help=(
            "Explicitly name the live dev.task this filing replaces. Required "
            "to re-file a spec whose identity already has an open bead; "
            "refuses if BEAD_ID does not match that identity among the live "
            "duplicates found."
        ),
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.spec == "-" else open(args.spec).read()
    spec = json.loads(raw)
    spec_identity = _spec_identity(args.spec)

    filed = file_spec(
        spec,
        CREATED_BY,
        spec_identity=spec_identity,
        supersede=args.supersede,
        run_pristine_verification=True,
    )
    bead = filed.bead
    content = filed.content
    release_binding = filed.release_binding
    superseded_task = filed.superseded_task
    repointed = filed.repointed

    print(f"filed dev.task {bead['id']}")
    print(f"  {content['title']}")
    print(f"  lane={content['lane']} risk={content['risk_class']} state=pending")
    if release_binding is not None:
        outcome_suffix = (
            f"/{release_binding.outcome_id}" if release_binding.outcome_id else ""
        )
        print(f"  delivers {release_binding.release_ref}{outcome_suffix}")
    if superseded_task is not None:
        print(f"  supersedes {superseded_task['id']}")
        if repointed:
            print(f"  repointed dependent(s): {', '.join(repointed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
