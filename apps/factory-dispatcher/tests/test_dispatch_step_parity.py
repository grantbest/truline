"""The class guard: the CLI and the scheduled drain run the same steps.

dispatch.dispatch_once (the CLI, `--once`) and workflows.dispatch_task.
DispatchTaskWorkflow (the scheduled Temporal drain) used to be two hand-
written, independently-drifting copies of the same nine-step sequence. A fix
landed on one and the other kept the defect (2026-09-01 audit). The fix is
one shared implementation, not a longer parity checklist -- see
.factory/design.md.

This test does not hand-list an expected sequence and compare both entry
points against it: it drives each entry point for real (dispatch_once, and
DispatchTaskWorkflow.run through a stubbed temporalio so no server is
needed) with the real activities.dispatch_steps.ACTIVITY_FUNCTIONS spied on,
records the step names each entry point actually invoked, and asserts the
two recorded sequences are equal. A future change that makes either entry
point skip, reorder, or fork a step -- reintroducing a hand-rolled
implementation, or routing through a different resolution table -- fails
this test without anyone having to remember to update a checklist.

No substrate, no network, no Temporal server: the only I/O boundaries this
stubs are git/gh (dispatch.make_clone/open_pull_request), the worker
subprocess (dispatch.run_worker), and the bead store (FakeSubstrate).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402

from test_dispatch import FakeSubstrate, stub_successful_worker_run, task  # noqa: E402
from test_dispatch_activity_liveness import (  # noqa: E402
    _fresh_import,
    _install_temporal_stubs,
)

# Captured once, before any test wraps dispatch_steps.ACTIVITY_FUNCTIONS in
# spies -- both entry points are exercised in the same test against the same
# monkeypatch fixture, so re-wrapping whatever is currently installed would
# chain one entry point's spy onto the other's.
_ORIGINAL_ACTIVITY_FUNCTIONS = dict(dispatch_steps.ACTIVITY_FUNCTIONS)


def _spy_on_activity_functions(monkeypatch, calls: list[str]) -> None:
    """Wrap every dispatch_steps.ACTIVITY_FUNCTIONS entry to record its name.

    Patches the one table both entry points resolve steps through
    (dispatch.dispatch_once directly; the real Temporal worker via
    activities/__init__.py's ACTIVITIES, which this dict is built from) --
    not a second, hand-maintained list of step names.
    """

    def make_spy(name, fn):
        def spy(payload):
            calls.append(name)
            return fn(payload)

        return spy

    # Wrapped from _ORIGINAL_ACTIVITY_FUNCTIONS, not from whatever is
    # currently installed on dispatch_steps: this helper runs twice in the
    # same test (once per entry point), sharing one monkeypatch fixture, and
    # re-wrapping an already-wrapped dict would chain both entry points'
    # spies onto the same underlying calls, corrupting both lists.
    spied = {
        name: make_spy(name, fn) for name, fn in _ORIGINAL_ACTIVITY_FUNCTIONS.items()
    }
    monkeypatch.setattr(dispatch_steps, "ACTIVITY_FUNCTIONS", spied)


def _cli_step_sequence(monkeypatch, tmp_path) -> list[str]:
    calls: list[str] = []
    _spy_on_activity_functions(monkeypatch, calls)

    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    stub_successful_worker_run(monkeypatch)
    monkeypatch.setattr(dispatch, "prepare_worker_argv", lambda argv, *_a, **_k: argv)

    sub = FakeSubstrate(task())

    rc = dispatch.dispatch_once(dispatch.Config(repo_root=Path.cwd()), sub, None, dry_run=False)

    assert rc == 0, "the CLI pass must reach review for its step sequence to be meaningful"
    return calls


def _scheduled_step_sequence(monkeypatch, tmp_path) -> list[str]:
    calls: list[str] = []
    _spy_on_activity_functions(monkeypatch, calls)

    monkeypatch.setattr(dispatch.tempfile, "gettempdir", lambda: str(tmp_path))
    stub_successful_worker_run(monkeypatch)
    monkeypatch.setattr(dispatch, "prepare_worker_argv", lambda argv, *_a, **_k: argv)

    sub = FakeSubstrate(task())
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)

    workflow_mod = _install_temporal_stubs(monkeypatch)
    dispatch_task = _fresh_import(monkeypatch, "workflows.dispatch_task")

    async def execute_activity(name, payload, **_kwargs):
        return await asyncio.to_thread(dispatch_steps.ACTIVITY_FUNCTIONS[name], payload)

    workflow_mod.execute_activity = execute_activity

    result = asyncio.run(dispatch_task.DispatchTaskWorkflow().run({}))

    assert result["status"] == "review", (
        "the scheduled pass must reach review for its step sequence to be meaningful"
    )
    return calls


def test_cli_and_scheduled_entry_points_run_the_identical_step_sequence(monkeypatch, tmp_path):
    cli_workdir_root = tmp_path / "cli"
    cli_workdir_root.mkdir()
    scheduled_workdir_root = tmp_path / "scheduled"
    scheduled_workdir_root.mkdir()

    cli_calls = _cli_step_sequence(monkeypatch, cli_workdir_root)
    scheduled_calls = _scheduled_step_sequence(monkeypatch, scheduled_workdir_root)

    # "reconcile" is deliberately not part of the per-dispatch sequence
    # (see .factory/design.md and workflow_core.run_dispatch_sequence's
    # docstring): the scheduled drain runs it as separate bookkeeping ahead
    # of the claim, and the CLI reaches the identical, single
    # reconcile_review_tasks implementation through --reconcile-review
    # instead of on every dispatch. Stripping its one occurrence here is not
    # a second hand-listed expectation -- everything from "claim" on must
    # still match exactly, in order, with nothing else stripped.
    assert scheduled_calls[:1] == ["reconcile"]
    scheduled_claim_through_propose = scheduled_calls[1:]

    # The derived assertion above is the whole guard: both sequences come
    # from the code, so a step added to both pipelines passes and a step
    # added to one fails. A hand-maintained list here would reintroduce the
    # silent-divergence hole this test exists to close (release-gate finding
    # on #633) — deliberately absent.
    assert cli_calls == scheduled_claim_through_propose
    assert len(cli_calls) >= 8, "step sequence implausibly short — derivation broke"
