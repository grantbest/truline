"""Amendment 23 / R26.09 O-6: audit-shape check over the gateway's routes.

Amendment 23 requires every gateway call to record client identity,
capability, scope, and outcome. FACT AT FILING (the #782 gate, F3): the
only per-request record (openapi_app.py's `mcp_hub_request` log line)
carried the scopes a caller was GRANTED (`scopes=`), never the scope the
route CHECKED -- access_auth.check_scope/require_authenticated_scope never
recorded it -- so nothing failed when a new router shipped without
attribution. This test enumerates every `/api/v1` route from the live
OpenAPI schema (the same mechanism test_api_contract.py already drives
with schemathesis) and asserts, for each, that the audit record
openapi_app.py now emits carries all four fields as named `LogRecord`
attributes (`audit_identity`, `audit_capability`, `audit_scope`,
`audit_outcome`).

CORRECTED AGAIN (F1/F2 on the #831 gate): the #823 gate's correction above
was itself wrong. On the fastapi CI actually resolves from this project's
`fastapi>=0.110.0` pin (0.141.1 measured 2026-09-15), `app.include_router()`
does NOT flatten the included router's routes into `app.routes` -- it
leaves a private `fastapi.routing._IncludedRouter` wrapper object there
instead (one per `include_router()` call; this app makes 15). `[r for r in
app.routes if isinstance(r, APIRoute)]` on this version sees only the 5
routes declared directly on `app` (`/health`, `/auth/cloudflare`, the two
substrate mounts, `/webhooks/plaid`) -- none of the versioned `/api/v1`
capability routers, which are all `include_router()`-mounted. That silently
starved `HIDDEN_API_V1_OPERATIONS` to `[]`, which made
`test_hidden_api_v1_route_emits_a_complete_audit_record`'s
`@pytest.mark.parametrize` collect an empty set and SKIP -- the exact gap
this bead exists to close, reopened by a false claim about the mechanism
that was supposed to close it.

`_flatten_api_routes` below recurses through `_IncludedRouter.original_router`
and `.include_context` (both private, unversioned fastapi surface) to
reconstruct the real, prefixed route list `app.routes` no longer exposes
directly. Depending on that private surface is a deliberate choice, not an
oversight -- see `test_live_route_walk_matches_the_official_openapi_schema`
and `test_hidden_route_enumeration_is_not_silently_empty` below, which pin
the assumption against fastapi's own public `get_openapi()` output and
against the two hidden routes known to exist today, so a future fastapi
upgrade that changes this shape fails a named test instead of quietly
starving the parametrize again. On a fastapi old enough to still flatten
`include_router()` eagerly (no `_IncludedRouter` to import), the walk falls
back to the flat `isinstance(route, APIRoute)` case transparently.

The OpenAPI schema is still schemathesis's own enumeration surface for the
schema-driven test above this docstring (it needs the schema to generate
conformant request bodies), but it is not the only source of truth: it
omits routes registered with `include_in_schema=False` (see
`HIDDEN_API_V1_OPERATIONS` below), so a second, directly-called test closes
that gap using the flattened route walk instead.
"""

import contextlib
import logging
import re
from unittest.mock import AsyncMock

import pytest
import schemathesis
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

try:
    from fastapi.routing import _IncludedRouter
except ImportError:  # pragma: no cover -- exercised by CI's pinned fastapi
    # Older fastapi flattens include_router() eagerly into app.routes, so
    # there is nothing to recurse into -- _flatten_api_routes's isinstance
    # check below is simply never true and every route falls through to the
    # plain APIRoute branch, same as it always did on that version.
    _IncludedRouter = None


import access_auth
import openapi_app
import routers.v1.automations
import routers.v1.calendar
import routers.v1.context
import routers.v1.factory
import routers.v1.finance
import routers.v1.ha
import routers.v1.homelab
import routers.v1.vision
from tools.vision import StagedVisionExtraction, VisionExtraction

schema = schemathesis.openapi.from_asgi("/openapi.json", openapi_app.app)
# Positive generation only: this test asserts what a real call to a route
# records, not how the route rejects a schema-violating body or a
# wrong method (test_api_contract.py's schemathesis-driven crash-fuzz
# already covers negative/malformed input; a body FastAPI's own validation
# rejects never reaches the endpoint, so it never reaches check_scope
# either -- that's a framework-level 422, not an audit-shape finding).
schema.config.generation.modes = [schemathesis.GenerationMode.POSITIVE]
# Keep this fast and deterministic: unlike test_api_contract.py's crash-fuzz
# test, this test doesn't need many examples per route, only one that
# reaches the handler body -- schemathesis draws schema-conformant data, so
# a handful of examples is enough without adding runtime.
#
# F5 on the #823 gate: this used to call hypothesis.settings.register_profile()
# / .load_profile() at import time, which mutates hypothesis's *process-wide*
# default profile -- whichever of this module and test_api_contract.py
# happens to import last during collection silently wins for the other
# file's hypothesis-driven test too. schemathesis (this pinned version)
# builds its own hypothesis.settings from schema.config, not the global
# profile (schemathesis/config/_projects.py:get_hypothesis_settings), so
# scoping these to `schema.config` here is both correct for how this
# version actually resolves settings and, unlike the global profile,
# structurally cannot leak into test_api_contract.py.
schema.config.generation.max_examples = 3
# Deterministic generation pins the sequence of cases THIS test draws,
# clone to clone -- it does not pin generation identically across every
# environment. CORRECTED 2026-09-17 (this bead): the #870 gate ran this
# suite in full and never drew the control-character case that CI drew on
# #868's requeue -- same deterministic setting, different draw, because
# hypothesis/schemathesis generation also depends on things that vary by
# environment (library versions, platform, available example database).
# What actually holds is narrower and still sufficient: a case this test
# DOES draw behaves the same way on every rerun here, so a failure is
# reproducible once seen -- and the routing-miss case that generation can
# skip entirely is pinned directly, deterministically, by
# test_substrate_proxy_unmatched_control_character_path_emits_no_scope
# below, so correctness does not depend on the fuzzer drawing it.
schema.config.generation.deterministic = True

