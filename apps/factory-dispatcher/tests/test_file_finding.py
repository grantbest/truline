"""S55-10a: file_finding.py is the one filing path for dev.finding beads.

Reuses tests/test_file_task.py's FakeSubstrate (extended in place with
create_bead/transition_state/add_link/list_beads) rather than a new
hand-rolled double -- MAX_HAND_ROLLED_DOUBLES stays at 35
(tests/test_store_double_call_surface.py).
"""

from __future__ import annotations

import ast
import io
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import dispatch  # noqa: E402
import file_finding  # noqa: E402
import substrate  # noqa: E402

from test_file_task import FakeSubstrate  # noqa: E402


def _finding_spec(**overrides):
    """A spec missing only ``state`` -- the field AC-2's tests exercise."""
    spec = {
        "kind": "bug",
        "disposition": "backlog",
        "severity": "low",
        "summary": "a summary",
    }
    spec.update(overrides)
    return spec


def _good_spec(**overrides):
    spec = _finding_spec(state="backlogged")
    spec.update(overrides)
    return spec


# ---------------------------------------------------------------------------
# AC-1: validation before any store call, naming the field.
# ---------------------------------------------------------------------------

REFUSAL_CASES = [
    ({"kind": None}, "kind"),
    ({"kind": "typo"}, "kind"),
    ({"disposition": None}, "disposition"),
    ({"disposition": "typo"}, "disposition"),
    ({"severity": None}, "severity"),
    ({"severity": "typo"}, "severity"),
    ({"summary": ""}, "summary"),
    ({"summary": "   "}, "summary"),
    ({"summary": None}, "summary"),
    ({"evidence": 123}, "evidence"),
    ({"reproduction": 123}, "reproduction"),
    ({"disposition": "blocking", "evidence": None}, "evidence"),
    ({"disposition": "blocking", "evidence": ""}, "evidence"),
    ({"disposition": "blocking", "evidence": "   "}, "evidence"),
]


@pytest.mark.parametrize("overrides,expected_field", REFUSAL_CASES)
def test_refuses_naming_the_field(overrides, expected_field):
    spec = _good_spec(**overrides)
    with pytest.raises(SystemExit) as exc:
        file_finding._validate_and_build_content(spec)
    assert expected_field in str(exc.value)


def test_blocking_with_evidence_is_accepted():
    content, state = file_finding._validate_and_build_content(
        _good_spec(disposition="blocking", evidence="measured on main")
    )
    assert content["disposition"] == "blocking"
    assert state == "backlogged"


def test_extra_keys_ride_into_content_unchanged():
    content, _ = file_finding._validate_and_build_content(_good_spec(found_in="PR #123"))
    assert content["found_in"] == "PR #123"


def test_valid_spec_builds_content_without_state_key():
    content, state = file_finding._validate_and_build_content(_good_spec(evidence="e"))
    assert "state" not in content
    assert state == "backlogged"
    assert content["kind"] == "bug"


# ---------------------------------------------------------------------------
# c2c52aaf AC-1: a spec that is not a JSON object is refused by name, before
# any field check, at every entry point that reaches
# _validate_and_build_content -- not a traceback, not a per-character refusal.
# ---------------------------------------------------------------------------

NON_DICT_SPECS = [[], "x", 1, None, True]


@pytest.mark.parametrize("bad_spec", NON_DICT_SPECS)
def test_validate_and_build_content_refuses_non_dict_spec_by_name(bad_spec):
    with pytest.raises(SystemExit) as exc:
        file_finding._validate_and_build_content(bad_spec)
    assert str(exc.value) == f"spec must be a JSON object; got {type(bad_spec).__name__}"


@pytest.mark.parametrize("bad_spec", NON_DICT_SPECS)
def test_file_finding_refuses_non_dict_spec(bad_spec):
    with pytest.raises(SystemExit) as exc:
        file_finding.file_finding(bad_spec, "grant", prompt_ref="x", sub=object())
    assert str(exc.value) == f"spec must be a JSON object; got {type(bad_spec).__name__}"


@pytest.mark.parametrize("bad_spec", NON_DICT_SPECS)
def test_cli_refuses_non_dict_spec_by_name_with_no_store_call(monkeypatch, bad_spec):
    """The gate's own reproduction: `printf '[]' | file_finding.py - --prompt-ref x
    --created-by x --dry-run` ended in an AttributeError traceback rather than
    a SystemExit naming the shape."""

    class ExplodingSubstrate:
        def __init__(self, *a, **k):
            raise AssertionError("must never construct a store client")

        def __getattr__(self, name):
            raise AssertionError(f"touched the store via {name!r}")

    monkeypatch.setattr(file_finding, "Substrate", ExplodingSubstrate)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad_spec)))

    with pytest.raises(SystemExit) as exc:
        file_finding.main(["-", "--created-by", "x", "--prompt-ref", "x", "--dry-run"])

    assert str(exc.value) == f"spec must be a JSON object; got {type(bad_spec).__name__}"


