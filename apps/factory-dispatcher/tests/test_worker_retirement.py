"""Amendment 30 PR-3: retirement is a third worker state, distinct from
quarantine. A hint naming a retired worker falls back to the default worker
loudly (PRIN-008) rather than refusing, crashing, or silently ignoring the
hint — retirement of the default worker itself still refuses, since there is
no lower rung to fall back to."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import dispatch
from dispatch import DispatchError, WorkerEntry, select_worker


def _task(hint):
    return {"content": {"worker_hint": hint, "lane": "code-health"}}


def _with_registry(monkeypatch, **entries):
    monkeypatch.setattr(dispatch, "WORKER_REGISTRY", entries)
    monkeypatch.setattr(dispatch, "DEFAULT_WORKER", "claude")


def test_hinted_retired_worker_falls_back_to_default_with_note(monkeypatch):
    _with_registry(
        monkeypatch,
        oldtool=WorkerEntry(
            argv=("oldtool",), quarantined=False, allowed_lanes=("code-health",),
            retired=True, retirement_reason="Amendment 30",
        ),
        claude=WorkerEntry(
            argv=("claude",), quarantined=False, allowed_lanes=("code-health",),
        ),
    )
    selection = select_worker(_task("oldtool"))

    assert selection.name == "claude"
    assert selection.argv == ("claude",)
    assert selection.fallback_note is not None
    assert "oldtool" in selection.fallback_note
    assert "retired" in selection.fallback_note
    assert "Amendment 30" in selection.fallback_note
    assert "claude" in selection.fallback_note


def test_default_worker_selection_carries_no_fallback_note(monkeypatch):
    _with_registry(
        monkeypatch,
        claude=WorkerEntry(
            argv=("claude",), quarantined=False, allowed_lanes=("code-health",),
        ),
    )
    selection = select_worker({"content": {"lane": "code-health"}})
    assert selection.fallback_note is None


def test_quarantined_worker_still_refuses_when_hinted(monkeypatch):
    _with_registry(
        monkeypatch,
        claude=WorkerEntry(
            argv=("claude",), quarantined=False, allowed_lanes=("code-health",),
        ),
        risky=WorkerEntry(argv=("risky",), quarantined=True,
                          allowed_lanes=("code-health",),
                          quarantine_reason="containment escape"),
    )
    with pytest.raises(DispatchError) as e:
        select_worker(_task("risky"))
    assert "quarantined" in str(e.value)
    assert "retired" not in str(e.value)


def test_retired_and_quarantined_hint_falls_back_without_quarantine_error(monkeypatch):
    # Retirement is checked first and short-circuits straight to the default:
    # a decommissioned worker's quarantine status is moot once it is never
    # coming back.
    _with_registry(
        monkeypatch,
        both=WorkerEntry(
            argv=("both",), quarantined=True, allowed_lanes=("code-health",),
            quarantine_reason="x", retired=True, retirement_reason="Amendment 30",
        ),
        claude=WorkerEntry(
            argv=("claude",), quarantined=False, allowed_lanes=("code-health",),
        ),
    )
    selection = select_worker(_task("both"))
    assert selection.name == "claude"
    assert selection.fallback_note and "retired" in selection.fallback_note


def test_retired_default_worker_with_no_hint_still_refuses(monkeypatch):
    _with_registry(monkeypatch, claude=WorkerEntry(
        argv=("claude",), quarantined=False, allowed_lanes=("code-health",),
        retired=True, retirement_reason="Amendment 30",
    ))
    with pytest.raises(DispatchError) as e:
        select_worker({"content": {"lane": "code-health"}})
    msg = str(e.value)
    assert "retired" in msg
    assert "no longer licensed" in msg
    assert "new amendment" in msg
    assert "Amendment 30" in msg


def test_hinted_retired_worker_refuses_when_fallback_target_unusable(monkeypatch):
    _with_registry(
        monkeypatch,
        oldtool=WorkerEntry(
            argv=("oldtool",), quarantined=False, allowed_lanes=("code-health",),
            retired=True, retirement_reason="Amendment 30",
        ),
        claude=WorkerEntry(
            argv=("claude",), quarantined=True, allowed_lanes=("code-health",),
            quarantine_reason="containment escape",
        ),
    )
    with pytest.raises(DispatchError) as e:
        select_worker(_task("oldtool"))
    assert "not dispatchable either" in str(e.value)


def test_no_live_registry_entry_is_retired():
    for name, entry in dispatch.WORKER_REGISTRY.items():
        assert entry.retired is False, f"{name} unexpectedly retired"
