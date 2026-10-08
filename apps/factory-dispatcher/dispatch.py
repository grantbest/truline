#!/usr/bin/env python3
"""Factory dispatcher — claim one dev.task, run a worker on it, open a PR.

    python -m dispatch --once            # claim and run the oldest pending task
    python -m dispatch --once --dry-run  # show what would be claimed, change nothing
    python -m dispatch --task <bead-id>  # run one specific task
    python -m dispatch --report-stuck    # show stale doing tasks, write nothing
    python -m dispatch --traceability-report
                                      # count open dev.task refs/waivers/neither
    python -m dispatch --task <bead-id> --release-stranded "why"
                                      # release an unowned doing task
    python -m dispatch --reconcile-review --dry-run
                                      # show landed review tasks, write nothing
    python -m dispatch --report-held-prs
                                      # show held beads whose PR closed or merged, write nothing

The loop, and why each step is where it is:

    claim      pending -> doing, so the board shows the work is live
    isolate    clone into a temp dir whose .git has NO path back to this repo
    run        selected registry worker, budget-bounded
    contain    verify this working tree is byte-identical to before the run
    scope      verify the diff stayed inside scope.paths
    smoke      byte-compile changed Python before declared verification
    verify     run the task's declared verification in a clone-local venv
    propose    branch + push + PR;  doing -> review, pr_url stamped on the task
    notes      dev.note{kind:status} at every step, so a 20-minute run is legible

Isolation is a **clone, not a worktree.** During the F1 spike a worker escaped a
worktree and edited the real working tree while reporting success (see
docs/archive/2026-08-agent-harness-spike/FINDINGS.md #11): a worktree's ``.git`` is a file pointing
at the parent repository, and a tool with its own project resolution follows it
home. A clone has its own object store and an ``origin`` rewritten to the GitHub
URL, so there is no filesystem path back for anything to resolve. The
before/after fingerprint of the real tree stays as defence in depth.

This dispatcher **never merges.** Auto-merge is gated on G1 (registry digest
pinning + measured rollback), which is still open, so every run ends at a PR for
human review regardless of a task's ``autonomy`` field.

Claiming is a compare-and-set transition (PR #191), so two dispatchers racing
cannot both claim the same task — the loser is told which way it lost. What
still argues for running one is the absence of a reaper (FA-S6): a dispatcher
killed mid-run leaves its task in ``doing`` with no lease to expire, and
nothing reclaims it. ``--report-stuck`` makes those tasks visible, but reclaim
belongs to Amendment 24 Phase 4's Temporal heartbeat timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess

import containment
import sys
import textwrap
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import dev_task_contract
import doctrine
import git_control
import guards
import process_env
import queue_order
import retry_policy
import workflow_core
from beadstore import BeadStore
from substrate import default_store, use_store

CREATED_BY = "factory-dispatcher/claude"
# Not a live default: FACTORY_REPO / FACTORY_REMOTE are required configuration
# (config.REQUIRED_CONFIG) and Config.from_env() refuses rather than falling
# back to these. They exist only so Config() can be constructed directly
# (as most of this test suite does) without every caller naming a repo it
# does not care about — a placeholder any household can read as a fixture,
# not a real one (R2603-5).
_PLACEHOLDER_REPO = "example/repo"
_PLACEHOLDER_REMOTE = "git@github.com:example/repo.git"
DEFAULT_BASE_REF = "main"
# macOS ships no `timeout` binary (docs/archive/2026-08-agent-harness-spike/FINDINGS.md #3 lost a
# whole arm to that), so the budget is enforced by subprocess timeout instead.
DEFAULT_BUDGET_MINUTES = 30
DEFAULT_WORKER = "claude"
# Detection-only: this must stay above the default 30m worker budget so an
# ordinary full-budget run is not reported as stuck.
DEFAULT_STUCK_THRESHOLD_MINUTES = DEFAULT_BUDGET_MINUTES + 15
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 20 * 60
DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 20 * 60
# classify_verification_failure_against_base re-runs one already-failed
# command once, inside the "record_failure" Temporal activity, whose own
# start_to_close budget is workflow_core.DISPATCH_SHORT_STEP_TIMEOUT (10
# minutes). Bounded well under that, not at it, so save_failure_patch's own
# git calls, the worktree add/remove, and the note write around it still fit
# inside the activity's budget when the base run itself is slow -- a base
# run that would blow this budget degrades to INDETERMINATE instead of the
# whole activity being killed and the failure note never getting written.
#
# F3 (gate finding on PR #929): this is well under
# DEFAULT_VERIFICATION_TIMEOUT_SECONDS's 1200s, on purpose, not by oversight
# -- a declared command that legitimately takes longer than this to fail will
# ALWAYS classify INDETERMINATE here, never INTRODUCED/PRE_EXISTING. Left
# unraised: DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS's 1200s was tried for this
# exact constant before (see BASE_VERIFICATION_WORKTREE_TIMEOUT_SECONDS's
# comment below) and on its own already exceeds the whole 600s activity
# budget, killing record_failure with no heartbeat and losing the failure
# note entirely -- worse than the diagnostic it was meant to improve. Declared
# verification suites that legitimately run past roughly six minutes are out
# of reach for this diagnostic; that is accepted, not silently unmentioned.
BASE_VERIFICATION_CHECK_TIMEOUT_SECONDS = int(
    workflow_core.DISPATCH_SHORT_STEP_TIMEOUT.total_seconds() * 0.6
)
# The `git worktree add`/`remove` either side of that command run are local,
# object-store-only operations against a clone that already exists on disk --
# nothing like DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS's 20-minute budget for an
# actual network clone, which classify_verification_failure_against_base used
# to (wrongly) reuse here. At 1200s, that bound alone exceeds the whole
# 600s record_failure activity budget the comment above promises room inside
# of -- a slow-but-real worktree add would be killed by Temporal with no
# heartbeat (record_failure is a NON_SEQUENCE_DISPATCH_STEP) before this
# timeout ever fired, and the failure note would never get written at all.
# Sized so add + remove + BASE_VERIFICATION_CHECK_TIMEOUT_SECONDS still leave
# room for save_failure_patch's own git calls and the note write.
BASE_VERIFICATION_WORKTREE_TIMEOUT_SECONDS = int(
    workflow_core.DISPATCH_SHORT_STEP_TIMEOUT.total_seconds() * 0.1
)
DEFAULT_PROVENANCE_MODEL = "claude-sonnet-5"
# Below this free space on the workdir root's filesystem, the dispatcher
# refuses to claim rather than start a ~1.2GB clone it cannot finish.
# FACTORY_DISK_FLOOR_GB overrides; <= 0 disables the check.
DEFAULT_DISK_FLOOR_GB = 5.0
# Age past which an orphaned factory-* workdir is swept regardless of what
# killed its owner. Comfortably above the 30m default worker budget plus the
# verification/bootstrap timeouts, so a live, merely slow run is never caught
# by age alone - workdir_owner_is_alive is what actually decides.
DEFAULT_ORPHAN_WORKDIR_MINUTES = 120
# Ceiling on a task-declared budget.max_agent_minutes. Derived from (not
# merely below) DEFAULT_ORPHAN_WORKDIR_MINUTES: a run's wall clock is the
# worker budget plus the pristine/declared verification and bootstrap
# timeouts bracketing it, and the total must stay under the age the orphan
# sweep treats as dead - otherwise a legitimately long, still-live run could
# lose its workdir mid-worker before mark_workdir_owner's liveness check ever
# gets a say. Computed rather than a second literal, so the two cannot
# silently drift apart.
MAX_AGENT_MINUTES_CEILING = DEFAULT_ORPHAN_WORKDIR_MINUTES - (
    (DEFAULT_VERIFICATION_TIMEOUT_SECONDS + DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS) // 60
)
VERIFICATION_ENV_ALLOWLIST = (
    "HOME",  # Needed by shells and Python tooling for user-local config discovery.
    "PATH",  # Needed to find command-line tools; clone-local venv/bin is prepended.
    "VIRTUAL_ENV",  # Needed so Python tooling recognises the clone-local venv.
    # R2603-5 moved this deployment's cluster name out of the code and into
    # required configuration; ea-derive.py now refuses to run without it, and
    # `python scripts/ea-conformance.py` is a declared verification command on
    # live beads. CI's lint job carries the same variable at the job level for
    # the same reason. Only the NAME is allowlisted — the household value
    # stays in the operator's worker environment, which is exactly where
    # R2603-5 says it belongs. Preflight looped three environmental faults on
    # 2026-09-04 (R2603-7) before this passthrough existed.
    "EA_CANONICAL_CLUSTER",
)
#: Who is running an operator command, for ``created_by`` on the beads it
#: writes. CREATED_BY names the dispatcher acting as its worker and is right
#: when a worker actually ran; it is wrong for a command a human typed.
#: --bind-pr, --requeue and --reconcile-review all stamped
#: "factory-dispatcher/codex" on transitions no codex performed, so between
#: 2026-08-09 and 2026-08-12 the event log reported worker activity during a
#: period when the worker had no quota and had run nothing since 08-08 15:30.
#: A log that misattributes work is worse than a log with a gap, because the
#: gap is visible.
OPERATOR_ENV = "FACTORY_OPERATOR"
DEFAULT_OPERATOR = "operator"
#: Reconciliation runs from the Temporal schedule as well as from the CLI (#340).
#: Neither is a worker run, and neither is a person, so the schedule records
#: itself rather than borrowing an identity from either.
SCHEDULED_ACTOR = "factory-dispatcher/schedule"
#: Which orchestrator actually ran a piece of work, for the ``ran_by`` fact
#: recorded on the task it completes (R2603-2). Amendment 35 once authorised
#: Gastown to drive the code-health lane beside this dispatcher for one
#: sprint (R26.03/O-7); Amendment 35 was withdrawn 2026-09-07, and Gastown is
#: no longer a recognised orchestrator (see KNOWN_ORCHESTRATORS below). What
#: the amendment leaves behind: this being a fact a run declares about
#: itself, never inferred afterwards from branch names, commit style or
#: timing — that is the classification-of-prose failure OPS-40 already
#: forbade, in a new costume. Distinct from ``worker_hint`` (content.py's
#: declared intent, a worker name) — this is an orchestrator identity,
#: recorded by the process that actually ran the task.
ORCHESTRATOR_ENV = "FACTORY_ORCHESTRATOR"
DEFAULT_ORCHESTRATOR = "factory-dispatcher"
#: Amendment 35's roster for the R26.03 comparison sprint. A declared identity
#: outside this set is not silently folded into this dispatcher's own
#: identity, or accepted as though it meant something recognised — PC-SUB-
#: 002/AC-3 names exactly that gap for agent-authored provenance, and this
#: platform has already paid for the unattributed-writer version of it once:
#: between 2026-08-09 and 2026-08-12 operator commands stamped
#: "factory-dispatcher/codex" on transitions no codex performed. A log that
#: misattributes work is worse than a log with a gap, because the gap is
#: visible.
KNOWN_ORCHESTRATORS = frozenset({DEFAULT_ORCHESTRATOR})
RAN_BY_UNRECOGNIZED = "unrecognized"
# Failed runs leave their diff here rather than vanishing with the temp clone.
FAILURE_PATCH_DIR = Path(
    os.environ.get("FACTORY_PATCH_DIR", Path.home() / ".factory-dispatcher" / "failed")
)

# Xcode's git fails with a license error on this machine; the CommandLineTools
# copy does not. Prefer it when present. (dark-factory-roadmap memory, 2026-07-27)
_CLT_GIT = "/Library/Developer/CommandLineTools/usr/bin/git"
GIT = _CLT_GIT if Path(_CLT_GIT).exists() else "git"

# FA-S49-1: identity for every git commit the dispatcher itself makes (not
# declared-verification subprocesses -- those get their own env from
# _verification_env). Nothing in this module set one before this; a clone on
# a CI runner with no global git identity fails "git commit" with "Author
# identity unknown", and on the operator's host the commit silently borrowed
# the operator's own identity instead. GIT_AUTHOR_*/GIT_COMMITTER_* are
# resolved by git before any config file, so setting them once here in
# `run()` covers every commit call site by construction, including ones a
# sibling bead adds later, rather than needing `-c user.name=...` repeated at
# each site. The `.invalid` TLD is reserved by RFC 2606 for addresses
# guaranteed never to be real, so this identity can never be misread as a
# person's -- the same "a log that misattributes work is worse than a log
# with a gap" reasoning as CREATED_BY/ORCHESTRATOR_ENV above.
FACTORY_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "factory-dispatcher",
    "GIT_AUTHOR_EMAIL": "factory-dispatcher@factory.invalid",
    "GIT_COMMITTER_NAME": "factory-dispatcher",
    "GIT_COMMITTER_EMAIL": "factory-dispatcher@factory.invalid",
}


class DispatchError(RuntimeError):
    """A run failed in a way that should mark the task, not crash the loop."""


class MissingConfigError(RuntimeError):
    """Refused to start: required household configuration is absent.

    Mirrors worker.py's MissingConfigError (same reasoning, same "name what's
    missing, don't silently substitute a compiled-in default" contract) but
    declared locally rather than imported, since worker.py already imports
    from dispatch.py and the reverse would be circular.
    """


@dataclass(frozen=True)
class CapacityFailure:
    """A worker failure caused by exhausted agent capacity, not task work."""

    cause: str
    retry_at: str | None = None

    def describe(self) -> str:
        message = f"worker reported {self.cause}"
        if self.retry_at:
            message += f"; retry_at={self.retry_at}"
        return message


class DispatchCapacityError(DispatchError):
    """A worker failed because the agent account is out of usable capacity."""

    def __init__(self, failure: CapacityFailure):
        self.failure = failure
        super().__init__(f"capacity backpressure: {failure.describe()}")


class DispatchEnvironmentError(RuntimeError):
    """The run environment failed before the worker could be judged."""


class CloneGitControlTampered(DispatchError):
    """A clone's ``.git`` control paths no longer match the fingerprint
    recorded when it was made (dev.finding 79db3113 part c1/c2). Deliberately
    a DispatchError, never a DispatchEnvironmentError -- work-vs-environment
    is retry_policy.classify_dispatch_failure's call, not this type's."""


def _failure_class_note(
    classification: retry_policy.DispatchFailureClass,
) -> str:
    return f" Failure class: {classification.name}."


def store_error_status(exc: Exception) -> int | None:
    """Return an HTTP-style store status when the implementation exposes one."""
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


class IllegalTransitionError(RuntimeError):
    """A transition was requested for an edge dev_task_contract.DEV_TASK_STATE_MACHINE
    does not declare.

    Raised locally, before any store call — see transition_state_checked.
    Substrate happens to refuse the same request server-side (409/422), but
    that is one implementation's behaviour, not a property of the BeadStore
    interface: bd's only compare-and-set is pending -> doing
    (dev_task_contract.BD_CLAIM_SUPPORTED_EDGES), so a store built around that
    single edge has nothing built in to refuse anything else with. Trusting
    such a store to say no is how it gets obeyed instead of caught.
    """

    def __init__(self, from_state: str, to_state: str):
        super().__init__(
            f"illegal_transition: {from_state!r} -> {to_state!r} is not a "
            "declared edge in dev_task_contract.DEV_TASK_STATE_MACHINE"
        )
        self.from_state = from_state
        self.to_state = to_state


def transition_state_checked(
    sub: BeadStore, bead_id: str, from_state: str, to_state: str, created_by: str
) -> dict:
    """The dispatcher's one path to BeadStore.transition_state.

    Asserts (from_state, to_state) is a declared edge in
    dev_task_contract.DEV_TASK_STATE_MACHINE before the store is ever called.
    This does not replace the store's own compare-and-set — a store can still
    (and must) reject the call because the bead has moved on from from_state
    since it was last read; that race is still the store's to catch, and
    still surfaces by raising out of the sub.transition_state call below. What
    this adds is independent of any one store's behaviour: the edge itself is
    judged against the machine the dispatcher already knows, not against
    whatever a particular store implementation happens to enforce.
    """
    legal_targets = dev_task_contract.DEV_TASK_STATE_MACHINE.get(from_state, frozenset())
    if to_state not in legal_targets:
        raise IllegalTransitionError(from_state, to_state)
    return sub.transition_state(bead_id, from_state, to_state, created_by)


@dataclass(frozen=True)
class WorkerEntry:
    argv: tuple[str, ...]
    quarantined: bool
    allowed_lanes: tuple[str, ...]
    quarantine_reason: str | None = None
    # Retirement is a third state, distinct from quarantine (Amendment 30 diff
    # item 1): quarantine invites a reader to look for a fix, retirement says
    # the worker is no longer licensed for this platform.
    retired: bool = False
    retirement_reason: str | None = None
    # Extra subpaths the containment profile allows writes to, per worker
    # (Amendment 30 PR-6). Reviewed like any registry change; ~ expands.
    containment_allow: tuple[str, ...] = ()
    # Amendment 30 PR-7. extra_env is applied to the worker process only;
    # provenance_model names what provenance records for this worker's runs;
    # grade_json_result parses the CLI's result JSON because exit codes are
    # not a health signal for the claude CLI (spike finding #15); uses_personas
    # materialises docs/agents/ into the clone and assembles the persona
    # system prompt per the architect predicate.
    extra_env: tuple[tuple[str, str], ...] = ()
    provenance_model: str | None = None
    grade_json_result: bool = False
    uses_personas: bool = False


@dataclass(frozen=True)
class WorkerSelection:
    name: str
    argv: tuple[str, ...]
    containment_allow: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()
    provenance_model: str | None = None
    grade_json_result: bool = False
    uses_personas: bool = False
    # Set when a hinted worker was retired and this selection is the default
    # worker standing in for it (PRIN-008 fallback, see select_worker). The
    # caller writes this to the bead as a status note rather than losing it.
    fallback_note: str | None = None

    def command_display(self) -> str:
        return " ".join(self.argv[:2] or (self.name,))


def provenance_for_task(
    task_id: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    *,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> dict:
    """Attribution for a bead a worker run produced.

    ``tokens``/``cost_usd`` are the worker's own measurement — read off its
    result JSON's ``usage``/``total_cost_usd`` fields by the caller and passed
    through unchanged. The default is ``None``, not ``0``: every code path that
    writes this record before a worker has run (the claim note) or without a
    parsed result to read (a pre-run environmental fault) has not measured
    anything, and ``0`` would assert the run spent nothing when the truth is
    that no one looked. ``BeadProvenance`` (substrate schemas.py) accepts
    ``None`` here for exactly this reason — see its docstring for the
    alternatives that were rejected.
    """
    return {
        "worker": worker_name,
        "model": os.environ.get("FACTORY_PROVENANCE_MODEL", DEFAULT_PROVENANCE_MODEL),
        "prompt_ref": f"dev.task/{task_id}",
        "tokens": tokens,
        "cost_usd": cost_usd,
        "duration_s": max(0.0, duration_s),
    }


def operator_actor() -> str:
    """The identity to record for a command an operator invoked."""
    return os.environ.get(OPERATOR_ENV, DEFAULT_OPERATOR)


@dataclass(frozen=True)
class RanBy:
    """The ``ran_by`` fact to record for a task, and a warning if it's dodgy.

    ``value`` is what goes on the bead; ``warning`` is non-``None`` exactly
    when the declared identity was not honoured as given, so the caller can
    report it rather than write it silently (PC-SUB-002/AC-3).
    """

    value: str
    warning: str | None = None


def declared_orchestrator() -> str:
    """What this process declares itself as, absent an explicit override.

    Read fresh at completion time (never cached), so a deployment running
    this same dispatcher code under a different identity (FACTORY_ORCHESTRATOR)
    is reflected per-run rather than once at import time.
    """
    return os.environ.get(ORCHESTRATOR_ENV, DEFAULT_ORCHESTRATOR)


def resolve_ran_by(declared: str) -> RanBy:
    """Validate a self-declared orchestrator identity for the ``ran_by`` fact.

    ``declared`` is what the writer says it is — never inferred from branch
    names, commit style or timing (R2603-2, OPS-40's classification-of-prose
    failure in a new costume). Empty or unrecognised input is recorded as
    unrecognised, carrying the raw value for audit, and is never silently
    absorbed into ``DEFAULT_ORCHESTRATOR``: an unrecognised writer folded into
    a known identity is exactly how the R26.03 comparison's own evidence would
    get corrupted (see ``KNOWN_ORCHESTRATORS``'s docstring for the incident
    this already happened once as).
    """
    actor = (declared or "").strip()
    if not actor:
        return RanBy(
            RAN_BY_UNRECOGNIZED,
            "orchestrator declared no identity when it ran this task; "
            "recorded as unrecognized rather than assumed to be "
            f"{DEFAULT_ORCHESTRATOR!r}",
        )
    if actor in KNOWN_ORCHESTRATORS:
        return RanBy(actor)
    return RanBy(
        f"{RAN_BY_UNRECOGNIZED}:{actor}",
        f"orchestrator {actor!r} is not on the known roster "
        f"({', '.join(sorted(KNOWN_ORCHESTRATORS))}); recorded as unrecognized "
        "rather than mapped onto a known lane",
    )


def provenance_for_operator_action(task_id: str, action: str) -> dict:
    """Attribution for a bead an operator wrote by running a command.

    Distinct from :func:`provenance_for_task` on purpose. That function names a
    worker and the model it ran; an operator action runs no model at all, so
    reusing it would record ``codex-cli`` against work a human typed — a false
    attribution in the one field the provenance record exists to make true.

    ``model`` is ``"none"`` rather than absent because ``BeadProvenance``
    requires all six fields with ``extra="forbid"`` and ``min_length=1``
    (schemas.py:655). ``"none"`` is therefore the honest way to say no model
    call occurred, and it is distinguishable from every real model id in use.

    ``tokens``/``cost_usd`` stay ``0``/``0.0`` here, not ``None`` —
    :func:`provenance_for_task` uses ``None`` for "a worker ran but reported no
    usage figures," which does not apply to an action that ran no worker at
    all. ``model: "none"`` already carries that distinction for this function;
    a second one on the same record would say the same thing twice.
    """
    return {
        "worker": os.environ.get("FACTORY_OPERATOR", f"operator/{action}"),
        "model": "none",
        "prompt_ref": f"dev.task/{task_id}",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }


@dataclass(frozen=True)
class PullRequestStatus:
    number: int | None
    url: str
    state: str
    merge_commit: str | None
    # The PR branch's own tip, independent of whether it ever merged --
    # distinct from merge_commit, which only exists once GitHub actually
    # merges the PR. A closed-unmerged PR has no merge_commit but its head
    # still names the last commit the work reached, and GitHub retains it at
    # refs/pull/<number>/head indefinitely regardless of what happens to the
    # branch ref afterward (see reconcile_review_tasks's preserved_attempts
    # write, and .factory/design.md decision 1). Defaulted so the other two
    # construction sites (pull_requests_for_branch, tests' pr_status()) that
    # never asked GitHub for it are not forced to name a value they don't have.
    head_sha: str | None = None

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED"


DEV_LANES = ("code-health", "drift", "bug-triage", "feature")
# The claude CLI harness (the process itself, not the task work it performs)
# lazily creates a per-invocation scratchpad under /tmp/claude-<uid>/<slug of
# cwd> for its own session bookkeeping, and does not honor the TMPDIR
# redirection run_worker already sets for worker writes - confirmed live
# (dev.task 070afe98) when this very worker's own Bash calls failed EPERM
# against that path despite TMPDIR pointing at the per-run tmpdir. The
# slugged subdirectory name is an internal detail of the harness the
# dispatcher cannot predict per run, so the allowance is the UID-scoped root:
# the tightest boundary constructible without depending on that detail.
CLAUDE_HARNESS_SCRATCHPAD_ROOT = f"/tmp/claude-{os.getuid()}"
WORKER_REGISTRY: dict[str, WorkerEntry] = {
    "claude": WorkerEntry(
        # Spike-validated shape (FINDINGS #12-#16): the skip-permissions flag
        # belongs INSIDE the wrapper and nowhere else - the pair is the argv
        # shape. Session persistence off so no ~/.claude writes; settings
        # sources emptied and MCP strict so the worker inherits nothing from
        # user scope; Sonnet 5 pinned per the decided worker tier; JSON output
        # because exit codes are not a health signal for this CLI.
        argv=(
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--model",
            "claude-sonnet-5",
            "--output-format",
            "json",
        ),
        quarantined=False,
        allowed_lanes=DEV_LANES,
        containment_allow=(CLAUDE_HARNESS_SCRATCHPAD_ROOT,),
        extra_env=(("DISABLE_AUTOUPDATER", "1"),),
        provenance_model="claude-sonnet-5",
        grade_json_result=True,
        uses_personas=True,
    ),
    "antigravity": WorkerEntry(
        argv=("agy", "-p"),
        quarantined=True,
        allowed_lanes=DEV_LANES,
        quarantine_reason=(
            "containment escape in F1 spike: escaped worktree isolation and edited "
            "the real working tree while reporting success "
            "(docs/archive/2026-08-agent-harness-spike/FINDINGS.md #11)"
        ),
    ),
}


@dataclass(frozen=True)
class Config:
    repo: str = _PLACEHOLDER_REPO
    remote: str = _PLACEHOLDER_REMOTE
    base_ref: str = DEFAULT_BASE_REF
    repo_root: Path = Path(__file__).resolve().parents[2]
    # None => resolved at call time via tempfile.gettempdir(), not cached on
    # the class - so a test (or an operator via FACTORY_WORKDIR_ROOT-style
    # override at the call site) can redirect it without reimporting.
    workdir_root: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        """Build Config from the process environment, refusing rather than
        guessing when FACTORY_REPO / FACTORY_REMOTE are absent (R2603-5: this
        used to fall back to one household's real repository slug, so a
        differently-configured household silently ran against the wrong
        repo instead of being told what to set).
        """
        from config import missing_required_config

        missing = [
            name
            for name in missing_required_config(os.environ)
            if name in ("FACTORY_REPO", "FACTORY_REMOTE")
        ]
        if missing:
            raise MissingConfigError(
                "factory-dispatcher refusing to start. Missing required "
                f"configuration: {', '.join(missing)}. This is a configuration "
                "fault, not a connectivity fault. Set these in the worker "
                "environment (or the launchd env file) and start again."
            )
        return cls(
            repo=os.environ["FACTORY_REPO"],
            remote=os.environ["FACTORY_REMOTE"],
            base_ref=os.environ.get("FACTORY_BASE_REF", DEFAULT_BASE_REF),
        )


def required_worker_executables() -> tuple[str, ...]:
    """Base tooling plus the binaries of workers that can actually be selected.

    Amendment 30 PR-4: a retired or quarantined worker's binary must not be
    demanded of a host that can no longer dispatch to it.
    """
    from config import BASE_WORKER_EXECUTABLES

    worker_binaries = sorted(
        {
            entry.argv[0]
            for entry in WORKER_REGISTRY.values()
            if entry.argv and not entry.quarantined and not entry.retired
        }
    )
    return tuple(BASE_WORKER_EXECUTABLES) + tuple(
        b for b in worker_binaries if b not in BASE_WORKER_EXECUTABLES
    )


def select_worker(task: dict) -> WorkerSelection:
    """Resolve a task's worker before isolation or invocation begins.

    A hint naming a retired worker falls back to the default worker rather
    than refusing the run: PRIN-008 says a hint is never silently ignored,
    but Amendment 30 PR-3's retirement is a permanent, decided state (unlike
    quarantine, which stays open pending investigation) - stranding a bead
    over a worker that is never coming back serves no one. The fallback is
    recorded on the returned selection's ``fallback_note`` so the caller can
    write it to the bead as a status note naming both the retired hint and
    the worker that ran instead. Retirement of the *default* worker itself
    has no lower rung to fall back to, so that case still refuses.
    """
    content = task.get("content") or {}
    hinted = content.get("worker_hint")
    worker_name = hinted or DEFAULT_WORKER
    fallback_note: str | None = None

    if worker_name not in WORKER_REGISTRY:
        if hinted:
            raise DispatchError(
                f"unknown worker {worker_name!r}; refusing instead of falling back "
                f"to default {DEFAULT_WORKER!r}"
            )
        raise DispatchError(
            f"default worker {worker_name!r} is not present in WORKER_REGISTRY"
        )

    worker = WORKER_REGISTRY[worker_name]
    if worker.retired:
        reason = worker.retirement_reason or "no retirement reason recorded"
        if not hinted:
            raise DispatchError(
                f"worker {worker_name!r} is retired: {reason}. Retirement means no "
                "longer licensed for this platform; returning it needs a new "
                "amendment, not a fix."
            )
        fallback_note = (
            f"worker_hint {worker_name!r} names a retired worker and was not "
            f"honored - {reason} Falling back to the default worker "
            f"{DEFAULT_WORKER!r} for this run instead of refusing it."
        )
        worker_name = DEFAULT_WORKER
        if worker_name not in WORKER_REGISTRY:
            raise DispatchError(
                f"default worker {worker_name!r} is not present in WORKER_REGISTRY"
            )
        worker = WORKER_REGISTRY[worker_name]
        if worker.retired or worker.quarantined:
            raise DispatchError(
                f"worker_hint {hinted!r} is retired and the fallback default "
                f"worker {worker_name!r} is not dispatchable either; refusing"
            )
    if worker.quarantined:
        reason = worker.quarantine_reason or "no quarantine reason recorded"
        raise DispatchError(f"worker {worker_name!r} is quarantined: {reason}")

    lane = content.get("lane") or "unknown"
    if lane not in worker.allowed_lanes:
        allowed = ", ".join(worker.allowed_lanes) or "(none)"
        raise DispatchError(
            f"worker {worker_name!r} is not permitted for lane {lane!r}; "
            f"allowed lanes: {allowed}"
        )

    return WorkerSelection(
        name=worker_name,
        argv=worker.argv,
        containment_allow=worker.containment_allow,
        extra_env=worker.extra_env,
        provenance_model=worker.provenance_model,
        grade_json_result=worker.grade_json_result,
        uses_personas=worker.uses_personas,
        fallback_note=fallback_note,
    )


# ---------------------------------------------------------------------------
# shell helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str], cwd: Path | None = None, timeout: float | None = None, check: bool = True,
    env: dict[str, str] | None = None, needs: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    # The environment is narrowed (dev.finding a0166920): with no explicit
    # env=, this is process_env.child_env(needs=needs), not raw os.environ,
    # merged with FACTORY_GIT_IDENTITY (harmless noise to non-git commands,
    # load-bearing for every commit this module makes). ``needs`` names the
    # credential keys this call's child actually needs (for example
    # run_merge_script passes needs=('DISCORD_WEBHOOK_URL',)); child_env hands
    # back only those, never the full os.environ. An explicit ``env`` is used
    # exactly as given, never merged with ``needs``.
    env = {**process_env.child_env(needs=needs), **FACTORY_GIT_IDENTITY} if env is None else env
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        raise DispatchError(
            f"`{' '.join(cmd[:3])}...` exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    return proc


def git_subcommand(cmd: list[str]) -> str:
    """First argv token after GIT that isn't a flag, skipping ``-c``/``-C``'s
    value (dev.finding 79db3113 part b; shared by the AST ratchet and tests)."""
    skip_next = False
    for item in cmd[1:]:
        if skip_next:
            skip_next = False
        elif item in ("-c", "-C"):
            skip_next = True
        elif not item.startswith("-"):
            return item
    raise ValueError(f"no git subcommand found in {cmd!r}")


# Re-exported: tests drive the record path directly (dev.finding 79db3113
# part c1, AC-1/AC-2). The fingerprint, the manifest walk, the hash helper,
# the record path/read/write, and the raise-free compare all live in
# git_control.py -- pure, stdlib-only, never importing this module (AC-5).
_git_control_record_path = git_control.record_path


def record_clone_git_control(clone: Path) -> None:
    """Called as make_clone's last statement. Refuses to overwrite an
    existing record (dev.finding 79db3113 part c1): the baseline must
    never move."""
    if git_control.record_exists(clone):
        raise DispatchError(
            f"git-control record already exists for {clone}: {git_control.record_path(clone)}"
        )
    git_control.write_record(clone, git_control.compute_git_control_fingerprint(clone))


def verify_clone_git_control(clone: Path) -> None:
    """Raises CloneGitControlTampered naming the first changed path,
    ``".git entry replaced"``, ``".git/commondir present"``, or (no record
    at all) ``"no git-control record"`` -- fail closed. Wired to
    check_clone_git_control, called before every clone-routed git call
    (dev.finding 79db3113 part c1/c2)."""
    try:
        baseline = git_control.read_record(clone)
    except FileNotFoundError:
        raise CloneGitControlTampered(
            f"{clone}: no git-control record at {git_control.record_path(clone)}"
        ) from None
    current = git_control.compute_git_control_fingerprint(clone)
    difference = git_control.compare_git_control_fingerprint(baseline, current, clone)
    if difference is not None:
        raise CloneGitControlTampered(difference)


def check_clone_git_control(clone: Path) -> None:
    """Live (dev.finding 79db3113 part c2): calls verify_clone_git_control
    and nothing else. git_in_clone calls this before every clone-routed git
    call, so every such call fails closed on a tampered or unrecorded
    clone -- see verify_clone_git_control's own docstring for what is
    checked."""
    verify_clone_git_control(clone)


def git_in_clone(clone: Path, args: list[str], **run_kwargs: Any) -> subprocess.CompletedProcess:
    """The one path to a git call whose cwd is a worker-written clone
    (dev.finding 79db3113 part b). Argv stays ``[GIT, *args]``; the env
    disables hooks, fsmonitor and gc/maintenance (gc can rewrite
    ``.git/info/refs``, a path part c's fingerprint reads) and neutralises
    global/system config. ``run()`` takes ``env`` as-is and ignores ``needs``,
    applied to ``child_env`` here first."""
    needs = run_kwargs.pop("needs", ())
    check_clone_git_control(clone)
    env = {
        **process_env.child_env(needs=needs), **FACTORY_GIT_IDENTITY,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "gc.auto", "GIT_CONFIG_VALUE_2": "0",
        "GIT_CONFIG_KEY_3": "maintenance.auto", "GIT_CONFIG_VALUE_3": "false",
    }
    return run([GIT, *args], cwd=clone, env=env, **run_kwargs)


# worker_revision imports DEFAULT_BASE_REF/GIT/run from this module, so
# failure_diagnosis (which imports worker_revision) can only be imported
# here, after all three are defined — importing it at the top of this file
# with the rest reintroduces the circular import worker_revision.py's own
# module docstring already explains.
import failure_diagnosis  # noqa: E402
import stranded_alerts  # noqa: E402

@dataclass(frozen=True)
class TreeState:
    """Cheap identity of a working tree: HEAD plus the dirty-file list.

    Kept as two fields rather than one blob so the breach message can name the
    paths that changed without mistaking a moved HEAD for one of them.
    """

    head: str
    status: str


def fingerprint_tree(root: Path) -> TreeState:
    return TreeState(
        head=run([GIT, "rev-parse", "HEAD"], cwd=root).stdout.strip(),
        status=run([GIT, "status", "--porcelain"], cwd=root).stdout.strip(),
    )


def fingerprint_clone(clone: Path) -> TreeState:
    """``fingerprint_tree``'s reading via ``git_in_clone`` (dev.finding
    79db3113 part b/AC-3); ``fingerprint_tree`` stays repo_root-only."""
    return TreeState(
        head=git_in_clone(clone, ["rev-parse", "HEAD"]).stdout.strip(),
        status=git_in_clone(clone, ["status", "--porcelain"]).stdout.strip(),
    )


def changed_paths(clone: Path) -> list[str]:
    """Every path the worker created, modified or deleted, tracked or not.

    The dispatcher's contract is that a worker leaves its diff uncommitted
    (``open_pull_request`` does the committing) -- but a worker that commits
    it anyway leaves a clean working tree, which the plain diff-against-HEAD
    below cannot distinguish from a worker that changed nothing at all
    (dev.finding 71418f56). Where that plain diff and the untracked listing
    both come back empty, fall back to
    ``_committed_but_unpushed_paths`` -- the same base-resolution
    ``save_failure_patch``'s preservation path already uses to name a
    committed-but-unpushed patch's files, so this guard and that path can
    never disagree about whether work exists.
    """
    tracked = git_in_clone(clone, ["diff", "--name-only", "HEAD"]).stdout.split()
    # Untracked files are invisible to `git diff` — a lesson that made a
    # complete spike run look half-done (FINDINGS, meta-lesson).
    untracked = git_in_clone(
        clone, ["ls-files", "--others", "--exclude-standard"]
    ).stdout.split()
    paths = set(tracked) | set(untracked)
    if not paths:
        paths.update(_committed_but_unpushed_paths(clone))
    return sorted(paths)


def tracked_paths(clone: Path) -> list[str]:
    """Every path git tracks at ``clone``'s current HEAD.

    A plain ``git ls-tree`` read of the clone the dispatcher already made --
    no network call, no read of ``cfg.repo_root`` or any operator working
    tree, no walk of an arbitrary directory (see
    ``guards.absent_premise_paths``, which this feeds: it grades a spec's
    named premise paths against exactly this listing).

    ``-z`` NUL-terminates each entry instead of newline-terminating it, so a
    tracked path containing a space or newline still comes back as one
    listing entry (dev.finding: a bare ``.stdout.split()`` split on ANY
    whitespace, not on lines, so ``apps/space dir/f.py`` used to arrive as
    two separate entries and read as absent from a ref it was present on).
    ``-z`` also disables ``git ls-tree``'s default C-style quoting of paths
    with special or non-ASCII bytes, so this reads raw filenames rather than
    quoted ones that would need a second unescaping pass to match a spec's
    plain-text ``context_refs``/``scope.paths`` entries.
    """
    output = git_in_clone(clone, ["ls-tree", "-rz", "--name-only", "HEAD"]).stdout
    return output.split("\0")[:-1] if output else []


def diff_paths_since(clone: Path, rev: str) -> list[str]:
    """Every path that differs between ``rev`` and the clone's current state.

    ``changed_paths`` diffs against ``HEAD``, so a commit that already sits at
    HEAD before the worker's own turn -- a preserved-baseline commit applied
    by ``apply_preserved_baseline`` -- is invisible to it: the diff is empty
    even though that commit's files ride along in the PR the branch eventually
    opens. ``rev`` should be the revision the clone was cloned from
    (``verified_revision``), the true merge-base with the PR's target branch,
    so this reports the full set of paths the PR will land -- baseline commit
    and worker's own changes alike -- exactly what the scope check must grade.
    """
    tracked = git_in_clone(clone, ["diff", "--name-only", rev]).stdout.split()
    untracked = git_in_clone(
        clone, ["ls-files", "--others", "--exclude-standard"]
    ).stdout.split()
    return sorted(set(tracked) | set(untracked))


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseRefStatus:
    """Relationship between the local clone source branch and its upstream."""

    base_ref: str
    local_rev: str
    upstream_ref: str
    upstream_rev: str
    local_only: int
    upstream_only: int

    @property
    def current(self) -> bool:
        return self.local_rev == self.upstream_rev

    @property
    def behind_upstream(self) -> bool:
        return self.upstream_only > 0

    @property
    def wrong(self) -> bool:
        """True when ``base_ref`` carries commits its tracking upstream does not
        have -- not merely behind, but pointed somewhere the upstream never was.

        2026-09-17: an operator repo's local ``main`` was pointed at a commit
        that existed only on an unmerged pull request's branch. ``upstream_only``
        (this status's other count) was 0 -- the tracking remote had nothing
        the local ref lacked -- so ``behind_upstream`` read False and the old
        healthy-path early-return never inspected ``local_only`` at all. A
        branch's own tip is always among its local-only commits whenever any
        exist (every earlier commit on the branch is an ancestor of the tip,
        so if the tip were reachable from upstream every ancestor would be
        too) -- so ``local_only > 0`` alone, independent of ``upstream_only``,
        is both necessary and sufficient to say ``local_rev`` is not an
        ancestor-or-equal of ``upstream_rev``.
        """
        return self.local_only > 0

    @property
    def classification(self) -> str:
        """One of "healthy", "stale", "wrong", "diverged".

        "stale" (behind only) has a one-command fast-forward remedy. "wrong"
        and "diverged" both carry commits absent from the tracking upstream --
        fast-forwarding cannot fix either, the ref must be repointed -- and
        differ only in whether the ref is *also* behind.
        """
        if self.local_only and self.upstream_only:
            return "diverged"
        if self.local_only:
            return "wrong"
        if self.upstream_only:
            return "stale"
        return "healthy"

    def describe(self) -> str:
        return (
            f"{self.base_ref} local={self.local_rev} tracking "
            f"{self.upstream_ref}={self.upstream_rev} "
            f"(local_only={self.local_only}, upstream_only={self.upstream_only})"
        )


def _base_ref_refusal_message(status: BaseRefStatus) -> str:
    """The refusal text for ``BaseRefStaleError``, distinct by ``status.wrong``.

    A stale-only base (``wrong`` False) keeps the exact message this replaced --
    a fast-forward is the one-command remedy and operators already recognise
    this wording. A wrong or diverged base (``wrong`` True) states plainly that
    fast-forwarding will not fix it and names the commit that is not on the
    tracking upstream, so the operator reaches for `repoint`, not `fetch` --
    conflating the two sends them to the wrong command (2026-09-17).
    """
    if status.wrong:
        plural = "commit" if status.local_only == 1 else "commits"
        return (
            "base ref carries commits its tracking remote does not have; "
            "refused to clone: "
            + status.describe()
            + f". {status.local_rev} is NOT an ancestor-or-equal of "
            f"{status.upstream_ref}={status.upstream_rev} ({status.local_only} "
            f"local-only {plural}). This is WRONG, not stale -- fast-forwarding "
            f"will not fix it. {status.base_ref} must be repointed (reset or "
            f"rebased) at a commit {status.upstream_ref} actually contains."
        )
    return (
        "base ref is behind its tracking remote; refused to clone: "
        + status.describe()
    )


class BaseRefStaleError(DispatchEnvironmentError):
    """The configured local base branch cannot be safely cloned from: it is either
    behind the remote ref it tracks and could not be safely fast-forwarded (or a
    concurrent clone made fast-forwarding it right now unsafe), or it carries
    commits its tracking remote does not have at all (``status.wrong`` -- see
    ``BaseRefStatus.wrong``), which no fast-forward can fix."""

    def __init__(self, status: BaseRefStatus):
        self.status = status
        super().__init__(_base_ref_refusal_message(status))


class BaseRefFastForwardBlockedError(RuntimeError):
    """`_fast_forward_base_ref` refused: the working tree stands in the way.

    Internal to this module: `ensure_base_ref_current` catches this, raises the
    declared alert (this is one of the two causes that genuinely needs a person),
    and re-raises `BaseRefStaleError` with the shape every existing caller already
    handles. This class itself never crosses that boundary.
    """


class SourceMirrorBaseRefWrongError(DispatchEnvironmentError):
    """The source mirror's ``main`` carries commits GitHub does not have --
    confirmed by a successful fetch FROM GitHub, not merely suspected.

    dev.finding ac4e8569 (2026-09-24), reversing #649's "must never fail the
    drain" for this one outcome only: #649's fear was an UNREACHABLE canonical
    remote stopping every drain, and that fear is untouched -- a fetch failure
    still degrades to a best-effort skip (``_sync_source_mirror_main``'s
    generic ``except Exception``, and ``SourceMirrorSyncResult.error`` for a
    reachable-but-not-applied outcome like the mirror's ``main`` being checked
    out). This is the other outcome: GitHub WAS reached, and the comparison
    says the mirror is wrong. Proceeding past that -- as the prior code did,
    announcing the declared alert and then returning -- is proceeding on a
    base confirmed wrong, the 2026-09-17 incident's own shape one hop further
    out. No fast-forward here or one hop down can fix it: unlike
    ``BaseRefStaleError`` (which names ``status.base_ref``, the WORKER
    CHECKOUT's own ref, as needing repointing), the ref that actually needs
    repointing here is the MIRROR's ``main`` -- a different repository, so this
    gets its own message rather than reusing that one's wording.

    Deliberately carries "refused to clone:" in its message, the same marker
    ``is_stale_base_ref_reason`` keys off of for ``BaseRefStaleError`` -- this
    refusal is a base-ref problem with a one-command remedy (an operator fixes
    the mirror), not a fault in the bead's own work, so it must route through
    ``record_stale_base_ref_fault`` (retried at zero cost every cycle, never
    tripping the bound-3 environmental-fault breaker) exactly like the inner
    hop's WRONG case, not through ``record_environment_failure``.
    """

    def __init__(
        self, main_ref: str, local_rev: str, remote_ref: str, remote_rev: str, local_only: int
    ):
        self.main_ref = main_ref
        self.local_rev = local_rev
        self.remote_ref = remote_ref
        self.remote_rev = remote_rev
        self.local_only = local_only
        plural = "commit" if local_only == 1 else "commits"
        super().__init__(
            "source mirror's main carries commits its canonical remote (GitHub) "
            "does not have; refused to clone: "
            f"{main_ref} local={local_rev} tracking {remote_ref}={remote_rev} "
            f"(local_only={local_only}). {local_rev} is NOT an ancestor-or-equal "
            f"of {remote_ref}={remote_rev} ({local_only} local-only {plural}). "
            "This is WRONG, not stale -- fast-forwarding will not fix it. The "
            f"SOURCE MIRROR's {main_ref} -- not the worker checkout's -- must be "
            "repointed (reset or rebased) at a commit the canonical remote "
            "actually contains."
        )


class WorkerCheckoutStaleError(DispatchEnvironmentError):
    """The checkout ``make_clone`` would clone from is a valid but outdated ancestor of main.

    2026-08-29: PR #583 was cut from a checkout nine commits behind main and
    its declared verification ran with no revision recorded anywhere the
    release gate could read -- see
    ``worker_revision.check_checkout_freshness`` for the incident. Wraps
    ``worker_revision.WorkerCheckoutStaleError`` so ``dispatch_once``'s
    existing ``except DispatchEnvironmentError`` handling
    (``record_environment_failure``) covers it without a second bespoke
    failure-recording path -- this is a fault the run must never reach the
    worker having spent budget on, exactly like a stale base ref or missing
    config.
    """


def base_ref_status(cfg: Config) -> BaseRefStatus:
    """Compare the local base branch with its configured upstream ref.

    The dispatcher clones from ``cfg.repo_root``, so ``cfg.base_ref`` must not be
    older than the remote-tracking ref Git says it tracks. This intentionally
    reads local Git refs only; operators update/fetch the tracking ref, and the
    dispatcher refuses when the working branch has not caught up.
    """
    local_rev = run([GIT, "rev-parse", cfg.base_ref], cwd=cfg.repo_root).stdout.strip()
    upstream_ref = run(
        [GIT, "rev-parse", "--abbrev-ref", f"{cfg.base_ref}@{{upstream}}"],
        cwd=cfg.repo_root,
    ).stdout.strip()
    upstream_rev = run(
        [GIT, "rev-parse", f"{cfg.base_ref}@{{upstream}}"],
        cwd=cfg.repo_root,
    ).stdout.strip()
    counts = run(
        [GIT, "rev-list", "--left-right", "--count", f"{local_rev}...{upstream_rev}"],
        cwd=cfg.repo_root,
    ).stdout.split()
    local_only = int(counts[0]) if len(counts) >= 1 else 0
    upstream_only = int(counts[1]) if len(counts) >= 2 else 0
    return BaseRefStatus(
        base_ref=cfg.base_ref,
        local_rev=local_rev,
        upstream_ref=upstream_ref,
        upstream_rev=upstream_rev,
        local_only=local_only,
        upstream_only=upstream_only,
    )


#: How long a fetched tracking ref may be trusted before ensure_base_ref_current
#: refetches rather than comparing against it as-is. 2026-09-03: `merge --ff-only`
#: reported the base ref "up to date" against a tracking ref two merges old, because
#: nothing had fetched in the interim -- on a factory whose own merges land several
#: times a day, a bound this loose is worse than none: it reports "current" while the
#: remote has moved. 15 minutes matches the dispatch schedule's own firing interval, so
#: the tracking ref is never older than one dispatch cycle without being refreshed.
BASE_REF_TRACKING_STALENESS_BOUND_S = 15 * 60


def _fetch_head_age_s(repo_root: Path) -> float | None:
    """Seconds since the last `git fetch` landed in ``repo_root``; None if never."""
    try:
        mtime = (repo_root / ".git" / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime


def _upstream_remote_name(cfg: Config) -> str:
    return run(
        [GIT, "for-each-ref", "--format=%(upstream:remotename)", f"refs/heads/{cfg.base_ref}"],
        cwd=cfg.repo_root,
    ).stdout.strip()


def _refresh_stale_tracking_ref(cfg: Config, *, force: bool = False) -> bool:
    """Fetch before comparing when the tracking ref might be older than the remote.

    ``force`` skips the staleness bound. Two callers set it: the
    ``status.wrong`` branch of ``ensure_base_ref_current`` (always -- it must
    not accuse a ref of being wrong on the strength of a tracking ref it
    never refreshed; see the comment at that call site for why the bound is
    actively harmful there rather than merely unhelpful), and
    ``ensure_base_ref_current``'s own opening call when ITS caller passes
    ``force_tracking_refresh=True`` (dev.finding 5170b3f9a: the scheduled
    drift tick, which needs this cycle's comparison to be against a tracking
    ref proven fresh against GitHub, not one that might be up to the
    staleness bound old).

    ``base_ref_status``/``schedule_status.describe_base_ref_status`` stay a pure local
    read (PRIN-004, #525: the operator's checked-out branch is a frozen contract, pinned
    by test_describe_base_ref_status_never_writes_to_the_checkout) -- a fetch only
    updates the remote-tracking ref, never the working tree or ``cfg.base_ref`` itself,
    and only ever happens here, on the clone-gating path that is about to act on the
    comparison regardless.

    Returns whether a fetch actually ran this call -- ``False`` when skipped inside
    the trust window or when no upstream remote is configured, ``True`` once the
    fetch below has landed. ``ensure_base_ref_current`` records this (AC-2/AC-3 of
    the bead that split the rendering half of 05a884ff out): a comparison made while
    this returned ``False`` is against a tracking ref that might be up to
    ``BASE_REF_TRACKING_STALENESS_BOUND_S`` seconds old, not one confirmed fresh this
    cycle -- purely an additional return value, no change to which branch runs.
    """
    age = _fetch_head_age_s(cfg.repo_root)
    if not force and age is not None and age < BASE_REF_TRACKING_STALENESS_BOUND_S:
        return False
    remote_name = _upstream_remote_name(cfg)
    if not remote_name:
        return False  # no upstream configured; base_ref_status's own rev-parse reports that
    try:
        run([GIT, "fetch", remote_name, cfg.base_ref], cwd=cfg.repo_root, timeout=120)
    except DispatchError as exc:
        age_desc = "unknown" if age is None else f"{age:.0f}s ago"
        raise DispatchEnvironmentError(
            f"could not refresh {cfg.base_ref}'s tracking ref from {remote_name} before "
            f"comparing (last fetch {age_desc}): {exc}"
        ) from exc
    return True


def _other_clone_in_flight(
    cfg: Config, *, exclude_workdir: Path | None = None
) -> tuple[str, ...]:
    """Which OTHER dispatch attempts currently hold a live isolated clone, if any.

    Empty means none -- callers that only care about yes/no can keep testing
    truthiness. Non-empty names the blocking workdirs, which is what the declared
    alert (``_record_concurrent_clone_defer``) needs once this defers long enough to
    look wedged rather than transient (OPS-60).

    Reuses the owner marker ``sweep_orphan_workdirs`` already trusts to tell a live
    run from an abandoned one, rather than a second liveness mechanism -- and, since
    OPS-60, that marker's liveness is asked of the RUN (Temporal, for an
    activity-created workdir), not merely the daemon process that created it. Mirrors
    ``worker_checkout_drift_response``'s DEFERRED outcome: moving a ref a concurrent
    clone might be reading from right now is exactly the "underneath a running task"
    case OPS-55 already refuses to do to the worker checkout -- this is the same defer
    for ``cfg.repo_root`` itself. ``exclude_workdir`` is the caller's own workdir (which
    already has a live owner marker by the time this runs -- itself), so this answers
    "some *other* clone", never "any clone including mine".
    """
    workdir_root = cfg.workdir_root or Path(tempfile.gettempdir())
    try:
        candidates = list(workdir_root.glob("factory-*"))
    except OSError:
        return ()
    exclude_resolved = exclude_workdir.resolve() if exclude_workdir is not None else None
    blocking: list[str] = []
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            if exclude_resolved is not None and candidate.resolve() == exclude_resolved:
                continue
        except OSError:
            continue
        if workdir_owner_is_alive(candidate):
            blocking.append(candidate.name)
    return tuple(blocking)


def _fast_forward_base_ref(cfg: Config, status: BaseRefStatus) -> None:
    """Fast-forward ``cfg.base_ref`` to ``status.upstream_rev``; raise, touching nothing,
    if that is not provably safe.

    Only called once the caller has already established ``status.local_only == 0`` (a
    genuine fast-forward -- nothing local to lose) and no other clone is in flight. When
    ``base_ref`` is not the checked-out branch, ``git branch -f`` is trivially safe: it
    moves a ref no working tree reads from, so there is nothing to obstruct. When it IS
    checked out, ``git merge --ff-only`` is itself the working-tree-obstruction check --
    git already refuses precisely when an uncommitted change would be overwritten, so
    that logic is not reimplemented here.
    """
    current_branch = run(
        [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.repo_root
    ).stdout.strip()

    if current_branch != status.base_ref:
        run([GIT, "branch", "-f", status.base_ref, status.upstream_rev], cwd=cfg.repo_root)
        # A detached HEAD faithfully at the old base tip -- the exact shape
        # worker_checkout.py advance leaves behind -- must move with the
        # branch, or the very next gauge (worker_revision's HEAD-vs-main
        # freshness check in make_clone) refuses the fast-forward this
        # function just performed: the OPS-21 re-stranding the release gate
        # traced on #629. A detached HEAD anywhere else is a deliberate pin
        # and stays untouched; the freshness check's refusal is then honest.
        if current_branch == "HEAD":
            head_rev = run(
                [GIT, "rev-parse", "HEAD"], cwd=cfg.repo_root
            ).stdout.strip()
            if head_rev == status.local_rev:
                moved = run(
                    [GIT, "checkout", "--detach", status.upstream_rev],
                    cwd=cfg.repo_root,
                    check=False,
                )
                if moved.returncode != 0:
                    raise BaseRefFastForwardBlockedError(
                        f"detached HEAD at the old {status.base_ref} tip could not "
                        "be advanced with the branch: "
                        + (moved.stderr or moved.stdout or "").strip()[:300]
                    )
        return

    merged = run(
        [GIT, "merge", "--ff-only", status.upstream_rev], cwd=cfg.repo_root, check=False
    )
    if merged.returncode != 0:
        raise BaseRefFastForwardBlockedError(
            f"{status.base_ref} is checked out in {cfg.repo_root} and `git merge "
            "--ff-only` refused: "
            + (merged.stderr or merged.stdout or "").strip()[:300]
        )


def _sync_source_mirror_main(cfg: Config) -> str | None:
    """Ref-only-fetch the source mirror's ``main`` from its own canonical remote --
    the outer hop in worker-checkout <- source mirror <- canonical remote (GitHub),
    run before ``_refresh_stale_tracking_ref``'s inner-hop comparison so "current"
    finally has one definition: GitHub's ``main``, with every downstream ref derived
    from it this same cycle.

    ``cfg.repo_root``'s own ``origin`` remote *is* the mirror's path in the deployed
    topology (``worker_checkout.advance()`` sets it there when it clones), so this
    resolves the mirror via ``worker_checkout.declared_source_repo_root`` rather than
    a second, parallel configuration surface for the same value.

    Best-effort by design, like ``_refresh_stale_tracking_ref`` one hop down, for every
    outcome where GitHub was never confirmed to disagree: a network blip, an
    unconfigured mirror, or a mirror whose ``main`` happens to be checked out right now
    all fall through to a returned reason string, never a raise -- #649's fear (an
    unreachable GitHub stopping every drain) is unchanged for all three.

    2026-09-24 (dev.finding ac4e8569) reverses #649 for the one outcome that is NOT a
    reachability failure: local-only commits on the mirror's ``main``, discovered only
    after a fetch FROM GitHub succeeded. That is GitHub confirming the mirror is wrong,
    not GitHub being unreachable -- letting the drain proceed past it (announcing the
    declared alert and returning, the prior behaviour) is proceeding on a base known
    wrong, the 2026-09-17 incident's shape one hop further out. This now raises
    ``SourceMirrorBaseRefWrongError`` after the same announce, so the refusal
    propagates out of ``ensure_base_ref_current`` (its first call) with nothing else
    in that function running -- no inner-hop fetch, no fast-forward attempt. See
    ``SourceMirrorBaseRefWrongError``'s own docstring for why this is a distinct
    exception rather than a reused ``BaseRefStaleError``.

    Local import: ``worker_checkout`` imports ``DEFAULT_BASE_REF``/``GIT``/``run`` from
    this module at module scope, so importing it back here at module scope would be
    circular -- the same reason ``ensure_worker_checkout_is_current`` imports
    ``worker_revision`` lazily.

    Returns ``None`` when the mirror is confirmed synced with its canonical remote
    this call (updated, or already equal) -- otherwise the reason it could not be
    confirmed, for ``ensure_base_ref_current`` to record as the authoritative-check
    state (AC-2/AC-3 of the bead that split the rendering half of 05a884ff out; the
    raise path above writes that same record itself, since it never reaches the normal
    return).
    """
    import worker_checkout

    try:
        mirror_root = worker_checkout.declared_source_repo_root(cfg.repo_root)
        result = worker_checkout.sync_source_mirror_main(
            source_repo_root=mirror_root, main_ref=cfg.base_ref
        )
    except worker_checkout.SourceMirrorLocalCommitsError as exc:
        failure_diagnosis.announce_base_ref_needs_person(
            exc.main_ref, exc.local_rev, exc.remote_ref, exc.remote_rev, str(exc)
        )
        _write_base_ref_check_record(checked=False, reason=str(exc))
        raise SourceMirrorBaseRefWrongError(
            exc.main_ref, exc.local_rev, exc.remote_ref, exc.remote_rev, exc.local_only
        ) from exc
    except Exception as exc:  # noqa: BLE001 - this outer hop is best-effort; the
        # inner hop below (still local-only) must keep working without it.
        print(f"  source mirror sync skipped: {exc}")
        return f"source mirror sync skipped: {exc}"

    if result.error:
        age = _fetch_head_age_s(mirror_root)
        age_desc = "unknown" if age is None else f"{age:.0f}s ago"
        print(
            f"  source mirror main not updated from its canonical remote (last fetch "
            f"{age_desc}): {result.error}"
        )
        return result.error
    if result.updated:
        print(f"  source mirror main fetched: {mirror_root} -> {result.revision}")
    return None


#: Host-local execution state (Pillar 10, never bead content -- same reasoning as
#: failure_diagnosis._alert_state_path / _concurrent_clone_defer_state_path below):
#: whether the LAST ensure_base_ref_current cycle actually confirmed the tracking
#: ref it compares against reflects GitHub's main -- i.e. the outer (mirror <-
#: GitHub) hop succeeded AND the inner hop actually refetched rather than trusting
#: a possibly-stale tracking ref inside BASE_REF_TRACKING_STALENESS_BOUND_S.
#: schedule_status.describe_base_ref_status reads this back so the operator-facing
#: render can say "not checked" instead of asserting a comparison against GitHub
#: that never happened this cycle (05a884ff's 2026-09-17 incident: base_ref_stale
#: and base_ref_wrong both read false for hours over a base nobody had compared to
#: GitHub at all).
BASE_REF_CHECK_STATE_PATH_ENV = "FACTORY_BASE_REF_CHECK_STATE_PATH"


def _base_ref_check_state_path() -> Path:
    return Path(
        os.environ.get(
            BASE_REF_CHECK_STATE_PATH_ENV,
            str(Path.home() / ".factory-dispatcher" / "base-ref-check-state.json"),
        )
    )


@dataclass(frozen=True)
class BaseRefCheckRecord:
    """Whether the last recorded ``ensure_base_ref_current`` cycle confirmed the
    tracking ref it compares the base ref against actually reflects GitHub's
    ``main`` -- see ``_record_base_ref_authoritative_check``. ``reason`` is set
    only when ``checked`` is False, naming which of the two hops did not confirm
    it (or that no cycle has ever recorded anything on this host)."""

    checked: bool
    reason: str = ""


def _write_base_ref_check_record(*, checked: bool, reason: str) -> None:
    path = _base_ref_check_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": checked, "reason": reason}))
    except OSError:
        pass


def read_base_ref_check_record() -> BaseRefCheckRecord:
    """Best-effort read of the last recorded authoritative base-ref check.

    Missing or unreadable state (no drain has run on this host yet, the file is
    corrupt, ...) reads as ``checked=False`` -- a check that was never recorded
    must never be asserted as "checked and fine", matching every other best-effort
    read in this area (``BaseRefLiveStatus.could_not_determine`` and friends).
    """
    try:
        doc = json.loads(_base_ref_check_state_path().read_text())
    except (OSError, ValueError):
        return BaseRefCheckRecord(checked=False, reason="no recorded base-ref check found")
    if not isinstance(doc, dict):
        return BaseRefCheckRecord(checked=False, reason="no recorded base-ref check found")
    return BaseRefCheckRecord(checked=bool(doc.get("checked")), reason=str(doc.get("reason") or ""))


def _record_base_ref_authoritative_check(
    *, mirror_sync_reason: str | None, tracking_ref_refreshed: bool
) -> None:
    """Persist whether this cycle's comparison is against a tracking ref confirmed
    fresh against GitHub -- called once per ``ensure_base_ref_current`` cycle, after
    both hops have resolved and before any branch that decides the drain's outcome,
    so recording this can never itself change which branch runs (AC-1).

    ``mirror_sync_reason`` set means the outer hop could not confirm the mirror
    against GitHub this cycle (mirror ahead, network blip, mirror's main checked
    out, ...) -- that reason wins outright, independent of the inner hop, since a
    tracking ref refreshed from an unconfirmed mirror proves nothing about GitHub.
    Otherwise, ``tracking_ref_refreshed=False`` means the inner hop skipped its own
    fetch inside the trust window: the comparison is against a tracking ref that
    might be up to ``BASE_REF_TRACKING_STALENESS_BOUND_S`` seconds old, so this
    reads "not checked" too rather than "healthy" -- a `healthy` reading must be
    reachable only when the comparison actually ran fresh this cycle.
    """
    if mirror_sync_reason is not None:
        reason = mirror_sync_reason
    elif not tracking_ref_refreshed:
        reason = (
            "tracking ref not refreshed this cycle -- inside the "
            f"{BASE_REF_TRACKING_STALENESS_BOUND_S}s trust window since the last fetch"
        )
    else:
        reason = ""
    _write_base_ref_check_record(checked=not reason, reason=reason)


#: Host-local execution state (Pillar 10, never bead content -- same reasoning as
#: failure_diagnosis._alert_state_path): how many CONSECUTIVE times in a row
#: ensure_base_ref_current has deferred for _other_clone_in_flight specifically, not
#: for any bead. Unlike trailing_environmental_fault_streak (bead notes), this cause
#: is not any one bead's fact -- the same shared repo_root/workdir_root condition can
#: defer a different bead's claim attempt every cycle, so a per-bead counter would
#: never accumulate and the breaker below would never trip (dev.task OPS-60: 188
#: refusals across a drain, none of them the same bead every time).
CONCURRENT_CLONE_DEFER_STATE_PATH_ENV = "FACTORY_CONCURRENT_CLONE_DEFER_STATE_PATH"


def _concurrent_clone_defer_state_path() -> Path:
    return Path(
        os.environ.get(
            CONCURRENT_CLONE_DEFER_STATE_PATH_ENV,
            str(Path.home() / ".factory-dispatcher" / "concurrent-clone-defer-state.json"),
        )
    )


def _read_concurrent_clone_defer_streak() -> int:
    try:
        doc = json.loads(_concurrent_clone_defer_state_path().read_text())
    except (OSError, ValueError):
        return 0
    streak = doc.get("streak") if isinstance(doc, dict) else None
    return streak if isinstance(streak, int) else 0


def _write_concurrent_clone_defer_streak(streak: int) -> None:
    path = _concurrent_clone_defer_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"streak": streak}))
    except OSError:
        pass


def _reset_concurrent_clone_defer_streak() -> None:
    if _read_concurrent_clone_defer_streak():
        _write_concurrent_clone_defer_streak(0)


def _record_concurrent_clone_defer(blocking_workdirs: tuple[str, ...]) -> int:
    """Bump the consecutive same-cause defer streak; raise the declared alert once it
    crosses the bound (retry_policy.CONSECUTIVE_CONCURRENT_CLONE_DEFER_LIMIT).

    A deferral this platform's own doctrine already calls transient
    ("clears on its own next cycle" -- ensure_base_ref_current's prior docstring)
    stops being a safe thing to stay silent about once it recurs past the bound: that
    is exactly the condition OPS-60 found only because a person happened to ask why
    nothing was being picked up. Returns the new streak for the caller to log.
    """
    streak = _read_concurrent_clone_defer_streak() + 1
    _write_concurrent_clone_defer_streak(streak)
    limit = retry_policy.CONSECUTIVE_CONCURRENT_CLONE_DEFER_LIMIT
    if streak >= limit:
        failure_diagnosis.announce_concurrent_clone_defer_wedged(
            blocking_workdirs, streak, limit
        )
    return streak


def ensure_base_ref_current(
    cfg: Config,
    *,
    exclude_workdir: Path | None = None,
    force_tracking_refresh: bool = False,
) -> BaseRefStatus:
    """Fast-forward a base ref that is safely behind; refuse everything else, loudly.

    ``force_tracking_refresh`` (dev.finding 5170b3f9a) forwards straight to the
    OPENING call to ``_refresh_stale_tracking_ref`` below as its ``force=`` --
    no other line of this function's branching changes. Every existing caller
    (``make_clone``, called once per dispatch) omits it and keeps today's
    behaviour: the opening refresh still respects
    ``BASE_REF_TRACKING_STALENESS_BOUND_S``. The one caller that passes
    ``True`` is the scheduled worker-revision-drift tick
    (``activities/worker_revision_drift.py::bring_base_ref_current``), which
    runs on its own 15-minute cadence rather than only when a bead is
    dispatched, and needs this cycle's comparison to be against a tracking
    ref it can prove reflects GitHub's ``main`` right now, not one that might
    still be sitting inside the trust window from a dispatch run 14 minutes
    ago. The ``status.wrong`` branch's own re-measure keeps forcing
    unconditionally either way, exactly as before.

    Checks ``status.wrong`` (``BaseRefStatus.wrong`` -- local-only commits absent
    from the tracking upstream) BEFORE ``status.behind_upstream``, and independently
    of it: a base ref that is not behind at all (``upstream_only == 0``) can still be
    wrong, if it is ahead of or diverged from its upstream rather than equal to it --
    exactly the 2026-09-17 shape (an operator repo's local ``main`` pointed at an
    unmerged pull request's branch commit, with nothing the tracking remote had that
    the local ref lacked). The healthy-path early return used to run first and skip
    the local-only check entirely whenever ``upstream_only == 0``, so that shape read
    healthy. Wrong or diverged always refuses and raises the declared alert -- no
    fast-forward is attempted, because none would fix it.

    "Safely behind" (the one case eligible for a fast-forward) means ``wrong`` False
    AND ``local_only == 0`` -- the refusal this replaces had already computed that a
    fast-forward loses nothing local -- AND no working-tree obstruction
    (``_fast_forward_base_ref``'s own check) AND no other dispatch clone in flight
    against the same ``cfg.repo_root`` (``_other_clone_in_flight``). Any of those failing
    refuses exactly as before: ``BaseRefStaleError``, the same message shape
    ``record_stale_base_ref_fault`` and ``is_stale_base_ref_reason`` already key off of.
    A blocked working tree additionally raises the declared alert first
    (``failure_diagnosis.announce_base_ref_needs_person``) -- that genuinely needs a
    person immediately, same as ``wrong``. A concurrent clone is different: ONE
    deferral for it is not a person's problem (the next attempt, once the concurrent
    clone finishes, finds the checkout free) -- but ``_record_concurrent_clone_defer``
    tracks how many CONSECUTIVE cycles hit this exact cause, and once that streak
    crosses the declared bound, it stops being "clears on its own" and becomes its own
    alert (OPS-60): a deadlock where the "other clone" was never actually live looks
    identical to this one cycle to cycle, and only a bound makes the difference
    visible.

    Syncs the source mirror's own ``main`` from its canonical remote first
    (``_sync_source_mirror_main``) -- the outer hop OPS-59 adds -- so the fast-forward
    below has a chance to reflect GitHub's ``main``, not just the mirror's last
    manually-fetched state. Called first and unguarded: since dev.finding ac4e8569
    (2026-09-24), that outer hop itself raises ``SourceMirrorBaseRefWrongError`` when
    GitHub confirms the mirror is wrong (local-only commits on its ``main``), and
    nothing below this call runs in that case -- no inner-hop fetch, no fast-forward,
    no comparison against ``cfg.repo_root``'s own tracking ref at all. Every other
    outcome of that hop (GitHub unreachable, the mirror's ``main`` checked out, an
    unconfigured mirror) still returns a reason string exactly as before.

    Records, as a side effect, whether this cycle's comparison is against a
    tracking ref confirmed fresh against GitHub (``_record_base_ref_authoritative_check``)
    -- purely observational, read back by ``schedule_status.describe_base_ref_status``;
    it never changes which branch below runs or whether this function returns or
    raises via that branching (AC-1 of the bead that split the rendering half of
    05a884ff out) -- the new outer-hop raise above is the one exception, recorded via
    its own inline write before it propagates, since it never reaches this branching
    at all.
    """
    mirror_sync_reason = _sync_source_mirror_main(cfg)
    tracking_ref_refreshed = _refresh_stale_tracking_ref(cfg, force=force_tracking_refresh)
    status = base_ref_status(cfg)

    if status.wrong:
        # GATE FINDING 1 (#925): DO NOT ACCUSE ON A TRACKING REF WE NEVER REFRESHED.
        # `worker_checkout.advance()` -- the one documented command for moving the
        # checkout, run after every merge -- does `git fetch <source-path> main`,
        # which is a PATH fetch: it writes FETCH_HEAD and NEVER updates
        # refs/remotes/origin/main. It then `git branch -f main <mirror tip>`. So the
        # instant a routine advance succeeds, local main is ahead of a tracking ref
        # that was not moved, `local_only > 0`, and this branch reads WRONG on a
        # checkout that is perfectly correct.
        #
        # The staleness bound makes it worse rather than better: advance() has just
        # written FETCH_HEAD, so `_refresh_stale_tracking_ref` short-circuits for the
        # next BASE_REF_TRACKING_STALENESS_BOUND_S and cannot self-correct inside the
        # window. Measured at the gate immediately after a successful advance():
        #     checkout main = 1aa978eb   origin/main = 36570bb8
        #     local_only/upstream_only = 1/0        FETCH_HEAD age = 0.1s
        # and the refusal told the operator to `reset or rebase` that correct ref.
        #
        # So force one fetch and re-measure. `wrong` is the rare branch, the fetch is
        # bounded, and being wrong here costs an urgent page advising a DESTRUCTIVE
        # remedy -- the one kind of false positive worth a network round trip to
        # avoid. A ref that is genuinely wrong stays wrong across the refetch and
        # still refuses below; that case is the control, not an afterthought.
        tracking_ref_refreshed = _refresh_stale_tracking_ref(cfg, force=True)
        status = base_ref_status(cfg)

    _record_base_ref_authoritative_check(
        mirror_sync_reason=mirror_sync_reason,
        tracking_ref_refreshed=tracking_ref_refreshed,
    )

    if status.wrong:
        _reset_concurrent_clone_defer_streak()
        failure_diagnosis.announce_base_ref_needs_person(
            status.base_ref,
            status.local_rev,
            status.upstream_ref,
            status.upstream_rev,
            f"{status.local_only} local-only commit(s) not on {status.upstream_ref}; "
            f"{status.local_rev} is not an ancestor-or-equal of it -- a fast-forward "
            "would not fix this, it must be repointed.",
        )
        raise BaseRefStaleError(status)

    if not status.behind_upstream:
        _reset_concurrent_clone_defer_streak()
        return status

    blocking = _other_clone_in_flight(cfg, exclude_workdir=exclude_workdir)
    if blocking:
        # Not necessarily a person's problem on its own -- the next dispatch attempt,
        # once this concurrent clone finishes, finds the checkout free and either
        # fast-forwards or refuses then, same shape record_stale_base_ref_fault
        # already retries for free. _record_concurrent_clone_defer is what notices
        # when that stops being true.
        _record_concurrent_clone_defer(blocking)
        raise BaseRefStaleError(status)
    _reset_concurrent_clone_defer_streak()

    try:
        _fast_forward_base_ref(cfg, status)
    except BaseRefFastForwardBlockedError as exc:
        failure_diagnosis.announce_base_ref_needs_person(
            status.base_ref, status.local_rev, status.upstream_ref, status.upstream_rev,
            str(exc),
        )
        raise BaseRefStaleError(status) from exc

    refreshed = base_ref_status(cfg)
    print(
        f"  base ref fast-forwarded: {status.base_ref} {status.local_rev} -> "
        f"{refreshed.local_rev} (was {status.upstream_only} commit(s) behind "
        f"{status.upstream_ref})"
    )
    return refreshed


def ensure_worker_checkout_is_current(cfg: Config) -> None:
    """Refuse to clone from a checkout confirmed behind ``cfg.base_ref``.

    Beside ``ensure_base_ref_current``, not instead of it: that check catches
    a local base branch behind the remote it tracks; this one catches the
    checkout's own revision sitting on a valid-but-outdated ancestor of that
    branch (worker_revision.check_checkout_freshness's docstring has the
    incident). Local import: ``worker_revision`` imports from this module at
    module scope (for ``DEFAULT_BASE_REF``/``GIT``/``run``), so importing it
    back at module scope here would be circular -- the same reason
    ``worker.py``'s ``ensure_required_executables`` imports ``dispatch``
    lazily instead.
    """
    import worker_revision

    try:
        worker_revision.ensure_checkout_is_current_with_main(
            repo_root=cfg.repo_root, main_ref=cfg.base_ref
        )
    except worker_revision.WorkerCheckoutStaleError as exc:
        raise WorkerCheckoutStaleError(str(exc)) from exc


def make_clone(cfg: Config, dest: Path) -> None:
    """Clone the repo into ``dest`` with no filesystem path back to the origin.

    Objects come from the local repo (fast, no network), but ``origin`` is then
    rewritten to the GitHub URL and the alternates/objects borrowing is severed,
    so nothing inside the clone references the real working tree.
    """
    ensure_base_ref_current(cfg, exclude_workdir=dest.parent)
    ensure_worker_checkout_is_current(cfg)
    run(
        [
            GIT,
            "clone",
            "--no-hardlinks",
            "--branch",
            cfg.base_ref,
            "--single-branch",
            str(cfg.repo_root),
            str(dest),
        ],
        timeout=300,
    )
    run([GIT, "remote", "set-url", "origin", cfg.remote], cwd=dest)
    # The Amendment 30 persona protocol instructs the worker to write
    # .factory/design.md inside its workspace, and on 2026-08-18 the scope
    # guard convicted that exact file after a full worker run had been paid
    # for (dev.task 07a99270). Protocol artifacts are the dispatcher's side
    # channel, not the work: excluding them in the clone's own git config
    # makes changed_paths (whose untracked listing honors
    # --exclude-standard), the scope guard, and the commit path's
    # `git add -A` all ignore them through ONE mechanism, so the file can
    # never reach a PR either. The design-note lift is unaffected — it
    # reads the file straight from disk.
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as handle:
        handle.write("\n# factory dispatcher protocol artifacts (dev.task 07a99270)\n.factory/\n")
    verify_clone_isolated(dest, cfg.repo_root)
    record_clone_git_control(dest)


def verify_clone_isolated(clone: Path, repo_root: Path) -> None:
    """Fail loudly if anything in the clone still points at the real repo.

    The whole isolation argument rests on this, so it is asserted rather than
    assumed. ``--no-hardlinks`` copies objects but git still writes the source
    path into ``origin``; an alternates file would be worse.
    """
    alternates = clone / ".git" / "objects" / "info" / "alternates"
    if alternates.exists():
        raise DispatchError(
            f"clone borrows objects via {alternates} — not isolated from {repo_root}"
        )

    config_text = (clone / ".git" / "config").read_text()
    if str(repo_root) in config_text:
        raise DispatchError(
            f"clone .git/config still references {repo_root} — isolation not established"
        )


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class WorkerResult:
    exit_code: int
    stdout: str
    duration_s: float
    timed_out: bool
    stderr: str | None = None
    cost_usd: float | None = None
    # None when the worker's result carried no usage figures to sum — distinct
    # from 0, which would claim a measured run that spent nothing.
    tokens: int | None = None


CAPACITY_LIMIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:you(?:'ve| have) hit your (?:usage|session) limit|usage limit has been reached)\b",
            re.IGNORECASE,
        ),
        "usage limit exhaustion",
    ),
    (
        re.compile(
            r"\b(?:exceeded your current quota|insufficient quota|quota (?:is )?exhausted)\b",
            re.IGNORECASE,
        ),
        "quota exhaustion",
    ),
)
RETRY_AT_PATTERN = re.compile(r"\btry again at\s+([^\r\n]+)", re.IGNORECASE)
# dev.finding 69016c99: the claude CLI's own wording is "resets <time>", not
# "try again at <x>". Consulted ONLY from _matches_capacity_pattern, after a
# cause has already been found in an already-restricted envelope -- never
# folded into RETRY_AT_PATTERN, because that pattern's other consumer,
# _is_retry_line, is a `search` that _capacity_error_envelope's legacy
# fallback uses to decide a line is eligible at all. Sharing the pattern would
# make any line merely containing "resets" envelope-eligible -- the same
# catch-all AC-2 forbids for the capacity noun itself.
RESETS_AT_PATTERN = re.compile(r"\bresets\s+([^\r\n]+)", re.IGNORECASE)
# 2026-08-18: a revoked credential produced "Failed to authenticate. API Error:
# 401 ... authentication_error", and the dispatcher's ordinary nonzero-exit path
# spent the retry budget on it three times in eleven minutes (dev.task
# ea4d6a93) before any workspace diff could have existed. Both signals are
# required so a test asserting an unrelated HTTP 401, or a diff that merely
# quotes the phrase, is not misread as this.
#
# 2026-08-19 (bead a3c2ad3b): an expired subscription OAuth token produces the
# claude CLI's own prose instead — "OAuth access token has expired" / "Re-
# authenticate to continue" — with no "authentication_error" token anywhere, so
# the pattern above missed it and the 401 burned an attempt as ordinary work.
# The classifier keys on the failure, not on one credential backend's spelling
# of it: any of these phrasings, still gated on the literal "401" below, is an
# authentication failure. "failed to authenticate" is included because it is
# the wrapper prose common to both the revoked-key and OAuth-expiry shapes.
AUTHENTICATION_ERROR_PATTERN = re.compile(
    r"\bauthentication_error\b"
    r"|\boauth access token has expired\b"
    r"|\bre-authenticate to continue\b"
    r"|\bfailed to authenticate\b",
    re.IGNORECASE,
)
HTTP_401_PATTERN = re.compile(r"\b401\b")
CLI_ERROR_LINE_PATTERN = re.compile(r"^\s*(?:error|fatal)\b\s*:?\s*", re.IGNORECASE)
IGNORABLE_CLI_WRAPPER_LINE_PATTERN = re.compile(
    r"^\s*(?:worker exited\s+-?\d+|"
    r"warning: .*|"
    r"reading additional input from stdin\.\.\.)\s*$",
    re.IGNORECASE,
)
SENSITIVE_ENV_NAME_PATTERN = process_env.SENSITIVE_NAME_PATTERN
WORKER_OUTPUT_TAIL_CHARS = 400


def _clean_retry_at(value: str) -> str:
    """Trim sentence wrappers while preserving the worker's human time string."""
    return value.strip().strip(" .;")


def _matches_capacity_pattern(output: str) -> tuple[str | None, str | None]:
    cause = None
    for pattern, label in CAPACITY_LIMIT_PATTERNS:
        if pattern.search(output):
            cause = label
            break
    if cause is None:
        return None, None

    retry_match = RETRY_AT_PATTERN.search(output) or RESETS_AT_PATTERN.search(output)
    retry_at = _clean_retry_at(retry_match.group(1)) if retry_match else None
    return cause, retry_at


def _is_bare_capacity_line(line: str) -> bool:
    stripped = line.strip()
    return any(pattern.match(stripped) for pattern, _label in CAPACITY_LIMIT_PATTERNS)


def _is_retry_line(line: str) -> bool:
    return RETRY_AT_PATTERN.search(line) is not None


def _capacity_error_envelope(output: str) -> str:
    """Return only CLI-wrapper failure text eligible for capacity matching.

    A worker CLI's plain output mixes the agent transcript with wrapper
    diagnostics. Provider failures are treated as capacity only when they
    appear in an explicit CLI error line, or when legacy output consists
    solely of wrapper boilerplate plus the provider capacity line. A
    transcript, diff, file dump, or test log line that merely quotes the same
    words is intentionally ignored.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""

    for index, line in enumerate(lines):
        if CLI_ERROR_LINE_PATTERN.match(line):
            candidate_lines = [line]
            for following in lines[index + 1 :]:
                if _is_bare_capacity_line(following) or _is_retry_line(following):
                    candidate_lines.append(following)
                    continue
                break
            envelope = "\n".join(candidate_lines)
            cause, _retry_at = _matches_capacity_pattern(envelope)
            if cause:
                return envelope

    semantic_lines = [
        line
        for line in lines
        if not IGNORABLE_CLI_WRAPPER_LINE_PATTERN.match(line)
    ]
    if semantic_lines and all(
        _is_bare_capacity_line(line) or _is_retry_line(line)
        for line in semantic_lines
    ):
        return "\n".join(semantic_lines)

    return ""


def detect_capacity_failure(output: str) -> CapacityFailure | None:
    """Detect exhausted agent capacity from CLI-wrapper error text.

    Non-zero exit status is intentionally ignored here. The branch only opens on
    explicit provider-style usage/quota wording so ordinary worker failures
    still count against the task.
    """
    envelope = _capacity_error_envelope(output)
    if not envelope:
        return None

    cause, retry_at = _matches_capacity_pattern(envelope)
    if cause is None:
        return None

    return CapacityFailure(cause=cause, retry_at=retry_at)


def detect_worker_capacity_failure(result: WorkerResult) -> CapacityFailure | None:
    """Detect capacity from the worker process boundary, not the transcript.

    stderr is checked first: it is the narrowest reliable source for a worker
    CLI's wrapper failures. But for a JSON-graded worker (`grade_json_result`,
    `_grade_json_worker_result`) stderr is always a grading message -- "worker
    emitted no parseable result JSON", or the result document's `subtype` --
    never the CLI's own prose, which lands in stdout instead. dev.finding
    69016c99: six retry attempts were burned on a claude CLI session-limit
    exhaustion because the old `stderr is None` fallback never reached that
    stdout text. stdout is now also consulted whenever stderr carries no
    capacity signal of its own -- empty, missing, or simply not a match --
    through the same strict envelope filter `detect_capacity_failure` already
    applies to either stream, so what counts as an eligible line is unchanged.
    """
    if result.stderr:
        capacity = detect_capacity_failure(result.stderr)
        if capacity:
            return capacity
    return detect_capacity_failure(result.stdout)


def detect_authentication_failure(output: str) -> bool:
    """Detect a worker CLI authentication failure (HTTP 401) in worker output.

    Matches any known phrasing of the failure — a revoked API key's
    ``authentication_error`` token, or an expired OAuth token's CLI prose —
    since the credential backend that produced the 401 is not load-bearing.
    """
    return bool(AUTHENTICATION_ERROR_PATTERN.search(output)) and bool(
        HTTP_401_PATTERN.search(output)
    )


def detect_worker_authentication_failure(result: WorkerResult) -> bool:
    """Detect an authentication failure from the worker's combined output.

    Unlike capacity detection, this needs no CLI-wrapper envelope filtering:
    the caller only treats it as environmental when no workspace diff exists,
    which already excludes the case a diff or transcript merely quotes the
    phrase.
    """
    combined = result.stdout + (result.stderr or "")
    return detect_authentication_failure(combined)


def redact_sensitive_output(output: str) -> str:
    """Remove known environment secret values before writing worker output."""
    redacted = output
    secret_values = sorted(
        {
            value
            for name, value in os.environ.items()
            if value and process_env.is_credential_name(name)
        },
        key=len,
        reverse=True,
    )
    for value in secret_values:
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def worker_output_tail(output: str, limit: int = WORKER_OUTPUT_TAIL_CHARS) -> str:
    """Return a bounded, redacted tail of worker output for diagnostics."""
    return redact_sensitive_output(output).strip()[-limit:]


def failure_reason_with_worker_output(reason: str, result: WorkerResult | None) -> str:
    """Attach the worker's bounded output tail unless the reason already carries it."""
    safe_reason = redact_sensitive_output(reason)
    if result is None:
        return safe_reason
    tail = worker_output_tail(result.stdout)
    if not tail or tail in safe_reason:
        return safe_reason
    return f"{safe_reason}\nWorker output (tail): {tail}"


