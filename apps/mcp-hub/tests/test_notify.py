"""Shared Discord notifier (BG-S1)."""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from tools import notify


class _StubResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class _StubClient:
    def __init__(self, status_code: int, calls: list):
        self._status = status_code
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self._calls.append({"url": url, "json": json})
        return _StubResponse(self._status)


def _stub_httpx(monkeypatch, status_code: int) -> list:
    calls: list = []
    monkeypatch.setattr(
        notify.httpx, "AsyncClient",
        lambda **kw: _StubClient(status_code, calls),
    )
    return calls


class _PolicyState:
    def __init__(
        self,
        initial=None,
        *,
        load_raises=False,
        deferred_load_raises=False,
        deferred_record_raises=False,
        now=None,
    ):
        self.state = initial
        self.load_raises = load_raises
        self.deferred_load_raises = deferred_load_raises
        self.deferred_record_raises = deferred_record_raises
        self.now = now or (lambda: datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("America/Chicago")))
        self.posts = []
        self.records = []
        self.deferred = []
        self.cleared = []

    async def load(self, kind):
        if self.load_raises:
            raise RuntimeError("substrate down")
        return self.state

    async def record(self, kind, fingerprint, existing, members):
        self.records.append((kind, fingerprint, existing, members))
        self.state = {
            "id": (existing or {}).get("id", "state-1"),
            "content": {
                "kind": kind,
                "fingerprint": fingerprint,
                "members": sorted(members) if members is not None else None,
            },
        }

    async def post(self, content):
        self.posts.append(content)
        return True

    async def load_deferred(self):
        if self.deferred_load_raises:
            raise RuntimeError("deferral store down")
        return list(self.deferred)

    async def record_deferred(self, deferred):
        if self.deferred_record_raises:
            raise RuntimeError("deferral store down")
        stored = dict(deferred)
        stored["id"] = f"deferred-{len(self.deferred) + 1}"
        self.deferred.append(stored)

    async def clear_deferred(self, deferred):
        self.cleared.extend(deferred)
        cleared_ids = {alert.get("id") for alert in deferred}
        self.deferred = [alert for alert in self.deferred if alert.get("id") not in cleared_ids]

    def policy(self):
        return notify.AlertPolicy(
            load_alert_state=self.load,
            record_alert_posted=self.record,
            post=self.post,
            load_deferred_alerts=self.load_deferred,
            record_alert_deferred=self.record_deferred,
            clear_deferred_alerts=self.clear_deferred,
            clock=self.now,
            household_timezone="America/Chicago",
        )


