"""Announce a dev.task entering `failed`, and attach a mechanical diagnosis.

Before this, a bead that exhausted its retries (or was judged already
satisfied) wrote its failure to bead notes and stopped — nothing told
anyone, and nothing assembled what a human then had to work out by hand:
which declared verification command failed and what it printed, which
failure class the dispatcher stamped, what revision the worker ran against
versus local `main`, and whether a patch of the worker's own diff survived
the run. Every one of those was already recorded somewhere; this module is
the one place that gathers them and the one place that raises the alert.

Two things this deliberately does NOT do, per the task that added it: it
never requeues, retries, supersedes, or dispatches anything, and it never
decides a bead's disposition. Both stay the operator commands
`dispatch.py` already documents (`--bind-pr`, `--requeue`, or leave it).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import worker_revision

logger = logging.getLogger(__name__)

# Source reuse of the mcp-hub alert inventory/policy dataclasses, not a
# network call to the deployed mcp-hub service: the dispatcher already runs
# as a host-local process against its own working copy of this monorepo
# (see dispatch.Config.repo_root). Mirrors the sys.path pattern doctrine.py
# already uses for scripts/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)

from tools import notify  # noqa: E402

DEV_TASK_FAILED_ALERT_ID = "factory_dispatcher.dev_task_failed"
ENVIRONMENTAL_FAULT_BREAKER_LATCHED_ALERT_ID = (
    "factory_dispatcher.environmental_fault_breaker_latched"
)
CAPACITY_PAUSE_ALERT_ID = "factory_dispatcher.capacity_pause"
CAPACITY_RESUME_ALERT_ID = "factory_dispatcher.capacity_resume"
BASE_REF_NEEDS_PERSON_ALERT_ID = "factory_dispatcher.base_ref_needs_person"
CONCURRENT_CLONE_DEFER_WEDGED_ALERT_ID = "factory_dispatcher.concurrent_clone_defer_wedged"
SCHEDULE_WEDGED_ALERT_ID = "factory_dispatcher.dispatch_schedule_wedged"
WORKER_REVISION_DRIFT_ESCALATED_ALERT_ID = "factory_dispatcher.worker_revision_drift_escalated"
WORKER_REVISION_DRIFT_ESCALATED_RESOLVED_ALERT_ID = (
    "factory_dispatcher.worker_revision_drift_escalated_resolved"
)
WORKER_REVISION_DRIFT_UNKNOWN_WHILE_PAUSED_ALERT_ID = (
    "factory_dispatcher.worker_revision_drift_unknown_while_paused"
)
QUEUE_NOTHING_SELECTABLE_ALERT_ID = "factory_dispatcher.queue_nothing_selectable"  # emitted by B6
EXPEDITE_PROVENANCE_REJECTED_ALERT_ID = (
    "factory_dispatcher.expedite_provenance_rejected"  # emitted by B18
)
RELEASE_HEALTH_DRIFTING_ALERT_ID = "factory_dispatcher.release_health_drifting"  # emitted by B13
RELEASE_HEALTH_BREACHED_ALERT_ID = "factory_dispatcher.release_health_breached"  # emitted by B13
RELEASE_HEALTH_RECOVERED_ALERT_ID = "factory_dispatcher.release_health_recovered"  # emitted by B13
RELEASE_HEALTH_UNMEASURED_ALERT_ID = "factory_dispatcher.release_health_unmeasured"  # emitted by B13
RELEASE_HEALTH_UNMEASURED_PERSISTENT_ALERT_ID = (
    "factory_dispatcher.release_health_unmeasured_persistent"  # emitted by B13e
)

#: Must match `activities.worker_revision_drift.OBSERVATION_REF` -- duplicated as a literal
#: rather than imported, since that module imports this one (`worker_checkout_drift_response`
#: -> `activities.worker_revision_drift` -> `failure_diagnosis`), and this is the one standing
#: worker-revision-drift observation a single-tenant host ever has, so the two are never asked
#: to agree on anything but this fixed string.
WORKER_REVISION_DRIFT_OBSERVATION_REF = "obs.worker-revision-drift"

DIAGNOSIS_NOTE_HEADER = "Failure diagnosis:"

#: Declared re-alert interval for every alert this module raises (Amendment
#: 30 dedup fix): an unchanged fingerprint stays suppressed until this many
#: hours have passed since the last post, then re-alerts even though nothing
#: about the subject's content changed. Without this, the #593
#: delivered-vs-seen shape recurs: a bead (or the queue) wedged the same way
#: weeks apart inherits a suppression recorded once and never re-announces.
#: One shared constant, not per-alert-id, because the failure mode it guards
#: against — silent inherited suppression — is identical across all three.
ALERT_REALERT_INTERVAL_HOURS = 24.0

#: Host-local execution state, never bead content (Pillar 10) — same
#: reasoning as dispatch._spend_ledger_path and worker_revision's own state
#: file: this records what THIS host has already alerted about, not
#: anything the platform's durable state needs to agree on.
ALERT_STATE_PATH_ENV = "FACTORY_ALERT_STATE_PATH"


def _alert_state_path() -> Path:
    return Path(
        os.environ.get(
            ALERT_STATE_PATH_ENV,
            str(Path.home() / ".factory-dispatcher" / "alert-state.json"),
        )
    )


def _parse_alert_state_doc(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _read_alert_state_doc(path: Path) -> dict[str, Any]:
    # 2026-09-16 (OPS-99 follow-up): a *shared* lock here, so a read can never observe a torn
    # write from `_record_dev_task_alert_posted` below mid-truncate-and-rewrite -- it blocks
    # only for the instant that exclusive writer holds the lock, which is a single small JSON
    # file. Missing entirely (`OSError`, e.g. no alert has ever posted yet) is not an error.
    try:
        handle = path.open("r")
    except OSError:
        return {}
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _parse_alert_state_doc(handle.read())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def _load_dev_task_alert_state(kind: str) -> dict[str, Any] | None:
    doc = _read_alert_state_doc(_alert_state_path())
    entry = doc.get(kind)
    return {"content": entry} if entry else None


async def _record_dev_task_alert_posted(
    kind: str,
    fingerprint: str,
    _existing: dict[str, Any] | None,
    _members: list[str] | None,
) -> None:
    """Read-modify-write the shared alert-state file under an exclusive file lock.

    2026-09-16 (OPS-99 follow-up): before `worker.ACTIVITY_EXECUTOR_CONCURRENCY` moved past 1,
    only one dev.task-adjacent activity could ever be mid-flight at a time, so this
    read-then-write could never interleave with another writer of the same file. At
    concurrency 12, `record_failure` (dispatch, via `announce_failed_task`) and
    `probe_capacity_pause_resume` (a 15-minute reconciler, via `announce_capacity_resume`) --
    previously mutually exclusive by the same accident of arithmetic the checkout-advance guard
    relied on -- can now both be mid-read-modify-write on this file at once. Without a lock,
    whichever writes last wins and silently discards the other's just-recorded fingerprint,
    resetting that alert's dedup state -- the delivered-vs-seen shape OPS-69 already fixed once
    for a different alert. `fcntl.flock` is the same cross-process idiom
    `activities.dispatch_steps._acquire_dispatch_run_lock` already uses for a
    two-writers-one-resource problem; this one blocks (`LOCK_EX` with no `LOCK_NB`) rather than
    refusing, since serializing a few dozen bytes of JSON costs microseconds and losing an
    update does not get a second chance.
    """
    path = _alert_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            doc = _parse_alert_state_doc(handle.read())
            doc[kind] = {
                "fingerprint": fingerprint,
                "last_posted_at": datetime.now(timezone.utc).isoformat(),
            }
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(doc))
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def clear_stale_dev_task_alert_kinds(
    prefix: str, active_kinds: Iterable[str], *, observed_kinds: Iterable[str]
) -> None:
    """Forget stored state for a kind under `prefix` only if this run observed its subject
    and found it no longer active.

    Generalizes `cluster_health._clear_resolved_state`'s shape so a second call site
    (`workflow_run_health.py`) can share this file's state store safely: it only ever
    deletes keys under the caller's own declared prefix, so a bug in one checker's
    clearing can never touch another alert family's entry. Without this, a condition
    that resolves and later recurs with byte-identical content (e.g. the same failing
    workflow, red again for the same reason) would inherit a suppression window
    recorded for the earlier, now-resolved episode instead of re-alerting immediately.

    `observed_kinds` is required, not defaulted: a kind absent from it was not looked at
    this run at all (out of a bounded lookback, a mid-run subject, etc.) and must be left
    untouched -- "not currently active" is only "recovered" for a kind this run actually
    observed. A caller whose every run already observes its subject's entire universe
    (e.g. a full cluster snapshot) simply passes the same set for both arguments; a caller
    with a partial, windowed view (e.g. `workflow_run_health.py`) must not, which is the
    defect this parameter exists to make impossible to skip by accident.
    """
    active = set(active_kinds)
    observed = set(observed_kinds)
    path = _alert_state_path()
    doc = _read_alert_state_doc(path)
    tracked = {kind for kind in doc if kind.startswith(prefix)}
    stale = (tracked & observed) - active
    if not stale:
        return
    for kind in stale:
        del doc[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def dev_task_alert_policy(post: notify.PostAlert | None = None) -> "notify.AlertPolicy":
    """The dispatcher's own alert policy: same inventory, dispatcher-local state.

    Distinct from notify.finance_alert_policy() on purpose — that one's
    state lives in finance-namespaced beads via mcp-hub's substrate client,
    which is the wrong vertical for a dev.task fact. AlertPolicy is already
    parameterized so a second call site can supply its own state backend
    without forking the suppression/quiet-hours logic in notify.py.
    """
    return notify.AlertPolicy(
        load_alert_state=_load_dev_task_alert_state,
        record_alert_posted=_record_dev_task_alert_posted,
        post=post or notify.post_discord,
    )


@dataclass(frozen=True)
class FailureDiagnosis:
    """Everything a human or a retrying worker needs to skip re-deriving this."""

    bead_id: str
    failure_class: str
    worker_revision: str | None
    main_revision: str | None
    commits_behind: int | None
    revision_note: str
    patch_path: str | None

    def render(self) -> str:
        parts = [f"Failure class: {self.failure_class}.", self.revision_note]
        if self.patch_path:
            parts.append(f"Worker patch preserved at {self.patch_path}.")
        else:
            parts.append("No worker patch preserved for this failure.")
        return " ".join(parts)


def _revision_drift_note(status: "worker_revision.WorkerRevisionStatus") -> str:
    if status.worker_revision is None or status.main_revision is None:
        return f"Worker/main revision comparison unavailable: {status.error or 'no data recorded'}."
    worker_short = status.worker_revision[:8]
    main_short = status.main_revision[:8]
    if status.worker_revision == status.main_revision:
        return f"Worker ran at {worker_short}, matching {status.main_ref} ({main_short})."
    if status.is_ancestor and status.commits_behind:
        return (
            f"Worker ran at {worker_short}, {status.commits_behind} commit(s) "
            f"behind {status.main_ref} ({main_short}) — a predecessor's change "
            "landed on main after this worker's checkout was made."
        )
    if status.is_ancestor:
        return (
            f"Worker ran at {worker_short}, an ancestor of {status.main_ref} "
            f"({main_short}) with no commits between them."
        )
    return (
        f"Worker ran at {worker_short}, which is NOT an ancestor of "
        f"{status.main_ref} ({main_short}) — the checkout had diverged from main."
    )


def build_failure_diagnosis(
    bead_id: str,
    failure_class: str,
    *,
    patch_path: str | None = None,
    revision_status: "worker_revision.WorkerRevisionStatus | None" = None,
) -> FailureDiagnosis:
    """Gather the diagnosis for one bead entering `failed`.

    ``revision_status`` defaults to a live read of this host's recorded
    worker-revision-vs-main comparison; tests inject a fixed
    ``WorkerRevisionStatus`` instead so this needs no git repo or filesystem
    state to exercise the revision-gap wording.
    """
    status = (
        revision_status
        if revision_status is not None
        else worker_revision.describe_worker_revision_drift()
    )
    return FailureDiagnosis(
        bead_id=bead_id,
        failure_class=failure_class,
        worker_revision=status.worker_revision,
        main_revision=status.main_revision,
        commits_behind=status.commits_behind,
        revision_note=_revision_drift_note(status),
        patch_path=patch_path,
    )


def format_failure_diagnosis(diagnosis: FailureDiagnosis) -> str:
    return f"{DIAGNOSIS_NOTE_HEADER} {diagnosis.render()}"


async def _announce_failed_task_async(
    bead_id: str,
    title: str,
    failure_class: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"dev.task {bead_id} entered failed: {title!r}. "
        f"Failure class: {failure_class}."
    )
    fingerprint = notify.alert_content_fingerprint(f"{bead_id}:{failure_class}")
    return await notify.send_alert(
        policy,
        DEV_TASK_FAILED_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "bead_id": bead_id,
            "title": title,
            "failure_class": failure_class,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_failed_task(
    task: dict[str, Any],
    failure_class: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a bead that just entered `failed`.

    Best-effort by design, matching every other alert path in this app
    (cluster_health.notify, worker.TemporalTunnelAlerter._post_alert):
    alerting must never crash or fail a dispatch run. A run whose bead
    transition and diagnosis note already landed must not be turned into a
    dispatch failure by a bad webhook or a corrupt local alert-state file.
    """
    bead_id = str(task.get("id") or "")
    title = str((task.get("content") or {}).get("title") or "(untitled)")
    try:
        return asyncio.run(
            _announce_failed_task_async(
                bead_id, title, failure_class, policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce failed dev.task %s; the bead's own failed "
            "state and diagnosis note are unaffected",
            bead_id,
        )
        return False


async def _announce_environmental_fault_breaker_latched_async(
    bead_id: str,
    signature: str,
    streak: int,
    limit: int,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"dev.task {bead_id} latched the environmental-fault breaker: the same "
        f"fault (signature {signature}) recurred {streak} consecutive time(s), "
        f"past the declared bound of {limit}. This is a stop, not a verdict on "
        "the work — pick_task will not auto-select this bead again until the "
        "fault changes or an operator forces a run."
    )
    fingerprint = notify.alert_content_fingerprint(f"{bead_id}:{signature}")
    return await notify.send_alert(
        policy,
        ENVIRONMENTAL_FAULT_BREAKER_LATCHED_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "bead_id": bead_id,
            "signature": signature,
            "streak": streak,
            "limit": limit,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_environmental_fault_breaker_latched(
    bead_id: str,
    signature: str,
    streak: int,
    limit: int,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert the moment the bound-3 breaker latches a bead.

    Fires exactly once per latching, from
    ``dispatch.record_environment_failure`` at the point ``bound_reached``
    first becomes true — not from ``pick_task``'s skip branch, which
    re-evaluates on every scan and would otherwise need its own suppression
    logic for something this module's dedup already handles.

    Best-effort, matching ``announce_failed_task``: alerting must never crash
    a dispatch run over a bead whose pending-state write and fault note
    already landed.
    """
    try:
        return asyncio.run(
            _announce_environmental_fault_breaker_latched_async(
                bead_id, signature, streak, limit, policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce environmental-fault breaker latch for dev.task "
            "%s (signature %s); the bead's own pending state and fault note "
            "are unaffected",
            bead_id,
            signature,
        )
        return False


async def _announce_capacity_pause_async(
    bead_id: str,
    cause: str,
    schedule_status: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"Dispatch queue paused: capacity backpressure ({cause}), first "
        f"observed on dev.task {bead_id}. {schedule_status}"
    )
    fingerprint = notify.alert_content_fingerprint(cause)
    return await notify.send_alert(
        policy,
        CAPACITY_PAUSE_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "bead_id": bead_id,
            "cause": cause,
            "schedule_status": schedule_status,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_capacity_pause(
    bead_id: str,
    cause: str,
    schedule_status: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when capacity backpressure pauses the queue.

    The subject is the queue itself, not ``bead_id`` — ``CAPACITY_PAUSE_ALERT_ID``'s
    kind template is a constant, so this dedups per queue-condition (one
    slot total) rather than per bead, which is what lets an unrelated bead's
    later capacity failure with the SAME cause correctly suppress as "still
    the same paused condition" instead of alerting once per task that
    happened to be running when it hit.

    Best-effort, matching ``announce_failed_task``: alerting must never crash
    a dispatch run over a capacity failure whose pending-state write and
    backpressure note already landed.
    """
    try:
        return asyncio.run(
            _announce_capacity_pause_async(
                bead_id, cause, schedule_status, policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce capacity-pause alert (cause=%s); the "
            "schedule pause attempt and bead note are unaffected",
            cause,
        )
        return False


async def _announce_capacity_resume_async(
    note: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = f"Dispatch queue resumed: capacity backpressure has cleared. {note}"
    fingerprint = notify.alert_content_fingerprint(note)
    return await notify.send_alert(
        policy,
        CAPACITY_RESUME_ALERT_ID,
        fingerprint,
        content,
        template_values={"note": note},
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_capacity_resume(
    note: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when the capacity-resume responder resumes the queue (OPS-66).

    ``note`` is the exact text `capacity_pause_response.respond_to_capacity_pause` just wrote to
    the schedule -- the alert and the schedule's own note agree on what happened, and an operator
    reading the alert never has to separately go look up what evidence justified it.

    Best-effort, matching ``announce_capacity_pause``: alerting must never turn a successful
    resume (the schedule update itself already landed) into a reported failure.
    """
    try:
        return asyncio.run(
            _announce_capacity_resume_async(note, policy or dev_task_alert_policy())
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce capacity-resume alert; the schedule resume itself is unaffected"
        )
        return False


async def _announce_base_ref_needs_person_async(
    base_ref: str,
    local_rev: str,
    upstream_ref: str,
    upstream_rev: str,
    reason: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"{base_ref} local={local_rev} is behind its tracking ref "
        f"{upstream_ref}={upstream_rev} and could not be fast-forwarded "
        f"automatically: {reason}"
    )
    fingerprint = notify.alert_content_fingerprint(f"{local_rev}:{upstream_rev}:{reason}")
    return await notify.send_alert(
        policy,
        BASE_REF_NEEDS_PERSON_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "base_ref": base_ref,
            "local_rev": local_rev,
            "upstream_ref": upstream_ref,
            "upstream_rev": upstream_rev,
            "reason": reason,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_base_ref_needs_person(
    base_ref: str,
    local_rev: str,
    upstream_ref: str,
    upstream_rev: str,
    reason: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when `dispatch.ensure_base_ref_current` cannot safely
    fast-forward a behind base ref.

    Fires only for the two causes a person must actually resolve -- local-only commits
    that a fast-forward would lose, or a working tree that blocks the merge -- never for
    a concurrent dispatch clone in flight, which clears on its own the next cycle and is
    not a person's problem.

    Best-effort, matching announce_capacity_pause: alerting must never turn a base-ref
    refusal (whose bead-local note already lands via dispatch.record_stale_base_ref_fault)
    into a second, different failure.
    """
    try:
        return asyncio.run(
            _announce_base_ref_needs_person_async(
                base_ref, local_rev, upstream_ref, upstream_rev, reason,
                policy or dev_task_alert_policy(),
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce base-ref-needs-person alert (base_ref=%s); the "
            "refusal and its bead-local note are unaffected",
            base_ref,
        )
        return False


async def _announce_concurrent_clone_defer_wedged_async(
    blocking_workdirs: tuple[str, ...],
    streak: int,
    limit: int,
    policy: "notify.AlertPolicy",
) -> bool:
    names = ", ".join(blocking_workdirs) or "(none named)"
    content = (
        f"Base-ref fast-forward deferred for a concurrent clone {streak} consecutive "
        f"time(s), past the declared bound of {limit}. By its own docstring this "
        "clears on its own the next cycle -- it has not, which means the workdir(s) "
        f"it is deferring for are not actually live runs: {names}. This is a stop, "
        "not a person's fix by itself; whoever responds should confirm those "
        "workdirs' owners are really dead before removing anything by hand."
    )
    fingerprint = notify.alert_content_fingerprint(":".join(sorted(blocking_workdirs)))
    return await notify.send_alert(
        policy,
        CONCURRENT_CLONE_DEFER_WEDGED_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "blocking_workdirs": names,
            "streak": streak,
            "limit": limit,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_concurrent_clone_defer_wedged(
    blocking_workdirs: tuple[str, ...],
    streak: int,
    limit: int,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert once the SAME concurrent-clone deferral recurs past the
    declared bound (dispatch._record_concurrent_clone_defer).

    `dispatch.ensure_base_ref_current` treats one concurrent-clone deferral as nobody's
    problem: the next attempt finds the checkout free once that other clone finishes.
    OPS-60 (2026-09-04) is the case that assumption does not cover -- a workdir marker
    that could not distinguish "this run is live" from "the daemon that once ran it is
    live" made the condition permanent rather than transient, and 188 refusals produced
    no signal until a person happened to ask. This is that signal, raised the moment
    the bound-3 breaker latches, naming the workdirs it is deferring for so the
    response does not start from "which ones."

    Best-effort, matching announce_base_ref_needs_person: alerting must never turn a
    base-ref refusal into a second, different failure.
    """
    try:
        return asyncio.run(
            _announce_concurrent_clone_defer_wedged_async(
                blocking_workdirs, streak, limit, policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce concurrent-clone-defer-wedged alert (streak=%s); the "
            "refusal itself is unaffected",
            streak,
        )
        return False


async def _announce_schedule_wedged_async(
    workflow_id: str,
    running_for_minutes: int,
    threshold_minutes: int,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"Dispatch workflow {workflow_id} has been in flight for "
        f"{running_for_minutes}m, over the {threshold_minutes}m wedge threshold "
        f"schedule_status.py already measures. The workflow-level retry cannot "
        "help -- it is still waiting on the one activity that never returned."
    )
    fingerprint = notify.alert_content_fingerprint(workflow_id)
    return await notify.send_alert(
        policy,
        SCHEDULE_WEDGED_ALERT_ID,
        fingerprint,
        content,
        template_values={
            "workflow_id": workflow_id,
            "running_for_minutes": running_for_minutes,
            "threshold_minutes": threshold_minutes,
        },
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_schedule_wedged(
    workflow_id: str,
    running_for_minutes: int,
    threshold_minutes: int,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for an in-flight dispatch workflow stuck past
    schedule_status.py's own measured wedge threshold (2026-09-11 incident:
    a lost `claim` step wedged the queue for 24h with no alert reaching
    anybody -- the only remedy was a human noticing and running `temporal
    workflow terminate` by hand).

    Detection is schedule_status.py's own -- this only decides whether to
    announce. ``kind_template`` is a fixed string (one slot, mirroring
    ``announce_capacity_pause``'s "queue condition, not per-bead" reasoning):
    ``ScheduleOverlapPolicy.SKIP`` means at most one workflow is ever in
    flight for this schedule. The fingerprint is the wedged workflow_id, so
    the SAME wedged execution re-alerts only after ALERT_REALERT_INTERVAL_HOURS
    (not every schedule_status.py tick while it stays stuck), while a
    DIFFERENT workflow_id becoming wedged after the first cleared still
    alerts immediately -- the same AlertPolicy dedup every sibling alert in
    this module already reuses, not a second dedup mechanism.

    This never terminates the workflow itself. R2602-12/13's declared
    position is that a declared alert must never retry, requeue, supersede,
    or dispatch work; disposition (`temporal workflow terminate`) stays a
    human decision.

    Best-effort, matching announce_concurrent_clone_defer_wedged: alerting
    must never crash the status check that found this.
    """
    try:
        return asyncio.run(
            _announce_schedule_wedged_async(
                workflow_id, running_for_minutes, threshold_minutes,
                policy or dev_task_alert_policy(),
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce dispatch-schedule-wedged alert for workflow %s",
            workflow_id,
        )
        return False


async def _announce_worker_revision_drift_escalated_async(
    note: str,
    policy: "notify.AlertPolicy",
    *,
    revision: str | None,
    dispatch_paused: bool,
) -> bool:
    content = (
        f"Dispatch queue paused: worker-revision-drift deferral escalated. {note}"
        if dispatch_paused
        else (
            "Dispatch left running: worker-revision-drift deferral escalated; the blocker was "
            f"a non-dispatch schedule. {note}"
        )
    )
    # Fingerprint a STABLE identity (this alert's one observation ref + the revision that
    # drifted), never `note` itself: `note` embeds the per-tick consecutive-deferral counter
    # (see `activities.worker_revision_drift._escalate_persistent_deferral`), so fingerprinting
    # it produced a distinct fingerprint on every firing -- 5 distinct fingerprints across 5
    # ticks for what is the SAME open condition -- which defeats `re_alert_interval_hours`
    # entirely: every 15-minute tick of a run-in-flight queue posted a fresh ACTIONABLE alert
    # instead of the declared one-per-24h. The counter still reaches the reader, just via
    # `note`/`template_values` rather than the fingerprint, matching the convention every other
    # alert in this module already follows (`announce_failed_task`, `announce_environmental_
    # fault_breaker_latched`, `announce_base_ref_needs_person`: identity in the fingerprint,
    # the changing detail in the content/template only).
    fingerprint = notify.alert_content_fingerprint(
        f"{WORKER_REVISION_DRIFT_OBSERVATION_REF}:{revision}"
    )
    return await notify.send_alert(
        policy,
        WORKER_REVISION_DRIFT_ESCALATED_ALERT_ID,
        fingerprint,
        content,
        template_values={"note": note, "revision": revision},
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_worker_revision_drift_escalated(
    note: str,
    *,
    revision: str | None = None,
    dispatch_paused: bool = True,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when a worker-revision-drift deferral streak crosses the
    declared consecutive-deferral bound (f51e057c F1).

    2026-09-13: the same worker-revision-drift condition was deferred 15 consecutive times with
    nothing ever escalating. The fix that bounds the deferral streak
    (`activities.worker_revision_drift.MAX_CONSECUTIVE_DEFERRALS`) escalates past that bound, but
    escalating alone only acts; PRIN-008 also requires it announce itself, the same way
    `announce_capacity_pause` does for the sibling capacity-backpressure halt.

    2026-10-01 (dev.finding 639a20c5): escalating no longer always pauses dispatch through
    `schedule_runtime.pause_dispatch_schedule` -- `activities.worker_revision_drift.
    _escalate_persistent_deferral` only does that when the blocker was dispatch itself.
    ``dispatch_paused`` (default `True`, preserving this alert's original wording for any other
    caller) says which happened: the content states "Dispatch queue paused" only when it is;
    otherwise it states plainly that dispatch was left running and why, so an operator never
    reads a stale claim that the queue stopped when it did not.

    ``revision`` is the worker's recorded revision that is drifted -- pass the SAME value on
    every consecutive escalation of one open condition so the fingerprint (see below) stays
    stable across ticks; a fresh drift (a different revision) gets its own fingerprint and its
    own re-alert. The fingerprint does not depend on ``dispatch_paused``.

    Best-effort, matching every other alert in this module: alerting must never crash the
    activity over a pause (or non-pause) decision that has already landed.
    """
    try:
        return asyncio.run(
            _announce_worker_revision_drift_escalated_async(
                note,
                policy or dev_task_alert_policy(),
                revision=revision,
                dispatch_paused=dispatch_paused,
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce worker-revision-drift-escalated alert; the schedule pause "
            "itself is unaffected"
        )
        return False


async def _announce_worker_revision_drift_escalated_resolved_async(
    note: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = f"Dispatch queue resumed: worker-revision-drift escalation pause cleared. {note}"
    fingerprint = notify.alert_content_fingerprint(note)
    return await notify.send_alert(
        policy,
        WORKER_REVISION_DRIFT_ESCALATED_RESOLVED_ALERT_ID,
        fingerprint,
        content,
        template_values={"note": note},
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_worker_revision_drift_escalated_resolved(
    note: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when a worker-revision-drift escalation pause resolves on its
    own (f51e057c F-E), mirroring `announce_capacity_resume`
    (`activities.capacity_pause_response.default_announce_resume`) exactly: a halt that
    announces itself but never announces its own resolution leaves an operator who saw the
    escalation alert with no signal that dispatch is running again, short of checking by hand.

    ``note`` is `worker_checkout_drift_response.respond_to_cleared_drift`'s own resume note --
    the alert and the schedule's own note agree on what happened, same as every other
    resume/pause pair in this module.

    Best-effort, matching every other alert in this module: alerting must never turn a
    successful resume (the schedule update itself already landed) into a reported failure.
    """
    try:
        return asyncio.run(
            _announce_worker_revision_drift_escalated_resolved_async(
                note, policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce worker-revision-drift-escalated-resolved alert; the schedule "
            "resume itself is unaffected"
        )
        return False


async def _announce_worker_revision_drift_unknown_while_paused_async(
    note: str,
    error: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        "Dispatch queue remains paused: the worker-revision-drift condition that caused the "
        f"pause can no longer be evaluated ({error}). {note}"
    )
    # Fingerprint the PAUSE NOTE, not `status.error`: unlike the escalated alert (which has a
    # stable `revision` to key on), an unknown status carries no revision at all
    # (`WorkerRevisionStatus.could_not_determine` sets it to None), and `error` is not a
    # trustworthy stable key -- it can embed exception text (a git stderr snippet, a missing-file
    # path) that varies slightly tick to tick for what is operationally the same stuck condition.
    # The pause note itself IS stable across every tick of the same stuck pause: once
    # `_escalate_persistent_deferral` sets it, nothing rewrites it again while the condition stays
    # unknown (that rewrite only happens inside `if status.drifted:`, unreachable here) -- so this
    # reproduces the escalated alert's own fix (f51e057c F-D) rather than reintroducing its defect.
    fingerprint = notify.alert_content_fingerprint(
        f"{WORKER_REVISION_DRIFT_OBSERVATION_REF}:unknown-while-paused:{note}"
    )
    return await notify.send_alert(
        policy,
        WORKER_REVISION_DRIFT_UNKNOWN_WHILE_PAUSED_ALERT_ID,
        fingerprint,
        content,
        template_values={"note": note, "error": error},
        re_alert_interval_hours=ALERT_REALERT_INTERVAL_HOURS,
    )


def announce_worker_revision_drift_unknown_while_paused(
    note: str,
    *,
    error: str | None = None,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert when a worker-revision-drift escalation pause is still in force
    and the drift condition it exists to enforce has become `unknown` -- dev.finding 9a10f2aa.

    `report_worker_revision_drift_activity` resumes this mechanism's own pause in exactly two
    cases (a drifted status that resolves ADVANCED/RESTARTED, or the condition found CLEAR).
    `unknown` is a distinct third condition (`status.error` set, `drifted` False by construction)
    that reaches neither branch, and the original escalation alert's own 24h re-alert interval has
    already suppressed a repeat of itself -- so without this, the pause persists with no further
    signal of any kind for as long as the condition stays unresolved. This does NOT resume the
    pause and must never be called from a path that does: `unknown` must never be treated as
    though it were clean (PRIN-015 fail closed; `worker_revision.py`'s own `_condition_for`
    docstring, `:277-280`). The caller (`activities.worker_revision_drift`) is expected to have
    already confirmed the pause carries this mechanism's own note prefix
    (`worker_checkout_drift_response.is_our_pause_note`) before calling this -- an operator's
    manual pause, or the sibling capacity-backpressure pause, must never trigger this alert.

    Best-effort, matching every other alert in this module: alerting must never crash the
    activity over a pause (and its own escalation alert) that already landed.
    """
    try:
        return asyncio.run(
            _announce_worker_revision_drift_unknown_while_paused_async(
                note, error or "no detail recorded", policy or dev_task_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce worker-revision-drift-unknown-while-paused alert; the schedule "
            "pause itself is unaffected"
        )
        return False
