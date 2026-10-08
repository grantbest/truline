"""Dispatcher-owned OS-level write containment (Amendment 30, PR-6).

The dispatcher wraps every worker invocation in a `sandbox-exec` deny-write
profile scoped to the isolated clone. The profile text descends from the PR-2
spike's validated artifact
(docs/archive/2026-08-agent-harness-spike/claude-wrapper/deny-write.sb.template, validated
2026-08-16 on macOS 15): a write outside the allowed subpaths fails with EPERM
at the OS layer regardless of what the worker believes or reports. The post-run
diff guard remains as detection behind this enforcement.

That validation host is gone. This dispatcher now runs on macOS 26.5.2 (build
25F84; system files dated 2026-06-24) - a different major release from the one
the artifact was proven on, and no fresh PR-2-equivalent validation pass has
been run on it. The 2026-08-22 codex-retirement investigation (dev.task
b4338922) did exercise prepare_containment()/contained_argv() live on this
host and confirmed the deny-write wrapping and nested-sandbox_apply failure
mode both still behave as this module assumes, but that was a targeted repro
of one failure, not a substitute for revalidating the profile itself on the
current host.

Per-worker allowances exist for exactly this kind of reason. Claude's own CLI
harness - not the task work it performs - lazily creates a per-invocation
scratchpad under /tmp/claude-<uid>/<slug-of-cwd> for its own session
bookkeeping; denying that write does not stop the worker from writing, it
stops the harness from starting at all, so a healthy worker can only stop and
change nothing (dev.task 070afe98). The subdirectory name is an internal detail of the
harness, not something this dispatcher can predict per run, so the allowance
is the UID-scoped root (dispatch.CLAUDE_HARNESS_SCRATCHPAD_ROOT) rather than a
narrower per-run path. Allowances are declared on the WorkerEntry, reviewed
like any registry change, and rendered into the profile per run — never
accumulated silently.

Persona materialization (dev.finding bd4b2a9a) also lives here: the dispatcher
copies persona definitions from the clone's docs/agents/ into
<clone>/.claude/agents/ in-process, never by running the clone's own
scripts/materialize_agents.py — that would run a worker-controlled preserved
diff's planted script as the dispatcher's own user. A persona source is read
only when it is a regular file inside the clone and not a symlink
(persona_source); the destination is written without ever following a link,
at the leaf file or at .claude/.claude/agents themselves
(materialize_personas).

Declared VERIFICATION gets the same OS-level treatment as the worker (dev.
finding 6c19f60f). Before this, every declared command, the base-revision
re-run, and the bootstrap's `pip install` ran as a plain, unwrapped
subprocess -- worker-authored content (a planted conftest.py, a hostile PEP
517 build backend) executed as the host user with no write boundary at all,
and the interpreter it ran under (<clone>/venv, gitignored) was itself
worker-replaceable between preflight and the post-worker run. The fix has two
parts. First, `render_verification_profile`/`prepare_verification_containment`
give verification its own write-deny profile, same placement rule as the
worker's (profile and a private `.verify-tmp` NEXT TO the directory the
command runs in, never inside it, so neither appears in `changed_paths`);
writes are allowed to exactly that directory, `.verify-tmp`, `/dev`, and
(only for the bootstrap's pip install) the outside verification venv --
narrower than the worker's own boundary, which never appears in this list.
Second, the venv `bootstrap_verification_env` builds moves to
`<clone's parent>/.verification-venv` (`verification_venv_path`), outside the
clone entirely, so every route into the interpreter -- creating it, using it,
reinstalling into it -- passes through a tracked, diff-visible file instead
of a gitignored one a worker can silently replace. The profile additionally
denies READING the dispatcher's state directory
(`Path.home()/".factory-dispatcher"`, and `FACTORY_DISPATCHER_STATE_DIR` when
set) -- verification never legitimately needs it, and without the deny it
sits one read away from every declared command. This is not host-revalidated
beyond the tests in this repo, for the same reason the caveat two paragraphs
up applies to the worker's own profile.

The worker profile gets the analogous read deny (dev.finding c48a3827): the
state directory holds the write key, SUBSTRATE_API_KEY, so the worker
profile denies reading it -- and $FACTORY_DISPATCHER_STATE_DIR when that is
set -- the same way the verification profile above does, sharing this
module's `state_dir_read_denies`/`STATE_DIR_ENV` rather than a second copy.
`worker_environment` is the companion fix for the environment route: the
worker process is handed the read key under the name SUBSTRATE_API_KEY (the
name the substrate clients in this repo read), never the write key, under
any name. One consequence of the directory deny: a retried worker cannot
read its own previous attempt's preserved diff under that directory
(`failed/` lives under `~/.factory-dispatcher` by default) -- that is
expected, since `preserved_attempts` is the designed recovery path, not
something a live worker is meant to read back.

Both the worker profile and the declared-verification profile additionally
deny READING the host's named credential directories (CREDENTIAL_DIRS), the
files the dispatcher's own KUBECONFIG names (and the targets of any symlink
inside those directories), and deny outbound TCP to port 22 and to every
loopback port except the substrate tunnel's (dev.findings 019909c0, 75b50b58,
dd648709). The reasons: the dispatcher's kubeconfig is cluster-admin; ssh to
the cluster node gives passwordless root; Temporal, reached through the
tunnel on localhost, accepts unauthenticated clients; and other same-user
loopback services (an IDE's MCP server among them) are reachable from this
uid. The worker and declared verification therefore receive no KUBECONFIG or
SSH_AUTH_SOCK (`worker_environment`) and no KUBECONFIG in declared
verification's environment (dispatch.VERIFICATION_ENV_ALLOWLIST); every
cluster read, `git push`, and `gh` call runs in the dispatcher's own process,
never inside either sandbox. This is a deny-list, not an allow-list: the
macOS login keychain and a credential stored anywhere else are not covered,
and the structural fix -- running the factory as a separate macOS user -- is
tracked separately, not by this mechanism.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

WRAPPER_EXECUTABLE = "sandbox-exec"

# The two substrate key env var names, read literally rather than imported --
# containment.py must not import worker_revision (worker_revision imports
# dispatch, which imports containment: a cycle) or substrate.py (out of this
# bead's scope). SUBSTRATE_API_KEY is the name substrate.py's clients read.
SUBSTRATE_WRITE_KEY_ENV = "SUBSTRATE_API_KEY"
SUBSTRATE_READ_KEY_ENV = "SUBSTRATE_READ_API_KEY"

_SUBSTRING_CARRIER_FLOOR = 16

#: Names the dev.task worker must never receive (dev.findings 019909c0,
#: 75b50b58, AC-1): KUBECONFIG, because the dispatcher's own kubeconfig is
#: cluster-admin; SSH_AUTH_SOCK, because the worker never pushes and has no
#: use for the ssh agent. worker_environment drops both AFTER the scratch
#: variable and extra_env are merged in, so neither can restore them.
WORKER_DENIED_ENV_NAMES: tuple[str, ...] = ("KUBECONFIG", "SSH_AUTH_SOCK")

#: Path.home()-relative directories the worker and declared-verification
#: profiles must never read (dev.findings 019909c0, 75b50b58): the
#: dispatcher's kubeconfig is cluster-admin, an ssh key under ~/.ssh reaches
#: a cluster node with passwordless root, and ~/.config/gh, ~/.config/gcloud
#: and ~/.config/sops hold other same-uid credentials. One module-level
#: tuple, resolved at render time (see _resolved_credential_dirs), so a
#: later bead widens it in one place.
CREDENTIAL_DIRS: tuple[str, ...] = (
    ".kube",
    ".ssh",
    ".config/gh",
    ".config/gcloud",
    ".config/sops",
)

#: Mach services the worker and declared-verification profiles must never
#: look up (dev.finding dd648709's loopback survey; the part-B design's
#: §9.2 amendment, PR 1090). Measured live on this host with a ctypes
#: bootstrap_look_up probe against the process's own bootstrap port
#: (0 = reachable, 1100 = sandbox-denied, 1102 = not in this namespace):
#: com.apple.pasteboard.1 (pasteboard) -> 0, com.apple.coreservices.appleevents
#: (AppleEvents) -> 0, com.apple.coreservices.launchservicesd (LaunchServices)
#: -> 0. The sibling candidate names com.apple.pboard,
#: com.apple.CoreServices.appleevents (capital C), com.apple.launchservices.lsd
#: and com.apple.distnoted.ipc all answered 1102 on this host and are not
#: included here -- denying a name nothing on this host resolves is not a
#: measured deny.
MACH_DENY: tuple[str, ...] = (
    "com.apple.pasteboard.1",
    "com.apple.coreservices.appleevents",
    "com.apple.coreservices.launchservicesd",
)

PROFILE_TEMPLATE = """\
;; Rendered by apps/factory-dispatcher/containment.py — Amendment 30 PR-6.
;; Origin: PR-2 spike artifact, validated 2026-08-16 on macOS 15 — that host
;; is gone; this dispatcher now runs on macOS 26.5.2 (2026-08-22), unvalidated
;; against a fresh PR-2-equivalent pass on the current host.
(version 1)
(allow default)
{READ_DENIES}
{NETWORK_RULES}
{MACH_JOB_DENIES}
(deny file-write*)
(allow file-write*
  (subpath "@WORKSPACE@")
  (subpath "/dev")
  (literal "/dev/null"){EXTRA_ALLOWS})
{GIT_WRITE_DENIES}
"""

PROFILE_NAME = ".containment.sb"
TMPDIR_NAME = ".tmp"


def _carries_write_key(value: str | None, write_key: str) -> bool:
    """Whether ``value`` embeds ``write_key`` -- an exact match after
    stripping, or a substring match when the key is long enough that a short
    fixture value could not match unrelated strings by accident."""
    if not write_key:
        return False
    raw = value or ""
    if raw.strip() == write_key:
        return True
    return len(write_key) >= _SUBSTRING_CARRIER_FLOOR and write_key in raw


def _read_key_refusal_reason(read_key: str, carries_write: bool) -> str:
    state = "equal to (or carrying) SUBSTRATE_API_KEY" if carries_write else "absent or blank"
    return (
        f"SUBSTRATE_READ_API_KEY is {state}; set SUBSTRATE_READ_API_KEY "
        "(distinct from SUBSTRATE_API_KEY) in the worker's launchd env file "
        "and restart the worker"
    )


def worker_environment(
    base_env: dict[str, str], additions: tuple[tuple[str, str], ...] = ()
) -> tuple[dict[str, str], str | None]:
    """The dev.task worker's process environment, write key removed.

    ``base_env`` is the dispatcher's own environment (os.environ); additions
    are applied on top of it in order (today: the scratch variable, then the
    WorkerEntry's extra_env) exactly as run_worker builds env today. The
    write/read key check runs AFTER that merge so no extra_env entry can
    smuggle the write key back in under the protected names or an alias, but
    W and R themselves are read from ``base_env`` -- the dispatcher's own
    ambient values, not anything a WorkerEntry could redefine.

    Every name equal to SUBSTRATE_API_KEY or SUBSTRATE_READ_API_KEY is
    dropped outright, and every other entry whose value carries the write
    key (see _carries_write_key) is dropped too -- an aliased copy of the
    write key under a third name is the same exposure. If the read key is
    set, non-blank, and does not itself carry the write key, it is then
    installed under the name SUBSTRATE_API_KEY (what substrate.py's clients
    read) so a worker that talks to the store does so read-only. Otherwise
    no substrate key is set at all, and a stable, value-free refusal reason
    is returned instead.

    Every name in WORKER_DENIED_ENV_NAMES (KUBECONFIG, SSH_AUTH_SOCK) is
    also dropped outright, regardless of value (dev.findings 019909c0,
    75b50b58, AC-1) -- the worker never pushes and never reads the cluster,
    so it needs neither the ssh agent nor a kubeconfig, and dropping both
    only after ``additions`` is merged means neither a scratch var nor a
    WorkerEntry's extra_env can restore them.
    """
    merged = dict(base_env)
    for name, value in additions:
        merged[name] = value

    write_key = (base_env.get(SUBSTRATE_WRITE_KEY_ENV) or "").strip()
    read_key = (base_env.get(SUBSTRATE_READ_KEY_ENV) or "").strip()

    env = {
        name: value
        for name, value in merged.items()
        if name not in (SUBSTRATE_WRITE_KEY_ENV, SUBSTRATE_READ_KEY_ENV)
        and name not in WORKER_DENIED_ENV_NAMES
        and not _carries_write_key(value, write_key)
    }

    read_carries_write = _carries_write_key(read_key, write_key)
    if read_key and not read_carries_write:
        env[SUBSTRATE_WRITE_KEY_ENV] = read_key
        return env, None
    return env, _read_key_refusal_reason(read_key, read_carries_write)


def render_profile(workspace: Path, extra_allow: tuple[str, ...] = ()) -> str:
    """The profile text for one run, extra allowances expanded (~ included).

    Every path is fully resolved before rendering: sandbox-exec matches
    subpaths against the OS-resolved path, and on macOS /var and /tmp are
    symlinks into /private - an unresolved allow denies every write inside
    the workspace it names. Proven live by the first claude canary
    (2026-08-18): the worker could not write its own clone.

    Also denies READING every directory :func:`state_dir_read_denies` and
    CREDENTIAL_DIRS name, the files the dispatcher's own KUBECONFIG names,
    and any symlinked escape out of CREDENTIAL_DIRS -- see
    :func:`_read_deny_lines`. The dispatcher's state directory holds the
    write key (dev.finding c48a3827); the worker is handed the read key
    instead (see :func:`worker_environment`), and this directory deny
    closes the second route to the same secret. CREDENTIAL_DIRS closes the
    kubeconfig/ssh/gh/gcloud/sops routes (dev.findings 019909c0, 75b50b58).
    Also denies outbound ssh and every loopback port but the substrate
    tunnel's (dev.findings 75b50b58, dd648709), and mach-lookup of
    MACH_DENY's services plus all job-creation (dd648709's loopback survey).

    Also denies WRITING the workspace's ``.git`` entry, ``.git/hooks``,
    ``.git/config``, ``.git/info`` and ``.git/commondir`` (dev.finding
    79db3113 part a) -- see :func:`_git_write_deny_lines`. These come AFTER
    the write-allow block above so the deny wins (seatbelt: the later rule
    takes precedence).
    """
    resolved_ws = workspace.resolve()
    extras = "".join(
        f'\n  (subpath "{Path(os.path.expanduser(p)).resolve()}")'
        for p in extra_allow
    )
    return (
        PROFILE_TEMPLATE.replace("{EXTRA_ALLOWS}", extras)
        .replace("@WORKSPACE@", str(resolved_ws))
        .replace("{READ_DENIES}", _read_deny_lines())
        .replace("{NETWORK_RULES}", _network_rule_lines())
        .replace("{MACH_JOB_DENIES}", _mach_job_deny_lines())
        .replace("{GIT_WRITE_DENIES}", _git_write_deny_lines(resolved_ws))
    )


def prepare_containment(
    workspace: Path, extra_allow: tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """Write the profile and private tmp dir NEXT TO the workspace.

    Both live in the workspace's parent (the per-run mkdtemp workdir), never
    inside the clone: a dispatcher-owned file inside the clone reads as an
    out-of-scope worker write to the post-run diff guard, which is exactly
    what failed the first canary. The tmpdir is separately allowed in the
    profile. Everything still dies with the run. Returns (profile_path,
    tmpdir).

    Raises ValueError, naming both paths, when the workspace resolves to (or
    under) any directory :func:`_all_denied_subpaths` names (the dispatcher
    state directory or a CREDENTIAL_DIRS entry) -- a clone the worker
    cannot read would fail every dispatch as a work failure, not a
    containment success.
    """
    resolved_ws = workspace.resolve()
    collision = _under_any(resolved_ws, _all_denied_subpaths())
    if collision is not None:
        raise ValueError(
            f"workspace {resolved_ws} resolves under {collision}, "
            "which the worker profile denies reading"
        )
    holder = resolved_ws.parent
    tmpdir = holder / TMPDIR_NAME
    tmpdir.mkdir(exist_ok=True)
    profile = holder / PROFILE_NAME
    text = render_profile(workspace, extra_allow).replace(
        '  (subpath "/dev")',
        f'  (subpath "{tmpdir}")\n  (subpath "/dev")',
    )
    profile.write_text(text)
    return profile, tmpdir


def contained_argv(
    worker_argv: tuple[str, ...], profile: Path
) -> tuple[str, ...]:
    """The worker argv wrapped in the OS write boundary."""
    return (WRAPPER_EXECUTABLE, "-f", str(profile), *worker_argv)


# --- In-process persona materialization (dev.finding bd4b2a9a) ---
#
# The dispatcher used to run <clone>/scripts/materialize_agents.py as a
# subprocess so "the tested tree defines the personas". A preserved baseline
# is a prior worker's ungated diff, so that ran whatever a failed attempt had
# planted there, as the dispatcher's own user. The tested tree only needs to
# supply persona DATA, not the copying CODE, so the copy happens here,
# in-process, reading only docs/agents/<name> and refusing anything shaped
# like an escape: a symlinked source or destination, or a destination
# directory (.claude, .claude/agents) that is itself a symlink or resolves
# outside the clone. Reason strings name only a clone-relative path plus a
# fixed class, never an absolute path or an OS errno string that carries
# one — the clone is a fresh tmp dir every run, so anything else would make
# the same fault produce a new signature each time and the per-bead
# environmental-fault breaker would never latch.


def _resolve_inside(clone_root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(clone_root)
    except (OSError, ValueError):
        return False
    return True


def _persona_source_reason(clone: Path, clone_root: Path, name: str) -> str | None:
    path = clone / "docs" / "agents" / name
    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "symlink refused"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if not _resolve_inside(clone_root, path):
        return "outside the clone"
    return None


def persona_source_reason(clone: Path, name: str) -> str | None:
    """Why ``persona_source`` would refuse ``name``, or ``None`` if it would not."""
    return _persona_source_reason(clone, clone.resolve(), name)


def persona_source(clone: Path, name: str) -> Path | None:
    """<clone>/docs/agents/<name>, only when it is a real file inside the clone."""
    if persona_source_reason(clone, name) is not None:
        return None
    return clone / "docs" / "agents" / name


def _ensure_dir_no_symlink(path: Path, clone_root: Path, rel: str) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path)
        except OSError:
            return f"{rel}: could not be written"
        return None
    except OSError:
        return f"{rel}: could not be written"
    if stat.S_ISLNK(st.st_mode):
        return f"{rel}: symlink refused"
    if not stat.S_ISDIR(st.st_mode):
        return f"{rel}: not a directory"
    if not _resolve_inside(clone_root, path):
        return f"{rel}: outside the clone"
    return None


def _write_persona_file(source: Path, dest: Path, rel: str) -> str | None:
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        pass
    except OSError:
        return f"{rel}: could not be written"
    else:
        if stat.S_ISDIR(st.st_mode):
            return f"{rel}: not a regular file"
        try:
            os.unlink(dest)
        except OSError:
            return f"{rel}: could not be written"
    try:
        data = source.read_bytes()
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        return f"{rel}: could not be written"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError:
        return f"{rel}: could not be written"
    return None


def materialize_personas(clone: Path, names: tuple[str, ...]) -> str | None:
    """Copy each ``docs/agents/<name>`` into ``<clone>/.claude/agents/<name>``.

    Runs no subprocess and reads no script — see the section note above.
    Returns ``None`` on success or a stable, path-free-of-the-clone reason
    string naming the refused clone-relative path and its refusal class.
    """
    clone_root = clone.resolve()
    sources: dict[str, Path] = {}
    for name in names:
        reason = _persona_source_reason(clone, clone_root, name)
        if reason is not None:
            return f"docs/agents/{name}: {reason}"
        sources[name] = clone / "docs" / "agents" / name

    reason = _ensure_dir_no_symlink(clone / ".claude", clone_root, ".claude")
    if reason is not None:
        return reason
    agents_dir = clone / ".claude" / "agents"
    reason = _ensure_dir_no_symlink(agents_dir, clone_root, ".claude/agents")
    if reason is not None:
        return reason

    for name, source in sources.items():
        reason = _write_persona_file(
            source, agents_dir / name, f".claude/agents/{name}"
        )
        if reason is not None:
            return reason
    return None


# --- Release-gate read-only containment (OPS-61, Amendment 37 clause 1) ---
#
# Carried forward from Amendment 36: "the gate runs read-only; containment or
# tool denial - never a flag or a promise in prose - is the boundary." A36's
# own 2026-08-29 live run is the standing counter-example: told it was
# read-only, it checked out branches in the operator's working tree, started
# a port-forward to production, and reached for a production API key. The
# profile below is the mechanism that makes those actions fail at the OS
# layer regardless of what the gate believes or its prompt claims.
#
# It differs from PROFILE_TEMPLATE in one load-bearing way: PROFILE_TEMPLATE
# allows writes inside the workspace because a dev.task worker's job is to
# edit it. The gate's job is to judge a checkout, not change it, so the
# checkout named here gets NO write allowance at all - only a scratch dir
# outside it does. A `git commit` or an in-place edit against the reviewed
# checkout is therefore denied the same way a write anywhere else outside
# scratch is: there is no allow-rule that covers it. `(deny network*)` is
# added outright, rather than left to the credential the gate happens to be
# handed, because a gate that cannot open a socket cannot `git push`,
# cannot call `gh`, and cannot reach the production API the A36 incident
# reached for - one primitive covers all three rather than three separate
# promises.
GATE_PROFILE_TEMPLATE = """\
;; Rendered by apps/factory-dispatcher/containment.py — OPS-61.
;; Read-only boundary for release-gate dispatches (Amendment 37 clause 1,
;; carried forward from Amendment 36). The reviewed checkout is not in the
;; write allow-list below - only the scratch dir is - so a commit, an edit,
;; or a push against the checkout is denied by the OS. Network is denied
;; outright so gh, git push, and any other outbound call fail the same way.
(version 1)
(allow default)
(deny file-write*)
(deny network*)
(allow file-write*
  (subpath "@SCRATCH@")
  (subpath "/dev")
  (literal "/dev/null"))
