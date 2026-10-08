"""Activities for the knowledge ingestion workflow (F-DCE-4).

Turns a registered ``knowledge_sources.yaml`` entry into reviewed doctrine, in
two idempotent passes:

1. :func:`file_knowledge_extraction_tasks` files one extraction ``dev.task``
   per unfiled source, via the same filing machinery (``file_task.py``)
   every hand-filed task goes through.
2. :func:`land_knowledge_principles` observes PR state the same way
   ``dispatch.py``'s own reconciliation does, and once an extraction task's
   PR has merged, lands its candidates as ``proposed`` ``arch.principle``
   beads with a ``derived_from`` link back to the extraction task.

Temporal holds run-state only (Pillar 10): both activities read/write the
repo and the substrate directly and return ids, counts, and short structural
reasons — never takeaway or candidate text. See ``.factory/design.md`` for
the substrate gap this design works around (``derived_from`` is not yet in
the live link vocabulary) and why the extraction task, not ``arch.incident``,
is the link's target.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

import dispatch  # noqa: E402
import file_task  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

REGISTRY_PATH = _DISPATCHER_ROOT / "knowledge_sources.yaml"
CREATED_BY = "factory-dispatcher/knowledge-ingestion"
HTTP_TIMEOUT_S = 30.0

SOURCE_KINDS = ("book-takeaways", "audit", "incident-record", "external-doc")
SOURCE_CONTEXT_PREFIX = "knowledge-source:"
LANDED_NOTE_PREFIX = "knowledge-ingestion:landed:"
REJECTED_NOTE_PREFIX = "knowledge-ingestion:rejected:"
REQUIRED_CANDIDATE_FIELDS = ("statement", "source", "rationale")

DEFAULT_BUDGET = {"max_agent_minutes": 45, "max_usd": 3.0, "max_tokens": 300000}
REQUIREMENT_WAIVER = (
    "No LifeOps requirement governs the factory's own knowledge store; "
    "doctrine work is governed by PRIN-007 (humans enter through the review "
    "buffer) and PRIN-009 (feedback artifacts steer design work), per the "
    "S23 waiver precedent "
    "(docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md)."
)


class CandidatesUnavailable(RuntimeError):
    """The candidates file a merged extraction task should have written is
    missing or is not a non-empty JSON array."""


@dataclass(frozen=True)
class KnowledgeSource:
    id: str
    kind: str
    reference: str
    title: str
    summary: str

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise ValueError(
                f"knowledge source {self.id!r} has unknown kind {self.kind!r}; "
                f"must be one of {', '.join(SOURCE_KINDS)}"
            )

    @property
    def context_ref(self) -> str:
        return f"{SOURCE_CONTEXT_PREFIX}{self.id}"

    @property
    def takeaways_path(self) -> str:
        return f"docs/reference/{self.id}-takeaways.md"

    @property
    def candidates_path(self) -> str:
        return f"docs/reference/knowledge-candidates/{self.id}.json"


def load_registered_sources(path: Path = REGISTRY_PATH) -> list[KnowledgeSource]:
    """Parse ``knowledge_sources.yaml``.

    PyYAML is imported here, not at module scope, so importing this module
    for the landing half of the workflow does not require it installed —
    the same reasoning as ``scanner.py``'s suppression loader.
    """
    import yaml  # local import: see docstring

    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("sources") or []
    return [
        KnowledgeSource(
            id=str(entry["id"]),
            kind=str(entry["kind"]),
            reference=str(entry["reference"]),
            title=str(entry.get("title") or entry["id"]),
            summary=str(entry.get("summary") or ""),
        )
        for entry in entries
    ]


# -- the narrow store this workflow writes through ---------------------------


class KnowledgeStore(Protocol):
    def list_tasks(self) -> list[dict[str, Any]]: ...

    def file_task(self, content: dict[str, Any]) -> dict[str, Any]: ...

    def list_notes(self, parent_id: str) -> list[dict[str, Any]]: ...

    def add_note(self, parent_id: str, kind: str, body: str, created_by: str) -> dict[str, Any]: ...

    def create_principle(self, content: dict[str, Any]) -> dict[str, Any]: ...

    def create_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict[str, Any]: ...


class SubstrateKnowledgeStore:
    """Narrow client for the bead writes this workflow owns.

    Deliberately not a widening of ``substrate.Substrate`` (the dispatcher's
    six-call ``BeadStore``): that class has one tested consumer today, and a
    fat addition here would be a second place for the schema to drift. Same
    reasoning as ``staleness_report.SubstrateObservationStore``.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        import httpx

        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/beads", params={"namespace": "dev", "type": "task", "limit": 200}
        )

    def file_task(self, content: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "task",
                "state": "pending",
                "trust_tier": "user",
                "created_by": CREATED_BY,
                "content": content,
            },
        )

    def list_notes(self, parent_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/beads",
            params={
                "namespace": "dev",
                "type": "note",
                "parent_id": parent_id,
                "limit": 500,
            },
        )

    def add_note(self, parent_id: str, kind: str, body: str, created_by: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "note",
                "state": "active",
                "trust_tier": "system",
                "parent_id": parent_id,
                "created_by": created_by,
                "content": {"kind": kind, "body": body},
            },
        )

    def create_principle(self, content: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "arch",
                "type": "principle",
                "state": "active",
                "trust_tier": "system",
                "created_by": CREATED_BY,
                "content": content,
            },
        )

    def create_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict[str, Any]:
        """POST /beads/{source_id}/links. ``created_by`` travels as the
        ``X-Created-By`` header, per the route's contract — unlike every
        other write here, this endpoint does not read it from the JSON
        body (same shape as ``substrate.Substrate.add_link``)."""
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            headers={"X-Created-By": created_by},
            json={"target_id": target_id, "link_type": link_type},
        )


