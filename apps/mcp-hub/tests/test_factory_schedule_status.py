"""``tools.factory_schedule_status`` must answer exactly what
``schedule_runtime.describe_factory_schedule_status`` would -- it imports and
calls that reader rather than re-deriving it, and it must say "unknown" (not
an empty success) when Temporal or the dispatcher checkout cannot be
reached (PRIN-015). No browser, no cluster, no real Temporal client: every
dependency below is an in-process double (AC-6 -- no test here may open a
real Temporal client or pause/unpause/trigger/terminate anything).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.factory as factory_router
from tools import factory_schedule_status


# ---------------------------------------------------------------------------
# A Temporal client double, shaped exactly the way
# schedule_runtime.describe_factory_schedule_status and this module's own
# second describe() call read it -- built from schedule_runtime.py's own
# attribute reads (_in_flight_workflow, _time_attr, workflow_execution_status),
# never from a real temporalio.client.Client.
# ---------------------------------------------------------------------------


def _action(workflow_id, scheduled_at="", started_at=""):
    return SimpleNamespace(workflow_id=workflow_id, scheduled_at=scheduled_at, started_at=started_at)


class _ScheduleHandle:
    def __init__(self, description):
        self._description = description

    async def describe(self):
        if isinstance(self._description, Exception):
            raise self._description
        return self._description


class _WorkflowHandle:
    def __init__(self, status_name):
        self._status_name = status_name

    async def describe(self):
        return SimpleNamespace(status=SimpleNamespace(name=self._status_name))


class FakeTemporalClient:
    """A client double covering exactly the two calls this route makes:
    ``get_schedule_handle(...).describe()`` (twice -- once directly for the
    pause note, once inside the reused reader) and
    ``get_workflow_handle(...).describe()`` (the reused reader's own
    ``workflow_execution_status`` status lookup for each recent action).
    """

    def __init__(self, *, paused, note, running_actions=(), recent_actions=(), statuses=None):
        self.schedule_description = SimpleNamespace(
            schedule=SimpleNamespace(state=SimpleNamespace(paused=paused, note=note)),
            info=SimpleNamespace(running_actions=list(running_actions), recent_actions=list(recent_actions)),
        )
        self.statuses = statuses or {}

    def get_schedule_handle(self, schedule_id):
        return _ScheduleHandle(self.schedule_description)

    def get_workflow_handle(self, workflow_id, run_id=None):
        return _WorkflowHandle(self.statuses.get(workflow_id, "COMPLETED"))


class RaisingScheduleHandle:
    async def describe(self):
        raise RuntimeError("Temporal is unreachable")


class UnreachableTemporalClient:
    def get_schedule_handle(self, schedule_id):
        return RaisingScheduleHandle()

    def get_workflow_handle(self, workflow_id, run_id=None):  # pragma: no cover - never reached
        raise AssertionError("should not be called when describe() already failed")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# dispatch_schedule_status -- the tool function, against the real reused
# reader (schedule_runtime.describe_factory_schedule_status, imported from
# the real checkout via FACTORY_DISPATCHER_ROOT, set by conftest.py)
# ---------------------------------------------------------------------------


def test_paused_schedule_reports_paused_and_the_note_verbatim():
    client = FakeTemporalClient(
        paused=True,
        note="factory dispatcher paused: capacity backpressure; nightly window",
        recent_actions=[_action("wf-1", scheduled_at="2026-09-20T10:00:00Z")],
        statuses={"wf-1": "COMPLETED"},
    )
    result = _run(factory_schedule_status.dispatch_schedule_status(client=client))

    assert result["status"] == "ok"
    assert result["paused"] is True
    assert result["note"] == "factory dispatcher paused: capacity backpressure; nightly window"
    assert result["in_flight"] == []
    assert result["recent"] == [
        {
            "workflow_id": "wf-1",
            "scheduled_at": "2026-09-20T10:00:00Z",
            "status": "COMPLETED",
            "failed": False,
            "succeeded": True,
        }
    ]
    assert result["last_success_at"] == "2026-09-20T10:00:00Z"
    assert result["consecutive_failures"] == 0


def test_unpaused_schedule_with_in_flight_workflow():
    client = FakeTemporalClient(
        paused=False,
        note="factory dispatcher unattended drain",
        running_actions=[_action("wf-running", scheduled_at="2026-09-23T09:00:00Z", started_at="2026-09-23T09:00:05Z")],
        recent_actions=[_action("wf-0", scheduled_at="2026-09-23T08:45:00Z")],
        statuses={"wf-0": "FAILED"},
    )
    result = _run(factory_schedule_status.dispatch_schedule_status(client=client))

    assert result["status"] == "ok"
    assert result["paused"] is False
    assert result["in_flight"] == [
        {
            "workflow_id": "wf-running",
            "run_id": "",
            "scheduled_at": "2026-09-23T09:00:00Z",
            "started_at": "2026-09-23T09:00:05Z",
        }
    ]
    assert result["recent"][0]["failed"] is True
    assert result["consecutive_failures"] == 1
    assert result["last_success_at"] == ""


def test_cannot_reach_temporal_answers_a_declared_unknown_shape():
    """PRIN-015: a route that cannot reach Temporal must answer a declared
    cannot-evaluate shape, never an empty success."""
    result = _run(factory_schedule_status.dispatch_schedule_status(client=UnreachableTemporalClient()))

    assert result["status"] == "unknown"
    assert "Temporal is unreachable" in result["detail"]
    assert "paused" not in result
    assert "recent" not in result


def test_dispatcher_checkout_unavailable_answers_a_declared_unknown_shape(monkeypatch):
    def _raise():
        raise factory_schedule_status.Unavailable("FACTORY_DISPATCHER_ROOT is not set")

    monkeypatch.setattr(factory_schedule_status, "_schedule_runtime_module", _raise)
    result = _run(factory_schedule_status.dispatch_schedule_status(client=FakeTemporalClient(paused=True, note=None)))

    assert result["status"] == "unknown"
    assert "FACTORY_DISPATCHER_ROOT" in result["detail"]


# ---------------------------------------------------------------------------
# The gateway route: auth shape (mirrors test_factory_router_access.py) and
# that it never calls schedule_status.py's main() -- confirmed by
# construction (this module never imports schedule_status).
# ---------------------------------------------------------------------------

SCHEDULE_STATUS_PATH = "/api/v1/factory/schedule_status"

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


@pytest.fixture
def stub_dispatch_schedule_status(monkeypatch):
    """Not autouse: the tool-level tests above call
    ``factory_schedule_status.dispatch_schedule_status`` directly and must
    exercise the real function, not this stub -- ``factory_router
    .factory_schedule_status`` is the same module object (imported by
    reference), so an autouse patch here would silently overwrite the real
    function for those tests too."""

    async def _fake(*, client=None):
        return {
            "status": "ok",
            "schedule_id": "factory-dispatcher-dev",
            "paused": False,
            "note": "factory dispatcher unattended drain",
            "in_flight": [],
            "recent": [],
            "last_success_at": "",
            "consecutive_failures": 0,
            "recent_failures": 0,
        }

    monkeypatch.setattr(factory_router.factory_schedule_status, "dispatch_schedule_status", _fake)


def test_denied_without_any_identity(stub_dispatch_schedule_status):
    with TestClient(openapi_app.app) as client:
        response = client.get(SCHEDULE_STATUS_PATH)
    assert response.status_code == 401
    assert "Truline identity" in response.text


def test_denied_for_authenticated_client_without_scope(stub_dispatch_schedule_status):
    with TestClient(openapi_app.app) as client:
        response = client.get(SCHEDULE_STATUS_PATH, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.read" in response.text


def test_allowed_for_human_wildcard_like_the_console(stub_dispatch_schedule_status):
    with TestClient(openapi_app.app) as client:
        response = client.get(SCHEDULE_STATUS_PATH, headers=HUMAN)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["paused"] is False


def test_allowed_for_service_with_scope(stub_dispatch_schedule_status):
    with TestClient(openapi_app.app) as client:
        response = client.get(SCHEDULE_STATUS_PATH, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200


def test_route_module_never_imports_schedule_status_py():
    """``schedule_status.py``'s ``main()`` posts Discord alerts and can
    announce beads -- nothing served from a browser request may invoke it
    (this bead's own boundary). Pinned structurally: an ``import
    schedule_status`` anywhere in this module would bind a top-level name
    ``schedule_status`` in its namespace; ``factory_schedule_status`` (this
    module's own name, and the ``schedule_runtime`` module it does import)
    are distinct keys, so this cannot false-positive on either."""
    assert "schedule_status" not in vars(factory_schedule_status)
