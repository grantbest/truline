"""Fixture-driven tests for the knowledge ingestion workflow (F-DCE-4).

No network, no database: every store is in-memory, every PR lookup is
injected, and every repo read is a tmp_path fixture. The `Fake*` stores below
reject what the live substrate rejects by construction — they validate
`arch.principle` content with the real `ArchPrincipleContent` model and check
link types against the real `BEAD_LINK_TYPES` (plus the one documented,
tracked gap: `derived_from` — see .factory/design.md) imported directly from
`apps/substrate/src/schemas.py`, not hand-copied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "substrate"))

import dispatch  # noqa: E402
import file_task  # noqa: E402
from activities import knowledge_ingestion as ki  # noqa: E402
from src.schemas import ArchPrincipleContent, BEAD_LINK_TYPES  # noqa: E402

# The double's link vocabulary IS the live one, with no gap. It used to carry a
# union with `derived_from`: doctrine.md and the sprint plan specced that edge,
# but F-DCE-1 (#446) shipped only arch.principle's content schema, not the
# edge-vocabulary half. That comment said the union "becomes a no-op the moment
# the live substrate ships it for real" — S33-B1 (#501) is that moment, so the
# union is gone rather than left standing as a no-op. A double that admits more
# than the live contract is a second implementation (CLAUDE.md), and the only
# safe size for this set is exactly the live one.
FAKE_LINK_TYPES = BEAD_LINK_TYPES


def write_registry(path: Path, entries: list[dict[str, Any]]) -> None:
    lines = ["sources:"]
    for entry in entries:
        lines.append(f"  - id: {entry['id']}")
        lines.append(f"    kind: {entry['kind']}")
        lines.append(f"    reference: \"{entry['reference']}\"")
        lines.append(f"    title: \"{entry['title']}\"")
        lines.append(f"    summary: \"{entry.get('summary', '')}\"")
        lines.append(f"    filed: {str(entry.get('filed', False)).lower()}")
    path.write_text("\n".join(lines) + "\n")


def sample_source(**over: Any) -> dict[str, Any]:
    base = {
        "id": "gemini-4-traps",
        "kind": "book-takeaways",
        "reference": "GEMINI.md#4-accumulated-traps",
        "title": "GEMINI.md section 4 accumulated-traps list",
        "summary": "Traps the release gate has learned the hard way.",
    }
    base.update(over)
    return base


def write_candidates(repo_root: Path, source_id: str, candidates: list[dict[str, Any]]) -> None:
    path = repo_root / "docs" / "reference" / "knowledge-candidates" / f"{source_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates))


def write_takeaways(repo_root: Path, source_id: str, text: str = "# takeaways\n") -> None:
    path = repo_root / "docs" / "reference" / f"{source_id}-takeaways.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def sample_candidates() -> list[dict[str, Any]]:
    return [
        {
            "statement": "A rejectable option needs an actual gate behind it.",
            "source": "GEMINI.md section 4, trap 1",
            "rationale": "Green CI without a verdict-bearing gate let eight PRs merge unreviewed.",
        },
        {
            "statement": "A retry that cannot see its own failure reason repeats it.",
            "source": "GEMINI.md section 4, trap 2",
            "rationale": "Retry workers never read back the failure notes written for them.",
        },
    ]


# -- in-memory bead store: rejects what the live substrate rejects -----------


class FakeKnowledgeStore:
    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.notes: dict[str, list[dict[str, Any]]] = {}
        self.principles: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self._next_id = 1

    def _fresh_id(self, prefix: str) -> str:
        bead_id = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return bead_id

    def list_tasks(self) -> list[dict[str, Any]]:
        return [dict(task) for task in self.tasks]

    def file_task(self, content: dict[str, Any]) -> dict[str, Any]:
        task = {
            "id": self._fresh_id("task"),
            "namespace": "dev",
            "type": "task",
            "state": "pending",
            "content": content,
        }
        self.tasks.append(task)
        self.notes[task["id"]] = []
        return task

    def list_notes(self, parent_id: str) -> list[dict[str, Any]]:
        return [dict(note) for note in self.notes.get(parent_id, [])]

    def add_note(self, parent_id: str, kind: str, body: str, created_by: str) -> dict[str, Any]:
        note = {
            "id": self._fresh_id("note"),
            "parent_id": parent_id,
            "created_by": created_by,
            "content": {"kind": kind, "body": body},
        }
        self.notes.setdefault(parent_id, []).append(note)
        return note

    def create_principle(self, content: dict[str, Any]) -> dict[str, Any]:
        # Rejects malformed content exactly as POST /beads does: the real
        # pydantic model, not a hand-rolled check that can drift from it.
        ArchPrincipleContent.model_validate(content)
        bead = {
            "id": self._fresh_id("principle"),
            "namespace": "arch",
            "type": "principle",
            "state": "active",
            "content": content,
        }
        self.principles.append(bead)
        return bead

    def create_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict[str, Any]:
        normalized = link_type.strip().lower()
        if normalized not in FAKE_LINK_TYPES:
            allowed = ", ".join(sorted(FAKE_LINK_TYPES))
            raise ValueError(f"link_type must be one of: {allowed}")
        link = {
            "id": self._fresh_id("link"),
            "source_id": source_id,
            "target_id": target_id,
            "link_type": normalized,
            "created_by": created_by,
        }
        self.links.append(link)
        return link

    # helper for tests that stage a task directly in "review" or "done"
    def set_task_state(self, task_id: str, state: str, pr_url: str | None = None) -> None:
        for task in self.tasks:
            if task["id"] == task_id:
                task["state"] = state
                if pr_url is not None:
                    task["content"]["pr_url"] = pr_url
                return
        raise AssertionError(f"no such task {task_id}")


def merged_pr(url: str = "https://github.com/example/repo/pull/1") -> Any:
    return dispatch.PullRequestStatus(number=1, url=url, state="MERGED", merge_commit="deadbeef")


def open_pr(url: str = "https://github.com/example/repo/pull/1") -> Any:
    return dispatch.PullRequestStatus(number=1, url=url, state="OPEN", merge_commit=None)


# -- registry loading ---------------------------------------------------------


def test_load_registered_sources_parses_fixture_registry(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [sample_source()])

    sources = ki.load_registered_sources(path)

    assert len(sources) == 1
    source = sources[0]
    assert source.id == "gemini-4-traps"
    assert source.kind == "book-takeaways"
    assert source.context_ref == "knowledge-source:gemini-4-traps"
    assert source.takeaways_path == "docs/reference/gemini-4-traps-takeaways.md"
    assert source.candidates_path == (
        "docs/reference/knowledge-candidates/gemini-4-traps.json"
    )


def test_load_registered_sources_empty_registry_returns_nothing(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [])

    assert ki.load_registered_sources(path) == []


def test_load_registered_sources_rejects_unknown_kind(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [sample_source(kind="scripture")])

    with pytest.raises(ValueError, match="unknown kind"):
        ki.load_registered_sources(path)


def test_real_registry_file_parses_the_first_registered_source():
    """The shipped registry now carries its first real entry: GEMINI.md
    section 4's accumulated-traps list, named as the first candidate by
    #460's own intent and registered here per PRIN-011/PRIN-009."""
    sources = ki.load_registered_sources(ki.REGISTRY_PATH)
    assert len(sources) == 1
    source = sources[0]
    assert source.id == "gemini-4-traps"
    assert source.kind == "book-takeaways"
    assert source.reference == "GEMINI.md#4-accumulated-traps"
    assert source.title == "GEMINI.md section 4 accumulated-traps list"


