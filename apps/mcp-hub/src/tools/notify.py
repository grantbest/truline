"""Shared Discord webhook notifier.

One implementation for every workflow that posts to Discord (bank_sync,
budget_pulse, morning_brief, bill_guardian). Two error modes:

- best-effort (default): failures are logged and swallowed — alerting must
  never crash a sync (bank_sync semantics).
- raise_on_error=True: HTTP failures propagate so Temporal's activity retry
  policy handles delivery (budget_pulse / morning_brief semantics). A missing
  webhook URL never raises in either mode — it logs and returns False.

Every message carries a ``[env]`` prefix from DEPLOY_ENV. Before this, a dev
pod's alert was textually identical to prod's: the sandbox Plaid Items behind
the dev worker went stale and posted "Plaid re-auth required for `chase`"
against the same institution slugs prod uses, so a dead *test* token read as a
broken *real* account while the Console showed chase healthy (2026-08-01).
"""

import ast
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional

if TYPE_CHECKING:
    from . import incidents
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

# Discord hard limit is 2000 chars; leave headroom for ellipsis edits.
_MAX_CONTENT = 1900


def _env_label() -> str:
    """Deployment environment tag. Unset means dev — prod sets it explicitly.

    Defaulting to "dev" is deliberate: an untagged environment is far more
    likely to be a new stage/local pod than prod, and mislabelling prod
    output as dev is the harmless direction of the error.
    """
    return (os.environ.get("DEPLOY_ENV") or "dev").strip().lower()


async def post_discord(content: str, *, raise_on_error: bool = False) -> bool:
    """Post a message to the configured Discord webhook. Returns True on success."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        logger.warning("DISCORD_WEBHOOK_URL not set; suppressing alert: %s", content[:200])
        return False
    # Prefix BEFORE truncating so the env tag survives an oversized body.
    body = f"[{_env_label()}] {content}"[:_MAX_CONTENT]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook, json={"content": body})
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001 — best-effort mode must not crash callers
        if raise_on_error:
            raise
        logger.warning("Discord webhook failed (%s): %s", type(exc).__name__, exc)
        return False


async def post_discord_raise_on_error(content: str) -> bool:
    """Post through Discord and let HTTP failures reach Temporal retries."""
    return await post_discord(content, raise_on_error=True)


def alert_content_fingerprint(content: str) -> str:
    """Stable, compact fingerprint for policy-owned duplicate detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


AlertState = Dict[str, Any]
LoadAlertState = Callable[[str], Awaitable[Optional[AlertState]]]
RecordAlertPosted = Callable[
    [str, str, Optional[AlertState], Optional[List[str]]],
    Awaitable[None],
]
PostAlert = Callable[[str], Awaitable[bool]]
LoadDeferredAlerts = Callable[[], Awaitable[List[Dict[str, Any]]]]
RecordAlertDeferred = Callable[[Dict[str, Any]], Awaitable[None]]
ClearDeferredAlerts = Callable[[List[Dict[str, Any]]], Awaitable[None]]
LoadAlertStates = Callable[[], Awaitable[List[Dict[str, Any]]]]
AcknowledgeAlert = Callable[[str, str], Awaitable[bool]]
Clock = Callable[[], datetime]

_ALERT_STATE_TYPE = "alert_state"
_ALERT_DEFERRAL_TYPE = "alert_deferral"
DISCORD_NOTIFICATION_KIND = "discord.notification"
HOUSEHOLD_TIMEZONE_ENV = "HOUSEHOLD_TIMEZONE"
DEFAULT_HOUSEHOLD_TIMEZONE = "America/Chicago"
QUIET_HOURS_START = 22
QUIET_HOURS_END = 7


class AlertSeverity(str, Enum):
    URGENT = "urgent"
    ACTIONABLE = "actionable"
    INFORMATIONAL = "informational"


ALERT_SEVERITIES = frozenset(severity.value for severity in AlertSeverity)


def normalize_alert_severity(severity: AlertSeverity | str | None) -> AlertSeverity:
    """Closed-set severity. Missing declarations fail open to interruption."""
    if severity is None:
        return AlertSeverity.URGENT
    if isinstance(severity, AlertSeverity):
        return severity
    try:
        return AlertSeverity(severity)
    except ValueError as exc:
        raise ValueError(
            f"alert severity must be one of {sorted(ALERT_SEVERITIES)}: {severity!r}"
        ) from exc


URGENT_ALERT_IDS = frozenset({
    "bank_sync.sync_failure",
    "plaid.reauth_required",
})


class _FormatValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class AlertDefinition:
    """Enumerable contract for one outbound alert family.

    ``opens_incident`` (default ``False``) is the sole, explicit, reviewed opt-in
    that lets a firing open an ``arch.incident`` record (itsm-target-state.md
    §3) — a condition worth detecting is worth a record of what was decided
    about it (PRIN-017), but only for an alert someone has actually reviewed
    for that purpose. The ~138 Alertmanager/Prometheus rules surfaced
    read-only via ``tools.homelab.get_grafana_alerts`` carry no
    ``AlertDefinition`` at all and so can never set this field: opening an
    incident requires a declared, reviewed entry in ``ALERT_INVENTORY`` first,
    the same closed-set refusal ``get_alert_definition`` already enforces for
    every unknown alert id.

    ``incident_reopen_window_hours`` (default 24.0) only matters when
    ``opens_incident`` is set: a re-fire against an incident still ``resolved``
    within this many hours reopens it (``resolved -> detected``, the only
    legal reopen edge the registered machine carries); outside the window, or
    once the incident is ``closed`` (terminal, no legal transition), a re-fire
    opens a fresh incident instead. See ``tools.incidents``.
    """

    alert_id: str
    kind_template: str
    summary: str
    severity: AlertSeverity | str | None = None
    action_label_template: Optional[str] = None
    action_url_template: Optional[str] = None
    decision_prompt_template: Optional[str] = None
    removed_reason: Optional[str] = None
    removed_at: Optional[str] = None
    opens_incident: bool = False
    incident_reopen_window_hours: float = 24.0

    def __post_init__(self) -> None:
        if self.is_removed:
            return
        object.__setattr__(self, "severity", normalize_alert_severity(self.severity))
        if self.opens_incident and self.severity == AlertSeverity.INFORMATIONAL:
            raise ValueError(
                f"{self.alert_id} opts into opens_incident but is severity "
                "informational; only actionable/urgent alerts may open an incident"
            )

    @property
    def is_removed(self) -> bool:
        return self.removed_reason is not None

    @property
    def has_next_step(self) -> bool:
        return bool(
            self.action_label_template
            or self.action_url_template
            or self.decision_prompt_template
        )

    def _render(self, template: str, values: Mapping[str, Any]) -> str:
        return template.format_map(_FormatValues({k: str(v) for k, v in values.items()}))

    def kind(self, values: Mapping[str, Any]) -> str:
        return self._render(self.kind_template, values)

    def next_step(self, values: Mapping[str, Any]) -> str:
        if self.action_label_template or self.action_url_template:
            label = self._render(self.action_label_template or "Open", values)
            url = self._render(self.action_url_template or "", values).strip()
            return f"Next step: {label}: {url}" if url else f"Next step: {label}"
        if self.decision_prompt_template:
            decision = self._render(self.decision_prompt_template, values)
            return f"Decision: {decision}"
        raise ValueError(f"{self.alert_id} has no next step")

    def with_next_step(self, content: str, values: Mapping[str, Any]) -> str:
        if "Next step:" in content or "Decision:" in content:
            return content
        return f"{content.rstrip()}\n{self.next_step(values)}"


