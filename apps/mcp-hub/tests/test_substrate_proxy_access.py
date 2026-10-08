"""Identity boundary for the raw substrate passthrough (R26.06 consolidation).

`substrate_proxy` (apps/mcp-hub/src/openapi_app.py) is the single door onto
the whole store from mcp-hub's HTTP surface, mounted twice
(`/api/v1/substrate/{path}` and the legacy `/substrate/{path}`) for GET,
POST, PATCH and DELETE. It injects the real Substrate `X-API-Key`
server-side (tools.finance.substrate_headers) -- the one credential the
browser and every script client must never hold themselves (see
apps/lifeops-console/src/providers/substrate-client.ts and
scripts/substrate_client.py). Consolidating every reader/writer onto this
one door means a single permissive default here would widen reach for all
of them at once, and this route is the one reachable from a browser.

This file pins two things by test, not by inspection:
  - the exact set of routes reachable under a "substrate" path, so a new
    passthrough route added later without deliberately re-verifying its
    guard fails this file, not a security review months later;
  - the exact set of identities `require_authenticated_scope("substrate.proxy")`
    admits, across both path prefixes and every proxied HTTP verb.

See .factory/design.md (release-gate finding on #661) for why this used to
be pinned as a known, tracked exception -- `check_scope`'s internal/unknown
bypass let a headerless HTTP request through as if it were a trusted
stdio/local call. `substrate_proxy` now uses `require_authenticated_scope`,
the same fail-closed primitive `factory.read` adopted for the #620 finding,
closing that gap.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app

PROXY_PATHS = ["/api/v1/substrate/beads", "/substrate/beads"]
PROXY_METHODS = ["GET", "POST", "PATCH", "DELETE"]

# What the console's own browser traffic carries after Cloudflare Access
# authenticates and Traefik injects identity headers (same shape as
# test_factory_router_access.py's HUMAN/SERVICE_* fixtures).
HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}
SERVICE_WITH_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "substrate.proxy",
}
SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "probe.read",
}


# CAVEAT (release-gate advisory): this pin filters routes by the substring
# "substrate" in the path; a store passthrough registered under a different
# path name escapes it. String pins bound, they do not seal.
def test_route_surface_is_pinned():
    """A new passthrough route added under /substrate or /api/v1/substrate
    must show up here -- deliberately -- before it ships. If it doesn't,
    nothing in this file re-verifies its guard."""
    routes = {
        route.path: tuple(sorted(route.methods))
        for route in openapi_app.app.routes
        if "substrate" in getattr(route, "path", "")
    }
    assert routes == {
        "/api/v1/substrate/{path:path}": ("DELETE", "GET", "OPTIONS", "PATCH", "POST"),
        "/substrate/{path:path}": ("DELETE", "GET", "OPTIONS", "PATCH", "POST"),
    }


@pytest.fixture(autouse=True)
def stub_upstream(monkeypatch):
    """Every scope-admitted request in this file reaches the real
    `substrate_proxy` body, which calls tools.finance.substrate_headers()
    and then dials out over httpx. Stub both so these tests assert only the
    identity boundary -- never this developer machine's real local
    substrate, and never a real network call."""
    monkeypatch.setattr(openapi_app, "substrate_base_url", lambda: "http://substrate.test")
    monkeypatch.setattr(openapi_app, "substrate_headers", lambda: {"X-API-Key": "stub-key"})

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"[]"

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(openapi_app.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.parametrize("path", PROXY_PATHS)
@pytest.mark.parametrize("method", PROXY_METHODS)
def test_denied_for_authenticated_client_without_scope(path, method):
    with TestClient(openapi_app.app) as client:
        response = client.request(method, path, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "substrate.proxy" in response.text


@pytest.mark.parametrize("path", PROXY_PATHS)
@pytest.mark.parametrize("method", PROXY_METHODS)
def test_allowed_for_human_wildcard_like_the_console(path, method):
    # So the check cannot pass by refusing everything: the identity the
    # console's own traffic carries must keep working, unchanged.
    with TestClient(openapi_app.app) as client:
        response = client.request(method, path, headers=HUMAN)
    assert response.status_code == 200


@pytest.mark.parametrize("path", PROXY_PATHS)
@pytest.mark.parametrize("method", PROXY_METHODS)
def test_allowed_for_service_explicitly_granted_the_scope(path, method):
    with TestClient(openapi_app.app) as client:
        response = client.request(method, path, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200


@pytest.mark.parametrize("path", PROXY_PATHS)
def test_denied_without_any_identity(path):
    """A headerless HTTP request is what mcp-hub sees when a caller reaches
    it without passing through Traefik's ForwardAuth -- not a trusted
    local/stdio call. `require_authenticated_scope` (access_auth.py) never
    grants the internal/unknown bypass `check_scope` does, so this must be
    refused, the same shape `test_factory_router_access.py::
    test_denied_without_any_identity` pins for the factory router (the
    #620 fix this bead (#661 follow-up) applies to substrate_proxy).
    """
    with TestClient(openapi_app.app) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert "Truline identity" in response.text
