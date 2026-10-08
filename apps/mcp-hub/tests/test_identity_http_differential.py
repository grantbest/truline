"""#1052 gate required change 7 -- AC-8 (g)/(i)-enforce, kept in their own
file, deliberately HTTP-only.

Every other test for this bead (test_identity_verification.py) imports the
new access_auth symbols this bead adds (MODE_ENFORCE, VERDICT_*,
resolve_request_identity, IDENTITY_MODE_ENV, ...), several as *default
argument values* evaluated at collection time -- so importing that file
against a pre-bead checkout raises an ImportError/AttributeError before a
single test runs, which tells a reader nothing about whether the finding is
real.

This file avoids that: it references only entry points that already existed
before this bead (access_issuer, fetch_access_jwks, identity_from_claims /
identity_headers -- all used by the pre-existing /auth/cloudflare
ForwardAuth route), and spells the mode env var and its values as plain
string literals rather than access_auth constants. Collected against a
pre-bead checkout, this file still imports cleanly, and its two "-enforce"
tests fail on a status-code assertion instead -- see the PR body for that
run's actual output.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

import access_auth
import openapi_app
import routers.v1.finance

MODE_ENV = "MCP_HUB_IDENTITY_MODE"
MODE_ENFORCE = "enforce"
MODE_LOG_ONLY = "log-only"

TEAM_DOMAIN = "truline"
ACCEPTED_AUD = "aud-mcp"
KID = "http-differential-key"

# dev.finding 4208f26f / 52deaf69's own attack shape: the full forged set,
# no assertion behind it.
FORGED = {
    "X-Truline-Client": "attacker@example",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}


def _generate_rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _generate_rsa_keypair()


def _jwks() -> dict:
    key = jose_jwk.construct(PUBLIC_PEM, algorithm="RS256")
    data = dict(key.to_dict())
    data["kid"] = KID
    data["use"] = "sig"
    data["alg"] = "RS256"
    return {"keys": [data]}


def _service_token(common_name: str) -> str:
    now = int(time.time())
    claims = {
        "iss": access_auth.access_issuer(TEAM_DOMAIN),
        "aud": ACCEPTED_AUD,
        "common_name": common_name,
        "iat": now,
        "exp": now + 3600,
    }
    return jose_jwt.encode(claims, PRIVATE_PEM, algorithm="RS256", headers={"kid": KID})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUDS", ACCEPTED_AUD)
    monkeypatch.delenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    # This file is deliberately collectable against a pre-bead checkout (see
    # the module docstring), where the resolver's test-reset hook does not
    # exist yet -- getattr's fallback keeps that true while still clearing
    # the resolver's negative cache / kid-miss rate limit / single-flight
    # state between tests once the hook does exist, so one test's cached
    # failure can never flip another's outcome.
    reset = getattr(access_auth, "_reset_resolver_state_for_tests", lambda: None)
    reset()
    yield
    reset()


@pytest.fixture
def fake_jwks(monkeypatch):
    async def _fetch(team_domain, *, force=False):
        return _jwks()

    monkeypatch.setattr(access_auth, "fetch_access_jwks", _fetch)


class _FakeSubstrateResponse:
    status_code = 200
    headers = {"content-type": "application/json"}
    content = b"[]"


class _FakeSubstrateAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        return _FakeSubstrateResponse()


@pytest.fixture
def fake_substrate_upstream(monkeypatch):
    monkeypatch.setattr(openapi_app, "substrate_base_url", lambda: "http://substrate.test")
    monkeypatch.setattr(openapi_app, "substrate_headers", lambda: {"X-API-Key": "stub-key"})
    monkeypatch.setattr(openapi_app.httpx, "AsyncClient", _FakeSubstrateAsyncClient)


# ---------------------------------------------------------------------------
# AC-8 (g): the finding's own attack -- full forged headers, no assertion.
# ---------------------------------------------------------------------------


def test_g_forged_headers_no_assertion_log_only_reaches_substrate(monkeypatch, fake_substrate_upstream):
    monkeypatch.setenv(MODE_ENV, MODE_LOG_ONLY)

    with TestClient(openapi_app.app) as client:
        resp = client.get("/api/v1/substrate/beads", headers=FORGED)

    assert resp.status_code == 200, (
        "pinning today's (pre-fix) behaviour -- log-only must change no outcome -- "
        f"got {resp.status_code}: {resp.text}"
    )


def test_g_forged_headers_no_assertion_enforce_refuses_substrate(monkeypatch, fake_substrate_upstream):
    # fake_substrate_upstream is required even though this is expected to be
    # refused before the route runs: run against a pre-fix/baseline
    # checkout (or a regression), the request is admitted and would
    # otherwise reach the REAL substrate backend -- no test may touch the
    # network (AC-8), regardless of the outcome under test.
    monkeypatch.setenv(MODE_ENV, MODE_ENFORCE)

    with TestClient(openapi_app.app) as client:
        resp = client.get("/api/v1/substrate/beads", headers=FORGED)

    assert resp.status_code == 401, (
        "dev.finding 52deaf69(b): a request with the full forged X-Truline-* "
        "header set and no verified Cloudflare Access assertion must be refused "
        f"once MCP_HUB_IDENTITY_MODE=enforce -- got {resp.status_code}: {resp.text}"
    )


def test_g_forged_headers_no_assertion_enforce_refuses_check_scope_route(monkeypatch):
    monkeypatch.setenv(MODE_ENV, MODE_ENFORCE)
    # Mocked regardless of expected outcome -- see fake_substrate_upstream's
    # rationale above: an admittted request must never reach a real upstream
    # (here, get_expenses's own backing store) (AC-8).
    get_expenses_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(routers.v1.finance, "get_expenses", get_expenses_mock)

    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/finance/get_expenses", headers=FORGED, json={})

    assert resp.status_code == 401, (
        "a check_scope route must also refuse forged headers with no assertion "
        f"in enforce mode -- got {resp.status_code}: {resp.text}"
    )
    get_expenses_mock.assert_not_called()


def test_g_forged_headers_no_assertion_enforce_refuses_mcp(monkeypatch):
    monkeypatch.setenv(MODE_ENV, MODE_ENFORCE)

    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.0.1"},
                },
            },
            headers={**FORGED, "Accept": "application/json, text/event-stream"},
        )

    assert resp.status_code == 401, (
        "the mounted MCP surface shares the same middleware and must refuse "
        f"forged headers in enforce mode too -- got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# AC-8 (i): a valid service-token assertion, PLUS the full forged header set.
# ---------------------------------------------------------------------------


def test_i_service_token_plus_forged_headers_log_only_reaches_substrate(
    monkeypatch, fake_jwks, fake_substrate_upstream
):
    monkeypatch.setenv(MODE_ENV, MODE_LOG_ONLY)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=substrate.proxy")

    token = _service_token("svc.access")
    with TestClient(openapi_app.app) as client:
        resp = client.get(
            "/api/v1/substrate/beads",
            headers={"Cf-Access-Jwt-Assertion": token, **FORGED},
        )

    assert resp.status_code == 200, (
        "pinning today's (pre-fix) behaviour -- log-only keeps the header identity "
        f"even with a valid but mismatched assertion present -- got {resp.status_code}: {resp.text}"
    )


def test_i_service_token_plus_forged_headers_enforce_refuses_substrate(
    monkeypatch, fake_jwks, fake_substrate_upstream
):
    # fake_substrate_upstream is required for the same reason as in the (g)
    # enforce test above: an admitted request must never be able to reach
    # the real substrate backend, regardless of the outcome under test.
    monkeypatch.setenv(MODE_ENV, MODE_ENFORCE)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=finance.read")

    token = _service_token("svc.access")
    with TestClient(openapi_app.app) as client:
        resp = client.get(
            "/api/v1/substrate/beads",
            headers={"Cf-Access-Jwt-Assertion": token, **FORGED},
        )

    assert resp.status_code == 403, (
        "dev.finding 52deaf69(b): once enforced, the token's own mapped scopes "
        "govern -- this service token is not granted substrate.proxy, so the "
        f"forged X-Truline-Scopes: * must not win -- got {resp.status_code}: {resp.text}"
    )