#: 2026-08-19: bead 07a99270's worker verified the defect was already fixed by
#: a merged PR, changed nothing, and said so plainly. The dispatcher still
#: recorded a generic work failure and Temporal retried the same rediscovery
#: until an operator terminated it. Matched only against a changed-nothing
#: run's own stdout — the worker is the one entity that saw the acceptance
#: criteria and the repo state together, so its declaration, not a deeper
#: verification, is what routes the run to operator disposition.
ALREADY_SATISFIED_CLAIM_PATTERN = re.compile(
    r"already\s+(?:been\s+)?(?:met|satisfied|fixed|resolved|addressed|implemented)\b",
    re.IGNORECASE,
)


def detect_already_satisfied_claim(output: str) -> bool:
    """Whether the worker's own output claims the task is already satisfied."""
    return bool(ALREADY_SATISFIED_CLAIM_PATTERN.search(output))


def changed_nothing_failure_reason(
    result: WorkerResult, *, preserved_baseline_applied: bool = False
) -> str:
    if detect_already_satisfied_claim(result.stdout):
        reason = (
            f"{retry_policy.ALREADY_SATISFIED_WORK_MARKER} worker made no "
            "changes and its output declared the task already satisfied"
        )
    else:
        reason = "worker changed nothing"
    if preserved_baseline_applied:
        # On BOTH branches (the #805 gate: three of the five live refusals that
        # motivated OPS-117 took the plain branch). The baseline this run
        # started from is itself a closed prior attempt, not this attempt's
        # own work -- see guards.resume_context for what the actual task is (a
        # request-changes review or a reasoned requeue), rendered at the top
        # of the next brief this reason feeds via prior_failures.
        reason += (
            "; this run started from an applied preserved baseline, which "
            "is a closed prior attempt and not this attempt's own work -- "
            "the task is whatever a request-changes review note or a "
            "reasoned requeue on this bead says, not re-verifying that "
            "baseline"
        )
    return failure_reason_with_worker_output(reason, result)


