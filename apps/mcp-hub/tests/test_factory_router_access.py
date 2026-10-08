"""Auth for the factory status router -- the served read of the release
graph and of what can start (PC-TRU-002/AC-1, R26.02/O-7).

This must be reachable by exactly the identities that reach the console
today (Cloudflare Access + Traefik-injected X-Truline-* headers), and
refused for a request that carries none of that -- the same shape
test_mcp_surface.py guards for the /mcp surface, exercised here directly
against the HTTP router. No cluster, no network: the underlying tool
functions are monkeypatched so these hit no dispatcher checkout or substrate.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router

TASK_RUNNABLE_PATH = "/api/v1/factory/task_runnable"
RELEASE_DELIVERY_PATH = "/api/v1/factory/release_delivery"
IMPACT_PATH = "/api/v1/factory/impact"

PATHS_AND_PAYLOADS = [
    (TASK_RUNNABLE_PATH, {"task_id": "task-1"}),
    (RELEASE_DELIVERY_PATH, {"release_ref": "R26.02"}),
    (IMPACT_PATH, {"kind": "application", "ref": "app.example"}),
]

# What the console's own browser traffic carries after Cloudflare Access
# authenticates and Traefik injects identity headers.
HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}
SERVICE_WITH_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.read",
}
SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "probe.read",
}


@pytest.fixture(autouse=True)
def stub_factory_status(monkeypatch):
    monkeypatch.setattr(
        factory_router.factory_status,
        "task_runnability",
        lambda task_id: {"status": "ok", "found": False},
    )
    monkeypatch.setattr(
        factory_router.factory_status,
        "release_delivery",
        lambda release_ref: {"status": "ok", "found": False},
    )

    async def _fake_get_impact(kind, ref):
        return {
            "affected": {"applications": [], "cis": [], "services": [], "unknown_applications": []},
            "coverage": {
                "applications_assessed": 0,
                "applications_total": 0,
                "cis_owned": 0,
                "cis_total": 0,
                "changes_with_affects": 0,
                "changes_total": 0,
                "partial": False,
                "partial_reads": [],
            },
            "computed_at": "2026-09-19T00:00:00+00:00",
            "revision_hint": "test",
        }

    monkeypatch.setattr(factory_router.impact, "get_impact", _fake_get_impact)


@pytest.mark.parametrize("path,payload", PATHS_AND_PAYLOADS)
def test_denied_without_any_identity(path, payload):
    # No X-Truline-* headers at all -- what an HTTP request looks like when
    # it reaches mcp-hub without passing through Traefik's ForwardAuth. Must
    # be refused, not treated as a trusted internal/stdio call.
    with TestClient(openapi_app.app) as client:
        response = client.post(path, json=payload)
    assert response.status_code == 401
    assert "Truline identity" in response.text


@pytest.mark.parametrize("path,payload", PATHS_AND_PAYLOADS)
def test_denied_for_authenticated_client_without_scope(path, payload):
    with TestClient(openapi_app.app) as client:
        response = client.post(path, json=payload, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.read" in response.text


@pytest.mark.parametrize("path,payload", PATHS_AND_PAYLOADS)
def test_allowed_for_human_wildcard_like_the_console(path, payload):
    # So the check cannot pass by refusing everything: the identity the
    # console's own traffic carries must keep working, unchanged.
    with TestClient(openapi_app.app) as client:
        response = client.post(path, json=payload, headers=HUMAN)
    assert response.status_code == 200


@pytest.mark.parametrize("path,payload", PATHS_AND_PAYLOADS)
def test_allowed_for_service_with_scope(path, payload):
    with TestClient(openapi_app.app) as client:
        response = client.post(path, json=payload, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
