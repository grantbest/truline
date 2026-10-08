#!/usr/bin/env python3
"""Temporal worker for the factory dispatcher."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import Mapping
import urllib.error
import urllib.request

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.worker import Worker

from activities import ACTIVITIES
from config import (
    config,
    missing_required_config,
    missing_required_worker_executables,
    missing_worker_executables_diagnosis,
)
from schedule_runtime import (
    log_startup_mode,
    non_dispatch_schedule_ids,
    register_capacity_resume_probe_schedule,
    register_change_apply_schedule,
    register_cluster_health_schedule,
    register_dispatch_schedule,
    register_ea_apply_schedule,
    register_ea_observation_schedule,
    register_release_apply_schedule,
    register_doctrine_staleness_schedule,
    register_release_status_schedule,
    register_requirements_apply_schedule,
    register_staleness_schedule,
    register_worker_revision_drift_schedule,
)
from workflows.capacity_pause_response import CapacityPauseResumeWorkflow
from workflows.change_apply import ChangeApplyWorkflow
from workflows.cluster_health import ClusterHealthWorkflow
from workflows.dispatch_task import DISPATCH_RETRY_POLICY, DispatchTaskWorkflow
from workflows.doctrine_staleness import DoctrineStalenessReportWorkflow
from workflows.ea_apply import EaApplyWorkflow
from workflows.ea_observation import EAObservationWorkflow
from workflows.knowledge_ingestion import KnowledgeIngestionWorkflow
from workflows.merge_on_verdict import MergeOnVerdictWorkflow
from workflows.release_apply import ReleaseApplyWorkflow
from workflows.release_status import ReleaseStatusWorkflow
from workflows.requirements_apply import RequirementsApplyWorkflow
from workflows.verdict_staleness import VerdictStalenessReportWorkflow
from workflows.worker_revision_drift import WorkerRevisionDriftWorkflow
import worker_revision
from worker_revision import WorkerCheckoutDriftError, record_worker_start

TASK_QUEUE = "factory-dispatcher-dev"
# How many activity slots this worker offers Temporal, shared by all
# twenty-six registered activity types: the twelve dispatch-pipeline steps
# (activities/dispatch_steps.py) and the eleven non-dispatch reconciler /
# report / probe schedules main() registers below. This constant bounds
# THROUGHPUT ONLY. Before this was 1 and dispatch's own ~80-minute `run` step
# held that one slot for its whole budget, every reconciler queued behind it
# -- OPS-99/100 measured 15-minute reconciler ticks sitting Scheduled for up
# to 646.7s and up to 53 SkippedOverlap firings on factory-ea-apply-15m
# alone. Sized 1 (an in-flight dispatch step) + 11 (every non-dispatch
# schedule registered below: staleness, ea-apply, ea-observation,
# release-apply, release-status, requirements-apply, doctrine-staleness,
# worker-revision-drift, capacity-resume-probe, cluster-health, change-apply)
# so the worst case -- every reconciler ticking in the same instant a
# dispatch step is mid-flight -- still needs no slot to queue behind another.
#
# "At most one dispatch runs at a time" is a SEPARATE invariant and is
# deliberately NOT this constant's job any more: raising or lowering
# ACTIVITY_EXECUTOR_CONCURRENCY can never let two dispatch attempts run
# concurrently. That is enforced independently by the cross-process run lock
# in activities/dispatch_steps.py (claim_activity/cleanup_activity), which is
# also the only mechanism here that reaches dispatch.py's --once CLI path --
# a plain Python process this Worker and this executor have no visibility
# into at all. See tasks/done/FA-S26-worker-cannot-start.json, which
# originally sized this single pool to 1 for exactly the concurrency
# guarantee and warned against widening it for "parallelism nobody wants":
# that warning still holds, it just no longer applies to this constant --
# see .factory/design.md for the full split.
ACTIVITY_EXECUTOR_CONCURRENCY = 12
TEMPORAL_SCHEDULE_TYPES = type(
    "TemporalScheduleTypes",
    (),
    {
        "Schedule": Schedule,
        "ScheduleActionStartWorkflow": ScheduleActionStartWorkflow,
        "ScheduleIntervalSpec": ScheduleIntervalSpec,
        "ScheduleOverlapPolicy": ScheduleOverlapPolicy,
        "SchedulePolicy": SchedulePolicy,
        "ScheduleSpec": ScheduleSpec,
        "ScheduleState": ScheduleState,
        "ScheduleUpdate": ScheduleUpdate,
    },
)

LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Second-precision UTC with the Z designator, matching the convention the five
# standing-status writers stamp (activities/status_bead.py::iso). Local time was
# the first attempt and is not "unambiguous": one instant renders three ways on
# three hosts, a DST fall-back renders two instants identically, and a log
# stamped in local time cannot be diffed against the bead timestamps the same
# incident is reconstructed from. `converter` is set below because
# logging.Formatter defaults to time.localtime regardless of the datefmt.
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# httpx/httpcore log one INFO line per HTTP request; that was two thirds of
# this worker's log volume. Raised on these two loggers specifically, not
# the root level, so the worker's own logger.info(...) calls stay visible.
HTTP_CLIENT_LOG_LEVEL = logging.WARNING
QUIETED_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")

def configure_logging() -> None:
    """Configure the worker's logging, and mean it.

    A function rather than bare module-level statements so a test can exercise
    the real configuration against the real logging system without reloading
    this module -- reloading it re-runs the workflow decorators and breaks
    Temporal's registration identity for every other test holding a reference.

    ``force=True`` is load-bearing, not defensive: ``basicConfig`` does NOTHING
    -- silently, with no error -- when the root logger already has a handler,
    and by the time this runs it does. ``worker.py``'s own ``from activities
    import ACTIVITIES`` reaches ``cluster_health.py``, which calls
    ``basicConfig`` at import. Without ``force=`` this whole configuration is
    inert and every line the worker emits stays undated, which is the bug it
    exists to fix -- and the first attempt at this bead shipped exactly that.

    The converter is set explicitly because ``logging.Formatter`` uses
    ``time.localtime`` regardless of the datefmt, so a ``Z`` in the format
    string alone would render local time wearing a UTC designator -- worse than
    an honest local timestamp.
    """
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=LOG_LEVEL, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, force=True
    )
    for name in QUIETED_HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(HTTP_CLIENT_LOG_LEVEL)


configure_logging()
logger = logging.getLogger(__name__)

TEMPORAL_TUNNEL_CHECK_INTERVAL_SECONDS = 30
TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS = 10 * 60
TEMPORAL_TUNNEL_ALERT_DEDUP_WINDOW_SECONDS = 30 * 60
TEMPORAL_TUNNEL_ALERT_THRESHOLD_ENV = "FACTORY_TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS"
TEMPORAL_TUNNEL_ALERT_DEDUP_ENV = "FACTORY_TEMPORAL_TUNNEL_ALERT_DEDUP_SECONDS"
DISCORD_WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_USER_AGENT = "factory-dispatcher-worker/1.0"
DISCORD_WEBHOOK_TIMEOUT_SECONDS = 10

Clock = Callable[[], float]
TunnelAlertPoster = Callable[["TunnelAlert"], Awaitable[None]]


class MissingConfigError(RuntimeError):
    """The worker was started without configuration it cannot work without.

    Deliberately not an OSError or a connection error: a missing variable is
    an operator mistake that will never fix itself, and launchd restarting the
    process cannot help. A transient tunnel failure is the opposite, and the
    two must not read the same in a log.
    """


class MissingExecutableError(RuntimeError):
    """The worker was started without an executable it shells out to."""


@dataclass(frozen=True)
class TunnelAlert:
    severity: str
    event: str
    tunnel: str
    duration_seconds: float


def positive_seconds_from_env(
    values: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of seconds") from exc
    if seconds <= 0:
        raise ValueError(f"{name} must be positive") from None
    return seconds


def format_duration(seconds: float) -> str:
    remaining = max(0, int(round(seconds)))
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def tunnel_alert_content(alert: TunnelAlert) -> str:
    duration = format_duration(alert.duration_seconds)
    if alert.event == "temporal_tunnel_lost":
        return (
            "severity=urgent service=factory-dispatcher "
            f"event={alert.event} tunnel={alert.tunnel} duration={duration}. "
            "The worker cannot receive factory tasks until the Temporal tunnel "
            "is restored."
        )
    return (
        "severity=info service=factory-dispatcher "
        f"event={alert.event} tunnel={alert.tunnel} outage_duration={duration}. "
        "The Temporal tunnel is reachable again."
    )


def webhook_error_summary(exc: Exception) -> str:
    """Return a diagnostic that cannot include the webhook URL."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError status={exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError reason_type={type(exc.reason).__name__}"
    return type(exc).__name__


