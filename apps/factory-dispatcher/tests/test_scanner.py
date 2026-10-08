"""The lane scanner reports what it would file, and files nothing.

Sprint 12.1. The dry-run is not ceremony: a scanner that files five
well-formed, correctly-scoped, POINTLESS tasks a night passes every other check
in sprint 12, and this report is the only place that gets caught.

Fixtures are used rather than the live registries on purpose. Conformance
verdicts are dated snapshots that sprints burn down, so a test asserting "14
candidates" against the real registry would fail every time the platform got
better — and would be silenced rather than fixed.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner  # noqa: E402
import file_task  # noqa: E402
import dispatch  # noqa: E402
from substrate import Substrate  # noqa: E402


def registry(tmp_path, requirements, name="platform-requirements.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"registry": {"id": "REG-TEST"}, "requirements": requirements}))
    return tmp_path


def requirement(
    rid, conformances, implementation, title="a requirement", status="partial",
    measured_at="2026-08-06", measured_revision=None,
):
    criteria = []
    for i, c in enumerate(conformances, 1):
        ac = {"id": f"AC-{i}", "measured_at": measured_at, "conformance": c}
        if measured_revision is not None:
            ac["measured_revision"] = measured_revision
        criteria.append(ac)
    return {
        "id": rid,
        "title": title,
        "status": status,
        "rationale": "because it matters",
        "implementation": implementation,
        "acceptance_criteria": criteria,
    }


#: Default for tests that are not about staleness: the code has never changed,
#: so no verdict can be out of date and the test stays about its own subject.
NEVER_CHANGED = lambda _paths: None  # noqa: E731


def date_lookup_must_not_run(_paths):
    raise AssertionError("revision-measured criteria must not use the date lookup")


def finding_for(tmp_path, last_changed_fn=NEVER_CHANGED, scan_kwargs=None, **kwargs):
    found = scanner.scan_requirement_gaps(
        registry(tmp_path, [requirement(**kwargs)]),
        last_changed_fn,
        **(scan_kwargs or {}),
    )
    assert len(found) == 1
    return found[0]


def revision_lookup(order, last_touching):
    return {
        "last_changed_revision_fn": lambda _paths: last_touching,
        "revision_exists_fn": lambda rev: rev in order,
        "is_ancestor_fn": (
            lambda ancestor, descendant: order[ancestor] <= order[descendant]
        ),
    }


# --- what the scanner notices -------------------------------------------------


def test_a_requirement_with_no_failing_criteria_produces_nothing(tmp_path):
    d = registry(tmp_path, [requirement("PC-X-001", ["pass", "pass"], ["apps/a/b.py"])])

    assert scanner.scan_requirement_gaps(d) == []


def test_unverified_is_not_treated_as_failing(tmp_path):
    # The registry legend is explicit: unverified means "not exercised; do NOT
    # treat as passing". It equally does not mean failing, and a scanner that
    # filed on it would manufacture work from an absence of evidence.
    d = registry(tmp_path, [requirement("PC-X-001", ["unverified"], ["apps/a/b.py"])])

    assert scanner.scan_requirement_gaps(d) == []


def test_failing_criteria_are_grouped_per_requirement_not_per_criterion(tmp_path):
    finding = finding_for(tmp_path, rid="PC-X-001", conformances=["fail", "fail", "fail"],
                          implementation=["apps/a/b.py"])

    # Three failing criteria on one requirement are one piece of work.
    assert finding.requirement_refs == ("PC-X-001/AC-1", "PC-X-001/AC-2", "PC-X-001/AC-3")


def test_lifeops_requirements_are_skipped(tmp_path):
    d = registry(
        tmp_path,
        [
            requirement("LO-CAT-002", ["fail", "fail"], ["apps/mcp-hub/x.py"]),
            requirement("PC-X-001", ["fail"], ["apps/a/b.py"]),
        ],
    )

    found = scanner.scan_requirement_gaps(d)

    # Product behaviour needs a lane and a scope that a scanner reading a
    # registry does not have. Declining beats guessing.
    assert [f.dedupe_key for f in found] == ["requirement-gaps:PC-X-001"]


def test_the_finding_cites_the_criteria_it_came_from(tmp_path):
    finding = finding_for(tmp_path, rid="PC-X-001", conformances=["pass", "fail"],
                          implementation=["apps/a/b.py"])

    # Self-citing is why this source was chosen first: the generator cannot
    # invent a plausible-looking reference, which is what the traceability
    # refusal exists to catch.
    assert finding.requirement_refs == ("PC-X-001/AC-2",)


def test_a_line_suffix_is_stripped_from_an_implementation_path(tmp_path):
    finding = finding_for(tmp_path, rid="PC-X-001", conformances=["fail"],
                          implementation=["apps/substrate/src/schemas.py:246"])

    assert finding.scope_paths == ("apps/substrate/src/schemas.py",)


# --- what filing would say ----------------------------------------------------


def test_a_candidate_reported_fileable_really_would_file(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])

    candidate = scanner.assess(finding)

    assert candidate.fileable
    # Assessed through the real filing path, not a second copy of the rule.
    scanner.build_content(candidate.spec)


def test_a_requirement_scoped_entirely_to_forbidden_paths_is_refused(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-X-001", conformances=["fail"],
        implementation=[".github/workflows/build-substrate.yml", "infrastructure/k8s/"],
    )

    candidate = scanner.assess(finding)

    # CI-scoped work is the Operator's alone. A scanner reading only the registry would
    # happily propose it, and every such task would fail.
    assert not candidate.fileable
    assert not candidate.doable
    assert candidate.blocked_paths


def test_a_partially_forbidden_requirement_is_flagged_as_narrowed(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["GEMINI.md", ".github/workflows/lint.yml"],
    )

    candidate = scanner.assess(finding)

    # The most dangerous candidate shape: it files cleanly and may have had the
    # actual work removed from it, leaving a task completable without ever
    # addressing the requirement it cites.
    assert candidate.fileable
    assert candidate.narrowed
    assert candidate.allowed_paths == ("GEMINI.md",)
    assert candidate.blocked_paths == (".github/workflows/lint.yml",)


def test_a_requirement_scoped_entirely_to_forbidden_paths_produces_no_candidate(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-DEL-001", conformances=["fail"],
        implementation=[".github/workflows/build-substrate.yml", "infrastructure/k8s/"],
    )

    report = scanner.dry_run(one_scanner(finding), store=FakeStore([]), suppressions={})

    # Not produced as a candidate at all — this is a real gap, not a spec that
    # would fail a fixable filing check, so it must not count against the
    # well-formed ratio the way a defective spec would.
    assert report.candidates == ()
    assert [u.finding.dedupe_key for u in report.scope_forbidden] == [finding.dedupe_key]
    assert report.underivable == ()

    rendered = scanner.render_report(report)
    assert finding.dedupe_key in rendered
    assert "no path in scope the factory may touch" in rendered


def test_a_requirement_with_no_derivable_scope_is_reported_as_underivable(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-X-001", conformances=["fail"],
        implementation=["see the design doc for details"],
    )
    assert finding.scope_paths == ()

    report = scanner.dry_run(one_scanner(finding), store=FakeStore([]), suppressions={})

    # "Could not work out what to do" is not the same claim as "nothing to
    # do" — an empty scope must never be reported as though it were the
    # forbidden-scope case above.
    assert report.candidates == ()
    assert report.scope_forbidden == ()
    assert [u.finding.dedupe_key for u in report.underivable] == [finding.dedupe_key]

    rendered = scanner.render_report(report)
    assert "UNDERIVABLE" in rendered
    assert finding.dedupe_key in rendered


def test_a_partially_forbidden_requirement_still_produces_a_well_formed_candidate(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["GEMINI.md", ".github/workflows/lint.yml"],
    )

    report = scanner.dry_run(one_scanner(finding), store=FakeStore([]), suppressions={})

    # A requirement with at least one permitted path is a real candidate, not
    # an ungenerated one — only a scope with nothing permitted is diverted.
    assert report.ungenerated == ()
    assert len(report.candidates) == 1
    assert report.candidates[0].fileable
    assert report.candidates[0].narrowed


def test_an_unnarrowed_candidate_is_not_flagged_as_narrowed(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])

    assert not scanner.assess(finding).narrowed


def test_a_generated_spec_cannot_widen_its_own_boundary(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])

    content = scanner.build_content(scanner.assess(finding).spec)

    for always in scanner.FORBIDDEN_ALWAYS:
        assert always in content["scope"]["forbidden_paths"]
    assert "apps/factory-dispatcher/tasks/**" in content["scope"]["forbidden_paths"]


def test_scanner_forbids_docs_releases(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-X-001", conformances=["fail"],
        implementation=["docs/releases/policy/health-policy.json"],
    )

    candidate = scanner.assess(finding)

    # The charters and the health policy are fenced the same way CI-scoped
    # work is: a real gap no factory task may close, not a fixable spec
    # defect.
    assert not candidate.fileable
    assert candidate.blocked_paths


def test_scanner_content_has_no_class_of_service(tmp_path):
    """Assert on the exact dict ``post_task`` would POST, not on the earlier
    ``finding_to_spec`` shape -- a leak introduced inside ``content_for_filing``
    itself would pass unnoticed if we stopped at the pre-filing spec.
    """
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])

    candidate = scanner.assess(finding)
    assert candidate.fileable

    content = scanner.content_for_filing(candidate)

    for field in file_task.MARKER_FIELDS:
        assert field not in content


def test_scanner_forbidden_always_is_pinned():
    """Both copies converging on the wrong list is invisible to byte-parity
    checks (item 3 of the requeue note) -- pin the literal contents here.
    """
    assert scanner.FORBIDDEN_ALWAYS[0] == ".github/workflows/**"
    assert "docs/releases/**" in scanner.FORBIDDEN_ALWAYS


# --- release traceability: a waiver, never a guessed release -------------------
#
# scanner.py never calls file_task.main(), so the filing refusal cannot gate
# an auto-filed candidate. An automated filer that picked an open release for
# every candidate would recreate the pile the release-traceability mechanism
# exists to end, so every generated spec defaults to a written waiver instead.


def test_a_generated_spec_carries_a_release_waiver_not_a_release(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])

    spec = scanner.assess(finding).spec

    assert spec["release_ref_waived"] == scanner.SCANNER_RELEASE_WAIVER
    assert "release_ref" not in spec


def test_the_filed_content_carries_the_release_waiver(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/factory-dispatcher/guards.py"])
    candidate = scanner.assess(finding)

    content = scanner.content_for_filing(candidate)

    assert content["release_ref_waived"] == scanner.SCANNER_RELEASE_WAIVER


# --- staleness: a verdict is a dated snapshot, not a standing fact -----------


def test_a_verdict_older_than_the_code_it_judges_is_skipped(tmp_path):
    finding = finding_for(
        tmp_path, last_changed_fn=lambda _p: "2026-08-09",
        rid="PC-X-001", conformances=["fail"], implementation=["apps/a/b.py"],
        measured_at="2026-08-06",
    )

    # The case that forced this: the scanner proposed PC-FAC-006 on 2026-08-09,
    # work that had shipped that morning.
    assert finding.stale_reason
    assert "2026-08-06" in finding.stale_reason and "2026-08-09" in finding.stale_reason
    assert not scanner.assess(finding).doable


def test_a_verdict_newer_than_the_last_change_is_proposed(tmp_path):
    finding = finding_for(
        tmp_path, last_changed_fn=lambda _p: "2026-08-01",
        rid="PC-SUB-001", conformances=["fail"], implementation=["apps/factory-dispatcher/guards.py"],
        measured_at="2026-08-06",
    )

    assert finding.stale_reason == ""
    assert scanner.assess(finding).doable


def test_a_same_day_verdict_measured_at_a_current_revision_is_not_stale(tmp_path):
    finding = finding_for(
        tmp_path,
        last_changed_fn=date_lookup_must_not_run,
        scan_kwargs=revision_lookup({"touching": 1, "measured": 2}, "touching"),
        rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/factory-dispatcher/guards.py"],
        measured_at="2026-08-06", measured_revision="measured",
    )

    # The date rule would call this stale. A recorded revision makes the order
    # decidable, so a measurement at or after the touching commit is current.
    assert finding.stale_reason == ""
    assert scanner.assess(finding).doable


def test_a_verdict_measured_at_a_revision_older_than_touching_code_is_stale(tmp_path):
    finding = finding_for(
        tmp_path,
        last_changed_fn=date_lookup_must_not_run,
        scan_kwargs=revision_lookup({"measured": 1, "touching": 2}, "touching"),
        rid="PC-X-001", conformances=["fail"], implementation=["apps/a/b.py"],
        measured_at="2026-08-01", measured_revision="measured",
    )

    assert finding.stale_reason
    assert "revision rule" in finding.stale_reason
    assert "measured" in finding.stale_reason and "touching" in finding.stale_reason


def test_a_verdict_with_an_unresolvable_measured_revision_is_stale(tmp_path):
    finding = finding_for(
        tmp_path,
        last_changed_fn=date_lookup_must_not_run,
        scan_kwargs=revision_lookup({"touching": 1}, "touching"),
        rid="PC-X-001", conformances=["fail"], implementation=["apps/a/b.py"],
        measured_at="2026-08-01", measured_revision="missing",
    )

    assert finding.stale_reason
    assert "revision rule" in finding.stale_reason
    assert "cannot be resolved" in finding.stale_reason


def test_a_verdict_with_no_measured_revision_still_uses_the_date_rule(tmp_path):
    finding = finding_for(
        tmp_path, last_changed_fn=lambda _p: "2026-08-06",
        rid="PC-X-001", conformances=["fail"], implementation=["apps/a/b.py"],
        measured_at="2026-08-06",
    )

    # At date granularity the order is unknowable, and the errors are not
    # symmetrical: proposing stale work burns a whole attempt discovering there
    # is nothing to do, while skipping a fresh finding costs a later notice.
    assert finding.stale_reason
    assert "date rule" in finding.stale_reason


def test_the_oldest_failing_verdict_decides(tmp_path):
    d = registry(tmp_path, [{
        "id": "PC-X-001", "title": "t", "status": "partial", "rationale": "r",
        "implementation": ["apps/a/b.py"],
        "acceptance_criteria": [
            {"id": "AC-1", "measured_at": "2026-08-01", "conformance": "fail"},
            {"id": "AC-2", "measured_at": "2026-08-09", "conformance": "fail"},
        ],
    }])

    found = scanner.scan_requirement_gaps(d, lambda _p: "2026-08-05")

    # The finding rests on both criteria. One predates the change, so the
    # finding as a whole is suspect.
    assert found[0].stale_reason


def test_a_verdict_with_no_measured_at_is_skipped(tmp_path):
    d = registry(tmp_path, [{
        "id": "PC-X-001", "title": "t", "status": "partial", "rationale": "r",
        "implementation": ["apps/a/b.py"],
        "acceptance_criteria": [{"id": "AC-1", "conformance": "fail"}],
    }])

    found = scanner.scan_requirement_gaps(d, NEVER_CHANGED)

    assert "no measured_at" in found[0].stale_reason


def test_an_unanswerable_lookup_does_not_empty_the_scan(tmp_path):
    finding = finding_for(
        tmp_path, last_changed_fn=lambda _p: None,
        rid="PC-SUB-001", conformances=["fail"], implementation=["apps/factory-dispatcher/guards.py"],
        measured_at="2026-08-06",
    )

    # git cannot always answer — no matching path, no repository. Refusing to
    # propose anything in that case would silently produce an empty report that
    # reads as "nothing to do".
    assert finding.stale_reason == ""


def test_stale_candidates_are_reported_rather_than_silently_dropped(tmp_path):
    d = registry(tmp_path, [requirement("PC-X-001", ["fail"], ["apps/a/b.py"])])
    report = scanner.dry_run({"t": lambda: scanner.scan_requirement_gaps(d, lambda _p: "2026-08-09")})

    rendered = scanner.render_report(report)

    # A silent skip is a hole, the same way a silent waiver is.
    assert len(report.stale) == 1
    assert "SKIPPED" in rendered
    assert "Re-measure before proposing" in rendered


@pytest.mark.skipif(
    not (scanner.REPO_ROOT / ".git").exists(),
    reason=(
        "requires a .git directory to answer a real `git log` lookup against "
        "this checkout; absent in a tree produced by `git archive` (e.g. a "
        "gate-review export)"
    ),
)
def test_last_changed_answers_from_git_for_a_real_path():
    # The one test that exercises the real lookup rather than an injection.
    changed = scanner.last_changed(("apps/factory-dispatcher/guards.py",))

    assert changed and len(changed) == 10 and changed[4] == "-"


def test_last_changed_is_none_for_a_path_git_does_not_know():
    assert scanner.last_changed(("apps/there-is-no-such-directory/x.py",)) is None


def test_against_the_real_registry_every_finding_cites_something_that_resolves():
    """The property that made this a safe first signal source.

    The scanner reads the same registry directory the filing gate resolves
    against, so a finding derived from requirement X cites X and X exists by
    construction. A generator that invented plausible-looking references is
    exactly what the traceability refusal was built to catch, and this source
    cannot produce one.

    Asserted as a property, never as a count: the counts are dated conformance
    verdicts that sprints burn down.
    """
    findings = scanner.scan_requirement_gaps()

    for finding in findings:
        candidate = scanner.assess(finding)
        assert "resolve to nothing" not in candidate.refusal, finding.dedupe_key


# --- the dry run is a dry run -------------------------------------------------


def test_the_dry_run_makes_no_network_call(monkeypatch, tmp_path):
    def explode(*args, **kwargs):
        raise AssertionError("the scanner attempted network I/O during a dry run")

    import httpx

    monkeypatch.setattr(httpx, "request", explode)

    d = registry(tmp_path, [requirement("PC-X-001", ["fail"], ["apps/a/b.py"])])
    report = scanner.dry_run({"t": lambda: scanner.scan_requirement_gaps(d)})

    assert len(report.candidates) == 1


def test_there_is_no_flag_that_files_anything():
    # The ability to write is absent rather than defaulted off. A dry run you
    # can turn off is not a dry run.
    with pytest.raises(SystemExit):
        scanner.main(["--file"])
    with pytest.raises(SystemExit):
        scanner.main(["--no-dry-run"])


def test_the_scan_invocation_stays_the_default_command(monkeypatch, capsys, tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    monkeypatch.setattr(scanner, "SCANNERS", {"stub": lambda: [finding]})

    assert scanner.main(["--no-queue"]) == 0
    legacy = capsys.readouterr().out
    assert scanner.main(["scan", "--no-queue"]) == 0
    explicit = capsys.readouterr().out

    assert explicit == legacy


def test_the_exit_code_reports_that_the_scan_ran_not_what_it_found(capsys):
    # A scanner that failed the build because the platform has gaps would be
    # switched off inside a week.
    assert scanner.main([]) == 0
    assert "DRY RUN" in capsys.readouterr().out


# --- the report ---------------------------------------------------------------


def test_duplicate_dedupe_keys_are_counted(tmp_path):
    d = registry(tmp_path, [requirement("PC-X-001", ["fail"], ["apps/a/b.py"])])
    report = scanner.dry_run(
        {
            "a": lambda: scanner.scan_requirement_gaps(d),
            "b": lambda: scanner.scan_requirement_gaps(d),
        }
    )

    # 12.3 owns dedup against OPEN TASKS; this only proves the seam exists.
    assert report.duplicate_keys == ("requirement-gaps:PC-X-001",)


def test_the_report_says_nothing_was_filed(tmp_path):
    d = registry(tmp_path, [requirement("PC-X-001", ["fail"], ["apps/a/b.py"])])

    rendered = scanner.render_report(scanner.dry_run({"t": lambda: scanner.scan_requirement_gaps(d)}))

    assert "Nothing was filed and nothing can be" in rendered


def test_the_report_names_what_12_3_still_owes(tmp_path):
    d = registry(tmp_path, [requirement("PC-X-001", ["fail"], ["apps/a/b.py"])])

    rendered = scanner.render_report(scanner.dry_run({"t": lambda: scanner.scan_requirement_gaps(d)}))

    assert "dedup" in rendered and "rate limit" in rendered and "suppression" in rendered


def test_the_intent_tells_the_worker_the_verdict_may_be_stale(tmp_path):
    finding = finding_for(tmp_path, rid="PC-X-001", conformances=["fail"],
                          implementation=["apps/a/b.py"])

    # Conformance verdicts are dated snapshots. A task generated from one must
    # not assert it is still true at HEAD.
    assert "Confirm the criterion still fails at HEAD" in finding.intent


# --- the filing controls ------------------------------------------------------


class FakeStore:
    """Only what open_task_state reads, so a test states its queue directly."""

    def __init__(self, tasks):
        self._tasks = tasks

    def list_tasks(self, state=None):
        return list(self._tasks)


class FakeFilingStore(FakeStore):
    """Read/write double for scanner filing; no network, strict payload capture."""

    def __init__(self, tasks=()):
        super().__init__(tasks)
        self.posts = []

    def _request(self, method, path, **kwargs):
        assert method == "POST"
        assert path == "/beads"
        self.posts.append(kwargs["json"])
        return {"id": f"filed-{len(self.posts)}"}


def open_task(refs=(), state="pending", scanner_key=None):
    content = {"requirement_refs": list(refs)}
    if scanner_key:
        content[scanner.SCANNER_KEY_FIELD] = scanner_key
    return {"id": "t-1", "state": state, "content": content}


def spec_file(tasks_dir, name, title, done=False):
    directory = tasks_dir / "done" if done else tasks_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({"title": title}))
    return path


def task_with_title(title, state="pending", task_id="task-1"):
    return {"id": task_id, "state": state, "content": {"title": title}}


FIXED_NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)
OLD_SPEC = FIXED_NOW - timedelta(days=21)
FRESH_SPEC = FIXED_NOW - timedelta(days=1)


def one_scanner(finding):
    return {"stub": lambda: [finding]}


def assert_stopped_by_a_control(report):
    """The candidate is well-formed; a control is what stopped it.

    Without this these tests pass just as happily when the spec is refused for
    an unrelated reason — an unresolvable requirement reference, say — and would
    keep passing if dedup were deleted.
    """
    assert report.candidates, "expected a candidate to assess"
    for candidate in report.candidates:
        assert candidate.fileable, f"spec was refused, not deduped: {candidate.refusal}"
    assert report.would_file == ()


def test_spec_record_reports_top_level_specs_whose_bead_is_done(tmp_path):
    spec_file(tmp_path, "shipped.json", "Shipped work")
    store = FakeStore([task_with_title("Shipped work", state="done", task_id="done-1")])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.bead_state) for i in report.spec_record_issues] == [
        ("queued-spec-done", "Shipped work", "done")
    ]
    rendered = scanner.render_report(report)
    assert "SPEC RECORD DRIFT" in rendered
    assert "top-level specs whose bead is done" in rendered
    assert "shipped.json" in rendered


def test_spec_record_reports_done_specs_whose_bead_is_not_done(tmp_path):
    spec_file(tmp_path, "still-open.json", "Still open", done=True)
    store = FakeStore([task_with_title("Still open", state="review", task_id="open-1")])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.bead_state) for i in report.spec_record_issues] == [
        ("done-spec-not-done", "Still open", "review")
    ]
    rendered = scanner.render_report(report)
    assert "done/ specs whose bead is not done" in rendered
    assert "state=review" in rendered


def test_spec_record_does_not_flag_a_done_spec_with_no_matching_bead_as_drift(tmp_path):
    # OPS-34: a done/ spec with no bead at all predates dev.task beads as the
    # record. It is not a reopening and must not show up as something an
    # operator has to act on.
    spec_file(tmp_path, "predates-beads.json", "Shipped before beads existed", done=True)
    store = FakeStore([])  # injected bead population: empty, no matching bead

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title) for i in report.spec_record_issues] == [
        ("done-spec-no-bead", "Shipped before beads existed")
    ]
    rendered = scanner.render_report(report)
    assert "SPEC RECORD DRIFT" not in rendered
    assert "no drift found" in rendered
    assert "not drift, no action needed" in rendered
    assert "predates-beads.json" in rendered


def test_spec_record_still_flags_a_done_spec_whose_bead_is_pending_as_drift(tmp_path):
    # The mirror of the case above: a bead DOES exist and it genuinely has
    # not resolved to done — that is real drift and must still be reported
    # exactly as before, not swept up with the no-bead case.
    spec_file(tmp_path, "reopened.json", "Reopened after shipping", done=True)
    store = FakeStore(
        [task_with_title("Reopened after shipping", state="pending", task_id="pend-1")]
    )

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.bead_state) for i in report.spec_record_issues] == [
        ("done-spec-not-done", "Reopened after shipping", "pending")
    ]
    rendered = scanner.render_report(report)
    assert "SPEC RECORD DRIFT" in rendered
    assert "done/ specs whose bead is not done" in rendered
    assert "state=pending" in rendered


def test_spec_record_unreadable_population_reports_not_checked_not_clean(tmp_path):
    # Mirrors the --no-queue behaviour already present: when the bead
    # population cannot be read at all, the report must say so rather than
    # classifying the done/ spec as either drift or predates-beads.
    spec_file(tmp_path, "predates-beads.json", "Shipped before beads existed", done=True)

    report = scanner.dry_run(
        {},
        store=None,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert report.spec_record_known is False
    assert report.spec_record_issues == ()
    rendered = scanner.render_report(report)
    assert "SPEC RECORD: NOT CHECKED" in rendered
    assert "no drift found" not in rendered
    assert "predates-beads.json" not in rendered


def test_spec_record_reports_old_top_level_specs_with_no_matching_bead(tmp_path):
    old = spec_file(tmp_path, "unfiled-old.json", "Old unfiled")
    spec_file(tmp_path, "unfiled-fresh.json", "Fresh unfiled")

    report = scanner.dry_run(
        {},
        store=FakeStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_mtime_fn=lambda path: OLD_SPEC if path == old else FRESH_SPEC,
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.age_days) for i in report.spec_record_issues] == [
        ("queued-spec-no-bead", "Old unfiled", 21)
    ]
    rendered = scanner.render_report(report)
    assert "top-level specs with no exact-title bead" in rendered
    assert "age_days=21" in rendered
    assert "Fresh unfiled" not in rendered


def test_merged_titles_extracts_the_full_title_and_rejects_a_substring():
    subjects = (
        "fix(code-health): Ledger variance surfaces in the console (#395)",
        "chore(tasks): file REL-3, REL-4 and OPS-27 (#554)",
        "a subject with no dispatcher-shaped wrapper at all",
    )

    titles = scanner.merged_titles(subjects)

    assert titles == frozenset({
        "Ledger variance surfaces in the console",
        "file REL-3, REL-4 and OPS-27",
    })
    # The dispatcher writes the title wrapped in kind(lane): ... (#pr); a
    # spec titled merely "Ledger" must not match on the strength of being a
    # substring of the recovered title.
    assert "Ledger" not in titles


def test_spec_record_reports_a_merged_spec_with_no_bead_even_within_the_grace_window(
    tmp_path,
):
    spec_file(tmp_path, "shipped-unfiled.json", "Ledger variance surfaces in the console")

    report = scanner.dry_run(
        {},
        store=FakeStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        # Written one day ago — well inside SPEC_RECORD_UNFILED_GRACE. If the
        # merged check were gated by the grace period, this would report
        # nothing at all.
        spec_mtime_fn=lambda _path: FRESH_SPEC,
        spec_now_fn=lambda: FIXED_NOW,
        spec_commit_subjects_fn=lambda: (
            "fix(code-health): Ledger variance surfaces in the console (#395)",
        ),
    )

    assert [(i.kind, i.title) for i in report.spec_record_issues] == [
        ("queued-spec-merged-no-bead", "Ledger variance surfaces in the console")
    ]
    rendered = scanner.render_report(report)
    assert "already shipped, never filed" in rendered
    assert "shipped-unfiled.json" in rendered


def test_spec_record_with_no_bead_and_no_merged_commit_still_uses_the_grace_period(
    tmp_path,
):
    old = spec_file(tmp_path, "unfiled-old.json", "Old unfiled")

    report = scanner.dry_run(
        {},
        store=FakeStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_mtime_fn=lambda path: OLD_SPEC if path == old else FRESH_SPEC,
        spec_now_fn=lambda: FIXED_NOW,
        spec_commit_subjects_fn=lambda: (),
    )

    assert [(i.kind, i.title, i.age_days) for i in report.spec_record_issues] == [
        ("queued-spec-no-bead", "Old unfiled", 21)
    ]


def test_spec_queue_dirs_is_the_single_declared_list_and_includes_docs_plans_specs():
    assert scanner.TASKS_DIR in scanner.SPEC_QUEUE_DIRS
    assert (
        scanner.REPO_ROOT / "docs" / "plans" / "specs" in scanner.SPEC_QUEUE_DIRS
    )


def test_dry_run_scans_every_declared_queue_directory_not_just_tasks_dir(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    spec_file(dir_a, "shipped.json", "Shipped in A")
    spec_file(dir_b, "shipped.json", "Shipped in B")
    store = FakeStore([
        task_with_title("Shipped in A", state="done", task_id="a-1"),
        task_with_title("Shipped in B", state="done", task_id="b-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(dir_a, dir_b),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert {(i.kind, i.title) for i in report.spec_record_issues} == {
        ("queued-spec-done", "Shipped in A"),
        ("queued-spec-done", "Shipped in B"),
    }


def test_spec_record_says_no_drift_when_queue_and_record_agree(tmp_path):
    spec_file(tmp_path, "queued.json", "Queued work")
    spec_file(tmp_path, "done.json", "Done work", done=True)
    store = FakeStore([
        task_with_title("Queued work", state="pending", task_id="queued-1"),
        task_with_title("Done work", state="done", task_id="done-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert report.spec_record_issues == ()
    assert "SPEC RECORD: checked tasks/ against dev.task beads; no drift found." in (
        scanner.render_report(report)
    )


def test_spec_record_reports_top_level_specs_whose_bead_is_superseded(tmp_path):
    spec_file(tmp_path, "replaced.json", "Replaced work")
    store = FakeStore([task_with_title("Replaced work", state="superseded", task_id="sup-1")])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.bead_state) for i in report.spec_record_issues] == [
        ("queued-spec-superseded", "Replaced work", "superseded")
    ]
    rendered = scanner.render_report(report)
    assert "SPEC RECORD DRIFT" in rendered
    assert "top-level specs whose bead is superseded" in rendered
    assert "replaced.json" in rendered


def test_spec_record_does_not_flag_a_done_spec_whose_bead_is_superseded(tmp_path):
    spec_file(tmp_path, "replaced.json", "Replaced work", done=True)
    store = FakeStore([task_with_title("Replaced work", state="superseded", task_id="sup-1")])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert report.spec_record_issues == ()


def test_spec_record_does_not_flag_a_superseded_bead_when_a_live_bead_shares_its_identity(
    tmp_path,
):
    # OPS-116: file_task.py --supersede's designed pattern is exactly this
    # shape -- an old bead marked superseded, a new pending bead re-filed
    # under the same identity. That is correctly-queued work, not drift.
    spec_file(tmp_path, "replaced.json", "Replaced work")
    store = FakeStore([
        task_with_title("Replaced work", state="superseded", task_id="sup-1"),
        task_with_title("Replaced work", state="pending", task_id="pend-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert report.spec_record_issues == ()


def test_spec_record_does_not_flag_a_done_bead_when_a_live_bead_shares_its_identity(
    tmp_path,
):
    # The done-side mirror of the case above: a regression re-file permitted
    # by file_task._is_open without --supersede leaves an old done bead and a
    # new pending bead under the same identity. Zero live instances today,
    # but the shape is identical to the superseded case and must not regress.
    spec_file(tmp_path, "shipped.json", "Shipped work")
    store = FakeStore([
        task_with_title("Shipped work", state="done", task_id="done-1"),
        task_with_title("Shipped work", state="pending", task_id="pend-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert report.spec_record_issues == ()


def test_spec_record_reports_queued_spec_done_when_every_match_is_closed_and_one_is_done(
    tmp_path,
):
    # Precedence is unchanged: a {done, superseded} identity -- both
    # closed -- still reports queued-spec-done, exactly as before this fix.
    spec_file(tmp_path, "shipped.json", "Shipped work")
    store = FakeStore([
        task_with_title("Shipped work", state="done", task_id="done-1"),
        task_with_title("Shipped work", state="superseded", task_id="sup-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )

    assert [(i.kind, i.title, i.bead_state) for i in report.spec_record_issues] == [
        ("queued-spec-done", "Shipped work", "done")
    ]


def test_spec_record_check_does_not_write_files_or_beads(tmp_path):
    spec_file(tmp_path, "unfiled-old.json", "Old unfiled")

    class ReadOnlyStore(FakeStore):
        def set_state(self, bead_id, state, created_by):
            raise AssertionError("spec-record check must not set bead state")

        def transition_state(self, bead_id, from_state, to_state, created_by):
            raise AssertionError("spec-record check must not transition beads")

        def patch_content(self, bead_id, content, created_by):
            raise AssertionError("spec-record check must not patch beads")

        def add_note(
            self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
        ):
            raise AssertionError("spec-record check must not write notes")

    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    report = scanner.dry_run(
        {},
        store=ReadOnlyStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_mtime_fn=lambda _path: OLD_SPEC,
        spec_now_fn=lambda: FIXED_NOW,
    )

    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert report.spec_record_issues
    assert after == before


# --- reconciling tasks/ against bead state (OPS-25) --------------------------


def _issues_for(tmp_path, store):
    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_now_fn=lambda: FIXED_NOW,
    )
    return report.spec_record_issues


def test_reconcile_moves_top_level_spec_whose_bead_is_done(tmp_path):
    spec_file(tmp_path, "shipped.json", "Shipped work")
    store = FakeStore([task_with_title("Shipped work", state="done", task_id="done-1")])

    moved = scanner.reconcile_spec_record(_issues_for(tmp_path, store))

    assert [(i.kind, i.title) for i in moved] == [("queued-spec-done", "Shipped work")]
    assert not (tmp_path / "shipped.json").exists()
    assert (tmp_path / "done" / "shipped.json").exists()


def test_reconcile_moves_top_level_spec_whose_bead_is_superseded(tmp_path):
    spec_file(tmp_path, "replaced.json", "Replaced work")
    store = FakeStore([task_with_title("Replaced work", state="superseded", task_id="sup-1")])

    moved = scanner.reconcile_spec_record(_issues_for(tmp_path, store))

    assert [(i.kind, i.title) for i in moved] == [
        ("queued-spec-superseded", "Replaced work")
    ]
    assert not (tmp_path / "replaced.json").exists()
    assert (tmp_path / "done" / "replaced.json").exists()


def test_reconcile_moves_done_spec_back_when_bead_reopened(tmp_path):
    spec_file(tmp_path, "reopened.json", "Reopened work", done=True)
    store = FakeStore([task_with_title("Reopened work", state="review", task_id="open-1")])

    moved = scanner.reconcile_spec_record(_issues_for(tmp_path, store))

    assert [(i.kind, i.title) for i in moved] == [
        ("done-spec-not-done", "Reopened work")
    ]
    assert (tmp_path / "reopened.json").exists()
    assert not (tmp_path / "done" / "reopened.json").exists()


def test_reconcile_leaves_unresolved_specs_untouched(tmp_path):
    spec_file(tmp_path, "unfiled-old.json", "Old unfiled")
    spec_file(tmp_path, "orphaned.json", "Orphaned done entry", done=True)

    report = scanner.dry_run(
        {},
        store=FakeStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        spec_mtime_fn=lambda _path: OLD_SPEC,
        spec_now_fn=lambda: FIXED_NOW,
    )
    assert report.spec_record_issues, "expected both disagreement classes to be reported"

    moved = scanner.reconcile_spec_record(report.spec_record_issues)

    assert moved == ()
    assert (tmp_path / "unfiled-old.json").exists()
    assert (tmp_path / "done" / "orphaned.json").exists()


def test_reconcile_does_not_edit_spec_content(tmp_path):
    path = spec_file(tmp_path, "shipped.json", "Shipped work")
    original = path.read_text()
    store = FakeStore([task_with_title("Shipped work", state="done", task_id="done-1")])

    scanner.reconcile_spec_record(_issues_for(tmp_path, store))

    assert (tmp_path / "done" / "shipped.json").read_text() == original


def test_reconcile_never_calls_a_store_write_method(tmp_path):
    spec_file(tmp_path, "shipped.json", "Shipped work")
    spec_file(tmp_path, "reopened.json", "Reopened work", done=True)

    class ReadOnlyStore(FakeStore):
        def set_state(self, bead_id, state, created_by):
            raise AssertionError("reconcile must not set bead state")

        def transition_state(self, bead_id, from_state, to_state, created_by):
            raise AssertionError("reconcile must not transition beads")

        def patch_content(self, bead_id, content, created_by):
            raise AssertionError("reconcile must not patch beads")

        def add_note(
            self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra
        ):
            raise AssertionError("reconcile must not write notes")

    store = ReadOnlyStore([
        task_with_title("Shipped work", state="done", task_id="done-1"),
        task_with_title("Reopened work", state="pending", task_id="open-1"),
    ])

    moved = scanner.reconcile_spec_record(_issues_for(tmp_path, store))

    assert len(moved) == 2


def test_reconcile_report_says_nothing_to_move_when_agreement():
    assert scanner.reconcile_report(()) == (
        "spec record reconcile: tasks/ already agrees with the bead record; "
        "nothing to move."
    )


def test_reconcile_report_lists_each_move(tmp_path):
    spec_file(tmp_path, "shipped.json", "Shipped work")
    store = FakeStore([task_with_title("Shipped work", state="done", task_id="done-1")])

    moved = scanner.reconcile_spec_record(_issues_for(tmp_path, store))
    rendered = scanner.reconcile_report(moved)

    assert "Shipped work" in rendered
    assert "done/shipped.json" in rendered
    assert "bead done-1" in rendered


def test_reconcile_moves_each_issue_within_its_own_queue_directory(tmp_path):
    """Once SPEC_QUEUE_DIRS names more than one directory, a spec must move
    relative to the queue it was found in, not into a fixed directory."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    spec_file(dir_a, "shipped.json", "Shipped in A")
    spec_file(dir_b, "shipped.json", "Shipped in B")
    store = FakeStore([
        task_with_title("Shipped in A", state="done", task_id="a-1"),
        task_with_title("Shipped in B", state="done", task_id="b-1"),
    ])

    report = scanner.dry_run(
        {},
        store=store,
        suppressions={},
        queue_dirs=(dir_a, dir_b),
        spec_now_fn=lambda: FIXED_NOW,
    )
    moved = scanner.reconcile_spec_record(report.spec_record_issues)

    assert len(moved) == 2
    assert (dir_a / "done" / "shipped.json").exists()
    assert (dir_b / "done" / "shipped.json").exists()
    assert not (dir_a / "shipped.json").exists()
    assert not (dir_b / "shipped.json").exists()


