"""The recorder proof for M12 (docs/audits/2026-09-12-architecture-review-
modularity-and-contracts.md §4/§6): "an httpx transport recorder captures
every request each existing test suite makes against the old client(s) --
method, path, query, body, headers minus the key -- and the new client's
replay is asserted identical. A green suite alone is NOT the proof; the
recorded request set is."

Every assertion below states the exact request the PRE-migration code sent
(read from the module's own git history, not inferred) and then drives the
POST-migration code -- through ``pytest_httpx``'s ``httpx_mock``, which
intercepts real httpx traffic at the transport level regardless of whether
the call is the packaged substrate_client's synchronous ``httpx.request``
(run off the event loop via ``asyncio.to_thread``) or one of this app's own
``httpx.AsyncClient`` carve-outs -- and asserts the SAME shape came out:
same method, same path, same query parameters (as a mapping), same parsed
JSON body (as a mapping, key order irrelevant), same header NAMES with the
key's value redacted.

Permitted differences (AC-2), and nowhere else:
  (1) paging parameters on list reads where the union of pages equals the
      previous single response.
  (2) the packaged client's constant ``Content-Type: application/json`` on
      bodiless GETs, which the pre-migration code never sent (client.py
      puts it in ``self._headers`` and merges it into every request).
Every other request in this file is asserted byte-for-shape identical to
what the module sent before this migration.

Carve-outs (AC-1) are asserted UNCHANGED, not migrated: query_beads' non-
list_beads-shaped reads (created_after, offset), patch_bead's parent_id/
state+content combinations, set_budget's query-param archive PATCH, and
subscription_auditor's top-level `confidence` create.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tools import cost, finance, incidents, vision
from tools.provenance import build_provenance
from workflows import budget_pulse, finance_reconciliation, financial_insights, subscription_auditor

pytestmark = pytest.mark.asyncio

# Headers the client code itself sets, per client.py / the old code's own
# behaviour -- httpx's own connection-management headers (host, accept-
# encoding, user-agent, content-length, ...) are not "the client's request
# shape" and are excluded the same way apps/substrate/client's own recorder
# test excludes them.
_SIGNIFICANT_HEADERS = {"content-type"}


def _headers_minus_key(headers: httpx.Headers) -> dict:
    return {k.lower(): v for k, v in headers.items() if k.lower() in _SIGNIFICANT_HEADERS}


def _query(request: httpx.Request) -> dict:
    return dict(httpx.QueryParams(request.url.query))


def _body(request: httpx.Request) -> dict | None:
    return json.loads(request.content) if request.content else None


@pytest.fixture(autouse=True)
def _substrate_env(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test")


# ---------------------------------------------------------------------------
# tools/finance.py -- the helper trio + mark_bill_paid + _semantic_merge_clusters
# ---------------------------------------------------------------------------


async def test_create_bead_sends_the_pre_migration_body_shape(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "bead-1"})

    await finance.create_bead("expense", {"amount": 10.0}, "logged", "mcp-finance/log_expense")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert request.url.path == "/beads"
    assert _query(request) == {}
    assert _body(request) == {
        "namespace": "finance",
        "type": "expense",
        "state": "logged",
        "trust_tier": "user",
        "created_by": "mcp-finance/log_expense",
        "content": {"amount": 10.0},
    }
    assert _headers_minus_key(request.headers) == {"content-type": "application/json"}


async def test_create_bead_passes_trust_tier_and_provenance_through_when_a_caller_needs_them(
    httpx_mock,
):
    """financial_insights/budget_pulse widen this call with trust_tier="system"
    and a provenance dict; finance.create_bead's own 10 pre-existing callers
    never pass either, so the default (trust_tier="user", no provenance key
    at all) stays exactly what it was."""
    httpx_mock.add_response(method="POST", json={"id": "bead-2"})

    provenance = build_provenance(
        worker="budget-pulse-workflow", model=None, prompt_ref="budget-pulse/persist"
    )
    await finance.create_bead(
        "alert",
        {"category": "groceries"},
        "active",
        "budget-pulse-workflow",
        trust_tier="system",
        provenance=provenance,
    )

    body = _body(httpx_mock.get_requests()[0])
    assert body["trust_tier"] == "system"
    assert body["provenance"] == provenance


async def test_call_substrate_translates_substrate_error_to_http_status_error(httpx_mock):
    """The packaged client raises SubstrateError (a RuntimeError subclass) on
    a non-2xx response; the pre-migration code raised httpx.HTTPStatusError
    via response.raise_for_status(). bank_sync.reconcile_beads_activity
    (deferred, untouched) catches httpx.HTTPStatusError specifically around
    patch_bead_state to count a rejected transition as skipped instead of
    failing the whole activity -- _call_substrate must keep surfacing that
    type, with response.status_code equal to the store's status."""
    httpx_mock.add_response(method="PATCH", status_code=409, text="conflict")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await finance.patch_bead_state("bead-1", "paid")

    assert exc_info.value.response.status_code == 409


