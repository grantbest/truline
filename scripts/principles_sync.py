#!/usr/bin/env python3
"""Sync `docs/architecture/principles.md` with `arch.principle` beads.

doctrine.md's "Interim home" commits to the Pillar 2 pattern: once the substrate
knows a type, the file that used to be its only home becomes a *view* of the
beads. `arch.principle` landed in `apps/substrate/src/schemas.py`
(`ArchPrincipleContent`); this is the loader that materializes the interim
registry into beads with `PRIN-NNN` as a stable external id, and regenerates
the file back from parsed or fetched entries.

Six subcommands:

    principles_sync.py parse                       # -> JSON entries on stdout
    principles_sync.py push --dry-run|--apply       # -> create/update plan per PRIN id
    principles_sync.py render                       # -> regenerated Markdown on stdout
    principles_sync.py fetch                        # -> JSON entries read from the beads
    principles_sync.py check-view                   # -> exit 0 if the file matches the beads
    principles_sync.py propose-promotion ID STATUS  # -> drafts a transition; writes nothing

**Idempotent on `content["ref"]`.** Same convention `scripts/ea-load.py` already
uses for `arch.capability`/`application`/`observation` — the substrate's
`content_ref` query filters on this key. `ArchPrincipleContent` does not declare
`ref` itself, but its `extra="allow"` permits it; see `.factory/design.md`
decision 1 for why this key was chosen over inventing a new one.

**`rationale` is filled from `source`.** The interim registry carries one
provenance bullet per principle, not the two `doctrine.md`'s field table
describes. Until `principles.md` gains a distinct Rationale bullet, both schema
fields carry the same text — see `.factory/design.md` decision 2. Nothing here
invents rationale text; it duplicates what the reviewed file already states.

**`fetch`/`check-view`/`push` all go through `substrate_client.Substrate`** —
the shared HTTP client `ea-load.py`/`release-load.py`/`requirements-load.py`
already use for writes and the gate scripts use for reads, rather than a
fourth hand-rolled `X-API-Key` client. `Substrate` below is a thin subclass
adding the three `arch.principle`-shaped calls this module needs.

**`push --apply` against the real substrate is an operator action**, run by the
outer loop after this change merges — never by this task's own test suite. The
same is true of `fetch`/`check-view` run without a `store` argument (i.e. via
`main()`). No test in `scripts/tests/test_principles_sync.py` opens a network
connection; `fetch`/`check-view`/`propose-promotion` are exercised as pure
functions against `FakePrincipleStore`, the same double `push`'s tests use.

**`propose-promotion` writes nothing.** It drafts a dated `status_history`
transition and a unified diff of the registry entry, for a human to paste into
a PR — the skill's own rule, mechanized: propose, don't write.

Environment (only read when `push`/`fetch`/`check-view` target the real
substrate): SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Optional

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


_substrate_client_module = _load_substrate_client_module()
_Substrate = _substrate_client_module.Substrate

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO / "docs" / "architecture" / "principles.md"

NAMESPACE = "arch"
BEAD_TYPE = "principle"
CREATED_BY = "principles-sync"
TRUST_TIER = "user"

STATUS_VALUES = frozenset({"proposed", "adopted", "enforced", "retired"})
# Mirrors apps/substrate/src/schemas.py's _PRINCIPLE_MEASUREMENT_KEYS: dated
# application/staleness measurement belongs on arch.observation, never on
# principle structure (the F-DCE-5 boundary).
MEASUREMENT_KEYS = frozenset({"measured_at", "verdict", "applied_count", "conformance"})

_HEADING_RE = re.compile(r"^### (PRIN-\d{3}) — (.+)$")
_BULLET_RE = re.compile(r"^- \*\*([^*]+):\*\* (.*)$")
_CONTINUATION_RE = re.compile(r"^  (\S.*)$")
_SEEDED_DATE_RE = re.compile(r"Seeded (\d{4}-\d{2}-\d{2})")
_STATUS_VALUE_RE = re.compile(r"^`([a-z_]+)`\s*(.*)$")


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipleEntry:
    """One `PRIN-NNN` entry, as structured data.

    `status_note` is the free text after the status value on the Status
    bullet (an explanation, not itself part of the status vocabulary).
    `notes` holds any further bullets (e.g. "Enforcement gap:") verbatim as
    `"Label: text"` strings, so render() can reconstruct them without a fixed
    schema for what labels a principle may carry.
    """

    id: str
    name: str
    statement: str
    source: str
    status: str
    status_note: str = ""
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "statement": self.statement,
            "source": self.source,
            "status": self.status,
            "status_note": self.status_note,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Registry:
    """The parsed file: a preamble (kept verbatim) plus the PRIN entries."""

    header: str
    entries: tuple[PrincipleEntry, ...]

    def seeded_date(self) -> str:
        match = _SEEDED_DATE_RE.search(self.header)
        if not match:
            raise ValueError("registry header carries no 'Seeded YYYY-MM-DD' line")
        return match.group(1)


def _parse_entry(block: list[str]) -> PrincipleEntry:
    heading = _HEADING_RE.match(block[0])
    if not heading:
        raise ValueError(f"expected a PRIN-NNN heading, got: {block[0]!r}")
    entry_id, name = heading.group(1), heading.group(2).strip()

    fields: dict[str, list[str]] = {}
    order: list[str] = []
    label: Optional[str] = None
    for line in block[1:]:
        bullet = _BULLET_RE.match(line)
        if bullet:
            label = bullet.group(1).strip()
            if label not in fields:
                order.append(label)
            fields[label] = [bullet.group(2).strip()]
            continue
        continuation = _CONTINUATION_RE.match(line)
        if continuation and label is not None:
            fields[label].append(continuation.group(1).strip())
            continue
        label = None

    def joined(label_name: str) -> Optional[str]:
        if label_name not in fields:
            return None
        return " ".join(fields[label_name]).strip()

    statement = joined("Statement")
    source = joined("Source")
    raw_status = joined("Status")
    if statement is None:
        raise ValueError(f"{entry_id}: missing a Statement bullet")
    if source is None:
        raise ValueError(f"{entry_id}: missing a Source bullet")
    if raw_status is None:
        raise ValueError(f"{entry_id}: missing a Status bullet")

    status_match = _STATUS_VALUE_RE.match(raw_status)
    if not status_match:
        raise ValueError(
            f"{entry_id}: Status bullet does not start with a `value`: {raw_status!r}"
        )
    status, status_note = status_match.group(1), status_match.group(2).strip()
    if status not in STATUS_VALUES:
        raise ValueError(
            f"{entry_id}: status {status!r} is not one of {sorted(STATUS_VALUES)}"
        )

    notes = tuple(
        f"{label_name}: {' '.join(fields[label_name]).strip()}"
        for label_name in order
        if label_name not in {"Statement", "Source", "Status"}
    )

    return PrincipleEntry(
        id=entry_id,
        name=name,
        statement=statement,
        source=source,
        status=status,
        status_note=status_note,
        notes=notes,
    )


def parse_registry(text: str) -> Registry:
    """Read the registry Markdown into structured entries. Pure: no I/O."""
    lines = text.splitlines()
    heading_indexes = [i for i, line in enumerate(lines) if _HEADING_RE.match(line)]
    if not heading_indexes:
        return Registry(header=text, entries=())

    header = "\n".join(lines[: heading_indexes[0]])
    boundaries = heading_indexes + [len(lines)]
    entries = tuple(
        _parse_entry(lines[start:end])
        for start, end in zip(heading_indexes, boundaries[1:])
    )
    return Registry(header=header, entries=entries)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def _render_entry(entry: PrincipleEntry) -> str:
    lines = [f"### {entry.id} — {entry.name}", ""]
    lines.append(f"- **Statement:** {entry.statement}")
    lines.append(f"- **Source:** {entry.source}")
    status_line = f"- **Status:** `{entry.status}`"
    if entry.status_note:
        status_line += f" {entry.status_note}"
    lines.append(status_line)
    for note in entry.notes:
        note_label, _, note_text = note.partition(": ")
        lines.append(f"- **{note_label}:** {note_text}")
    return "\n".join(lines)


def render_registry(registry: Registry) -> str:
    """Regenerate the file text from parsed entries.

    Round-trips through parse() (whitespace-normalizing on both sides) but
    does not replay the source file's original line-wrap column width — see
    .factory/design.md decision 6.
    """
    header = registry.header.rstrip("\n")
    body = "\n\n".join(_render_entry(entry) for entry in registry.entries)
    if not body:
        return header + "\n"
    return f"{header}\n\n{body}\n"


# ---------------------------------------------------------------------------
# push — content shaping, validation double, and reconciliation
# ---------------------------------------------------------------------------


class PrincipleValidationError(ValueError):
    """Raised when content would 422 against apps/substrate/src/schemas.py's
    ArchPrincipleContent / ArchPrincipleStatusHistoryEntry.

    Raised both by the live Substrate client's callers (via SubstrateError,
    once the real API responds) and by the in-memory test double, which
    mirrors this contract instead of accepting a superset of it — the
    FakeSubstrate.add_note lesson (CLAUDE.md operating rules).
    """


def _forbidden_measurement_paths(
    value: Any, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    if isinstance(value, dict):
        found: list[tuple[str, ...]] = []
        for key, child in value.items():
            child_path = (*path, str(key))
            if key in MEASUREMENT_KEYS:
                found.append(child_path)
            found.extend(_forbidden_measurement_paths(child, child_path))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value):
            found.extend(_forbidden_measurement_paths(child, (*path, str(index))))
        return found
    return []


def validate_principle_content(content: dict) -> None:
    """Reject exactly what the live `arch.principle` schema rejects.

    Mirrors ArchPrincipleContent/ArchPrincipleStatusHistoryEntry: recursive
    measurement-key ban, required non-blank statement/rationale/source, a
    status in the known vocabulary, and every status_history entry carrying
    a parseable date, a valid status, and a non-blank reason.
    """
    paths = _forbidden_measurement_paths(content)
    if paths:
        rendered = ", ".join(".".join(p) for p in paths)
        raise PrincipleValidationError(
            f"principle content must not embed measurement fields: {rendered}"
        )

    for field_name in ("statement", "rationale", "source"):
        value = content.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise PrincipleValidationError(f"{field_name} must be a non-empty string")

    status = content.get("status")
    if status not in STATUS_VALUES:
        raise PrincipleValidationError(
            f"status must be one of {sorted(STATUS_VALUES)}, got {status!r}"
        )

    history = content.get("status_history", [])
    if not isinstance(history, list):
        raise PrincipleValidationError("status_history must be a list")
    for entry in history:
        if not isinstance(entry, dict):
            raise PrincipleValidationError("status_history entries must be objects")
        for key in ("date", "status", "reason"):
            if key not in entry:
                raise PrincipleValidationError(f"status_history entry missing {key!r}")
        try:
            _date.fromisoformat(str(entry["date"]))
        except ValueError:
            raise PrincipleValidationError(
                f"status_history entry has an invalid date: {entry['date']!r}"
            ) from None
        if entry["status"] not in STATUS_VALUES:
            raise PrincipleValidationError(
                f"status_history status must be one of {sorted(STATUS_VALUES)}, "
                f"got {entry['status']!r}"
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PrincipleValidationError("status_history entry reason must not be blank")


HISTORY_LABEL = "History"
_HISTORY_ITEM_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+`([a-z]+)`\s+\u2014\s+(.+)$"
)


def parse_status_history(entry: PrincipleEntry) -> list[dict] | None:
    """The entry's dated status transitions, from its `History:` bullet.

    Format, one bullet per entry, items `;`-separated so the existing
    bullet/continuation grammar (which space-joins rewrapped lines) cannot
    corrupt it: ``YYYY-MM-DD `status` \u2014 reason``. A reason therefore may
    not contain ``;`` — the item separator is reserved, and
    ``propose_promotion`` refuses a reason carrying one rather than drafting
    a note its own parser would reject (release-gate finding on the PR that
    landed this). Returns ``None`` when the
    entry carries no History bullet (pre-history entries keep the synthesized
    seed — backward compatible), and raises ``ValueError`` on a malformed
    item: a history that cannot be parsed must fail the sync loudly, not
    silently become a one-entry synthesis (the flattening this mechanism
    exists to end)."""
    for note in entry.notes:
        label, _, text = note.partition(": ")
        if label != HISTORY_LABEL:
            continue
        history: list[dict] = []
        for raw_item in text.split(";"):
            item = raw_item.strip()
            if not item:
                continue
            match = _HISTORY_ITEM_RE.match(item)
            if not match:
                raise ValueError(
                    f"{entry.id}: History item does not parse as "
                    f"'YYYY-MM-DD `status` \u2014 reason': {item!r}"
                )
            date_str, status, reason = match.groups()
            if status not in STATUS_VALUES:
                raise ValueError(
                    f"{entry.id}: History status {status!r} is not one of "
                    f"{sorted(STATUS_VALUES)}"
                )
            history.append({"date": date_str, "status": status, "reason": reason.strip()})
        if not history:
            raise ValueError(f"{entry.id}: History bullet is present but empty")
        return history
    return None


def content_for(entry: PrincipleEntry, seeded_date: str) -> dict:
    """The bead content for one principle. See .factory/design.md decisions 1-3
    for why `ref` is the identity key and `rationale` duplicates `source`.

    ``status_history`` is read from the entry's `History:` bullet when one
    exists (2026-09-07: the file gained a representation, so the registry is
    finally the reviewed home of dated transitions and a push can no longer
    flatten a real history into the synthesized seed). An entry with no
    History bullet keeps the original synthesized single-entry seed.

    Carries `status_note` verbatim (an extra field the schema's `extra="allow"`
    permits) so `entry_from_content` below can invert this mapping without
    re-parsing it out of a status_history reason string."""
    history = parse_status_history(entry)
    if history is None:
        reason = "Seeded from docs/architecture/principles.md at registry migration (F-DCE-2)."
        if entry.status_note:
            reason = f"{reason} {entry.status_note}"
        history = [{"date": seeded_date, "status": entry.status, "reason": reason}]
    return {
        "ref": entry.id,
        "name": entry.name,
        "statement": entry.statement,
        "rationale": entry.source,
        "source": entry.source,
        "status": entry.status,
        "status_note": entry.status_note,
        "status_history": history,
        "notes": list(entry.notes),
    }


def entry_from_content(content: dict) -> PrincipleEntry:
    """Invert `content_for`: bead content back into the same structured form
    `parse_registry` produces, field for field. `rationale` is not read here —
    `source` carries the same text (decision 2) and is the field `parse`
    itself populates."""
    return PrincipleEntry(
        id=content["ref"],
        name=content.get("name", ""),
        statement=content["statement"],
        source=content.get("source", ""),
        status=content["status"],
        status_note=content.get("status_note", ""),
        notes=tuple(content.get("notes", [])),
    )


@dataclass
class Plan:
    """What push did, or would do. Printed whole so a dry run is reviewable."""

    dry_run: bool
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def plan_push(store: Any, registry: Registry, dry_run: bool) -> Plan:
    """Create or update one bead per PRIN id. `store` needs three methods:
    `list_principles()`, `create(content)`, `patch(bead_id, content)` — see
    `Substrate` below for the real implementation and `FakePrincipleStore` in
    the test suite for the double. Idempotent on `content["ref"]`."""
    plan = Plan(dry_run=dry_run)
    seeded_date = registry.seeded_date()

    existing: dict[str, dict] = {}
    for bead in store.list_principles():
        ref = (bead.get("content") or {}).get("ref")
        if ref:
            existing[ref] = bead

    for entry in registry.entries:
        content = content_for(entry, seeded_date)
        current = existing.get(entry.id)
        if current is None:
            plan.created.append(entry.id)
            if not dry_run:
                store.create(content)
            continue
        if current.get("content") != content:
            plan.updated.append(entry.id)
            if not dry_run:
                store.patch(current["id"], content)
        else:
            plan.unchanged.append(entry.id)
    return plan


def report_plan(plan: Plan) -> str:
    head = "DRY RUN — nothing was written" if plan.dry_run else "applied"
    lines = [f"principles_sync push: {head}", ""]

    def section(title: str, items: list[str]) -> None:
        if not items:
            return
        lines.append(f"{title} ({len(items)})")
        lines.extend(f"  - {item}" for item in items)
        lines.append("")

    section("create", plan.created)
    section("update", plan.updated)
    lines.append(f"unchanged: {len(plan.unchanged)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# fetch — read arch.principle beads back as the same structured form parse()
# produces, so the file and the beads are diffable with the same tooling
# ---------------------------------------------------------------------------


def fetch_entries(store: Any) -> tuple[PrincipleEntry, ...]:
    """Read every `arch.principle` bead from `store` (same three-method shape
    `plan_push` uses: only `list_principles()` is needed here) and return
    them as `PrincipleEntry`, sorted by id. Beads with no `ref` are not this
    registry's beads (a stray or hand-created bead) and are skipped, same as
    `plan_push`'s `existing` index does."""
    entries = []
    for bead in store.list_principles():
        content = bead.get("content") or {}
        if not content.get("ref"):
            continue
        entries.append(entry_from_content(content))
    entries.sort(key=lambda entry: entry.id)
    return tuple(entries)