ALERT_INVENTORY: Dict[str, AlertDefinition] = {
    "plaid.reauth_required": AlertDefinition(
        alert_id="plaid.reauth_required",
        kind_template="plaid_reauth:{institution_slug}",
        summary="Plaid item needs repair before sync can continue.",
        severity=AlertSeverity.URGENT,
        action_label_template="{action}",
        action_url_template="{portal_url}",
    ),
    "bank_sync.sync_failure": AlertDefinition(
        alert_id="bank_sync.sync_failure",
        kind_template="sync_failure:{institution_slug}",
        summary="Bank sync exhausted all scheduled attempts.",
        severity=AlertSeverity.URGENT,
        decision_prompt_template=(
            "check Temporal history for `{institution_slug}` and decide whether "
            "to rerun now or wait for the next scheduled retry."
        ),
    ),
    "bank_sync.anomalies": AlertDefinition(
        alert_id="bank_sync.anomalies",
        kind_template="bank_sync.anomalies",
        summary="New unusual transactions were detected.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review the named transactions and decide whether any charge needs "
            "dispute, recategorization, or budget follow-up."
        ),
    ),
    "bank_sync.data_quality_queue": AlertDefinition(
        alert_id="bank_sync.data_quality_queue",
        kind_template="bank_sync.data_quality_queue",
        summary="Transactions need category-quality review.",
        severity=AlertSeverity.INFORMATIONAL,
        action_label_template="Review categorization queue",
        action_url_template="{queue_url}",
    ),
    "budget_pulse.finance_nudge": AlertDefinition(
        alert_id="budget_pulse.finance_nudge",
        kind_template=DISCORD_NOTIFICATION_KIND,
        summary="Budget or bill condition needs finance triage.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "decide whether to reduce spend, pay the named bill, or revise the "
            "monthly budget."
        ),
    ),
    "morning_brief.daily": AlertDefinition(
        alert_id="morning_brief.daily",
        kind_template=DISCORD_NOTIFICATION_KIND,
        summary="Daily digest requiring the Operator to pick today's follow-ups.",
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "pick today's schedule, finance, and platform follow-ups from the brief."
        ),
    ),
    "subscription_auditor.price_hike": AlertDefinition(
        alert_id="subscription_auditor.price_hike",
        kind_template=DISCORD_NOTIFICATION_KIND,
        summary="A recurring subscription price increased.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "decide whether to keep, cancel, or downgrade the subscriptions with "
            "price hikes."
        ),
    ),
    "factory_dispatcher.dev_task_failed": AlertDefinition(
        alert_id="factory_dispatcher.dev_task_failed",
        kind_template="dev_task_failed:{bead_id}",
        summary="A dev.task entered failed and needs operator disposition.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review dev.task {bead_id} ('{title}', failure class {failure_class}) "
            "and decide whether to requeue it, supersede it, bind it to a PR, or "
            "leave it failed."
        ),
    ),
    "factory_dispatcher.stranded_doing_task": AlertDefinition(
        alert_id="factory_dispatcher.stranded_doing_task",
        kind_template="stranded_doing_task:{bead_id}",
        summary="A dev.task has been in doing with no live owner shown.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check dev.task {bead_id} ('{title}') with --report-stuck and decide "
            "whether to --release-stranded it once Temporal shows no owner, or "
            "leave it if work is still live."
        ),
    ),
    "factory_dispatcher.review_missing_pr_url": AlertDefinition(
        alert_id="factory_dispatcher.review_missing_pr_url",
        kind_template="review_missing_pr_url:{bead_id}",
        summary="A dev.task is in review with no pr_url, so reconcile can never settle it.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "bind dev.task {bead_id} ('{title}') to its PR with --bind-pr, or "
            "requeue it if no PR was ever opened."
        ),
    ),
    "factory_dispatcher.environmental_fault_breaker_latched": AlertDefinition(
        alert_id="factory_dispatcher.environmental_fault_breaker_latched",
        kind_template="environmental_fault_breaker_latched:{bead_id}",
        summary=(
            "The same environmental fault recurred on every consecutive dispatch "
            "of a dev.task past the declared bound; pick_task stopped "
            "auto-selecting it."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "diagnose the recurring fault on dev.task {bead_id} (signature "
            "{signature}, {streak} consecutive, bound {limit}) and either fix the "
            "underlying cause or force a run with `dispatch.py --task {bead_id}`."
        ),
    ),
    "factory_dispatcher.capacity_pause": AlertDefinition(
        alert_id="factory_dispatcher.capacity_pause",
        kind_template="capacity_pause",
        summary="Capacity backpressure paused the dispatch schedule.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "a capacity-resume prober now probes this pause on its own cadence and resumes it "
            "once the usage window demonstrably resets — no action needed unless it stays "
            "paused past a usage window's normal reset. ({schedule_status})"
        ),
    ),
    "factory_dispatcher.capacity_resume": AlertDefinition(
        alert_id="factory_dispatcher.capacity_resume",
        kind_template="capacity_resume",
        summary="The capacity-resume prober resumed the dispatch schedule.",
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "no action needed — informational confirmation that dispatch resumed on its own "
            "after capacity backpressure cleared. ({note})"
        ),
    ),
    "factory_dispatcher.dispatch_schedule_wedged": AlertDefinition(
        alert_id="factory_dispatcher.dispatch_schedule_wedged",
        kind_template="dispatch_schedule_wedged",
        summary=(
            "An in-flight dispatch workflow has run past its wedge threshold with "
            "no completion -- the schedule's SKIP overlap policy holds every later "
            "tick behind it, and the workflow-level retry cannot help since it is "
            "still waiting on the one activity that never returned."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check whether dispatch workflow {workflow_id} (running "
            "{running_for_minutes}m, over the {threshold_minutes}m threshold) is "
            "genuinely stuck, and terminate it by hand (`temporal workflow "
            "terminate`) if so -- disposition stays a human decision."
        ),
    ),
    "factory_dispatcher.base_ref_needs_person": AlertDefinition(
        alert_id="factory_dispatcher.base_ref_needs_person",
        kind_template="base_ref_needs_person:{base_ref}",
        summary=(
            "A base ref is behind its tracking ref and could not be fast-forwarded "
            "automatically; the dispatcher will not resolve this on its own."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "inspect {base_ref} (local {local_rev}, tracking {upstream_ref}={upstream_rev}) "
            "and resolve the reason it could not fast-forward automatically: {reason}"
        ),
    ),
    "factory_dispatcher.concurrent_clone_defer_wedged": AlertDefinition(
        alert_id="factory_dispatcher.concurrent_clone_defer_wedged",
        kind_template="concurrent_clone_defer_wedged",
        summary=(
            "Base-ref fast-forward deferred for a concurrent clone past its declared "
            "consecutive bound; the blocking workdir(s) may not be a live run."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "confirm the blocking workdir(s) ({blocking_workdirs}) are really dead before "
            "removing anything by hand; {streak} consecutive deferral(s) past the bound "
            "of {limit}."
        ),
    ),
    "factory_dispatcher.doctrine_registry_drift": AlertDefinition(
        alert_id="factory_dispatcher.doctrine_registry_drift",
        kind_template="doctrine_registry_drift",
        summary=(
            "docs/architecture/principles.md has drifted from the arch.principle beads "
            "it is a view of."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review the diverging ids ({diverging_ids}, {leader} leads) and either run "
            "`scripts/principles_sync.py push --apply` to catch the beads up to the "
            "file, or edit the file to match the beads, then rerun check-view."
        ),
    ),
    "factory_dispatcher.doctrine_registry_view_unreachable": AlertDefinition(
        alert_id="factory_dispatcher.doctrine_registry_view_unreachable",
        kind_template="doctrine_registry_view_unreachable",
        summary=(
            "principles_sync check-view could not evaluate the registry against the "
            "substrate."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check substrate connectivity/credentials ({error}) and confirm the next "
            "nightly run evaluates cleanly; this run reported neither clean nor drifted."
        ),
    ),
    "factory_dispatcher.spec_record_drift": AlertDefinition(
        alert_id="factory_dispatcher.spec_record_drift",
        kind_template="spec_record_drift",
        summary=(
            "apps/factory-dispatcher/tasks/ has drifted from dev.task bead state: a "
            "dropped filing, a done-but-unmoved spec, or an ahead-of-gate filing."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review the spec record drift ({issue_count} issue(s)) and either file the "
            "missing bead(s), run `scanner.py reconcile` for the done-but-unmoved "
            "spec(s), or route the ahead-of-gate spec(s) to review/done as their bead "
            "state indicates."
        ),
    ),
    "factory_dispatcher.spec_record_check_unreachable": AlertDefinition(
        alert_id="factory_dispatcher.spec_record_check_unreachable",
        kind_template="spec_record_check_unreachable",
        summary=(
            "The nightly spec-record reconcile could not evaluate tasks/ against "
            "dev.task bead state."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check substrate/git connectivity ({error}) and confirm the next nightly "
            "run evaluates cleanly; this run reported neither clean nor drifted."
        ),
    ),
    "factory_dispatcher.deployed_revision_drift": AlertDefinition(
        alert_id="factory_dispatcher.deployed_revision_drift",
        kind_template="deployed_revision_drift:{namespace}/{deployment}",
        summary=(
            "A deployed app's reported git_sha has drifted from main for commits its own "
            "build would have picked up."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review {namespace}/{deployment} (running {deployed_sha}) and either redeploy "
            "it (rerun its build workflow or `kubectl rollout restart`) or confirm a "
            "redeploy is already in flight."
        ),
    ),
    "factory_dispatcher.deployed_revision_check_unreachable": AlertDefinition(
        alert_id="factory_dispatcher.deployed_revision_check_unreachable",
        kind_template="deployed_revision_check_unreachable:{namespace}/{deployment}",
        summary=(
            "The nightly deployed-revision-drift check could not evaluate a deployed app's "
            "revision against main."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check kubectl/git connectivity for {namespace}/{deployment} ({error}) and "
            "confirm the next run evaluates cleanly; this run reported neither current nor "
            "drifted."
        ),
    ),
    "factory_dispatcher.workflow_run_failing": AlertDefinition(
        alert_id="factory_dispatcher.workflow_run_failing",
        kind_template="workflow_run_failing:{workflow_name}",
        summary="A GitHub Actions workflow's most recent completed run failed.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "open the run history for {workflow_name} ({run_url}) and decide whether to "
            "revert the change that broke it, push a fix, or re-run once the cause is fixed."
        ),
    ),
    "factory_dispatcher.workflow_run_check_unreachable": AlertDefinition(
        alert_id="factory_dispatcher.workflow_run_check_unreachable",
        kind_template="workflow_run_check_unreachable",
        summary="The scheduled GitHub Actions run-health check could not reach gh/GitHub.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "check gh auth/connectivity ({error}) and confirm the next run evaluates cleanly; "
            "this run reported neither clean nor failing."
        ),
    ),
    "factory_dispatcher.ea_observer_cluster_mismatch": AlertDefinition(
        alert_id="factory_dispatcher.ea_observer_cluster_mismatch",
        kind_template="ea_observer_cluster_mismatch",
        summary=(
            "The EA observer's kubectl cluster identity shares no member with the model's "
            "declared workload cluster identity, so no arch.ci can be owner-matched."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "compare the observed cluster(s) ({observed_clusters}) against the declared "
            "cluster(s) ({declared_clusters}) and either set EA_OBSERVER_CLUSTER to match "
            "the model or correct the declared arch.application workload.objects[].cluster "
            "strings so the two agree."
        ),
    ),
    "factory_dispatcher.worker_revision_drift_escalated": AlertDefinition(
        alert_id="factory_dispatcher.worker_revision_drift_escalated",
        kind_template="worker_revision_drift_escalated",
        summary=(
            "The same worker-revision-drift condition was deferred past the declared "
            "consecutive-deferral bound; the note records whether the dispatch schedule was "
            "paused or left running."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "the worker-revision-drift responder could not clear the condition after repeated "
            "deferrals ({note}); find out why the blocking schedule never went idle long enough "
            "to advance the checkout or restart the worker, then resolve that, and resume the "
            "dispatch schedule by hand only if the note says it was paused."
        ),
    ),
    "factory_dispatcher.worker_revision_drift_escalated_resolved": AlertDefinition(
        alert_id="factory_dispatcher.worker_revision_drift_escalated_resolved",
        kind_template="worker_revision_drift_escalated_resolved",
        summary=(
            "The worker-revision-drift escalation pause resolved and the dispatch schedule "
            "resumed on its own."
        ),
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "no action needed -- informational confirmation that dispatch resumed on its own "
            "after the worker-revision-drift escalation pause cleared. ({note})"
        ),
    ),
    "factory_dispatcher.worker_revision_drift_unknown_while_paused": AlertDefinition(
        alert_id="factory_dispatcher.worker_revision_drift_unknown_while_paused",
        kind_template="worker_revision_drift_unknown_while_paused",
        summary=(
            "Dispatch stayed paused by the worker-revision-drift mechanism while the drift "
            "condition itself could no longer be evaluated."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "the worker-revision-drift pause ({note}) cannot resolve on its own because the "
            "condition is unknown ({error}); find out why describe_worker_revision_drift keeps "
            "failing (unreadable worker-revision.json, unresolvable main, missing checkout) and "
            "fix that, or resume the dispatch schedule by hand once you've confirmed it's safe."
        ),
    ),
    "factory_dispatcher.queue_nothing_selectable": AlertDefinition(
        alert_id="factory_dispatcher.queue_nothing_selectable",
        kind_template="queue_nothing_selectable",
        summary="Every pending dev.task is held; the queue has nothing selectable to dispatch.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review the held classes ({hold_classes}) and either force a run with "
            "`dispatch.py --task <id>`, `--requeue <id> --reason <reason>` a stuck one, "
            "or answer the blocking question holding it."
        ),
    ),
    "factory_dispatcher.expedite_provenance_rejected": AlertDefinition(
        alert_id="factory_dispatcher.expedite_provenance_rejected",
        kind_template="expedite_provenance_rejected:{bead_id}",
        summary=(
            "A class_of_service marker has no attesting operator note (or its newest "
            "note fails the predicate), so the expedite was rejected."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review dev.task {bead_id} and the rejecting note's worker ({note_worker}); "
            "re-issue `dispatch.py --expedite {bead_id} --reason <reason> --until <date>` "
            "from the operator path if the marker should stand."
        ),
    ),
    "factory_dispatcher.release_health_drifting": AlertDefinition(
        alert_id="factory_dispatcher.release_health_drifting",
        kind_template="release_health:{ref}",
        summary="A release's health drifted: at least one signal fired, none at high impact.",
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "no action needed yet -- review release {ref}'s fired signals, and dispose "
            "with `dispatch.py --dispose-health {ref} --dispose-kind "
            "accepted|replanned|rechartered --dispose-until <date> --dispose-reason "
            "<reason>` if none is coming."
        ),
    ),
    "factory_dispatcher.release_health_breached": AlertDefinition(
        alert_id="factory_dispatcher.release_health_breached",
        kind_template="release_health:{ref}",
        summary="A release's health breached: a high-impact signal fired.",
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "review release {ref}'s fired signals and either address the breach or "
            "dispose with `dispatch.py --dispose-health {ref} --dispose-kind "
            "accepted|replanned|rechartered --dispose-until <date> --dispose-reason "
            "<reason>`."
        ),
    ),
    "factory_dispatcher.release_health_recovered": AlertDefinition(
        alert_id="factory_dispatcher.release_health_recovered",
        kind_template="release_health:{ref}",
        summary="A release's health recovered to on_track.",
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "no action needed -- release {ref} returned to on_track; clear a standing "
            "disposition with `dispatch.py --dispose-health {ref} --dispose-kind "
            "accepted|replanned|rechartered --dispose-until <date> --dispose-reason "
            "<reason>` only if one no longer applies."
        ),
    ),
    "factory_dispatcher.release_health_unmeasured": AlertDefinition(
        alert_id="factory_dispatcher.release_health_unmeasured",
        kind_template="release_health:{ref}",
        summary=(
            "A release's health could not be measured this pass (incomplete coverage "
            "or invalid policy)."
        ),
        severity=AlertSeverity.INFORMATIONAL,
        decision_prompt_template=(
            "review why release {ref} is unmeasured (coverage/policy) and dispose with "
            "`dispatch.py --dispose-health {ref} --dispose-kind "
            "accepted|replanned|rechartered --dispose-until <date> --dispose-reason "
            "<reason>` if needed; this re-posts as release_health_unmeasured_persistent "
            "(ACTIONABLE) if it persists past the policy's "
            "unmeasured_actionable_after_hours."
        ),
    ),
    "factory_dispatcher.release_health_unmeasured_persistent": AlertDefinition(
        alert_id="factory_dispatcher.release_health_unmeasured_persistent",
        kind_template="release_health_persistent:{ref}",
        summary=(
            "A release has been unmeasurable for longer than the policy's "
            "unmeasured_actionable_after_hours."
        ),
        severity=AlertSeverity.ACTIONABLE,
        decision_prompt_template=(
            "release {ref} has been unmeasured past the policy's "
            "unmeasured_actionable_after_hours; fix the coverage/policy problem or "
            "dispose with `dispatch.py --dispose-health {ref} --dispose-kind "
            "accepted|replanned|rechartered --dispose-until <date> --dispose-reason "
            "<reason>`."
        ),
    ),
    "bank_sync.quality.unreconciled_count": AlertDefinition(
        alert_id="bank_sync.quality.unreconciled_count",
        kind_template="bank_sync.quality",
        summary="Retired unreconciled transaction count.",
        removed_reason=(
            "RC-5 retired this status-only count: the ledger already shows posted "
            "transactions and there was no standalone decision for the Operator."
        ),
        removed_at="2026-08-02",
    ),
    "subscription_auditor.weekly_status": AlertDefinition(
        alert_id="subscription_auditor.weekly_status",
        kind_template=DISCORD_NOTIFICATION_KIND,
        summary="Retired active-subscription rollup with no price hike.",
        removed_reason=(
            "The subscription console already lists active subscriptions; without "
            "a price hike the weekly rollup had no next action."
        ),
        removed_at="2026-08-16",
    ),
}


