"""A bead blocked on an unmerged PR is dispatchable, because the prerequisite
is recorded in prose that guards.is_runnable cannot read (dev.finding
30784136: 3b884fe3, ad0a0f95, 5bb23acc).

guards.is_runnable and predecessor_bead_ids evaluation at dispatch time are
untouched -- that mechanism is correct and proven. This bead adds one check
at filing time: when a spec's own intent/acceptance prose names a PR number
that is currently OPEN, and the spec carries no predecessor_bead_ids fence,
file_task.py now WARNS (never refuses -- of the three pending beads matching
this signature when the finding was measured, two were benign).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

DISPATCHER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISPATCHER))

import dispatch  # noqa: E402
import file_task  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_pristine_verification(monkeypatch):
    """Keep these tests hermetic -- see test_file_task.py's identical fixture."""
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(()),
    )


def _spec(**overrides):
    spec = {
        "lane": "bug-triage",
        "title": "t",
        "intent": "i",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        "requirement_refs_waived": "test fixture; exercises pr-prerequisite check only",
        "release_ref_waived": "test fixture; exercises pr-prerequisite check only",
    }
    spec.update(overrides)
    return spec


class FakeSubstrate:
    """No network, no database -- just enough of Substrate for main()'s wiring."""

    def __init__(self, tasks=None):
        self._tasks = tasks or []
        self.posted = None

    def list_tasks(self, state=None, limit=200):
        return list(self._tasks)

    def list_notes(self, parent_id, limit=500):
        return []

    def list_beads(self, namespace, type, state=None, limit=200):
        return []

    def find_bead(self, namespace, type, content_ref):
        return None

    def add_link(self, source_id, target_id, link_type, created_by):
        return {"id": "link-1"}

    def add_note(self, parent_id, kind, body, created_by, **extra):
        return {}

    def patch_content(self, bead_id, content, created_by):
        return {}

    def create_task(self, content, created_by, *, trust_tier="user"):
        # Added 2026-09-17: file_task.main() calls this, and a double that does
        # not implement it fails with AttributeError rather than saying what is
        # missing. dev.finding 24970f0e recorded exactly this pairing -- #864
        # taught main() to call create_task and updated every double that
        # existed at its base; this file's double was added on an older commit
        # and was invisible to that sweep. Routed through _request like every
        # other double's, so the recorded call shape stays identical.
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "task",
                "state": "pending",
                "trust_tier": trust_tier,
                "created_by": created_by,
                "content": content,
            },
        )

    def _request(self, method, path, **kwargs):
        self.posted = kwargs.get("json")
        return {"id": "new-bead-id"}


def _file(tmp_path, **overrides):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(**overrides)))
    return spec_path


def _task_with_pr(task_id, number):
    return {
        "id": task_id,
        "state": "review",
        "content": {"title": "other", "pr_url": f"https://github.com/example/repo/pull/{number}"},
    }


# ---------------------------------------------------------------------------
# _referenced_pr_numbers -- the regex is not a bare '#NNN' substring sweep
# ---------------------------------------------------------------------------


class TestReferencedPrNumbers:
    def test_finds_a_pr_number_in_intent(self):
        spec = _spec(intent="needs evaluate_cluster_identity, present at #834's head.")
        assert file_task._referenced_pr_numbers(spec) == [834]

    def test_finds_a_pr_number_in_acceptance(self):
        spec = _spec(acceptance=["THE symbol SHALL exist, per #835."])
        assert file_task._referenced_pr_numbers(spec) == [835]

    def test_dedupes_and_preserves_first_seen_order(self):
        spec = _spec(
            intent="see #838 for context",
            acceptance=["THE thing SHALL match #834.", "ALSO see #838 again."],
        )
        assert file_task._referenced_pr_numbers(spec) == [838, 834]

    def test_single_digit_ordinal_is_not_matched(self):
        """'#3' reads as an ordinal ('step #3'), not a GitHub reference --
        this repo's real PR numbers run in the many hundreds."""
        spec = _spec(intent="this is step #3 of the migration plan.")
        assert file_task._referenced_pr_numbers(spec) == []

    def test_no_reference_at_all(self):
        spec = _spec(intent="vision.py never calls log_llm_cost.")
        assert file_task._referenced_pr_numbers(spec) == []


