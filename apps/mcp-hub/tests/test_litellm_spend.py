"""get_litellm_spend -- Prometheus-backed LiteLLM spend."""

from datetime import UTC, datetime

import httpx

from tools import homelab


QUERY_TIME = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def _prometheus_result(*series):
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def _spend_series(model, value):
    return {"metric": {"model": model}, "value": [1785528000.0, str(value)]}


def test_returns_prometheus_spend_by_model(monkeypatch):
    calls = {}
    monkeypatch.setattr(homelab, "_current_utc_month_window", lambda: (QUERY_TIME, 2_678_400))

    def fake_get(url, params=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        calls["timeout"] = timeout
        return _StubResponse(
            _prometheus_result(
                _spend_series("gemini-2.5-pro", "1.240187"),
                _spend_series("gemini-2.5-flash", "0.113604"),
                _spend_series("gemini-embedding-001", "0.036854"),
            )
        )

    monkeypatch.setattr(homelab.httpx, "get", fake_get)

    spend = homelab.get_litellm_spend()

    assert spend == {
        "gemini-2.5-pro": 1.240187,
        "gemini-2.5-flash": 0.113604,
        "gemini-embedding-001": 0.036854,
    }
    assert calls["url"] == "http://prometheus-operated.monitoring.svc:9090/api/v1/query"
    assert calls["timeout"] == 5.0
    assert (
        calls["params"]["query"]
        == "sum by (model) (increase(litellm_spend_metric_total[2678400s]))"
    )
    assert calls["params"]["time"] == QUERY_TIME.timestamp()


def test_respects_prometheus_url_env(monkeypatch):
    calls = {}
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus.example:9090")
    monkeypatch.setattr(homelab, "_current_utc_month_window", lambda: (QUERY_TIME, 60))

    def fake_get(url, **kwargs):
        calls["url"] = url
        return _StubResponse(_prometheus_result(_spend_series("gemini-2.5-flash", 0)))

    monkeypatch.setattr(homelab.httpx, "get", fake_get)

    assert homelab.get_litellm_spend() == {"gemini-2.5-flash": 0.0}
    assert calls["url"] == "http://prometheus.example:9090/api/v1/query"


def test_unreachable_prometheus_returns_error_entry(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(homelab.httpx, "get", boom)

    spend = homelab.get_litellm_spend()

    assert spend["error"] == "Failed to reach Prometheus"
    assert "no route" in spend["detail"]


def test_http_error_returns_error_entry(monkeypatch):
    monkeypatch.setattr(
        homelab.httpx,
        "get",
        lambda *args, **kwargs: _StubResponse(_prometheus_result(), status_code=503),
    )

    spend = homelab.get_litellm_spend()

    assert spend["error"] == "Failed to reach Prometheus"


def test_absent_metric_is_distinguishable_from_zero_spend(monkeypatch):
    monkeypatch.setattr(
        homelab.httpx,
        "get",
        lambda *args, **kwargs: _StubResponse(_prometheus_result()),
    )

    spend = homelab.get_litellm_spend()

    assert spend["error"] == "LiteLLM spend metric absent"
    assert "litellm_spend_metric_total" in spend["detail"]
