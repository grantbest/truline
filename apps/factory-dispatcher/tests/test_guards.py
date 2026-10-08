"""Tests for the dispatcher's safety decisions.

These are the rules that stand between a worker's diff and a PR, so they are
tested for the *unsafe* direction specifically: a scope check that fails open is
worse than no scope check, because it looks like a gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guards  # noqa: E402


FORBIDDEN = [".github/workflows/**"]


def scope(paths, forbidden=None):
    return {"paths": paths, "forbidden_paths": forbidden if forbidden is not None else FORBIDDEN}


def task(**content):
    base = {
        "lane": "code-health",
        "title": "t",
        "intent": "i",
        "context_refs": [],
        "acceptance": ["a"],
        "verification": {"commands": ["pytest -q"]},
        "scope": scope(["apps/mcp-hub/"]),
        "risk_class": "structural",
        "budget": {"max_agent_minutes": 20, "max_usd": 1.0, "max_tokens": 200000},
    }
    base.update(content)
    return {"id": "task-1", "content": base, "created_at": "2026-07-29T00:00:00Z"}


def task_with_id(task_id, **content):
    bead = task(**content)
    bead["id"] = task_id
    return bead


def note(note_id, kind, body="b", created_at="2026-07-29T00:00:00Z", **extra):
    content = {"kind": kind, "body": body}
    content.update(extra)
    return {"id": note_id, "parent_id": "task-1", "content": content, "created_at": created_at}


# ---------------------------------------------------------------------------
# scope: the allow side
# ---------------------------------------------------------------------------


class TestPathAllowed:
    def test_file_under_declared_directory(self):
        assert guards.path_allowed("apps/mcp-hub/src/tools/vision.py", ["apps/mcp-hub/"])

    def test_directory_entry_without_trailing_slash(self):
        assert guards.path_allowed("apps/mcp-hub/src/x.py", ["apps/mcp-hub"])

    def test_double_star_suffix_is_a_prefix(self):
        assert guards.path_allowed("apps/mcp-hub/src/x.py", ["apps/mcp-hub/**"])

    def test_exact_file_entry(self):
        assert guards.path_allowed("README.md", ["README.md"])

    def test_sibling_directory_is_not_allowed(self):
        # The bug that matters: "apps/mcp-hub" must not authorize
        # "apps/mcp-hub-evil/" via a naive startswith.
        assert not guards.path_allowed("apps/mcp-hub-evil/x.py", ["apps/mcp-hub"])

    def test_parent_of_scope_is_not_allowed(self):
        assert not guards.path_allowed("apps/other/x.py", ["apps/mcp-hub/"])

    def test_repo_root_file_is_not_allowed_by_a_subdir_scope(self):
        assert not guards.path_allowed("ARCHITECTURE.md", ["apps/mcp-hub/"])

    def test_unsupported_glob_matches_nothing(self):
        # Fails closed: a pattern the matcher cannot reason about must not
        # silently authorize anything.
        assert not guards.path_allowed("apps/a/x.py", ["apps/*/x.py"])
        assert not guards.path_allowed("apps/a/x.py", ["apps/?/x.py"])

    def test_empty_scope_allows_nothing(self):
        assert not guards.path_allowed("apps/mcp-hub/x.py", [])


# ---------------------------------------------------------------------------
# scope: the forbid side
# ---------------------------------------------------------------------------


class TestPathForbidden:
    def test_the_rule_that_matters_workflows_are_forbidden(self):
        assert guards.path_forbidden(".github/workflows/lint.yml", FORBIDDEN)

    def test_nested_workflow_file(self):
        assert guards.path_forbidden(".github/workflows/nested/deep.yml", FORBIDDEN)

    def test_the_workflows_directory_itself(self):
        assert guards.path_forbidden(".github/workflows", FORBIDDEN)

    def test_unrelated_github_file_is_not_forbidden(self):
        assert not guards.path_forbidden(".github/PULL_REQUEST_TEMPLATE.md", FORBIDDEN)

    def test_plain_glob_pattern(self):
        assert guards.path_forbidden("secrets/prod.env", ["secrets/*"])

    def test_over_matching_is_accepted_on_the_forbid_side(self):
        # fnmatch's `*` spans `/`, so this matches deeper than real glob
        # semantics would. Asserted, not merely tolerated: over-denial is the
        # chosen failure direction.
        assert guards.path_forbidden("secrets/a/b/c.env", ["secrets/*"])


# ---------------------------------------------------------------------------
# scope: the composed verdict
# ---------------------------------------------------------------------------


class TestCheckScope:
    def test_clean_diff_passes(self):
        v = guards.check_scope(
            ["apps/mcp-hub/src/tools/vision.py"], scope(["apps/mcp-hub/"])
        )
        assert v.ok
        assert v.describe() == "all changes within scope"

    def test_forbidden_path_fails_even_when_also_in_scope(self):
        # A scope that allows everything must still not authorize workflows —
        # "the factory may not write the gates that judge it".
        v = guards.check_scope([".github/workflows/lint.yml"], scope([".github/"]))
        assert not v.ok
        assert v.forbidden == (".github/workflows/lint.yml",)
        assert v.out_of_scope == ()

    def test_out_of_scope_path_fails(self):
        v = guards.check_scope(
            ["apps/mcp-hub/src/x.py", "apps/substrate/src/routes.py"],
            scope(["apps/mcp-hub/"]),
        )
        assert not v.ok
        assert v.out_of_scope == ("apps/substrate/src/routes.py",)

    def test_forbidden_and_out_of_scope_are_reported_separately(self):
        v = guards.check_scope(
            [".github/workflows/lint.yml", "apps/other/x.py", "apps/mcp-hub/ok.py"],
            scope(["apps/mcp-hub/"]),
        )
        assert v.forbidden == (".github/workflows/lint.yml",)
        assert v.out_of_scope == ("apps/other/x.py",)
        assert "forbidden" in v.describe() and "outside declared scope" in v.describe()

    def test_unsupported_pattern_is_surfaced_not_silently_ignored(self):
        v = guards.check_scope(["apps/a/x.py"], scope(["apps/*/"]))
        assert not v.ok
        assert v.unsupported_patterns == ("apps/*/",)
        assert "not understood" in v.describe()

    def test_empty_diff_is_vacuously_in_scope(self):
        assert guards.check_scope([], scope(["apps/mcp-hub/"])).ok


# ---------------------------------------------------------------------------
# premise-path check
# ---------------------------------------------------------------------------


class TestAbsentPremisePaths:
    """dev.finding 0d1e952b: nothing checked that a spec's premise artifacts
    exist on the base ref before dispatch, so a bead naming a file that only
    exists on an unmerged PR branch was dispatched and failed at the worker's
    own premise check (OPS-141 / dev.task 9defb4c5), burning a full dispatch.
    """

    def test_context_ref_naming_an_absent_file_is_flagged(self):
        content = task(context_refs=["apps/mcp-hub/src/tools/vision.py"])["content"]
        absent = guards.absent_premise_paths(
            content, present_paths=["apps/mcp-hub/src/tools/other.py"]
        )
        assert absent == ("apps/mcp-hub/src/tools/vision.py",)

    def test_context_ref_naming_a_present_file_is_not_flagged(self):
        content = task(context_refs=["apps/mcp-hub/src/tools/vision.py"])["content"]
        absent = guards.absent_premise_paths(
            content, present_paths=["apps/mcp-hub/src/tools/vision.py"]
        )
        assert absent == ()

    def test_fixture_path_inside_acceptance_prose_is_not_flagged(self):
        # AC-2: acceptance-criterion text is free prose, not a structured
        # field, and is deliberately not scanned — see docstring. This is the
        # exact shape of 3 of the 4 sweep false positives: a fixture path
        # described inside the acceptance text, naming a file the task is
        # *about to create*, not one it depends on already existing.
        content = task(
            acceptance=[
                "Given a repo with no apps/empty/main.py, filing succeeds after "
                "creating apps/empty/main.py"
            ],
            context_refs=[],
            scope=scope([]),
        )["content"]
        absent = guards.absent_premise_paths(content, present_paths=[])
        assert absent == ()

    def test_bare_placeholder_directory_in_acceptance_prose_is_not_flagged(self):
        content = task(
            acceptance=["Verify the fixtures under scripts/tests/ still pass"],
            context_refs=[],
            scope=scope([]),
        )["content"]
        absent = guards.absent_premise_paths(content, present_paths=[])
        assert absent == ()

    def test_scope_path_naming_a_present_directory_is_not_flagged(self):
        content = task(scope=scope(["apps/mcp-hub/"]))["content"]
        absent = guards.absent_premise_paths(
            content, present_paths=["apps/mcp-hub/src/tools/vision.py"]
        )
        assert absent == ()

    def test_scope_path_naming_an_absent_directory_is_flagged(self):
        content = task(scope=scope(["apps/nonexistent/"]))["content"]
        absent = guards.absent_premise_paths(
            content, present_paths=["apps/mcp-hub/src/tools/vision.py"]
        )
        assert absent == ("apps/nonexistent",)

    def test_duplicate_named_path_reported_once(self):
        content = task(
            context_refs=["apps/gone/x.py"],
            scope=scope(["apps/gone/x.py"]),
        )["content"]
        absent = guards.absent_premise_paths(content, present_paths=[])
        assert absent == ("apps/gone/x.py",)

    def test_instance_1_ops_141_mcp_hub_audit_shape_fixture(self):
        # dev.task 9defb4c5 (OPS-141): apps/mcp-hub/tests/test_audit_shape.py
        # -- the file the bead existed to widen -- was absent from the
        # checkout because only PR #870 (unmerged) added it.
        content = task(
            context_refs=["apps/mcp-hub/tests/test_audit_shape.py"]
        )["content"]
        absent = guards.absent_premise_paths(
            content,
            present_paths=[
                "apps/mcp-hub/tests/test_other.py",
                "apps/mcp-hub/src/tools/vision.py",
            ],
        )
        assert absent == ("apps/mcp-hub/tests/test_audit_shape.py",)

    def test_instance_2_m7a_dependency_posture_snapshot_fixture(self):
        # dev.task e6277c3c (M7a): the snapshot file only existed on PR
        # #866's unmerged branch.
        snapshot = (
            "apps/factory-dispatcher/tasks/snapshots/"
            "2026-09-15-application-dependency-posture.json"
        )
        content = task(context_refs=[snapshot], scope=scope([]))["content"]
        absent = guards.absent_premise_paths(
            content, present_paths=["apps/factory-dispatcher/dispatch.py"]
        )
        assert absent == (snapshot,)

    def test_does_not_shell_out_or_touch_present_paths_beyond_the_argument(self):
        # present_paths is authoritative and the only source of truth passed
        # in -- no repo, no network, required so every test above can run
        # with a literal fixture list and nothing else.
        content = task(context_refs=["apps/anything/x.py"], scope=scope([]))["content"]
        assert guards.absent_premise_paths(content, present_paths=[]) == (
            "apps/anything/x.py",
        )
        assert guards.absent_premise_paths(
            content, present_paths=["apps/anything/x.py"]
        ) == ()

    def test_absent_paths_do_not_change_dispatchability(self):
        # AC-3: the check is advisory. A bead naming an absent premise path
        # must still be reported as dispatchable by is_runnable -- this
        # bead's own worker prompt draws the same distinction the operator
        # would need: silent dispatch into an impossibility is the failure,
        # not the absence of a veto.
        content = task(context_refs=["apps/mcp-hub/tests/test_audit_shape.py"])
        present_paths = ["apps/mcp-hub/keep.py"]  # keeps the default scope.paths present
        assert guards.absent_premise_paths(
            content["content"], present_paths=present_paths
        ) == ("apps/mcp-hub/tests/test_audit_shape.py",)
        assert guards.is_runnable(content, []).ok


class TestRenderAbsentPremiseNote:
    """The note body isolate_activity writes to the bead when
    absent_premise_paths finds something (AC-3: surfaced where a person
    actually looks, not only a log line)."""

    def test_names_every_absent_path(self):
        body = guards.render_absent_premise_note(
            ("apps/mcp-hub/tests/test_audit_shape.py", "apps/gone/x.py")
        )
        assert body.startswith(guards.ABSENT_PREMISE_NOTE_PREFIX)
        assert "apps/mcp-hub/tests/test_audit_shape.py" in body
        assert "apps/gone/x.py" in body

    def test_states_it_is_advisory_and_did_not_stop_the_worker(self):
        body = guards.render_absent_premise_note(("apps/gone/x.py",))
        assert "advisory" in body.lower()


class TestReviewDiffScratchRegression:
    """dev.task 4f24656a (SRE finding, 2026-09-10): a worker wrote its own
    self-review diff to `.review.diff` at the repository root because it had
    nowhere declared to put it, and the scope guard correctly discarded an
    otherwise complete and correct run. The fix (dispatch.py's
    WORKER_SCRATCH_ENV_VAR / _worker_scratch_instruction) gives the worker a
    declared place to write ephemera outside the clone; it must not widen
    what this guard tolerates inside the clone. This is the exact shape that
    convicted attempt 1 of that bead."""

    def test_review_diff_at_repo_root_is_still_out_of_scope(self):
        v = guards.check_scope(
            [
                ".review.diff",
                "apps/factory-dispatcher/dispatch.py",
                "apps/factory-dispatcher/tests/test_dispatch.py",
            ],
            scope(["apps/factory-dispatcher/"]),
        )
        assert not v.ok
        assert v.out_of_scope == (".review.diff",)


# ---------------------------------------------------------------------------
# collaboration gating
# ---------------------------------------------------------------------------


class TestQuestionGating:
    def test_source_bead_ids_make_supersession_discoverable_from_the_old_task(self):
        old = task_with_id("f8237e30")
        replacement = task_with_id("1eef771a", source_bead_ids=["f8237e30"])

        assert guards.superseding_task_ids(old, [old, replacement]) == ("1eef771a",)

    def test_no_source_bead_ids_means_no_supersession(self):
        old = task_with_id("f8237e30")
        unrelated = task_with_id("1eef771a")

        assert guards.superseding_task_ids(old, [old, unrelated]) == ()

    def test_superseded_task_is_not_runnable(self):
        old = task_with_id("f8237e30")
        replacement = task_with_id("1eef771a", source_bead_ids=["f8237e30"])

        verdict = guards.is_runnable(old, [], [old, replacement])

        assert not verdict.ok
        assert "superseded by bead 1eef771a" in verdict.reason

    def test_unlanded_predecessor_blocks_and_names_the_bead(self):
        predecessor = task_with_id("P10-A")
        predecessor["state"] = "review"
        dependent = task_with_id("P10-B", predecessor_bead_ids=["P10-A"])

        verdict = guards.is_runnable(dependent, [], [predecessor, dependent])

        assert not verdict.ok
        assert "predecessor bead P10-A" in verdict.reason

    def test_landed_predecessor_allows_ordering_to_clear(self):
        predecessor = task_with_id("P10-A")
        predecessor["state"] = "done"
        dependent = task_with_id("P10-B", predecessor_bead_ids=["P10-A"])

        assert guards.is_runnable(dependent, [], [predecessor, dependent]).ok

    def test_absent_predecessor_field_behaves_like_no_ordering_constraint(self):
        dependent = task_with_id("P10-B")

        assert guards.is_runnable(dependent, [], [dependent]).ok

    def test_unresolvable_predecessor_blocks(self):
        dependent = task_with_id("P10-B", predecessor_bead_ids=["missing-bead"])

        verdict = guards.is_runnable(dependent, [], [dependent])

        assert not verdict.ok
        assert "predecessor bead missing-bead" in verdict.reason
        assert "not found" in verdict.reason

    def test_held_predecessor_is_a_contradiction_not_a_wait(self):
        # The 2026-08-20 shape: S24-B1's predecessor was held under the
        # supersede-and-settle convention (named in a replacement's
        # source_bead_ids) and could never land. Every drain read "waiting for
        # predecessor bead ... to land (state=pending)" as though it were
        # merely slow.
        held = task_with_id("24f494d4")
        held["state"] = "pending"
        replacement = task_with_id("landed-repl", source_bead_ids=["24f494d4"])
        dependent = task_with_id("e089521b", predecessor_bead_ids=["24f494d4"])

        verdict = guards.is_runnable(
            dependent, [], [held, replacement, dependent]
        )

        assert not verdict.ok
        assert "is held" in verdict.reason
        assert "can never land" in verdict.reason
        assert "superseded by bead landed-repl" in verdict.reason
        assert "waiting" not in verdict.reason

    def test_repointing_at_the_landed_replacement_clears_the_block(self):
        held = task_with_id("24f494d4")
        held["state"] = "pending"
        replacement = task_with_id("landed-repl", source_bead_ids=["24f494d4"])
        replacement["state"] = "done"
        dependent = task_with_id("e089521b", predecessor_bead_ids=["landed-repl"])

        verdict = guards.is_runnable(
            dependent, [], [held, replacement, dependent]
        )

        assert verdict.ok

    # -- OPS-13: supersession is a graph operation ---------------------------

    def test_dependents_of_finds_every_bead_naming_the_predecessor(self):
        old = task_with_id("f9fc8ff9")
        dep_a = task_with_id("ef70d9fa", predecessor_bead_ids=["f9fc8ff9"])
        dep_b = task_with_id("f83f3de2", predecessor_bead_ids=["f9fc8ff9"])
        unrelated = task_with_id("9cedacad", predecessor_bead_ids=["other"])

        found = guards.dependents_of("f9fc8ff9", [old, dep_a, dep_b, unrelated])

        assert {t["id"] for t in found} == {"ef70d9fa", "f83f3de2"}

    def test_dependents_of_excludes_the_bead_itself(self):
        # A bead cannot be its own predecessor in practice, but the query
        # should not misreport it as a dependent of itself if it somehow did.
        self_referential = task_with_id(
            "loop", predecessor_bead_ids=["loop"]
        )

        assert guards.dependents_of("loop", [self_referential]) == ()

    def test_dependents_of_is_empty_when_nothing_points_at_it(self):
        assert guards.dependents_of("f9fc8ff9", [task_with_id("unrelated")]) == ()

    def test_repoint_predecessor_ids_swaps_the_dead_id_for_the_replacement(self):
        result = guards.repoint_predecessor_ids(
            ["f9fc8ff9"], "f9fc8ff9", "658a1d8a"
        )

        assert result == ("658a1d8a",)

    def test_repoint_predecessor_ids_leaves_unrelated_ids_untouched(self):
        result = guards.repoint_predecessor_ids(
            ["other-a", "f9fc8ff9", "other-b"], "f9fc8ff9", "658a1d8a"
        )

        assert result == ("other-a", "658a1d8a", "other-b")

    def test_repoint_predecessor_ids_does_not_duplicate_an_already_named_replacement(self):
        # A dependent that already names both the superseded bead and its
        # replacement must not end up naming the replacement twice.
        result = guards.repoint_predecessor_ids(
            ["f9fc8ff9", "658a1d8a"], "f9fc8ff9", "658a1d8a"
        )

        assert result == ("658a1d8a",)

    def test_superseded_predecessors_names_the_stranded_edge(self):
        held = task_with_id("f9fc8ff9")
        replacement = task_with_id("658a1d8a", source_bead_ids=["f9fc8ff9"])
        dependent = task_with_id("ef70d9fa", predecessor_bead_ids=["f9fc8ff9"])

        faults = guards.superseded_predecessors(
            dependent, [held, replacement, dependent]
        )

        assert faults == (("f9fc8ff9", ("658a1d8a",)),)

    def test_superseded_predecessors_is_empty_for_a_live_predecessor(self):
        live = task_with_id("f9fc8ff9")
        dependent = task_with_id("ef70d9fa", predecessor_bead_ids=["f9fc8ff9"])

        assert guards.superseded_predecessors(dependent, [live, dependent]) == ()

    def test_predecessor_cycle_is_reported(self):
        first = task_with_id("P10-A", predecessor_bead_ids=["P10-B"])
        second = task_with_id("P10-B", predecessor_bead_ids=["P10-A"])

        verdict = guards.is_runnable(first, [], [first, second])

        assert not verdict.ok
        assert "predecessor cycle" in verdict.reason
        assert "P10-A" in verdict.reason
        assert "P10-B" in verdict.reason

    def test_unanswered_question_is_open(self):
        notes = [note("q1", "question", blocking=True)]
        assert [n["id"] for n in guards.open_questions(notes)] == ["q1"]

    def test_answer_closes_its_own_question_only(self):
        notes = [
            note("q1", "question", blocking=True),
            note("q2", "question", blocking=True),
            note("a1", "answer", answers_ref="q1", releases_work=True),
        ]
        assert [n["id"] for n in guards.open_questions(notes)] == ["q2"]

    def test_answer_without_releases_work_field_does_not_close_the_question(self):
        """The 2026-08-29 incident: an answer that exists but never says it
        releases the work must not close the question it replies to."""
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", body="wait for rest of work to merge/clear", answers_ref="q1"),
        ]
        assert [n["id"] for n in guards.open_questions(notes)] == ["q1"]
        assert [n["id"] for n in guards.blocking_questions(notes)] == ["q1"]

    def test_answer_with_releases_work_false_does_not_close_the_question(self):
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", answers_ref="q1", releases_work=False),
        ]
        assert [n["id"] for n in guards.open_questions(notes)] == ["q1"]

    def test_answer_with_dangling_ref_closes_nothing(self):
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", answers_ref="some-other-thread"),
        ]
        assert [n["id"] for n in guards.blocking_questions(notes)] == ["q1"]

    def test_answer_with_dangling_ref_is_reported_as_orphaned(self):
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", answers_ref="some-other-thread"),
        ]
        assert [n["id"] for n in guards.orphaned_answers(notes)] == ["a1"]

    def test_answer_referring_to_thread_note_is_not_orphaned(self):
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", answers_ref="q1"),
        ]
        assert guards.orphaned_answers(notes) == []

    def test_non_blocking_question_does_not_gate(self):
        notes = [note("q1", "question", blocking=False)]
        assert guards.open_questions(notes)
        assert guards.blocking_questions(notes) == []

    def test_task_with_blocking_question_is_not_runnable(self):
        notes = [note("q1", "question", body="Which one?", blocking=True)]
        verdict = guards.is_runnable(task(), notes)
        assert not verdict.ok
        assert "blocking question" in verdict.reason
        assert "q1" in verdict.reason
        assert "Which one?" in verdict.reason

    def test_task_becomes_runnable_once_answered_with_release(self):
        notes = [
            note("q1", "question", blocking=True),
            note("a1", "answer", answers_ref="q1", releases_work=True),
        ]
        assert guards.is_runnable(task(), notes).ok

    def test_answer_declaring_hold_leaves_question_blocking_and_task_not_runnable(self):
        """Acceptance: an answer stating it does not release the work must
        keep the question blocking and is_runnable false."""
        notes = [
            note("q1", "question", body="Which one?", blocking=True),
            note("a1", "answer", body="hold until things clear", answers_ref="q1",
                 releases_work=False),
        ]
        verdict = guards.is_runnable(task(), notes)
        assert not verdict.ok
        assert "q1" in verdict.reason
        assert [n["id"] for n in guards.blocking_questions(notes)] == ["q1"]

    def test_answer_with_no_disposition_field_does_not_release_the_work(self):
        notes = [
            note("q1", "question", body="Which one?", blocking=True),
            note("a1", "answer", body="wait for rest of work to merge/clear",
                 answers_ref="q1"),
        ]
        verdict = guards.is_runnable(task(), notes)
        assert not verdict.ok
        assert [n["id"] for n in guards.blocking_questions(notes)] == ["q1"]

    def test_ambiguous_answer_reason_differs_from_unanswered_reason(self):
        """The hold is reportable, not silent: an answered-but-ambiguous
        question refuses with a different reason than an unanswered one."""
        unanswered = [note("q1", "question", body="Which one?", blocking=True)]
        answered_ambiguously = [
            note("q1", "question", body="Which one?", blocking=True),
            note("a1", "answer", body="hold until things clear", answers_ref="q1"),
        ]
        unanswered_reason = guards.is_runnable(task(), unanswered).reason
        ambiguous_reason = guards.is_runnable(task(), answered_ambiguously).reason
        assert unanswered_reason != ambiguous_reason
        assert "does not declare release" in ambiguous_reason

    def test_exhausted_attempts_blocks(self):
        notes = [
            note(f"f{i}", "status", body=f"Run failed: {i}")
            for i in range(1, 4)
        ]
        verdict = guards.is_runnable(task(), notes)
        assert not verdict.ok
        assert "attempts exhausted" in verdict.reason

    def test_attempts_below_max_is_runnable(self):
        notes = [
            note(f"f{i}", "status", body=f"Run failed: {i}")
            for i in range(1, 3)
        ]
        assert guards.is_runnable(task(), notes).ok

    def test_retry_bound_comes_from_policy_not_bead_max_attempts(self):
        notes = [
            note(f"f{i}", "status", body=f"Run failed: {i}")
            for i in range(1, 3)
        ]
        assert guards.is_runnable(task(max_attempts=2), notes).ok

        notes.append(note("f3", "status", body="Run failed: 3"))
        verdict = guards.is_runnable(task(max_attempts=99), notes)

        assert not verdict.ok
        assert "attempts exhausted (3/3" in verdict.reason

    def test_a_recorded_requeue_restarts_the_budget(self):
        # The #342 regression. On 2026-08-07 twelve beads burned their budget
        # against a stale local main whose clone carried a pre-#313 guard, so
        # verification failed regardless of worker output. Requeuing them was
        # correct; counting those failures afterwards strands them forever.
        notes = [
            note("f1", "status", body="Run failed: guard", created_at="2026-08-07T09:00:00Z"),
            note("f2", "status", body="Run failed: guard", created_at="2026-08-07T09:15:00Z"),
            note("f3", "status", body="Run failed: guard", created_at="2026-08-07T09:30:00Z"),
            note("rq", "status", body="Returned to pending by an operator",
                 created_at="2026-08-07T22:44:00Z", resets_attempts=True),
        ]

        assert guards.is_runnable(task(), notes).ok

    def test_the_2026_08_07_requeue_is_honoured_without_the_marker(self):
        # Those twelve beads predate the marker and are recorded in prose.
        notes = [
            note(f"f{i}", "status", body="Run failed: guard",
                 created_at=f"2026-08-07T09:{i}0:00Z")
            for i in range(1, 4)
        ] + [
            note("rq", "status", created_at="2026-08-07T22:44:00Z",
                 body="Returned to pending by the SRE on 2026-08-07, with attempts reset to 0.")
        ]

        assert guards.is_runnable(task(), notes).ok

    def test_failures_after_a_requeue_still_exhaust_the_budget(self):
        notes = [
            note("f0", "status", body="Run failed: old", created_at="2026-08-07T09:00:00Z"),
            note("rq", "status", body="Returned to pending by an operator",
                 created_at="2026-08-07T22:44:00Z", resets_attempts=True),
        ] + [
            note(f"n{i}", "status", body=f"Run failed: real {i}",
                 created_at=f"2026-08-08T0{i}:00:00Z")
            for i in range(1, 4)
        ]
        verdict = guards.is_runnable(task(), notes)

        assert not verdict.ok
        assert "attempts exhausted (3/3" in verdict.reason

    def test_only_the_most_recent_requeue_sets_the_barrier(self):
        notes = [
            note("rq1", "status", body="Returned to pending by an operator",
                 created_at="2026-08-07T22:44:00Z", resets_attempts=True),
            note("f1", "status", body="Run failed: a", created_at="2026-08-08T01:00:00Z"),
            note("f2", "status", body="Run failed: b", created_at="2026-08-08T02:00:00Z"),
            note("rq2", "status", body="Returned to pending by an operator",
                 created_at="2026-08-08T03:00:00Z", resets_attempts=True),
            note("f3", "status", body="Run failed: c", created_at="2026-08-08T04:00:00Z"),
        ]
        verdict = guards.is_runnable(task(), notes)

        assert verdict.ok
        assert [f.reason for f in guards.prior_failures(notes)] == ["c"]

    def test_historical_content_counter_does_not_grant_fresh_budget(self):
        # Patching or preserving historical content.attempts must not change
        # runnability. The durable local record is the failure notes.
        notes = [
            note(f"f{i}", "status", body=f"Run failed: {i}") for i in range(1, 4)
        ]
        verdict = guards.is_runnable(task(attempts=0, max_attempts=3), notes)

        assert not verdict.ok
        assert "per recorded failures" in verdict.reason

    def test_a_requeue_note_is_not_itself_counted_as_a_failure(self):
        notes = [
            note("rq", "status", body="Returned to pending by an operator",
                 created_at="2026-08-07T22:44:00Z", resets_attempts=True),
        ]

        assert guards.prior_failures(notes) == []

    def test_recorded_failures_outrank_a_counter_that_was_reset(self):
        # 2026-08-07: an operator requeued five ceiling-hit beads and the
        # content update zeroed every counter. The recorded failure notes are
        # what prevents the same false fresh budget now.
        notes = [
            note(f"f{i}", "status", body=f"Run failed: attempt {i}")
            for i in range(1, 4)
        ]
        verdict = guards.is_runnable(task(), notes)

        assert not verdict.ok
        assert "attempts exhausted (3/3, per recorded failures)" in verdict.reason

    def test_historical_content_counter_is_ignored_when_ahead_of_the_record(self):
        verdict = guards.is_runnable(task(attempts=3, max_attempts=3), [])

        assert verdict.ok

    def test_capacity_backpressure_is_not_a_recorded_attempt(self):
        # #333 established that capacity is not failure. A paused schedule must
        # not consume the retry budget.
        notes = [
            note(f"c{i}", "status", body="Capacity backpressure: usage limit")
            for i in range(1, 5)
        ]

        assert guards.is_runnable(task(), notes).ok

    def test_recorded_failures_below_max_stay_runnable(self):
        notes = [note("f1", "status", body="Run failed: once")]

        assert guards.is_runnable(task(), notes).ok

    def test_notes_are_read_once_so_a_generator_still_gates(self):
        # notes is an Iterable; reading it twice would silently drop the
        # blocking-question check.
        notes = iter([note("q1", "question", body="Which one?", blocking=True)])
        verdict = guards.is_runnable(task(), notes)

        assert not verdict.ok
        assert "blocking question" in verdict.reason

    def test_missing_scope_paths_blocks(self):
        verdict = guards.is_runnable(task(scope=scope([])), [])
        assert not verdict.ok
        assert "no scope.paths" in verdict.reason


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_includes_intent_acceptance_and_boundaries(self):
        prompt = guards.build_prompt(task(), [])
        assert "## Acceptance criteria" in prompt
        assert "apps/mcp-hub/" in prompt
        assert ".github/workflows/**" in prompt

    def test_answered_question_travels_into_the_prompt(self):
        # The point of the round-trip: the human's decision reaches the worker
        # attached to the question it settled.
        notes = [
            note("q1", "question", body="Fix the metering gap too?", blocking=True,
                 created_at="2026-07-29T01:00:00Z"),
            note("a1", "answer", body="No — file it separately.", answers_ref="q1",
                 created_at="2026-07-29T02:00:00Z"),
        ]
        prompt = guards.build_prompt(task(), notes)
        assert "Decisions already made" in prompt
        assert "Fix the metering gap too?" in prompt
        assert "No — file it separately." in prompt
        assert "binding" in prompt

    def test_unanswered_question_is_not_presented_as_a_decision(self):
        notes = [note("q1", "question", body="Which?", blocking=True)]
        prompt = guards.build_prompt(task(), notes)
        assert "Decisions already made" not in prompt

    def test_answered_pairs_are_ordered_oldest_first(self):
        notes = [
            note("q1", "question", body="first?", created_at="2026-07-29T01:00:00Z"),
            note("a1", "answer", body="A1", answers_ref="q1",
                 created_at="2026-07-29T02:00:00Z"),
            note("q2", "question", body="second?", created_at="2026-07-29T03:00:00Z"),
            note("a2", "answer", body="A2", answers_ref="q2",
                 created_at="2026-07-29T04:00:00Z"),
        ]
        pairs = guards.answered_pairs(notes)
        assert [p.answer for p in pairs] == ["A1", "A2"]

    def test_structural_risk_adds_the_no_drive_by_fixes_instruction(self):
        prompt = guards.build_prompt(task(risk_class="structural"), [])
        assert "behaviour must be identical" in prompt
        assert "add new test coverage" in prompt
        assert "do not weaken or rewrite existing assertions" in prompt
        assert "do not edit tests" not in prompt

    def test_behavioral_risk_omits_the_structural_warning(self):
        prompt = guards.build_prompt(task(risk_class="behavioral"), [])
        assert "behaviour must be identical" not in prompt

    def test_verification_section_states_dispatcher_reruns_declared_checks(self):
        prompt = guards.build_prompt(task(), [])
        assert "dispatcher will run the declared verification again" in prompt
        assert "which could not run" in prompt

    def test_feature_lane_names_requirement_ids_and_nfr_statement_and_threshold(self):
        prompt = guards.build_prompt(
            task(
                lane="feature",
                requirement_refs=["LO-042/AC-1", "LO-042/AC-2"],
                nfrs=[
                    {
                        "category": "latency",
                        "statement": "The board renders within budget.",
                        "threshold": "p95 < 200ms",
                        "verification": "load test",
                    }
                ],
            ),
            [],
        )
        assert "## Requirement contract" in prompt
        assert "LO-042/AC-1" in prompt
        assert "LO-042/AC-2" in prompt
        assert "The board renders within budget." in prompt
        assert "p95 < 200ms" in prompt

    def test_feature_lane_with_no_contract_fields_omits_the_section(self):
        prompt = guards.build_prompt(task(lane="feature"), [])
        assert "## Requirement contract" not in prompt

    def test_non_feature_lane_prompt_unchanged_by_requirement_refs_and_nfrs(self):
        # Same input, sans lane, dispatched through code-health/drift/bug-triage
        # must render byte-identically to before this feature existed.
        without_contract_fields = guards.build_prompt(task(lane="code-health"), [])
        with_contract_fields = guards.build_prompt(
            task(
                lane="code-health",
                requirement_refs=["LO-042/AC-1"],
                nfrs=[
                    {
                        "category": "latency",
                        "statement": "x",
                        "threshold": "y",
                        "verification": "z",
                    }
                ],
            ),
            [],
        )
        assert with_contract_fields == without_contract_fields
        assert "## Requirement contract" not in with_contract_fields

    def test_render_requirement_contract_empty_for_non_feature_lane(self):
        content = {
            "lane": "drift",
            "requirement_refs": ["LO-042/AC-1"],
            "nfrs": [{"statement": "x", "threshold": "y"}],
        }
        assert guards.render_requirement_contract(content) == ""


# ---------------------------------------------------------------------------
# resume section: 2026-09-13, FA-S49-1/FA-S49-2 re-verified an applied
# preserved baseline against acceptance criteria instead of acting on the
# gate's review note or a reasoned requeue, and burned the attempt.
# ---------------------------------------------------------------------------


PRESERVED_PR = "https://github.com/example/repo/pull/42"


def preserved_task(preserved=None, **content):
    preserved = (
        [{"pr": PRESERVED_PR, "head_sha": "abc1234def5678"}]
        if preserved is None
        else preserved
    )
    return task(preserved_attempts=preserved, **content)


class TestResumeSection:
    def test_no_preserved_attempts_renders_nothing(self):
        assert guards.render_resume_section(task()["content"], []) == ""

    def test_preserved_attempts_with_no_qualifying_note_renders_nothing(self):
        notes = [note("s1", "status", body="unrelated status update")]
        content = preserved_task()["content"]
        assert guards.render_resume_section(content, notes) == ""

    def test_attachment_of_another_pr_ends_the_binding_window(self):
        # The #805 gate F3: attachment(preserved PR) -> attachment(other PR) ->
        # review without pr_url. The review reads as the other PR's, not the
        # preserved one's, so nothing binds and nothing renders.
        notes = [
            note("a1", "attachment", url=PRESERVED_PR, created_at="2026-09-13T00:00:00Z"),
            note(
                "a2",
                "attachment",
                url="https://github.com/example/repo/pull/99",
                created_at="2026-09-13T00:01:00Z",
            ),
            note(
                "r1",
                "review",
                body="Findings for PR 99.",
                verdict="request-changes",
                created_at="2026-09-13T00:02:00Z",
            ),
        ]
        assert guards.render_resume_section(preserved_task()["content"], notes) == ""

    def test_prompt_is_byte_identical_without_a_qualifying_note(self):
        # AC2: a first attempt (no preserved_attempts) and a resume after a
        # plain environmental failure (preserved_attempts present, nothing
        # bound to it) must render exactly like before this feature existed.
        plain = guards.build_prompt(task(), [])
        first_attempt_notes = [note("f1", "status", body="Run failed: timeout")]
        # No preserved_attempts: the failure note is prior-failure context, and
        # the resume section must not appear (the #805 gate F4 replaced a
        # self-comparison here with this assertion).
        first_attempt = guards.build_prompt(task(), first_attempt_notes)
        assert "Resuming from applied preserved work" not in first_attempt
        assert first_attempt != plain  # the failure note IS rendered, as before

        with_unrelated_preserved = guards.build_prompt(
            preserved_task(), [note("s1", "status", body="unrelated")]
        )
        without_preserved = guards.build_prompt(task(), [note("s1", "status", body="unrelated")])
        assert with_unrelated_preserved == without_preserved
        assert "Resuming from applied preserved work" not in plain

    def test_request_changes_review_bound_by_pr_url_leads_the_brief(self):
        notes = [
            note(
                "r1",
                "review",
                body="MERGE-WITH-CHANGES: the drift check double-counts renamed files.",
                verdict="request-changes",
                pr_url=PRESERVED_PR,
                created_at="2026-09-13T00:10:00Z",
            )
        ]
        content = preserved_task()["content"]

        section = guards.render_resume_section(content, notes)
        assert "## Resuming from applied preserved work" in section
        assert PRESERVED_PR in section
        assert "abc1234d" in section
        assert "MERGE-WITH-CHANGES: the drift check double-counts renamed files." in section
        assert "not the task" in section or "NOT" in section

        prompt = guards.build_prompt(preserved_task(), notes)
        assert prompt.index("Resuming from applied preserved work") < prompt.index(
            "## Acceptance criteria"
        )

    def test_request_changes_review_bound_by_preceding_attachment_note(self):
        # A review filed without its own pr_url binds to the PR named by the
        # nearest preceding attachment note.
        notes = [
            note(
                "a1", "attachment", body="PR opened", url=PRESERVED_PR,
                created_at="2026-09-13T00:00:00Z",
            ),
            note(
                "r1", "review", body="Findings from the gate.",
                verdict="request-changes", created_at="2026-09-13T00:05:00Z",
            ),
        ]
        content = preserved_task()["content"]

        section = guards.render_resume_section(content, notes)
        assert "Findings from the gate." in section

    def test_review_note_not_bound_to_a_preserved_pr_is_ignored(self):
        notes = [
            note(
                "r1", "review", body="Unrelated PR's findings.",
                verdict="request-changes", pr_url="https://github.com/example/repo/pull/999",
                created_at="2026-09-13T00:10:00Z",
            )
        ]
        content = preserved_task()["content"]
        assert guards.render_resume_section(content, notes) == ""

    def test_approving_review_does_not_trigger_the_section(self):
        notes = [
            note(
                "r1", "review", body="Looks good.", verdict="approve",
                pr_url=PRESERVED_PR, created_at="2026-09-13T00:10:00Z",
            )
        ]
        content = preserved_task()["content"]
        assert guards.render_resume_section(content, notes) == ""

    def test_reasoned_requeue_after_the_barrier_leads_the_brief_when_no_review(self):
        notes = [
            note(
                "rq", "status",
                body=(
                    "Returned to pending by an operator, with the retry budget "
                    "restarted. Recorded failures before this point were judged "
                    "not to have been spent on this task's work. Reason: lead "
                    "with F1's findings, not a re-audit. Preserved work from 1 "
                    "closed attempt(s): keep."
                ),
                resets_attempts=True,
                created_at="2026-09-13T01:00:00Z",
            )
        ]
        content = preserved_task()["content"]

        section = guards.render_resume_section(content, notes)
        assert "lead with F1's findings, not a re-audit." in section
        assert "Preserved work from 1 closed attempt(s)" not in section

    def test_both_review_and_requeue_reason_render_together(self):
        notes = [
            note(
                "r1", "review", body="Findings X.", verdict="request-changes",
                pr_url=PRESERVED_PR, created_at="2026-09-13T00:10:00Z",
            ),
            note(
                "rq", "status",
                body="Returned to pending by an operator. Reason: reason Y.",
                resets_attempts=True, created_at="2026-09-13T01:00:00Z",
            ),
        ]
        content = preserved_task()["content"]

        section = guards.render_resume_section(content, notes)
        assert "Findings X." in section
        assert "reason Y." in section

    def test_legacy_requeue_with_no_marker_and_no_reason_yields_nothing(self):
        notes = [
            note(
                "rq", "status", created_at="2026-09-13T01:00:00Z",
                body="Returned to pending by the SRE on 2026-09-13, with attempts reset to 0.",
            )
        ]
        content = preserved_task()["content"]
        assert guards.render_resume_section(content, notes) == ""

    def test_a_stale_requeue_reason_before_the_current_barrier_does_not_leak(self):
        # A resets_attempts note WITH a parseable Reason, followed by a later
        # resets_attempts note with none, must not surface the older text --
        # the barrier moved and the old reason is not this attempt's task.
        notes = [
            note(
                "rq1", "status", created_at="2026-09-13T01:00:00Z",
                body="Returned to pending by an operator. Reason: old reason.",
                resets_attempts=True,
            ),
            note(
                "rq2", "status", created_at="2026-09-13T02:00:00Z",
                body="Returned to pending by an operator, no reason recorded.",
                resets_attempts=True,
            ),
        ]
        content = preserved_task()["content"]
        assert guards.render_resume_section(content, notes) == ""

    def test_preserved_entry_missing_a_pr_field_yields_nothing(self):
        content = preserved_task(preserved=[{"head_sha": "abc123"}])["content"]
        notes = [
            note(
                "r1", "review", body="Findings.", verdict="request-changes",
                created_at="2026-09-13T00:10:00Z",
            )
        ]
        assert guards.render_resume_section(content, notes) == ""


# ---------------------------------------------------------------------------
# requeue fallback: dev.finding 4b00324c, measured on bead 5bb23acc
# 2026-09-17/18. resume_context/render_resume_section only construct when a
# preserved attempt carries a usable `pr` -- so a bead that dies before it
# ever opens one never reaches that channel, and an operator's requeue
# diagnosis (and, once the requeue barrier resets prior_failures, the
# pre-barrier failure history) is silently dropped. build_prompt's fallback
# channel is what closes that gap; render_resume_section itself is untouched.
# ---------------------------------------------------------------------------


class TestRequeueFallbackSection:
    def test_first_attempt_with_no_barrier_renders_nothing(self):
        # AC-3: no preserved_attempts and no requeue reason must stay exactly
        # as empty as before this fallback existed.
        assert guards.render_requeue_fallback_section(task()["content"], []) == ""
        prompt = guards.build_prompt(task(), [])
        assert "Why this bead was requeued" not in prompt
        assert "Why earlier attempts failed, before this bead was requeued" not in prompt

    def test_plain_retry_with_no_barrier_still_renders_nothing(self):
        # A resume after a plain environmental failure (no requeue at all) is
        # the other case resume_context's docstring names as byte-identical.
        notes = [note("f1", "status", body="Run failed: timeout")]
        assert guards.render_requeue_fallback_section(task()["content"], notes) == ""

    def test_a_requeue_reason_reaches_the_worker_with_no_preserved_pr(self):
        # AC-1 / AC-4: bead 5bb23acc's measured shape -- preserved_attempts
        # empty (it never opened a PR), a resets_attempts note carrying a
        # Reason. The dropped-reason defect was exactly that this rendered "".
        notes = [
            note(
                "rq", "status",
                body=(
                    "Returned to pending by an operator, with the retry budget "
                    "restarted. Reason: the assertion fails because the fixture "
                    "asserts against a stale schema; update the fixture, not the "
                    "assertion."
                ),
                resets_attempts=True,
                created_at="2026-09-17T00:43:00Z",
            )
        ]
        content = task()["content"]

        # Preconditions matching the measured defect: the existing channel is
        # empty for this shape, and the reason is present and parseable.
        assert guards.resume_context(content, notes) is None
        assert guards.render_resume_section(content, notes) == ""
        assert guards._barrier_requeue_reason(notes) is not None

        section = guards.render_requeue_fallback_section(content, notes)
        assert "there is no prior pull request" in section.lower()
        assert (
            "update the fixture, not the assertion" in section
        )

        prompt = guards.build_prompt(task(), notes)
        assert "update the fixture, not the assertion" in prompt
        assert prompt.index("Why this bead was requeued") < prompt.index(
            "## Acceptance criteria"
        )

    def test_a_requeue_without_a_preserved_pr_does_not_reset_the_attempt_budget(self):
        # AC-7's second half: the fix must be prompt content only. The barrier
        # arithmetic prior_failures()/is_runnable() use is untouched.
        notes = [
            note("f1", "status", body="Run failed: a", created_at="2026-09-17T00:00:00Z"),
            note("f2", "status", body="Run failed: b", created_at="2026-09-17T00:10:00Z"),
            note(
                "rq", "status",
                body="Returned to pending by an operator. Reason: diagnosis here.",
                resets_attempts=True,
                created_at="2026-09-17T00:43:00Z",
            ),
        ]
        assert guards.prior_failures(notes) == []
        assert guards.is_runnable(task(), notes).ok

    def test_pre_barrier_failures_fill_in_when_the_requeue_carries_no_reason(self):
        # AC-7: a legacy/reason-less requeue with no preserved PR must not
        # leave the next worker with *less* than an unrequeued retry would
        # have shown -- the reset failure history is recovered, clearly
        # labelled as pre-barrier so it is never confused with the (reset)
        # attempt count.
        notes = [
            note("f1", "status", body="Run failed: first cause",
                 created_at="2026-09-17T00:00:00Z"),
            note("f2", "status", body="Run failed: second cause",
                 created_at="2026-09-17T00:10:00Z"),
            note(
                "rq", "status", created_at="2026-09-17T00:43:00Z",
                body="Returned to pending by the SRE on 2026-09-17, with attempts reset to 0.",
            ),
        ]
        content = task()["content"]

        assert guards._barrier_requeue_reason(notes) is None
        assert guards.prior_failures(notes) == []  # budget arithmetic: unchanged

        section = guards.render_requeue_fallback_section(content, notes)
        assert "Why earlier attempts failed, before this bead was requeued" in section
        assert "first cause" in section
        assert "second cause" in section
        assert "**Pre-requeue attempt 1:** first cause" in section

        prompt = guards.build_prompt(task(), notes)
        assert "second cause" in prompt

    def test_no_pre_barrier_failures_and_no_reason_renders_nothing(self):
        notes = [
            note(
                "rq", "status", created_at="2026-09-17T00:43:00Z",
                body="Returned to pending by the SRE on 2026-09-17, with attempts reset to 0.",
            ),
        ]
        assert guards.render_requeue_fallback_section(task()["content"], notes) == ""

    def test_with_pr_and_bound_reason_the_existing_channel_is_untouched(self):
        # AC-2 / the #919 gate F2 clarification: when render_resume_section
        # already renders (a preserved PR with a bound review or requeue
        # reason), the fallback must not fire and must not alter the prompt
        # by one byte.
        notes = [
            note(
                "rq", "status",
                body=(
                    "Returned to pending by an operator, with the retry budget "
                    "restarted. Reason: lead with F1's findings, not a "
                    "re-audit."
                ),
                resets_attempts=True,
                created_at="2026-09-13T01:00:00Z",
            )
        ]
        content = preserved_task()["content"]

        existing_section = guards.render_resume_section(content, notes)
        assert existing_section != ""

        prompt = guards.build_prompt(preserved_task(), notes)
        assert "Why this bead was requeued" not in prompt
        assert "lead with F1's findings, not a re-audit." in prompt

    def test_reason_outranks_pre_barrier_summary_when_both_are_available(self):
        notes = [
            note("f1", "status", body="Run failed: earlier cause",
                 created_at="2026-09-17T00:00:00Z"),
            note(
                "rq", "status",
                body="Returned to pending by an operator. Reason: the real diagnosis.",
                resets_attempts=True,
                created_at="2026-09-17T00:43:00Z",
            ),
        ]
        content = task()["content"]

        section = guards.render_requeue_fallback_section(content, notes)
        assert "the real diagnosis" in section
        assert "earlier cause" not in section
        assert "Why earlier attempts failed, before this bead was requeued" not in section


# ---------------------------------------------------------------------------
# golden renderings: dev.finding absorbed 2026-09-18, #933's F1. AC-2 of #933
# required the with-PR rendering be proven byte-identical and warned against a
# truthy/substring test -- exactly what shipped instead. These pin build_prompt's
# full output (not just render_resume_section's return) because the property
# these tests exist to protect is the `if not resume_section:` guard at the
# call site in build_prompt: render_resume_section itself does not change when
# that guard moves, only what build_prompt does with its result does.
# ---------------------------------------------------------------------------


GOLDEN_WITH_PR_REVIEW_ONLY = """\
# Task: t

