"""Tests for the arch.change merged-PR reconciler (R26.05/O-1).

No Temporal server, no substrate database, no network, no `gh`: the store is a FakeStore
implementing exactly `activities.change_apply.ChangeApplyStore`'s surface, and every merged-PR/
touched-path read is injected via `list_prs_fn`/`fetch_touched_paths` rather than shelling out.
`arch.change`/`arch.observation` content built by the module under test is validated against the
*real* schemas (apps/substrate/src/schemas.py), imported directly -- not hand-copied -- the same
pattern activities/ea_apply.py's own test suite uses.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "apps" / "substrate"))

import dispatch  # noqa: E402
from activities import change_apply as ca  # noqa: E402
from src.schemas import ArchChangeContent, ArchObservationContent  # noqa: E402

MERGE_PR_MODULE = ca._load_merge_pr_module()
CHANGES_FIXTURE_PATH = _DISPATCHER_ROOT / "tests" / "fixtures" / "changes-2026-09-23.json"


def _load_changes_fixture() -> dict:
    return json.loads(CHANGES_FIXTURE_PATH.read_text())


def _attachment_note(task_id: str, url: str, body_first_line: str) -> dict:
    """The shape dispatch_steps.py's PR-open step actually writes (`sub.add_note(task_id,
    "attachment", body, created_by, provenance=..., url=url)` -> `beadstore.add_note` puts
    `kind` and `url` into `content`): the fixture's attachments[] rows carry no `kind`, so every
    caller that drives them through `match_task_by_pr_url` synthesises it here."""
    return {"parent_id": task_id, "content": {"kind": "attachment", "url": url, "body": body_first_line}}


def _cfg(repo_root: Path | None = None) -> dispatch.Config:
    return dispatch.Config(
        repo="grant/test-repo",
        remote="origin",
        base_ref="main",
        repo_root=repo_root or _REPO_ROOT,
    )


def _pr(number=100, url=None, title="fix(x): thing", body="Change kind: structural", merged_at="2026-09-08T00:00:00Z"):
    return {
        "number": number,
        "url": url or f"https://github.com/grant/test-repo/pull/{number}",
        "title": title,
        "body": body,
        "mergedAt": merged_at,
    }


class FakeStore:
    """Mirrors `SubstrateChangeApplyStore`'s pagination and one-fetch-per-population caching
    (`_PagedDevBeadCache`) over an in-memory list, via `page_size` -- a store with a population
    larger than `page_size` behaves like the real substrate cap, and
    `dev_bead_population_fetches` counts population-level fetches so tests can pin "at most once
    per sweep"."""

    def __init__(
        self, *, tasks=None, releases=None, applications=None, notes=None, changes=None, page_size=500
    ):
        self.changes: dict[str, dict] = {}
        for change in changes or []:
            ref = (change.get("content") or {}).get("ref") or change["id"]
            self.changes[ref] = change
        self.status: dict | None = None
        self.tasks = list(tasks or [])
        self.releases = list(releases or [])
        self.applications = list(applications or [])
        self.notes = list(notes or [])
        self.links: dict[str, list[dict]] = {}
        self._next_id = 1
        self.create_change_calls = 0
        self.update_status_calls = 0
        self.create_status_calls = 0
        self.create_link_calls: list[tuple[str, str, str]] = []
        self._dev_beads = ca._PagedDevBeadCache(self._fetch_dev_bead_page, page_size=page_size)
        self._arch_beads = ca._PagedDevBeadCache(self._fetch_arch_bead_page, page_size=page_size)
        self.list_release_verdicts_calls = 0
        self.list_attachment_notes_calls = 0
        self.list_changes_calls = 0

    def _fetch_dev_bead_page(self, bead_type, limit, offset):
        population = {"task": self.tasks, "release": self.releases, "note": self.notes}[bead_type]
        return population[offset : offset + limit]

    def _fetch_arch_bead_page(self, bead_type, limit, offset):
        population = list(self.changes.values()) if bead_type == "change" else []
        return population[offset : offset + limit]

    def dev_bead_population_fetches(self, bead_type: str) -> int:
        """How many times `dev.<bead_type>` was actually fetched (paged to exhaustion), as
        opposed to how many times a bead was looked up in it."""
        return self._dev_beads.fetch_calls.get(bead_type, 0)

    def _new_id(self, prefix: str) -> str:
        bead_id = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return bead_id

    def find_change(self, ref):
        return self.changes.get(ref)

    def create_change(self, payload):
        self.create_change_calls += 1
        bead = {"id": self._new_id("chg"), **payload}
        self.changes[payload["content"]["ref"]] = bead
        return bead

    def find_status(self):
        return self.status

    def create_status(self, payload):
        self.create_status_calls += 1
        self.status = {"id": self._new_id("obs"), **payload}
        return self.status

    def update_status(self, bead_id, content, context):
        self.update_status_calls += 1
        assert self.status is not None and self.status["id"] == bead_id
        self.status = {**self.status, "content": content, "context": context}
        return self.status

    def find_application_by_ref(self, ref):
        for app in self.applications:
            if (app.get("content") or {}).get("ref") == ref:
                return app
        return None

    def find_task_by_pr_url(self, pr_url):
        return ca.match_task_by_pr_url(self._dev_beads.get("task"), self.list_attachment_notes, pr_url)

    def list_release_verdicts(self):
        self.list_release_verdicts_calls += 1
        # Through the paged cache, NOT `list(self.releases)`: the live store's
        # list_release_verdicts is `_list_dev_beads("release")`, so bypassing the
        # cache here would make OPS-92's release-population assertion read 0 and
        # silently stop guarding the truncation it exists to guard.
        return self._dev_beads.get("release")

    def list_attachment_notes(self):
        self.list_attachment_notes_calls += 1
        return [
            note for note in self._dev_beads.get("note")
            if (note.get("content") or {}).get("kind") == "attachment"
        ]

    def list_changes(self):
        self.list_changes_calls += 1
        return self._arch_beads.get("change")

    def list_links(self, bead_id, *, direction="outgoing", link_type=None):
        return [
            link
            for link in self.links.get(bead_id, [])
            if link["direction"] == direction and (link_type is None or link["link_type"] == link_type)
        ]

    def create_link(self, source_id, target_id, link_type):
        self.create_link_calls.append((source_id, target_id, link_type))
        self.links.setdefault(source_id, []).append(
            {"direction": "outgoing", "link_type": link_type, "target_id": target_id}
        )
        return {"id": self._new_id("link"), "target_id": target_id, "link_type": link_type}


# --- change_ref_for_pr / pr_number_from_url ---------------------------------------------------


def test_change_ref_for_pr_is_stable_and_namespaced():
    assert ca.change_ref_for_pr(657) == "chg.pr-657"


def test_pr_number_from_url_extracts_trailing_digits():
    assert ca.pr_number_from_url("https://github.com/grant/test-repo/pull/657") == "657"


def test_pr_number_from_url_returns_empty_for_an_unrecognized_shape():
    assert ca.pr_number_from_url("not-a-pr-url") == ""


# --- match_task_by_pr_url: resolution order, laziness, pr_refs_only ----------------------------


def test_match_task_by_pr_url_resolves_through_pr_refs_first():
    task = {"id": "t-1", "content": {"pr_refs": ["https://github.test/pr/1"]}}
    found = ca.match_task_by_pr_url([task], lambda: (_ for _ in ()).throw(AssertionError("called")), "https://github.test/pr/1")
    assert found is task


def test_match_task_by_pr_url_resolves_through_pr_url_when_pr_refs_miss():
    task = {"id": "t-1", "content": {"pr_url": "https://github.test/pr/1"}}
    found = ca.match_task_by_pr_url([task], lambda: (_ for _ in ()).throw(AssertionError("called")), "https://github.test/pr/1")
    assert found is task


def test_match_task_by_pr_url_resolves_through_an_attachment_notes_url_field():
    task = {"id": "t-1", "content": {}}
    notes = [_attachment_note("t-1", "https://github.test/pr/1", "Pull request opened for review: https://github.test/pr/1")]
    found = ca.match_task_by_pr_url([task], lambda: notes, "https://github.test/pr/1")
    assert found is task


def test_match_task_by_pr_url_never_matches_a_comment_notes_body_text():
    """The mutation this guards: resolving through a non-attachment note's body text instead of
    an attachment note's `content.url` field. The comment note's body contains the PR url as
    plain text but carries no `url` key and `content.kind != 'attachment'`."""
    task = {"id": "t-1", "content": {}}
    notes = [{"parent_id": "t-1", "content": {"kind": "comment", "body": "see https://github.test/pr/1"}}]
    found = ca.match_task_by_pr_url([task], lambda: notes, "https://github.test/pr/1")
    assert found is None


def test_match_task_by_pr_url_calls_attachment_notes_only_after_both_faster_passes_miss():
    calls = []

    def _attachment_notes():
        calls.append(1)
        return []

    task_resolved_by_pr_refs = {"id": "t-1", "content": {"pr_refs": ["https://github.test/pr/1"]}}
    ca.match_task_by_pr_url([task_resolved_by_pr_refs], _attachment_notes, "https://github.test/pr/1")
    assert calls == []

    task_resolved_by_pr_url = {"id": "t-2", "content": {"pr_url": "https://github.test/pr/2"}}
    ca.match_task_by_pr_url([task_resolved_by_pr_url], _attachment_notes, "https://github.test/pr/2")
    assert calls == []

    unresolved_task = {"id": "t-3", "content": {}}
    ca.match_task_by_pr_url([unresolved_task], _attachment_notes, "https://github.test/pr/3")
    assert calls == [1]


def test_match_task_by_pr_url_with_pr_refs_only_never_falls_through_to_pr_url_or_attachment():
    task = {"id": "t-1", "content": {"pr_url": "https://github.test/pr/1"}}
    found = ca.match_task_by_pr_url(
        [task],
        lambda: (_ for _ in ()).throw(AssertionError("called")),
        "https://github.test/pr/1",
        pr_refs_only=True,
    )
    assert found is None


def test_match_task_by_pr_url_pr_refs_wins_over_a_pr_url_match_on_a_different_task():
    """M13: the pass order must hold even when a *different*, earlier-listed task would also
    match through a later pass -- not just when the earlier passes miss outright. Task A (listed
    first) resolves only through `pr_url`; task B (listed second) resolves through `pr_refs`.
    `pr_refs` is pass 1, so B must win despite being listed after A."""
    url = "https://github.test/pr/1"
    task_pr_url = {"id": "task-a", "content": {"pr_url": url}}
    task_pr_refs = {"id": "task-b", "content": {"pr_refs": [url]}}
    found = ca.match_task_by_pr_url(
        [task_pr_url, task_pr_refs],
        lambda: (_ for _ in ()).throw(AssertionError("called")),
        url,
    )
    assert found is task_pr_refs


def test_match_task_by_pr_url_pr_url_wins_over_an_attachment_match_on_a_different_task():
    """Same check, one pass over: task D (listed first) is only the parent of an attachment
    note carrying the url; task C (listed second) carries `content.pr_url` directly. `pr_url` is
    pass 2, ahead of the attachment fallback, so C must win -- and the attachment callable must
    never even be invoked, since pass 2 already resolves it."""
    url = "https://github.test/pr/1"
    task_attachment_parent = {"id": "task-d", "content": {}}
    task_pr_url = {"id": "task-c", "content": {"pr_url": url}}
    calls = []

    def _attachment_notes():
        calls.append(1)
        return [_attachment_note("task-d", url, f"Pull request opened for review: {url}")]

    found = ca.match_task_by_pr_url([task_attachment_parent, task_pr_url], _attachment_notes, url)
    assert found is task_pr_url
    assert calls == []


# --- match_task_by_pr_url: both stores delegate, neither keeps its own loop (M5b) --------------


def test_fake_store_find_task_by_pr_url_delegates_to_match_task_by_pr_url(monkeypatch):
    """Pins that `FakeStore` keeps no matching loop of its own -- it must call the one
    module-level function, not re-implement the three-pass order. The mutation this guards: a
    full-fidelity matching loop written directly inside the store, which could drift from
    `match_task_by_pr_url` without any test here noticing."""
    sentinel = object()
    calls = []

    def _spy(tasks, attachment_notes, pr_url, **kwargs):
        calls.append((tasks, pr_url))
        return sentinel

    monkeypatch.setattr(ca, "match_task_by_pr_url", _spy)
    tasks = [{"id": "task-1", "content": {}}]
    store = FakeStore(tasks=tasks)

    result = store.find_task_by_pr_url("https://github.test/pr/1")

    assert result is sentinel
    assert calls == [(tasks, "https://github.test/pr/1")]


def test_live_store_find_task_by_pr_url_delegates_to_match_task_by_pr_url(monkeypatch):
    """Same pin, at the live-store seam: `SubstrateChangeApplyStore.find_task_by_pr_url` must
    call the shared `match_task_by_pr_url`, not a loop of its own."""
    sentinel = object()
    calls = []

    def _spy(tasks, attachment_notes, pr_url, **kwargs):
        calls.append((tasks, pr_url))
        return sentinel

    monkeypatch.setattr(ca, "match_task_by_pr_url", _spy)
    tasks = [{"id": "task-1", "content": {}}]

    def fake_request(method, url, **kwargs):
        assert method == "GET"

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return tasks

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()
    result = store.find_task_by_pr_url("https://github.test/pr/1")

    assert result is sentinel
    assert calls == [(tasks, "https://github.test/pr/1")]


# --- merged_prs_since --------------------------------------------------------------------------


def test_merged_prs_since_first_run_starts_at_the_initial_watermark():
    """History before R26.05's opening is unrecorded by design — the first run starts at the
    boundary (inclusive), never at the beginning of gh's page, and applies no lookback below
    a design cut that is not subject to ties or list lag."""
    prs = [
        _pr(1, merged_at="2026-09-08T00:00:00Z"),
        _pr(2, merged_at="2026-09-06T11:00:00Z"),
        _pr(3, merged_at=ca.INITIAL_WATERMARK),
    ]
    ordered = ca.merged_prs_since(prs, None)
    assert [pr["number"] for pr in ordered] == [3, 1]


def test_merged_prs_since_lookback_overlap_recovers_ties_and_late_surfacers():
    """The out-of-order loss-proofing (release-gate finding on #700/#701): a merge whose
    mergedAt ties the watermark to the second, or that surfaces one tick late with a slightly
    earlier mergedAt, must re-enter the window — the dedup gate absorbs re-examination."""
    prs = [
        _pr(1, merged_at="2026-09-08T12:00:00Z"),  # the tie
        _pr(2, merged_at="2026-09-08T11:59:30Z"),  # surfaced late, slightly earlier
        _pr(3, merged_at="2026-09-08T12:00:05Z"),  # genuinely new
        _pr(4, merged_at="2026-09-07T11:59:59Z"),  # beyond the 24h lookback
    ]
    pending = ca.merged_prs_since(prs, "2026-09-08T12:00:00Z")
    assert [pr["number"] for pr in pending] == [2, 1, 3]


def test_merged_prs_since_lookback_boundary_is_inclusive():
    prs = [_pr(1, merged_at="2026-09-07T12:00:00Z")]
    assert ca.merged_prs_since(prs, "2026-09-08T12:00:00Z") == prs


# --- classify_change_kind (change_type derivation + PRIN-008 gap handling) ---------------------


def test_classify_change_kind_structural_maps_to_standard():
    change_type, gap = ca.classify_change_kind(
        "Change kind: structural",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == "standard"
    assert gap is None


def test_classify_change_kind_behavioral_maps_to_normal():
    change_type, gap = ca.classify_change_kind(
        "Change kind: behavioral",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == "normal"
    assert gap is None


def test_classify_change_kind_emergency_marker_overrides_and_never_infers():
    change_type, gap = ca.classify_change_kind(
        "Emergency change: true\n\nChange kind: structural",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == "emergency"
    assert gap is None


def test_classify_change_kind_emergency_marker_alone_is_sufficient():
    change_type, gap = ca.classify_change_kind(
        "Emergency change: TRUE",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == "emergency"
    assert gap is None


def test_classify_change_kind_missing_declaration_defaults_and_states_the_gap():
    change_type, gap = ca.classify_change_kind(
        "no declaration here",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == ca.DEFAULT_CHANGE_TYPE_ON_GAP
    assert gap is not None and "missing or unparseable" in gap


def test_classify_change_kind_multiple_declarations_defaults_and_states_the_gap():
    change_type, gap = ca.classify_change_kind(
        "Change kind: structural\nChange kind: behavioral",
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
    )
    assert change_type == ca.DEFAULT_CHANGE_TYPE_ON_GAP
    assert gap is not None


# --- portfolio path resolution ------------------------------------------------------------------


def test_application_path_prefixes_reads_evidence_paths_and_manifests():
    applications = [
        {
            "ref": "app.widgets",
            "content": {
                "evidence": ["apps/widgets/", "PR #12 not a path"],
                "workload": {"objects": [{"manifest": "infrastructure/k8s/widgets.yaml"}]},
            },
        }
    ]
    prefixes = ca.application_path_prefixes(applications, repo_root=_REPO_ROOT)
    assert prefixes["app.widgets"] == sorted(["apps/widgets/", "infrastructure/k8s/widgets.yaml"])


def test_application_path_prefixes_adds_the_apps_directory_convention_when_it_exists():
    applications = [{"ref": "app.factory-dispatcher", "content": {}}]
    prefixes = ca.application_path_prefixes(applications, repo_root=_REPO_ROOT)
    assert "apps/factory-dispatcher/" in prefixes["app.factory-dispatcher"]


def test_application_path_prefixes_skips_the_convention_for_a_directory_that_does_not_exist():
    applications = [{"ref": "app.nonexistent-thing", "content": {}}]
    prefixes = ca.application_path_prefixes(applications, repo_root=_REPO_ROOT)
    assert "app.nonexistent-thing" not in prefixes


def test_applications_for_touched_paths_matches_and_dedupes():
    prefixes = {"app.a": ["apps/a/"], "app.b": ["apps/b/", "docs/b.md"]}
    touched = ["apps/a/x.py", "apps/a/y.py", "docs/b.md"]
    assert ca.applications_for_touched_paths(touched, prefixes) == ["app.a", "app.b"]


def test_applications_for_touched_paths_returns_empty_when_nothing_matches():
    prefixes = {"app.a": ["apps/a/"]}
    assert ca.applications_for_touched_paths(["scripts/tool.py"], prefixes) == []


# --- build_change_content: schema-legal output, including gap and fallback paths ---------------


def _content(**overrides):
    pr = _pr(**{k: v for k, v in overrides.items() if k in ("number", "url", "title", "body", "merged_at")})
    return ca.build_change_content(
        pr=pr,
        touched_paths=overrides.get("touched_paths", ["apps/factory-dispatcher/dispatch.py"]),
        application_prefixes=overrides.get(
            "application_prefixes", {"app.factory-dispatcher": ["apps/factory-dispatcher/"]}
        ),
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        verdict_evidence=overrides.get("verdict_evidence", "release-gate verdict: merge (dev.release r-1)"),
    )


def test_build_change_content_is_schema_legal_for_the_happy_path():
    content = _content()
    validated = ArchChangeContent.model_validate(content)
    assert validated.change_type == "standard"
    assert validated.applications == ["app.factory-dispatcher"]
    assert content["evidence"][0] == f"https://github.com/grant/test-repo/pull/{100}"
    assert "release_ref" not in content
    assert content["source_class"] == "derived"


def test_build_change_content_states_the_gap_when_change_kind_is_unparseable():
    content = _content(body="no declaration")
    validated = ArchChangeContent.model_validate(content)
    assert validated.change_type == ca.DEFAULT_CHANGE_TYPE_ON_GAP
    assert any("missing or unparseable" in entry for entry in content["evidence"])


def test_build_change_content_falls_back_to_unresolved_application_with_stated_gap():
    content = _content(touched_paths=["docs/random.md"], application_prefixes={})
    validated = ArchChangeContent.model_validate(content)
    assert validated.applications == [ca.UNRESOLVED_APPLICATION_REF]
    assert any("no portfolio application matched" in entry for entry in content["evidence"])


def test_build_change_content_never_writes_release_ref_into_content():
    content = _content()
    assert "release_ref" not in content or content["release_ref"] is None


# --- land_merged_pr: idempotency, verdict evidence, delivers binding ----------------------------


def test_land_merged_pr_creates_once_and_is_zero_writes_on_a_second_call():
    store = FakeStore()
    cfg = _cfg()
    pr = _pr()
    kwargs = dict(
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: ["apps/factory-dispatcher/dispatch.py"],
        release_verdicts=[],
    )

    first = ca.land_merged_pr(store, cfg, pr, **kwargs)
    assert first["status"] == "created"
    assert store.create_change_calls == 1

    second = ca.land_merged_pr(store, cfg, pr, **kwargs)
    assert second["status"] == "unchanged"
    assert store.create_change_calls == 1


def test_land_merged_pr_lands_a_merge_that_bypassed_merge_pr_sh():
    """AC-3: nothing about landing depends on any merge-pr.sh side effect.

    Simulates a PR body written directly by a human at PR-creation time (never touched by
    scripts/merge-pr.py's squash-commit composition, and merged via some other path entirely --
    e.g. GitHub's own merge button). The reconciler reads the PR body over `gh pr view` at
    reconcile time, so this landing does not depend on how the PR was merged at all.
    """
    store = FakeStore()
    cfg = _cfg()
    pr = _pr(number=999, body="Change kind: behavioral", title="a hand-merged hotfix")
    result = ca.land_merged_pr(
        store,
        cfg,
        pr,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: ["docs/runbooks/whatever.md"],
        release_verdicts=[],
    )
    assert result["status"] == "created"
    bead = store.changes["chg.pr-999"]
    assert bead["content"]["change_type"] == "normal"
    ArchChangeContent.model_validate(bead["content"])


def test_land_merged_pr_binds_release_via_the_delivers_edge_of_the_matching_task():
    pr = _pr()
    task = {"id": "task-1", "content": {"pr_refs": [pr["url"]]}}
    store = FakeStore(tasks=[task])
    store.links["task-1"] = [
        {"direction": "outgoing", "link_type": "delivers", "target_id": "release-1"}
    ]

    ca.land_merged_pr(
        store,
        _cfg(),
        pr,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: [],
        release_verdicts=[],
    )

    assert store.create_link_calls
    source_id, target_id, link_type = store.create_link_calls[0]
    assert target_id == "release-1"
    assert link_type == "delivers"


def test_land_merged_pr_with_no_matching_task_creates_no_delivers_edge():
    store = FakeStore()
    ca.land_merged_pr(
        store,
        _cfg(),
        _pr(),
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: [],
        release_verdicts=[],
    )
    assert store.create_link_calls == []


# --- dev.task/dev.release population: paged to exhaustion, fetched once per sweep -------------


def test_find_task_by_pr_url_pages_past_the_population_cap_instead_of_truncating():
    """A population larger than one page must not be silently treated as the whole population:
    the target bead sits beyond the first page (population 6 > page_size 2), and the lookup
    must still find it by paging, not return None as if it does not exist."""
    tasks = [{"id": f"task-{i}", "content": {"pr_refs": []}} for i in range(5)]
    target_url = "https://github.com/grant/test-repo/pull/999"
    tasks.append({"id": "task-target", "content": {"pr_refs": [target_url]}})
    store = FakeStore(tasks=tasks, page_size=2)

    found = store.find_task_by_pr_url(target_url)

    assert found is not None
    assert found["id"] == "task-target"


def test_dev_bead_listing_fails_loudly_if_paging_never_terminates():
    """A store that never reports a short page (e.g. a server bug that ignores `offset`) must
    raise rather than loop forever or hand back a silently partial population."""
    cache = ca._PagedDevBeadCache(
        lambda bead_type, limit, offset: [{"id": f"t-{offset}"}] * limit,
        page_size=2,
        max_pages=3,
    )
    with pytest.raises(ca.ChangeApplyError, match="did not terminate"):
        cache.get("task")


def test_apply_merged_pr_changes_fetches_dev_task_population_once_per_sweep():
    """The reconciler must not refetch the whole dev.task population once per PR: a sweep of
    three merged PRs, each resolving its `delivers` edge via a matching dev.task, must cost the
    same one population fetch a sweep of one PR would -- not one fetch per PR."""
    def _task(number):
        pr_url = f"https://github.com/grant/test-repo/pull/{number}"
        return {"id": f"task-{number}", "content": {"pr_refs": [pr_url]}}

    store = FakeStore(tasks=[_task(1), _task(2), _task(3)])
    prs = [
        _pr(1, merged_at="2026-09-08T00:00:00Z"),
        _pr(2, merged_at="2026-09-08T00:01:00Z"),
        _pr(3, merged_at="2026-09-08T00:02:00Z"),
    ]

    result = ca.apply_merged_pr_changes(
        store,
        _cfg(),
        list_prs_fn=lambda cfg: prs,
        fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    assert result["created"] == 3
    # Each PR resolves both its delivers edge (dev.task) and its verdict evidence (dev.release);
    # without the fix this would be 3 fetches per type (one per PR), not 1.
    assert store.dev_bead_population_fetches("task") == 1
    assert store.dev_bead_population_fetches("release") == 1


# --- AC-1: the committed fixture measures the widened match ------------------------------------


def test_release_binding_resolves_through_pr_url():
    """PC-DEL-004/AC-1: `match_task_by_pr_url` over the committed fixture's `done_tasks` and
    `attachments`, driven for every landed change's `evidence_pr_url`, must resolve exactly as
    many as the fixture header says the widened matcher resolves (171, all through
    `content.pr_url`, none needing the attachment fallback) -- and, with `pr_refs_only=True`,
    exactly what the pre-B19 matcher alone resolved (0, since nothing in this population writes
    `content.pr_refs`). 'Resolves' means the matched task also carries a `delivers_target`
    (`resolve_release_binding` would return a release id for it)."""
    fixture = _load_changes_fixture()
    tasks = fixture["done_tasks"]
    notes = [
        _attachment_note(a["task_id"], a["url"], a["body_first_line"]) for a in fixture["attachments"]
    ]

    def _resolving_count(*, pr_refs_only: bool) -> int:
        count = 0
        for change in fixture["changes"]:
            task = ca.match_task_by_pr_url(
                tasks, lambda: notes, change["evidence_pr_url"], pr_refs_only=pr_refs_only
            )
            if task is not None and task.get("delivers_target"):
                count += 1
        return count

    assert _resolving_count(pr_refs_only=False) == fixture["header"]["resolvable_by_pr_url"]
    assert _resolving_count(pr_refs_only=True) == fixture["header"]["resolvable_by_pr_refs"]


# --- verdict evidence resolution -----------------------------------------------------------------


@pytest.mark.parametrize(
    "pr_ref",
    [
        "https://github.com/grant/test-repo/pull/100",
        "#100",
        "PR-100",
        "PR #100",
    ],
)
def test_resolve_verdict_evidence_matches_every_accepted_pr_ref_spelling(pr_ref):
    """Pins that the evidence is DERIVED from store content, not hardcoded: the verdict text
    ('merge') and the release id ('rel-1') both flow straight out of the release record, through
    the same code path exercised when the store holds nothing at all -- so a real dev.release
    writer landing later needs no edit here to be reflected."""
    release = {"id": "rel-1", "content": {"verdict": "merge", "pr_refs": [pr_ref]}}
    evidence = ca.resolve_verdict_evidence(
        [release], pr_number="100", pr_url="https://github.com/grant/test-repo/pull/100"
    )
    assert "merge" in evidence and "rel-1" in evidence


def test_resolve_verdict_evidence_states_the_structural_gap_when_the_store_holds_no_release_records():
    """AC-1: 'no verdict record exists for any PR, because nothing writes this type' must read
    differently from 'this PR has no verdict' -- a reader of the change ledger must be able to
    tell an ungated merge from an unwritten record."""
    evidence = ca.resolve_verdict_evidence(
        [], pr_number="100", pr_url="https://github.com/grant/test-repo/pull/100"
    )
    assert evidence == ca.NO_RELEASE_VERDICTS_WRITTEN_EVIDENCE
    assert "no dev.release found referencing this PR" not in evidence

    # Assert the literal substance, not only equality with the constant. Comparing
    # `evidence` to the constant it came from is a tautology: rewriting the constant
    # to "release-gate verdict: merge (all suites green)" left the whole suite green
    # while every arch.change record would then assert a gate verdict for a PR that
    # nothing gated -- the exact class of lie this bead exists to remove.
    assert "no dev.release record exists" in evidence
    assert "no writer" in evidence
    assert "structural gap" in evidence
    # And it must never read as a verdict. A ledger entry that says a merge was
    # approved when no record of approval exists is worse than a blank one.
    for verdict_word in ("merge-with-changes", "do-not-merge", "approved", "green"):
        assert verdict_word not in evidence.lower()
    assert not evidence.lower().startswith("release-gate verdict: merge")


def test_resolve_verdict_evidence_states_the_per_pr_gap_when_releases_exist_but_none_match():
    """The per-PR sentence is honest exactly when a writer demonstrably exists (the list is
    non-empty) and simply has no record naming this PR -- distinct from the structural case."""
    other_release = {"id": "rel-9", "content": {"verdict": "merge", "pr_refs": ["#999"]}}
    evidence = ca.resolve_verdict_evidence(
        [other_release], pr_number="100", pr_url="https://github.com/grant/test-repo/pull/100"
    )
    assert "no dev.release found referencing this PR" in evidence
    assert evidence != ca.NO_RELEASE_VERDICTS_WRITTEN_EVIDENCE


# --- apply_merged_pr_changes: end to end, watermark, zero-writes-when-unchanged -----------------


def test_apply_merged_pr_changes_creates_one_record_per_merged_pr():
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z"), _pr(2, merged_at="2026-09-09T00:00:00Z")]
    result = ca.apply_merged_pr_changes(
        store,
        _cfg(),
        list_prs_fn=lambda cfg: prs,
        fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
        now_fn=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    assert result["created"] == 2
    assert len(store.changes) == 2
    assert store.status["context"]["last_merged_at"] == "2026-09-09T00:00:00Z"
    ArchObservationContent.model_validate(store.status["content"])


def test_watermark_observation_declares_source_class_derived_and_created_by_on_create():
    """"factory-dispatcher/change-apply" is already enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] -- the first
    landing (create) of obs.arch-change-watermark must declare it, not only the
    arch.change beads this reconciler also writes."""
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z")]

    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=lambda cfg: prs, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    assert store.status["content"]["source_class"] == "derived"
    assert store.status["created_by"] == ca.CREATED_BY


def test_watermark_observation_declares_source_class_derived_and_created_by_on_update():
    """Same identity, existing-status (update) path -- a second sweep that advances the
    watermark patches the same standing bead."""
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z")]
    list_prs_fn = lambda cfg: prs  # noqa: E731
    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    prs.append(_pr(2, merged_at="2026-09-09T00:00:00Z"))
    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    assert store.update_status_calls == 1
    assert store.status["content"]["source_class"] == "derived"
    assert store.status["created_by"] == ca.CREATED_BY


def test_apply_merged_pr_changes_is_zero_writes_the_second_time_over_the_same_range():
    """With the lookback, the second run re-EXAMINES the recent window (that is the
    loss-proofing) but WRITES nothing: no new bead, no new link, no status advance."""
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z")]
    list_prs_fn = lambda cfg: prs  # noqa: E731

    first = ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    assert first["created"] == 1
    creates_after_first = store.create_change_calls
    links_after_first = list(store.create_link_calls)
    status_writes_after_first = store.update_status_calls + store.create_status_calls

    second = ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    assert second["created"] == 0
    assert second["unchanged"] == 1
    assert store.create_change_calls == creates_after_first
    assert store.create_link_calls == links_after_first
    assert store.update_status_calls + store.create_status_calls == status_writes_after_first


def test_apply_merged_pr_changes_only_advances_for_new_merges_after_the_watermark():
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z")]
    list_prs_fn = lambda cfg: prs  # noqa: E731
    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    prs.append(_pr(2, merged_at="2026-09-09T00:00:00Z"))
    result = ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    assert result["created"] == 1
    assert set(store.changes) == {"chg.pr-1", "chg.pr-2"}


def test_apply_merged_pr_changes_reads_release_verdicts_once_per_sweep_not_per_pr():
    """AC-2: the reconciler must not perform a per-PR search against dev.release -- a bead type
    it can determine (by observing one fetch returns nothing) has no writer. Sweeping 3 merged
    PRs must cost exactly one release-listing store read, not three."""
    store = FakeStore()
    prs = [
        _pr(1, merged_at="2026-09-08T00:00:00Z"),
        _pr(2, merged_at="2026-09-08T01:00:00Z"),
        _pr(3, merged_at="2026-09-08T02:00:00Z"),
    ]
    result = ca.apply_merged_pr_changes(
        store,
        _cfg(),
        list_prs_fn=lambda cfg: prs,
        fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    assert result["created"] == 3
    assert store.list_release_verdicts_calls == 1


def test_apply_merged_pr_changes_reflects_a_real_release_verdict_without_editing_the_evidence_string():
    """Pins that the evidence flowing all the way into the arch.change record is derived from
    store content, end to end -- a real dev.release write is picked up by the same code that
    renders the structural gap, with no code change required."""
    pr = _pr(50)
    release = {"id": "rel-50", "content": {"verdict": "merge", "pr_refs": [pr["url"]]}}
    store = FakeStore(releases=[release])
    ca.apply_merged_pr_changes(
        store,
        _cfg(),
        list_prs_fn=lambda cfg: [pr],
        fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    evidence = store.changes["chg.pr-50"]["content"]["evidence"]
    assert any("merge" in entry and "rel-50" in entry for entry in evidence)
    assert not any(entry == ca.NO_RELEASE_VERDICTS_WRITTEN_EVIDENCE for entry in evidence)


# --- AC-3: the sweep resolves the factory-opened shape (content.pr_url), not only pr_refs ------


def test_land_merged_pr_binds_release_via_a_task_carrying_only_content_pr_url():
    """The factory shape: dispatch_steps.py's PR-open step writes `content.pr_url` via
    `patch_content`, never `content.pr_refs` (only `--bind-pr` writes that). Before B19 this
    task was invisible to `find_task_by_pr_url` and the change landed with no `delivers` edge."""
    pr = _pr()
    task = {"id": "task-1", "content": {"pr_url": pr["url"]}}
    store = FakeStore(tasks=[task])
    store.links["task-1"] = [
        {"direction": "outgoing", "link_type": "delivers", "target_id": "release-1"}
    ]

    ca.land_merged_pr(
        store,
        _cfg(),
        pr,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: [],
        release_verdicts=[],
    )

    assert store.create_link_calls
    source_id, target_id, link_type = store.create_link_calls[0]
    assert target_id == "release-1"
    assert link_type == "delivers"


def test_land_merged_pr_still_binds_release_via_the_pr_refs_shape():
    """The pre-B19 shape (`--bind-pr` writes `content.pr_refs`) must keep resolving."""
    pr = _pr()
    task = {"id": "task-1", "content": {"pr_refs": [pr["url"]]}}
    store = FakeStore(tasks=[task])
    store.links["task-1"] = [
        {"direction": "outgoing", "link_type": "delivers", "target_id": "release-1"}
    ]

    ca.land_merged_pr(
        store,
        _cfg(),
        pr,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes={},
        fetch_touched_paths=lambda cfg, number: [],
        release_verdicts=[],
    )

    assert store.create_link_calls
    assert store.create_link_calls[0][1:] == ("release-1", "delivers")


def test_apply_merged_pr_changes_reads_no_attachment_notes_when_every_pr_resolves_through_pr_url():
    """Neither `apply_merged_pr_changes` nor `land_merged_pr` may read the attachment notes up
    front: the whole sweep, not one lookup, must cost zero `list_attachment_notes()` calls when
    every PR resolves through `content.pr_url`."""
    def _task(number):
        pr_url = f"https://github.com/grant/test-repo/pull/{number}"
        return {"id": f"task-{number}", "content": {"pr_url": pr_url}}

    store = FakeStore(tasks=[_task(1), _task(2)])
    prs = [
        _pr(1, merged_at="2026-09-08T00:00:00Z"),
        _pr(2, merged_at="2026-09-08T00:01:00Z"),
    ]

    result = ca.apply_merged_pr_changes(
        store,
        _cfg(),
        list_prs_fn=lambda cfg: prs,
        fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )

    assert result["created"] == 2
    assert store.list_attachment_notes_calls == 0


# --- affects edges + two-gate resume (release-gate findings F2 + regression on #701) ---------


def _app(ref, bead_id):
    return {"id": bead_id, "content": {"ref": ref}}


def _land(store, pr, prefixes, touched, *, release_verdicts=()):
    return ca.land_merged_pr(
        store,
        _cfg(),
        pr,
        extract_change_kind=MERGE_PR_MODULE.extract_change_kind,
        change_kind_re=MERGE_PR_MODULE.CHANGE_KIND_RE,
        application_prefixes=prefixes,
        fetch_touched_paths=lambda cfg, number: touched,
        release_verdicts=list(release_verdicts),
    )


def test_land_merged_pr_writes_an_affects_edge_per_resolved_application():
    """The schema's own contract: a change is an event against a CI, carried by an affects
    edge (PRIN-003) — the console's persona panels traverse exactly this edge."""
    store = FakeStore(applications=[_app("app.widget", "app-1"), _app("app.gadget", "app-2")])
    prefixes = {"app.widget": ["apps/widget/"], "app.gadget": ["apps/gadget/"]}

    result = _land(store, _pr(10), prefixes, ["apps/widget/x.py", "apps/gadget/y.py"])

    assert result["status"] == "created"
    affects = [c for c in store.create_link_calls if c[2] == "affects"]
    assert {(c[1]) for c in affects} == {"app-1", "app-2"}


def test_land_merged_pr_writes_no_affects_edge_for_the_unresolved_sentinel():
    store = FakeStore()
    result = _land(store, _pr(11), {}, ["scripts/x.py"])
    assert result["status"] == "created"
    assert [c for c in store.create_link_calls if c[2] == "affects"] == []


def test_resume_after_bead_created_but_edges_not_written_finishes_the_edges():
    """The two-gate resume, restored: a crash between create_change and the link writes must
    be completable — the bead existing does not mean its edges do."""
    store = FakeStore(
        applications=[_app("app.widget", "app-1")],
        tasks=[{"id": "task-1", "content": {"pr_refs": [_pr(12)["url"]]}}],
    )
    store.links["task-1"] = [
        {"direction": "outgoing", "link_type": "delivers", "target_id": "rel-1"}
    ]
    prefixes = {"app.widget": ["apps/widget/"]}
    # First landing crashes after create_change: simulate by landing then deleting the links.
    _land(store, _pr(12), prefixes, ["apps/widget/x.py"])
    change_id = store.changes["chg.pr-12"]["id"]
    store.links[change_id] = []
    creates_before = store.create_change_calls

    result = _land(store, _pr(12), prefixes, ["apps/widget/x.py"])

    assert result["status"] == "completed_edges"
    assert store.create_change_calls == creates_before  # no duplicate bead
    edge_types = {link["link_type"] for link in store.links[change_id]}
    assert edge_types == {"delivers", "affects"}


def test_resume_with_edges_complete_is_unchanged_and_writes_nothing():
    store = FakeStore(applications=[_app("app.widget", "app-1")])
    prefixes = {"app.widget": ["apps/widget/"]}
    _land(store, _pr(13), prefixes, ["apps/widget/x.py"])
    links_before = list(store.create_link_calls)

    result = _land(store, _pr(13), prefixes, ["apps/widget/x.py"])

    assert result["status"] == "unchanged"
    assert store.create_link_calls == links_before


def test_first_run_records_the_pre_history_gap_once(monkeypatch):
    """PRIN-008: the pre-charter gap is stated on the standing status bead, not silent."""
    store = FakeStore()
    prs = [_pr(1, merged_at="2026-09-08T00:00:00Z")]
    list_prs_fn = lambda cfg: prs  # noqa: E731
    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    assert store.status["context"]["pre_history_gap"] == ca.PRE_HISTORY_GAP_NOTE

    prs.append(_pr(2, merged_at="2026-09-09T00:00:00Z"))
    ca.apply_merged_pr_changes(
        store, _cfg(), list_prs_fn=list_prs_fn, fetch_touched_paths=lambda cfg, number: [],
        merge_pr_module=MERGE_PR_MODULE,
    )
    # The gap record survives later status writes rather than being overwritten away.
    assert store.status["context"]["pre_history_gap"] == ca.PRE_HISTORY_GAP_NOTE


# --- AC-2: backfill_change_edges walks every existing arch.change idempotently -----------------


def _seeded_backfill_store() -> FakeStore:
    """The fixture's `done_tasks`, `changes` and `attachments`, loaded into a `FakeStore` with
    each task's `delivers_target` and each change's `outgoing_links` seeded as pre-existing
    links -- exactly the shape the fixture header's `existing_delivers_edges` and
    `expected_new_delivers_edges` measure against. No `arch.application` beads: every `affects`
    resolution misses, the way the fixture's own population does."""
    fixture = _load_changes_fixture()
    tasks = fixture["done_tasks"]
    notes = [
        _attachment_note(a["task_id"], a["url"], a["body_first_line"]) for a in fixture["attachments"]
    ]
    changes = [
        {
            "id": c["id"],
            "content": {"evidence": [c["evidence_pr_url"], "verdict placeholder"], "applications": c["applications"]},
            "outgoing_links": c.get("outgoing_links") or [],
        }
        for c in fixture["changes"]
    ]
    store = FakeStore(tasks=tasks, notes=notes, changes=changes)
    for task in tasks:
        if task.get("delivers_target"):
            store.links[task["id"]] = [
                {"direction": "outgoing", "link_type": "delivers", "target_id": task["delivers_target"]}
            ]
    for change in changes:
        for link in change["outgoing_links"]:
            store.links.setdefault(change["id"], []).append(
                {"direction": "outgoing", "link_type": link["link_type"], "target_id": link["target_id"]}
            )
    return store, fixture


def test_backfill_is_idempotent():
    store, fixture = _seeded_backfill_store()
    expected_new = fixture["header"]["expected_new_delivers_edges"]

    dry = ca.backfill_change_edges(store, dry_run=True)
    assert dry["changes"] == fixture["header"]["change_count"]
    assert dry["edges_created"] == {"delivers": expected_new, "affects": 0}
    assert store.create_link_calls == []
    assert len(dry["unresolved"]) == (
        fixture["header"]["change_count"] - fixture["header"]["resolvable_by_pr_url"]
    )

    wet = ca.backfill_change_edges(store, dry_run=False)
    assert wet["edges_created"] == {"delivers": expected_new, "affects": 0}
    assert len(store.create_link_calls) == expected_new
    assert all(call[2] == "delivers" for call in store.create_link_calls)

    second_wet = ca.backfill_change_edges(store, dry_run=False)
    assert second_wet["edges_created"] == {"delivers": 0, "affects": 0}
    assert len(store.create_link_calls) == expected_new


def test_backfill_dry_run_with_an_application_seeded_writes_nothing():
    """M12/M15: the fixture-seeded idempotency test above seeds no `arch.application`, so every
    `affects` resolution misses by construction -- a dry run that accidentally created an
    `affects` link would still pass it. Seed one change whose `applications` names a seeded
    `arch.application`, plus a task that resolves the same PR to a release, so both edge kinds
    are live for this change: a dry run must still create nothing, and the wet run must create
    both exactly once."""
    change = {
        "id": "chg-1",
        "content": {
            "evidence": ["https://github.com/grant/test-repo/pull/500", "verdict placeholder"],
            "applications": ["app.widget"],
        },
    }
    task = {"id": "task-500", "content": {"pr_url": "https://github.com/grant/test-repo/pull/500"}}
    store = FakeStore(changes=[change], tasks=[task], applications=[_app("app.widget", "app-1")])
    store.links["task-500"] = [
        {"direction": "outgoing", "link_type": "delivers", "target_id": "release-1"}
    ]

    dry = ca.backfill_change_edges(store, dry_run=True)
    assert dry["edges_created"] == {"delivers": 1, "affects": 1}
    assert store.create_link_calls == []

    wet = ca.backfill_change_edges(store, dry_run=False)
    assert wet["edges_created"] == {"delivers": 1, "affects": 1}
    assert len(store.create_link_calls) == 2

    second_wet = ca.backfill_change_edges(store, dry_run=False)
    assert second_wet["edges_created"] == {"delivers": 0, "affects": 0}
    assert len(store.create_link_calls) == 2


# --- worker/workflow registration ----------------------------------------------------------------


def test_change_apply_workflow_registered_with_worker():
    import inspect

    from temporalio import workflow as temporal_workflow

    import worker
    from workflows.change_apply import ChangeApplyWorkflow

    definition = temporal_workflow._Definition.from_class(ChangeApplyWorkflow)
    assert definition.name == "ChangeApplyWorkflow"
    assert "ChangeApplyWorkflow" in inspect.getsource(worker.build_worker)


def test_change_apply_activity_registered_with_worker():
    from activities import ACTIVITIES

    names = {getattr(a, "__name__", "") for a in ACTIVITIES}
    assert "apply_merged_pr_changes_activity" in names


def test_change_apply_live_store_request_merges_per_call_headers_over_auth(monkeypatch):
    """The first live sweep died on `httpx.request() got multiple values for
    keyword argument 'headers'`: create_link passed X-Created-By through
    **kwargs while _request also passed the auth headers positionally. The
    FakeStore replaces the whole store, so only a test at the HTTP seam sees
    this. Pins: one merged headers dict carrying BOTH the auth key and the
    per-call header, per-call winning on collision.
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
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()
    store.create_link("src-1", "tgt-1", "delivers")

    headers = captured["headers"]
    assert headers["X-API-Key"] == "test-key"
    assert headers["X-Created-By"] == ca.CREATED_BY

    # Precedence, not just presence. create_link's X-Created-By never collides
    # with the standing headers, so the assertions above pass even with the
    # merge inverted -- proven by mutation. Drive the seam once more with a
    # colliding header so this store is pinned like the other six.
    store._request(
        "POST",
        "/beads/x/links",
        headers={"X-Created-By": ca.CREATED_BY, "Content-Type": "text/plain"},
    )
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["headers"]["X-API-Key"] == "test-key"


def test_live_store_pages_the_dev_population_and_fetches_it_once(monkeypatch):
    """The seam the FakeStore cannot reach.

    OPS-92's fix lives on ``SubstrateChangeApplyStore``: page ``dev.task`` to
    exhaustion, then answer every later lookup from the one snapshot. The
    FakeStore replaces the whole store and carries its own cache, so the
    AC1/AC2 tests pin ``_PagedDevBeadCache`` -- the shared mechanism -- and
    leave the production wiring untested: revert ``_list_dev_beads`` to a
    single capped read and those tests all stay green.

    This is the same lesson #727 paid for in this very file (see
    ``test_change_apply_live_store_request_merges_per_call_headers_over_auth``): the first
    live sweep died at the HTTP seam because only a fake stood between the
    tests and it. So drive the real store over a population larger than one
    page, and pin both halves at the seam -- the bead past the first page is
    found, and a second lookup costs no further request.
    """
    page_size = ca.DEV_BEAD_PAGE_SIZE
    population = [
        {"id": f"task-{i}", "content": {"pr_refs": [f"https://github.test/pr/{i}"]}}
        for i in range(page_size + 7)
    ]
    requests: list[tuple[int, int]] = []

    def fake_request(method, url, **kwargs):
        params = kwargs.get("params") or {}
        assert method == "GET"
        requests.append((params["limit"], params.get("offset", 0)))
        offset = params.get("offset", 0)
        page = population[offset : offset + params["limit"]]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return page

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()

    # The bead one past the first page: exactly what a single capped read drops.
    past_the_cap = f"https://github.test/pr/{page_size + 3}"
    assert store.find_task_by_pr_url(past_the_cap)["id"] == f"task-{page_size + 3}"
    assert len(requests) > 1, "a single request cannot have covered the population"
    assert requests[0] == (page_size, 0)

    # Every later lookup is free: one population fetch per sweep, not per PR.
    after_first_lookup = len(requests)
    for i in range(4):
        assert store.find_task_by_pr_url(f"https://github.test/pr/{i}") is not None
    assert len(requests) == after_first_lookup


def test_live_store_pages_release_verdicts_and_fetches_them_once(monkeypatch):
    """The seam the FakeStore cannot reach, for `list_release_verdicts`.

    `_list_dev_beads("task")` got its live-store seam test in OPS-92's last mile
    (`test_live_store_pages_the_dev_population_and_fetches_it_once`); its sibling call,
    `_list_dev_beads("release")` via `list_release_verdicts`, did not, and is the exact blind
    spot #727 and OPS-92 each already paid for in this file: the FakeStore replaces the whole
    store and carries its own cache, so every AC test that only drives `list_release_verdicts`
    through FakeStore pins `_PagedDevBeadCache` in the abstract and leaves the production
    `namespace=dev&type=release` wiring untested. Revert `list_release_verdicts` to a single
    capped read and every FakeStore-backed test still stays green.

    So drive the real store over a `dev.release` population larger than one page, at the httpx
    seam: the release bead past the first page is found, a second sweep-scoped call costs no
    further request, and the request params are namespaced `dev`/`release` -- not `dev`/`task`.
    """
    page_size = ca.DEV_BEAD_PAGE_SIZE
    population = [
        {"id": f"release-{i}", "content": {"pr_refs": [f"PR-{i}"], "verdict": "pass"}}
        for i in range(page_size + 7)
    ]
    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        params = kwargs.get("params") or {}
        assert method == "GET"
        requests.append(dict(params))
        offset = params.get("offset", 0)
        page = population[offset : offset + params["limit"]]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return page

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()

    releases = store.list_release_verdicts()
    assert len(releases) == page_size + 7, "a single capped read would have dropped the tail"
    assert releases[page_size + 3]["id"] == f"release-{page_size + 3}"
    assert len(requests) > 1, "a single request cannot have covered the population"
    assert requests[0]["namespace"] == "dev"
    assert requests[0]["type"] == "release"
    assert requests[0]["limit"] == page_size
    assert requests[0].get("offset", 0) == 0

    # One population fetch per sweep, not one per lookup: the second call is free.
    after_first_fetch = len(requests)
    store.list_release_verdicts()
    assert len(requests) == after_first_fetch


def test_live_store_list_changes_pages_the_arch_population_and_fetches_it_once(monkeypatch):
    """The seam the FakeStore cannot reach, for `list_changes`.

    `backfill_change_edges` (AC-2) walks every `arch.change` through
    `SubstrateChangeApplyStore.list_changes()`, which must page `GET /beads?namespace=arch&
    type=change` to exhaustion the same way `_list_dev_beads` already does for `dev.task`/`dev.
    release` -- `_fetch_dev_bead_page` hard-codes `namespace=dev`, so `_fetch_arch_bead_page` is
    its own, separately-tested wiring. The FakeStore carries its own `_PagedDevBeadCache`
    instance and cannot regress this. Drive the real store over an `arch.change` population
    larger than one page: every request is a `namespace=arch`/`type=change` GET, offsets advance
    by the page size until a short page, the full population comes back, and a second call costs
    no further request.
    """
    page_size = ca.DEV_BEAD_PAGE_SIZE
    population = [
        {"id": f"chg-{i}", "content": {"evidence": [f"https://github.test/pr/{i}"]}}
        for i in range(page_size + 7)
    ]
    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        params = kwargs.get("params") or {}
        assert method == "GET"
        assert url.endswith("/beads")
        requests.append(dict(params))
        offset = params.get("offset", 0)
        page = population[offset : offset + params["limit"]]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return page

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()

    changes = store.list_changes()
    assert len(changes) == page_size + 7, "a single capped read would have dropped the tail"
    assert changes[page_size + 3]["id"] == f"chg-{page_size + 3}"
    assert len(requests) > 1, "a single request cannot have covered the population"
    assert all(r["namespace"] == "arch" and r["type"] == "change" for r in requests)
    offsets = [r.get("offset", 0) for r in requests]
    assert offsets[0] == 0
    for i in range(1, len(offsets)):
        assert offsets[i] == offsets[i - 1] + page_size

    # One population fetch per sweep, not one per lookup: the second call is free.
    after_first_fetch = len(requests)
    store.list_changes()
    assert len(requests) == after_first_fetch


def test_live_store_find_task_by_pr_url_reads_dev_note_only_on_a_miss(monkeypatch):
    """The seam the FakeStore cannot reach, for AC-1's three resolution cases:

    1. a task carrying only `content.pr_url` resolves with dev.task GETs only -- no dev.note
       request at all.
    2. a task resolvable only through an attachment note's `content.url` resolves; the dev.note
       read is issued only after the pr_refs/pr_url miss.
    3. a task whose only note is a *comment* carrying the url in its body text (not `content.
       url`) does not match -- and, since dev.note is already cached from case 2, costs no
       further request.

    Also pins that no request this method issues ever carries `parent_id` -- the per-task notes
    read B19 removes.
    """
    tasks = [
        {"id": "task-pr-url", "content": {"pr_url": "https://github.test/pr/1"}},
        {"id": "task-attach", "content": {}},
        {"id": "task-comment", "content": {}},
    ]
    notes = [
        {
            "id": "note-1",
            "parent_id": "task-attach",
            "content": {
                "kind": "attachment",
                "url": "https://github.test/pr/2",
                "body": "Pull request opened for review: https://github.test/pr/2",
            },
        },
        {
            "id": "note-2",
            "parent_id": "task-comment",
            "content": {"kind": "comment", "body": "see https://github.test/pr/3"},
        },
    ]
    populations = {"task": tasks, "note": notes}
    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        params = kwargs.get("params") or {}
        assert method == "GET"
        assert "parent_id" not in params
        requests.append(dict(params))
        population = populations[params["type"]]
        offset = params.get("offset", 0)
        page = population[offset : offset + params["limit"]]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return page

        return _Resp()

    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(ca.httpx, "request", fake_request)

    store = ca.SubstrateChangeApplyStore()

    # Case 1: resolves through content.pr_url -- dev.task GETs only.
    result1 = store.find_task_by_pr_url("https://github.test/pr/1")
    assert result1["id"] == "task-pr-url"
    assert all(r["type"] != "note" for r in requests)
    assert any(r["type"] == "task" for r in requests)

    # Case 2: resolves only through the attachment note -- the dev.note read fires now, after
    # both faster passes miss (dev.task is already cached, so no new task GET).
    task_requests_before_case2 = sum(1 for r in requests if r["type"] == "task")
    note_requests_before_case2 = sum(1 for r in requests if r["type"] == "note")
    result2 = store.find_task_by_pr_url("https://github.test/pr/2")
    assert result2["id"] == "task-attach"
    assert sum(1 for r in requests if r["type"] == "task") == task_requests_before_case2
    assert sum(1 for r in requests if r["type"] == "note") > note_requests_before_case2

    # Case 3: a comment note's body text is never the key -- no match -- and the dev.note
    # population is already cached, so this "second miss" issues no further dev.note request.
    requests_before_case3 = len(requests)
    result3 = store.find_task_by_pr_url("https://github.test/pr/3")
    assert result3 is None
    assert len(requests) == requests_before_case3

    assert all("parent_id" not in r for r in requests)


def test_default_store_hands_back_a_fresh_cache_each_time(monkeypatch):
    """The cache is per store instance, and correctness depends on it.

    ``default_store()`` is called once per activity invocation, so the snapshot
    dies with the sweep. If it ever became memoized -- an lru_cache, a module
    singleton, a use_store-style override -- the snapshot would outlive the
    sweep, a dev.task arriving between two ticks would never be seen, and its
    ``delivers`` edge would be lost permanently with the suite still green.
    Pin the property the correctness argument rests on.
    """
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")

    first, second = ca.default_store(), ca.default_store()

    assert first is not second
    assert first._dev_beads is not second._dev_beads
    assert first._dev_beads.fetch_calls == {}
