"""PR-body markers shared by the release-gate tooling.

``gate-prepass.py`` and ``release-manifest.py`` must recognise an attended
outer-loop PR — and the originating dev.task bead id — by the same rule. The
definitions lived in each script separately and drifted from each other and
from what the dispatcher actually writes (observed 2026-08-18: both scripts
returned no bead id on the dispatcher's live PR body); a single shared
definition is what prevents that drift.

A release-gate verdict is recorded as a bare ``Release-gate: <VALUE>`` line
(VALUE is ``MERGE``, ``MERGE-WITH-CHANGES``, or ``DO-NOT-MERGE``) in a PR
body or comment; see ``RELEASE_GATE_VERDICT_RE`` below for the exact
accepted shapes. That verdict line may optionally be followed, on the very
next line of the same body or comment, by a second bare line naming the
revision the verdict judged: ``Release-gate-revision: <sha>``. That line is
optional — every verdict recorded before this convention existed, and any
verdict a gate chooses not to pin, has no such line and remains a fully
valid record. ``<sha>`` may be abbreviated: it is matched against the PR's
current head by PREFIX, in either direction, not by exact equality, so a
short form a gate report already prints (e.g. ``ac3584d8``) matches a full
40-character head that begins with it (``revisions_match``).
``find_release_gate_verdict_record`` reads both lines together; this is the
one place that defines the record, so it is the one place that must be read
to know the whole convention.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# An attended outer-loop PR carries no originating dev.task bead by design.
# It declares itself with a body line such as `Outer-loop: true` or a checked
# `[x] outer-loop` box.
OUTER_LOOP_RE = re.compile(
    r"(?im)^\s*(?:(?:[-*]\s*)?outer[-_ ]loop\s*[:=]\s*(?:true|yes)|(?:[-*]\s*)?\[x\]\s*outer[-_ ]loop)\s*$"
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

UUID_RE = re.compile(rf"^{_UUID}$")

# An explicit bead-id declaration line, e.g. `Dispatched-bead id: <uuid>`.
BEAD_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:dispatched[-_ ]bead(?:\s+id)?|bead\s+id)\s*[:=]\s*(\S+)\s*$"
)

# A prose reference to the originating dev.task. The live dispatcher template
# (apps/factory-dispatcher/dispatch.py, open_pull_request) opens every factory
# PR body with:
#     Dispatched by the factory from `dev.task` [`<uuid>`](<uuid>).
# so `dev.task` may be backticked and the id may arrive bare, backticked, or as
# a markdown-link label. Older bodies wrote `dev.task: <uuid>` or
# `dev.task <uuid>`; all of these resolve here.
DEV_TASK_REF_RE = re.compile(rf"(?<![\w.])`?dev\.task`?\s*:?\s*\[?`?({_UUID})\b")


def find_bead_id(body: str) -> str | None:
    """Extract the originating dev.task bead id a PR body declares, if any.

    An explicit bead-id line wins; a malformed one stays a handoff defect and
    is never rescued by a prose reference further down the body.
    """
    text = body or ""
    line = BEAD_LINE_RE.search(text)
    if line:
        candidate = line.group(1).strip("`")
        return candidate.lower() if UUID_RE.match(candidate) else None
    ref = DEV_TASK_REF_RE.search(text)
    return ref.group(1).lower() if ref else None


# A release-gate verdict, recorded as a bare line in a PR comment or the PR
# body: `Release-gate: MERGE`, `Release-gate: MERGE-WITH-CHANGES`, or
# `Release-gate: DO-NOT-MERGE`. This is the machine-recognizable record that
# merge-pr.sh requires before it will act — the JUDGMENT stays human; this
# regex only recognises that a judgment was recorded (PC-FAC-005/AC-1).
# MERGE-WITH-CHANGES must be tried before MERGE in the alternation, or the
# shorter alternative would match the prefix and leave "-WITH-CHANGES"
# dangling against the trailing `\s*$` anchor, failing the whole line.
RELEASE_GATE_VERDICT_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?release[-_ ]gate\s*[:=]\s*(MERGE-WITH-CHANGES|DO-NOT-MERGE|MERGE)\s*$"
)

# The optional line naming the revision a verdict judged, adjacent to it —
# see the module docstring for the full convention. Only the line
# immediately following the winning verdict line, in the same body or
# comment text, is read by find_release_gate_verdict_record below; a
# revision line anywhere else is not attributed to the verdict. The value is
# 4-40 hex characters so an abbreviated sha is accepted — comparison against
# the current head is by prefix, not exact equality (revisions_match).
# Checked and excluded FIRST, before _RELEASE_GATE_MENTION_RE or
# _STALENESS_MENTION_RE, at both of their call sites: both of those regexes
# anchor only on the bare `release[-_ ]gate` prefix and would otherwise read
# a compliant `Release-gate-revision:` line as an unparseable release-gate
# mention -- a false near miss that, on the staleness path, would make every
# verdict recorded under this convention look stale and refuse the merge.
RELEASE_GATE_REVISION_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?release[-_ ]gate[-_ ]revision\s*[:=]\s*([0-9a-fA-F]{4,40})\s*$"
)


def find_release_gate_verdict(body: str, comments: Iterable[str] | None = None) -> str | None:
    """Return the most recently recorded release-gate verdict, if any.

    Scans ``body`` then ``comments`` in the order given — callers pass
    comments oldest-first, matching ``gh``'s own ordering — and returns the
    last recognised verdict line, upper-cased. A later recorded verdict
    supersedes an earlier one, the same way CLAUDE.md requires re-verifying
    after any change to what was reviewed. A malformed line (unrecognised
    value, trailing text) never matches and is never rescued by a
    well-formed line elsewhere.

    The body is excluded entirely when it declares a dev.task bead id (see
    BODY_VERDICT_RULE), and every scanned text has its quoted lines (fenced,
    indented, or blockquoted -- see _quoted_mask) blanked before matching, so
    a verdict is never read from a region that is quoted rather than
    declared.
    """
    texts = _scannable_texts(body, comments)

    verdict: str | None = None
    for text in texts:
        matches = RELEASE_GATE_VERDICT_RE.findall(_mask_quoted_lines(text))
        if matches:
            verdict = matches[-1].upper()
    return verdict


# A line that mentions a release gate, decorated or not -- the set a near
# miss is drawn from before RELEASE_GATE_VERDICT_RE narrows it down to the
# well-formed ones. Anchored to where the mention begins the line, allowing
# the same leading decoration the strict regex allows (whitespace, a '-' or
# '*' list marker) plus surrounding backticks or asterisks the strict regex
# does NOT allow -- that's exactly the decoration this diagnostic exists to
# see through. Unanchored used to match ordinary prose anywhere in a PR body
# ("the release-gate value is..."), which isn't a near miss at all.
_RELEASE_GATE_MENTION_RE = re.compile(r"^[\s\-*`_]*release[-_ ]gate", re.IGNORECASE)

# A line that MIGHT carry a release-gate verdict, used ONLY by
# find_release_gate_verdict_status to decide staleness -- never shared with
# _RELEASE_GATE_MENTION_RE above, even though the two look similar. The two
# serve opposite risk postures and that is why they must stay separate
# definitions: as a diagnostic, a false positive is just noise for a person
# to dismiss, so _RELEASE_GATE_MENTION_RE stays narrow (anchored decoration
# set, no `#` or `>`). As a staleness control, a false positive merely
# refuses a merge (safe) while a false negative proceeds on a verdict that
# may have been superseded and lost to decoration (unsafe) -- so THIS regex
# is written to over-match, deliberately broader: it additionally allows a
# leading markdown heading marker (`#`) or blockquote marker (`>`) in front
# of the phrase. #858 narrowed _RELEASE_GATE_MENTION_RE from an unanchored
# `release[-_ ]gate` to the anchored, tighter form for diagnostic precision;
# sharing one regex between both call sites would have let that narrowing
# silently reopen the exact hazard this bead exists to close (measured: a
# heading-form or blockquote-form retraction recorded after a readable
# verdict stopped being recognised as even mentioning a release gate, so it
# could no longer make that verdict stale). Giving the staleness path its
# own definition makes it immune to any future narrowing of the diagnostic
# one, in either order.
_STALENESS_MENTION_RE = re.compile(r"^[\s>#\-*`_]*release[-_ ]gate", re.IGNORECASE)

# The same separator RELEASE_GATE_VERDICT_RE requires between the phrase and
# the value, used here to locate where a candidate value begins so decoration
# around it can be stripped and inspected.
_RELEASE_GATE_SEPARATOR_RE = re.compile(r"release[-_ ]gate\s*[:=]\s*", re.IGNORECASE)

# A verdict word standing alone on a line, with no `release-gate:` phrase at
# all -- the shape measured live on #924: the outer loop's dispatch template
# printed only the verdict word, with a `Release-gate-revision:` line
# immediately after it, so RELEASE_GATE_VERDICT_RE's required
# `release[-_ ]gate\s*[:=]` prefix had nothing to match and the verdict was
# lost entirely (read as no verdict, not a near miss). This regex is
# deliberately as narrow as RELEASE_GATE_VERDICT_RE's own leading decoration
# (whitespace, an optional `-`/`*` list marker) and allows no other
# decoration -- on its own a bare "MERGE" is indistinguishable from prose,
# which is exactly why find_release_gate_near_misses only reports a match
# here when the very next line is a Release-gate-revision: line (AC-3): that
# adjacency, not the word alone, is what the repository's recording
# convention (#910) actually produces and nothing else does.
_BARE_VERDICT_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(MERGE-WITH-CHANGES|DO-NOT-MERGE|MERGE)\s*$"
)

# The three legal values, longest-first for the same reason
# RELEASE_GATE_VERDICT_RE orders them: MERGE-WITH-CHANGES must be tried
# before MERGE or the shorter alternative wins the match and leaves
# "-WITH-CHANGES" as unrecognised trailing text. Case-insensitive to match
# RELEASE_GATE_VERDICT_RE -- the recovered value is upper-cased below
# regardless, so rejecting lowercase input here was an oversight, not a
# decision. The trailing negative lookahead rejects a match that is itself
# only a prefix of a longer, non-legal token immediately abutting it (no
# separating whitespace) -- e.g. "DO-NOT-MERGE-YET" or
# "MERGE-WITH-CHANGES-PENDING-CI" -- so the diagnostic never guesses a
# verdict out of an illegal hyphen-suffixed value. A *space*-separated
# continuation ("MERGE please") is still caught below, by the isalpha guard
# on what follows decoration-stripping.
_VALUE_TOKEN_RE = re.compile(r"(MERGE-WITH-CHANGES|DO-NOT-MERGE|MERGE)(?![A-Za-z0-9-])", re.IGNORECASE)

# Decoration characters that can legitimately wrap or trail a value without
# changing what a person plainly intended: a code span, bold/italic markup,
# or a bullet. Deliberately excludes bare letters -- a letter immediately
# after the value means the "value" was actually a fragment of a longer
# phrase (`MERGE IF CI GOES GREEN`, `MERGE please`), not decoration around a
# real one, and AC-3 requires that case report no recognised value at all.
_DECORATION_CHARS = "`*_"

# A blockquote line (`> ...`). The strict verdict/revision regexes already
# reject a leading `>` via their `^\s*` anchor (whitespace, not an arbitrary
# character), so this never changed what they accept -- it exists so
# _quoted_mask below is the one place that names every quoted shape, rather
# than leaving blockquote exclusion as an accident of the strict regexes'
# own anchoring that a future change to them could silently undo.
_BLOCKQUOTE_RE = re.compile(r"^\s*>")

# A markdown indented code block: 4+ literal leading spaces. Measured live on
# #939's own PR body, which explains the #924 defect by quoting its exact
# malformed shape indented under a paragraph:
#
#     MERGE-WITH-CHANGES
#     Release-gate-revision: 15307c6ccd1490e3840cf8e1bebde775a60be51f
#
# That quotation is prose *about* the defect, not an instance of it, but the
# BARE-verdict branch of find_release_gate_near_misses read it as one --
# exactly the self-inflicted dilution this constant exists to close: every
# future comment explaining the #924 shape would trip the detector that
# exists because of it. Scoped to the bare branch only, the same way fence
# tracking is scoped below -- the `release-gate:`-mention branch is left
# alone (pre-existing behaviour, bead 1c212f4e).
_INDENTED_QUOTE_RE = re.compile(r"^ {4}")


def _quoted_mask(lines: Sequence[str]) -> list[bool]:
    """Mark each line in ``lines`` as quoted -- inside a fenced code block
    (``` or ~~~, closed only by the marker that opened it), an indented code
    block (4+ leading spaces), or a blockquote -- so a verdict is never read
    from a quoted region (dev.finding 807baee1, AC-1). This is the ONE
    definition of "quoted" in this module (PRIN-005): find_release_gate_verdict,
    find_release_gate_verdict_record, and find_release_gate_verdict_status all
    scan text run through _mask_quoted_lines below, which calls this, and
    find_release_gate_near_misses' bare-verdict-word branch calls this
    directly in place of its own former inline fence tracking. The
    `release-gate:`-mention branch and the staleness mention source
    (_STALENESS_MENTION_RE) are deliberately NOT routed through this -- they
    are diagnostics for a person, quote-blind by design (bead 1c212f4e), and
    stay that way.

    A fence marker line itself counts as quoted -- it is never a candidate
    verdict line either.
    """
    mask: list[bool] = []
    fence_char: str | None = None
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line.startswith("```") or line.startswith("~~~"):
            marker = line[0]
            if fence_char is None:
                fence_char = marker
            elif marker == fence_char:
                fence_char = None
            mask.append(True)
            continue
        if fence_char is not None:
            mask.append(True)
            continue
        if _INDENTED_QUOTE_RE.match(line) or _BLOCKQUOTE_RE.match(line):
            mask.append(True)
            continue
        mask.append(False)
    return mask


def _masked_lines(text: str) -> list[str]:
    """Return ``text``'s lines (``text.splitlines()``) with every quoted
    line (per _quoted_mask) replaced by an empty string -- same length and
    order as the input, so a caller that walks lines by position still sees
    the same positions after masking."""
    lines = text.splitlines()
    mask = _quoted_mask(lines)
    return ["" if quoted else line for line, quoted in zip(lines, mask)]


def _mask_quoted_lines(text: str) -> str:
    """Return ``text`` with every quoted line (per _quoted_mask) replaced by
    an empty line, preserving line count and order. A verdict regex anchored
    with MULTILINE ``^...$`` can then never match quoted content, while a
    caller that walks lines by position still sees the same positions.

    Joining with ``"\\n"`` rather than returning ``_masked_lines`` directly:
    callers that need the find-all whole-text regex behaviour (crossing a
    newline between the phrase and its value, the shape
    find_release_gate_verdict already accepts) need actual text, not a line
    list -- callers that need positions use ``_masked_lines`` instead, since
    ``"".splitlines() == []`` would silently drop an all-quoted single-line
    text's one (blanked) line if round-tripped through this function.
    """
    return "\n".join(_masked_lines(text))


# A release-gate verdict is recorded only as a PR COMMENT -- never in the
# body -- on any PR whose body declares a dev.task bead id (dev.finding
# 807baee1, AC-2). apps/factory-dispatcher/dispatch.py's open_pull_request
# interpolates the worker's own stdout verbatim into the body of every such
# PR; the worker carries no GitHub credential since #1101/#1103, so a
# comment is a surface only the gate or a person can write, while the body
# is the worker's own surface. A PR with no declared bead id (an attended
# outer-loop PR) is unaffected -- its body is read exactly as before.
BODY_VERDICT_RULE = (
    "dev.finding 807baee1: on a PR whose body declares a dev.task bead id, "
    "a release-gate verdict is read from PR comments only -- the body is "
    "never consulted, because the body is the worker's own writable surface."
)


def _scannable_texts(body: str | None, comments: Iterable[str] | None) -> list[str]:
    """The texts a verdict reader may consult, in order: the PR body is
    included only when it does NOT declare a dev.task bead id -- see
    BODY_VERDICT_RULE. Comments always count, in the order given."""
    body_text = body or ""
    texts: list[str] = [] if find_bead_id(body_text) else [body_text]
    if comments:
        texts.extend(comment or "" for comment in comments)
    return texts


@dataclass(frozen=True)
class ReleaseGateNearMiss:
    """A line that mentions a release gate but doesn't satisfy RELEASE_GATE_VERDICT_RE.

    ``recognized_value`` is the verdict value recovered from inside the
    decoration when -- and only when -- one of the three legal values is
    unambiguously present; it is ``None`` when the line's value isn't one of
    the three legal ones. This function never guesses: a near miss is a
    diagnostic for a person to fix, and turning an unrecognised value into a
    guessed verdict would make this diagnostic a second, looser way to
    supply a merge decision, which is exactly the hazard AC-3 forbids.
    ``suggested_line`` is the exact bare line RELEASE_GATE_VERDICT_RE would
    accept in its place, or ``None`` when no value was recognised to build
    one from.
    """

    line: str
    recognized_value: str | None
    suggested_line: str | None


def _diagnose_near_miss(line: str) -> ReleaseGateNearMiss:
    separator = _RELEASE_GATE_SEPARATOR_RE.search(line)
    if not separator:
        return ReleaseGateNearMiss(line=line, recognized_value=None, suggested_line=None)

    rest = line[separator.end() :].lstrip(_DECORATION_CHARS + " \t")
    match = _VALUE_TOKEN_RE.match(rest)
    if not match:
        return ReleaseGateNearMiss(line=line, recognized_value=None, suggested_line=None)

    tail = rest[match.end() :].lstrip(_DECORATION_CHARS)
    remainder = tail.lstrip()
    if remainder and remainder[0].isalpha():
        # A letter right after the value means it was a fragment of a longer
        # phrase, not a decorated verdict -- e.g. "MERGE IF CI GOES GREEN".
        return ReleaseGateNearMiss(line=line, recognized_value=None, suggested_line=None)

    value = match.group(1).upper()
    return ReleaseGateNearMiss(
        line=line,
        recognized_value=value,
        suggested_line=f"Release-gate: {value}",
    )


def find_release_gate_near_misses(
    body: str, comments: Iterable[str] | None = None
) -> list[ReleaseGateNearMiss]:
    """Find lines that mention a release gate but don't parse as a verdict,
    plus a bare verdict word immediately followed by a revision line.

    Scans ``body`` then ``comments``, same order and inputs as
    find_release_gate_verdict, so a caller can run both over the same PR and
    know exactly which lines the strict recognizer rejected. This is the
    diagnostic the strict recognizer deliberately never performs itself
    (its docstring: "never rescued by a well-formed line elsewhere") --
    surfaced here for a person to fix, not to feed back into a merge
    decision.

    A second, narrower source feeds the same list: a line matching
    _BARE_VERDICT_LINE_RE -- a verdict word with no `release-gate:` phrase at
    all -- immediately followed by a Release-gate-revision: line. That
    adjacency is the only thing that makes a bare word a near miss rather
    than ordinary prose (AC-3); a bare word alone, or one followed by a
    revision line only after a blank line, is not reported.

    That second source skips QUOTED regions: fenced code blocks (``` or
    ~~~), indented code blocks (4+ leading spaces), and blockquotes --
    _quoted_mask is the one definition of "quoted" this module has (shared
    with the three strict verdict readers, dev.finding 807baee1/AC-1). A
    factory PR body is not wholly human-authored -- dispatch.py:3277
    interpolates the worker's stdout verbatim into a ``` fence -- so a bare
    verdict word there is the worker's text, not a person's near miss. A PR
    body explaining a past malformed verdict, the way #939's own body quotes
    the #924 shape in an indented block, is prose *about* the defect, not an
    instance of it. merge-pr.sh prints near misses next to its no-verdict
    refusal, so reporting either would tell the operator a verdict was
    nearly recorded and hand them the exact line to write, sourced from the
    thing being judged or from an explanation of a past defect. The
    `release-gate`-mention source is deliberately left quote-blind here;
    that is pre-existing behaviour carried by bead 1c212f4e.
    """
    texts: list[str] = [body or ""]
    if comments:
        texts.extend(comment or "" for comment in comments)

    near_misses: list[ReleaseGateNearMiss] = []
    for text in texts:
        lines = [raw_line.rstrip("\r") for raw_line in text.splitlines()]
        quoted = _quoted_mask(lines)
        for index, line in enumerate(lines):
            # Fence delimiter lines are never mention-check input, regardless
            # of whether they open, close, or (a non-matching marker inside an
            # already-open fence) do neither -- same as _quoted_mask's own
            # toggle, kept inline here only to skip the MENTION branch, which
            # stays quote-blind per bead 1c212f4e and so cannot read `quoted`.
            if line.startswith("```") or line.startswith("~~~"):
                continue

            if _RELEASE_GATE_MENTION_RE.search(line):
                if RELEASE_GATE_VERDICT_RE.match(line):
                    continue
                if RELEASE_GATE_REVISION_RE.match(line):
                    continue
                near_misses.append(_diagnose_near_miss(line))
                continue

            if quoted[index]:
                continue

            bare_match = _BARE_VERDICT_LINE_RE.match(line)
            if not bare_match:
                continue
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            if not RELEASE_GATE_REVISION_RE.match(next_line):
                continue
            value = bare_match.group(1).upper()
            near_misses.append(
                ReleaseGateNearMiss(
                    line=line,
                    recognized_value=value,
                    suggested_line=f"Release-gate: {value}",
                )
            )
    return near_misses


@dataclass(frozen=True)
class ReleaseGateStatus:
    """The verdict find_release_gate_verdict returns, plus order-aware staleness.

    ``verdict`` is byte-for-byte what ``find_release_gate_verdict`` returns
    for the same inputs -- guaranteed by construction, because
    find_release_gate_verdict_status computes it by calling
    find_release_gate_verdict directly rather than reimplementing the scan
    inline; a dedicated test still pins the two against each other so any
    future refactor that reintroduces a second implementation is caught
    immediately (the #431 lesson). ``stale_near_miss`` is the near miss
    recorded, in the same body-then-comments scan order, strictly AFTER the
    line ``verdict`` came from -- i.e. a later line mentioned a release gate
    but this reader could not parse it as one. That makes the returned
    verdict stale: a later verdict may genuinely have been recorded and lost
    to decoration, and the reader has no way to tell "nothing more was
    written" from "something unreadable was written after this." A near miss
    recorded before or alongside the winning verdict is not reported here --
    the winning verdict legitimately came later and supersedes it, per
    CLAUDE.md's re-verify rule.

    When the verdict spans a shape this dataclass's line-by-line position
    walk cannot place -- e.g. the phrase and separator end one line and the
    value begins the next, which the strict recognizer's whole-text findall
    accepts (its `\\s*` crosses the newline) but a per-line `.match()` never
    can -- there is no position to compare a later mention against. Per
    PRIN-015, a control that cannot evaluate its question fails closed here:
    any near miss found anywhere in the scanned text is reported as stale
    rather than silently treating the un-placeable verdict as undisputed.
    """

    verdict: str | None
    stale_near_miss: ReleaseGateNearMiss | None


def find_release_gate_verdict_status(
    body: str, comments: Iterable[str] | None = None
) -> ReleaseGateStatus:
    """Pair find_release_gate_verdict's answer with order-aware staleness.

    ``verdict`` is find_release_gate_verdict's own return value for these
    same inputs -- called directly, not reimplemented, so it cannot drift
    from it (the #431 lesson). Locating WHERE that verdict came from is a
    separate, best-effort line-by-line walk: it reuses RELEASE_GATE_VERDICT_RE
    per line, against the QUOTED-masked text (see _quoted_mask /
    BODY_VERDICT_RULE, dev.finding 807baee1), to find the winning line's
    position, and the staleness-specific _STALENESS_MENTION_RE (deliberately
    broader than the diagnostic's _RELEASE_GATE_MENTION_RE -- see its
    definition) plus _diagnose_near_miss, against the ORIGINAL unmasked
    text, to find candidate near-miss positions to compare against it --
    that mention source stays quote-blind by design, unaffected by this
    bead (over-matching there only ever refuses a merge, the safe
    direction).
    """
    texts = _scannable_texts(body, comments)

    verdict = find_release_gate_verdict(body, comments)

    verdict_position: tuple[int, int] | None = None
    near_misses_in_order: list[tuple[tuple[int, int], ReleaseGateNearMiss]] = []

    for text_index, text in enumerate(texts):
        masked_lines = _masked_lines(text)
        for line_index, raw_line in enumerate(text.splitlines()):
            line = raw_line.rstrip("\r")
            masked_line = masked_lines[line_index].rstrip("\r")
            if RELEASE_GATE_VERDICT_RE.match(masked_line):
                verdict_position = (text_index, line_index)
                continue
            if RELEASE_GATE_REVISION_RE.match(masked_line):
                continue
            if _STALENESS_MENTION_RE.search(line):
                near_misses_in_order.append(((text_index, line_index), _diagnose_near_miss(line)))

    stale_near_miss: ReleaseGateNearMiss | None = None
    if verdict is not None:
        if verdict_position is not None:
            for position, near_miss in near_misses_in_order:
                if position > verdict_position:
                    stale_near_miss = near_miss
                    break
        elif near_misses_in_order:
            # Fail closed (PRIN-015): the strict recognizer found a verdict
            # in a shape (e.g. a value on the line after "Release-gate:")
            # this line-by-line walk cannot place, so there is no position
            # to compare a later mention against. Refuse to treat the
            # verdict as undisputed rather than silently trusting it.
            stale_near_miss = near_misses_in_order[-1][1]

    return ReleaseGateStatus(verdict=verdict, stale_near_miss=stale_near_miss)


@dataclass(frozen=True)
class ReleaseGateVerdictRecord:
    """The verdict paired with the revision it was recorded against, if named.

    ``verdict`` is byte-for-byte ``find_release_gate_verdict``'s own return
    value for these same inputs — called directly, not reimplemented (the
    #431 lesson). ``revision`` is the value of an optional
    ``Release-gate-revision:`` line immediately following the winning
    verdict line, in the same body or comment text as that line — never a
    different occurrence, so a revision recorded next to a superseded
    verdict is never attributed to the one that won. It is ``None`` when no
    such line immediately follows, which is the shape every verdict
    recorded before this convention existed has, and remains a fully valid
    record (see the module docstring).
    """

    verdict: str | None
    revision: str | None


def find_release_gate_verdict_record(
    body: str, comments: Iterable[str] | None = None
) -> ReleaseGateVerdictRecord:
    """Pair find_release_gate_verdict's answer with the revision line that
    may immediately follow it, per the convention the module docstring
    defines.

    Locating the winning verdict line to look next to is a separate,
    best-effort line-by-line walk — the same *kind* find_release_gate_verdict_status
    performs for its own, unrelated purpose (staleness), deliberately not
    shared with it: the two look for different things near the verdict line
    (a stale mention anywhere after it, vs. a revision line immediately
    after it) and conflating them would make a future change to one
    silently change the other. Both the verdict-position walk and the
    adjacent-revision check scan the QUOTED-masked text (_quoted_mask,
    dev.finding 807baee1/AC-1), so neither a quoted verdict nor a quoted
    revision line is ever read as part of the record.
    """
    texts = _scannable_texts(body, comments)

    verdict = find_release_gate_verdict(body, comments)
    if verdict is None:
        return ReleaseGateVerdictRecord(verdict=None, revision=None)

    verdict_position: tuple[int, int] | None = None
    for text_index, text in enumerate(texts):
        masked_lines = _masked_lines(text)
        for line_index, masked_line in enumerate(masked_lines):
            if RELEASE_GATE_VERDICT_RE.match(masked_line.rstrip("\r")):
                verdict_position = (text_index, line_index)

    revision: str | None = None
    if verdict_position is not None:
        text_index, line_index = verdict_position
        masked_lines = _masked_lines(texts[text_index])
        if line_index + 1 < len(masked_lines):
            next_line = masked_lines[line_index + 1].rstrip("\r")
            match = RELEASE_GATE_REVISION_RE.match(next_line)
            if match:
                revision = match.group(1).lower()

    return ReleaseGateVerdictRecord(verdict=verdict, revision=revision)


def revisions_match(a: str, b: str) -> bool:
    """True when two revisions name the same commit, tolerating either
    being an abbreviated PREFIX of the other — git's own convention, and
    the one a gate report already writes (e.g. ``ac3584d8`` for a
    40-character head). Case-insensitive. Empty input never matches, so a
    missing revision or an unresolvable head skips the comparison entirely
    rather than being treated as identical (an unmeasured condition must
    not become a refusal)."""
    a_lower, b_lower = a.lower(), b.lower()
    if not a_lower or not b_lower:
        return False
    return a_lower.startswith(b_lower) or b_lower.startswith(a_lower)


def format_near_miss(near_miss: ReleaseGateNearMiss) -> str:
    """Render a near miss as the multi-line diagnostic merge-pr.sh and the
    writer-side `check` command both print: the offending line verbatim,
    then either the recognised value and the exact fix, or a plain statement
    that no legal value was found -- never a guessed one."""
    lines = [f"  found:    {near_miss.line}"]
    if near_miss.suggested_line is not None:
        lines.append(f"  read as:  {near_miss.recognized_value}")
        lines.append(f"  required: {near_miss.suggested_line}")
    else:
        lines.append("  read as:  no recognised release-gate verdict value")
    return "\n".join(lines)


def _comment_bodies(comments: Any) -> list[str]:
    bodies: list[str] = []
    for comment in comments or []:
        if isinstance(comment, dict):
            bodies.append(str(comment.get("body") or ""))
        else:
            bodies.append(str(comment or ""))
    return bodies


def main(argv: Sequence[str] | None = None, stdin: Any = None) -> int:
    """CLI entry point used by scripts/merge-pr.sh: the sole consumer of
    RELEASE_GATE_VERDICT_RE outside this module. It calls find_release_gate_verdict()
    rather than re-implementing recognition, so the shell path and this module
    can never drift (the #431 lesson).

    Six commands:
      verdict       Print the recognised release-gate verdict (or a blank
                     line) for a PR body on stdin and --comments-json.
                     What scripts/merge-pr.sh consults at merge time.
      revision      Print the revision recorded next to the winning verdict
                     (or a blank line when none was recorded) for a PR body
                     on stdin and --comments-json. What scripts/merge-pr.sh
                     consults, only for a MERGE-WITH-CHANGES verdict, to
                     decide whether it can check for a moved tree at all.
      same-revision Exit 0 when two revisions given as positional arguments
                     name the same commit by PREFIX (either direction),
                     exit 1 otherwise. What scripts/merge-pr.sh consults to
                     compare a recorded revision against the PR's current
                     head.
      near-misses   Print the near-miss diagnostic for a PR body on stdin
                     and --comments-json: every line that mentions a release
                     gate but doesn't parse, with the exact fix. What
                     scripts/merge-pr.sh prints alongside its refusal when
                     `verdict` comes back empty.
      check         Validate a candidate verdict comment BEFORE posting it:
                     read text from --file or stdin, exit 0 when it carries
                     a machine-readable verdict, exit 1 and print the
                     near-miss diagnostic when it doesn't.
                     Example: python3 scripts/gate_markers.py check --file /tmp/comment.txt
                     Example: echo 'Release-gate: MERGE' | python3 scripts/gate_markers.py check
      stale-near-miss
                     Read a PR body on stdin and --comments-json: exit 0 and
                     print nothing when the verdict `verdict` would return is
                     undisputed, exit 1 and print the near-miss diagnostic
                     when a line recorded AFTER that verdict mentions a
                     release gate but doesn't parse as one. What
                     scripts/merge-pr.sh consults after a MERGE or
                     MERGE-WITH-CHANGES verdict, before proceeding.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        choices=["verdict", "revision", "same-revision", "near-misses", "check", "stale-near-miss"],
    )
    parser.add_argument(
        "revisions",
        nargs="*",
        help="For 'same-revision': the two revisions to compare.",
    )
    parser.add_argument(
        "--comments-json",
        default="[]",
        help="JSON array of PR comments (gh's shape, or a plain list of strings). "
        "Used by 'verdict', 'revision', and 'near-misses'.",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="For 'check': read the candidate text from this file instead of stdin.",
    )
    args = parser.parse_args(argv)

    stream = stdin if stdin is not None else sys.stdin

    if args.command == "verdict":
        body = stream.read()
        comments = _comment_bodies(json.loads(args.comments_json))
        verdict = find_release_gate_verdict(body, comments)
        print(verdict or "")
        return 0

    if args.command == "revision":
        body = stream.read()
        comments = _comment_bodies(json.loads(args.comments_json))
        record = find_release_gate_verdict_record(body, comments)
        print(record.revision or "")
        return 0

    if args.command == "same-revision":
        if len(args.revisions) != 2:
            print("same-revision requires exactly two revisions", file=sys.stderr)
            return 2
        return 0 if revisions_match(args.revisions[0], args.revisions[1]) else 1

    if args.command == "near-misses":
        body = stream.read()
        comments = _comment_bodies(json.loads(args.comments_json))
        near_misses = find_release_gate_near_misses(body, comments)
        print("\n\n".join(format_near_miss(nm) for nm in near_misses))
        return 0

    if args.command == "stale-near-miss":
        body = stream.read()
        comments = _comment_bodies(json.loads(args.comments_json))
        status = find_release_gate_verdict_status(body, comments)
        if status.stale_near_miss is None:
            return 0
        print(format_near_miss(status.stale_near_miss))
        return 1

    # check
    # Validate the candidate as the COMMENT it documents itself as
    # validating (dev.finding 807baee1, PR #1117 review): once posted, a
    # verdict is read by find_release_gate_verdict with the candidate as a
    # comment, never as the body -- a body carrying the same text is
    # excluded outright on any PR that declares a dev.task bead
    # (BODY_VERDICT_RULE). Checking it as a body here would accept a
    # candidate that the real read path, after posting, rejects. The
    # near-miss fallback is evaluated the same way so its diagnostic matches
    # what the posted comment will actually produce.
    text = pathlib.Path(args.file).read_text() if args.file else stream.read()
    if find_release_gate_verdict("", [text]) is not None:
        return 0
    near_misses = find_release_gate_near_misses("", [text])
    if near_misses:
        print("No machine-readable release-gate verdict -- found a near miss instead:", file=sys.stderr)
        for near_miss in near_misses:
            print(format_near_miss(near_miss), file=sys.stderr)
    else:
        print(
            "No release-gate verdict found. Record one of:\n"
            "  Release-gate: MERGE\n"
            "  Release-gate: MERGE-WITH-CHANGES\n"
            "  Release-gate: DO-NOT-MERGE",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
