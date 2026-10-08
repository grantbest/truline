"""F-DCE-5: doctrine rot becomes a dated verdict, not a suspicion.

docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md, sprint 26. An
`adopted`/`enforced` `arch.principle` with no incoming `applies` link and no incoming
`enforced_by` link, past a configurable window since its last dated status transition,
produces one standing `arch.observation` — updated in place on every run the condition
persists, resolved the run it clears. No database, no network: FakePrincipleObservationStore
below accepts exactly the link vocabulary and observation shape the live substrate does
(apps/substrate/src/schemas.py's BEAD_LINK_TYPES / ArchObservationContent /
ArchPrincipleContent), same discipline as test_doctrine_injection.py's
LinkValidatingFakeSubstrate and test_verdict_staleness_report.py's FakeObservationStore.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner  # noqa: E402
from activities import doctrine_staleness  # noqa: E402


def fixed_now() -> datetime:
    return datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def principle(
    ref: str,
    *,
    status: str = "adopted",
    transitioned_on: str = "2026-08-01",
    bead_id: str | None = None,
) -> dict:
    return {
        "id": bead_id or f"bead-{ref}",
        "content": {
            "ref": ref,
            "statement": "a statement",
            "rationale": "a rationale",
            "source": "incident-1",
            "status": status,
            "status_history": [
                {"date": transitioned_on, "status": status, "reason": "seeded"}
            ],
        },
    }


class FakePrincipleObservationStore:
    """Mirrors what the live substrate would accept — see module docstring."""

    def __init__(self, principles, links=None):
        self._principles = list(principles)
        self._links = {k: set(v) for k, v in (links or {}).items()}
        self.observations: dict[str, dict] = {}
        self._next_id = 1

    def list_principles(self):
        return list(self._principles)

    def has_governing_link(self, principle_bead_id):
        return bool(self._links.get(principle_bead_id))

    def find_observation(self, ref):
        return self.observations.get(ref)

    def create_observation(self, payload):
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["created_by"] == doctrine_staleness.CREATED_BY
        ref = payload["content"]["ref"]
        assert ref not in self.observations, "create called for an existing ref"
        bead = {
            "id": f"obs-{self._next_id}",
            "created_by": payload["created_by"],
            "state": payload["state"],
            "content": payload["content"],
            "context": payload["context"],
        }
        self._next_id += 1
        self.observations[ref] = bead
        return dict(bead)

    def update_observation(self, bead_id, content, context, created_by):
        for bead in self.observations.values():
            if bead["id"] == bead_id:
                bead["created_by"] = created_by
                bead["content"] = content
                bead["context"] = context
                bead["state"] = "active"
                return dict(bead)
        raise AssertionError(f"update_observation called for unknown bead_id {bead_id!r}")

    def resolve_observation(self, bead_id, created_by):
        for bead in self.observations.values():
            if bead["id"] == bead_id:
                bead["state"] = "resolved"
                return dict(bead)
        raise AssertionError(f"resolve_observation called for unknown bead_id {bead_id!r}")


# ---------------------------------------------------------------------------
# scanner.py — pure decision logic
# ---------------------------------------------------------------------------


def test_principle_last_transition_date_is_the_max_dated_entry():
    content = {
        "status_history": [
            {"date": "2026-08-01", "status": "proposed", "reason": "seeded"},
            {"date": "2026-08-10", "status": "adopted", "reason": "promoted"},
        ]
    }
    assert scanner.principle_last_transition_date(content) == "2026-08-10"


def test_principle_last_transition_date_is_none_without_history():
    assert scanner.principle_last_transition_date({"status_history": []}) is None
    assert scanner.principle_last_transition_date({}) is None


def test_principle_staleness_reason_flags_adopted_past_window_with_no_link():
    content = principle("PRIN-011")["content"]
    reason = scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=False, now=fixed_now()
    )
    assert "PRIN-011" in reason
    assert "no applies citation and no enforced_by edge" in reason


def test_principle_staleness_reason_empty_within_window():
    content = principle("PRIN-011", transitioned_on="2026-08-15")["content"]
    reason = scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=False, now=fixed_now()
    )
    assert reason == ""


def test_principle_staleness_reason_empty_when_governed():
    content = principle("PRIN-011")["content"]
    reason = scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=True, now=fixed_now()
    )
    assert reason == ""


@pytest.mark.parametrize("status", ["proposed", "retired"])
def test_principle_staleness_reason_empty_for_non_binding_status(status):
    content = principle("PRIN-011", status=status)["content"]
    reason = scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=False, now=fixed_now()
    )
    assert reason == ""


def test_principle_staleness_reason_empty_without_a_dated_transition():
    content = principle("PRIN-011")["content"]
    content["status_history"] = []
    reason = scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=False, now=fixed_now()
    )
    assert reason == ""


def test_principle_staleness_reason_respects_a_configurable_window():
    content = principle("PRIN-011", transitioned_on="2026-08-15")["content"]
    # 6 days elapsed by fixed_now(): not stale at the 14-day default...
    assert scanner.principle_staleness_reason(
        "PRIN-011", content, has_governing_link=False, now=fixed_now()
    ) == ""
    # ...but stale at a 5-day window.
    reason = scanner.principle_staleness_reason(
        "PRIN-011",
        content,
        has_governing_link=False,
        now=fixed_now(),
        window=timedelta(days=5),
    )
    assert reason != ""


def test_doctrine_staleness_dedupe_key_names_the_principle():
    assert scanner.doctrine_staleness_dedupe_key("PRIN-011") == "doctrine-staleness:PRIN-011"


# ---------------------------------------------------------------------------
# activities.doctrine_staleness.run_doctrine_staleness_report
# ---------------------------------------------------------------------------


def test_stale_principle_produces_one_dated_observation_naming_id_window_and_measured_at():
    store = FakePrincipleObservationStore([principle("PRIN-011")])

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["checked"] == 1
    assert result["flagged"] == 1
    assert result["observations_created"] == 1
    [obs] = store.observations.values()
    assert obs["content"]["workload"]["name"] == "PRIN-011"
    assert obs["context"]["principle_id"] == "PRIN-011"
    assert obs["context"]["window_days"] == scanner.DOCTRINE_STALENESS_WINDOW.days
    assert obs["content"]["observed_at"] == "2026-08-21T12:00:00Z"
    assert obs["context"]["measured_at"] == "2026-08-21T12:00:00Z"


def test_observation_declares_source_class_derived_and_created_by_on_create():
    """"factory-dispatcher/doctrine-staleness" is enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822) --
    the fresh-ref (create) path must declare it."""
    store = FakePrincipleObservationStore([principle("PRIN-011")])

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["observations_created"] == 1
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "derived"
    assert obs["created_by"] == doctrine_staleness.CREATED_BY


def test_observation_declares_source_class_derived_and_created_by_on_update():
    """Same identity, existing-ref (update) path."""
    store = FakePrincipleObservationStore([principle("PRIN-011")])
    doctrine_staleness.run_doctrine_staleness_report(store, now_fn=fixed_now, suppressions={})

    later = lambda: fixed_now() + timedelta(days=1)  # noqa: E731
    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=later, suppressions={}
    )

    assert result["observations_updated"] == 1
    [obs] = store.observations.values()
    assert obs["content"]["source_class"] == "derived"
    assert obs["created_by"] == doctrine_staleness.CREATED_BY


def test_rerun_while_condition_persists_updates_the_same_observation_not_a_duplicate():
    store = FakePrincipleObservationStore([principle("PRIN-011")])

    first = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )
    later = lambda: fixed_now() + timedelta(days=1)  # noqa: E731
    second = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=later, suppressions={}
    )

    assert first["observations_created"] == 1
    assert second["observations_created"] == 0
    assert second["observations_updated"] == 1
    assert len(store.observations) == 1
    [obs] = store.observations.values()
    assert obs["content"]["observed_at"] == doctrine_staleness._iso(later())


def test_governing_applies_link_emits_no_verdict():
    prin = principle("PRIN-011")
    store = FakePrincipleObservationStore(
        [prin], links={prin["id"]: {"applies"}}
    )

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["flagged"] == 0
    assert store.observations == {}


def test_governing_enforced_by_link_emits_no_verdict():
    prin = principle("PRIN-011")
    store = FakePrincipleObservationStore(
        [prin], links={prin["id"]: {"enforced_by"}}
    )

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["flagged"] == 0
    assert store.observations == {}


def test_standing_observation_resolves_the_run_a_link_appears():
    prin = principle("PRIN-011")
    store = FakePrincipleObservationStore([prin])
    doctrine_staleness.run_doctrine_staleness_report(store, now_fn=fixed_now, suppressions={})
    [obs_before] = store.observations.values()
    assert obs_before["state"] == "active"

    store._links[prin["id"]] = {"applies"}
    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["observations_resolved"] == 1
    [obs_after] = store.observations.values()
    assert obs_after["state"] == "resolved"


def test_suppressed_principle_is_skipped_and_counted():
    store = FakePrincipleObservationStore([principle("PRIN-011")])

    result = doctrine_staleness.run_doctrine_staleness_report(
        store,
        now_fn=fixed_now,
        suppressions={"doctrine-staleness:PRIN-011": "deliberately file-only, see incident-9"},
    )

    assert result["flagged"] == 0
    assert result["suppressed"] == 1
    assert store.observations == {}


@pytest.mark.parametrize("status", ["proposed", "retired"])
def test_proposed_and_retired_principles_are_ignored_entirely(status):
    store = FakePrincipleObservationStore([principle("PRIN-999", status=status)])

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["checked"] == 0
    assert result["flagged"] == 0
    assert store.observations == {}


def test_principle_within_window_produces_no_observation():
    store = FakePrincipleObservationStore(
        [principle("PRIN-011", transitioned_on="2026-08-15")]
    )

    result = doctrine_staleness.run_doctrine_staleness_report(
        store, now_fn=fixed_now, suppressions={}
    )

    assert result["checked"] == 1
    assert result["flagged"] == 0
    assert store.observations == {}


def test_run_reads_scanner_suppressions_by_default(monkeypatch):
    store = FakePrincipleObservationStore([principle("PRIN-011")])
    monkeypatch.setattr(
        scanner, "load_suppressions", lambda: {"doctrine-staleness:PRIN-011": "reason"}
    )

    result = doctrine_staleness.run_doctrine_staleness_report(store, now_fn=fixed_now)

    assert result["suppressed"] == 1
    assert store.observations == {}


# ---------------------------------------------------------------------------
# activities.__init__ — the activity is registered for the live worker
# ---------------------------------------------------------------------------


def test_report_doctrine_staleness_activity_is_registered():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "report_doctrine_staleness_activity" in names


def test_doctrine_staleness_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
    """#727: the change reconciler's live `_request` passed `headers=self._headers`
    unconditionally, so a caller supplying its own `headers=` kwarg collided with it
    (`httpx.request() got multiple values for keyword argument 'headers'`) -- a
    live-only shape the FakeStore, which replaces the whole store, never walks. This
    store shares the same `_request` shape. Pins: one merged headers dict carrying
    BOTH the standing auth header and a per-call header, per-call winning on
    collision.
    """
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(doctrine_staleness.httpx, "request", fake_request)

    store = doctrine_staleness.SubstratePrincipleObservationStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={
            "X-Created-By": doctrine_staleness.CREATED_BY,
            "Content-Type": "text/plain",
        },
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == doctrine_staleness.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"