def default_store() -> KnowledgeStore:
    return SubstrateKnowledgeStore()


# -- phase 1: file one extraction task per unfiled source --------------------


def _task_with_context_ref(tasks: list[dict[str, Any]], context_ref: str) -> dict[str, Any] | None:
    for task in tasks:
        refs = (task.get("content") or {}).get("context_refs") or []
        if context_ref in refs:
            return task
    return None


def extraction_task_spec(source: KnowledgeSource) -> dict[str, Any]:
    """The ``file_task.py`` spec for one source's extraction task."""
    return {
        "lane": "code-health",
        "title": f"Extract doctrine candidates from {source.title}",
        "intent": (
            f"Read {source.reference} ({source.kind}) and produce reviewed "
            "takeaways plus structured arch.principle candidates -- the same "
            "shape as the Design Rules V2 pass done by hand "
            "(docs/reference/design-rules-v2-takeaways.md), for this one "
            "source. This proposes candidates for the Product Owner to "
            "review; it never writes doctrine directly."
        ),
        "context_refs": [source.context_ref, source.reference],
        "acceptance": [
            "WHEN the source is read, THE worker SHALL write one takeaway "
            f"per notable point to {source.takeaways_path}",
            "WHEN takeaways are written, THE worker SHALL write a JSON "
            f"array to {source.candidates_path}, each entry carrying "
            "non-blank 'statement', 'source', and 'rationale' string fields",
            "THE candidates SHALL be proposals only -- this task SHALL NOT "
            "edit docs/architecture/principles.md or create any "
            "arch.principle bead",
        ],
        "verification": {
            "commands": [
                f"test -f {source.takeaways_path}",
                (
                    "python3 -c \"import json; d = json.load(open("
                    f"'{source.candidates_path}')); "
                    "assert isinstance(d, list) and d; "
                    "assert all({'statement', 'source', 'rationale'} <= "
                    "set(c) and all(str(c[k]).strip() for k in "
                    "('statement', 'source', 'rationale')) for c in d)\""
                ),
            ],
        },
        "scope": {"paths": ["docs/reference/"]},
        "risk_class": "structural",
        "budget": DEFAULT_BUDGET,
        "requirement_refs_waived": REQUIREMENT_WAIVER,
    }


