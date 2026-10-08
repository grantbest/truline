import pytest
from fastapi.testclient import TestClient

import access_auth
import openapi_app


def test_normalize_team_domain_accepts_team_slug():
    assert access_auth.normalize_team_domain("truline") == "truline.cloudflareaccess.com"


def test_validate_access_claims_rejects_wrong_audience():
    with pytest.raises(access_auth.AccessAuthError, match="audience"):
        access_auth.validate_access_claims(
            {"aud": ["aud-console"]},
            "aud-mcp,aud-substrate",
        )


def test_validate_access_claims_allows_matching_audience():
    access_auth.validate_access_claims(
        {"aud": ["aud-console", "aud-shared"]},
        "aud-shared",
    )


def test_identity_from_claims_maps_service_token():
    identity = access_auth.identity_from_claims(
        {"common_name": "abc.access"},
        service_token_names={"abc.access": "pipeline-probe"},
        service_token_scopes={"pipeline-probe": "probe.read"},
    )

    assert identity.client == "pipeline-probe"
    assert identity.client_type == "service"
    assert identity.scopes == "probe.read"


def test_identity_from_claims_maps_human_email():
    identity = access_auth.identity_from_claims(
        {"email": "operator@example.org"},
        fallback_email="ignored@example.com",
    )

    assert identity.client == "operator@example.org"
    assert identity.client_type == "human"
    assert identity.email == "operator@example.org"


def test_cloudflare_forward_auth_rejects_missing_assertion():
    client = TestClient(openapi_app.app)
    resp = client.get("/auth/cloudflare")

    assert resp.status_code == 403


def test_cloudflare_forward_auth_sets_identity_headers(monkeypatch):
    async def fake_verify_access_jwt(token, *, team_domain, accepted_audiences):
        assert token == "signed-token"
        assert team_domain == "truline"
        assert accepted_audiences == "aud-console"
        return {"email": "operator@example.org", "aud": ["aud-console"]}

    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUDS", "aud-console")
    monkeypatch.setattr(openapi_app, "verify_access_jwt", fake_verify_access_jwt)

    client = TestClient(openapi_app.app)
    resp = client.get("/auth/cloudflare", headers={"Cf-Access-Jwt-Assertion": "signed-token"})

    assert resp.status_code == 204
    assert resp.headers["x-truline-client"] == "operator@example.org"
    assert resp.headers["x-truline-client-type"] == "human"
    assert resp.headers["x-truline-email"] == "operator@example.org"


def test_check_scope_no_context():
    # Bypassed when context is not set
    access_auth.check_scope("finance.read")  # should not raise


def test_check_scope_internal_client():
    # Bypassed for internal/unknown client (local dev)
    identity = access_auth.AccessIdentity(client="internal/unknown", client_type="internal")
    token = access_auth.current_client_identity.set(identity)
    try:
        access_auth.check_scope("finance.read")  # should not raise
    finally:
        access_auth.current_client_identity.reset(token)


def test_check_scope_missing_scope():
    identity = access_auth.AccessIdentity(client="my-agent", client_type="service", scopes="homelab.read")
    token = access_auth.current_client_identity.set(identity)
    try:
        with pytest.raises(access_auth.HTTPException) as excinfo:
            access_auth.check_scope("finance.read")
        assert excinfo.value.status_code == 403
    finally:
        access_auth.current_client_identity.reset(token)


def test_check_scope_correct_scope():
    identity = access_auth.AccessIdentity(client="my-agent", client_type="service", scopes="finance.read,homelab.read")
    token = access_auth.current_client_identity.set(identity)
    try:
        access_auth.check_scope("finance.read")  # should not raise
    finally:
        access_auth.current_client_identity.reset(token)