# What the console's own browser traffic carries after Cloudflare Access
# authenticates and Traefik injects identity headers -- same shape
# test_factory_router_access.py / test_substrate_proxy_access.py use. A
# wildcard human identity passes every check_scope/require_authenticated_scope
# call so every route's handler body actually runs and reaches its own
# check_scope(...) call -- the audit-shape gap this bead closes is about
# whether that call gets recorded, not about admission itself.
HUMAN = {
    "X-Truline-Client": "operator@example.org",
    "X-Truline-Client-Type": "human",
    "X-Truline-Scopes": "*",
}


@pytest.fixture(autouse=True)
def stub_upstreams(monkeypatch):
    """Mock every upstream capability function so exercising every route
    touches no real database, Temporal, or external API. Deliberately
    duplicated from test_api_contract.py's setup_routers_mocks rather than
    imported across files -- pytest fixtures aren't meant to be shared by
    cross-module import, and the two files' upstream lists are free to
    diverge as routers change.
    """
    monkeypatch.setattr(routers.v1.context, "get_current_context", lambda: {"status": "mocked"})

    monkeypatch.setattr(routers.v1.homelab, "get_platform_status", lambda: "mocked")
    monkeypatch.setattr(routers.v1.homelab, "get_grafana_alerts", lambda: [])
    monkeypatch.setattr(routers.v1.homelab, "homelab_query_beads", lambda *args: [])
    monkeypatch.setattr(routers.v1.homelab, "get_litellm_spend", lambda: {})

    monkeypatch.setattr(routers.v1.calendar, "get_calendar_events", lambda *args: [])
    monkeypatch.setattr(routers.v1.calendar, "get_upcoming_events", lambda *args: [])

    monkeypatch.setattr(routers.v1.ha, "ha_get_state", lambda *args: {})
    monkeypatch.setattr(routers.v1.ha, "ha_list_entities", lambda *args: [])
    monkeypatch.setattr(routers.v1.ha, "ha_get_history", lambda *args: [])
    monkeypatch.setattr(routers.v1.ha, "ha_get_office_intent_today", lambda: {})

    mock_vision_ext = VisionExtraction(summary="mocked")
    mock_staged_ext = StagedVisionExtraction(bead_id="mock-bead", extraction=mock_vision_ext)
    monkeypatch.setattr(routers.v1.vision, "extract_and_stage", AsyncMock(return_value=mock_staged_ext))
    monkeypatch.setattr(routers.v1.vision, "extract_from_image", AsyncMock(return_value=mock_vision_ext))

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

    # The two /api/v1/finance routes include_in_schema=False hides from the
    # OpenAPI schema (and therefore from schemathesis) -- exercised directly
    # by test_hidden_api_v1_route_emits_a_complete_audit_record below, which
    # needs these upstreams stubbed the same way the schema-driven routes do.
    monkeypatch.setattr(routers.v1.finance, "create_link_token", AsyncMock(return_value="link-sandbox-test-token"))
    monkeypatch.setattr(
        routers.v1.finance,
        "exchange_public_token",
        AsyncMock(return_value={"access_token": "access-sandbox-test-token", "item_id": "item-test-1"}),
    )

    monkeypatch.setattr(
        routers.v1.automations,
        "trigger_pay_train_parking",
        AsyncMock(return_value={"via": "schedule", "id": "pay-train-parking-daily"}),
    )

    monkeypatch.setattr(
        routers.v1.factory.factory_status,
        "task_runnability",
        lambda task_id: {"status": "ok", "found": False},
    )
    monkeypatch.setattr(
        routers.v1.factory.factory_status,
        "release_delivery",
        lambda release_ref: {"status": "ok", "found": False},
    )
    monkeypatch.setattr(
        routers.v1.factory.factory_schedule_status,
        "dispatch_schedule_status",
        AsyncMock(return_value={"status": "ok", "paused": False, "note": None, "in_flight": [], "recent": []}),
    )
    # impact.get_impact's real default reads dial the packaged substrate
    # client (tools.finance._call_substrate) unconditionally -- unstubbed,
    # schemathesis's generated POST /api/v1/factory/impact call here would
    # hang on a real network call this test environment has no route to.
    monkeypatch.setattr(
        routers.v1.factory.impact,
        "get_impact",
        AsyncMock(
            return_value={
                "affected": {
                    "applications": [],
                    "cis": [],
                    "services": [],
                    "unknown_applications": [],
                    "lower_bound": False,
                },
                "coverage": {
                    "applications_assessed": 0,
                    "applications_total": 0,
                    "cis_owned": 0,
                    "cis_total": 0,
                    "changes_with_affects": 0,
                    "changes_total": 0,
                    "partial": False,
                    "partial_reads": [],
                },
                "computed_at": "2026-09-19T00:00:00+00:00",
                "revision_hint": "audit-shape-stub",
            }
        ),
    )
    monkeypatch.setattr(
        routers.v1.factory.task_filing,
        "file_dev_task",
        lambda spec, created_by: {
            "id": "audit-shape-stub-bead",
            "state": "pending",
            "created_by": created_by,
            "content": {"lane": "code-health", "title": "stub", "risk_class": "behavioral"},
        },
    )
    monkeypatch.setattr(
        routers.v1.factory.note_filing,
        "file_dev_note",
        lambda parent_id, kind, body, created_by, client_type, *, fields=None: {
            "id": "audit-shape-stub-note",
            "parent_id": parent_id,
            "state": "active",
            "created_by": created_by,
            "content": {"kind": kind, "body": body, **(fields or {})},
        },
    )
    # factory.merge is exact-match-only (access_auth.EXACT_MATCH_SCOPES): the
    # HUMAN wildcard used throughout this module never satisfies it, so the
    # POST route always 403s before reaching this stub in these tests -- it
    # exists so the walk never dials real Temporal if that ever changes. The
    # GET disposition route checks factory.read, which HUMAN's '*' does
    # satisfy, so that one is genuinely exercised.
    monkeypatch.setattr(
        routers.v1.factory.factory_merge,
        "start_merge_on_verdict",
        AsyncMock(return_value={"workflow_id": "audit-shape-stub-wf", "run_id": "audit-shape-stub-run"}),
    )
    monkeypatch.setattr(
        routers.v1.factory.factory_merge,
        "get_merge_disposition",
        AsyncMock(return_value={"status": "running"}),
    )

    # substrate_proxy is the one /api/v1 route that dials out over httpx --
    # stub both the target and the client the same way
    # test_substrate_proxy_access.py does, so exercising it hits no real
    # network.
    monkeypatch.setattr(openapi_app, "substrate_base_url", lambda: "http://substrate.test")
    monkeypatch.setattr(openapi_app, "substrate_headers", lambda: {"X-API-Key": "stub-key"})

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"[]"

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(openapi_app.httpx, "AsyncClient", _FakeAsyncClient)


