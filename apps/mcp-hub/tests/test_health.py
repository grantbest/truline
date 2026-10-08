"""GET /health carries the build's provenance (git_sha), not just liveness.

`kubectl rollout status` and this endpoint's own `status: ok` both reported
healthy throughout the nine days prod silently served a pre-#614 commit --
the pod was genuinely fine, it was just running old code. git_sha makes the
served commit observable from outside the cluster without trusting the
rollout's own verdict on itself.
"""

import openapi_app
from fastapi.testclient import TestClient


def test_health_reports_unknown_git_sha_by_default(monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    with TestClient(openapi_app.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "git_sha": "unknown"}


def test_health_reports_the_build_time_git_sha(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    with TestClient(openapi_app.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "git_sha": "abc1234"}
