#!/usr/bin/env python3
"""Cluster health checker — three signals that fired during the 2026-08-23

undeployability incident (PR #476 pinned six apps to @sha256:000...0) and
reached nobody: pods wedged in ImagePullBackOff/ErrImagePull, a backup or
restore-drill CronJob whose most recently scheduled run did not succeed, and
an ArgoCD Application reporting Degraded. Dashboards read healthy through all
of it because Deployments kept serving their previous ReplicaSet; the
CronJobs had no such fallback and simply failed.

The checking functions below take already-parsed ``kubectl get ... -o json``
/ ``kubectl get applications.argoproj.io -o json`` output and return
``Notification`` objects. They run no subprocess and touch no network, so
they can be driven entirely from fixtures. ``main()`` is the thin, separately
runnable wiring that shells out to kubectl and posts to Discord.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import process_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Source reuse of the mcp-hub Discord alert-policy machinery (fingerprint +
# declared re-alert-window + host-local state file), not a network call to
# the deployed mcp-hub service -- same sys.path pattern failure_diagnosis.py
# already uses, and the same reasoning: the dispatcher runs host-local
# against its own working copy of this monorepo. See .factory/design.md for
# why this is its own AlertPolicy call site rather than an import of
# failure_diagnosis.py (wrong vertical) or a new notify.ALERT_INVENTORY entry
# (apps/mcp-hub/** is out of scope for this change).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)

#: Aliased, not `from tools import notify`: this module already defines its
#: own top-level `notify()` (the raw "post this batch" primitive, predating
#: this change) which would otherwise shadow the module import of the same
#: name for every reference after its `def` executes.
from tools import notify as alert_notify  # noqa: E402

DISCORD_WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_USER_AGENT = "factory-dispatcher-cluster-health/1.0"
DISCORD_WEBHOOK_TIMEOUT_SECONDS = 10

IMAGE_PULL_FAILURE_REASONS = ("ImagePullBackOff", "ErrImagePull")
IMAGE_PULL_GRACE_SECONDS_DEFAULT = 10 * 60
IMAGE_PULL_GRACE_SECONDS_ENV = "CLUSTER_HEALTH_IMAGE_PULL_GRACE_SECONDS"

#: Cross-run dedup (Amendment 30 gate finding on #672): a finding that stays
#: open must post at most once per this many hours, not once per 15-minute
#: schedule tick -- the real pg-restore-drill incident (8 days open) would
#: otherwise have posted ~770 times, exactly the shape that gets a schedule
#: paused forever. See .factory/design.md.
FINDING_RE_ALERT_INTERVAL_HOURS_DEFAULT = 4.0
FINDING_RE_ALERT_INTERVAL_HOURS_ENV = "CLUSTER_HEALTH_FINDING_RE_ALERT_INTERVAL_HOURS"

#: Host-local execution state, never bead content (Pillar 10) -- same
#: reasoning as failure_diagnosis.ALERT_STATE_PATH_ENV, but a distinct file:
#: a bug in one checker's alert-state bookkeeping must not corrupt the
#: other's.
ALERT_STATE_PATH_ENV = "FACTORY_CLUSTER_HEALTH_ALERT_STATE_PATH"

#: Alert "kind" prefix for a specific finding (pod/cronjob/application);
#: distinguishes tracked finding kinds from FINDING_UNREACHABLE_KIND when
#: scanning the state file for entries to clear on resolution.
FINDING_KIND_PREFIX = "cluster_health.finding:"

#: The checker itself could not read the cluster -- distinct from any single
#: finding: there is only one way to say this, so it gets a fixed kind and a
#: fixed fingerprint rather than one derived per-notification.
FINDING_UNREACHABLE_KIND = "cluster_health.unreachable"
FINDING_UNREACHABLE_FINGERPRINT_SEED = "kubectl-unreachable"

#: A CronJob is in this checker's remit only if its name says it protects a
#: safety net. R8 (docs/runbooks/discord-notifications.md): page-tier is
#: "things that silently destroy a safety net (backups)", not every CronJob
#: in the cluster.
BACKUP_CRONJOB_NAME_MARKERS = ("backup", "restore-drill")

#: ReplicaSet-name pattern kubectl itself generates: `<deployment>-<hash>`.
_REPLICASET_HASH_SUFFIX = re.compile(r"-[0-9a-f]{6,10}$")


class MissingKubectlOutputError(RuntimeError):
    """kubectl (or the resource type) was unavailable; the check was skipped."""


@dataclass(frozen=True)
class Notification:
    severity: str
    source: str
    title: str
    detail: str
    #: Stable identity of *what* is failing, independent of which pod/job
    #: incarnation currently exhibits it -- the alert "kind" for cross-run
    #: dedup. Defaults to "" so ad-hoc Notifications built directly (as the
    #: existing notify()/build_payload tests do) keep working unchanged.
    key: str = ""
    #: Deliberately coarse failure description used only to compute the
    #: dedup fingerprint -- excludes volatile fields (durations, schedule
    #: timestamps, job names) that change every run even when the underlying
    #: cause hasn't, which would otherwise defeat dedup entirely. See
    #: .factory/design.md.
    reason: str = ""


def _parse_timestamp(value: str) -> datetime:
    """Parse a Kubernetes RFC3339 (`...Z`) timestamp as an aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_duration(delta: timedelta) -> str:
    remaining = max(0, int(delta.total_seconds()))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def _workload_name(pod: dict[str, Any]) -> str:
    """The deployment/workload name a human would look for, not the pod name."""
    metadata = pod.get("metadata") or {}
    owners = metadata.get("ownerReferences") or []
    if not owners:
        return str(metadata.get("name") or "(unknown pod)")
    owner_name = str(owners[0].get("name") or "")
    if owners[0].get("kind") == "ReplicaSet":
        stripped = _REPLICASET_HASH_SUFFIX.sub("", owner_name)
        if stripped:
            return stripped
    return owner_name or str(metadata.get("name") or "(unknown pod)")


