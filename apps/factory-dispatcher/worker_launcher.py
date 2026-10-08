#!/opt/homebrew/bin/python3.12 -I
"""Part B, B2a (design docs/plans/2026-09-26-design-part-b-worker-runs-as-separate-user.md
§8 row B2, first half): the launcher that runs factory work as the worker user.

Invariant L (design §2.1): the launcher runs nothing from a worker-writable path and reads
no worker-written configuration outside one of the three named profiles (worker,
verification, pip). Only selfcheck, probe, reap and purge run unsandboxed, and none of them
executes worker content. SUBCOMMAND_PROFILE/SUBCOMMAND_FUNCTION record that mapping; an
ast-based static test (tests/test_worker_launcher.py) enforces it, including that this
module never reads the process environment (no `environ`/`getenv` name appears below).

B2b adds the git, venv, pip, purge and probe subcommands. Nothing calls this module yet --
worker_identity.py (B1) is not wired to it until B4, and this module imports nothing from
this checkout except process_env, installed beside it.

Every result is exactly one JSON envelope on stdout: {status, child_rc, stdout, stderr,
stdout_truncated_result}. status is "ok", "deadline" (deadline_s elapsed, whole process
group killed) or "refused" (no worker/verification child ever started). Exit code is
always 0 -- the envelope carries the outcome (design §3.5).
"""

from __future__ import annotations

import base64
import ctypes
import functools
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_env  # noqa: E402 -- installed beside this file (design §3.3)

# --- AC-1: the closed subcommand set -------------------------------------------

#: design §2.1's full categorical set -- Invariant L's own statement of what never runs
#: sandboxed, not a population derived from what this half happens to spawn.
UNSANDBOXED_SUBCOMMANDS: frozenset[str] = frozenset({"selfcheck", "probe", "reap", "purge"})

#: Subcommand -> sandbox profile its spawn runs under, or "UNSANDBOXED".
SUBCOMMAND_PROFILE: dict[str, str] = {
    "selfcheck": "UNSANDBOXED",
    "reap": "UNSANDBOXED",
    "run": "worker",
    "exec": "verification",
    "seed": "worker",
    "vseed": "verification",
    "materialize": "worker",
    "export": "worker",
    "venv": "pip",
    "pip": "pip",
    "purge": "UNSANDBOXED",
    "probe": "UNSANDBOXED",
}

#: Fixed absolute binaries probe may execute (purge never spawns: it clears flags and
#: removes entries through os.chflags/os.chmod/os.unlink/os.rmdir directly).
LAUNCHCTL_PATH = "/bin/launchctl"
SECURITY_PATH = "/usr/bin/security"
GIT_PATH = "/usr/bin/git"
UNSANDBOXED_FIXED_BINARIES: tuple[str, ...] = (LAUNCHCTL_PATH, SECURITY_PATH, GIT_PATH)

SANDBOX_EXEC_PATH = "/usr/bin/sandbox-exec"

#: AC-12: the one fixed root (besides W itself) a B2b subcommand's profile or plist
#: path may resolve under -- where the launcher's own install lives (design §10 step
#: 10), never a payload-chosen location.
FIXED_TRUSTED_ROOT = "/Users/Shared/factory/bin"


def _realpath(path_str: object) -> str:
    return os.path.realpath(str(path_str))


def _is_under(real_path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    return real_path == root_real or real_path.startswith(root_real + os.sep)


def _confine_under_w_or_fixed_root(path_str: object, w_path: Path) -> str | None:
    """AC-12: None when ``path_str`` resolves under W or FIXED_TRUSTED_ROOT; a refusal
    reason otherwise. Pure -- no spawn, safe to share across every B2b subcommand."""
    real = _realpath(path_str)
    if _is_under(real, str(w_path)) or _is_under(real, FIXED_TRUSTED_ROOT):
        return None
    return f"{path_str!r} does not resolve under W or {FIXED_TRUSTED_ROOT}"


def _confine_bundle(bundle_str: object, w_path: Path) -> tuple[str | None, str | None]:
    """AC-12: ``bundle`` must resolve under W and be a regular file. Returns
    ``(realpath, None)`` on success or ``(None, reason)`` on refusal."""
    real = _realpath(bundle_str)
    if not _is_under(real, str(w_path)):
        return None, f"bundle {bundle_str!r} does not resolve under W"
    if not os.path.isfile(real):
        return None, f"bundle {bundle_str!r} is not a regular file"
    return real, None


def _confine_patch(patch_str: object, w_path: Path) -> tuple[str | None, str | None]:
    """AC-12: ``patch`` must resolve to exactly W/vtree/handoff.patch."""
    expected = _realpath(w_path / "vtree" / "handoff.patch")
    real = _realpath(patch_str)
    if real != expected:
        return None, f"patch {patch_str!r} must resolve to {expected}"
    return real, None


#: AC-11: restated equal to dispatch.git_in_clone's own pins (dispatch.py:823-829) --
#: this module imports nothing from this checkout except process_env (Invariant L),
#: so the four pins are duplicated here rather than imported; a test asserts equality.
GIT_CONFIG_COUNT_PINS: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)


def _git_env(payload_env: object) -> dict[str, str]:
    """AC-11: the one builder every launcher git spawn routes through. Drops every
    payload key starting ``GIT_`` (GIT_CONFIG_PARAMETERS, GIT_DIR, GIT_EXEC_PATH,
    GIT_SSH_COMMAND and the rest), then pins global/system config off and the four
    GIT_CONFIG_COUNT keys dispatch.git_in_clone uses."""
    base = payload_env if isinstance(payload_env, dict) else {}
    filtered = {k: v for k, v in base.items() if not k.startswith("GIT_")}
    env = process_env.child_env(environ=filtered)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_COUNT"] = str(len(GIT_CONFIG_COUNT_PINS))
    for index, (key, value) in enumerate(GIT_CONFIG_COUNT_PINS):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _base_ref_leading_dash_refusal(base_ref: object) -> str | None:
    """AC-12: pure pre-spawn check -- a ref starting with '-' (or absent/non-string)
    never reaches git, not even the check-ref-format validation call."""
    if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-"):
        return f"base_ref {base_ref!r} must not start with '-'"
    return None

# --- The envelope --------------------------------------------------------------

