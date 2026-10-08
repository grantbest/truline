"""OPS-83: the two OPS-74 reconcilers must stamp ``observed_at`` in one format.

``ea_apply`` and ``requirements_apply`` used to format their own ``observed_at`` --
``ea_apply`` in second-precision ``Z``, ``requirements_apply`` in microsecond
``+00:00`` -- so a consumer comparing sibling status beads' ages parsed two shapes.
``status_bead.iso`` is now the one implementation both use; this pins that both
reconcilers' fresh writes agree, in the second-precision ``Z`` shape that the wider
five-writer convention (``change_apply``, ``release_status``, ``ea_observation``,
``doctrine_staleness``, ``worker_revision_drift``) already stamps.

OPS-8x (#710's gate trail): ``write_status``'s first-landing branch -- no existing
bead found, so a new one is created -- calls ``sub.create("observation", "active", ...)``.
Neither caller's own test suite ever inspects the state a fresh landing is created
with (``test_ea_apply.py``/``test_requirements_apply.py`` assert on ``content`` and
``context``, never ``state``; ``test_requirements_apply.py``'s own ``FakeSub.create``
does not even retain the ``state`` argument on the bead it returns), so a mutation
that changed the literal ``"active"`` passed on first landing survived the suite.
``test_the_first_landing_of_a_status_bead_is_created_active`` below pins it directly
against ``status_bead.write_status``, independent of either reconciler and of the
``observed_at`` format OPS-83/OPS-84 govern.

OPS-87: the surviving sibling was the update branch (``write_status``'s ``else``) --
same gap, because ``test_requirements_apply.py``'s ``FakeSub.patch`` filtered ``"state"``
out of every patch body it applied, so a mutation to that branch's ``"active"`` literal
also survived. ``test_the_update_path_of_a_status_bead_is_patched_active`` below closes
it the same way; the fake was tightened to stop dropping fields a real substrate keeps.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import ea_apply, requirements_apply, status_bead  # noqa: E402

NOW = datetime(2026, 9, 8, 12, 0, 0, 654321, tzinfo=timezone.utc)

# The shape every one of the five-writer convention's members (change_apply,
# release_status, ea_observation, doctrine_staleness, worker_revision_drift) emits,
# and that worker_revision_drift's own reader requires on the way back in.
SECOND_PRECISION_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_iso_helper_formats_second_precision_z():
    assert status_bead.iso(NOW) == "2026-09-08T12:00:00Z"


def test_both_reconcilers_fresh_writes_agree_on_observed_at_format():
    ea_content = ea_apply._status_content("rev-1", NOW)
    requirements_content = requirements_apply._status_content("rev-1", NOW)

    assert ea_content["observed_at"] == requirements_content["observed_at"]
    assert SECOND_PRECISION_Z.match(ea_content["observed_at"])
    assert SECOND_PRECISION_Z.match(requirements_content["observed_at"])


class _RecordingSubstrate:
    """Retains every argument ``write_status`` passes to ``create``/``patch``,
    unlike ``test_requirements_apply.py``'s ``FakeSub`` which drops ``state``."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.patch_calls: list[dict[str, Any]] = []

    def list_beads(self, bead_type: str, limit: int = 1000) -> list[dict[str, Any]]:
        return []

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict[str, Any] | None:
        return None

    def create(
        self, bead_type: str, state: str, content: dict[str, Any], parent_id,
        *, created_by: str = "",
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "bead_type": bead_type, "state": state, "content": content,
                "parent_id": parent_id, "created_by": created_by,
            }
        )
        return {"id": "bead-1", "type": bead_type, "state": state, "content": content}

    def patch(self, bead_id: str, body: dict[str, Any], *, created_by: str = "") -> dict[str, Any]:
        self.patch_calls.append({"bead_id": bead_id, "body": body, "created_by": created_by})
        return {"id": bead_id, **body}


def test_the_first_landing_of_a_status_bead_is_created_active():
    """No existing status bead: ``write_status`` must create it in state ``"active"``,
    not some other state -- a fresh landing that never reconciled is exactly as active
    as one that has been reconciling for a year; nothing else stamps this state."""
    sub = _RecordingSubstrate()
    content = {"ref": "some-status", "observed_at": "2026-09-08T12:00:00Z", "workload": {}}

    status_bead.write_status(sub, None, content, {"status": "ok"}, created_by="test")

    assert len(sub.create_calls) == 1
    assert sub.create_calls[0]["state"] == "active"


class _NinetyDupRefSubstrate(_RecordingSubstrate):
    """A create refused by the store's own unique-ref constraint (migration
    0006_unique_arch_ref) -- the shape OPS-181/OPS-182 measured: `find_status`
    reported no existing bead, but a bead with this `content.ref` already exists,
    so the store's 409 carries a body a finance-integrity mapper elsewhere
    mislabels (OPS-182). ``write_status`` must never let that body text into the
    reason it stamps."""

    class _Refused(RuntimeError):
        def __init__(self, status: int, body: str):
            super().__init__(f"substrate {status}: {body}")
            self.status = status
            self.body = body

    def create(self, bead_type, state, content, parent_id, *, created_by: str = ""):
        raise self._Refused(409, "plaid: duplicate transaction reference detected")


def test_a_409_duplicate_ref_create_raises_duplicate_ref_refusal_not_a_bare_exception():
    sub = _NinetyDupRefSubstrate()
    content = {"ref": "obs.some-status", "observed_at": "2026-09-08T12:00:00Z", "workload": {}}

    with pytest.raises(status_bead.DuplicateRefRefusal) as excinfo:
        status_bead.write_status(sub, None, content, {"status": "ok"}, created_by="test")

    assert excinfo.value.status == 409
    assert excinfo.value.ref == "obs.some-status"


def test_duplicate_ref_refusal_message_carries_status_and_ref_never_the_response_body():
    """OPS-182: until the finance-integrity mapper is fixed, the response's detail
    text is its plaid-duplicate mislabel -- the recorded reason must never contain
    it, only the status code and the ref."""
    sub = _NinetyDupRefSubstrate()
    content = {"ref": "obs.some-status", "observed_at": "2026-09-08T12:00:00Z", "workload": {}}

    with pytest.raises(status_bead.DuplicateRefRefusal) as excinfo:
        status_bead.write_status(sub, None, content, {"status": "ok"}, created_by="test")

    message = str(excinfo.value)
    assert "409" in message
    assert "obs.some-status" in message
    assert "plaid" not in message
    assert "duplicate transaction" not in message


def test_the_update_path_of_a_status_bead_is_patched_active():
    """OPS-85's sibling: an existing status bead -- ``write_status``'s ``else`` branch --
    must patch state back to ``"active"``, same as the first-landing branch does on
    create. A bead that has been reconciling for a year is exactly as active as one
    just created; nothing else stamps this state on the update path either.

    Pinned directly against ``status_bead.write_status`` with ``_RecordingSubstrate``,
    independent of either reconciler -- unlike ``test_requirements_apply.py``'s own
    ``FakeSub``, which used to filter ``"state"`` out of every patch body before this
    bead (OPS-87), so a mutation to the literal on the update path survived that suite."""
    sub = _RecordingSubstrate()
    existing = {"id": "obs-9", "content": {}, "context": {}}
    content = {"ref": "some-status", "observed_at": "2026-09-08T12:00:00Z", "workload": {}}

    status_bead.write_status(sub, existing, content, {"status": "ok"}, created_by="test")

    assert len(sub.patch_calls) == 1
    assert sub.patch_calls[0]["bead_id"] == "obs-9"
    assert sub.patch_calls[0]["body"]["state"] == "active"