# ---------------------------------------------------------------------------
# AC-2: a finding with no disposition state is refused.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state_value", [None, "pending", "bklogged"])
def test_state_refusals_name_the_four_admitted_states(state_value):
    spec = _finding_spec()
    if state_value is not None:
        spec["state"] = state_value
    with pytest.raises(SystemExit) as exc:
        file_finding._validate_and_build_content(spec)
    message = str(exc.value)
    for name in file_finding.FINDING_DISPOSITION_STATES:
        assert name in message


# ---------------------------------------------------------------------------
# AC-3: complete, honest provenance.
# ---------------------------------------------------------------------------


def test_provenance_key_set_matches_operator_action_shape():
    provenance = file_finding._provenance("grant", "pr#1234")
    assert set(provenance) == set(dispatch.provenance_for_operator_action("x", "y"))


def test_provenance_fields_are_exact():
    provenance = file_finding._provenance("operator@example.org", "audit-2026-10-01")
    assert provenance == {
        "worker": "operator@example.org",
        "model": "none",
        "prompt_ref": "audit-2026-10-01",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }


def test_file_finding_refuses_missing_created_by():
    with pytest.raises(SystemExit):
        file_finding.file_finding(_good_spec(), "", prompt_ref="x", sub=object())


def test_file_finding_refuses_missing_prompt_ref():
    with pytest.raises(SystemExit):
        file_finding.file_finding(_good_spec(), "grant", prompt_ref="", sub=object())


def test_cli_refuses_when_no_identity_given(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_OPERATOR", raising=False)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_good_spec()))

    with pytest.raises(SystemExit):
        file_finding.main([str(spec_path), "--prompt-ref", "pr#1"])


def test_cli_refuses_blank_prompt_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_OPERATOR", "grant")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_good_spec()))

    with pytest.raises(SystemExit):
        file_finding.main([str(spec_path), "--prompt-ref", "   "])


def test_cli_created_by_defaults_to_factory_operator_verbatim(tmp_path, monkeypatch):
    fake = FakeSubstrate([])
    monkeypatch.setattr(file_finding, "Substrate", lambda: fake)
    monkeypatch.setenv("FACTORY_OPERATOR", "outer-loop/claude")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_good_spec()))

    rc = file_finding.main([str(spec_path), "--prompt-ref", "pr#1"])
    assert rc == 0
    bead_id = next(iter(fake._beads))
    assert fake.transitions == [(bead_id, "pending", "backlogged", "outer-loop/claude")]


# ---------------------------------------------------------------------------
# AC-4: two writes, and a partial write is reported, never hidden.
# ---------------------------------------------------------------------------


def test_create_bead_always_uses_the_pending_entry_state():
    fake = FakeSubstrate([])
    file_finding.file_finding(_good_spec(), "grant", prompt_ref="pr#1", sub=fake)
    bead_id = next(iter(fake._beads))
    assert fake._beads[bead_id]["state"] == "backlogged"  # transitioned past pending
    # The create itself always requests pending -- transition_state is what
    # moves it on, never a direct non-pending create.
    assert fake.transitions == [(bead_id, "pending", "backlogged", "grant")]


def test_success_returns_frozen_result():
    fake = FakeSubstrate([])
    result = file_finding.file_finding(_good_spec(evidence="e"), "grant", prompt_ref="pr#1", sub=fake)
    assert result.state == "backlogged"
    assert result.id == next(iter(fake._beads))


def test_transition_failure_reports_partial_write_and_exits_1(capsys):
    fake = FakeSubstrate([], transition_error=substrate.SubstrateError(409, "already dispositioned"))

    with pytest.raises(SystemExit) as exc:
        file_finding.file_finding(_good_spec(), "grant", prompt_ref="pr#1", sub=fake)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert len(fake._beads) == 1  # exactly one create_bead call; no second create
    created_id = next(iter(fake._beads))
    assert created_id in captured.err
    assert "pending" in captured.err
    assert fake.transitions == []  # the transition never recorded as succeeded


# ---------------------------------------------------------------------------
# AC-5: output and dry run.
# ---------------------------------------------------------------------------


def test_cli_success_stdout_is_exactly_one_line(tmp_path, monkeypatch, capsys):
    fake = FakeSubstrate([])
    monkeypatch.setattr(file_finding, "Substrate", lambda: fake)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_good_spec()))

    rc = file_finding.main([str(spec_path), "--created-by", "grant", "--prompt-ref", "pr#1"])
    assert rc == 0
    out = capsys.readouterr().out
    bead_id = next(iter(fake._beads))
    assert out == f"{bead_id} backlogged\n"