EXEC_STREAM_CAP_BYTES = 1 * 1024 * 1024
RUN_STDOUT_CAP_BYTES = 16 * 1024 * 1024
RUN_STDERR_CAP_BYTES = 1 * 1024 * 1024
ENVELOPE_CAP_SLACK_BYTES = 4 * 1024
#: Equal to worker_identity's constants of the same name: the SERIALIZED envelope's cap,
#: not the raw stream bytes -- replace-decoding and JSON escaping can both inflate a
#: capped stream past its own byte count once encoded (see _fit_envelope).
EXEC_ENVELOPE_CAP_BYTES = 2 * EXEC_STREAM_CAP_BYTES + ENVELOPE_CAP_SLACK_BYTES
RUN_ENVELOPE_CAP_BYTES = RUN_STDOUT_CAP_BYTES + RUN_STDERR_CAP_BYTES + ENVELOPE_CAP_SLACK_BYTES

def _ok(child_rc: int | None, stdout_text: str, stderr_text: str, truncated: bool = False) -> dict:
    return {
        "status": "ok", "child_rc": child_rc, "stdout": stdout_text, "stderr": stderr_text,
        "stdout_truncated_result": truncated,
    }

def _deadline(stdout_text: str, stderr_text: str) -> dict:
    return {
        "status": "deadline", "child_rc": None, "stdout": stdout_text, "stderr": stderr_text,
        "stdout_truncated_result": False,
    }

def _refused(reason: str) -> dict:
    return {
        "status": "refused", "child_rc": None, "stdout": "", "stderr": reason,
        "stdout_truncated_result": False,
    }

def _cap_stream(raw: bytes, cap: int) -> str:
    """Tail-keep ``raw`` to ``cap`` bytes, decoded errors='replace', prefixed with the
    exact truncated-byte count when it was cut."""
    if len(raw) <= cap:
        return raw.decode("utf-8", errors="replace")
    truncated = len(raw) - cap
    tail = raw[-cap:] if cap > 0 else b""
    return f"[... {truncated} bytes truncated ...]" + tail.decode("utf-8", errors="replace")

def _stdout_truncated_result(raw: bytes, cap: int) -> bool:
    """True when the final line -- ``raw`` minus one trailing newline, then whatever
    follows the last remaining newline -- is itself longer than ``cap``."""
    body = raw[:-1] if raw.endswith(b"\n") else raw
    final_line = body.rsplit(b"\n", 1)[-1]
    return len(final_line) > cap

def _max_fitting_cap(size_fn: Callable[[int], int], cap: int, budget: int) -> int:
    """Binary search over the raw byte cap in [0, cap] for the largest value whose
    size_fn fits in budget: a linear subtraction of the serialized overage assumes 1
    raw byte costs 1 serialized byte, which errors='replace' (-> U+FFFD, 3 bytes) and
    JSON escaping both violate, overshooting past zero and losing a tail a smaller
    nonzero cap would have kept."""
    if size_fn(0) > budget:
        return 0
    lo, hi, best = 0, cap, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if size_fn(mid) <= budget:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best

def _fit_envelope(
    envelope: dict, stdout_raw: bytes, stderr_raw: bytes, stdout_cap: int, stderr_cap: int,
    envelope_cap: int,
) -> tuple[dict, int, int]:
    """Tail-caps both streams, then narrows whichever cap is larger via _max_fitting_cap
    until the envelope fits; returns the envelope plus the caps actually used, so a
    caller can tell whether fitting shrank a cap below its nominal value."""

    def size_with(so: int, se: int) -> int:
        trial = dict(envelope, stdout=_cap_stream(stdout_raw, so), stderr=_cap_stream(stderr_raw, se))
        return len(json.dumps(trial, ensure_ascii=False).encode("utf-8"))

    while size_with(stdout_cap, stderr_cap) > envelope_cap:
        if stdout_cap >= stderr_cap and stdout_cap > 0:
            stdout_cap = _max_fitting_cap(lambda c: size_with(c, stderr_cap), stdout_cap, envelope_cap)
        elif stderr_cap > 0:
            stderr_cap = _max_fitting_cap(lambda c: size_with(stdout_cap, c), stderr_cap, envelope_cap)
        else:
            break

    envelope["stdout"] = _cap_stream(stdout_raw, stdout_cap)
    envelope["stderr"] = _cap_stream(stderr_raw, stderr_cap)
    return envelope, stdout_cap, stderr_cap

def _refused_from_stderr(prefix: str, stderr_raw: bytes) -> dict:
    """Review item 4 (PR 1178 gate): a failing git call's own stderr can be arbitrarily
    large (a hostile or merely large clone/apply failure); fit it through _fit_envelope
    at the exec caps so the refused envelope's serialized size stays bounded, the same
    way `run`/`exec` bound their child's streams."""
    prefix_bytes = prefix.encode("utf-8")
    envelope = {
        "status": "refused", "child_rc": None, "stdout": "", "stderr": "",
        "stdout_truncated_result": False,
    }
    envelope, _, _ = _fit_envelope(
        envelope, prefix_bytes, stderr_raw, len(prefix_bytes), EXEC_STREAM_CAP_BYTES,
        EXEC_ENVELOPE_CAP_BYTES,
    )
    envelope["stderr"] = envelope["stdout"] + envelope["stderr"]
    envelope["stdout"] = ""
    return envelope

# --- Child env: HOME/TMPDIR/PATH/git identity, set AFTER the payload's env -----

GIT_IDENTITY_NAME = "factory-worker"
GIT_IDENTITY_EMAIL = "factory-worker@factory.invalid"
FIXED_CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
RUN_TMPDIR_NAME = ".tmp"
VERIFY_TMPDIR_NAME = ".verify-tmp"

def _finalize_child_env(env: dict[str, str], home: Path, tmp_dir: Path) -> dict[str, str]:
    env = dict(env)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp_dir)
    env["PATH"] = FIXED_CHILD_PATH
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = GIT_IDENTITY_NAME
    env["GIT_AUTHOR_EMAIL"] = GIT_IDENTITY_EMAIL
    env["GIT_COMMITTER_NAME"] = GIT_IDENTITY_NAME
    env["GIT_COMMITTER_EMAIL"] = GIT_IDENTITY_EMAIL
    return env

def _require(payload: dict, *names: str) -> str | None:
    for name in names:
        if name not in payload:
            return name
    return None

def _check_profile(payload: dict) -> dict | None:
    """AC-3's refusal gate; returns a refused envelope, or None when the profile is fine."""
    profile = payload.get("profile")
    if not profile:
        return _refused("payload missing profile")
    profile_path = Path(profile)
    try:
        is_file = profile_path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        return _refused(f"profile {profile} is not a regular file")
    owner_uid = payload.get("profile_owner_uid")
    if not isinstance(owner_uid, int) or isinstance(owner_uid, bool):
        return _refused("payload missing a valid profile_owner_uid")
    try:
        actual_owner = os.stat(profile_path).st_uid
    except OSError as exc:
        return _refused(f"could not stat profile {profile}: {exc}")
    if actual_owner != owner_uid:
        return _refused(
            f"profile {profile} is owned by uid {actual_owner}, not the trusted owner {owner_uid}"
        )
    return None

