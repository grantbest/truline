"""Opens or attaches evidence to an ``arch.incident`` bead for an opted-in alert.

Only ``AlertDefinition``s that set ``opens_incident=True`` (docs/architecture/itsm-target-state.md
§3) ever reach this module — ``notify.send_alert`` is its sole caller, gated on that field, and no
current ``ALERT_INVENTORY`` entry opts in. Everything here is either "create the incident that
does not exist yet" or "attach evidence to the one that does" (PRIN-014): no retry, no requeue, no
disposition. Resolving or closing an incident is a separate, later governed action outside this
module's writes.

The identity key is ``(source=alert_id, alert_subject=the alert's own rendered per-subject "kind"
string)`` — reusing the dedup key ``notify.AlertPolicy`` already keys suppression state by, per
itsm-target-state.md §3's "the definition's existing per-subject dedup key becomes the incident
identity."

Reopen semantics follow the state machine actually registered in
``apps/substrate/src/bead_rules.py`` (``STATE_MACHINES[("arch", "incident")]``), not a looser
reading of the phrase "after the incident closed": that machine gives ``closed`` zero outgoing
edges — only ``resolved -> detected`` is a legal reopen. A recurrence found in ``resolved`` within
the declared window transitions that bead back to ``detected``; a recurrence found in ``closed``,
or in ``resolved`` outside the window, always opens a fresh incident rather than attempting an
illegal transition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ARCH_NAMESPACE = "arch"
INCIDENT_TYPE = "incident"
DEFAULT_REOPEN_WINDOW_HOURS = 24.0
INCIDENT_CREATED_BY = "notify/incident"

FindIncident = Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]
CreateIncident = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
AttachEvidence = Callable[[str, Dict[str, Any], Dict[str, Any]], Awaitable[None]]
ReopenIncident = Callable[[str, Dict[str, Any], Dict[str, Any]], Awaitable[None]]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_applications_for_alert(alert_id: str) -> List[str]:
    """The one portfolio application ref each alert family's condition is against.

    A stand-in for the "touched paths via the portfolio model" derivation
    itsm-target-state.md §2 describes for ``arch.change``, which does not
    exist yet for alerts. Every alert_id in ``ALERT_INVENTORY`` today belongs
    to one of exactly two verticals: ``factory_dispatcher.*`` is
    app.factory-dispatcher itself; everything else (bank_sync, budget_pulse,
    morning_brief, subscription_auditor, plaid) is the finance vertical
    living inside app.mcp-hub (docs/architecture/model/application-portfolio.yaml
    app.mcp-hub's own note: "in practice, the entire finance vertical").
    """
    if alert_id.startswith("factory_dispatcher."):
        return ["app.factory-dispatcher"]
    return ["app.mcp-hub"]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class IncidentPolicy:
    """The single decision point for turning an opted-in firing into a bead write.

    The four callables are the read/write seam ``default_incident_policy()``
    fills with real HTTP calls; tests supply plain-Python fakes — the same
    shape ``notify.AlertPolicy`` already established for alert-state
    persistence.
    """

    find_incident: FindIncident
    create_incident: CreateIncident
    attach_evidence: AttachEvidence
    reopen_incident: ReopenIncident
    clock: Clock = _utc_now

    async def fire(
        self,
        alert_id: str,
        subject: str,
        *,
        summary: str,
        severity: str,
        applications: Optional[List[str]] = None,
        reopen_window_hours: float = DEFAULT_REOPEN_WINDOW_HOURS,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Open the incident this (alert_id, subject) identity has never had,
        attach evidence to the one still open, or reopen the one that recurred
        inside its declared window — never more than one write, never a
        duplicate for an identity already open."""
        now = self.clock()
        existing = await self.find_incident(alert_id, subject)
        evidence_entry: Dict[str, Any] = {"observed_at": now.isoformat(), **(evidence or {})}

        if existing is None:
            return await self._open(alert_id, subject, summary, severity, applications, now)

        state = existing.get("state")
        if state in ("detected", "mitigating"):
            await self.attach_evidence(existing["id"], existing, evidence_entry)
            return {"action": "attached", "id": existing["id"]}

        if state == "resolved":
            resolved_at = _parse_timestamp((existing.get("content") or {}).get("resolved_at"))
            if resolved_at is not None and now - resolved_at <= timedelta(hours=reopen_window_hours):
                await self.reopen_incident(existing["id"], existing, evidence_entry)
                return {"action": "reopened", "id": existing["id"]}
            return await self._open(alert_id, subject, summary, severity, applications, now)

        # state == "closed" (terminal — bead_rules.STATE_MACHINES gives it zero
        # outgoing edges) or anything else unrecognized: no legal transition
        # exists, so a recurrence always opens a fresh incident.
        return await self._open(alert_id, subject, summary, severity, applications, now)

    async def _open(
        self,
        alert_id: str,
        subject: str,
        summary: str,
        severity: str,
        applications: Optional[List[str]],
        now: datetime,
    ) -> Dict[str, Any]:
        content = {
            "summary": summary,
            "severity": severity,
            "detected_at": now.isoformat(),
            "source": alert_id,
            "alert_subject": subject,
            "applications": applications or default_applications_for_alert(alert_id),
        }
        created = await self.create_incident(content)
        return {"action": "opened", "id": created.get("id"), "bead": created}


async def _find_incident(alert_id: str, subject: str) -> Optional[Dict[str, Any]]:
    """Most recent arch.incident bead for (alert_id, subject), or None.

    Exactly the packaged substrate_client's ``list_beads`` shape
    (namespace/type/limit, no state filter) -- routed there via
    ``finance._call_substrate``, the one shared lazy-load-and-offload seam
    (M12), instead of a second copy of it. Still fetches the population and
    filters client-side: ``GET /beads`` has no content-field filter beyond
    ``content_ref`` (apps/substrate/src/routes.py's
    ``_LIST_BEADS_QUERY_PARAMS``).
    """
    from .finance import _call_substrate

    beads = await _call_substrate("list_beads", ARCH_NAMESPACE, INCIDENT_TYPE, limit=500)
    matches = [
        bead
        for bead in beads
        if (bead.get("content") or {}).get("source") == alert_id
        and (bead.get("content") or {}).get("alert_subject") == subject
    ]
    if not matches:
        return None
    return max(matches, key=lambda bead: (bead.get("content") or {}).get("detected_at") or "")


async def _create_incident(content: Dict[str, Any]) -> Dict[str, Any]:
    from .finance import _call_substrate

    return await _call_substrate(
        "create_bead",
        ARCH_NAMESPACE,
        INCIDENT_TYPE,
        "detected",
        content,
        INCIDENT_CREATED_BY,
        trust_tier="system",
    )


async def _attach_evidence(
    bead_id: str, existing: Dict[str, Any], evidence_entry: Dict[str, Any]
) -> None:
    """Whole-content read-modify-write -- the API has no field-level merge
    (apps/factory-dispatcher/substrate.py's ``patch_content`` carries the same
    warning): append to ``content["evidence"]`` and PATCH the full dict back."""
    from .finance import _call_substrate

    content = dict(existing.get("content") or {})
    entries = list(content.get("evidence") or [])
    entries.append(evidence_entry)
    content["evidence"] = entries
    await _call_substrate("patch_content", bead_id, content, INCIDENT_CREATED_BY)


async def _reopen_incident(
    bead_id: str, existing: Dict[str, Any], evidence_entry: Dict[str, Any]
) -> None:
    """``resolved -> detected`` compare-and-set, then the same evidence attach
    a plain re-fire against an already-open incident gets."""
    from .finance import _call_substrate

    await _call_substrate("transition_state", bead_id, "resolved", "detected", INCIDENT_CREATED_BY)
    await _attach_evidence(bead_id, existing, evidence_entry)


def default_incident_policy() -> IncidentPolicy:
    """The substrate-backed policy ``notify.send_alert`` uses when a caller
    supplies no override — real HTTP calls against the same
    ``SUBSTRATE_URL``/``SUBSTRATE_API_KEY`` contract ``tools.finance`` and
    ``factory-dispatcher/substrate.py`` already use."""
    return IncidentPolicy(
        find_incident=_find_incident,
        create_incident=_create_incident,
        attach_evidence=_attach_evidence,
        reopen_incident=_reopen_incident,
    )