def test_cli_dry_run_makes_no_store_call(tmp_path, monkeypatch, capsys):
    class ExplodingSubstrate:
        def __init__(self, *a, **k):
            raise AssertionError("dry-run must never construct a store client")

        def __getattr__(self, name):
            raise AssertionError(f"dry-run touched the store via {name!r}")

    monkeypatch.setattr(file_finding, "Substrate", ExplodingSubstrate)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_good_spec()))

    rc = file_finding.main(
        [str(spec_path), "--created-by", "grant", "--prompt-ref", "pr#1", "--dry-run"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["create"]["namespace"] == "dev"
    assert payload["create"]["type"] == "finding"
    assert payload["create"]["state"] == "pending"
    assert payload["create"]["content"]["kind"] == "bug"
    assert "state" not in payload["create"]["content"]
    assert payload["transition"] == {
        "from_state": "pending",
        "to_state": "backlogged",
        "created_by": "grant",
    }


# ---------------------------------------------------------------------------
# AC-7: the double rejects what the store rejects.
# ---------------------------------------------------------------------------

GOOD_CONTENT = {"kind": "bug", "disposition": "backlog", "severity": "low", "summary": "s"}
GOOD_PROVENANCE = {
    "worker": "grant",
    "model": "none",
    "prompt_ref": "x",
    "tokens": 0,
    "cost_usd": 0.0,
    "duration_s": 0.0,
}


def test_fake_create_bead_accepts_well_formed_content():
    fake = FakeSubstrate([])
    created = fake.create_bead(
        "dev", "finding", "pending", GOOD_CONTENT, "grant", provenance=GOOD_PROVENANCE
    )
    assert created["state"] == "pending"


def test_fake_create_bead_rejects_missing_required_field():
    bad = dict(GOOD_CONTENT)
    del bad["kind"]
    fake = FakeSubstrate([])
    with pytest.raises(substrate.SubstrateError) as exc:
        fake.create_bead("dev", "finding", "pending", bad, "grant", provenance=GOOD_PROVENANCE)
    assert exc.value.status == 422


def test_fake_create_bead_rejects_blocking_without_evidence():
    bad = dict(GOOD_CONTENT, disposition="blocking")
    fake = FakeSubstrate([])
    with pytest.raises(substrate.SubstrateError):
        fake.create_bead("dev", "finding", "pending", bad, "grant", provenance=GOOD_PROVENANCE)


def test_fake_create_bead_rejects_non_string_evidence():
    bad = dict(GOOD_CONTENT, disposition="blocking", evidence=123)
    fake = FakeSubstrate([])
    with pytest.raises(substrate.SubstrateError):
        fake.create_bead("dev", "finding", "pending", bad, "grant", provenance=GOOD_PROVENANCE)


def test_fake_create_bead_rejects_agent_created_by_without_provenance():
    fake = FakeSubstrate([])
    with pytest.raises(substrate.SubstrateError):
        fake.create_bead("dev", "finding", "pending", GOOD_CONTENT, "claude", provenance=None)


def test_fake_create_bead_rejects_wrong_provenance_key_set():
    bad_provenance = dict(GOOD_PROVENANCE, extra_field="nope")
    fake = FakeSubstrate([])
    with pytest.raises(substrate.SubstrateError):
        fake.create_bead(
            "dev", "finding", "pending", GOOD_CONTENT, "grant", provenance=bad_provenance
        )


def test_fake_transition_state_rejects_wrong_from_state():
    fake = FakeSubstrate(
        [], beads=[{"id": "f-1", "namespace": "dev", "type": "finding", "state": "backlogged", "content": {}}]
    )
    with pytest.raises(substrate.SubstrateError) as exc:
        fake.transition_state("f-1", "pending", "ruled", "grant")
    assert exc.value.status == 409


def test_fake_add_link_rejects_missing_target():
    fake = FakeSubstrate([{"id": "task-1", "state": "pending", "content": {}}])
    with pytest.raises(substrate.SubstrateError) as exc:
        fake.add_link("task-1", "missing-finding", "derived_from", "grant")
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# AC-8 (and AC-10's "calling _request directly for the create" mutation):
# no hand-rolled HTTP, checked structurally rather than trusting the fake's
# leniency to catch a regression.
# ---------------------------------------------------------------------------


def test_file_finding_never_calls_request_directly_or_imports_hand_rolled_http():
    source = pathlib.Path(file_finding.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_request":
            pytest.fail("file_finding.py calls _request directly")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.name for alias in node.names}
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            for forbidden in ("urllib", "requests", "httpx"):
                assert not any(n == forbidden or n.startswith(forbidden + ".") for n in names), (
                    f"file_finding.py imports {forbidden!r} directly"
                )
