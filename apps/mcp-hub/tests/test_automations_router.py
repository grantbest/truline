"""Automations capability router + trigger tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.automations
import tools.automations as automations_tool

TRIGGER_PATH = "/api/v1/automations/pay_train_parking/trigger"

SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "probe.read",
}
SERVICE_WITH_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "finance.read,automations.trigger",
}
HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}


@pytest.fixture()
def mock_trigger(monkeypatch):
    mock = AsyncMock(return_value={"via": "schedule", "id": "pay-train-parking-daily"})
    monkeypatch.setattr(routers.v1.automations, "trigger_pay_train_parking", mock)
    return mock


def test_trigger_denied_without_scope(mock_trigger):
    with TestClient(openapi_app.app) as client:
        response = client.post(TRIGGER_PATH, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "automations.trigger" in response.text
    mock_trigger.assert_not_awaited()


def test_trigger_allowed_with_scope(mock_trigger):
    with TestClient(openapi_app.app) as client:
        response = client.post(TRIGGER_PATH, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200
    assert response.json() == {"via": "schedule", "id": "pay-train-parking-daily"}
    mock_trigger.assert_awaited_once()


def test_trigger_allowed_for_human_wildcard(mock_trigger):
    with TestClient(openapi_app.app) as client:
        response = client.post(TRIGGER_PATH, headers=HUMAN)
    assert response.status_code == 200


async def test_tool_prefers_schedule_trigger(monkeypatch):
    handle = MagicMock()
    handle.trigger = AsyncMock()
    client = MagicMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock()
    monkeypatch.setattr(automations_tool, "get_automations_client", AsyncMock(return_value=client))

    result = await automations_tool.trigger_pay_train_parking()

    assert result == {"via": "schedule", "id": "pay-train-parking-daily"}
    handle.trigger.assert_awaited_once()
    client.start_workflow.assert_not_awaited()


async def test_tool_falls_back_to_direct_start_when_schedule_missing(monkeypatch):
    handle = MagicMock()
    handle.trigger = AsyncMock(side_effect=Exception("schedule not found"))
    client = MagicMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock()
    monkeypatch.setattr(automations_tool, "get_automations_client", AsyncMock(return_value=client))

    result = await automations_tool.trigger_pay_train_parking()

    assert result["via"] == "workflow"
    assert result["id"].startswith("pay-train-parking-")
    assert result["id"].endswith("-manual")
    client.start_workflow.assert_awaited_once()
    args, kwargs = client.start_workflow.call_args
    assert args[0] == "PayTrainParkingWorkflow"
    assert kwargs["task_queue"] == "automations-queue"


async def test_tool_reraises_non_notfound_errors(monkeypatch):
    handle = MagicMock()
    handle.trigger = AsyncMock(side_effect=RuntimeError("temporal unavailable"))
    client = MagicMock()
    client.get_schedule_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock()
    monkeypatch.setattr(automations_tool, "get_automations_client", AsyncMock(return_value=client))

    with pytest.raises(RuntimeError, match="temporal unavailable"):
        await automations_tool.trigger_pay_train_parking()
    client.start_workflow.assert_not_awaited()
