"""M8 recorder proof (docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md
§4/§6, and the 2026-08-28 record's F2 "the seam covers the drain and not the
intake"): the eight activities under ``apps/factory-dispatcher/activities/``
each used to build ``self.base_url``/``self._headers`` inline in their own
``__init__`` -- the exact ``{"X-API-Key": key, "Content-Type":
"application/json"}`` dict, seven times over. M8 replaces that construction
with a call into ``substrate_client`` (the one Python substrate client, M6),
leaving ``_request`` and every domain method (``find_change``,
``create_observation``, ...) byte-for-byte unchanged.

Per the plan's own words: "A green suite alone is NOT the proof -- the
recorded request set is." This drives every store's full method surface
through a request-recording httpx transport twice: once against the actual,
post-M8 construction, and once against a hand-built pre-M8 stand-in whose
``.base_url``/``._headers`` are set via the literal formula every one of
these classes' ``__init__`` used to run inline -- the same formula
``apps/factory-dispatcher/substrate.py``'s own untouched ``Substrate.__init__``
still runs, since M8 does not touch that file. If the two recorded request
sets -- method, path, query, body, headers minus the key -- ever diverge,
something about routing a request through the shared client changed a byte
this store used to send.

``file_task.py``'s own create path is the other half of M8: it used to call
``sub._request("POST", "/beads", json={...})`` directly; it now calls
``sub.create_task(content, created_by, trust_tier=...)``, the protocol method
the same ``Substrate`` instance already exposed. The bottom section proves
that swap request-identical too, including the ``trust_tier`` field the
acceptance criterion names as the one a re-pointed intake most easily drops.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

from activities import change_apply as ca  # noqa: E402
from activities import doctrine_registry_view as drv  # noqa: E402
from activities import doctrine_staleness as ds  # noqa: E402
from activities import ea_observation as eo  # noqa: E402
from activities import knowledge_ingestion as ki  # noqa: E402
from activities import spec_record_reconcile as src_mod  # noqa: E402
from activities import staleness_report as sr  # noqa: E402
from activities import worker_revision_drift as wrd  # noqa: E402
import substrate as substrate_module  # noqa: E402

BASE_URL = "https://substrate.example.test"
API_KEY = "test-key"


# ---------------------------------------------------------------------------
# the recorder: an httpx transport that captures every request it handles,
# normalised to (method, path, query, body, headers minus the credential) --
# identical shape to apps/substrate/client/tests/test_recorder_parity.py's.
# ---------------------------------------------------------------------------


class RecordedRequest(NamedTuple):
    method: str
    path: str
    query: dict
    body: Any
    headers: dict


_SIGNIFICANT_HEADERS = {"content-type", "x-created-by"}


def _normalise_headers(headers: httpx.Headers) -> dict:
    return {k.lower(): v for k, v in headers.items() if k.lower() in _SIGNIFICANT_HEADERS}


class RecordingTransport(httpx.MockTransport):
    def __init__(self, respond: Callable[["RecordedRequest"], tuple[int, Any]]):
        self.records: list[RecordedRequest] = []
        self._respond = respond
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        query = dict(httpx.QueryParams(request.url.query))
        body = json.loads(request.content) if request.content else None
        record = RecordedRequest(
            method=request.method,
            path=request.url.path,
            query=query,
            body=body,
            headers=_normalise_headers(request.headers),
        )
        self.records.append(record)
        status, payload = self._respond(record)
        return httpx.Response(status, json=payload, request=request)


def _default_responder(record: RecordedRequest) -> tuple[int, Any]:
    """One canned, always-2xx response per route shape -- the proof is about
    the REQUESTS a store sends, not the responses it gets back."""
    if record.method == "GET" and record.path.endswith("/links"):
        return 200, []
    if record.method == "GET" and record.path == "/beads":
        # dev.finding 5c3cf2b3: SubstrateEAObserverStore.list_active_findings
        # now pages by offset (see activities/ea_observation.py). Only that
        # call ever sends an `offset` query param through this harness --
        # answering it with an empty page is what lets the walk terminate
        # after its second request instead of reading the same canned bead
        # forever and raising on the repeated-first-id guard.
        if "offset" in record.query:
            return 200, []
        return 200, [
            {
                "id": "bead-1",
                "state": "active",
                "content": {"ref": "x", "pr_refs": []},
                "context": {"observation_kind": eo.OBSERVATION_KIND},
            }
        ]
    return 200, {"id": "bead-1", "state": "doing"}


def _drive(make_store: Callable[[], Any], calls: list[Callable[[Any], Any]]) -> list[RecordedRequest]:
    """Monkeypatch the real ``httpx.request`` (shared process-wide, regardless
    of whether a module imported it at module scope or inside a function --
    see ``knowledge_ingestion.py``'s local ``import httpx``) to route through
    a fresh recording transport, run every call in ``calls`` against a freshly
    built store, and return the transport's recorded requests."""
    transport = RecordingTransport(_default_responder)
    real_client = httpx.Client(transport=transport)

    def _patched_request(method, url, **kwargs):
        return real_client.request(method, url, **kwargs)

    original = httpx.request
    httpx.request = _patched_request
    try:
        store = make_store()
        for call in calls:
            call(store)
    finally:
        httpx.request = original
        real_client.close()
    return transport.records


def _pre_m8_instance(cls, extra_setup: Callable[[Any], None] | None = None):
    """A store instance wired exactly the way every one of these classes'
    ``__init__`` built itself before M8 -- bypassing the (now
    substrate_client-backed) ``__init__`` and setting ``.base_url``/
    ``._headers`` via the literal formula that used to live inline in each of
    the eight, identical to ``apps/factory-dispatcher/substrate.py``'s own
    untouched ``Substrate.__init__``."""
    instance = object.__new__(cls)
    instance.base_url = BASE_URL.rstrip("/")
    instance._headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    if extra_setup is not None:
        extra_setup(instance)
    return instance


def _assert_parity(cls, calls: list[Callable[[Any], Any]], *, extra_setup=None, ctor_kwargs=None):
    ctor_kwargs = ctor_kwargs or {}
    new_records = _drive(lambda: cls(base_url=BASE_URL, api_key=API_KEY, **ctor_kwargs), calls)
    old_records = _drive(lambda: _pre_m8_instance(cls, extra_setup), calls)
    assert len(new_records) == len(calls) or extra_setup is not None, (
        "call script must not silently fan out or collapse requests"
    )
    assert new_records == old_records


# ---------------------------------------------------------------------------
# per-store call scripts -- every public (non-underscore) method each store
# declares, driven with representative arguments.
# ---------------------------------------------------------------------------


def test_change_apply_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.find_change("chg.pr-1"),
        lambda c: c.create_change(
            {"namespace": "arch", "type": "change", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.find_status(),
        lambda c: c.create_status(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_status("bead-1", {"a": 1}, {"b": 2}),
        lambda c: c.find_application_by_ref("app-1"),
        lambda c: c.find_task_by_pr_url("https://github.test/pr/1"),
        lambda c: c.list_release_verdicts(),
        lambda c: c.list_links("bead-1", direction="outgoing", link_type="delivers"),
        lambda c: c.create_link("src-1", "tgt-1", "delivers"),
    ]

    def _extra_setup(instance):
        instance._dev_beads = ca._PagedDevBeadCache(instance._fetch_dev_bead_page)

    new_records = _drive(lambda: ca.SubstrateChangeApplyStore(base_url=BASE_URL, api_key=API_KEY), calls)
    old_records = _drive(lambda: _pre_m8_instance(ca.SubstrateChangeApplyStore, _extra_setup), calls)
    assert new_records == old_records
    assert len(new_records) >= len(calls)


def test_doctrine_registry_view_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.list_principles(),
        lambda c: c.find_observation("obs.principles-check-view"),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_observation("bead-1", content={"c": 1}, context={"d": 2}, state="active"),
    ]
    _assert_parity(drv.SubstrateDoctrineRegistryViewStore, calls)


def test_doctrine_staleness_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.list_principles(),
        lambda c: c.has_governing_link("principle-1"),
        lambda c: c.find_observation("obs.x"),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_observation("bead-1", {"c": 1}, {"d": 2}, "created-by-x"),
        lambda c: c.resolve_observation("bead-1", "created-by-x"),
    ]
    _assert_parity(ds.SubstratePrincipleObservationStore, calls)


def test_ea_observation_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.list_applications(),
        lambda c: c.find_observation("obs.x"),
        lambda c: c.list_active_findings(),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_observation("bead-1", content={"c": 1}, state="active", context={"d": 2}),
        lambda c: c.create_link("src-1", "tgt-1", "depends_on", "created-by-x"),
        lambda c: c.list_links("bead-1", direction="both", link_type="depends_on"),
        lambda c: c.find_ci("ci.x"),
        lambda c: c.list_active_cis(),
        lambda c: c.create_ci(
            {"namespace": "arch", "type": "ci", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_ci("bead-1", content={"c": 1}, state="active"),
    ]
    # Not the generic _assert_parity: list_active_findings (dev.finding
    # 5c3cf2b3) deliberately pages, so it alone sends two requests for this
    # harness's canned one-bead-then-empty responder -- the same reasoning
    # test_change_apply_store_is_request_identical_to_pre_m8_construction
    # above already applies for its own paged dev-bead cache.
    new_records = _drive(lambda: eo.SubstrateEAObserverStore(base_url=BASE_URL, api_key=API_KEY), calls)
    old_records = _drive(lambda: _pre_m8_instance(eo.SubstrateEAObserverStore), calls)
    assert new_records == old_records
    assert len(new_records) >= len(calls)


def test_knowledge_ingestion_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.list_tasks(),
        lambda c: c.file_task({"title": "t"}),
        lambda c: c.list_notes("task-1"),
        lambda c: c.add_note("task-1", "status", "body text", "created-by-x"),
        lambda c: c.create_principle({"ref": "x"}),
        lambda c: c.create_link("src-1", "tgt-1", "derived_from", "created-by-x"),
    ]
    _assert_parity(ki.SubstrateKnowledgeStore, calls)


def test_spec_record_reconcile_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.list_tasks(),
        lambda c: c.find_observation("obs.spec-record-reconcile"),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_observation("bead-1", content={"c": 1}, context={"d": 2}, state="active"),
    ]
    _assert_parity(src_mod.SubstrateSpecRecordStore, calls)