def raise_for_worker_failure(
    result: WorkerResult, budget_minutes: int, clone: Path | None = None
) -> None:
    """Raise the right dispatcher error for an unsuccessful worker result.

    ``clone`` lets the authentication branch below confirm no workspace diff
    exists before downgrading a 401 to an environment failure. A caller that
    genuinely cannot supply it does not get that branch silently skipped: the
    raised failure note names the skip instead, so the downgrade never
    happens invisibly.
    """
    if result.timed_out:
        raise DispatchError(
            failure_reason_with_worker_output(
                f"worker exceeded its {budget_minutes}m budget",
                result,
            )
        )
    if result.exit_code != 0:
        capacity = detect_worker_capacity_failure(result)
        if capacity:
            raise DispatchCapacityError(capacity)
        if detect_worker_authentication_failure(result):
            if clone is None:
                raise DispatchError(
                    failure_reason_with_worker_output(
                        f"worker exited {result.exit_code}: authentication "
                        "failure detected but not classified as environmental "
                        "- no clone was supplied to check for a workspace diff",
                        result,
                    )
                )
            # changed_paths is called lazily, only once an authentication
            # signal is present, so ordinary nonzero-exit failures never touch
            # the clone here (dev.task ea4d6a93: the failure preceded any
            # workspace diff).
            if not changed_paths(clone):
                raise DispatchEnvironmentError(
                    failure_reason_with_worker_output(
                        "worker failed to authenticate (401) before any workspace "
                        "diff existed",
                        result,
                    )
                )
        raise DispatchError(
            f"worker exited {result.exit_code}: {worker_output_tail(result.stdout)}"
        )


ARCHITECT_PERSONA = "architect-sme.md"
POLECAT_PERSONA = "polecat-developer.md"

# SRE finding 2026-09-10 (dev.task 4f24656a): a worker redirected its own
# `git diff` to a file so it could read its work back, had no declared place
# to put it, wrote `.review.diff` at the repository root instead, and the
# scope guard - correctly - discarded an otherwise complete and correct run.
# TMPDIR is already the worker's scratch location: run_worker (below) points
# it at containment.prepare_containment's tmpdir, a directory the dispatcher
# creates NEXT TO the clone, never inside it, so anything written there is
# structurally invisible to changed_paths/check_scope (both walk the clone's
# own git state, never that sibling directory) - see
# test_prepare_writes_profile_and_tmp_beside_workspace. The worker was simply
# never told this location existed. Naming it once, here, means the prompt
# text that tells the worker about it (_worker_scratch_instruction) and the
# code that actually sets it (run_worker) read the same value and cannot
# drift apart.
WORKER_SCRATCH_ENV_VAR = "TMPDIR"


def _worker_scratch_instruction() -> str:
    """Prompt text pointing the worker at its declared scratch location.

    Deliberately part of every personas-aware prompt (assembled once, at the
    top level) rather than persona-specific text: for a behavioral task the
    architect and developer personas share one prompt and one worker
    invocation, so a single instruction here reaches both without duplicating
    it into docs/agents/architect-sme.md too.
    """
    return (
        "Ephemera you write only to do the work - a self-review diff, a "
        "scratch note, anything you do not intend as part of the change - "
        f"belongs in the directory named by the ${WORKER_SCRATCH_ENV_VAR} "
        "environment variable, which this run sets outside this repository "
        "checkout for exactly that purpose. Writing it anywhere inside this "
        "checkout instead, including the repository root, ends the run as a "
        "scope violation and discards the work, complete or not."
    )


def architect_predicate(content: dict) -> bool:
    """Amendment 30 / PR-5 decision: design judgment joins the run when the
    change is behavioral or the bead already carries architectural signals."""
    return (
        content.get("risk_class") == "behavioral"
        or bool(content.get("arch_impact"))
        or bool(content.get("nfrs"))
    )


def _persona_body(clone: Path, name: str) -> str:
    reason = containment.persona_source_reason(clone, name)
    if reason is not None:
        raise DispatchEnvironmentError(
            f"persona definition refused: docs/agents/{name}: {reason} - the "
            "tested tree must carry its own personas (Amendment 30 PR-5)"
        )
    text = containment.persona_source(clone, name).read_text()
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        _, _, rest = rest.partition("---")
        return rest.strip()
    return text.strip()


def assemble_personas_prompt(
    clone: Path,
    content: dict,
    principles: Iterable[tuple[str, str]] = (),
) -> str:
    """The system-prompt displacement text for a personas-aware worker."""
    parts = [_persona_body(clone, POLECAT_PERSONA), _worker_scratch_instruction()]
    if architect_predicate(content):
        parts.insert(
            0,
            "Before implementing, act as the Architect/SME persona below: record "
            "your design judgment and any derived NFRs by writing the file "
            ".factory/design.md at the repository root (inside your workspace); "
            "the dispatcher attaches it to the bead. Then implement as the "
            "developer persona.\n\n" + _persona_body(clone, ARCHITECT_PERSONA),
        )
    doctrine_section = guards.render_doctrine_section(principles)
    if doctrine_section:
        parts.append(doctrine_section)
    return "\n\n---\n\n".join(parts)


def materialize_personas(clone: Path) -> None:
    """Copy personas from the clone's docs/agents/ in-process (bd4b2a9a): running
    the clone's own materializer would run worker-controlled code as this user."""
    reason = containment.materialize_personas(clone, (POLECAT_PERSONA, ARCHITECT_PERSONA))
    if reason is not None:
        raise DispatchEnvironmentError(f"persona materialization refused: {reason}")


def prepare_worker_argv(
    worker_argv: tuple[str, ...],
    clone: Path,
    task_content: dict,
    uses_personas: bool,
    principles: Iterable[tuple[str, str]] = (),
) -> tuple[str, ...]:
    if not uses_personas:
        return worker_argv
    materialize_personas(clone)
    prompt = assemble_personas_prompt(clone, task_content, principles)
    return (*worker_argv, "--append-system-prompt", prompt)


def _spend_ledger_path() -> Path:
    return Path(
        os.environ.get(
            "FACTORY_SPEND_LEDGER",
            str(Path.home() / ".factory-dispatcher" / "spend.json"),
        )
    )


def read_daily_spend_usd(today: str | None = None) -> float:
    """Cumulative recorded worker spend for today, from the host-local ledger.

    Execution state, host-local by design - never bead content (Pillar 10).
    """
    today = today or time.strftime("%Y-%m-%d")
    path = _spend_ledger_path()
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0.0
    return float(doc.get(today, 0.0))


def record_spend_usd(amount: float, today: str | None = None) -> None:
    if not amount:
        return
    today = today or time.strftime("%Y-%m-%d")
    path = _spend_ledger_path()
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        doc = {}
    doc[today] = float(doc.get(today, 0.0)) + float(amount)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def budget_floor_reason() -> str | None:
    """Non-None when today's spend has reached the daily cap from the env.

    Amendment 30 PR-7: the stated degraded mode - the factory yields to the
    outer loop. No cap configured means no floor enforced.
    """
    cap = os.environ.get("FACTORY_DAILY_USD_CAP")
    if not cap:
        return None
    try:
        cap_value = float(cap)
    except ValueError:
        return f"FACTORY_DAILY_USD_CAP is not a number: {cap!r}"
    spent = read_daily_spend_usd()
    if spent >= cap_value:
        return (
            f"daily worker spend {spent:.2f} USD has reached the cap "
            f"{cap_value:.2f} USD; skipping the drain so the factory yields "
            "to the outer loop"
        )
    return None


def effective_budget_minutes(content: dict[str, Any]) -> int:
    """The worker time budget a run actually gets, capped below the orphan
    sweep's age threshold.

    A task can declare budget.max_agent_minutes with no ceiling of its own.
    Both the CLI path and the scheduled path call this rather than reading
    max_agent_minutes directly, so a task cannot request a budget long enough
    to make a live run indistinguishable, by age alone, from one truly
    abandoned by a kill.
    """
    requested = int(
        (content.get("budget") or {}).get("max_agent_minutes") or DEFAULT_BUDGET_MINUTES
    )
    return min(requested, MAX_AGENT_MINUTES_CEILING)


def disk_floor_reason(path: Path) -> str | None:
    """Non-None when free space on ``path``'s filesystem is below the floor.

    Checked against the workdir root - the filesystem the ~1.2GB isolated
    clone actually lands on, and the one a leak fills. Naming the free space
    and the floor is the point: this is an environmental fault the operator
    can act on, not a verdict on whatever task was about to claim.
    """
    floor_gb = DEFAULT_DISK_FLOOR_GB
    raw = os.environ.get("FACTORY_DISK_FLOOR_GB")
    if raw:
        try:
            floor_gb = float(raw)
        except ValueError:
            return f"FACTORY_DISK_FLOOR_GB is not a number: {raw!r}"
    if floor_gb <= 0:
        return None
    try:
        free_gb = shutil.disk_usage(path).free / (1024**3)
    except OSError as exc:
        return f"could not read free disk space at {path}: {exc}"
    if free_gb < floor_gb:
        return (
            f"free disk at {path} is {free_gb:.2f} GiB, below the "
            f"{floor_gb:.2f} GiB floor (FACTORY_DISK_FLOOR_GB)"
        )
    return None


def _workdir_owner_marker_path(workdir: Path) -> Path:
    return workdir / ".owner-pid"


def _pid_start_time(pid: int) -> str | None:
    """The OS's own record of when ``pid`` started, in whatever format ``ps`` emits.

    Not parsed into a timestamp -- only ever compared for equality against a value
    recorded earlier by this same function, so an opaque, OS-native string is exactly
    as useful as a parsed one and carries no timezone/format assumptions across
    platforms. Exists so a marker can tell "the same process that wrote this marker"
    from "a different process that later reused this pid" (OPS-60): ``os.kill(pid, 0)``
    alone cannot distinguish the two. Returns ``None`` (never a stale answer) when
    ``ps`` cannot be asked -- callers then fall back to pid-alone liveness rather than
    trusting a start time this function did not actually confirm.
    """
    try:
        proc = run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


@dataclass(frozen=True)
class _WorkdirOwner:
    """Everything a workdir's marker records about the run that created it.

    ``workflow_id``/``workflow_run_id`` are set only for an activity-created workdir
    (``activities.dispatch_steps.isolate_activity``, which runs inside a Temporal
    activity) -- a CLI-driven run (``dispatch_once``/``verify_pristine_commands``) has
    no workflow to name, and stays on the pid+start-time identity below.
    """

    pid: int
    pid_started_at: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None

    def to_json(self) -> str:
        doc: dict[str, Any] = {"pid": self.pid}
        if self.pid_started_at is not None:
            doc["pid_started_at"] = self.pid_started_at
        if self.workflow_id:
            doc["workflow_id"] = self.workflow_id
        if self.workflow_run_id:
            doc["workflow_run_id"] = self.workflow_run_id
        return json.dumps(doc)


def _parse_workdir_owner(text: str) -> "_WorkdirOwner | None":
    """Parse a marker's content, honoring the pre-OPS-60 bare-pid shape too.

    A marker written before this fix (or one this ``ps``-less host wrote before
    ``pid_started_at`` could be recorded) is just ``str(pid)`` -- a bare integer is
    valid JSON (a number, not an object), so that shape is recognised explicitly
    rather than mistaken for a corrupt marker.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        doc = json.loads(stripped)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        try:
            pid = int(doc["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        return _WorkdirOwner(
            pid=pid,
            pid_started_at=doc.get("pid_started_at"),
            workflow_id=doc.get("workflow_id") or None,
            workflow_run_id=doc.get("workflow_run_id") or None,
        )
    try:
        return _WorkdirOwner(pid=int(stripped))
    except ValueError:
        return None


def _read_workdir_owner(workdir: Path) -> "_WorkdirOwner | None":
    try:
        text = _workdir_owner_marker_path(workdir).read_text()
    except OSError:
        return None
    return _parse_workdir_owner(text)


def mark_workdir_owner(
    workdir: Path,
    *,
    workflow_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    """Record this run as owning ``workdir``, for a later liveness check to ask about.

    Written right after mkdtemp, before anything else touches the directory: a check
    that finds no marker treats the workdir as ownerless rather than live, so the
    marker has to exist before there is anything worth protecting.

    The marker identifies the RUN, not merely the process (OPS-60): a Temporal
    activity's worker daemon is long-lived and outlives every run it ever executes, so
    stamping only its pid made every workdir a finished run ever touched read "owner
    alive" forever. ``workflow_id``/``workflow_run_id`` (passed by
    ``isolate_activity``) let ``workdir_owner_is_alive`` ask Temporal whether that
    specific run is still executing, rather than the process table whether the daemon
    is. The pid is still recorded, paired with the OS's own start-time for it, as the
    identity a CLI-driven run (which has no workflow) is checked by instead.
    """
    pid = os.getpid()
    owner = _WorkdirOwner(
        pid=pid,
        pid_started_at=_pid_start_time(pid),
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
    )
    try:
        _workdir_owner_marker_path(workdir).write_text(owner.to_json())
    except OSError:
        pass


def _pid_is_alive(pid: int) -> bool:
    """Liveness by signal, not by whether ``pid``'s ``finally`` block ran.

    ``os.kill(pid, 0)`` sends no signal, only probes - and it answers
    identically whether the process is about to exit cleanly, is hung, or was
    already ``kill -9``'d. That is the property a lockfile/flag-file
    convention lacks: it needs no cooperation from the process being checked,
    which is the whole reason a killed process's leak is otherwise invisible.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def workdir_owner_is_alive(workdir: Path) -> bool:
    """Whether the RUN that created ``workdir`` is still live -- not merely its process.

    Two identities, two liveness checks (OPS-60). An activity-created workdir's marker
    carries the Temporal workflow run that created it; liveness is then asked of
    Temporal ("is that run still executing"), via ``temporal_workflow_run_is_alive``
    (a bare module-level call, resolved at call time, so a test's
    ``monkeypatch.setattr(dispatch, "temporal_workflow_run_is_alive", ...)`` reaches
    it) -- never of the process table, because the worker daemon process that hosts
    every activity invocation outlives every run it ever executes: the daemon's pid
    being alive proves nothing about whether any one run of it has finished. A
    CLI-created workdir (or a marker written before this fix) carries no workflow
    identity, so it falls back to the pid it was actually marked with, matched against
    the OS's own recorded start time for that pid -- a recycled pid cannot vouch for a
    run that is actually long dead.

    A workdir with no readable/parseable owner marker is presumed dead:
    mark_workdir_owner writes the marker immediately after mkdtemp, before any real
    work starts, so a missing or unreadable marker means the owner never got that far -
    never a live run this check should protect.

    A Temporal lookup that cannot be completed is NOT treated as dead: the sweep and
    the concurrent-clone defer check must never reclaim a workdir they could not prove
    is no longer owned, so an inconclusive answer is "still alive."
    """
    owner = _read_workdir_owner(workdir)
    if owner is None:
        return False
    if owner.workflow_id and owner.workflow_run_id:
        try:
            return temporal_workflow_run_is_alive(owner.workflow_id, owner.workflow_run_id)
        except Exception as exc:  # noqa: BLE001 - an unprovable answer must not delete
            print(
                f"  WARNING: could not confirm Temporal liveness for workflow run "
                f"{owner.workflow_id}/{owner.workflow_run_id} owning {workdir}: {exc}; "
                "presuming alive rather than risk reclaiming a live run's workdir"
            )
            return True
    if not _pid_is_alive(owner.pid):
        return False
    if owner.pid_started_at is None:
        return True
    current_start = _pid_start_time(owner.pid)
    if current_start is None:
        return True
    return current_start == owner.pid_started_at