def test_real_registry_source_is_eligible_for_extraction_task_filing():
    """The registered source must parse into a valid extraction-task spec
    and actually file via the same machinery every hand-filed task uses --
    the registration is only real if the workflow can act on it."""
    sources = ki.load_registered_sources(ki.REGISTRY_PATH)
    source = sources[0]

    content = ki.extraction_task_spec(source)
    built = file_task.build_content(content)
    assert built["lane"] == "code-health"
    assert built["risk_class"] == "structural"
    assert source.context_ref in built["context_refs"]

    store = FakeKnowledgeStore()
    result = ki.file_knowledge_extraction_tasks(store, registry_path=ki.REGISTRY_PATH)

    assert result["status"] == "filed"
    assert len(result["filed"]) == 1
    assert result["filed"][0]["source_id"] == "gemini-4-traps"
    assert len(store.tasks) == 1


# -- phase 1: filing -----------------------------------------------------------


def test_extraction_task_spec_builds_valid_dev_task_content(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [sample_source()])
    source = ki.load_registered_sources(path)[0]

    content = ki.extraction_task_spec(source)
    built = file_task.build_content(content)

    assert built["lane"] == "code-health"
    assert built["risk_class"] == "structural"
    assert source.context_ref in built["context_refs"]
    assert built["scope"]["paths"] == ["docs/reference/"]
    assert ".github/workflows/**" in built["scope"]["forbidden_paths"]
    assert built["requirement_refs_waived"]


