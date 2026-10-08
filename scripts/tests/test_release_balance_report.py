"""Tests for the release balance report.

R26.01 declares a balance and, once REL-1 lands, tasks carry the edges that
would let it be checked. Nothing checked it — this closes that gap. The
properties that matter most: a class declared at a non-zero share with no
delivered work is ABSENT, not 0%; a task's class comes only through its
outcome_ref, never off the task; the unassigned count reuses
traceability.classify_population rather than a second hand-rolled split; and
a criterion whose newest observation predates the release's opened_at is
"not re-measured," never reported as a current verdict.

No substrate, no cluster, no network: every fixture below is a plain dict.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rs = _load("release_status", "release-status.py")


def charter(**overrides):
    base = {
        "ref": "R26.01",
        "name": "Trust earns its amendment",
        "objective": "x",
        "opened_at": "2026-08-25",
        "declared_balance": {
            "feature": 15, "enabling": 35, "blocking": 15, "risk": 25, "security": 10,
        },
        "outcomes": [
            {"id": "O-1", "statement": "enabling work", "work_class": "enabling", "requirement_refs": []},
            {"id": "O-6", "statement": "security work", "work_class": "security", "requirement_refs": []},
        ],
    }
    base.update(overrides)
    return base


def task(identifier, *, state="pending", outcome_ref=None, release_ref_waived=None, updated_at=None):
    content = {}
    if outcome_ref is not None:
        content["outcome_ref"] = outcome_ref
    if release_ref_waived is not None:
        content["release_ref_waived"] = release_ref_waived
    bead = {"id": identifier, "state": state, "content": content}
    if updated_at is not None:
        bead["updated_at"] = updated_at
    return bead


# --- outcomes grouped by bead state (AC1) ------------------------------------


def test_outcome_tasks_are_grouped_by_bead_state():
    tasks = [
        task("t1", state="done", outcome_ref="O-1"),
        task("t2", state="doing", outcome_ref="O-1"),
        task("t3", state="review", outcome_ref="O-1"),
    ]
    delivers = {"t1": "R26.01", "t2": "R26.01", "t3": "R26.01"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    outcome = next(o for o in report.outcomes if o.id == "O-1")
    assert outcome.tasks_by_state == {"done": ["t1"], "doing": ["t2"], "review": ["t3"]}


def test_a_task_delivering_a_different_release_is_not_counted():
    tasks = [task("t1", outcome_ref="O-1")]
    delivers = {"t1": "R99.99"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    outcome = next(o for o in report.outcomes if o.id == "O-1")
    assert outcome.tasks_by_state == {}


# --- balance arithmetic and ABSENT marking (AC2) -----------------------------


def test_declared_and_unmet_is_absent_not_zero_percent():
    """The load-bearing distinction: 'declared and unmet' != 'not declared'."""
    tasks = [task("t1", outcome_ref="O-1")]
    delivers = {"t1": "R26.01"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    security = next(b for b in report.balance if b.work_class == "security")
    assert security.declared_pct == 10
    assert security.actual_count == 0
    assert security.actual_pct is None
    assert security.absent is True
    assert "ABSENT" in rs.format_release_report(report)


def test_a_class_not_declared_with_no_work_is_zero_not_absent():
    tasks: list[dict] = []
    c = charter(declared_balance={"enabling": 100})

    report = rs.build_release_report(c, tasks, {}, [])

    feature = next(b for b in report.balance if b.work_class == "feature")
    assert feature.declared_pct == 0
    assert feature.actual_pct is None
    assert feature.insufficient_sample is True
    assert feature.absent is False


def test_actual_percentage_is_computed_over_classified_work_only():
    tasks = [
        task("t1", outcome_ref="O-1"),
        task("t2", outcome_ref="O-1"),
        task("t3", outcome_ref="O-1"),
        task("t4", outcome_ref="O-1"),
        task("t5", outcome_ref="O-6"),
        task("t6"),  # unclassified: no outcome_ref
    ]
    delivers = {tid: "R26.01" for tid in ("t1", "t2", "t3", "t4", "t5", "t6")}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    enabling = next(b for b in report.balance if b.work_class == "enabling")
    security = next(b for b in report.balance if b.work_class == "security")
    assert enabling.actual_count == 4 and enabling.actual_pct == pytest.approx(80.0)
    assert security.actual_count == 1 and security.actual_pct == pytest.approx(20.0)


# --- n < MIN_CLASSIFIED_FOR_PERCENTAGE renders no percentage at all (AC2/AC3) --


def test_balance_below_min_classified_renders_no_percentage():
    tasks = [
        task("t1", outcome_ref="O-1"),
        task("t2", outcome_ref="O-1"),
        task("t3", outcome_ref="O-1"),
        task("t4", outcome_ref="O-1"),
    ]
    delivers = {tid: "R26.01" for tid in ("t1", "t2", "t3", "t4")}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    assert all(entry.sample_size == 4 for entry in report.balance)
    assert all(entry.actual_pct is None for entry in report.balance)
    assert all(entry.insufficient_sample is True for entry in report.balance)

    rendered = rs.format_release_report(report)
    balance_section = rendered.split("Declared vs actual balance")[1]
    for line in balance_section.strip().splitlines():
        assert "insufficient sample (n=4)" in line
        assert "declared" in line and "%" in line
        assert not re.search(r"actual\s+-?\d+(\.\d+)?%", line)
    security = next(b for b in report.balance if b.work_class == "security")
    assert security.absent is True
    security_line = next(line for line in balance_section.splitlines() if "security" in line)
    assert "ABSENT" in security_line


def test_balance_at_min_classified_renders_percentages():
    tasks = [task(f"t{i}", outcome_ref="O-1") for i in range(5)]
    delivers = {t["id"]: "R26.01" for t in tasks}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    enabling = next(b for b in report.balance if b.work_class == "enabling")
    assert enabling.sample_size == 5
    assert enabling.insufficient_sample is False
    assert enabling.actual_pct == pytest.approx(100.0)
    rendered = rs.format_release_report(report)
    assert "insufficient sample" not in rendered


# --- work class is derived through outcome_ref, never read off the task (AC4) --


def test_class_comes_from_the_outcome_never_from_the_task():
    """Even if a task carried its own notion of class, it would be ignored:
    the model's split lives on the outcome."""
    tasks = [{"id": "t1", "state": "pending", "content": {"outcome_ref": "O-1", "lane": "feature"}}]
    delivers = {"t1": "R26.01"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    enabling = next(b for b in report.balance if b.work_class == "enabling")
    feature = next(b for b in report.balance if b.work_class == "feature")
    assert enabling.actual_count == 1
    assert feature.actual_count == 0


def test_no_outcome_ref_is_unclassified_not_defaulted():
    tasks = [task("t1")]
    delivers = {"t1": "R26.01"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    assert report.unclassified_delivering == ["t1"]
    assert all(b.actual_count == 0 for b in report.balance)


def test_an_outcome_ref_naming_an_undeclared_outcome_is_unclassified():
    tasks = [task("t1", outcome_ref="O-99")]
    delivers = {"t1": "R26.01"}

    report = rs.build_release_report(charter(), tasks, delivers, [])

    assert report.unclassified_delivering == ["t1"]


# --- UNMEASURED vs NOT DELIVERED for a zero-task outcome (finding 2026-09-16) --
#
# A zero-task outcome can mean the work was never done, or that it was done
# with no `delivers` edge -- pre-REL-1, or an edge that was simply never
# written. The two call for opposite operator actions and must never render
# the same way.


def test_the_two_zero_task_kinds_render_differently():
    """The bead's literal AC1 test: one outcome of each kind, fixtures only
    (CLAUDE.md decision record D7 -- no live store read), and the two must
    render differently."""
    not_delivered = rs.OutcomeStatus(id="O-1", statement="never started", work_class="enabling")
    unmeasured = rs.OutcomeStatus(
        id="O-6", statement="shipped, unbound", work_class="security", unmeasured_candidates=["t9"]
    )

    rendered_not_delivered = "\n".join(rs.format_outcome(not_delivered))
    rendered_unmeasured = "\n".join(rs.format_outcome(unmeasured))

    assert rendered_not_delivered != rendered_unmeasured
    assert "NOT DELIVERED: no work bound" in rendered_not_delivered
    assert "UNMEASURED" not in rendered_not_delivered
    assert "UNMEASURED, not NOT DELIVERED" in rendered_unmeasured
    assert "NOT DELIVERED: no work bound" not in rendered_unmeasured
    assert "t9" in rendered_unmeasured


def test_zero_task_outcome_with_no_closed_unbound_candidate_is_not_delivered():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "never started", "work_class": "enabling", "requirement_refs": []},
    ])

    report = rs.build_release_report(c, [], {}, [], now="2026-09-16")

    outcome = report.outcomes[0]
    assert outcome.task_count == 0
    assert outcome.unmeasured_candidates == []


