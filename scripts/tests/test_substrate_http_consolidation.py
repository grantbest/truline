"""arch-source-class-backfill.py no longer carries its own HTTP client -- it
subclasses scripts/substrate_client.py's `Substrate` now
(docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md
§4/§6, M7). This pins the request shapes it actually sends the same way
test_substrate_client.py pins the shared client's own contract: a green
suite alone would not show the migration is request-identical to what the
script built by hand before, so this exercises the real request path
(method, URL, headers minus the key, body) rather than asserting on source
text.

The other three scripts M7 names are not touched here:

principles_sync.py already routed through `substrate_client.Substrate`
before this task (its own `push`/`fetch`/`check-view` docstring says so;
scripts/tests/test_principles_sync.py exercises it against a fake). It
needed no change and has no HTTP surface of its own to pin.

ea-derive.py's own `Substrate` now also moved to this pattern -- see
scripts/tests/test_ea_derive_substrate_consolidation.py, not this file. It needed two
things arch-source-class-backfill.py did not: a bead to widen scope to cover its
byte-identical published copy (scripts/tests/test_csdm_publish_tree_drift.py's
BYTE_IDENTICAL pins scripts/ea-derive.py against
apps/substrate/publish/csdm-on-beads/scripts/ea-derive.py, which arch-source-class-
backfill.py carries no such copy of), and a deferred rather than top-level import of
substrate_client -- the published tree's own promise is that none of its files import
outside the standard library and pydantic/pyyaml, and ea-conformance.py's DERIVED/WORKLOAD
checks load ea-derive.py by path in both trees, turning a load-time ImportError into a
conformance error for every caller, including ones that only derive and never write.

regression-test.py's one substrate-directed check runs `httpx` inside a
different pod's network namespace via `kubectl exec`, to prove in-cluster
DNS/auth reachability from that pod -- there is no local HTTP call in this
script's own process to route through a client that talks to `SUBSTRATE_URL`
from wherever the script itself runs, and doing so would test something
different (this process's reachability, not the pod's). Nothing to pin here
either.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load(alias: str, filename: str):
    spec = importlib.util.spec_from_file_location(alias, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = _load("substrate_http_consolidation_backfill", "arch-source-class-backfill.py")

import substrate_client  # noqa: E402 -- backfill above already put scripts/ on sys.path


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


def test_backfill_substrate_is_the_shared_client_not_a_hand_rolled_one():
    assert issubclass(backfill.Substrate, substrate_client.Substrate)
    assert backfill.SubstrateError is substrate_client.SubstrateError
    assert backfill.SubstrateClient is substrate_client.SubstrateClient


def test_backfill_list_beads_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch, b"[]")
    sub = backfill.Substrate()

    sub.list_beads("capability")

    assert capture["method"] == "GET"
    assert capture["url"] == "http://substrate.test/beads?namespace=arch&type=capability&limit=1000"
    assert capture["headers"]["x-api-key"] == "k"
    assert capture["body"] is None


def test_backfill_patch_request_is_unchanged(monkeypatch):
    _with_substrate_env(monkeypatch)
    capture = _capturing_urlopen(monkeypatch)
    sub = backfill.Substrate()

    sub.patch("bead-1", {"content": {"source_class": "authored"}})

    assert capture["method"] == "PATCH"
    assert capture["url"] == "http://substrate.test/beads/bead-1"
    assert capture["headers"]["x-api-key"] == "k"
    assert capture["body"] == (
        b'{"content": {"source_class": "authored"}, "created_by": "arch-source-class-backfill"}'
    )
