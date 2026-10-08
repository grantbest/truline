from __future__ import annotations

from fastapi.testclient import TestClient

import openapi_app
from workflows import finance_reconciliation
from workflows.finance_reconciliation import compute_ledger_variance


FINANCE_READ_HEADERS = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "finance.read",
}


def _disc(
    account_id,
    *,
    first_drift,
    first_at="2026-07-11T03:45:00",
    state="pending",
    name="Checking",
):
    return {
        "id": f"disc-{account_id}-{first_at}",
        "state": state,
        "content": {
            "account_id": account_id,
            "account_name": name,
            "first_observed_drift": first_drift,
            "first_observed_at": first_at,
            "drift": first_drift,
        },
    }


def _row(report, account_id):
    return next(r for r in report["accounts"] if r["account_id"] == account_id)


def test_ledger_variance_endpoint_returns_the_wrapper_result(monkeypatch):
    discrepancies = [
        _disc("chk", first_drift=-400.0, first_at="2026-07-11T03:45:00"),
        _disc("sav", first_drift=125.50, first_at="2026-06-01T03:45:00", name="Savings"),
    ]
    accounts = [
        {"id": "chk", "content": {"name": "Checking"}},
        {"id": "sav", "content": {"name": "Savings"}},
        {"id": "new", "content": {"name": "New Account"}},
    ]

    async def fake_query_beads(params):
        if params["type"] == "audit_discrepancy":
            return discrepancies
        if params["type"] == "account":
            return accounts
        raise AssertionError(f"unexpected query: {params}")

    monkeypatch.setattr(finance_reconciliation, "query_beads", fake_query_beads)

    expected = compute_ledger_variance(
        discrepancies,
        [
            {"id": "chk", "name": "Checking"},
            {"id": "sav", "name": "Savings"},
            {"id": "new", "name": "New Account"},
        ],
    )
    with TestClient(openapi_app.app) as client:
        response = client.get(
            "/api/v1/finance/ledger_variance",
            headers=FINANCE_READ_HEADERS,
        )

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_ledger_variance_endpoint_reports_no_anchor_as_not_yet_measuring(monkeypatch):
    async def fake_query_beads(params):
        if params["type"] == "audit_discrepancy":
            return [_disc("chk", first_drift=-400.0)]
        if params["type"] == "account":
            return [
                {"id": "chk", "content": {"name": "Checking"}},
                {"id": "new", "content": {"name": "New Account"}},
            ]
        raise AssertionError(f"unexpected query: {params}")

    monkeypatch.setattr(finance_reconciliation, "query_beads", fake_query_beads)

    with TestClient(openapi_app.app) as client:
        response = client.get(
            "/api/v1/finance/ledger_variance",
            headers=FINANCE_READ_HEADERS,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    row = _row(body, "new")
    assert row["unexplained_variance"] is None
    assert row["anchor_date"] is None
    assert body["total"]["accounts_not_measured"] == 1


def test_ledger_variance_endpoint_says_when_data_cannot_be_read(monkeypatch):
    async def unreadable(_params):
        raise RuntimeError("substrate unavailable")

    monkeypatch.setattr(finance_reconciliation, "query_beads", unreadable)

    with TestClient(openapi_app.app) as client:
        response = client.get(
            "/api/v1/finance/ledger_variance",
            headers=FINANCE_READ_HEADERS,
        )

    assert response.status_code == 502
    assert "Ledger variance read error" in response.json()["detail"]
    assert "substrate unavailable" in response.json()["detail"]
