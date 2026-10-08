"""dev_task_contract must never drift from the store's declared rules.

``apps/substrate/src/bead_rules.py`` is the source of truth since #662
extracted the state machines and edge vocabulary out of the web layer:
it is a module with ZERO import statements, importable bare with no
FastAPI/SQLAlchemy stack — exactly the store-agnostic reachability
Amendment 35 said was missing. So the pin is a direct import and an
equality assertion; no AST derivation, no re-parsing of routes.py (this
test's first draft parsed the routes.py literal that #662 removed — the
release gate caught it going red on the merged tree).

The substrate-side mutation demonstration re-executes bead_rules' SOURCE
TEXT with a drifted edge (never the file on disk — apps/substrate/** is
out of scope for this change), so both drift directions are shown caught.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dev_task_contract  # noqa: E402

BEAD_RULES_PY = (
    Path(__file__).resolve().parents[2] / "substrate" / "src" / "bead_rules.py"
)
sys.path.insert(0, str(BEAD_RULES_PY.parent))

import bead_rules  # noqa: E402  (zero-import module; bare import is safe)


def test_dev_task_state_machine_matches_bead_rules():
    assert (
        bead_rules.STATE_MACHINES[("dev", "task")]
        == dev_task_contract.DEV_TASK_STATE_MACHINE
    ), (
        "dev_task_contract.DEV_TASK_STATE_MACHINE has drifted from "
        "apps/substrate/src/bead_rules.py:STATE_MACHINES[('dev', 'task')] -- "
        "update both together, in the same change."
    )


def test_dev_task_entry_states_match_bead_rules():
    assert (
        bead_rules.STATE_MACHINE_ENTRY_STATES[("dev", "task")]
        == dev_task_contract.DEV_TASK_ENTRY_STATES
    )


def test_bead_link_types_match_bead_rules():
    assert bead_rules.BEAD_LINK_TYPES == dev_task_contract.BEAD_LINK_TYPES, (
        "dev_task_contract.BEAD_LINK_TYPES has drifted from "
        "apps/substrate/src/bead_rules.py:BEAD_LINK_TYPES"
    )


def test_the_source_actually_declares_a_plausible_machine():
    """Both sides emptied together would pass equality vacuously; pin the
    source to something real."""
    assert ("dev", "task") in bead_rules.STATE_MACHINES
    assert ("arch", "release") in bead_rules.STATE_MACHINES
    machine = bead_rules.STATE_MACHINES[("dev", "task")]
    assert len(machine) >= 5
    assert all(isinstance(v, frozenset) for v in machine.values())
    assert len(bead_rules.BEAD_LINK_TYPES) >= 15


def test_a_mutated_contract_side_is_caught():
    drifted = dict(dev_task_contract.DEV_TASK_STATE_MACHINE)
    drifted["archived"] = frozenset({"pending"})  # bead_rules declares archived terminal
    assert drifted != bead_rules.STATE_MACHINES[("dev", "task")]


def test_a_mutated_bead_rules_side_is_caught():
    """Re-execute bead_rules' source text with one edge widened (the file on
    disk is never touched) and show the equality would fail."""
    source = BEAD_RULES_PY.read_text()
    drifted_source = source.replace(
        '"archived": frozenset(),',
        '"archived": frozenset({"pending"}),',
        1,
    )
    assert drifted_source != source, (
        "the substitution did not match bead_rules.py's current text -- "
        "its formatting changed, update this probe"
    )
    namespace: dict = {}
    exec(compile(drifted_source, str(BEAD_RULES_PY), "exec"), namespace)
    assert (
        namespace["STATE_MACHINES"][("dev", "task")]
        != dev_task_contract.DEV_TASK_STATE_MACHINE
    )