@dataclass(frozen=True)
class OrphanSweepResult:
    """What an orphan sweep did, so a recurring leak is visible rather than silently absorbed."""

    removed: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    failures: tuple[str, ...] = ()

    def describe(self) -> str:
        if not self.removed and not self.failures:
            return "no orphaned workdirs found"
        parts = []
        if self.removed:
            reclaimed_gb = self.reclaimed_bytes / (1024**3)
            parts.append(
                f"removed {len(self.removed)} orphaned workdir(s), reclaimed "
                f"{reclaimed_gb:.2f} GiB: {', '.join(self.removed)}"
            )
        if self.failures:
            parts.append(
                f"could not remove {len(self.failures)} workdir(s): "
                + "; ".join(self.failures)
            )
        return "; ".join(parts)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def sweep_orphan_workdirs(
    workdir_root: Path,
    threshold_minutes: int = DEFAULT_ORPHAN_WORKDIR_MINUTES,
) -> OrphanSweepResult:
    """Reclaim factory-* workdirs from runs whose ``finally`` never ran.

    FA-S6's clone-side gap: a worker killed mid-run (launchd restart, a
    kickstart after merge, tunnel loss) strands its isolated clone with
    nothing to revisit it, because the cleanup that would have removed it
    lives in the ``finally`` block the kill skipped. Called before every
    claim, so the leak is swept by the next dispatch rather than by nothing
    at all.

    A candidate is eligible only if it is older than ``threshold_minutes``
    AND its recorded owner is no longer alive - age alone would race a
    legitimately slow, still-live run past the threshold.
    """
    try:
        candidates = sorted(workdir_root.glob("factory-*"))
    except OSError as exc:
        return OrphanSweepResult(failures=(f"could not list {workdir_root}: {exc}",))

    cutoff = time.time() - threshold_minutes * 60
    removed: list[str] = []
    failures: list[str] = []
    reclaimed = 0

    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            if candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if workdir_owner_is_alive(candidate):
            continue
        size = _dir_size_bytes(candidate)
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        removed.append(str(candidate))
        reclaimed += size

    return OrphanSweepResult(tuple(removed), reclaimed, tuple(failures))


def cleanup_workdir(workdir: Path) -> None:
    """Remove a run's isolated workdir, and say so if it could not be removed.

    Not ``ignore_errors=True``: that discarded rmtree's own failures
    silently, so once the disk holding these workdirs fills, rmtree itself
    starts failing and nothing reported it - a second, quieter leak stacked
    on the first. This still must not raise: it runs from a ``finally`` that
    may already be propagating the run's real failure (or carrying its
    return value), and an exception raised here would replace that outcome
    rather than add to it.
    """
    try:
        shutil.rmtree(workdir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"  WARNING: could not remove workdir {workdir}: {exc}")


# The claude CLI's --output-format json result carries a `usage` object with
# these fields when it ran a model at all (cache fields are absent on a run
# that used no cache). Summed, they are the total tokens the run spent -
# input alone under-reports by the cache and output shares.
USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _sum_usage_tokens(usage: Any) -> int | None:
    """Sum a worker result's reported usage fields, or None if it reported none.

    None, not 0: a `usage` object absent or empty means the run's own result
    JSON gave no figure to measure, which is a different fact from a measured
    run that spent zero tokens.
    """
    if not isinstance(usage, dict):
        return None
    numeric = [
        usage[field]
        for field in USAGE_TOKEN_FIELDS
        if isinstance(usage.get(field), (int, float)) and not isinstance(usage.get(field), bool)
    ]
    if not numeric:
        return None
    return int(sum(numeric))


def _grade_json_worker_result(proc, duration_s: float) -> WorkerResult:
    """Grade a JSON-emitting CLI by its result document, not its exit code.

    Spike finding #15: for the claude CLI, "Not logged in" and
    error_during_execution both exit 0 - the exit status is not a health
    signal. The JSON's is_error field is; total_cost_usd and usage feed the
    spend ledger and provenance.
    """
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    try:
        doc = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return WorkerResult(
            exit_code=proc.returncode if proc.returncode != 0 else 1,
            stdout=stdout + stderr,
            duration_s=duration_s,
            timed_out=False,
            stderr=(stderr + "\nworker emitted no parseable result JSON").strip(),
        )
    is_error = bool(doc.get("is_error"))
    cost = doc.get("total_cost_usd")
    result_text = str(doc.get("result") or "")
    return WorkerResult(
        exit_code=1 if is_error else 0,
        stdout=(result_text or stdout) + stderr,
        duration_s=duration_s,
        timed_out=False,
        stderr=(str(doc.get("subtype") or "") if is_error else stderr) or "",
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        tokens=_sum_usage_tokens(doc.get("usage")),
    )


