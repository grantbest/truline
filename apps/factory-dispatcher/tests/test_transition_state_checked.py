"""The dispatcher must not trust a store to refuse an illegal dev.task edge.

beadstore.BeadStore.transition_state documents that a conforming store MUST
fail rather than no-op or clobber when the bead has moved on from
``from_state`` -- but that says nothing about a store that dutifully performs
a CAS onto an edge the dev.task machine never declared in the first place.
FakeSubstrate (test_dispatch.py) is exactly such a store: it enforces the
compare-and-set (current state must equal ``from_state``) but never checks
``to_state`` against dev_task_contract.DEV_TASK_STATE_MACHINE at all, which
makes it a faithful stand-in for the "single open -> in_progress edge" tool
described in beadstore.py's module docstring -- permissive on everything it
was not built to refuse.

These tests drive dispatch.transition_state_checked -- the one path
dispatch.py's six call sites now use instead of calling
BeadStore.transition_state directly -- to prove the machine is asserted
locally, before the store is ever asked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dev_task_contract  # noqa: E402
import dispatch  # noqa: E402

from test_dispatch import FakeSubstrate, bindable_task  # noqa: E402


def test_a_declared_edge_is_delegated_to_the_store():
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state="pending")])

    dispatch.transition_state_checked(sub, "task-1", "pending", "doing", "factory-agent")

    assert sub.transitions == [("task-1", "pending", "doing", "factory-agent")]


def test_an_edge_the_machine_never_declares_is_refused_before_any_store_call():
    """"done" -> "doing" is not in DEV_TASK_STATE_MACHINE at all. A store that
    only checks "is the bead currently in from_state" (FakeSubstrate; also the
    documented shape of a bd-backed adapter's single CAS edge) has nothing
    built in to refuse this with, and would perform it.
    """
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state="done")])

    with pytest.raises(dispatch.IllegalTransitionError) as excinfo:
        dispatch.transition_state_checked(sub, "task-1", "done", "doing", "factory-agent")

    assert excinfo.value.from_state == "done"
    assert excinfo.value.to_state == "doing"
    assert "done" in str(excinfo.value) and "doing" in str(excinfo.value)
    # The whole point: the store was never asked.
    assert sub.transitions == []


def test_a_permissive_fake_store_would_have_obeyed_the_illegal_edge():
    """Documents the failure this guard exists to prevent: called directly,
    with no local check in front of it, FakeSubstrate performs the exact
    transition the machine forbids and reports success -- proving the
    dispatcher cannot rely on the store alone to catch this.
    """
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state="done")])

    sub.transition_state("task-1", "done", "doing", "factory-agent")

    assert sub.transitions == [("task-1", "done", "doing", "factory-agent")]
    assert sub._task_by_id("task-1")["state"] == "doing"


@pytest.mark.parametrize("from_state,to_state", dev_task_contract.all_dev_task_edges())
def test_every_edge_the_machine_declares_legal_is_accepted(from_state, to_state):
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state=from_state)])

    dispatch.transition_state_checked(sub, "task-1", from_state, to_state, "factory-agent")

    assert sub.transitions == [("task-1", from_state, to_state, "factory-agent")]


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("done", "doing"),
        ("archived", "pending"),
        ("superseded", "doing"),
        ("pending", "review"),
        ("pending", "done"),
        ("doing", "archived"),
        ("failed", "review"),
        ("failed", "done"),
    ],
)
def test_a_sample_of_undeclared_edges_are_all_refused_locally(from_state, to_state):
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state=from_state)])

    with pytest.raises(dispatch.IllegalTransitionError):
        dispatch.transition_state_checked(sub, "task-1", from_state, to_state, "factory-agent")

    assert sub.transitions == []


def test_a_lost_race_still_raises_from_the_store_not_the_local_check():
    """The local check only judges the abstract edge. A from_state that is
    legal in the machine but no longer true of the bead is still the store's
    compare-and-set to catch.
    """
    sub = FakeSubstrate(tasks=[bindable_task("task-1", state="doing")])

    with pytest.raises(Exception):
        dispatch.transition_state_checked(sub, "task-1", "pending", "doing", "factory-agent")

    assert sub.transitions == []
