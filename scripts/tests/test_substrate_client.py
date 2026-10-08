"""The consolidated store client's own HTTP-level contract.

Ports the properties orphaned when test_gate_substrate.py died with its
module (release-gate finding on the consolidating PR): empty-body decoding,
fail-fast construction, plus the contracts that PR introduced — the
explicit-vs-env sentinel branching, the get-only reader facade the merge
gate binds, and the no-unattributed-write refusal.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import urllib.error
import pathlib
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import substrate_client  # noqa: E402
import schemas  # noqa: E402 -- apps/substrate/src on sys.path via substrate_client

#: Every way `Substrate`/`SubstrateReader` currently let a caller reach the
#: store. AC (R26.06, identity consolidation): "the identities admitted
#: before and after consolidation are compared... established by a test
#: that would fail if a path were added" -- this is that test's fixture. A
#: method added to either class without updating this set fails
#: test_public_surface_is_pinned first, which is the prompt to also confirm
#: it funnels through `_request` (test_every_public_method_funnels_through
#: _the_single_request_choke_point) rather than opening a new way to reach
#: the store that neither test can see.
_PUBLIC_SUBSTRATE_METHODS = {
    "get",
    "find_bead",
    "list_beads",
    "validate",
    "create",
    "patch",
    "links",
    "add_link",
    "delete_link",
    "delete_bead",
}
#: `validate` is the one public method deliberately excluded from the
#: single-request choke point below: its entire purpose (AC, R26.06 content
#: schema/provenance gate above the store) is to answer "would this be
#: accepted" without ever reaching the store, so requiring it to funnel
#: through `_request` would be requiring it to defeat its own point.
_NETWORK_SUBSTRATE_METHODS = _PUBLIC_SUBSTRATE_METHODS - {"validate"}
_PUBLIC_READER_METHODS = {"get"}


def _respond(monkeypatch, payload: bytes, capture: dict):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        capture["url"] = req.full_url
        capture["headers"] = dict(req.header_items())
        capture["timeout"] = timeout
        return _Resp(payload)

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake_urlopen)


def test_empty_body_decodes_to_none(monkeypatch):
    capture: dict = {}
    _respond(monkeypatch, b"", capture)
    client = substrate_client.Substrate(base_url="http://substrate.test/", key="k")
    assert client.get("/beads/x") is None


def test_base_url_trailing_slash_is_normalised(monkeypatch):
    capture: dict = {}
    _respond(monkeypatch, b"{}", capture)
    client = substrate_client.Substrate(base_url="http://substrate.test/", key="k")
    client.get("/beads/x")
    assert capture["url"] == "http://substrate.test/beads/x"


def test_env_shape_construction_fails_fast_naming_the_env_vars(monkeypatch):
    monkeypatch.delenv(substrate_client.URL_ENV, raising=False)
    monkeypatch.delenv(substrate_client.KEY_ENV, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        substrate_client.Substrate(created_by="test")
    assert substrate_client.URL_ENV in str(exc_info.value)
    assert substrate_client.KEY_ENV in str(exc_info.value)


def test_explicit_shape_defers_a_missing_value_to_the_first_request(monkeypatch):
    # The gate scripts' contract, ported from gate_substrate.fetch_json:
    # constructing with explicit-but-falsy values must not exit the process;
    # the first actual request refuses instead.
    monkeypatch.delenv(substrate_client.URL_ENV, raising=False)
    monkeypatch.delenv(substrate_client.KEY_ENV, raising=False)
    client = substrate_client.Substrate(base_url=None, key=None)
    with pytest.raises(RuntimeError, match=substrate_client.URL_ENV):
        client.get("/beads/x")


def test_http_error_carries_status_path_and_bounded_body_never_the_key(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", None, io.BytesIO(b"who are you")
        )

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake_urlopen)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="sekrit")
    with pytest.raises(substrate_client.SubstrateError) as exc_info:
        client.get("/beads/x")
    message = str(exc_info.value)
    assert "substrate 401 on /beads/x" in message
    assert "who are you" in message
    assert "sekrit" not in message


def test_reader_facade_is_read_only_by_construction():
    view = substrate_client.reader(base_url="http://substrate.test", key="k")
    assert hasattr(view, "get")
    for verb in ("create", "patch", "add_link", "delete_link", "delete_bead", "list_beads"):
        assert not hasattr(view, verb), f"reader facade exposes write/list surface: {verb}"


def test_an_unattributed_write_is_refused_before_any_request(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no request may leave an unattributed write path")

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", explode)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k")
    with pytest.raises(RuntimeError, match="unattributed"):
        client.create("task", "pending", {})
    with pytest.raises(RuntimeError, match="unattributed"):
        client.patch("some-id", {"state": "done"})
    # The same call succeeds once attributed, at the call site or instance.
    sent: dict = {}
    _respond(monkeypatch, b"{}", sent)
    client.patch("some-id", {"state": "done"}, created_by="operator")


def test_public_surface_is_pinned():
    """Every way the store is reachable through this client, enumerated. A
    method added to `Substrate` or `SubstrateReader` without updating this
    pinned set fails here first, before it can quietly widen (or narrow) who
    can reach the store."""
    public = {
        name
        for name, _ in inspect.getmembers(substrate_client.Substrate, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == _PUBLIC_SUBSTRATE_METHODS

    reader_public = {
        name
        for name, _ in inspect.getmembers(substrate_client.SubstrateReader, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert reader_public == _PUBLIC_READER_METHODS


def test_every_public_method_funnels_through_the_single_request_choke_point():
    """`_request` is the one place `X-API-Key` is attached and the one place
    a missing base/key is refused (checked behaviourally below). This is the
    source-level half of that guarantee: every public method's body must
    call `self._request(`, because a method that reached the store any other
    way -- a raw `urlopen`, a second client -- would carry neither
    guarantee, and no behavioral test enumerating call shapes could catch a
    method it doesn't yet know the arguments for. `validate` is excluded --
    see `_NETWORK_SUBSTRATE_METHODS`."""
    for name in _NETWORK_SUBSTRATE_METHODS:
        source = inspect.getsource(getattr(substrate_client.Substrate, name))
        assert "self._request(" in source, f"Substrate.{name} does not route through _request"


def _capturing_urlopen(monkeypatch):
    capture: dict = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        capture["method"] = req.get_method()
        capture["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(b"{}")

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake_urlopen)
    return capture


_CONSOLIDATED_CALLS = [
    pytest.param(lambda c: c.get("/beads/x"), "GET", id="get"),
    pytest.param(lambda c: c.find_bead("arch", "observation", "obs.x"), "GET", id="find_bead"),
    pytest.param(lambda c: c.list_beads("task"), "GET", id="list_beads"),
    pytest.param(lambda c: c.create("task", "pending", {}, created_by="op"), "POST", id="create"),
    pytest.param(lambda c: c.patch("id", {"a": 1}, created_by="op"), "PATCH", id="patch"),
    pytest.param(lambda c: c.links("id"), "GET", id="links"),
    pytest.param(lambda c: c.add_link("a", "b", "rel"), "POST", id="add_link"),
    pytest.param(lambda c: c.delete_link("lid"), "DELETE", id="delete_link"),
    pytest.param(lambda c: c.delete_bead("id"), "DELETE", id="delete_bead"),
]


@pytest.mark.parametrize("call,expected_method", _CONSOLIDATED_CALLS)
def test_x_api_key_attached_on_every_consolidated_path(monkeypatch, call, expected_method):
    """AC: "a request that reaches the store carries the credential
    substrate requires, on every consolidated path" -- exercised, not
    inspected, for every method `test_public_surface_is_pinned` knows about."""
    capture = _capturing_urlopen(monkeypatch)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="the-key", created_by="op")
    call(client)
    assert capture["method"] == expected_method
    assert capture["headers"].get("x-api-key") == "the-key"


@pytest.mark.parametrize("call,expected_method", _CONSOLIDATED_CALLS)
def test_missing_key_refuses_every_consolidated_path_before_any_request(monkeypatch, call, expected_method):
    """AC: "a request that reaches the store without the credential
    substrate requires... SHALL be refused on every consolidated path" --
    and refused before a socket ever opens, on every method, not just `get`."""

    def explode(*args, **kwargs):
        raise AssertionError("no request may leave the process without a key")

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", explode)
    client = substrate_client.Substrate(base_url="http://substrate.test", key=None, created_by="op")
    with pytest.raises(RuntimeError, match=substrate_client.KEY_ENV):
        call(client)


# ---------------------------------------------------------------------------
# add_link attribution (dev.finding 94e7a1f4): create_bead_link
# (apps/substrate/src/routes.py) reads created_by from the X-Created-By
# header, not the JSON body -- BeadLinkCreate declares no such field, so a
# body carrying one is silently accepted with the value dropped (pydantic's
# default extra="ignore"), not rejected. A double that accepted the body
# field would pass while production still wrote "unknown" for every link
# this client wrote, which is how the defect survived undetected.
# ---------------------------------------------------------------------------


def _route_like_urlopen(monkeypatch, capture: dict):
    """A urlopen fake that enforces exactly what create_bead_link enforces:
    created_by comes from the X-Created-By header (defaulting to "unknown"
    when absent), and the body is parsed through the real BeadLinkCreate
    schema, which has no created_by field to read -- so a client that only
    put it in the body would silently lose it here too."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        headers = {k.lower(): v for k, v in req.header_items()}
        raw_body = json.loads(req.data.decode())
        link = schemas.BeadLinkCreate(**raw_body)
        created_by = headers.get("x-created-by") or "unknown"
        capture["headers"] = headers
        capture["raw_body"] = raw_body
        capture["created_by"] = created_by
        payload = {
            "id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "target_id": str(link.target_id),
            "link_type": link.link_type,
            "content": link.content,
            "created_at": "2026-09-16T00:00:00Z",
            "created_by": created_by,
        }
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake_urlopen)