def iter_alert_inventory(*, include_removed: bool = False) -> Iterable[AlertDefinition]:
    """List every alert family the system can emit or has explicitly retired."""
    for definition in ALERT_INVENTORY.values():
        if include_removed or not definition.is_removed:
            yield definition


def get_alert_definition(alert_id: str) -> AlertDefinition:
    try:
        return ALERT_INVENTORY[alert_id]
    except KeyError as exc:
        raise KeyError(f"alert is not in inventory: {alert_id}") from exc


def urgent_alert_ids() -> List[str]:
    """The alert classes allowed to bypass quiet hours."""
    return sorted(
        definition.alert_id
        for definition in iter_alert_inventory()
        if definition.severity == AlertSeverity.URGENT
    )


def _household_timezone(name: Optional[str] = None) -> ZoneInfo:
    tz_name = (
        name
        or os.environ.get(HOUSEHOLD_TIMEZONE_ENV)
        or DEFAULT_HOUSEHOLD_TIMEZONE
    ).strip()
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "unknown household timezone %r; using %s",
            tz_name,
            DEFAULT_HOUSEHOLD_TIMEZONE,
        )
        return ZoneInfo(DEFAULT_HOUSEHOLD_TIMEZONE)


def _local_now(clock: Clock, timezone_name: Optional[str]) -> datetime:
    now = clock()
    timezone = _household_timezone(timezone_name)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def is_quiet_hours(local_time: datetime) -> bool:
    hour = local_time.hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def _system_now() -> datetime:
    return datetime.now()


