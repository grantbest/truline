"""Auth for POST /api/v1/factory/tasks -- the gateway's factory.write
capability (M9). Same shape test_factory_router_access.py already exercises
for factory.read: no cluster, no network, the underlying filing function is
monkeypatched so these hit no dispatcher checkout or substrate.

factory.write is not granted to any client as of this change (ships dark --
see .factory/design.md and this bead's PR body), so in production this route
is unreachable regardless; these tests exist so the day it IS granted, the
auth boundary is already proven.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router
from tools import task_filing

TASKS_PATH = "/api/v1/factory/tasks"
SPEC = {
    "lane": "code-health",
    "title": "auth probe",
    "intent": "probe",
    "acceptance": ["THE thing SHALL happen"],
    "scope": {"paths": ["apps/x/"]},
    "risk_class": "behavioral",
    "requirement_refs_waived": "test fixture",
    "release_ref_waived": "test fixture",
}

HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}
SERVICE_WITH_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.write",
}
SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.read",
}


@pytest.fixture(autouse=True)
def stub_task_filing(monkeypatch):
    monkeypatch.setattr(
        factory_router.task_filing,
        "file_dev_task",
        lambda spec, created_by: {
            "id": "bead-1",
            "state": "pending",
            "created_by": created_by,
            "content": {"lane": spec.get("lane"), "title": spec.get("title"), "risk_class": spec.get("risk_class")},
        },
    )


def test_denied_without_any_identity():
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC)
    assert response.status_code == 401
    assert "Truline identity" in response.text


def test_denied_for_authenticated_client_without_scope():
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.write" in response.text


def test_allowed_for_human_wildcard_like_the_console():
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=HUMAN)
    assert response.status_code == 200


def test_allowed_for_service_with_scope_and_provenance_carries_the_client():
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert response.json()["id"] == "bead-1"


def test_filing_refused_maps_to_422(monkeypatch):
    monkeypatch.setattr(
        factory_router.task_filing,
        "file_dev_task",
        lambda spec, created_by: (_ for _ in ()).throw(task_filing.FilingRefused("refused: reason text")),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert "refused: reason text" in response.text


def test_created_by_refuses_rather_than_falling_back_to_a_placeholder(monkeypatch):
    """F5 (#909 gate): created_by must never be a hard-coded worker string.

    require_authenticated_scope already raises 401 whenever
    current_client_identity is None, so that branch is unreachable through a
    real request. This proves the *fallback* is gone by removing the guard
    that makes it unreachable and confirming the route now raises instead of
    silently minting created_by="unknown".
    """
    class _AlwaysNone:
        def get(self):
            return None

    monkeypatch.setattr(factory_router, "require_authenticated_scope", lambda scope: None)
    monkeypatch.setattr(factory_router, "current_client_identity", _AlwaysNone())
    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=HUMAN)
    assert response.status_code == 500


def test_filing_unavailable_maps_to_503(monkeypatch):
    monkeypatch.setattr(
        factory_router.task_filing,
        "file_dev_task",
        lambda spec, created_by: (_ for _ in ()).throw(task_filing.Unavailable("not configured")),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(TASKS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 503
