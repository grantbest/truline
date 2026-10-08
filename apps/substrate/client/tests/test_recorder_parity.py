"""The recorder proof: apps/substrate/client (package ``substrate_client``) is
request-identical to the two old clients it replaces the surface of.

M6 (docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md
§4/§6): "an httpx transport recorder captures every request each existing
test suite makes against the old client(s) -- method, path, query, body,
headers minus the key -- and the new client's replay is asserted identical.
A green suite alone is NOT the proof; the recorded request set is."

This module drives the call shapes ``apps/factory-dispatcher/tests/test_substrate.py``
and ``test_store_contract.py`` already exercise against the OLD dispatcher
client (``apps/factory-dispatcher/substrate.py``, all 14 ``BeadStore``
methods) through a recording ``httpx.MockTransport``, then drives the same
calls against the NEW client (this package) through a second instance of the
same transport, and asserts the two recorded request sets are identical
(method, path, query, body, headers minus ``X-API-Key``).

``SubstrateReader`` traces back to ``scripts/substrate_client.py``, whose
client is urllib-based, not httpx. There is no old httpx traffic to record
for it -- so its one call (``get``) is captured on the old side with the same
urllib monkeypatch ``scripts/tests/test_substrate_client.py`` already uses,
normalised to the same (method, path, query, headers-minus-key) shape the
httpx recorder produces for the new side, and compared against that.

Neither old client is modified: both are imported read-only, by file path, so
this suite stays inside apps/substrate/ and apps/factory-dispatcher/ (the
scope this bead is allowed to touch) while still exercising the real old
source.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DISPATCHER_DIR = _REPO_ROOT / "apps" / "factory-dispatcher"
_SCRIPTS_CLIENT_PATH = _REPO_ROOT / "scripts" / "substrate_client.py"
_NEW_CLIENT_PKG_INIT = (
    _REPO_ROOT / "apps" / "substrate" / "client" / "src" / "substrate_client" / "__init__.py"
)

if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

import substrate as old_dispatcher_substrate  # noqa: E402


def _load_module_by_path(alias: str, path: Path, *, is_package: bool = False):
    """Load a module under an explicit alias, by file path, bypassing
    ``sys.path``/``sys.modules`` name resolution entirely.

    The old scripts client and this new package are BOTH named
    ``substrate_client`` (the new package's name IS the old module's name --
    that is the point of M6), so an ordinary ``import substrate_client``
    cannot name both in one process, and whichever sys.path entry happens to
    come first (or whichever module some unrelated test collected earlier
    already cached under that name) would silently win. Loading each one
    explicitly by path, under its own alias, makes this suite's outcome
    independent of collection order and of anything else sys.path holds.
    """
    kwargs = {"submodule_search_locations": [str(path.parent)]} if is_package else {}
    spec = importlib.util.spec_from_file_location(alias, path, **kwargs)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module  # relative imports inside a package need this pre-registered
    spec.loader.exec_module(module)
    return module


old_scripts_substrate_client = _load_module_by_path(
    "old_scripts_substrate_client", _SCRIPTS_CLIENT_PATH
)
new_substrate_client = _load_module_by_path(
    "new_substrate_client", _NEW_CLIENT_PKG_INIT, is_package=True
)
new_client_module = sys.modules["new_substrate_client.client"]
# ``new_substrate_client.reader`` is shadowed by ``__init__.py``'s own
# ``from .reader import ..., reader`` (the attribute now names that
# function, not the submodule) -- fetched from sys.modules instead, which
# import machinery populates regardless of that rebind.
new_reader_module = sys.modules["new_substrate_client.reader"]
NewSubstrate = new_substrate_client.Substrate
NewSubstrateReader = new_substrate_client.SubstrateReader


# ---------------------------------------------------------------------------
# The recorder: an httpx transport that captures every request it handles,
# normalised to (method, path, query, body, headers minus the credential).
# ---------------------------------------------------------------------------


class RecordedRequest(NamedTuple):
    method: str
    path: str
    query: dict
    body: Any
    headers: dict


#: Headers httpx itself attaches to every request regardless of what the
#: client code asked for (host, content-length, accept-encoding, the default
#: user-agent, ...). Comparing those would assert that two httpx versions
#: agree with themselves, not that the two CLIENTS constructed the same
#: request -- so the comparison is restricted to headers the client code
#: itself sets, which today is exactly this set (plus X-Api-Key, stripped
#: separately per the AC).
_SIGNIFICANT_HEADERS = {"content-type", "x-created-by"}


def _normalise_headers(headers: httpx.Headers) -> dict:
    return {
        k.lower(): v
        for k, v in headers.items()
        if k.lower() in _SIGNIFICANT_HEADERS
    }


class RecordingTransport(httpx.MockTransport):
    """An httpx transport that records every request before answering it
    from a caller-supplied responder, keyed on the normalised request."""

    def __init__(self, respond: Callable[[RecordedRequest], tuple[int, Any]]):
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
    """One canned, always-2xx response per route shape. The recorder proof
    is about the REQUESTS a client sends, not the responses it gets back --
    these bodies exist only so each client call completes normally (a short
    page to stop `list_beads`'s paging loop after one request, a decodable
    JSON object everywhere else)."""
    if record.method == "GET" and record.path == "/beads":
        return 200, [{"id": "bead-1", "content": {"ref": "PRIN-003"}}]
    if record.method == "GET" and record.path.endswith("/links"):
        return 200, []
    if record.method == "GET" and record.path.endswith("/events"):
        return 200, []
    return 200, {"id": "bead-1", "state": "doing"}


def _drive_via_transport(
    module, make_client: Callable[[], Any], calls: list[Callable[[Any], Any]]
) -> list[RecordedRequest]:
    """Monkeypatch ``module.httpx.request`` to route through a fresh
    recording transport, run every call in ``calls`` against a freshly built
    client, and return the transport's recorded requests."""
    transport = RecordingTransport(_default_responder)
    real_httpx_client = httpx.Client(transport=transport)

    def _patched_request(method, url, **kwargs):
        return real_httpx_client.request(method, url, **kwargs)

    original = module.httpx.request
    module.httpx.request = _patched_request
    try:
        client = make_client()
        for call in calls:
            call(client)
    finally:
        module.httpx.request = original
        real_httpx_client.close()
    return transport.records