def test_file_knowledge_extraction_tasks_files_exactly_one_task_per_source(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [sample_source()])
    store = FakeKnowledgeStore()

    result = ki.file_knowledge_extraction_tasks(store, registry_path=path)

    assert result["status"] == "filed"
    assert len(result["filed"]) == 1
    assert result["filed"][0]["source_id"] == "gemini-4-traps"
    assert len(store.tasks) == 1
    task = store.tasks[0]
    assert task["content"]["lane"] == "code-health"
    assert "knowledge-source:gemini-4-traps" in task["content"]["context_refs"]


def test_file_knowledge_extraction_tasks_is_idempotent_across_reruns(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(path, [sample_source()])
    store = FakeKnowledgeStore()

    first = ki.file_knowledge_extraction_tasks(store, registry_path=path)
    second = ki.file_knowledge_extraction_tasks(store, registry_path=path)

    assert len(first["filed"]) == 1
    assert len(second["filed"]) == 0
    assert second["already_filed"] == ["gemini-4-traps"]
    assert len(store.tasks) == 1  # never re-filed


def test_file_knowledge_extraction_tasks_files_one_per_multiple_sources(tmp_path):
    path = tmp_path / "knowledge_sources.yaml"
    write_registry(
        path,
        [
            sample_source(id="a", reference="docs/a.md"),
            sample_source(id="b", reference="docs/b.md", kind="audit"),
        ],
    )
    store = FakeKnowledgeStore()

    result = ki.file_knowledge_extraction_tasks(store, registry_path=path)

    assert {entry["source_id"] for entry in result["filed"]} == {"a", "b"}
    assert len(store.tasks) == 2


# -- phase 2: landing ----------------------------------------------------------


def _filed_and_merged(store: FakeKnowledgeStore, path: Path, source_id: str = "gemini-4-traps"):
    ki.file_knowledge_extraction_tasks(store, registry_path=path)
    task = store.tasks[0]
    store.set_task_state(task["id"], "review", pr_url="https://github.com/example/repo/pull/9")
    return task["id"]


def test_land_knowledge_principles_lands_proposed_beads_with_derived_from_links(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    task_id = _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)

    result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert result["status"] == "landed"
    assert len(result["landed"]) == 1
    landed = result["landed"][0]
    assert landed["source_id"] == "gemini-4-traps"
    assert len(landed["principle_ids"]) == 2

    assert len(store.principles) == 2
    for principle in store.principles:
        assert principle["content"]["status"] == "proposed"
        assert principle["content"]["status_history"] == []

    assert len(store.links) == 2
    for link in store.links:
        assert link["link_type"] == "derived_from"
        assert link["target_id"] == task_id
        assert link["source_id"] in {p["id"] for p in store.principles}

    # Never any other status.
    assert {p["content"]["status"] for p in store.principles} == {"proposed"}


def test_land_knowledge_principles_declares_derived_source_class(tmp_path):
    """OPS-86/OPS-90 gate: a fresh arch.principle POST with no source_class
    is refused admission unless the writer is enrolled for the class it
    declares. This activity must declare one, and "derived" -- a mechanical
    mirror of a prior, PR-reviewed extraction task's candidates.json, not a
    synthesis of new text -- is the class factory-dispatcher/knowledge-ingestion
    is enrolled for (bead_rules.SOURCE_CLASS_WRITERS['derived']). See
    .factory/design.md."""
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)

    ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert len(store.principles) == 2
    for principle in store.principles:
        assert principle["content"]["source_class"] == "derived"


