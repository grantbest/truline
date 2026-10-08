"""The standing-status-bead landing shape shared by the revision-gated appliers (OPS-74).

``activities/ea_apply.py`` and ``activities/requirements_apply.py`` each land one standing
``arch.observation`` bead per condition (``content.ref`` names the condition; a re-run finds
and updates the same bead instead of accumulating one per firing) and carry a PRIN-008 failure
record forward on it. That landing shape -- find the bead, build its content, write it,
extract a prior failure from its context -- was byte-identical between the two reconcilers
except for the field each stores its own revision under, the ``created_by`` tag, and the
per-domain ``ref``/``workload``. This module is that shape; each caller supplies the values
that vary.

The OPS-74 extraction left each caller's ``observed_at``/``failed_at``/``applied_at`` timestamp
formatting (``_iso``) out of the shared shape: at the time, ``ea_apply``'s stamped second-precision
``Z`` while ``requirements_apply``'s stamped microsecond ``+00:00`` -- a real divergence, so leaving
it unshared kept the extraction's "byte-identical" claim true instead of papering over a difference
(OPS-83). That divergence is gone now: :func:`iso` is the one implementation both callers use,
producing second-precision ``Z`` -- the convention five other standing-status writers already stamp
(``change_apply``, ``release_status``, ``ea_observation``, ``doctrine_staleness``, and
``worker_revision_drift`` -- whose own reader also *expects* ``Z``), leaving
``requirements_apply``'s prior microsecond ``+00:00`` form as the sole outlier. Existing beads written before this unification keep
whatever format they were stamped with; nothing here rewrites history.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol


#: OPS-181/OPS-182: both callers' standing bead lives in ``arch.observation``.
#: ``find_status`` resolves it by ``content.ref`` through the store's own
#: indexed lookup, never by paging this population.
NAMESPACE = "arch"
BEAD_TYPE = "observation"


class StatusSubstrate(Protocol):
    """The three calls landing a standing status bead needs."""

    def find_bead(
        self, namespace: str, type: str, content_ref: str
    ) -> dict[str, Any] | None: ...

    def create(
        self, bead_type: str, state: str, content: dict[str, Any], parent_id: str | None,
        *, created_by: str = "",
    ) -> dict[str, Any]: ...

    def patch(
        self, bead_id: str, body: dict[str, Any], *, created_by: str = ""
    ) -> dict[str, Any]: ...


def iso(value: datetime) -> str:
    """Second-precision ``Z`` timestamp -- the standing convention the five other writers
    stamp (``change_apply``, ``release_status``, ``ea_observation``, ``doctrine_staleness``,
    ``worker_revision_drift``), now shared by both callers of this module."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def find_status(sub: StatusSubstrate, status_ref: str) -> dict[str, Any] | None:
    """Resolve the standing bead by ``content.ref`` through the store's own
    indexed lookup -- never a paged ``list_beads`` scan.

    OPS-181: a scan capped at the caller's page size (1000) stopped finding
    ``obs.ea-apply-status`` once nightly per-condition mints (~45-52/night,
    newest-first) pushed it past the first page -- every tick then treated the
    record as absent and attempted a `create`, which the store's own
    unique-ref constraint refused (OPS-182). ``find_bead`` asks the store for
    exactly this bead by its unique ``content.ref`` instead of walking toward
    it, so no page size is ever a countdown to this failure again.
    """
    return sub.find_bead(NAMESPACE, BEAD_TYPE, status_ref)


def status_content(
    status_ref: str, workload: dict[str, Any], revision: str | None, observed_at: str,
    *, source_class: str,
) -> dict[str, Any]:
    """``source_class`` is required, not defaulted: each caller's own writer identity is
    enrolled in ``apps/substrate/src/bead_rules.py``'s ``SOURCE_CLASS_WRITERS`` for a
    specific class (OPS-119, #822), and this shared shape must not silently pick one for
    a caller that forgot to pass it. See .factory/design.md."""
    content: dict[str, Any] = {
        "ref": status_ref,
        "source_class": source_class,
        "observed_at": observed_at,
        "workload": workload,
    }
    if revision:
        content["last_synced_revision"] = revision
    return content


class DuplicateRefRefusal(RuntimeError):
    """A create was refused because a bead with this ``content.ref`` already
    exists -- the store's own unique-ref constraint (migration
    0006_unique_arch_ref) catching a create/create race ``find_status``
    ordinarily prevents by finding the existing bead first (OPS-181/OPS-182).

    Carries the HTTP status and the ref, deliberately not the response's
    detail text: until OPS-182 lands, that text is the finance-integrity
    mapper's plaid-duplicate mislabel, and a standing failure record built
    from it inherits the mislabel.
    """

    def __init__(self, status: int, ref: str):
        super().__init__(f"duplicate-ref refusal: status={status} ref={ref!r}")
        self.status = status
        self.ref = ref


def write_status(
    sub: StatusSubstrate, existing: dict[str, Any] | None, content: dict[str, Any],
    context: dict[str, Any], *, created_by: str,
) -> None:
    if existing is None:
        try:
            bead = sub.create("observation", "active", content, None, created_by=created_by)
        except Exception as exc:
            status = getattr(exc, "status", None)
            ref = content.get("ref")
            if status == 409 and ref:
                raise DuplicateRefRefusal(status, ref) from exc
            raise
        sub.patch(bead["id"], {"context": context}, created_by=created_by)
    else:
        sub.patch(
            existing["id"],
            {"content": content, "context": context, "state": "active"},
            created_by=created_by,
        )


def prior_failure(context: dict[str, Any], revision_key: str) -> dict[str, Any] | None:
    if context.get("status") != "failed":
        return None
    return {
        "revision": context.get(revision_key),
        "reason": context.get("reason"),
        "failed_at": context.get("failed_at"),
    }