# ---------------------------------------------------------------------------
# check-view — is docs/architecture/principles.md still the beads' view?
# ---------------------------------------------------------------------------

_ENTRY_DIFF_FIELDS = ("name", "statement", "source", "status", "status_note", "notes")


def diff_view(
    file_entries: tuple[PrincipleEntry, ...], bead_entries: tuple[PrincipleEntry, ...]
) -> list[str]:
    """Field-level diff between the file's entries and the beads' entries,
    each line naming the PRIN id and the field (or noting an id present on
    only one side). Empty means the view is not stale."""
    file_by_id = {entry.id: entry for entry in file_entries}
    bead_by_id = {entry.id: entry for entry in bead_entries}
    lines: list[str] = []

    for missing_id in sorted(set(file_by_id) - set(bead_by_id)):
        lines.append(f"{missing_id}: present in the file, missing from the beads")
    for extra_id in sorted(set(bead_by_id) - set(file_by_id)):
        lines.append(f"{extra_id}: present in the beads, missing from the file")

    for entry_id in sorted(set(file_by_id) & set(bead_by_id)):
        file_entry = file_by_id[entry_id]
        bead_entry = bead_by_id[entry_id]
        for field_name in _ENTRY_DIFF_FIELDS:
            file_value = getattr(file_entry, field_name)
            bead_value = getattr(bead_entry, field_name)
            if file_value != bead_value:
                lines.append(
                    f"{entry_id} {field_name}: file={file_value!r} beads={bead_value!r}"
                )
    return lines


