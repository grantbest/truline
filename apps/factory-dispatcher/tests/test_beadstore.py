"""Tests for the BeadStore protocol (Path 1, Track B1).

Two things are worth testing about an interface that exists to gain a second
implementation:

  1. The implementation we already ship satisfies it. If ``Substrate`` ever
     drifts from the protocol, that is the moment ``bd`` work would break, and
     it should surface here rather than during a migration.

  2. The protocol's own contract is stated in a form a future adapter can be
     checked against — specifically that ``transition_state`` RAISES on a lost
     race rather than returning falsy or silently clobbering. An adapter that
     no-ops instead would reintroduce the exact double-claim PR #191 fixed, and
     nothing else in the dispatcher would notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beadstore import BeadStore  # noqa: E402
from substrate import Substrate, SubstrateError  # noqa: E402


REQUIRED = (
    "list_tasks",
    "list_notes",
    "find_bead",
    "list_links",
    "list_beads",
    "list_events",
    "create_task",
    "create_bead",
    "set_state",
    "transition_state",
    "patch_content",
    "patch_context",
    "add_note",
    "add_link",
)


def test_substrate_satisfies_the_protocol():
    """The shipping implementation conforms, so adding the protocol is a no-op."""
    assert issubclass(Substrate, BeadStore)


@pytest.mark.parametrize("name", REQUIRED)
def test_substrate_implements_every_method(name):
    """runtime_checkable Protocol only checks names exist; be explicit about which."""
    assert callable(getattr(Substrate, name, None)), f"Substrate is missing {name}()"


def test_protocol_surface_is_exactly_fourteen_methods():
    """A fat abstraction here would be a second place for the schema to drift.

    If this fails, someone widened the interface — make sure that was deliberate
    and that every implementation grew the method too.

    Widened from six to eight for F-DCE-3 (``find_bead``, ``add_link``): the
    dispatcher needs to resolve a ``PRIN-NNN`` id to a bead and write an
    ``applies`` edge back. Both are generic bead-graph primitives, not
    principle-specific — reusable the next time a bead needs a typed edge home.

    Widened from eight to nine for the release-state gate (2026-08-30 decision
    record, "the backlog is filed in reverse", D3): ``pick_task`` needs to read
    a task's outgoing ``delivers`` edge back, and ``add_link`` only writes one.

    Widened from nine to ten for the intake-path seam (Amendment 35 /
    PC-SUB-004, PC-SUB-003): every prior method covers the drain half of the
    dispatcher's work on a bead it did not create. ``create_task`` is the
    method that lets a second store be held to the intake path at all.

    Widened from ten to eleven by the release-gate review of #635:
    ``list_beads`` was already on the dispatcher's call surface (the
    release-state gate and reconciler read ``("arch", "release")`` through
    it) while the contract did not carry it — the half-covered seam the
    derived call-surface test now polices. BdStore declares its refusal.

    Widened from eleven to twelve for #635's own predecessor bead: a caller
    that needs to know when a bead entered its current state has no way to
    ask, so ``list_events`` joins the protocol as a read with no call site
    yet — 72067fdb (waiting-queue age from the event log) is the consumer.

    Widened from twelve to fourteen for OPS-190, the predecessor of e1cf3515
    (the console-surface probe): ``create_bead`` (a generic create, gaining
    optional ``context``/``provenance`` keywords) and ``patch_context`` (a
    whole-context replace, mirroring ``patch_content``) join the protocol
    with no call site yet, so a store bound to the probe's own arch.observation
    write has somewhere to put data that does not belong in ``content``.
    """
    surface = {
        n
        for n in dir(BeadStore)
        if not n.startswith("_") and callable(getattr(BeadStore, n, None))
    }
    assert surface == set(REQUIRED)


def test_a_method_added_to_the_protocol_fails_a_stub_missing_it():
    """The mechanism behind every "SHALL fail the build" acceptance criterion
    here: ``runtime_checkable`` Protocol conformance is structural, so a stub
    implementing every method except the newest one stops satisfying
    ``BeadStore`` the moment that method is added — exactly what would happen
    to a real adapter that had not caught up yet.
    """

    class _MissingCreateTask:
        def list_tasks(self, state=None, limit=200):
            ...

        def list_notes(self, parent_id, limit=500):
            ...

        def find_bead(self, namespace, type, content_ref):
            ...

        def list_links(self, bead_id, *, direction="both", link_type=None):
            ...

        def set_state(self, bead_id, state, created_by):
            ...

        def transition_state(self, bead_id, from_state, to_state, created_by):
            ...

        def patch_content(self, bead_id, content, created_by):
            ...

        def add_note(self, parent_id, kind, body, created_by, **extra):
            ...

        def add_link(self, source_id, target_id, link_type, created_by):
            ...

        # create_task deliberately omitted.

    assert not issubclass(_MissingCreateTask, BeadStore)
    assert issubclass(Substrate, BeadStore)


class _LosesTheRace:
    """An adapter that returns falsy instead of raising when the CAS fails.

    This is the plausible-looking bug: `bd update --claim` prints its rejection
    to stderr and exits 1, so an adapter that checks output text (or reads a
    pipeline's exit code) sees "nothing on stdout" and reports "no change".
    """

    def transition_state(self, bead_id, from_state, to_state, created_by):
        return {}


class _HonoursTheContract:
    def transition_state(self, bead_id, from_state, to_state, created_by):
        raise SubstrateError(409, "conflict: bead is not in from_state")


def _claim(store) -> bool:
    """The dispatcher's claim idiom: raised == lost the race."""
    try:
        store.transition_state("t-1", "pending", "doing", "test")
        return True
    except SubstrateError:
        return False


def test_a_conforming_adapter_reports_a_lost_race():
    assert _claim(_HonoursTheContract()) is False


def test_an_adapter_that_returns_instead_of_raising_silently_double_claims():
    """Documents the failure mode, so the contract is not just prose.

    A `bd` adapter MUST raise on exit code 1. If it returns instead, the
    dispatcher believes it owns a task another dispatcher is already running.
    """
    assert _claim(_LosesTheRace()) is True  # i.e. it wrongly thinks it won