def test_zero_task_outcome_with_a_closed_unbound_candidate_in_window_is_unmeasured():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "shipped, unbound", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [task("t9", state="done", updated_at="2026-09-01T00:00:00Z")]

    report = rs.build_release_report(c, tasks, {}, [], now="2026-09-16")

    outcome = report.outcomes[0]
    assert outcome.unmeasured_candidates == ["t9"]


def test_a_candidate_never_attaches_to_an_outcome_with_delivered_work():
    """The report never picks a winner among outcomes -- unmeasured_candidates
    is populated only where task_count is already 0."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "has work", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [
        task("t1", outcome_ref="O-1"),
        task("t9", state="done", updated_at="2026-09-01T00:00:00Z"),
    ]
    delivers = {"t1": "R26.01"}

    report = rs.build_release_report(c, tasks, delivers, [], now="2026-09-16")

    outcome = report.outcomes[0]
    assert outcome.task_count == 1
    assert outcome.unmeasured_candidates == []


def test_a_closed_task_completed_before_the_release_window_is_not_a_candidate():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [task("t9", state="done", updated_at="2026-07-01T00:00:00Z")]  # before opened_at

    report = rs.build_release_report(c, tasks, {}, [], now="2026-09-16")

    assert report.outcomes[0].unmeasured_candidates == []


def test_a_closed_task_with_a_delivers_edge_is_never_a_candidate_even_in_window():
    """Bound to some release already -- not the ambiguity this check exists for."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [task("t9", state="done", updated_at="2026-09-01T00:00:00Z")]
    delivers = {"t9": "R99.99"}  # bound, just to a different release

    report = rs.build_release_report(c, tasks, delivers, [], now="2026-09-16")

    assert report.outcomes[0].unmeasured_candidates == []


