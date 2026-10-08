"""Amendment 30 PR-7: claude registered (not default), with its controls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch


def test_claude_is_default_with_its_controls():
    entry = dispatch.WORKER_REGISTRY["claude"]
    assert dispatch.DEFAULT_WORKER == "claude"
    assert dispatch.CREATED_BY == "factory-dispatcher/claude"
    assert not entry.quarantined and not entry.retired
    assert "--dangerously-skip-permissions" in entry.argv
    assert "--no-session-persistence" in entry.argv
    assert "--strict-mcp-config" in entry.argv
    assert "claude-sonnet-5" in entry.argv
    assert entry.grade_json_result and entry.uses_personas
    assert ("DISABLE_AUTOUPDATER", "1") in entry.extra_env


def test_provenance_for_task_defaults_to_unmeasured_not_zero():
    """PC-INF-003/AC-2: a literal 0 asserts a measured spend of nothing; the
    honest default, absent a worker result to read, is None."""
    record = dispatch.provenance_for_task("task-1", "claude", 12.5)
    assert record["tokens"] is None
    assert record["cost_usd"] is None
    assert record["duration_s"] == 12.5


def test_provenance_for_task_records_measured_usage():
    """PC-INF-003/AC-2: when the worker result carries usage, provenance
    records the measured tokens and cost, not a placeholder."""
    record = dispatch.provenance_for_task(
        "task-1", "claude", 12.5, tokens=48213, cost_usd=1.7654
    )
    assert record["tokens"] == 48213
    assert record["cost_usd"] == 1.7654


def test_architect_predicate():
    assert dispatch.architect_predicate({"risk_class": "behavioral"})
    assert dispatch.architect_predicate({"risk_class": "structural", "nfrs": [{"x": 1}]})
    assert dispatch.architect_predicate({"arch_impact": {"applications": ["a"]}})
    assert not dispatch.architect_predicate({"risk_class": "structural"})


def _clone_with_personas(tmp_path):
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "polecat-developer.md").write_text("---\nx: y\n---\nPOLECAT BODY")
    (agents / "architect-sme.md").write_text("---\nx: y\n---\nARCHITECT BODY")
    return tmp_path

def test_personas_prompt_polecat_alone_for_structural(tmp_path):
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(clone, {"risk_class": "structural"})
    assert "POLECAT BODY" in prompt and "ARCHITECT BODY" not in prompt


def test_personas_prompt_architect_first_for_behavioral(tmp_path):
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(clone, {"risk_class": "behavioral"})
    assert prompt.index("ARCHITECT BODY") < prompt.index("POLECAT BODY")
    assert ".factory/design.md" in prompt


def test_personas_prompt_names_scratch_location_outside_checkout(tmp_path):
    """dev.task 4f24656a: a worker wrote its own review diff to the repo root
    because it had nowhere declared to put it, and the otherwise complete run
    was discarded as a scope violation. The assembled prompt must now say
    where ephemera goes, using the same constant run_worker sets TMPDIR from
    (test_worker_scratch_env_var_matches_what_run_worker_sets), so the two
    cannot drift apart."""
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(clone, {"risk_class": "structural"})
    assert f"${dispatch.WORKER_SCRATCH_ENV_VAR}" in prompt
    assert "ends the run" in prompt
    assert "repository root" in prompt


def test_personas_prompt_scratch_instruction_reaches_architect_phase_too(tmp_path):
    """Behavioral tasks run the architect and developer personas in one
    prompt/one worker invocation, so the scratch instruction is assembled at
    the top level (not persona-specific text) and reaches both without
    docs/agents/architect-sme.md needing its own copy."""
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(clone, {"risk_class": "behavioral"})
    assert f"${dispatch.WORKER_SCRATCH_ENV_VAR}" in prompt


def test_json_grading_overrides_lying_exit_code():
    class P:
        returncode = 0
        stdout = json.dumps({"is_error": True, "subtype": "error_during_execution", "result": "boom"})
        stderr = ""
    r = dispatch._grade_json_worker_result(P(), 1.0)
    assert r.exit_code == 1 and "error_during_execution" in (r.stderr or "")


def test_json_grading_captures_cost():
    class P:
        returncode = 0
        stdout = json.dumps({"is_error": False, "result": "done", "total_cost_usd": 0.2015})
        stderr = ""
    r = dispatch._grade_json_worker_result(P(), 1.0)
    assert r.exit_code == 0 and r.cost_usd == 0.2015


def test_json_grading_captures_usage_tokens():
    """PC-INF-003/AC-2: the claude CLI's `usage` object feeds provenance."""
    class P:
        returncode = 0
        stdout = json.dumps({
            "is_error": False,
            "result": "done",
            "total_cost_usd": 0.2015,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 2000,
            },
        })
        stderr = ""
    r = dispatch._grade_json_worker_result(P(), 1.0)
    assert r.tokens == 1200 + 340 + 500 + 2000


def test_json_grading_leaves_tokens_unmeasured_when_no_usage_reported():
    """A run with no usage object is unmeasured, not a measured zero."""
    class P:
        returncode = 0
        stdout = json.dumps({"is_error": False, "result": "done"})
        stderr = ""
    r = dispatch._grade_json_worker_result(P(), 1.0)
    assert r.tokens is None


def test_sum_usage_tokens_ignores_non_numeric_and_unknown_fields():
    assert dispatch._sum_usage_tokens(None) is None
    assert dispatch._sum_usage_tokens({}) is None
    assert dispatch._sum_usage_tokens({"server_tool_use": {"web_search_requests": 1}}) is None
    assert dispatch._sum_usage_tokens({"input_tokens": 10, "output_tokens": 5}) == 15


def test_unparseable_json_is_a_failure():
    class P:
        returncode = 0
        stdout = "not json at all"
        stderr = ""
    r = dispatch._grade_json_worker_result(P(), 1.0)
    assert r.exit_code == 1


def test_budget_floor(monkeypatch, tmp_path):
    ledger = tmp_path / "spend.json"
    monkeypatch.setenv("FACTORY_SPEND_LEDGER", str(ledger))
    monkeypatch.delenv("FACTORY_DAILY_USD_CAP", raising=False)
    assert dispatch.budget_floor_reason() is None
    monkeypatch.setenv("FACTORY_DAILY_USD_CAP", "5.00")
    assert dispatch.budget_floor_reason() is None
    dispatch.record_spend_usd(4.0)
    assert dispatch.budget_floor_reason() is None
    dispatch.record_spend_usd(1.5)
    reason = dispatch.budget_floor_reason()
    assert reason and "yields to the outer loop" in reason
    assert dispatch.read_daily_spend_usd() == 5.5