def test_staleness_report_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.observation_exists("obs.x"),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
    ]
    _assert_parity(sr.SubstrateObservationStore, calls)


def test_worker_revision_drift_store_is_request_identical_to_pre_m8_construction():
    calls: list[Callable[[Any], Any]] = [
        lambda c: c.find_observation("obs.worker-revision-drift"),
        lambda c: c.create_observation(
            {"namespace": "arch", "type": "observation", "state": "active", "content": {}, "created_by": "x"}
        ),
        lambda c: c.update_observation("bead-1", content={"c": 1}, context={"d": 2}, state="active"),
    ]
    _assert_parity(wrd.SubstrateWorkerRevisionDriftStore, calls)


# ---------------------------------------------------------------------------
# construction identity: the fields the recorder above cannot see because
# they never leave __init__ -- base_url/api_key resolution and the missing-
# key refusal, across every store, matching apps/factory-dispatcher/substrate.py's
# own untouched Substrate.__init__ exactly (M8 does not touch that file).
# ---------------------------------------------------------------------------

STORE_CLASSES = [
    ca.SubstrateChangeApplyStore,
    drv.SubstrateDoctrineRegistryViewStore,
    ds.SubstratePrincipleObservationStore,
    eo.SubstrateEAObserverStore,
    ki.SubstrateKnowledgeStore,
    sr.SubstrateObservationStore,
    wrd.SubstrateWorkerRevisionDriftStore,
]


