"""Record and report which git revision the running worker process loaded.

2026-08-22/23: a launchd worker started at 13:24 on 08-22, before #500 (the
consecutive-environmental-fault breaker) merged at 08:55 on 08-23. Python
loaded dispatch.py once, at process start; the merge changed nothing about
the process already running. Bead f9fc8ff9 then faulted eight consecutive
times and was never stopped, because the breaker code that would have
stopped it was not the code the running process had imported. Nothing about
the factory's own status output said so -- an unpaused schedule and a
registered poller looked identical whether the worker held last week's code
or this morning's.

Design: the revision is captured once, at worker startup, from the checkout
the running process's own module file lives in -- `REPO_ROOT` below is
derived from `__file__`, never from an environment variable a caller could
set to anything and never re-read after start. It is written to a small JSON
state file so a *separate* process (`schedule_status.py`, run whenever an
operator wants a status check) can read it back without touching the running
worker: no RPC into the worker, no restart, no interruption. Comparing the
recorded revision against local `main` deliberately mirrors
`dispatch.base_ref_status` -- it reads local git refs only and never fetches,
so a status check never touches the network or requires a live cluster.

Rejected: exposing the revision over a socket/HTTP endpoint inside the
worker process. That would work while the worker is healthy, but the whole
point here is to answer "is the running code current" even when the worker
is wedged or between polls; a file on disk answers that regardless of what
the process is doing at the moment someone asks. It also avoids opening a
new network-facing surface on a single-tenant factory host for a
read-mostly diagnostic.

Best-effort by design: a worker that cannot determine or record its own
revision (no git, no writable state directory) must still be able to start
and dispatch work -- the fix for one blind spot must not become a new single
point of failure. `record_worker_start` therefore never raises; a failure
here means later drift checks read "could not determine," not that the
worker refuses to run.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dispatch import DEFAULT_BASE_REF, GIT, run

logger = logging.getLogger(__name__)

#: The checkout this module itself was loaded from -- what "record_worker_start"
#: reports on. Not configurable: a value a caller could redirect would defeat
#: the point of proving what the running process actually imported.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Relocates *where the record is stored*, never what revision it reports.
STATE_DIR_ENV = "FACTORY_DISPATCHER_STATE_DIR"
STATE_FILE_NAME = "worker-revision.json"

#: Same trunk the rest of the dispatcher treats as canonical (dispatch.DEFAULT_BASE_REF).
DEFAULT_MAIN_REF = DEFAULT_BASE_REF

GitRunner = Callable[..., Any]


def default_state_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the worker's revision record lives, in this environment."""
    values = os.environ if environ is None else environ
    base = values.get(STATE_DIR_ENV)
    directory = Path(base) if base else Path.home() / ".factory-dispatcher"
    return directory / STATE_FILE_NAME


@dataclass(frozen=True)
class WorkerRevisionRecord:
    revision: str
    started_at: str  # ISO 8601 UTC
    #: The pid of the process that called `record_worker_start`. Lets a later
    #: reader (a wholly separate process) tell "the worker that recorded this
    #: is still running" from "whatever wrote this is long gone" -- see
    #: `describe_worker_revision_drift`. None only for records written before
    #: this field existed.
    pid: int | None = None


def capture_git_revision(repo_root: Path = REPO_ROOT, *, git_runner: GitRunner = run) -> str:
    """The HEAD commit of the checkout at `repo_root`, per git itself."""
    return git_runner([GIT, "rev-parse", "HEAD"], cwd=repo_root).stdout.strip()


def record_worker_start(
    *,
    repo_root: Path = REPO_ROOT,
    state_path: Path | None = None,
    now: datetime | None = None,
    git_runner: GitRunner = run,
    pid: int | None = None,
) -> WorkerRevisionRecord | None:
    """Capture and durably record the revision this process just loaded.

    Returns the record on success, or None if it could not be captured or
    written -- logged, never raised, so a worker that cannot record its own
    revision still starts.
    """
    try:
        record = WorkerRevisionRecord(
            revision=capture_git_revision(repo_root, git_runner=git_runner),
            started_at=(now or datetime.now(timezone.utc)).isoformat(),
            pid=pid if pid is not None else os.getpid(),
        )
        path = state_path if state_path is not None else default_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(
                {
                    "revision": record.revision,
                    "started_at": record.started_at,
                    "pid": record.pid,
                }
            ),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return record
    except Exception:  # noqa: BLE001 - recording must never block worker startup.
        logger.exception(
            "Could not record worker revision; drift checks will read no "
            "record until a worker start succeeds in recording one."
        )
        return None