# ---------------------------------------------------------------------------
# The call script: the shapes apps/factory-dispatcher/tests/test_substrate.py
# and test_store_contract.py already exercise against the old dispatcher
# client, run here against both the old and the new one.
# ---------------------------------------------------------------------------

_PROVENANCE = {
    "worker": "codex",
    "model": "codex-cli",
    "prompt_ref": "dev.task/task-1",
    "tokens": 0,
    "cost_usd": 0.0,
    "duration_s": 1.2,
}

_CALL_SCRIPT: list[Callable[[Any], Any]] = [
    lambda c: c.find_bead("arch", "principle", "PRIN-003"),
    lambda c: c.list_beads("arch", "release", state="planned"),
    lambda c: c.list_beads("arch", "release"),
    lambda c: c.list_tasks(state="pending"),
    lambda c: c.list_notes("task-1"),
    lambda c: c.list_links("task-1", direction="outgoing", link_type="delivers"),
    lambda c: c.list_events("task-1"),
    lambda c: c.create_task({"title": "t", "intent": "i", "acceptance": ["a"]}, "factory-agent"),
    lambda c: c.create_task({}, "factory-dispatcher/scanner", trust_tier="system"),
    lambda c: c.set_state("bead-1", "failed", "factory-agent"),
    lambda c: c.transition_state("bead-1", "pending", "doing", "factory-agent"),
    lambda c: c.patch_content(
        "bead-1", {"title": "revised", "intent": "i", "acceptance": ["a"]}, "factory-agent"
    ),
    lambda c: c.add_note("task-1", "status", "done", "factory-dispatcher/codex", provenance=_PROVENANCE),
    lambda c: c.add_link("task-1", "principle-uuid-3", "applies", "factory-dispatcher/claude"),
    lambda c: c.create_bead("arch", "release", "planned", {"ref": "R26.01"}, "factory-agent"),
    lambda c: c.create_bead(
        "arch",
        "observation",
        "active",
        {"ref": "wrd/worker"},
        "factory-dispatcher/worker-revision-drift",
        context={"condition": "clear"},
    ),
    lambda c: c.create_bead(
        "arch",
        "observation",
        "active",
        {"ref": "wrd/worker"},
        "factory-dispatcher/worker-revision-drift",
        provenance=_PROVENANCE,
    ),
    lambda c: c.patch_context("bead-1", {"condition": "clear"}, "factory-agent"),
]


def _make_old_dispatcher_client() -> old_dispatcher_substrate.Substrate:
    return old_dispatcher_substrate.Substrate(base_url="https://substrate.example.test", api_key="test-key")


def _make_new_client() -> NewSubstrate:
    return NewSubstrate(base_url="https://substrate.example.test", api_key="test-key")


