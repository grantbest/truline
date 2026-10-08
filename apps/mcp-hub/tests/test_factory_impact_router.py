"""Route-level coverage for POST /api/v1/factory/impact's failure modes that
test_impact.py can't reach by calling ``tools.impact`` directly: the router's
own translation of ``impact.Unavailable`` to a 503, exercised through the
real (unmocked) ``get_impact`` orchestrator when the packaged substrate
client can't even be loaded (round-2 #1020 gate finding -- an unset
``FACTORY_DISPATCHER_ROOT`` must never surface as "no bead with that ref").
"""

from fastapi.testclient import TestClient

import openapi_app

IMPACT_PATH = "/api/v1/factory/impact"

# What the console's own browser traffic carries after Cloudflare Access
# authenticates and Traefik injects identity headers -- same shape
# test_factory_router_access.py uses.
HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}


def test_impact_returns_503_not_404_when_factory_dispatcher_root_is_unset(monkeypatch):
    # tools.finance._substrate_client_src_dir raises RuntimeError with this
    # var unset -- every one of impact.py's population reads fails the same
    # way, so the "application" collection (this request's own kind) is
    # among the failed types, and get_impact must raise Unavailable rather
    # than let compute_impact report a fabricated "not found".
    monkeypatch.delenv("FACTORY_DISPATCHER_ROOT", raising=False)

    with TestClient(openapi_app.app) as client:
        response = client.post(
            IMPACT_PATH, json={"kind": "application", "ref": "app.example"}, headers=HUMAN
        )

    assert response.status_code == 503
    assert "arch.application" in response.text
