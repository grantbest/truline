"""F-DCE-3: doctrine reaches the worker through the bead.

docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md, sprint 24. Four connected
pieces: the lane -> principles mapping (doctrine.py), the prompt builder embedding adopted/
enforced statements (guards.build_prompt / dispatch.assemble_personas_prompt), the PR body's
"Applies:" stamp, and the applies-link write on task completion.

The applies/derived_from/enforced_by link_type vocabulary the plan says S23-B1 already
registered has NOT actually landed in apps/substrate/src/schemas.py's BEAD_LINK_TYPES as of
this change (see .factory/design.md decision 5) — docs/architecture/doctrine.md:70 itself
still calls these edges "proposed, in the inventory's vocabulary". The fake below validates
against the vocabulary S23-B1 is SPECIFIED to deliver, not the one currently shipped —
importing the real (still-stale) BEAD_LINK_TYPES here would make this fake reject `applies`
today, which is correct for what's shipped and wrong for what this feature needs tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import doctrine  # noqa: E402
import guards  # noqa: E402
import substrate  # noqa: E402

from test_dispatch import (  # noqa: E402
    FakeSubstrate,
    StoreStatusError,
    legacy_pr_body,
    passed,
    stub_merge_base_diff_paths,
    task,
    verification_report,
)


REGISTRY_TEXT = """# Principles registry

Seeded 2026-08-01.

### PRIN-003 — A real dependency is a typed edge or it is a defect

- **Statement:** A real dependency is a typed edge or it is a defect.
- **Source:** incident-1
- **Status:** `adopted` — binding text.

### PRIN-005 — Modularity survives only through enforcement

- **Statement:** Modularity survives only through enforcement.
- **Source:** incident-2
- **Status:** `enforced` — mechanism ships.

### PRIN-999 — Conjecture under review

- **Statement:** This is a conjecture that must never reach a worker prompt.
- **Source:** incident-3
- **Status:** `proposed` — not yet binding.
"""

#: A heading with no `### PRIN-NNN` prefix at all just parses to zero entries
#: (parse_registry's own contract) — to exercise the actual raise path this needs a
#: recognised heading that then fails a required field, mirroring _parse_entry's checks.
MALFORMED_REGISTRY_TEXT = """# Principles registry

### PRIN-003 — Missing its Statement bullet entirely