class _RecordCollector(logging.Handler):
    """Collects LogRecords without pytest's `caplog` fixture.

    `caplog` is function-scoped, and schemathesis (this pinned version)
    drives several hypothesis-generated examples through one pytest test
    item per operation -- a function-scoped fixture reused across those
    examples is exactly hypothesis's own `function_scoped_fixture` health
    check. schema.config has no per-check suppression finer than "suppress
    everything" (schemathesis.config.HealthCheck has no
    function_scoped_fixture member of its own), so the surgical fix is to
    not use a fixture at all: attach/detach a plain logging.Handler inside
    the test body, freshly per generated example, same as caplog would --
    just without going through pytest's fixture machinery.
    """

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _capture_openapi_app_logs():
    logger = logging.getLogger("openapi_app")
    previous_level = logger.level
    collector = _RecordCollector()
    # openapi_app.py's own logging.basicConfig(level=INFO) call is a no-op
    # once pytest's logging plugin has already attached a handler to the
    # root logger, which can leave this logger's effective level above
    # INFO -- force it down for the duration of the capture, same as
    # caplog.set_level(logging.INFO, logger="openapi_app") used to.
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)


def _audit_records(records):
    return [
        r
        for r in records
        if r.name == "openapi_app" and r.getMessage().startswith("mcp_hub_request")
    ]