def pod_image_pull_notifications(
    pods: dict[str, Any],
    *,
    now: datetime,
    grace_seconds: int = IMAGE_PULL_GRACE_SECONDS_DEFAULT,
) -> list[Notification]:
    """Pods stuck in ImagePullBackOff/ErrImagePull past `grace_seconds`.

    Duration is read from the pod's own `status.startTime` (falling back to
    `metadata.creationTimestamp`) rather than accumulated in memory, because
    this checker runs statelessly — once per invocation, not as a long-lived
    process holding state across polls the way `worker.py`'s tunnel alerter
    does.
    """
    notifications: list[Notification] = []
    for pod in pods.get("items") or []:
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        since_raw = status.get("startTime") or metadata.get("creationTimestamp")
        if not since_raw:
            continue
        since = _parse_timestamp(since_raw)
        age = now - since
        namespace = str(metadata.get("namespace") or "(unknown namespace)")
        pod_name = str(metadata.get("name") or "(unknown pod)")
        for container in status.get("containerStatuses") or []:
            waiting = ((container.get("state") or {}).get("waiting")) or {}
            reason = waiting.get("reason")
            if reason not in IMAGE_PULL_FAILURE_REASONS:
                continue
            if age.total_seconds() < grace_seconds:
                continue
            workload = _workload_name(pod)
            image = str(container.get("image") or "(no image recorded)")
            container_name = str(container.get("name") or "(unknown container)")
            notifications.append(
                Notification(
                    severity="urgent",
                    source="image-pull",
                    title=f"{namespace}/{workload} cannot pull its image",
                    detail=(
                        f"pod={namespace}/{pod_name} container={container_name} "
                        f"reason={reason} image={image} "
                        f"failing_for={format_duration(age)}"
                    ),
                    key=f"image-pull:{namespace}/{workload}/{container_name}",
                    reason=f"{reason}:{image}",
                )
            )
    return notifications


def _is_backup_cronjob(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in BACKUP_CRONJOB_NAME_MARKERS)


def _matching_jobs(
    jobs: dict[str, Any], namespace: str, cronjob_name: str
) -> list[dict[str, Any]]:
    matches = []
    for job in jobs.get("items") or []:
        job_meta = job.get("metadata") or {}
        if str(job_meta.get("namespace") or "") != namespace:
            continue
        owners = job_meta.get("ownerReferences") or []
        if any(
            owner.get("kind") == "CronJob" and owner.get("name") == cronjob_name
            for owner in owners
        ):
            matches.append(job)
    return matches