- **Source:** incident-1
- **Status:** `adopted`
"""


# ---------------------------------------------------------------------------
# doctrine.py — pure filtering + the I/O wrapper
# ---------------------------------------------------------------------------


def test_citations_for_lane_includes_adopted_and_enforced_only():
    citations = doctrine.citations_for_lane(REGISTRY_TEXT, "code-health")

    ids = [c.id for c in citations]
    assert "PRIN-003" in ids and "PRIN-005" in ids
    assert "PRIN-999" not in ids
    for c in citations:
        assert "conjecture" not in c.statement.lower()


def test_citations_for_lane_skips_a_mapped_id_missing_from_the_registry():
    # bug-triage maps to PRIN-008 and PRIN-011; neither is in REGISTRY_TEXT.
    assert doctrine.citations_for_lane(REGISTRY_TEXT, "bug-triage") == ()


def test_citations_for_lane_returns_nothing_for_an_unmapped_lane_without_parsing():
    assert doctrine.citations_for_lane(MALFORMED_REGISTRY_TEXT, "some-other-lane") == ()


def test_lane_principles_maps_feature_to_prin_010_and_prin_012():
    assert doctrine.LANE_PRINCIPLES["feature"] == ("PRIN-010", "PRIN-012")


def test_citations_for_lane_raises_on_malformed_registry_for_a_mapped_lane():
    with pytest.raises(doctrine.PrinciplesParseError):
        doctrine.citations_for_lane(MALFORMED_REGISTRY_TEXT, "code-health")


def test_load_citations_for_lane_does_not_touch_disk_for_an_unmapped_lane(tmp_path):
    # No docs/architecture/principles.md exists under tmp_path; reading it would raise.
    assert doctrine.load_citations_for_lane(tmp_path, "unmapped") == ()


def test_load_citations_for_lane_raises_when_the_file_is_missing(tmp_path, monkeypatch):
    # The loader falls back to the module's repo root; point that at an empty
    # directory too, so this exercises the registry being absent everywhere.
    monkeypatch.setattr(doctrine, "_REPO_ROOT", tmp_path / "elsewhere")
    with pytest.raises(doctrine.PrinciplesParseError):
        doctrine.load_citations_for_lane(tmp_path, "code-health")


def test_load_citations_for_lane_falls_back_to_the_module_repo_root(tmp_path, monkeypatch):
    # CI and the test suite run pytest from apps/factory-dispatcher, so a
    # cwd-derived repo_root carries no docs/ tree; the loader then reads the
    # registry versioned with this module rather than failing the dispatch.
    fallback_root = tmp_path / "module-root"
    registry_path = fallback_root / doctrine.PRINCIPLES_MD_PATH
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(REGISTRY_TEXT)
    monkeypatch.setattr(doctrine, "_REPO_ROOT", fallback_root)

    citations = doctrine.load_citations_for_lane(tmp_path / "not-the-repo", "code-health")

    assert {c.id for c in citations} == {"PRIN-003", "PRIN-005"}


def test_load_citations_for_lane_reads_and_parses_a_real_file(tmp_path):
    registry_path = tmp_path / doctrine.PRINCIPLES_MD_PATH
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(REGISTRY_TEXT)

    citations = doctrine.load_citations_for_lane(tmp_path, "code-health")

    assert {c.id for c in citations} == {"PRIN-003", "PRIN-005"}


# ---------------------------------------------------------------------------
# guards.render_doctrine_section / build_prompt
# ---------------------------------------------------------------------------


def test_render_doctrine_section_is_empty_for_no_principles():
    assert guards.render_doctrine_section(()) == ""


def test_render_doctrine_section_names_id_and_statement():
    section = guards.render_doctrine_section(
        (("PRIN-003", "A real dependency is a typed edge or it is a defect."),)
    )
    assert "## Doctrine" in section
    assert "PRIN-003" in section
    assert "A real dependency is a typed edge or it is a defect." in section


def test_build_prompt_is_byte_identical_when_no_principles_are_passed():
    without_arg = guards.build_prompt(task(), [])
    with_empty_arg = guards.build_prompt(task(), [], ())
    assert without_arg == with_empty_arg
    assert "## Doctrine" not in without_arg


def test_build_prompt_embeds_mapped_principles_and_excludes_proposed_ones():
    citations = doctrine.citations_for_lane(REGISTRY_TEXT, "code-health")
    prompt = guards.build_prompt(task(), [], doctrine.as_prompt_pairs(citations))

    assert "PRIN-003" in prompt
    assert "PRIN-005" in prompt
    assert "A real dependency is a typed edge or it is a defect." in prompt
    assert "PRIN-999" not in prompt
    assert "conjecture" not in prompt.lower()


# ---------------------------------------------------------------------------
# dispatch.assemble_personas_prompt
# ---------------------------------------------------------------------------


def _clone_with_personas(tmp_path):
    agents = tmp_path / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / "polecat-developer.md").write_text("---\nx: y\n---\nPOLECAT BODY")
    (agents / "architect-sme.md").write_text("---\nx: y\n---\nARCHITECT BODY")
    return tmp_path


def test_assemble_personas_prompt_embeds_doctrine_when_given_principles(tmp_path):
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(
        clone, {"risk_class": "structural"}, (("PRIN-003", "typed edge or defect"),)
    )
    assert "POLECAT BODY" in prompt
    assert "PRIN-003" in prompt
    assert "typed edge or defect" in prompt


def test_assemble_personas_prompt_omits_doctrine_section_by_default(tmp_path):
    clone = _clone_with_personas(tmp_path)
    prompt = dispatch.assemble_personas_prompt(clone, {"risk_class": "structural"})
    assert "## Doctrine" not in prompt


# ---------------------------------------------------------------------------
# dispatch.render_applies_stamp / open_pull_request PR body
# ---------------------------------------------------------------------------


def test_render_applies_stamp_empty_for_no_ids():
    assert dispatch.render_applies_stamp(()) == ""


def test_render_applies_stamp_names_every_id_once():
    assert dispatch.render_applies_stamp(("PRIN-003", "PRIN-005")) == "Applies: PRIN-003, PRIN-005"


def _open_pr_capturing_body(monkeypatch, bead, applies_ids=()):
    import subprocess

    captured = {}

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == [dispatch.GIT, "checkout"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == [dispatch.GIT, "add"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == [dispatch.GIT, "diff", "--cached"]:
            # returncode 1 == something is staged, so the commit below runs.
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[:3] == [dispatch.GIT, "-c", "commit.gpgsign=false"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == [dispatch.GIT, "ls-remote"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == [dispatch.GIT, "push"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "create"]:
            captured["body"] = cmd[cmd.index("--body") + 1]
            return subprocess.CompletedProcess(cmd, 0, "https://example.test/pr/1\n", "")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(dispatch, "run", fake_run)
    # Path.cwd() here is NOT a fixture clone -- it is wherever this test
    # happens to run from. recorded(Path.cwd()) would write outside the
    # repository and fingerprint the operator's own live hooks, so this
    # stubs the hook itself instead (dev.finding 79db3113 part c1, AC-3).
    monkeypatch.setattr(dispatch, "check_clone_git_control", lambda clone: None)

    dispatch.open_pull_request(
        dispatch.Config(repo="example/repo", base_ref="main"),
        Path.cwd(),
        bead,
        "factory/task-1",
        "worker report",
        dispatch.guards.ScopeVerdict(),
        verification_report(passed("pytest -q")),
        applies_ids,
    )
    return captured["body"]


def test_pr_body_carries_one_applies_line_naming_the_embedded_ids(monkeypatch):
    body = _open_pr_capturing_body(
        monkeypatch, task(), applies_ids=("PRIN-003", "PRIN-005", "PRIN-008")
    )
    applies_lines = [line for line in body.splitlines() if line.startswith("Applies:")]
    assert applies_lines == ["Applies: PRIN-003, PRIN-005, PRIN-008"]


def test_pr_body_first_line_is_unchanged_by_the_applies_stamp(monkeypatch):
    bead = task()
    body = _open_pr_capturing_body(monkeypatch, bead, applies_ids=("PRIN-003",))
    assert body.splitlines()[0] == (
        f"Dispatched by the factory from `dev.task` [`{bead['id']}`]({bead['id']})."
    )


def test_pr_body_matches_legacy_exactly_when_nothing_was_embedded(monkeypatch):
    bead = task()
    body = _open_pr_capturing_body(monkeypatch, bead, applies_ids=())
    assert body == legacy_pr_body(bead)
    assert "Applies:" not in body


# ---------------------------------------------------------------------------
# dispatch.record_applies_links — resilience, dedup, and the vocabulary meta-test
# ---------------------------------------------------------------------------

#: The vocabulary S23-B1 (F-DCE-1) is SPECIFIED to add — see module docstring and
#: .factory/design.md decision 5/9. Reconcile this constant with
#: apps/substrate/src/schemas.py:951 BEAD_LINK_TYPES once that bead actually merges.
SPECIFIED_BEAD_LINK_TYPES = frozenset(
    {
        "designs",
        "supersedes",
        "gates",
        "regresses",
        "found_by",
        "affects",
        "measures",
        "applies",
        "derived_from",
        "enforced_by",
    }
)


class LinkValidatingFakeSubstrate:
    """Rejects what the live schema's create_bead_link endpoint rejects.

    Mirrors BeadLinkCreate.normalize_link_type (422 on an unrecognised link_type) and the
    ``uq_bead_link_edge`` unique constraint (409 on a duplicate (source, target, link_type))
    — see apps/substrate/src/schemas.py and apps/substrate/src/routes.py::create_bead_link.
    """

    def __init__(self, principle_beads=None):
        self.principle_beads = dict(principle_beads or {})
        self.links: list[tuple[str, str, str]] = []
        self.notes: list[dict] = []

    def find_bead(self, namespace, type, content_ref):
        if namespace == "arch" and type == "principle":
            bead_id = self.principle_beads.get(content_ref)
            return {"id": bead_id} if bead_id else None
        return None

    def add_link(self, source_id, target_id, link_type, created_by):
        normalized = link_type.strip().lower()
        if normalized not in SPECIFIED_BEAD_LINK_TYPES:
            raise substrate.SubstrateError(
                422, f"link_type must be one of: {sorted(SPECIFIED_BEAD_LINK_TYPES)}"
            )
        key = (source_id, target_id, normalized)
        if key in self.links:
            raise substrate.SubstrateError(409, "duplicate_link")
        self.links.append(key)
        return {"id": f"link-{len(self.links)}"}

    def add_note(self, parent_id, kind, body, created_by, provenance=None, **extra):
        self.notes.append({"parent_id": parent_id, "kind": kind, "body": body})
        return {"id": f"note-{len(self.notes)}"}


def test_the_double_rejects_an_unrecognised_link_type():
    fake = LinkValidatingFakeSubstrate()
    with pytest.raises(substrate.SubstrateError) as excinfo:
        fake.add_link("task-1", "principle-1", "reelizes-typo", "factory-dispatcher/claude")
    assert excinfo.value.status == 422


def test_the_double_rejects_a_duplicate_source_target_link_type():
    fake = LinkValidatingFakeSubstrate()
    fake.add_link("task-1", "principle-1", "applies", "factory-dispatcher/claude")
    with pytest.raises(substrate.SubstrateError) as excinfo:
        fake.add_link("task-1", "principle-1", "applies", "factory-dispatcher/claude")
    assert excinfo.value.status == 409


def _citations(*ids):
    by_id = {c.id: c for c in doctrine.citations_for_lane(REGISTRY_TEXT, "code-health")}
    return tuple(by_id[i] for i in ids)


def test_record_applies_links_writes_one_edge_per_citation():
    fake = LinkValidatingFakeSubstrate(
        principle_beads={"PRIN-003": "principle-uuid-3", "PRIN-005": "principle-uuid-5"}
    )
    citations = _citations("PRIN-003", "PRIN-005")

    problems = dispatch.record_applies_links(
        fake, "task-1", citations, "factory-dispatcher/claude"
    )

    assert problems == []
    assert ("task-1", "principle-uuid-3", "applies") in fake.links
    assert ("task-1", "principle-uuid-5", "applies") in fake.links


def test_record_applies_links_reports_a_missing_principle_bead_without_raising():
    fake = LinkValidatingFakeSubstrate(principle_beads={"PRIN-003": None})
    citations = _citations("PRIN-003")

    problems = dispatch.record_applies_links(
        fake, "task-1", citations, "factory-dispatcher/claude"
    )

    assert len(problems) == 1
    assert "PRIN-003" in problems[0]
    assert "no arch.principle bead found" in problems[0]
    assert fake.links == []


def test_record_applies_links_second_run_does_not_duplicate():
    fake = LinkValidatingFakeSubstrate(principle_beads={"PRIN-003": "principle-uuid-3"})
    citations = _citations("PRIN-003")

    first = dispatch.record_applies_links(fake, "task-1", citations, "factory-dispatcher/claude")
    second = dispatch.record_applies_links(fake, "task-1", citations, "factory-dispatcher/claude")

    assert first == [] and second == []
    assert fake.links == [("task-1", "principle-uuid-3", "applies")]


def test_record_applies_links_reports_unrecognised_link_type_without_raising():
    """As of this change the live substrate does not accept `applies` at all (design.md
    decision 5) — a double that mirrors today's shipped vocabulary exactly must produce a
    problem string here, and the dispatch must survive it (decision 6)."""

    class TodaysLiveVocabularyFake(LinkValidatingFakeSubstrate):
        def add_link(self, source_id, target_id, link_type, created_by):
            # Only the seven types actually in BEAD_LINK_TYPES today.
            if link_type not in {
                "designs",
                "supersedes",
                "gates",
                "regresses",
                "found_by",
                "affects",
                "measures",
            }:
                raise substrate.SubstrateError(422, "link_type must be one of: ...")
            return super().add_link(source_id, target_id, link_type, created_by)

    fake = TodaysLiveVocabularyFake(principle_beads={"PRIN-003": "principle-uuid-3"})
    citations = _citations("PRIN-003")

    problems = dispatch.record_applies_links(
        fake, "task-1", citations, "factory-dispatcher/claude"
    )

    assert len(problems) == 1
    assert "PRIN-003" in problems[0] and "rejected" in problems[0]


# ---------------------------------------------------------------------------
# dispatch_once — end to end
# ---------------------------------------------------------------------------


def _stub_worker_run(monkeypatch):
    monkeypatch.setattr(dispatch, "make_clone", lambda _cfg, _clone: None)
    monkeypatch.setattr(dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "prepare_worker_argv", lambda argv, *_a, **_k: argv)
    monkeypatch.setattr(
        dispatch,
        "run_worker",
        lambda _prompt, _clone, _budget, _argv, *_a, **_k: dispatch.WorkerResult(
            exit_code=0, stdout="done", duration_s=1.0, timed_out=False
        ),
    )
    monkeypatch.setattr(
        dispatch, "changed_paths", lambda _clone: ["apps/factory-dispatcher/dispatch.py"]
    )
    stub_merge_base_diff_paths(monkeypatch)
    monkeypatch.setattr(dispatch, "smoke_check_python", lambda _clone, _paths: None)
    monkeypatch.setattr(
        dispatch,
        "verify_declared_commands",
        lambda _clone, commands: verification_report(*[passed(c) for c in commands]),
    )
    monkeypatch.setattr(dispatch, "open_pull_request", lambda *_a, **_kw: "https://example.test/pr/1")


def test_dispatch_once_writes_applies_links_and_adds_no_extra_note_on_success(monkeypatch):
    _stub_worker_run(monkeypatch)
    bead = task(lane="code-health")
    sub = FakeSubstrate(bead)  # default find_bead: every PRIN id "found"

    rc = dispatch.dispatch_once(dispatch.Config(repo_root=Path.cwd()), sub, None, dry_run=False)

    assert rc == 0
    assert sub.states[-1] == ("task-1", "review", dispatch.CREATED_BY)
    assert len(sub.links) == 3  # code-health -> PRIN-003, PRIN-005, PRIN-008
    assert all(link_type == "applies" for _s, _t, link_type in sub.links)
    assert len(sub.notes) == 3  # unchanged from the pre-doctrine happy path


def test_dispatch_once_records_a_note_for_a_missing_principle_bead_and_still_succeeds(
    monkeypatch,
):
    _stub_worker_run(monkeypatch)
    bead = task(lane="code-health")
    sub = FakeSubstrate(bead, principle_beads={"PRIN-003": None})

    rc = dispatch.dispatch_once(dispatch.Config(repo_root=Path.cwd()), sub, None, dry_run=False)

    assert rc == 0
    assert sub.states[-1] == ("task-1", "review", dispatch.CREATED_BY)
    assert len(sub.notes) == 4
    problem_note = sub.notes[-1][0][2]
    assert "PRIN-003" in problem_note
    assert "provenance only, not a gate" in problem_note


def test_dispatch_once_prompt_is_byte_identical_for_an_unmapped_lane(monkeypatch):
    seen = {}

    def fake_make_clone(_cfg, _clone):
        seen["cloned"] = True

    monkeypatch.setattr(dispatch, "make_clone", fake_make_clone)
    monkeypatch.setattr(dispatch, "fingerprint_tree", lambda _root: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "fingerprint_clone", lambda _clone: dispatch.TreeState("h", ""))
    monkeypatch.setattr(dispatch, "prepare_worker_argv", lambda argv, *_a, **_k: argv)

    captured_prompt = {}

    def fake_run_worker(prompt, _clone, _budget, _argv, *_a, **_k):
        captured_prompt["prompt"] = prompt
        return dispatch.WorkerResult(exit_code=0, stdout="done", duration_s=1.0, timed_out=False)

    monkeypatch.setattr(dispatch, "run_worker", fake_run_worker)
    monkeypatch.setattr(
        dispatch, "changed_paths", lambda _clone: ["apps/factory-dispatcher/dispatch.py"]
    )
    stub_merge_base_diff_paths(monkeypatch)
    monkeypatch.setattr(dispatch, "smoke_check_python", lambda _clone, _paths: None)
    monkeypatch.setattr(
        dispatch,
        "verify_declared_commands",
        lambda _clone, commands: verification_report(*[passed(c) for c in commands]),
    )
    monkeypatch.setattr(dispatch, "open_pull_request", lambda *_a, **_kw: "https://example.test/pr/1")

    # Every lane in DEV_LANES happens to be doctrine-mapped (that's the seed), so an
    # "unmapped lane" needs a worker explicitly licensed for one LANE_PRINCIPLES does
    # not know about — mirrors test_missing_worker_executable_records_environment_
    # without_attempt's pattern of registering a throwaway worker for one test.
    monkeypatch.setitem(
        dispatch.WORKER_REGISTRY,
        "unmapped-lane-test-worker",
        dispatch.WorkerEntry(
            argv=("claude", "-p"),
            quarantined=False,
            allowed_lanes=("some-lane-with-no-mapping",),
        ),
    )
    bead = task(lane="some-lane-with-no-mapping", worker_hint="unmapped-lane-test-worker")
    sub = FakeSubstrate(bead)
    rc = dispatch.dispatch_once(dispatch.Config(repo_root=Path.cwd()), sub, None, dry_run=False)

    assert rc == 0
    notes = sub.list_notes("task-1")
    prompt_without_doctrine = guards.build_prompt(bead, notes)
    assert captured_prompt["prompt"] == prompt_without_doctrine
    assert "## Doctrine" not in captured_prompt["prompt"]
    assert sub.links == []


def test_dispatch_once_fails_before_claiming_when_principles_md_cannot_be_parsed(
    monkeypatch, tmp_path
):
    _stub_worker_run(monkeypatch)
    bead = task(lane="code-health")
    sub = FakeSubstrate(bead)
    registry_path = tmp_path / doctrine.PRINCIPLES_MD_PATH
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(MALFORMED_REGISTRY_TEXT)

    rc = dispatch.dispatch_once(dispatch.Config(repo_root=tmp_path), sub, None, dry_run=False)

    assert rc == 1
    assert sub.transitions == []
    assert sub.notes == []
    assert sub.states == []


class StoreStatusErrorFactory:
    """Small helper so record_applies_links's 409-swallow branch is exercised against the
    exact exception dispatch.store_error_status() is written to recognise."""

    @staticmethod
    def conflict(message: str) -> StoreStatusError:
        return StoreStatusError(409, message)


def test_store_error_status_recognises_the_fake_conflict_error():
    exc = StoreStatusErrorFactory.conflict("dup")
    assert dispatch.store_error_status(exc) == 409