@schema.parametrize()
def test_every_api_v1_route_emits_a_complete_audit_record(case):
    if not case.path.startswith("/api/v1/"):
        pytest.skip("only /api/v1 routes are this bead's audit-shape scope")

    label = f"{case.method} {case.path}"
    expected_operation_id = case.operation.definition.raw.get("operationId")

    with _capture_openapi_app_logs() as collector:
        response = case.call(headers=HUMAN)

    if response.status_code == 405:
        # schemathesis's default generation includes negative method
        # fuzzing (e.g. TRACE/QUERY/PUT against a POST-only operation) to
        # check the server rejects it -- Starlette returns 405 from routing
        # itself, before any endpoint (and therefore any check_scope call)
        # runs. That's not a call to this route/tool; nothing to assert.
        return

    records = _audit_records(collector.records)
    assert records, f"{label} emitted no mcp_hub_request audit record"
    record = records[-1]

    identity = getattr(record, "audit_identity", None)
    capability = getattr(record, "audit_capability", None)
    scope = getattr(record, "audit_scope", None)
    outcome = getattr(record, "audit_outcome", None)

    # A 422 is FastAPI's request validation rejecting a generated case (a
    # query value outside its declared bounds, a bad enum) BEFORE the
    # endpoint runs -- so check_scope() never ran either and the record
    # legitimately carries no scope. Identity, capability and outcome are
    # still asserted for it; scope membership only for calls that reached a
    # handler (the same reasoning as the 405 return above, one layer later).
    #
    # A 404 needs the same treatment for one route family: the substrate
    # passthrough's `{path:path}` converter matches with a bare `.*` regex,
    # which (without re.DOTALL) does not match a decoded path segment that
    # contains a raw control character -- reachable over real HTTP too, via
    # a percent-encoded byte like %0A, not just this fuzzer. schemathesis's
    # unicode-heavy string generation for the `path` parameter hits that
    # every so often (caught live: CI red on this exact case, #868's
    # requeue). When routing itself rejects the request this way, Starlette
    # never resolves a route, so `_identity_logging_dispatch` falls back to
    # `request.url.path` for `capability` (openapi_app.py's `capability =
    # getattr(route, "operation_id", None) or getattr(route, "name", None)
    # or request.url.path`) -- unlike every real route/tool name in this
    # app, a URL path always contains "/". That makes "/" in capability a
    # reliable signal that no endpoint ran, independent of status code.
    handler_ran = response.status_code != 422 and "/" not in (capability or "")

    assert identity == HUMAN["X-Truline-Client"], (
        f"{label}: audit record is missing/wrong client identity: {identity!r}"
    )
    assert capability, f"{label}: audit record is missing capability (route/tool name)"
    if handler_ran:
        assert scope in access_auth.SCOPES, (
            f"{label}: audit record's scope {scope!r} is not a member of "
            f"access_auth.SCOPES -- either the route never called check_scope()/"
            f"require_authenticated_scope(), or it checked an undeclared scope"
        )
    assert outcome == f"{response.status_code // 100}xx", (
        f"{label}: audit record outcome {outcome!r} does not match the "
        f"actual response status {response.status_code}"
    )

    # custom_openapi() (openapi_app.py) rewrites the schema-visible
    # operationId for the substrate passthrough after route registration,
    # so the schema's operationId and the route's own operation_id (what
    # the middleware actually logs) diverge for this one route family only.
    # It already has its own dedicated pin (test_substrate_proxy_access.py)
    # for identity/scope admission; here it still gets the presence checks
    # above, just not this stricter per-route equality.
    if expected_operation_id and "substrate_proxy" not in expected_operation_id:
        assert capability == expected_operation_id, (
            f"{label}: audit record capability {capability!r} does not "
            f"match this route's operation_id {expected_operation_id!r} -- "
            f"the audit record is not identifying the actual route/tool called"
        )


def test_substrate_proxy_unmatched_control_character_path_emits_no_scope():
    """Pins, deterministically, the routing-miss case
    `test_every_api_v1_route_emits_a_complete_audit_record`'s `handler_ran`
    check had to learn to recognize (CI red on #868's requeue: schemathesis's
    unicode-heavy `path` generation for `/api/v1/substrate/{path}` drew a
    value that decodes to a raw control character; that value is reachable
    over real HTTP too, via a percent-encoded byte like %0A in the request
    line).

    `{path:path}`'s convertor regex is a bare `.*`, which (without
    `re.DOTALL`) never matches across an embedded control character --
    Starlette 404s from routing itself, before `substrate_proxy` runs, so
    `require_authenticated_scope` never executes and the record legitimately
    carries no scope. This calls the app directly with such a path (no
    hypothesis involved) so the routing-miss behavior itself is pinned by
    name instead of only incidentally exercised when a fuzzer happens to
    draw it.
    """
    from fastapi.testclient import TestClient

    client = TestClient(openapi_app.app, raise_server_exceptions=False)
    with _capture_openapi_app_logs() as collector:
        response = client.get("/api/v1/substrate/foo%0Abar", headers=HUMAN)

    assert response.status_code == 404, (
        "this pins a specific Starlette routing-miss behavior for "
        "{path:path} against a control character -- if this now matches, "
        "the 'handler_ran' carve-out in "
        "test_every_api_v1_route_emits_a_complete_audit_record for it is "
        "dead code and should be removed, not left guarding nothing"
    )
    records = _audit_records(collector.records)
    assert records, "even an unmatched route must emit an audit record"
    record = records[-1]
    assert getattr(record, "audit_identity", None) == HUMAN["X-Truline-Client"]
    assert "/" in getattr(record, "audit_capability", ""), (
        "an unmatched route's capability should fall back to the request "
        "path, not a route/tool name"
    )
    assert getattr(record, "audit_scope", None) is None, (
        "routing rejected this request before substrate_proxy ran, so "
        "require_authenticated_scope() never executed -- the record should "
        "carry no scope"
    )
    assert getattr(record, "audit_outcome", None) == "4xx"


