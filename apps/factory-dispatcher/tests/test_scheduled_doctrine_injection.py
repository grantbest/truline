"""The scheduled (Temporal) dispatch path must load doctrine exactly as the CLI does.

Before this change, dispatch_once (dispatch.py) resolved lane principles via
doctrine.load_citations_for_lane and refused to dispatch on a PrinciplesParseError
(dispatch.py:4181-4188), but claim_activity (activities/dispatch_steps.py) called
guards.build_prompt(task, notes) with no principles at all -- every scheduled dispatch,
which is the only path production actually runs through since Amendment 24, ran with
zero doctrine and no worse a case reported. This file proves the gap is closed: the
scheduled claim step now loads doctrine through the same doctrine.load_citations_for_lane
call the CLI makes (a double replaces it here, never a repo checkout), a load failure
halts the pass before the pending->doing transition so no retry attempt is spent, and
the two paths render byte-identical doctrine content for the same lane.

No substrate, no network, no Temporal server: FakeSubstrate doubles and an in-process
workflow_core.run_dispatch_sequence harness, exactly as test_claim_atomicity.py and
test_workflow_core.py already do.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import doctrine  # noqa: E402
import guards  # noqa: E402
import workflow_core  # noqa: E402
from activities import dispatch_steps  # noqa: E402

from test_claim_atomicity import FakeSubstrate as ScheduledFakeSubstrate  # noqa: E402
from test_claim_atomicity import make_task  # noqa: E402
from test_dispatch import FakeSubstrate as CliFakeSubstrate  # noqa: E402
from test_dispatch import task as cli_task  # noqa: E402
from test_doctrine_injection import REGISTRY_TEXT, _stub_worker_run  # noqa: E402


def test_claim_activity_injects_doctrine_from_an_injected_loader_double(monkeypatch):
    """AC: the scheduled claim step injects doctrine, driven with an injected
    doctrine loader double -- no principles.md on disk, no repo checkout."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")  # lane "code-health"
    sub = ScheduledFakeSubstrate([task_a])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    seen = {}

    def fake_loader(repo_root, lane):
        seen["repo_root"] = repo_root
        seen["lane"] = lane
        return (doctrine.PrincipleCitation(id="PRIN-777", statement="injected statement"),)

    monkeypatch.setattr(doctrine, "load_citations_for_lane", fake_loader)

    result = dispatch_steps.claim_activity({})

    assert result["status"] == "claimed"
    assert seen["lane"] == "code-health"
    assert "## Doctrine" in result["prompt"]
    assert "PRIN-777" in result["prompt"]
    assert "injected statement" in result["prompt"]
    assert result["principle_pairs"] == [["PRIN-777", "injected statement"]]


def test_scheduled_dispatch_halts_without_consuming_a_retry_when_doctrine_load_fails(
    monkeypatch,
):
    """AC: a doctrine load failure halts the scheduled dispatch, classified as an
    environmental fault, before the bead is ever claimed -- so no retry attempt is
    burned. Driven through workflow_core.run_dispatch_sequence, the actual sequence
    a Temporal workflow runs, with every step after "claim" asserted unreachable."""
    task_a = make_task("bead-a", "2026-08-23T00:00:00Z")
    sub = ScheduledFakeSubstrate([task_a])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    def failing_loader(repo_root, lane):
        raise doctrine.PrinciplesParseError("could not parse principles.md: boom")

    monkeypatch.setattr(doctrine, "load_citations_for_lane", failing_loader)

    async def execute(name, state):
        if name == "reconcile":
            return {"status": "reconciled"}
        if name == "claim":
            return dispatch_steps.claim_activity(state)
        raise AssertionError(
            f"step {name!r} must not run: claim already refused the dispatch"
        )

    result = asyncio.run(workflow_core.run_dispatch_sequence({}, execute))

    assert result["status"] == "environmental_fault"
    assert result["exit_code"] == 1
    assert "Cannot dispatch" in result["message"]
    assert task_a["state"] == "pending", "the bead must never reach doing"
    assert sub.transitions == [], "no pending->doing transition, so no attempt spent"
    assert sub.notes == []


def test_cli_and_scheduled_paths_produce_the_same_doctrine_section_for_one_lane(
    monkeypatch, tmp_path
):
    """AC: parity -- both paths must render byte-identical doctrine content for the
    same lane, so this cannot silently regress before the successor bead unifies the
    pipelines. Each path is driven through its real entry point (dispatch.dispatch_once
    for the CLI, dispatch_steps.claim_activity for the schedule); the loader itself is
    called once, independently, only to compute what both are expected to contain."""
    registry_path = tmp_path / doctrine.PRINCIPLES_MD_PATH
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(REGISTRY_TEXT)

    expected_section = guards.render_doctrine_section(
        doctrine.as_prompt_pairs(doctrine.citations_for_lane(REGISTRY_TEXT, "code-health"))
    )
    assert expected_section, "sanity: code-health has embeddable citations in REGISTRY_TEXT"

    # --- CLI path ---
    _stub_worker_run(monkeypatch)
    captured = {}

    def fake_run_worker(prompt, _clone, _budget, _argv, *_a, **_k):
        captured["prompt"] = prompt
        return dispatch.WorkerResult(exit_code=0, stdout="done", duration_s=1.0, timed_out=False)

    monkeypatch.setattr(dispatch, "run_worker", fake_run_worker)
    cli_sub = CliFakeSubstrate(cli_task(lane="code-health"))
    rc = dispatch.dispatch_once(dispatch.Config(repo_root=tmp_path), cli_sub, None, dry_run=False)

    assert rc == 0
    assert expected_section in captured["prompt"]

    # --- Scheduled path ---
    monkeypatch.setattr(
        dispatch.Config, "from_env", staticmethod(lambda: dispatch.Config(repo_root=tmp_path))
    )
    task_b = make_task("bead-b", "2026-08-23T00:05:00Z")
    scheduled_sub = ScheduledFakeSubstrate([task_b])
    monkeypatch.setattr(dispatch_steps, "default_store", lambda: scheduled_sub)
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)

    result = dispatch_steps.claim_activity({})

    assert result["status"] == "claimed"
    assert expected_section in result["prompt"]