def _latest_job(jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    def _created(job: dict[str, Any]) -> str:
        return str((job.get("metadata") or {}).get("creationTimestamp") or "")

    return max(jobs, key=_created, default=None)


def _job_failure_reason(job: dict[str, Any] | None) -> str:
    if job is None:
        return "no Job found for the most recently scheduled run"
    job_name = str((job.get("metadata") or {}).get("name") or "(unknown job)")
    status = job.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            reason = condition.get("reason") or "Failed"
            message = condition.get("message") or reason
            return f"Job {job_name} failed ({reason}): {message}"
    if status.get("failed"):
        return f"Job {job_name} recorded {status['failed']} failed pod(s)"
    return f"Job {job_name} has not recorded success"


def _job_failure_category(job: dict[str, Any] | None) -> str:
    """Coarse, dedup-stable classification of why the latest Job didn't succeed.

    Deliberately excludes the job name and free-text condition message that
    `_job_failure_reason` includes for humans: a CronJob reschedules daily
    (or more often), so a fingerprint built from the job name or message
    would change on every new scheduled run even when the underlying failure
    is identical, defeating cross-run dedup. The condition `reason` (e.g.
    `BackoffLimitExceeded`) is a small, Kubernetes-controller-owned enum, not
    free text, so it stays stable across runs of the same failure.
    """
    if job is None:
        return "no-job"
    status = job.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return f"failed:{condition.get('reason') or 'Failed'}"
    if status.get("failed"):
        return "failed-pods"
    return "no-success-recorded"


def cronjob_failure_notifications(
    cronjobs: dict[str, Any],
    jobs: dict[str, Any] | None = None,
) -> list[Notification]:
    """Backup/restore-drill CronJobs whose last scheduled run did not succeed.

    Compares the CronJob controller's own `status.lastScheduleTime` against
    `status.lastSuccessfulTime` — no cron-expression parsing needed, and no
    risk of this check's notion of "the schedule window" drifting from the
    controller's. `lastSuccessfulTime` absent or older than `lastScheduleTime`
    means the most recently scheduled run has not succeeded, which covers
    both an outright failed Job and no Job having run at all as the same
    condition, per the requirement that absence is treated as failure rather
    than silence.
    """
    notifications: list[Notification] = []
    jobs = jobs or {"items": []}
    for cronjob in cronjobs.get("items") or []:
        metadata = cronjob.get("metadata") or {}
        name = str(metadata.get("name") or "")
        if not _is_backup_cronjob(name):
            continue
        namespace = str(metadata.get("namespace") or "(unknown namespace)")
        status = cronjob.get("status") or {}
        last_schedule_raw = status.get("lastScheduleTime")
        if not last_schedule_raw:
            continue
        last_schedule = _parse_timestamp(last_schedule_raw)
        last_success_raw = status.get("lastSuccessfulTime")
        last_success = _parse_timestamp(last_success_raw) if last_success_raw else None
        if last_success is not None and last_success >= last_schedule:
            continue
        matching = _matching_jobs(jobs, namespace, name)
        latest_job = _latest_job(matching)
        reason = _job_failure_reason(latest_job)
        notifications.append(
            Notification(
                severity="urgent",
                source="cronjob",
                title=f"{namespace}/{name} has no successful run for its last schedule",
                detail=(
                    f"cronjob={namespace}/{name} last_scheduled={last_schedule_raw} "
                    f"last_successful={last_success_raw or '(never)'}: {reason}"
                ),
                key=f"cronjob:{namespace}/{name}",
                reason=_job_failure_category(latest_job),
            )
        )
    return notifications


def degraded_application_notifications(
    applications: dict[str, Any],
) -> list[Notification]:
    """ArgoCD Applications currently reporting `status.health.status: Degraded`."""
    notifications: list[Notification] = []
    for application in applications.get("items") or []:
        metadata = application.get("metadata") or {}
        status = application.get("status") or {}
        health = (status.get("health") or {}).get("status")
        if health != "Degraded":
            continue
        name = str(metadata.get("name") or "(unknown application)")
        namespace = str(metadata.get("namespace") or "(unknown namespace)")
        message = (status.get("health") or {}).get("message") or "(no message recorded)"
        notifications.append(
            Notification(
                severity="urgent",
                source="argocd-application",
                title=f"ArgoCD application {namespace}/{name} is Degraded",
                detail=f"application={namespace}/{name} health_message={message}",
                key=f"argocd-application:{namespace}/{name}",
                reason=f"degraded:{message}",
            )
        )
    return notifications


def run_checks(
    *,
    pods: dict[str, Any] | None = None,
    cronjobs: dict[str, Any] | None = None,
    jobs: dict[str, Any] | None = None,
    applications: dict[str, Any] | None = None,
    now: datetime,
    grace_seconds: int = IMAGE_PULL_GRACE_SECONDS_DEFAULT,
) -> list[Notification]:
    notifications: list[Notification] = []
    if pods is not None:
        notifications.extend(
            pod_image_pull_notifications(pods, now=now, grace_seconds=grace_seconds)
        )
    if cronjobs is not None:
        notifications.extend(cronjob_failure_notifications(cronjobs, jobs))
    if applications is not None:
        notifications.extend(degraded_application_notifications(applications))
    return notifications


def _webhook_error_summary(exc: Exception) -> str:
    """Diagnostic string that cannot include the webhook URL."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError status={exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError reason_type={type(exc.reason).__name__}"
    return type(exc).__name__


class DiscordWebhookPoster:
    """Posts plain content to a Discord webhook. Synchronous — this checker

    is a one-shot invocation (cron/launchd), not a long-lived async worker.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        user_agent: str = DISCORD_WEBHOOK_USER_AGENT,
        timeout_seconds: float = DISCORD_WEBHOOK_TIMEOUT_SECONDS,
    ):
        self._webhook_url = webhook_url
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds

    def __call__(self, content: str) -> None:
        payload = json.dumps({"content": content}).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310 - URL is operator-provided.
            request,
            timeout=self._timeout_seconds,
        ) as response:
            response.read()