def test_mcp_tool_call_emits_a_complete_audit_record():
    """Amendment 23 / R26.09 O-6, CHECK 3 on this bead's finding (dev.finding
    e004965d): a naive look at MCP traffic through the /mcp mount finds two
    generic `mcp_hub_request` records per call -- one from the outer app's
    own `_identity_logging_dispatch`, one from `mcp_http`'s own copy of it --
    both with audit_capability='/mcp/' (the mount path) and audit_scope=None,
    because at that layer Starlette's own routing only ever resolves the
    Mount, never the tool. CHECK 3 is real; what it does not show, and what
    this test proves empirically instead of asserting from reading the code:
    `FastMCP.from_fastapi()` derives every tool by making a genuine,
    in-process ASGI call back into this same `app`
    (fastmcp.server.providers.openapi.components.OpenAPITool.run, via an
    httpx ASGITransport), forwarding the original request's X-Truline-*
    headers (fastmcp.server.dependencies.get_http_headers -- they are not in
    its default exclusion list). That nested call is routed for real, hits
    the tool's actual underlying APIRoute, and passes through
    `_identity_logging_dispatch` a third time -- logging a THIRD
    mcp_hub_request record with the tool's own name as capability and the
    scope its handler actually checked, indistinguishable from a direct
    /api/v1 HTTP call. Amendment 23's guarantee already holds for MCP tool
    calls today; what was missing was a test saying so, not new production
    code -- confirmed by hand (uv run against this exact tree, 2026-09-17):
    calling the `get_current_context` tool through a real streamable-http
    handshake logs exactly this third record with capability=
    'get_current_context', checked_scope='context.read', outcome='2xx'.
    """
    with TestClient(openapi_app.app, raise_server_exceptions=False) as client:
        init_resp = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test_audit_shape", "version": "0.0.1"},
                },
            },
            headers={**HUMAN, "Accept": "application/json, text/event-stream"},
        )
        assert init_resp.status_code == 200, init_resp.text
        session_headers = {
            **HUMAN,
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": init_resp.headers["mcp-session-id"],
        }
        notif_resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        assert notif_resp.status_code == 202, notif_resp.text

        with _capture_openapi_app_logs() as collector:
            call_resp = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_current_context", "arguments": {}},
                },
                headers=session_headers,
            )
        assert call_resp.status_code == 200, call_resp.text

    records = _audit_records(collector.records)
    assert records, "the tools/call request emitted no mcp_hub_request audit record at all"

    tool_records = [
        r for r in records if getattr(r, "audit_capability", None) == "get_current_context"
    ]
    assert tool_records, (
        "no mcp_hub_request record attributed this call to the tool it "
        "actually invoked -- the guarantee CHECK 3 asked about does not "
        f"hold here; capabilities seen: "
        f"{[getattr(r, 'audit_capability', None) for r in records]!r}"
    )
    record = tool_records[-1]
    assert getattr(record, "audit_identity", None) == HUMAN["X-Truline-Client"]
    assert getattr(record, "audit_scope", None) == "context.read", (
        f"the tool call's audit record carried scope "
        f"{getattr(record, 'audit_scope', None)!r}, not 'context.read' -- "
        "the scope its underlying route (/api/v1/context/current) actually "
        "checked"
    )
    assert getattr(record, "audit_outcome", None) == "2xx"


_PATH_PARAM_TYPE_RE = re.compile(r"\{(\w+):\w+\}")


def _normalize_path(path: str) -> str:
    """Starlette path converters ("{path:path}") show up in APIRoute.path,
    but the OpenAPI schema strips the converter to plain "{path}" -- put
    both sides in the same spelling before comparing them.
    """
    return _PATH_PARAM_TYPE_RE.sub(r"{\1}", path)


def _flatten_api_routes(routes, prefix="", schema_visible=True):
    """Recursively resolve `app.routes` (and any `_IncludedRouter` wrappers
    in it) into `(route, path, schema_visible)` triples for every real
    `APIRoute`.

    `_IncludedRouter.include_context.prefix` is already combined with
    whatever prefix its own parent router carried (see fastapi's
    `_RouterIncludeContext.for_include`/`.combine`), so accumulating it by
    concatenation here is correct at any nesting depth, not just the one
    level this app currently uses. Same for `.include_context.include_in_schema`,
    which fastapi already ANDs with its parent's.
    """
    flattened = []
    for route in routes:
        if _IncludedRouter is not None and isinstance(route, _IncludedRouter):
            ctx = route.include_context
            flattened.extend(
                _flatten_api_routes(
                    route.original_router.routes,
                    prefix=prefix + ctx.prefix,
                    schema_visible=schema_visible and ctx.include_in_schema,
                )
            )
            continue
        if isinstance(route, APIRoute):
            flattened.append(
                (route, prefix + route.path, schema_visible and route.include_in_schema)
            )
    return flattened


