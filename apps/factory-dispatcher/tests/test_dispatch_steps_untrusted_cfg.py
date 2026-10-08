"""A Temporal-invoked dispatch step never takes its Config from its payload.

Narrows dev.finding dd648709 (the Temporal frontend on :7233 accepts
unauthenticated clients): a plain workflow start on task queue
factory-dispatcher-dev can carry a "cfg" key claim would otherwise honor, and
the state that flows through isolate/run/contain/scope/propose was built by
claim itself and normally cannot be forged by a plain start -- but this
suite tests each step's own refusal directly, independent of how its state
came to carry an untrusted cfg.

Every test here drives activities.dispatch_steps functions through
temporalio.testing.ActivityEnvironment (an in-process activity context, no
Temporal server) or, for the "outside a context" half of the contract,
directly -- exactly as tests/test_claim_atomicity.py and
tests/test_scheduled_workdir_lifecycle.py already do. Nothing here talks to a
real Temporal server or the live substrate (D7).
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import doctrine  # noqa: E402
from activities import dispatch_steps  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment  # noqa: E402

from test_claim_atomicity import FakeSubstrate, make_task  # noqa: E402


class _StopHere(Exception):
    """Raised by a recording spy once it has captured its call, so a test can
    assert on what a step passed downstream without needing that downstream
    call to actually succeed."""


def _hostile_cfg_state(tmp_path: Path) -> dict[str, str]:
    return dispatch_steps._cfg_to_state(
        dispatch.Config(
            repo="attacker/repo",
            remote="https://attacker.test/r.git",
            repo_root=tmp_path / "hostile",
        )
    )


def _malformed_cfg_state(tmp_path: Path) -> dict[str, str]:
    state = _hostile_cfg_state(tmp_path)
    del state["repo_root"]
    return state


BAD_CFG_BUILDERS = pytest.mark.parametrize(
    "bad_cfg_fn", [_hostile_cfg_state, _malformed_cfg_state], ids=["hostile", "malformed"]
)


def _fail_if_called(message: str):
    def _fail(*_args, **_kwargs):
        raise AssertionError(message)

    return _fail


# ---------------------------------------------------------------------------
# (a)/(b)/(c): claim
# ---------------------------------------------------------------------------


def test_claim_refuses_hostile_cfg(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("FACTORY_DISPATCH_RUN_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    monkeypatch.setattr(
        dispatch_steps,
        "default_store",
        _fail_if_called("default_store must not be reached: the request was refused"),
    )
    monkeypatch.setattr(
        dispatch,
        "budget_floor_reason",
        _fail_if_called("budget_floor_reason must not be reached: the request was refused"),
    )
    monkeypatch.setattr(
        dispatch, "pick_task", _fail_if_called("pick_task must not be reached: the request was refused")
    )
    monkeypatch.setattr(
        doctrine,
        "load_citations_for_lane",
        _fail_if_called("load_citations_for_lane must not be reached: the request was refused"),
    )

    env = ActivityEnvironment()
    with caplog.at_level(logging.ERROR, logger="factory-dispatcher.dispatch-steps"):
        with pytest.raises(ApplicationError) as exc_info:
            env.run(dispatch_steps.claim_activity, {"cfg": _hostile_cfg_state(tmp_path)})

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True
    assert not (tmp_path / "lock").exists()
    assert dispatch_steps._dispatch_run_lock_handle is None

    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "factory-dispatcher.dispatch-steps"
    ]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "claim" in message
    assert "test" in message
    assert "test-run" in message
    assert "hostile" not in message
    assert "attacker" not in message
    assert str(tmp_path) not in message


def test_claim_refuses_cfg_equal_to_trusted(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_DISPATCH_RUN_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    monkeypatch.setattr(
        dispatch_steps,
        "default_store",
        _fail_if_called("default_store must not be reached: the request was refused"),
    )

    trusted_state = dispatch_steps._cfg_to_state(dispatch.Config.from_env())
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.claim_activity, {"cfg": trusted_state})

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


def test_claim_refuses_cfg_none(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_DISPATCH_RUN_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    monkeypatch.setattr(
        dispatch_steps,
        "default_store",
        _fail_if_called("default_store must not be reached: the request was refused"),
    )

    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.claim_activity, {"cfg": None})

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


def test_claim_with_no_cfg_proceeds_in_a_context(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_DISPATCH_RUN_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    received_repo_roots: list[Path] = []

    def _spy(repo_root, _lane):
        received_repo_roots.append(repo_root)
        return ()

    monkeypatch.setattr(doctrine, "load_citations_for_lane", _spy)

    env = ActivityEnvironment()
    result = env.run(dispatch_steps.claim_activity, {"dry_run": True})

    assert result["status"] == "dry_run"
    assert received_repo_roots == [dispatch.Config.from_env().repo_root]


# ---------------------------------------------------------------------------
# (d): isolate / run / contain / scope / propose all refuse a hostile or
# malformed cfg, before the first dispatch call each step makes today.
# ---------------------------------------------------------------------------


@BAD_CFG_BUILDERS
def test_isolate_refuses_untrusted_cfg(monkeypatch, tmp_path, bad_cfg_fn):
    fake_tmp = tmp_path / "system-tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(dispatch_steps.tempfile, "gettempdir", lambda: str(fake_tmp))
    monkeypatch.setattr(
        dispatch, "make_clone", _fail_if_called("make_clone must not be reached: cfg was refused")
    )

    state = {
        "cfg": bad_cfg_fn(tmp_path),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
    }
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.isolate_activity, state)

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True
    assert not any(p.name.startswith("factory-") for p in fake_tmp.iterdir())


@BAD_CFG_BUILDERS
def test_run_refuses_untrusted_cfg(monkeypatch, tmp_path, bad_cfg_fn):
    monkeypatch.setattr(
        dispatch,
        "apply_preserved_baseline",
        _fail_if_called("apply_preserved_baseline must not be reached: cfg was refused"),
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    state = {
        "cfg": bad_cfg_fn(tmp_path),
        "clone": str(clone),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
    }
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.run_activity, state)

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


@BAD_CFG_BUILDERS
def test_contain_refuses_untrusted_cfg(monkeypatch, tmp_path, bad_cfg_fn):
    monkeypatch.setattr(
        dispatch,
        "raise_for_worker_failure",
        _fail_if_called("raise_for_worker_failure must not be reached: cfg was refused"),
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    state = {
        "cfg": bad_cfg_fn(tmp_path),
        "clone": str(clone),
        "worker_result": {
            "exit_code": 0,
            "stdout": "",
            "duration_s": 1.0,
            "timed_out": False,
        },
        "budget": 10,
    }
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.contain_activity, state)

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


@BAD_CFG_BUILDERS
def test_scope_refuses_untrusted_cfg(monkeypatch, tmp_path, bad_cfg_fn):
    monkeypatch.setattr(
        dispatch_steps,
        "_merge_base_diff_paths",
        _fail_if_called("_merge_base_diff_paths must not be reached: cfg was refused"),
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    state = {
        "cfg": bad_cfg_fn(tmp_path),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
        "paths": ["apps/factory-dispatcher/dispatch.py"],
        "clone": str(clone),
    }
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.scope_activity, state)

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


@BAD_CFG_BUILDERS
def test_propose_refuses_untrusted_cfg(monkeypatch, tmp_path, bad_cfg_fn):
    monkeypatch.setattr(
        dispatch_steps,
        "default_store",
        _fail_if_called("default_store must not be reached: cfg was refused"),
    )
    state = {
        "cfg": bad_cfg_fn(tmp_path),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
        "worker_result": {
            "exit_code": 0,
            "stdout": "",
            "duration_s": 1.0,
            "timed_out": False,
        },
        "scope_verdict": {"out_of_scope": [], "forbidden": [], "unsupported_patterns": []},
        "verification_report": {"bootstrap_note": "", "commands": []},
        "citation_ids": [],
        "paths": [],
        "worker": {"name": "claude"},
        "clone": str(tmp_path / "clone"),
    }
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        env.run(dispatch_steps.propose_activity, state)

    assert exc_info.value.type == "UntrustedDispatchConfig"
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# (e): isolate / run / scope accept a cfg equal to Config.from_env() and pass
# the trusted object (not the payload's copy) down to the step's own first
# dispatch call.
# ---------------------------------------------------------------------------


def test_isolate_accepts_cfg_equal_to_trusted(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch_steps.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", ""))

    trusted = dispatch.Config.from_env()
    received: list[dispatch.Config] = []

    def _spy(cfg, _dest):
        received.append(cfg)
        raise _StopHere()

    monkeypatch.setattr(dispatch, "make_clone", _spy)

    state = {
        "cfg": dispatch_steps._cfg_to_state(trusted),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
    }
    env = ActivityEnvironment()
    with pytest.raises(_StopHere):
        env.run(dispatch_steps.isolate_activity, state)

    assert received == [trusted]


def test_run_accepts_cfg_equal_to_trusted(monkeypatch, tmp_path):
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: FakeSubstrate([]))

    trusted = dispatch.Config.from_env()
    received: list[dispatch.Config] = []

    def _spy(cfg, _sub, _clone, _task):
        received.append(cfg)
        raise _StopHere()

    monkeypatch.setattr(dispatch, "apply_preserved_baseline", _spy)

    clone = tmp_path / "clone"
    clone.mkdir()
    state = {
        "cfg": dispatch_steps._cfg_to_state(trusted),
        "clone": str(clone),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
    }
    env = ActivityEnvironment()
    with pytest.raises(_StopHere):
        env.run(dispatch_steps.run_activity, state)

    assert received == [trusted]


def test_scope_accepts_cfg_equal_to_trusted(monkeypatch, tmp_path):
    trusted = dispatch.Config.from_env()
    received: list[tuple[str, str]] = []

    def _spy(_clone, remote, base_ref):
        received.append((remote, base_ref))
        raise _StopHere()

    monkeypatch.setattr(dispatch_steps, "_merge_base_diff_paths", _spy)

    clone = tmp_path / "clone"
    clone.mkdir()
    state = {
        "cfg": dispatch_steps._cfg_to_state(trusted),
        "task": make_task("bead-a", "2026-08-23T00:00:00Z"),
        "paths": [],
        "clone": str(clone),
    }
    env = ActivityEnvironment()
    with pytest.raises(_StopHere):
        env.run(dispatch_steps.scope_activity, state)

    assert received == [(trusted.remote, trusted.base_ref)]


# ---------------------------------------------------------------------------
# (f): the in-process path (dispatch.dispatch_once, and a direct
# ACTIVITY_FUNCTIONS call with no Temporal context) is unaffected.
# ---------------------------------------------------------------------------


def test_dispatch_once_in_process_path_still_uses_the_passed_cfg(monkeypatch, tmp_path):
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    task = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = FakeSubstrate([task])

    received_repo_roots: list[Path] = []

    def _spy(repo_root, _lane):
        received_repo_roots.append(repo_root)
        return ()

    monkeypatch.setattr(doctrine, "load_citations_for_lane", _spy)

    cfg = dispatch.Config(
        repo="example/repo", remote="git@github.com:example/repo.git", repo_root=tmp_path
    )

    exit_code = dispatch.dispatch_once(cfg, sub, None, dry_run=True)

    assert exit_code == 0
    assert received_repo_roots == [tmp_path]


def test_activity_functions_isolate_direct_call_still_honors_payload_cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", ""))

    received: list[dispatch.Config] = []

    def fake_make_clone(cfg, dest):
        received.append(cfg)
        dest.mkdir(parents=True)

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)

    cfg = dispatch.Config(repo_root=tmp_path)
    dispatch_steps.ACTIVITY_FUNCTIONS["isolate"]({"cfg": dispatch_steps._cfg_to_state(cfg)})

    assert received[0].repo_root == tmp_path


# ---------------------------------------------------------------------------
# (g): a source ratchet -- every call of _cfg_from_state and Config.from_env
# in this module sits inside _resolve_cfg (or, for Config.from_env,
# reconcile_activity).
# ---------------------------------------------------------------------------


def test_cfg_resolution_calls_are_ratcheted_to_one_helper():
    source = Path(dispatch_steps.__file__).read_text()
    tree = ast.parse(source)

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []
            self.violations: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            enclosing = self.stack[-1] if self.stack else None
            if name == "_cfg_from_state" and enclosing != "_resolve_cfg":
                self.violations.append(
                    f"_cfg_from_state called in {enclosing!r} at line {node.lineno}, "
                    "outside _resolve_cfg"
                )
            if name == "from_env" and enclosing not in ("_resolve_cfg", "reconcile_activity"):
                self.violations.append(
                    f"Config.from_env called in {enclosing!r} at line {node.lineno}, "
                    "outside _resolve_cfg/reconcile_activity"
                )
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    assert visitor.violations == []