async def test_missing_webhook_returns_false_never_raises(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert await notify.post_discord("hello") is False
    assert await notify.post_discord("hello", raise_on_error=True) is False


async def test_success_returns_true_and_truncates(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    calls = _stub_httpx(monkeypatch, 204)
    assert await notify.post_discord("x" * 5000) is True
    assert len(calls) == 1
    assert len(calls[0]["json"]["content"]) == notify._MAX_CONTENT


async def test_message_carries_environment_tag(monkeypatch):
    """A dev alert must never read as a prod one.

    Dev's sandbox Plaid Items go stale and post "Plaid re-auth required for
    `chase`" — the same institution slug prod uses — so without a tag the
    message claims a real account is broken while the Console shows it
    healthy (2026-08-01).
    """
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.setenv("DEPLOY_ENV", "prod")
    calls = _stub_httpx(monkeypatch, 204)

    assert await notify.post_discord("Plaid re-auth required for `chase`") is True
    assert calls[0]["json"]["content"] == "[prod] Plaid re-auth required for `chase`"


async def test_untagged_environment_defaults_to_dev(monkeypatch):
    """Only prod sets DEPLOY_ENV, so an unset value must not read as prod."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    calls = _stub_httpx(monkeypatch, 204)

    await notify.post_discord("hello")
    assert calls[0]["json"]["content"] == "[dev] hello"


async def test_env_tag_survives_truncation(monkeypatch):
    """Tag first, then truncate — an oversized body must not shed the tag."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.setenv("DEPLOY_ENV", "dev")
    calls = _stub_httpx(monkeypatch, 204)

    await notify.post_discord("x" * 5000)
    content = calls[0]["json"]["content"]
    assert content.startswith("[dev] ")
    assert len(content) == notify._MAX_CONTENT


async def test_http_error_best_effort_swallows(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    _stub_httpx(monkeypatch, 500)
    assert await notify.post_discord("hello") is False


async def test_http_error_raises_when_asked(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    _stub_httpx(monkeypatch, 500)
    with pytest.raises(httpx.HTTPStatusError):
        await notify.post_discord("hello", raise_on_error=True)


async def test_alert_policy_suppresses_repeat_fingerprint():
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "bank_sync.anomalies",
            "fingerprint": "tx-1",
            "last_posted_at": "2026-08-01T03:00:00",
        },
    })

    posted = await state.policy().send(
        "bank_sync.anomalies",
        "tx-1",
        ":rotating_light: one anomaly",
    )

    assert posted is False
    assert state.posts == []
    assert state.records == []


async def test_alert_policy_re_alerts_after_declared_interval_elapses():
    """#593's delivered-vs-seen shape: an unchanged fingerprint must not
    suppress forever once a re-alert interval is declared — a condition
    quiet longer than the interval re-announces even with identical content."""
    stale = datetime.now() - timedelta(hours=25)
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "bank_sync.anomalies",
            "fingerprint": "tx-1",
            "last_posted_at": stale.isoformat(),
        },
    })

    posted = await state.policy().send(
        "bank_sync.anomalies",
        "tx-1",
        ":rotating_light: one anomaly",
        re_alert_interval_hours=24.0,
    )

    assert posted is True
    assert state.posts == [":rotating_light: one anomaly"]


async def test_alert_policy_still_suppresses_within_the_declared_interval():
    recent = datetime.now() - timedelta(hours=1)
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "bank_sync.anomalies",
            "fingerprint": "tx-1",
            "last_posted_at": recent.isoformat(),
        },
    })

    posted = await state.policy().send(
        "bank_sync.anomalies",
        "tx-1",
        ":rotating_light: one anomaly",
        re_alert_interval_hours=24.0,
    )

    assert posted is False
    assert state.posts == []


async def test_alert_policy_re_alert_interval_is_opt_in():
    """No declared interval (the default) must keep the historical
    unchanged-forever suppression exactly as every existing caller relies on."""
    ancient = datetime.now() - timedelta(days=365)
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "bank_sync.anomalies",
            "fingerprint": "tx-1",
            "last_posted_at": ancient.isoformat(),
        },
    })

    posted = await state.policy().send("bank_sync.anomalies", "tx-1", "still the same")

    assert posted is False
    assert state.posts == []