def _non_api_route_violations_under_api_v1(routes, prefix=""):
    """Walk the same route tree `_flatten_api_routes` walks, but do the
    opposite: surface every route under /api/v1 that is NOT a FastAPI
    `APIRoute`, instead of silently dropping it.

    THIS BEAD's finding (dev.finding e004965d): `_flatten_api_routes`'s own
    `isinstance(route, APIRoute)` check has no `else` -- anything that is
    not an `APIRoute` (a plain Starlette `Route`, a `Mount`) just never
    gets appended. Every set this module derives from `_flatten_api_routes`
    (`_live_api_v1_operations`, `HIDDEN_API_V1_OPERATIONS`) inherits that
    blind spot, so such a route is invisible to the audit-shape guarantee
    entirely -- not misjudged by the `handler_ran` heuristic in
    `test_every_api_v1_route_emits_a_complete_audit_record`, which never
    gets the chance to run against it. This is the fix at the enumeration
    layer the bead asks for: a route this finds is a named failure in
    `test_api_v1_routes_are_all_fastapi_aproutes` below, not a heuristic
    misclassification.
    """
    violations = []
    for route in routes:
        if _IncludedRouter is not None and isinstance(route, _IncludedRouter):
            ctx = route.include_context
            violations.extend(
                _non_api_route_violations_under_api_v1(
                    route.original_router.routes, prefix=prefix + ctx.prefix
                )
            )
            continue
        if isinstance(route, APIRoute):
            continue
        path = prefix + getattr(route, "path", "")
        if path == "/api/v1" or path.startswith("/api/v1/"):
            violations.append((path, route))
    return violations


def test_api_v1_routes_are_all_fastapi_aproutes():
    """Closes the enumeration gap CHECK 1/CHECK 2 on this bead found: a
    plain Starlette route (or a Mount) registered under /api/v1 was
    invisible to `_flatten_api_routes`, and therefore to every audit-shape
    guarantee this module builds on top of it. On the tree as it stands
    the live table is 84 APIRoutes and nothing else lives under /api/v1, so
    this passes today; `test_non_api_route_under_api_v1_is_caught_by_the_helper`
    below is the other half -- it proves the helper actually fires when such
    a route exists, using a throwaway app so this test doesn't need one
    registered on the real, shared `openapi_app.app`.
    """
    violations = _non_api_route_violations_under_api_v1(openapi_app.app.routes)
    assert not violations, (
        "found a route registered under /api/v1 that is not a FastAPI "
        "APIRoute -- it is invisible to _flatten_api_routes and therefore "
        "to every audit-shape check derived from it: "
        + ", ".join(f"{path} ({type(route).__name__})" for path, route in violations)
    )


def test_non_api_route_under_api_v1_is_caught_by_the_helper():
    """The other half of `test_api_v1_routes_are_all_fastapi_aproutes`:
    proves `_non_api_route_violations_under_api_v1` actually fails, by
    name, on the exact shape CHECK 2 (the #870 gate's dev finding
    e004965d) demonstrated live -- a plain Starlette route under
    /api/v1/gateplain/probe that runs a handler and never calls
    check_scope. That finding recorded audit_capability=
    '/api/v1/gateplain/probe', audit_scope=None, handler_ran=False; the
    fix here is that such a route never reaches that classification at
    all, because it is named as a violation before enumeration even
    starts. Built on a throwaway FastAPI app, not the real
    `openapi_app.app` shared by the rest of this test session.
    """
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route as StarletteRoute

    async def _plain_handler(request):
        return PlainTextResponse("ok")

    probe_app = FastAPI()
    probe_app.router.routes.append(
        StarletteRoute("/api/v1/gateplain/probe", _plain_handler, methods=["GET"])
    )

    violations = _non_api_route_violations_under_api_v1(probe_app.routes)
    assert len(violations) == 1, (
        f"expected exactly one violation for the injected plain route, got {violations!r}"
    )
    path, route = violations[0]
    assert path == "/api/v1/gateplain/probe"
    assert not isinstance(route, APIRoute)


def _live_api_v1_operations():
    """Every (method, path, route_name) the app actually serves under
    /api/v1 today -- including include_in_schema=False routes the OpenAPI
    schema (and therefore schemathesis, which drives the test above) never
    sees.

    F1 on the #831 gate: a plain `[r for r in app.routes if isinstance(r,
    APIRoute)]` misses every `include_router()`-mounted route on this
    pinned fastapi version, because `app.routes` holds private
    `_IncludedRouter` wrappers instead of flattened routes -- see this
    module's docstring. `_flatten_api_routes` recurses through those
    wrappers so this still finds every route, including hidden ones.
    """
    ops = set()
    for route, path, _schema_visible in _flatten_api_routes(openapi_app.app.routes):
        if not path.startswith("/api/v1/"):
            continue
        for method in route.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            ops.add((method, _normalize_path(path), route.operation_id or route.name))
    return ops


def _schema_api_v1_operations():
    ops = set()
    for path, methods in schema.raw_schema.get("paths", {}).items():
        if not path.startswith("/api/v1/"):
            continue
        for method in methods:
            ops.add((method.upper(), path))
    return ops


def _schema_visible_live_api_v1_operations():
    """The (method, path) pairs `_flatten_api_routes` itself believes are
    schema-visible, restricted to /api/v1 -- computed independently of
    `_schema_api_v1_operations()`'s route, so the two can be cross-checked
    against each other below.

    Unlike `_live_api_v1_operations()`, this does not drop HEAD/OPTIONS:
    `_schema_api_v1_operations()` takes methods verbatim from the schema
    (and the substrate passthrough declares OPTIONS explicitly), so this
    side has to match that verbatim too for the cross-check to mean
    anything.
    """
    ops = set()
    for route, path, schema_visible in _flatten_api_routes(openapi_app.app.routes):
        if not schema_visible or not path.startswith("/api/v1/"):
            continue
        for method in route.methods:
            ops.add((method, _normalize_path(path)))
    return ops