def test_land_knowledge_principles_ignores_a_status_injected_by_the_candidate(tmp_path):
    """A candidate is {statement, source, rationale} only. Even if a merged
    candidates.json smuggled in a 'status' field, landing must still force
    'proposed' -- promotion is the Product Owner's act through the registry
    PR path (PRIN-007), never something a worker's output can request."""
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    candidates = sample_candidates()
    candidates[0]["status"] = "adopted"
    write_candidates(tmp_path, "gemini-4-traps", candidates)
    cfg = dispatch.Config(repo_root=tmp_path)

    ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert len(store.principles) == 2
    assert {p["content"]["status"] for p in store.principles} == {"proposed"}


def test_land_knowledge_principles_is_idempotent_across_reruns(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)
    kwargs = dict(
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    first = ki.land_knowledge_principles(store, cfg, **kwargs)
    second = ki.land_knowledge_principles(store, cfg, **kwargs)

    assert len(first["landed"]) == 1
    assert len(second["landed"]) == 0
    assert second["already_landed"] == ["gemini-4-traps"]
    assert len(store.principles) == 2  # never re-landed


def test_land_knowledge_principles_skips_when_pr_not_yet_merged(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)

    result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: open_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert result["landed"] == []
    assert result["not_ready"] == ["gemini-4-traps"]
    assert store.principles == []


def test_land_knowledge_principles_trusts_done_state_without_a_pr_lookup(tmp_path):
    """dispatch.reconcile_review_tasks only reaches review -> done after
    confirming the merge commit is on main, so 'done' alone is sufficient."""
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    task_id = _filed_and_merged(store, registry_path)
    store.set_task_state(task_id, "done")
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)

    def refuse_lookup(url, cfg):
        raise AssertionError("must not look up PR state for an already-done task")

    result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=refuse_lookup,
        is_ancestor=refuse_lookup,
    )

    assert len(result["landed"]) == 1