async def test_re_alert_window_is_timezone_correct_not_local_offset_skewed(monkeypatch):
    """PR #674: `_re_alert_due` stored a UTC timestamp but compared it against
    naive LOCAL `datetime.now()` — on an America/Chicago host a declared 4h
    window behaved as ~9-10h. Pinning the process to the most extreme
    non-UTC zone that exists (UTC+14) and proving the window still fires at
    exactly 4h — not late, and not early either — is the regression test for
    that skew in both directions.
    """
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14
    time.tzset()
    try:
        now = datetime.now(timezone.utc)

        just_over = _PolicyState({
            "id": "state-1",
            "content": {
                "kind": "cluster_health.finding:x",
                "fingerprint": "fp",
                "last_posted_at": (now - timedelta(hours=4, minutes=1)).isoformat(),
            },
        })
        posted_over = await just_over.policy().send(
            "cluster_health.finding:x",
            "fp",
            "still happening",
            re_alert_interval_hours=4.0,
        )

        just_under = _PolicyState({
            "id": "state-1",
            "content": {
                "kind": "cluster_health.finding:x",
                "fingerprint": "fp",
                "last_posted_at": (now - timedelta(hours=3, minutes=59)).isoformat(),
            },
        })
        posted_under = await just_under.policy().send(
            "cluster_health.finding:x",
            "fp",
            "still happening",
            re_alert_interval_hours=4.0,
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()

    # Window elapsed: must re-alert at 4h, not 9-10h later (never quieter than declared).
    assert posted_over is True
    # Window not yet elapsed: must not fire early (never spammier than declared).
    assert posted_under is False


def test_parse_stored_timestamp_accepts_naive_and_utc_aware():
    """Migration safety: a state file written before this fix (naive, the
    historical `datetime.now().isoformat()` shape) and one written after
    (aware UTC, `Z` or an explicit offset) must both parse without error and
    resolve to the same instant."""
    naive = notify._parse_stored_timestamp("2026-08-01T12:00:00")
    aware_offset = notify._parse_stored_timestamp("2026-08-01T12:00:00+00:00")
    aware_z = notify._parse_stored_timestamp("2026-08-01T12:00:00Z")

    assert naive == aware_offset == aware_z
    assert naive.tzinfo is not None

    assert notify._parse_stored_timestamp("not-a-timestamp") is None


async def test_alert_policy_unrelated_subject_never_suppresses_another():
    """Per-subject dedup: two different kinds must never share suppression
    state, even when one has already posted and the other has not."""
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "environmental_fault_breaker_latched:bead-a",
            "fingerprint": "bead-a:sig-1",
            "last_posted_at": datetime.now().isoformat(),
        },
    })

    posted = await state.policy().send(
        "environmental_fault_breaker_latched:bead-b",
        "bead-b:sig-1",
        "a different bead latched",
        re_alert_interval_hours=24.0,
    )

    assert posted is True
    assert state.posts == ["a different bead latched"]


async def test_alert_policy_state_read_failure_fails_open():
    state = _PolicyState(load_raises=True)

    posted = await state.policy().send("bank_sync.quality", "312:0", "312 unreconciled")

    assert posted is True
    assert state.posts == ["312 unreconciled"]
    assert state.records == [("bank_sync.quality", "312:0", None, None)]


async def test_informational_alert_at_0300_local_is_deferred():
    state = _PolicyState(
        now=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
    )

    posted = await state.policy().send(
        "morning_brief.daily",
        "brief-1",
        "Daily brief",
        severity=notify.AlertSeverity.INFORMATIONAL,
    )

    assert posted is False
    assert state.posts == []
    assert len(state.deferred) == 1
    assert state.deferred[0]["content"] == "Daily brief"


async def test_informational_alert_at_1200_local_is_delivered():
    state = _PolicyState(
        now=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    )

    posted = await state.policy().send(
        "morning_brief.daily",
        "brief-1",
        "Daily brief",
        severity=notify.AlertSeverity.INFORMATIONAL,
    )

    assert posted is True
    assert state.posts == ["Daily brief"]
    assert state.deferred == []


async def test_urgent_alert_at_0300_local_is_delivered_immediately():
    state = _PolicyState(
        now=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
    )

    posted = await state.policy().send(
        "sync_failure:chase",
        "boom",
        "Bank sync failed",
        severity=notify.AlertSeverity.URGENT,
    )

    assert posted is True
    assert state.posts == ["Bank sync failed"]
    assert state.deferred == []


async def test_deferred_alerts_deliver_as_one_digest_with_every_alert():
    now = {"value": datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))}
    state = _PolicyState(now=lambda: now["value"])
    policy = state.policy()

    assert await policy.send(
        "morning_brief.daily",
        "brief-1",
        "Daily brief one",
        severity=notify.AlertSeverity.INFORMATIONAL,
    ) is False
    assert await policy.send(
        "bank_sync.data_quality_queue",
        "quality-1",
        "Three transactions need category review",
        severity=notify.AlertSeverity.INFORMATIONAL,
    ) is False

    now["value"] = datetime(2026, 1, 1, 7, 0, tzinfo=ZoneInfo("America/Chicago"))
    posted = await policy.deliver_deferred_digest()

    assert posted is True
    assert len(state.posts) == 1
    assert state.posts[0].startswith("Deferred informational alerts (2):")
    assert "Daily brief one" in state.posts[0]
    assert "Three transactions need category review" in state.posts[0]
    assert state.deferred == []
    assert len(state.records) == 2