def test_add_link_attributes_through_the_header_not_the_silently_dropped_body(monkeypatch):
    capture: dict = {}
    _route_like_urlopen(monkeypatch, capture)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k", created_by="ea-derive")
    result = client.add_link("a", str(uuid.uuid4()), "depends_on")
    assert capture["headers"].get("x-created-by") == "ea-derive"
    assert result["created_by"] == "ea-derive"
    assert result["created_by"] != "unknown"


def test_add_link_does_not_rely_on_the_body_field_the_route_silently_drops(monkeypatch):
    """The second half of the fix: not just "the header is present" but "the
    client no longer depends on the body carrying it" -- a regression that
    put created_by back in the body only would still pass the header
    assertion above (the route ignores the extra field) while silently
    resurrecting the dependency this bead removes."""
    capture: dict = {}
    _route_like_urlopen(monkeypatch, capture)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k", created_by="ea-derive")
    client.add_link("a", str(uuid.uuid4()), "depends_on")
    assert "created_by" not in capture["raw_body"]


def test_bead_link_create_silently_ignores_an_unknown_created_by_body_field():
    """Pins the behaviour that let this defect survive undetected: BeadLinkCreate
    has no created_by field, and pydantic's default extra="ignore" means a body
    carrying one is accepted with the value dropped, never rejected with a 422
    that would have surfaced the mistake."""
    parsed = schemas.BeadLinkCreate(
        target_id=uuid.uuid4(), link_type="depends_on", created_by="someone"
    )
    assert "created_by" not in parsed.model_dump()