class DiscordWebhookPoster:
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

    async def __call__(self, alert: TunnelAlert) -> None:
        await asyncio.to_thread(self._post, alert)

    def _post(self, alert: TunnelAlert) -> None:
        payload = json.dumps({"content": tunnel_alert_content(alert)}).encode("utf-8")
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


class TemporalTunnelAlerter:
    def __init__(
        self,
        tunnel: str,
        *,
        poster: TunnelAlertPoster | None,
        clock: Clock = time.monotonic,
        threshold_seconds: int = TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS,
        dedup_window_seconds: int = TEMPORAL_TUNNEL_ALERT_DEDUP_WINDOW_SECONDS,
    ):
        self._tunnel = tunnel
        self._poster = poster
        self._clock = clock
        self._threshold_seconds = threshold_seconds
        self._dedup_window_seconds = dedup_window_seconds
        self._lost_since: float | None = None
        self._last_urgent_at: float | None = None
        self._current_loss_alerted = False
        self._current_loss_suppressed = False

    async def observe(self, reachable: bool) -> None:
        now = self._clock()
        if reachable:
            await self._observe_reachable(now)
            return
        await self._observe_unreachable(now)

    async def _observe_reachable(self, now: float) -> None:
        if self._lost_since is None:
            return
        duration = now - self._lost_since
        should_post_recovery = self._current_loss_alerted and self._poster is not None
        self._lost_since = None
        self._current_loss_alerted = False
        self._current_loss_suppressed = False
        if should_post_recovery:
            await self._post_alert(
                TunnelAlert(
                    severity="info",
                    event="temporal_tunnel_recovered",
                    tunnel=self._tunnel,
                    duration_seconds=duration,
                )
            )

    async def _observe_unreachable(self, now: float) -> None:
        if self._lost_since is None:
            self._lost_since = now
            return
        if self._current_loss_alerted or self._current_loss_suppressed:
            return
        duration = now - self._lost_since
        if duration < self._threshold_seconds:
            return
        if self._inside_dedup_window(now):
            self._current_loss_suppressed = True
            logger.info(
                "Temporal tunnel alert suppressed by dedup window: address=%s "
                "duration=%s dedup_window=%s",
                self._tunnel,
                format_duration(duration),
                format_duration(self._dedup_window_seconds),
            )
            return
        self._last_urgent_at = now
        self._current_loss_alerted = True
        await self._post_alert(
            TunnelAlert(
                severity="urgent",
                event="temporal_tunnel_lost",
                tunnel=self._tunnel,
                duration_seconds=duration,
            )
        )

    def _inside_dedup_window(self, now: float) -> bool:
        if self._last_urgent_at is None:
            return False
        return now - self._last_urgent_at < self._dedup_window_seconds

    async def _post_alert(self, alert: TunnelAlert) -> None:
        if self._poster is None:
            logger.warning(
                "Temporal tunnel alert threshold reached but %s is not set; "
                "Discord webhook post skipped.",
                DISCORD_WEBHOOK_URL_ENV,
            )
            return
        try:
            await self._poster(alert)
        except Exception as exc:  # noqa: BLE001 - alerting must never crash polling.
            logger.warning(
                "Temporal tunnel Discord webhook post failed; alert swallowed: %s",
                webhook_error_summary(exc),
            )