def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass

def _sandboxed_argv(payload: dict, argv: list) -> list[str]:
    return [SANDBOX_EXEC_PATH, "-f", str(payload["profile"]), *argv]

def _collect_child_output(
    proc: subprocess.Popen, deadline_s: float, stdout_cap: int, stderr_cap: int,
    envelope_cap: int, truncated_cap: int | None = None,
) -> dict:
    """Waits up to deadline_s, killing proc's group on timeout (AC-5), then returns the
    fitted envelope (AC-4). truncated_cap requests stdout_truncated_result for `run`,
    computed AFTER fitting against the cap fitting actually used -- fitting can shrink
    the nominal cap, and a line only truncated by that shrink must read true."""
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=deadline_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        stdout_bytes, stderr_bytes = proc.communicate()
        timed_out = True

    envelope = _deadline("", "") if timed_out else _ok(proc.returncode, "", "")
    envelope, final_stdout_cap, _ = _fit_envelope(
        envelope, stdout_bytes, stderr_bytes, stdout_cap, stderr_cap, envelope_cap
    )
    if not timed_out and truncated_cap is not None:
        envelope["stdout_truncated_result"] = _stdout_truncated_result(stdout_bytes, final_stdout_cap)
    return envelope

# --- `run`: the worker child (design §2.1's "worker" profile) -----------------

#: run's payload env allowlist (design §3.3): exactly these, where present.
RUN_PAYLOAD_ENV_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY", "SUBSTRATE_URL", "DISABLE_AUTOUPDATER",
)
#: Restored into child_env via `needs` -- decision D's third AC-5 exception.
RUN_CREDENTIAL_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "SUBSTRATE_API_KEY")