# ---------------------------------------------------------------------------
# pr_prerequisite_warnings -- the three pinned shapes plus the degrade-safe path
# ---------------------------------------------------------------------------


class TestPrPrerequisiteWarnings:
    def test_open_pr_with_no_fence_warns(self, monkeypatch):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "OPEN")
        spec = _spec(intent="needs evaluate_cluster_identity, present at #834's head.")
        content = {"predecessor_bead_ids": []}

        warnings = file_task.pr_prerequisite_warnings(spec, content, [])

        assert len(warnings) == 1
        assert "#834" in warnings[0]
        assert "OPEN" in warnings[0]
        assert "content.predecessor_bead_ids" in warnings[0]

    def test_open_pr_names_the_resolved_bead_id(self, monkeypatch):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "OPEN")
        spec = _spec(intent="present at #834's head.")
        content = {"predecessor_bead_ids": []}
        tasks = [_task_with_pr("pr-bead-1", 834)]

        warnings = file_task.pr_prerequisite_warnings(spec, content, tasks)

        assert "pr-bead-1" in warnings[0]

    def test_open_pr_with_a_fence_is_silent(self, monkeypatch):
        """AC shape (b): the fence silences the check even without asserting
        it names this exact PR's bead -- any fence is enough."""
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "OPEN")
        spec = _spec(intent="present at #834's head.")
        content = {"predecessor_bead_ids": ["some-bead"]}

        assert file_task.pr_prerequisite_warnings(spec, content, []) == []

    def test_closed_pr_with_no_fence_is_silent(self, monkeypatch):
        """AC shape (c): e.g. a self-reference to the bead's own closed PR."""
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "CLOSED")
        spec = _spec(intent="closes #838.")
        content = {"predecessor_bead_ids": []}

        assert file_task.pr_prerequisite_warnings(spec, content, []) == []

    def test_merged_pr_with_no_fence_is_silent(self, monkeypatch):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "MERGED")
        spec = _spec(intent="landed in #822.")
        content = {"predecessor_bead_ids": []}

        assert file_task.pr_prerequisite_warnings(spec, content, []) == []

    def test_no_pr_reference_is_silent(self, monkeypatch):
        called = []
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: called.append(n) or "OPEN")
        spec = _spec(intent="vision.py never calls log_llm_cost.")
        content = {"predecessor_bead_ids": []}

        assert file_task.pr_prerequisite_warnings(spec, content, []) == []
        assert called == []

    def test_github_unreachable_warns_could_not_evaluate_and_does_not_raise(self, monkeypatch):
        def _boom(number):
            raise dispatch.DispatchError("gh: no such host")

        monkeypatch.setattr(file_task, "_lookup_pr_state", _boom)
        spec = _spec(intent="present at #834's head.")
        content = {"predecessor_bead_ids": []}

        warnings = file_task.pr_prerequisite_warnings(spec, content, [])

        assert len(warnings) == 1
        assert "#834" in warnings[0]
        assert "could not be evaluated" in warnings[0]

    def test_missing_factory_repo_config_degrades_the_same_way(self, monkeypatch):
        """PRIN-015: a control that cannot evaluate its question fails
        closed -- missing FACTORY_REPO (dispatch.Config.from_env()) is exactly
        as unevaluable as GitHub being unreachable, and must not crash filing."""

        def _boom(number):
            raise dispatch.MissingConfigError("FACTORY_REPO is not set")

        monkeypatch.setattr(file_task, "_lookup_pr_state", _boom)
        spec = _spec(intent="present at #834's head.")
        content = {"predecessor_bead_ids": []}

        warnings = file_task.pr_prerequisite_warnings(spec, content, [])

        assert "could not be evaluated" in warnings[0]

    def test_multiple_references_each_evaluated_independently(self, monkeypatch):
        states = {834: "OPEN", 838: "MERGED"}
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: states[n])
        spec = _spec(intent="needs #834; already shipped as #838.")
        content = {"predecessor_bead_ids": []}

        warnings = file_task.pr_prerequisite_warnings(spec, content, [])

        assert len(warnings) == 1
        assert "#834" in warnings[0]
        assert "#838" not in warnings[0]


