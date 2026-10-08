#!/usr/bin/env python3
"""Run one release-gate verification command under the read-only OS boundary.

OPS-61 (Amendment 37 carried-forward clause 1): the gate runs read-only, and
containment or tool denial is the boundary, never a flag or a promise in
prose. This is the concrete mechanism - the gate agent runs its declared
read-only verification (pytest, ruff, `git log`/`diff`/`show`, ...) through
this script instead of invoking them directly, so a write against the
reviewed checkout, a `git commit`/`push`, a `gh` call, or any other network
access fails at the OS layer regardless of what the gate's charter prompt
asks of it. See apps/factory-dispatcher/containment.py's
``prepare_gate_containment``/``GATE_PROFILE_TEMPLATE`` for the profile
itself, and apps/factory-dispatcher/tests/test_gate_containment.py for the
probes that prove each denial (and the control that proves the boundary is
enforcing, not decorative).

Usage:
    python3 scripts/gate-verify.py <checkout> -- <command> [args...]

Exits with the wrapped command's own exit code. A denial (a write, a push, a
network call) surfaces as that command's own nonzero exit plus sandbox-exec's
stderr - this script does not translate or hide it, because a gate that
could not run a declared check must record that, not silently drop it
(carried-forward clause 3).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_DISPATCHER_DIR = str(REPO_ROOT / "apps" / "factory-dispatcher")
if _DISPATCHER_DIR not in sys.path:
    sys.path.insert(0, _DISPATCHER_DIR)

import containment  # noqa: E402


def build_argv(checkout: Path, command: tuple[str, ...]) -> tuple[tuple[str, ...], Path, dict[str, str]]:
    """The wrapped argv, the scratch dir, and the env the command should run under."""
    profile, scratch = containment.prepare_gate_containment(checkout)
    wrapped = containment.contained_argv(command, profile)
    env = dict(os.environ, TMPDIR=str(scratch))
    return wrapped, scratch, env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="the reviewed checkout to run the command against")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="'--' followed by the read-only verification command to run",
    )
    args = parser.parse_args(argv)

    # argparse consumes the first literal "--" itself as the positional
    # separator before REMAINDER collects, so the command arrives here either
    # with or without its leading "--" depending on where argparse found it.
    # Accept both; demanding the "--" argparse already ate rejected every
    # correctly-formed invocation (third preserved-patch failure on this bead).
    command = tuple(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("pass the verification command after a literal '--'")

    checkout = args.checkout
    if not checkout.is_dir():
        parser.error(f"checkout is not an existing directory: {checkout}")

    if sys.platform != "darwin":
        parser.error("gate containment is sandbox-exec, macOS-only; refusing to run unwrapped")

    wrapped, _scratch, env = build_argv(checkout, command)
    proc = subprocess.run(wrapped, cwd=str(checkout), env=env)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
