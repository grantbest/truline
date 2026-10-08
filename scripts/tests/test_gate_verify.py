"""OPS-61: scripts/gate-verify.py is the entry point a release-gate dispatch
uses to run its declared read-only verification under the containment
profile instead of trusting a charter's prose. The OS-level denials
themselves are probed in
apps/factory-dispatcher/tests/test_gate_containment.py; this file covers the
script's own argument handling and its wiring into that profile.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load_gate_verify():
    spec = importlib.util.spec_from_file_location(
        "gate_verify",
        REPO / "scripts" / "gate-verify.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _require_sandbox_apply():
    """Skip OS-level probes where this process cannot itself apply a
    sandbox profile - e.g. a dev.task worker developing this script is
    dispatched inside the dispatcher's own deny-write profile already, and
    a second sandbox_apply from inside one fails regardless of the inner
    profile's content (see apps/factory-dispatcher/tests/test_gate_containment.py's
    ``_sandbox_apply_available`` for the full precedent - the retired
    "codex" WorkerEntry hit the identical nesting wall)."""
    probe = subprocess.run(
        ["sandbox-exec", "-f", "/dev/stdin", "/usr/bin/true"],
        input="(version 1)\n(allow default)\n",
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            "sandbox_apply unavailable to this process (likely already "
            f"nested inside another sandbox-exec profile): {(probe.stderr or '').strip()}"
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_accepts_command_without_separator(tmp_path):
    # argparse consumes the first literal "--" itself before REMAINDER
    # collects, so main() cannot distinguish "-- git log" from "git log" and
    # must accept both -- demanding the separator argparse already ate
    # rejected every correctly-formed invocation (this bead's third
    # preserved-patch failure). The command still runs contained; here it
    # exits nonzero because tmp_path is not a git repository, which is the
    # command's own honest result, not a parser refusal.
    gate_verify = _load_gate_verify()
    code = gate_verify.main([str(tmp_path), "git", "log"])
    assert code != 0


def test_rejects_empty_command_after_separator(tmp_path):
    gate_verify = _load_gate_verify()
    with pytest.raises(SystemExit):
        gate_verify.main([str(tmp_path), "--"])


def test_rejects_missing_checkout(tmp_path):
    gate_verify = _load_gate_verify()
    with pytest.raises(SystemExit):
        gate_verify.main([str(tmp_path / "nope"), "--", "git", "log"])


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_build_argv_wraps_command_and_denies_writes_outside_scratch(tmp_path):
    _require_sandbox_apply()
    gate_verify = _load_gate_verify()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    wrapped, scratch, env = gate_verify.build_argv(checkout, ("/usr/bin/touch", str(checkout / "x.txt")))
    assert wrapped[0] == "sandbox-exec"
    assert env["TMPDIR"] == str(scratch)
    denied = subprocess.run(wrapped, capture_output=True)
    assert denied.returncode != 0
    assert not (checkout / "x.txt").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_main_runs_read_only_command_and_returns_its_exit_code(tmp_path):
    _require_sandbox_apply()
    gate_verify = _load_gate_verify()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "gate@example.com"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "gate"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=checkout, check=True)
    code = gate_verify.main([str(checkout), "--", "/usr/bin/git", "log", "--oneline"])
    assert code == 0