def file_knowledge_extraction_tasks(
    store: KnowledgeStore,
    *,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    sources = load_registered_sources(registry_path)
    tasks = store.list_tasks()

    filed: list[dict[str, Any]] = []
    already_filed: list[str] = []
    for source in sources:
        if _task_with_context_ref(tasks, source.context_ref) is not None:
            already_filed.append(source.id)
            continue
        content = file_task.build_content(extraction_task_spec(source))
        bead = store.file_task(content)
        filed.append({"source_id": source.id, "task_id": bead["id"]})

    return {"status": "filed", "filed": filed, "already_filed": already_filed}


@activity.defn(name="file_knowledge_extraction_tasks")
def file_knowledge_extraction_tasks_activity(request: dict[str, Any]) -> dict[str, Any]:
    request = request or {}
    return file_knowledge_extraction_tasks(
        default_store(),
        registry_path=Path(request.get("registry_path") or REGISTRY_PATH),
    )


# -- phase 2: land merged candidates as proposed principles ------------------


def _pr_is_merged_on_main(
    task: dict[str, Any],
    cfg: "dispatch.Config",
    *,
    lookup_pr: Any,
    is_ancestor: Any,
) -> bool:
    state = task.get("state")
    if state == "done":
        # dispatch.reconcile_review_tasks only transitions review -> done
        # after confirming pr.merged and merge_commit is an ancestor of
        # cfg.base_ref, so this state already proves both.
        return True
    if state != "review":
        return False
    pr_url = str((task.get("content") or {}).get("pr_url") or "").strip()
    if not pr_url:
        return False
    pr = lookup_pr(pr_url, cfg)
    if not pr.merged or not pr.merge_commit:
        return False
    return bool(is_ancestor(pr.merge_commit, cfg))


def _landing_marker(notes: list[dict[str, Any]]) -> str | None:
    for note in notes:
        body = str((note.get("content") or {}).get("body") or "")
        if body.startswith(LANDED_NOTE_PREFIX) or body.startswith(REJECTED_NOTE_PREFIX):
            return body
    return None


def _load_candidates(repo_root: Path, source: KnowledgeSource) -> list[dict[str, Any]]:
    path = repo_root / source.candidates_path
    if not path.exists():
        raise CandidatesUnavailable(
            f"{source.candidates_path} does not exist at {repo_root}"
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CandidatesUnavailable(
            f"{source.candidates_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, list) or not data:
        raise CandidatesUnavailable(
            f"{source.candidates_path} is not a non-empty JSON array"
        )
    return data


def _malformed_candidate_fields(candidate: Any) -> list[str]:
    if not isinstance(candidate, dict):
        return list(REQUIRED_CANDIDATE_FIELDS)
    return [
        field
        for field in REQUIRED_CANDIDATE_FIELDS
        if not str(candidate.get(field) or "").strip()
    ]


def _principle_content(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement": str(candidate["statement"]).strip(),
        "rationale": str(candidate["rationale"]).strip(),
        "source": str(candidate["source"]).strip(),
        "status": "proposed",
        "status_history": [],
        # "derived", not "authored" or "observed": this mechanically mirrors
        # a candidate array a prior, separately-reviewed and merged
        # extraction task wrote to source.candidates_path -- no synthesis
        # happens here -- the same pattern as requirements-load's mirror of
        # docs/requirements/*.json. See .factory/design.md.
        "source_class": "derived",
    }


def land_knowledge_principles(
    store: KnowledgeStore,
    cfg: "dispatch.Config",
    *,
    registry_path: Path = REGISTRY_PATH,
    repo_root: Path | None = None,
    lookup_pr: Any = dispatch.lookup_pull_request,
    is_ancestor: Any = dispatch.merge_commit_is_ancestor_of_main,
) -> dict[str, Any]:
    repo_root = repo_root if repo_root is not None else cfg.repo_root
    sources = load_registered_sources(registry_path)
    tasks = store.list_tasks()

    landed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    already_landed: list[str] = []
    not_ready: list[str] = []

    for source in sources:
        task = _task_with_context_ref(tasks, source.context_ref)
        if task is None:
            not_ready.append(source.id)
            continue
        task_id = task["id"]

        if _landing_marker(store.list_notes(task_id)) is not None:
            already_landed.append(source.id)
            continue

        if not _pr_is_merged_on_main(task, cfg, lookup_pr=lookup_pr, is_ancestor=is_ancestor):
            not_ready.append(source.id)
            continue

        try:
            candidates = _load_candidates(repo_root, source)
        except CandidatesUnavailable as exc:
            reason = f"{REJECTED_NOTE_PREFIX}{source.id}: {exc}"
            store.add_note(task_id, "status", reason, CREATED_BY)
            rejected.append({"source_id": source.id, "task_id": task_id, "reason": str(exc)})
            continue

        problems = {
            index: missing
            for index, candidate in enumerate(candidates)
            if (missing := _malformed_candidate_fields(candidate))
        }
        if problems:
            reason = f"{REJECTED_NOTE_PREFIX}{source.id}: " + "; ".join(
                f"candidate[{index}] missing {', '.join(fields)}"
                for index, fields in sorted(problems.items())
            )
            store.add_note(task_id, "status", reason, CREATED_BY)
            rejected.append({"source_id": source.id, "task_id": task_id, "reason": reason})
            continue

        principle_ids: list[str] = []
        for candidate in candidates:
            principle = store.create_principle(_principle_content(candidate))
            store.create_link(
                principle["id"], task_id, "derived_from", created_by=CREATED_BY
            )
            principle_ids.append(principle["id"])

        store.add_note(
            task_id,
            "status",
            f"{LANDED_NOTE_PREFIX}{source.id}: landed {len(principle_ids)} "
            "proposed arch.principle bead(s) with derived_from provenance.",
            CREATED_BY,
        )
        landed.append(
            {"source_id": source.id, "task_id": task_id, "principle_ids": principle_ids}
        )

    return {
        "status": "landed",
        "landed": landed,
        "rejected": rejected,
        "already_landed": already_landed,
        "not_ready": not_ready,
    }


@activity.defn(name="land_knowledge_principles")
def land_knowledge_principles_activity(request: dict[str, Any]) -> dict[str, Any]:
    request = request or {}
    return land_knowledge_principles(
        default_store(),
        dispatch.Config.from_env(),
        registry_path=Path(request.get("registry_path") or REGISTRY_PATH),
    )


ACTIVITIES = [
    file_knowledge_extraction_tasks_activity,
    land_knowledge_principles_activity,
]