def test_check_scope_human_wildcard():
    # Humans get admin wildcard scopes automatically
    identity = access_auth.AccessIdentity(client="operator@example.org", client_type="human", scopes="")
    headers = access_auth.identity_headers(identity)
    identity_with_scopes = access_auth.AccessIdentity(
        client=headers["X-Truline-Client"],
        client_type=headers["X-Truline-Client-Type"],
        scopes=headers.get("X-Truline-Scopes", ""),
    )
    token = access_auth.current_client_identity.set(identity_with_scopes)
    try:
        access_auth.check_scope("finance.read")  # should not raise
        access_auth.check_scope("finance.write")  # should not raise
    finally:
        access_auth.current_client_identity.reset(token)


def test_require_authenticated_scope_denies_internal_unknown_over_http():
    # This is what a request that reached mcp-hub with no X-Truline-* headers
    # collapses to (see _identity_logging_dispatch's defaults) -- an HTTP
    # request that bypassed Traefik's ForwardAuth, not a trusted local call.
    identity = access_auth.AccessIdentity(client="internal/unknown", client_type="internal")
    token = access_auth.current_client_identity.set(identity)
    try:
        with pytest.raises(access_auth.HTTPException) as excinfo:
            access_auth.require_authenticated_scope("factory.read")
        assert excinfo.value.status_code == 401
    finally:
        access_auth.current_client_identity.reset(token)


def test_require_authenticated_scope_refuses_none_context():
    # Fail closed (release-gate finding on #620): every HTTP request passes
    # the identity middleware, which always sets an identity, and nothing
    # outside the HTTP routers calls this function -- so a None context means
    # the middleware did not run, never a trusted local call.
    with pytest.raises(access_auth.HTTPException) as exc:
        access_auth.require_authenticated_scope("factory.read")
    assert exc.value.status_code == 401


def test_require_authenticated_scope_allows_real_identity_with_scope():
    identity = access_auth.AccessIdentity(client="my-agent", client_type="service", scopes="factory.read")
    token = access_auth.current_client_identity.set(identity)
    try:
        access_auth.require_authenticated_scope("factory.read")  # should not raise
    finally:
        access_auth.current_client_identity.reset(token)


def test_require_authenticated_scope_denies_real_identity_without_scope():
    identity = access_auth.AccessIdentity(client="my-agent", client_type="service", scopes="probe.read")
    token = access_auth.current_client_identity.set(identity)
    try:
        with pytest.raises(access_auth.HTTPException) as excinfo:
            access_auth.require_authenticated_scope("factory.read")
        assert excinfo.value.status_code == 403
    finally:
        access_auth.current_client_identity.reset(token)


def test_parse_mapping_legacy_comma_pairs():
    assert access_auth.parse_mapping("a=1,b=2") == {"a": "1", "b": "2"}


def test_parse_mapping_semicolon_pairs_hold_scope_lists():
    mapping = access_auth.parse_mapping(
        "agent-dev=finance.read,automations.trigger;goose=finance.read,finance.write"
    )
    assert mapping == {
        "agent-dev": "finance.read,automations.trigger",
        "goose": "finance.read,finance.write",
    }


def test_parse_mapping_single_pair_keeps_comma_scopes():
    # No ";" and not every comma segment is a pair -> one client, commas belong
    # to the scope list. The old parser silently dropped automations.trigger.
    mapping = access_auth.parse_mapping("agent-dev=finance.read,automations.trigger")
    assert mapping == {"agent-dev": "finance.read,automations.trigger"}


def test_parse_mapping_empty_and_whitespace():
    assert access_auth.parse_mapping("") == {}
    assert access_auth.parse_mapping(" ; ") == {}


@pytest.mark.parametrize("path", ["/api/v1/substrate/beads?limit=1", "/substrate/beads?limit=1"])
def test_substrate_proxy_denied_without_scope(path):
    from fastapi.testclient import TestClient

    with TestClient(openapi_app.app) as client:
        response = client.get(
            path,
            headers={
                "X-Truline-Client": "pipeline-probe",
                "X-Truline-Client-Type": "service",
                "X-Truline-Scopes": "probe.read",
            },
        )
    assert response.status_code == 403
    assert "substrate.proxy" in response.text