def temporal_tunnel_alerter_from_env(
    address: str,
    environ: Mapping[str, str] | None = None,
) -> TemporalTunnelAlerter:
    values = os.environ if environ is None else environ
    webhook_url = values.get(DISCORD_WEBHOOK_URL_ENV)
    poster = DiscordWebhookPoster(webhook_url) if webhook_url else None
    return TemporalTunnelAlerter(
        address,
        poster=poster,
        threshold_seconds=positive_seconds_from_env(
            values,
            TEMPORAL_TUNNEL_ALERT_THRESHOLD_ENV,
            TEMPORAL_TUNNEL_ALERT_THRESHOLD_SECONDS,
        ),
        dedup_window_seconds=positive_seconds_from_env(
            values,
            TEMPORAL_TUNNEL_ALERT_DEDUP_ENV,
            TEMPORAL_TUNNEL_ALERT_DEDUP_WINDOW_SECONDS,
        ),
    )


def ensure_required_config(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to start when required configuration is absent, naming what is.

    `substrate.py` reads SUBSTRATE_URL at call time, inside an activity. So
    before this guard existed, a worker missing it connected to Temporal,
    registered the schedule, logged 'worker started', and then failed every
    firing with `KeyError: 'SUBSTRATE_URL'` fifteen minutes apart. Everything
    an operator could see said the factory was healthy; it was dead for three
    hours on 2026-08-06.

    A process that cannot possibly do its job should not report that it has
    started. Same reasoning as the interpreter guard in dispatch.py and the
    installer's Temporal check in launchd_agent.py: refusing up front is the
    difference between a diagnosis and a wall of text.
    """
    missing = missing_required_config(os.environ if environ is None else environ)
    if not missing:
        return
    raise MissingConfigError(
        "factory-dispatcher worker refusing to start. Missing required "
        f"configuration: {', '.join(missing)}. This is a configuration fault, "
        "not a connectivity fault — restarting will not fix it. Set these in "
        "the worker environment (or the launchd env file) and start again."
    )


def ensure_required_executables(environ: Mapping[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    from dispatch import required_worker_executables

    missing = missing_required_worker_executables(
        values,
        required=required_worker_executables(),
    )
    if not missing:
        return
    raise MissingExecutableError(
        "factory-dispatcher worker refusing to start. "
        f"{missing_worker_executables_diagnosis(missing, values)}. "
        "This is a worker dependency fault, not a task failure — restarting "
        "will not fix it. Install the missing command-line tool or set PATH "
        "in the worker environment."
    )


def new_activity_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=ACTIVITY_EXECUTOR_CONCURRENCY,
        thread_name_prefix="factory-dispatcher-activity",
    )


def build_worker(client: Client, activity_executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            DispatchTaskWorkflow,
            DoctrineStalenessReportWorkflow,
            VerdictStalenessReportWorkflow,
            KnowledgeIngestionWorkflow,
            EaApplyWorkflow,
            EAObservationWorkflow,
            ReleaseApplyWorkflow,
            ReleaseStatusWorkflow,
            RequirementsApplyWorkflow,
            WorkerRevisionDriftWorkflow,
            CapacityPauseResumeWorkflow,
            ClusterHealthWorkflow,
            ChangeApplyWorkflow,
            MergeOnVerdictWorkflow,
        ],
        activities=ACTIVITIES,
        activity_executor=activity_executor,
        max_concurrent_activities=ACTIVITY_EXECUTOR_CONCURRENCY,
    )


async def temporal_connection_reachable(client: Client) -> bool:
    """Exercise the Temporal connection with a real RPC, not a TCP handshake.

    A `kubectl port-forward` can leave its local listener open while the
    tunnel behind it is dead: a raw TCP connect (and `nc -z`) both succeed for
    the whole outage while every actual Temporal call fails with a reset
    connection. `check_health` goes over the same channel the worker polls
    with, so it fails exactly when the worker actually cannot reach Temporal.
    """
    try:
        return bool(await client.service_client.check_health())
    except Exception:
        return False


async def monitor_temporal_tunnel(
    client: Client,
    address: str,
    alerter: TemporalTunnelAlerter | None = None,
) -> None:
    """Log tunnel loss/restoration distinctly while the SDK manages polling."""
    alerter = temporal_tunnel_alerter_from_env(address) if alerter is None else alerter
    reachable = True
    while True:
        await asyncio.sleep(TEMPORAL_TUNNEL_CHECK_INTERVAL_SECONDS)
        now_reachable = await temporal_connection_reachable(client)
        if reachable and not now_reachable:
            logger.error(
                "Temporal tunnel lost: address=%s. The worker cannot receive "
                "factory tasks until the kubectl port-forward is restored.",
                address,
            )
        elif not reachable and now_reachable:
            logger.info(
                "Temporal tunnel reachable again: address=%s. Worker polling "
                "can reconnect after wake or tunnel restart.",
                address,
            )
        await alerter.observe(now_reachable)
        reachable = now_reachable


async def main() -> None:
    # First, and unconditionally: what code this process actually loaded is
    # true regardless of whether the rest of startup succeeds, and a worker
    # that dies at the next guard should still leave a record of which
    # revision it died running. See worker_revision.py for why this can never
    # block startup.
    revision = record_worker_start()
    if revision is not None:
        logger.info(
            "factory-dispatcher worker revision: %s (captured_at=%s)",
            revision.revision,
            revision.started_at,
        )

    # Before anything else, and specifically before connecting: a worker
    # whose loaded checkout is not derived from main must never register as
    # a poller either -- that is exactly how a launchd restart during an
    # operator's mid-edit branch checkout went unnoticed until OPS-11's
    # drift report caught it hours later (worker_revision.py). Recording the
    # revision above is deliberately best-effort and never raises; this is
    # the hard gate that record was never meant to be.
    try:
        worker_revision.ensure_checkout_is_on_main_ancestor()
    except WorkerCheckoutDriftError as exc:
        logger.error("%s", exc)
        raise

    # Also before connecting: a worker that cannot reach the substrate must
    # never register as a poller, because a registered poller is what an
    # operator reads as health.
    try:
        ensure_required_config()
        ensure_required_executables()
    except (MissingConfigError, MissingExecutableError) as exc:
        logger.error("%s", exc)
        raise

    try:
        client = await Client.connect(
            config.TEMPORAL_URL,
            namespace=config.TEMPORAL_NAMESPACE,
        )
    except Exception:
        logger.exception(
            "factory-dispatcher worker could not connect to Temporal at %s "
            "in namespace %s. Verify the kubectl port-forward before launchd "
            "restarts this process.",
            config.TEMPORAL_URL,
            config.TEMPORAL_NAMESPACE,
        )
        raise
    logger.info(
        "factory-dispatcher connected to Temporal: address=%s namespace=%s",
        config.TEMPORAL_URL,
        config.TEMPORAL_NAMESPACE,
    )
    registration = await register_dispatch_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        DispatchTaskWorkflow.run,
        schedule_id=config.DISPATCH_SCHEDULE_ID,
        interval_seconds=config.DISPATCH_SCHEDULE_INTERVAL_SECONDS,
        task_queue=TASK_QUEUE,
        logger=logger,
        retry_policy=DISPATCH_RETRY_POLICY,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=registration,
    )
    # One resolution of every non-dispatch schedule id, from the one registry
    # (schedule_runtime.NON_DISPATCH_SCHEDULE_ENV_DEFAULTS) that
    # activities/worker_revision_drift.py's factory-activity-in-flight guard also reads --
    # so registering a schedule here and only adding it to one of the two lists cannot
    # happen; there is only one list.
    schedule_ids = non_dispatch_schedule_ids()
    staleness_registration = await register_staleness_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        VerdictStalenessReportWorkflow.run,
        schedule_id=schedule_ids["staleness"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=staleness_registration,
    )
    ea_apply_registration = await register_ea_apply_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        EaApplyWorkflow.run,
        schedule_id=schedule_ids["ea_apply"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=ea_apply_registration,
    )
    ea_observation_registration = await register_ea_observation_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        EAObservationWorkflow.run,
        schedule_id=schedule_ids["ea_observation"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=ea_observation_registration,
    )
    release_apply_registration = await register_release_apply_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        ReleaseApplyWorkflow.run,
        schedule_id=schedule_ids["release_apply"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=release_apply_registration,
    )
    release_status_registration = await register_release_status_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        ReleaseStatusWorkflow.run,
        schedule_id=schedule_ids["release_status"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=release_status_registration,
    )
    requirements_apply_registration = await register_requirements_apply_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        RequirementsApplyWorkflow.run,
        schedule_id=schedule_ids["requirements_apply"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=requirements_apply_registration,
    )
    doctrine_staleness_registration = await register_doctrine_staleness_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        DoctrineStalenessReportWorkflow.run,
        schedule_id=schedule_ids["doctrine_staleness"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=doctrine_staleness_registration,
    )
    worker_revision_drift_registration = await register_worker_revision_drift_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        WorkerRevisionDriftWorkflow.run,
        schedule_id=schedule_ids["worker_revision_drift"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=worker_revision_drift_registration,
    )
    capacity_resume_probe_registration = await register_capacity_resume_probe_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        CapacityPauseResumeWorkflow.run,
        schedule_id=schedule_ids["capacity_resume_probe"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=capacity_resume_probe_registration,
    )
    cluster_health_registration = await register_cluster_health_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        ClusterHealthWorkflow.run,
        schedule_id=schedule_ids["cluster_health"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=cluster_health_registration,
    )
    change_apply_registration = await register_change_apply_schedule(
        client,
        TEMPORAL_SCHEDULE_TYPES,
        ChangeApplyWorkflow.run,
        schedule_id=schedule_ids["change_apply"],
        task_queue=TASK_QUEUE,
        logger=logger,
    )
    log_startup_mode(
        logger,
        namespace=config.TEMPORAL_NAMESPACE,
        registration=change_apply_registration,
    )
    activity_executor = new_activity_executor()
    monitor_task = asyncio.create_task(monitor_temporal_tunnel(client, config.TEMPORAL_URL))
    try:
        worker = build_worker(client, activity_executor)
        logger.info(
            "factory-dispatcher worker started: namespace=%s task_queue=%s",
            config.TEMPORAL_NAMESPACE,
            TASK_QUEUE,
        )
        await worker.run()
        logger.warning(
            "factory-dispatcher worker run loop returned; launchd KeepAlive "
            "will start it again if the agent remains loaded."
        )
    except Exception:
        logger.exception(
            "factory-dispatcher worker stopped unexpectedly. If the Temporal "
            "kubectl port-forward died, restore the tunnel before relying on "
            "unattended dispatch."
        )
        raise
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        activity_executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    asyncio.run(main())
