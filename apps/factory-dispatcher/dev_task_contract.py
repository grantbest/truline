#!/usr/bin/env python3
"""The `dev.task` rules a second store must be held to — reachable without the web app.

``apps/substrate/src/bead_rules.py`` is the source of truth since #662
extracted ``STATE_MACHINES``, the entry states and ``BEAD_LINK_TYPES`` out of
the web layer into a module with zero imports. Amendment 35's original
complaint — that the rules were "unreachable to any caller that is not the
web app" — no longer holds; what remains is that the dispatcher ships and
tests as its own tree, so it keeps this local copy rather than a cross-app
import at runtime.

The copy is no longer trusted by hand: tests/test_dev_task_contract_parity.py
imports bead_rules directly and asserts equality on all three structures, so
a drift on either side fails the dispatcher suite (OPS-64 — the by-hand
lockstep this docstring used to request went unchecked, and the release gate
on #659 flagged that nothing anywhere failed on divergence).

A mismatch here is exactly the drift PC-SUB-004 / PC-SUB-003 exist to catch —
update both together, in the same change that touches the substrate side; the
parity test is what makes forgetting that fail.
"""

from __future__ import annotations

# Mirrors apps/substrate/src/bead_rules.py:STATE_MACHINES[("dev", "task")].
DEV_TASK_STATE_MACHINE: dict[str, frozenset[str]] = {
    "pending": frozenset({"doing", "superseded"}),
    "doing": frozenset({"review", "pending", "failed"}),
    "review": frozenset({"done", "pending", "failed"}),
    "done": frozenset({"archived"}),
    "failed": frozenset({"pending", "superseded"}),
    "archived": frozenset(),
    "superseded": frozenset(),
}

# Mirrors apps/substrate/src/bead_rules.py:STATE_MACHINE_ENTRY_STATES[("dev", "task")].
# A dev.task bead may only ever be CREATED in this state; every other state in
# DEV_TASK_STATE_MACHINE is reached by a governed transition, never by intake.
DEV_TASK_ENTRY_STATES: frozenset[str] = frozenset({"pending"})

# Mirrors apps/substrate/src/bead_rules.py:BEAD_LINK_TYPES — the full closed
# vocabulary, not the subset any one call site happens to use today.
BEAD_LINK_TYPES: frozenset[str] = frozenset(
    {
        "designs",
        "supersedes",
        "gates",
        "regresses",
        "found_by",
        "affects",
        "measures",
        "supports",
        "realizes",
        "depends_on",
        "consumes",
        "applies",
        "derived_from",
        "enforced_by",
        "caused_by",
        "resolved_by",
        "delivers",
        "threatens",
        "accepted_by",
    }
)

# The known divergence. bd 1.1.2's only compare-and-set is `bd update --claim`
# (verified against a real binary — see bdstore.py's module docstring), and it
# runs exactly open -> in_progress on an unassigned issue. Mapped onto
# DEV_TASK_STATE_MACHINE that is pending -> doing and NOTHING else: bd exposes
# no CAS for doing -> review, review -> pending, review -> failed,
# review -> done, failed -> pending, pending -> superseded, or
# failed -> superseded. This is not a gap to close by inventing an unverified
# workaround — see bdstore.BdStore.transition_state's BdUnsupportedTransition,
# and tests/test_bdstore.py's parametrized walk of every OTHER legal edge in
# DEV_TASK_STATE_MACHINE.
BD_CLAIM_SUPPORTED_EDGES: frozenset[tuple[str, str]] = frozenset({("pending", "doing")})


def all_dev_task_edges() -> list[tuple[str, str]]:
    """Every legal (from_state, to_state) pair in DEV_TASK_STATE_MACHINE."""
    return [
        (from_state, to_state)
        for from_state, to_states in DEV_TASK_STATE_MACHINE.items()
        for to_state in to_states
    ]