def run_worker(
    prompt: str,
    clone: Path,
    budget_minutes: int,
    worker_argv: tuple[str, ...],
    containment_allow: tuple[str, ...] = (),
    extra_env: tuple[tuple[str, str], ...] = (),
    grade_json_result: bool = False,
) -> WorkerResult:
    started = time.monotonic()
    # The worker binary is pre-checked so a missing executable stays a distinct
    # environmental fault (17.4) - once wrapped, subprocess would resolve
    # sandbox-exec instead and the miss would surface as an opaque exit code.
    if worker_argv and shutil.which(worker_argv[0]) is None:
        raise DispatchEnvironmentError(
            f"required executable not found on PATH: {worker_argv[0]}"
        )
    # Amendment 30 PR-6: every worker runs inside the dispatcher-owned OS
    # write boundary. Fail closed: a host without the wrapper runs nothing.
    profile, tmpdir = containment.prepare_containment(clone, containment_allow)
    wrapped = containment.contained_argv(worker_argv, profile)
    env, env_reason = containment.worker_environment(
        process_env.child_env(
            needs=("CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY", "SUBSTRATE_READ_API_KEY")
        ),
        ((WORKER_SCRATCH_ENV_VAR, str(tmpdir)), *extra_env),
    )
    if env_reason is not None:
        raise DispatchEnvironmentError(env_reason)
    try:
        proc = subprocess.run(
            [*wrapped, prompt],
            cwd=str(clone),
            capture_output=True,
            text=True,
            timeout=budget_minutes * 60,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError as exc:
        missing = wrapped[0] if wrapped else str(exc)
        raise DispatchEnvironmentError(
            f"required executable not found on PATH: {missing}"
        ) from exc
    except PermissionError as exc:
        missing = wrapped[0] if wrapped else str(exc)
        raise DispatchEnvironmentError(
            f"required executable is not executable: {missing}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return WorkerResult(
            exit_code=-1,
            stdout=partial,
            duration_s=time.monotonic() - started,
            timed_out=True,
            stderr=(
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
            or "",
        )
    if grade_json_result:
        return _grade_json_worker_result(proc, time.monotonic() - started)
    return WorkerResult(
        exit_code=proc.returncode,
        stdout=(proc.stdout or "") + (proc.stderr or ""),
        duration_s=time.monotonic() - started,
        timed_out=False,
        stderr=proc.stderr or "",
    )


def smoke_check_python(clone: Path, paths: list[str]) -> str | None:
    """Byte-compile changed Python. Returns an error string, or None if clean."""
    py_files = [p for p in paths if p.endswith(".py") and (clone / p).exists()]
    if not py_files:
        return None
    proc = run(
        [sys.executable, "-I", "-m", "py_compile", *py_files], cwd=clone, check=False
    )
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "py_compile failed").strip()[:500]
    return None


# Word-boundary, not substring: a command that happens to mention "ruff" inside
# an unrelated word should not be mistaken for a declared lint invocation.
_LINT_COMMAND_PATTERN = re.compile(r"\bruff\b")


def declares_lint_command(commands: Iterable[str]) -> bool:
    """Whether ``commands`` already runs ruff.

    Guards ``effective_verification_commands`` against appending a second
    lint pass onto a spec that already declares one (OPS-30's legal form,
    e.g. ``python -m ruff check apps/``) — a lint command that runs twice is
    exactly the kind of drift-prone duplication this fix must not add.
    """
    return any(_LINT_COMMAND_PATTERN.search(command) for command in commands)


def default_lint_command(clone: Path, paths: list[str]) -> str | None:
    """The pinned ruff check over this task's own changed Python paths.

    ``python -m ruff``, not a bare ``ruff`` binary: the clone-local venv
    ``bootstrap_verification_env`` builds is what has the pinned version
    installed (``verification_dependency_args`` already globs
    ``apps/factory-dispatcher/requirements.txt``, where OPS-30 pinned
    ``ruff==`` to match ``.github/workflows/lint.yml`` exactly — see
    ``tests/test_lint_dependency.py``). Filtered the same way
    ``smoke_check_python`` filters its own targets: only paths that still
    exist and end in ``.py`` — a deleted or non-Python changed path is not a
    lint target. Returns ``None`` when nothing changed qualifies, so a task
    that touched no Python gains no lint command at all.
    """
    py_files = sorted(p for p in paths if p.endswith(".py") and (clone / p).exists())
    if not py_files:
        return None
    # --force-exclude: explicitly-passed paths bypass ruff.toml's
    # extend-exclude by default, so without it a task touching the
    # deliberately-malformed scan fixtures (scripts/tests/fixtures/) is
    # refused for lint CI's directory-level invocation never imposes. The
    # flag makes exclusions apply to named paths too -- the exact check CI
    # runs, not a stricter cousin.
    return "python -m ruff check --force-exclude " + " ".join(
        shlex.quote(p) for p in py_files
    )


def effective_verification_commands(
    task: dict, clone: Path, paths: list[str]
) -> list[str]:
    """Declared verification, plus the pinned lint check no spec has to remember.

    Composed here rather than in ``file_task.py``'s ``DEFAULT_VERIFICATION``:
    that default only substitutes in for a spec that declares NO verification
    at all, but most lint-red work comes from a spec that declares its own
    commands (a pytest invocation, a check script) and simply never named
    ruff. Running here, for every task that reaches ``verify_activity``
    regardless of what it declares, is the level that catches every future
    spec instead of relying on each one to remember.

    Not used by ``preflight_activity``: that step verifies an unmodified
    clone before the worker has run, so there are no changed paths yet to
    scope a lint check to.
    """
    commands = list(declared_verification_commands(task))
    if declares_lint_command(commands):
        return commands
    lint_command = default_lint_command(clone, paths)
    if lint_command:
        commands.append(lint_command)
    return commands


@dataclass(frozen=True)
class VerificationCommandResult:
    command: str
    outcome: str
    exit_code: int | None
    output: str
    duration_s: float
    reason: str = ""

    @property
    def executed(self) -> bool:
        return self.outcome in {"passed", "failed"}


# pytest defers most warnings to a "warnings summary" block that prints AFTER
# the FAILURES section, not before it - so a plain last-N-chars tail slice
# keeps that trailing noise and still drops the failing assertion it was
# supposed to preserve. On 2026-08-18 that noise was pydantic deprecation
# warnings: the Temporal result, the worker log, and every substrate note on
# ea4d6a93 carried it while the failing assertion was elided, and diagnosis
# ran blind for an hour. Anchoring on whichever of these markers appears
# first and keeping forward from there puts the load-bearing detail back
# inside the record limit regardless of how much trailing noise follows it.
PYTEST_FAILURE_REGION_PATTERN = re.compile(
    r"^=+\s*(?:FAILURES|ERRORS|short test summary info)\s*=+$",
    re.MULTILINE,
)


def _bounded_verification_output(output: str, limit: int) -> str:
    """Bound ``output`` to ``limit`` chars without eliding a pytest failure.

    A plain tail slice can still drop the failing assertion when pytest's
    trailing warnings summary is long enough to push it out of the window.
    Anchor on the first FAILURES/ERRORS/short-summary marker and keep
    forward from there; fall back to a plain tail when no such marker is
    present, which preserves prior behaviour for non-pytest commands.
    """
    if len(output) <= limit:
        return output
    match = PYTEST_FAILURE_REGION_PATTERN.search(output)
    if match:
        return output[match.start():][:limit]
    return output[-limit:]


@dataclass(frozen=True)
class VerificationReport:
    commands: tuple[VerificationCommandResult, ...]
    bootstrap_note: str = ""

    @property
    def declared(self) -> bool:
        return bool(self.commands)

    @property
    def failed(self) -> tuple[VerificationCommandResult, ...]:
        return tuple(r for r in self.commands if r.outcome == "failed")

    @property
    def could_not_start(self) -> tuple[VerificationCommandResult, ...]:
        return tuple(r for r in self.commands if r.outcome == "could_not_start")

    @property
    def ok(self) -> bool:
        return not self.failed and not self.could_not_start

    def describe(self, output_limit: int = 500) -> str:
        if not self.commands:
            return "No verification commands declared."

        ran = [r for r in self.commands if r.executed]
        lines: list[str] = []
        if self.bootstrap_note:
            lines.append(f"Bootstrap: {self.bootstrap_note}")
        lines.append(
            "Ran: "
            + (
                ", ".join(
                    f"`{r.command}` (exit {r.exit_code}, {r.duration_s:.0f}s)"
                    for r in ran
                )
                if ran
                else "(none)"
            )
        )
        lines.append(
            "Failed: "
            + (
                ", ".join(f"`{r.command}` (exit {r.exit_code})" for r in self.failed)
                if self.failed
                else "(none)"
            )
        )
        lines.append(
            "Could not start: "
            + (
                ", ".join(
                    f"`{r.command}` ({r.reason or 'no reason captured'})"
                    for r in self.could_not_start
                )
                if self.could_not_start
                else "(none)"
            )
        )

        details = [
            r for r in self.commands
            if r.outcome in {"failed", "could_not_start"} and r.output.strip()
        ]
        if details:
            lines.append("Output:")
            for result in details:
                output = _bounded_verification_output(result.output.strip(), output_limit)
                lines.append(f"$ {result.command}\n{output}")
        return "\n".join(lines)


def declared_verification_commands(task: dict) -> list[str]:
    verification = (task.get("content") or {}).get("verification") or {}
    return [str(cmd) for cmd in (verification.get("commands") or []) if str(cmd).strip()]


def expects_pristine_failure(task: dict) -> bool:
    """Whether the task declares its verification a red-first check.

    ``DevTaskVerification.expect_pristine_failure`` (substrate schemas.py)
    names a command that is SUPPOSED to fail before the work — a
    reproduction, or a red test for a fix this task exists to make. Every
    reader of ``declared_verification_commands`` that treats a pristine
    failure as an environmental fault must check this first, or a red-first
    task can never be dispatched (the S33-B2 incident: 431 deselected tests,
    exit 5, faulted on every tick for two hours).
    """
    verification = (task.get("content") or {}).get("verification") or {}
    return bool(verification.get("expect_pristine_failure"))


def verification_dependency_args(clone: Path) -> list[str]:
    """Return pip arguments for the dependency set used by declared checks.

    The dispatcher chooses the faithful path for B-109: create a clone-local
    virtualenv, then install the repo's Python dependency declarations into it.
    `PyYAML` is included explicitly because the committed EA checker imports
    `yaml` but no current requirements file owns that dependency.
    """
    args: list[str] = ["pytest>=8.0", "PyYAML>=6.0"]
    apps_dir = clone / "apps"
    if apps_dir.exists():
        for req in sorted(apps_dir.glob("*/requirements.txt")):
            args.extend(["-r", str(req.relative_to(clone))])
        for pyproject in sorted(apps_dir.glob("*/pyproject.toml")):
            import tomllib

            app_path = str(pyproject.parent.relative_to(clone))
            with pyproject.open("rb") as handle:
                data = tomllib.load(handle)
            optional = (data.get("project") or {}).get("optional-dependencies") or {}
            if isinstance(optional, dict) and "test" in optional:
                args.append(f"{app_path}[test]")
            else:
                args.append(app_path)
    return args


# [min, max) for the interpreter the verification venv is built from.
#
# 2026-08-02: the host venv was Python 3.14.4, so sys.executable was 3.14 and
# every clone inherited it. greenlet (reached via sqlalchemy) publishes no 3.14
# wheel and fails to build from source, so bootstrap_verification_env exited 1
# and EVERY declared verification command reported could-not-start. Correct work
# was refused and returned to pending — the factory could not verify anything on
# that host, which also makes autonomous merge unreachable rather than merely
# unbuilt. The failure surfaced as 231 lines of wheel-build log, which names the
# compiler error and not the decision that caused it.
#
# Refusing an unsupported interpreter up front is the difference between a
# diagnosis and a wall of text. Raise the upper bound when greenlet ships a
# wheel for the next version and the suite passes on it — not before.
SUPPORTED_PYTHON: tuple[tuple[int, int], tuple[int, int]] = ((3, 11), (3, 14))

# Searched in order when the launching interpreter is unsuitable.
PYTHON_CANDIDATES = ("python3.13", "python3.12", "python3.11")


def python_is_supported(version: tuple[int, int] | None) -> bool:
    """True when ``version`` is inside :data:`SUPPORTED_PYTHON`."""
    if version is None:
        return False
    low, high = SUPPORTED_PYTHON
    return low <= version < high


def interpreter_version(executable: str) -> tuple[int, int] | None:
    """Return ``(major, minor)`` for ``executable``, or None if it will not run."""
    try:
        proc = run(
            [executable, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def resolve_verification_python(
    launching: str | None = None,
    candidates: tuple[str, ...] = PYTHON_CANDIDATES,
    version_of=interpreter_version,
    which=shutil.which,
) -> str:
    """Choose an interpreter the verification bootstrap can install into.

    ``FACTORY_PYTHON`` wins when set, and is NOT second-guessed beyond being
    checked — an operator naming an interpreter explicitly should be told it is
    unsupported rather than quietly given a different one.
    """
    low, high = SUPPORTED_PYTHON
    window = f">={low[0]}.{low[1]},<{high[0]}.{high[1]}"

    override = os.environ.get("FACTORY_PYTHON")
    if override:
        found = which(override) or override
        version = version_of(found)
        if not python_is_supported(version):
            shown = f"{version[0]}.{version[1]}" if version else "unusable"
            raise DispatchError(
                f"FACTORY_PYTHON={override!r} is Python {shown}; verification needs {window}"
            )
        return found

    launching = launching or sys.executable
    if python_is_supported(version_of(launching)):
        return launching

    for name in candidates:
        found = which(name)
        if found and python_is_supported(version_of(found)):
            return found

    current = version_of(launching)
    shown = f"{current[0]}.{current[1]}" if current else "unknown"
    raise DispatchError(
        f"no usable Python for the verification venv: this process is {shown} and "
        f"none of {', '.join(candidates)} is on PATH. Verification needs {window} "
        f"because greenlet has no wheel above it. Set FACTORY_PYTHON to a suitable "
        f"interpreter."
    )


def bootstrap_verification_env(clone: Path) -> str:
    """Build the venv OUTSIDE the clone (dev.finding 6c19f60f AC-3) and
    install repo test deps. `-I`, cwd at the PARENT: a planted <clone>/venv
    PACKAGE would otherwise shadow the stdlib module on `-m venv`'s cwd-first
    sys.path."""
    venv_dir = containment.verification_venv_path(clone)
    venv_python = venv_dir / "bin" / "python"
    base_python = resolve_verification_python()
    if not venv_python.exists():
        run(
            [base_python, "-I", "-m", "venv", str(venv_dir)],
            cwd=clone.resolve().parent,
            timeout=DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
        )

    pip_args = verification_dependency_args(clone)
    try:
        profile, verify_tmp = containment.prepare_verification_containment(clone, (venv_dir,))
    except ValueError as exc:
        raise DispatchError(str(exc)) from exc
    # --no-cache-dir: pip's cache lives in the HOST's home (bead 9d34734f,
    # 2026-08-01: a stale host cache broke every declared command). Contained
    # and env-scrubbed (dev.finding 6c19f60f AC-4): a hostile PEP 517 backend
    # in a local app's pyproject.toml gets no write access outside {clone,
    # the venv, .verify-tmp} and no secret in its environment.
    env = {**_verification_env(clone), **containment.verify_tmp_env(verify_tmp),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    run(
        containment.contained_argv(
            (str(venv_python), "-m", "pip", "install", "--no-cache-dir", *pip_args), profile
        ),
        cwd=clone, timeout=DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS, env=env,
    )
    return (
        f"created verification venv outside the clone from {base_python} and installed "
        "repo Python dependencies plus `PyYAML` for the EA/check-* scripts, contained "
        "and without the host pip cache"
    )


def _verification_env(clone: Path) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in VERIFICATION_ENV_ALLOWLIST
        if name in os.environ
    }
    venv_dir = containment.verification_venv_path(clone)
    venv_bin = venv_dir / "bin"
    inherited_path = env.get("PATH")
    env["PATH"] = (
        f"{venv_bin}{os.pathsep}{inherited_path}" if inherited_path else str(venv_bin)
    )
    env["VIRTUAL_ENV"] = str(venv_dir)
    # The clone declares itself non-interactive; it does not inherit that
    # fact from the operator's shell. Without this, a declared command that
    # runs pnpm (or any other tool that prompts when it can't find a TTY)
    # aborts asking for confirmation it can never receive (bead 60031b02).
    # Set unconditionally, after the allowlisted copy above, so a host-set
    # CI value is never read through VERIFICATION_ENV_ALLOWLIST — the value
    # here is always the clone's own, not a passthrough.
    env["CI"] = "true"
    # FA-S49-1: HOME stays allowlisted (R2603-5/R2605-8 and ordinary shell
    # tooling need it), but that also hands every declared command whatever
    # ambient git config the operator's host carries -- an `init.defaultBranch`
    # and a committer identity CI has neither of. GIT_CONFIG_GLOBAL/
    # GIT_CONFIG_SYSTEM override the *path* git reads for the global and
    # system config tiers (git-config(1)); pointing both at os.devnull hides
    # $HOME/.gitconfig and a conventional /etc/gitconfig, without touching
    # HOME itself or the clone's own repo-local .git/config. This is why
    # #765's create_repo_with_tracking_main-shaped specimen (plain
    # `git init --bare`, relying on init.defaultBranch) now fails here
    # instead of only in CI -- see
    # test_verification_env_neutralizes_ambient_init_default_branch.
    #
    # GIT_CONFIG_SYSTEM alone is not enough: PR #792's gate (F2) caught that
    # Apple's git additionally bakes in an installation-scope config file
    # (`git config --show-scope` reports it as `unknown`, not `system`) that
    # GIT_CONFIG_SYSTEM's path override does not redirect -- on this
    # dispatcher's own host it is
    # /Applications/Xcode.app/Contents/Developer/usr/share/git-core/gitconfig
    # (or the CommandLineTools equivalent), and it sets init.defaultBranch=main
    # too. GIT_CONFIG_NOSYSTEM=1 disables system-tier config lookup outright
    # -- the only thing that also hides a file GIT_CONFIG_SYSTEM's redirect
    # doesn't reach.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _could_not_start(proc: subprocess.CompletedProcess) -> bool:
    # 126/127: the shell couldn't exec/find the command. 65/71 with a
    # "sandbox-exec:" stderr: the WRAPPER failed, not the command.
    if proc.returncode in {126, 127}:
        return True
    return proc.returncode in {65, 71} and (proc.stderr or "").lstrip().startswith(
        "sandbox-exec:"
    )


def _could_not_start_result(command: str, reason: str, started: float) -> VerificationCommandResult:
    return VerificationCommandResult(
        command=command, outcome="could_not_start", exit_code=None,
        output=reason, duration_s=time.monotonic() - started, reason=reason,
    )


def run_verification_shell(
    cwd: Path, command: str, env: dict[str, str], timeout: float
) -> VerificationCommandResult:
    started = time.monotonic()
    try:
        extra_write = containment.linked_worktree_write_extras(cwd)
        profile, verify_tmp = containment.prepare_verification_containment(cwd, extra_write)
    except ValueError as exc:
        return _could_not_start_result(command, str(exc), started)
    env = {**env, **containment.verify_tmp_env(verify_tmp)}
    # Test seam only (never a production flag -- AC-2 forbids one): a test
    # running nested inside this dispatcher's own sandbox monkeypatches
    # containment.contained_argv to skip only the wrap, keeping profile
    # computation, env scrubbing and the real subprocess real.
    argv = containment.contained_argv(("/bin/sh", "-c", command), profile)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return _could_not_start_result(command, str(exc), started)
    except PermissionError as exc:
        return _could_not_start_result(command, str(exc), started)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        err = exc.stderr or ""
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return VerificationCommandResult(
            command=command,
            outcome="failed",
            exit_code=None,
            output=(partial or "") + (err or ""),
            duration_s=time.monotonic() - started,
            reason=f"timed out after {timeout:.0f}s",
        )

    output = (proc.stdout or "") + (proc.stderr or "")
    if _could_not_start(proc):
        return VerificationCommandResult(
            command=command,
            outcome="could_not_start",
            exit_code=proc.returncode,
            output=output,
            duration_s=time.monotonic() - started,
            reason=(output.strip() or f"shell exited {proc.returncode}")[-500:],
        )
    if proc.returncode != 0:
        return VerificationCommandResult(
            command=command,
            outcome="failed",
            exit_code=proc.returncode,
            output=output,
            duration_s=time.monotonic() - started,
        )
    return VerificationCommandResult(
        command=command,
        outcome="passed",
        exit_code=0,
        output=output,
        duration_s=time.monotonic() - started,
    )


def run_verification_command(clone: Path, command: str) -> VerificationCommandResult:
    return run_verification_shell(
        clone, command, _verification_env(clone), DEFAULT_VERIFICATION_TIMEOUT_SECONDS
    )


def verify_declared_commands(clone: Path, commands: list[str]) -> VerificationReport:
    if not commands:
        return VerificationReport(())

    try:
        bootstrap_note = bootstrap_verification_env(clone)
    except DispatchError as exc:
        return VerificationReport(
            tuple(
                VerificationCommandResult(
                    command=command,
                    outcome="could_not_start",
                    exit_code=None,
                    output=str(exc),
                    duration_s=0.0,
                    reason=f"verification environment bootstrap failed: {exc}",
                )
                for command in commands
            )
        )

    results = tuple(run_verification_command(clone, command) for command in commands)
    return VerificationReport(results, bootstrap_note=bootstrap_note)


# dev.finding 91dedb1a: a worker's own claim that a verification failure is
# "pre-existing on main" was recorded unmeasured, so a bead re-dispatched
# into the same wall with a full budget each time (5bb23acc: three attempts,
# same test, same unmeasured claim -- the change was actually correct and a
# fixture just hadn't been updated). These three verdicts make that claim a
# measurement instead of prose.
BASE_VERDICT_INTRODUCED = "INTRODUCED"
BASE_VERDICT_PRE_EXISTING = "PRE_EXISTING"
BASE_VERDICT_INDETERMINATE = "INDETERMINATE"

#: Matches the "Failed: `cmd1` (exit N), `cmd2` (exit M)" line
#: VerificationReport.describe() renders -- the ONE existing place a failed
#: command's exact text already reaches the failure note. Parsed instead of
#: threaded through a new field because describe_failure's docstring
#: documents that a Temporal ActivityError loses everything but the message
#: string crossing the workflow boundary; every other failure-classification
#: path in this module (auth, capacity, stale-base-ref) already re-derives
#: its signal from that same string rather than fighting the boundary.
_FAILED_VERIFICATION_COMMAND_PATTERN = re.compile(r"^Failed: `(.+?)`", re.MULTILINE)

#: Section headers describe() writes after "Failed: ..." plus the
#: worker-appended stdout tail -- see _trusted_detail_prefix for what each
#: marker guards against (PR #953 F1, PR #1004).
_UNTRUSTED_DETAIL_SECTION_MARKERS = ("Output:", "Worker output (tail):", "Could not start:")


def _trusted_detail_prefix(detail: str) -> str:
    """The portion of a composed failure detail written before ANY section a
    worker can shape. Shared by every parser here that reads a failure
    detail/note body:
    "Output:" embeds a failed command's own stdout/stderr verbatim
    (worker-controlled -- gate finding F1 on PR #953), "Worker output
    (tail): " is the worker process's own stdout appended by
    ``failure_reason_with_worker_output``, and "Could not start: " is
    describe()'s could_not_start summary line, which also embeds the
    command's own captured tail (gate finding on PR #1004) and sits BEFORE
    "Output:". Truncating at the first marker found -- rather than trusting
    callers to hand this a pre-truncated string -- means a different
    composition order, or a future marker added to the tuple, is still
    refused rather than silently admitted.
    """
    trusted = detail
    for marker in _UNTRUSTED_DETAIL_SECTION_MARKERS:
        cut = trusted.find(f"\n{marker}")
        if cut != -1:
            trusted = trusted[:cut]
        elif trusted.startswith(marker):
            trusted = ""
    return trusted


def first_failed_verification_command(detail: str) -> str | None:
    """The first declared command VerificationReport.describe() reported failed.

    None when ``detail`` names no failed command at all -- a could-not-start
    failure, a scope violation, a changed-nothing failure, or any other shape
    that never ran a declared command against the patched tree. Also None
    when the only "Failed: `...`" line found sits inside a worker-controlled
    section rather than describe()'s own summary line.
    """
    trusted = _trusted_detail_prefix(detail)
    match = _FAILED_VERIFICATION_COMMAND_PATTERN.search(trusted)
    return match.group(1) if match else None


@dataclass(frozen=True)
class BaseVerificationCheck:
    """One measured answer to "does this failure also happen on the base?".

    ``detail`` is written for the failure note, not a log -- it is meant to
    be read by whichever worker attempts this bead next.
    """

    verdict: str
    detail: str


#: Appended to every BaseVerificationCheck.detail naming a command and base
#: revision, so a later attempt's find_prior_base_verification_check can
#: recognize "this pair was already measured" from guards.prior_failures.
_BASE_CHECK_KEY_PATTERN = re.compile(
    r"Base-revision check: (?P<verdict>INTRODUCED|PRE_EXISTING|INDETERMINATE)\b"
    r".*?\(base-check key: `(?P<command>.*?)` @ (?P<base_revision>\S+)\)",
    re.DOTALL,
)


def _tag_base_check_key(detail: str, command: str, base_revision: str) -> str:
    return f"{detail} (base-check key: `{command}` @ {base_revision})"


def find_prior_base_verification_check(
    notes: Iterable[dict[str, Any]], command: str, base_revision: str
) -> BaseVerificationCheck | None:
    """A verdict an earlier attempt already recorded for this exact pair.

    F2 (dev.finding 91dedb1a's follow-up, gate finding on PR #929): re-running
    the same failing command against the same base revision on every retry of
    an already-classified failure is pure waste. Scans ``guards.prior_failures``
    (oldest first) for the key ``classify_verification_failure_against_base``
    tags onto every verdict it returns; the newest match wins, and a moved
    ``base_revision`` changes the key so it always falls through to a fresh
    measurement. A memo whose verdict is ``BASE_VERDICT_INDETERMINATE`` is
    never honoured either: that means the measurement itself failed, not
    that an answer was reached.

    Defence against a worker-forged memo: the key pattern is matched with
    ``re.match``, anchored at position 0 of ``_trusted_detail_prefix(failure.reason)``.
    ``record_failure_activity`` is the only writer of a legitimate memo and
    always PREPENDS it, before describe()'s own sections and the worker's
    appended stdout tail -- so "is this the very first thing in the note" is
    a structural property only the dispatcher's own prepend can satisfy,
    regardless of which section a forgery is planted in. This also closes a
    fourth channel no marker enumerates: a smoke-check failure's
    ``py_compile`` error text quotes worker-authored source verbatim with no
    marker in front of it at all (gate finding on PR #1022) -- position-0
    anchoring catches that by construction, where a marker list could not.
    """
    match: BaseVerificationCheck | None = None
    for failure in guards.prior_failures(notes):
        trusted = _trusted_detail_prefix(failure.reason)
        found = _BASE_CHECK_KEY_PATTERN.match(trusted)
        if (
            found
            and found.group("command") == command
            and found.group("base_revision") == base_revision
            and found.group("verdict") != BASE_VERDICT_INDETERMINATE
        ):
            match = BaseVerificationCheck(found.group("verdict"), found.group(0))
    return match


def classify_verification_failure_against_base(
    clone: Path,
    command: str,
    base_revision: str,
    *,
    prior_notes: Iterable[dict[str, Any]] | None = None,
) -> BaseVerificationCheck:
    """Re-run ``command`` once against ``base_revision``, and say what changed.

    Isolation note (see dispatch.py's module docstring on clone-vs-worktree):
    that WORKER-facing rule doesn't apply here -- the worktree is rooted at
    ``clone``, the dispatcher's OWN already-isolated clone, never handed to a
    worker, and ``git worktree add``/``remove`` touch only
    ``clone/.git/worktrees/*``. The base revision is already in ``clone``'s
    own object database (its HEAD from before the worker ran), so this is
    the same-object-store checkout AC-2 asks for without a second network
    clone, reusing ``clone``'s own already-bootstrapped venv rather than
    paying that cost again (AC-3); the one inexactness this accepts is that
    a dependency-declaration change under test still runs the base command
    against the PATCHED dependency set.

    Fails soft, always: any inability to check out or run the base revision
    returns INDETERMINATE rather than raising (PRIN-015), intrinsic to the
    function -- the cleanup ``finally`` block below never lets a removal
    failure escape and replace a verdict the ``try`` block already computed.

    ``prior_notes``, when given, is scanned via
    ``find_prior_base_verification_check`` for a verdict an earlier attempt
    already recorded for this exact ``(command, base_revision)`` pair (F2),
    returned immediately with no git activity if found. That scan fails soft
    on its own: an unparseable ``prior_notes`` or note shape, or a memo that
    exists but doesn't match the pattern, is treated as "no memo found" and
    falls through to the real measurement, never raised past this function.
    """
    command = command.strip()
    base_revision = base_revision.strip()
    if not command or not base_revision:
        return BaseVerificationCheck(
            BASE_VERDICT_INDETERMINATE,
            "Base-revision check: INDETERMINATE -- no failing command or base "
            "revision available to compare against.",
        )
    if prior_notes is not None:
        try:
            prior = find_prior_base_verification_check(prior_notes, command, base_revision)
        except Exception:  # noqa: BLE001 - AC-4: an unreadable memo must fail soft into measuring, not raise past this diagnostic
            prior = None
        if prior is not None:
            return prior
    holder: Path | None = None
    try:
        holder = Path(tempfile.mkdtemp(prefix="factory-basecheck-"))
        worktree = holder / "base"
        added = git_in_clone(
            clone,
            ["worktree", "add", "--detach", str(worktree), base_revision],
            check=False,
            timeout=BASE_VERIFICATION_WORKTREE_TIMEOUT_SECONDS,
        )
        if added.returncode != 0:
            return BaseVerificationCheck(
                BASE_VERDICT_INDETERMINATE,
                _tag_base_check_key(
                    f"Base-revision check: INDETERMINATE -- could not check out "
                    f"{base_revision} to compare against: "
                    + (added.stderr or added.stdout or "").strip()[-500:],
                    command,
                    base_revision,
                ),
            )
        base_result = run_verification_shell(
            worktree,
            command,
            _verification_env(clone),
            BASE_VERIFICATION_CHECK_TIMEOUT_SECONDS,
        )
        if base_result.outcome == "failed" and (base_result.reason or "").startswith(
            "timed out"
        ):
            return BaseVerificationCheck(
                BASE_VERDICT_INDETERMINATE,
                _tag_base_check_key(
                    f"Base-revision check: INDETERMINATE -- `{command}` against "
                    f"{base_revision} timed out after "
                    f"{BASE_VERIFICATION_CHECK_TIMEOUT_SECONDS}s.",
                    command,
                    base_revision,
                ),
            )
        if base_result.outcome == "passed":
            return BaseVerificationCheck(
                BASE_VERDICT_INTRODUCED,
                _tag_base_check_key(
                    f"Base-revision check: INTRODUCED -- `{command}` PASSES on "
                    f"{base_revision} and fails with this change applied; not "
                    "pre-existing.",
                    command,
                    base_revision,
                ),
            )
        if base_result.outcome == "failed":
            return BaseVerificationCheck(
                BASE_VERDICT_PRE_EXISTING,
                _tag_base_check_key(
                    f"Base-revision check: PRE_EXISTING -- `{command}` also FAILS on "
                    f"{base_revision} (exit {base_result.exit_code}).",
                    command,
                    base_revision,
                ),
            )
        return BaseVerificationCheck(
            BASE_VERDICT_INDETERMINATE,
            _tag_base_check_key(
                f"Base-revision check: INDETERMINATE -- `{command}` against "
                f"{base_revision} could not start: "
                f"{base_result.reason or 'no reason captured'}",
                command,
                base_revision,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never mask the real failure
        return BaseVerificationCheck(
            BASE_VERDICT_INDETERMINATE,
            _tag_base_check_key(
                f"Base-revision check: INDETERMINATE -- comparison for `{command}` "
                f"against {base_revision} could not be completed: {exc}",
                command,
                base_revision,
            ),
        )
    finally:
        if holder is not None:
            # Bounded (previously unbounded) so a slow/hung removal cannot by
            # itself blow record_failure's activity budget, and wrapped in
            # try/except so a removal failure -- or timeout -- is best-effort
            # cleanup, not a new exception raised out of this `finally` that
            # would silently replace a verdict the `try` block already
            # computed and returned.
            try:
                git_in_clone(
                    clone,
                    ["worktree", "remove", "--force", str(holder / "base")],
                    check=False,
                    timeout=BASE_VERIFICATION_WORKTREE_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001 - cleanup is best-effort only
                pass
            shutil.rmtree(holder, ignore_errors=True)


def pristine_verification_failure_reason(report: VerificationReport) -> str:
    """Explain why pre-worker verification makes this an environmental fault."""
    failing = (report.failed or report.could_not_start)[0]
    return (
        f"Declared verification command `{failing.command}` failed before the worker ran. "
        "The failure preceded the work, so it is an environmental fault rather than "
        "a verdict on the task.\n"
        + report.describe(output_limit=1000)
    )


#: Marker record_pristine_already_satisfied and dispatch_once's failure
#: handler match on, so a pristine-already-satisfied refusal is routed to its
#: own recording path rather than the generic work-failure path — no worker
#: ever ran, so wording that claims one did (record_already_satisfied_work's)
#: would misreport what happened.
PRISTINE_ALREADY_SATISFIED_MARKER = "pristine verification already satisfied:"


def pristine_already_satisfied_reason(report: VerificationReport) -> str:
    """Explain why a bead declaring expect_pristine_failure must not dispatch.

    The counterpart to :func:`pristine_verification_failure_reason`: that one
    names which command failed before the worker ran; this one names which
    command a red-first task expected to fail but that already passed. Either
    way the bead cannot be dispatched as declared, but the reasons — and what
    an operator does next — are opposite, so they stay two functions rather
    than one branching on a flag.
    """
    passed_commands = [r.command for r in report.commands if r.outcome == "passed"]
    named = ", ".join(f"`{c}`" for c in passed_commands) or "(none identified)"
    return (
        f"{PRISTINE_ALREADY_SATISFIED_MARKER} {named} passed against an unmodified clone "
        "of main, but verification.expect_pristine_failure declared it should fail before "
        "the worker ran. The work this bead exists to do already appears done.\n"
        + report.describe(output_limit=1000)
    )


def verify_pristine_commands(
    commands: list[str], cfg: "Config | None" = None
) -> VerificationReport:
    """Run declared verification ``commands`` against an unmodified clone of main.

    The one entry point for "does this declared verification pass on main" -
    file_task.py's pre-filing refusal calls this directly, and it is built from
    exactly the pieces the dispatcher's own preflight step uses
    (``make_clone`` for isolation, ``verify_declared_commands`` for execution),
    not a second copy of either. That is deliberate: a spec whose verification
    could never pass was filed anyway and then re-dispatched every ~15 minutes
    for three hours before anything caught it (OPS-6, 2026-08-23, dev.task
    05262e55) — the dispatcher already ran this exact check, just three hours
    and twelve clone bootstraps too late. Two independent implementations of
    "does it pass on main" is exactly the shape that drifts; this keeps there
    being only one.
    """
    if not commands:
        return VerificationReport(())
    cfg = cfg or Config.from_env()
    workdir = Path(tempfile.mkdtemp(prefix="factory-pristine-", dir=cfg.workdir_root))
    mark_workdir_owner(workdir)
    clone = workdir / "repo"
    try:
        make_clone(cfg, clone)
        return verify_declared_commands(clone, commands)
    finally:
        cleanup_workdir(workdir)


# ---------------------------------------------------------------------------
# proposing
# ---------------------------------------------------------------------------


def render_traceability(task_content: dict) -> str:
    """Render optional bead traceability fields for a PR body.

    Returns an empty string when the bead carries no traceability, preserving
    the legacy PR body exactly for tasks filed before these fields existed.
    """
    sections: list[str] = []

    requirement_refs = [
        str(ref) for ref in (task_content.get("requirement_refs") or [])
    ]
    if requirement_refs:
        sections.append(
            "## Requirement refs\n\n"
            + "\n".join(f"- {ref}" for ref in requirement_refs)
        )

    nfrs = task_content.get("nfrs") or []
    if nfrs:
        lines = ["## Non-functional requirements", ""]
        required_fields = ("category", "statement", "threshold", "verification")
        for index, nfr in enumerate(nfrs, start=1):
            if not isinstance(nfr, dict):
                raise DispatchError(f"NFR #{index} must be an object")
            missing = [
                field
                for field in required_fields
                if not str(nfr.get(field) or "").strip()
            ]
            if missing:
                raise DispatchError(
                    f"NFR #{index} is missing required field(s): {', '.join(missing)}"
                )
            lines.append(
                "- "
                f"Category: {nfr['category']}; "
                f"Statement: {nfr['statement']}; "
                f"Threshold: {nfr['threshold']}; "
                f"Verification: {nfr['verification']}"
            )
        sections.append("\n".join(lines))

    arch_impact = task_content.get("arch_impact")
    if arch_impact:
        if not isinstance(arch_impact, dict):
            raise DispatchError("arch_impact must be an object")
        applications = [str(app) for app in (arch_impact.get("applications") or [])]
        capabilities = [str(cap) for cap in (arch_impact.get("capabilities") or [])]
        lines = [
            "## Architectural impact",
            "",
            f"Applications: {', '.join(applications) or '(none declared)'}",
            f"Capabilities: {', '.join(capabilities) or '(none declared)'}",
        ]
        notes = str(arch_impact.get("notes") or "").strip()
        if notes:
            lines.append(f"Notes: {notes}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def render_applies_stamp(applies_ids: Iterable[str]) -> str:
    """The PR body's 'Applies:' line naming embedded PRIN ids (F-DCE-3).

    Returns "" when nothing was embedded, so a PR body with no doctrine citations —
    and the #432 first-line contract, untouched here — stays byte-identical to today.
    """
    ids = [str(prin_id) for prin_id in applies_ids if str(prin_id).strip()]
    if not ids:
        return ""
    return "Applies: " + ", ".join(ids)


# dev.finding 3dc4a938: GitHub's fence pattern (0-3 spaces, then >=3 of "`"
# or "~") is a superset of the CI change-kind awk's own fence-toggle trigger,
# so defusing lines matching it defuses both readers.
WORKER_REPORT_CAP = 12000
_FENCE_LINE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")


def neutralize_fence_lines(text: str) -> tuple[str, int]:
    lines, neutralized = [], 0
    for line in text.split("\n"):
        m = _FENCE_LINE_RE.match(line)
        if m:
            lines.append(f"{m.group(1)}\\{line[len(m.group(1)):]}")
            neutralized += 1
        else:
            lines.append(line)
    return "\n".join(lines), neutralized


def render_worker_report(worker_note: str, cap: int = WORKER_REPORT_CAP) -> str:
    stripped = worker_note.strip()
    escaped, neutralized = neutralize_fence_lines(stripped[:cap])
    note = ""
    if neutralized:
        plural = "line" if neutralized == 1 else "lines"
        note = f"_Note: {neutralized} {plural} escaped to keep this container singular._\n\n"
    section = f"## Worker's own report\n\n{note}```\n{escaped}\n```"
    if len(stripped) > cap:
        section += f"\n\n[worker report truncated: {cap} of {len(stripped)} characters shown]"
    return section


def branch_commit_message(task: dict, risk: str) -> str:
    content = task.get("content") or {}
    title = content.get("title") or "factory task"
    lane = content.get("lane") or "unknown"
    prefix = "refactor" if risk == "structural" else "fix"
    return (
        f"{prefix}({lane}): {title}\n\n"
        f"Dispatched from dev.task {task['id']} by {CREATED_BY}.\n\n"
        f"Change kind: {risk}\n\n"
        f"Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    )


def _remote_branch_head(clone: Path, remote: str, branch: str) -> str | None:
    """The sha ``<branch>`` currently points at on ``remote``, or None if it
    has no such ref.

    ``remote`` is a URL, not a local remote name (dev.finding 79db3113 part
    a): the clone's own ``origin`` is a file (``.git/config``) a worker can
    rewrite, so the lease this feeds (``clear_stale_branch_for_retry``) is
    computed against the same explicit ``cfg.remote`` URL ``open_pull_request``
    is about to push to -- the identical reasoning
    ``merge_commit_is_ancestor_of_main`` (OPS-115) already applies to its own
    fetch. ``git`` accepts a URL anywhere it accepts a remote name.
    """
    proc = git_in_clone(clone, ["ls-remote", "--heads", remote, branch], check=False)
    if proc.returncode != 0:
        raise DispatchError(
            f"could not check {remote!r} for branch {branch!r}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    lines = (proc.stdout or "").strip().splitlines()
    return lines[0].split()[0] if lines else None


def pull_requests_for_branch(cfg: Config, clone: Path, branch: str) -> list[PullRequestStatus]:
    """Every PR (any state) whose head is ``branch``, as GitHub currently sees it."""
    proc = run(
        [
            "gh", "pr", "list",
            # Explicit high limit: the default 30 could page an older OPEN or
            # MERGED PR out of the safety check on a bead with many closed
            # retries (release-gate advisory on the PR that landed this).
            "--limit", "200",
            "--repo", cfg.repo,
            "--head", branch,
            "--state", "all",
            "--json", "number,state,url",
        ],
        cwd=clone,
        timeout=60,
    )
    payload = json.loads(proc.stdout or "[]")
    return [
        PullRequestStatus(
            number=item.get("number"),
            url=str(item.get("url") or ""),
            state=str(item.get("state") or "UNKNOWN").upper(),
            merge_commit=None,
        )
        for item in payload
    ]


def clear_stale_branch_for_retry(cfg: Config, clone: Path, branch: str) -> str | None:
    """Make ``branch`` pushable for a fresh attempt without ever clobbering
    someone else's work (first occurrence OPS-62/5a5b4b7f, 2026-09-05).

    A bead's branch name is deterministic (it embeds the bead id), so a
    remote branch under this exact name belongs to this bead by
    construction. Under the Amendment 37 regime a release gate's
    DO-NOT-MERGE closes the PR but leaves that branch on the remote, so the
    bead's next attempt recomputes the same branch name and its push
    collides with its own prior, superseded work -- a normal, expected path,
    not a conflict with anyone else's.

    If the branch exists remotely, this looks up every PR GitHub has ever
    recorded against it. Any PR still open or merged means the branch is not
    this bead's to reclaim (a merged PR's branch is shipped history; an open
    one is someone's live review) and this refuses loudly, naming the PR. If
    every PR against it is closed-unmerged -- or none was ever recorded, e.g.
    a prior attempt that pushed but died before ``gh pr create`` -- the
    branch is safe to reclaim.

    Returns the ``--force-with-lease=<branch>:<sha>`` value the caller must
    push with (the sha this function just observed, so the lease still fails
    safely if the remote moves again between this check and the push), or
    None when there is nothing to reclaim and an ordinary push suffices.
    """
    remote_sha = _remote_branch_head(clone, cfg.remote, branch)
    if remote_sha is None:
        return None

    blocking = [
        pr for pr in pull_requests_for_branch(cfg, clone, branch)
        if pr.state in ("OPEN", "MERGED")
    ]
    if blocking:
        pr = blocking[0]
        pr_ref = f"#{pr.number}" if pr.number is not None else pr.url
        raise DispatchError(
            f"refusing to push branch {branch!r}: PR {pr_ref} ({pr.url}) is "
            f"{pr.state.lower()}, not closed-unmerged. The branch name is "
            "deterministic per bead, but the dispatcher never overwrites an "
            "open or merged PR's branch."
        )
    return f"{branch}:{remote_sha}"


#: FA-S49-1/AC-5: what a green declared-verification pre-check does and does
#: not establish about CI parity, for the person reading the PR it opens.
#: Fixed rather than computed per-run -- the divergences this names are the
#: enumerated, dated ones (#765, #759) the bead was filed from, not a
#: per-command detector guessing which declared test is ref- or
#: config-sensitive (that detector is the open-ended problem the bead
#: explicitly disclaims).
ENVIRONMENT_PARITY_NOTE = """## Environment parity with CI

- **Neutralized:** ambient git config. Declared verification runs with \
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` pointed at `/dev/null` and \
`GIT_CONFIG_NOSYSTEM=1` set, so `init.defaultBranch` and any committer \
identity from the operator's `~/.gitconfig`, a conventional \
`/etc/gitconfig`, *or* a vendor-baked installation-scope config (Apple's \
git ships one CI has no equivalent of -- PR #792's gate caught that \
`GIT_CONFIG_SYSTEM` alone doesn't hide it) are invisible to it, matching a \
CI runner that has none of the three (#765's `create_repo_with_tracking_main` \
specimen).
- **Reported, not neutralized:** this clone carries a local `main` branch \
tracking `origin/main`, because the dispatcher's own later git operations \
in this same clone need one. CI's `actions/checkout` on a PR instead \
leaves a detached HEAD with no local `main`. A declared command that \
depends on `main` being a resolvable ref -- not merely on config -- can \
still pass here and fail in CI (#759).
- **Undetectable by this pre-check:** anything not git-configuration or \
dependency-presence shaped -- network reachability, OS/platform \
differences, resource limits, and any ambient environment variable outside \
`VERIFICATION_ENV_ALLOWLIST` that CI also lacks. A green pre-check \
establishes parity only for the two points above, not general \
CI-equivalence."""


def open_pull_request(
    cfg: Config,
    clone: Path,
    task: dict,
    branch: str,
    worker_note: str,
    scope_verdict: guards.ScopeVerdict,
    verification_report: VerificationReport,
    applies_ids: Iterable[str] = (),
    *,
    verified_revision: str | None = None,
    preserved_baseline_refs: tuple[str, ...] = (),
) -> str:
    content = task.get("content") or {}
    title = content.get("title") or "factory task"
    lane = content.get("lane") or "unknown"
    risk = content.get("risk_class") or "structural"

    git_in_clone(clone, ["checkout", "-b", branch])
    git_in_clone(clone, ["add", "-A"])
    staged = git_in_clone(clone, ["diff", "--cached", "--quiet"], check=False)
    if staged.returncode != 0:
        # Ordinary shape: the worker left its diff uncommitted, as the
        # dispatcher's contract asks, and `git add -A` just staged it -- this
        # is the one commit that ships it.
        git_in_clone(
            clone,
            [
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                branch_commit_message(task, risk),
            ],
        )
    # else: `git add -A` staged nothing because the tree was already clean --
    # the worker committed its own diff directly instead (against the
    # dispatcher's contract, but nothing stops it, dev.finding 71418f56), and
    # `changed_paths`'s call in `contain_activity` already had to find that
    # commit for this run to reach here at all. HEAD is exactly the worker's
    # patch, so pushing it as-is is correct; committing again would fail with
    # "nothing to commit, working tree clean" (release-gate finding on #846) --
    # this is the guard fix's other half, without which a committing worker
    # passed containment only to die one step later, here.
    # Pushes to cfg.remote BY URL (dev.finding 79db3113 part a), not to the
    # local remote name "origin" -- a worker-writable .git/config -- the same
    # reasoning merge_commit_is_ancestor_of_main (OPS-115) applies to its own
    # fetch. No "-u": tracking a literal URL is meaningless, and "-u" writes
    # into .git/config, which the containment profiles now deny.
    lease = clear_stale_branch_for_retry(cfg, clone, branch)
    push_args = ["push", "-q"]
    if lease is not None:
        push_args.append(f"--force-with-lease={lease}")
    push_args += [cfg.remote, f"{branch}:refs/heads/{branch}"]
    git_in_clone(clone, push_args, timeout=300)

    acceptance = "\n".join(f"- {a}" for a in (content.get("acceptance") or []))
    verification = content.get("verification") or {}
    commands = "\n".join(f"- `{c}`" for c in (verification.get("commands") or []))
    verification_summary = verification_report.describe(output_limit=1000)
    applies_stamp = render_applies_stamp(applies_ids)
    applies_block = f"{applies_stamp}\n\n" if applies_stamp else ""
    traceability = render_traceability(content)
    traceability_block = f"{traceability}\n\n" if traceability else ""

    #: A spec may declare, in writing, that it needs the live production store
    #: and why -- CLAUDE.md's 2026-09-13 rule (decision record D7) otherwise
    #: requires it to measure against a committed fixture or snapshot. That
    #: declaration is written into bead content at filing time and, until this
    #: block existed, appeared NOWHERE a reviewer would see it: the release gate
    #: reads the pull request, and the field was invisible there. A declaration
    #: nobody reads is not a control, and a field that is never surfaced becomes
    #: one that gets set to make a refusal go away.
    live_store_access = str(content.get("live_store_access") or "").strip()
    live_store_block = (
        (
            "## Live store access is DECLARED by this task's spec\n\n"
            "This spec sets `live_store_access`: the filer's own written statement that "
            "the work needs the live production store, and why. It is SELF-ISSUED at "
            "filing time. Nothing approves it -- there is no reviewer sign-off, no "
            "schema validation, and no ratified exemption behind it.\n\n"
            "**D7 GRANTS NO EXEMPTION.** CLAUDE.md's 2026-09-13 rule says an acceptance "
            "criterion measures against a snapshot the outer loop supplies, NEVER "
            "against the live store, because the worker inherits the launchd "
            "environment and therefore holds the production substrate write key. That "
            "rule contains no escape hatch, and none has been ratified. This "
            "declaration does not displace it.\n\n"
            "**Gate: judge the stated reason against D7 as written.** This section "
            "exists so the declaration is visible to you at all -- nothing else in this "
            "pull request would show it. Treat it as an assertion to be tested, not as "
            "permission that was granted.\n\n"
            f"{textwrap.indent(live_store_access, '> ', lambda _line: True)}\n\n"
        )
        if live_store_access
        else ""
    )
    checkout_section = ""
    if verified_revision:
        # Two distinct guards ran before this clone was made, and the dispatcher
        # refuses to clone -- so this PR would not exist -- if either fails:
        # `worker_revision.ensure_checkout_is_current_with_main` confirms the
        # worker checkout's own revision is a current ancestor of its LOCAL
        # `cfg.base_ref`, and (2026-09-17) `dispatch.ensure_base_ref_current`
        # confirms `cfg.base_ref` itself carries no commit absent from its own
        # tracking upstream (`BaseRefStatus.wrong`). Naming only the first, as
        # this sentence used to, reads as "verified against main" to a human but
        # is silent on whether local main agreed with anything upstream -- a
        # checkout pointed at an unmerged branch passed the first guard alone
        # for hours while GitHub's real main was three commits ahead (measured
        # on PR #906). Name both, precisely, rather than let the citation imply
        # more than either check alone establishes.
        checkout_section = (
            "## Verification checkout\n\n"
            f"Declared verification below ran against `{verified_revision}` of "
            f"`{cfg.base_ref}`. The dispatcher refuses to clone (so this PR would "
            "not exist) unless both hold at dispatch time: the worker checkout's "
            f"own revision is a confirmed, current ancestor of local `{cfg.base_ref}` "
            "(`worker_revision.ensure_checkout_is_current_with_main`), and "
            f"`{cfg.base_ref}` itself is a confirmed ancestor-or-equal of its own "
            "tracking upstream -- carries no commit absent from it "
            "(`dispatch.ensure_base_ref_current`; `BaseRefStatus.wrong`). Date this "
            "evidence against that commit, not against when the PR was opened.\n\n"
        )
        if preserved_baseline_refs:
            refs = ", ".join(preserved_baseline_refs)
            checkout_section += (
                "This attempt resumed from preserved work: the latest of "
                f"{refs} was applied as the clone's starting point before the "
                "worker's first action (OPS-106). The revision above is the base "
                "the baseline was applied onto, not the tree verification ran against "
                "-- that tree is base + baseline + this attempt's work.\n\n"
            )

    body = f"""Dispatched by the factory from `dev.task` [`{task['id']}`]({task['id']}).

**This PR was written by an agent and checked by the dispatcher.** The dispatcher
runs declared verification in the isolated clone before opening a PR. It
bootstraps a clone-local `venv/` from the repo's Python dependency declarations
and adds `PyYAML` because the committed EA/checker scripts import `yaml` without
owning that dependency in a requirements file. That path is slower than relying
on the host interpreter, but it keeps task specs faithful and makes
`venv/bin/python ...`, `python ...`, and `python3 ...` declarations resolve
inside the clone.

Change kind: {risk}

{live_store_block}{applies_block}{traceability_block}{checkout_section}\
## Intent

{content.get('intent') or '(none given)'}

## Acceptance criteria

{acceptance or '(none declared)'}

## Declared scope

Allowed: {', '.join(content.get('scope', {}).get('paths') or []) or '(none)'}
Forbidden: {', '.join(content.get('scope', {}).get('forbidden_paths') or []) or '(none)'}

Dispatcher scope verdict: **{scope_verdict.describe()}**

## Verification the task asked for

{commands or '(none declared)'}

## Dispatcher verification

```
{verification_summary.strip()[:4000]}
```

{ENVIRONMENT_PARITY_NOTE}

{render_worker_report(worker_note)}

🤖 Generated with [Claude Code](https://claude.com/claude-code)
"""

    proc = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            cfg.repo,
            "--base",
            cfg.base_ref,
            "--head",
            branch,
            "--title",
            f"{'refactor' if risk == 'structural' else 'fix'}({lane}): {title}",
            "--body",
            body,
        ],
        cwd=clone,
        timeout=180,
    )
    url = (proc.stdout or "").strip().splitlines()[-1]
    return url


def record_applies_links(
    sub: BeadStore,
    task_id: str,
    citations: Iterable[doctrine.PrincipleCitation],
    created_by: str,
) -> list[str]:
    """Write one ``applies`` edge per embedded principle citation (F-DCE-3).

    Best-effort and non-fatal by construction: the edge is provenance, not a gate. A
    missing arch.principle bead (the registry not yet pushed to this environment) or any
    other rejection from the store is collected as a problem string rather than raised — see
    .factory/design.md decision 6 for why this is broader than just the missing-bead case a
    live substrate does not yet even accept the ``applies`` link_type. A 409 (duplicate
    (source, target, link_type)) means an earlier run already wrote this edge; that is
    success, not a problem, which is what makes a second run non-duplicating.
    """
    problems: list[str] = []
    for citation in citations:
        try:
            principle_bead = sub.find_bead("arch", "principle", citation.id)
        except Exception as exc:  # noqa: BLE001 - provenance write, never a gate
            problems.append(f"{citation.id}: lookup failed: {exc}")
            continue
        if principle_bead is None:
            problems.append(
                f"{citation.id}: no arch.principle bead found in this environment"
            )
            continue
        try:
            sub.add_link(task_id, principle_bead["id"], "applies", created_by)
        except Exception as exc:  # noqa: BLE001 - provenance write, never a gate
            if store_error_status(exc) == 409:
                continue
            problems.append(f"{citation.id}: applies link rejected: {exc}")
    return problems


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _committed_but_unpushed_base(clone: Path) -> str | None:
    """The commit to diff HEAD against for a committed-but-unpushed worker
    patch, or ``None`` when HEAD carries no such patch.

    Shared by ``_committed_but_unpushed_diff`` (the preservation path) and
    ``changed_paths``'s fallback (the changed-nothing guard) -- one place
    that decides "is there committed-but-unpushed work here", so the two can
    never compute a different answer to that question (dev.finding
    71418f56: they used to, because the guard never asked it at all).

    ``open_pull_request`` commits the worker's whole diff as a single commit
    on top of whatever the clone had checked out, then pushes it. A failure
    at or after that commit -- a rejected push chief among them -- leaves the
    working tree clean, so a plain diff-against-HEAD sees nothing. The
    commit itself is still exactly the worker's patch, and this is the only
    place that patch still exists once ``cleanup_activity`` deletes the
    clone. The same is true when the worker commits its own patch directly
    instead of leaving it for ``open_pull_request`` -- the dispatcher's
    contract says it shouldn't, but nothing stops it, and a worker is free
    to make more than one commit before it stops, so the patch can be
    several commits deep rather than exactly one (release-gate finding on
    #848: a literal ``HEAD^`` here graded only the last of a two-commit
    worker patch and let a forbidden path committed earlier on the branch
    escape ``check_scope`` entirely).

    A second, older case of the same attribution question: ``HEAD`` may be a
    commit ``apply_preserved_baseline`` created rather than one the worker
    made, when preserved work was applied before this run's worker began and
    the worker then changed nothing on top of it -- a changed-nothing failure
    reaches this same fallback, and ``HEAD`` is still exactly the baseline
    commit (see PRESERVED_BASELINE_TRAILER). That is inherited work already
    named on the bead's ``preserved_attempts``, not this attempt's own, so it
    is excluded before the unpushed-commit walk below even starts, and the
    walk itself stops at (without crossing) any earlier commit carrying the
    same trailer, for a worker that committed on top of an applied baseline.
    """
    if _head_is_preserved_baseline_commit(clone):
        return None
    head = git_in_clone(clone, ["rev-parse", "--verify", "HEAD"], check=False)
    if head.returncode != 0:
        return None
    cursor = head.stdout.strip()
    oldest_unpushed: str | None = None
    while True:
        # Only a commit that exists NOWHERE on the remote is the worker's
        # unpushed patch. A clean clone whose HEAD is the base ref's shipped
        # tip reaches this fallback too (a changed-nothing work failure), and
        # before this guard it saved someone else's merged commit as the
        # bead's "preserved diff" -- corrupting the OPS-49 attribution signal
        # this preservation exists to protect (release-gate probe on the PR
        # that landed this).
        on_remote = git_in_clone(clone, ["branch", "-r", "--contains", cursor], check=False)
        if on_remote.returncode != 0:
            return None
        if on_remote.stdout.strip():
            break
        if oldest_unpushed is not None and _commit_carries_preserved_baseline_trailer(
            clone, cursor
        ):
            # `cursor` is an applied baseline the worker committed on top of,
            # not HEAD itself (that case already returned above) -- stop
            # here so the baseline's own content is excluded exactly as the
            # single-commit case already excluded it when the baseline sat
            # directly at HEAD^.
            break
        oldest_unpushed = cursor
        parent = git_in_clone(clone, ["rev-parse", "--verify", f"{cursor}^"], check=False)
        if parent.returncode != 0:
            return None
        cursor = parent.stdout.strip()
    if oldest_unpushed is None:
        return None
    return f"{oldest_unpushed}^"


def _committed_but_unpushed_diff(clone: Path) -> str:
    """The diff of HEAD's own commit, for a clone whose committed work
    ``_committed_but_unpushed_base`` recognizes as the worker's own (OPS-62,
    OPS-82, dev.finding 71418f56 -- see that function for the cases)."""
    base = _committed_but_unpushed_base(clone)
    if base is None:
        return ""
    diff = git_in_clone(clone, ["diff", base, "HEAD"], check=False)
    return diff.stdout if diff.returncode == 0 else ""


def _committed_but_unpushed_paths(clone: Path) -> list[str]:
    """The paths ``_committed_but_unpushed_diff`` would report, name-only.

    ``changed_paths``'s fallback, so a worker that commits its own patch is
    visible to the changed-nothing guard exactly where it is already visible
    to the preservation path."""
    base = _committed_but_unpushed_base(clone)
    if base is None:
        return []
    result = git_in_clone(clone, ["diff", "--name-only", base, "HEAD"], check=False)
    return result.stdout.split() if result.returncode == 0 else []


def save_failure_patch(clone: Path, task_id: str) -> Path | None:
    """Write the clone's full diff (tracked + untracked) somewhere durable.

    Returns the path, or None if there was nothing to save. Best-effort: a
    failure to preserve must not mask the failure being reported.
    """
    if not (clone / ".git").exists():
        return None
    try:
        git_in_clone(clone, ["add", "-A"])
        diff = git_in_clone(clone, ["diff", "--cached"]).stdout
        if not diff.strip():
            diff = _committed_but_unpushed_diff(clone)
        if not diff.strip():
            return None
        dest = FAILURE_PATCH_DIR / f"{task_id[:8]}-{int(time.time())}.patch"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(diff)
        return dest
    except CloneGitControlTampered:
        # A tamper refusal is not an ordinary preservation failure (dev.finding
        # 79db3113 part c2, AC-3) -- it must reach record_failure_activity so
        # the failure note says preservation was skipped, not silently return
        # None as if there had simply been nothing to save.
        raise
    except Exception:  # noqa: BLE001 - never let preservation mask the real error
        return None


def task_state(task: dict) -> str:
    return task.get("state") or "pending"


def resolve_task_release_states(
    sub: BeadStore, pending: Iterable[dict]
) -> dict[str, guards.ReleaseState]:
    """The release each pending task delivers, resolved once per pick.

    Only tasks carrying a resolvable outgoing ``delivers`` edge appear in the
    result. A task with none — waived, or filed before REL-1 existed — has
    nothing for ``guards.release_block_reason`` to check, which is the point:
    the filter must not strand work it cannot resolve (see that function's
    docstring).

    Raises whatever ``sub`` raises. A pick that resolves part of the pending
    population and then fails must not act on that partial map — see
    ``pick_task``, which catches this and refuses the whole selection rather
    than gating some tasks and silently not gating the rest.
    """
    releases_by_id = {release["id"]: release for release in sub.list_beads("arch", "release")}
    resolved: dict[str, guards.ReleaseState] = {}
    for task in pending:
        task_id = task["id"]
        links = sub.list_links(task_id, direction="outgoing", link_type="delivers")
        for link in links:
            release = releases_by_id.get(link.get("target_id"))
            if release is None:
                continue
            release_content = release.get("content") or {}
            resolved[task_id] = guards.ReleaseState(
                ref=str(release_content.get("ref") or release["id"]),
                state=str(release.get("state") or ""),
            )
            break
    return resolved


def pick_task(
    sub: BeadStore,
    task_id: str | None,
    tasks: list[dict] | None = None,
) -> dict | None:
    all_tasks = list(tasks) if tasks is not None else sub.list_tasks()
    if task_id:
        matches = [t for t in all_tasks if t["id"] == task_id]
        if not matches:
            raise DispatchError(f"task {task_id} not found")
        return matches[0]

    pending = [task for task in all_tasks if task_state(task) == "pending"]
    # Oldest first: a task that has waited longest goes next.
    pending.sort(key=lambda t: t.get("created_at") or "")

    try:
        release_by_task_id = resolve_task_release_states(sub, pending)
    except Exception as exc:  # noqa: BLE001 - unreadable release state must refuse
        # the whole pick, not silently ungate every task — the same posture
        # file_task.py's _check_release_traceability takes when it cannot list
        # open releases (2026-08-30 decision record, D3).
        print(f"  refuse — release state could not be read: {type(exc).__name__}: {exc}")
        return None

    return queue_order.first_selectable(sub, pending, all_tasks, release_by_task_id)


def parse_bead_timestamp(value: str) -> datetime:
    """Parse a substrate timestamp and normalize it to UTC."""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def doing_task_exceeds_threshold(bead: dict, now: datetime, threshold: timedelta) -> bool:
    """Return whether a dev.task is visibly stuck, without reclaiming it.

    This is deliberately detection-only. Reclaim belongs to Amendment 24 Phase 4,
    where Temporal heartbeat timeouts own durable execution state. This predicate
    writes no lease, holder, deadline, timeout, attempt count, or retry metadata
    into bead content.
    """
    if bead.get("state") != "doing":
        return False

    updated_at = bead.get("updated_at")
    if not updated_at:
        return False

    age = as_utc(now) - parse_bead_timestamp(updated_at)
    return age > threshold


def stuck_doing_tasks(beads: list[dict], now: datetime, threshold: timedelta) -> list[dict]:
    """Filter beads to stale ``doing`` dev.tasks using no substrate state."""
    return [
        bead
        for bead in beads
        if doing_task_exceeds_threshold(bead, now, threshold)
    ]


#: Terminal states excluded from the open population, mirroring
#: scanner.py:open_task_state — "open" is "not yet landed or shelved", not a
#: single named state. "superseded" joined "done"/"archived" when the
#: dev.task machine gained it as a terminal state (apps/substrate/src/
#: routes.py:STATE_MACHINES): a superseded bead is dead, and neither the
#: traceability report nor the claim loop should count it as open work.
CLOSED_TASK_STATES = frozenset({"done", "archived", "superseded"})


def is_open_task(task: dict) -> bool:
    return task_state(task) not in CLOSED_TASK_STATES


def classify_traceability(task: dict) -> str:
    """Classify one dev.task's requirement traceability.

    A task carrying both ``requirement_refs`` and a waiver is ref-carrying —
    file_task.py's waiver check (file_task.py:86) only reads the waiver when
    no ref is present, so the waiver population here means "waiver only" to
    match what filing actually enforces.
    """
    content = task.get("content") or {}
    if content.get("requirement_refs"):
        return "refs"
    if str(content.get("requirement_refs_waived") or "").strip():
        return "waiver"
    return "neither"


@dataclass(frozen=True)
class TraceabilityReport:
    generated_at: str
    total_open: int
    with_refs: int
    with_waiver_only: int
    with_neither: int
    neither_ids: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total_open": self.total_open,
            "with_refs": self.with_refs,
            "with_waiver_only": self.with_waiver_only,
            "with_neither": self.with_neither,
            "neither_ids": list(self.neither_ids),
        }


def build_traceability_report(
    tasks: list[dict], now: datetime | None = None
) -> TraceabilityReport:
    """Count the open dev.task population's requirement traceability (AC-5).

    Completed or done beads are excluded entirely — this measures the open
    population that can still reach a worker, not the historical record.
    """
    open_tasks = [t for t in tasks if is_open_task(t)]
    with_refs = 0
    with_waiver = 0
    neither_ids: list[str] = []
    for t in open_tasks:
        classification = classify_traceability(t)
        if classification == "refs":
            with_refs += 1
        elif classification == "waiver":
            with_waiver += 1
        else:
            neither_ids.append(t["id"])
    return TraceabilityReport(
        generated_at=as_utc(now or datetime.now(timezone.utc)).isoformat(),
        total_open=len(open_tasks),
        with_refs=with_refs,
        with_waiver_only=with_waiver,
        with_neither=len(neither_ids),
        neither_ids=tuple(sorted(neither_ids)),
    )


def report_traceability(sub: BeadStore, now: datetime | None = None) -> int:
    """Print the AC-5 traceability count for the open dev.task population.

    Read-only, like report_stuck_doing_tasks. Exit nonzero when any open task
    carries neither refs nor a waiver, so this can gate a later rollout
    without being rewritten.
    """
    tasks = sub.list_tasks()
    report = build_traceability_report(tasks, now)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.with_neither == 0 else 1


def report_stuck_doing_tasks(
    sub: BeadStore,
    threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    now: datetime | None = None,
) -> int:
    """Print dev.task beads stuck in ``doing``; never mutates beads.

    Two distinct signals, both read-only: a ``doing`` bead whose age exceeds
    ``threshold_minutes`` ("stuck"), and a ``doing`` bead carrying no claim
    note at all ("stranded") — the shape a note write leaves behind when it
    fails after claim_activity's pending->doing transition already landed and
    is not compensated. Stranded is reported regardless of age; the danger it
    names is that nothing else distinguishes it from live work.
    """
    threshold = timedelta(minutes=threshold_minutes)
    checked_at = as_utc(now or datetime.now(timezone.utc))
    tasks = sub.list_tasks()
    doing = [task for task in tasks if task_state(task) == "doing"]
    stuck = stuck_doing_tasks(doing, checked_at, threshold)

    for task in sorted(stuck, key=lambda item: item.get("updated_at") or ""):
        content = task.get("content") or {}
        updated_at = task.get("updated_at") or ""
        age_minutes = int(
            (checked_at - parse_bead_timestamp(updated_at)).total_seconds() // 60
        )
        title = content.get("title") or "(untitled)"
        print(
            f"{task['id']}\tstate=doing\tupdated_at={updated_at}\t"
            f"age_minutes={age_minutes}\ttitle={title}"
        )

    # A doing bead with no claim note recorded is not "work that has run
    # long" (the age-based check above) — it is the partial-claim shape a
    # note write can leave behind when it fails after claim_activity's
    # pending->doing transition already landed and compensation itself also
    # fails. Reported regardless of age: the whole danger is that nothing
    # else on the platform distinguishes it from live work, and a retrying
    # workflow can pile a fresh claim onto a different bead within seconds.
    stranded = [task for task in doing if not guards.has_claim_note(sub.list_notes(task["id"]))]
    for task in sorted(stranded, key=lambda item: item.get("updated_at") or ""):
        content = task.get("content") or {}
        updated_at = task.get("updated_at") or ""
        title = content.get("title") or "(untitled)"
        print(
            f"{task['id']}\tstranded\tstate=doing\tupdated_at={updated_at}\t"
            f"reason=no_claim_note_recorded\ttitle={title}"
        )

    for task in sorted(tasks, key=lambda item: item.get("created_at") or ""):
        if task_state(task) != "pending":
            continue
        superseded_by = guards.superseding_task_ids(task, tasks)
        if not superseded_by:
            continue
        print(
            f"{task['id']}\tskipped\tstate=pending\t"
            f"reason={guards.superseded_reason(superseded_by)}"
        )

    # Graph faults (OPS-13): a bead whose predecessor is superseded is
    # structurally incapable of ever clearing is_runnable's ordering check,
    # and it stays that way until something repoints it. --supersede
    # repoints dependents it finds itself, but this catches the edge from any
    # other path — a hand-edited predecessor_bead_ids, a repoint that failed
    # halfway — so it is visible in the same read-only pass rather than only
    # discoverable by reading each bead.
    for task in sorted(tasks, key=lambda item: item.get("created_at") or ""):
        if not is_open_task(task):
            continue
        for predecessor_id, replacement_ids in guards.superseded_predecessors(task, tasks):
            print(
                f"{task['id']}\tgraph_fault\tpredecessor={predecessor_id}\t"
                f"reason={guards.superseded_reason(replacement_ids)}"
            )

    return 0


@dataclass(frozen=True)
class ExecutionOwnership:
    """Temporal's answer about whether a live execution still owns a task."""

    owned: bool
    detail: str


def temporal_task_execution_ownership(
    task_id: str,
    *,
    client_factory=None,
    lookup_timeout_seconds: int = 60,
) -> ExecutionOwnership:
    """Ask Temporal whether a running dispatcher execution still owns ``task_id``.

    The bead is the stale record in this failure mode, so this deliberately
    reads the orchestrator. The schedule tells us which dispatcher workflows are
    in flight; the workflow history tells us whether the claim activity ever
    returned this task id to that execution.

    If Temporal cannot be queried or a running workflow cannot be inspected,
    the safe answer is "owned": the release command must prove absence before it
    writes.
    """
    try:
        return asyncio.run(
            _temporal_task_execution_ownership(
                task_id,
                client_factory=client_factory,
                lookup_timeout_seconds=lookup_timeout_seconds,
            )
        )
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"temporal_lookup_failed={detail[:500]}") from exc


async def _default_temporal_client_factory(address: str, namespace: str):
    from temporalio.client import Client

    return await Client.connect(address, namespace=namespace)


async def _temporal_task_execution_ownership(
    task_id: str,
    *,
    client_factory,
    lookup_timeout_seconds: int,
) -> ExecutionOwnership:
    schedule_id = os.environ.get("FACTORY_DISPATCH_SCHEDULE_ID", "factory-dispatcher-dev")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "dev")
    address = os.environ.get("TEMPORAL_URL")
    if not address:
        raise RuntimeError("TEMPORAL_URL is required")

    factory = client_factory or _default_temporal_client_factory
    client_result = factory(address, namespace)
    if inspect.isawaitable(client_result):
        client = await asyncio.wait_for(
            client_result,
            timeout=lookup_timeout_seconds,
        )
    else:
        client = client_result
    description = await asyncio.wait_for(
        client.get_schedule_handle(schedule_id).describe(),
        timeout=lookup_timeout_seconds,
    )
    info = getattr(description, "info", None)
    running = _workflow_refs_from_running_actions(
        getattr(info, "running_actions", ()) or ()
    )
    if not running:
        return ExecutionOwnership(
            False,
            f"schedule_id={schedule_id}; running_dispatcher_workflows=0",
        )

    matches: list[str] = []
    unknown: list[str] = []
    for workflow_id, run_id in running:
        ref = f"{workflow_id}/{run_id}" if run_id else workflow_id
        handle = client.get_workflow_handle(workflow_id, run_id=run_id or None)
        try:
            contains_task = await _workflow_history_contains_task_id(
                handle,
                task_id,
                timeout_seconds=lookup_timeout_seconds,
            )
        except Exception:
            unknown.append(ref)
            continue
        if contains_task:
            matches.append(ref)

    if matches:
        return ExecutionOwnership(True, f"running_workflow={','.join(matches)}")
    if unknown:
        raise RuntimeError("running_workflow_uninspectable=" + ",".join(unknown))
    return ExecutionOwnership(
        False,
        f"schedule_id={schedule_id}; running_workflows_checked={len(running)}; "
        f"task_id_not_in_histories={task_id}",
    )


def _workflow_refs_from_running_actions(actions: Any) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for action in actions:
        action_detail = _field(action, "action") or action
        workflow_id = _field(action, "workflow_id") or _field(
            action_detail, "workflow_id"
        )
        if not workflow_id:
            continue
        run_id = (
            _field(action, "first_execution_run_id")
            or _field(action_detail, "first_execution_run_id")
            or _field(action, "run_id")
            or _field(action_detail, "run_id")
            or _field(action, "workflow_run_id")
            or _field(action_detail, "workflow_run_id")
            or ""
        )
        ref = (str(workflow_id), str(run_id))
        if ref not in refs:
            refs.append(ref)
    return refs


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


async def _workflow_history_contains_task_id(
    handle: Any,
    task_id: str,
    *,
    timeout_seconds: int,
) -> bool:
    async def scan() -> bool:
        events = handle.fetch_history_events(
            rpc_timeout=timedelta(seconds=timeout_seconds),
        )
        if inspect.isawaitable(events):
            events = await events
        if hasattr(events, "__aiter__"):
            async for event in events:
                if _object_contains_string(event, task_id):
                    return True
        else:
            for event in events:
                if _object_contains_string(event, task_id):
                    return True
        return False

    return await asyncio.wait_for(scan(), timeout=timeout_seconds)


def _object_contains_string(value: Any, needle: str, seen: set[int] | None = None) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, bytes):
        try:
            return needle in value.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(value, (int, float, bool)):
        return False

    seen = seen or set()
    value_id = id(value)
    if value_id in seen:
        return False
    seen.add(value_id)

    if isinstance(value, dict):
        return any(
            _object_contains_string(key, needle, seen)
            or _object_contains_string(child, needle, seen)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_object_contains_string(child, needle, seen) for child in value)
    if hasattr(value, "ListFields"):
        return any(
            _object_contains_string(child, needle, seen)
            for _field_descriptor, child in value.ListFields()
        )
    if hasattr(value, "__dict__"):
        return _object_contains_string(vars(value), needle, seen)
    return needle in str(value)


#: WorkflowExecutionStatus names (temporalio.client.WorkflowExecutionStatus) that mean
#: the run is over. Compared by name, not by importing the enum, so this stays
#: reachable from workdir_owner_is_alive without adding a hard temporalio import to a
#: module worker_revision.py and other non-Temporal callers already depend on staying
#: free of one.
_TERMINAL_WORKFLOW_STATUS_NAMES = frozenset(
    {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
)


def temporal_workflow_run_is_alive(
    workflow_id: str,
    run_id: str,
    *,
    client_factory=None,
    lookup_timeout_seconds: int = 15,
) -> bool:
    """Ask Temporal whether ``workflow_id``/``run_id`` is still executing (OPS-60).

    The backstop ``workdir_owner_is_alive`` asks of Temporal, not the process table,
    for an activity-created workdir: the worker daemon's own pid outlives every run it
    ever executes, so pid liveness alone cannot tell "this run finished" from "the
    daemon that once ran it is still up."

    Raises rather than guessing on a lookup failure -- the caller decides the safe
    default, which is "presume alive": a sweep or a concurrent-clone defer check must
    never reclaim a workdir it could not prove is dead.
    """
    try:
        return asyncio.run(
            _temporal_workflow_run_is_alive(
                workflow_id,
                run_id,
                client_factory=client_factory,
                lookup_timeout_seconds=lookup_timeout_seconds,
            )
        )
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise RuntimeError(f"temporal_lookup_failed={detail[:500]}") from exc


async def _temporal_workflow_run_is_alive(
    workflow_id: str,
    run_id: str,
    *,
    client_factory,
    lookup_timeout_seconds: int,
) -> bool:
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "dev")
    address = os.environ.get("TEMPORAL_URL")
    if not address:
        raise RuntimeError("TEMPORAL_URL is required")

    factory = client_factory or _default_temporal_client_factory
    client_result = factory(address, namespace)
    if inspect.isawaitable(client_result):
        client = await asyncio.wait_for(client_result, timeout=lookup_timeout_seconds)
    else:
        client = client_result
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    description = await asyncio.wait_for(
        handle.describe(), timeout=lookup_timeout_seconds
    )
    status = getattr(description, "status", None)
    status_name = (getattr(status, "name", None) or str(status)).upper()
    return status_name not in _TERMINAL_WORKFLOW_STATUS_NAMES


def lookup_pull_request(pr_url: str, cfg: Config) -> PullRequestStatus:
    """Read the PR state from GitHub.

    GitHub's ``MERGED`` state only says the PR merged into its configured base.
    The caller must still prove the merge commit is on ``main`` before moving
    the bead to ``done``.
    """
    proc = run(
        [
            "gh",
            "pr",
            "view",
            pr_url,
            "--repo",
            cfg.repo,
            "--json",
            "number,state,mergeCommit,url,headRefOid",
        ],
        cwd=cfg.repo_root,
        timeout=60,
    )
    payload = json.loads(proc.stdout or "{}")
    merge_commit = payload.get("mergeCommit") or {}
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    return PullRequestStatus(
        number=payload.get("number"),
        url=payload.get("url") or pr_url,
        state=payload.get("state") or "UNKNOWN",
        merge_commit=oid,
        head_sha=payload.get("headRefOid") or None,
    )


#: Cue phrases modeled on the real note this check was written for
#: (dev.task ad0a0f95: "the re-spec waits for #888 to settle ... Trigger:
#: #888 reaching a terminal state"). A bare "#888" mention alone is not
#: evidence of a live hold — notes cite PR numbers as history constantly —
#: so one of these SHALL also be present in the same note before a named PR
#: is treated as a trigger (AC-4). Deliberately small and literal rather than
#: broadened to sentiment or a longer synonym list: guards.RELEASES_WORK_FIELD
#: already documents a 2026-08-29 incident where free-text keyword
#: classification ("wait for rest of work to merge/clear", "hold until things
#: clear") wrongly closed a question it should not have. That incident was in
#: a *deciding* context; this one is report-only (AC-2), which is why the same
#: technique is safe to reuse here — the worst a false positive costs is one
#: extra line in front of a person, never a released hold.
HELD_NOTE_LANGUAGE_PATTERN = re.compile(
    r"waits?\s+for|waiting\s+for|blocked\s+on|trigger\s*:|held\s+for|"
    r"hold\s+until|pending\s+on",
    re.IGNORECASE,
)

#: A PR number cited anywhere in a note's free-text body, e.g. "#888". Not
#: ``_PR_NUMBER_PATTERN`` (below) — that pattern is anchored to match a whole
#: ``preserved_attempts`` ``pr`` field value in one shot, not to find
#: zero-or-more mentions embedded in prose.
HELD_NOTE_PR_REFERENCE_PATTERN = re.compile(r"#(\d+)\b")


def _newest_note(notes: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The note with the latest ``created_at``, never by list position.

    ``_find_supersession_note`` (below) already documents why: a prior
    version of that function trusted ``list_notes``'s return order and read
    a bead's very first matching note as its latest. Keying on ``created_at``
    here avoids reintroducing that under a new caller.
    """
    newest: dict[str, Any] | None = None
    newest_created_at = ""
    for note in notes:
        created_at = str(note.get("created_at") or "")
        if newest is None or created_at >= newest_created_at:
            newest = note
            newest_created_at = created_at
    return newest


def _held_trigger_pr_numbers(note_body: str) -> list[int]:
    """PR numbers this note's hold language names as its trigger, if any.

    Empty unless hold language is present — a note mentioning a PR number in
    passing, with no hold language anywhere in it, names no trigger (AC-4).
    """
    if not HELD_NOTE_LANGUAGE_PATTERN.search(note_body):
        return []
    seen: list[int] = []
    for match in HELD_NOTE_PR_REFERENCE_PATTERN.finditer(note_body):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


@dataclass(frozen=True)
class HeldPrFinding:
    """One held bead naming one PR that has reached a terminal state."""

    task_id: str
    title: str
    pr_number: int
    pr_state: str


def held_beads_with_terminal_prs(
    sub: BeadStore,
    cfg: Config,
    lookup_pr=lookup_pull_request,
) -> tuple[list[HeldPrFinding], list[HeldPrFinding]]:
    """Held ``pending``/``failed`` beads whose hold names a PR that closed.

    Returns ``(closed_without_merging, merged)`` — kept as two lists, never
    one "terminal" bucket, because the two mean opposite things for the hold:
    merged usually DISCHARGES it (the trigger fired as expected), closed-
    without-merging VOIDS its premise (the trigger fired the one way that
    makes the wait permanent) — AC-1.

    A bead is "held" here only when its newest note carries both hold
    language and a ``#<number>`` PR reference together (AC-4); an ``OPEN``
    named PR means the bead is still correctly waiting and is not reported at
    all. Strictly read-only: no note, transition, or content patch is ever
    written — a hold is a judgement the outer loop made in prose, and only
    the outer loop can retire it (AC-2). ``lookup_pr`` is the same injected
    PR-state collaborator shape ``reconcile_review_tasks`` already takes, so
    this never makes a network call on its own; a monkeypatched collaborator
    proves the call happens, never that live GitHub agrees with the fixture.
    """
    closed: list[HeldPrFinding] = []
    merged: list[HeldPrFinding] = []

    for task in sub.list_tasks():
        if task_state(task) not in ("pending", "failed"):
            continue

        newest = _newest_note(sub.list_notes(task["id"]))
        if newest is None:
            continue
        body = str((newest.get("content") or {}).get("body") or "")
        pr_numbers = _held_trigger_pr_numbers(body)
        if not pr_numbers:
            continue

        title = str((task.get("content") or {}).get("title") or "(untitled)")
        for number in pr_numbers:
            try:
                pr = lookup_pr(str(number), cfg)
            except Exception:  # noqa: BLE001 - one bad lookup must not stop the sweep
                continue
            state = pr.state.upper()
            if state == "MERGED":
                merged.append(HeldPrFinding(task["id"], title, number, state))
            elif state == "CLOSED":
                closed.append(HeldPrFinding(task["id"], title, number, state))
            # OPEN, or anything unrecognised: still correctly waiting.

    return closed, merged


def report_held_prs(
    sub: BeadStore,
    cfg: Config,
    lookup_pr=lookup_pull_request,
) -> int:
    """Print held beads whose named PR reached a terminal state; write nothing.

    Read-only, like ``report_stuck_doing_tasks``. Exits nonzero only when a
    closed-without-merging finding exists — that is the shape that strands a
    bead forever; a merged discharge candidate is informational, a human
    still decides whether to act on it.
    """
    closed, merged = held_beads_with_terminal_prs(sub, cfg, lookup_pr=lookup_pr)

    for finding in sorted(closed, key=lambda f: f.task_id):
        print(
            f"{finding.task_id}\tclosed_without_merging\tpr=#{finding.pr_number}\t"
            f"title={finding.title}"
        )
    for finding in sorted(merged, key=lambda f: f.task_id):
        print(
            f"{finding.task_id}\tmerged_discharge_candidate\tpr=#{finding.pr_number}\t"
            f"title={finding.title}"
        )

    return 1 if closed else 0


TRUNK_CHECK_REF = "refs/factory/trunk-check"


def merge_commit_is_ancestor_of_main(merge_commit: str, cfg: Config) -> bool:
    """Return whether ``merge_commit`` is reachable from the current trunk tip.

    Fetches ``cfg.remote`` by URL rather than a remote named ``origin``
    (OPS-115): inside a dispatch clone ``origin`` is rewritten to ``cfg.remote``
    at clone time (``make_clone``, :1261) so the two name the same URL, but in
    ``cfg.repo_root`` itself -- the worker checkout under launchd -- ``origin``
    is the operator's local source mirror, not GitHub, and is only as fresh as
    the mirror's last pull. ``git fetch`` accepts a URL wherever it accepts a
    remote name, so naming ``cfg.remote`` directly answers "is this commit on
    GitHub's trunk" regardless of which remote name the checkout carries. The
    fetch lands in ``TRUNK_CHECK_REF`` with ``--no-write-fetch-head`` (git >=
    2.29) so ``FETCH_HEAD``'s age still means what ``_refresh_stale_tracking_ref``
    takes it to mean.
    """
    # Into a private ref, and never FETCH_HEAD: _refresh_stale_tracking_ref
    # reads FETCH_HEAD's age as its proxy for "the tracking ref was just
    # refreshed", and a URL fetch updates no tracking ref. reconcile runs this
    # check before the attempt clones (workflow_core.run_dispatch_sequence), so
    # writing FETCH_HEAD here would make the inner hop skip its fetch on the
    # pass after every merge and clone from a stale base (the #802 gate, F1).
    run(
        [
            GIT,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            cfg.remote,
            f"+refs/heads/{cfg.base_ref}:{TRUNK_CHECK_REF}",
        ],
        cwd=cfg.repo_root,
        timeout=300,
    )
    proc = run(
        [GIT, "merge-base", "--is-ancestor", merge_commit, TRUNK_CHECK_REF],
        cwd=cfg.repo_root,
        check=False,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise DispatchError(
        f"could not test whether {merge_commit} is on {cfg.base_ref}: "
        f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
    )


# ---------------------------------------------------------------------------
# release resolution — a dev.task binds to arch.release by a `delivers` edge,
# never by a string in its own content ("everything in this release" has to
# be one indexed query — apps/substrate/src/schemas.py's ArchReleaseContent
# docstring). This is the one place both file_task.py (at filing) and
# --bind-release (for work already in flight) resolve a release_ref, so the
# two paths can never validate a different shape.
# ---------------------------------------------------------------------------

#: Mirrors DevTaskContent's own `_RELEASE_TASK_REF` (apps/substrate/src/
#: schemas.py) with capturing groups added for parsing. Kept in lockstep by
#: eye rather than import — the substrate model is out of this app's reach —
#: so a release ref this pattern accepts is a release ref the schema accepts.
_RELEASE_TASK_REF_PATTERN = re.compile(r"^(R\d{2}\.\d{2})(?:/(O-\d+))?$")

#: A release still accepting work. "closing"/"released"/"abandoned" are not:
#: filing a task against a release that is already wrapping up would land
#: work the notes generator has already stopped watching for.
OPEN_RELEASE_STATES = frozenset({"planned", "in_flight"})


class ReleaseResolutionError(RuntimeError):
    """A release_ref (filing or --bind-release) does not resolve, or could not
    be checked because the substrate was unreachable."""


@dataclass(frozen=True)
class ReleaseBinding:
    """What a resolved ``release_ref`` commits a task to.

    ``release_id`` is the substrate id the ``delivers`` edge targets;
    ``release_ref`` and ``outcome_id`` are the human-readable ref and the bare
    outcome id (``O-2``, never ``R26.01/O-2`` — see DevTaskContent.outcome_ref)
    for content and messages.
    """

    release_id: str
    release_ref: str
    outcome_id: str | None = None


def parse_release_ref(ref: str) -> tuple[str, str | None]:
    """Split ``R26.01`` or ``R26.01/O-2`` into ``(release_ref, outcome_id)``.

    Pure string shape — no resolution. Raises ``ValueError`` on anything else,
    including a well-formed-looking id from a different scheme (this repo's
    gitops-resilience sprints are already ``R1``/``R2.2``, which is exactly
    the ambiguity ``ArchReleaseContent.release_ref_must_match_the_id_scheme``
    exists to keep out).
    """
    match = _RELEASE_TASK_REF_PATTERN.match(ref.strip())
    if not match:
        raise ValueError(
            f"release_ref {ref!r} is not shaped like a release reference; "
            "expected R26.01 or R26.01/O-2"
        )
    return match.group(1), match.group(2)


def list_open_release_refs(sub: BeadStore) -> list[str]:
    """Refs of every ``arch.release`` bead currently in an open state."""
    releases = sub.list_beads("arch", "release")
    return sorted(
        {
            ref
            for release in releases
            if release.get("state") in OPEN_RELEASE_STATES
            for ref in [(release.get("content") or {}).get("ref")]
            if ref
        }
    )


def _safe_list_open_release_refs(sub: BeadStore) -> list[str]:
    """Best-effort enrichment of a refusal that is already firing.

    A failure here must never mask or replace the refusal it was meant to
    explain, so it degrades to an empty list rather than raising.
    """
    try:
        return list_open_release_refs(sub)
    except Exception:  # noqa: BLE001 - enrichment only, never the gate itself
        return []


def resolve_release_ref(ref: str, sub: BeadStore) -> ReleaseBinding:
    """Resolve ``ref`` against the substrate, or raise :class:`ReleaseResolutionError`.

    Resolution, not shape (the schema already accepts a well-formed ref that
    points nowhere — see file_task._check_traceability's docstring for the
    identical argument about requirement refs). Three ways to fail: the ref is
    not shaped like a release reference; the release id it names is not a bead
    the substrate holds; or it names an outcome the release's charter does not
    declare. A substrate that cannot be reached at all is its own failure,
    distinct from "not found" — the caller is told to retry, not to pick a
    different ref.
    """
    try:
        release_part, outcome_id = parse_release_ref(ref)
    except ValueError as exc:
        raise ReleaseResolutionError(str(exc)) from exc

    try:
        release_bead = sub.find_bead("arch", "release", release_part)
    except Exception as exc:  # noqa: BLE001 - unreachable substrate must refuse, not crash
        raise ReleaseResolutionError(
            f"release_ref {ref!r} could not be resolved: the substrate could "
            f"not be reached ({type(exc).__name__}: {exc}). Refusing rather "
            "than binding to nothing."
        ) from exc

    if release_bead is None:
        open_refs = _safe_list_open_release_refs(sub)
        raise ReleaseResolutionError(
            f"release_ref {ref!r} names a release the substrate does not hold "
            f"({release_part!r}).\nopen releases: "
            f"{', '.join(open_refs) or '(none open)'}"
        )

    if outcome_id:
        declared = {
            str(outcome.get("id"))
            for outcome in (release_bead.get("content") or {}).get("outcomes") or []
        }
        if outcome_id not in declared:
            raise ReleaseResolutionError(
                f"release_ref {ref!r} names outcome {outcome_id!r}, which "
                f"{release_part} does not declare.\ndeclared outcomes: "
                f"{', '.join(sorted(declared)) or '(none)'}"
            )

    return ReleaseBinding(
        release_id=release_bead["id"], release_ref=release_part, outcome_id=outcome_id
    )


# The substrate's dev.task state machine (routes.py:246) allows pending -> doing
# only, so binding walks the legal path rather than widening the machine. A new
# edge would be an ARCHITECTURE.md §3.1 change and would need an amendment; the
# path below is the same one the work actually took.
BIND_PATH_TO_REVIEW: dict[str, tuple[str, ...]] = {
    "pending": ("doing", "review"),
    "doing": ("review",),
    "review": (),
    "failed": ("pending", "doing", "review"),
}


def bind_task_to_pull_request(
    cfg: Config,
    sub: BeadStore,
    task_id: str,
    pr_url: str,
    dry_run: bool = False,
    lookup_pr=lookup_pull_request,
) -> int:
    """Bind a bead to a PR opened outside the dispatcher, and move it to review.

    Only the dispatcher stamps ``pr_url``, at propose time. Work done by hand —
    every attended run while the worker has no quota — is therefore invisible to
    ``reconcile_review_tasks`` forever: the bead stays pending while its change
    sits on main, and the next unattended drain claims it and spends an attempt
    redoing merged work.

    Binding is explicit and recorded rather than inferred. Guessing which bead a
    commit belongs to is how the board started reporting fake work as complete
    on 2026-08-06.
    """
    tasks = [item for item in sub.list_tasks() if item["id"] == task_id]
    if not tasks:
        print(f"{task_id}\tnot_found")
        return 1
    task = tasks[0]
    content = dict(task.get("content") or {})
    state = task.get("state") or "pending"

    if state not in BIND_PATH_TO_REVIEW:
        print(f"{task_id}\trefused\tstate={state}\treason=not_bindable")
        return 1

    existing = str(content.get("pr_url") or "").strip()
    if existing and existing != pr_url:
        print(f"{task_id}\trefused\treason=already_bound\tbound_to={existing}")
        return 1

    try:
        pr = lookup_pr(pr_url, cfg)
    except Exception as exc:  # noqa: BLE001 - a bead bound to a PR that does not
        # resolve can never be settled by the reconciler, so refuse rather than
        # record a binding nothing downstream can act on.
        print(f"{task_id}\trefused\treason=pr_lookup_failed\tpr={pr_url}\terror={exc}")
        return 1

    pr_ref = f"#{pr.number}" if pr.number is not None else pr.url
    path = BIND_PATH_TO_REVIEW[state]

    if dry_run:
        hops = "->".join((state, *path)) if path else f"{state} (already review)"
        print(f"{task_id}\twould_bind\tpr={pr_ref}\tpath={hops}")
        return 0

    content["pr_url"] = pr.url
    pr_refs = list(content.get("pr_refs") or [])
    if pr.url not in pr_refs:
        pr_refs.append(pr.url)
    content["pr_refs"] = pr_refs
    sub.patch_content(task_id, content, operator_actor())

    current = state
    for target in path:
        transition_state_checked(sub, task_id, current, target, operator_actor())
        current = target

    sub.add_note(
        task_id,
        "status",
        f"Bound to {pr.url} ({pr_ref}) by an operator: this work was done outside "
        f"the dispatcher, so nothing stamped pr_url at propose time. Moved "
        f"{'->'.join((state, *path)) if path else state} so the reconciler can "
        "settle it when the merge commit reaches main.",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "bind-pr"),
    )
    print(f"{task_id}\tbound\tpr={pr_ref}\tstate={current}")
    return 0


def bind_task_to_release(
    sub: BeadStore,
    task_id: str,
    release_ref: str,
    dry_run: bool = False,
    resolve=resolve_release_ref,
) -> int:
    """Bind ``task_id`` to a release outside the filing path (Amendment 30).

    Same edge-and-content split ``file_task.py`` uses at filing: the release
    itself is a ``delivers`` edge (dev.task -> arch.release), never written
    into content, and only the bare outcome id — meaningless without the edge
    — is persisted as ``content.outcome_ref``. This exists so work already in
    flight, filed with a waiver or before this mechanism existed, can be bound
    to its objective without refiling.

    A duplicate edge (409 from ``add_link``) is treated as already-bound, not
    an error — the same idempotency ``record_applies_links`` already relies on
    for the ``applies`` edge, so a re-run of this command is not destructive.
    """
    tasks = [item for item in sub.list_tasks() if item["id"] == task_id]
    if not tasks:
        print(f"{task_id}\tnot_found")
        return 1

    try:
        binding = resolve(release_ref, sub)
    except ReleaseResolutionError as exc:
        print(f"{task_id}\trefused\treason=release_ref_unresolved\t{exc}")
        return 1

    ref_display = binding.release_ref + (
        f"/{binding.outcome_id}" if binding.outcome_id else ""
    )

    if dry_run:
        print(f"{task_id}\twould_bind\trelease={ref_display}")
        return 0

    try:
        sub.add_link(task_id, binding.release_id, "delivers", operator_actor())
    except Exception as exc:  # noqa: BLE001 - a duplicate edge is success, not failure
        if store_error_status(exc) != 409:
            print(f"{task_id}\trefused\treason=delivers_link_rejected\terror={exc}")
            return 1

    if binding.outcome_id:
        content = dict(tasks[0].get("content") or {})
        content["outcome_ref"] = binding.outcome_id
        sub.patch_content(task_id, content, operator_actor())

    sub.add_note(
        task_id,
        "status",
        f"Bound to release {ref_display} by an operator, outside the filing "
        "path: work already in flight can be bound to its objective without "
        "refiling.",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "bind-release"),
    )
    print(f"{task_id}\tbound\trelease={ref_display}")
    return 0


def start_attended_task(
    sub: BeadStore,
    task_id: str,
    reason: str,
    dry_run: bool = False,
) -> int:
    """Move a bead to ``doing`` for work an operator is about to do by hand.

    The dispatcher stamps ``doing`` when it claims a task, so a worker run is
    visible on the board while it happens. Attended work had no equivalent, and
    the board is a live view of the beads — so between 2026-08-09 and
    2026-08-12 ten merged PRs (#339 to #348) passed through with no bead at
    all, and the board showed an idle factory while the estate changed under it.

    This is the front half of the attended lifecycle; ``--bind-pr`` is the back
    half, and the reconciler settles it once the PR reaches main.
    """
    if not reason.strip():
        print(f"{task_id}\trefused\treason=no_reason_given")
        return 1

    tasks = [item for item in sub.list_tasks() if item["id"] == task_id]
    if not tasks:
        print(f"{task_id}\tnot_found")
        return 1
    task = tasks[0]
    state = task.get("state") or "pending"
    if state != "pending":
        print(f"{task_id}\trefused\tstate={state}\treason=not_claimable")
        return 1

    if dry_run:
        print(f"{task_id}\twould_start\tpending->doing\tactor={operator_actor()}")
        return 0

    sub.add_note(
        task_id,
        "status",
        f"{guards.CLAIM_NOTE_PREFIX}for ATTENDED work by {operator_actor()}. No worker "
        f"was dispatched and no model ran on this claim. {reason.strip()}",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "start"),
    )
    transition_state_checked(sub, task_id, "pending", "doing", operator_actor())
    print(f"{task_id}\tstarted\tpending->doing\tactor={operator_actor()}")
    return 0


#: What actually applies when a bead's state is not one --requeue handles.
#: Named in the refusal so an operator is pointed at the right command instead
#: of re-trying --requeue against a state it will never accept.
REQUEUE_INAPPLICABLE_STATE_HINT: dict[str, str] = {
    "doing": "in flight; use --release-stranded once Temporal shows no owner",
    "review": "awaiting its PR; use --reconcile-review, or --bind-pr if the "
    "PR is known",
    "done": "already landed; no requeue applies",
}

#: Same bound as the retry prompt (guards.MAX_FAILURE_REASONS_IN_PROMPT), and
#: for the same reason: a bead can carry seven attempts' worth of notes, and an
#: unbounded terminal dump is as unreadable as no display at all.
REQUEUE_FAILURE_DISPLAY_LIMIT = guards.MAX_FAILURE_REASONS_IN_PROMPT


def _print_recent_failure_notes(sub: BeadStore, task_id: str) -> None:
    """Print the failure(s) a requeue's reason is supposed to be judged against.

    2026-08-22: an operator requeue swept four failed beads under one
    rationale ("attempts burned during the auth outage; worker fixed").
    dispatch.py --requeue performed the failed -> pending transition and wrote
    that reason atomically (#473's front door, working as designed) but never
    showed the failure the reason was meant to address. For three beads the
    rationale held. For the fourth (b4338922) its notes had said
    "sandbox_apply: Operation not permitted" since before the outage window
    the reason cited - a defect the worker fix never touched - and the fresh
    budget burned to zero in minutes against the identical error. The note
    that would have caught this existed and was never opened, because nothing
    put it in front of the person acting (the 2026-08-09 #342 failure shape,
    here in an operator tool).

    This is called before the note write and the state transition below, not
    after, so the reason an operator gives is always written against a
    failure they were actually shown.
    """
    failures = guards.prior_failures(sub.list_notes(task_id))
    print(f"{task_id}\tfailure_history:")
    if not failures:
        print("    (no recorded Run failed notes for this bead)")
        return
    for failure in failures[-REQUEUE_FAILURE_DISPLAY_LIMIT:]:
        lines = failure.reason.splitlines() or [""]
        print(f"    Attempt {failure.ordinal}: {lines[0]}")
        for line in lines[1:]:
            print(f"    {line}")


#: Extra ``dev.note`` content field the requeue barrier note carries whenever
#: the bead has recorded ``preserved_attempts`` (dispatch.PRESERVED_ATTEMPTS_FIELD):
#: the operator's explicit "keep" or "discard" choice. Absent when the bead
#: carries no preserved work — there is nothing for the barrier to decide.
REQUEUE_PRESERVED_CHOICE_FIELD = "preserved_choice"

#: Valid values for ``requeue_task``'s ``preserved_choice`` parameter.
REQUEUE_PRESERVED_CHOICES = ("keep", "discard")


def requeue_task(
    sub: BeadStore,
    task_id: str,
    reason: str,
    dry_run: bool = False,
    assume_yes: bool = False,
    preserved_choice: str | None = None,
) -> int:
    """Return a task to pending and restart its retry budget, on the record.

    This is the one front door for a requeue. Requeuing a ``failed`` bead is
    two things, not one: the ``failed -> pending`` compare-and-set transition,
    and the ``resets_attempts`` status note (guards.REQUEUE_MARKER_FIELD). A
    note written by hand outside this function restarts the budget on a bead
    whose state never moves — it stays out of the pending pool, and nothing
    about the note itself says so. That is what a 2026-08-20 incident took a
    state read and two hours to notice, after an operator posted the note by
    the old convention and neither drain nor the note explained why nothing
    was claimable.

    The budget is represented locally by ``Run failed:`` notes. This writes a
    barrier note and refuses without a reason, so "these attempts were not spent
    on this task's work" is a claim someone made rather than a side effect.

    Order matters and is deliberate: the note is written first. #341's bind
    wrote content first and left a partial state — bound with no explanation —
    when the note write failed after. Writing the note first means a note
    failure here leaves the bead exactly as it was found. The mirror case, the
    transition failing after the note lands, is still possible (a concurrent
    CAS loss); that is caught and reported as a distinct, immediately visible
    failure rather than allowed to look like success, which is the failure
    mode this function exists to close off.

    It exists because the judgement is real and sometimes right. On 2026-08-07
    twelve beads burned their whole budget against a stale local ``main`` whose
    clone carried a pre-#313 Change-kind guard, so declared verification failed
    regardless of the worker's output. Requeuing them was correct.

    Reads before it writes (2026-08-22): the bead's recorded failure note(s)
    print before the note write or the transition, in every mode including
    ``dry_run``, so the reason given is always written against a failure
    that was actually shown. When run at an interactive terminal this also
    pauses for a yes/no confirmation after the display; ``assume_yes`` skips
    the pause for scripted sweeps but never the display, and the pause is
    skipped automatically when stdin is not a terminal (a script or a test).

    A bead carrying recorded ``preserved_attempts`` (PRESERVED_ATTEMPTS_FIELD
    -- work a prior gate-closed PR left recoverable, see
    ``reconcile_review_tasks``) makes requeue a second decision, not just the
    budget restart: ``preserved_choice`` must be ``"keep"`` or ``"discard"``,
    and refuses without one. There is no path that strips preserved work
    without a recorded ``"discard"``, and no path that requeues such a bead
    with neither choice recorded -- the choice IS the barrier's behavior, not
    a note beside it (.factory/design.md decision 2). A bead with no
    preserved work takes ``preserved_choice=None`` exactly as before this
    parameter existed; there is nothing for it to decide.
    """
    if not reason.strip():
        print(f"{task_id}\trefused\treason=no_reason_given")
        return 1
    if preserved_choice is not None and preserved_choice not in REQUEUE_PRESERVED_CHOICES:
        raise ValueError(
            f"preserved_choice must be one of {REQUEUE_PRESERVED_CHOICES} or None, "
            f"got {preserved_choice!r}"
        )

    tasks = sub.list_tasks()
    matches = [item for item in tasks if item["id"] == task_id]
    if not matches:
        print(f"{task_id}\tnot_found")
        return 1
    task = matches[0]
    state = task.get("state") or "pending"
    superseded_by = guards.superseding_task_ids(task, tasks)
    if superseded_by:
        print(
            f"{task_id}\trefused\treason=superseded_by\t"
            f"replacement={','.join(superseded_by)}"
        )
        return 1
    if state not in ("pending", "failed"):
        hint = REQUEUE_INAPPLICABLE_STATE_HINT.get(state, "no requeue path applies")
        print(
            f"{task_id}\trefused\tstate={state}\treason=not_requeueable\t"
            f"applies_instead={hint}"
        )
        return 1

    content = task.get("content") or {}
    preserved = list(content.get(PRESERVED_ATTEMPTS_FIELD) or [])
    if preserved and preserved_choice is None:
        print(
            f"{task_id}\trefused\treason=preserved_choice_required\t"
            f"preserved_attempts={len(preserved)}\t"
            "use --keep-preserved or --discard-preserved"
        )
        return 1

    _print_recent_failure_notes(sub, task_id)

    if dry_run:
        hop = "failed->pending" if state == "failed" else "pending (unchanged)"
        preserved_note = f"\tpreserved_choice={preserved_choice}" if preserved else ""
        print(
            f"{task_id}\twould_requeue\t{hop}\tretry_budget_restarted=true"
            f"{preserved_note}"
        )
        return 0

    if not assume_yes and sys.stdin.isatty():
        try:
            answer = input(f"{task_id}\trequeue against the failure shown above? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print(f"{task_id}\trefused\treason=operator_declined")
            return 1

    note_extra: dict[str, Any] = {guards.REQUEUE_MARKER_FIELD: True}
    note_body = (
        f"Returned to pending by an operator, with the retry budget restarted. "
        f"Recorded failures before this point were judged not to have been spent "
        f"on this task's work. Reason: {reason.strip()}"
    )
    if preserved:
        note_extra[REQUEUE_PRESERVED_CHOICE_FIELD] = preserved_choice
        note_body += (
            f" Preserved work from {len(preserved)} closed attempt(s): "
            f"{preserved_choice}."
        )
    sub.add_note(
        task_id,
        "status",
        note_body,
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "requeue"),
        **note_extra,
    )
    if preserved and preserved_choice == "discard":
        try:
            sub.patch_content(
                task_id,
                {
                    **{k: v for k, v in content.items() if k != PRESERVED_ATTEMPTS_FIELD},
                    # The discard is remembered on content, not only in the note,
                    # because retro-intake reads content (pr_url) and would
                    # otherwise re-record the very PR the operator threw away.
                    PRESERVED_ATTEMPTS_DISCARDED_FIELD: [
                        *(content.get(PRESERVED_ATTEMPTS_DISCARDED_FIELD) or []),
                        *preserved,
                    ],
                },
                operator_actor(),
            )
        except Exception as exc:  # noqa: BLE001 - a recorded discard must be loud, not silent.
            print(
                f"{task_id}\tinconsistent\treason=discard_recorded_but_strip_failed\t"
                f"error={str(exc)[:500]}"
            )
            return 1
    if state == "failed":
        try:
            transition_state_checked(sub, task_id, "failed", "pending", operator_actor())
        except Exception as exc:  # noqa: BLE001 - a stuck note must be loud, not silent.
            print(
                f"{task_id}\tinconsistent\tstate=failed\t"
                f"reason=transition_failed_after_note_recorded\t"
                f"error={str(exc)[:500]}"
            )
            return 1

    print(f"{task_id}\trequeued\tstate=pending\tretry_budget_restarted=true")
    return 0


def release_stranded_task(
    sub: BeadStore,
    task_id: str,
    reason: str,
    dry_run: bool = False,
    ownership_check=temporal_task_execution_ownership,
) -> int:
    """Release an unowned ``doing`` task without restarting its retry budget.

    This is not ``--requeue``. Requeue says recorded attempts were not spent on
    this task's work and resets them. Release says the live owner is gone and
    moves the stale claim out of ``doing`` while preserving the budget already
    recorded on the bead and in its failure notes.
    """
    if not reason.strip():
        print(f"{task_id}\trefused\treason=no_reason_given")
        return 1

    tasks = [item for item in sub.list_tasks() if item["id"] == task_id]
    if not tasks:
        print(f"{task_id}\tnot_found")
        return 1
    task = tasks[0]
    state = task.get("state") or "pending"
    if state != "doing":
        print(f"{task_id}\trefused\tstate={state}\treason=not_stranded_doing")
        return 1

    try:
        ownership = ownership_check(task_id)
    except Exception as exc:  # noqa: BLE001 - inability to prove absence is refusal.
        print(
            f"{task_id}\trefused\treason=execution_lookup_failed\t"
            f"error={str(exc)[:500]}"
        )
        return 1
    if ownership.owned:
        print(
            f"{task_id}\trefused\treason=execution_still_running\t"
            f"evidence={ownership.detail}"
        )
        return 1

    notes = sub.list_notes(task_id)
    attempts = len(guards.prior_failures(notes))
    target = (
        "failed"
        if retry_policy.retry_exhausted(attempts)
        else "pending"
    )

    if dry_run:
        print(
            f"{task_id}\twould_release_stranded\tdoing->{target}\t"
            f"recorded_failures={attempts}\tevidence={ownership.detail}"
        )
        return 0

    sub.add_note(
        task_id,
        "status",
        f"Released from doing by an operator after Temporal showed no running "
        f"dispatcher execution owns this task. State moved doing->{target}. "
        f"Retry budget was not restarted: recorded Run failed notes since "
        f"requeue={attempts}. Temporal evidence: {ownership.detail}. "
        f"Reason: {reason.strip()}",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "release-stranded"),
    )
    sub.set_state(task_id, target, operator_actor())

    print(
        f"{task_id}\treleased_stranded\tstate={target}\t"
        f"recorded_failures={attempts}"
    )
    return 0


#: States a bead may legally become "superseded" from (apps/substrate/src/
#: routes.py:STATE_MACHINES). A bead already "doing" or in "review" is live
#: work, not dead work — the machine has no edge for it.
SUPERSEDABLE_STATES = frozenset({"pending", "failed"})

#: Two components write this claim in two vocabularies, and both must be
#: readable: file_task.py's --supersede has always written "Superseded by
#: dev.task <id>, filed with --supersede.", while guards.superseded_reason()
#: (used by --supersede's own ordering-block reporting and by the SRE's
#: settlement notes) writes "superseded by bead <id>[, <id>...]". The keyword
#: is required, not optional — no note anywhere omits it, and an optional
#: keyword only widens the pattern to catch unrelated prose that happens to
#: contain "superseded by" ahead of some other token (guards.py's own
#: fail-closed posture, rule 2). The id is matched as a bare non-punctuation
#: token rather than assumed hex/UUID-shaped, since a bead id's format is not
#: this pattern's business; sentence-trailing punctuation is stripped after
#: the match instead (see ``_clean_replacement_id``), so a note ending in a
#: full stop does not fold the period into the id.
SUPERSESSION_NOTE_PATTERN = re.compile(
    r"superseded by (?:dev\.task|bead)\s+([^\s,]+)", re.IGNORECASE
)


def _clean_replacement_id(raw: str) -> str:
    """Strip sentence-trailing punctuation a captured id is not part of."""
    return raw.rstrip(".,;:!?")


def _find_supersession_note(notes: list[dict]) -> tuple[dict, str] | None:
    """The most recent note claiming supersession, and the successor id it names.

    Selected by each matching note's own ``created_at``, examined across the
    whole list — never by scan order or position. A prior version of this
    function scanned ``reversed(notes)`` on the documented belief that
    ``list_notes`` returns oldest-first; it in fact returns newest-first, so
    that reversal read a bead's very first supersession claim as though it
    were the latest. Keying on ``created_at`` instead of position makes the
    result correct regardless of what order the caller's list happens to be
    in, rather than swapping one order assumption for another.
    """
    best: tuple[dict, str] | None = None
    best_created_at = ""
    for note in notes:
        body = str((note.get("content") or {}).get("body") or "")
        match = SUPERSESSION_NOTE_PATTERN.search(body)
        if not match:
            continue
        created_at = str(note.get("created_at") or "")
        if best is None or created_at >= best_created_at:
            best = (note, _clean_replacement_id(match.group(1)))
            best_created_at = created_at
    return best


def backfill_superseded_task(
    sub: BeadStore, task_id: str, dry_run: bool = False
) -> int:
    """Make a bead's state agree with a supersession note it already carries.

    2026-08-22: three pending beads and two failed duplicates each carry a
    note naming the bead that replaced them, written by --supersede before it
    performed the state transition (the defect this state addition closes).
    The note is durable evidence an operator or the filing tool already
    recorded; this command does not invent a supersession, it only makes state
    agree with one already on the record — reading the note is the whole
    backfill, and a bead with no such note is refused rather than guessed at.

    Follows the same note-then-transition idiom as requeue_task: a note is
    written first (citing the evidence), the transition is attempted second,
    and a transition failure after the note lands is reported as loudly
    inconsistent rather than allowed to look like a clean backfill.
    """
    all_tasks = list(sub.list_tasks())
    tasks = [item for item in all_tasks if item["id"] == task_id]
    if not tasks:
        print(f"{task_id}\tnot_found")
        return 1
    task = tasks[0]
    state = task.get("state") or "pending"
    if state not in SUPERSEDABLE_STATES:
        print(
            f"{task_id}\trefused\tstate={state}\t"
            "reason=only_pending_or_failed_can_be_backfilled"
        )
        return 1

    notes = sub.list_notes(task_id)
    found = _find_supersession_note(notes)
    if found is None:
        print(
            f"{task_id}\trefused\treason=no_supersession_note_found\t"
            "this bead carries no note naming a replacement — nothing to backfill"
        )
        return 1
    _note, replacement_id = found

    # A parse failure that still yields a syntactically plausible token (the
    # 'bead' vocabulary defect returned the literal word "bead") must not
    # become a durable record — refuse rather than transition when nothing on
    # the board is that id.
    if not any(item.get("id") == replacement_id for item in all_tasks):
        print(
            f"{task_id}\trefused\treason=replacement_bead_not_found\t"
            f"replacement={replacement_id}"
        )
        return 1

    if dry_run:
        print(
            f"{task_id}\twould_backfill\t{state}->superseded\t"
            f"replacement={replacement_id}"
        )
        return 0

    sub.add_note(
        task_id,
        "status",
        f"Backfilled to superseded by an operator, citing the existing note "
        f"recording replacement by dev.task {replacement_id}.",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "backfill-superseded"),
    )
    try:
        transition_state_checked(sub, task_id, state, "superseded", operator_actor())
    except Exception as exc:  # noqa: BLE001 - a stuck note must be loud, not silent.
        print(
            f"{task_id}\tinconsistent\tstate={state}\t"
            f"reason=transition_failed_after_note_recorded\t"
            f"error={str(exc)[:500]}"
        )
        return 1

    print(f"{task_id}\tbackfilled\tstate=superseded\treplacement={replacement_id}")
    return 0


def announce_stranded_doing_tasks(
    sub: BeadStore,
    threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    now: datetime | None = None,
    alert_policy: "stranded_alerts.notify.AlertPolicy | None" = None,
) -> None:
    """Raise the declared alert for every doing bead with no live owner shown.

    Detection is not reimplemented here: this reuses the exact two checks
    ``report_stuck_doing_tasks`` already runs read-only — ``stuck_doing_tasks``
    (age past ``threshold_minutes``) and ``guards.has_claim_note`` (no claim
    note recorded at all). Only the missing announcement is new.
    """
    checked_at = as_utc(now or datetime.now(timezone.utc))
    doing = sub.list_tasks(state="doing")
    stuck = stuck_doing_tasks(doing, checked_at, timedelta(minutes=threshold_minutes))
    stuck_ids = {task["id"] for task in stuck}

    for task in stuck:
        age_minutes = int(
            (checked_at - parse_bead_timestamp(task.get("updated_at") or "")).total_seconds()
            // 60
        )
        stranded_alerts.announce_stranded_doing_task(
            task, age_minutes, "age_exceeded_threshold", policy=alert_policy
        )

    for task in doing:
        if task["id"] in stuck_ids:
            continue
        if guards.has_claim_note(sub.list_notes(task["id"])):
            continue
        updated_at = task.get("updated_at") or ""
        age_minutes = (
            int((checked_at - parse_bead_timestamp(updated_at)).total_seconds() // 60)
            if updated_at
            else 0
        )
        stranded_alerts.announce_stranded_doing_task(
            task, age_minutes, "no_claim_note_recorded", policy=alert_policy
        )


#: Content field a gate-closure transition (reconcile_review_tasks's CLOSED
#: branch) and a retro-intake re-dispatch both key on -- see
#: .factory/design.md decision 1. A list of ``{"pr": <url>, "head_sha": <sha>}``,
#: oldest attempt first. Deliberately not ``preserved_ref``: GitHub retains
#: ``refs/pull/<number>/head`` for a closed PR indefinitely regardless of what
#: happens to the branch ref, so nothing here needs to fetch-and-push a copy of
#: its own to survive the next attempt's force-with-lease reclaim.
PRESERVED_ATTEMPTS_FIELD = "preserved_attempts"
#: Where `requeue --discard-preserved` records what it removed, so retro-intake
#: (`recovered_preserved_attempts`) never quietly restores a discarded PR from
#: `pr_url` at the next claim -- the #787/#790 gate F1.
PRESERVED_ATTEMPTS_DISCARDED_FIELD = "preserved_attempts_discarded"


def _preserved_attempts_after_closure(
    content: dict[str, Any], pr: PullRequestStatus
) -> list[dict[str, Any]] | None:
    """``content[PRESERVED_ATTEMPTS_FIELD]`` with ``pr``'s head appended.

    None when there is nothing to add: no head_sha to record (the lookup
    predates headRefOid, or GitHub returned none), or this exact PR is already
    recorded (a second reconcile pass over the same bead, e.g. after a note or
    transition failure on the first attempt at this same closure).
    """
    if not pr.head_sha:
        return None
    pr_ref = pr.url or (f"#{pr.number}" if pr.number is not None else "")
    if not pr_ref:
        return None
    existing = list(content.get(PRESERVED_ATTEMPTS_FIELD) or [])
    if any(entry.get("pr") == pr_ref for entry in existing):
        return None
    existing.append({"pr": pr_ref, "head_sha": pr.head_sha})
    return existing


def reconcile_review_tasks(
    cfg: Config,
    sub: BeadStore,
    dry_run: bool = False,
    actor: str | None = None,
    lookup_pr=lookup_pull_request,
    is_merge_commit_ancestor=merge_commit_is_ancestor_of_main,
    stuck_threshold_minutes: int = DEFAULT_STUCK_THRESHOLD_MINUTES,
    now: datetime | None = None,
    alert_policy: "stranded_alerts.notify.AlertPolicy | None" = None,
) -> int:
    """Move review tasks to done only when their PR's merge commit reached trunk.

    Also the scheduled drain's only pass over the full task list every cycle
    (``activities/dispatch_steps.py::reconcile_activity`` runs this before
    every claim), so it is where both stranded shapes ``--report-stuck``
    already knows about get announced rather than left for a human to find:
    a doing bead with no live owner shown (via ``announce_stranded_doing_tasks``
    above) and a review bead with no ``pr_url``, right below.
    """
    if not dry_run:
        announce_stranded_doing_tasks(
            sub, stuck_threshold_minutes, now, alert_policy
        )

    tasks = sub.list_tasks(state="review")
    settled_by = actor or operator_actor()
    exit_code = 0

    for task in sorted(tasks, key=lambda item: item.get("updated_at") or ""):
        if task.get("state") not in (None, "review"):
            continue

        content = task.get("content") or {}
        pr_url = str(content.get("pr_url") or "").strip()
        title = content.get("title") or "(untitled)"
        task_id = task["id"]

        if not pr_url:
            print(f"{task_id}\tmissing_pr_url\tstate=review\ttitle={title}")
            if not dry_run:
                stranded_alerts.announce_review_missing_pr_url(task, policy=alert_policy)
            continue

        try:
            pr = lookup_pr(pr_url, cfg)
        except Exception as exc:  # noqa: BLE001 - report and keep reconciling
            print(f"{task_id}\tpr_lookup_failed\tpr={pr_url}\terror={exc}")
            exit_code = 1
            continue

        pr_ref = f"#{pr.number}" if pr.number is not None else pr.url
        pr_state = pr.state.upper()
        if pr_state == "OPEN":
            print(f"{task_id}\tpr_state={pr.state}\tpr={pr_ref}\tleft=review")
            continue
        if pr_state == "CLOSED":
            if dry_run:
                print(
                    f"{task_id}\twould_transition\treview->failed\tpr={pr_ref}\t"
                    f"pr_url={pr.url}\treason=closed_without_merging"
                )
                continue

            # Preservation intake comes first, before the note or the
            # transition: the closed PR's head is the work that survived, and
            # GitHub is the only place it provably still exists once the
            # bead's next attempt reclaims this branch with its own
            # force-with-lease push (.factory/design.md decision 1). A
            # failure here must not let the bead reach `failed` with nothing
            # preserved, so it refuses this bead's reconcile this pass rather
            # than falling through to the note/transition -- the next pass
            # retries the whole closure from scratch.
            preserved = _preserved_attempts_after_closure(content, pr)
            if preserved is not None:
                try:
                    sub.patch_content(
                        task_id,
                        {**content, PRESERVED_ATTEMPTS_FIELD: preserved},
                        settled_by,
                    )
                except Exception as exc:  # noqa: BLE001 - store patch failure
                    print(f"{task_id}\tpreserve_failed\tpr={pr_ref}\terror={exc}")
                    exit_code = 1
                    continue

            try:
                sub.add_note(
                    task_id,
                    "status",
                    f"Review ended without shipping: PR {pr_ref} ({pr.url}) was "
                    "closed without merging. State moved review->failed so this "
                    "work does not silently become runnable again.",
                    settled_by,
                    provenance=provenance_for_operator_action(
                        task_id,
                        "reconcile-review",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - store note failure
                print(f"{task_id}\tnote_failed\tpr={pr_ref}\terror={exc}")
                exit_code = 1
                continue

            try:
                transition_state_checked(sub, task_id, "review", "failed", settled_by)
            except Exception as exc:  # noqa: BLE001 - store transition failure
                status = store_error_status(exc)
                if status == 404:
                    print(
                        f"{task_id}\ttransition_endpoint_missing\tedge=review->failed\t"
                        f"pr={pr_ref}\tleft=review\tmessage=merged_is_not_deployed"
                    )
                elif status == 409:
                    print(
                        f"{task_id}\ttransition_conflict\texpected=review\t"
                        "left_unchanged=true"
                    )
                elif status == 422:
                    print(
                        f"{task_id}\ttransition_rejected\tedge=review->failed\t"
                        f"pr={pr_ref}\tleft=review\terror={exc}"
                    )
                else:
                    print(
                        f"{task_id}\ttransition_failed\tedge=review->failed\t"
                        f"pr={pr_ref}\terror={exc}"
                    )
                exit_code = 1
                continue

            print(
                f"{task_id}\ttransitioned\treview->failed\tpr={pr_ref}\t"
                "reason=closed_without_merging"
            )
            continue
        if not pr.merged:
            print(
                f"{task_id}\tunrecognised_pr_state\tpr_state={pr.state}\t"
                f"pr={pr_ref}\tleft=review"
            )
            exit_code = 1
            continue

        if not pr.merge_commit:
            print(f"{task_id}\tmerged_without_merge_commit\tpr={pr_ref}\tleft=review")
            exit_code = 1
            continue

        try:
            on_main = is_merge_commit_ancestor(pr.merge_commit, cfg)
        except Exception as exc:  # noqa: BLE001 - report and keep reconciling
            print(
                f"{task_id}\tancestor_check_failed\tpr={pr_ref}\t"
                f"merge_commit={pr.merge_commit}\terror={exc}"
            )
            exit_code = 1
            continue

        if not on_main:
            print(
                f"{task_id}\torphaned_merge\tpr={pr_ref}\t"
                f"merge_commit={pr.merge_commit}\tnot_ancestor_of={cfg.base_ref}"
            )
            continue

        if dry_run:
            print(
                f"{task_id}\twould_transition\treview->done\tpr={pr_ref}\t"
                f"merge_commit={pr.merge_commit}"
            )
            continue

        try:
            transition_state_checked(sub, task_id, "review", "done", settled_by)
        except Exception as exc:  # noqa: BLE001 - store transition failure
            if store_error_status(exc) == 409:
                print(
                    f"{task_id}\ttransition_conflict\texpected=review\t"
                    "left_unchanged=true"
                )
            else:
                print(f"{task_id}\ttransition_failed\terror={exc}")
            exit_code = 1
            continue

        print(
            f"{task_id}\ttransitioned\treview->done\tpr={pr_ref}\t"
            f"merge_commit={pr.merge_commit}"
        )

    return exit_code


# ---------------------------------------------------------------------------
# preserved-attempt consumption
#
# The write side lives just above (PRESERVED_ATTEMPTS_FIELD,
# _preserved_attempts_after_closure, reconcile_review_tasks's CLOSED branch).
# It shipped in PR #751 and nothing since read it back: a retry still began
# from an empty clone with a prior attempt's work recoverable only as prose
# in a closed PR nobody re-opened. This section is the read side -- retro-
# intake for a bead closed before the write side existed, applying the
# recorded (or recovered) head as a commit before the worker begins, and
# naming a conflict on the bead rather than silently discarding it.
#
# This docstring, not .factory/design.md, is where that reasoning has to
# live. The predecessor's own worker wrote its design for this exact gap to
# .factory/design.md -- git-excluded by make_clone's .git/info/exclude entry
# (dev.task 07a99270) -- and it never reached a diff. This bead being filed
# at all is the proof that a design recorded only outside the diff does not
# survive to the next reader.
# ---------------------------------------------------------------------------

#: Git trailer stamped on the commit `apply_preserved_baseline` creates.
#: `_committed_but_unpushed_diff` (above, in `save_failure_patch`'s
#: neighborhood) greps HEAD's own commit message for this before treating an
#: unpushed HEAD as "the worker's own committed patch": a baseline applied
#: here and never built on (the worker changed nothing) leaves HEAD pointed
#: at exactly this commit, and without this check that fallback returns the
#: baseline's own diff as though it were this attempt's fresh work -- the
#: OPS-49 attribution corruption this whole mechanism exists to avoid
#: repeating, one layer further from the guard already written to catch it.
#: Verified against this repo's HEAD rather than cited as a bare line number:
#: the on_remote check this trailer check sits beside is
#: `_committed_but_unpushed_diff`'s first git call, and that function's own
#: docstring names the OPS-49 incident it was written for.
PRESERVED_BASELINE_TRAILER = "Factory-preserved-baseline"

#: A PR number out of either shape `preserved_attempts` stores under "pr":
#: a full URL (`_preserved_attempts_after_closure` prefers `pr.url`) or a
#: bare `#<n>` fallback when GitHub returned no URL at write time.
_PR_NUMBER_PATTERN = re.compile(r"/pull/(\d+)\b|^#(\d+)$")


def _pr_number_from_ref(pr_ref: str) -> int | None:
    """The PR number named by a preserved_attempts ``pr`` entry, or None.

    Never guesses: an unparseable ref means there is nothing to fetch, not a
    number to approximate.
    """
    match = _PR_NUMBER_PATTERN.search(pr_ref.strip())
    if not match:
        return None
    group = match.group(1) or match.group(2)
    return int(group) if group else None


def recovered_preserved_attempts(
    cfg: Config,
    content: dict[str, Any],
    lookup_pr=lookup_pull_request,
) -> list[dict[str, Any]] | None:
    """RETRO-INTAKE: recover a closed PR's head for a bead whose closure
    predates the live write (``reconcile_review_tasks``'s CLOSED branch,
    PR #751) or otherwise missed it.

    ``content["pr_url"]`` survives a bead's whole life once a PR opens
    (``propose_activity`` stamps it and nothing since clears it), so a bead
    re-dispatched with no ``preserved_attempts`` of its own but a ``pr_url``
    on file has exactly one prior PR to ask GitHub about. GitHub retains
    ``refs/pull/<n>/head`` indefinitely regardless of what happened to the
    branch afterward -- the same fact that makes the live write recoverable
    at all (PRESERVED_ATTEMPTS_FIELD's own docstring) -- so a PR closed
    before this function existed is exactly as recoverable as one closed
    after, provided this runs before the next attempt reclaims the branch.

    Returns ``None`` when there is nothing to recover: preserved_attempts is
    already populated (nothing for retro-intake to add -- this is not a
    merge, it only fires when there is nothing already on record), no
    pr_url is on file, the URL does not parse to a PR number, the lookup
    itself fails (never fatal -- a GitHub hiccup must not block an otherwise
    ordinary re-dispatch), or the PR is not CLOSED (an OPEN or MERGED
    pr_url on a pending/failed bead is a different inconsistency, not this
    function's job to fix). Reuses ``_preserved_attempts_after_closure`` for
    the actual list construction, so retro-intake and the live write share
    one field, one shape, one dedup rule -- never a second key.

    Deliberately not ``guards.prior_failures``: that reads only ``kind ==
    "status"`` failure notes and is requeue-barriered, an unrelated
    mechanism for an unrelated question (how many attempts has an operator
    already charged to this bead), not what a closed PR's head was.
    """
    if content.get(PRESERVED_ATTEMPTS_FIELD):
        return None
    pr_url = str(content.get("pr_url") or "").strip()
    if not pr_url or _pr_number_from_ref(pr_url) is None:
        return None
    if any(
        entry.get("pr") == pr_url
        for entry in (content.get(PRESERVED_ATTEMPTS_DISCARDED_FIELD) or [])
    ):
        # An operator's recorded --discard-preserved names this PR: honour it.
        return None
    try:
        pr = lookup_pr(pr_url, cfg)
    except Exception:  # noqa: BLE001 - a lookup failure is not this bead's problem
        return None
    if pr.state.upper() != "CLOSED":
        return None
    return _preserved_attempts_after_closure(content, pr)


@dataclass(frozen=True)
class PreservedBaselineOutcome:
    """What `apply_preserved_baseline` did to the clone, for the caller's note.

    ``conflict`` is set only when preserved work existed but could not be
    applied -- the caller records it on the bead (AC4) and dispatch still
    proceeds, from the clone `apply_preserved_baseline` has already reset to
    plain ``cfg.base_ref``. ``applied=False, conflict=None`` covers both "no
    preserved work to apply" (a first attempt, AC5) and "the preserved head
    introduced nothing beyond its own merge-base" -- neither is a problem to
    report, so neither writes a note.
    """

    applied: bool
    pr_refs: tuple[str, ...] = ()
    conflict: str | None = None


def _head_commit_message(clone: Path) -> str:
    return git_in_clone(clone, ["log", "-1", "--format=%B", "HEAD"], check=False).stdout


def _commit_carries_preserved_baseline_trailer(clone: Path, commit: str) -> bool:
    """Whether ``commit`` (any ref/sha, not just HEAD) is one
    `apply_preserved_baseline` created.

    Generalizes `_head_is_preserved_baseline_commit` to an arbitrary commit
    so `_committed_but_unpushed_base`'s multi-commit walk can recognize a
    baseline sitting one or more commits back in history, not only at HEAD
    itself."""
    message = git_in_clone(clone, ["log", "-1", "--format=%B", commit], check=False).stdout
    return f"{PRESERVED_BASELINE_TRAILER}:" in message


def _head_is_preserved_baseline_commit(clone: Path) -> bool:
    """Whether HEAD is itself a commit `apply_preserved_baseline` created.

    The one thing `_committed_but_unpushed_diff` needs, to tell "a baseline
    the worker never built on" (HEAD unchanged since the apply) apart from
    "the worker's own committed-but-unpushed patch" (HEAD advanced past the
    baseline) -- both present identically to that function's older checks
    alone: an unpushed HEAD commit with a resolvable parent.
    """
    return _commit_carries_preserved_baseline_trailer(clone, "HEAD")


def _preserved_baseline_commit_message(pr_refs: Iterable[str]) -> str:
    named = ", ".join(pr_refs) or "(unknown)"
    return (
        f"Preserved baseline from {named}\n"
        "\n"
        "Applied by the dispatcher before this attempt's worker began (see "
        "PRESERVED_ATTEMPTS_FIELD/apply_preserved_baseline). If this attempt "
        "contributes nothing beyond this commit, the changed-nothing guard "
        "reports it as such -- this content is not this attempt's own work. "
        "If this bead carries a request-changes review note or a reasoned "
        "requeue, that is this attempt's task, not re-verifying this commit "
        "against the acceptance criteria.\n"
        "\n"
        f"{PRESERVED_BASELINE_TRAILER}: true\n"
    )


def apply_preserved_baseline(
    cfg: Config,
    sub: BeadStore,
    clone: Path,
    task: dict[str, Any],
    lookup_pr=lookup_pull_request,
) -> PreservedBaselineOutcome:
    """Apply a bead's recorded preserved work as a commit before its worker runs.

    Called once per dispatch, from ``run_activity``, immediately before the
    worker subprocess starts -- after ``isolate``/``preflight`` have already
    run against a plain ``cfg.base_ref`` clone unchanged by anything here, so
    a first attempt (no ``preserved_attempts`` and no recoverable ``pr_url``)
    takes the first ``return`` below and touches the clone not at all (AC5).

    Otherwise: the latest entry in ``preserved_attempts`` (oldest-first per
    PRESERVED_ATTEMPTS_FIELD's own docstring; earlier entries are named
    alongside it in both the commit message and the bead note, never
    dropped) is fetched from ``refs/pull/<n>/head`` and merged (``git merge
    --squash``, a three-way merge using git's own conflict machinery) onto
    the clone's current HEAD, then committed as a single ordinary commit
    with one parent -- the same base_ref tip a first attempt would have
    started from, so ``_committed_but_unpushed_diff``'s existing HEAD^-diff
    assumption and ``open_pull_request``'s own commit-on-top both keep
    working unchanged.

    A conflict beyond what that merge can resolve mechanically is recorded
    on the bead by name (AC4) and the clone is reset to exactly the state a
    first attempt would have found it in -- dispatch proceeds from there,
    never silently and never raised as an ordinary failure (a conflict is
    not this attempt's fault and must not spend its retry budget).
    """
    task_id = task["id"]
    content = task.get("content") or {}
    preserved = list(content.get(PRESERVED_ATTEMPTS_FIELD) or [])

    if not preserved:
        recovered = recovered_preserved_attempts(cfg, content, lookup_pr=lookup_pr)
        if recovered is None:
            return PreservedBaselineOutcome(applied=False)
        try:
            sub.patch_content(
                task_id,
                {**content, PRESERVED_ATTEMPTS_FIELD: recovered},
                operator_actor(),
            )
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort, not a gate
            print(f"{task_id}\tretro_intake_patch_failed\terror={exc}")
        preserved = recovered

    baseline = preserved[-1]
    pr_ref = str(baseline.get("pr") or "")
    head_sha = str(baseline.get("head_sha") or "")
    pr_refs = tuple(str(entry.get("pr")) for entry in preserved if entry.get("pr"))
    base_head = git_in_clone(clone, ["rev-parse", "HEAD"]).stdout.strip()

    def _record_conflict(reason: str) -> PreservedBaselineOutcome:
        git_in_clone(clone, ["reset", "--hard", base_head], check=False)
        git_in_clone(clone, ["clean", "-fdq"], check=False)
        sub.add_note(
            task_id,
            "status",
            f"Preserved work from {pr_ref or '(unknown)'} could not be applied "
            f"before this attempt began, so dispatch proceeds from a plain "
            f"{cfg.base_ref} instead: {reason}",
            operator_actor(),
            provenance=provenance_for_operator_action(
                task_id, "apply-preserved-baseline"
            ),
        )
        return PreservedBaselineOutcome(applied=False, pr_refs=pr_refs, conflict=reason)

    number = _pr_number_from_ref(pr_ref) if pr_ref else None
    if not pr_ref or not head_sha or number is None:
        return _record_conflict(
            f"preserved_attempts entry is missing a usable pr/head_sha: {baseline!r}"
        )

    fetch = git_in_clone(
        clone,
        ["fetch", "-q", "origin", f"refs/pull/{number}/head"],
        check=False,
        timeout=120,
    )
    if fetch.returncode != 0:
        return _record_conflict(
            f"could not fetch refs/pull/{number}/head from origin: "
            f"{(fetch.stderr or fetch.stdout or '').strip()[:300]}"
        )
    fetched_sha = git_in_clone(clone, ["rev-parse", "FETCH_HEAD"], check=False).stdout.strip()
    if not fetched_sha:
        return _record_conflict(f"refs/pull/{number}/head resolved to nothing")
    if fetched_sha != head_sha:
        return _record_conflict(
            f"recorded head {head_sha[:8]} does not match the fetched "
            f"refs/pull/{number}/head {fetched_sha[:8]}"
        )

    merge_base = git_in_clone(
        clone, ["merge-base", base_head, fetched_sha], check=False
    ).stdout.strip()
    if not merge_base:
        return _record_conflict(f"{pr_ref} shares no history with {cfg.base_ref}")
    if merge_base == fetched_sha:
        # The preserved head is already an ancestor of the current base ref
        # (its work landed some other way since) -- nothing new to apply,
        # and not a conflict either.
        return PreservedBaselineOutcome(applied=False, pr_refs=pr_refs)

    merge = git_in_clone(clone, ["merge", "--squash", "-q", fetched_sha], check=False)
    if merge.returncode != 0:
        return _record_conflict(
            f"applying {pr_ref}'s diff over {cfg.base_ref} hit a conflict "
            f"beyond mechanical rebase: "
            f"{(merge.stderr or merge.stdout or '').strip()[:500]}"
        )

    staged = git_in_clone(clone, ["diff", "--cached", "--name-only"], check=False).stdout
    if not staged.strip():
        git_in_clone(clone, ["reset", "--hard", base_head], check=False)
        return PreservedBaselineOutcome(applied=False, pr_refs=pr_refs)

    git_in_clone(
        clone,
        [
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            _preserved_baseline_commit_message(pr_refs),
        ],
    )
    sub.add_note(
        task_id,
        "status",
        f"Preserved work from {', '.join(pr_refs)} applied as this attempt's "
        "starting point, before the worker began.",
        operator_actor(),
        provenance=provenance_for_operator_action(task_id, "apply-preserved-baseline"),
    )
    return PreservedBaselineOutcome(applied=True, pr_refs=pr_refs)


#: A base ref that is merely behind its tracking remote has a one-command
#: remedy (an operator fetches/fast-forwards it) and is not a verdict on any
#: bead — deliberately its own prefix, deliberately excluded from
#: _DISPATCH_OUTCOME_NOTE_PREFIXES in queue_order.py, so trailing_environmental_fault_streak
#: never sees it and it can never trip the bound-3 environmental breaker. See
#: record_stale_base_ref_fault and schedule_status.describe_base_ref_status,
#: which is where this condition is actually surfaced to an operator.
STALE_BASE_REF_NOTE_PREFIX = "Stale base ref:"

from queue_order import (  # noqa: E402
    ALREADY_SATISFIED_CLAIM_NOTE_PREFIX as ALREADY_SATISFIED_CLAIM_NOTE_PREFIX,
    CAPACITY_BACKPRESSURE_NOTE_PREFIX as CAPACITY_BACKPRESSURE_NOTE_PREFIX,
    ENVIRONMENTAL_FAULT_NOTE_PREFIX as ENVIRONMENTAL_FAULT_NOTE_PREFIX,
    ENVIRONMENTAL_FAULT_SIGNATURE_FIELD as ENVIRONMENTAL_FAULT_SIGNATURE_FIELD,
    RUN_FAILED_NOTE_PREFIX as RUN_FAILED_NOTE_PREFIX,
    _DISPATCH_OUTCOME_NOTE_PREFIXES as _DISPATCH_OUTCOME_NOTE_PREFIXES,
    _is_dispatch_outcome_note as _is_dispatch_outcome_note,
    _next_environmental_fault_streak as _next_environmental_fault_streak,
    trailing_environmental_fault_streak as trailing_environmental_fault_streak,
)

#: The only part of a pristine/verification failure reason that legitimately
#: varies between two runs of the identical, fully deterministic defect —
#: pytest and VerificationReport.describe() both print wall-clock duration
#: (e.g. "exit 1, 3s"). Normalizing it out before hashing is what makes two
#: occurrences of the SAME fault compare equal.
_VOLATILE_DURATION_PATTERN = re.compile(r"\d+(?:\.\d+)?s\b")


def environmental_fault_signature(reason: str) -> str:
    """Stable identity for an environmental fault, ignoring run-to-run timing noise."""
    normalized = _VOLATILE_DURATION_PATTERN.sub("Ns", redact_sensitive_output(reason))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def fail_task(
    sub: BeadStore,
    task: dict,
    reason: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    *,
    classification: retry_policy.DispatchFailureClass = retry_policy.WORK_FAILURE,
    result: "WorkerResult | None" = None,
    patch_path: str | None = None,
) -> None:
    """Record the failure, then set the state required by the retry policy.

    Exhaustion is derived solely from this bead's own recorded failure notes
    (``guards.prior_failures``) -- never from a Temporal workflow's
    retry-attempt count, which belongs to whichever bead that attempt
    happened to claim and is not necessarily this one: a retried workflow
    re-runs ``claim`` and may pick a different pending bead each time.
    """
    safe_reason = redact_sensitive_output(reason)
    attempts = len(guards.prior_failures(sub.list_notes(task["id"]))) + 1
    exhausted = retry_policy.retry_exhausted(attempts)
    # The diagnosis only matters once the bead is actually leaving the retry
    # chain: an ordinary retry-eligible failure goes straight back to
    # pending, and the next attempt already reads this same note back via
    # guards.prior_failures without needing a revision/patch diagnosis.
    if exhausted:
        diagnosis = failure_diagnosis.build_failure_diagnosis(
            task["id"], classification.name, patch_path=patch_path
        )
        safe_reason = f"{safe_reason}\n{failure_diagnosis.format_failure_diagnosis(diagnosis)}"
    sub.add_note(
        task["id"],
        "status",
        f"{RUN_FAILED_NOTE_PREFIX} {safe_reason}{_failure_class_note(classification)}",
        CREATED_BY,
        provenance=provenance_for_task(
            task["id"],
            worker_name,
            duration_s,
            tokens=result.tokens if result else None,
            cost_usd=result.cost_usd if result else None,
        ),
    )
    max_attempts = retry_policy.DISPATCH_RETRY_MAXIMUM_ATTEMPTS
    if exhausted:
        sub.set_state(task["id"], "failed", CREATED_BY)
        failure_diagnosis.announce_failed_task(task, classification.name)
        print(f"  -> failed permanently ({attempts}/{max_attempts})")
    else:
        sub.set_state(task["id"], "pending", CREATED_BY)
        print(f"  -> returned to pending for retry ({attempts}/{max_attempts})")


def record_capacity_failure(
    sub: BeadStore,
    task: dict,
    failure: CapacityFailure,
    *,
    schedule_status: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    classification: retry_policy.DispatchFailureClass = retry_policy.CAPACITY_FAILURE,
    result: "WorkerResult | None" = None,
) -> None:
    """Return a capacity-blocked task to pending without spending an attempt."""
    note = (
        f"{CAPACITY_BACKPRESSURE_NOTE_PREFIX} {failure.describe()}. "
        "Task returned to pending without incrementing attempts. "
        f"{schedule_status}{_failure_class_note(classification)}"
    )
    sub.add_note(
        task["id"],
        "status",
        note,
        CREATED_BY,
        provenance=provenance_for_task(
            task["id"],
            worker_name,
            duration_s,
            tokens=result.tokens if result else None,
            cost_usd=result.cost_usd if result else None,
        ),
    )
    sub.set_state(task["id"], "pending", CREATED_BY)
    print("  -> capacity backpressure; returned to pending without burning an attempt")
    print(f"  -> {schedule_status}")
    failure_diagnosis.announce_capacity_pause(task["id"], failure.describe(), schedule_status)


def record_environment_failure(
    sub: BeadStore,
    task: dict,
    reason: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    classification: retry_policy.DispatchFailureClass = retry_policy.ENVIRONMENT_FAILURE,
    result: "WorkerResult | None" = None,
    *,
    notes: list[dict[str, Any]] | None = None,
) -> None:
    """Return an environmentally-blocked task to pending without an attempt.

    Also tracks how many CONSECUTIVE dispatches recorded this exact fault
    (trailing_environmental_fault_streak). A transient fault must keep
    retrying unbounded, with the budget untouched — that stays true here,
    unconditionally: the state write below is always "pending", never
    "failed", and this never writes a Run failed: note, so
    guards.prior_failures (the actual retry-budget counter) never sees it. A
    PERMANENT fault recurring on every consecutive dispatch is still not a
    verdict on the work, but with no bound it starves every other pending
    task behind it forever (OPS-6, 2026-08-23) — once it repeats
    retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT times running, this
    note names the count so the stop is legible on the bead itself, and
    dispatch.pick_task's automatic scan starts skipping the bead (it stays
    `pending` the whole time). That skip is not permanent: it lifts on a
    later successful run's "Worker finished in" status note, on a recorded
    requeue (guards.requeue_barrier_at), or an operator can force an
    out-of-band attempt with `dispatch.py --task <bead-id>`, which is what
    actually tests whether the fault stopped.
    """
    safe_reason = redact_sensitive_output(reason)
    notes = sub.list_notes(task["id"]) if notes is None else notes
    signature = environmental_fault_signature(reason)
    streak = _next_environmental_fault_streak(notes, signature)
    limit = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
    bound_reached = streak >= limit
    body = (
        f"{ENVIRONMENTAL_FAULT_NOTE_PREFIX} {safe_reason} "
        "Task returned to pending without incrementing attempts."
        f"{_failure_class_note(classification)}"
    )
    if bound_reached:
        body += (
            f" This is the same fault {streak} consecutive times (bound {limit}); "
            "the dispatcher will stop automatically selecting this bead until the "
            "fault changes — this is a stop, not a verdict on the work. Force a run "
            f"with `dispatch.py --task {task['id']}` to test whether it still occurs."
        )
    sub.add_note(
        task["id"],
        "status",
        body,
        CREATED_BY,
        provenance=provenance_for_task(
            task["id"],
            worker_name,
            duration_s,
            tokens=result.tokens if result else None,
            cost_usd=result.cost_usd if result else None,
        ),
        **{ENVIRONMENTAL_FAULT_SIGNATURE_FIELD: signature},
    )
    sub.set_state(task["id"], "pending", CREATED_BY)
    print("  -> environmental fault; returned to pending without burning an attempt")
    if bound_reached:
        print(
            f"  -> same fault {streak}x consecutively (bound {limit}); "
            "pick_task will stop auto-selecting this bead"
        )
        failure_diagnosis.announce_environmental_fault_breaker_latched(
            task["id"], signature, streak, limit
        )


#: Substring unique to BaseRefStaleError's message (dispatch.py, `describe()`
#: is embedded after it). Used to re-detect a stale-base-ref failure from its
#: flattened reason text on the Temporal activity side of the boundary,
#: where the original exception type does not survive
#: (activities/dispatch_steps.py's record_failure_activity mirrors the same
#: shape already used for auth/capacity re-detection there).
STALE_BASE_REF_REASON_MARKER = "refused to clone:"


def is_stale_base_ref_reason(reason: str) -> bool:
    """Whether ``reason`` is BaseRefStaleError's message, by text alone."""
    return STALE_BASE_REF_REASON_MARKER in reason


def record_stale_base_ref_fault(
    sub: BeadStore,
    task: dict,
    reason: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    classification: retry_policy.DispatchFailureClass = retry_policy.ENVIRONMENT_FAILURE,
    result: "WorkerResult | None" = None,
) -> None:
    """Return a task blocked on a stale base ref to pending, distinctly.

    A base ref that is merely behind its tracking remote is not a fault in
    the bead's own work, and it has a one-command remedy (an operator
    fetches/fast-forwards it) — recording it under
    ENVIRONMENTAL_FAULT_NOTE_PREFIX, as an ordinary DispatchEnvironmentError
    would, means it counts toward retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT
    (bound-3) exactly like a real per-bead environmental fault, so three
    identical refusals trip the breaker and pick_task moves on to the next
    bead — which then starts its own streak toward the same bound. Recovery
    becomes "fix the ref AND force every latched bead individually" instead
    of "fix the ref."

    Using STALE_BASE_REF_NOTE_PREFIX instead keeps this invisible to
    trailing_environmental_fault_streak (it only recognizes
    _DISPATCH_OUTCOME_NOTE_PREFIXES), so this cause can never trip the
    bound and pick_task never skips the bead over it: the same oldest
    runnable bead is retried, at zero cost, every cycle the ref stays
    stale, and the very next cycle after the ref is fixed proceeds
    normally. The operator-visible signal for this condition lives at the
    schedule level (schedule_status.describe_base_ref_status), not here —
    this note is bead-local history, not the primary alert.

    ``reason`` is a plain string, not a BaseRefStatus, so this can be called
    identically from dispatch_once (which has the raised exception) and from
    activities/dispatch_steps.py's record_failure_activity (which only has
    the flattened failure text — the exception type does not survive the
    Temporal activity boundary).
    """
    safe_reason = redact_sensitive_output(reason)
    body = (
        f"{STALE_BASE_REF_NOTE_PREFIX} {safe_reason} Task returned to "
        "pending without incrementing attempts. This is not counted toward "
        "the environmental-fault bound (bound-3): it recurs on every bead "
        "until an operator fetches/fast-forwards the base ref, which is a "
        "one-command remedy, not per-bead work."
        f"{_failure_class_note(classification)}"
    )
    sub.add_note(
        task["id"],
        "status",
        body,
        CREATED_BY,
        provenance=provenance_for_task(
            task["id"],
            worker_name,
            duration_s,
            tokens=result.tokens if result else None,
            cost_usd=result.cost_usd if result else None,
        ),
    )
    sub.set_state(task["id"], "pending", CREATED_BY)
    print(
        f"  -> STALE BASE REF: {safe_reason}; returned to pending without "
        "burning an attempt or counting toward the environmental-fault bound"
    )


def record_already_satisfied_work(
    sub: BeadStore,
    task: dict,
    reason: str,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
    classification: retry_policy.DispatchFailureClass = retry_policy.ALREADY_SATISFIED_FAILURE,
    result: "WorkerResult | None" = None,
) -> None:
    """Record a worker's already-satisfied claim and end this run's retry chain.

    The dispatcher does not adjudicate the claim — only stops spending
    autonomous retries on rediscovering it, and moves the bead to ``failed``
    so an operator can dispose of it with ``--bind-pr``, ``--requeue``, or by
    rejecting the claim and requeuing anyway (dev.task 07a99270).
    """
    safe_reason = redact_sensitive_output(reason)
    diagnosis = failure_diagnosis.build_failure_diagnosis(task["id"], classification.name)
    sub.add_note(
        task["id"],
        "status",
        f"{ALREADY_SATISFIED_CLAIM_NOTE_PREFIX} {safe_reason} Worker made no changes and "
        "reported the task's acceptance criteria already met. Ending this "
        "run's retry chain without spending further attempts — use "
        "--bind-pr to close it against the satisfying PR, --requeue to "
        "reject the claim and try again, or leave it failed."
        f"{_failure_class_note(classification)}\n"
        f"{failure_diagnosis.format_failure_diagnosis(diagnosis)}",
        CREATED_BY,
        provenance=provenance_for_task(
            task["id"],
            worker_name,
            duration_s,
            tokens=result.tokens if result else None,
            cost_usd=result.cost_usd if result else None,
        ),
    )
    sub.set_state(task["id"], "failed", CREATED_BY)
    failure_diagnosis.announce_failed_task(task, classification.name)
    print("  -> already-satisfied claim; ended retry chain for operator disposition")


def record_pristine_already_satisfied(
    sub: BeadStore,
    task: dict,
    reason: str,
    *,
    worker_name: str = DEFAULT_WORKER,
    duration_s: float = 0.0,
) -> None:
    """Record a red-first bead whose pristine check already passed. No worker ran.

    Deliberately its own function rather than a call to
    :func:`record_already_satisfied_work`: that one's note claims a worker
    ran and reported the task already met — untrue here, since this refusal
    fires before ``run_worker`` is ever invoked. The retry-chain-ending
    behaviour is the same (ALREADY_SATISFIED_FAILURE does not consume a
    retry), so an operator disposes of it the same way — --bind-pr,
    --requeue, or leave it failed.
    """
    safe_reason = redact_sensitive_output(reason)
    diagnosis = failure_diagnosis.build_failure_diagnosis(
        task["id"], retry_policy.ALREADY_SATISFIED_FAILURE.name
    )
    sub.add_note(
        task["id"],
        "status",
        f"{ALREADY_SATISFIED_CLAIM_NOTE_PREFIX} {safe_reason} No worker was dispatched — "
        "the declared verification.expect_pristine_failure check already passed against "
        "an unmodified clone of main, before any work ran. Ending this run's retry chain "
        "without spending an attempt — use --bind-pr to close it against a PR that "
        "already did the work, --requeue to reject the claim and try again, or leave it "
        "failed."
        f"{_failure_class_note(retry_policy.ALREADY_SATISFIED_FAILURE)}\n"
        f"{failure_diagnosis.format_failure_diagnosis(diagnosis)}",
        CREATED_BY,
        provenance=provenance_for_task(task["id"], worker_name, duration_s),
    )
    sub.set_state(task["id"], "failed", CREATED_BY)
    failure_diagnosis.announce_failed_task(
        task, retry_policy.ALREADY_SATISFIED_FAILURE.name
    )
    print("  -> pristine verification already satisfied; ended retry chain for operator disposition")


# Imported here, at module level but this late in the file, rather than at
# the top: activities.dispatch_steps imports this module (dispatch) at ITS
# own top level, and by this point in dispatch.py's own execution everything
# dispatch_steps needs (WorkerResult, Config, CREATED_BY, PRISTINE_ALREADY_
# SATISFIED_MARKER, record_pristine_already_satisfied, ...) is already
# defined — a top-of-file import here would deadlock the cycle instead.
# Binding this once, at dispatch.py's own import time, also matters beyond
# breaking the cycle: a per-call ``from activities import dispatch_steps``
# inside dispatch_once re-resolves via sys.modules on every dispatch, and at
# least one test helper elsewhere (test_dispatch_activity_liveness._fresh_
# import, reused by test_design_note_provenance.py) leaves sys.modules
# pointing at a different activities.dispatch_steps object once it has run —
# a per-call import would then silently stop seeing a test's
# monkeypatch.setattr(dispatch_steps, ...) at all.
from activities import dispatch_steps  # noqa: E402 — deliberate: see cycle note above


def _report_dispatch_once(result: dict[str, Any]) -> int:
    """Translate one shared-pipeline dispatch pass into console output.

    Every note an operator can act on, and the bead's own state transition,
    already happened inside the step activities themselves — claim_
    activity's refusal messages, and dispatch.record_*/fail_task's notes,
    written from record_failure_activity — the same functions dispatch_once
    called directly before this converged onto the one implementation both
    entry points now share (see .factory/design.md). This only supplies the
    exit code and one console line naming what happened; it writes nothing
    to the bead itself.
    """
    status = result.get("status")
    message = str(result.get("message") or "")
    exit_code = int(result.get("exit_code", 1))
    sweep_note = result.get("sweep_note")
    if sweep_note:
        print(f"  orphan workdir sweep: {sweep_note}")
    if status == "dry_run":
        print("\n--- prompt ---\n")
        print(message)
        print("--- end prompt (dry run: nothing claimed, nothing written) ---")
        return exit_code
    if status == "review":
        print(f"  PR: {result.get('pr_url')}")
        print("  -> review")
        return exit_code
    if status in (
        "no_task",
        "not_runnable",
        "budget_floor",
        "disk_floor",
        "claim_failed",
        "dispatch_in_flight",
    ):
        print(message)
        return exit_code
    # Every other status (capacity_backpressure, environmental_fault,
    # stale_base_ref, already_satisfied, or the generic work "failed") was
    # already recorded on the bead by record_failure_activity, via the same
    # dispatch.record_*/fail_task functions dispatch_once used to call
    # directly — this is one console label naming which fired, not a second
    # record of it.
    print(f"  {(status or 'FAILED').upper()}: {message}")
    return exit_code


def dispatch_once(cfg: Config, sub: BeadStore, task_id: str | None, dry_run: bool) -> int:
    """Claim and run one task, through the shared dispatch step sequence.

    Drives ``workflow_core.run_dispatch_attempt`` over the exact
    ``activities.dispatch_steps`` functions the scheduled Temporal drain
    calls (see ``workflows.dispatch_task.DispatchTaskWorkflow``) — one
    implementation of claim/isolate/.../propose, not a hand-maintained copy
    of it; see .factory/design.md for what that replaced and why.
    Reconciliation is a separate concern on both entry points
    (``--reconcile-review`` here, the ``reconcile`` activity there) and is
    deliberately not part of this pass — see
    ``workflow_core.run_dispatch_sequence``'s docstring.
    """

    async def execute(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Off the event loop, in a worker thread — the same execution shape
        # a real (synchronous) Temporal activity runs under. record_failure_
        # activity's capacity path calls asyncio.run() of its own, to pause
        # the Temporal schedule; that raises if invoked from a thread that
        # already has this coroutine's own event loop running.
        return await asyncio.to_thread(dispatch_steps.ACTIVITY_FUNCTIONS[name], payload)

    request: dict[str, Any] = {
        "cfg": dispatch_steps._cfg_to_state(cfg),
        "task_id": task_id,
        "dry_run": dry_run,
    }
    with use_store(sub):
        result = asyncio.run(workflow_core.run_dispatch_attempt(request, execute))
    return _report_dispatch_once(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one factory task to a PR.")
    parser.add_argument("--once", action="store_true", help="claim and run one task")
    parser.add_argument("--task", help="run this specific dev.task bead id")
    parser.add_argument(
        "--report-stuck",
        action="store_true",
        help="report doing tasks older than the stuck threshold; read-only",
    )
    parser.add_argument(
        "--traceability-report",
        action="store_true",
        help=(
            "report open dev.task requirement-traceability counts (refs, "
            "waiver-only, neither), dated and machine-readable; read-only, "
            "exits nonzero when any open task carries neither"
        ),
    )
    parser.add_argument(
        "--reconcile-review",
        action="store_true",
        help=(
            "move review tasks to done when their PR is merged and its merge "
            "commit is on main"
        ),
    )
    parser.add_argument(
        "--report-held-prs",
        action="store_true",
        help=(
            "report pending/failed beads whose newest note holds on a PR that "
            "has since closed without merging (a stranded hold) or merged (a "
            "discharge candidate); read-only"
        ),
    )
    parser.add_argument(
        "--bind-pr",
        help=(
            "bind --task's bead to a PR opened outside the dispatcher and move it "
            "to review, so the reconciler can settle it"
        ),
    )
    parser.add_argument(
        "--bind-release",
        metavar="RELEASE_REF",
        help=(
            "bind --task's bead to a release (R26.01 or R26.01/O-2), the same "
            "edge-and-content split filing uses, so work already in flight can "
            "be bound without refiling"
        ),
    )
    parser.add_argument(
        "--start",
        metavar="REASON",
        help=(
            "claim --task's bead for attended work: pending -> doing, recording "
            "REASON and who claimed it; a reason is required"
        ),
    )
    parser.add_argument(
        "--requeue",
        metavar="REASON",
        help=(
            "return --task's bead to pending and restart its retry budget, "
            "recording REASON on the bead; a reason is required"
        ),
    )
    parser.add_argument(
        "--keep-preserved",
        action="store_true",
        help=(
            "for --requeue on a bead carrying preserved_attempts: keep that "
            "recorded work through the requeue. Required (with "
            "--discard-preserved as the alternative) whenever the bead has "
            "any preserved_attempts recorded"
        ),
    )
    parser.add_argument(
        "--discard-preserved",
        action="store_true",
        help=(
            "for --requeue on a bead carrying preserved_attempts: strip that "
            "recorded work as part of the requeue. Required (with "
            "--keep-preserved as the alternative) whenever the bead has any "
            "preserved_attempts recorded"
        ),
    )
    parser.add_argument(
        "--release-stranded",
        metavar="REASON",
        help=(
            "release --task's stranded doing bead after Temporal shows no "
            "running dispatcher execution owns it; a reason is required"
        ),
    )
    parser.add_argument(
        "--backfill-superseded",
        action="store_true",
        help=(
            "transition --task's pending or failed bead to superseded, citing "
            "the supersession note it already carries; refuses and names what "
            "is missing if no such note is found"
        ),
    )
    parser.add_argument(
        "--stuck-threshold-minutes",
        type=int,
        default=DEFAULT_STUCK_THRESHOLD_MINUTES,
        help=(
            "minutes a doing task may sit without an updated_at change before it is "
            f"reported as stuck (default: {DEFAULT_STUCK_THRESHOLD_MINUTES})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print intended dispatch or reconciliation actions; write nothing",
    )
    parser.add_argument("--backfill-edges", action="store_true", help="walk every arch.change idempotently, backfilling delivers/affects edges")
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "for --requeue: skip the interactive confirmation pause (for "
            "scripted sweeps); the bead's recorded failure note(s) still "
            "print before the transition regardless"
        ),
    )
    args = parser.parse_args(argv)

    if args.stuck_threshold_minutes <= 0:
        parser.error("--stuck-threshold-minutes must be positive")
    if args.report_stuck and (args.once or args.task or args.dry_run or args.reconcile_review):
        parser.error(
            "--report-stuck cannot be combined with --once, --task, --dry-run, "
            "or --reconcile-review"
        )
    if args.traceability_report and (
        args.once
        or args.task
        or args.dry_run
        or args.reconcile_review
        or args.report_stuck
        or args.report_held_prs
    ):
        parser.error(
            "--traceability-report cannot be combined with --once, --task, "
            "--dry-run, --reconcile-review, --report-stuck, or --report-held-prs"
        )
    if args.report_held_prs and (
        args.once
        or args.task
        or args.dry_run
        or args.reconcile_review
        or args.report_stuck
        or args.traceability_report
    ):
        parser.error(
            "--report-held-prs cannot be combined with --once, --task, "
            "--dry-run, --reconcile-review, --report-stuck, or "
            "--traceability-report"
        )
    if args.reconcile_review and (args.once or args.task):
        parser.error("--reconcile-review cannot be combined with --once or --task")
    if args.bind_pr and not args.task:
        parser.error("--bind-pr requires --task <bead-id>")
    if args.bind_release and not args.task:
        parser.error("--bind-release requires --task <bead-id>")
    if args.start is not None and not args.task:
        parser.error("--start requires --task <bead-id>")
    if args.start is not None and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_pr or args.bind_release
        or args.requeue is not None
        or args.release_stranded is not None
    ):
        parser.error("--start cannot be combined with the other actions")
    if args.requeue is not None and not args.task:
        parser.error("--requeue requires --task <bead-id>")
    if args.keep_preserved and args.discard_preserved:
        parser.error("--keep-preserved and --discard-preserved are mutually exclusive")
    if (args.keep_preserved or args.discard_preserved) and args.requeue is None:
        parser.error("--keep-preserved and --discard-preserved require --requeue")
    if args.requeue is not None and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_pr or args.bind_release
        or args.release_stranded is not None
    ):
        parser.error(
            "--requeue cannot be combined with --once, --report-stuck, "
            "--traceability-report, --report-held-prs, --reconcile-review, "
            "--bind-pr, --bind-release, or --release-stranded"
        )
    if args.release_stranded is not None and not args.task:
        parser.error("--release-stranded requires --task <bead-id>")
    if args.release_stranded is not None and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_pr or args.bind_release
    ):
        parser.error(
            "--release-stranded cannot be combined with --once, --report-stuck, "
            "--traceability-report, --report-held-prs, --reconcile-review, "
            "--bind-pr, or --bind-release"
        )
    if args.bind_pr and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_release
    ):
        parser.error(
            "--bind-pr cannot be combined with --once, --report-stuck, "
            "--traceability-report, --report-held-prs, --reconcile-review, "
            "or --bind-release"
        )
    if args.bind_release and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_pr
    ):
        parser.error(
            "--bind-release cannot be combined with --once, --report-stuck, "
            "--traceability-report, --report-held-prs, --reconcile-review, "
            "or --bind-pr"
        )
    if args.backfill_superseded and not args.task:
        parser.error("--backfill-superseded requires --task <bead-id>")
    if args.backfill_superseded and (
        args.once or args.report_stuck or args.traceability_report
        or args.report_held_prs
        or args.reconcile_review or args.bind_pr or args.bind_release
        or args.start is not None
        or args.requeue is not None
        or args.release_stranded is not None
    ):
        parser.error(
            "--backfill-superseded cannot be combined with the other actions"
        )
    if args.backfill_edges:
        from activities import change_apply
        return print(change_apply.backfill_change_edges(change_apply.default_store(), dry_run=args.dry_run)) or 0
    if (
        not args.report_stuck
        and not args.traceability_report
        and not args.report_held_prs
        and not args.reconcile_review
        and not args.once
        and not args.task
    ):
        parser.error(
            "pass --once, --task <bead-id>, --report-stuck, "
            "--traceability-report, --report-held-prs, or --reconcile-review"
        )

    # Python block-buffers stdout when it is a pipe, so `dispatch.py | tee log`
    # showed nothing at all until the process exited — the progress prints are
    # worthless for the exact long run they exist to narrate.
    sys.stdout.reconfigure(line_buffering=True)

    cfg = Config.from_env()
    sub = default_store()
    if args.report_stuck:
        return report_stuck_doing_tasks(sub, args.stuck_threshold_minutes)
    if args.traceability_report:
        return report_traceability(sub)
    if args.report_held_prs:
        return report_held_prs(sub, cfg)
    if args.reconcile_review:
        return reconcile_review_tasks(cfg, sub, dry_run=args.dry_run)
    if args.bind_pr:
        return bind_task_to_pull_request(
            cfg, sub, args.task, args.bind_pr, dry_run=args.dry_run
        )
    if args.bind_release:
        return bind_task_to_release(
            sub, args.task, args.bind_release, dry_run=args.dry_run
        )
    if args.start is not None:
        return start_attended_task(sub, args.task, args.start, dry_run=args.dry_run)
    if args.requeue is not None:
        preserved_choice = (
            "keep" if args.keep_preserved else "discard" if args.discard_preserved else None
        )
        return requeue_task(
            sub,
            args.task,
            args.requeue,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            preserved_choice=preserved_choice,
        )
    if args.release_stranded is not None:
        return release_stranded_task(
            sub,
            args.task,
            args.release_stranded,
            dry_run=args.dry_run,
        )
    if args.backfill_superseded:
        return backfill_superseded_task(sub, args.task, dry_run=args.dry_run)
    return dispatch_once(cfg, sub, args.task, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
