#!/usr/bin/env python3
"""Load the worker/watchdog's credential env file after exec, not before.

Why this exists (dev.finding a0166920): a same-uid process can read another
process's EXEC-TIME environment through sysctl KERN_PROCARGS2 (what `ps -E`
reads on darwin) or /proc/<pid>/environ (linux). Rendering the worker and
keeper-watchdog plists as `set -a; . <env file>; set +a; exec ...` put every
name the env file declares -- SUBSTRATE_API_KEY, the write key, included --
into the exec-time strings those mechanisms read. Values assigned into
`os.environ` AFTER exec reach libc's environ through putenv, but do not
reach the exec-time argv/environ block those two mechanisms expose. This
module's launcher (see `main`, below) is what makes "after exec" true: it
runs the target script with `runpy`, having already loaded the env file
into `os.environ` itself, so the target script never has to.

Why this module imports only the standard library: `config.Config`'s class
body (config.py:99-104) and `dispatch.FAILURE_PATCH_DIR` (dispatch.py:218-220)
read configuration at IMPORT time, not at use time, and `worker_checkout`
(imported by `launchd_agent`) imports `dispatch`. If anything imports
`config` or `dispatch` before the env file's values are in `os.environ`, the
worker silently registers against the default Temporal namespace ('dev')
instead of the one the env file names. A loader that itself imported either
module, directly or transitively, could trip that trap before it ever ran;
importing nothing but the standard library is what rules that out.

Why values are loaded into `os.environ` rather than a separate credential
store: every in-process reader of these values today -- substrate.py, the
activities that build a substrate client, cluster_health.poster_from_env,
worker.temporal_tunnel_alerter_from_env -- calls `os.environ["NAME"]` or
`os.environ.get("NAME")` with no argument. Moving the values elsewhere would
require changing every one of those readers (and their tests, which set the
key with monkeypatch.setenv) in the same change; that is out of scope for
this seam-only bead.

What this module does NOT close on its own: a child process spawned with no
explicit `env=` still inherits `os.environ` wholesale, credentials included,
into ITS OWN exec-time environment. `child_env()` below is the builder every
spawn site should route through instead; `tests/test_spawn_inventory.py`
counts the call sites that do not yet do that, and SEC-a0166920-2 is the
migration that drives that count to zero. A bead that adds a new spawn call
must pass `env=process_env.child_env(...)` and must never raise that count.
"""

from __future__ import annotations

import os
import re
import runpy
import shlex
import sys
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping

CREDENTIAL_NAMES = (
    "SUBSTRATE_API_KEY",
    "SUBSTRATE_READ_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "DISCORD_WEBHOOK_URL",
)

# Kept byte-for-byte equal to dispatch.SENSITIVE_ENV_NAME_PATTERN (dispatch.py:1778);
# tests/test_process_env.py asserts the equality directly. This module cannot import
# dispatch (see the module docstring's stdlib-only rationale), so the pattern is
# duplicated here rather than shared; SEC-a0166920-2 collapses the two into one.
SENSITIVE_NAME_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


def is_credential_name(name: str) -> bool:
    """True when `name` is a declared credential name or matches the sensitive pattern."""
    return name in CREDENTIAL_NAMES or SENSITIVE_NAME_PATTERN.search(name) is not None


class ProcessEnvError(RuntimeError):
    """The env file could not be parsed the way `sh` sourcing would read it."""


# Any of these appearing in a raw assignment line means shlex and `sh` could read the
# line differently -- comments, command separators/redirection, quoting or expansion.
_REFUSED_LITERAL_CHARS = ";&|<>()\\'\"`$"


def _raw_name_and_value(line: str) -> tuple[str, str]:
    candidate = line
    if candidate.startswith("export ") or candidate.startswith("export\t"):
        candidate = candidate[len("export") :].lstrip()
    name, _sep, value = candidate.partition("=")
    return name.strip(), value


