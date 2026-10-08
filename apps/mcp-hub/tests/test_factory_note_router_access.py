"""Auth, refusal-parity and provenance for POST /api/v1/factory/note -- the
gateway's dev.note capability (this bead: the console's other dev.* direct
substrate write, closing the other half of the intake #909 started).

Same shape test_factory_tasks_router_access.py already exercises: no
cluster, no network, ``note_filing.file_dev_note`` is monkeypatched so these
hit no dispatcher checkout or substrate.
"""

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router
from tools import note_filing

NOTE_PATH = "/api/v1/factory/note"

COMMENT = {"parent_id": "task-1", "kind": "comment", "body": "context for the worker"}

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
SERVICE_ANOTHER_CLIENT = {
    "X-Truline-Client": "agent-review",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.write",
}
SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.read",
}


@pytest.fixture
def note_filing_calls(monkeypatch):
    """Records every call ``factory_file_note`` makes into ``note_filing``,
    and stands in for the real store -- no substrate, no network."""

    calls: list[dict] = []

    def _fake_file_dev_note(parent_id, kind, body, created_by, client_type, *, fields=None):
        record = {
            "parent_id": parent_id,
            "kind": kind,
            "body": body,
            "created_by": created_by,
            "client_type": client_type,
            "fields": dict(fields or {}),
        }
        calls.append(record)
        content = {"kind": kind, "body": body, **record["fields"]}
        return {
            "id": f"note-{len(calls)}",
            "parent_id": parent_id,
            "state": "active",
            "created_by": created_by,
            "content": content,
        }

    monkeypatch.setattr(factory_router.note_filing, "file_dev_note", _fake_file_dev_note)
    return calls


# ---------------------------------------------------------------------------
# auth (AC-1's scope, mirroring the tasks route's own boundary)
# ---------------------------------------------------------------------------


def test_denied_without_any_identity(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT)
    assert response.status_code == 401
    assert note_filing_calls == []


def test_denied_for_authenticated_client_without_scope(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.write" in response.text
    assert note_filing_calls == []


def test_allowed_for_human_wildcard_like_the_console(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=HUMAN)
    assert response.status_code == 200
    assert note_filing_calls[0]["created_by"] == "operator@example.org"
    assert note_filing_calls[0]["client_type"] == "human"


# ---------------------------------------------------------------------------
# provenance (AC-5): the calling client, never a hard-coded worker
# ---------------------------------------------------------------------------


def test_provenance_carries_the_client_not_a_constant(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert note_filing_calls[0]["created_by"] == "agent-dev"
    assert note_filing_calls[0]["created_by"] != "lifeops-console/factory-board"


def test_two_different_clients_produce_two_different_provenance_records(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        first = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_WITH_SCOPE)
        second = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_ANOTHER_CLIENT)

    assert first.status_code == 200
    assert second.status_code == 200
    assert note_filing_calls[0]["created_by"] == "agent-dev"
    assert note_filing_calls[1]["created_by"] == "agent-review"
    assert note_filing_calls[0]["created_by"] != note_filing_calls[1]["created_by"]


def test_created_by_refuses_rather_than_falling_back_to_a_placeholder(monkeypatch, note_filing_calls):
    """Mirrors test_factory_tasks_router_access.py's identical test for
    /tasks (F5, #909 gate; reintroduced and caught again on this branch by
    #938/#942's CREATED_BY_FALLBACK_GRANDFATHERED gate). created_by must
    never be a hard-coded worker string: require_authenticated_scope already
    raises 401 whenever current_client_identity is None, so that branch is
    unreachable through a real request. This proves the *fallback* is gone
    by removing the guard that makes it unreachable and confirming the route
    now raises instead of silently minting created_by="unknown".
    """

    class _AlwaysNone:
        def get(self):
            return None

    monkeypatch.setattr(factory_router, "require_authenticated_scope", lambda scope: None)
    monkeypatch.setattr(factory_router, "current_client_identity", _AlwaysNone())
    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=HUMAN)
    assert response.status_code == 500
    assert note_filing_calls == []


# ---------------------------------------------------------------------------
# refusal parity with the store's closed shape (AC-3) -- each with a
# known-good control alongside it, so a route that refused everything
# could not pass.
# ---------------------------------------------------------------------------