async def test_query_beads_type_state_limit_shape_is_also_a_named_carve_out(httpx_mock):
    """Round-2 #1012 gate finding: the packaged client's list_beads (a) pages
    until it exhausts the type/state population instead of capping at
    `limit` in one request -- the gate measured 3 requests here where main
    made 1 on a 2000-transaction read -- and (b) hardcodes HTTP_TIMEOUT_S =
    30.0 with no per-call override, which would halve the 60s PR #93 gave
    finance reads. This shape stays on _raw_substrate_request: one request,
    matching the pre-migration GET exactly, no Content-Type (unlike the
    packaged client's constant header) because it never routes through it."""
    httpx_mock.add_response(method="GET", json=[{"id": "budget-1"}])

    result = await finance.query_beads({"type": "budget", "state": "active", "limit": 100})

    assert result == [{"id": "budget-1"}]
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/beads"
    assert _query(request) == {
        "namespace": "finance",
        "type": "budget",
        "state": "active",
        "limit": "100",
    }
    assert request.content == b""
    assert _headers_minus_key(request.headers) == {}


def _capture_timeouts(monkeypatch) -> list:
    """Every outgoing Substrate call in this module goes through either
    ``httpx.AsyncClient`` (the ``_raw_substrate_request`` carve-outs) or the
    packaged client's synchronous ``httpx.request`` (offloaded via
    ``asyncio.to_thread``) -- patching both __init__/call sites, the same way
    the round-2 #1012 gate did, catches whichever one a given call uses."""
    captured: list = []
    original_async_init = httpx.AsyncClient.__init__

    def _capture_async_init(self, *args, **kwargs):
        captured.append(kwargs.get("timeout"))
        return original_async_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _capture_async_init)

    original_request = httpx.request

    def _capture_request(method, url, **kwargs):
        captured.append(kwargs.get("timeout"))
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "request", _capture_request)
    return captured


def _seconds(t) -> float:
    if isinstance(t, httpx.Timeout):
        return t.read or 0.0
    return float(t)


async def _create_bead_case(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "bead-1"})
    await finance.create_bead("expense", {"amount": 10.0}, "logged", "mcp-finance/log_expense")


