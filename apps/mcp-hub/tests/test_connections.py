"""Tests for tools.connections — the /finance/connections status feed (PR 11)."""

import pytest

from tools import connections as conn


def _item_bead(slug, env="sandbox", cursor=None, last_synced=None, item_id="item-1"):
    content = {
        "institution": slug,
        "plaid_env": env,
        "item_id": item_id,
        "linked_at": "2026-06-01T00:00:00",
    }
    if cursor:
        content["transactions_cursor"] = cursor
    if last_synced:
        content["last_synced"] = last_synced
    return {"id": f"bead-{slug}", "content": content}


def _account_bead(slug, last_synced=None):
    content = {"institution": slug}
    if last_synced:
        content["last_synced"] = last_synced
    return {"id": f"acct-{slug}", "content": content}


def _fake_query_beads(item_beads, account_beads):
    async def fake(params):
        return item_beads if params.get("type") == "plaid_item" else account_beads
    return fake


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("MCP_HUB_PUBLIC_URL", "https://mcp-hub.test")
    monkeypatch.delenv("PLAID_ACCESS_TOKEN_CHASE", raising=False)
    monkeypatch.delenv("PLAID_ACCESS_TOKEN_AMEX", raising=False)
    return monkeypatch


async def test_merges_env_tokens_and_item_beads(env):
    # chase: token + bead. amex: bead only (token removed — dead sync).
    env.setenv("PLAID_ACCESS_TOKEN_CHASE", "access-1")
    env.setattr(
        conn,
        "query_beads",
        _fake_query_beads(
            [
                _item_bead("chase", cursor="cur-1", last_synced="2026-06-12T03:00:00"),
                _item_bead("amex"),
                _item_bead("prod_only", env="production"),  # filtered: wrong env
            ],
            [
                _account_bead("chase", last_synced="2026-06-12T03:01:00"),
                _account_bead("chase", last_synced="2026-06-11T03:01:00"),
            ],
        ),
    )

    async def fake_probe(slug):
        return {"healthy": slug == "chase", "error_code": None, "repairable": False}
    env.setattr(conn, "_probe_item", fake_probe)

    result = await conn.get_connections_status()

    assert result["plaid_env"] == "sandbox"
    assert [i["institution"] for i in result["institutions"]] == ["amex", "chase"]

    chase = result["institutions"][1]
    assert chase["token_present"] is True
    assert chase["cursor_present"] is True
    assert chase["last_synced"] == "2026-06-12T03:00:00"
    assert chase["account_count"] == 2
    assert chase["accounts_last_synced"] == "2026-06-12T03:01:00"
    # This fixture stubs no transactions, so there is no cadence to learn and no
    # freshness evidence to judge. Since LO-OBS-002, `ok` asserts that money data
    # is arriving — which nothing here has shown — so the honest status is that
    # freshness is unmeasurable. The merge behaviour this test covers is unchanged.
    assert chase["status"] == "freshness_unknown"
    assert chase["link_url"] == "https://mcp-hub.test/finance/link/chase"
    assert chase["repair_url"] == "https://mcp-hub.test/finance/link/chase?mode=update"

    amex = result["institutions"][0]
    assert amex["token_present"] is False
    assert amex["account_count"] == 0
    assert amex["status"] == "no_token"


async def test_probe_false_skips_plaid_and_reports_unprobed(env):
    env.setenv("PLAID_ACCESS_TOKEN_CHASE", "access-1")
    env.setattr(conn, "query_beads", _fake_query_beads([], []))

    async def boom(slug):  # pragma: no cover - must not be called
        raise AssertionError("probe ran with probe=False")
    env.setattr(conn, "_probe_item", boom)

    result = await conn.get_connections_status(probe=False)

    assert result["probed"] is False
    (chase,) = result["institutions"]
    assert chase["healthy"] is None
    assert chase["status"] == "unprobed"


async def test_status_maps_repairable_vs_relink(env):
    env.setenv("PLAID_ACCESS_TOKEN_CHASE", "access-1")
    env.setenv("PLAID_ACCESS_TOKEN_AMEX", "access-2")
    env.setattr(conn, "query_beads", _fake_query_beads([], []))

    async def fake_probe(slug):
        if slug == "chase":  # update-mode repairable
            return {"healthy": False, "error_code": "ITEM_LOGIN_REQUIRED", "repairable": True}
        return {"healthy": False, "error_code": "ITEM_NOT_FOUND", "repairable": False}
    env.setattr(conn, "_probe_item", fake_probe)

    result = await conn.get_connections_status()
    by_slug = {i["institution"]: i for i in result["institutions"]}

    assert by_slug["chase"]["status"] == "reauth_required"
    assert by_slug["amex"]["status"] == "relink_required"


async def test_probe_item_classifies_sdk_exception(env):
    env.setenv("PLAID_ACCESS_TOKEN_CHASE", "access-1")

    class FakeApiException(Exception):
        body = '{"error_code": "ITEM_LOGIN_REQUIRED"}'

    async def fake_get_item_status(token):
        raise FakeApiException()
    env.setattr(conn, "get_item_status", fake_get_item_status)

    result = await conn._probe_item("chase")

    assert result == {
        "healthy": False,
        "error_code": "ITEM_LOGIN_REQUIRED",
        "repairable": True,
    }


async def test_probe_item_healthy_with_benign_item_error(env):
    env.setenv("PLAID_ACCESS_TOKEN_CHASE", "access-1")

    async def fake_get_item_status(token):
        return {"item": {"item_id": "i-1", "error": {"error_code": "RATE_LIMIT"}}}
    env.setattr(conn, "get_item_status", fake_get_item_status)

    result = await conn._probe_item("chase")

    assert result["healthy"] is True
    assert result["error_code"] == "RATE_LIMIT"


async def test_probe_item_no_token(env):
    result = await conn._probe_item("chase")
    assert result == {"healthy": False, "error_code": "NO_TOKEN", "repairable": False}