def test_unknown_kind_is_refused(note_filing_calls):
    body = {**COMMENT, "kind": "sidebar"}
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert note_filing_calls == []


def test_a_known_kind_is_accepted(note_filing_calls):
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200


def test_answer_without_answers_ref_is_refused(note_filing_calls):
    body = {"parent_id": "task-1", "kind": "answer", "body": "the fix ships in v2"}
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert "answers_ref" in response.text
    assert note_filing_calls == []


def test_answer_with_answers_ref_is_accepted(note_filing_calls):
    body = {
        "parent_id": "task-1",
        "kind": "answer",
        "body": "the fix ships in v2",
        "answers_ref": "note-9",
    }
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert note_filing_calls[0]["fields"]["answers_ref"] == "note-9"


def test_review_without_verdict_is_refused(note_filing_calls):
    body = {"parent_id": "task-1", "kind": "review", "body": "looks good structurally"}
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert "verdict" in response.text
    assert note_filing_calls == []


def test_review_with_verdict_is_accepted(note_filing_calls):
    body = {
        "parent_id": "task-1",
        "kind": "review",
        "body": "looks good structurally",
        "verdict": "approve",
    }
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert note_filing_calls[0]["fields"]["verdict"] == "approve"


def test_answers_ref_on_a_non_answer_kind_is_refused_even_as_explicit_null(note_filing_calls):
    """Pins the model_fields_set switch: an explicitly-sent null is refused
    the same way a real value is, for every "only valid for kind X" guard,
    not just releases_work."""
    body = {**COMMENT, "answers_ref": None}
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert note_filing_calls == []


# ---------------------------------------------------------------------------
# releases_work carriage (AC-4): present iff the client set it, never
# invented, both directions asserted against what reaches the store.
# ---------------------------------------------------------------------------


def test_releases_work_true_arrives_at_the_store(note_filing_calls):
    body = {
        "parent_id": "task-1",
        "kind": "answer",
        "body": "shipping now",
        "answers_ref": "note-9",
        "releases_work": True,
    }
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert note_filing_calls[0]["fields"]["releases_work"] is True


def test_releases_work_omitted_is_never_invented(note_filing_calls):
    body = {
        "parent_id": "task-1",
        "kind": "answer",
        "body": "still thinking",
        "answers_ref": "note-9",
    }
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert "releases_work" not in note_filing_calls[0]["fields"]


def test_releases_work_false_is_preserved_not_treated_as_absent(note_filing_calls):
    body = {
        "parent_id": "task-1",
        "kind": "answer",
        "body": "not yet",
        "answers_ref": "note-9",
        "releases_work": False,
    }
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert note_filing_calls[0]["fields"]["releases_work"] is False


def test_releases_work_on_a_non_answer_kind_is_refused(note_filing_calls):
    body = {**COMMENT, "releases_work": True}
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=body, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 422
    assert note_filing_calls == []


# ---------------------------------------------------------------------------
# misconfiguration and store refusal are loud, not degraded
# ---------------------------------------------------------------------------


def test_filing_unavailable_maps_to_503(monkeypatch):
    monkeypatch.setattr(
        factory_router.note_filing,
        "file_dev_note",
        lambda parent_id, kind, body, created_by, client_type, *, fields=None: (
            _ for _ in ()
        ).throw(note_filing.Unavailable("not configured")),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 503


def test_filing_refused_surfaces_the_stores_own_status_and_reason(monkeypatch):
    """Gate finding #4: a SubstrateError-shaped refusal (mapped by
    note_filing.file_dev_note to FilingRefused) must surface as the store's
    own status/detail, not an opaque 500."""

    monkeypatch.setattr(
        factory_router.note_filing,
        "file_dev_note",
        lambda parent_id, kind, body, created_by, client_type, *, fields=None: (
            _ for _ in ()
        ).throw(note_filing.FilingRefused(409, "answers_ref does not resolve to an open question")),
    )
    with TestClient(openapi_app.app) as client:
        response = client.post(NOTE_PATH, json=COMMENT, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 409
    assert "answers_ref does not resolve to an open question" in response.text