def _utc_now() -> datetime:
    """Wall-clock "now" for interval math — always UTC, never host-local.

    Distinct from ``_system_now``/``clock``, which drive household-timezone
    quiet-hours math on purpose. Re-alert and min-interval windows compare
    two instants in time and must not care what timezone the host happens to
    run in.
    """
    return datetime.now(timezone.utc)


def _parse_stored_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored ``last_posted_at`` as an aware UTC datetime, or None.

    Every writer NOW records ``datetime.now(timezone.utc).isoformat()`` — an
    aware UTC value (this fix aligned the two finance-side writers, which
    historically wrote naive LOCAL time). A naive value from an old state
    file is read as UTC anyway — for a Chicago-written value that makes the
    window appear expired ~5-6h early, so the deploy transition errs toward
    at most one extra post per kind, never toward permanent suppression
    (release-gate reading of the migration direction). Returns None
    when the value cannot be parsed at all, so the caller can fail open the
    same way a missing value already does.
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def send_alert(
    policy: "AlertPolicy",
    alert_id: str,
    fingerprint: str,
    content: str,
    *,
    severity: AlertSeverity | str | None = None,
    template_values: Optional[Mapping[str, Any]] = None,
    min_interval_hours: float = 0.0,
    re_alert_interval_hours: float = 0.0,
    members: Optional[List[str]] = None,
    incident_policy: "incidents.IncidentPolicy | None" = None,
) -> bool:
    """Render inventory-owned actionability before delegating delivery policy."""
    definition = get_alert_definition(alert_id)
    if definition.is_removed:
        logger.info("%s alert retired; suppressed: %s", alert_id, definition.removed_reason)
        return False
    values = template_values or {}
    declared_severity = normalize_alert_severity(severity)
    if severity is not None and declared_severity != definition.severity:
        raise ValueError(
            f"{alert_id} declared {declared_severity.value} but inventory registers "
            f"{definition.severity.value}"
        )
    if definition.opens_incident:
        await _fire_incident(definition, values, declared_severity, content, incident_policy)
    return await policy.send(
        definition.kind(values),
        fingerprint,
        definition.with_next_step(content, values),
        severity=declared_severity,
        min_interval_hours=min_interval_hours,
        re_alert_interval_hours=re_alert_interval_hours,
        members=members,
    )