def read_worker_revision_record(state_path: Path | None = None) -> WorkerRevisionRecord | None:
    """The most recently recorded worker start, or None if none is on file."""
    path = state_path if state_path is not None else default_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return WorkerRevisionRecord(
            revision=payload["revision"],
            started_at=payload["started_at"],
            # .get, not []: a record written before pid tracking existed is
            # still readable, just permanently unattributable -- see
            # describe_worker_revision_drift.
            pid=payload.get("pid"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class WorkerRevisionStatus:
    """Whether the worker's recorded revision is reachable from local main.

    ``is_ancestor`` alone answers "is this checkout derived from main at
    all" -- the question ``ensure_checkout_is_on_main_ancestor`` needs, and
    it keeps that exact meaning. It does not answer "is this worker
    current": an ancestor that is not main's tip is the ordinary case of a
    worker that simply hasn't been restarted since the last merge, and that
    is drift too -- distance from main, not mere ancestry, is what
    ``drifted`` measures. ``commits_behind`` is only meaningful when
    ``is_ancestor`` is True; a revision that isn't on main at all has no
    distance-along-main to report.
    """

    worker_revision: str | None
    worker_started_at: str | None
    main_ref: str
    main_revision: str | None
    is_ancestor: bool | None
    commits_behind: int | None = None
    error: str = ""

    @property
    def drifted(self) -> bool:
        if self.is_ancestor is None:
            return False
        if not self.is_ancestor:
            return True
        return bool(self.commits_behind)

    @classmethod
    def could_not_determine(cls, main_ref: str, error: BaseException | str) -> "WorkerRevisionStatus":
        return cls(
            worker_revision=None,
            worker_started_at=None,
            main_ref=main_ref,
            main_revision=None,
            is_ancestor=None,
            commits_behind=None,
            error=str(error),
        )


def _default_pid_is_running(pid: int) -> bool:
    """True if `pid` names a process currently running on this host.

    A signal-0 `kill` neither sends a signal nor requires owning the
    process -- it only asks the kernel whether the pid exists. Local-only,
    no RPC into the process itself, matching every other check in this
    module.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


PidChecker = Callable[[int], bool]


def _unattributable_record_error(record: WorkerRevisionRecord) -> str:
    if record.pid is None:
        return (
            f"worker revision record for {record.revision} was written before "
            "process-identity tracking existed and cannot be attributed to a "
            "running worker -- restart the worker to record a fresh one."
        )
    return (
        f"worker revision record names {record.revision}, recorded by pid "
        f"{record.pid}, but that process is not currently running. This "
        "record cannot be attributed to the running worker: something other "
        "than a live worker start wrote it -- a test run, a manual "
        "invocation, or a review checkout are the known ways this happens "
        "(2026-08-27). Restart the worker to record a fresh, live one."
    )


def describe_worker_revision_drift(
    *,
    state_path: Path | None = None,
    repo_root: Path = REPO_ROOT,
    main_ref: str = DEFAULT_MAIN_REF,
    read_record: Callable[[Path | None], WorkerRevisionRecord | None] = read_worker_revision_record,
    git_runner: GitRunner = run,
    pid_is_running: PidChecker = _default_pid_is_running,
) -> WorkerRevisionStatus:
    """Compare the worker's recorded revision against local main -- no network.

    Reads only local git refs, matching `dispatch.base_ref_status`: an
    operator is expected to fetch main; this must answer "is the running
    worker current" from what is already on disk, not by reaching out.

    2026-08-27: this record has no writer scoped to it -- anything that calls
    `record_worker_start()` from this host overwrites the same default path,
    including a pytest run of `test_worker_config_guard.py` that never sets
    `FACTORY_DISPATCHER_STATE_DIR`. Twice in one day that clobbered a real
    launchd worker's record: once with a revision that happened to parse as
    main's own tip (reported `commits_behind=0`, i.e. falsely "current" for a
    worker 17 commits behind), once with a revision only a PR branch ever
    held (reported "could not determine" for the right question asked of the
    wrong reason). Both times the process that wrote the record had already
    exited by the time this ran. Checking that the recorded pid is still
    alive catches both: a live worker's pid stays alive for as long as
    "current" is a question worth asking; a test run's does not.
    """
    record = read_record(state_path)
    if record is None:
        return WorkerRevisionStatus.could_not_determine(
            main_ref,
            "no worker revision record found -- has a worker been started "
            "since revision tracking was added?",
        )
    if record.pid is None or not pid_is_running(record.pid):
        return WorkerRevisionStatus.could_not_determine(
            main_ref,
            _unattributable_record_error(record),
        )
    try:
        main_revision = git_runner([GIT, "rev-parse", main_ref], cwd=repo_root).stdout.strip()
        proc = git_runner(
            [GIT, "merge-base", "--is-ancestor", record.revision, main_ref],
            cwd=repo_root,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - status must say unknown, not "current".
        return WorkerRevisionStatus.could_not_determine(main_ref, exc)

    if proc.returncode not in (0, 1):
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()[:500]
        return WorkerRevisionStatus.could_not_determine(
            main_ref,
            f"could not test whether {record.revision} is on {main_ref}: {detail}",
        )

    is_ancestor = proc.returncode == 0
    commits_behind: int | None = None
    if is_ancestor:
        commits_behind = 0
        if record.revision != main_revision:
            try:
                count_proc = git_runner(
                    [GIT, "rev-list", "--count", f"{record.revision}..{main_ref}"],
                    cwd=repo_root,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001 - status must say unknown, not "current".
                return WorkerRevisionStatus.could_not_determine(main_ref, exc)
            if count_proc.returncode != 0:
                detail = (
                    getattr(count_proc, "stderr", "") or getattr(count_proc, "stdout", "") or ""
                ).strip()[:500]
                return WorkerRevisionStatus.could_not_determine(
                    main_ref,
                    f"could not count commits between {record.revision} and {main_ref}: {detail}",
                )
            try:
                commits_behind = int(count_proc.stdout.strip())
            except ValueError:
                return WorkerRevisionStatus.could_not_determine(
                    main_ref,
                    f"unexpected `git rev-list --count` output: {count_proc.stdout!r}",
                )

    return WorkerRevisionStatus(
        worker_revision=record.revision,
        worker_started_at=record.started_at,
        main_ref=main_ref,
        main_revision=main_revision,
        is_ancestor=is_ancestor,
        commits_behind=commits_behind,
    )


class WorkerCheckoutDriftError(RuntimeError):
    """The checkout this process loaded from is not derived from main.

    2026-08-25: the launchd worker restarted while the operator had PR #518's
    branch checked out in the shared working tree it runs from
    (`Active.nosync`) -- an ordinary lint fix, mid-edit. The worker loaded
    that branch's `dispatch.py` as the factory's own code, and nothing
    stopped it: `record_worker_start` is deliberately best-effort and never
    raises, so a worker on any branch starts, registers a Temporal poller,
    and looks exactly as healthy as one on main. OPS-11's drift report caught
    it after the fact, hours later.

    `ensure_checkout_is_on_main_ancestor` is the fix's read-only half: a hard
    gate at startup, unlike the best-effort record above, because a worker
    that cannot show its own code descends from main must not register as a
    poller in the first place. `worker_checkout.advance` is the write half --
    the only path meant to move what this check considers current.
    """


def ensure_checkout_is_on_main_ancestor(
    *,
    repo_root: Path | None = None,
    main_ref: str = DEFAULT_MAIN_REF,
    git_runner: GitRunner = run,
) -> str:
    """Refuse loudly, naming the branch, unless HEAD is an ancestor of main.

    ``repo_root`` defaults to the module-level ``REPO_ROOT`` looked up at
    call time (not bound at import), so a test can monkeypatch
    ``worker_revision.REPO_ROOT`` and have this function see it without
    threading the override through every caller -- the same shape the
    worker's config guards already use for ``dispatch.required_worker_executables``.
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    try:
        revision = capture_git_revision(root, git_runner=git_runner)
        branch = git_runner(
            [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root
        ).stdout.strip()
        proc = git_runner(
            [GIT, "merge-base", "--is-ancestor", revision, main_ref],
            cwd=root,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - an unverifiable checkout must not start.
        raise WorkerCheckoutDriftError(
            "factory-dispatcher worker refusing to start. Could not verify that "
            f"the checkout at {root} is derived from {main_ref}: {exc}. This is a "
            "checkout fault, not a connectivity fault -- restarting will not fix it."
        ) from exc

    if proc.returncode == 0:
        return revision
    if proc.returncode != 1:
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()[:500]
        raise WorkerCheckoutDriftError(
            "factory-dispatcher worker refusing to start. Could not test whether "
            f"{revision} is an ancestor of {main_ref} in {root}: {detail}. This is a "
            "checkout fault, not a connectivity fault -- restarting will not fix it."
        )

    where = f"branch {branch!r}" if branch and branch != "HEAD" else f"a detached checkout at {revision}"
    raise WorkerCheckoutDriftError(
        f"factory-dispatcher worker refusing to start. The checkout at {root} is on "
        f"{where}, which is not an ancestor of {main_ref}. The worker must load its "
        "code from a checkout the factory controls, advanced deliberately with "
        "`python worker_checkout.py advance` -- not from whatever branch an operator's "
        "shared working tree happens to have checked out. This is a checkout fault, "
        "not a connectivity fault -- restarting will not fix it."
    )


@dataclass(frozen=True)
class CheckoutFreshness:
    """Whether a checkout's revision is behind ``main_ref`` -- ancestry is a separate question.

    2026-08-29: PR #583 was cut from a checkout nine commits behind main
    (``76da5019`` while main was at ``87b45db``).
    ``ensure_checkout_is_on_main_ancestor`` passed -- ``76da5019`` IS an
    ancestor of main, exactly as designed; that guard was never asked whether
    the checkout was *current*, and nothing else asked either. The PR that
    shipped named no revision, so the release gate read a bare test count it
    could not date without computing ``git merge-base`` by hand.

    ``is_ancestor`` and ``error`` are kept alongside ``stale`` rather than
    folded into it so a caller can never read "not stale" off a checkout this
    function was not able to judge: a diverged checkout (``is_ancestor`` is
    False) is ``ensure_checkout_is_on_main_ancestor``'s refusal to make, not
    this one's, and an unresolvable comparison must say so rather than
    default to "fine."
    """

    revision: str
    main_ref: str
    main_revision: str | None
    is_ancestor: bool | None
    commits_behind: int | None = None
    error: str = ""

    @property
    def stale(self) -> bool:
        if self.error or not self.is_ancestor:
            return False
        return bool(self.commits_behind)


def check_checkout_freshness(
    *,
    repo_root: Path | None = None,
    main_ref: str = DEFAULT_MAIN_REF,
    git_runner: GitRunner = run,
) -> CheckoutFreshness:
    """How far the checkout at ``repo_root`` is behind ``main_ref`` -- local refs only.

    Same trust model as every other check in this module: no ``git fetch``,
    no network, an injectable git runner. Deliberately does not raise -- a
    caller that wants a hard gate uses ``ensure_checkout_is_current_with_main``
    below; this is the pure half, for callers (and tests) that just want the
    comparison.
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    try:
        revision = capture_git_revision(root, git_runner=git_runner)
        main_revision = git_runner([GIT, "rev-parse", main_ref], cwd=root).stdout.strip()
        ancestor_proc = git_runner(
            [GIT, "merge-base", "--is-ancestor", revision, main_ref],
            cwd=root,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - status must say unknown, not "fresh".
        return CheckoutFreshness(
            revision="",
            main_ref=main_ref,
            main_revision=None,
            is_ancestor=None,
            error=str(exc),
        )

    if ancestor_proc.returncode not in (0, 1):
        detail = (
            getattr(ancestor_proc, "stderr", "") or getattr(ancestor_proc, "stdout", "") or ""
        ).strip()[:500]
        return CheckoutFreshness(
            revision=revision,
            main_ref=main_ref,
            main_revision=main_revision,
            is_ancestor=None,
            error=f"could not test whether {revision} is an ancestor of {main_ref}: {detail}",
        )

    is_ancestor = ancestor_proc.returncode == 0
    if not is_ancestor:
        return CheckoutFreshness(
            revision=revision,
            main_ref=main_ref,
            main_revision=main_revision,
            is_ancestor=False,
        )

    if revision == main_revision:
        return CheckoutFreshness(
            revision=revision,
            main_ref=main_ref,
            main_revision=main_revision,
            is_ancestor=True,
            commits_behind=0,
        )

    count_proc = git_runner(
        [GIT, "rev-list", "--count", f"{revision}..{main_ref}"],
        cwd=root,
        check=False,
    )
    if count_proc.returncode != 0:
        detail = (
            getattr(count_proc, "stderr", "") or getattr(count_proc, "stdout", "") or ""
        ).strip()[:500]
        return CheckoutFreshness(
            revision=revision,
            main_ref=main_ref,
            main_revision=main_revision,
            is_ancestor=True,
            error=f"could not count commits between {revision} and {main_ref}: {detail}",
        )
    try:
        commits_behind = int(count_proc.stdout.strip())
    except ValueError:
        return CheckoutFreshness(
            revision=revision,
            main_ref=main_ref,
            main_revision=main_revision,
            is_ancestor=True,
            error=f"unexpected `git rev-list --count` output: {count_proc.stdout!r}",
        )

    return CheckoutFreshness(
        revision=revision,
        main_ref=main_ref,
        main_revision=main_revision,
        is_ancestor=True,
        commits_behind=commits_behind,
    )


class WorkerCheckoutStaleError(RuntimeError):
    """The checkout is a valid but outdated ancestor of main.

    Distinct from ``WorkerCheckoutDriftError``: that one refuses a checkout
    that has diverged from main entirely. This refuses one that IS derived
    from main but is not its tip -- exactly the shape
    ``ensure_checkout_is_on_main_ancestor`` is designed to let through (see
    ``test_ensure_checkout_passes_for_a_detached_head_at_an_old_ancestor_of_main``).
    Ancestry is not freshness; this is freshness, checked beside the ancestry
    guard rather than folded into it. See ``CheckoutFreshness`` for the
    incident this exists to catch.
    """


def ensure_checkout_is_current_with_main(
    *,
    repo_root: Path | None = None,
    main_ref: str = DEFAULT_MAIN_REF,
    git_runner: GitRunner = run,
) -> CheckoutFreshness:
    """Refuse when the checkout is a confirmed, valid, outdated ancestor of ``main_ref``.

    Silent about a diverged checkout (``is_ancestor`` False) or one this
    could not evaluate (``error`` set): those are
    ``ensure_checkout_is_on_main_ancestor``'s refusal to make, with its own
    message. Callers run both checks; this one must never be the check that
    reports a diverged tree as merely "not stale."
    """
    freshness = check_checkout_freshness(
        repo_root=repo_root, main_ref=main_ref, git_runner=git_runner
    )
    if freshness.stale:
        root_desc = str(repo_root) if repo_root is not None else str(REPO_ROOT)
        raise WorkerCheckoutStaleError(
            f"factory-dispatcher refusing to dispatch from {root_desc}. Its checkout is "
            f"at {freshness.revision}, {freshness.commits_behind} commit(s) behind "
            f"{main_ref} at {freshness.main_revision}. This is a checkout the factory "
            "controls -- advance it deliberately (`python worker_checkout.py advance`) "
            "before dispatching again; a stale-but-valid checkout will not fix itself "
            "on retry. This is a checkout fault, not a connectivity fault."
        )
    return freshness
