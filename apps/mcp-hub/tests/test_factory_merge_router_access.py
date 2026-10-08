"""Auth and dispatch shape for POST /api/v1/factory/prs/{number}/merge and its
companion GET /api/v1/factory/prs/merge/{workflow_id} -- R26.09/O-2's
merge-by-verdict gateway surface.

Same no-cluster, no-network shape test_factory_note_router_access.py already
uses: `tools.factory_merge`'s Temporal calls are monkeypatched, so these hit
no real Temporal server and no live GitHub. The headline behavior this file
exists to pin: unlike every other scope this router checks, the human
wildcard ('*', HUMAN below) does NOT satisfy factory.merge -- only an
identity whose scope list names it explicitly passes.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router
from tools import factory_merge

MERGE_PATH = "/api/v1/factory/prs/42/merge"
DISPOSITION_PATH = "/api/v1/factory/prs/merge/merge-on-verdict-42-abcd1234"

HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}
SERVICE_WITH_MERGE_SCOPE = {
    "X-Truline-Client": "agent-merge",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.merge",
}
SERVICE_WITH_UNRELATED_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.write",
}


@pytest.fixture
def start_calls(monkeypatch):
    calls: list[dict] = []

    async def _fake_start(number, requested_by, scope):
        record = {"number": number, "requested_by": requested_by, "scope": scope}
        calls.append(record)
        return {"workflow_id": "merge-on-verdict-42-abcd1234", "run_id": "run-1"}

    monkeypatch.setattr(factory_router.factory_merge, "start_merge_on_verdict", _fake_start)
    return calls


# ---------------------------------------------------------------------------
# auth: factory.merge is exact-match-only (access_auth.EXACT_MATCH_SCOPES)
# ---------------------------------------------------------------------------


def test_denied_without_any_identity(start_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(MERGE_PATH)
    assert response.status_code == 401
    assert start_calls == []


def test_human_wildcard_is_refused_not_granted(start_calls):
    """The headline behavior: '*' passes every other factory.* route this
    gateway serves, but not this one."""
    with TestClient(openapi_app.app) as client:
        response = client.post(MERGE_PATH, headers=HUMAN)
    assert response.status_code == 403
    assert "factory.merge" in response.text
    assert start_calls == []


def test_a_different_granted_scope_is_still_refused(start_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(MERGE_PATH, headers=SERVICE_WITH_UNRELATED_SCOPE)
    assert response.status_code == 403
    assert start_calls == []


def test_identity_with_the_exact_scope_is_admitted(start_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(MERGE_PATH, headers=SERVICE_WITH_MERGE_SCOPE)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "started"
    assert body["pr"] == 42
    assert body["workflow_id"] == "merge-on-verdict-42-abcd1234"
    assert start_calls == [{"number": 42, "requested_by": "agent-merge", "scope": "factory.merge"}]


def test_provenance_carries_the_calling_client_not_a_constant(start_calls):
    with TestClient(openapi_app.app) as client:
        client.post(MERGE_PATH, headers=SERVICE_WITH_MERGE_SCOPE)
    assert start_calls[0]["requested_by"] == "agent-merge"
    assert start_calls[0]["requested_by"] != "factory-dispatcher/claude"


# ---------------------------------------------------------------------------
# the gateway never blocks on gh/merge-pr.sh/Temporal itself -- proven here
# by the fact that a synchronous 202 with no Temporal server running at all
# is possible only because start_merge_on_verdict is the sole call made.
# ---------------------------------------------------------------------------


def test_response_is_202_not_200(start_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(MERGE_PATH, headers=SERVICE_WITH_MERGE_SCOPE)
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# companion read: factory.read, not factory.merge -- '*' satisfies it
# ---------------------------------------------------------------------------


@pytest.fixture
def disposition_calls(monkeypatch):
    calls: list[str] = []

    async def _fake_get(workflow_id):
        calls.append(workflow_id)
        return {"status": "done", "pr": 42, "disposition": "merged", "reason": None}

    monkeypatch.setattr(factory_router.factory_merge, "get_merge_disposition", _fake_get)
    return calls


def test_disposition_read_allowed_for_human_wildcard(disposition_calls):
    with TestClient(openapi_app.app) as client:
        response = client.get(DISPOSITION_PATH, headers=HUMAN)
    assert response.status_code == 200
    assert response.json()["disposition"] == "merged"
    assert disposition_calls == ["merge-on-verdict-42-abcd1234"]


def test_disposition_read_denied_without_any_identity(disposition_calls):
    with TestClient(openapi_app.app) as client:
        response = client.get(DISPOSITION_PATH)
    assert response.status_code == 401
    assert disposition_calls == []


def test_disposition_not_found_maps_to_404(monkeypatch):
    async def _raise_not_found(workflow_id):
        raise factory_merge.NotFound(f"no merge-on-verdict workflow found for id {workflow_id!r}")

    monkeypatch.setattr(factory_router.factory_merge, "get_merge_disposition", _raise_not_found)
    with TestClient(openapi_app.app) as client:
        response = client.get(DISPOSITION_PATH, headers=HUMAN)
    assert response.status_code == 404