async def test_deferred_digest_does_not_flush_during_quiet_hours():
    state = _PolicyState(
        now=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
    )
    state.deferred.append({
        "id": "deferred-1",
        "kind": "morning_brief.daily",
        "fingerprint": "brief-1",
        "content": "Daily brief",
        "severity": notify.AlertSeverity.INFORMATIONAL.value,
        "raised_at": "2026-01-01T03:00:00-06:00",
    })

    assert await state.policy().deliver_deferred_digest() is False
    assert state.posts == []
    assert len(state.deferred) == 1


async def test_deferral_store_failure_posts_informational_alert():
    state = _PolicyState(
        deferred_record_raises=True,
        now=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago")),
    )

    posted = await state.policy().send(
        "morning_brief.daily",
        "brief-1",
        "Daily brief",
        severity=notify.AlertSeverity.INFORMATIONAL,
    )

    assert posted is True
    assert state.posts == ["Daily brief"]
    assert state.deferred == []


async def test_deferral_store_read_failure_does_not_block_current_delivery():
    state = _PolicyState(
        deferred_load_raises=True,
        now=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("America/Chicago")),
    )

    posted = await state.policy().send(
        "morning_brief.daily",
        "brief-1",
        "Daily brief",
        severity=notify.AlertSeverity.INFORMATIONAL,
    )

    assert posted is True
    assert state.posts == ["Daily brief"]


def test_quiet_hours_use_household_timezone_not_utc_boundary():
    local = datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
    same_instant_utc = local.astimezone(ZoneInfo("UTC"))

    assert same_instant_utc.hour == 9
    assert notify.is_quiet_hours(local) is True
    assert notify.is_quiet_hours(same_instant_utc) is False


def test_missing_alert_severity_defaults_to_urgent_not_informational():
    assert notify.normalize_alert_severity(None) == notify.AlertSeverity.URGENT


async def test_missing_alert_severity_at_0300_defaults_to_delivery():
    state = _PolicyState(
        now=lambda: datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("America/Chicago"))
    )

    posted = await state.policy().send(
        "unknown",
        "fingerprint",
        "No severity declaration",
    )

    assert posted is True
    assert state.posts == ["No severity declaration"]
    assert state.deferred == []


async def test_same_condition_through_multiple_callers_posts_once(monkeypatch):
    from workflows import budget_pulse, morning_brief

    state = _PolicyState()
    monkeypatch.setattr(morning_brief, "_alert_policy", state.policy)
    monkeypatch.setattr(budget_pulse, "_alert_policy", state.policy)

    assert await morning_brief._send_discord_notification("same condition") is True
    assert await budget_pulse._send_discord_notification("same condition") is False
    assert state.posts == [
        "same condition\n"
        "Decision: pick today's schedule, finance, and platform follow-ups from the brief."
    ]


def test_alert_policy_bypass_count_is_computed_from_source():
    bypasses = notify.find_alert_policy_bypasses()

    assert notify.count_alert_policy_bypasses() == len(bypasses)
    assert notify.count_alert_policy_bypasses() == 0
    assert bypasses == []


def test_worker_revision_drift_escalated_definition_is_true_whichever_way_it_resolved():
    """dev.finding 639a20c5 AC-4b: factory-dispatcher's escalation no longer always pauses
    dispatch -- `failure_diagnosis.announce_worker_revision_drift_escalated` now passes a
    `dispatch_paused` flag that can be False. This definition's static text (summary, decision
    prompt) must stay true regardless of which outcome actually fired, since the two sides
    (factory-dispatcher's content, this definition's summary/prompt) can ship in either order."""
    definition = notify.get_alert_definition("factory_dispatcher.worker_revision_drift_escalated")

    assert not definition.is_removed
    # Renders cleanly with no missing-template surprises.
    rendered_next_step = definition.next_step({"note": "deferred 4 times"})
    assert "{note}" not in rendered_next_step

    assert "consecutive-deferral bound and dispatch was paused" not in definition.summary
    assert "only if" in definition.decision_prompt_template