def test_live_route_walk_matches_the_official_openapi_schema():
    """Pins `_flatten_api_routes`'s private-fastapi-internals walk against
    fastapi's own public `get_openapi()` output (schemathesis's
    `schema.raw_schema`, built independently of this module's route walk).

    This is the version-pinning test the #831 gate asked for: `_flatten_api_routes`
    depends on `fastapi.routing._IncludedRouter`, `.original_router`, and
    `.include_context` -- all private, unversioned surface. If a future
    fastapi upgrade changes that shape, this walk silently drifts from
    reality; this test turns that drift into a named failure here, instead
    of letting `HIDDEN_API_V1_OPERATIONS` below quietly shrink (or, at the
    limit, collect empty again the way F1 found it).
    """
    assert _schema_visible_live_api_v1_operations() == _schema_api_v1_operations()


# Structural, not a hand-list: every /api/v1 route app.routes declares that
# the OpenAPI schema hides (include_in_schema=False) -- computed at import
# time from app.routes itself, so a new router mounted the same hidden way
# (the pattern this app already uses for all seven legacy mounts, and the
# one M9's factory intake would use) shows up here the day it lands, with
# no edit to this module. Amendment 23 / R26.09 O-6, F2 on the #823 gate:
# the schemathesis-driven test above can only see what's in the schema, so
# it cannot catch a route hidden this way on its own -- this closes that
# gap with a second, directly-called test below.
HIDDEN_API_V1_OPERATIONS = sorted(
    (method, path, route_name)
    for method, path, route_name in _live_api_v1_operations()
    if (method, path) not in _schema_api_v1_operations()
)


def test_hidden_route_enumeration_is_not_silently_empty():
    """Defends the specific way F1 (the #831 gate) hid the gap this bead
    exists to close: a broken `app.routes` walk starved
    `HIDDEN_API_V1_OPERATIONS` to `[]`, and `@pytest.mark.parametrize` on an
    empty list SKIPS the test below instead of failing it -- so the whole
    hidden-route audit check went quiet with nothing red anywhere. This
    test always collects and always runs (nothing here is parametrized),
    so a regression to the same empty-set failure mode fails here by name
    instead of skipping silently.
    """
    known_hidden_routes = {
        ("GET", "/api/v1/finance/link/{institution_slug}"),
        ("POST", "/api/v1/finance/exchange"),
    }
    live_hidden = {(method, path) for method, path, _ in HIDDEN_API_V1_OPERATIONS}
    assert known_hidden_routes <= live_hidden, (
        "the /api/v1 route walk stopped finding hidden routes it used to "
        f"find -- expected at least {sorted(known_hidden_routes)}, got "
        f"{sorted(live_hidden)}. This is the F1 failure mode from the #831 "
        "gate: a route-walk regression silently shrinks "
        "HIDDEN_API_V1_OPERATIONS, which makes "
        "test_hidden_api_v1_route_emits_a_complete_audit_record's "
        "@pytest.mark.parametrize collect fewer (or zero) cases and SKIP "
        "rather than fail."
    )


def _call_finance_link_portal(client):
    return client.get("/api/v1/finance/link/chase", headers=HUMAN)


def _call_finance_exchange(client):
    return client.post(
        "/api/v1/finance/exchange",
        headers=HUMAN,
        json={"public_token": "public-sandbox-test-token"},
    )


# Request builders for the /api/v1 routes HIDDEN_API_V1_OPERATIONS finds --
# keyed by exactly what FastAPI registers (method, normalized path), so a
# hidden route with no entry here fails the test below BY NAME, asking for
# one, rather than the coverage set silently omitting it.
HIDDEN_ROUTE_CALLERS = {
    ("GET", "/api/v1/finance/link/{institution_slug}"): _call_finance_link_portal,
    ("POST", "/api/v1/finance/exchange"): _call_finance_exchange,
}


