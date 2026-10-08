import pytest
import schemathesis
import hypothesis
from unittest.mock import AsyncMock

# Import the application normally at module level
import openapi_app
from tools.vision import StagedVisionExtraction, VisionExtraction

# Configure hypothesis to run quickly in test/CI
hypothesis.settings.register_profile("fast", max_examples=5)
hypothesis.settings.load_profile("fast")

# Load OpenAPI schema from the application
schema = schemathesis.openapi.from_asgi("/openapi.json", openapi_app.app)


@pytest.fixture(autouse=True)
def setup_routers_mocks(monkeypatch):
    """Automatically mock all upstream capability functions bound inside the routers.
    This prevents fuzzed requests from hitting live databases, Temporal, or external APIs.
    """
    import routers.v1.context
    import routers.v1.homelab
    import routers.v1.calendar
    import routers.v1.finance
    import routers.v1.ha
    import routers.v1.vision

    # 1. Mock context router functions
    monkeypatch.setattr(routers.v1.context, "get_current_context", lambda: {"status": "mocked"})

    # 2. Mock homelab router functions
    monkeypatch.setattr(routers.v1.homelab, "get_platform_status", lambda: "mocked")
    monkeypatch.setattr(routers.v1.homelab, "get_grafana_alerts", lambda: [])
    monkeypatch.setattr(routers.v1.homelab, "homelab_query_beads", lambda *args: [])
    monkeypatch.setattr(routers.v1.homelab, "get_litellm_spend", lambda: {})

    # 3. Mock calendar router functions
    monkeypatch.setattr(routers.v1.calendar, "get_calendar_events", lambda *args: [])
    monkeypatch.setattr(routers.v1.calendar, "get_upcoming_events", lambda *args: [])

    # 4. Mock HA router functions
    monkeypatch.setattr(routers.v1.ha, "ha_get_state", lambda *args: {})
    monkeypatch.setattr(routers.v1.ha, "ha_list_entities", lambda *args: [])
    monkeypatch.setattr(routers.v1.ha, "ha_get_history", lambda *args: [])
    monkeypatch.setattr(routers.v1.ha, "ha_get_office_intent_today", lambda: {})

    # 5. Mock vision router functions
    mock_vision_ext = VisionExtraction(summary="mocked")
    mock_staged_ext = StagedVisionExtraction(bead_id="mock-bead", extraction=mock_vision_ext)
    monkeypatch.setattr(routers.v1.vision, "extract_and_stage", AsyncMock(return_value=mock_staged_ext))
    monkeypatch.setattr(routers.v1.vision, "extract_from_image", AsyncMock(return_value=mock_vision_ext))

    # 6. Mock finance router functions
    monkeypatch.setattr(routers.v1.finance, "log_expense", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_expenses", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "log_bill", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "mark_bill_paid", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_bills_due", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "set_budget", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_budget_status", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_category_history", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "get_recent_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "get_account_balances", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "get_unreconciled_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(routers.v1.finance, "list_subscriptions", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_bill_ledger", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "calculate_run_rate", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_ledger_variance", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "run_scenario", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "commit_rule", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_connections_status", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "trigger_bank_sync", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "get_bank_sync_schedules", AsyncMock(return_value={}))
    monkeypatch.setattr(routers.v1.finance, "reconcile_schedules", AsyncMock(return_value={}))

    # 7. Mock automations router functions (fuzzing must never reach Temporal)
    import routers.v1.automations
    monkeypatch.setattr(
        routers.v1.automations,
        "trigger_pay_train_parking",
        AsyncMock(return_value={"via": "schedule", "id": "pay-train-parking-daily"}),
    )


@schema.parametrize()
def test_api_contract(case):
    # Exclude infrastructure, proxy, and interactive HTML endpoints
    if (
        "/substrate" in case.path
        or "/link" in case.path
        or "/exchange" in case.path
        or "/webhooks" in case.path
        or "/auth" in case.path
    ):
        pytest.skip("Skipping infrastructure or proxy endpoint")

    # Run the test case in-process against the ASGI application.
    # The endpoint must never return a 5xx server crash error regardless of input.
    response = case.call()
    assert (
        response.status_code < 500
    ), f"Endpoint {case.method} {case.path} failed with server crash {response.status_code}:\n{response.text}"