async def _fire_incident(
    definition: AlertDefinition,
    values: Mapping[str, Any],
    severity: AlertSeverity,
    content: str,
    incident_policy: "incidents.IncidentPolicy | None",
) -> None:
    """Open or attach to the one arch.incident this firing may touch.

    Best-effort like every other side channel in this file (deferred-alert
    recording, alert-state persistence): a substrate hiccup on the incident
    write must never block the Discord delivery this function is nested
    inside. Imported lazily so the common (non-``opens_incident``) path never
    pays for or requires ``tools.incidents``' dependencies — no current
    ``ALERT_INVENTORY`` entry opts in, so this import never runs in practice
    yet (see ``tools.incidents`` module docstring).
    """
    from . import incidents

    policy = incident_policy or incidents.default_incident_policy()
    subject = definition.kind(values)
    try:
        await policy.fire(
            definition.alert_id,
            subject,
            # The firing's own rendered content, not definition.summary: the
            # latter is identical boilerplate across every subject (every
            # chase/wells/... bank_sync.sync_failure would read the same),
            # while content already carries the subject-specific detail a
            # human reading the incident needs.
            summary=content,
            severity=severity.value,
            reopen_window_hours=definition.incident_reopen_window_hours,
            evidence={"content": content},
        )
    except Exception as exc:  # noqa: BLE001 - incident bookkeeping must never block alert delivery
        logger.warning(
            "could not open/attach arch.incident for %s (%s); alert delivery is unaffected",
            definition.alert_id,
            exc,
        )