def test_an_archived_task_is_not_a_closed_unbound_candidate():
    """Archived means abandoned, not delivered -- must not inflate the
    candidate list with work nobody intended to ship."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [task("t9", state="archived", updated_at="2026-09-01T00:00:00Z")]

    report = rs.build_release_report(c, tasks, {}, [], now="2026-09-16")

    assert report.outcomes[0].unmeasured_candidates == []


def test_a_task_with_no_updated_at_is_not_a_candidate():
    """No completion-shaped timestamp at all -- excluded, not guessed in."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
    ])
    tasks = [task("t9", state="done")]

    report = rs.build_release_report(c, tasks, {}, [], now="2026-09-16")

    assert report.outcomes[0].unmeasured_candidates == []


def test_window_end_uses_target_at_when_the_charter_declares_one():
    c = charter(
        target_at="2026-09-05",
        outcomes=[{"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []}],
    )
    tasks = [task("t9", state="done", updated_at="2026-09-10T00:00:00Z")]  # after target_at

    report = rs.build_release_report(c, tasks, {}, [], now="2026-09-16")

    assert report.outcomes[0].unmeasured_candidates == []


# --- the waiver filter applies to both the unmeasured-candidate population
# and compute_closed_unbound (dev.finding e1703658) -------------------------


def test_waiver_filter_applied_to_both_populations():
    """The real instance: a closed, waived bead ('probe task', shape of
    8d55c554) must not appear as an UNMEASURED candidate on a zero-task
    outcome, and must still be reported by compute_closed_unbound as waived,
    not as carrying neither a delivers edge nor a waiver."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "never started", "work_class": "enabling", "requirement_refs": []},
    ])
    waived_task = task(
        "t-probe", state="superseded", release_ref_waived="probe", updated_at="2026-09-01T00:00:00Z"
    )

    report = rs.build_release_report(c, [waived_task], {}, [], now="2026-09-16")

    outcome = report.outcomes[0]
    assert outcome.unmeasured_candidates == []
    rendered = "\n".join(rs.format_outcome(outcome))
    assert "NOT DELIVERED" in rendered
    assert "UNMEASURED" not in rendered

    closed_unbound = rs.compute_closed_unbound([waived_task], {})
    assert closed_unbound.waiver_reason_counts == {"probe": 1}
    assert closed_unbound.closed_no_release_ref == []


def test_unmeasured_candidates_are_a_subset_of_the_closed_unbound_population():
    c = charter(
        target_at="2026-09-16",
        outcomes=[
            {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
        ],
    )
    tasks = [
        task("t-neither-in-window", state="done", updated_at="2026-09-01T00:00:00Z"),
        task("t-superseded-neither-in-window", state="superseded", updated_at="2026-09-04T00:00:00Z"),
        task("t-waived-in-window", state="done", release_ref_waived="spike", updated_at="2026-09-02T00:00:00Z"),
        task("t-bound-in-window", state="done", updated_at="2026-09-03T00:00:00Z"),
        task("t-neither-out-of-window", state="done", updated_at="2026-07-01T00:00:00Z"),
        task("t-open", state="pending"),
    ]
    delivers = {"t-bound-in-window": "R99.99"}
    closed_tasks = [t for t in tasks if t["state"] in rs.CLOSED_TASK_STATES]

    report = rs.build_release_report(c, tasks, delivers, [], now="2026-09-16")
    closed_unbound = rs.compute_closed_unbound(closed_tasks, delivers)

    candidates = set(report.outcomes[0].unmeasured_candidates)
    assert candidates == {"t-neither-in-window", "t-superseded-neither-in-window"}
    assert candidates <= set(closed_unbound.closed_no_release_ref)
    assert set(closed_unbound.closed_no_release_ref) == {
        "t-neither-in-window",
        "t-superseded-neither-in-window",
        "t-neither-out-of-window",
    }


# --- unassigned population and waiver reasons, counted (AC3) ----------------


def test_unassigned_population_reuses_traceabilitys_classify_population():
    """release-status must not fork a second counter (AC7)."""
    assert rs.compute_unassigned.__code__.co_names.count("classify_population") >= 1


def test_open_tasks_with_no_edge_or_waiver_are_counted():
    tasks = [
        task("t1"),  # neither
        task("t2", release_ref_waived="prototype, not shipping"),
        task("t3", outcome_ref="O-1"),  # has a delivers edge below
    ]
    delivers = {"t3": "R26.01"}

    unassigned = rs.compute_unassigned(tasks, delivers)

    assert unassigned.open_no_release_ref == ["t1"]
    assert unassigned.waiver_reason_counts == {"prototype, not shipping": 1}


def test_waiver_reasons_are_counted_by_distinct_text():
    tasks = [
        task("t1", release_ref_waived="spike"),
        task("t2", release_ref_waived="spike"),
        task("t3", release_ref_waived="ops backlog"),
    ]

    unassigned = rs.compute_unassigned(tasks, {})

    assert unassigned.waiver_reason_counts == {"spike": 2, "ops backlog": 1}


def test_done_and_archived_and_superseded_are_excluded_from_the_open_population():
    tasks = [
        task("t1", state="done"),
        task("t2", state="archived"),
        task("t3", state="superseded"),
        task("t4", state="pending"),
    ]

    open_tasks = [t for t in tasks if t["state"] not in rs.TERMINAL_TASK_STATES]
    unassigned = rs.compute_unassigned(open_tasks, {})

    assert unassigned.open_no_release_ref == ["t4"]


# --- closed-but-unbound population, alongside the open one (finding 2026-09-16) --
#
# The open-population line reads 0 on the measured board while 178 closed
# beads carry no `delivers` edge -- a reader of the open-only line alone
# would wrongly conclude the backlog is fully bound. This is an addition,
# not a redefinition: the existing open line keeps its label and meaning.


def test_compute_closed_unbound_reuses_traceabilitys_classify_population():
    assert rs.compute_closed_unbound.__code__.co_names.count("classify_population") >= 1


def test_closed_and_unbound_tasks_are_counted_separately_from_open():
    tasks = [
        task("t1", state="done"),          # closed, unbound
        task("t2", state="superseded"),    # closed, unbound
        task("t3", state="archived"),      # closed but abandoned -- excluded
        task("t4", state="pending"),       # open, unbound -- not this population
        task("t5", state="done", outcome_ref="O-1"),  # closed, but bound below
    ]
    delivers = {"t5": "R26.01"}
    closed_tasks = [t for t in tasks if t["state"] in rs.CLOSED_TASK_STATES]

    closed_unbound = rs.compute_closed_unbound(closed_tasks, delivers)

    assert sorted(closed_unbound.closed_no_release_ref) == ["t1", "t2"]


def test_closed_unbound_waiver_reasons_are_counted_by_distinct_text():
    tasks = [
        task("t1", state="done", release_ref_waived="spike"),
        task("t2", state="superseded", release_ref_waived="spike"),
    ]
    closed_tasks = [t for t in tasks if t["state"] in rs.CLOSED_TASK_STATES]

    closed_unbound = rs.compute_closed_unbound(closed_tasks, {})

    assert closed_unbound.waiver_reason_counts == {"spike": 2}
    assert closed_unbound.closed_no_release_ref == []


def test_the_open_population_label_and_meaning_are_unchanged_by_the_addition():
    """AC2, literally: the existing line must not be redefined."""
    rendered = rs.format_unassigned(rs.UnassignedPopulation())
    assert rendered.startswith("Unassigned population (open dev.task, no delivers edge)")


def test_closed_unbound_section_has_its_own_distinct_label():
    rendered = rs.format_closed_unbound(rs.ClosedUnboundPopulation())
    assert rendered.startswith("Closed-but-unbound population (done/superseded dev.task, no delivers edge)")


# --- observation verdict as-at-opened_at vs latest, and staleness (AC5) -----


def _observation(requirement_id, criterion_id, measured_at, verdict):
    return {
        "content": {
            "requirement_id": requirement_id,
            "acceptance_criterion_id": criterion_id,
            "measured_at": measured_at,
            "verdict": verdict,
        }
    }


def test_movement_is_reported_when_a_fresh_observation_exists():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": ["PC-FAC-001/AC-5"]},
    ])
    observations = [
        _observation("PC-FAC-001", "AC-5", "2026-08-06", "absent"),
        _observation("PC-FAC-001", "AC-5", "2026-08-26", "pass"),
    ]

    report = rs.build_release_report(c, [], {}, observations)

    status = report.criteria[0]
    assert status.as_of_opened.value == "absent"
    assert status.latest.value == "pass"
    assert status.stale is False
    assert status.changed is True


def test_a_newest_observation_predating_opened_at_is_not_re_measured():
    """AC5's literal wording: say 'not re-measured', never report the stale
    verdict as current."""
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": ["PC-FAC-001/AC-5"]},
    ])
    observations = [_observation("PC-FAC-001", "AC-5", "2026-08-06", "absent")]

    report = rs.build_release_report(c, [], {}, observations)

    status = report.criteria[0]
    assert status.stale is True
    rendered = rs.format_criterion(status)
    assert "not re-measured" in rendered
    assert "as current" not in rendered  # sanity: no accidental "reported as current" phrasing


def test_no_observation_at_all_is_reported_honestly():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": ["PC-NONE-001/AC-1"]},
    ])

    report = rs.build_release_report(c, [], {}, [])

    status = report.criteria[0]
    assert status.latest is None
    assert "no arch.requirement_conformance recorded" in rs.format_criterion(status)


def test_unchanged_verdict_is_reported_as_unchanged():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": ["PC-FAC-001/AC-5"]},
    ])
    observations = [
        _observation("PC-FAC-001", "AC-5", "2026-08-06", "pass"),
        _observation("PC-FAC-001", "AC-5", "2026-08-26", "pass"),
    ]

    report = rs.build_release_report(c, [], {}, observations)

    status = report.criteria[0]
    assert status.changed is False
    assert "[unchanged]" in rs.format_criterion(status)


def test_a_malformed_reference_is_unknown_not_no_observation_recorded():
    """A citation that fails REFERENCE_RE cannot be looked up at all -- that is
    a different fact from a well-formed reference with zero recorded
    conformances, and it must not render the same way."""
    status = rs._criterion_status("not a real ref", [], "2026-01-01")

    assert status.latest is None
    assert status.unmeasurable is not None
    rendered = rs.format_criterion(status)
    assert "UNKNOWN" in rendered
    assert "no arch.requirement_conformance recorded" not in rendered


def test_a_well_formed_reference_with_no_conformances_is_not_unknown():
    """The sibling case: zero recorded conformances is a real, verified
    absence (like BalanceEntry.absent), not a computation failure."""
    status = rs._criterion_status("PC-FAKE-999/AC-1", [], "2026-01-01")

    assert status.unmeasurable is None
    assert "no arch.requirement_conformance recorded" in rs.format_criterion(status)


def test_requirement_level_citation_does_not_match_a_criterion_observation():
    c = charter(outcomes=[
        {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": ["PC-FAC-001"]},
    ])
    observations = [_observation("PC-FAC-001", "AC-5", "2026-08-06", "pass")]

    report = rs.build_release_report(c, [], {}, observations)

    assert report.criteria[0].latest is None


# --- it reports; it does not gate (AC6) --------------------------------------


class _FakeReader:
    def __init__(self, beads=None, links=None):
        self.beads = beads or {}
        self.links = links or {}

    def list_beads(self, namespace, type_, **params):
        key = (namespace, type_)
        results = self.beads.get(key, [])
        content_ref = params.get("content_ref")
        if content_ref is not None:
            results = [b for b in results if (b.get("content") or {}).get("ref") == content_ref]
        return results

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return self.links.get(bead_id, [])


def _write_charter(tmp_path, data):
    releases = tmp_path / "docs" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    (releases / f"{data['ref']}.json").write_text(json.dumps(data))
    (tmp_path / "docs" / "requirements").mkdir(parents=True, exist_ok=True)


def test_exit_code_is_zero_even_with_absent_classes_and_unassigned_work(tmp_path, monkeypatch, capsys):
    _write_charter(tmp_path, charter())
    reader = _FakeReader(
        beads={
            ("dev", "task"): [{"id": "t1", "state": "pending", "content": {}}],
            ("arch", "release"): [],
            ("arch", "requirement_conformance"): [],
        }
    )

    code = rs.main(
        ["--repo", str(tmp_path)],
        reader_factory=lambda base, key: reader,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "ABSENT" in out
    assert "t1" in out


def test_main_prints_the_closed_unbound_section_alongside_the_open_one(tmp_path, capsys):
    _write_charter(tmp_path, charter())
    reader = _FakeReader(
        beads={
            ("dev", "task"): [
                {"id": "t1", "state": "pending", "content": {}},  # open, unbound
                {"id": "t2", "state": "done", "content": {}},  # closed, unbound
            ],
            ("arch", "release"): [],
            ("arch", "requirement_conformance"): [],
        }
    )

    code = rs.main(["--repo", str(tmp_path)], reader_factory=lambda base, key: reader)

    assert code == 0
    out = capsys.readouterr().out
    assert "Closed-but-unbound population (done/superseded dev.task, no delivers edge)" in out
    assert "- t2" in out.split("Closed-but-unbound population")[1]
    assert "- t2" not in out.split("Closed-but-unbound population")[0].split("Unassigned population")[1]


class _WriteRefusingReader(_FakeReader):
    """Every write-shaped method a real substrate client exposes
    (`Substrate` in substrate_client.py), refusing each one. Proves AC3 by
    construction -- if release-status.py ever calls a write, this reader
    fails the test, rather than the test inspecting the report's text for
    the absence of a claim."""

    def _refuse(self, *args, **kwargs):
        raise AssertionError("release-status.py must never write to the substrate")

    create = _refuse
    patch = _refuse
    add_link = _refuse
    delete_link = _refuse
    delete_bead = _refuse


def test_the_report_never_writes_to_the_substrate(tmp_path, capsys):
    _write_charter(tmp_path, charter())
    reader = _WriteRefusingReader(
        beads={
            ("dev", "task"): [
                {"id": "t1", "state": "pending", "content": {}},
                {"id": "t2", "state": "done", "content": {}},
            ],
            ("arch", "release"): [],
            ("arch", "requirement_conformance"): [],
        }
    )

    code = rs.main(["--repo", str(tmp_path)], reader_factory=lambda base, key: reader)

    assert code == 0


def test_substrate_unreachable_says_so_and_exits_nonzero(tmp_path, capsys):
    _write_charter(tmp_path, charter())

    class _BrokenReader:
        def list_beads(self, namespace, type_, **params):
            raise RuntimeError("SUBSTRATE_URL and SUBSTRATE_API_KEY must be set")

        def list_links(self, bead_id, **params):
            raise RuntimeError("unreachable")

    code = rs.main(["--repo", str(tmp_path)], reader_factory=lambda base, key: _BrokenReader())

    assert code != 0
    assert "could not run" in capsys.readouterr().err


def test_a_bad_charter_stops_the_run_rather_than_reporting_an_empty_population(tmp_path, capsys):
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "docs" / "releases" / "R26.01.json").write_text("{ not json")
    (tmp_path / "docs" / "requirements").mkdir(parents=True)

    code = rs.main(["--repo", str(tmp_path)], reader_factory=lambda base, key: _FakeReader())

    assert code != 0
    assert "could not run" in capsys.readouterr().err


def test_live_data_maps_delivers_edges_through_the_release_bead(tmp_path):
    release_bead = {"id": "release-bead-1", "content": {"ref": "R26.01"}}
    reader = _FakeReader(
        beads={
            ("dev", "task"): [{"id": "t1", "state": "pending", "content": {"outcome_ref": "O-1"}}],
            ("arch", "release"): [release_bead],
            ("arch", "requirement_conformance"): [],
        },
        links={"release-bead-1": [{"source_id": "t1", "target_id": "release-bead-1", "link_type": "delivers"}]},
    )

    tasks, delivers, observations = rs.gather_live_data(reader, [charter()])

    assert delivers == {"t1": "R26.01"}
    assert tasks[0]["id"] == "t1"


def test_a_release_not_yet_mirrored_delivers_nothing_rather_than_erroring(tmp_path):
    reader = _FakeReader(beads={("dev", "task"): [], ("arch", "release"): [], ("arch", "requirement_conformance"): []})

    tasks, delivers, observations = rs.gather_live_data(reader, [charter()])

    assert delivers == {}


# --- --release must not narrow the unassigned population (dev.finding 47c133c3) ---


def _reader_for_two_releases():
    """Two releases, R26.01 and R26.05, each with one task delivering to it.
    Mirrors the measured shape: a task bound to R26.05 must not show up as
    unassigned when the report is scoped to R26.01, and vice versa."""
    release_a = {"id": "release-bead-a", "content": {"ref": "R26.01"}}
    release_b = {"id": "release-bead-b", "content": {"ref": "R26.05"}}
    return _FakeReader(
        beads={
            ("dev", "task"): [
                {"id": "t-a", "state": "pending", "content": {"outcome_ref": "O-1"}},
                {"id": "t-b", "state": "pending", "content": {"outcome_ref": "O-1"}},
                {"id": "t-unassigned", "state": "pending", "content": {}},
            ],
            ("arch", "release"): [release_a, release_b],
            ("arch", "requirement_conformance"): [],
        },
        links={
            "release-bead-a": [{"source_id": "t-a", "target_id": "release-bead-a", "link_type": "delivers"}],
            "release-bead-b": [{"source_id": "t-b", "target_id": "release-bead-b", "link_type": "delivers"}],
        },
    )


def _write_two_charters(tmp_path):
    _write_charter(tmp_path, charter(ref="R26.01"))
    (tmp_path / "docs" / "releases" / "R26.05.json").write_text(json.dumps(charter(ref="R26.05")))


def test_unassigned_count_is_invariant_under_release_flag(tmp_path, capsys):
    """FACT AT FILING: unfiltered reported 0 while --release R26.01 reported
    30 and --release R26.05 reported 13 against the same substrate snapshot.
    The label promises "no delivers edge to any release" -- that population
    must not change size depending on which release is being planned."""
    _write_two_charters(tmp_path)

    code_all = rs.main(["--repo", str(tmp_path)], reader_factory=lambda base, key: _reader_for_two_releases())
    out_all = capsys.readouterr().out

    code_a = rs.main(
        ["--repo", str(tmp_path), "--release", "R26.01"],
        reader_factory=lambda base, key: _reader_for_two_releases(),
    )
    out_a = capsys.readouterr().out

    code_b = rs.main(
        ["--repo", str(tmp_path), "--release", "R26.05"],
        reader_factory=lambda base, key: _reader_for_two_releases(),
    )
    out_b = capsys.readouterr().out

    assert code_all == code_a == code_b == 0
    for out in (out_all, out_a, out_b):
        assert "carrying neither a delivers edge nor a waiver: 1" in out
        assert "- t-unassigned" in out
        # The tell from the finding: a task delivering elsewhere must never
        # be listed as unassigned just because --release named a different
        # release.
        assert "t-a" not in out.split("Unassigned population")[1]
        assert "t-b" not in out.split("Unassigned population")[1]


def test_a_task_delivering_a_different_release_does_not_leak_into_this_ones_report(tmp_path, capsys):
    """The exact shape that was wrong: a task delivering to A ("t-a"), reported
    with --release B, must not appear anywhere in B's run -- not in B's own
    outcome/balance sections (it never did -- build_release_report already
    scopes by ref) and not in the unassigned population (it did, before the
    fix, because the delivers map held only B's edges)."""
    _write_two_charters(tmp_path)

    rs.main(
        ["--repo", str(tmp_path), "--release", "R26.05"],
        reader_factory=lambda base, key: _reader_for_two_releases(),
    )
    out = capsys.readouterr().out

    assert "R26.01" not in out
    assert "t-a" not in out


def test_per_release_report_is_unchanged_by_widening_the_delivers_map(tmp_path, capsys):
    """AC: the named release's own outcome/balance/criteria sections must be
    byte-identical to what a single-release run without any other charter on
    disk would produce -- widening `delivers` to all charters must not leak
    another release's tasks into this one's counts."""
    _write_charter(tmp_path, charter(ref="R26.01"))
    reader_single = _FakeReader(
        beads={
            ("dev", "task"): [{"id": "t-a", "state": "pending", "content": {"outcome_ref": "O-1"}}],
            ("arch", "release"): [{"id": "release-bead-a", "content": {"ref": "R26.01"}}],
            ("arch", "requirement_conformance"): [],
        },
        links={"release-bead-a": [{"source_id": "t-a", "target_id": "release-bead-a", "link_type": "delivers"}]},
    )
    rs.main(["--repo", str(tmp_path), "--release", "R26.01"], reader_factory=lambda base, key: reader_single)
    out_single_charter = capsys.readouterr().out
    single_release_report = out_single_charter.split("Unassigned population")[0]

    _write_two_charters(tmp_path)
    rs.main(
        ["--repo", str(tmp_path), "--release", "R26.01"],
        reader_factory=lambda base, key: _reader_for_two_releases(),
    )
    out_two_charters = capsys.readouterr().out
    two_charter_release_report = out_two_charters.split("Unassigned population")[0]

    assert single_release_report == two_charter_release_report
