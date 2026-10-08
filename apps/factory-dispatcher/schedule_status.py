#!/usr/bin/env python3
"""Report whether the factory dispatcher Temporal schedule is genuinely quiet."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import cluster_health
import failure_diagnosis
import queue_order
import tunnel_keeper
from config import DEFAULT_DISPATCH_INTERVAL_SECONDS, config
from dispatch import (
    DEFAULT_STUCK_THRESHOLD_MINUTES,
    BaseRefStatus,
    Config,
    DispatchError,
    base_ref_status,
    read_base_ref_check_record,
    resolve_task_release_states,
    select_worker,
)
from schedule_runtime import (
    CAPACITY_PAUSE_NOTE_PREFIX,
    FactoryScheduleStatus,
    describe_factory_schedule_status,
    dispatch_schedule_pause_state,
    render_factory_schedule_status,
)
from worker_revision import WorkerRevisionStatus, describe_worker_revision_drift

#: How many multiples of the schedule's own interval may pass with no
#: successful drain before that silence is reported as a fault rather than a
#: quiet queue. A schedule firing every 15 minutes that has not completed one
#: in 45+ minutes is not "nothing to do" — it is indistinguishable from firing
#: into a Temporal the worker cannot reach, which is exactly what happened for
#: 13h23m on 2026-08-25.
DEFAULT_STALE_DRAIN_MULTIPLE = 3


@dataclass(frozen=True)
class WaitingQueueStatus:
    claimable_count: int | None
    oldest_waiting_age: timedelta | None
    non_claimable_pending_count: int | None
    #: Count of pending tasks queue_order.selectable_verdict reports
    #: selectable -- distinct from claimable_count, which also requires
    #: select_worker to not raise.
    selectable_count: int | None = None
    #: Count of pending tasks latched by the environmental-fault breaker.
    latched_count: int | None = None
    error: str = ""

    @classmethod
    def could_not_determine(cls, error: BaseException | str) -> "WaitingQueueStatus":
        return cls(
            claimable_count=None,
            oldest_waiting_age=None,
            non_claimable_pending_count=None,
            selectable_count=None,
            latched_count=None,
            error=str(error),
        )


#: The closed vocabulary AC-2 asks for: a base-ref reading is exactly one of these
#: four, never a pair of independent booleans a caller has to reconcile by hand.
#: `not_checked` is distinct from all three others -- it means the authoritative
#: comparison (this checkout's tracking ref confirmed fresh against GitHub's
#: `main`, via the mirror) was never made this cycle, so `healthy` cannot be
#: asserted even though the local `stale`/`wrong` bits both happen to read False.
BaseRefCheckState = Literal["healthy", "stale", "wrong", "not_checked"]


@dataclass(frozen=True)
class BaseRefLiveStatus:
    """Whether the dispatcher's local base ref is behind its tracking remote.

    2026-08-26: a merge to main left the checkout's local `main` branch
    behind `origin/main`. `dispatch.ensure_base_ref_current` correctly
    refused to clone from it (BaseRefStaleError), but nothing surfaced that
    refusal outside the one bead's own notes -- the Temporal schedule kept
    firing every 15 minutes, `DispatchTaskWorkflow` completed rather than
    failed each time, and the board just showed pending work. That state
    lasted ten hours before anyone noticed.

    This mirrors `worker_revision.describe_worker_revision_drift`: read
    local git refs only, no network, no substrate, best-effort so a
    checkout this cannot be determined for reads as "unknown", never as
    "current".

    `checked`/`check_reason` are not derived from this local read at all --
    they come from `dispatch.read_base_ref_check_record`, host-local state
    `dispatch.ensure_base_ref_current` records each drain cycle recording
    whether it actually confirmed this checkout's tracking ref against
    GitHub's `main` (via the source mirror) this cycle, or merely compared
    against a tracking ref that might be stale. `describe_base_ref_status`
    is, and stays, a pure local git read (PRIN-004, #525) -- it never itself
    talks to GitHub or the mirror; `checked` is what tells its caller whether
    anything upstream of it recently did.
    """

    base_ref: str
    local_rev: str | None
    upstream_ref: str | None
    upstream_rev: str | None
    commits_behind: int | None
    local_only: int | None
    checked: bool = True
    check_reason: str = ""
    error: str = ""

    @property
    def stale(self) -> bool:
        return bool(self.commits_behind)

    @property
    def wrong(self) -> bool:
        """True when the base ref carries commits its tracking upstream does not
        have -- see ``dispatch.BaseRefStatus.wrong``. Independent of ``stale``:
        a ref can be wrong while not behind at all (2026-09-17's shape, where
        this read ``base_ref_stale: false`` throughout because ``stale`` was
        the only bit checked), or wrong AND behind (diverged) at once.
        """
        return bool(self.local_only)

    @property
    def state(self) -> BaseRefCheckState:
        """The closed-vocabulary reading AC-2 asks for.

        ``not_checked`` is checked FIRST and wins outright over a locally
        ``stale``/``wrong`` reading, not just over ``healthy``: this local read
        only ever compares against this checkout's own tracking ref of the
        *mirror*, and when the last drain cycle could not confirm that ref
        against GitHub, neither a false "healthy" nor a false "wrong"/"stale"
        local reading is trustworthy -- both are equally an artifact of
        comparing against data nobody has confirmed matches GitHub's `main`
        this cycle. A ``healthy`` reading is reachable only when ``checked``
        is True, i.e. the comparison actually ran fresh and passed.
        """
        if not self.checked:
            return "not_checked"
        if self.wrong:
            return "wrong"
        if self.stale:
            return "stale"
        return "healthy"

    @classmethod
    def could_not_determine(cls, base_ref: str, error: BaseException | str) -> "BaseRefLiveStatus":
        return cls(
            base_ref=base_ref,
            local_rev=None,
            upstream_ref=None,
            upstream_rev=None,
            commits_behind=None,
            local_only=None,
            checked=False,
            error=str(error),
        )

    @classmethod
    def from_base_ref_status(
        cls, status: BaseRefStatus, *, checked: bool = True, check_reason: str = ""
    ) -> "BaseRefLiveStatus":
        return cls(
            base_ref=status.base_ref,
            local_rev=status.local_rev,
            upstream_ref=status.upstream_ref,
            upstream_rev=status.upstream_rev,
            commits_behind=status.upstream_only,
            local_only=status.local_only,
            checked=checked,
            check_reason=check_reason,
        )


#: How many multiples of the keeper's own check interval may pass with no heartbeat before that
#: silence is reported as the keeper being dead rather than merely between checks. Matches the
#: default `tunnel_keeper.check_heartbeat` staleness threshold, so the LaunchAgent watchdog and
#: this on-demand report agree on what "stale" means without either hard-coding the other's
#: number.
DEFAULT_TUNNEL_KEEPER_STALE_MULTIPLE = tunnel_keeper.DEFAULT_HEARTBEAT_STALE_MULTIPLE


@dataclass(frozen=True)
class TunnelKeeperLiveStatus:
    """Whether the tunnel keeper (the supervised replacement for the stood-down launchd tunnel
    LaunchAgents -- macOS Local Network privacy denies them LAN access, 2026-09-04) is alive.

    Read from the keeper's own heartbeat file (`tunnel_keeper.read_heartbeat`): a local-file
    read, no RPC into the keeper process, matching `describe_worker_revision_drift`'s design so
    this answers "is the tunnel keeper alive" even when the keeper itself is wedged or dead.
    Best-effort: a host with no keeper ever started reads "could not determine," never "dead" --
    those are different facts, and only one of them means a human needs to act right now.
    """

    heartbeat_at: str | None
    pid: int | None
    pid_running: bool | None
    links: dict[str, Any]
    error: str = ""

    @classmethod
    def could_not_determine(cls, error: BaseException | str) -> "TunnelKeeperLiveStatus":
        return cls(heartbeat_at=None, pid=None, pid_running=None, links={}, error=str(error))


def describe_tunnel_keeper_status(
    *,
    state_path: Path | None = None,
) -> TunnelKeeperLiveStatus:
    """Live, local-only read of the tunnel keeper's heartbeat file."""
    try:
        record = tunnel_keeper.read_heartbeat(state_path)
        if record is None:
            return TunnelKeeperLiveStatus.could_not_determine(
                "no tunnel keeper heartbeat file found -- has the keeper ever been started? "
                f"See docs/runbooks/factory-tunnel-keeper.md. Recovery: {tunnel_keeper.RECOVERY_COMMAND}"
            )
        pid_running = (
            tunnel_keeper.default_pid_is_running(record.pid) if record.pid is not None else None
        )
        return TunnelKeeperLiveStatus(
            heartbeat_at=record.heartbeat_at.isoformat(),
            pid=record.pid,
            pid_running=pid_running,
            links=record.links,
        )
    except Exception as exc:  # noqa: BLE001 - status must say unknown, not "alive".
        return TunnelKeeperLiveStatus.could_not_determine(exc)


def describe_base_ref_status(cfg: Config | None = None) -> BaseRefLiveStatus:
    """Live, local-only read of whether the base ref is behind its remote.

    Best-effort like `describe_worker_revision_drift`: a checkout with no
    upstream configured, or no git at all, reports "could not determine"
    rather than raising -- a status check must never crash the thing it is
    checking on.
    """
    cfg = cfg if cfg is not None else Config.from_env()
    try:
        status = base_ref_status(cfg)
    except Exception as exc:  # noqa: BLE001 - status must say unknown, not "current".
        return BaseRefLiveStatus.could_not_determine(cfg.base_ref, exc)
    check_record = read_base_ref_check_record()
    return BaseRefLiveStatus.from_base_ref_status(
        status, checked=check_record.checked, check_reason=check_record.reason
    )


class _NotServedSentinel:
    """Marks an IdleInputs field this host does not serve at all.

    Distinct from ``"unknown"`` (a read that was attempted and failed):
    describe_idle_reason must never synthesise a reason or a healthy mark for
    an input nobody even tried to read -- that is exactly the gap PC-EXE-007
    /AC-6 exists to close. A real singleton (rather than a plain ``object()``)
    so every caller -- this CLI, and B14's hub -- compares against the same
    importable constant with ``is``.
    """

    def __repr__(self) -> str:
        return "NOT_SERVED"


#: Module constant: "this host does not serve this input," never an error.
NOT_SERVED = _NotServedSentinel()


@dataclass(frozen=True)
class SchedulePauseInput:
    """The schedule facts describe_idle_reason needs: nothing it has to make
    a second Temporal round trip for."""

    paused: bool
    note: str = ""


@dataclass(frozen=True)
class TunnelKeeperIdleInput:
    """Pre-computed tunnel-keeper staleness, so describe_idle_reason stays
    pure -- staleness is a function of `now`, which only the caller has."""

    down: bool
    detail: str = ""


#: The six inputs, in the fixed order AC-1/AC-2 both rely on: it is the order
#: `inputs_not_served` is reported in, and the order multiple 'unknown'
#: inputs are reported in.
IDLE_INPUT_FIELDS = (
    "schedule",
    "worker_revision",
    "base_ref",
    "tunnel_keeper",
    "queue",
    "in_flight",
)


@dataclass(frozen=True)
class IdleInputs:
    """The six facts describe_idle_reason answers "why isn't it running" from.

    Every field holds exactly one of: a value object (a successful read),
    the string ``"unknown"`` (a read that was attempted and failed -- the
    caller tracks the error text itself; it is not threaded through here),
    or ``NOT_SERVED`` (this host never serves this input at all -- B14's
    hub case). No field has a default: a caller that forgets one gets a
    ``TypeError`` from the dataclass constructor, not a silently-healthy
    input nobody actually read.
    """

    schedule: Any
    worker_revision: Any
    base_ref: Any
    tunnel_keeper: Any
    queue: Any
    in_flight: Any


@dataclass(frozen=True)
class IdleReason:
    running: bool | str
    reasons: list[list[str]]
    inputs_not_served: list[str]


def describe_idle_reason(inputs: IdleInputs) -> IdleReason:
    """Pure answer to "why is the factory not running," from six named inputs.

    PC-EXE-007/AC-6. Precedence is load-bearing: any input read as
    ``"unknown"`` makes the whole answer ``"unknown"`` -- named by which
    input failed -- before any reason is derived from the inputs that did
    come back. Only once every supplied input is known does this derive
    reasons, and it never derives one for an input that is ``NOT_SERVED``:
    synthesising a reason (or a healthy mark) for an input nobody served
    would be exactly the false-healthy failure this function exists to
    close.
    """
    values = {name: getattr(inputs, name) for name in IDLE_INPUT_FIELDS}
    not_served = [name for name in IDLE_INPUT_FIELDS if values[name] is NOT_SERVED]

    unknown = [name for name in IDLE_INPUT_FIELDS if values[name] == "unknown"]
    if unknown:
        reasons = [[f"{name}_unknown", f"{name} could not be read"] for name in unknown]
        return IdleReason(running="unknown", reasons=reasons, inputs_not_served=not_served)

    reasons = []

    schedule = values["schedule"]
    if schedule is not NOT_SERVED and schedule.paused:
        if schedule.note.startswith(CAPACITY_PAUSE_NOTE_PREFIX):
            reasons.append(["capacity_pause", schedule.note])
        else:
            reasons.append(["schedule_paused", schedule.note])

    base_ref = values["base_ref"]
    if base_ref is not NOT_SERVED and base_ref.stale:
        reasons.append(["base_ref_stale", _base_ref_stale_detail(base_ref)])

    tunnel_keeper_input = values["tunnel_keeper"]
    if tunnel_keeper_input is not NOT_SERVED and tunnel_keeper_input.down:
        reasons.append(["tunnel_keeper_down", tunnel_keeper_input.detail])

    queue = values["queue"]
    if queue is not NOT_SERVED and queue.selectable_count == 0:
        reasons.append(["nothing_selectable", f"latched {queue.latched_count}"])

    in_flight = values["in_flight"]
    has_in_flight = in_flight is not NOT_SERVED and in_flight > 0

    running = bool(has_in_flight or not reasons)
    return IdleReason(running=running, reasons=reasons, inputs_not_served=not_served)


def _schedule_idle_input(
    status: FactoryScheduleStatus | None, note: str, error: str, note_error: str
) -> Any:
    if error or status is None or note_error:
        return "unknown"
    return SchedulePauseInput(paused=status.paused, note=note)


def _worker_revision_idle_input(status: WorkerRevisionStatus) -> Any:
    return "unknown" if status.error else status


def _base_ref_idle_input(status: BaseRefLiveStatus) -> Any:
    return "unknown" if status.error else status


def _tunnel_keeper_idle_input(status: TunnelKeeperLiveStatus, *, now: datetime) -> Any:
    if status.error:
        return "unknown"
    down, _age = _tunnel_keeper_stale(status, now=now)
    detail = _tunnel_keeper_stale_detail(status) if down else ""
    return TunnelKeeperIdleInput(down=down, detail=detail)


def _queue_idle_input(status: WaitingQueueStatus) -> Any:
    return "unknown" if status.error else status


def _in_flight_idle_input(status: FactoryScheduleStatus | None, error: str) -> Any:
    if error or status is None:
        return "unknown"
    return len(status.in_flight)


def _build_idle_inputs(
    *,
    status: FactoryScheduleStatus | None,
    schedule_note: str,
    schedule_error: str,
    schedule_note_error: str,
    worker_revision_status: WorkerRevisionStatus,
    base_ref_status: BaseRefLiveStatus,
    tunnel_keeper_status: TunnelKeeperLiveStatus,
    queue_status: WaitingQueueStatus,
    now: datetime,
) -> IdleInputs:
    """Build the six IdleInputs this CLI serves -- all six, always; NOT_SERVED
    is for B14's hub, which does not have a Temporal client of its own."""
    return IdleInputs(
        schedule=_schedule_idle_input(status, schedule_note, schedule_error, schedule_note_error),
        worker_revision=_worker_revision_idle_input(worker_revision_status),
        base_ref=_base_ref_idle_input(base_ref_status),
        tunnel_keeper=_tunnel_keeper_idle_input(tunnel_keeper_status, now=now),
        queue=_queue_idle_input(queue_status),
        in_flight=_in_flight_idle_input(status, schedule_error),
    )


def _idle_errors(
    *,
    schedule_error: str,
    schedule_note_error: str,
    worker_revision_status: WorkerRevisionStatus,
    base_ref_status: BaseRefLiveStatus,
    tunnel_keeper_status: TunnelKeeperLiveStatus,
    queue_status: WaitingQueueStatus,
) -> dict[str, str]:
    """The error text behind every 'unknown' IdleInputs field, keyed by input
    name -- describe_idle_reason itself never sees this; it is for this CLI's
    own rendering (the IDLE block's error lines, and the --json 'errors' key)."""
    errors: dict[str, str] = {}
    if schedule_error:
        errors["schedule"] = schedule_error
        errors["in_flight"] = schedule_error
    elif schedule_note_error:
        errors["schedule"] = schedule_note_error
    if worker_revision_status.error:
        errors["worker_revision"] = worker_revision_status.error
    if base_ref_status.error:
        errors["base_ref"] = base_ref_status.error
    if tunnel_keeper_status.error:
        errors["tunnel_keeper"] = tunnel_keeper_status.error
    if queue_status.error:
        errors["queue"] = queue_status.error
    return errors


def _idle_reason_lines(idle_reason: IdleReason, errors: dict[str, str]) -> list[str]:
    running = (
        idle_reason.running
        if isinstance(idle_reason.running, str)
        else str(idle_reason.running).lower()
    )
    lines = ["IDLE:", f"running: {running}"]
    for reason_class, detail in idle_reason.reasons:
        lines.append(f"{reason_class}: {detail}")
    lines.append(
        "inputs_not_served: "
        + (", ".join(idle_reason.inputs_not_served) if idle_reason.inputs_not_served else "none")
    )
    for name, text in errors.items():
        lines.append(f"idle_{name}_error: {text}")
    return lines


async def _read_schedule_status(
    *, address: str, namespace: str, schedule_id: str
) -> tuple[FactoryScheduleStatus | None, str, str, str]:
    """Live Temporal read of the schedule's pause/note/in-flight facts.

    Returns ``(status, note, error, note_error)``: ``status`` is ``None``
    iff ``error`` is non-empty, and ``error`` is set only by the
    connect-or-describe failure AC-3 names -- a failed read here must read
    as "unknown," never as "running," for a redeploy script watching for
    the ``in_flight_workflows:`` line to decide whether it is safe to
    restart. The pause-note read is a second, independent RPC on the same
    client; its own failure is carried out separately as ``note_error``
    rather than folded into ``error`` -- a note-read failure must still
    render the schedule's status, print the ``in_flight_workflows:`` line,
    run the notifications, and exit ``0 if status.genuinely_quiet else 1``
    exactly as a fully successful read does; only the ``schedule`` IdleInput
    itself (never ``in_flight``) reads as "unknown" because of it (RC-4).
    """
    try:
        from temporalio.client import Client

        client = await Client.connect(address, namespace=namespace)
        status = await describe_factory_schedule_status(client, schedule_id=schedule_id)
    except Exception as exc:  # noqa: BLE001 - status must say unknown, not running.
        return None, "", str(exc), ""

    try:
        _, note = await dispatch_schedule_pause_state(client, schedule_id=schedule_id)
    except Exception as exc:  # noqa: BLE001 - the schedule read above already succeeded.
        return status, "", "", str(exc)

    return status, note, "", ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report pause state and in-flight factory workflows together."
    )
    parser.add_argument(
        "--address",
        default=os.environ.get("TEMPORAL_URL"),
        help="Temporal address; defaults to TEMPORAL_URL",
    )
    parser.add_argument(
        "--namespace",
        default=os.environ.get("TEMPORAL_NAMESPACE", "dev"),
        help="Temporal namespace; defaults to TEMPORAL_NAMESPACE or dev",
    )
    parser.add_argument(
        "--schedule-id",
        default=os.environ.get(
            "FACTORY_DISPATCH_SCHEDULE_ID",
            "factory-dispatcher-dev",
        ),
        help="Temporal schedule id; defaults to FACTORY_DISPATCH_SCHEDULE_ID",
    )
    parser.add_argument(
        "--stuck-threshold-minutes",
        type=int,
        default=DEFAULT_STUCK_THRESHOLD_MINUTES,
        help=(
            "minutes before an in-flight workflow is reported as wedged; "
            f"defaults to dispatch.DEFAULT_STUCK_THRESHOLD_MINUTES "
            f"({DEFAULT_STUCK_THRESHOLD_MINUTES})"
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=config.DISPATCH_SCHEDULE_INTERVAL_SECONDS,
        help=(
            "the dispatch schedule's own firing interval, used to judge drain "
            "staleness; defaults to FACTORY_DISPATCH_INTERVAL_SECONDS"
        ),
    )
    parser.add_argument(
        "--stale-drain-multiple",
        type=int,
        default=DEFAULT_STALE_DRAIN_MULTIPLE,
        help=(
            "multiples of --interval-seconds with no successful drain before "
            f"that is reported as a fault; defaults to {DEFAULT_STALE_DRAIN_MULTIPLE}"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the idle-reason object as JSON instead of the text report",
    )
    args = parser.parse_args(argv)

    if not args.address:
        parser.error("--address or TEMPORAL_URL is required")

    return asyncio.run(
        _main(
            address=args.address,
            namespace=args.namespace,
            schedule_id=args.schedule_id,
            stuck_threshold_minutes=args.stuck_threshold_minutes,
            interval_seconds=args.interval_seconds,
            stale_drain_multiple=args.stale_drain_multiple,
            json_output=args.json,
        )
    )


async def _main(
    *,
    address: str,
    namespace: str,
    schedule_id: str,
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    interval_seconds: int = DEFAULT_DISPATCH_INTERVAL_SECONDS,
    stale_drain_multiple: int = DEFAULT_STALE_DRAIN_MULTIPLE,
    json_output: bool = False,
) -> int:
    now = datetime.now(timezone.utc)
    status, schedule_note, schedule_error, schedule_note_error = await _read_schedule_status(
        address=address, namespace=namespace, schedule_id=schedule_id
    )
    queue_status = describe_waiting_queue()
    worker_revision_status = describe_worker_revision_drift()
    base_ref_live_status = describe_base_ref_status()
    tunnel_keeper_live_status = describe_tunnel_keeper_status()

    idle_reason = describe_idle_reason(
        _build_idle_inputs(
            status=status,
            schedule_note=schedule_note,
            schedule_error=schedule_error,
            schedule_note_error=schedule_note_error,
            worker_revision_status=worker_revision_status,
            base_ref_status=base_ref_live_status,
            tunnel_keeper_status=tunnel_keeper_live_status,
            queue_status=queue_status,
            now=now,
        )
    )
    errors = _idle_errors(
        schedule_error=schedule_error,
        schedule_note_error=schedule_note_error,
        worker_revision_status=worker_revision_status,
        base_ref_status=base_ref_live_status,
        tunnel_keeper_status=tunnel_keeper_live_status,
        queue_status=queue_status,
    )

    if json_output:
        print(
            json.dumps(
                {
                    "idle_reason": {
                        "running": idle_reason.running,
                        "reasons": idle_reason.reasons,
                        "inputs_not_served": idle_reason.inputs_not_served,
                        "errors": errors,
                    }
                }
            )
        )
    else:
        lines: list[str] = []
        if status is not None:
            lines.append(
                render_schedule_status(
                    status,
                    namespace=namespace,
                    address=address,
                    stuck_threshold_minutes=stuck_threshold_minutes,
                    queue_status=queue_status,
                    worker_revision_status=worker_revision_status,
                    base_ref_status=base_ref_live_status,
                    tunnel_keeper_status=tunnel_keeper_live_status,
                    interval_seconds=interval_seconds,
                    stale_drain_multiple=stale_drain_multiple,
                )
            )
        else:
            # No FactoryScheduleStatus to render from -- print the describers
            # that need no Temporal status instead of crashing, and
            # deliberately print NO `in_flight_workflows:` line: that line's
            # absence is what scripts/factory-redeploy.py's check_in_flight
            # treats as unsafe to restart, and a line asserted from a failed
            # read would let a redeploy kickstart over work that may still
            # be running.
            lines.append(f"schedule_status_error: {schedule_error}")
            lines.extend(_worker_revision_lines(worker_revision_status, now=now))
            lines.extend(_base_ref_lines(base_ref_live_status))
            lines.extend(_tunnel_keeper_lines(tunnel_keeper_live_status, now=now))
            lines.extend(_waiting_queue_lines(queue_status))
        lines.append("")
        lines.extend(_idle_reason_lines(idle_reason, errors))
        print("\n".join(lines))

    if status is None:
        return 1

    notifications = factory_health_notifications(
        status,
        stuck_threshold_minutes=stuck_threshold_minutes,
        interval_seconds=interval_seconds,
        stale_drain_multiple=stale_drain_multiple,
        base_ref_status=base_ref_live_status,
        tunnel_keeper_status=tunnel_keeper_live_status,
    )
    cluster_health.notify(notifications, cluster_health.poster_from_env())
    # announce_schedule_wedged (like every sibling announce_* in
    # failure_diagnosis.py) assumes it owns the only event loop and calls
    # asyncio.run() internally -- this coroutine is already running inside
    # one (asyncio.run(_main(...)) in main() below), so the call is bridged
    # onto its own thread rather than special-cased.
    await asyncio.to_thread(
        announce_wedged_dispatch_workflows,
        status,
        stuck_threshold_minutes=stuck_threshold_minutes,
    )
    return 0 if status.genuinely_quiet else 1


def render_schedule_status(
    status: FactoryScheduleStatus,
    *,
    namespace: str,
    address: str = "$TEMPORAL_URL",
    worker_command: str = "python worker.py",
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    queue_status: WaitingQueueStatus | None = None,
    worker_revision_status: WorkerRevisionStatus | None = None,
    base_ref_status: BaseRefLiveStatus | None = None,
    tunnel_keeper_status: TunnelKeeperLiveStatus | None = None,
    interval_seconds: int | None = None,
    stale_drain_multiple: int = DEFAULT_STALE_DRAIN_MULTIPLE,
    now: datetime | None = None,
) -> str:
    """Render schedule status plus in-flight age diagnostics."""
    rendered = render_factory_schedule_status(
        status,
        namespace=namespace,
        address=address,
        worker_command=worker_command,
    )

    now_value = now or datetime.now(timezone.utc)
    inserted_lines: list[str] = []
    if queue_status is not None:
        inserted_lines.extend(_waiting_queue_lines(queue_status))
    if worker_revision_status is not None:
        inserted_lines.extend(_worker_revision_lines(worker_revision_status, now=now_value))
    if base_ref_status is not None:
        inserted_lines.extend(_base_ref_lines(base_ref_status))
    if tunnel_keeper_status is not None:
        inserted_lines.extend(_tunnel_keeper_lines(tunnel_keeper_status, now=now_value))
    if interval_seconds is not None:
        inserted_lines.extend(
            _drain_staleness_lines(
                status,
                interval_seconds=interval_seconds,
                stale_drain_multiple=stale_drain_multiple,
                now=now_value,
            )
        )
    if status.in_flight:
        inserted_lines.extend(
            _in_flight_age_lines(
                status,
                stuck_threshold_minutes=stuck_threshold_minutes,
                now=now_value,
            )
        )
    if not inserted_lines:
        return rendered

    lines = rendered.splitlines()
    insert_after = _operator_diagnostic_insert_index(lines, status)
    return "\n".join(lines[:insert_after] + inserted_lines + lines[insert_after:])


def describe_waiting_queue(
    store: Any | None = None,
    *,
    now: datetime | None = None,
) -> WaitingQueueStatus:
    """Return claimable pending task count and age, or an explicit unknown."""
    try:
        if store is None:
            from substrate import default_store

            store = default_store()
        return _describe_waiting_queue_from_store(
            store,
            now=now or datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 - status must report unknown, not zero.
        return WaitingQueueStatus.could_not_determine(exc)


def _describe_waiting_queue_from_store(
    store: Any,
    *,
    now: datetime,
) -> WaitingQueueStatus:
    tasks = list(store.list_tasks())
    pending = [task for task in tasks if _task_state(task) == "pending"]
    # Raises on an unreachable substrate, same as every other read here — the
    # outer describe_waiting_queue() catch turns that into "could not
    # determine", never a claimable count computed without this gate. Named
    # in the re-raise so the input that failed is visible in the reported
    # error, not masked by whatever generic text the underlying exception
    # happened to carry (or by a later read failing first and taking credit).
    try:
        release_by_task_id = resolve_task_release_states(store, pending)
    except Exception as exc:
        raise RuntimeError(f"release state could not be read: {type(exc).__name__}: {exc}") from exc

    claimable: list[dict[str, Any]] = []
    non_claimable = 0
    selectable_count = 0
    latched_count = 0
    for task in pending:
        notes = store.list_notes(task["id"])
        verdict = queue_order.selectable_verdict(task, notes, tasks, release_by_task_id)
        if verdict.selectable:
            selectable_count += 1
            try:
                select_worker(task)
            except DispatchError:
                non_claimable += 1
                continue
            claimable.append(task)
        else:
            non_claimable += 1
            if verdict.latched:
                latched_count += 1

    return WaitingQueueStatus(
        claimable_count=len(claimable),
        oldest_waiting_age=_oldest_waiting_age(store, claimable, now),
        non_claimable_pending_count=non_claimable,
        selectable_count=selectable_count,
        latched_count=latched_count,
    )


def _operator_diagnostic_insert_index(
    lines: list[str],
    status: FactoryScheduleStatus,
) -> int:
    try:
        return lines.index(f"drains_failing: {str(status.drains_failing).lower()}") + 1
    except ValueError:
        return len(lines)


def _waiting_queue_lines(status: WaitingQueueStatus) -> list[str]:
    if status.error:
        return [
            "waiting_claimable_tasks: could-not-determine",
            "oldest_waiting_age: could-not-determine",
            "pending_not_claimable_tasks: could-not-determine",
            f"waiting_queue_error: {status.error}",
        ]

    return [
        f"waiting_claimable_tasks: {status.claimable_count}",
        f"oldest_waiting_age: {_format_waiting_age(status)}",
        f"pending_not_claimable_tasks: {status.non_claimable_pending_count}",
    ]


def _worker_revision_lines(status: WorkerRevisionStatus, *, now: datetime) -> list[str]:
    if status.error:
        return [
            "worker_revision: could-not-determine",
            "worker_revision_drift: could-not-determine",
            f"worker_revision_error: {status.error}",
        ]

    started_at = _parse_time(status.worker_started_at)
    running_for = max(timedelta(), _as_utc(now) - started_at) if started_at else None
    lines = [
        f"worker_revision: {status.worker_revision}",
        f"worker_running_for: {_format_duration(running_for)}",
        f"worker_revision_drift: {str(status.drifted).lower()}",
    ]
    if status.drifted:
        lines.extend(["", _worker_revision_drift_detail(status, running_for=running_for)])
    return lines


def _worker_revision_drift_detail(
    status: WorkerRevisionStatus,
    *,
    running_for: timedelta | None,
) -> str:
    """The operator-facing drift sentence, worded to match the needed action.

    'Behind main' (still an ancestor) and 'not on main at all' are both
    drift, but they call for different operator responses -- a restart
    versus investigating an unreviewed branch -- so the wording must not
    collapse them into one sentence.
    """
    running = f"It has been running for {_format_duration(running_for)}."
    if status.is_ancestor is False:
        return (
            "WORKER REVISION DRIFT: the running worker loaded "
            f"{status.worker_revision}, which is not on {status.main_ref} "
            f"({status.main_revision}) at all. {running} This worker is "
            "running an unreviewed branch, not code that has landed on "
            f"{status.main_ref} -- it needs investigation, not just a restart."
        )
    plural = "commit" if status.commits_behind == 1 else "commits"
    return (
        "WORKER REVISION DRIFT: the running worker loaded "
        f"{status.worker_revision}, which is {status.commits_behind} {plural} "
        f"behind {status.main_ref} ({status.main_revision}). {running} "
        "Behavioral changes merged to main since this process started are "
        "not in effect until the worker is restarted."
    )


def _base_ref_lines(status: BaseRefLiveStatus) -> list[str]:
    if status.error:
        return [
            "base_ref_stale: could-not-determine",
            "base_ref_wrong: could-not-determine",
            f"base_ref_error: {status.error}",
        ]

    state = status.state
    lines = [
        f"base_ref_stale: {str(status.stale).lower()}",
        f"base_ref_wrong: {str(status.wrong).lower()}",
        f"base_ref_state: {state}",
    ]
    if state == "wrong":
        lines.extend(["", _base_ref_wrong_detail(status)])
    elif state == "stale":
        lines.extend(["", _base_ref_stale_detail(status)])
    elif state == "not_checked":
        lines.extend(["", _base_ref_not_checked_detail(status)])
    return lines


def _base_ref_stale_detail(status: BaseRefLiveStatus) -> str:
    """The operator-facing stale-base-ref sentence, naming both revisions.

    This is the distinct, operator-visible signal the dispatcher's own
    per-bead notes are deliberately not: it fires once here regardless of
    how many pending beads are blocked behind the same stale ref, and names
    the one-command remedy directly so the ten-hour-silent-failure shape
    (2026-08-26) cannot recur.
    """
    plural = "commit" if status.commits_behind == 1 else "commits"
    return (
        f"STALE BASE REF: {status.base_ref} local={status.local_rev} is "
        f"{status.commits_behind} {plural} behind "
        f"{status.upstream_ref}={status.upstream_rev}. The dispatcher is "
        "refusing to clone from it and will keep refusing every cycle until "
        "this is fixed -- every pending bead behind it is affected, not "
        "just one. This is a one-command remedy, not a per-bead fault: "
        f"fetch and fast-forward {status.base_ref} to {status.upstream_ref} "
        "in the dispatcher's own checkout."
    )


def _base_ref_wrong_detail(status: BaseRefLiveStatus) -> str:
    """The operator-facing wrong-base-ref sentence, naming the offending commit.

    Deliberately distinct from ``_base_ref_stale_detail``: a wrong base ref
    carries commits its tracking upstream never had, and no fast-forward can
    fix that -- the ref must be repointed. 2026-09-17: an operator repo's
    local ``main`` was pointed at a commit that existed only on an unmerged
    pull request's branch, for hours, while this exact status read
    ``base_ref_stale: false`` throughout, because staleness was the only bit
    ever checked. This is checked and reported independently of staleness (a
    base can be wrong while also behind -- diverged -- in which case it is
    reported here, not merely as stale, because a fast-forward would not fix
    it either).
    """
    plural = "commit" if status.local_only == 1 else "commits"
    return (
        f"WRONG BASE REF: {status.base_ref} local={status.local_rev} carries "
        f"{status.local_only} local-only {plural} -- {status.local_rev} is not "
        f"an ancestor-or-equal of {status.upstream_ref}={status.upstream_rev}. "
        "The dispatcher is refusing to clone from it and will keep refusing "
        "every cycle until this is fixed -- every pending bead behind it is "
        "affected, not just one. This is NOT a stale base: fast-forwarding "
        f"will not fix it. {status.base_ref} must be repointed (reset or "
        f"rebased) at a commit {status.upstream_ref} actually contains."
    )


def _base_ref_not_checked_detail(status: BaseRefLiveStatus) -> str:
    """The operator-facing 'we do not actually know' sentence.

    Distinct from a plain ``base_ref_stale: false`` / ``base_ref_wrong: false``
    reading as healthy: ``describe_base_ref_status`` only ever compares this
    checkout's local `main` against its own tracking ref of the *mirror* --
    never against GitHub directly -- and the last drain cycle did not confirm
    that tracking ref actually reflects GitHub's `main` right now. This is the
    2026-09-17 incident's own shape, generalized: that incident read healthy
    for hours because staleness and wrongness were the only two bits ever
    checked; this is the same false-healthy hole one layer up, where the
    tracking ref being compared against was never itself confirmed current.
    """
    locally = "stale" if status.stale else ("wrong" if status.wrong else "healthy")
    reason = status.check_reason or "the last drain cycle did not record confirming this"
    return (
        "BASE REF NOT CHECKED AGAINST GITHUB: "
        f"{status.base_ref} local={status.local_rev} reads {locally} against this "
        f"checkout's own tracking ref ({status.upstream_ref}={status.upstream_rev}), "
        "but the last drain cycle could not confirm that tracking ref reflects "
        f"GitHub's main: {reason}. Treat this as unknown, not healthy, until the "
        "next cycle confirms it."
    )


def _tunnel_keeper_stale(
    status: TunnelKeeperLiveStatus,
    *,
    now: datetime,
    stale_multiple: int = DEFAULT_TUNNEL_KEEPER_STALE_MULTIPLE,
) -> tuple[bool, timedelta | None]:
    if status.error or status.heartbeat_at is None:
        return True, None
    heartbeat_at = _parse_time(status.heartbeat_at)
    if heartbeat_at is None:
        return True, None
    age = max(timedelta(), now - heartbeat_at)
    threshold = timedelta(
        seconds=tunnel_keeper.DEFAULT_CHECK_INTERVAL_SECONDS * stale_multiple
    )
    return age > threshold or status.pid_running is False, age


def _tunnel_keeper_lines(status: TunnelKeeperLiveStatus, *, now: datetime) -> list[str]:
    if status.error:
        return [
            "tunnel_keeper_status: could-not-determine",
            f"tunnel_keeper_error: {status.error}",
        ]

    stale, age = _tunnel_keeper_stale(status, now=now)
    lines = [
        f"tunnel_keeper_heartbeat_age: {_format_duration(age)}",
        f"tunnel_keeper_pid: {status.pid}",
        f"tunnel_keeper_pid_running: {str(bool(status.pid_running)).lower()}",
        f"tunnel_keeper_stale: {str(stale).lower()}",
    ]
    for name, link in sorted(status.links.items()):
        lines.append(
            f"tunnel_keeper_link_{name}_reachable: "
            f"{str(bool(link.get('reachable'))).lower()}"
        )
    if stale:
        lines.append(_tunnel_keeper_stale_detail(status))
    else:
        unreachable = [
            name for name, link in status.links.items() if not link.get("reachable")
        ]
        if unreachable:
            lines.append(
                "TUNNEL LINK DOWN: " + ", ".join(sorted(unreachable)) +
                " -- the keeper is alive and should be restarting it; see its own alert."
            )
    return lines


def _tunnel_keeper_stale_detail(status: TunnelKeeperLiveStatus) -> str:
    """The operator-facing 'the keeper itself may be dead' sentence.

    This is the one failure the keeper cannot self-report -- if the supervising process died
    outright, nothing it would have posted gets posted. A stale or absent heartbeat is read
    here, and by `tunnel_keeper.check_heartbeat`'s own LaunchAgent watchdog, as exactly that.
    """
    return (
        "TUNNEL KEEPER STALE OR NOT RUNNING: no fresh heartbeat "
        f"(pid={status.pid}, pid_running={status.pid_running}). Local Network privacy denies a "
        "launchd-managed process the LAN access kubectl needs, so this must be restarted by a "
        f"human from an interactive session: {tunnel_keeper.RECOVERY_COMMAND}"
    )


def _format_waiting_age(status: WaitingQueueStatus) -> str:
    if not status.claimable_count:
        return "none"
    # `oldest_waiting_age` is None here iff at least one claimable task's
    # became-claimable moment could not be resolved from its event log (see
    # `_oldest_waiting_age`) -- the store itself answered fine, so this is
    # not the fully-unreachable-store "could-not-determine" from
    # `_waiting_queue_lines` above. `_format_duration(None)` renders that as
    # "unknown", which is what AC-5 asks for: reporting the minimum of the
    # *resolvable* tasks here would silently drop the unresolved task's own
    # starvation, which may be the largest in the queue.
    return _format_duration(status.oldest_waiting_age)


def _oldest_waiting_age(
    store: Any,
    tasks: list[dict[str, Any]],
    now: datetime,
) -> timedelta | None:
    """Age since the oldest claimable task BECAME CLAIMABLE, per its event log.

    Not `created_at` (filed time -- the bug #923 fixed: a task requeued from
    `review` minutes ago read as hours of starvation) and not `updated_at`
    (last-written time -- the bug #923 introduced: `updated_at` is a
    SQLAlchemy `onupdate` column, apps/substrate/src/models.py:37, bumped by
    ANY write, so a content-only PATCH of a still-pending task -- e.g.
    dispatch.bind_task_to_release running in bulk over filed work, or
    file_task._repoint_dependents rewriting a dependent's
    predecessor_bead_ids -- reset the reported age to zero and hid real
    starvation that had nothing to do with the write).

    The became-claimable moment for one task is the `created_at` of the LAST
    event in its log (`store.list_events`, oldest-first) whose `to_state` is
    `"pending"` AND whose `from_state` is NOT `"pending"` -- a genuine
    transition into pending. A task's own `created` event (`from_state` is
    `None`, `to_state` is `"pending"`, since `pending` is dev.task's only
    entry state) already satisfies this filter, so it serves as the fallback
    for a task that has never transitioned without needing a separate branch.

    THE `from_state` FILTER IS LOAD-BEARING, NOT DECORATIVE. A content-only
    PATCH on an already-pending task appends an `"updated"` event whose
    `to_state` is ALSO `"pending"` -- the state never changed. Filtering on
    `to_state` alone would treat that write as a fresh became-claimable
    moment and reintroduce exactly the erasure above, one layer down; adding
    `from_state != "pending"` excludes it while still catching every genuine
    transition into pending (including the requeue-from-review case #923
    fixed, and the initial `created` event).

    Costs one `store.list_events` read per task in `tasks` (bounded and
    linear -- 20 claimable tasks costs exactly 20 calls).

    Returns None if ANY task's moment could not be resolved from its event
    log -- never the minimum of only the resolvable ones. A queue with one
    unresolvable task and one filed five minutes ago must not report the
    five minutes: the unresolvable task's own starvation, possibly the
    larger of the two, would be silently dropped. `_format_waiting_age`
    renders None as "unknown" via `_format_duration`.
    """
    moments = [_became_claimable_at(store.list_events(task["id"])) for task in tasks]
    if not moments or any(moment is None for moment in moments):
        return None
    return max(timedelta(), _as_utc(now) - min(moments))


def _became_claimable_at(events: list[dict[str, Any]]) -> datetime | None:
    """The became-claimable moment from one task's event log, or None.

    `events` is oldest-first (the substrate route orders by `created_at`
    ascending), so the last qualifying entry is the most recent transition
    into pending -- see `_oldest_waiting_age` for why the filter is shaped
    this way.
    """
    qualifying = [
        event
        for event in events
        if event.get("to_state") == "pending" and event.get("from_state") != "pending"
    ]
    if not qualifying:
        return None
    return _parse_time(qualifying[-1].get("created_at"))


def _task_state(task: dict[str, Any]) -> str:
    return task.get("state") or "pending"


@dataclass(frozen=True)
class InFlightAgeStatus:
    """One in-flight workflow's measured age and what it means.

    Three states, not two: `wedged` (running_for was measured and exceeds the
    threshold), healthy (measured, at or under threshold -- both `wedged` and
    `could_not_determine` are false), and `could_not_determine` (no
    started_at/scheduled_at to compute running_for from at all). Collapsing
    the third state into `wedged=True` (the pre-2026-09 behavior) fails
    closed correctly per PRIN-015 but asserts a measurement -- "over the 45m
    threshold" -- that was never taken; collapsing it into `wedged=False`
    reopens the 2026-08-25 hole (OPS-15, #524) where "cannot compute" read as
    "not wedged" for 13 hours. Neither collapse is truthful, so this is a
    field, not a derivation from `running_for is None`.
    """

    workflow: Any
    running_for: timedelta | None
    wedged: bool
    could_not_determine: bool


def _in_flight_wedge_details(
    status: FactoryScheduleStatus,
    *,
    stuck_threshold_minutes: int,
    now: datetime,
) -> list[InFlightAgeStatus]:
    """Measured wedge/healthy/cannot-determine status for every in-flight workflow."""
    threshold = timedelta(minutes=stuck_threshold_minutes)
    details = []
    for workflow in status.in_flight:
        running_for = _workflow_running_for(workflow, now)
        if running_for is None:
            details.append(
                InFlightAgeStatus(
                    workflow=workflow,
                    running_for=None,
                    wedged=False,
                    could_not_determine=True,
                )
            )
        else:
            details.append(
                InFlightAgeStatus(
                    workflow=workflow,
                    running_for=running_for,
                    wedged=running_for > threshold,
                    could_not_determine=False,
                )
            )
    return details


def _in_flight_age_lines(
    status: FactoryScheduleStatus,
    *,
    stuck_threshold_minutes: int,
    now: datetime,
) -> list[str]:
    details = _in_flight_wedge_details(
        status,
        stuck_threshold_minutes=stuck_threshold_minutes,
        now=now,
    )
    wedged = [detail for detail in details if detail.wedged]
    undetermined = [detail for detail in details if detail.could_not_determine]
    count = len(wedged)
    undetermined_count = len(undetermined)

    lines = [
        f"in_flight_wedged: {str(bool(wedged)).lower()}",
        f"in_flight_wedged_workflows: {count}",
        f"in_flight_wedged_threshold_minutes: {stuck_threshold_minutes}",
        f"in_flight_cannot_determine_workflows: {undetermined_count}",
    ]
    if wedged:
        plural = "execution" if count == 1 else "executions"
        lines.append(
            f"WEDGED IN-FLIGHT WORKFLOW: {count} {plural} over "
            f"{stuck_threshold_minutes}m threshold."
        )
    if undetermined:
        plural = "execution" if undetermined_count == 1 else "executions"
        # Name the actual cause per execution: a timestamp that is absent and
        # one that is present but unparseable are different failures, and
        # claiming "has no started_at" about a value printed two lines below
        # would be the same asserted-non-measurement this line exists to end.
        causes = []
        for detail in undetermined:
            raw = _workflow_started_at(detail.workflow)
            # Falsy (None or "") is absent, matching _parse_time's own check;
            # only a truthy value that failed to parse is "present".
            if not raw:
                causes.append(
                    f"{detail.workflow.workflow_id} (no started_at or scheduled_at)"
                )
            else:
                causes.append(
                    f"{detail.workflow.workflow_id} "
                    f"(timestamp present but unparseable: {raw!r})"
                )
        lines.append(
            f"CANNOT DETERMINE AGE: {undetermined_count} in-flight {plural} -- "
            + "; ".join(causes)
            + ". Not counted as wedged -- that would assert a "
            "measurement never taken -- but not confirmed healthy either; "
            "fails closed until a measurable timestamp appears."
        )
    lines.extend(["", "In-flight workflow ages:"])

    for detail in details:
        workflow = detail.workflow
        started_at = _workflow_started_at(workflow)
        line = f"- workflow_id={workflow.workflow_id}"
        if workflow.run_id:
            line += f" run_id={workflow.run_id}"
        line += f" running_for={_format_duration(detail.running_for)}"
        if detail.could_not_determine:
            line += " wedged=cannot-determine"
        else:
            line += f" wedged={str(detail.wedged).lower()}"
        if started_at:
            line += f" age_from={started_at}"
        lines.append(line)
    return lines


def announce_wedged_dispatch_workflows(
    status: FactoryScheduleStatus,
    *,
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    now: datetime | None = None,
    announce: Callable[[str, int, int], bool] | None = None,
) -> list[str]:
    """Raise the declared, deduped alert for every in-flight workflow measured
    over the wedge threshold (2026-09-11: a lost `claim` step wedged the queue
    for 24h and nothing ever told anybody -- the only remedy was a human
    noticing and running `temporal workflow terminate` by hand).

    Detection is `_in_flight_wedge_details`'s own measured running_for -- this
    only decides whether to announce, reusing `announce_schedule_wedged`'s
    AlertPolicy for the "does not re-alert every tick" dedup instead of a
    second dedup mechanism. `announce` is injectable so tests can verify this
    calls out exactly once per wedged workflow without a real Discord/alert-
    state file; it defaults to the real declared alert.

    Returns the workflow_ids this called `announce` for (successfully posted
    or suppressed by dedup -- both are correct outcomes, not failures) for
    the caller's own logging.
    """
    now_value = now or datetime.now(timezone.utc)
    announce_fn = announce or failure_diagnosis.announce_schedule_wedged
    announced: list[str] = []
    for detail in _in_flight_wedge_details(
        status, stuck_threshold_minutes=stuck_threshold_minutes, now=now_value
    ):
        if not detail.wedged:
            continue
        running_for_minutes = int((detail.running_for or timedelta()).total_seconds() // 60)
        announce_fn(detail.workflow.workflow_id, running_for_minutes, stuck_threshold_minutes)
        announced.append(detail.workflow.workflow_id)
    return announced


def _time_since_last_success(
    status: FactoryScheduleStatus,
    *,
    now: datetime,
) -> timedelta | None:
    """How long since the schedule last completed a drain, or None if unknown.

    `last_success_at` only covers the recent-actions window Temporal retains.
    When nothing in that window succeeded, the earliest `scheduled_at` this
    check can see — across recorded recent firings and still-running
    executions — stands in as a lower bound: "at least this long since
    anything finished." That understates the true gap when history has
    aged out, but it never overstates it, so it cannot manufacture a false
    staleness fault.
    """
    if status.last_success_at:
        parsed = _parse_time(status.last_success_at)
        if parsed is not None:
            return max(timedelta(), _as_utc(now) - parsed)

    candidates = [_parse_time(outcome.scheduled_at) for outcome in status.recent]
    candidates.extend(_parse_time(workflow.scheduled_at) for workflow in status.in_flight)
    known = [candidate for candidate in candidates if candidate is not None]
    if not known:
        return None
    return max(timedelta(), _as_utc(now) - min(known))


def _drains_stale(
    status: FactoryScheduleStatus,
    *,
    interval_seconds: int,
    stale_drain_multiple: int,
    now: datetime,
) -> tuple[bool, timedelta | None]:
    since = _time_since_last_success(status, now=now)
    threshold = timedelta(seconds=interval_seconds * stale_drain_multiple)
    stale = since is not None and since > threshold
    return stale, since


def _drain_staleness_lines(
    status: FactoryScheduleStatus,
    *,
    interval_seconds: int,
    stale_drain_multiple: int,
    now: datetime,
) -> list[str]:
    stale, since = _drains_stale(
        status,
        interval_seconds=interval_seconds,
        stale_drain_multiple=stale_drain_multiple,
        now=now,
    )
    threshold_seconds = interval_seconds * stale_drain_multiple
    lines = [
        f"drains_stale: {str(stale).lower()}",
        f"drains_stale_since: {_format_duration(since)}",
        (
            f"drains_stale_threshold: {stale_drain_multiple}x interval "
            f"({_format_duration(timedelta(seconds=threshold_seconds))})"
        ),
    ]
    if stale:
        lines.append(
            "SCHEDULE STALE: no drain has completed in "
            f"{_format_duration(since)}, more than {stale_drain_multiple}x "
            "this schedule's own interval. A quiet board here can mean the "
            "schedule is firing into a Temporal the worker cannot reach, not "
            "that there is no work."
        )
    return lines


def factory_health_notifications(
    status: FactoryScheduleStatus,
    *,
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    interval_seconds: int,
    stale_drain_multiple: int = DEFAULT_STALE_DRAIN_MULTIPLE,
    base_ref_status: BaseRefLiveStatus | None = None,
    tunnel_keeper_status: TunnelKeeperLiveStatus | None = None,
    now: datetime | None = None,
) -> list[cluster_health.Notification]:
    """Faults worth paging on, in cluster_health's Notification shape.

    Reuses cluster_health.py's Notification/build_payload/notify/
    poster_from_env rather than a second webhook implementation, so a
    schedule firing into an unreachable Temporal alerts through the same path
    as every other platform fault.
    """
    now_value = now or datetime.now(timezone.utc)
    notifications: list[cluster_health.Notification] = []

    for detail in _in_flight_wedge_details(
        status,
        stuck_threshold_minutes=stuck_threshold_minutes,
        now=now_value,
    ):
        if not detail.wedged:
            continue
        notifications.append(
            cluster_health.Notification(
                severity="urgent",
                source="schedule-wedged",
                title=f"{status.schedule_id} has a wedged in-flight workflow",
                detail=(
                    f"workflow_id={detail.workflow.workflow_id} "
                    f"running_for={_format_duration(detail.running_for)} "
                    f"threshold_minutes={stuck_threshold_minutes}"
                ),
            )
        )

    stale, since = _drains_stale(
        status,
        interval_seconds=interval_seconds,
        stale_drain_multiple=stale_drain_multiple,
        now=now_value,
    )
    if stale:
        notifications.append(
            cluster_health.Notification(
                severity="urgent",
                source="schedule-stale",
                title=f"{status.schedule_id} has produced no successful drain recently",
                detail=(
                    f"drains_stale_since={_format_duration(since)} "
                    f"threshold={stale_drain_multiple}x interval "
                    f"({interval_seconds}s)"
                ),
            )
        )

    if base_ref_status is not None and base_ref_status.wrong:
        # Checked before (not alongside) `.stale`: a wrong ref can also be
        # behind (diverged), and that must page as WRONG -- repoint, not
        # fetch -- not merely as stale, since a fast-forward would not fix it.
        notifications.append(
            cluster_health.Notification(
                severity="urgent",
                source="base-ref-wrong",
                title=(
                    f"{status.schedule_id} base ref carries commits its tracking "
                    "remote does not have"
                ),
                detail=(
                    f"{base_ref_status.base_ref} local={base_ref_status.local_rev} "
                    f"has {base_ref_status.local_only} local-only commit(s), not an "
                    f"ancestor-or-equal of {base_ref_status.upstream_ref}="
                    f"{base_ref_status.upstream_rev}"
                ),
            )
        )
    elif base_ref_status is not None and base_ref_status.stale:
        notifications.append(
            cluster_health.Notification(
                severity="urgent",
                source="base-ref-stale",
                title=f"{status.schedule_id} base ref is behind its tracking remote",
                detail=(
                    f"{base_ref_status.base_ref} local={base_ref_status.local_rev} is "
                    f"{base_ref_status.commits_behind} commits behind "
                    f"{base_ref_status.upstream_ref}={base_ref_status.upstream_rev}"
                ),
            )
        )

    if tunnel_keeper_status is not None:
        stale, _age = _tunnel_keeper_stale(tunnel_keeper_status, now=now_value)
        if stale:
            notifications.append(
                cluster_health.Notification(
                    severity="urgent",
                    source="tunnel-keeper-stale",
                    title="factory tunnel keeper is stale or not running",
                    detail=(
                        f"pid={tunnel_keeper_status.pid} "
                        f"pid_running={tunnel_keeper_status.pid_running} "
                        f"heartbeat_at={tunnel_keeper_status.heartbeat_at} "
                        f"recovery={tunnel_keeper.RECOVERY_COMMAND}"
                    ),
                )
            )

    return notifications


def _workflow_running_for(workflow: Any, now: datetime) -> timedelta | None:
    started_at = _parse_time(_workflow_started_at(workflow))
    if started_at is None:
        return None
    return max(timedelta(), _as_utc(now) - started_at)


def _workflow_started_at(workflow: Any) -> Any:
    return getattr(workflow, "started_at", None) or getattr(workflow, "scheduled_at", None)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_duration(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    total_seconds = max(0, int(value.total_seconds()))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