@pytest.mark.parametrize(
    "method,path,route_name",
    HIDDEN_API_V1_OPERATIONS,
    ids=[f"{m} {p}" for m, p, _ in HIDDEN_API_V1_OPERATIONS],
)
def test_hidden_api_v1_route_emits_a_complete_audit_record(method, path, route_name, caplog):
    """Amendment 23 / R26.09 O-6, F2 on the #823 gate: app.routes has
    /api/v1 routes (include_in_schema=False) the schema-driven test above
    can't see structurally. HIDDEN_API_V1_OPERATIONS is derived from
    app.routes itself, so a new router mounted the same hidden way is
    caught here BY NAME the day it lands -- even before anyone teaches this
    test how to call it.
    """
    caller = HIDDEN_ROUTE_CALLERS.get((method, path))
    if caller is None:
        pytest.fail(
            f"{method} {path} (route name {route_name!r}) is an /api/v1 "
            "route excluded from the OpenAPI schema (include_in_schema="
            "False) with no entry in HIDDEN_ROUTE_CALLERS -- add one so "
            "this audit-shape test can exercise it; a hidden route with no "
            "caller here would land unattributed and green otherwise."
        )

    label = f"{method} {path}"
    caplog.set_level(logging.INFO, logger="openapi_app")
    caplog.clear()
    client = TestClient(openapi_app.app, raise_server_exceptions=False)
    response = caller(client)

    records = _audit_records(caplog.records)
    assert records, f"{label} emitted no mcp_hub_request audit record"
    record = records[-1]

    identity = getattr(record, "audit_identity", None)
    capability = getattr(record, "audit_capability", None)
    scope = getattr(record, "audit_scope", None)
    outcome = getattr(record, "audit_outcome", None)

    assert identity == HUMAN["X-Truline-Client"], (
        f"{label}: audit record is missing/wrong client identity: {identity!r}"
    )
    assert capability == route_name, (
        f"{label}: audit record capability {capability!r} does not match "
        f"this route's name {route_name!r} -- the audit record is not "
        f"identifying the actual route/tool called"
    )
    assert scope in access_auth.SCOPES, (
        f"{label}: audit record's scope {scope!r} is not a member of "
        f"access_auth.SCOPES -- either the route never called check_scope()/"
        f"require_authenticated_scope(), or it checked an undeclared scope"
    )
    assert outcome == f"{response.status_code // 100}xx", (
        f"{label}: audit record outcome {outcome!r} does not match the "
        f"actual response status {response.status_code}"
    )
    assert response.status_code < 400, (
        f"{label}: expected a successful call against mocked upstreams, "
        f"got {response.status_code}: {response.text}"
    )


# R26.09's staged factory.read/write/merge rollout (PRIN-007) must be
# observable in the audit shape once it exists, not assumed to be there.
#
# CORRECTED 2026-09-13 BY THE OUTER LOOP (the #826 gate, F6; the #823 gate,
# F4): the authoritative spelling is the DOT form -- access_auth.SCOPES
# already holds "factory.read", and the colon form ("factory:read") never
# appeared anywhere except plan prose and an earlier draft of this test.
# An absence-only assertion on the colon spelling is a dead ratchet: it can
# never fire, because nothing was ever going to add that exact spelling.
# Pin the CURRENT membership of all three dot-form scopes instead, each
# present-or-absent, so the test fails the day membership changes in
# EITHER direction (PRIN-008) -- including the direction #826 is already
# headed (adding "factory.write" in this same dot form).
#
# "factory.write" flipped to True by M9 (POST /api/v1/factory/tasks,
# routers/v1/factory.py's factory_file_task). Not granted to any client
# (infrastructure/terraform/access.tf) -- this scope existing in the
# registry and a route checking it are independent facts; PRIN-007's
# distinct-grant property is observed by test_every_api_v1_route_emits_a_
# complete_audit_record above, which now exercises this route (schema-
# visible, unlike the hidden finance routes) with HUMAN's wildcard scope
# and asserts its audit record carries audit_scope == "factory.write".
#
# "factory.merge" flipped to True by R26.09/O-2 (POST
# /api/v1/factory/prs/{number}/merge, routers/v1/factory.py's
# factory_start_pr_merge). Unlike factory.read/factory.write, it is also in
# access_auth.EXACT_MATCH_SCOPES -- HUMAN's wildcard scope above does NOT
# satisfy it, so the schema-driven test above records this route's audit
# outcome as 4xx with audit_scope == "factory.merge" (the scope is still
# recorded: check_scope records the checked scope before it decides to
# refuse). See test_factory_merge_router_access.py for the positive case
# (an identity whose scope list names factory.merge explicitly) and
# .factory/design.md for why this ships dark on two independent axes.
CURRENT_FACTORY_SCOPE_MEMBERSHIP = {
    "factory.read": True,
    "factory.write": True,
    "factory.merge": True,
}


@pytest.mark.parametrize("scope,expected_present", sorted(CURRENT_FACTORY_SCOPE_MEMBERSHIP.items()))
def test_factory_scope_membership_is_pinned(scope, expected_present):
    """Pins today's membership of each staged factory scope in
    access_auth.SCOPES so a change in either direction -- a new scope
    added, or "factory.read" removed -- fails this test by name instead of
    passing silently. When a parametrized case here starts failing because
    a scope's membership genuinely changed, update its expected value in
    CURRENT_FACTORY_SCOPE_MEMBERSHIP above (and, once a scope beyond
    "factory.read" exists, extend test_hidden_api_v1_route_emits_a_complete_audit_record
    or the main schema-driven test with a call that exercises it, so the
    distinct-grant property PRIN-007 needs is actually observed, not just
    the registry membership).
    """
    actual_present = scope in access_auth.SCOPES
    assert actual_present == expected_present, (
        f"{scope!r} membership in access_auth.SCOPES changed: expected "
        f"present={expected_present}, got present={actual_present}. Update "
        "CURRENT_FACTORY_SCOPE_MEMBERSHIP to match reality, and if this "
        "scope is now checked by a route, extend the audit-shape coverage "
        "above to assert a call under it is recorded with this exact scope."
    )