def console_url(path: str, *, base_url: Optional[str] = None) -> str:
    base = (base_url or os.environ.get("LIFEOPS_CONSOLE_PUBLIC_URL") or "https://lifeops.example.org").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def data_quality_queue_url(*, base_url: Optional[str] = None) -> str:
    # /rules is the existing console route for categorization review/remediation.
    return console_url("/rules", base_url=base_url)


def format_data_quality_queue_alert(count: int, *, base_url: Optional[str] = None) -> str:
    definition = get_alert_definition("bank_sync.data_quality_queue")
    queue_url = data_quality_queue_url(base_url=base_url)
    body = f"Data-quality review queue has {count} transaction(s) needing category review."
    return definition.with_next_step(body, {"queue_url": queue_url})


@dataclass(frozen=True)
class AlertPolicy:
    """Single decision point for outbound alerts.

    The state callbacks are supplied by the workflow that owns persistence.
    This module owns the policy: changed fingerprint posts, unchanged
    fingerprint suppresses, optional set shrink suppresses, optional interval
    defers, and state lookup failures fail open.
    """

    load_alert_state: LoadAlertState
    record_alert_posted: RecordAlertPosted
    post: PostAlert = post_discord
    load_deferred_alerts: Optional[LoadDeferredAlerts] = None
    record_alert_deferred: Optional[RecordAlertDeferred] = None
    clear_deferred_alerts: Optional[ClearDeferredAlerts] = None
    clock: Clock = _system_now
    household_timezone: Optional[str] = None

    async def send(
        self,
        kind: str,
        fingerprint: str,
        content: str,
        *,
        severity: AlertSeverity | str | None = None,
        min_interval_hours: float = 0.0,
        re_alert_interval_hours: float = 0.0,
        members: Optional[List[str]] = None,
    ) -> bool:
        """Send ``content`` only if the alert is new information.

        ``re_alert_interval_hours`` is opt-in (default ``0.0``, meaning: keep
        every existing caller's behavior exactly) — declaring it is what
        turns "unchanged fingerprint suppresses forever" into "unchanged
        fingerprint suppresses until the declared interval has elapsed, then
        re-alerts." Without it, a condition that recurs identically after a
        long quiet stretch inherits a suppression recorded weeks earlier and
        never re-announces (the #593 delivered-vs-seen shape).
        """
        alert_severity = normalize_alert_severity(severity)
        try:
            existing = await self.load_alert_state(kind)
        except Exception as exc:  # noqa: BLE001 — suppression is best-effort
            logger.warning(
                "alert-state lookup failed for %s (%s); posting without suppression",
                kind,
                exc,
            )
            existing = None
            state: AlertState = {}
        else:
            state = (existing or {}).get("content") or {}

        if state.get("fingerprint") == fingerprint and not self._re_alert_due(
            state, re_alert_interval_hours
        ):
            logger.info("%s alert unchanged (%s); suppressed", kind, fingerprint)
            return False

        if members is not None:
            previous = set(state.get("members") or [])
            if previous and set(members).issubset(previous):
                logger.info(
                    "%s alert set shrank to a subset of the last post; suppressed",
                    kind,
                )
                return False

        last_posted_at = state.get("last_posted_at")
        if min_interval_hours and last_posted_at:
            last = _parse_stored_timestamp(last_posted_at)
            if last is not None:
                age_hours = (_utc_now() - last).total_seconds() / 3600.0
                if age_hours < min_interval_hours:
                    logger.info(
                        "%s alert changed but only %.1fh since last post (<%.1fh); deferred",
                        kind,
                        age_hours,
                        min_interval_hours,
                    )
                    return False

        local_now = _local_now(self.clock, self.household_timezone)
        if (
            alert_severity == AlertSeverity.INFORMATIONAL
            and is_quiet_hours(local_now)
        ):
            deferred = {
                "kind": kind,
                "fingerprint": fingerprint,
                "content": content,
                "severity": alert_severity.value,
                "members": members,
                "raised_at": local_now.isoformat(),
                "household_timezone": local_now.tzinfo.key
                if isinstance(local_now.tzinfo, ZoneInfo)
                else str(local_now.tzinfo),
            }
            try:
                await self._record_deferred_alert(deferred)
            except Exception as exc:  # noqa: BLE001 — deferral is best-effort
                logger.warning(
                    "alert deferral failed for %s (%s); posting immediately",
                    kind,
                    exc,
                )
            else:
                logger.info(
                    "%s informational alert raised during quiet hours; deferred",
                    kind,
                )
                return False

        if not is_quiet_hours(local_now):
            await self.deliver_deferred_digest()

        posted = await self.post(content)
        if not posted:
            return False

        try:
            await self.record_alert_posted(kind, fingerprint, existing, members)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist alert state for %s: %s", kind, exc)
        return True

    def _re_alert_due(self, state: AlertState, re_alert_interval_hours: float) -> bool:
        """Whether an unchanged fingerprint should re-alert anyway.

        ``re_alert_interval_hours <= 0`` means the caller declared no
        interval at all — preserve the historical "unchanged forever
        suppresses" behavior exactly. A caller that declares one gets a
        subject that re-announces once it has been quiet that long, even
        with the identical fingerprint.
        """
        if re_alert_interval_hours <= 0:
            return False
        last_posted_at = state.get("last_posted_at")
        if not last_posted_at:
            return True
        last = _parse_stored_timestamp(last_posted_at)
        if last is None:
            return True
        age_hours = (_utc_now() - last).total_seconds() / 3600.0
        return age_hours >= re_alert_interval_hours

    async def _record_deferred_alert(self, deferred: Dict[str, Any]) -> None:
        if self.record_alert_deferred is None:
            raise RuntimeError("alert deferral store is not configured")
        await self.record_alert_deferred(deferred)

    async def deliver_deferred_digest(self) -> bool:
        """Deliver held informational alerts as one digest when quiet hours end."""
        if is_quiet_hours(_local_now(self.clock, self.household_timezone)):
            return False
        if self.load_deferred_alerts is None:
            return False
        try:
            deferred = await self.load_deferred_alerts()
        except Exception as exc:  # noqa: BLE001 — fail open for current alert
            logger.warning("alert deferral read failed; skipping digest: %s", exc)
            return False
        if not deferred:
            return False

        digest = format_deferred_alert_digest(deferred)
        posted = await self.post(digest)
        if not posted:
            return False

        for alert in deferred:
            try:
                existing = await self.load_alert_state(str(alert["kind"]))
                await self.record_alert_posted(
                    str(alert["kind"]),
                    str(alert["fingerprint"]),
                    existing,
                    alert.get("members"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "could not persist delivered deferred alert state for %s: %s",
                    alert.get("kind"),
                    exc,
                )

        if self.clear_deferred_alerts is not None:
            try:
                await self.clear_deferred_alerts(deferred)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not clear deferred alerts after digest: %s", exc)
        return True


def is_alert_outstanding(alert: Mapping[str, Any]) -> bool:
    """A delivered alert is outstanding until explicitly acknowledged.

    Never inferred from delivery success, elapsed time, or a later alert on
    the same kind — the only signal for "seen" is an explicit
    ``acknowledged_at``. ``record_alert_posted`` never sets it, so nothing
    on the delivery path can flip this by accident.
    """
    return not alert.get("acknowledged_at")


def outstanding_alerts(alerts: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Delivered alerts nobody has explicitly acknowledged yet."""
    return [dict(alert) for alert in alerts if is_alert_outstanding(alert)]


@dataclass(frozen=True)
class AlertAcknowledgements:
    """Read/write seam for the seen/unseen state of a delivered alert.

    Deliberately separate from ``AlertPolicy``: delivery decides whether to
    post, this decides whether a human looked at what posted. Conflating
    them would let a delivery-policy change accidentally touch what "seen"
    means, or vice versa.
    """

    load_alert_states: LoadAlertStates
    acknowledge: AcknowledgeAlert

    async def outstanding(self) -> List[Dict[str, Any]]:
        """Every delivered alert with no explicit acknowledgement on record."""
        states = await self.load_alert_states()
        return outstanding_alerts(states)


def format_deferred_alert_digest(deferred: List[Dict[str, Any]]) -> str:
    ordered = sorted(
        deferred,
        key=lambda alert: (
            str(alert.get("raised_at") or ""),
            str(alert.get("kind") or ""),
            str(alert.get("fingerprint") or ""),
        ),
    )
    lines = [f"Deferred informational alerts ({len(ordered)}):"]
    for index, alert in enumerate(ordered, start=1):
        raised_at = alert.get("raised_at") or "unknown time"
        kind = alert.get("kind") or "unknown"
        content = str(alert.get("content") or "").rstrip()
        lines.append(f"{index}. {kind} raised at {raised_at}\n{content}")
    return "\n\n".join(lines)


async def load_finance_alert_state(kind: str) -> Optional[Dict[str, Any]]:
    """Most recent ``finance.alert_state`` bead for ``kind``, or None."""
    from .finance import query_beads

    beads = await query_beads({"type": _ALERT_STATE_TYPE, "limit": 100})
    matches = [b for b in beads if (b.get("content") or {}).get("kind") == kind]
    if not matches:
        return None
    return max(
        matches,
        key=lambda b: (b.get("content") or {}).get("last_posted_at") or "",
    )


async def load_finance_deferred_alerts() -> List[Dict[str, Any]]:
    """Pending informational alerts held during household quiet hours."""
    from .finance import query_beads

    beads = await query_beads({"type": _ALERT_DEFERRAL_TYPE, "state": "pending", "limit": 500})
    alerts: List[Dict[str, Any]] = []
    for bead in beads:
        content = bead.get("content") or {}
        alerts.append({
            "id": bead.get("id"),
            "kind": content.get("kind"),
            "fingerprint": content.get("fingerprint"),
            "content": content.get("content"),
            "severity": content.get("severity"),
            "members": content.get("members"),
            "raised_at": content.get("raised_at"),
            "household_timezone": content.get("household_timezone"),
        })
    return alerts


async def record_finance_alert_deferred(deferred: Dict[str, Any]) -> None:
    """Store one held informational alert as a pending finance bead."""
    from .finance import FINANCE_NAMESPACE, substrate_beads_url, substrate_headers

    payload = {
        "namespace": FINANCE_NAMESPACE,
        "type": _ALERT_DEFERRAL_TYPE,
        "state": "pending",
        "content": deferred,
        "trust_tier": "system",
        "created_by": "notify/alert_deferral",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            substrate_beads_url(), json=payload, headers=substrate_headers()
        )
        resp.raise_for_status()


async def clear_finance_deferred_alerts(deferred: List[Dict[str, Any]]) -> None:
    """Mark delivered deferrals resolved after their digest posts."""
    from .finance import patch_bead

    for alert in deferred:
        bead_id = alert.get("id")
        if not bead_id:
            continue
        await patch_bead(
            str(bead_id),
            state="resolved",
            created_by="notify/alert_deferral_digest",
        )


async def record_finance_alert_posted(
    kind: str,
    fingerprint: str,
    existing: Optional[Dict[str, Any]],
    members: Optional[List[str]] = None,
) -> None:
    """Persist the policy decision after a Discord alert posts."""
    from .finance import (
        FINANCE_NAMESPACE,
        patch_bead,
        substrate_beads_url,
        substrate_headers,
    )

    content: Dict[str, Any] = {
        "kind": kind,
        "fingerprint": fingerprint,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
    }
    if members is not None:
        content["members"] = sorted(members)

    if existing and existing.get("id"):
        await patch_bead(
            existing["id"], content=content, created_by="notify/alert_state"
        )
        return

    payload = {
        "namespace": FINANCE_NAMESPACE,
        "type": _ALERT_STATE_TYPE,
        "state": "active",
        "content": content,
        "trust_tier": "system",
        "created_by": "notify/alert_state",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            substrate_beads_url(), json=payload, headers=substrate_headers()
        )
        resp.raise_for_status()


def finance_alert_policy(post: PostAlert = post_discord) -> AlertPolicy:
    """Alert policy backed by finance alert-state beads."""
    return AlertPolicy(
        load_alert_state=load_finance_alert_state,
        record_alert_posted=record_finance_alert_posted,
        post=post,
        load_deferred_alerts=load_finance_deferred_alerts,
        record_alert_deferred=record_finance_alert_deferred,
        clear_deferred_alerts=clear_finance_deferred_alerts,
    )


async def load_finance_alert_states() -> List[Dict[str, Any]]:
    """Every recorded ``finance.alert_state`` bead, flattened for reporting.

    One record per alert kind currently tracked (the write path patches the
    existing bead rather than appending), each carrying whatever
    acknowledgement fields ``acknowledge_finance_alert`` has set.
    """
    from .finance import query_beads

    beads = await query_beads({"type": _ALERT_STATE_TYPE, "limit": 500})
    states: List[Dict[str, Any]] = []
    for bead in beads:
        content = bead.get("content") or {}
        states.append({
            "id": bead.get("id"),
            "kind": content.get("kind"),
            "fingerprint": content.get("fingerprint"),
            "last_posted_at": content.get("last_posted_at"),
            "acknowledged_at": content.get("acknowledged_at"),
            "acknowledged_by": content.get("acknowledged_by"),
        })
    return states


async def acknowledge_finance_alert(kind: str, acknowledged_by: str) -> bool:
    """Explicitly mark the most recent delivery of ``kind`` as seen.

    Returns False when nothing has been delivered under ``kind`` —
    acknowledgement can only apply to a delivery that already happened, it
    never fabricates one.
    """
    from .finance import patch_bead

    existing = await load_finance_alert_state(kind)
    if not existing or not existing.get("id"):
        return False
    content = dict(existing.get("content") or {})
    content["acknowledged_at"] = datetime.now().isoformat()
    content["acknowledged_by"] = acknowledged_by
    await patch_bead(
        existing["id"], content=content, created_by="notify/alert_acknowledged"
    )
    return True


def finance_alert_acknowledgements() -> AlertAcknowledgements:
    """Acknowledgement seam backed by the same finance alert-state beads."""
    return AlertAcknowledgements(
        load_alert_states=load_finance_alert_states,
        acknowledge=acknowledge_finance_alert,
    )


@dataclass(frozen=True)
class AlertPolicyBypass:
    path: str
    lineno: int
    symbol: str


def find_alert_policy_bypasses(src_root: str | Path | None = None) -> List[AlertPolicyBypass]:
    """Return direct webhook calls in source code that bypass ``AlertPolicy``."""
    root = Path(src_root) if src_root is not None else Path(__file__).resolve().parents[1]
    this_file = Path(__file__).resolve()
    bypasses: List[AlertPolicyBypass] = []

    for path in sorted(root.rglob("*.py")):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == this_file:
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError):
            continue

        direct_names: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module in {"src.tools.notify", "tools.notify"}:
                    for alias in node.names:
                        if alias.name == "post_discord":
                            direct_names.add(alias.asname or alias.name)
                if node.module in {"src.tools", "tools"}:
                    for alias in node.names:
                        if alias.name == "notify":
                            module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"src.tools.notify", "tools.notify"}:
                        module_aliases.add(alias.asname or alias.name.split(".")[0])

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in direct_names:
                bypasses.append(
                    AlertPolicyBypass(str(path.relative_to(root)), node.lineno, func.id)
                )
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "post_discord"
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            ):
                bypasses.append(
                    AlertPolicyBypass(
                        str(path.relative_to(root)),
                        node.lineno,
                        f"{func.value.id}.{func.attr}",
                    )
                )

    return bypasses


def count_alert_policy_bypasses(src_root: str | Path | None = None) -> int:
    """Count direct webhook calls in source code that bypass ``AlertPolicy``."""
    return len(find_alert_policy_bypasses(src_root))


@dataclass(frozen=True)
class UnregisteredAlertSend:
    path: str
    lineno: int


@dataclass(frozen=True)
class AlertSendWithoutSeverity:
    path: str
    lineno: int


def find_unregistered_alert_sends(src_root: str | Path | None = None) -> List[UnregisteredAlertSend]:
    """Return workflow ``AlertPolicy.send`` calls that bypass the inventory."""
    root = Path(src_root) if src_root is not None else Path(__file__).resolve().parents[1] / "workflows"
    sends: List[UnregisteredAlertSend] = []

    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "send":
                sends.append(UnregisteredAlertSend(str(path.relative_to(root)), node.lineno))

    return sends


def find_alert_sends_without_declared_severity(
    src_root: str | Path | None = None,
) -> List[AlertSendWithoutSeverity]:
    """Return workflow ``send_alert`` calls missing caller-declared severity."""
    root = Path(src_root) if src_root is not None else Path(__file__).resolve().parents[1] / "workflows"
    sends: List[AlertSendWithoutSeverity] = []

    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError):
            continue
        send_alert_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"src.tools.notify", "tools.notify"}:
                for alias in node.names:
                    if alias.name == "send_alert":
                        send_alert_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id not in send_alert_names:
                continue
            if not any(keyword.arg == "severity" for keyword in node.keywords):
                sends.append(AlertSendWithoutSeverity(str(path.relative_to(root)), node.lineno))

    return sends
