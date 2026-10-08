"""The pure core of running untrusted factory work as its own macOS user (Amendment 30,
part B, design §8 row B1: docs/plans/2026-09-26-design-part-b-worker-runs-as-separate-user.md).

Nothing calls this module yet -- B4 and B5 wire it into the split run and verification paths.
Every host side effect (a sudo call, a chmod ACL grant) is reachable only through a dedicated
function or an injectable seam; no test here touches a real account, a real sudo, or a real
ACL. Cross-uid facts (EACCES, EINVAL, the launchd session, claude authenticating as the worker
user) are proven only by the attended probe and the canaries (design §10), never by a test
here. "Split mode" means FACTORY_WORKER_USER is set (design §1); load_config returns None when
it is unset, so a caller treats None as "behave exactly as today." This module reads no
credential, directly or indirectly: load_config and build_launch_call take an explicit
environ/constants rather than os.environ, and the run payload is built by
containment.worker_environment (unchanged, imported here), the same builder run_worker uses.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping

import containment
import dispatch
import process_env

# --- AC-1: config, or None ---------------------------------------------------

FACTORY_WORKER_USER_ENV = "FACTORY_WORKER_USER"
RUNS_ROOT = "/Users/Shared/factory-runs"  # design §2.2's R

#: The exact path the sudoers rule names (design §3.1): the operator account may run this one
#: path, NOPASSWD, as the worker user -- nothing else.
LAUNCHER_PATH = "/Users/Shared/factory/bin/factory-worker-launch"
INSTALLED_PROCESS_ENV_PATH = str(Path(LAUNCHER_PATH).parent / "process_env.py")  # beside it

@dataclass(frozen=True)
class WorkerIdentityConfig:
    user: str
    runs_root: Path = Path(RUNS_ROOT)
    launcher_path: Path = Path(LAUNCHER_PATH)
    process_env_path: Path = Path(INSTALLED_PROCESS_ENV_PATH)

def load_config(environ: Mapping[str, str]) -> WorkerIdentityConfig | None:
    """Split mode's config, from an explicit mapping -- never os.environ. None when
    FACTORY_WORKER_USER is unset; ValueError when set but blank/whitespace-only -- an
    operator who clears it to "" meant to unset it, not to name a user called ""."""
    raw = environ.get(FACTORY_WORKER_USER_ENV)
    if raw is None:
        return None
    user = raw.strip()
    if not user:
        raise ValueError(f"{FACTORY_WORKER_USER_ENV} is set but blank")
    return WorkerIdentityConfig(user=user)

# --- AC-2: the golden launch call --------------------------------------------

#: design §3.3's closed subcommand set.
SUBCOMMANDS: frozenset[str] = frozenset(
    "seed vseed materialize run export exec venv pip selfcheck probe reap purge".split()
)

SUDO_PATH = "/usr/bin/sudo"
LAUNCH_TIMEOUT_MARGIN_S = 120.0  # design §3.3: "the deadline plus a 120-second margin"

#: A fixed, minimal PATH for the sudo call's own env (design §3.3: "env of PATH only") -- a
#: literal, never derived from os.environ, so the worker account's PATH never depends on
#: whatever the operator's own shell happens to export.
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

@dataclass(frozen=True)
class LaunchCall:
    argv: tuple[str, ...]
    env: dict[str, str]
    input: bytes
    timeout: float

def build_launch_call(
    cfg: WorkerIdentityConfig,
    subcommand: str,
    workdir: Path,
    payload: bytes,
    deadline_s: float,
    *,
    sudo_path: str = SUDO_PATH,
    margin_s: float = LAUNCH_TIMEOUT_MARGIN_S,
) -> LaunchCall:
    """design §3.3's call, pure: builds argv/env/input/timeout, runs nothing. ``sudo_path``
    and ``margin_s`` are the two named seams a test overrides without touching the
    module-level constants."""
    if subcommand not in SUBCOMMANDS:
        raise ValueError(f"unknown subcommand: {subcommand!r}")
    argv = (sudo_path, "-n", "-u", cfg.user, str(cfg.launcher_path), subcommand, str(workdir))
    env = process_env.child_env(environ={"PATH": MINIMAL_PATH})
    return LaunchCall(argv=argv, env=env, input=payload, timeout=deadline_s + margin_s)

def run_launch_call(call: LaunchCall) -> subprocess.CompletedProcess:
    """Actually executes ``call``. Used today only by the AC-6 real-timeout test; no other
    caller exists until B4. Propagates subprocess.TimeoutExpired -- the caller's job is to
    catch it and feed dispatcher_timeout_fired=True into map_exit."""
    return subprocess.run(
        call.argv, input=call.input, env=call.env, timeout=call.timeout, capture_output=True
    )

# --- AC-7: the injectable ACL runner -----------------------------------------

#: design §2.2's four permission sets, exact strings rendered verbatim into the ACE spec.
ALLOWED_ACL_PERMISSIONS: tuple[str, ...] = (
    "search",
    "search,list,add_subdirectory",
    "read",
    "list,search,add_file,add_subdirectory,delete_child",
)

AclRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess]

def _acl_argv(flag: str, user: str, permissions: str, path: Path) -> tuple[str, ...]:
    if permissions not in ALLOWED_ACL_PERMISSIONS:
        raise ValueError(f"unsupported ACL permission set: {permissions!r}")
    return ("chmod", flag, f"{user} allow {permissions}", str(path))

def _default_acl_runner(argv: tuple[str, ...]) -> subprocess.CompletedProcess:
    env = process_env.child_env(environ={"PATH": MINIMAL_PATH})
    return subprocess.run(argv, env=env, check=False, capture_output=True)

def run_acl_add(
    user: str, permissions: str, path: Path, *, runner: AclRunner | None = None
) -> subprocess.CompletedProcess:
    """``chmod +a "<user> allow <permissions>" <path>`` (design §2.2), through ``runner``."""
    return (runner or _default_acl_runner)(_acl_argv("+a", user, permissions, path))

def run_acl_remove(
    user: str, permissions: str, path: Path, *, runner: AclRunner | None = None
) -> subprocess.CompletedProcess:
    """The matching ``chmod -a`` removal, through ``runner``."""
    return (runner or _default_acl_runner)(_acl_argv("-a", user, permissions, path))

# --- AC-3/AC-4: per-subcommand payloads --------------------------------------

#: The run payload's allowlist (AC-3): exactly these, where present.
RUN_PAYLOAD_KEYS: tuple[str, ...] = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SUBSTRATE_API_KEY",
    "SUBSTRATE_URL",
    "DISABLE_AUTOUPDATER",
)

def build_run_payload(
    base_env: Mapping[str, str], extra_env: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    """The ``run`` subcommand's payload: PR 1087's builder, narrowed to RUN_PAYLOAD_KEYS.
    ``base_env`` is the dispatcher's own environment, read by the caller -- never here --
    and ``extra_env`` is the claude WorkerEntry's extra_env (where DISABLE_AUTOUPDATER comes
    from). A refusal from containment.worker_environment is re-raised unchanged as
    dispatch.DispatchEnvironmentError -- identical to what run_worker raises today for the
    same condition -- and no launch call is built."""
    env, reason = containment.worker_environment(dict(base_env), extra_env)
    if reason is not None:
        raise dispatch.DispatchEnvironmentError(reason)
    return {key: env[key] for key in RUN_PAYLOAD_KEYS if key in env}

def build_payload(subcommand: str, fields: Mapping[str, str] | None = None) -> dict[str, str]:
    """The payload for every subcommand except ``run``: no credential-named key, ever --
    mechanically enforced (ValueError) rather than left as a property later callers must
    remember."""
    if subcommand == "run":
        raise ValueError("build_run_payload builds the run payload; it carries credentials")
    if subcommand not in SUBCOMMANDS:
        raise ValueError(f"unknown subcommand: {subcommand!r}")
    fields = fields or {}
    for name in fields:
        if process_env.is_credential_name(name):
            raise ValueError(
                f"payload field {name!r} is a credential-named key, refused for "
                f"subcommand {subcommand!r}"
            )
    return dict(fields)

# --- AC-5: the exit mapping (design §3.5), a pure function -------------------

EXEC_STREAM_CAP_BYTES = 1 * 1024 * 1024
RUN_STDOUT_CAP_BYTES = 16 * 1024 * 1024
RUN_STDERR_CAP_BYTES = 1 * 1024 * 1024
ENVELOPE_CAP_SLACK_BYTES = 4 * 1024
EXEC_ENVELOPE_CAP_BYTES = 2 * EXEC_STREAM_CAP_BYTES + ENVELOPE_CAP_SLACK_BYTES
RUN_ENVELOPE_CAP_BYTES = RUN_STDOUT_CAP_BYTES + RUN_STDERR_CAP_BYTES + ENVELOPE_CAP_SLACK_BYTES

_ENVELOPE_STATUSES = ("ok", "deadline", "refused")

def _parse_envelope(raw: bytes, cap: int) -> dict | None:
    """A well-formed envelope from ``raw``, or None (missing/malformed/over cap)."""
    if len(raw) > cap:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    status = doc.get("status")
    if status not in _ENVELOPE_STATUSES:
        return None
    stdout, stderr = doc.get("stdout", ""), doc.get("stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    child_rc = doc.get("child_rc")
    if status == "ok" and (not isinstance(child_rc, int) or isinstance(child_rc, bool)):
        return None
    truncated = doc.get("stdout_truncated_result", False)
    if not isinstance(truncated, bool):
        return None
    return {
        "status": status, "stdout": stdout, "stderr": stderr, "child_rc": child_rc,
        "stdout_truncated_result": truncated,
    }

def _timed_out(kind, stdout_text, stderr_text, duration_s, command, timeout_s):
    if kind == "run":
        return dispatch.WorkerResult(
            exit_code=-1, timed_out=True, stdout=stdout_text or "", stderr=stderr_text or "",
            duration_s=duration_s,
        )
    return dispatch.VerificationCommandResult(
        command=command, outcome="failed", exit_code=None,
        output=(stdout_text or "") + (stderr_text or ""), duration_s=duration_s,
        reason=f"timed out after {timeout_s:.0f}s",
    )

def _opaque_failure(kind, stdout_text, stderr_text, exit_code, duration_s, command):
    """Row (c)/truncated-result: no trustworthy envelope. Always charged; for ``run`` the
    exit code is never 0 (the process's own code if nonzero, else 1)."""
    if kind == "run":
        code = exit_code if exit_code not in (None, 0) else 1
        return dispatch.WorkerResult(
            exit_code=code, stdout=(stdout_text or "") + (stderr_text or ""),
            duration_s=duration_s, timed_out=False, stderr=stderr_text or "",
        )
    output = (stdout_text or "") + (stderr_text or "")
    reason = stderr_text or stdout_text or "launcher produced no well-formed envelope"
    return dispatch.VerificationCommandResult(
        command=command, outcome="failed", exit_code=exit_code, output=output,
        duration_s=duration_s, reason=reason.strip()[-500:],
    )

def _graded_ok(kind, envelope, duration_s, command, grade_json_result):
    """Row (f): graded exactly as the non-split path grades the same bytes, by calling
    dispatch's own (private) grading helpers against a stand-in for a CompletedProcess --
    rather than a second implementation that could silently drift from them."""
    child_rc, stdout_text, stderr_text = (
        envelope["child_rc"], envelope["stdout"], envelope["stderr"]
    )
    proc_like = SimpleNamespace(returncode=child_rc, stdout=stdout_text, stderr=stderr_text)
    if kind == "run":
        if grade_json_result:
            return dispatch._grade_json_worker_result(proc_like, duration_s)
        return dispatch.WorkerResult(
            exit_code=child_rc, stdout=stdout_text + stderr_text, duration_s=duration_s,
            timed_out=False, stderr=stderr_text,
        )
    output = stdout_text + stderr_text
    if dispatch._could_not_start(proc_like):
        reason = (output.strip() or f"shell exited {child_rc}")[-500:]
        return dispatch.VerificationCommandResult(
            command=command, outcome="could_not_start", exit_code=child_rc, output=output,
            duration_s=duration_s, reason=reason,
        )
    outcome = "passed" if child_rc == 0 else "failed"
    return dispatch.VerificationCommandResult(
        command=command, outcome=outcome, exit_code=child_rc, output=output,
        duration_s=duration_s,
    )

def map_exit(
    kind: str,
    launcher_stdout: bytes,
    sudo_stderr: bytes,
    exit_code: int | None,
    dispatcher_timeout_fired: bool,
    duration_s: float,
    *,
    command: str | None = None,
    timeout_s: float | None = None,
    grade_json_result: bool = False,
):
    """design §3.5's exit mapping, pure. ``kind`` is "run" or "exec". Only a well-formed
    envelope, or sudo's stderr starting with "sudo:", is trusted. A dispatcher-side timeout
    is checked FIRST, before any envelope parse, because the call never completed and
    anything captured is partial and untrusted (row b) -- distinct from the launcher's own
    envelope reporting status "deadline" (row a), where the call completed and the launcher
    itself detected its child's timeout. An exit code of 124/125 without a well-formed
    envelope is never treated as a timeout; it falls through to the opaque-failure row (c).
    """
    if kind not in ("run", "exec"):
        raise ValueError(f"unknown kind: {kind!r}")
    if kind == "exec" and (command is None or timeout_s is None):
        raise ValueError("exec requires command and timeout_s")

    sudo_text = sudo_stderr.decode("utf-8", "replace")

    if dispatcher_timeout_fired:
        return _timed_out(
            kind, launcher_stdout.decode("utf-8", "replace"), sudo_text, duration_s, command,
            timeout_s,
        )

    cap = RUN_ENVELOPE_CAP_BYTES if kind == "run" else EXEC_ENVELOPE_CAP_BYTES
    envelope = _parse_envelope(launcher_stdout, cap)

    if envelope is not None:
        if envelope["status"] == "deadline":
            return _timed_out(
                kind, envelope["stdout"], envelope["stderr"], duration_s, command, timeout_s
            )
        if envelope["status"] == "refused":
            reason = (
                envelope["stderr"] or envelope["stdout"]
                or "worker-identity launcher refused the call"
            )
            if kind == "run":
                raise dispatch.DispatchEnvironmentError(reason)
            return dispatch.VerificationCommandResult(
                command=command, outcome="could_not_start", exit_code=None, output=reason,
                duration_s=duration_s, reason=reason,
            )
        if kind == "run" and envelope["stdout_truncated_result"]:
            return _opaque_failure(
                kind, envelope["stdout"], envelope["stderr"], envelope["child_rc"],
                duration_s, command,
            )
        return _graded_ok(kind, envelope, duration_s, command, grade_json_result)

    if sudo_text.lstrip().startswith("sudo:"):
        if kind == "run":
            raise dispatch.DispatchEnvironmentError(sudo_text)
        return dispatch.VerificationCommandResult(
            command=command, outcome="could_not_start", exit_code=exit_code, output=sudo_text,
            duration_s=duration_s, reason=sudo_text,
        )

    return _opaque_failure(
        kind, launcher_stdout.decode("utf-8", "replace"), sudo_text, exit_code, duration_s,
        command,
    )

# --- AC-8: import-time hashes -------------------------------------------------

_MODULE_DIR = Path(__file__).resolve().parent

def sha256_or_none(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()

#: Recorded at IMPORT, beside this module's own __file__ (design §3.4). worker_launcher.py
#: does not exist until B2, so this is None until then -- by design, not by accident.
WORKER_LAUNCHER_SOURCE_HASH: str | None = sha256_or_none(_MODULE_DIR / "worker_launcher.py")
PROCESS_ENV_SOURCE_HASH: str | None = sha256_or_none(_MODULE_DIR / "process_env.py")

def source_hashes() -> tuple[str | None, str | None]:
    """(worker_launcher.py hash, process_env.py hash), recorded at import -- never re-read."""
    return WORKER_LAUNCHER_SOURCE_HASH, PROCESS_ENV_SOURCE_HASH

def compare_hash(source_hash: str | None, installed_bytes: bytes | None) -> str:
    """"match"/"mismatch"/"missing" -- "missing" covers an absent installed copy AND an
    absent source (recorded as None at import)."""
    if installed_bytes is None or source_hash is None:
        return "missing"
    return "match" if hashlib.sha256(installed_bytes).hexdigest() == source_hash else "mismatch"

def compare_installed_hashes(
    worker_launcher_bytes: bytes | None, process_env_bytes: bytes | None
) -> dict[str, str]:
    """Per-file match/mismatch/missing against the hashes recorded at import."""
    return {
        "worker_launcher.py": compare_hash(WORKER_LAUNCHER_SOURCE_HASH, worker_launcher_bytes),
        "process_env.py": compare_hash(PROCESS_ENV_SOURCE_HASH, process_env_bytes),
    }