def test_worker_revision_drift_escalated_resolved_is_a_registered_informational_alert():
    """factory-dispatcher's `failure_diagnosis.announce_worker_revision_drift_escalated_resolved`
    (f51e057c F-E) needs a real `ALERT_INVENTORY` entry to actually deliver -- `get_alert_definition`
    raises `KeyError` for anything unregistered, and that KeyError is swallowed by that function's
    best-effort `except Exception`, so an unregistered id looks like a successful, silent no-op
    exactly like the sibling `worker_revision_drift_escalated` id did before it was registered."""
    definition = notify.get_alert_definition(
        "factory_dispatcher.worker_revision_drift_escalated_resolved"
    )
    assert not definition.is_removed
    assert definition.severity is notify.AlertSeverity.INFORMATIONAL
    assert definition.has_next_step


def test_worker_revision_drift_unknown_while_paused_is_a_registered_actionable_alert():
    """factory-dispatcher's `failure_diagnosis.announce_worker_revision_drift_unknown_while_paused`
    (dev.finding 9a10f2aa) needs a real `ALERT_INVENTORY` entry to actually deliver -- same
    KeyError-swallowed-as-silent-no-op failure mode `worker_revision_drift_escalated` was
    registered to fix, and the exact mistake its predecessor bead shipped once already."""
    definition = notify.get_alert_definition(
        "factory_dispatcher.worker_revision_drift_unknown_while_paused"
    )
    assert not definition.is_removed
    assert definition.severity is notify.AlertSeverity.ACTIONABLE
    assert definition.has_next_step


def test_alert_inventory_is_enumerable_and_actionable():
    active = list(notify.iter_alert_inventory())
    all_defs = list(notify.iter_alert_inventory(include_removed=True))

    assert active
    assert len(all_defs) > len(active)
    assert {a.alert_id for a in all_defs} == set(notify.ALERT_INVENTORY)
    assert all(a.has_next_step for a in active)
    assert all(isinstance(a.severity, notify.AlertSeverity) for a in active)
    assert all(a.removed_reason for a in all_defs if a.is_removed)
    assert notify.urgent_alert_ids() == sorted(notify.URGENT_ALERT_IDS)


def test_r2612_b5_release_health_and_queue_alerts_are_registered_structurally():
    """R26.12 B5 is structural only: seven AlertDefinitions enter the inventory with no
    emitter yet (B6, B13 and B18 add those). This pins the shape the design and the
    2026-10-02/2026-10-04 amendments require: none opens an incident, each carries a
    decision prompt so an operator always has a next step, and
    release_health_unmeasured_persistent gets its OWN kind_template -- a shared
    release_health:{ref} with release_health_unmeasured would make the dispatcher's
    kind-keyed dedup state (failure_diagnosis._load_dev_task_alert_state) overwrite the
    two alerts' fingerprints and flip-flop every 48 hours."""
    seven_ids = [
        "factory_dispatcher.queue_nothing_selectable",
        "factory_dispatcher.expedite_provenance_rejected",
        "factory_dispatcher.release_health_drifting",
        "factory_dispatcher.release_health_breached",
        "factory_dispatcher.release_health_recovered",
        "factory_dispatcher.release_health_unmeasured",
        "factory_dispatcher.release_health_unmeasured_persistent",
    ]
    # the Operator's notification budget (decision record 2026-09-25 item 11) is "never
    # page": these four are allowed to be actionable, the other three merely
    # informational, and none of the seven -- actionable or not -- may be URGENT.
    expected_severity_by_id = {
        "factory_dispatcher.queue_nothing_selectable": notify.AlertSeverity.ACTIONABLE,
        "factory_dispatcher.expedite_provenance_rejected": notify.AlertSeverity.ACTIONABLE,
        "factory_dispatcher.release_health_drifting": notify.AlertSeverity.INFORMATIONAL,
        "factory_dispatcher.release_health_breached": notify.AlertSeverity.ACTIONABLE,
        "factory_dispatcher.release_health_recovered": notify.AlertSeverity.INFORMATIONAL,
        "factory_dispatcher.release_health_unmeasured": notify.AlertSeverity.INFORMATIONAL,
        "factory_dispatcher.release_health_unmeasured_persistent": notify.AlertSeverity.ACTIONABLE,
    }
    # The four release_health_* state alerts share one dedup slot
    # (release_health:{ref}); the persistent escalation gets its own
    # (release_health_persistent:{ref}) so it never overwrites the base
    # unmeasured fingerprint (amended 2026-10-04).
    expected_kind_template_by_id = {
        "factory_dispatcher.queue_nothing_selectable": "queue_nothing_selectable",
        "factory_dispatcher.expedite_provenance_rejected": "expedite_provenance_rejected:{bead_id}",
        "factory_dispatcher.release_health_drifting": "release_health:{ref}",
        "factory_dispatcher.release_health_breached": "release_health:{ref}",
        "factory_dispatcher.release_health_recovered": "release_health:{ref}",
        "factory_dispatcher.release_health_unmeasured": "release_health:{ref}",
        "factory_dispatcher.release_health_unmeasured_persistent": "release_health_persistent:{ref}",
    }
    for alert_id in seven_ids:
        definition = notify.get_alert_definition(alert_id)
        assert not definition.is_removed
        assert definition.opens_incident is False
        assert definition.decision_prompt_template
        assert definition.severity is expected_severity_by_id[alert_id]
        assert definition.kind_template == expected_kind_template_by_id[alert_id]

    assert not (set(seven_ids) & notify.URGENT_ALERT_IDS)
    assert not (set(seven_ids) & set(notify.urgent_alert_ids()))

    unmeasured = notify.get_alert_definition("factory_dispatcher.release_health_unmeasured")
    persistent = notify.get_alert_definition(
        "factory_dispatcher.release_health_unmeasured_persistent"
    )
    assert persistent.kind_template == "release_health_persistent:{ref}"
    assert persistent.kind_template != unmeasured.kind_template

    assert len(notify.ALERT_INVENTORY) == 37


