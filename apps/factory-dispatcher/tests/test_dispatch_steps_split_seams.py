"""Part B, B4a (design §7, rows E2 and E6): the two seams the split run path
needs in activities/dispatch_steps.py, with no split behaviour wired yet.

E2: ``_cfg_to_state``/``_cfg_from_state`` carry ``cfg.workdir_root`` in every
mode, because claim sets it to R via ``dataclasses.replace`` in split mode
(design §2.3 step 1) and every later activity reads it back off state.

E6: ``_run_worker_with_heartbeat`` gains a keyword-only ``prepare_argv``,
resolved at call time as ``prepare_argv or dispatch.prepare_worker_argv`` --
the split partial B4b passes replaces the direct call, but with the keyword
unset (every caller today) behaviour is byte-identical to before this
parameter existed.

No network, no substrate, no live sudo (decision record 2026-09-13 D7).
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


# ---------------------------------------------------------------------------
# AC-3 (E2): workdir_root round-trips through _cfg_to_state/_cfg_from_state.
# ---------------------------------------------------------------------------


def test_cfg_round_trip_carries_workdir_root(tmp_path):
    cfg = dispatch.Config(
        repo="grant/test-repo",
        remote="origin",
        base_ref="main",
        repo_root=tmp_path,
        workdir_root=tmp_path / "factory-runs",
    )

    state = dispatch_steps._cfg_to_state(cfg)

    assert state["workdir_root"] == str(tmp_path / "factory-runs")

    restored = dispatch_steps._cfg_from_state({"cfg": state})

    assert restored == cfg
    assert restored.workdir_root == tmp_path / "factory-runs"


def test_cfg_round_trip_preserves_none_workdir_root(tmp_path):
    cfg = dispatch.Config(
        repo="grant/test-repo", remote="origin", base_ref="main", repo_root=tmp_path,
    )
    assert cfg.workdir_root is None

    state = dispatch_steps._cfg_to_state(cfg)

    assert state["workdir_root"] is None

    restored = dispatch_steps._cfg_from_state({"cfg": state})

    assert restored == cfg
    assert restored.workdir_root is None


def test_cfg_round_trip_changes_nothing_else(tmp_path):
    """AC-3: round-tripping workdir_root must not disturb any other field."""
    without_root = dispatch.Config(
        repo="grant/test-repo", remote="origin", base_ref="main", repo_root=tmp_path,
    )
    with_root = dataclasses.replace(without_root, workdir_root=tmp_path / "runs")

    state_without = dispatch_steps._cfg_to_state(without_root)
    state_with = dispatch_steps._cfg_to_state(with_root)

    non_workdir_keys = {"repo", "remote", "base_ref", "repo_root"}
    for key in non_workdir_keys:
        assert state_without[key] == state_with[key]

    restored_without = dispatch_steps._cfg_from_state({"cfg": state_without})
    restored_with = dispatch_steps._cfg_from_state({"cfg": state_with})
    for field in non_workdir_keys:
        assert getattr(restored_without, field) == getattr(restored_with, field)


# ---------------------------------------------------------------------------
# AC-4 (E6): the prepare_argv seam.
# ---------------------------------------------------------------------------


def _base_state(tmp_path):
    return {
        "task": {"id": "task-1", "content": {"title": "do the thing"}},
        "worker": {"name": "codex", "argv": ["codex", "exec"], "uses_personas": True},
        "prompt": "do the work",
        "clone": str(tmp_path),
        "budget": dispatch.DEFAULT_BUDGET_MINUTES,
        "principle_pairs": (("PRIN-1", "do it right"),),
    }


def _fake_run_worker(prompt, clone, budget, argv, *a, **k):
    return dispatch.WorkerResult(exit_code=0, stdout="ok", stderr=None, duration_s=0.1, timed_out=False)


def test_prepare_argv_keyword_receives_identical_arguments(monkeypatch, tmp_path):
    """A recording fake, passed as prepare_argv, receives exactly the
    arguments the direct dispatch.prepare_worker_argv call would have."""
    state = _base_state(tmp_path)
    direct_calls = []
    fake_calls = []

    def recording_direct(*args):
        direct_calls.append(args)
        return ("codex", "exec")

    def recording_fake(*args):
        fake_calls.append(args)
        return ("codex", "exec")

    monkeypatch.setattr(dispatch, "prepare_worker_argv", recording_direct)

    dispatch_steps._run_worker_with_heartbeat(
        state, run_worker=_fake_run_worker, prepare_argv=recording_fake,
    )

    assert len(fake_calls) == 1
    assert direct_calls == []  # dispatch.prepare_worker_argv must not have run

    # Drive the unset-keyword path with the same state to prove the args a
    # direct call would have received are identical to what the fake got.
    dispatch_steps._run_worker_with_heartbeat(state, run_worker=_fake_run_worker)

    assert len(direct_calls) == 1
    assert direct_calls[0] == fake_calls[0]


def test_prepare_argv_unset_runs_dispatch_prepare_worker_argv(monkeypatch, tmp_path):
    """With the keyword unset, dispatch.prepare_worker_argv is what runs."""
    state = _base_state(tmp_path)
    calls = []

    def recording(*args):
        calls.append(args)
        return ("codex", "exec")

    monkeypatch.setattr(dispatch, "prepare_worker_argv", recording)

    dispatch_steps._run_worker_with_heartbeat(state, run_worker=_fake_run_worker)

    assert len(calls) == 1


def test_prepare_argv_given_never_calls_dispatch_prepare_worker_argv(monkeypatch, tmp_path):
    """AC-6 mutation: calling dispatch.prepare_worker_argv when a prepare_argv
    was given must turn this test red."""
    state = _base_state(tmp_path)

    def exploding_direct(*_a, **_k):
        raise AssertionError("dispatch.prepare_worker_argv must not run when prepare_argv is given")

    monkeypatch.setattr(dispatch, "prepare_worker_argv", exploding_direct)

    result = dispatch_steps._run_worker_with_heartbeat(
        state, run_worker=_fake_run_worker, prepare_argv=lambda *a: ("codex", "exec"),
    )

    assert result.exit_code == 0