def _unsafe_line_reason(line: str) -> str | None:
    """Why `line` is refused, or None if it is safe -- checked on the RAW line.

    Checking the raw line (not the shlex-parsed value) matters: shlex has already
    silently truncated a mid-word `#` by the time a parsed value would exist.
    """
    if "#" in line:
        return "a '#' that is not at the start of the line"
    for ch in _REFUSED_LITERAL_CHARS:
        if ch in line:
            return f"the character {ch!r}"
    _name, value = _raw_name_and_value(line)
    if value.startswith("~") or ":~" in value:
        return "a value starting with '~' or containing ':~'"
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse the shell-compatible KEY=value subset used by the worker env file.

    Refuses any assignment line that `sh` sourcing could read differently from this
    parse (see `_unsafe_line_reason`) before it ever reaches shlex, and raises
    `ProcessEnvError` naming the file, line number and variable name -- never the
    value -- for both that case and every pre-existing parse failure.
    """
    values: dict[str, str] = {}
    if not path.exists():
        raise ProcessEnvError(f"Env file does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProcessEnvError(f"Env file {path} is not valid UTF-8: {exc}") from exc
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        reason = _unsafe_line_reason(line)
        if reason is not None:
            name, _value = _raw_name_and_value(line)
            raise ProcessEnvError(
                f"Could not parse {path}:{lineno}: variable {name!r} uses {reason}, "
                "which a shell could read differently from a plain parse"
            )
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise ProcessEnvError(f"Could not parse {path}:{lineno}: {exc}") from exc
        if not parts:
            continue
        if parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            raise ProcessEnvError(
                f"Could not parse {path}:{lineno}: expected KEY=value or export KEY=value"
            )
        name, value = parts[0].split("=", 1)
        if not name:
            raise ProcessEnvError(f"Could not parse {path}:{lineno}: empty variable name")
        values[name] = value
    return values


def load_env_file(path: Path, environ: MutableMapping[str, str]) -> list[str]:
    """Parse `path` and set every name it declares into `environ`, like `set -a; .` does.

    Overrides a value already present in `environ`, and returns the list of NAMES set
    (never values).
    """
    values = parse_env_file(path)
    for name, value in values.items():
        environ[name] = value
    return list(values.keys())


def _carries_credential(value: str, credential_values: list[str]) -> bool:
    stripped = value.strip()
    for cred in credential_values:
        if stripped == cred:
            return True
        if len(cred) >= 16 and cred in value:
            return True
    return False


def child_env(
    needs: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment a spawned child should get: no credential in, by name or value.

    Starts from a copy of `environ` (os.environ when None). Drops every entry whose
    NAME is a credential name and every entry whose VALUE carries a credential value
    (equals one, stripped, or contains one of length >= 16 as a substring). Then adds
    back, for each name in `needs` that is present in `environ`, that name and its
    value. Never mutates `environ`; a needed name absent from `environ` is simply
    absent from the result.
    """
    source = dict(os.environ if environ is None else environ)
    credential_values = [
        value.strip()
        for name, value in source.items()
        if is_credential_name(name) and value.strip()
    ]
    result: dict[str, str] = {}
    for name, value in source.items():
        if is_credential_name(name):
            continue
        if _carries_credential(value, credential_values):
            continue
        result[name] = value
    for name in needs:
        if name in source:
            result[name] = source[name]
    return result


#: Set by `main()` on the IMPORTABLE copy of this module (see the module docstring's
#: "second-copy" note: `python process_env.py ...` runs this file as `__main__`, a
#: distinct module object from `import process_env`). Nothing else can observe state
#: recorded only on the __main__ copy, so the launcher records it here instead.
LOADED_ENV_NAMES: list[str] = []


class _LauncherUsageError(RuntimeError):
    pass


def _parse_launcher_args(argv: list[str]) -> tuple[Path, str, list[str]]:
    if len(argv) < 3 or argv[0] != "--env-file":
        raise _LauncherUsageError("usage: process_env.py --env-file PATH SCRIPT [ARGS...]")
    return Path(argv[1]), argv[2], list(argv[3:])


def main(argv: list[str] | None = None) -> int:
    """`process_env.py --env-file PATH SCRIPT [ARGS...]` -- load, then run SCRIPT.

    Loads PATH into os.environ (through the importable module, not this __main__
    copy), then runs SCRIPT with runpy so SCRIPT never has to load its own
    credentials and so nothing imports config/dispatch before the load happens.
    SystemExit from SCRIPT propagates unchanged.
    """
    argv = sys.argv[1:] if argv is None else argv
    try:
        env_file, script, script_args = _parse_launcher_args(argv)
    except _LauncherUsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import process_env  # the importable module, distinct from this __main__ run

    try:
        names = process_env.load_env_file(env_file, os.environ)
    except process_env.ProcessEnvError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Could not read env file {env_file}: {exc}", file=sys.stderr)
        return 2
    process_env.LOADED_ENV_NAMES = names

    script_path = Path(script)
    sys.argv = [str(script_path), *script_args]
    sys.path[0] = str(script_path.resolve().parent)
    runpy.run_path(str(script_path), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