def test_data_quality_alert_links_to_review_queue():
    message = notify.format_data_quality_queue_alert(
        3, base_url="https://console.truline.test"
    )

    assert "Data-quality review queue has 3 transaction(s)" in message
    assert "Next step: Review categorization queue:" in message
    assert "https://console.truline.test/rules" in message


def test_workflow_alert_sends_are_inventory_registered():
    assert notify.find_unregistered_alert_sends() == []


def test_workflow_alert_sends_declare_severity_at_raise_point():
    assert notify.find_alert_sends_without_declared_severity() == []


def test_outstanding_alert_is_reported():
    """Delivered, never acknowledged: must read as outstanding."""
    delivered = {
        "id": "state-1",
        "kind": "bank_sync.sync_failure",
        "fingerprint": "chase-1",
        "last_posted_at": "2026-08-30T03:00:00",
        "acknowledged_at": None,
        "acknowledged_by": None,
    }

    assert notify.is_alert_outstanding(delivered) is True
    assert notify.outstanding_alerts([delivered]) == [delivered]


def test_acknowledged_alert_is_not_outstanding():
    """Delivered and explicitly acknowledged: must not read as outstanding."""
    acknowledged = {
        "id": "state-1",
        "kind": "bank_sync.sync_failure",
        "fingerprint": "chase-1",
        "last_posted_at": "2026-08-30T03:00:00",
        "acknowledged_at": "2026-08-30T07:15:00",
        "acknowledged_by": "grant",
    }

    assert notify.is_alert_outstanding(acknowledged) is False
    assert notify.outstanding_alerts([acknowledged]) == []


async def test_delivery_success_does_not_acknowledge():
    """Posting an alert must never itself mark it seen.

    Elapsed time and a later alert on the same kind are equally forbidden
    signals for "seen" (per acceptance criteria); this test exercises the
    one that is reachable purely through the delivery path — a second,
    changed-fingerprint delivery of the same kind must not resurrect or
    fabricate an acknowledgement on the first record.
    """
    state = _PolicyState()

    posted = await state.policy().send(
        "bank_sync.sync_failure",
        "chase-1",
        "Bank sync failed",
        severity=notify.AlertSeverity.URGENT,
    )

    assert posted is True
    delivered_state = state.state["content"]
    assert "acknowledged_at" not in delivered_state
    assert notify.is_alert_outstanding(delivered_state) is True

    posted_again = await state.policy().send(
        "bank_sync.sync_failure",
        "chase-2",
        "Bank sync failed again",
        severity=notify.AlertSeverity.URGENT,
    )

    assert posted_again is True
    assert "acknowledged_at" not in state.state["content"]
    assert notify.is_alert_outstanding(state.state["content"]) is True