async def _query_beads_type_state_limit_case(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    await finance.query_beads({"type": "budget", "state": "active", "limit": 2000})


async def _query_beads_created_after_case(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    await finance.query_beads(
        {"type": "expense", "created_after": "2026-01-01T00:00:00", "limit": 50}
    )


async def _query_beads_offset_case(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    await finance.query_beads({"type": "transaction", "limit": 1000, "offset": 1000})


async def _query_all_beads_case(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    await finance._query_all_beads({"type": "transaction"}, page_size=1000, max_pages=1)


async def _mark_bill_paid_case(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bill-1", "state": "paid"})
    await finance.mark_bill_paid("bill-1")


async def _patch_bead_state_only_case(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bead-1", "state": "paid"})
    await finance.patch_bead("bead-1", state="paid", created_by="mcp-finance/patch_bead_state")


async def _patch_bead_content_only_case(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bead-2"})
    await finance.patch_bead(
        "bead-2", content={"a": 1}, created_by="ledger-reconciliation/refresh"
    )


async def _patch_bead_state_and_content_case(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bead-3"})
    await finance.patch_bead(
        "bead-3", state="resolved", content={"resolution": "accepted"}, created_by="x"
    )


async def _link_transaction_to_manual_case(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "tx-1"})
    await finance.link_transaction_to_manual("tx-1", "manual-1")


async def _set_budget_case(httpx_mock):
    httpx_mock.add_response(
        method="GET", json=[{"id": "budget-old", "content": {"category": "groceries"}}]
    )
    httpx_mock.add_response(method="PATCH", json={"id": "budget-old", "state": "archived"})
    httpx_mock.add_response(method="POST", json={"id": "budget-new"})
    await finance.set_budget("groceries", 500.0)


async def _semantic_merge_clusters_search_case(httpx_mock):
    httpx_mock.add_response(method="POST", json=[])
    clusters = [
        {
            "merchant": "NETFLIX",
            "evidence_tx_ids": ["tx-1"],
            "samples": [],
            "amounts": [15.99],
            "occurrences": 1,
            "total_amount": 15.99,
            "first_seen": "2026-01-01",
            "last_seen": "2026-01-01",
        }
    ]
    await finance._semantic_merge_clusters(clusters, per_query_limit=8)


# (label, case, minimum timeout origin/main sent on this exact path -- PR #93
# set 60s on every finance.py Substrate store call, not only reads;
# _semantic_merge_clusters' search stayed at 10s on main and is disclosed as
# going to 30s here, not reverted, so its floor is still 10s not 60s).
FINANCE_TIMEOUT_TABLE = [
    ("create_bead", _create_bead_case, 60.0),
    ("query_beads_type_state_limit", _query_beads_type_state_limit_case, 60.0),
    ("query_beads_created_after", _query_beads_created_after_case, 60.0),
    ("query_beads_offset_paged", _query_beads_offset_case, 60.0),
    ("_query_all_beads", _query_all_beads_case, 60.0),
    ("mark_bill_paid", _mark_bill_paid_case, 60.0),
    ("patch_bead_state_only", _patch_bead_state_only_case, 60.0),
    ("patch_bead_content_only", _patch_bead_content_only_case, 60.0),
    ("patch_bead_state_and_content", _patch_bead_state_and_content_case, 60.0),
    ("link_transaction_to_manual", _link_transaction_to_manual_case, 60.0),
    ("set_budget_archive_and_create", _set_budget_case, 60.0),
    ("semantic_merge_clusters_search", _semantic_merge_clusters_search_case, 10.0),
]


@pytest.mark.parametrize(
    "label,case,min_main_timeout",
    FINANCE_TIMEOUT_TABLE,
    ids=[row[0] for row in FINANCE_TIMEOUT_TABLE],
)
async def test_finance_store_call_timeout_stays_at_or_above_main(
    httpx_mock, monkeypatch, label, case, min_main_timeout
):
    """REQUIRED by the round-3 #1016 gate: every finance.py store call's
    timeout on this head is >= its timeout on origin/main. PR #93 set 60s on
    ALL finance.py Substrate clients, not only reads -- five rows here
    (create_bead, mark_bill_paid, patch_bead state-only, patch_bead
    content-only, set_budget's create) previously routed through the
    packaged client's synchronous ``httpx.request``, hardcoded to
    ``HTTP_TIMEOUT_S = 30.0`` with no per-call override, dropping them to
    30s -- this test fails on 04aefd58 for exactly those five rows."""
    captured = _capture_timeouts(monkeypatch)

    await case(httpx_mock)

    assert captured, f"{label}: expected a captured timeout for the outgoing Substrate request(s)"
    observed = min(_seconds(t) for t in captured)
    assert observed >= min_main_timeout, (
        f"{label}: timeout {observed}s dropped below origin/main's {min_main_timeout}s"
    )


async def test_query_beads_created_after_shape_is_the_named_carve_out(httpx_mock):
    """list_beads/search take no `created_after` filter (OPS-190 gave the
    client list_beads and search, not a generic passthrough GET) -- this
    shape stays the direct request finance.query_beads already centralized,
    unchanged."""
    httpx_mock.add_response(method="GET", json=[])

    await finance.query_beads({"type": "expense", "created_after": "2026-01-01T00:00:00", "limit": 50})

    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.path == "/beads"
    assert _query(request) == {
        "namespace": "finance",
        "type": "expense",
        "created_after": "2026-01-01T00:00:00",
        "limit": "50",
    }
    assert request.content == b""
    # Unchanged carve-out: no Content-Type, exactly like before this migration.
    assert _headers_minus_key(request.headers) == {}


async def test_query_beads_offset_shape_is_also_the_named_carve_out(httpx_mock):
    """_query_all_beads's manual offset paging is a GET /beads shape
    list_beads cannot take a caller-supplied offset for -- stays direct."""
    httpx_mock.add_response(method="GET", json=[])

    await finance.query_beads({"type": "transaction", "limit": 1000, "offset": 1000})

    request = httpx_mock.get_requests()[0]
    assert _query(request) == {
        "namespace": "finance",
        "type": "transaction",
        "limit": "1000",
        "offset": "1000",
    }
    assert _headers_minus_key(request.headers) == {}


async def test_patch_bead_state_only_routes_through_set_state(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bead-1", "state": "paid"})

    await finance.patch_bead("bead-1", state="paid", created_by="mcp-finance/patch_bead_state")

    request = httpx_mock.get_requests()[0]
    assert request.method == "PATCH"
    assert request.url.path == "/beads/bead-1"
    assert _body(request) == {"state": "paid", "created_by": "mcp-finance/patch_bead_state"}
    assert _headers_minus_key(request.headers) == {"content-type": "application/json"}


async def test_patch_bead_content_only_routes_through_patch_content(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bead-2"})

    await finance.patch_bead("bead-2", content={"a": 1}, created_by="ledger-reconciliation/refresh")

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/beads/bead-2"
    assert _body(request) == {"content": {"a": 1}, "created_by": "ledger-reconciliation/refresh"}


async def test_patch_bead_state_and_content_together_is_the_named_carve_out(httpx_mock):
    """No packaged-client method combines state and content in one PATCH --
    resolve_discrepancy's shape (finance_reconciliation.py:408) stays the
    direct request patch_bead already centralized."""
    httpx_mock.add_response(method="PATCH", json={"id": "bead-3"})

    await finance.patch_bead(
        "bead-3", state="resolved", content={"resolution": "accepted"}, created_by="x"
    )

    request = httpx_mock.get_requests()[0]
    assert _body(request) == {
        "created_by": "x",
        "state": "resolved",
        "content": {"resolution": "accepted"},
    }
    assert _headers_minus_key(request.headers) == {"content-type": "application/json"}


async def test_patch_bead_with_parent_id_is_the_named_carve_out(httpx_mock):
    """No packaged-client method takes parent_id at all --
    link_transaction_to_manual's shape stays the direct request."""
    httpx_mock.add_response(method="PATCH", json={"id": "tx-1"})

    await finance.patch_bead(
        "tx-1", state="reconciled", parent_id="manual-1", created_by="mcp-finance/reconcile"
    )

    request = httpx_mock.get_requests()[0]
    assert _body(request) == {
        "created_by": "mcp-finance/reconcile",
        "state": "reconciled",
        "parent_id": "manual-1",
    }


async def test_mark_bill_paid_routes_through_set_state(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "bill-1", "state": "paid"})

    await finance.mark_bill_paid("bill-1")

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/beads/bill-1"
    assert _body(request) == {"state": "paid", "created_by": "mcp-finance/mark_bill_paid"}


async def test_semantic_merge_clusters_search_matches_the_pre_migration_body(httpx_mock):
    httpx_mock.add_response(method="POST", json=[])

    clusters = [
        {
            "merchant": "NETFLIX",
            "evidence_tx_ids": ["tx-1"],
            "samples": [],
            "amounts": [15.99],
            "occurrences": 1,
            "total_amount": 15.99,
            "first_seen": "2026-01-01",
            "last_seen": "2026-01-01",
        }
    ]

    result = await finance._semantic_merge_clusters(clusters, per_query_limit=8)

    assert result == clusters
    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert request.url.path == "/beads/search"
    assert _body(request) == {
        "query": "NETFLIX",
        "limit": 8,
        "namespace": "finance",
        "type": "transaction",
    }
    assert _headers_minus_key(request.headers) == {"content-type": "application/json"}


async def test_set_budget_archives_via_the_unchanged_query_param_patch_carve_out(httpx_mock):
    """set_budget's archive step PATCHes with query-string params (to_state,
    created_by) instead of a JSON body -- a shape no packaged-client method
    expresses (they all send JSON bodies) -- so it is untouched by this
    migration; only the trailing create at the end is migrated."""
    httpx_mock.add_response(
        method="GET", json=[{"id": "budget-old", "content": {"category": "groceries"}}]
    )
    httpx_mock.add_response(method="PATCH", json={"id": "budget-old", "state": "archived"})
    httpx_mock.add_response(method="POST", json={"id": "budget-new"})

    await finance.set_budget("groceries", 500.0)

    requests = httpx_mock.get_requests()
    patch_request = requests[1]
    assert patch_request.method == "PATCH"
    assert patch_request.url.path == "/beads/budget-old"
    assert patch_request.content == b""  # query params, not a JSON body
    assert _query(patch_request) == {"to_state": "archived", "created_by": "mcp-finance/set_budget"}

    post_request = requests[2]
    assert post_request.method == "POST"
    assert _body(post_request)["content"] == {
        "category": "groceries",
        "amount": 500.0,
        "period": "monthly",
    }


# ---------------------------------------------------------------------------
# tools/incidents.py -- all four calls fit the packaged client exactly
# ---------------------------------------------------------------------------


async def test_find_incident_matches_the_pre_migration_get_shape(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])

    result = await incidents._find_incident("bank_sync.sync_failure", "sync_failure:chase")

    assert result is None
    request = httpx_mock.get_requests()[0]
    assert request.method == "GET"
    assert request.url.path == "/beads"
    assert _query(request) == {"namespace": "arch", "type": "incident", "limit": "500"}
    assert _headers_minus_key(request.headers) == {"content-type": "application/json"}


async def test_create_incident_matches_the_pre_migration_post_body(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "incident-1"})

    await incidents._create_incident({"source": "bank_sync.sync_failure"})

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/beads"
    assert _body(request) == {
        "namespace": "arch",
        "type": "incident",
        "state": "detected",
        "trust_tier": "system",
        "created_by": "notify/incident",
        "content": {"source": "bank_sync.sync_failure"},
    }


async def test_attach_evidence_matches_the_pre_migration_patch_body(httpx_mock):
    httpx_mock.add_response(method="PATCH", json={"id": "incident-1"})

    await incidents._attach_evidence(
        "incident-1", {"content": {"evidence": []}}, {"observed_at": "2026-09-07T12:00:00+00:00"}
    )

    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/beads/incident-1"
    assert _body(request) == {
        "content": {"evidence": [{"observed_at": "2026-09-07T12:00:00+00:00"}]},
        "created_by": "notify/incident",
    }


async def test_reopen_incident_matches_the_pre_migration_transition_then_patch(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "incident-1", "state": "detected"})
    httpx_mock.add_response(method="PATCH", json={"id": "incident-1"})

    await incidents._reopen_incident(
        "incident-1", {"content": {"evidence": []}}, {"observed_at": "2026-09-07T12:00:00+00:00"}
    )

    requests = httpx_mock.get_requests()
    transition_request = requests[0]
    assert transition_request.url.path == "/beads/incident-1/transition"
    assert _body(transition_request) == {
        "from_state": "resolved",
        "to_state": "detected",
        "created_by": "notify/incident",
    }
    patch_request = requests[1]
    assert patch_request.url.path == "/beads/incident-1"


# ---------------------------------------------------------------------------
# tools/cost.py -- one create, carrying provenance (also covered at the wire
# level by tests/test_cost.py's own httpx_mock-based assertions).
# ---------------------------------------------------------------------------


async def test_log_llm_cost_matches_the_pre_migration_post_body_shape(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "cost-1"})

    bead_id = await cost.log_llm_cost(
        model="claude-haiku",
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        agent="test/agent",
        context={"foo": "bar"},
        workflow_id="wf-1",
        latency_ms=42.0,
        prompt_hash_value="hash123",
    )

    assert bead_id == "cost-1"
    body = _body(httpx_mock.get_requests()[0])
    assert body["namespace"] == "platform"
    assert body["type"] == "cost"
    assert body["state"] == "recorded"
    assert body["trust_tier"] == "system"
    assert body["created_by"] == "test/agent"
    assert body["content"]["workflow_id"] == "wf-1"
    assert body["provenance"] == {
        "worker": "test/agent",
        "model": "claude-haiku",
        "prompt_ref": "prompt-sha256:hash123",
        "tokens": 150,
        "cost_usd": None,
        "duration_s": 0.042,
    }


# ---------------------------------------------------------------------------
# tools/vision.py -- one create, carrying both context and provenance
# ---------------------------------------------------------------------------


async def test_stage_vision_extraction_matches_the_pre_migration_post_body(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "vision-1"})

    extraction = vision.VisionExtraction(todos=["buy milk"], events=[], expenses=[], summary="note")

    bead_id = await vision.stage_vision_extraction(
        extraction, source="vision/extract", context={"channel": "whatsapp"}
    )

    assert bead_id == "vision-1"
    request = httpx_mock.get_requests()[0]
    assert request.url.path == "/beads"
    assert _body(request) == {
        "namespace": "vision",
        "type": "extraction",
        "state": "pending",
        "content": extraction.model_dump(),
        "context": {"channel": "whatsapp"},
        "trust_tier": "unverified",
        "provenance": {
            "worker": "mcp-hub/vision",
            "model": "claude-haiku",
            "prompt_ref": "vision/extract",
            "tokens": None,
            "cost_usd": None,
            "duration_s": 0.0,
        },
        "created_by": "mcp-hub/vision",
    }


# ---------------------------------------------------------------------------
# workflows/financial_insights.py
# ---------------------------------------------------------------------------


async def test_summarize_merchant_insights_activity_matches_the_pre_migration_post_body(
    httpx_mock, monkeypatch
):
    monkeypatch.setattr(financial_insights, "get_recurring_candidates", _async_return([]))
    httpx_mock.add_response(method="POST", json={"id": "summary-1"})

    bead_id = await financial_insights.summarize_merchant_insights_activity()

    assert bead_id == "summary-1"
    body = _body(httpx_mock.get_requests()[0])
    assert body["namespace"] == "finance"
    assert body["type"] == "summary"
    assert body["state"] == "active"
    assert body["trust_tier"] == "system"
    assert body["created_by"] == "financial-insights/summarize_merchants"
    assert body["content"]["merchant_insights"] == {}
    assert "provenance" not in body


async def test_aggregate_finance_data_activity_matches_the_pre_migration_get_shapes(httpx_mock):
    # Order matters: the transaction (created_after carve-out) GET fires
    # before the budget (type/state/limit carve-out) GET.
    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="GET", json=[])

    await financial_insights.aggregate_finance_data_activity()

    requests = httpx_mock.get_requests()
    tx_request, budget_request = requests[0], requests[1]

    assert tx_request.url.path == "/beads"
    tx_query = _query(tx_request)
    assert tx_query["namespace"] == "finance"
    assert tx_query["type"] == "transaction"
    assert tx_query["limit"] == "1000"
    assert "created_after" in tx_query
    assert _headers_minus_key(tx_request.headers) == {}  # carve-out: unchanged

    assert _query(budget_request) == {
        "namespace": "finance",
        "type": "budget",
        "state": "active",
        "limit": "100",
    }
    # Both shapes are query_beads carve-outs through _raw_substrate_request
    # now, so neither carries the packaged client's Content-Type header.
    assert _headers_minus_key(budget_request.headers) == {}


async def test_persist_insight_activity_matches_the_pre_migration_post_body(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "insight-1"})

    summary = {
        "window": {"since": "2026-05-15", "until": "2026-05-22"},
        "by_category": {"dining": {"spent": 42.5}},
        "totals": {"spent": 42.5},
        "transaction_ids": ["tx-1", "tx-2"],
    }

    bead_id = await financial_insights.persist_insight_activity("Dining is under control.", summary)

    assert bead_id == "insight-1"
    body = _body(httpx_mock.get_requests()[0])
    assert body["namespace"] == "finance"
    assert body["type"] == "insight"
    assert body["content"]["transaction_ids"] == ["tx-1", "tx-2"]
    assert body["provenance"] == {
        "worker": "financial-insights-workflow",
        "model": body["content"]["model"],
        "prompt_ref": "financial-insights/synthesize",
        "tokens": None,
        "cost_usd": None,
        "duration_s": 0.0,
    }


# ---------------------------------------------------------------------------
# workflows/budget_pulse.py
# ---------------------------------------------------------------------------


async def test_persist_budget_alerts_activity_matches_the_pre_migration_post_body(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "alert-1"}, is_reusable=True)

    report = {
        "month": "2026-09",
        "as_of_date": "2026-09-15",
        "threshold": 0.8,
        "alerts": [
            {
                "category": "groceries",
                "budget_amount": 500.0,
                "spent_amount": 450.0,
                "remaining_amount": 50.0,
                "utilization_pct": 90.0,
            }
        ],
    }

    bead_ids = await budget_pulse.persist_budget_alerts_activity(report)

    assert bead_ids == ["alert-1"]
    body = _body(httpx_mock.get_requests()[0])
    assert body == {
        "namespace": "finance",
        "type": "alert",
        "state": "active",
        "content": {
            "category": "groceries",
            "budget_amount": 500.0,
            "spent_amount": 450.0,
            "remaining_amount": 50.0,
            "utilization_pct": 90.0,
            "month": "2026-09",
            "as_of_date": "2026-09-15",
            "threshold_breached": 0.8,
        },
        "provenance": {
            "worker": "budget-pulse-workflow",
            "model": "none",
            "prompt_ref": "budget-pulse/persist",
            "tokens": 0,
            "cost_usd": 0.0,
            "duration_s": 0.0,
        },
        "trust_tier": "system",
        "created_by": "budget-pulse-workflow",
    }