i

## Resuming from applied preserved work

This attempt resumes from preserved work already applied as its starting point: https://github.com/example/repo/pull/42 at abc1234d. Read its discussion with `gh pr view https://github.com/example/repo/pull/42 --json comments` if you need more than what is quoted below.

Re-verifying that baseline against the acceptance criteria is NOT the task, and reporting it as already satisfied without making a change is a refused outcome. The task is:

**The review's request-changes findings:**

MERGE-WITH-CHANGES: the drift check double-counts renamed files.

The acceptance criteria below are the frame for that work, not a checklist to re-verify the baseline against.

## Acceptance criteria
- a

## Hard boundaries
You may only create or modify files under: apps/mcp-hub/
You must not touch: .github/workflows/**
A change outside those paths fails the whole run and is discarded, however good it is. If the task appears to require touching something out of scope, stop and say so instead of doing it.

This is a **structural** change: behaviour must be identical afterwards. Do not fix unrelated inconsistencies you notice along the way, do not add defaults to required configuration, and do not weaken or rewrite existing assertions to make the change pass. You may add new test coverage when the task asks for it.

## Verification
Run what you can of:
- `pytest -q`
State plainly which checks you ran, which failed, and which could not run. The dispatcher will run the declared verification again inside its isolated clone before it opens a PR.
"""


GOLDEN_WITH_PR_REQUEUE_REASON_ONLY = """\
# Task: t

i

## Resuming from applied preserved work

This attempt resumes from preserved work already applied as its starting point: https://github.com/example/repo/pull/42 at abc1234d. Read its discussion with `gh pr view https://github.com/example/repo/pull/42 --json comments` if you need more than what is quoted below.

Re-verifying that baseline against the acceptance criteria is NOT the task, and reporting it as already satisfied without making a change is a refused outcome. The task is:

**The reason this bead was requeued:**

lead with F1's findings, not a re-audit.

The acceptance criteria below are the frame for that work, not a checklist to re-verify the baseline against.

## Acceptance criteria
- a

## Hard boundaries
You may only create or modify files under: apps/mcp-hub/
You must not touch: .github/workflows/**
A change outside those paths fails the whole run and is discarded, however good it is. If the task appears to require touching something out of scope, stop and say so instead of doing it.

This is a **structural** change: behaviour must be identical afterwards. Do not fix unrelated inconsistencies you notice along the way, do not add defaults to required configuration, and do not weaken or rewrite existing assertions to make the change pass. You may add new test coverage when the task asks for it.

## Verification
Run what you can of:
- `pytest -q`
State plainly which checks you ran, which failed, and which could not run. The dispatcher will run the declared verification again inside its isolated clone before it opens a PR.
"""


GOLDEN_WITH_PR_REVIEW_AND_REQUEUE_REASON = """\
# Task: t

i

## Resuming from applied preserved work

This attempt resumes from preserved work already applied as its starting point: https://github.com/example/repo/pull/42 at abc1234d. Read its discussion with `gh pr view https://github.com/example/repo/pull/42 --json comments` if you need more than what is quoted below.

Re-verifying that baseline against the acceptance criteria is NOT the task, and reporting it as already satisfied without making a change is a refused outcome. The task is:

**The review's request-changes findings:**

Findings X.

**The reason this bead was requeued:**

reason Y.

The acceptance criteria below are the frame for that work, not a checklist to re-verify the baseline against.

## Acceptance criteria
- a

## Hard boundaries
You may only create or modify files under: apps/mcp-hub/
You must not touch: .github/workflows/**
A change outside those paths fails the whole run and is discarded, however good it is. If the task appears to require touching something out of scope, stop and say so instead of doing it.

This is a **structural** change: behaviour must be identical afterwards. Do not fix unrelated inconsistencies you notice along the way, do not add defaults to required configuration, and do not weaken or rewrite existing assertions to make the change pass. You may add new test coverage when the task asks for it.

## Verification
Run what you can of:
- `pytest -q`
State plainly which checks you ran, which failed, and which could not run. The dispatcher will run the declared verification again inside its isolated clone before it opens a PR.
"""


GOLDEN_NO_PR_REQUEUE_REASON_FALLBACK = """\
# Task: t

i

## Why this bead was requeued

There is no prior pull request to resume from. An operator requeued this bead with a diagnosis:

the assertion fails because the fixture asserts against a stale schema; update the fixture, not the assertion.

Treat this as the reason the previous approach was rejected. If your plan would repeat it, STOP and say so instead of attempting it again.

## Acceptance criteria
- a

## Hard boundaries
You may only create or modify files under: apps/mcp-hub/
You must not touch: .github/workflows/**
A change outside those paths fails the whole run and is discarded, however good it is. If the task appears to require touching something out of scope, stop and say so instead of doing it.

This is a **structural** change: behaviour must be identical afterwards. Do not fix unrelated inconsistencies you notice along the way, do not add defaults to required configuration, and do not weaken or rewrite existing assertions to make the change pass. You may add new test coverage when the task asks for it.

## Verification
Run what you can of:
- `pytest -q`
State plainly which checks you ran, which failed, and which could not run. The dispatcher will run the declared verification again inside its isolated clone before it opens a PR.
"""


class TestGoldenRenderings:
    """AC-1/AC-2/AC-4: golden-string pins, not substrings or truthiness.

    #933's AC-2 required the with-PR rendering be proven byte-identical and
    warned specifically against a truthy/substring test -- exactly the gap
    left by ``test_with_pr_and_bound_reason_the_existing_channel_is_untouched``
    below, which this class does not replace (AC-5: that test's assertions
    stay as-is). A single changed character in any of these four shapes fails
    the comparison; a substring test would not have caught it.
    """

    def test_with_pr_and_request_changes_review_only(self):
        notes = [
            note(
                "r1", "review",
                body="MERGE-WITH-CHANGES: the drift check double-counts renamed files.",
                verdict="request-changes",
                pr_url=PRESERVED_PR,
                created_at="2026-09-13T00:10:00Z",
            )
        ]
        prompt = guards.build_prompt(preserved_task(), notes)
        assert prompt == GOLDEN_WITH_PR_REVIEW_ONLY

    def test_with_pr_and_requeue_reason_only_no_review(self):
        notes = [
            note(
                "rq", "status",
                body=(
                    "Returned to pending by an operator, with the retry budget "
                    "restarted. Recorded failures before this point were judged "
                    "not to have been spent on this task's work. Reason: lead "
                    "with F1's findings, not a re-audit. Preserved work from 1 "
                    "closed attempt(s): keep."
                ),
                resets_attempts=True,
                created_at="2026-09-13T01:00:00Z",
            )
        ]
        prompt = guards.build_prompt(preserved_task(), notes)
        assert prompt == GOLDEN_WITH_PR_REQUEUE_REASON_ONLY

    def test_with_pr_and_both_review_and_requeue_reason(self):
        notes = [
            note(
                "r1", "review", body="Findings X.", verdict="request-changes",
                pr_url=PRESERVED_PR, created_at="2026-09-13T00:10:00Z",
            ),
            note(
                "rq", "status",
                body="Returned to pending by an operator. Reason: reason Y.",
                resets_attempts=True, created_at="2026-09-13T01:00:00Z",
            ),
        ]
        prompt = guards.build_prompt(preserved_task(), notes)
        assert prompt == GOLDEN_WITH_PR_REVIEW_AND_REQUEUE_REASON

    def test_no_pr_requeue_reason_fallback(self):
        # AC-4: the mirror of the three with-PR pins above -- the fallback
        # channel (no preserved PR) gets the same golden treatment, so it
        # cannot drift freely while only the untouched paths are protected.
        notes = [
            note(
                "rq", "status",
                body=(
                    "Returned to pending by an operator, with the retry budget "
                    "restarted. Reason: the assertion fails because the fixture "
                    "asserts against a stale schema; update the fixture, not the "
                    "assertion."
                ),
                resets_attempts=True,
                created_at="2026-09-17T00:43:00Z",
            )
        ]
        prompt = guards.build_prompt(task(), notes)
        assert prompt == GOLDEN_NO_PR_REQUEUE_REASON_FALLBACK


class TestTreeDelta:
    """The containment guard's diagnosis, added after the first live run.

    A guard that says only "something changed" is why a false positive read as a
    worker escaping. These pin the naming.
    """

    def test_identical_status_has_no_delta(self):
        assert guards.tree_delta(" M foo.py", " M foo.py") == ()

    def test_names_the_path_a_concurrent_edit_added(self):
        # The exact false positive from the first live run: a human edited
        # lint.yml while the worker was running.
        before = "?? apps/factory-dispatcher/"
        after = "?? apps/factory-dispatcher/\n M .github/workflows/lint.yml"
        assert guards.tree_delta(before, after) == ("M .github/workflows/lint.yml",)

    def test_names_a_worker_written_file(self):
        assert guards.tree_delta("", " M apps/mcp-hub/src/tools/vision.py") == (
            "M apps/mcp-hub/src/tools/vision.py",
        )

    def test_reports_multiple_changes_sorted(self):
        assert guards.tree_delta("", " M b.py\n M a.py") == ("M a.py", "M b.py")

    def test_a_disappearing_entry_is_not_reported_as_new(self):
        # Someone reverting a file mid-run is not the worker writing one.
        assert guards.tree_delta(" M foo.py", "") == ()

    def test_a_sha_is_never_mistaken_for_a_path(self):
        # tree_delta takes the porcelain status only. If a caller ever passes a
        # fingerprint with the HEAD sha prepended, that sha would surface as a
        # phantom "changed path" — the bug this signature exists to prevent.
        assert guards.tree_delta("", "") == ()
        assert "abc123" not in guards.tree_delta("", " M a.py")


class TestContainmentBreach:
    """The narrowed guard, rewritten after three false positives in one day.

    The whole-tree version caught the operator committing mid-run twice, and
    caught a different agent session implementing an unrelated story in the same
    checkout once. Zero true positives. These tests pin the intersection
    semantics that replaced it.
    """

    WORKER = ["apps/substrate/src/cache.py", "apps/substrate/tests/test_cache.py"]

    def test_clean_run_is_not_a_breach(self):
        assert guards.containment_breach(self.WORKER, "", "") == ()

    def test_the_real_escape_is_caught(self):
        # Worker edited cache.py in its clone; the real cache.py also changed.
        after = " M apps/substrate/src/cache.py"
        assert guards.containment_breach(self.WORKER, "", after) == (
            "apps/substrate/src/cache.py",
        )

    def test_unrelated_concurrent_work_is_not_a_breach(self):
        # The actual 2026-07-29 false positive: another session implementing
        # bead_link while an FA-S3 run was in flight.
        after = "?? apps/substrate/tests/test_bead_link.py\n M docs/reference/substrate-api.md"
        assert guards.containment_breach(self.WORKER, "", after) == ()

    def test_operator_commit_is_not_a_breach(self):
        # HEAD moving is handled by the caller as drift, never here.
        after = " M docs/plans/factory-architecture-cleanup-stories.md"
        assert guards.containment_breach(self.WORKER, "", after) == ()

    def test_a_file_already_dirty_before_the_run_is_not_a_breach(self):
        # If the operator had cache.py open and dirty before the worker started,
        # its presence after proves nothing.
        dirty = " M apps/substrate/src/cache.py"
        assert guards.containment_breach(self.WORKER, dirty, dirty) == ()

    def test_multiple_escaped_files_all_reported_sorted(self):
        after = " M apps/substrate/tests/test_cache.py\n M apps/substrate/src/cache.py"
        assert guards.containment_breach(self.WORKER, "", after) == (
            "apps/substrate/src/cache.py",
            "apps/substrate/tests/test_cache.py",
        )

    def test_no_worker_changes_cannot_breach(self):
        assert guards.containment_breach([], "", " M apps/substrate/src/cache.py") == ()


class TestPorcelainPaths:
    def test_strips_the_status_prefix(self):
        assert guards.porcelain_paths(" M apps/a.py") == ("apps/a.py",)

    def test_handles_untracked(self):
        assert guards.porcelain_paths("?? apps/new/") == ("apps/new/",)

    def test_keeps_the_destination_of_a_rename(self):
        assert guards.porcelain_paths("R  old.py -> new.py") == ("new.py",)

    def test_strips_quotes_from_escaped_paths(self):
        assert guards.porcelain_paths(' M "apps/wéird.py"') == ("apps/wéird.py",)

    def test_multiple_lines(self):
        assert guards.porcelain_paths(" M a.py\n?? b.py") == ("a.py", "b.py")

    def test_blank_input(self):
        assert guards.porcelain_paths("") == ()


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Extract the LiteLLM call sites", "extract-the-litellm-call-sites"),
            ("422 instead of 500!", "422-instead-of-500"),
            ("---", "task"),
            ("", "task"),
        ],
    )
    def test_branch_safe_slugs(self, raw, expected):
        assert guards.slugify(raw) == expected
