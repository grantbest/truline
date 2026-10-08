"""Doctrine principle staleness measurement (F-DCE-5).

docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md, sprint 26. PRIN-011: an
`adopted`/`enforced` `arch.principle` bead with no incoming `applies` link and no incoming
`enforced_by` link, past a configurable window since its last dated status transition,
governs nothing — the system says so on a cadence instead of waiting for an audit.

This activity reads `arch.principle` beads and their incoming links, and writes
`arch.observation` beads. It never writes principle beads (doctrine.md's
ArchPrincipleContent deliberately omits staleness fields — that measurement lives on the
observation, never on principle structure) and never files `dev.task` work: doctrine rot
is a measurement stream, not a structure edit or a work generator.

One standing record per principle, not one per run (the A3 pattern). Unlike
staleness_report.py's per-day ref, this rule's observation ref carries only the principle
id, so a re-run while the condition persists finds and updates the same bead's
`measured_at` rather than creating a new one; when the condition clears — an `applies` or
`enforced_by` edge appears — the next run resolves it instead of leaving it standing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from temporalio import activity

import scanner

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

CREATED_BY = "factory-dispatcher/doctrine-staleness"
# "factory-dispatcher/doctrine-staleness" is enrolled in
# apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119,
# #822) -- declaring "derived" here depends on that enrolment being DEPLOYED,
# not just merged (unenrolled, this would be a 409). See .factory/design.md.
SOURCE_CLASS = "derived"
OBSERVATION_KIND = "doctrine_principle_staleness"
GOVERNING_LINK_TYPES = frozenset({"applies", "enforced_by"})
HTTP_TIMEOUT_S = 30.0


class PrincipleObservationStore(Protocol):
    def list_principles(self) -> list[dict[str, Any]]:
        ...

    def has_governing_link(self, principle_bead_id: str) -> bool:
        ...

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def update_observation(
        self, bead_id: str, content: dict[str, Any], context: dict[str, Any], created_by: str
    ) -> dict[str, Any]:
        ...

    def resolve_observation(self, bead_id: str, created_by: str) -> dict[str, Any]:
        ...


class SubstratePrincipleObservationStore:
    """Narrow client for the doctrine-staleness reads and writes this report owns."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def list_principles(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "principle", "limit": 1000},
        )

    def has_governing_link(self, principle_bead_id: str) -> bool:
        links = self._request(
            "GET",
            f"/beads/{principle_bead_id}/links",
            params={"direction": "incoming"},
        )
        return any(link.get("link_type") in GOVERNING_LINK_TYPES for link in links)

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={
                "namespace": "arch",
                "type": "observation",
                "content_ref": ref,
                "limit": 1,
            },
        )
        return found[0] if found else None

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_observation(
        self, bead_id: str, content: dict[str, Any], context: dict[str, Any], created_by: str
    ) -> dict[str, Any]:
        # Reopens a previously resolved observation as well as refreshing a
        # standing one: either way, a reason was just found for it, so the
        # record it points to must be active.
        return self._request(
            "PATCH",
            f"/beads/{bead_id}",
            json={
                "state": "active",
                "content": content,
                "context": context,
                "created_by": created_by,
            },
        )

    def resolve_observation(self, bead_id: str, created_by: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/beads/{bead_id}",
            json={"state": "resolved", "created_by": created_by},
        )


def run_doctrine_staleness_report(
    store: PrincipleObservationStore,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    window: timedelta = scanner.DOCTRINE_STALENESS_WINDOW,
    suppressions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate every adopted/enforced principle once and reconcile its observation."""
    now = now_fn()
    suppressed = scanner.load_suppressions() if suppressions is None else suppressions

    checked = 0
    flagged = 0
    created = 0
    updated = 0
    resolved = 0
    skipped_suppressed = 0

    for principle in store.list_principles():
        content = principle.get("content") or {}
        status = str(content.get("status") or "")
        if status not in ("adopted", "enforced"):
            continue
        checked += 1
        principle_id = str(content.get("ref") or "")

        dedupe_key = scanner.doctrine_staleness_dedupe_key(principle_id)
        if dedupe_key in suppressed:
            skipped_suppressed += 1
            continue

        has_link = store.has_governing_link(str(principle["id"]))
        reason = scanner.principle_staleness_reason(
            principle_id, content, has_link, now, window
        )
        ref = _observation_ref(principle_id)
        existing = store.find_observation(ref)

        if reason:
            flagged += 1
            last_transition = scanner.principle_last_transition_date(content) or ""
            obs_content, obs_context = _observation_content_and_context(
                principle_id, status, reason, last_transition, window, now
            )
            if existing is not None:
                store.update_observation(str(existing["id"]), obs_content, obs_context, CREATED_BY)
                updated += 1
            else:
                store.create_observation(
                    {
                        "namespace": "arch",
                        "type": "observation",
                        "state": "active",
                        "trust_tier": "system",
                        "created_by": CREATED_BY,
                        "content": obs_content,
                        "context": obs_context,
                    }
                )
                created += 1
        elif existing is not None and existing.get("state") == "active":
            store.resolve_observation(str(existing["id"]), CREATED_BY)
            resolved += 1

    return {
        "status": "reported",
        "checked": checked,
        "flagged": flagged,
        "observations_created": created,
        "observations_updated": updated,
        "observations_resolved": resolved,
        "suppressed": skipped_suppressed,
    }


@activity.defn(name="report_doctrine_staleness")
def report_doctrine_staleness_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    window_days = request.get("window_days")
    window = (
        timedelta(days=int(window_days)) if window_days else scanner.DOCTRINE_STALENESS_WINDOW
    )
    return run_doctrine_staleness_report(SubstratePrincipleObservationStore(), window=window)


def _observation_ref(principle_id: str) -> str:
    """Stable per principle, unlike staleness_report.py's per-day ref — one standing
    record per principle, not one per day the condition persists."""
    return f"obs.doctrine-staleness.{_ref_part(principle_id)}"


def _observation_content_and_context(
    principle_id: str,
    status: str,
    reason: str,
    last_transition: str,
    window: timedelta,
    observed_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    at = _iso(observed_at)
    content = {
        "ref": _observation_ref(principle_id),
        "source_class": SOURCE_CLASS,
        "observed_at": at,
        "workload": {
            "cluster": "repository",
            "namespace": "doctrine",
            "kind": "ArchPrinciple",
            "name": principle_id,
        },
    }
    context = {
        "observation_kind": OBSERVATION_KIND,
        "principle_id": principle_id,
        "status": status,
        "window_days": window.days,
        "last_transition_at": last_transition,
        "measured_at": at,
        "reason": reason,
    }
    return content, context


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"
