"""ea-derive.py no longer carries its own HTTP client -- it subclasses
scripts/substrate_client.py's `Substrate` now, the same pattern
ea-load.py/release-load.py/requirements-load.py/arch-source-class-backfill.py already
use (see scripts/tests/test_substrate_http_consolidation.py for those). This pins the
request shapes it actually sends the same way that file pins the shared client's own
contract: a green suite alone would not show the migration is request-identical to what
the script built by hand before, so this exercises the real request path (method, URL,
headers minus the key, body) against a recording transport rather than asserting on
source text.

`add_link` is the one method kept as a local override, and for a reason specific to this
script: apps/substrate/src/routes.py's `create_bead_link` reads `created_by` from the
`X-Created-By` header (never the body -- `BeadLinkCreate` declares no such field and
silently drops it), and resolves a MISSING header as the literal string `"unknown"`
(routes.py: `created_by=x_created_by or "unknown"`). ea-derive.py's own
`reconcile_dependencies` picks a writer identity per call, not one fixed for the life of
a `Substrate` instance -- `apps/factory-dispatcher/activities/ea_apply.py`'s
`DependencySubstrate` Protocol declares `add_link(..., *, created_by: str)` for exactly
this reason, and that file is out of scope to change. The attribution tests below prove
the fix this file exists to protect: dev.finding 94e7a1f4 (fixed by dev.task 95b909ca)
found five links landing as "unknown" because a caller sent `created_by` in the body
instead of the header -- a double that accepted the body field would pass this test suite
while production wrote "unknown", so the fake below is deliberately built to reject
exactly what the real route rejects.

Also pinned here: the two failure paths (missing SUBSTRATE_URL/SUBSTRATE_API_KEY, and a
non-2xx response) are unchanged in exit code and message shape, and the deferred-import
design that lets this module still load with no `scripts/substrate_client.py` on disk --
the situation apps/substrate/publish/csdm-on-beads/scripts/ea-derive.py (this file's
byte-identical published copy, scripts/tests/test_csdm_publish_tree_drift.py) is actually
in, and which ea-conformance.py's DERIVED/WORKLOAD checks depend on not raising.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EA_DERIVE_PATH = REPO / "scripts" / "ea-derive.py"


def _load():
    spec = importlib.util.spec_from_file_location("ea_derive_substrate_consolidation", EA_DERIVE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ead = _load()

import substrate_client  # noqa: E402 -- ead's own load above already put scripts/ on sys.path


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _capturing_urlopen(monkeypatch, payload: bytes = b"{}"):
    capture: dict = {}

    def fake_urlopen(req, timeout=None):
        capture["method"] = req.get_method()
        capture["url"] = req.full_url
        capture["headers"] = {k.lower(): v for k, v in req.header_items()}
        capture["body"] = req.data
        return _Resp(payload)

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake_urlopen)
    return capture


def _with_substrate_env(monkeypatch, url="http://substrate.test", key="k"):
    monkeypatch.setenv(substrate_client.URL_ENV, url)
    monkeypatch.setenv(substrate_client.KEY_ENV, key)


# --- the shared client, not a hand-rolled one ------------------------------------------


def test_ea_derive_substrate_is_the_shared_client_not_a_hand_rolled_one():
    assert issubclass(ead.Substrate, substrate_client.Substrate)


def test_ea_derive_substrate_constructs_with_no_arguments(monkeypatch):
    """apps/factory-dispatcher/activities/ea_apply.py calls `ea_derive.Substrate()` with
    no arguments -- out of scope to change, so this stays a hard contract."""
    _with_substrate_env(monkeypatch)
    ead.Substrate()


# --- request-identical: the real request path, not source text -------------------------


def test_list_beads_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch, b"[]")
    sub = ead.Substrate()

    sub.list_beads("application")

    assert capture["method"] == "GET"
    assert capture["url"] == "http://substrate.test/beads?namespace=arch&type=application&limit=1000"
    assert capture["headers"]["x-api-key"] == "k"
    assert "x-created-by" not in capture["headers"]
    assert capture["body"] is None


def test_links_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch, b"[]")
    sub = ead.Substrate()

    sub.links("bead-1")

    assert capture["method"] == "GET"
    assert capture["url"] == "http://substrate.test/beads/bead-1/links?direction=outgoing"
    assert capture["headers"]["x-api-key"] == "k"
    assert capture["body"] is None


def test_add_link_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch)
    sub = ead.Substrate()

    sub.add_link("bead-1", "bead-2", "depends_on", created_by="ea-derive")

    assert capture["method"] == "POST"
    assert capture["url"] == "http://substrate.test/beads/bead-1/links"
    assert capture["headers"]["x-api-key"] == "k"
    assert capture["headers"]["x-created-by"] == "ea-derive"
    # The body carries no created_by field at all -- BeadLinkCreate declares none, and
    # sending one there is the exact defect (dev.finding 94e7a1f4) that made every such
    # link land as "unknown" before #881 fixed the shared client's own add_link.
    assert capture["body"] == b'{"target_id": "bead-2", "link_type": "depends_on"}'


def test_delete_link_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch)
    sub = ead.Substrate()

    sub.delete_link("link-1")

    assert capture["method"] == "DELETE"
    assert capture["url"] == "http://substrate.test/links/link-1"
    assert capture["body"] is None


# --- edge attribution: verified against a fake that rejects what the real route rejects -


class RouteAccurateFakeUrlopen:
    """Stands in for `POST /beads/{id}/links` the way apps/substrate/src/routes.py's
    `create_bead_link` actually behaves: `created_by` is read from the `X-Created-By`
    HEADER, any `created_by` in the JSON BODY is silently ignored (BeadLinkCreate has no
    such field), and a request that carries no header resolves to the literal string
    "unknown" -- never a null or an absent value. A double that accepted the body field
    instead would pass every test in this file while production wrote "unknown"; this one
    cannot, by construction.
    """

    def __init__(self):
        self.created_links: list[dict] = []

    def __call__(self, req, timeout=None):
        import json

        headers = {k.lower(): v for k, v in req.header_items()}
        body = json.loads(req.data.decode()) if req.data else {}
        attributed = headers.get("x-created-by") or "unknown"
        link = {
            "id": f"link-{len(self.created_links) + 1}",
            "target_id": body.get("target_id"),
            "link_type": body.get("link_type"),
            "created_by": attributed,
        }
        self.created_links.append(link)
        return _Resp(b"{}")


def test_add_link_attributes_to_ea_derive_never_unknown(monkeypatch):
    _with_substrate_env(monkeypatch)
    fake = RouteAccurateFakeUrlopen()
    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake)
    sub = ead.Substrate()

    sub.add_link("bead-1", "bead-2", "depends_on", created_by=ead.DEFAULT_CREATED_BY)

    assert len(fake.created_links) == 1
    assert fake.created_links[0]["created_by"] == "ea-derive"
    assert fake.created_links[0]["created_by"] != "unknown"


def test_reconcile_dependencies_attributes_every_created_edge_to_ea_derive(monkeypatch):
    """End to end through the writing layer this bead exists to protect: 27 edges in the
    live model are attributed correctly today because this script still tags its own
    writes with the X-Created-By header. This proves the migrated Substrate keeps doing
    that -- through reconcile_dependencies, not just through a direct add_link call."""
    _with_substrate_env(monkeypatch)
    fake = RouteAccurateFakeUrlopen()
    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", fake)

    class ListingSubstrate(ead.Substrate):
        """add_link/list_beads/links/delete_link real; only list_beads/links are stubbed
        so the reconcile has beads and no pre-existing links to reconcile against."""

        def list_beads(self, bead_type, limit=1000):
            return [
                {"id": "b-api", "type": "application", "content": {"ref": "app.api"}},
                {"id": "b-db", "type": "application", "content": {"ref": "app.db"}},
            ]

        def links(self, bead_id, direction="outgoing"):
            return []

    sub = ListingSubstrate()
    derived = {"app.api": {"app.db": None}}

    plan = ead.reconcile_dependencies(sub, derived)

    assert plan.created == ["app.api -depends_on-> app.db"]
    assert len(fake.created_links) == 1
    assert fake.created_links[0]["created_by"] == "ea-derive"


# --- failure paths: unchanged exit code and message -------------------------------------


def test_missing_env_vars_exits_with_the_same_message(monkeypatch):
    monkeypatch.delenv(substrate_client.URL_ENV, raising=False)
    monkeypatch.delenv(substrate_client.KEY_ENV, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        ead.Substrate()

    assert str(excinfo.value) == "SUBSTRATE_URL and SUBSTRATE_API_KEY must be set"


def test_non_2xx_response_raises_substrate_error_with_status_and_body(monkeypatch):
    import urllib.error

    _with_substrate_env(monkeypatch)

    def failing_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"detail": "bad link_type"}')
        )

    monkeypatch.setattr(substrate_client.urllib.request, "urlopen", failing_urlopen)
    sub = ead.Substrate()

    with pytest.raises(substrate_client.SubstrateError) as excinfo:
        sub.list_beads("application")

    assert excinfo.value.status == 422
    assert "substrate 422 on /beads" in str(excinfo.value)
    assert "bad link_type" in str(excinfo.value)


# --- self-containment: this module still loads with no substrate_client.py on disk ------


def test_module_still_loads_with_substrate_client_unimportable(monkeypatch):
    """Mirrors the published tree's actual situation
    (apps/substrate/publish/csdm-on-beads/scripts/ carries no substrate_client.py
    sibling): blocking the import proves ea-derive.py still loads cleanly -- the property
    scripts/tests/test_csdm_publish_conformance_fixture.py's
    test_conformance_check_passes_against_the_fixture_repository depends on, since
    ea-conformance.py's DERIVED/WORKLOAD checks load this whole module by path and turn
    any load-time exception into a conformance error -- and that only constructing
    `Substrate` fails, with a clear message rather than an ImportError.
    """
    monkeypatch.setitem(sys.modules, "substrate_client", None)

    spec = importlib.util.spec_from_file_location(
        "ea_derive_without_substrate_client", EA_DERIVE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not raise

    assert mod._Substrate is None
    with pytest.raises(RuntimeError, match="substrate_client.py is not present"):
        mod.Substrate()