def test_every_store_resolves_explicit_base_url_and_api_key_identically():
    for cls in STORE_CLASSES:
        store = cls(base_url="https://example.test/", api_key="k")
        assert store.base_url == "https://example.test"
        assert store._headers == {"X-API-Key": "k", "Content-Type": "application/json"}


def test_every_store_falls_back_to_the_environment_identically(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://env.example.test/")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "env-key")
    for cls in STORE_CLASSES:
        store = cls()
        assert store.base_url == "https://env.example.test"
        assert store._headers["X-API-Key"] == "env-key"


def test_every_store_refuses_a_missing_api_key_with_the_same_message(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_URL", "https://env.example.test/")
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    for cls in STORE_CLASSES:
        try:
            cls()
        except RuntimeError as exc:
            assert str(exc) == "SUBSTRATE_API_KEY is not set"
        else:
            raise AssertionError(f"{cls.__name__} did not refuse a missing SUBSTRATE_API_KEY")


def test_spec_record_store_resolves_explicit_credentials_identically():
    """SubstrateSpecRecordStore takes base_url/api_key as required positional
    arguments (never reads the environment itself) -- covered separately from
    STORE_CLASSES above because its constructor has no defaults to omit."""
    store = src_mod.SubstrateSpecRecordStore("https://example.test/", "k")
    assert store.base_url == "https://example.test"
    assert store._headers == {"X-API-Key": "k", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# file_task.py's own create path: it used to call
# `sub._request("POST", "/beads", json={...})` directly on its `substrate.Substrate`
# instance; it now calls `sub.create_task(content, created_by, trust_tier=...)`,
# the protocol method that same (unmodified by M8) class already exposed.
# ---------------------------------------------------------------------------


def test_file_tasks_create_path_is_request_identical_to_the_old_direct_post():
    content = {
        "title": "t",
        "intent": "i",
        "acceptance": ["a"],
        "lane": "code-health",
        "spec_identity": "abc123",
    }
    created_by = "factory-dispatcher/file-task"

    def _old_direct_post(store):
        store._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "task",
                "state": "pending",
                "trust_tier": "user",
                "created_by": created_by,
                "content": content,
            },
        )

    def _new_create_task_call(store):
        store.create_task(content, created_by, trust_tier="user")

    old_records = _drive(
        lambda: substrate_module.Substrate(base_url=BASE_URL, api_key=API_KEY), [_old_direct_post]
    )
    new_records = _drive(
        lambda: substrate_module.Substrate(base_url=BASE_URL, api_key=API_KEY), [_new_create_task_call]
    )

    assert old_records == new_records
    assert new_records[0].body["trust_tier"] == "user"
    assert new_records[0].body["created_by"] == created_by
    assert new_records[0].body["content"] == content