def build_payload(notifications: list[Notification]) -> str:
    lines = [
        f"severity=urgent service=cluster-health count={len(notifications)}",
        "Silent platform failures from the 2026-08-23 undeployability shape:",
    ]
    for notification in notifications:
        lines.append(f"- [{notification.source}] {notification.title}")
        lines.append(f"    {notification.detail}")
    return "\n".join(lines)


def notify(notifications: list[Notification], poster) -> bool:
    """Post one batched message for every finding. Never raises.

    Returns whether delivery actually happened, so a caller that recorded
    dedup state optimistically (see `finding_alert_policy`'s noop `post`)
    knows whether that record now needs reverting -- a webhook failure here
    must not read as "delivered" upstream.
    """
    if not notifications:
        return True
    if poster is None:
        logger.warning(
            "Cluster health found %d issue(s) but %s is not set; Discord "
            "webhook post skipped.",
            len(notifications),
            DISCORD_WEBHOOK_URL_ENV,
        )
        return False
    try:
        poster(build_payload(notifications))
    except Exception as exc:  # noqa: BLE001 - alerting must never crash the checker.
        logger.warning(
            "Cluster health Discord webhook post failed; alert swallowed: %s",
            _webhook_error_summary(exc),
        )
        return False
    return True


def poster_from_env(environ: Mapping[str, str] | None = None) -> DiscordWebhookPoster | None:
    values = os.environ if environ is None else environ
    webhook_url = values.get(DISCORD_WEBHOOK_URL_ENV)
    return DiscordWebhookPoster(webhook_url) if webhook_url else None


def grace_seconds_from_env(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    raw = values.get(IMAGE_PULL_GRACE_SECONDS_ENV)
    if not raw:
        return IMAGE_PULL_GRACE_SECONDS_DEFAULT
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{IMAGE_PULL_GRACE_SECONDS_ENV} must be an integer number of seconds"
        ) from exc
    if seconds <= 0:
        raise ValueError(f"{IMAGE_PULL_GRACE_SECONDS_ENV} must be positive")
    return seconds


def finding_re_alert_interval_hours_from_env(
    environ: Mapping[str, str] | None = None,
) -> float:
    values = os.environ if environ is None else environ
    raw = values.get(FINDING_RE_ALERT_INTERVAL_HOURS_ENV)
    if not raw:
        return FINDING_RE_ALERT_INTERVAL_HOURS_DEFAULT
    try:
        hours = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{FINDING_RE_ALERT_INTERVAL_HOURS_ENV} must be a number of hours"
        ) from exc
    if hours <= 0:
        raise ValueError(f"{FINDING_RE_ALERT_INTERVAL_HOURS_ENV} must be positive")
    return hours