async def test_acknowledge_finance_alert_requires_prior_delivery(monkeypatch):
    """Acknowledgement is explicit and cannot fabricate a delivery record."""

    async def _no_state(kind):
        return None

    monkeypatch.setattr(notify, "load_finance_alert_state", _no_state)

    assert await notify.acknowledge_finance_alert("never.delivered", "grant") is False


async def test_acknowledge_finance_alert_patches_existing_record(monkeypatch):
    """Acknowledging a delivered alert stamps who/when without touching delivery fields."""
    from tools import finance

    existing = {
        "id": "state-1",
        "content": {
            "kind": "bank_sync.sync_failure",
            "fingerprint": "chase-1",
            "last_posted_at": "2026-08-30T03:00:00",
        },
    }
    patched = {}

    async def _load(kind):
        return existing

    async def _patch_bead(bead_id, *, content=None, created_by=None):
        patched["bead_id"] = bead_id
        patched["content"] = content
        patched["created_by"] = created_by
        return {}

    monkeypatch.setattr(notify, "load_finance_alert_state", _load)
    monkeypatch.setattr(finance, "patch_bead", _patch_bead)

    result = await notify.acknowledge_finance_alert("bank_sync.sync_failure", "grant")

    assert result is True
    assert patched["bead_id"] == "state-1"
    assert patched["content"]["kind"] == "bank_sync.sync_failure"
    assert patched["content"]["fingerprint"] == "chase-1"
    assert patched["content"]["acknowledged_by"] == "grant"
    assert patched["content"]["acknowledged_at"]


# ---------------------------------------------------------------------------
# opens_incident: only a declared, reviewed opt-in may open an arch.incident
# (docs/architecture/itsm-target-state.md §3)
# ---------------------------------------------------------------------------


class _IncidentPolicySpy:
    def __init__(self):
        self.fired: list[dict] = []

    async def fire(self, alert_id, subject, **kwargs):
        self.fired.append({"alert_id": alert_id, "subject": subject, **kwargs})
        return {"action": "opened", "id": "incident-1"}


def _opt_in_definition(**overrides):
    fields = dict(
        alert_id="test.opens_incident",
        kind_template="test_opens_incident:{subject_key}",
        summary="Synthetic alert for opens_incident tests.",
        severity=notify.AlertSeverity.URGENT,
        decision_prompt_template="do the thing for {subject_key}",
    )
    fields.update(overrides)
    return notify.AlertDefinition(**fields)


def test_opens_incident_defaults_to_false():
    definition = notify.AlertDefinition(
        alert_id="test.default", kind_template="k", summary="s",
        decision_prompt_template="d",
    )
    assert definition.opens_incident is False


def test_opens_incident_rejects_informational_severity():
    with pytest.raises(ValueError, match="informational"):
        notify.AlertDefinition(
            alert_id="test.bad",
            kind_template="k",
            summary="s",
            severity=notify.AlertSeverity.INFORMATIONAL,
            decision_prompt_template="d",
            opens_incident=True,
        )


def test_opens_incident_allows_actionable_and_urgent_severity():
    for severity in (notify.AlertSeverity.ACTIONABLE, notify.AlertSeverity.URGENT):
        definition = _opt_in_definition(severity=severity, opens_incident=True)
        assert definition.opens_incident is True


def test_alert_definition_docstring_excludes_alertmanager_and_prometheus():
    doc = notify.AlertDefinition.__doc__ or ""
    assert "Alertmanager" in doc
    assert "Prometheus" in doc
    assert "opens_incident" in doc