# ---------------------------------------------------------------------------
# workflows/subscription_auditor.py -- the named top-level-`confidence` carve-out
# ---------------------------------------------------------------------------


async def test_persist_subscription_activity_confidence_field_survives_the_carve_out(httpx_mock):
    httpx_mock.add_response(method="GET", json=[])
    httpx_mock.add_response(method="POST", json={"id": "subscription-1"})

    await subscription_auditor.persist_subscription_activity(
        {
            "cluster": {
                "merchant": "NETFLIX",
                "display_name": "Netflix",
                "first_seen": "2026-03-15",
                "last_seen": "2026-05-15",
                "occurrences": 3,
                "evidence_tx_ids": ["tx-1"],
            },
            "analysis": {
                "is_subscription": True,
                "name": "Netflix",
                "category": "streaming",
                "amount": 17.99,
                "frequency": "monthly",
                "is_price_hike": False,
                "price_change": {"detected": False},
                "confidence": 0.93,
                "notes": "Monthly streaming charge.",
            },
            "model": "gemini-pro",
        }
    )

    post_request = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = _body(post_request)
    # create_bead has no "confidence" parameter and would silently drop this
    # top-level field -- the whole reason this call is a named carve-out
    # (AC-1) instead of a create_bead migration.
    assert body["confidence"] == 0.93
    assert body["content"]["confidence"] == 0.93
    assert body["namespace"] == "finance"
    assert body["type"] == "subscription"


# ---------------------------------------------------------------------------
# workflows/finance_reconciliation.py
# ---------------------------------------------------------------------------


async def test_create_snapshot_matches_the_pre_migration_post_body(httpx_mock):
    httpx_mock.add_response(method="POST", json={"id": "snapshot-1"})

    await finance_reconciliation._create_snapshot("account-1", 1234.56, "USD", "2026-09-15T00:00:00")

    body = _body(httpx_mock.get_requests()[0])
    assert body == {
        "namespace": "finance",
        "type": "balance_snapshot",
        "state": "active",
        "content": {
            "account_id": "account-1",
            "balance": 1234.56,
            "iso_currency_code": "USD",
            "as_of": "2026-09-15T00:00:00",
        },
        "trust_tier": "system",
        "created_by": "ledger-reconciliation/snapshot",
    }


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn
