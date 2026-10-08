"""get_grafana_alerts — real Alertmanager query (Wave M7)."""

import httpx

from tools import homelab


def _am_alert(name, severity=None, summary="", state="active", starts="2026-07-12T22:00:00Z"):
    labels = {"alertname": name}
    if severity is not None:
        labels["severity"] = severity
    return {
        "labels": labels,
        "annotations": {"summary": summary},
        "status": {"state": state},
        "startsAt": starts,
    }


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def test_filters_watchdog_and_sorts_by_severity(monkeypatch):
    payload = [
        _am_alert("Watchdog", "none"),
        _am_alert("Host-BNodeDown", "warning", "host-b node_exporter unreachable"),
        _am_alert("InternetDown", "critical", "Internet is down"),
    ]
    monkeypatch.setattr(homelab.httpx, "get", lambda *a, **kw: _StubResponse(payload))

    alerts = homelab.get_grafana_alerts()

    assert [a["alert"] for a in alerts] == ["InternetDown", "Host-BNodeDown"]
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["summary"] == "Internet is down"
    assert alerts[0]["since"] == "2026-07-12T22:00:00Z"


def test_only_watchdog_means_no_alerts(monkeypatch):
    payload = [_am_alert("Watchdog", "none")]
    monkeypatch.setattr(homelab.httpx, "get", lambda *a, **kw: _StubResponse(payload))

    assert homelab.get_grafana_alerts() == []


def test_queries_active_unsilenced(monkeypatch):
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        return _StubResponse([])

    monkeypatch.setattr(homelab.httpx, "get", fake_get)
    homelab.get_grafana_alerts()

    assert calls["url"].endswith("/api/v2/alerts")
    assert calls["params"]["active"] == "true"
    assert calls["params"]["silenced"] == "false"


def test_respects_alertmanager_url_env(monkeypatch):
    calls = {}
    monkeypatch.setenv("ALERTMANAGER_URL", "http://am.example:9093")
    monkeypatch.setattr(
        homelab.httpx, "get",
        lambda url, **kw: calls.setdefault("url", url) and _StubResponse([]) or _StubResponse([]),
    )
    homelab.get_grafana_alerts()
    assert calls["url"] == "http://am.example:9093/api/v2/alerts"


def test_unreachable_alertmanager_returns_error_entry(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(homelab.httpx, "get", boom)

    alerts = homelab.get_grafana_alerts()

    assert len(alerts) == 1
    assert alerts[0]["error"] == "Failed to reach Alertmanager"
    assert "no route" in alerts[0]["detail"]


def test_http_error_returns_error_entry(monkeypatch):
    monkeypatch.setattr(
        homelab.httpx, "get", lambda *a, **kw: _StubResponse([], status_code=503)
    )

    alerts = homelab.get_grafana_alerts()

    assert alerts[0]["error"] == "Failed to reach Alertmanager"
