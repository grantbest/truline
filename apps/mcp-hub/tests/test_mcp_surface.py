"""Scope enforcement on the /mcp surface.

The MCP tools are derived from the FastAPI routers via FastMCP.from_fastapi,
which re-issues tool calls as in-process HTTP requests and forwards the inbound
X-Truline-* identity headers (fastmcp get_http_headers). These tests pin that
forwarding chain: if a fastmcp upgrade stops forwarding identity headers, the
inner request falls back to internal/unknown — which check_scope() bypasses —
and scope enforcement silently opens. The failure mode is invisible in normal
use, so only these tests guard it.
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import openapi_app
import routers.v1.finance

MCP_PATH = "/mcp/"
ACCEPT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

SERVICE_IDENTITY_NO_FINANCE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "probe.read",
}
SERVICE_IDENTITY_WITH_FINANCE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "finance.read,automations.trigger",
}


def _parse_sse_result(response) -> dict:
    """Extract the last JSON-RPC message from an SSE or JSON response."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    payload = None
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[len("data:"):].strip())
    assert payload is not None, f"no data frame in response: {response.text!r}"
    return payload


def _initialize_session(client: TestClient, identity: dict) -> str:
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "scope-test", "version": "0"},
            },
        },
        headers={**ACCEPT_HEADERS, **identity},
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id, "initialize returned no mcp-session-id"

    notified = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**ACCEPT_HEADERS, **identity, "mcp-session-id": session_id},
    )
    assert notified.status_code in (200, 202), notified.text
    return session_id


def _call_tool(client: TestClient, identity: dict, name: str, arguments: dict) -> dict:
    session_id = _initialize_session(client, identity)
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={**ACCEPT_HEADERS, **identity, "mcp-session-id": session_id},
    )
    assert response.status_code == 200, response.text
    message = _parse_sse_result(response)
    return message["result"]


@pytest.fixture()
def mock_bills(monkeypatch):
    mock = AsyncMock(return_value=[{"vendor": "ComEd", "amount": 42.0, "due_date": "2026-07-11"}])
    monkeypatch.setattr(routers.v1.finance, "get_bills_due", mock)
    return mock


def test_service_client_without_scope_is_denied(mock_bills):
    with TestClient(openapi_app.app) as client:
        result = _call_tool(
            client,
            SERVICE_IDENTITY_NO_FINANCE,
            "get_bills_due",
            {"days_ahead": 7},
        )

    assert result.get("isError") is True
    text = json.dumps(result.get("content", ""))
    assert "finance.read" in text or "403" in text
    mock_bills.assert_not_awaited()


def test_service_client_with_scope_succeeds(mock_bills):
    with TestClient(openapi_app.app) as client:
        result = _call_tool(
            client,
            SERVICE_IDENTITY_WITH_FINANCE,
            "get_bills_due",
            {"days_ahead": 7},
        )

    assert result.get("isError") is not True, result
    text = json.dumps(result.get("content", ""))
    assert "ComEd" in text
    mock_bills.assert_awaited()


def test_substrate_proxy_is_not_an_mcp_tool():
    with TestClient(openapi_app.app) as client:
        session_id = _initialize_session(client, SERVICE_IDENTITY_WITH_FINANCE)
        response = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers={**ACCEPT_HEADERS, **SERVICE_IDENTITY_WITH_FINANCE, "mcp-session-id": session_id},
        )
        assert response.status_code == 200
        tools = _parse_sse_result(response)["result"]["tools"]

    names = [t["name"] for t in tools]
    assert names, "tools/list returned no tools"
    assert not [n for n in names if "substrate" in n.lower()], names


def test_factory_note_is_not_an_mcp_tool():
    """POST /api/v1/factory/note (routers/v1/factory.py's factory_file_note)
    would otherwise be auto-derived into an MCP tool by
    FastMCP.from_fastapi, exactly like every other router. That would let
    any authenticated MCP client discover and call it -- and a note with
    kind=answer, answers_ref=<id>, releases_work=true is the one thing that
    releases a held dev.task blocking question, so an unexcluded route would
    let a remote agent answer and release its own gate. openapi_app.py's
    route_maps excludes it the same way it already excludes the substrate
    passthrough; other factory tools (file_dev_task, get_task_runnable, ...)
    stay reachable, so this pins the exclusion is scoped to /factory/note
    only, not the whole router.
    """
    with TestClient(openapi_app.app) as client:
        session_id = _initialize_session(client, SERVICE_IDENTITY_WITH_FINANCE)
        response = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers={**ACCEPT_HEADERS, **SERVICE_IDENTITY_WITH_FINANCE, "mcp-session-id": session_id},
        )
        assert response.status_code == 200
        tools = _parse_sse_result(response)["result"]["tools"]

    names = [t["name"] for t in tools]
    assert names, "tools/list returned no tools"
    assert "file_dev_note" not in names, names
    assert "file_dev_task" in names, names


def test_file_dev_finding_is_derived_not_hand_registered():
    """POST /api/v1/factory/findings (routers/v1/factory.py's
    factory_file_finding, operation_id file_dev_finding) must reach the MCP
    surface only through FastMCP.from_fastapi's derivation from the route
    (AC-5), never a hand-written ``@mcp.tool``/``FastMCP.tool``/``add_tool``
    registration. Cardinality alone is insufficient (a mutant like
    ``@mcp.tool(name="file_dev_finding")`` appended to openapi_app.py would
    still produce exactly one entry, but with an empty description): this
    also pins the derived tool's description to the route's own summary
    string, which only the FastAPI-to-MCP derivation path carries over.
    """
    with TestClient(openapi_app.app) as client:
        session_id = _initialize_session(client, SERVICE_IDENTITY_WITH_FINANCE)
        response = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers={**ACCEPT_HEADERS, **SERVICE_IDENTITY_WITH_FINANCE, "mcp-session-id": session_id},
        )
        assert response.status_code == 200
        tools = _parse_sse_result(response)["result"]["tools"]

    matches = [t for t in tools if t["name"] == "file_dev_finding"]
    assert len(matches) == 1, tools
    assert matches[0]["description"] == (
        "File a dev.finding bead from a JSON spec — the same shape "
        "file_finding.py's CLI accepts"
    )


def test_bare_mcp_path_serves_directly_without_redirect():
    # The claude.ai MCP client (Claude-User) POSTs /mcp without a trailing
    # slash and does NOT follow redirects — a 307 here breaks the connector
    # (observed live 2026-07-09). Both spellings must serve directly.
    with TestClient(openapi_app.app) as client:
        for path in ("/mcp", "/mcp/"):
            response = client.post(
                path,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "slash-test", "version": "0"}}},
                headers=ACCEPT_HEADERS,
                follow_redirects=False,
            )
            assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
            assert response.headers.get("mcp-session-id"), f"{path}: no session id"
