"""Tests for the two methods this bead adds beyond the frozen 11-method
BeadStore protocol: ``Substrate.create_bead`` (a generic ``POST /beads`` that
does not default namespace/type/state to dev.task's values) and
``Substrate.search`` (``POST /beads/search``, matching
``apps/substrate/src/schemas.py``'s ``BeadSearchRequest`` contract).

See dev.finding de106c5d and the bead this closes: eleven ``BeadStore``
methods and no generic create/search is why mcp-hub's finance/incident/cost
tools were building their own HTTP calls instead of widening this client.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NEW_CLIENT_PKG_INIT = (
    _REPO_ROOT / "apps" / "substrate" / "client" / "src" / "substrate_client" / "__init__.py"
)
_ALIAS = "new_substrate_client_for_generic_create_and_search_test"


def _load_new_substrate_client():
    """Load this package by explicit file path, under an alias distinct from
    the bare name ``substrate_client``.

    scripts/substrate_client.py is ALSO importable as ``substrate_client``
    (deliberately -- see this package's own docstring), and a dispatcher
    module collected earlier in the same pytest session can leave that OLD
    module cached under the bare name in ``sys.modules`` before this file
    ever runs. An ordinary ``import substrate_client`` would then silently
    pin this suite to the old client instead of the one under test -- see
    ``test_public_surface.py``'s ``_load_new_substrate_client``, which
    documents and follows the same pattern.
    """
    spec = importlib.util.spec_from_file_location(
        _ALIAS,
        _NEW_CLIENT_PKG_INIT,
        submodule_search_locations=[str(_NEW_CLIENT_PKG_INIT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ALIAS] = module
    spec.loader.exec_module(module)
    return module


_substrate_client = _load_new_substrate_client()
Substrate = _substrate_client.Substrate
SubstrateError = _substrate_client.SubstrateError
client_module = sys.modules[f"{_ALIAS}.client"]


class _CapturedRequest:
    def __init__(self, method: str, path: str, body: Any):
        self.method = method
        self.path = path
        self.body = body


def _run_against(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[_CapturedRequest], tuple[int, Any]],
    call: Callable[[Substrate], Any],
) -> tuple[Any, list[_CapturedRequest]]:
    """Patch ``client_module.httpx.request`` to route through a MockTransport
    driven by ``respond``, run ``call`` against a fresh client, and return
    (call result or raised exception, captured requests)."""
    captured: list[_CapturedRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        record = _CapturedRequest(request.method, request.url.path, body)
        captured.append(record)
        status, payload = respond(record)
        return httpx.Response(status, json=payload, request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client(transport=transport)
    monkeypatch.setattr(
        client_module.httpx, "request", lambda method, url, **kw: real_client.request(method, url, **kw)
    )

    client = Substrate(base_url="https://substrate.example.test", api_key="test-key")
    try:
        result = call(client)
    except SubstrateError as exc:
        result = exc
    finally:
        real_client.close()
    return result, captured


# ---------------------------------------------------------------------------
# create_bead: a non-dev namespace, field by field, and no default masking.
# ---------------------------------------------------------------------------


def test_create_bead_composes_the_full_payload_for_a_non_dev_namespace(monkeypatch):
    def respond(record: _CapturedRequest) -> tuple[int, Any]:
        return 200, {"id": "bead-9", "namespace": "finance", "type": "bill", "state": "pending"}

    result, captured = _run_against(
        monkeypatch,
        respond,
        lambda c: c.create_bead(
            "finance",
            "bill",
            "pending",
            {"amount": 42.5, "vendor": "acme"},
            "mcp-finance/log_bill",
            trust_tier="system",
        ),
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.path == "/beads"
    assert request.body["namespace"] == "finance"
    assert request.body["type"] == "bill"
    assert request.body["state"] == "pending"
    assert request.body["trust_tier"] == "system"
    assert request.body["created_by"] == "mcp-finance/log_bill"
    assert request.body["content"] == {"amount": 42.5, "vendor": "acme"}
    assert result == {"id": "bead-9", "namespace": "finance", "type": "bill", "state": "pending"}


def test_create_bead_defaults_trust_tier_to_user(monkeypatch):
    def respond(record: _CapturedRequest) -> tuple[int, Any]:
        return 200, {"id": "bead-1"}

    _, captured = _run_against(
        monkeypatch,
        respond,
        lambda c: c.create_bead("incident", "outage", "open", {}, "mcp-incidents/open"),
    )

    assert captured[0].body["trust_tier"] == "user"


# ---------------------------------------------------------------------------
# create_task: unchanged. Pinned payload, byte-identical to today's.
# ---------------------------------------------------------------------------


def test_create_task_payload_is_unchanged(monkeypatch):
    def respond(record: _CapturedRequest) -> tuple[int, Any]:
        return 200, {"id": "bead-1"}

    _, captured = _run_against(
        monkeypatch,
        respond,
        lambda c: c.create_task(
            {"title": "t", "intent": "i", "acceptance": ["a"]}, "factory-agent", trust_tier="system"
        ),
    )

    assert len(captured) == 1
    assert captured[0].body == {
        "namespace": "dev",
        "type": "task",
        "state": "pending",
        "trust_tier": "system",
        "created_by": "factory-agent",
        "content": {"title": "t", "intent": "i", "acceptance": ["a"]},
    }


# ---------------------------------------------------------------------------
# search: matches BeadSearchRequest's contract exactly; a fake that enforces
# that contract's own bounds (query 1..4000 chars, limit 1..50) rather than a
# looser double, per test_recorder_parity.py's rule against a second
# implementation of the same contract.
# ---------------------------------------------------------------------------


def _beadsearch_fake_responder(record: _CapturedRequest) -> tuple[int, Any]:
    assert record.method == "POST"
    assert record.path == "/beads/search"
    body = record.body
    query = body.get("query")
    limit = body.get("limit")
    if not isinstance(query, str) or not (1 <= len(query) <= 4000):
        return 422, {"detail": "query must be 1..4000 chars"}
    if not isinstance(limit, int) or not (1 <= limit <= 50):
        return 422, {"detail": "limit must be 1..50"}
    for key in body:
        if key not in {"query", "limit", "namespace", "type"}:
            return 422, {"detail": f"unexpected field {key!r}"}
    return 200, [{"bead": {"id": "bead-1"}, "score": 0.91}]


def test_search_sends_exactly_the_beadsearchrequest_fields(monkeypatch):
    result, captured = _run_against(
        monkeypatch,
        _beadsearch_fake_responder,
        lambda c: c.search("NETFLIX", limit=8, namespace="finance", type="transaction"),
    )

    assert len(captured) == 1
    assert captured[0].body == {
        "query": "NETFLIX",
        "limit": 8,
        "namespace": "finance",
        "type": "transaction",
    }
    assert result == [{"bead": {"id": "bead-1"}, "score": 0.91}]


def test_search_omits_namespace_and_type_when_not_given(monkeypatch):
    _, captured = _run_against(monkeypatch, _beadsearch_fake_responder, lambda c: c.search("PRIN-003"))

    assert captured[0].body == {"query": "PRIN-003", "limit": 10}


def test_search_does_not_mask_an_out_of_contract_query_from_the_store(monkeypatch):
    """An empty query is outside BeadSearchRequest's own bounds. The client
    must not swallow or coerce it -- it reaches the fake, and the fake's
    rejection (a 422) surfaces as SubstrateError, proving there is no
    client-side re-implementation of this validation to drift from the
    store's."""
    result, captured = _run_against(monkeypatch, _beadsearch_fake_responder, lambda c: c.search(""))

    assert len(captured) == 1
    assert isinstance(result, SubstrateError)
    assert result.status == 422


# ---------------------------------------------------------------------------
# Error shape: both new methods raise the same SubstrateError the other nine
# methods raise, carrying .status/.body, on both a 4xx and a 5xx.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 500])
def test_create_bead_raises_substrate_error_with_status_and_body(monkeypatch, status):
    def respond(record: _CapturedRequest) -> tuple[int, Any]:
        return status, {"detail": "boom"}

    result, _ = _run_against(
        monkeypatch, respond, lambda c: c.create_bead("finance", "bill", "pending", {}, "someone")
    )

    assert isinstance(result, SubstrateError)
    assert result.status == status
    assert "boom" in result.body


@pytest.mark.parametrize("status", [422, 503])
def test_search_raises_substrate_error_with_status_and_body(monkeypatch, status):
    def respond(record: _CapturedRequest) -> tuple[int, Any]:
        return status, {"detail": "boom"}

    result, _ = _run_against(monkeypatch, respond, lambda c: c.search("query"))

    assert isinstance(result, SubstrateError)
    assert result.status == status
    assert "boom" in result.body