def test_new_client_replays_the_dispatcher_suites_request_set_identically():
    old_records = _drive_via_transport(
        old_dispatcher_substrate, _make_old_dispatcher_client, _CALL_SCRIPT
    )
    new_records = _drive_via_transport(new_client_module, _make_new_client, _CALL_SCRIPT)

    assert len(old_records) == len(_CALL_SCRIPT), "call script itself must not fan out per call"
    assert old_records == new_records


# ---------------------------------------------------------------------------
# Pagination: list_beads/list_tasks page past a full first page rather than
# truncating (test_list_beads_pages_past_a_full_first_page_instead_of_truncating,
# test_list_tasks_also_pages_past_the_limit in the old suite). A short page
# stops the walk; a full page must be followed by another request at the next
# offset. Proven here as its own scenario because the default responder above
# always returns a short (length 1) page, which would never exercise this.
# ---------------------------------------------------------------------------


def _paged_responder(all_beads: list[dict]) -> Callable[[RecordedRequest], tuple[int, Any]]:
    def _respond(record: RecordedRequest) -> tuple[int, Any]:
        if record.method == "GET" and record.path == "/beads":
            limit = int(record.query["limit"])
            offset = int(record.query.get("offset", 0))
            return 200, all_beads[offset : offset + limit]
        return 200, {"id": "bead-1"}

    return _respond


def test_new_client_pages_list_beads_identically_to_the_old_one():
    all_beads = [{"id": f"bead-{i}"} for i in range(5)]

    old_transport = RecordingTransport(_paged_responder(all_beads))
    old_client_httpx = httpx.Client(transport=old_transport)
    original = old_dispatcher_substrate.httpx.request
    old_dispatcher_substrate.httpx.request = lambda method, url, **kw: old_client_httpx.request(method, url, **kw)
    try:
        old_result = _make_old_dispatcher_client().list_beads("dev", "task", limit=2)
    finally:
        old_dispatcher_substrate.httpx.request = original
        old_client_httpx.close()

    new_transport = RecordingTransport(_paged_responder(all_beads))
    new_client_httpx = httpx.Client(transport=new_transport)
    original_new = new_client_module.httpx.request
    new_client_module.httpx.request = lambda method, url, **kw: new_client_httpx.request(method, url, **kw)
    try:
        new_result = _make_new_client().list_beads("dev", "task", limit=2)
    finally:
        new_client_module.httpx.request = original_new
        new_client_httpx.close()

    assert old_result == new_result == all_beads
    assert old_transport.records == new_transport.records
    assert len(new_transport.records) == 3  # offsets 0, 2, 4
    assert [int(r.query.get("offset", 0)) for r in new_transport.records] == [0, 2, 4]


# ---------------------------------------------------------------------------
# SubstrateReader.get -- ported from scripts/substrate_client.py's
# SubstrateReader/reader(). The old side is urllib, not httpx; captured with
# the same monkeypatch scripts/tests/test_substrate_client.py uses, then
# normalised to the same shape the httpx recorder produces.
# ---------------------------------------------------------------------------


def _capture_old_reader_get(path: str) -> RecordedRequest:
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["full_url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    original = old_scripts_substrate_client.urllib.request.urlopen
    old_scripts_substrate_client.urllib.request.urlopen = fake_urlopen
    try:
        view = old_scripts_substrate_client.reader(base_url="https://substrate.example.test", key="test-key")
        view.get(path)
    finally:
        old_scripts_substrate_client.urllib.request.urlopen = original

    url = httpx.URL(captured["full_url"])
    return RecordedRequest(
        method=captured["method"],
        path=url.path,
        query=dict(httpx.QueryParams(url.query)),
        body=None,
        headers=_normalise_headers(httpx.Headers(captured["headers"])),
    )


def test_new_reader_get_matches_the_old_scripts_reader_request():
    old_record = _capture_old_reader_get("/beads/x")

    new_transport = RecordingTransport(lambda record: (200, {}))
    new_client_httpx = httpx.Client(transport=new_transport)
    original_request = new_client_module.httpx.request
    new_client_module.httpx.request = lambda method, url, **kw: new_client_httpx.request(method, url, **kw)
    try:
        view = new_reader_module.reader(base_url="https://substrate.example.test", api_key="test-key")
        assert isinstance(view, NewSubstrateReader)
        view.get("/beads/x")
    finally:
        new_client_module.httpx.request = original_request
        new_client_httpx.close()

    assert len(new_transport.records) == 1
    new_record = new_transport.records[0]

    assert (old_record.method, old_record.path, old_record.query, old_record.body) == (
        new_record.method,
        new_record.path,
        new_record.query,
        new_record.body,
    )
    # Headers minus the key: neither the old reader's GET nor the new one
    # attaches Content-Type (no body on a read) -- both normalise to nothing.
    assert old_record.headers == new_record.headers == {}