def test_a_candidate_fully_covered_by_open_work_is_not_filed(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail", "fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([open_task(refs=["PC-SUB-001/AC-1", "PC-SUB-001/AC-2"])])

    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    assert_stopped_by_a_control(report)
    assert len(report.duplicates) == 1
    assert "already covered by open work" in report.duplicates[0].duplicate_of


def test_coverage_is_a_union_across_open_tasks_not_one_task(tmp_path):
    # Measured 2026-08-12: PC-SUB-001's criteria are split across 71fb26bb
    # (AC-1, AC-3) and a2e8468e (AC-2). A per-task containment rule would have
    # filed a fourth bead for work already twice claimed.
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail", "fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([
        open_task(refs=["PC-SUB-001/AC-1"]),
        open_task(refs=["PC-SUB-001/AC-2"]),
    ])

    assert_stopped_by_a_control(
        scanner.dry_run(one_scanner(finding), store=store, suppressions={})
    )


def test_one_shared_reference_is_not_a_duplicate(tmp_path):
    # d6d3cee7 is ARCHITECTURE.md drift work that cites PC-SUB-001/AC-1 in
    # passing. Treating that as a match would suppress unrelated candidates for
    # as long as it stays open.
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail", "fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([open_task(refs=["PC-SUB-001/AC-1"])])

    assert len(scanner.dry_run(one_scanner(finding), store=store, suppressions={}).would_file) == 1


def test_a_closed_task_does_not_keep_suppressing_its_requirement(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([open_task(refs=["PC-SUB-001/AC-1"], state="done")])

    assert len(scanner.dry_run(one_scanner(finding), store=store, suppressions={}).would_file) == 1


def test_a_superseded_task_does_not_keep_suppressing_its_requirement(tmp_path):
    # A superseded bead is dead, not merely quiet (dispatch.py:CLOSED_TASK_STATES).
    # Its requirement_refs must not read as coverage.
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([open_task(refs=["PC-SUB-001/AC-1"], state="superseded")])

    assert len(scanner.dry_run(one_scanner(finding), store=store, suppressions={}).would_file) == 1


def test_open_task_state_closed_states_agree_with_dispatch_and_file_task():
    # The three declarations (dispatch.py, file_task.py, and whatever
    # open_task_state actually treats as closed) must not drift apart
    # silently. scanner consumes file_task.CLOSED_TASK_STATES directly, so
    # this only needs to pin file_task's copy against dispatch's.
    assert dispatch.CLOSED_TASK_STATES == file_task.CLOSED_TASK_STATES
    store = FakeStore([
        open_task(refs=["PC-X-001/AC-1"], state=state)
        for state in file_task.CLOSED_TASK_STATES
    ])
    covered_refs, _, open_tasks = scanner.open_task_state(store)
    assert covered_refs == frozenset()
    assert open_tasks == 0


def test_open_task_state_covers_an_open_task_older_than_the_default_page(monkeypatch):
    """scanner.py:669's coverage union must not be a function of how many
    beads happen to exist. Substrate lists newest-first
    (``order_by(created_at.desc())`` in apps/substrate/src/routes.py), so 200
    closed beads stand in for the newest page and the one OPEN bead is placed
    past them -- exactly where a store that stopped after one truncated
    request would drop it, reintroducing the S24-P1/S24-P2 double-filing
    ``_check_not_a_duplicate`` exists to prevent (#477/#478).

    ``store`` here is a real ``Substrate`` in front of a fake HTTP backend
    that enforces limit/offset like the live server, so this exercises the
    actual pagination fix (substrate.py's ``list_beads``) rather than a fake
    that was never truncating to begin with.
    """
    # Derived from the real default, never a literal: with 200 hard-coded, this
    # test passes against the pre-pagination code as soon as someone raises the
    # default to 500 -- i.e. it would pin "population > 200" while the cheap fix
    # AC3 forbids sails through it. Reading the signature keeps the population
    # one past whatever the code actually asks for.
    default_page = inspect.signature(Substrate.list_tasks).parameters["limit"].default
    newest_first_closed = [
        open_task(refs=["PC-SUB-002/AC-1"], state="done") for _ in range(default_page)
    ]
    old_open = open_task(
        refs=["PC-SUB-001/AC-1"], state="pending", scanner_key="doctrine-staleness:PC-SUB-001"
    )
    backend_tasks = newest_first_closed + [old_open]

    def fake_request(self, method, path, **kwargs):
        assert method == "GET" and path == "/beads"
        params = kwargs["params"]
        offset = params.get("offset", 0)
        limit = params["limit"]
        return backend_tasks[offset : offset + limit]

    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(Substrate, "_request", fake_request)
    store = Substrate()

    covered_refs, filed_keys, open_tasks = scanner.open_task_state(store)

    assert open_tasks == 1
    assert "PC-SUB-001/AC-1" in covered_refs
    assert "doctrine-staleness:PC-SUB-001" in filed_keys

    ref_finding = scanner.Finding(
        scanner="requirement-gaps", lane="dev", dedupe_key="requirement-gaps:PC-SUB-001",
        title="t", intent="i", requirement_refs=("PC-SUB-001/AC-1",),
    )
    assert scanner.duplicate_reason(ref_finding, covered_refs, filed_keys)

    key_finding = scanner.Finding(
        scanner="doctrine-staleness", lane="dev", dedupe_key="doctrine-staleness:PC-SUB-001",
        title="t", intent="i",
    )
    assert scanner.duplicate_reason(key_finding, covered_refs, filed_keys)


def test_the_scanner_does_not_refile_its_own_work(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([open_task(refs=[], scanner_key=finding.dedupe_key)])

    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    assert_stopped_by_a_control(report)
    assert "already filed by this scanner" in report.duplicates[0].duplicate_of


def test_a_suppression_stops_a_candidate_and_reports_the_reason(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    store = FakeStore([])

    report = scanner.dry_run(
        one_scanner(finding), store=store,
        suppressions={finding.dedupe_key: "gated on G1a"},
    )

    assert_stopped_by_a_control(report)
    assert report.suppressed[0].suppressed_by == "gated on G1a"


def test_a_suppressed_candidate_stays_visible_in_the_report(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    report = scanner.dry_run(
        one_scanner(finding), store=FakeStore([]),
        suppressions={finding.dedupe_key: "gated on G1a"},
    )

    assert "SUPPRESSED by a standing decision" in scanner.render_report(report)
    assert "gated on G1a" in scanner.render_report(report)


def test_the_rate_limit_caps_one_pass_and_names_what_it_held(tmp_path):
    rids = ["PC-SUB-001", "PC-SUB-002", "PC-SUB-003", "PC-FAC-004", "PC-EXE-003"]
    assert len(rids) == scanner.MAX_FILINGS_PER_RUN + 2
    findings = []
    for i, rid in enumerate(rids):
        d = tmp_path / f"r{i}"
        d.mkdir()
        findings.append(finding_for(d, rid=rid, conformances=["fail"],
                                    implementation=["apps/mcp-hub/x.py"]))
    report = scanner.dry_run(
        {"stub": lambda: findings}, store=FakeStore([]), suppressions={}
    )

    assert len(report.would_file) == scanner.MAX_FILINGS_PER_RUN
    assert len(report.held_by_rate_limit) == 2


def test_live_filing_honours_the_cap_after_controls(tmp_path):
    rids = ["PC-SUB-001", "PC-SUB-002", "PC-SUB-003", "PC-FAC-004", "PC-EXE-003"]
    findings = []
    for i, rid in enumerate(rids):
        d = tmp_path / f"filing-{i}"
        d.mkdir()
        findings.append(finding_for(d, rid=rid, conformances=["fail"],
                                    implementation=["apps/mcp-hub/x.py"]))
    store = FakeFilingStore()
    report = scanner.dry_run({"stub": lambda: findings}, store=store, suppressions={})

    filed = scanner.file_report(report, store)

    assert [bead["id"] for bead in filed] == ["filed-1", "filed-2", "filed-3"]
    assert len(store.posts) == scanner.MAX_FILINGS_PER_RUN
    assert [post["content"][scanner.SCANNER_KEY_FIELD] for post in store.posts] == [
        finding.dedupe_key for finding in findings[:scanner.MAX_FILINGS_PER_RUN]
    ]


def test_filing_lane_is_pinned_to_code_health():
    # The scanner files toil, never product work — a future lane addition (the
    # 'feature' lane, sprint 24) must not silently widen this without the
    # change showing up here.
    assert scanner.FILING_LANE == "code-health"


def test_feature_lane_candidate_is_reported_and_not_filed(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    finding = scanner.Finding(
        scanner=finding.scanner,
        lane="feature",
        dedupe_key=finding.dedupe_key,
        title=finding.title,
        intent=finding.intent,
        requirement_refs=finding.requirement_refs,
        scope_paths=finding.scope_paths,
    )
    store = FakeFilingStore()
    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    filed = scanner.file_report(report, store)

    assert filed == ()
    assert store.posts == []
    assert len(report.wrong_lane) == 1
    assert "[feature]" in scanner.render_report(report)


def test_non_code_health_candidate_is_reported_and_not_filed(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    finding = scanner.Finding(
        scanner=finding.scanner,
        lane="drift",
        dedupe_key=finding.dedupe_key,
        title=finding.title,
        intent=finding.intent,
        requirement_refs=finding.requirement_refs,
        scope_paths=finding.scope_paths,
    )
    store = FakeFilingStore()
    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    filed = scanner.file_report(report, store)

    assert filed == ()
    assert store.posts == []
    assert len(report.wrong_lane) == 1
    assert "[drift]" in scanner.render_report(report)
    assert "scanner filing is code-health lane only" in scanner.render_report(report)


def test_filed_bead_carries_complete_scanner_provenance_and_file_task_defaults(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    store = FakeFilingStore()
    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    scanner.file_report(report, store)

    payload = store.posts[0]
    content = payload["content"]
    assert payload["created_by"] == "factory-scanner"
    assert payload["provenance"] == {
        "worker": "factory-scanner",
        "model": "none",
        "prompt_ref": f"scanner/{finding.scanner}/{finding.dedupe_key}",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }
    assert content["requirement_refs"] == list(finding.requirement_refs)
    assert content[scanner.SCANNER_KEY_FIELD] == finding.dedupe_key
    assert content["budget"] == file_task.DEFAULT_BUDGET
    assert content["verification"] == file_task.DEFAULT_VERIFICATION
    assert content["scope"]["forbidden_paths"] == list(file_task.FORBIDDEN_ALWAYS)


def test_dedup_prevents_duplicate_live_filing(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    store = FakeFilingStore([open_task(refs=[], scanner_key=finding.dedupe_key)])
    report = scanner.dry_run(one_scanner(finding), store=store, suppressions={})

    filed = scanner.file_report(report, store)

    assert filed == ()
    assert store.posts == []
    assert len(report.duplicates) == 1


def test_suppression_prevents_live_filing(tmp_path):
    finding = finding_for(tmp_path, rid="PC-SUB-001", conformances=["fail"],
                          implementation=["apps/mcp-hub/x.py"])
    store = FakeFilingStore()
    report = scanner.dry_run(
        one_scanner(finding), store=store,
        suppressions={finding.dedupe_key: "gated on G1a"},
    )

    filed = scanner.file_report(report, store)

    assert filed == ()
    assert store.posts == []
    assert len(report.suppressed) == 1


def test_without_a_queue_a_live_run_is_refused_outright(tmp_path):
    # The degraded run would file duplicates it cannot see. Filing the part it
    # can still justify is exactly the wrong failure.
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    report = scanner.dry_run(one_scanner(finding), store=None, suppressions={})

    assert report.may_file_live is False
    assert report.duplicates == ()
    assert "NOT CHECKED" in scanner.render_report(report)
    assert "WOULD REFUSE THIS PASS" in scanner.render_report(report)


def test_a_reachable_queue_permits_a_live_run(tmp_path):
    finding = finding_for(
        tmp_path, rid="PC-SUB-001", conformances=["fail"],
        implementation=["apps/mcp-hub/x.py"],
    )
    report = scanner.dry_run(one_scanner(finding), store=FakeStore([]), suppressions={})

    assert report.may_file_live is True
    assert "WOULD REFUSE THIS PASS" not in scanner.render_report(report)


def test_the_checked_in_suppressions_file_parses_and_states_reasons():
    entries = scanner.load_suppressions()

    assert entries, "the checked-in suppressions file should not be empty"
    for key, reason in entries.items():
        assert len(reason.strip()) > 40, f"{key} must say why, not just that"


# ---------------------------------------------------------------------------
# classify_filing_note — a _filing_note exempts only if it STATES a hold
# condition. Four of the five branches are sourced from the real notes this
# corpus already carries; only the defective branch is synthetic, because its
# former real specimen (OPS-68) turned out terminal (#673) and now routes to
# done-but-unmoved before classification is ever reached.
# ---------------------------------------------------------------------------


def _real_filing_note(name: str) -> str:
    return json.loads((scanner.TASKS_DIR / name).read_text())["_filing_note"]


def test_classify_filing_note_ops14_no_hold_provenance_note_fails():
    note = _real_filing_note("OPS-14-cluster-health-runs-on-a-schedule.json")

    verdict = scanner.classify_filing_note(note)

    assert verdict.category == "no-hold"
    assert verdict.exempts is False


def test_classify_filing_note_r2605_3_machine_resolvable_not_yet_occurred_passes():
    note = _real_filing_note("R2605-3-only-a-declared-alert-may-open-an-incident.json")

    verdict = scanner.classify_filing_note(
        note, bead_terminal_fn=lambda spec_id: False if spec_id == "R2605-1" else None
    )

    assert verdict.category == "machine-resolvable-pending"
    assert verdict.exempts is True


def test_classify_filing_note_a34_1_operator_decision_wins_over_an_occurred_condition():
    note = _real_filing_note("A34-1-a-merge-verdict-executes-without-a-person-behind-a-buffer.json")

    verdict = scanner.classify_filing_note(
        note, amendment_status_fn=lambda n: "PROPOSED" if n == 34 else None
    )

    assert verdict.category == "operator-decision"
    assert verdict.exempts is True
    assert "the Operator" in verdict.reason


def test_classify_filing_note_ops70_expired_hold_fails_even_beside_a_no_decider_clause():
    # The corpus's live specimen: an occurred amendment-status clause (A35 has
    # resolved WITHDRAWN) sits beside a volume-judgment clause naming no
    # decider -- not a clause of any kind -- and the occurred clause governs.
    note = _real_filing_note("OPS-70-a-lane-executed-bead-is-invisible-to-reconciliation.json")

    verdict = scanner.classify_filing_note(
        note, amendment_status_fn=lambda n: "WITHDRAWN" if n == 35 else None
    )

    assert verdict.category == "machine-resolvable-expired"
    assert verdict.exempts is False


def test_classify_filing_note_ops70_against_the_real_architecture_doc():
    """Same note, real resolver: ARCHITECTURE.md really does record Amendment
    35 as WITHDRAWN, so the default wiring reaches the same verdict with no
    injected double at all."""
    note = _real_filing_note("OPS-70-a-lane-executed-bead-is-invisible-to-reconciliation.json")

    verdict = scanner.classify_filing_note(
        note, amendment_status_fn=lambda n: scanner.amendment_statuses().get(n)
    )

    assert verdict.category == "machine-resolvable-expired"


def test_classify_filing_note_defective_for_an_unresolvable_referent():
    # Synthetic (OPS-68's own former note of this shape is no longer live --
    # it turned out terminal, #673 -- so the corpus carries no real specimen).
    # Closed-set-shaped ("do not file until ... merges") but names no PR
    # number, no bead id, no amendment: unresolvability must never exempt.
    verdict = scanner.classify_filing_note("Do NOT file until the next structural PR merges.")

    assert verdict.category == "defective"
    assert verdict.exempts is False


def test_classify_filing_note_empty_note_is_no_hold():
    assert scanner.classify_filing_note("").category == "no-hold"
    assert scanner.classify_filing_note("   ").category == "no-hold"


def test_classify_filing_note_unresolvable_bead_referent_is_defective():
    verdict = scanner.classify_filing_note(
        "File after ZZZZ-9 lands.", bead_terminal_fn=lambda spec_id: None
    )

    assert verdict.category == "defective"


def test_classify_filing_note_pr_merged_occurred_expires_the_hold():
    verdict = scanner.classify_filing_note(
        "Do not file until #700 is merged.", pr_merged_fn=lambda n: n == 700
    )

    assert verdict.category == "machine-resolvable-expired"


def test_classify_filing_note_pr_not_yet_merged_exempts():
    verdict = scanner.classify_filing_note(
        "Do not file until #700 is merged.", pr_merged_fn=lambda n: False
    )

    assert verdict.category == "machine-resolvable-pending"
    assert verdict.exempts is True


def test_classify_filing_note_a_bare_pr_reference_with_no_merge_context_is_not_a_clause():
    # OPS-14's own note carries "#509 shipped the checker" -- provenance, not
    # a condition. A bare `#NNN` with no "merge" nearby must not be read as one.
    verdict = scanner.classify_filing_note("#509 shipped the checker this schedules.")

    assert verdict.category == "no-hold"


# ---------------------------------------------------------------------------
# parse_amendment_statuses — the three textual shapes ARCHITECTURE.md's §9
# body actually carries.
# ---------------------------------------------------------------------------


def test_parse_amendment_statuses_handles_backticked_bare_and_absent_shapes():
    text = "\n".join([
        "### Amendment 1 — no status line at all",
        "**Summary:** does something",
        "**Rationale:** because",
        "",
        "### Amendment 2 — backticked",
        "**Status:** `RATIFIED` 2026-08-17 by the Operator.",
        "",
        "### Amendment 3 — bare",
        "**Status:** WITHDRAWN — no longer pursued",
        "",
    ])

    statuses = scanner.parse_amendment_statuses(text)

    assert statuses == {2: "RATIFIED", 3: "WITHDRAWN"}
    assert 1 not in statuses


def test_parse_amendment_statuses_only_reads_the_first_status_line_per_section():
    text = "\n".join([
        "### Amendment 5 — two status-shaped lines",
        "**Status:** `PROPOSED` first.",
        "**Status:** `RATIFIED` second, must not override the first.",
        "",
    ])

    assert scanner.parse_amendment_statuses(text) == {5: "PROPOSED"}


def test_parse_amendment_statuses_resets_at_an_unrelated_heading():
    text = "\n".join([
        "### Amendment 6",
        "### Phase 0 — unrelated heading before any Status line",
        "**Status:** should not be attributed to Amendment 6",
        "",
    ])

    assert 6 not in scanner.parse_amendment_statuses(text)


def test_amendment_statuses_reads_the_real_architecture_doc():
    statuses = scanner.amendment_statuses()

    assert statuses[34] == "RATIFIED"  # ratified 2026-10-04 (PR 1198)
    assert statuses[35] == "WITHDRAWN"
    assert 1 not in statuses  # Amendment 1 carries no Status line at all


def test_merged_pr_numbers_extracts_trailing_pr_numbers():
    subjects = (
        "fix(code-health): Ledger variance surfaces in the console (#395)",
        "chore(tasks): file REL-3, REL-4 and OPS-27 (#554)",
        "a subject with no dispatcher-shaped wrapper at all",
    )

    assert scanner.merged_pr_numbers(subjects) == frozenset({395, 554})


# ---------------------------------------------------------------------------
# scan_spec_record — hold-note teeth wired into the existing reconciler
# ---------------------------------------------------------------------------


def spec_file_with_note(tmp_path, name, title, note, done=False):
    directory = tmp_path / "done" if done else tmp_path
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({"title": title, "_filing_note": note}))
    return path


def test_scan_spec_record_exempts_a_top_level_spec_with_a_live_operator_decision_hold(tmp_path):
    spec_file_with_note(
        tmp_path, "held.json", "Held work", "Do not file until the Operator directs the next step."
    )

    issues = scanner.scan_spec_record(
        FakeStore([]), tasks_dir=tmp_path, now_fn=lambda: FIXED_NOW, mtime_fn=lambda path: OLD_SPEC
    )

    assert issues == ()


def test_scan_spec_record_reports_a_dropped_filing_whose_hold_note_expired(tmp_path):
    spec_file_with_note(
        tmp_path, "expired.json", "Expired hold", "Do not file until Amendment 35 is WITHDRAWN."
    )

    issues = scanner.scan_spec_record(
        FakeStore([]),
        tasks_dir=tmp_path,
        now_fn=lambda: FIXED_NOW,
        mtime_fn=lambda path: OLD_SPEC,
        note_verdict_fn=lambda note, tasks, subjects: scanner.classify_filing_note(
            note, amendment_status_fn=lambda n: "WITHDRAWN" if n == 35 else None
        ),
    )

    assert [(i.kind, i.hold_note_category) for i in issues] == [
        ("queued-spec-no-bead", "machine-resolvable-expired")
    ]


def test_scan_spec_record_a_no_hold_note_does_not_exempt(tmp_path):
    spec_file_with_note(
        tmp_path,
        "no-hold.json",
        "No hold work",
        "Rebound to R26.06/O-6 by the 2026-09-06 sprint plan.",
    )

    issues = scanner.scan_spec_record(
        FakeStore([]), tasks_dir=tmp_path, now_fn=lambda: FIXED_NOW, mtime_fn=lambda path: OLD_SPEC
    )

    assert [(i.kind, i.hold_note_category) for i in issues] == [
        ("queued-spec-no-bead", "no-hold")
    ]


def test_scan_spec_record_exempts_a_merged_no_bead_spec_with_a_pending_hold(tmp_path):
    spec_file_with_note(tmp_path, "merged-held.json", "Merged held work", "File AFTER ZZZZ-1 lands.")

    issues = scanner.scan_spec_record(
        FakeStore([]),
        tasks_dir=tmp_path,
        now_fn=lambda: FIXED_NOW,
        mtime_fn=lambda path: FRESH_SPEC,
        commit_subjects_fn=lambda: ("fix(code-health): Merged held work (#123)",),
        note_verdict_fn=lambda note, tasks, subjects: scanner.classify_filing_note(
            note, bead_terminal_fn=lambda spec_id: False
        ),
    )

    assert issues == ()


def test_scan_spec_record_never_consults_the_note_once_a_bead_already_matches(tmp_path):
    # Match-first-then-state: any bead matching, live or terminal, pre-empts
    # note evaluation entirely -- only match-none ever reaches the note.
    spec_file_with_note(tmp_path, "live.json", "Live work", "Do not file until the Operator directs.")

    def boom(note, tasks, subjects):
        raise AssertionError("note must not be consulted once a bead matches")

    issues = scanner.scan_spec_record(
        FakeStore([task_with_title("Live work", state="doing")]),
        tasks_dir=tmp_path,
        now_fn=lambda: FIXED_NOW,
        note_verdict_fn=boom,
    )

    assert issues == ()


def test_scan_spec_record_matches_by_spec_identity_path_even_when_the_title_differs(tmp_path):
    # file_task._matches_spec_identity, reused: a top-level spec's identity is
    # its own path, so a bead recording that path matches even if its title
    # (e.g. edited after filing) no longer matches the spec file's title.
    path = spec_file(tmp_path, "renamed-title.json", "New title in the spec file")
    identity = file_task._spec_identity(str(path))
    bead = {
        "id": "b-1",
        "state": "doing",
        "content": {"title": "Old title recorded on the bead", "spec_identity": identity},
    }

    issues = scanner.scan_spec_record(FakeStore([bead]), tasks_dir=tmp_path, now_fn=lambda: FIXED_NOW)

    assert issues == ()


# ---------------------------------------------------------------------------
# scan_ahead_of_gate_filings / merged_main_tasks_basenames — the mirror
# direction: a dev.task whose spec_identity names a tasks/ path absent from
# merged main.
# ---------------------------------------------------------------------------


def ahead_task(spec_identity, state="pending", title="Filed ahead of gate", task_id="a-1"):
    return {
        "id": task_id,
        "state": state,
        "content": {"title": title, "spec_identity": spec_identity},
    }


def test_scan_ahead_of_gate_filings_cannot_evaluate_when_main_is_unresolvable():
    result = scanner.scan_ahead_of_gate_filings([], main_basenames_fn=lambda: None)

    assert result is None


def test_scan_ahead_of_gate_filings_reports_a_live_bead_as_wanting_review_routing():
    task = ahead_task("apps/factory-dispatcher/tasks/AHEAD-1-not-on-main.json", state="pending")

    issues = scanner.scan_ahead_of_gate_filings([task], main_basenames_fn=lambda: frozenset())

    assert [(i.kind, i.bead_id, i.title) for i in issues] == [
        ("ahead-of-gate-live", "a-1", "Filed ahead of gate")
    ]


def test_scan_ahead_of_gate_filings_reports_a_terminal_bead_as_wanting_done_commit():
    task = ahead_task(
        "apps/factory-dispatcher/tasks/AHEAD-2-not-on-main.json", state="done", task_id="a-2"
    )

    issues = scanner.scan_ahead_of_gate_filings([task], main_basenames_fn=lambda: frozenset())

    assert [(i.kind, i.bead_id) for i in issues] == [("ahead-of-gate-terminal", "a-2")]


def test_scan_ahead_of_gate_filings_labels_key_on_bead_state_for_every_closed_state():
    # Never commit-subject extraction: OPS-82's own filing commit does not
    # match _COMMIT_SUBJECT_TITLE_RE, so that signal is lossy for exactly this
    # population. Bead state alone decides the label.
    for state in ("done", "archived", "superseded"):
        task = ahead_task(
            "apps/factory-dispatcher/tasks/AHEAD-3-not-on-main.json", state=state, task_id=state
        )
        [issue] = scanner.scan_ahead_of_gate_filings([task], main_basenames_fn=lambda: frozenset())
        assert issue.kind == "ahead-of-gate-terminal"


def test_scan_ahead_of_gate_filings_terminal_label_survives_an_unparseable_title():
    # OPS-82's own filing commit subject does not match
    # _COMMIT_SUBJECT_TITLE_RE -- lossy for exactly this population. This
    # title would never parse as a dispatcher-shaped commit subject either,
    # and the terminal label is unaffected because it never consults one.
    task = ahead_task(
        "apps/factory-dispatcher/tasks/AHEAD-4-not-on-main.json",
        state="done",
        title="a title with no dispatcher-shaped wrapper at all",
        task_id="a-4",
    )

    [issue] = scanner.scan_ahead_of_gate_filings([task], main_basenames_fn=lambda: frozenset())

    assert issue.kind == "ahead-of-gate-terminal"


def test_scan_ahead_of_gate_filings_does_not_report_a_spec_present_on_main():
    task = ahead_task("apps/factory-dispatcher/tasks/PRESENT-1.json")

    issues = scanner.scan_ahead_of_gate_filings(
        [task], main_basenames_fn=lambda: frozenset({"PRESENT-1.json"})
    )

    assert issues == ()


def test_scan_ahead_of_gate_filings_ignores_beads_with_no_spec_identity_or_outside_tasks_dir():
    tasks = [
        {"id": "b-4", "state": "pending", "content": {"title": "No identity"}},
        ahead_task("docs/plans/specs/OTHER.json", task_id="b-5"),
    ]

    issues = scanner.scan_ahead_of_gate_filings(tasks, main_basenames_fn=lambda: frozenset())

    assert issues == ()


def test_merged_main_tasks_basenames_returns_none_when_main_cannot_be_resolved(tmp_path):
    assert scanner.merged_main_tasks_basenames(repo_root=tmp_path) is None


def test_merged_main_tasks_basenames_reads_both_tasks_and_done_on_main(tmp_path):
    # The resolver's positive path, hermetically. Every other ahead-of-gate test
    # injects a double for it (main_basenames_fn=lambda: ...), and the only other
    # test that calls the real thing asserts the None branch -- so without this
    # one, CI exercises the basename-against-BOTH-locations rule nowhere. That
    # rule is what AC-3 calls load-bearing, and an earlier draft of the spec had
    # it inverted, so it is exactly the thing that must not go unwatched.
    #
    # Builds its own repository with a branch literally named `main` rather than
    # reading this one, so it runs under actions/checkout's detached HEAD too.
    def git(*args):
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")

    tasks_dir = tmp_path / "apps" / "factory-dispatcher" / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "QUEUED-1.json").write_text("{}")
    (tasks_dir / "done" / "SHIPPED-1.json").write_text("{}")
    (tasks_dir / "not-a-spec.txt").write_text("ignored")
    git("add", "-A")
    git("commit", "-qm", "specs")

    basenames = scanner.merged_main_tasks_basenames(repo_root=tmp_path)

    assert basenames is not None
    # Both locations resolve by basename: a done/-resident spec must be found,
    # which is what stops the whole done/ corpus reporting as ahead-of-gate.
    assert "QUEUED-1.json" in basenames
    assert "SHIPPED-1.json" in basenames
    # Only *.json is collected.
    assert "not-a-spec.txt" not in basenames


def test_merged_main_tasks_basenames_finds_the_real_negative_fixture_population():
    # 2026-09-09: three specs committed into tasks/done/ by #738. Under the
    # basename-either-location rule they must never be reported as
    # ahead-of-gate, however a bead's spec_identity records the top-level
    # path it was filed at.
    #
    # This is the one test in this file that reads the live repository rather
    # than a fixture, which the module docstring above says is deliberately
    # not done here. It earns the exception by pinning the real negative
    # population the acceptance criterion names -- but it can only run where
    # `main` is a resolvable ref. It is not in CI: actions/checkout leaves a
    # detached HEAD at the PR merge ref with no local `main`, so
    # merged_main_tasks_basenames correctly returns None (its documented
    # cannot-evaluate contract, asserted directly by the test above) and the
    # assertion below fired on every CI run while passing in the factory
    # clone, which does have `main`. Skip where the ref is absent rather than
    # fail: a missing `main` says nothing about the corpus this asserts.
    basenames = scanner.merged_main_tasks_basenames()

    if basenames is None:
        pytest.skip("`main` is not a resolvable ref here; nothing to assert against")

    for name in (
        "FA-S29-dispatch-from-a-stale-base-fails-every-task.json",
        "OPS-69-a-persistent-finding-pings-urgent-every-fifteen-minutes.json",
        "OPS-70-every-declared-alert-window-runs-long-and-a-first-post-can-silence-one.json",
    ):
        assert name in basenames


# ---------------------------------------------------------------------------
# render_report — AHEAD OF GATE section
# ---------------------------------------------------------------------------


def test_render_report_ahead_of_gate_not_checked_with_no_substrate():
    report = scanner.dry_run({}, store=None, suppressions={})

    assert "AHEAD-OF-GATE: NOT CHECKED" in scanner.render_report(report)


def test_render_report_ahead_of_gate_clean(tmp_path):
    report = scanner.dry_run(
        {},
        store=FakeStore([]),
        suppressions={},
        queue_dirs=(tmp_path,),
        ahead_of_gate_main_basenames_fn=lambda: frozenset(),
    )

    assert "AHEAD OF GATE: no dev.task" in scanner.render_report(report)


def test_render_report_ahead_of_gate_lists_issues_by_state(tmp_path):
    task = ahead_task("apps/factory-dispatcher/tasks/GHOST-1.json")
    report = scanner.dry_run(
        {},
        store=FakeStore([task]),
        suppressions={},
        queue_dirs=(tmp_path,),
        ahead_of_gate_main_basenames_fn=lambda: frozenset(),
    )

    assert report.ahead_of_gate_known is True
    rendered = scanner.render_report(report)
    assert "AHEAD OF GATE" in rendered
    assert "GHOST-1.json" in rendered
    assert "wants routing to review" in rendered