async def test_send_alert_without_opt_in_never_touches_incident_policy(monkeypatch):
    """A firing that announces (posts) through a definition that has not
    opted in must write no incident record — the opt-out demonstration."""
    monkeypatch.setitem(notify.ALERT_INVENTORY, "test.no_opt_in", _opt_in_definition())
    spy = _IncidentPolicySpy()
    policy = _PolicyState().policy()

    posted = await notify.send_alert(
        policy,
        "test.no_opt_in",
        "fp-1",
        "condition observed",
        template_values={"subject_key": "chase"},
        incident_policy=spy,
    )

    assert posted is True
    assert spy.fired == []


async def test_send_alert_with_opt_in_fires_the_incident_policy(monkeypatch):
    monkeypatch.setitem(
        notify.ALERT_INVENTORY,
        "test.opt_in",
        _opt_in_definition(alert_id="test.opt_in", opens_incident=True),
    )
    spy = _IncidentPolicySpy()
    policy = _PolicyState().policy()

    posted = await notify.send_alert(
        policy,
        "test.opt_in",
        "fp-1",
        "condition observed",
        severity=notify.AlertSeverity.URGENT,
        template_values={"subject_key": "chase"},
        incident_policy=spy,
    )

    assert posted is True
    assert len(spy.fired) == 1
    fired = spy.fired[0]
    assert fired["alert_id"] == "test.opt_in"
    assert fired["subject"] == "test_opens_incident:chase"
    assert fired["severity"] == "urgent"
    assert fired["evidence"] == {"content": "condition observed"}


async def test_send_alert_fires_incident_policy_even_when_discord_post_is_suppressed(monkeypatch):
    """A re-fire with an unchanged fingerprint still must attach evidence to
    the open incident, even though the Discord announcement is suppressed."""
    monkeypatch.setitem(
        notify.ALERT_INVENTORY,
        "test.opt_in_suppressed",
        _opt_in_definition(alert_id="test.opt_in_suppressed", opens_incident=True),
    )
    spy = _IncidentPolicySpy()
    state = _PolicyState({
        "id": "state-1",
        "content": {
            "kind": "test_opens_incident:chase",
            "fingerprint": "fp-1",
            "last_posted_at": "2026-08-01T03:00:00",
        },
    })

    posted = await notify.send_alert(
        state.policy(),
        "test.opt_in_suppressed",
        "fp-1",
        "condition observed",
        severity=notify.AlertSeverity.URGENT,
        template_values={"subject_key": "chase"},
        incident_policy=spy,
    )

    assert posted is False
    assert state.posts == []
    assert len(spy.fired) == 1


async def test_send_alert_incident_failure_never_blocks_delivery(monkeypatch):
    monkeypatch.setitem(
        notify.ALERT_INVENTORY,
        "test.opt_in_explodes",
        _opt_in_definition(alert_id="test.opt_in_explodes", opens_incident=True),
    )

    class _ExplodingIncidentPolicy:
        async def fire(self, *args, **kwargs):
            raise RuntimeError("substrate down")

    policy = _PolicyState().policy()

    posted = await notify.send_alert(
        policy,
        "test.opt_in_explodes",
        "fp-1",
        "condition observed",
        severity=notify.AlertSeverity.URGENT,
        template_values={"subject_key": "chase"},
        incident_policy=_ExplodingIncidentPolicy(),
    )

    assert posted is True


async def test_load_finance_alert_states_flattens_and_reports_outstanding(monkeypatch):
    """The reporting query surfaces acknowledgement state with no network."""
    from tools import finance

    beads = [
        {
            "id": "state-1",
            "content": {
                "kind": "bank_sync.sync_failure",
                "fingerprint": "chase-1",
                "last_posted_at": "2026-08-30T03:00:00",
            },
        },
        {
            "id": "state-2",
            "content": {
                "kind": "plaid.reauth_required",
                "fingerprint": "chase-2",
                "last_posted_at": "2026-08-29T03:00:00",
                "acknowledged_at": "2026-08-29T07:00:00",
                "acknowledged_by": "grant",
            },
        },
    ]

    async def _query_beads(params):
        return beads

    monkeypatch.setattr(finance, "query_beads", _query_beads)

    states = await notify.load_finance_alert_states()
    outstanding = notify.outstanding_alerts(states)

    assert len(states) == 2
    assert [a["kind"] for a in outstanding] == ["bank_sync.sync_failure"]
