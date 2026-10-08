"""The BeadStore contract — a one-store contract suite for ``Substrate``.

This file is the suite PC-SUB-004/PC-SUB-003 ask for. The assertion FUNCTIONS
below (the ``_assert_*`` helpers) are the contract, stated once, each called
against ``Substrate`` bound to ``FakeSubstrateBackend`` (an in-memory double
that enforces the same rules the live substrate enforces server-side, sourced
from dev_task_contract.py).

Until 2026-09, this suite ran every assertion a second time against
``BdStore`` bound to a scripted subprocess double, asserting by name where
bd 1.1.2 could not agree with Substrate (no create contract, no CAS beyond
pending -> doing). Amendment 33 rescinded the bd migration and Amendment 35
(the bd/Dolt comparison sprint that suite existed to cost) was withdrawn on
2026-09-07 — the bd half moved to
``docs/archive/2026-09-bd-dolt-evaluation/`` along with ``bdstore.py`` itself;
see that folder's README for the full record.

No test in this file spawns a real substrate; every case here is expressible
against a double.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_task_contract import (  # noqa: E402
    BEAD_LINK_TYPES,
    DEV_TASK_ENTRY_STATES,
    DEV_TASK_STATE_MACHINE,
)
from substrate import Substrate, SubstrateError  # noqa: E402

# apps/substrate/src is already on sys.path by the time the import above
# finishes -- substrate.py imports beadstore, and beadstore.py:71 puts it
# there itself. Imported here as plain modules, the same way beadstore.py
# does, rather than re-describing validate_bead_content /
# check_source_class_admission / SOURCE_CLASS_WRITERS by hand.
import bead_rules  # noqa: E402
import schemas  # noqa: E402


# ---------------------------------------------------------------------------
# Substrate side: a fake HTTP backend enforcing the rules the live substrate
# enforces server-side. Substrate itself (substrate.py) is a thin client — it
# does not validate anything — so this class is the "other half" a real
# deployment supplies, standing in for it without FastAPI/SQLAlchemy/a
# database.
# ---------------------------------------------------------------------------


class FakeSubstrateBackend:
    """Enough of the live substrate to hold Substrate to the dev.task contract.

    Routes on (method, path) exactly as substrate.py's ``_request`` calls
    them, and enforces DEV_TASK_STATE_MACHINE / DEV_TASK_ENTRY_STATES /
    BEAD_LINK_TYPES from dev_task_contract.py — the same rules
    apps/substrate/src/routes.py and schemas.py enforce, reachable here
    without importing either.
    """

    def __init__(self):
        self._beads: dict[str, dict] = {}
        self._links: list[tuple[str, str, str]] = []
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def __call__(self, method: str, path: str, headers=None, **kwargs):
        if method == "POST" and path == "/beads":
            return self._create(kwargs.get("json") or {})
        if method == "GET" and path == "/beads":
            return self._list(kwargs.get("params") or {})
        if method == "PATCH" and path.startswith("/beads/"):
            return self._patch(path[len("/beads/") :], kwargs.get("json") or {})
        if method == "POST" and path.endswith("/transition"):
            bead_id = path[len("/beads/") : -len("/transition")]
            return self._transition(bead_id, kwargs.get("json") or {})
        if method == "GET" and path.endswith("/links"):
            bead_id = path[len("/beads/") : -len("/links")]
            return self._list_links(bead_id, kwargs.get("params") or {})
        if method == "GET" and path.endswith("/events"):
            bead_id = path[len("/beads/") : -len("/events")]
            return self._list_events(bead_id)
        if method == "POST" and path.endswith("/links"):
            bead_id = path[len("/beads/") : -len("/links")]
            return self._add_link(bead_id, kwargs.get("json") or {}, headers or {})
        raise AssertionError(f"FakeSubstrateBackend has no route for {method} {path}")

    def _create(self, payload: dict) -> dict:
        namespace, type_, state = payload["namespace"], payload["type"], payload["state"]
        if (namespace, type_) == ("dev", "task") and state not in DEV_TASK_ENTRY_STATES:
            raise SubstrateError(
                422, f"illegal_entry_state: dev.task may not be created in {state!r}"
            )
        content = payload.get("content") or {}
        if namespace == "arch":
            self._validate_arch_content_or_422(type_, content)
            self._reject_admission_violation_or_409(content, payload.get("created_by"))
        bead_id = self._new_id("bead")
        bead = {
            "id": bead_id,
            "namespace": namespace,
            "type": type_,
            "state": state,
            "trust_tier": payload.get("trust_tier"),
            "created_by": payload.get("created_by"),
            "parent_id": payload.get("parent_id"),
            "content": content,
            "context": payload.get("context") or {},
            "provenance": payload.get("provenance"),
        }
        self._beads[bead_id] = bead
        return dict(bead)

    @staticmethod
    def _validate_arch_content_or_422(type_: str, content: dict) -> None:
        """Mirrors routes.py's ``_validate_content_or_422`` (:271-281, called
        for every create/content-PATCH at :486/:621): the same
        ``validate_bead_content`` the live substrate runs, raising the same
        422 on a schema refusal."""
        try:
            schemas.validate_bead_content("arch", type_, content)
        except ValidationError as exc:
            raise SubstrateError(422, f"arch_content_schema_violation: {exc}") from exc

    @staticmethod
    def _reject_admission_violation_or_409(content: dict, writer: str) -> None:
        """Mirrors routes.py's ``_reject_source_class_admission_violation_or_409``
        (:313-340, run at :515 for a fresh mint): the writer must be
        enrolled in ``bead_rules.SOURCE_CLASS_WRITERS`` for the class this
        content declares."""
        declared_class = schemas.read_source_class(content)
        violation = schemas.check_source_class_admission(declared_class, writer)
        if violation is not None:
            raise SubstrateError(
                409,
                f"source_class_admission_violation: declared_class={violation.owning_class!r} "
                f"rejected_writer={violation.rejected_writer!r}",
            )

    def _list(self, params: dict) -> list[dict]:
        namespace, type_ = params.get("namespace"), params.get("type")
        state, parent_id = params.get("state"), params.get("parent_id")
        content_ref, limit = params.get("content_ref"), params.get("limit")
        results = [
            b
            for b in self._beads.values()
            if (namespace is None or b["namespace"] == namespace)
            and (type_ is None or b["type"] == type_)
            and (state is None or b["state"] == state)
            and (parent_id is None or b.get("parent_id") == parent_id)
            and (content_ref is None or (b["content"] or {}).get("ref") == content_ref)
        ]
        if limit is not None:
            results = results[: int(limit)]
        return [dict(b) for b in results]

    def _patch(self, bead_id: str, payload: dict) -> dict:
        bead = self._beads.get(bead_id)
        if bead is None:
            # Mirrors routes.py's update_bead (:600, "Bead not found"): PATCH
            # has an existence check GET /beads/{bead_id}/events does not.
            raise SubstrateError(404, f"bead not found: {bead_id}")
        if "state" in payload:
            bead["state"] = payload["state"]
        if "content" in payload:
            if bead["namespace"] == "arch":
                self._validate_arch_content_or_422(bead["type"], payload["content"])
            bead["content"] = payload["content"]
        if "context" in payload:
            # A context-only PATCH validates nothing -- routes.py's
            # update_bead runs no check on update.context, ever.
            bead["context"] = payload["context"]
        return dict(bead)

    def _transition(self, bead_id: str, payload: dict) -> dict:
        bead = self._beads[bead_id]
        from_state, to_state = payload["from_state"], payload["to_state"]
        if bead["state"] != from_state:
            raise SubstrateError(409, f"conflict: bead is {bead['state']}, not {from_state}")
        if (bead["namespace"], bead["type"]) == ("dev", "task"):
            allowed = DEV_TASK_STATE_MACHINE.get(from_state, frozenset())
            if to_state not in allowed:
                raise SubstrateError(
                    422, f"illegal_transition: {from_state} -> {to_state} not in machine"
                )
        bead["state"] = to_state
        return dict(bead)

    def _list_links(self, bead_id: str, params: dict) -> list[dict]:
        link_type = params.get("link_type")
        return [
            {"source_id": s, "target_id": t, "link_type": lt}
            for (s, t, lt) in self._links
            if (s == bead_id or t == bead_id) and (link_type is None or lt == link_type)
        ]

    def _list_events(self, bead_id: str) -> list[dict]:
        """Mirrors the live route's UUID4 path-parameter validation
        (apps/substrate/src/routes.py:973-993): a malformed id never reaches
        the lookup and 422s; a well-formed but unknown id has no existence
        check and answers [] — this fake tracks no events at all, so every
        well-formed id (known bead or not) answers [] the same way."""
        try:
            uuid.UUID(bead_id)
        except ValueError:
            raise SubstrateError(422, f"bead_id is not a valid UUID: {bead_id!r}")
        return []

    def _add_link(self, source_id: str, payload: dict, headers: dict) -> dict:
        target_id, link_type = payload["target_id"], payload["link_type"]
        if link_type not in BEAD_LINK_TYPES:
            raise SubstrateError(422, f"link_type must be one of: {sorted(BEAD_LINK_TYPES)}")
        key = (source_id, target_id, link_type)
        if key in self._links:
            raise SubstrateError(409, "duplicate_link")
        self._links.append(key)
        return {
            "id": self._new_id("link"),
            "source_id": source_id,
            "target_id": target_id,
            "link_type": link_type,
        }


@pytest.fixture
def substrate_store(monkeypatch) -> Substrate:
    backend = FakeSubstrateBackend()
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(
        Substrate,
        "_request",
        lambda self, method, path, headers=None, **kwargs: backend(
            method, path, headers=headers, **kwargs
        ),
    )
    return Substrate()


def _create(store: Substrate, extra_content: dict | None = None) -> str:
    content = {"title": "t", "intent": "i", "acceptance": ["a"]}
    content.update(extra_content or {})
    return store.create_task(content, "factory-agent")["id"]


def _create_release(store: Substrate, *, state: str = "in_flight", ref: str = "R26.01") -> str:
    """``arch.release`` beads have no entry-state restriction and no BeadStore
    intake method of their own — the release-state gate only ever reads them
    (``list_beads``) — so this posts directly through the same fake backend
    ``create_task`` uses, the way ``test_create_task_refuses_an_illegal_entry_state``
    already pokes the backend directly for a case outside what BeadStore exposes.

    Content is a minimal but schema-VALID ``ArchReleaseContent`` (name,
    objective, a single outcome, opened_at) — since OPS-190,
    ``FakeSubstrateBackend`` validates arch content through the substrate's
    own schema (see ``_validate_arch_content_or_422``), so a bare
    ``{"ref": ref}`` no longer files."""
    return store._request(
        "POST",
        "/beads",
        json={
            "namespace": "arch",
            "type": "release",
            "state": state,
            "created_by": "factory-agent",
            "content": {
                "ref": ref,
                "name": "test release",
                "objective": "test objective",
                "outcomes": [
                    {"id": "O-1", "statement": "test outcome", "work_class": "feature"}
                ],
                "opened_at": "2026-01-01",
            },
        },
    )["id"]


# ---------------------------------------------------------------------------
# The contract, stated once per capability.
# ---------------------------------------------------------------------------


def _assert_transition_state_is_a_true_cas(store, bead_id, from_state, to_state):
    """The dispatcher's core safety property (PR #191): a rejected claim MUST
    raise, never no-op or clobber — see beadstore.BeadStore's class docstring.
    """
    first = store.transition_state(bead_id, from_state, to_state, "factory-agent")
    assert first["state"] == to_state
    with pytest.raises(Exception):
        store.transition_state(bead_id, from_state, to_state, "factory-agent")


def test_transition_state_is_a_true_cas_on_substrate(substrate_store):
    bead_id = _create(substrate_store)
    _assert_transition_state_is_a_true_cas(substrate_store, bead_id, "pending", "doing")


def _assert_patch_content_replaces_wholesale(store, bead_id):
    new_content = {"title": "new", "intent": "new intent", "acceptance": ["x"]}
    result = store.patch_content(bead_id, new_content, "factory-agent")
    assert result["content"] == new_content
    assert "old_only_key" not in result["content"]


def test_patch_content_replaces_wholesale_on_substrate(substrate_store):
    bead_id = _create(substrate_store, {"old_only_key": "keep-me-out"})
    _assert_patch_content_replaces_wholesale(substrate_store, bead_id)


def _assert_add_note_then_list_notes_round_trips(store, bead_id):
    store.add_note(bead_id, "status", "claimed", "factory-agent")
    notes = store.list_notes(bead_id)
    assert notes, "add_note then list_notes returned nothing"
    assert notes[-1]["content"]["kind"] == "status"
    assert notes[-1]["content"]["body"] == "claimed"


def test_add_note_then_list_notes_round_trips_on_substrate(substrate_store):
    bead_id = _create(substrate_store)
    _assert_add_note_then_list_notes_round_trips(substrate_store, bead_id)


def _assert_list_tasks_filters_by_state(store, state, expected_ids):
    tasks = store.list_tasks(state=state)
    assert {t["id"] for t in tasks} == set(expected_ids)


def test_list_tasks_filters_by_state_on_substrate(substrate_store):
    pending_id = _create(substrate_store)
    doing_id = _create(substrate_store)
    substrate_store.transition_state(doing_id, "pending", "doing", "factory-agent")

    _assert_list_tasks_filters_by_state(substrate_store, "pending", [pending_id])
    _assert_list_tasks_filters_by_state(substrate_store, "doing", [doing_id])


def _assert_set_state_writes_unconditionally(store, bead_id, state):
    """``set_state`` has no compare-and-set, unlike ``transition_state``
    above — the dispatcher only reaches for it on moves it already owns
    (beadstore.BeadStore's class docstring)."""
    result = store.set_state(bead_id, state, "factory-agent")
    assert result["state"] == state


def test_set_state_writes_unconditionally_on_substrate(substrate_store):
    bead_id = _create(substrate_store)
    _assert_set_state_writes_unconditionally(substrate_store, bead_id, "failed")


# ---------------------------------------------------------------------------
# list_beads — the release-state gate's read of ``("arch", "release")``
# (dispatch.py's resolve_task_release_states / list_open_release_refs). On
# the protocol since the release-gate review of #635, but never once called
# from this suite until now: the exact half-covered seam PC-SUB-004/AC-2
# exists to close, now pinned by name rather than left to the next reviewer
# to notice.
# ---------------------------------------------------------------------------


def _assert_list_beads_filters_by_namespace_and_type(store, namespace, type_, expected_ids):
    beads = store.list_beads(namespace, type_)
    assert {b["id"] for b in beads} == set(expected_ids)


def test_list_beads_filters_by_namespace_and_type_on_substrate(substrate_store):
    release_id = _create_release(substrate_store)
    _create(substrate_store)  # a dev.task bead — must not show up under arch/release

    _assert_list_beads_filters_by_namespace_and_type(
        substrate_store, "arch", "release", [release_id]
    )


# ---------------------------------------------------------------------------
# list_events — the predecessor of the waiting-queue-age bead (72067fdb): a
# caller has no call site yet, but the double contract's two arms (AC-2)
# still need to hold: [] for an unknown-but-well-formed id, SubstrateError
# with a 422 status for a malformed one — never a raise for "unknown".
# ---------------------------------------------------------------------------


def test_list_events_returns_empty_for_an_unknown_well_formed_id_on_substrate(substrate_store):
    assert substrate_store.list_events(str(uuid.uuid4())) == []


def test_list_events_raises_422_for_a_malformed_id_on_substrate(substrate_store):
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.list_events("not-a-uuid")
    assert excinfo.value.status == 422


# ---------------------------------------------------------------------------
# The intake path — file_task.py's create_task through BeadStore, expressible
# only against Substrate. bd's own divergence here
# (test_create_task_is_explicitly_unsupported_on_bd) archived with the rest
# of bdstore.py's tests: docs/archive/2026-09-bd-dolt-evaluation/.
# ---------------------------------------------------------------------------


def test_create_task_enters_at_pending_and_nowhere_else(substrate_store):
    bead = substrate_store.create_task(
        {"title": "t", "intent": "i", "acceptance": ["a"]}, "factory-agent"
    )
    assert bead["namespace"] == "dev"
    assert bead["type"] == "task"
    assert bead["state"] == "pending"


def test_create_task_refuses_an_illegal_entry_state():
    """FakeSubstrateBackend enforces DEV_TASK_ENTRY_STATES the way the live
    substrate's _validate_entry_state_or_422 does — a caller cannot mint a
    dev.task bead directly into e.g. "done"."""
    backend = FakeSubstrateBackend()
    with pytest.raises(SubstrateError):
        backend("POST", "/beads", json={"namespace": "dev", "type": "task", "state": "done"})


def test_intake_then_drain_round_trips_through_one_seam(substrate_store):
    """The seam this task exists to prove is now reachable end to end: file,
    claim, patch — all through BeadStore, none of it a raw POST."""
    bead_id = _create(substrate_store)
    substrate_store.transition_state(bead_id, "pending", "doing", "factory-agent")
    result = substrate_store.patch_content(
        bead_id, {"title": "revised", "intent": "i", "acceptance": ["a"]}, "factory-agent"
    )
    assert result["content"]["title"] == "revised"


# ---------------------------------------------------------------------------
# The closed edge vocabulary and bead-graph primitives — the other half of
# "unreachable to any caller that is not the web app."
# ---------------------------------------------------------------------------


def test_add_link_enforces_the_closed_vocabulary_on_substrate(substrate_store):
    task_id = _create(substrate_store)
    principle_id = "principle-1"
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.add_link(task_id, principle_id, "reelizes-typo", "factory-agent")
    assert excinfo.value.status == 422


def test_add_link_rejects_a_duplicate_edge_on_substrate(substrate_store):
    task_id = _create(substrate_store)
    substrate_store.add_link(task_id, "principle-1", "applies", "factory-agent")
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.add_link(task_id, "principle-1", "applies", "factory-agent")
    assert excinfo.value.status == 409


def test_add_link_accepts_every_type_in_the_seventeen_type_vocabulary(substrate_store):
    task_id = _create(substrate_store)
    for i, link_type in enumerate(sorted(BEAD_LINK_TYPES)):
        substrate_store.add_link(task_id, f"target-{i}", link_type, "factory-agent")


def test_find_bead_returns_the_one_bead_matching_content_ref_on_substrate(substrate_store):
    release_id = _create_release(substrate_store, ref="R26.01")
    _create_release(substrate_store, ref="R26.02")

    found = substrate_store.find_bead("arch", "release", "R26.01")
    assert found["id"] == release_id


def test_list_links_returns_the_links_touching_a_bead_on_substrate(substrate_store):
    task_id = _create(substrate_store)
    substrate_store.add_link(task_id, "principle-1", "applies", "factory-agent")

    links = substrate_store.list_links(task_id)
    assert links == [{"source_id": task_id, "target_id": "principle-1", "link_type": "applies"}]


# ---------------------------------------------------------------------------
# create_bead / patch_context (OPS-190) — the refusing double AC-3 asks for.
# FakeSubstrateBackend's POST /beads and PATCH /beads/{id} arms now validate
# arch content through schemas.validate_bead_content and run the same
# writer-admission check the live route runs (routes.py :486/:511-515/:621),
# so the double refuses what the store refuses rather than filing anything a
# caller hands it.
# ---------------------------------------------------------------------------

_OBSERVED_WRITER = next(iter(bead_rules.SOURCE_CLASS_WRITERS["observed"]))
_UNENROLLED_WRITER = "factory-dispatcher/not-enrolled-for-anything"


def _observation_content(**overrides: object) -> dict:
    content = {
        "ref": "wrd/worker",
        "observed_at": "2026-09-23T00:00:00Z",
        "workload": {
            "cluster": "prod",
            "namespace": "factory",
            "kind": "Process",
            "name": "worker",
        },
        "source_class": "observed",
    }
    content.update(overrides)
    return content


def test_create_bead_files_an_arch_observation_and_context_round_trips_on_substrate(
    substrate_store,
):
    substrate_store.create_bead(
        "arch",
        "observation",
        "active",
        _observation_content(),
        _OBSERVED_WRITER,
        context={"condition": "clear"},
    )

    found = substrate_store.find_bead("arch", "observation", "wrd/worker")
    assert found is not None
    assert found["context"] == {"condition": "clear"}


def test_create_bead_with_provenance_round_trips_on_substrate(substrate_store):
    provenance = {
        "worker": "codex",
        "model": "codex-cli",
        "prompt_ref": "dev.task/task-1",
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 1.2,
    }
    substrate_store.create_bead(
        "arch", "observation", "active", _observation_content(), _OBSERVED_WRITER,
        provenance=provenance,
    )

    found = substrate_store.find_bead("arch", "observation", "wrd/worker")
    assert found["provenance"] == provenance


def test_create_bead_by_an_unenrolled_observed_writer_raises_409_and_files_nothing_on_substrate(
    substrate_store,
):
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.create_bead(
            "arch", "observation", "active", _observation_content(), _UNENROLLED_WRITER
        )
    assert excinfo.value.status == 409
    assert substrate_store.list_beads("arch", "observation") == []


@pytest.mark.parametrize(
    "content",
    [
        _observation_content(unexpected_extra_key="not in the schema"),
        {k: v for k, v in _observation_content().items() if k != "observed_at"},
        {k: v for k, v in _observation_content().items() if k != "workload"},
    ],
    ids=["extra_key", "missing_observed_at", "missing_workload"],
)
def test_create_bead_with_invalid_arch_content_raises_422_and_files_nothing_on_substrate(
    substrate_store, content
):
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.create_bead(
            "arch", "observation", "active", content, _OBSERVED_WRITER
        )
    assert excinfo.value.status == 422
    assert substrate_store.list_beads("arch", "observation") == []


def test_patch_context_replaces_context_and_leaves_content_byte_identical_on_substrate(
    substrate_store,
):
    content = {"title": "t", "intent": "i", "acceptance": ["a"]}
    bead_id = substrate_store.create_task(content, "factory-agent")["id"]

    result = substrate_store.patch_context(bead_id, {"condition": "clear"}, "factory-agent")

    assert result["context"] == {"condition": "clear"}
    assert result["content"] == content


def test_patch_context_on_an_unknown_id_raises_404_on_substrate(substrate_store):
    with pytest.raises(SubstrateError) as excinfo:
        substrate_store.patch_context(str(uuid.uuid4()), {"a": 1}, "factory-agent")
    assert excinfo.value.status == 404


# ---------------------------------------------------------------------------
# Mechanical self-check: a suite that drifts to covering only the drain path
# by omission would regress the gap this suite exists to close.
# ---------------------------------------------------------------------------


def test_the_suite_itself_exercises_an_intake_path_method_not_only_drain():
    """A future edit that quietly deletes every real create_task() call from
    this file regresses exactly the gap A35 named — this fails if that
    happens, rather than relying on someone noticing."""
    source = Path(__file__).read_text()
    intake_calls = source.count(".create_task(")
    drain_calls = source.count(".transition_state(")
    assert intake_calls >= 3, "no intake-path (create_task) coverage in this suite"
    assert drain_calls >= 2, "no drain-path (transition_state) coverage in this suite"

