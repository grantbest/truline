"""IncidentPolicy.fire: open once per (alert, subject), attach on re-fire, reopen
inside the declared window -- never a second incident for the same identity.

No network, no substrate: every callable IncidentPolicy takes is a plain
Python fake recording what would have been written, matching the fake style
test_notify.py already uses for AlertPolicy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools import incidents


def _clock(now: datetime):
    return lambda: now


class RecordingIncidentStore:
    """Fakes the four IncidentPolicy callables against an in-memory list."""

    def __init__(self, existing=None):
        self.beads = list(existing or [])
        self.created: list[dict] = []
        self.attached: list[tuple[str, dict]] = []
        self.reopened: list[tuple[str, dict]] = []
        self._next_id = len(self.beads) + 1

    async def find_incident(self, alert_id, subject):
        matches = [
            b for b in self.beads
            if (b.get("content") or {}).get("source") == alert_id
            and (b.get("content") or {}).get("alert_subject") == subject
        ]
        if not matches:
            return None
        return max(matches, key=lambda b: (b.get("content") or {}).get("detected_at") or "")

    async def create_incident(self, content):
        bead = {"id": f"incident-{self._next_id}", "state": "detected", "content": content}
        self._next_id += 1
        self.created.append(content)
        self.beads.append(bead)
        return bead

    async def attach_evidence(self, bead_id, existing, evidence_entry):
        self.attached.append((bead_id, evidence_entry))
        for bead in self.beads:
            if bead["id"] == bead_id:
                content = dict(bead.get("content") or {})
                entries = list(content.get("evidence") or [])
                entries.append(evidence_entry)
                content["evidence"] = entries
                bead["content"] = content

    async def reopen_incident(self, bead_id, existing, evidence_entry):
        self.reopened.append((bead_id, evidence_entry))
        for bead in self.beads:
            if bead["id"] == bead_id:
                bead["state"] = "detected"
        await self.attach_evidence(bead_id, existing, evidence_entry)

    def policy(self, now: datetime) -> incidents.IncidentPolicy:
        return incidents.IncidentPolicy(
            find_incident=self.find_incident,
            create_incident=self.create_incident,
            attach_evidence=self.attach_evidence,
            reopen_incident=self.reopen_incident,
            clock=_clock(now),
        )


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


async def test_no_existing_incident_opens_one_at_detected():
    store = RecordingIncidentStore()

    result = await store.policy(NOW).fire(
        "bank_sync.sync_failure", "sync_failure:chase",
        summary="Bank sync exhausted all scheduled attempts.",
        severity="urgent",
    )

    assert result["action"] == "opened"
    assert len(store.created) == 1
    content = store.created[0]
    assert content["source"] == "bank_sync.sync_failure"
    assert content["alert_subject"] == "sync_failure:chase"
    assert content["severity"] == "urgent"
    assert content["detected_at"] == NOW.isoformat()
    assert content["applications"] == ["app.mcp-hub"]
    assert store.attached == []
    assert store.reopened == []


def test_factory_dispatcher_alert_maps_to_its_own_application():
    assert incidents.default_applications_for_alert(
        "factory_dispatcher.dev_task_failed"
    ) == ["app.factory-dispatcher"]
    assert incidents.default_applications_for_alert(
        "bank_sync.sync_failure"
    ) == ["app.mcp-hub"]


async def test_refire_against_open_detected_incident_attaches_not_duplicates():
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "detected",
        "content": {
            "source": "bank_sync.sync_failure",
            "alert_subject": "sync_failure:chase",
            "detected_at": (NOW - timedelta(hours=1)).isoformat(),
        },
    }])

    result = await store.policy(NOW).fire(
        "bank_sync.sync_failure", "sync_failure:chase",
        summary="Bank sync exhausted all scheduled attempts.",
        severity="urgent",
        evidence={"content": "still failing"},
    )

    assert result == {"action": "attached", "id": "incident-1"}
    assert store.created == []
    assert len(store.attached) == 1
    bead_id, entry = store.attached[0]
    assert bead_id == "incident-1"
    assert entry["content"] == "still failing"
    assert entry["observed_at"] == NOW.isoformat()


async def test_refire_against_mitigating_incident_attaches_not_duplicates():
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "mitigating",
        "content": {"source": "a.b", "alert_subject": "s"},
    }])

    result = await store.policy(NOW).fire("a.b", "s", summary="x", severity="urgent")

    assert result == {"action": "attached", "id": "incident-1"}
    assert store.created == []


async def test_refire_within_reopen_window_reopens_resolved_incident():
    resolved_at = NOW - timedelta(hours=2)
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "resolved",
        "content": {
            "source": "a.b",
            "alert_subject": "s",
            "detected_at": (resolved_at - timedelta(hours=1)).isoformat(),
            "resolved_at": resolved_at.isoformat(),
        },
    }])

    result = await store.policy(NOW).fire(
        "a.b", "s", summary="x", severity="urgent", reopen_window_hours=24.0
    )

    assert result == {"action": "reopened", "id": "incident-1"}
    assert store.created == []
    assert len(store.reopened) == 1
    assert [b["state"] for b in store.beads if b["id"] == "incident-1"] == ["detected"]


async def test_refire_outside_reopen_window_opens_a_new_incident_not_a_reopen():
    resolved_at = NOW - timedelta(hours=48)
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "resolved",
        "content": {
            "source": "a.b",
            "alert_subject": "s",
            "detected_at": (resolved_at - timedelta(hours=1)).isoformat(),
            "resolved_at": resolved_at.isoformat(),
        },
    }])

    result = await store.policy(NOW).fire(
        "a.b", "s", summary="x", severity="urgent", reopen_window_hours=24.0
    )

    assert result["action"] == "opened"
    assert len(store.created) == 1
    assert store.reopened == []
    # The old resolved incident is untouched -- a second, independent bead exists.
    assert len([b for b in store.beads if b["id"] == "incident-1"]) == 1
    assert store.beads[0]["state"] == "resolved"


async def test_refire_against_closed_incident_always_opens_a_new_one():
    """closed is terminal in the landed machine (bead_rules.STATE_MACHINES
    gives it zero outgoing edges) -- even a recurrence a minute later must
    never attempt an illegal transition, regardless of any window."""
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "closed",
        "content": {
            "source": "a.b",
            "alert_subject": "s",
            "resolved_at": (NOW - timedelta(minutes=1)).isoformat(),
        },
    }])

    result = await store.policy(NOW).fire(
        "a.b", "s", summary="x", severity="urgent", reopen_window_hours=24.0
    )

    assert result["action"] == "opened"
    assert store.reopened == []
    assert len(store.created) == 1


async def test_resolved_incident_with_no_resolved_at_recorded_opens_fresh_rather_than_guessing():
    store = RecordingIncidentStore(existing=[{
        "id": "incident-1",
        "state": "resolved",
        "content": {"source": "a.b", "alert_subject": "s"},
    }])

    result = await store.policy(NOW).fire("a.b", "s", summary="x", severity="urgent")

    assert result["action"] == "opened"
    assert store.reopened == []


async def test_different_subjects_never_share_an_incident():
    store = RecordingIncidentStore()

    first = await store.policy(NOW).fire("a.b", "chase", summary="x", severity="urgent")
    second = await store.policy(NOW).fire("a.b", "wells", summary="x", severity="urgent")

    assert first["action"] == "opened"
    assert second["action"] == "opened"
    assert first["id"] != second["id"]
    assert len(store.created) == 2


def test_default_incident_policy_is_substrate_backed():
    policy = incidents.default_incident_policy()

    assert policy.find_incident is incidents._find_incident
    assert policy.create_incident is incidents._create_incident
    assert policy.attach_evidence is incidents._attach_evidence
    assert policy.reopen_incident is incidents._reopen_incident
