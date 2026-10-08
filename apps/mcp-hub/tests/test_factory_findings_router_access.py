"""Auth for POST /api/v1/factory/findings -- the gateway's factory.write
capability, S55-10b's half of R26.11 (S55-10a is the dispatcher-side filer
this route calls through). Same shape test_factory_tasks_router_access.py
already exercises for /tasks: no cluster, no network, the underlying filing
function is monkeypatched so these hit no dispatcher checkout or substrate.

factory.write is not granted to any client as of this change (ships dark --
see .factory/design.md and this bead's PR body), so in production this route
is unreachable regardless; these tests exist so the day it IS granted, the
auth boundary is already proven.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router
from tools import finding_filing

FINDINGS_PATH = "/api/v1/factory/findings"
SPEC = {
    "kind": "bug",
    "disposition": "backlog",
    "severity": "low",
    "summary": "auth probe",
    "state": "backlogged",
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
def stub_finding_filing(monkeypatch):
    monkeypatch.setattr(
        factory_router.finding_filing,
        "file_dev_finding",
        lambda spec, created_by: {
            "id": "finding-1",
            "state": spec.get("state"),
            "kind": spec.get("kind"),
            "severity": spec.get("severity"),
        },
    )


def test_denied_without_any_identity():
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC)
    assert response.status_code == 401
    assert "Truline identity" in response.text


def test_denied_for_authenticated_client_without_scope():
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.write" in response.text


def test_allowed_for_human_wildcard_like_the_console():
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=HUMAN)
    assert response.status_code == 200


def test_allowed_for_service_with_scope_and_response_shape(monkeypatch):
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "id": "finding-1",
        "state": "backlogged",
        "kind": "bug",
        "severity": "low",
    }


def test_filing_refused_maps_to_422(monkeypatch):
    monkeypatch.setattr(
        factory_router.finding_filing,
        "file_dev_finding",
        lambda spec, created_by: (_ for _ in ()).throw(
            finding_filing.FilingRefused("refused: reason text")
        ),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert response.json()["detail"] == "refused: reason text"


def test_filing_partial_write_maps_to_500_with_the_created_id(monkeypatch):
    monkeypatch.setattr(
        factory_router.finding_filing,
        "file_dev_finding",
        lambda spec, created_by: (_ for _ in ()).throw(
            finding_filing.PartialWrite("finding-partial-9", "1")
        ),
    )
    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 500
    assert "finding-partial-9" in response.text
    assert "pending" in response.text


def test_filing_unavailable_maps_to_503(monkeypatch):
    monkeypatch.setattr(
        factory_router.finding_filing,
        "file_dev_finding",
        lambda spec, created_by: (_ for _ in ()).throw(
            finding_filing.Unavailable("not configured")
        ),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 503


def test_created_by_refuses_rather_than_falling_back_to_a_placeholder(monkeypatch):
    """F5 (#909 gate), same shape as the tasks route's own test:
    created_by must never be a hard-coded worker string.

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
        response = client.post(FINDINGS_PATH, json=SPEC, headers=HUMAN)
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# provenance (AC-4): created_by names the client, prompt_ref names where the
# finding came from, and both survive the gateway unchanged or honestly
# defaulted.
# ---------------------------------------------------------------------------


def test_created_by_and_explicit_prompt_ref_pass_through_unchanged(monkeypatch):
    calls = []

    def _capture(spec, created_by):
        calls.append({"spec": spec, "created_by": created_by})
        return {"id": "finding-1", "state": spec.get("state"), "kind": spec.get("kind"), "severity": spec.get("severity")}

    monkeypatch.setattr(factory_router.finding_filing, "file_dev_finding", _capture)
    body = dict(SPEC, prompt_ref="pr#1234")
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert calls[0]["created_by"] == "agent-dev"
    assert calls[0]["spec"]["prompt_ref"] == "pr#1234"


def test_prompt_ref_defaults_to_gateway_slash_client_identity_when_omitted(monkeypatch):
    calls = []

    def _capture(spec, created_by):
        calls.append({"spec": spec, "created_by": created_by})
        return {"id": "finding-1", "state": spec.get("state"), "kind": spec.get("kind"), "severity": spec.get("severity")}

    monkeypatch.setattr(factory_router.finding_filing, "file_dev_finding", _capture)
    assert "prompt_ref" not in SPEC
    with TestClient(openapi_app.app) as client:
        response = client.post(FINDINGS_PATH, json=SPEC, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert calls[0]["created_by"] == "agent-dev"
    assert calls[0]["spec"]["prompt_ref"] == "gateway/agent-dev"
