"""Amendment 30 PR-4: worker binaries derive from dispatchable registry entries."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch
from dispatch import WorkerEntry, required_worker_executables


def _registry(monkeypatch, **entries):
    monkeypatch.setattr(dispatch, "WORKER_REGISTRY", entries)


def test_base_tooling_always_required(monkeypatch):
    _registry(monkeypatch)
    assert required_worker_executables() == ("gh", "git", "sandbox-exec", "temporal")


def test_dispatchable_worker_binary_is_required(monkeypatch):
    _registry(monkeypatch, codex=WorkerEntry(
        argv=("codex", "exec"), quarantined=False, allowed_lanes=("code-health",)))
    assert "codex" in required_worker_executables()


def test_retired_worker_binary_is_not_required(monkeypatch):
    _registry(monkeypatch, codex=WorkerEntry(
        argv=("codex", "exec"), quarantined=False, allowed_lanes=("code-health",),
        retired=True, retirement_reason="Amendment 30"))
    assert "codex" not in required_worker_executables()


def test_quarantined_worker_binary_is_not_required(monkeypatch):
    _registry(monkeypatch, agy=WorkerEntry(
        argv=("agy",), quarantined=True, allowed_lanes=("code-health",),
        quarantine_reason="containment escape"))
    assert "agy" not in required_worker_executables()


def test_live_registry_requires_claude_only():
    # antigravity is quarantined: claude is the only dispatchable worker, so
    # it is the only extra binary demanded.
    assert required_worker_executables() == (
        "gh", "git", "sandbox-exec", "temporal", "claude",
    )