def check_view(store: Any, file_text: str) -> tuple[bool, list[str]]:
    """Fetch the beads and render them, then compare the *structured* result
    against the file's own parse — not the two texts byte-for-byte, since
    `render_registry` does not replay the file's original line-wrap column
    width (decision 6) and a rewrap is not drift. Round-tripping the render
    back through `parse_registry` normalizes that away before `diff_view`
    names which id and field actually differ."""
    file_registry = parse_registry(file_text)
    bead_entries = fetch_entries(store)
    rendered = render_registry(Registry(header=file_registry.header, entries=bead_entries))
    rendered_entries = parse_registry(rendered).entries

    diffs = diff_view(file_registry.entries, rendered_entries)
    return (not diffs, diffs)


# ---------------------------------------------------------------------------
# propose-promotion — draft a status_history transition; write nothing
# ---------------------------------------------------------------------------


class PromotionError(ValueError):
    """Raised when propose-promotion is asked to draft a transition for an
    unknown id, an unknown target status, or a no-op (target == current)."""


REASON_PLACEHOLDER = (
    "<reason: fill in before opening the PR — why did this transition earn its way?>"
)


@dataclass(frozen=True)
class ProposedPromotion:
    """What propose-promotion drafts. Nothing here is written to disk or to
    the substrate — `diff` is text for a human to paste into a PR."""

    id: str
    from_status: str
    to_status: str
    status_history_entry: dict
    diff: str