def test_land_knowledge_principles_rejects_whole_batch_on_malformed_candidate(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    task_id = _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    candidates = sample_candidates()
    del candidates[1]["rationale"]
    write_candidates(tmp_path, "gemini-4-traps", candidates)
    cfg = dispatch.Config(repo_root=tmp_path)

    result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert result["landed"] == []
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["source_id"] == "gemini-4-traps"
    assert "candidate[1] missing rationale" in result["rejected"][0]["reason"]
    assert store.principles == []  # nothing partial
    assert store.links == []

    notes = store.notes[task_id]
    assert len(notes) == 1
    assert notes[0]["content"]["body"].startswith(ki.REJECTED_NOTE_PREFIX)
    assert "rationale" in notes[0]["content"]["body"]

    # No candidate prose (the statement/source text) leaks into the reason.
    assert candidates[0]["statement"] not in notes[0]["content"]["body"]


def test_land_knowledge_principles_rejected_batch_is_not_retried(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    candidates = sample_candidates()
    del candidates[0]["statement"]
    write_candidates(tmp_path, "gemini-4-traps", candidates)
    cfg = dispatch.Config(repo_root=tmp_path)
    kwargs = dict(
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    first = ki.land_knowledge_principles(store, cfg, **kwargs)
    second = ki.land_knowledge_principles(store, cfg, **kwargs)

    assert len(first["rejected"]) == 1
    assert second["rejected"] == []
    assert second["already_landed"] == ["gemini-4-traps"]
    assert store.principles == []


def test_land_knowledge_principles_rejects_missing_candidates_file(tmp_path):
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    task_id = _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    # No candidates file written.
    cfg = dispatch.Config(repo_root=tmp_path)

    result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert result["landed"] == []
    assert len(result["rejected"]) == 1
    assert store.principles == []
    assert store.notes[task_id][0]["content"]["body"].startswith(ki.REJECTED_NOTE_PREFIX)


# -- Pillar 10: no candidate/takeaway text ever leaves the activity boundary -


def test_activity_results_carry_no_candidate_or_takeaway_text(tmp_path):
    """What a Temporal workflow sees is exactly what these activities return.
    If candidate prose never appears in either return value, it never reaches
    workflow history."""
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()

    filing_result = ki.file_knowledge_extraction_tasks(store, registry_path=registry_path)

    task_id = store.tasks[0]["id"]
    store.set_task_state(task_id, "review", pr_url="https://github.com/example/repo/pull/9")
    write_takeaways(tmp_path, "gemini-4-traps", text="# a takeaway nobody should serialize back\n")
    candidates = sample_candidates()
    write_candidates(tmp_path, "gemini-4-traps", candidates)
    cfg = dispatch.Config(repo_root=tmp_path)

    landing_result = ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    serialized = json.dumps(filing_result) + json.dumps(landing_result)
    for candidate in candidates:
        assert candidate["statement"] not in serialized
        assert candidate["rationale"] not in serialized
    assert "a takeaway nobody should serialize back" not in serialized


# -- meta-test: the double rejects what the live substrate rejects -----------


def test_fake_store_status_vocabulary_matches_live_schema():
    live_statuses = set(ArchPrincipleContent.model_fields["status"].annotation.__args__)
    assert live_statuses == {"proposed", "adopted", "enforced", "retired"}

    store = FakeKnowledgeStore()
    for status in live_statuses:
        content = {
            "statement": "s",
            "rationale": "r",
            "source": "src",
            "status": status,
            "status_history": [],
        }
        store.create_principle(content)  # must not raise for any live status


def test_fake_store_rejects_invalid_status_exactly_as_live_schema_does():
    content = {
        "statement": "s",
        "rationale": "r",
        "source": "src",
        "status": "deprecated",
        "status_history": [],
    }
    store = FakeKnowledgeStore()

    with pytest.raises(ValidationError) as fake_exc:
        store.create_principle(content)
    with pytest.raises(ValidationError) as live_exc:
        ArchPrincipleContent.model_validate(content)

    fake_bad = {tuple(e["loc"]) for e in fake_exc.value.errors()}
    live_bad = {tuple(e["loc"]) for e in live_exc.value.errors()}
    assert ("status",) in fake_bad
    assert fake_bad == live_bad


def test_fake_store_link_vocabulary_is_exactly_the_live_vocabulary():
    """Proves the double's link vocabulary tracks the live substrate exactly.

    This asserted a one-type gap (`derived_from`) until S33-B1 closed the live
    vocabulary. Both directions are asserted rather than equality alone, so the
    failure names which way they drifted: a double that admits more is a second
    implementation, and a double that admits less makes tests fail on contracts
    the live schema honours.
    """
    assert FAKE_LINK_TYPES - BEAD_LINK_TYPES == set()
    assert BEAD_LINK_TYPES <= FAKE_LINK_TYPES


def test_fake_store_rejects_unknown_link_type_exactly_as_live_schema_does():
    store = FakeKnowledgeStore()
    with pytest.raises(ValueError, match="link_type must be one of"):
        store.create_link("a", "b", "invented_edge", ki.CREATED_BY)


def test_fake_store_accepts_every_live_link_type():
    store = FakeKnowledgeStore()
    for link_type in BEAD_LINK_TYPES:
        store.create_link("a", "b", link_type, ki.CREATED_BY)  # must not raise


def test_fake_store_accepts_derived_from_now_that_it_is_live():
    store = FakeKnowledgeStore()
    store.create_link("a", "b", "derived_from", ki.CREATED_BY)  # must not raise
    assert store.links[-1]["link_type"] == "derived_from"


def test_knowledge_ingestion_create_link_requires_created_by_with_no_default():
    """#758-class fix: a header-less link write must be unrepresentable at
    this seam. Both the Protocol and its implementation declare `created_by`
    with no default, so a caller cannot satisfy either by omission."""
    import inspect

    for callable_ in (ki.KnowledgeStore.create_link, ki.SubstrateKnowledgeStore.create_link):
        params = inspect.signature(callable_).parameters
        assert "created_by" in params
        assert params["created_by"].default is inspect.Parameter.empty


def test_land_knowledge_principles_link_writes_carry_the_true_writer_identity(tmp_path):
    """The two live link writers named by #729's gate landed edges attributed
    'unknown' because they never passed X-Created-By. This asserts the seam:
    every link this workflow creates carries knowledge-ingestion's own
    CREATED_BY identity, not a header-less default."""
    registry_path = tmp_path / "knowledge_sources.yaml"
    write_registry(registry_path, [sample_source()])
    store = FakeKnowledgeStore()
    _filed_and_merged(store, registry_path)
    write_takeaways(tmp_path, "gemini-4-traps")
    write_candidates(tmp_path, "gemini-4-traps", sample_candidates())
    cfg = dispatch.Config(repo_root=tmp_path)

    ki.land_knowledge_principles(
        store,
        cfg,
        registry_path=registry_path,
        lookup_pr=lambda url, cfg: merged_pr(url),
        is_ancestor=lambda commit, cfg: True,
    )

    assert store.links
    for link in store.links:
        assert link["created_by"] == ki.CREATED_BY


def test_knowledge_ingestion_live_store_create_link_sends_created_by_as_header(monkeypatch):
    """HTTP-seam pin: SubstrateKnowledgeStore.create_link must forward its
    `created_by` argument as the `X-Created-By` header on the live request,
    since POST /beads/{id}/links reads attribution from the header, not the
    JSON body."""
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "link-1"}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "request", fake_request)

    store = ki.SubstrateKnowledgeStore()
    store.create_link("src-1", "tgt-1", "derived_from", "someone-real")

    assert captured["headers"]["X-Created-By"] == "someone-real"
    assert captured["json"] == {"target_id": "tgt-1", "link_type": "derived_from"}


# -- worker registration -------------------------------------------------------


def test_knowledge_ingestion_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.knowledge_ingestion import KnowledgeIngestionWorkflow

    # A class temporalio can't validate as a workflow raises here.
    definition = temporal_workflow._Definition.from_class(KnowledgeIngestionWorkflow)
    assert definition.name == "KnowledgeIngestionWorkflow"
    # build_worker constructs the workflows list inline; confirm the class is
    # actually passed to Worker(...) by reading the registration source,
    # rather than duplicating Temporal SDK internals here.
    assert "KnowledgeIngestionWorkflow" in inspect.getsource(worker.build_worker)


def test_knowledge_ingestion_activities_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "file_knowledge_extraction_tasks_activity" in names
    assert "land_knowledge_principles_activity" in names


def test_knowledge_ingestion_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
    """#727: the change reconciler's live `_request` passed `headers=self._headers`
    unconditionally, so a caller supplying its own `headers=` kwarg collided with it
    (`httpx.request() got multiple values for keyword argument 'headers'`) -- a
    live-only shape the FakeStore, which replaces the whole store, never walks. This
    store shares the same `_request` shape (its `import httpx` is local to `_request`,
    but binds the same cached module patched here). Pins: one merged headers dict
    carrying BOTH the standing auth header and a per-call header, per-call winning on
    collision.
    """
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        captured["method"] = method
        captured["url"] = url

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "request", fake_request)

    store = ki.SubstrateKnowledgeStore()
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": ki.CREATED_BY, "Content-Type": "text/plain"},
    )

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == ki.CREATED_BY
    # Precedence, not just presence: every docstring in this family claims
    # per-call wins on collision, and nothing asserted it -- the merge could
    # be inverted in all six stores with the whole suite still green.
    assert headers["Content-Type"] == "text/plain"