def _alert_state_path() -> Path:
    return Path(
        os.environ.get(
            ALERT_STATE_PATH_ENV,
            str(Path.home() / ".factory-dispatcher" / "cluster-health-alert-state.json"),
        )
    )


def _read_alert_state_doc(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_alert_state_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


async def _load_finding_alert_state(kind: str) -> dict[str, Any] | None:
    doc = _read_alert_state_doc(_alert_state_path())
    entry = doc.get(kind)
    return {"content": entry} if entry else None


async def _record_finding_alert_posted(
    kind: str,
    fingerprint: str,
    _existing: dict[str, Any] | None,
    _members: list[str] | None,
) -> None:
    path = _alert_state_path()
    doc = _read_alert_state_doc(path)
    doc[kind] = {
        "fingerprint": fingerprint,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_alert_state_doc(path, doc)


async def _noop_post(_content: str) -> bool:
    """Drive AlertPolicy's decision + state bookkeeping without a network call.

    The real Discord POST happens once, in a batch, via `notify()` — see
    `notify_new_findings` and .factory/design.md for why a per-finding
    `AlertPolicy.send()` (which posts as part of deciding) would turn one
    batched message into N. Because this always reports success, the state
    write happens before the real send is even attempted; the caller keeps
    the pre-call value from `_gate_findings_async`'s `reverts` map and
    undoes the write via `_revert_recorded_state` if that real send fails.
    """
    return True


def finding_alert_policy() -> "alert_notify.AlertPolicy":
    return alert_notify.AlertPolicy(
        load_alert_state=_load_finding_alert_state,
        record_alert_posted=_record_finding_alert_posted,
        post=_noop_post,
    )


def _finding_kind(notification: Notification) -> str:
    return f"{FINDING_KIND_PREFIX}{notification.key}"


def _finding_fingerprint(notification: Notification) -> str:
    return alert_notify.alert_content_fingerprint(f"{notification.key}:{notification.reason}")


def _revert_recorded_state(reverts: dict[str, dict[str, Any] | None]) -> None:
    """Undo `_record_finding_alert_posted` writes the real batched post did not deliver.

    `AlertPolicy.send()` commits state through a noop `post` (see
    `finding_alert_policy`) so gating N findings can share one real Discord
    call instead of sending N -- but that means the record lands before the
    real send happens, not after. When that real send then fails, leaving
    the optimistic record in place would silence a finding that was never
    delivered for a full re-alert window -- the same silent-when-broken
    shape the dedup window itself exists to avoid. Restoring each kind to
    its pre-call value (or removing it if there was none) makes the next
    run treat the finding as still-new.
    """
    if not reverts:
        return
    path = _alert_state_path()
    doc = _read_alert_state_doc(path)
    changed = False
    for kind, previous_entry in reverts.items():
        if previous_entry is None:
            if kind in doc:
                del doc[kind]
                changed = True
        elif doc.get(kind) != previous_entry:
            doc[kind] = previous_entry
            changed = True
    if changed:
        _write_alert_state_doc(path, doc)


def _clear_resolved_state(active_finding_kinds: set[str]) -> None:
    """Forget alert state for anything not true this run.

    Called only after a successful `collect_kubectl_snapshot()`, so an active
    set with a kind missing means that finding resolved, and clearing
    `FINDING_UNREACHABLE_KIND` unconditionally here means "the checker can
    see the cluster again." Either way, the next occurrence — even with
    identical content — has no prior state to match against, so it posts
    immediately instead of inheriting a suppression window recorded for a
    different incident.
    """
    path = _alert_state_path()
    doc = _read_alert_state_doc(path)
    tracked = {kind for kind in doc if kind.startswith(FINDING_KIND_PREFIX)}
    stale = (tracked - active_finding_kinds) | ({FINDING_UNREACHABLE_KIND} & doc.keys())
    if not stale:
        return
    for kind in stale:
        del doc[kind]
    _write_alert_state_doc(path, doc)


async def _gate_findings_async(
    notifications: list[Notification],
    policy: "alert_notify.AlertPolicy",
    re_alert_interval_hours: float,
) -> tuple[list[Notification], dict[str, dict[str, Any] | None]]:
    """Decide which findings are new information, and what to undo if the
    real post that follows doesn't land.

    Returns the gated findings plus a `kind -> pre-call state entry` map
    (None meaning "there was no prior entry") for exactly the findings this
    call just recorded as posted -- the set `_revert_recorded_state` needs
    if the caller's real Discord send fails.
    """
    _clear_resolved_state({_finding_kind(n) for n in notifications})
    doc_before = _read_alert_state_doc(_alert_state_path())
    gated: list[Notification] = []
    reverts: dict[str, dict[str, Any] | None] = {}
    for notification in notifications:
        kind = _finding_kind(notification)
        should_post = await policy.send(
            kind,
            _finding_fingerprint(notification),
            notification.detail,
            severity=alert_notify.AlertSeverity.URGENT,
            re_alert_interval_hours=re_alert_interval_hours,
        )
        if should_post:
            gated.append(notification)
            reverts[kind] = doc_before.get(kind)
    return gated, reverts


def notify_new_findings(
    notifications: list[Notification],
    poster,
    *,
    policy: "alert_notify.AlertPolicy | None" = None,
    re_alert_interval_hours: float | None = None,
) -> list[Notification]:
    """Post only the findings that are new information since the last run.

    Reuses `alert_notify.AlertPolicy` (the same fingerprint + declared re-alert-
    window machinery `failure_diagnosis.py` already built) as its own call
    site: an identical finding persisting across runs posts at most once per
    `re_alert_interval_hours`, and a finding that stops appearing has its
    state cleared so a later recurrence posts immediately rather than
    inheriting a stale suppression. See .factory/design.md.

    Fails open: a broken dedup path (corrupt state file, import failure)
    must never suppress a finding that would otherwise have posted, so any
    exception here falls back to posting everything undeduplicated rather
    than silently dropping it.

    Dedup state for a gated finding is recorded before this function's own
    real Discord send happens (see `_gate_findings_async`), so a failed send
    here reverts those records rather than leaving a finding that was never
    delivered marked as posted for a full re-alert window.
    """
    interval = (
        re_alert_interval_hours
        if re_alert_interval_hours is not None
        else finding_re_alert_interval_hours_from_env()
    )
    reverts: dict[str, dict[str, Any] | None] = {}
    try:
        gated, reverts = asyncio.run(
            _gate_findings_async(notifications, policy or finding_alert_policy(), interval)
        )
    except Exception:  # noqa: BLE001 - dedup must never suppress a real finding by failing.
        logger.exception(
            "cluster health finding dedup failed; posting all %d finding(s) undeduplicated",
            len(notifications),
        )
        gated = notifications
    if not notify(gated, poster):
        _revert_recorded_state(reverts)
    return gated


def notify_unreachable(
    error: "MissingKubectlOutputError",
    poster,
    *,
    policy: "alert_notify.AlertPolicy | None" = None,
    re_alert_interval_hours: float | None = None,
) -> bool:
    """Deduped, best-effort notice that the checker itself cannot see the cluster.

    Distinct from `notify_new_findings`: this fires when `collect_kubectl_snapshot`
    raised before any finding could be computed, and the Discord webhook is an
    independent, internet-reachable path that does not need the LAN/tunnel kubectl
    needs (see .factory/design.md) — the one alerting path still available is exactly
    the one this posts through. Gated through the same AlertPolicy machinery and the
    same state file as findings, under a fixed kind, so it is bounded by the same
    re-alert window rather than firing on every failed schedule tick.
    """
    interval = (
        re_alert_interval_hours
        if re_alert_interval_hours is not None
        else finding_re_alert_interval_hours_from_env()
    )
    content = (
        "severity=urgent service=cluster-health event=kubectl-unreachable "
        f"detail={error}. The cluster health checker could not read any tracked "
        "resource type this run; treat it as blind, not clean, until this clears."
    )
    previous_entry = _read_alert_state_doc(_alert_state_path()).get(FINDING_UNREACHABLE_KIND)
    try:
        should_post = asyncio.run(
            (policy or finding_alert_policy()).send(
                FINDING_UNREACHABLE_KIND,
                alert_notify.alert_content_fingerprint(FINDING_UNREACHABLE_FINGERPRINT_SEED),
                content,
                severity=alert_notify.AlertSeverity.URGENT,
                re_alert_interval_hours=interval,
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the caller's re-raise.
        logger.exception("cluster health unreachable-notice dedup failed; posting anyway")
        should_post = True
    if not should_post:
        return False
    delivered = notify(
        [
            Notification(
                severity="urgent",
                source="kubectl",
                title="cluster health checker cannot see the cluster",
                detail=str(error),
                key="unreachable",
            )
        ],
        poster,
    )
    if not delivered:
        _revert_recorded_state({FINDING_UNREACHABLE_KIND: previous_entry})
    return delivered


RESOURCE_KIND_LABELS = ("pods", "cronjobs", "jobs", "applications")


def skipped_resource_kinds(snapshot: "ClusterSnapshot") -> list[str]:
    """Which tracked resource kinds came back empty this run, by name.

    A run with 1-3 of 4 kinds unreadable still runs every check it has data
    for (existing `run_checks` behavior — unchanged), which previously made
    a partial outage indistinguishable from a clean run in the result. This
    names what was skipped so a human or drain-health reader doesn't have to
    infer it. All 4 empty is the separate `MissingKubectlOutputError` case
    (the checker never got this far).
    """
    return [kind for kind in RESOURCE_KIND_LABELS if getattr(snapshot, kind) is None]


def _kubectl_json(*args: str) -> dict[str, Any] | None:
    """Run `kubectl get <args> -o json` and parse it, or None if unavailable.

    Unavailable (kubectl missing, context unreachable, resource type not
    installed — e.g. no ArgoCD CRD) is logged and treated as "this check did
    not run", never as "this check found nothing" — the caller only runs the
    checks it has data for.
    """
    try:
        proc = subprocess.run(
            ["kubectl", "get", *args, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("kubectl get %s failed: %s", " ".join(args), exc)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("kubectl get %s returned unparseable JSON: %s", " ".join(args), exc)
        return None


@dataclass(frozen=True)
class ClusterSnapshot:
    pods: dict[str, Any] | None
    cronjobs: dict[str, Any] | None
    jobs: dict[str, Any] | None
    applications: dict[str, Any] | None


def collect_kubectl_snapshot(runner: Any = _kubectl_json) -> ClusterSnapshot:
    """Fetch every resource type this checker tracks via `runner` (`_kubectl_json` by default).

    Raises `MissingKubectlOutputError` when *every* tracked resource type came back empty — the
    signature of kubectl itself being unreachable (binary missing, cluster/tunnel down), as
    opposed to one resource kind legitimately not being installed (e.g. no ArgoCD CRD) while the
    rest of the cluster answers fine. A single absent CRD cannot explain all four calls failing
    identically, so "all four empty" is what distinguishes "the checker itself is broken" from
    "the checker ran and found nothing" — see `.factory/design.md`.
    """
    snapshot = ClusterSnapshot(
        pods=runner("pods", "-A"),
        cronjobs=runner("cronjob", "-A"),
        jobs=runner("jobs", "-A"),
        applications=runner("applications.argoproj.io", "-A"),
    )
    if (
        snapshot.pods is None
        and snapshot.cronjobs is None
        and snapshot.jobs is None
        and snapshot.applications is None
    ):
        raise MissingKubectlOutputError(
            "kubectl produced no output for any tracked resource type (pods, cronjobs, jobs, "
            "applications.argoproj.io). kubectl may be missing or the cluster may be unreachable."
        )
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    grace_seconds = grace_seconds_from_env()
    poster = poster_from_env()

    try:
        snapshot = collect_kubectl_snapshot()
    except MissingKubectlOutputError as exc:
        notify_unreachable(exc, poster)
        print(f"cluster health check could not run: {exc}", file=sys.stderr)
        return 2

    notifications = run_checks(
        pods=snapshot.pods,
        cronjobs=snapshot.cronjobs,
        jobs=snapshot.jobs,
        applications=snapshot.applications,
        now=now,
        grace_seconds=grace_seconds,
    )

    for notification in notifications:
        print(f"[{notification.source}] {notification.title}")
        print(f"    {notification.detail}")

    notify_new_findings(notifications, poster)

    return 1 if notifications else 0


if __name__ == "__main__":
    sys.exit(main())