def propose_promotion(
    registry: Registry,
    principle_id: str,
    target_status: str,
    reason: Optional[str] = None,
    today: Optional[str] = None,
) -> ProposedPromotion:
    entries_by_id = {entry.id: entry for entry in registry.entries}
    entry = entries_by_id.get(principle_id)
    if entry is None:
        raise PromotionError(f"unknown principle id {principle_id!r}")
    if target_status not in STATUS_VALUES:
        raise PromotionError(
            f"{target_status!r} is not a known status ({sorted(STATUS_VALUES)})"
        )
    if target_status == entry.status:
        raise PromotionError(
            f"{principle_id} is already {entry.status!r} — not a transition"
        )

    date_str = today or _date.today().isoformat()
    reason_text = reason.strip() if reason and reason.strip() else REASON_PLACEHOLDER
    if ";" in reason_text:
        raise PromotionError(
            "a History reason may not contain ';' — it is the item separator, "
            "and a note drafted with one would wedge every later parse of the "
            f"registry: {reason_text!r}"
        )
    history_entry = {"date": date_str, "status": target_status, "reason": reason_text}

    history_item = f"{date_str} `{target_status}` \u2014 {reason_text}"
    new_notes: list[str] = []
    history_note_found = False
    for note in entry.notes:
        label, _, text = note.partition(": ")
        if label == HISTORY_LABEL:
            new_notes.append(f"{HISTORY_LABEL}: {text}; {history_item}")
            history_note_found = True
        else:
            new_notes.append(note)
    if not history_note_found:
        new_notes.append(f"{HISTORY_LABEL}: {history_item}")

    new_entry = PrincipleEntry(
        id=entry.id,
        name=entry.name,
        statement=entry.statement,
        source=entry.source,
        status=target_status,
        status_note=reason_text,
        notes=tuple(new_notes),
    )
    old_block = _render_entry(entry) + "\n"
    new_block = _render_entry(new_entry) + "\n"
    diff = "".join(
        difflib.unified_diff(
            old_block.splitlines(keepends=True),
            new_block.splitlines(keepends=True),
            fromfile=f"{REGISTRY_PATH.name} ({principle_id}, current)",
            tofile=f"{REGISTRY_PATH.name} ({principle_id}, proposed)",
        )
    )

    return ProposedPromotion(
        id=principle_id,
        from_status=entry.status,
        to_status=target_status,
        status_history_entry=history_entry,
        diff=diff,
    )


