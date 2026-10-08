#!/usr/bin/env python3
"""Lane -> architecture-principle mapping, and the offline principles.md parser wiring.

F-DCE-3 (docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md, sprint 24): a
lane declares which PRIN ids are relevant to its work; this module resolves that into the
statements a worker prompt can embed, by parsing docs/architecture/principles.md with the
parser scripts/principles_sync.py already ships (S23-B2, #449) — offline, in-repo, no
network at dispatch time.

Kept out of guards.py on purpose: guards.py's whole design point is being testable "without
a repo, a network, or an agent" (its own module docstring). The I/O and parsing happen here;
guards.py only ever receives an already-resolved ``Iterable[(id, statement)]``.

Only ``adopted``/``enforced`` principles are embeddable — a ``proposed`` principle is a
conjecture under review, and the platform-doctrine skill's own rule is that conjectures are
not injected as context.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# scripts/principles_sync.py does `import substrate_client` unqualified, so scripts/ has to be
# on sys.path for that import to resolve — not scripts as a package. Nothing under scripts/
# is created or modified here, only imported, per this task's explicit direction.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import principles_sync  # noqa: E402

PRINCIPLES_MD_PATH = Path("docs") / "architecture" / "principles.md"

#: Seed mapping (F-DCE-3). Data, reviewable in one place — extend here when a lane needs a
#: different doctrine set. Order is the order citations are embedded in the prompt and named
#: in the PR body's "Applies:" line.
LANE_PRINCIPLES: dict[str, tuple[str, ...]] = {
    "code-health": ("PRIN-003", "PRIN-005", "PRIN-008"),
    "bug-triage": ("PRIN-008", "PRIN-011"),
    "drift": ("PRIN-011",),
    "feature": ("PRIN-010", "PRIN-012"),
}

#: Statuses binding enough to hand a worker as fact. See module docstring.
EMBEDDABLE_STATUSES = frozenset({"adopted", "enforced"})


class PrinciplesParseError(RuntimeError):
    """docs/architecture/principles.md could not be read or parsed at dispatch time.

    A worker must never run with silently-absent doctrine, so this is meant to propagate
    all the way to the caller and stop the dispatch before the task is claimed.
    """


@dataclass(frozen=True)
class PrincipleCitation:
    id: str
    statement: str


def citations_for_lane(principles_md_text: str, lane: str) -> tuple[PrincipleCitation, ...]:
    """Pure: parse already-read text, filter to one lane's adopted/enforced ids.

    A PRIN id mapped for the lane but missing from the registry, or present with status
    ``proposed``/``retired``, is silently skipped — this is a soft citation list, not a
    completeness gate on the registry. A genuine parse failure (malformed file) raises
    :class:`PrinciplesParseError` instead of returning an empty result, so a broken registry
    can never look like "no doctrine for this lane".
    """
    wanted = LANE_PRINCIPLES.get(lane) or ()
    if not wanted:
        return ()
    try:
        registry = principles_sync.parse_registry(principles_md_text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same fault
        raise PrinciplesParseError(f"could not parse principles.md: {exc}") from exc

    by_id = {entry.id: entry for entry in registry.entries}
    citations: list[PrincipleCitation] = []
    for prin_id in wanted:
        entry = by_id.get(prin_id)
        if entry is None or entry.status not in EMBEDDABLE_STATUSES:
            continue
        citations.append(PrincipleCitation(id=entry.id, statement=entry.statement))
    return tuple(citations)


def load_citations_for_lane(repo_root: Path, lane: str) -> tuple[PrincipleCitation, ...]:
    """I/O wrapper: read principles.md under ``repo_root`` and delegate to
    :func:`citations_for_lane`.

    Reads nothing, and raises nothing, when the lane has no mapped principles — a lane the
    mapping does not know about must dispatch exactly as it did before this feature existed.
    """
    if not (LANE_PRINCIPLES.get(lane) or ()):
        return ()
    path = repo_root / PRINCIPLES_MD_PATH
    if not path.exists():
        # A Config rooted below the repository has no docs/ tree — CI and the
        # test suite run pytest from apps/factory-dispatcher, so a cwd-derived
        # repo_root lands here. The registry is versioned with this module, so
        # the module's own repo root is the honest fallback; an isolated clone
        # carries the file at its repo_root and never reaches this branch.
        path = _REPO_ROOT / PRINCIPLES_MD_PATH
    try:
        text = path.read_text()
    except OSError as exc:
        raise PrinciplesParseError(f"could not read {path}: {exc}") from exc
    return citations_for_lane(text, lane)


def as_prompt_pairs(
    citations: Iterable[PrincipleCitation],
) -> tuple[tuple[str, str], ...]:
    """Render citations as the (id, statement) pairs guards.py's renderer expects."""
    return tuple((citation.id, citation.statement) for citation in citations)