def run(payload: dict, w_path: Path) -> dict:
    """The `run` subcommand; its own body holds the one `subprocess.Popen` call, since
    test_spawn_env_fixture.py keys its third AC-5 exception on the enclosing `def`."""
    missing = _require(payload, "argv", "cwd", "env", "profile", "profile_owner_uid", "deadline_s")
    if missing is not None:
        return _refused(f"run payload missing {missing!r}")

    expected_cwd = w_path / "wtree" / "repo"
    cwd = Path(payload["cwd"])
    if cwd != expected_cwd:
        return _refused(f"run payload cwd {cwd} is not {expected_cwd}")

    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    payload_env = payload["env"]
    if not isinstance(payload_env, dict):
        return _refused("run payload env must be an object")

    base_env = {k: v for k, v in payload_env.items() if k in RUN_PAYLOAD_ENV_KEYS}
    env = process_env.child_env(environ=base_env, needs=RUN_CREDENTIAL_NAMES)

    tmp_dir = w_path / "wtree" / RUN_TMPDIR_NAME  # from W, never the payload's cwd
    home = tmp_dir / "home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _refused(f"could not create HOME {home}: {exc}")
    env = _finalize_child_env(env, home, tmp_dir)

    argv = _sandboxed_argv(payload, list(payload["argv"]))
    deadline_s = float(payload["deadline_s"])

    proc = subprocess.Popen(
        argv, env=env, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    return _collect_child_output(
        proc, deadline_s, RUN_STDOUT_CAP_BYTES, RUN_STDERR_CAP_BYTES, RUN_ENVELOPE_CAP_BYTES,
        truncated_cap=RUN_STDOUT_CAP_BYTES,
    )

# --- `exec`: declared verification (design §2.1's "verification" profile) -----

def _cmd_exec(payload: dict, w_path: Path) -> dict:
    """The `exec` subcommand, under the verification profile; its own `subprocess.Popen`
    call for the spawn-scan attribution reason `run`'s docstring explains."""
    missing = _require(payload, "argv", "cwd", "env", "profile", "profile_owner_uid", "deadline_s")
    if missing is not None:
        return _refused(f"exec payload missing {missing!r}")

    cwd = Path(payload["cwd"])
    tree = None
    for candidate in ("ptree", "vtree"):
        if cwd == w_path / candidate / "repo":
            tree = candidate
            break
    if tree is None:
        return _refused(f"exec payload cwd {cwd} is not <W>/ptree/repo or <W>/vtree/repo")

    payload_env = payload["env"]
    if not isinstance(payload_env, dict):
        return _refused("exec payload env must be an object")
    for name in payload_env:
        if process_env.is_credential_name(name):
            return _refused(f"exec payload env carries a credential-named key: {name}")

    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    env = process_env.child_env(environ=payload_env)

    tmp_dir = w_path / tree / VERIFY_TMPDIR_NAME  # from W + the validated tree, not cwd
    home = tmp_dir / "home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _refused(f"could not create HOME {home}: {exc}")
    env = _finalize_child_env(env, home, tmp_dir)

    argv = _sandboxed_argv(payload, list(payload["argv"]))
    deadline_s = float(payload["deadline_s"])

    proc = subprocess.Popen(
        argv, env=env, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    return _collect_child_output(
        proc, deadline_s, EXEC_STREAM_CAP_BYTES, EXEC_STREAM_CAP_BYTES, EXEC_ENVELOPE_CAP_BYTES,
    )

# --- `seed`/`vseed`: clone <tree>/repo from a bundle at base_ref (design §2.3) ------

VALID_TREES = ("wtree", "ptree", "vtree")


def _tree_repo_path(w_path: Path, tree: object) -> Path | None:
    if tree not in VALID_TREES:
        return None
    return w_path / tree / "repo"


def _seed_vseed_core(payload: dict, w_path: Path) -> dict:
    """The shared git-spawning body for both `seed` and `vseed` (SUBCOMMAND_FUNCTION
    names this one function for each key): every AC-12 confinement check and every git
    subprocess call lives here directly, so spawn-scan's attribution (and Invariant L's
    enclosing-function requirement) lands on one function regardless of which thin
    wrapper (`_cmd_seed`/`_cmd_vseed`) delegated to it. The seed/vseed-only AC-1
    separation checks run in those wrappers, before this function is ever called."""
    missing = _require(
        payload, "bundle", "base_ref", "tree", "remote_url", "profile", "profile_owner_uid",
        "deadline_s",
    )
    if missing is not None:
        return _refused(f"seed payload missing {missing!r}")
    repo = _tree_repo_path(w_path, payload["tree"])
    if repo is None:
        return _refused(f"seed payload tree {payload['tree']!r} is not one of {VALID_TREES}")
    if repo.exists():
        return _refused(f"seed refused: {repo} already exists")

    bundle_real, bundle_reason = _confine_bundle(payload["bundle"], w_path)
    if bundle_reason is not None:
        return _refused(bundle_reason)

    patch = payload.get("patch")
    patch_real: str | None = None
    if patch is not None:
        patch_real, patch_reason = _confine_patch(patch, w_path)
        if patch_reason is not None:
            return _refused(patch_reason)

    profile_reason = _confine_under_w_or_fixed_root(payload["profile"], w_path)
    if profile_reason is not None:
        return _refused(profile_reason)
    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    base_ref = str(payload["base_ref"])
    dash_reason = _base_ref_leading_dash_refusal(base_ref)
    if dash_reason is not None:
        return _refused(dash_reason)

    env = _git_env(payload.get("env"))
    deadline_s = float(payload["deadline_s"])

    try:
        ref_check = subprocess.run(
            [GIT_PATH, "check-ref-format", "--branch", base_ref],
            env=env, capture_output=True, timeout=deadline_s,
        )
    except subprocess.TimeoutExpired:
        return _refused("seed timed out validating base_ref")
    if ref_check.returncode != 0:
        return _refused(f"base_ref {base_ref!r} failed check-ref-format")

    remote_url = str(payload["remote_url"])
    ref = f"refs/remotes/origin/{base_ref}"
    steps = [
        [GIT_PATH, "clone", "--quiet", "--no-checkout", "--", bundle_real, str(repo)],
        # git 2.34 (the CI runner's /usr/bin/git) treats a bare "--end-of-options
        # <ref>" to `checkout` as a pathspec, giving "pathspec '<ref>' did not match
        # any file(s)" -- checkout's own branch-vs-pathspec disambiguation only
        # recognizes the ref as a branch when followed by "--" (outer-loop ruling:
        # the pre-spawn leading-dash refusal plus check-ref-format above is the real
        # guarantee here, not this placement).
        [GIT_PATH, "-C", str(repo), "checkout", "--quiet", base_ref, "--"],
        [GIT_PATH, "-C", str(repo), "remote", "set-url", "origin", "--", remote_url],
        [GIT_PATH, "-C", str(repo), "fetch", "--quiet", "--end-of-options", bundle_real, f"{ref}:{ref}"],
    ]
    if patch_real is not None:
        steps.append([GIT_PATH, "-C", str(repo), "apply", "--index", "--binary", "--", patch_real])
    for argv in steps:
        sandboxed = _sandboxed_argv(payload, argv)
        try:
            result = subprocess.run(sandboxed, env=env, capture_output=True, timeout=deadline_s)
        except subprocess.TimeoutExpired:
            return _refused(f"seed timed out running {argv!r}")
        if result.returncode != 0:
            return _refused_from_stderr(f"seed git command {argv!r} failed: ", result.stderr)
    return _ok(None, str(repo), "")


def _cmd_seed(payload: dict, w_path: Path) -> dict:
    """AC-1 SEPARATION: seed never applies a patch -- vseed does. Decided from the
    subcommand's own identity (this wrapper is only ever reached via SUBCOMMAND
    dispatch's `seed` key), never by reading payload content."""
    if "patch" in payload:
        return _refused("seed payload must not carry a patch; vseed applies patches")
    return _seed_vseed_core(payload, w_path)


def _cmd_vseed(payload: dict, w_path: Path) -> dict:
    """AC-1 SEPARATION: vseed takes tree ptree or vtree only; vtree requires a patch,
    ptree must not carry one. Every refusal here happens before `_seed_vseed_core` --
    and so before any spawn."""
    tree = payload.get("tree")
    if tree == "wtree":
        return _refused("vseed refused: tree must be ptree or vtree, not wtree")
    if tree == "vtree" and "patch" not in payload:
        return _refused("vseed of vtree requires a patch")
    if tree == "ptree" and "patch" in payload:
        return _refused("vseed of ptree must not carry a patch")
    return _seed_vseed_core(payload, w_path)


# --- `materialize`: the repo's own persona script, run with -I (design §7 E6) ------


def _cmd_materialize(payload: dict, w_path: Path) -> dict:
    missing = _require(payload, "tree", "profile", "profile_owner_uid", "deadline_s")
    if missing is not None:
        return _refused(f"materialize payload missing {missing!r}")
    repo = _tree_repo_path(w_path, payload["tree"])
    if repo is None or not repo.is_dir():
        return _refused(f"materialize refused: {repo} is not a directory")
    script = repo / "scripts" / "materialize_agents.py"
    profile_reason = _confine_under_w_or_fixed_root(payload["profile"], w_path)
    if profile_reason is not None:
        return _refused(profile_reason)
    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    payload_env = payload.get("env")
    env = process_env.child_env(environ=payload_env if isinstance(payload_env, dict) else {})
    tmp_dir = w_path / payload["tree"] / RUN_TMPDIR_NAME
    home = tmp_dir / "home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _refused(f"could not create HOME {home}: {exc}")
    env = _finalize_child_env(env, home, tmp_dir)

    argv = _sandboxed_argv(payload, [sys.executable, "-I", str(script), str(repo)])
    deadline_s = float(payload["deadline_s"])
    proc = subprocess.Popen(
        argv, env=env, cwd=str(repo),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    return _collect_child_output(
        proc, deadline_s, EXEC_STREAM_CAP_BYTES, EXEC_STREAM_CAP_BYTES, EXEC_ENVELOPE_CAP_BYTES,
    )


# --- `export`: the only path worker-written bytes leave by (design §5) -------------

_EXPORT_GIT_CONFIG = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
    "-c", "core.attributesFile=/dev/null",
)
#: .factory never travels in the patch, case-folded, AT ANY DEPTH (review item 7, PR
#: 1178 gate), on both `add` and `diff --cached` (AC-2): a committed `.factory/x`,
#: `.Factory/y`, `.factory/design.md` or a nested `sub/.factory/z` must all be absent
#: from the exported patch -- design.md travels separately below, capped. The `**/`
#: glob prefix matches zero or more path components (so it still covers the top-level
#: case), which a bare `.factory` pathspec does not extend into subdirectories.
_FACTORY_EXCLUDE_PATHSPECS = (
    ":(exclude,icase,glob)**/.factory", ":(exclude,icase,glob)**/.factory/**",
)

DESIGN_TEXT_CAP_BYTES = 64 * 1024
#: Equal to handoff.MAX_PATCH_BYTES (a test imports both and asserts the equality,
#: since this module does not import handoff -- Invariant L's stdlib-plus-process_env
#: only rule).
MAX_PATCH_BYTES = 16 * 1024 * 1024
#: base64.b64encode's exact output length for MAX_PATCH_BYTES input bytes (4 chars per
#: 3-byte group, rounded up, with padding) -- base64's own alphabet needs no JSON
#: escaping, so this term is exact rather than padded for escaping like the design-text
#: term below.
_MAX_PATCH_B64_BYTES = 4 * ((MAX_PATCH_BYTES + 2) // 3)
#: Sized for the base64 of MAX_PATCH_BYTES, plus DESIGN_TEXT_CAP_BYTES inflated for the
#: worst-case JSON string escape (\\uXXXX, 6 bytes per source byte), plus B2a's own
#: exec stream caps and slack as a safety margin for every other envelope field.
EXPORT_ENVELOPE_CAP_BYTES = (
    _MAX_PATCH_B64_BYTES + DESIGN_TEXT_CAP_BYTES * 6
    + EXEC_STREAM_CAP_BYTES * 2 + ENVELOPE_CAP_SLACK_BYTES
)


def _cmd_export(payload: dict, w_path: Path) -> dict:
    """export's own body holds every git subprocess call directly (the base_ref
    validation, then add, then diff), same spawn-scan-attribution reason as
    `_seed_vseed_core`'s docstring. AC-2: the patch and design.md travel only as the
    top-level `patch_b64`/`design_text` envelope fields, never inside `stdout`."""
    missing = _require(payload, "tree", "base_ref", "profile", "profile_owner_uid", "deadline_s")
    if missing is not None:
        return _refused(f"export payload missing {missing!r}")
    repo = _tree_repo_path(w_path, payload["tree"])
    if repo is None or not repo.is_dir():
        return _refused(f"export refused: {repo} is not a directory")

    profile_reason = _confine_under_w_or_fixed_root(payload["profile"], w_path)
    if profile_reason is not None:
        return _refused(profile_reason)
    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    base_ref = str(payload["base_ref"])
    dash_reason = _base_ref_leading_dash_refusal(base_ref)
    if dash_reason is not None:
        return _refused(dash_reason)

    env = _git_env(payload.get("env"))
    env["GIT_ATTR_NOSYSTEM"] = "1"
    deadline_s = float(payload["deadline_s"])

    try:
        ref_check = subprocess.run(
            [GIT_PATH, "check-ref-format", "--branch", base_ref],
            env=env, capture_output=True, timeout=deadline_s,
        )
    except subprocess.TimeoutExpired:
        return _refused("export timed out validating base_ref")
    if ref_check.returncode != 0:
        return _refused(f"export refused: base_ref {base_ref!r} failed check-ref-format")

    add_argv = _sandboxed_argv(
        payload,
        [
            GIT_PATH, "-C", str(repo), *_EXPORT_GIT_CONFIG, "add", "-A", "--", ".",
            *_FACTORY_EXCLUDE_PATHSPECS,
        ],
    )
    try:
        result = subprocess.run(add_argv, env=env, capture_output=True, timeout=deadline_s)
    except subprocess.TimeoutExpired:
        return _refused("export timed out staging changes")
    if result.returncode != 0:
        return _refused(f"export refused: git add failed: {result.stderr.decode('utf-8', 'replace')}")

    diff_argv = _sandboxed_argv(
        payload,
        [
            GIT_PATH, "-C", str(repo), *_EXPORT_GIT_CONFIG, "diff", "--cached", "--binary",
            "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
            "--end-of-options", base_ref, "--", *_FACTORY_EXCLUDE_PATHSPECS,
        ],
    )
    try:
        result = subprocess.run(diff_argv, env=env, capture_output=True, timeout=deadline_s)
    except subprocess.TimeoutExpired:
        return _refused("export timed out running diff")
    if result.returncode != 0:
        return _refused(f"export refused: git diff failed: {result.stderr.decode('utf-8', 'replace')}")

    patch_bytes = result.stdout
    if len(patch_bytes) > MAX_PATCH_BYTES:
        return _refused("export too large")
    patch_b64 = base64.b64encode(patch_bytes).decode("ascii")

    design_path = repo / ".factory" / "design.md"
    design_text = ""
    try:
        os.lstat(str(design_path))
    except OSError:
        design_exists = False
    else:
        design_exists = True
    if design_exists:
        # Review item 3 (PR 1178 gate): a directory or FIFO at this path used to pass
        # straight through as "absent" (is_file() is False for both, so the block
        # below was simply skipped). Open with O_NOFOLLOW (refuses a symlink outright)
        # and O_NONBLOCK (a FIFO reader never blocks waiting for a writer), then
        # require S_ISREG on the opened fd's own fstat before reading anything.
        try:
            design_fd = os.open(str(design_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            return _refused(f"export refused: could not open {design_path}: {exc}")
        try:
            design_fstat = os.fstat(design_fd)
            if not stat.S_ISREG(design_fstat.st_mode):
                return _refused(f"export refused: {design_path} is not a regular file")
            design_bytes = os.read(design_fd, DESIGN_TEXT_CAP_BYTES + 1)
        finally:
            os.close(design_fd)
        if len(design_bytes) > DESIGN_TEXT_CAP_BYTES:
            return _refused(f"export refused: {design_path} exceeds {DESIGN_TEXT_CAP_BYTES} bytes")
        try:
            design_text = design_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _refused(f"export refused: {design_path} is not valid UTF-8: {exc}")

    envelope = _ok(0, "", "")
    envelope["patch_b64"] = patch_b64
    envelope["design_text"] = design_text
    serialized = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    if len(serialized) > EXPORT_ENVELOPE_CAP_BYTES:
        return _refused("export too large")
    return envelope


# --- `venv`/`pip`: the pip profile (design §9.3) ------------------------------------


def _venv_tmp_env(payload: dict, tree_dir: Path) -> dict[str, str] | dict:
    """AC-4: drops every GIT_* key unconditionally, and every PIP_*/PYTHON* key not
    named in the payload's `pip_env` allowlist."""
    payload_env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    pip_allowed = set(payload.get("pip_env") or [])
    filtered = {}
    for name, value in payload_env.items():
        if name.startswith("GIT_"):
            continue
        if name.startswith(("PYTHON", "PIP_")) and name not in pip_allowed:
            continue
        filtered[name] = value
    env = process_env.child_env(environ=filtered)
    tmp_dir = tree_dir / VERIFY_TMPDIR_NAME
    home = tmp_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    return _finalize_child_env(env, home, tmp_dir)


def _cmd_venv(payload: dict, w_path: Path, kind: str) -> dict:
    """Implements both `venv` and `pip` (SUBCOMMAND_FUNCTION names this one function for
    each). AC-4 SEPARATION: `kind` is bound by `_HANDLERS` at module-definition time via
    `functools.partial` -- never read from the payload -- so `venv` is refused when the
    payload carries `args` and `pip` is refused when it does not, before any spawn. Its
    own body holds the one Popen call, same reason as `run`'s docstring."""
    if kind == "venv" and "args" in payload:
        return _refused("venv payload must not carry args; pip installs")
    if kind == "pip" and "args" not in payload:
        return _refused("pip payload requires args")

    missing = _require(payload, "tree", "profile", "profile_owner_uid", "deadline_s")
    if missing is not None:
        return _refused(f"{kind} payload missing {missing!r}")
    if payload["tree"] not in VALID_TREES:
        return _refused(f"{kind} payload tree {payload['tree']!r} is not one of {VALID_TREES}")
    tree_dir = w_path / payload["tree"]
    if not tree_dir.is_dir():
        return _refused(f"{kind} refused: {tree_dir} is not a directory")

    profile_reason = _confine_under_w_or_fixed_root(payload["profile"], w_path)
    if profile_reason is not None:
        return _refused(profile_reason)
    profile_refusal = _check_profile(payload)
    if profile_refusal is not None:
        return profile_refusal

    try:
        env = _venv_tmp_env(payload, tree_dir)
    except OSError as exc:
        return _refused(f"could not create HOME under {tree_dir}: {exc}")

    if kind == "pip":
        venv_python = tree_dir / ".verification-venv" / "bin" / "python"
        argv = _sandboxed_argv(
            payload, [str(venv_python), "-m", "pip", "install", *list(payload["args"])]
        )
    else:
        argv = _sandboxed_argv(payload, [sys.executable, "-I", "-m", "venv", ".verification-venv"])
    deadline_s = float(payload["deadline_s"])
    proc = subprocess.Popen(
        argv, env=env, cwd=str(tree_dir),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    return _collect_child_output(
        proc, deadline_s, EXEC_STREAM_CAP_BYTES, EXEC_STREAM_CAP_BYTES, EXEC_ENVELOPE_CAP_BYTES,
    )


# --- `purge`: clears uchg, restores u+w, removes only what this uid owns -----------

#: 0x00020000 on every platform stat.py defines it on; absent (not merely 0) is why
#: getattr(..., 0) below would be wrong -- a real but unsupported flag bit must never
#: match a file's st_flags by coincidence, so purge simply never offers it off-darwin.
UF_IMMUTABLE: int | None = getattr(stat, "UF_IMMUTABLE", None)
_CHFLAGS: Callable[[str, int], None] | None = getattr(os, "chflags", None)


def _purge_unlock_dir(path: Path, euid: int) -> None:
    """Pass 1 (AC-5, order amended): clears `uchg` and restores u+w on a directory this
    uid owns, best-effort, BEFORE pass 2 tries to remove its children -- unlinking or
    renaming an entry needs write+execute on its PARENT directory, not just on the
    entry itself, so a 0555 or uchg-flagged directory must be unlocked before, never
    after, its contents are walked for removal."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return
    if st.st_uid != euid or not stat.S_ISDIR(st.st_mode):
        return
    flags = getattr(st, "st_flags", 0)
    try:
        if _CHFLAGS is not None and UF_IMMUTABLE is not None and flags & UF_IMMUTABLE:
            _CHFLAGS(str(path), flags & ~UF_IMMUTABLE)
        os.chmod(str(path), stat.S_IMODE(st.st_mode) | stat.S_IWUSR)
    except OSError:
        pass


def _purge_clear_and_remove(path: Path, euid: int) -> bool:
    """True when `path` is gone (or was already gone) after this call. Only ever acts
    on an entry owned by `euid`; never follows a symlink (lstat, then unlink it as a
    plain entry rather than treating it as the directory it may point to)."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return True
    if st.st_uid != euid:
        return False
    if stat.S_ISLNK(st.st_mode):
        try:
            os.unlink(str(path))
        except OSError:
            return False
        return True
    flags = getattr(st, "st_flags", 0)
    try:
        if _CHFLAGS is not None and UF_IMMUTABLE is not None and flags & UF_IMMUTABLE:
            _CHFLAGS(str(path), flags & ~UF_IMMUTABLE)
        os.chmod(str(path), stat.S_IMODE(st.st_mode) | stat.S_IWUSR)
    except OSError:
        return False
    try:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(str(path))
        else:
            os.unlink(str(path))
    except OSError:
        return False
    return True


def _cmd_purge(payload: dict, w_path: Path) -> dict:
    euid = os.geteuid()
    keep_root = bool(payload.get("keep_root", False))
    timeout_s = float(payload.get("timeout_s", 60.0))
    deadline = time.monotonic() + timeout_s
    remaining: list[str] = []

    # Pass 1, top-down: unlock every owned directory (uchg cleared, u+w restored)
    # before pass 2 below ever tries to remove one of its children.
    for root, _dirs, _files in os.walk(str(w_path), topdown=True, followlinks=False):
        if time.monotonic() > deadline:
            break
        _purge_unlock_dir(Path(root), euid)

    # Pass 2, bottom-up: remove everything this uid owns.
    for root, dirs, files in os.walk(str(w_path), topdown=False, followlinks=False):
        root_path = Path(root)
        for name in (*files, *dirs):
            target = root_path / name
            if time.monotonic() > deadline:
                remaining.append(str(target))
                continue
            if not _purge_clear_and_remove(target, euid):
                remaining.append(str(target))

    if not keep_root:
        if time.monotonic() > deadline:
            remaining.append(str(w_path))
        elif not _purge_clear_and_remove(w_path, euid):
            remaining.append(str(w_path))

    if remaining:
        body = json.dumps({"remaining_count": len(remaining), "remaining": remaining[:10]})
        return _ok(None, body, "")
    return _ok(None, "purged", "")


# --- `probe`: measures runbook step 13 (a)-(i); never decides closure (design §10) --

#: A bare reference, never a Call -- invisible to the Invariant L ast scan and to
#: spawn_scan.py's walk, the same shape as tunnel_keeper.TunnelKeeper's injected Popen
#: default (spawn_scan.py's own docstring names that precedent). Tests inject a fake.
PROBE_RUNNER: Callable[..., subprocess.CompletedProcess] = subprocess.run


def _default_bootstrap_look_up(name: str) -> int:
    """The real ctypes binding; exercised only by the attended probe (design §10),
    never by a test -- tests inject PROBE_BOOTSTRAP_LOOK_UP."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        bootstrap_port = ctypes.c_uint32.in_dll(libc, "bootstrap_port")
        out_port = ctypes.c_uint32(0)
        return int(
            libc.bootstrap_look_up(bootstrap_port, name.encode("utf-8"), ctypes.byref(out_port))
        )
    except (OSError, AttributeError, ValueError):
        return -1


PROBE_BOOTSTRAP_LOOK_UP: Callable[[str], int] = _default_bootstrap_look_up


def _procargs2_mib(pid: int) -> list[int]:
    """sysctl's [CTL_KERN, KERN_PROCARGS2, pid] mib, factored out of
    _default_sysctl_procargs2 (review item 8, PR 1178 gate) so a pure test can assert
    the exact mib -- [1, 49, pid] -- without exercising the real ctypes/sysctl call."""
    return [1, 49, pid]


def _default_sysctl_procargs2(pid: int) -> dict:
    """The real ctypes sysctl([CTL_KERN, KERN_PROCARGS2, pid]) binding; same
    exercised-only-by-the-attended-probe note as _default_bootstrap_look_up above."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        mib_values = _procargs2_mib(pid)
        mib = (ctypes.c_int * len(mib_values))(*mib_values)
        size = ctypes.c_size_t(0)
        rc = libc.sysctl(mib, len(mib_values), None, ctypes.byref(size), None, 0)
        if rc != 0:
            return {"errno": ctypes.get_errno()}
        return {"ok": True, "size": size.value}
    except OSError as exc:
        return {"error": str(exc)}


PROBE_SYSCTL_PROCARGS2: Callable[[int], dict] = _default_sysctl_procargs2

#: Review item 4 (PR 1178 gate): a 25 MB probe envelope slipped past EXEC_ENVELOPE_CAP_BYTES
#: because no individual item's stream was ever capped. Each item's own tail is capped
#: to this constant before it goes into `observations`, so the final json.dumps of all
#: items together stays well inside EXEC_ENVELOPE_CAP_BYTES without needing the
#: envelope-level fit (below) to truncate -- which would otherwise break the JSON.
PROBE_ITEM_STREAM_CAP_BYTES = 16 * 1024


def _probe_run(argv: list[str], env: dict[str, str]) -> dict:
    try:
        result = PROBE_RUNNER(argv, env=env, capture_output=True, text=True, timeout=30)
        stdout_text = _cap_stream((result.stdout or "").encode("utf-8", "replace"), PROBE_ITEM_STREAM_CAP_BYTES)
        stderr_text = _cap_stream((result.stderr or "").encode("utf-8", "replace"), PROBE_ITEM_STREAM_CAP_BYTES)
        return {"argv": argv, "rc": result.returncode, "stdout": stdout_text, "stderr": stderr_text}
    except Exception as exc:  # the probe reports every item; it never dies mid-scan
        return {"argv": argv, "error": str(exc)}


def _probe_paths_eacces(paths: list[str]) -> dict:
    observations = {}
    for path in paths:
        try:
            os.listdir(path)
            observations[path] = {"errno": None}
        except OSError as exc:
            observations[path] = {"errno": exc.errno, "strerror": exc.strerror}
    return observations


def _cmd_probe(payload: dict, w_path: Path) -> dict:
    """AC-12: `worker_profile` and `bootstrap_plist`, when present, are confined to W
    or FIXED_TRUSTED_ROOT and `worker_profile` passes B2a's `_check_profile` -- checked
    before any of probe's own observations run."""
    worker_profile = payload.get("worker_profile")
    if worker_profile:
        profile_reason = _confine_under_w_or_fixed_root(worker_profile, w_path)
        if profile_reason is not None:
            return _refused(profile_reason)
        profile_refusal = _check_profile(
            {"profile": worker_profile, "profile_owner_uid": payload.get("worker_profile_owner_uid")}
        )
        if profile_refusal is not None:
            return profile_refusal

    bootstrap_plist = payload.get("bootstrap_plist")
    if bootstrap_plist:
        plist_reason = _confine_under_w_or_fixed_root(bootstrap_plist, w_path)
        if plist_reason is not None:
            return _refused(plist_reason)

    payload_env = payload.get("env")
    env = process_env.child_env(environ=payload_env if isinstance(payload_env, dict) else {})

    def _worker_profile_argv(argv: list[str]) -> list[str]:
        if not worker_profile:
            return argv
        return _sandboxed_argv({"profile": worker_profile}, argv)

    observations: dict[str, object] = {}

    observations["a"] = {
        "managername": _probe_run([LAUNCHCTL_PATH, "managername"], env),
        "windowserver_lookup": PROBE_BOOTSTRAP_LOOK_UP("com.apple.windowserver.active"),
    }
    observations["b"] = _probe_paths_eacces(list(payload.get("paths", [])))
    observations["c"] = PROBE_SYSCTL_PROCARGS2(int(payload.get("target_pid", os.getpid())))
    observations["d"] = _probe_run(
        [SECURITY_PATH, "find-generic-password", "-s", "Claude Code-credentials"], env
    )

    if payload.get("run_claude_probe"):
        version_payload = payload.get("claude_version_payload")
        observations["e_version"] = (
            run(dict(version_payload), w_path) if isinstance(version_payload, dict) else None
        )
        claude_payload = payload.get("claude_run_payload")
        observations["e"] = (
            run(dict(claude_payload), w_path) if isinstance(claude_payload, dict) else None
        )
    else:
        observations["e"] = None

    observations["f"] = {
        # Review item 9 (PR 1178 gate): probe's `git --version` must go through the
        # same AC-11 hardening every other launcher git call does, not the plain
        # process_env-only `env` the other (non-git) probe items use.
        "git_version": _probe_run([GIT_PATH, "--version"], _git_env(payload_env)),
        "python_ssl": _probe_run([sys.executable, "-I", "-c", "import ssl"], env),
    }

    label = str(payload.get("submit_label", "com.gastown.factory-probe"))
    observations["g"] = {
        "submit": _probe_run(
            _worker_profile_argv([LAUNCHCTL_PATH, "submit", "-l", label, "--", "/usr/bin/true"]), env
        ),
        "remove": _probe_run(_worker_profile_argv([LAUNCHCTL_PATH, "remove", label]), env),
    }

    plist = str(payload.get("bootstrap_plist", ""))
    domain = str(payload.get("bootstrap_domain", "user/850"))
    observations["h"] = {
        "bootstrap": _probe_run(_worker_profile_argv([LAUNCHCTL_PATH, "bootstrap", domain, plist]), env),
        "bootout": _probe_run(_worker_profile_argv([LAUNCHCTL_PATH, "bootout", domain, plist]), env),
    }

    observations["i"] = {
        name: PROBE_BOOTSTRAP_LOOK_UP(name) for name in list(payload.get("mach_names", []))
    }

    # Review item 4 (PR 1178 gate): fit the final envelope at the exec cap too, same as
    # every other B2b envelope. Each item's own stream is already tail-capped above, so
    # in practice this never needs to truncate further (which would break the JSON) --
    # it is a defensive bound on the aggregate, not the primary cap.
    stdout_bytes = json.dumps(observations).encode("utf-8")
    envelope = {
        "status": "ok", "child_rc": None, "stdout": "", "stderr": "",
        "stdout_truncated_result": False,
    }
    envelope, _, _ = _fit_envelope(
        envelope, stdout_bytes, b"", len(stdout_bytes), 0, EXEC_ENVELOPE_CAP_BYTES,
    )
    return envelope


# --- `selfcheck`: /tmp/claude-<uid>, created or verified, never touched if foreign --

SELFCHECK_DIR_MODE = 0o700

def _cmd_selfcheck(payload: dict, w_path: Path) -> dict:
    tmp_root = Path(payload.get("tmp_root", "/tmp"))
    euid = os.geteuid()
    path = tmp_root / f"claude-{euid}"
    try:
        st = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=SELFCHECK_DIR_MODE)
            os.chmod(path, SELFCHECK_DIR_MODE)
        except OSError as exc:
            return _refused(f"could not create {path}: {exc}")
        return _ok(None, str(path), "")
    except OSError as exc:
        return _refused(f"could not stat {path}: {exc}")

    if stat.S_ISLNK(st.st_mode):
        return _refused(f"{path} exists and is a symlink")
    if st.st_uid != euid:
        return _refused(f"{path} exists and is owned by uid {st.st_uid}, not {euid}")
    if stat.S_IMODE(st.st_mode) != SELFCHECK_DIR_MODE:
        return _refused(f"{path} exists with mode {oct(stat.S_IMODE(st.st_mode))}, not 0700")
    return _ok(None, str(path), "")

# --- `reap`: kill(-1, SIGKILL), guarded by three filesystem/process facts -----

#: Live indirections (looked up by attribute at call time): a test that monkeypatches
#: the real os.kill/os.stat directly must still observe it.
def _os_kill(pid: int, sig: int) -> None:
    os.kill(pid, sig)

def _os_stat_uid(path: str) -> int:
    return os.stat(path).st_uid

REAP_KILL: Callable[[int, int], None] = _os_kill
REAP_STAT_UID: Callable[[str], int] = _os_stat_uid

REAP_MIN_UID = 500  # design §2.1: 850 and friends are never below this

def _cmd_reap(payload: dict, w_path: Path) -> dict:
    euid = os.geteuid()
    worker_uid = payload.get("worker_uid")
    if not isinstance(worker_uid, int) or isinstance(worker_uid, bool) or worker_uid != euid:
        return _refused(
            f"reap refused: payload worker_uid {worker_uid!r} does not match effective uid {euid}"
        )
    if euid < REAP_MIN_UID:
        return _refused(f"reap refused: effective uid {euid} is below {REAP_MIN_UID}")
    try:
        owner_uid = REAP_STAT_UID(str(w_path))
    except OSError as exc:
        return _refused(f"reap refused: could not stat {w_path}: {exc}")
    if owner_uid == euid:
        return _refused(f"reap refused: {w_path} is owned by the effective uid {euid}")
    REAP_KILL(-1, signal.SIGKILL)
    return _ok(None, "reaped", "")

# --- Dispatch ------------------------------------------------------------------

#: Subcommand -> handler. Keys must equal SUBCOMMAND_PROFILE's keys exactly.
#: AC-4/AC-1 SEPARATION: venv/pip and seed/vseed are each selected here, at
#: module-definition time -- never from payload content. `functools.partial` binds
#: `kind` for the shared `_cmd_venv`; `seed` and `vseed` are distinct wrapper functions
#: over the shared `_seed_vseed_core` (see their own docstrings).
_HANDLERS: dict[str, Callable[[dict, Path], dict]] = {
    "selfcheck": _cmd_selfcheck,
    "reap": _cmd_reap,
    "run": run,
    "exec": _cmd_exec,
    "seed": _cmd_seed,
    "vseed": _cmd_vseed,
    "materialize": _cmd_materialize,
    "export": _cmd_export,
    "venv": functools.partial(_cmd_venv, kind="venv"),
    "pip": functools.partial(_cmd_venv, kind="pip"),
    "purge": _cmd_purge,
    "probe": _cmd_probe,
}

#: Subcommand -> its implementing function's name, for the Invariant L ast scan.
#: `seed`/`vseed` both name `_seed_vseed_core`, where every git spawn for either lives;
#: `venv`/`pip` both name `_cmd_venv` for the same reason -- see test_worker_launcher.py's
#: `_invariant_l_violations`, which only needs this map's VALUES to resolve to a
#: non-UNSANDBOXED category, a property both pairs share regardless of which key's
#: value wins the resulting dict's last-write.
SUBCOMMAND_FUNCTION: dict[str, str] = {
    "selfcheck": "_cmd_selfcheck",
    "reap": "_cmd_reap",
    "run": "run",
    "exec": "_cmd_exec",
    "seed": "_seed_vseed_core",
    "vseed": "_seed_vseed_core",
    "materialize": "_cmd_materialize",
    "export": "_cmd_export",
    "venv": "_cmd_venv",
    "pip": "_cmd_venv",
    "purge": "_cmd_purge",
    "probe": "_cmd_probe",
}

def _read_payload(stdin) -> tuple[dict | None, str | None]:
    raw = stdin.read()
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"malformed payload: {exc}"
    if not isinstance(payload, dict):
        return None, "payload must be a JSON object"
    return payload, None

def _dispatch(argv: list[str], stdin) -> dict:
    if len(argv) != 2:
        return _refused(f"usage: <subcommand> <W>, got argv={argv!r}")
    subcommand, w_arg = argv
    if subcommand not in SUBCOMMAND_PROFILE:
        return _refused(f"unknown subcommand: {subcommand!r}")
    payload, error = _read_payload(stdin)
    if error is not None:
        return _refused(error)
    handler = _HANDLERS[subcommand]
    try:
        return handler(payload, Path(w_arg))
    except Exception as exc:  # the envelope, never a crash, carries every outcome
        return _refused(f"{subcommand} failed: {exc}")

def main(argv: list[str], stdin, stdout) -> int:
    """`<subcommand> <W>` on argv, one JSON payload from stdin, one JSON envelope to
    stdout. Exit code is always 0 (design §3.5): only the envelope carries the outcome."""
    envelope = _dispatch(argv, stdin)
    # ensure_ascii=False matches _fit_envelope's own size accounting (AC-4).
    stdout.write(json.dumps(envelope, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], sys.stdin.buffer, sys.stdout))