def report_promotion(promotion: ProposedPromotion) -> str:
    lines = [
        f"principles_sync propose-promotion: {promotion.id} "
        f"{promotion.from_status} -> {promotion.to_status}",
        "",
        "status_history transition to add to the arch.principle bead:",
        json.dumps(promotion.status_history_entry, indent=2),
        "",
        "registry entry diff (paste into the PR):",
        promotion.diff,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the real substrate client — only exercised by an operator's `push --apply`
# ---------------------------------------------------------------------------


class Substrate(_Substrate):
    """Only the calls `push`/`fetch`/`check-view` need, same shape as
    scripts/ea-load.py's client — the three `arch.principle`-specific calls
    (`list_principles`, `create`, `patch`) sit on top of the shared client's
    `list_beads`/`create`/`patch`, which carry the HTTP mechanics and the
    `X-API-Key` request/error handling.

    No retry logic: idempotency on `content["ref"]` is what makes a failed run
    safe to re-run, not error handling here.
    """

    def __init__(self) -> None:
        super().__init__(created_by=CREATED_BY, namespace=NAMESPACE, trust_tier=TRUST_TIER)

    def list_principles(self, limit: int = 1000) -> list[dict]:
        return self.list_beads(BEAD_TYPE, limit=limit)

    def create(self, content: dict) -> dict:
        return super().create(BEAD_TYPE, "active", content)

    def patch(self, bead_id: str, content: dict) -> dict:
        return super().patch(bead_id, {"content": content})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subparsers.add_parser("parse", help="read the registry into JSON entries")
    parse_cmd.add_argument("file", nargs="?", default=str(REGISTRY_PATH))

    push_cmd = subparsers.add_parser("push", help="materialize entries as arch.principle beads")
    push_cmd.add_argument("file", nargs="?", default=str(REGISTRY_PATH))
    mode = push_cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    mode.add_argument("--apply", action="store_true", help="create/update beads for real")

    render_cmd = subparsers.add_parser("render", help="regenerate the registry Markdown")
    render_cmd.add_argument("file", nargs="?", default=str(REGISTRY_PATH))

    subparsers.add_parser(
        "fetch", help="read arch.principle beads from the substrate as structured entries"
    )

    check_view_cmd = subparsers.add_parser(
        "check-view", help="exit 0 if the registry file matches the substrate beads"
    )
    check_view_cmd.add_argument("file", nargs="?", default=str(REGISTRY_PATH))

    promote_cmd = subparsers.add_parser(
        "propose-promotion",
        help="draft a status_history transition and an entry diff; writes nothing",
    )
    promote_cmd.add_argument("id", help="PRIN-NNN id")
    promote_cmd.add_argument("status", choices=sorted(STATUS_VALUES), help="target status")
    promote_cmd.add_argument(
        "--reason", default=None, help="reason for the transition; omitted -> a placeholder"
    )
    promote_cmd.add_argument("--file", default=str(REGISTRY_PATH))

    args = parser.parse_args(argv)

    if args.command == "fetch":
        entries = fetch_entries(Substrate())
        print(json.dumps([entry.as_dict() for entry in entries], indent=2))
        return 0

    if args.command == "propose-promotion":
        try:
            text = pathlib.Path(args.file).read_text()
        except OSError as exc:
            print(f"principles_sync: could not read {args.file}: {exc}", file=sys.stderr)
            return 2
        registry = parse_registry(text)
        try:
            promotion = propose_promotion(registry, args.id, args.status, reason=args.reason)
        except PromotionError as exc:
            print(f"principles_sync propose-promotion: refused — {exc}", file=sys.stderr)
            return 1
        print(report_promotion(promotion))
        return 0

    try:
        text = pathlib.Path(args.file).read_text()
    except OSError as exc:
        print(f"principles_sync: could not read {args.file}: {exc}", file=sys.stderr)
        return 2

    if args.command == "parse":
        registry = parse_registry(text)
        print(json.dumps([entry.as_dict() for entry in registry.entries], indent=2))
        return 0

    if args.command == "render":
        sys.stdout.write(render_registry(parse_registry(text)))
        return 0

    if args.command == "check-view":
        ok, diffs = check_view(Substrate(), text)
        if ok:
            print(f"principles_sync check-view: {args.file} matches the beads")
            return 0
        print(f"principles_sync check-view: {args.file} is stale against the beads")
        for line in diffs:
            print(f"  - {line}")
        return 1

    # push
    registry = parse_registry(text)
    store = Substrate()
    plan = plan_push(store, registry, dry_run=args.dry_run)
    print(report_plan(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
