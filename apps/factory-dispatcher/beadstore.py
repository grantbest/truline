#!/usr/bin/env python3
"""The store interface the dispatcher actually depends on.

Path 1 Track B1. The dispatcher currently talks to :class:`substrate.Substrate`
directly. Before `dev` can move to upstream ``bd`` (Amendment 24), the six calls
it makes have to become an interface with more than one implementation — this
is that interface, and nothing more.

Deliberately six methods, not a general SDK. Same reasoning as ``substrate.py``:
a fat abstraction here would be a second place for the schema to drift.

Conformance is structural (``typing.Protocol``), so ``Substrate`` already
satisfies this without importing or subclassing anything. That is the point —
adding the protocol is a no-op for the running dispatcher, which is what makes
it safe to land ahead of any ``bd`` work.

--------------------------------------------------------------------------
What a ``bd`` implementation has to honour (verified against bd 1.1.2)
--------------------------------------------------------------------------

**State vocabulary is not free.** ``bd`` accepts custom statuses via
``bd config set status.custom``, but a custom status is NOT claimable:

    $ bd config set status.custom "pending,doing,review,failed"
    $ bd update <id> --status pending && bd update <id> --claim
    Error claiming <id>: issue not claimable: status pending

``--claim`` is the only atomic compare-and-set ``bd`` exposes, and it runs
exactly ``open -> in_progress`` on an unassigned issue. Every other status —
``blocked``, ``deferred``, ``in_progress``, ``closed``, and any custom value —
returns ``not claimable``. So:

    dispatcher state   bd status      needs CAS?   note
    ----------------   ------------   ----------   -------------------------
    pending            open           YES          only claimable status
    doing              in_progress     -           where --claim lands
    review             (free choice)   no          set_state, unconditional
    failed             (free choice)   no          set_state, unconditional

``pending`` and ``doing`` are therefore pinned to bd's built-in vocabulary.
``review`` and ``failed`` are free — they are only ever reached through
:meth:`BeadStore.set_state`, which is unconditional against substrate today, so
mapping them to custom statuses loses nothing.

**Two traps for the adapter, both found the hard way:**

1. ``bd`` resolves its actor from ``--actor`` / ``$BEADS_ACTOR`` /
   ``git user.name`` / ``$USER``. ``--claim`` is documented "idempotent if
   already claimed by you" — so two dispatchers that share an identity will
   BOTH succeed at claiming the same task. The compare-and-set is only as good
   as the actor string; pass ``--actor`` explicitly, never inherit it.

2. A rejected claim exits **1** and writes to **stderr**, printing nothing on
   stdout. Assert on the exit code. (Reading a pipeline's ``$?`` gets you
   ``tail``'s status, not ``bd``'s, and a rejected claim then looks like a
   successful one.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

_SUBSTRATE_SRC = Path(__file__).resolve().parents[2] / "apps" / "substrate" / "src"
if str(_SUBSTRATE_SRC) not in sys.path:
    sys.path.insert(0, str(_SUBSTRATE_SRC))
from schemas import validate_candidate_bead  # noqa: E402


def validate_task_content(
    content: dict,
    created_by: str,
    *,
    trust_tier: str = "user",
    provenance: Optional[dict] = None,
) -> None:
    """Raise ``pydantic.ValidationError`` iff ``create_task(content, created_by,
    trust_tier=trust_tier)`` would be rejected by the live ``dev.task`` schema,
    without calling it on any :class:`BeadStore`.

    Imports ``apps/substrate/src/schemas.py`` directly — the same
    ``DevTaskContent``/``BeadProvenance`` models any ``BeadStore``
    implementation's writes are ultimately checked against — so a caller can
    establish that a candidate task bead is well-formed independently of
    which store answers ``create_task``, and before it is ever filed.
    """
    validate_candidate_bead(
        "dev", "task", content, provenance or {}, created_by,
        trust_tier=trust_tier, state="pending",
    )


def validate_note_content(
    content: dict,
    created_by: str,
    *,
    trust_tier: str = "system",
    provenance: Optional[dict] = None,
) -> None:
    """The ``add_note`` counterpart of :func:`validate_task_content`."""
    validate_candidate_bead(
        "dev", "note", content, provenance or {}, created_by,
        trust_tier=trust_tier, state="active",
    )


@runtime_checkable
class BeadStore(Protocol):
    """The dispatcher's view of a bead store.

    Implementations must preserve three properties, or the dispatcher is unsafe:

    * :meth:`transition_state` is a compare-and-set. It must fail — not
      silently no-op, and not clobber — when the bead is no longer in
      ``from_state``. This is what stops two dispatchers working one task
      (PR #191).
    * :meth:`patch_content` replaces content wholesale. Callers read, mutate,
      and write back the full dict; an implementation that merges instead would
      silently change dispatcher behaviour.
    * :meth:`create_task` must create the bead in the machine's single entry
      state (``pending`` for ``dev.task``) and nowhere else — this protocol had
      no intake method at all until Amendment 35 named it as the half of the
      seam nothing held a second store to.
    """

    # -- reads ---------------------------------------------------------------

    def list_tasks(self, state: str | None = None, limit: int = 200) -> list[dict]:
        """Tasks in the dev namespace, optionally filtered to one state."""
        ...

    def list_notes(self, parent_id: str, limit: int = 500) -> list[dict]:
        """Notes attached to one task, oldest-first."""
        ...

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict | None:
        """The one bead in ``namespace``/``type`` whose ``content["ref"]`` equals
        ``content_ref``, or None. Used to resolve a stable external id (e.g. a
        ``PRIN-NNN`` principle id) to the substrate id that owns it."""
        ...

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict]:
        """Edges touching ``bead_id``, e.g. a ``dev.task``'s outgoing ``delivers``
        edge to the ``arch.release`` it serves. Each entry carries at least
        ``source_id``, ``target_id`` and ``link_type``."""
        ...

    def list_beads(self, namespace: str, type: str, limit: int = 200) -> list[dict]:
        """Beads of one ``(namespace, type)`` — the dispatcher reads
        ``("arch", "release")`` through this for the release-state gate and
        the reconciler. Surfaced into the protocol by the release-gate review
        of #635: the call existed on the dispatcher's surface while the
        contract did not carry it, which is exactly the half-covered seam the
        suite exists to end."""
        ...

    def list_events(self, bead_id: str) -> list[dict]:
        """Every event recorded against ``bead_id``, oldest-first, as the
        substrate serialises them (each carrying at least ``event_type``,
        ``from_state``, ``to_state``, ``created_at``). No ``limit`` — the
        route it is drawn from takes only ``bead_id`` and has no pagination.

        Unknown-but-well-formed ``bead_id`` answers ``[]``, not a raise: the
        route has no existence check (unlike ``GET /beads/{bead_id}``), and
        every bead carries at least its ``created`` event, which makes ``[]``
        the unambiguous "no such bead" answer. A malformed id raises
        ``SubstrateError`` with a 422 status."""
        ...

    # -- writes --------------------------------------------------------------

    def create_task(self, content: dict, created_by: str, *, trust_tier: str = "user") -> dict:
        """File a new ``dev.task`` bead. The intake path.

        Always ``dev``/``task`` at the machine's one entry state (``pending``)
        — no ``state`` parameter, because ``STATE_MACHINE_ENTRY_STATES``
        (apps/substrate/src/routes.py) declares exactly one legal choice and a
        parameter for a choice that does not exist only invites a caller to
        eventually pass the wrong thing. MUST raise on content the store's
        schema rejects rather than silently filing a malformed bead. A caller
        may check this ahead of time with :func:`validate_task_content`.
        """
        ...

    def create_bead(
        self,
        namespace: str,
        type: str,
        state: str,
        content: dict,
        created_by: str,
        *,
        trust_tier: str = "user",
        context: dict | None = None,
        provenance: dict | None = None,
    ) -> dict:
        """Create a bead in any namespace, of any type, in any entry state
        the store accepts — ``create_task``'s generic counterpart, for every
        write that is not dev/task/pending.

        ``context`` and ``provenance``, when given, ride as their own
        top-level keys — ``BeadCreate.context`` / ``BeadCreate.provenance``
        (apps/substrate/src/schemas.py) — never folded into ``content``.
        Omitted (``None``), the request carries neither key at all.
        """
        ...

    def patch_context(self, bead_id: str, context: dict, created_by: str) -> dict:
        """Whole-context replace — ``patch_content``'s counterpart for
        ``context`` instead of ``content``. A PATCH carrying only context
        SHALL leave content untouched; the route merges nothing."""
        ...

    def set_state(self, bead_id: str, state: str, created_by: str) -> dict:
        """Unconditional state write. No concurrency guarantee — by design;
        the dispatcher only uses this for terminal-ish moves it already owns."""
        ...

    def transition_state(
        self, bead_id: str, from_state: str, to_state: str, created_by: str
    ) -> dict:
        """Atomic compare-and-set. MUST raise if the bead is not in ``from_state``.

        Substrate raises ``SubstrateError(409)``. A ``bd`` adapter maps this to
        ``bd update --claim`` and must raise on exit code 1 — see the module
        docstring for why exit code and not output text.
        """
        ...

    def patch_content(self, bead_id: str, content: dict, created_by: str) -> dict:
        """Whole-content replace. See the class docstring."""
        ...

    def add_note(
        self,
        parent_id: str,
        kind: str,
        body: str,
        created_by: str,
        trust_tier: str = "system",
        provenance: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict:
        """Append a note to a task. A caller may check ``{"kind": kind, "body":
        body, **extra}`` ahead of time with :func:`validate_note_content`."""
        ...

    def add_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict:
        """Create a directed edge ``source_id -> target_id``.

        MUST raise on an unrecognised ``link_type`` and on a duplicate
        ``(source_id, target_id, link_type)`` — see ``BEAD_LINK_TYPES`` and the
        ``uq_bead_link_edge`` constraint in ``apps/substrate/src/schemas.py`` /
        ``models.py``. Callers that treat the edge as provenance rather than a gate
        (F-DCE-3) are expected to catch and record rejections themselves."""
        ...
