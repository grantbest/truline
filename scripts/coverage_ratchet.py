#!/usr/bin/env python3
"""Run one suite's committed coverage-baseline command, refusing anything else.

S56-1 committed a baseline file per suite (apps/factory-dispatcher/,
apps/mcp-hub/ and scripts/coverage-baseline.json) whose `command` carries
--cov, --cov-precision=2 and --cov-fail-under. The nightly coverage-ratchet
workflow runs those commands. A committed file is still a place where a
change could switch the ratchet off or run something else, so this script
accepts exactly one shape and executes it WITHOUT a shell:

    [cd <relative dir> &&] (pytest | python -m pytest) <args...>

with exactly one --cov=<path>, exactly one --cov-precision=2, and exactly one
--cov-fail-under whose value equals the file's fail_under_pct numerically.
No other shell operator is allowed anywhere, and no argument may carry a
newline. Every argument is on an allow-list: relative test paths that exist
inside the repo, -q, --deselect <id>, and the three coverage flags; a cd
target must resolve inside the repo. The command is split with shlex and run with subprocess.run(argv,
cwd=...), so `&&`, `;`, `|`, `&` and redirections can never take effect.

Usage: scripts/coverage_ratchet.py <baseline.json> [--check-only]
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "2>", "2>&1", "(", ")"})
SHELL_CHARS = frozenset(";|&<>`$\n\r")


class RefusedCommand(ValueError):
    """The baseline command is not the one shape this script runs."""


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _check_allowed_args(args: list[str], cwd: Path, repo_root: Path) -> None:
    """Allow-list: relative test paths inside the repo, -q, --deselect <id>,
    and the three coverage flags. Anything else (--no-cov, --cov-reset,
    -o/--override-ini, -p, -c, --cov-config, --co, --help, ...) could change
    or skip what is measured, so it is refused."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-q":
            i += 1
            continue
        if a == "--deselect":
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                raise RefusedCommand("--deselect needs a test id")
            node = args[i + 1].split("::", 1)[0]
            if node.startswith("/") or not _inside(cwd / node, repo_root):
                raise RefusedCommand(f"--deselect target is outside the repo: {args[i + 1]!r}")
            i += 2
            continue
        if a.startswith(("--cov=", "--cov-precision=", "--cov-fail-under=")):
            if a.startswith("--cov="):
                target = a.split("=", 1)[1]
                if not target or target.startswith("/") or not _inside(cwd / target, repo_root):
                    raise RefusedCommand(f"--cov source must be a path inside the repo: {a!r}")
            i += 1
            continue
        if a.startswith("-"):
            raise RefusedCommand(f"argument not on the allow-list: {a!r}")
        path = a.split("::", 1)[0]
        if path.startswith("/") or not (cwd / path).exists() or not _inside(cwd / path, repo_root):
            raise RefusedCommand(f"test path must exist inside the repo: {a!r}")
        i += 1


def parse(command: str, fail_under_pct: object, repo_root: Path) -> tuple[Path, list[str]]:
    """Return (cwd, argv) for a valid baseline command, or raise RefusedCommand."""
    if not isinstance(command, str) or not command.strip():
        raise RefusedCommand("command must be a non-empty string")
    if "\n" in command or "\r" in command:
        raise RefusedCommand("command must be a single line")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise RefusedCommand(f"command does not tokenize: {exc}") from exc

    cwd = repo_root
    if tokens[:1] == ["cd"]:
        if len(tokens) < 3 or tokens[2] != "&&":
            raise RefusedCommand("a leading cd must be exactly `cd <dir> &&`")
        target = tokens[1]
        if target.startswith("/") or ".." in Path(target).parts:
            raise RefusedCommand(f"cd target must be a relative path inside the repo: {target!r}")
        cwd = (repo_root / target).resolve()
        if not cwd.is_dir():
            raise RefusedCommand(f"cd target does not exist: {target!r}")
        if not cwd.is_relative_to(repo_root.resolve()):
            raise RefusedCommand(f"cd target resolves outside the repo: {target!r}")
        tokens = tokens[3:]

    for token in tokens:
        if token in SHELL_OPERATORS or any(ch in token for ch in SHELL_CHARS):
            raise RefusedCommand(f"shell operator or metacharacter in argument: {token!r}")

    if tokens[:1] == ["pytest"]:
        argv = [sys.executable, "-m", "pytest", *tokens[1:]]
    elif tokens[:3] == ["python", "-m", "pytest"]:
        argv = [sys.executable, "-m", "pytest", *tokens[3:]]
    else:
        raise RefusedCommand("command must run `pytest` or `python -m pytest`")

    args = argv[3:]
    _check_allowed_args(args, cwd, repo_root)
    cov = [a for a in args if a == "--cov" or a.startswith("--cov=")]
    precision = [a for a in args if a == "--cov-precision" or a.startswith("--cov-precision=")]
    fail_under = [a for a in args if a == "--cov-fail-under" or a.startswith("--cov-fail-under=")]
    if len(cov) != 1 or cov[0] == "--cov":
        raise RefusedCommand("command must carry exactly one --cov=<path>")
    if precision != ["--cov-precision=2"]:
        raise RefusedCommand("command must carry exactly one --cov-precision=2")
    if len(fail_under) != 1 or fail_under[0] == "--cov-fail-under":
        raise RefusedCommand("command must carry exactly one --cov-fail-under=<value>")
    try:
        declared = Decimal(str(fail_under_pct))
        given = Decimal(fail_under[0].split("=", 1)[1])
    except InvalidOperation as exc:
        raise RefusedCommand(f"fail-under value is not a number: {exc}") from exc
    if declared != given:
        raise RefusedCommand(f"--cov-fail-under={given} disagrees with fail_under_pct={declared}")
    return cwd, argv


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--check-only"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    baseline = Path(argv[1])
    repo_root = Path.cwd()
    doc = json.loads(baseline.read_text())
    try:
        cwd, run_argv = parse(doc.get("command"), doc.get("fail_under_pct"), repo_root)
    except RefusedCommand as exc:
        print(f"refused {baseline}: {exc}", file=sys.stderr)
        return 3
    print(f"cwd={cwd.relative_to(repo_root) if cwd != repo_root else '.'} argv={run_argv}")
    if len(argv) == 3:
        return 0
    return subprocess.run(run_argv, cwd=cwd).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