# ---------------------------------------------------------------------------
# main() -- wired in, warns to stderr, never blocks filing
# ---------------------------------------------------------------------------


class TestMainIntegration:
    def test_main_warns_but_still_files(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "OPEN")
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = _file(tmp_path, intent="needs evaluate_cluster_identity, at #834's head.")

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None
        err = capsys.readouterr().err
        assert "#834" in err
        assert "content.predecessor_bead_ids" in err

    def test_main_is_silent_when_fenced(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "OPEN")
        fenced_predecessor = {"id": "some-bead", "state": "pending", "content": {"title": "other"}}
        fake = FakeSubstrate([fenced_predecessor])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = _file(
            tmp_path,
            intent="needs evaluate_cluster_identity, at #834's head.",
            predecessor_bead_ids=["some-bead"],
        )

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert "#834" not in capsys.readouterr().err

    def test_main_is_silent_for_a_closed_pr_self_reference(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(file_task, "_lookup_pr_state", lambda n: "CLOSED")
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = _file(tmp_path, intent="closes #838, this bead's own PR.")

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert "#838" not in capsys.readouterr().err

    def test_main_never_raises_when_github_is_unreachable(self, tmp_path, monkeypatch, capsys):
        def _boom(number):
            raise dispatch.DispatchError("gh: no such host")

        monkeypatch.setattr(file_task, "_lookup_pr_state", _boom)
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = _file(tmp_path, intent="needs evaluate_cluster_identity, at #834's head.")

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None
        assert "could not be evaluated" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _lookup_pr_state / _bead_id_for_pr -- the seams the checks above stub out
# ---------------------------------------------------------------------------


class TestLookupPrState:
    def test_reuses_dispatch_lookup_pull_request(self, monkeypatch):
        captured = {}

        def fake_from_env():
            return dispatch.Config(repo="example/repo")

        def fake_lookup(pr_ref, cfg):
            captured["pr_ref"] = pr_ref
            captured["repo"] = cfg.repo
            return dispatch.PullRequestStatus(
                number=834, url="https://github.com/example/repo/pull/834",
                state="OPEN", merge_commit=None,
            )

        monkeypatch.setattr(dispatch.Config, "from_env", staticmethod(fake_from_env))
        monkeypatch.setattr(dispatch, "lookup_pull_request", fake_lookup)

        assert file_task._lookup_pr_state(834) == "OPEN"
        assert captured["pr_ref"] == "834"
        assert captured["repo"] == "example/repo"


class TestBeadIdForPr:
    def test_resolves_from_a_matching_pr_url(self):
        tasks = [_task_with_pr("bead-a", 834), _task_with_pr("bead-b", 900)]
        assert file_task._bead_id_for_pr(834, tasks) == "bead-a"

    def test_returns_none_when_nothing_matches(self):
        tasks = [_task_with_pr("bead-a", 900)]
        assert file_task._bead_id_for_pr(834, tasks) is None

    def test_returns_none_for_empty_tasks(self):
        assert file_task._bead_id_for_pr(834, []) is None


# ---------------------------------------------------------------------------
# regression -- pre-existing refusal for a spec whose declared verification
# cannot pass on today's main must be unchanged by this bead. The full
# behaviour of that refusal is already covered by
# test_file_task_pristine_verification.py (OPS-6); this test only pins that
# this bead's new, unrelated warn-only check does not interfere with it.
# ---------------------------------------------------------------------------


def test_pristine_verification_refusal_is_unaffected(monkeypatch):
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (
                dispatch.VerificationCommandResult(
                    command=commands[0],
                    outcome="failed",
                    exit_code=1,
                    output="boom",
                    duration_s=0.1,
                ),
            )
        ),
    )
    content = file_task.build_content(_spec())
    content["verification"] = {"commands": ["false"], "must_report_unverified": True}

    with pytest.raises(SystemExit) as exc:
        file_task._check_pristine_verification(content)

    assert "does not pass against an unmodified clone" in str(exc.value)