# ---------------------------------------------------------------------------
# validate() / find_schema_violations(): the content/provenance gate above the
# store (R26.06). AC1: a caller holding a candidate bead can learn whether it
# would be accepted without writing it. AC2: a store that accepted a bead its
# own schema would reject is detectable by a check. AC3: both use the exact
# schema objects the store uses (apps/substrate/src/schemas.py), not a copy.
# ---------------------------------------------------------------------------


def _valid_dev_task_content() -> dict:
    return {
        "lane": "bug-triage",
        "title": "t",
        "intent": "i",
        "context_refs": [],
        "acceptance": ["WHEN x THE y SHALL z"],
        "verification": {"commands": ["true"]},
        "scope": {"paths": ["x"], "forbidden_paths": [".github/workflows/**"]},
        "risk_class": "structural",
        "budget": {"max_agent_minutes": 1, "max_usd": 1.0, "max_tokens": 1},
    }


def test_validate_never_opens_a_request(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("validate() must not reach the store")

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", explode)
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k", namespace="dev")
    with pytest.raises(substrate_client.ValidationError):
        client.validate("task", {"lane": "nope"}, created_by="human")
    client.validate("task", _valid_dev_task_content(), created_by="human")


def test_validate_rejects_what_the_live_content_schema_rejects():
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k", namespace="dev")
    bad_content = {"lane": "nope"}
    with pytest.raises(substrate_client.ValidationError) as client_exc:
        client.validate("task", bad_content, created_by="human")
    with pytest.raises(substrate_client.ValidationError) as direct_exc:
        schemas.validate_bead_content("dev", "task", bad_content)
    # Same schema object, not a second transcription of it (AC3): the two
    # calls report the same underlying pydantic errors.
    assert client_exc.value.errors() == direct_exc.value.errors()


def test_validate_rejects_incomplete_provenance_for_an_agent_writer():
    client = substrate_client.Substrate(base_url="http://substrate.test", key="k", namespace="dev")
    with pytest.raises(substrate_client.ValidationError):
        client.validate(
            "task",
            _valid_dev_task_content(),
            created_by="claude",
            provenance={},
        )
    client.validate(
        "task",
        _valid_dev_task_content(),
        created_by="claude",
        provenance={
            "worker": "claude",
            "model": "m",
            "prompt_ref": "p",
            "tokens": None,
            "cost_usd": None,
            "duration_s": 1.0,
        },
    )


def test_find_schema_violations_flags_a_bead_the_store_should_never_have_accepted():
    """AC2: a store that accepted a bead its own declared schema now rejects
    (e.g. written before a field became required, or by a bug in the write
    path) is caught by this check rather than surfacing only when some later
    reader chokes on the shape."""
    stored = [
        {
            "id": "good-1",
            "namespace": "dev",
            "type": "task",
            "content": _valid_dev_task_content(),
            "provenance": {},
        },
        {
            "id": "bad-1",
            "namespace": "dev",
            "type": "task",
            "content": {"lane": "nope"},
            "provenance": {},
        },
    ]
    violations = substrate_client.find_schema_violations(stored)
    assert [v.bead_id for v in violations] == ["bad-1"]
    assert "lane" in violations[0].errors
