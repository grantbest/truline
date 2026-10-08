"""dev.finding 52deaf69(b) + 4208f26f -- mcp-hub takes caller identity only
from what it can verify (a re-verified Cloudflare Access assertion), and an
HTTP request with no verified identity no longer passes check_scope once
MCP_HUB_IDENTITY_MODE=enforce.

Everything here is in-process. An RSA key pair is generated once at import
and served as the JWKS either through a monkeypatched `httpx.AsyncClient`
(so `fetch_access_jwks`'s own real caching participates -- see the AC-6
tests) or a monkeypatched `fetch_access_jwks` itself (when caching behavior
is not what's under test) or an injected `verifier=` callable (bypassing
JWKS entirely). No test reaches Cloudflare, the substrate, or any real
network. No key material is committed -- both key pairs are generated fresh
at test-session import time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt
from starlette.datastructures import Headers

import access_auth
import openapi_app
import routers.v1.context
import routers.v1.finance
import routers.v1.ha

# ---------------------------------------------------------------------------
# Key material and token/JWKS builders
# ---------------------------------------------------------------------------

TEAM_DOMAIN = "truline"
ACCEPTED_AUD = "aud-mcp"
KID = "test-key-1"
OTHER_KID = "test-key-2"


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


def _jwk_for(public_pem: str, kid: str) -> dict:
    key = jose_jwk.construct(public_pem, algorithm="RS256")
    data = dict(key.to_dict())
    data["kid"] = kid
    data["use"] = "sig"
    data["alg"] = "RS256"
    return data


PRIVATE_PEM, PUBLIC_PEM = _generate_rsa_keypair()
OTHER_PRIVATE_PEM, OTHER_PUBLIC_PEM = _generate_rsa_keypair()
JWKS = {"keys": [_jwk_for(PUBLIC_PEM, KID)]}


def _make_token(claims: dict, *, kid: str = KID, private_pem: str = PRIVATE_PEM) -> str:
    return jose_jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _human_claims(email: str = "operator@example.org", aud: str = ACCEPTED_AUD, exp_delta: int = 3600) -> dict:
    now = int(time.time())
    return {
        "iss": access_auth.access_issuer(TEAM_DOMAIN),
        "aud": aud,
        "email": email,
        "iat": now,
        "exp": now + exp_delta,
    }


def _service_claims(common_name: str, aud: str = ACCEPTED_AUD, exp_delta: int = 3600) -> dict:
    now = int(time.time())
    return {
        "iss": access_auth.access_issuer(TEAM_DOMAIN),
        "aud": aud,
        "common_name": common_name,
        "iat": now,
        "exp": now + exp_delta,
    }


HUMAN_HEADERS = {
    "x-truline-client": "operator@example.org",
    "x-truline-client-type": "human",
    "x-truline-scopes": "*",
    "x-truline-email": "operator@example.org",
}

# dev.finding 4208f26f / 52deaf69's own attack shape: the full forged set,
# no assertion behind it.
FORGED = {
    "x-truline-client": "attacker@example",
    "x-truline-client-type": "human",
    "x-truline-scopes": "*",
}

LOG_EXPENSE_BODY = {"amount": 10.0, "category": "misc", "description": "test expense"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(access_auth.IDENTITY_MODE_ENV, raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUDS", ACCEPTED_AUD)
    monkeypatch.delenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", raising=False)
    access_auth._reset_resolver_state_for_tests()
    yield
    access_auth._reset_resolver_state_for_tests()


@pytest.fixture
def fake_jwks(monkeypatch):
    """Replaces fetch_access_jwks outright -- for tests where the shared
    300s success cache participating (or not) isn't what's under test.
    AC-6's own tests use `jwks_transport` below instead, which patches one
    layer lower so that cache stays live."""

    async def _fetch(team_domain, *, force=False):
        return JWKS

    monkeypatch.setattr(access_auth, "fetch_access_jwks", _fetch)


@pytest.fixture
def jwks_transport(monkeypatch):
    """Patches httpx.AsyncClient itself, so fetch_access_jwks's real 300s
    success cache stays in the loop -- required for AC-6's negative-cache
    and kid-miss-refetch tests to mean anything."""
    state = {"calls": 0, "queue": []}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            state["calls"] += 1
            if not state["queue"]:
                raise AssertionError("jwks_transport queue exhausted")
            outcome = state["queue"].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return _Resp(outcome)

    monkeypatch.setattr(access_auth.httpx, "AsyncClient", _Client)
    return state


@pytest.fixture(autouse=True)
def _stub_context_upstream(monkeypatch):
    monkeypatch.setattr(routers.v1.context, "get_current_context", lambda: {"status": "mocked"})


def _resolve(
    headers: dict,
    cookies: dict | None = None,
    *,
    client_host: str | None = "203.0.113.5",
    mode: str = access_auth.MODE_LOG_ONLY,
    verifier=None,
):
    return access_auth.resolve_request_identity(
        headers=Headers(headers=headers),
        cookies=cookies or {},
        client_host=client_host,
        mode=mode,
        verifier=verifier,
    )


# ---------------------------------------------------------------------------
# AC-1: mode handling
# ---------------------------------------------------------------------------


def test_mode_defaults_to_log_only_when_unset():
    assert access_auth.current_identity_mode() == access_auth.MODE_LOG_ONLY


def test_mode_accepts_underscore_aliases(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, "log_only")
    assert access_auth.current_identity_mode() == access_auth.MODE_LOG_ONLY
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, "trust_headers")
    assert access_auth.current_identity_mode() == access_auth.MODE_TRUST_HEADERS


def test_mode_unknown_value_is_enforce_and_logs_error(monkeypatch, caplog):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, "totally-bogus")
    caplog.set_level(logging.ERROR, logger="access_auth")
    assert access_auth.current_identity_mode() == access_auth.MODE_ENFORCE
    assert any("totally-bogus" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# AC-3: the behaviour table (direct resolver calls)
# ---------------------------------------------------------------------------


async def test_no_assertion_no_headers_is_no_identity_every_mode():
    for mode in (access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS):
        result = await _resolve({}, mode=mode)
        assert result.verdict == access_auth.VERDICT_NO_IDENTITY
        assert result.identity.client == "internal/unknown"
        assert result.rejection is None


async def test_headers_without_assertion_log_only_admits_enforce_refuses():
    log_only = await _resolve(FORGED, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS
    assert log_only.rejection is None
    assert log_only.identity.client == "attacker@example"

    enforced = await _resolve(FORGED, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS
    assert enforced.rejection is not None
    assert enforced.rejection[0] == 401


async def test_trust_headers_never_calls_verifier_and_ignores_assertion():
    async def _boom(*args, **kwargs):
        raise AssertionError("trust-headers must never call the verifier")

    token = _make_token(_human_claims())
    with_headers = await _resolve(
        {**FORGED, "cf-access-jwt-assertion": token}, mode=access_auth.MODE_TRUST_HEADERS, verifier=_boom
    )
    assert with_headers.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS

    without_headers = await _resolve(
        {"cf-access-jwt-assertion": token}, mode=access_auth.MODE_TRUST_HEADERS, verifier=_boom
    )
    assert without_headers.verdict == access_auth.VERDICT_NO_IDENTITY


async def test_empty_configured_audiences_fails_closed_only_in_enforce(fake_jwks, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUDS", "")
    token = _make_token(_human_claims(aud="anything-at-all"))

    log_only = await _resolve({"cf-access-jwt-assertion": token, **HUMAN_HEADERS}, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_VERIFIED

    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert enforced.rejection[0] == 401


# ---------------------------------------------------------------------------
# AC-8: the regression cases
# ---------------------------------------------------------------------------


async def test_valid_assertion_header_matching_headers_is_verified(fake_jwks):
    token = _make_token(_human_claims())
    headers = {"cf-access-jwt-assertion": token, **HUMAN_HEADERS}

    log_only = await _resolve(headers, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_VERIFIED
    assert log_only.identity.client == "operator@example.org"

    enforced = await _resolve(headers, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_VERIFIED
    assert enforced.identity.client == "operator@example.org"
    assert enforced.identity.scopes == "*"
    assert enforced.rejection is None


async def test_valid_assertion_cookie_matching_headers_is_verified(fake_jwks):
    token = _make_token(_human_claims())
    result = await _resolve(HUMAN_HEADERS, cookies={"CF_Authorization": token}, mode=access_auth.MODE_LOG_ONLY)
    assert result.verdict == access_auth.VERDICT_VERIFIED


async def test_expired_assertion_is_invalid(fake_jwks):
    token = _make_token(_human_claims(exp_delta=-10))
    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert enforced.rejection[0] == 401


async def test_bad_signature_under_correct_kid_is_invalid(fake_jwks):
    token = _make_token(_human_claims(), kid=KID, private_pem=OTHER_PRIVATE_PEM)
    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert enforced.rejection[0] == 401


async def test_audience_not_accepted_is_invalid(fake_jwks):
    token = _make_token(_human_claims(aud="aud-console-only"))
    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert enforced.rejection[0] == 401


async def test_kid_absent_from_jwks_is_invalid(fake_jwks):
    token = _make_token(_human_claims(), kid="totally-unknown-kid")
    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert enforced.rejection[0] == 401


async def test_full_forged_headers_no_assertion_direct(fake_jwks):
    log_only = await _resolve(FORGED, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS

    enforced = await _resolve(FORGED, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS
    assert enforced.rejection[0] == 401


async def test_no_headers_no_assertion_direct_every_mode():
    for mode in (access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS):
        result = await _resolve({}, mode=mode)
        assert result.verdict == access_auth.VERDICT_NO_IDENTITY


async def test_service_token_with_forged_headers_is_mismatch(fake_jwks, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=finance.read")
    token = _make_token(_service_claims("svc.access"))
    headers = {"cf-access-jwt-assertion": token, **FORGED}

    log_only = await _resolve(headers, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_MISMATCH
    assert log_only.identity.client == "attacker@example"  # header identity, pinned

    enforced = await _resolve(headers, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_MISMATCH
    assert enforced.identity.client == "agent-dev"
    assert enforced.identity.scopes == "finance.read"
    assert enforced.rejection is None


async def test_verifier_unavailable_log_only_and_enforce(monkeypatch):
    async def _boom(team_domain):
        raise RuntimeError("network down")

    monkeypatch.setattr(access_auth, "fetch_access_jwks", _boom)
    token = _make_token(_human_claims())

    log_only = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)
    assert log_only.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert log_only.rejection is None

    access_auth._reset_resolver_state_for_tests()
    monkeypatch.setattr(access_auth, "fetch_access_jwks", _boom)
    enforced = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_ENFORCE)
    assert enforced.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert enforced.rejection[0] == 503


# ---------------------------------------------------------------------------
# AC-6: resolver-only JWKS caching
# ---------------------------------------------------------------------------


async def test_negative_cache_avoids_refetch_within_window(jwks_transport):
    jwks_transport["queue"] = [RuntimeError("down")]
    token = _make_token(_human_claims())

    r1 = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)
    r2 = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)

    assert r1.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert r2.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert jwks_transport["calls"] == 1


async def test_forwardauth_route_unaffected_by_resolver_negative_cache(jwks_transport):
    jwks_transport["queue"] = [RuntimeError("down")]
    token = _make_token(_human_claims())

    result = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)
    assert result.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert jwks_transport["calls"] == 1

    jwks_transport["queue"] = [JWKS]
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": token})

    assert resp.status_code == 204
    assert resp.headers["x-truline-client"] == "operator@example.org"
    assert jwks_transport["calls"] == 2


async def test_kid_miss_triggers_one_refetch_then_rate_limited(jwks_transport):
    jwks_transport["queue"] = [JWKS, JWKS]
    token = _make_token(_human_claims(), kid="totally-different-kid")

    r1 = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)
    assert r1.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert jwks_transport["calls"] == 2

    r2 = await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)
    assert r2.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert jwks_transport["calls"] == 2


async def test_concurrent_cold_cache_produces_one_fetch(monkeypatch):
    event = asyncio.Event()
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _SlowClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls["n"] += 1
            await event.wait()
            return _Resp(JWKS)

    monkeypatch.setattr(access_auth.httpx, "AsyncClient", _SlowClient)
    token = _make_token(_human_claims())

    async def _one():
        return await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)

    task1 = asyncio.create_task(_one())
    task2 = asyncio.create_task(_one())
    await asyncio.sleep(0.05)
    event.set()
    r1, r2 = await asyncio.gather(task1, task2)

    assert calls["n"] == 1
    assert r1.verdict in (access_auth.VERDICT_VERIFIED, access_auth.VERDICT_MISMATCH)
    assert r2.verdict in (access_auth.VERDICT_VERIFIED, access_auth.VERDICT_MISMATCH)


# ---------------------------------------------------------------------------
# #1052 gate required change 1 (probe P3): a kid-miss refetch failure must
# never touch the shared JWKS cache ForwardAuth (/auth/cloudflare) reads --
# on 09bc1a23 the kid-miss handler zeroed _JWKS_CACHE["expires_at"] BEFORE
# the refetch was known to succeed, so a failed refetch left every public
# hostname's ForwardAuth 500ing until the next successful fetch.
# ---------------------------------------------------------------------------


async def test_kid_miss_refetch_failure_does_not_poison_shared_cache_for_forwardauth(jwks_transport):
    # 1. Warm ForwardAuth's own cache with one successful fetch.
    jwks_transport["queue"] = [JWKS]
    warm_token = _make_token(_human_claims())
    with TestClient(openapi_app.app) as client:
        warm = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": warm_token})
    assert warm.status_code == 204
    assert jwks_transport["calls"] == 1

    # 2. A resolver-path request with an unknown kid triggers the rate-limited
    # refetch, and the fetcher fails.
    jwks_transport["queue"] = [RuntimeError("kid refetch failed")]
    kid_miss_token = _make_token(_human_claims(), kid="totally-different-kid")
    result = await _resolve({"cf-access-jwt-assertion": kid_miss_token}, mode=access_auth.MODE_LOG_ONLY)
    assert result.verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE
    assert jwks_transport["calls"] == 2

    # 3. /auth/cloudflare, with a valid assertion and a fetcher that would
    # fail if called, must still return 204 from the still-live shared
    # cache -- and the fetcher must NOT be called.
    jwks_transport["queue"] = [RuntimeError("must not be called")]
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": warm_token})
    assert resp.status_code == 204
    assert resp.headers["x-truline-client"] == "operator@example.org"
    assert jwks_transport["calls"] == 2


async def test_malformed_or_kidless_assertion_never_triggers_refetch(jwks_transport):
    jwks_transport["queue"] = [JWKS]

    malformed = await _resolve({"cf-access-jwt-assertion": "not-a-jwt-at-all"}, mode=access_auth.MODE_LOG_ONLY)
    assert malformed.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert jwks_transport["calls"] == 1  # the initial (cold-cache) fetch only

    kidless_token = jose_jwt.encode(_human_claims(), PRIVATE_PEM, algorithm="RS256")
    kidless = await _resolve({"cf-access-jwt-assertion": kidless_token}, mode=access_auth.MODE_LOG_ONLY)
    assert kidless.verdict == access_auth.VERDICT_INVALID_ASSERTION
    assert jwks_transport["calls"] == 1  # still no refetch attempt


# ---------------------------------------------------------------------------
# #1052 gate required change 2 (probes P1, P2): the resolver must convert
# any exception from the verifier into a verdict, never a 500 with no audit
# record in log-only mode; and the internal-call token comparison must not
# raise on non-ASCII input.
# ---------------------------------------------------------------------------


def test_verifier_unexpected_exception_log_only_returns_200_and_logs_one_record(monkeypatch, caplog):
    async def _boom(token, *, team_domain, accepted_audiences=""):
        raise ValueError("totally unexpected verifier failure")

    monkeypatch.setattr(access_auth, "_resolver_verify_access_jwt", _boom)
    caplog.set_level(logging.INFO, logger="openapi_app")
    with TestClient(openapi_app.app) as client:
        resp = client.post(
            "/api/v1/context/current",
            headers={"Cf-Access-Jwt-Assertion": "irrelevant-token"},
        )
    assert resp.status_code == 200, resp.text

    records = [
        r for r in caplog.records if r.name == "openapi_app" and r.getMessage().startswith("mcp_hub_request")
    ]
    assert len(records) == 1
    assert records[-1].audit_identity_verdict == access_auth.VERDICT_VERIFIER_UNAVAILABLE


def test_verifier_unexpected_exception_enforce_fails_closed_503(monkeypatch):
    async def _boom(token, *, team_domain, accepted_audiences=""):
        raise ValueError("totally unexpected verifier failure")

    monkeypatch.setattr(access_auth, "_resolver_verify_access_jwt", _boom)
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    with TestClient(openapi_app.app) as client:
        resp = client.post(
            "/api/v1/context/current",
            headers={"Cf-Access-Jwt-Assertion": "irrelevant-token"},
        )
    assert resp.status_code == 503
    assert "verifier-unavailable" in resp.text


async def test_non_ascii_internal_call_header_is_rejected_not_typeerror():
    result = await _resolve(
        {"x-truline-internal-call": "tökén-not-ascii", **FORGED},
        client_host="127.0.0.1",
        mode=access_auth.MODE_LOG_ONLY,
    )
    assert result.verdict == access_auth.VERDICT_INTERNAL_CALL_REJECTED


# ---------------------------------------------------------------------------
# #1052 gate required change 3 (probe P4): cancelling one coalesced
# resolver-path request must not cancel the shared JWKS fetch other
# concurrent requests are also waiting on.
# ---------------------------------------------------------------------------


async def test_cancelling_one_coalesced_request_does_not_cancel_the_other(monkeypatch):
    event = asyncio.Event()
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _SlowClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            calls["n"] += 1
            await event.wait()
            return _Resp(JWKS)

    monkeypatch.setattr(access_auth.httpx, "AsyncClient", _SlowClient)
    token = _make_token(_human_claims())

    async def _one():
        return await _resolve({"cf-access-jwt-assertion": token}, mode=access_auth.MODE_LOG_ONLY)

    task1 = asyncio.create_task(_one())
    task2 = asyncio.create_task(_one())
    await asyncio.sleep(0.05)
    task1.cancel()
    event.set()

    with pytest.raises(asyncio.CancelledError):
        await task1

    r2 = await task2
    assert calls["n"] == 1
    assert r2.verdict in (access_auth.VERDICT_VERIFIED, access_auth.VERDICT_MISMATCH)


# ---------------------------------------------------------------------------
# #1052 gate required change 4 (AC-4): /auth/cloudflare's own behaviour must
# be identical across every MCP_HUB_IDENTITY_MODE value -- it verifies the
# assertion itself and is excluded from resolve_request_identity, so no mode
# should change it. This goes red if that exclusion is ever removed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS])
def test_auth_cloudflare_missing_assertion_rejected_in_every_mode(monkeypatch, mode):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare")
    assert resp.status_code == 403


@pytest.mark.parametrize("mode", [access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS])
def test_auth_cloudflare_valid_assertion_sets_headers_in_every_mode(monkeypatch, mode, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
    token = _make_token(_human_claims())
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 204
    assert resp.headers["x-truline-client"] == "operator@example.org"
    assert resp.headers["x-truline-client-type"] == "human"
    assert resp.headers["x-truline-email"] == "operator@example.org"


# #1062 gate MERGE-WITH-CHANGES at d4081a09, required change 1: the two tests
# above pass under a mutation that routes /auth/cloudflare through
# resolve_request_identity too (a missing assertion still 403s -- the route's
# own check fires either way -- and a valid assertion still 204s, since a
# `verified`/`mismatch` verdict never produces a resolver rejection). These
# two close that gap: a bad/expired assertion, present on the request, DOES
# produce a resolver rejection (`invalid-assertion`, 401 in enforce mode) --
# so if that mutation is applied, the enforce-mode case here observes 401
# instead of the route's own 403. The dedicated spy test below is the direct
# check: it fails under the mutation regardless of status-code coincidences.
@pytest.mark.parametrize("mode", [access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS])
def test_auth_cloudflare_garbage_assertion_rejected_in_every_mode(monkeypatch, mode, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": "not-a-jwt-at-all"})
    assert resp.status_code == 403


@pytest.mark.parametrize("mode", [access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS])
def test_auth_cloudflare_expired_assertion_rejected_in_every_mode(monkeypatch, mode, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
    token = _make_token(_human_claims(exp_delta=-3600))
    with TestClient(openapi_app.app) as client:
        resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 403


@pytest.mark.parametrize("mode", [access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS])
def test_auth_cloudflare_never_calls_the_resolver(monkeypatch, mode, fake_jwks):
    """The direct check: /auth/cloudflare verifies its own assertion and must
    never be routed through resolve_request_identity, in any mode -- doing so
    would double the verification per public request and turn its own 403
    into a 401 (AC-4). Spies on the name openapi_app's dispatch actually
    calls (bound into that module's namespace by its own `from access_auth
    import resolve_request_identity`), so this fails if the dispatch's
    `/auth/cloudflare` exclusion is ever removed -- independent of whether a
    given request's status code happens to coincide either way.
    """
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
    calls = {"n": 0}
    original = openapi_app.resolve_request_identity

    async def _spy(**kwargs):
        calls["n"] += 1
        return await original(**kwargs)

    monkeypatch.setattr(openapi_app, "resolve_request_identity", _spy)
    with TestClient(openapi_app.app) as client:
        client.get("/auth/cloudflare")
        client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": "not-a-jwt-at-all"})
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# AC-7: the probe's own loopback credential
# ---------------------------------------------------------------------------


async def test_internal_call_right_token_from_loopback():
    result = await _resolve(
        {
            "x-truline-internal-call": access_auth.PROCESS_INTERNAL_CALL_TOKEN,
            "x-truline-client": "probe",
            "x-truline-client-type": "service",
            "x-truline-scopes": "factory.read",
        },
        client_host="127.0.0.1",
        mode=access_auth.MODE_ENFORCE,
    )
    assert result.verdict == access_auth.VERDICT_INTERNAL_CALL
    assert result.identity.client == "probe"
    assert result.rejection is None


async def test_internal_call_right_token_from_non_loopback_is_rejected():
    log_only = await _resolve(
        {"x-truline-internal-call": access_auth.PROCESS_INTERNAL_CALL_TOKEN, **FORGED},
        client_host="10.0.0.5",
        mode=access_auth.MODE_LOG_ONLY,
    )
    assert log_only.verdict == access_auth.VERDICT_INTERNAL_CALL_REJECTED

    enforced = await _resolve(
        {"x-truline-internal-call": access_auth.PROCESS_INTERNAL_CALL_TOKEN, **FORGED},
        client_host="10.0.0.5",
        mode=access_auth.MODE_ENFORCE,
    )
    assert enforced.verdict == access_auth.VERDICT_INTERNAL_CALL_REJECTED
    assert enforced.rejection[0] == 401


async def test_internal_call_wrong_token_from_loopback_is_rejected():
    result = await _resolve(
        {"x-truline-internal-call": "definitely-the-wrong-token", **FORGED},
        client_host="127.0.0.1",
        mode=access_auth.MODE_LOG_ONLY,
    )
    assert result.verdict == access_auth.VERDICT_INTERNAL_CALL_REJECTED


async def test_tokenless_loopback_with_forged_headers_is_unverified_headers():
    # Loopback alone grants nothing -- kubectl port-forward also arrives on
    # loopback, so only the token (not the address) is a credential.
    result = await _resolve(FORGED, client_host="127.0.0.1", mode=access_auth.MODE_LOG_ONLY)
    assert result.verdict == access_auth.VERDICT_UNVERIFIED_HEADERS


def test_dockerfile_cmd_has_no_workers_flag():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text()
    cmd_lines = [line for line in content.splitlines() if line.strip().startswith("CMD")]
    assert cmd_lines, "apps/mcp-hub/Dockerfile's CMD line could not be found"
    assert "--workers" not in cmd_lines[0], (
        "AC-7: apps/mcp-hub/Dockerfile's CMD now passes --workers. "
        "access_auth.PROCESS_INTERNAL_CALL_TOKEN is generated once per "
        "process and is valid only within it -- a multi-worker deployment "
        "needs a new design (a shared credential, or per-worker discovery) "
        "before the probe's loopback call can keep working across workers."
    )


# ---------------------------------------------------------------------------
# AC-4: where the resolver runs (the shared dispatch, FastAPI + /mcp)
# ---------------------------------------------------------------------------


def test_enforce_mode_401_on_fastapi_route_with_forged_headers(monkeypatch, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/context/current", headers=FORGED)
    assert resp.status_code == 401
    assert "unverified-headers" in resp.text


def test_enforce_mode_401_on_mcp_route_with_forged_headers(monkeypatch, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
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
    assert resp.status_code == 401
    assert "unverified-headers" in resp.text


def test_enforce_mode_mcp_finance_read_tool_succeeds_with_valid_assertion(monkeypatch, fake_jwks, caplog):
    """AC-4: the header form is required -- the installed fastmcp (4.0.10
    measured in this environment) does not forward the CF_Authorization
    cookie to the inner in-process tool call, only the header. If the
    installed fastmcp ever stops forwarding the header too, this test fails
    on the tool call's status/identity, not on an ImportError, and the PR
    body must say so rather than weaken this test.
    """
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    monkeypatch.setattr(routers.v1.finance, "get_expenses", AsyncMock(return_value=[]))
    token = _make_token(_human_claims())
    human_headers = {
        "Cf-Access-Jwt-Assertion": token,
        "X-Truline-Client": "operator@example.org",
        "X-Truline-Client-Type": "human",
        "X-Truline-Scopes": "*",
        "X-Truline-Email": "operator@example.org",
    }

    caplog.set_level(logging.INFO, logger="openapi_app")
    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        init_resp = client.post(
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
            headers={**human_headers, "Accept": "application/json, text/event-stream"},
        )
        assert init_resp.status_code == 200, init_resp.text
        session_headers = {
            **human_headers,
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": init_resp.headers["mcp-session-id"],
        }
        notif_resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        assert notif_resp.status_code == 202, notif_resp.text

        call_resp = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_expenses", "arguments": {"month": "current", "limit": 10}},
            },
            headers=session_headers,
        )
    assert call_resp.status_code == 200, call_resp.text

    tool_records = [
        r
        for r in caplog.records
        if r.name == "openapi_app"
        and r.getMessage().startswith("mcp_hub_request")
        and getattr(r, "audit_capability", None) == "get_expenses"
    ]
    assert tool_records, (
        "no mcp_hub_request record attributed the inner call to get_expenses -- "
        f"capabilities seen: {[getattr(r, 'audit_capability', None) for r in caplog.records]!r}"
    )
    assert getattr(tool_records[-1], "audit_identity_verdict", None) == access_auth.VERDICT_VERIFIED, (
        "the inner in-process request fastmcp makes must resolve as verified, "
        f"not {getattr(tool_records[-1], 'audit_identity_verdict', None)!r}"
    )


def test_rejection_still_emits_audit_record(monkeypatch, fake_jwks, caplog):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    caplog.set_level(logging.INFO, logger="openapi_app")
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/context/current", headers=FORGED)
    assert resp.status_code == 401
    records = [
        r for r in caplog.records if r.name == "openapi_app" and r.getMessage().startswith("mcp_hub_request")
    ]
    assert records
    record = records[-1]
    assert record.audit_outcome == "4xx"
    assert record.audit_identity_verdict == access_auth.VERDICT_UNVERIFIED_HEADERS


# ---------------------------------------------------------------------------
# AC-5: log/audit hygiene
# ---------------------------------------------------------------------------


def _assert_no_secret_leak(records, secrets) -> None:
    """Checks getMessage(), the raw %-args, every audit_* extra attribute,
    and any formatted exc_info text -- not just the rendered message -- since
    this bead added logger.exception() calls whose traceback text must also
    never carry a secret value."""
    audit_extra_keys = (
        "audit_identity",
        "audit_capability",
        "audit_scope",
        "audit_outcome",
        "audit_identity_verdict",
        "audit_client_host",
    )
    for record in records:
        message = record.getMessage()
        for secret in secrets:
            assert secret not in message, f"log record leaked a secret value in its message: {record.name} {message!r}"
        for arg in record.args or ():
            arg_text = str(arg)
            for secret in secrets:
                assert secret not in arg_text, f"log record leaked a secret value in its args: {record.name} {arg!r}"
        for key in audit_extra_keys:
            value = getattr(record, key, None)
            if value is None:
                continue
            value_text = str(value)
            for secret in secrets:
                assert secret not in value_text, (
                    f"log record leaked a secret value in extra {key!r}: {record.name} {value!r}"
                )
        if record.exc_info:
            exc_text = logging.Formatter().formatException(record.exc_info)
            for secret in secrets:
                assert secret not in exc_text, f"log record leaked a secret value in its exc_info text: {record.name}"


def test_no_log_record_ever_contains_secret_values(monkeypatch, fake_jwks, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(routers.v1.finance, "get_expenses", AsyncMock(return_value=[]))
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=finance.read")

    valid_token = _make_token(_human_claims())
    expired_token = _make_token(_human_claims(exp_delta=-10))
    service_token = _make_token(_service_claims("svc.access"))
    secrets_to_check = [valid_token, expired_token, service_token, access_auth.PROCESS_INTERNAL_CALL_TOKEN]

    human_headers = {"Cf-Access-Jwt-Assertion": valid_token, **HUMAN_HEADERS}

    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        client.get("/health")
        client.post("/api/v1/context/current", headers=human_headers)  # verified
        # verified via the CF_Authorization cookie, not the header
        client.post("/api/v1/context/current", cookies={"CF_Authorization": valid_token})
        client.post(  # invalid-assertion
            "/api/v1/context/current", headers={**human_headers, "Cf-Access-Jwt-Assertion": expired_token}
        )
        client.post("/api/v1/context/current", headers=FORGED)  # unverified-headers
        client.post("/api/v1/context/current")  # no-identity
        client.post(  # mismatch: valid service-token assertion, forged headers on top
            "/api/v1/context/current", headers={"Cf-Access-Jwt-Assertion": service_token, **FORGED}
        )
        client.post(  # internal-call
            "/api/v1/context/current",
            headers={
                "X-Truline-Internal-Call": access_auth.PROCESS_INTERNAL_CALL_TOKEN,
                "X-Truline-Client": "probe",
                "X-Truline-Client-Type": "service",
                "X-Truline-Scopes": "context.read",
            },
        )
        client.post(  # internal-call-rejected: wrong token, from loopback
            "/api/v1/context/current", headers={"X-Truline-Internal-Call": "wrong-token", **FORGED}
        )
        client.get("/auth/cloudflare")
        client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": valid_token})

        async def _boom(team_domain, *, force=False):
            raise RuntimeError("network down")

        monkeypatch.setattr(access_auth, "fetch_access_jwks", _boom)
        client.post("/api/v1/context/current", headers={"Cf-Access-Jwt-Assertion": valid_token})  # verifier-unavailable
        access_auth._reset_resolver_state_for_tests()

    _assert_no_secret_leak(caplog.records, secrets_to_check)


def test_no_log_record_ever_contains_secret_values_in_enforce_mode_or_mcp(monkeypatch, fake_jwks, caplog):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    monkeypatch.setattr(routers.v1.finance, "get_expenses", AsyncMock(return_value=[]))
    caplog.set_level(logging.DEBUG)

    valid_token = _make_token(_human_claims())
    expired_token = _make_token(_human_claims(exp_delta=-10))
    secrets_to_check = [valid_token, expired_token, access_auth.PROCESS_INTERNAL_CALL_TOKEN]

    human_headers = {
        "Cf-Access-Jwt-Assertion": valid_token,
        "X-Truline-Client": "operator@example.org",
        "X-Truline-Client-Type": "human",
        "X-Truline-Scopes": "*",
        "X-Truline-Email": "operator@example.org",
    }

    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        client.post("/api/v1/context/current", headers=FORGED)  # 401, unverified-headers
        client.post(  # 401, invalid-assertion
            "/api/v1/context/current", headers={**human_headers, "Cf-Access-Jwt-Assertion": expired_token}
        )

        init_resp = client.post(
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
            headers={**human_headers, "Accept": "application/json, text/event-stream"},
        )
        assert init_resp.status_code == 200, init_resp.text
        session_headers = {
            **human_headers,
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": init_resp.headers["mcp-session-id"],
        }
        client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_expenses", "arguments": {"month": "current", "limit": 10}},
            },
            headers=session_headers,
        )

    _assert_no_secret_leak(caplog.records, secrets_to_check)


def test_substrate_proxy_drops_internal_call_header_upstream(monkeypatch):
    monkeypatch.setattr(openapi_app, "substrate_base_url", lambda: "http://substrate.test")
    monkeypatch.setattr(openapi_app, "substrate_headers", lambda: {"X-API-Key": "stub-key"})

    captured = {}

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
            captured["headers"] = kwargs.get("headers", {})
            return _FakeResponse()

    monkeypatch.setattr(openapi_app.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(openapi_app.app) as client:
        resp = client.get(
            "/api/v1/substrate/beads",
            headers={
                "X-Truline-Client": "operator@example.org",
                "X-Truline-Client-Type": "human",
                "X-Truline-Scopes": "*",
                "X-Truline-Internal-Call": access_auth.PROCESS_INTERNAL_CALL_TOKEN,
            },
        )
    assert resp.status_code == 200
    forwarded = {k.lower() for k in captured["headers"]}
    assert "x-truline-internal-call" not in forwarded
    # The server's own substrate_headers() key is expected to be present --
    # this only pins that the *client-forwarded* internal-call header never
    # reaches the upstream store.
    assert captured["headers"]["X-API-Key"] == "stub-key"


# ---------------------------------------------------------------------------
# AC-11: the no-header bypass (dev.finding 4208f26f)
# ---------------------------------------------------------------------------


def test_no_header_finance_write_log_only_admits_and_records_no_identity(monkeypatch, caplog):
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    caplog.set_level(logging.INFO, logger="openapi_app")
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY)
    assert resp.status_code == 200
    log_expense_mock.assert_called_once()
    records = [
        r for r in caplog.records if r.name == "openapi_app" and r.getMessage().startswith("mcp_hub_request")
    ]
    assert records
    record = records[-1]
    assert record.audit_identity_verdict == access_auth.VERDICT_NO_IDENTITY
    assert record.audit_scope == "finance.write"


def test_no_header_finance_write_trust_headers_admits(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_TRUST_HEADERS)
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY)
    assert resp.status_code == 200
    log_expense_mock.assert_called_once()


def test_no_header_finance_write_refused_in_enforce(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY)
    assert resp.status_code == 401
    log_expense_mock.assert_not_called()


def test_no_header_finance_read_refused_in_enforce(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    get_expenses_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(routers.v1.finance, "get_expenses", get_expenses_mock)
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/finance/get_expenses", json={})
    assert resp.status_code == 401
    get_expenses_mock.assert_not_called()


def test_no_header_ha_read_route_refused_in_enforce(monkeypatch):
    # The automations worker's call shape: no identity headers at all.
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    ha_mock = AsyncMock(return_value={})
    monkeypatch.setattr(routers.v1.ha, "ha_get_state", ha_mock)
    with TestClient(openapi_app.app) as client:
        resp = client.post("/api/v1/ha/get_state", json={"entity_id": "person.grant_best"})
    assert resp.status_code == 401
    ha_mock.assert_not_called()


def test_check_scope_no_request_context_never_raises_in_any_mode(monkeypatch):
    for mode in (access_auth.MODE_LOG_ONLY, access_auth.MODE_ENFORCE, access_auth.MODE_TRUST_HEADERS):
        monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, mode)
        access_auth.check_scope("finance.write")  # should not raise


def test_finance_write_route_function_direct_call_outside_request(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    monkeypatch.setattr(routers.v1.finance, "log_expense", AsyncMock(return_value={"id": "direct"}))
    result = asyncio.run(
        routers.v1.finance.finance_log_expense(routers.v1.finance.LogExpenseRequest(**LOG_EXPENSE_BODY))
    )
    assert result == {"id": "direct"}


def test_health_ok_in_enforce_with_no_headers(monkeypatch):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    with TestClient(openapi_app.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200


def test_valid_assertion_finance_write_admitted_for_human_in_enforce(monkeypatch, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    token = _make_token(_human_claims())
    with TestClient(openapi_app.app) as client:
        resp = client.post(
            "/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY, headers={"Cf-Access-Jwt-Assertion": token}
        )
    assert resp.status_code == 200
    log_expense_mock.assert_called_once()


def test_valid_assertion_finance_write_admitted_for_service_with_scope(monkeypatch, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=finance.write")
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    token = _make_token(_service_claims("svc.access"))
    with TestClient(openapi_app.app) as client:
        resp = client.post(
            "/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY, headers={"Cf-Access-Jwt-Assertion": token}
        )
    assert resp.status_code == 200
    log_expense_mock.assert_called_once()


def test_valid_assertion_finance_write_refused_for_service_without_scope(monkeypatch, fake_jwks):
    monkeypatch.setenv(access_auth.IDENTITY_MODE_ENV, access_auth.MODE_ENFORCE)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "svc.access=agent-dev")
    monkeypatch.setenv("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "agent-dev=finance.read")
    log_expense_mock = AsyncMock(return_value={"id": "x"})
    monkeypatch.setattr(routers.v1.finance, "log_expense", log_expense_mock)
    token = _make_token(_service_claims("svc.access"))
    with TestClient(openapi_app.app) as client:
        resp = client.post(
            "/api/v1/finance/log_expense", json=LOG_EXPENSE_BODY, headers={"Cf-Access-Jwt-Assertion": token}
        )
    assert resp.status_code == 403
    log_expense_mock.assert_not_called()