"""

GATE_PROFILE_NAME = ".gate-containment.sb"
GATE_SCRATCH_NAME = ".gate-scratch"


def render_gate_profile(scratch: Path) -> str:
    """The read-only gate profile text, write access scoped to ``scratch``.

    ``scratch`` is resolved before rendering for the same reason
    :func:`render_profile` resolves the workspace: sandbox-exec matches
    subpaths against the OS-resolved path, and /tmp and /var are symlinks
    into /private on macOS - an unresolved allow denies every write inside
    the directory it names.
    """
    return GATE_PROFILE_TEMPLATE.replace("@SCRATCH@", str(scratch.resolve()))


def prepare_gate_containment(checkout: Path) -> tuple[Path, Path]:
    """Write the read-only gate profile and its scratch dir NEXT TO the
    checkout being reviewed - never inside it, mirroring
    :func:`prepare_containment`'s placement, so the profile/scratch
    themselves never read as part of the tree under review.

    Unlike :func:`prepare_containment`, the checkout itself receives no
    write allowance at all: only the returned scratch dir does. Returns
    ``(profile_path, scratch_dir)``.
    """
    holder = checkout.resolve().parent
    scratch = holder / GATE_SCRATCH_NAME
    scratch.mkdir(exist_ok=True)
    profile = holder / GATE_PROFILE_NAME
    profile.write_text(render_gate_profile(scratch))
    return profile, scratch


# --- Declared-verification containment (dev.finding 6c19f60f) ---
#
# See the module docstring's paragraph on this. Everything below is new
# mechanism; dispatch.py only wires its existing call sites to it.

#: Where the venv `bootstrap_verification_env` builds now lives -- a sibling
#: of the clone, never inside it, so a worker-planted <clone>/venv package
#: (dev.finding 6c19f60f AC-3) has nothing left to shadow.
VERIFICATION_VENV_NAME = ".verification-venv"
VERIFY_PROFILE_NAME = ".verify-containment.sb"
VERIFY_TMPDIR_NAME = ".verify-tmp"

#: The dispatcher's own state directory: verification never legitimately
#: reads it (the clone's origin is reset to cfg.remote, and
#: verify_clone_isolated refuses alternates), so it is one of the things this
#: profile denies READING, on top of the write-deny both profiles share. The
#: worker profile (PROFILE_TEMPLATE, above) denies reading it too, for the
#: separate reason that it holds the write key (dev.finding c48a3827) --
#: must equal worker_revision.STATE_DIR_ENV (asserted by a cross-module
#: test); kept as a literal here rather than imported, for the same
#: import-cycle reason as the SUBSTRATE_*_KEY_ENV names above.
STATE_DIR_ENV = "FACTORY_DISPATCHER_STATE_DIR"

VERIFY_PROFILE_TEMPLATE = """\
;; Rendered by apps/factory-dispatcher/containment.py — dev.finding 6c19f60f.
;; Declared verification runs worker-authored content (a planted conftest.py,
;; a hostile PEP 517 build backend); this profile denies every write outside
;; the command's own cwd/.verify-tmp/venv, denies reading the dispatcher's
;; state directory and the host's named credential directories outright, and
;; denies outbound ssh and every loopback port but the substrate tunnel's.
(version 1)
(allow default)
{READ_DENIES}
{NETWORK_RULES}
{MACH_JOB_DENIES}
(deny file-write*)
(allow file-write*{WRITE_ALLOWS})
{GIT_WRITE_DENIES}
"""


def verification_venv_path(clone: Path) -> Path:
    """Where the verification interpreter lives for ``clone`` -- OUTSIDE it."""
    return clone.resolve().parent / VERIFICATION_VENV_NAME


def verify_tmp_env(verify_tmp: Path) -> dict[str, str]:
    """Cache/tmp variables a contained subprocess's env needs pointed at
    ``verify_tmp`` instead of $HOME or /tmp, where the profile denies writes.
    """
    return {
        "TMPDIR": str(verify_tmp),
        "npm_config_cache": str(verify_tmp / "npm-cache"),
        "XDG_CACHE_HOME": str(verify_tmp / "xdg-cache"),
    }


def state_dir_read_denies(environ: dict[str, str] | None = None) -> list[Path]:
    """Resolved director(y/ies) the worker and verification must never read
    from -- shared by ``render_profile``/``prepare_containment`` (the
    worker's own profile) and ``render_verification_profile``/
    ``prepare_verification_containment`` below.

    Always ``Path.home()/".factory-dispatcher"``; additionally
    ``FACTORY_DISPATCHER_STATE_DIR`` when set and non-empty, resolved
    separately so an operator relocation is covered too. Resolved at CALL
    time, not import time, so a test that monkeypatches HOME or the env var
    sees it reflected. De-duplicated when both name the same path.
    """
    denies = [(Path.home() / ".factory-dispatcher").resolve()]
    values = os.environ if environ is None else environ
    override = values.get(STATE_DIR_ENV)
    if override:
        resolved = Path(override).resolve()
        if resolved not in denies:
            denies.append(resolved)
    return denies


def _under_any(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        if path == root or root in path.parents:
            return root
    return None


# --- dev.findings 019909c0, 75b50b58, dd648709: the host's named credential
# directories, KUBECONFIG's targets, ssh, and every loopback port but the
# substrate tunnel's -- rendered for BOTH render_profile and
# render_verification_profile from the functions below, so the two profiles
# cannot drift apart. ---


def _resolved_credential_dirs() -> list[Path]:
    """CREDENTIAL_DIRS, resolved at call time under the live Path.home()."""
    return [(Path.home() / rel).resolve() for rel in CREDENTIAL_DIRS]


def _all_denied_subpaths(environ: dict[str, str] | None = None) -> list[Path]:
    """Every directory either profile denies reading WHOLESALE: the
    dispatcher's own state director(y/ies) plus CREDENTIAL_DIRS. Shared by
    :func:`_read_deny_lines` (what gets rendered) and by
    :func:`prepare_containment`/:func:`prepare_verification_containment`
    (what a workspace/verification cwd must not resolve under) -- the two
    checks must agree, or a workspace the profile cannot read would fail
    every dispatch as an opaque work failure instead of this named refusal.
    """
    return state_dir_read_denies(environ) + _resolved_credential_dirs()


def _kubeconfig_literal_denies(
    denied_subpaths: list[Path], environ: dict[str, str] | None
) -> list[Path]:
    """Resolved KUBECONFIG entries not already covered by ``denied_subpaths``.

    KUBECONFIG may name an ``os.pathsep``-joined chain (kubectl merges
    them); each non-empty entry is resolved and denied individually unless
    it already falls under a directory this module denies wholesale.
    """
    values = os.environ if environ is None else environ
    raw = values.get("KUBECONFIG") or ""
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        if not part:
            continue
        resolved = Path(part).resolve()
        if _under_any(resolved, denied_subpaths) is not None:
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def _symlink_child_denies(denied_subpaths: list[Path]) -> list[tuple[str, Path]]:
    """Resolved targets of any symlink directly inside an existing
    CREDENTIAL_DIRS entry, not already covered by ``denied_subpaths`` -- a
    directory deny alone does not follow a symlink the directory contains
    out to wherever it points.
    """
    out: list[tuple[str, Path]] = []
    for cred_dir in _resolved_credential_dirs():
        if not cred_dir.is_dir():
            continue
        try:
            children = sorted(cred_dir.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_symlink():
                continue
            try:
                target = child.resolve(strict=True)
            except OSError:
                continue
            if _under_any(target, denied_subpaths) is not None:
                continue
            entry = ("subpath" if target.is_dir() else "literal", target)
            if entry not in out:
                out.append(entry)
    return out


def _read_deny_lines(environ: dict[str, str] | None = None) -> str:
    """Every ``(deny file-read* ...)`` line the worker and verification
    profiles share: the dispatcher's state directory (dev.finding
    c48a3827), CREDENTIAL_DIRS, the files KUBECONFIG names, and symlinked
    escapes out of CREDENTIAL_DIRS (dev.findings 019909c0, 75b50b58).
    """
    denied_subpaths = _all_denied_subpaths(environ)
    lines = [f'(deny file-read* (subpath "{d}"))' for d in denied_subpaths]
    lines += [
        f'(deny file-read* (literal "{p}"))'
        for p in _kubeconfig_literal_denies(denied_subpaths, environ)
    ]
    lines += [
        f'(deny file-read* ({form} "{p}"))'
        for form, p in _symlink_child_denies(denied_subpaths)
    ]
    return "\n".join(lines)


def _network_rule_lines() -> str:
    """The only network rules either profile carries (dev.findings 75b50b58,
    dd648709): outbound ssh denied outright, Temporal's gRPC port denied on
    every host (since 2026-10-08 Temporal is also a tailnet device,
    `temporal.<tailnet>.ts.net:7233`, so a loopback-only deny no longer
    bounds it; the worker has no legitimate use of 7233 anywhere), every
    loopback TCP port denied except the substrate tunnel's -- allowed LAST,
    because in SBPL the later rule wins (measured: swapping the order denies
    the substrate port too). A function-level import: a module-level ``import launchd_agent``
    would cycle (launchd_agent -> worker_checkout -> dispatch ->
    containment).
    """
    from launchd_agent import TUNNEL_DEFAULTS

    substrate_port = TUNNEL_DEFAULTS["substrate-prod"]["local_port"]
    temporal_port = TUNNEL_DEFAULTS["temporal"]["local_port"]
    return (
        '(deny network-outbound (remote tcp "*:22"))\n'
        f'(deny network-outbound (remote tcp "*:{temporal_port}"))\n'
        '(deny network-outbound (remote tcp "localhost:*"))\n'
        f'(allow network-outbound (remote tcp "localhost:{substrate_port}"))'
    )


def _mach_job_deny_lines() -> str:
    """The only mach and job rules either profile carries (dev.finding
    dd648709's loopback survey; the part-B design's §9.2 amendment):
    MACH_DENY's services unreachable, and no job-creation at all.
    """
    names = " ".join(f'(global-name "{name}")' for name in MACH_DENY)
    return f"(deny mach-lookup {names})\n(deny job-creation)"


def _git_write_deny_lines(resolved: Path) -> str:
    """The five ``(deny file-write* ...)`` lines dev.finding 79db3113 part a
    adds to BOTH profiles, beside ``resolved`` (a worker's resolved
    workspace, or a verification command's resolved cwd): the ``.git`` entry
    itself (a literal, so it cannot be renamed or replaced out from under
    git), ``.git/hooks`` and ``.git/info`` (subpaths), and ``.git/config``
    and ``.git/commondir`` (literals). Shared by :func:`render_profile` and
    :func:`render_verification_profile` so the two profiles cannot drift
    apart on this rule, the same reason :func:`_read_deny_lines`,
    :func:`_network_rule_lines` and :func:`_mach_job_deny_lines` are shared.

    Both templates place these lines AFTER their ``(allow file-write* ...)``
    block: in SBPL the later rule wins (measured for the substrate-port
    allow in :func:`_network_rule_lines`), so a deny placed before the
    allow would be overridden by it.

    ``.git/objects``, ``.git/index``, ``.git/refs``, ``.git/logs`` and
    ``.git/worktrees`` are deliberately left writable -- ``git add`` in a
    linked worktree writes loose objects into the common store (#1071), and
    the literal deny on ``.git`` itself does not block creating or renaming
    its children, so ``index.lock`` still works.

    ``.git/commondir`` is denied even though an ordinary (non-worktree)
    clone never creates one: git reads ``<gitdir>/commondir`` when present
    and redirects config, hooks, objects and refs lookups to the directory
    it names, so a worker-writable commondir bypasses every other deny here
    (measured 2026-10-02: with ``.git/commondir`` pointing at a
    worker-controlled directory, ``git -c core.hooksPath=/dev/null add``
    fired a clean filter from the redirected config and ``git commit`` ran
    the redirected ``hooks/pre-commit``).
    """
    git_dir = resolved / ".git"
    return "\n".join(
        [
            f'(deny file-write* (literal "{git_dir}"))',
            f'(deny file-write* (subpath "{git_dir / "hooks"}"))',
            f'(deny file-write* (literal "{git_dir / "config"}"))',
            f'(deny file-write* (subpath "{git_dir / "info"}"))',
            f'(deny file-write* (literal "{git_dir / "commondir"}"))',
        ]
    )


def linked_worktree_write_extras(cwd: Path) -> tuple[Path, ...]:
    """The extra write-allowances a linked git worktree's cwd needs.

    ``classify_verification_failure_against_base`` runs its base-revision
    command in a `git worktree add`-created linked worktree. `git add` there
    writes loose objects into the COMMON object store, not just the
    worktree's own admin dir (`<clone>/.git/worktrees/<name>`) -- measured:
    the admin dir alone still gets EPERM ("unable to create temporary file")
    on `git add`. Parsed directly from the worktree's `.git` file and the
    admin dir's `commondir` file (git-worktree(1)) rather than shelled out to
    `git rev-parse`, so this module stays free of a dependency on
    `dispatch.run`. Returns `()` for a plain (non-worktree) checkout, whose
    `.git` is an ordinary directory.
    """
    git_marker = cwd.resolve() / ".git"
    try:
        if not git_marker.is_file():
            return ()
        text = git_marker.read_text()
    except OSError:
        return ()
    match = re.match(r"gitdir:\s*(.+?)\s*$", text.strip())
    if not match:
        return ()
    admin_dir = Path(match.group(1))
    if not admin_dir.is_absolute():
        admin_dir = cwd.resolve() / admin_dir
    try:
        admin_dir = admin_dir.resolve(strict=True)
    except OSError:
        return ()
    try:
        common_text = (admin_dir / "commondir").read_text().strip()
    except OSError:
        return (admin_dir,)
    common_dir = Path(common_text)
    if not common_dir.is_absolute():
        common_dir = admin_dir / common_dir
    try:
        common_dir = common_dir.resolve(strict=True)
    except OSError:
        return (admin_dir,)
    return (admin_dir, common_dir / "objects")


def render_verification_profile(
    cwd: Path,
    extra_write: tuple[Path, ...] = (),
    environ: dict[str, str] | None = None,
) -> str:
    """The verification profile text for a command run at ``cwd``.

    Every path is resolved before rendering, for the same reason
    :func:`render_profile` resolves the workspace. Writes: ``cwd``, its
    ``.verify-tmp``, ``/dev``, plus ``extra_write``. Reads: everything
    except :func:`_read_deny_lines` -- the dispatcher's state directory,
    CREDENTIAL_DIRS, KUBECONFIG's targets, and their symlinked escapes
    (dev.findings 019909c0, 75b50b58). Network: everything except outbound
    ssh and every loopback port but the substrate tunnel's (dev.findings
    75b50b58, dd648709) -- pip and gh stay reachable; a local service other
    than the tunnel does not.

    Also denies WRITING ``cwd``'s ``.git`` entry, ``.git/hooks``,
    ``.git/config``, ``.git/info`` and ``.git/commondir`` (dev.finding
    79db3113 part a) -- see :func:`_git_write_deny_lines`. These come AFTER
    the write-allow block above so the deny wins.
    """
    resolved_cwd = cwd.resolve()
    verify_tmp = resolved_cwd.parent / VERIFY_TMPDIR_NAME
    writes = [resolved_cwd, verify_tmp, *(p.resolve() for p in extra_write)]
    write_allows = "".join(f'\n  (subpath "{p}")' for p in writes)
    write_allows += '\n  (subpath "/dev")\n  (literal "/dev/null")'
    return (
        VERIFY_PROFILE_TEMPLATE.replace("{WRITE_ALLOWS}", write_allows)
        .replace("{READ_DENIES}", _read_deny_lines(environ))
        .replace("{NETWORK_RULES}", _network_rule_lines())
        .replace("{MACH_JOB_DENIES}", _mach_job_deny_lines())
        .replace("{GIT_WRITE_DENIES}", _git_write_deny_lines(resolved_cwd))
    )


def prepare_verification_containment(
    cwd: Path,
    extra_write: tuple[Path, ...] = (),
    environ: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Write the verification profile and its private tmpdir NEXT TO ``cwd``.

    Mirrors :func:`prepare_containment`'s placement rule so neither the
    profile nor the tmpdir is ever inside the directory the command runs in
    (and so never appears in `changed_paths`). Raises ``ValueError`` naming
    both paths when ``cwd`` resolves to, or under, any directory
    :func:`_all_denied_subpaths` names (the dispatcher's own state
    directory or a CREDENTIAL_DIRS entry) -- a host-configuration fault,
    since verification has no legitimate reason to run there.
    """
    resolved_cwd = cwd.resolve()
    collision = _under_any(resolved_cwd, _all_denied_subpaths(environ))
    if collision is not None:
        raise ValueError(
            f"verification cwd {resolved_cwd} resolves under {collision}, "
            "which this profile denies reading; refusing to run there"
        )
    holder = resolved_cwd.parent
    verify_tmp = holder / VERIFY_TMPDIR_NAME
    verify_tmp.mkdir(exist_ok=True)
    profile = holder / VERIFY_PROFILE_NAME
    profile.write_text(render_verification_profile(cwd, extra_write, environ))
    return profile, verify_tmp
