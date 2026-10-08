"""OPS-122 / dev.finding c48a3827: a second, read-only key for the substrate.

``require_api_key`` (src/routes.py) is the single dependency every route in
``routes.router`` and ``rules.router`` declares -- there is no per-route copy
to keep in sync, so most of these tests exercise that one function directly
rather than standing up a database-backed app. Routes are discovered by
walking the *composed* app (``src.main.app``, the S52-5 pattern) rather than
the two router objects directly, so a route added later -- on either router,
or a new router included later -- is exercised automatically, without
editing this file: whatever HTTP methods it declares get run against both
keys.

Walking ``app.routes`` and stopping at the first ``APIRoute`` is not enough:
fastapi 0.141.1's ``include_router`` wraps each included router in an
``_IncludedRouter`` rather than flattening its routes onto the parent, so
``app.routes`` holds wrapper objects, not the routes inside them, for two of
its entries. (The exact entry count on ``app.routes`` is not pinned here --
it shifts with unrelated routes gaining or losing siblings on ``app``
itself, e.g. the built-in docs routes -- but the wrapping behaviour is
structural to fastapi 0.141.1 and does not depend on how many routes are
nested inside it.) A naive ``isinstance(route, APIRoute)`` filter over
``app.routes`` would silently see only ``/health`` and call the fixture
sane, missing every route nested inside an ``_IncludedRouter``.
``_walk_api_routes`` recurses through ``original_router.routes`` instead, so
it keeps working if a future router nests another router inside itself (as
a straightforward ``include_router`` composition would) rather than only
including APIRouters directly on ``app``. The assertions below on
``_UNFILTERED_WALK_PATHS`` and ``ALL_ROUTES`` are the live proof that the
walk actually resolves routes nested this way, rather than a count asserted
once and left to rot.

Calling ``require_api_key`` directly proves the function's own behaviour but
not that any given route actually depends on it -- a route decorated without
``Depends(require_api_key)`` would never show up as a gap to those tests.
``test_every_route_declares_require_api_key`` below closes that: it inspects
each route's resolved dependency chain instead of calling the function, so
dropping the dependency from a route (or adding a new route without it)
fails structurally regardless of what require_api_key itself does.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from src import routes
from src.main import app
from src.routes import require_api_key

WRITE_KEY = "write-key"
READ_KEY = "read-key"

# main.py:68 declares GET /health outside require_api_key by design -- a
# liveness probe carries no secret to leak, so it is not a gap in the
# ratchet, it is the one route the ratchet must not flag.
UNAUTHENTICATED_ROUTE_PATHS = {"/health"}


def _request(method: str) -> SimpleNamespace:
    return SimpleNamespace(method=method)


def _declared_methods(route) -> set:
    """The methods a route was decorated with. FastAPI's APIRoute does not
    add HEAD or OPTIONS automatically (verified against fastapi==0.141.1,
    the pinned version) -- a live HEAD request 405s at routing before any
    dependency runs -- so this is a no-op today and only guards against a
    future FastAPI version changing that."""
    return set(route.methods or set()) - {"HEAD", "OPTIONS"}


def _walk_api_routes(routes):
    """Recurse through wrapped included routers to the real ``APIRoute``s.

    Duck-typed on ``original_router`` rather than importing fastapi's
    private ``_IncludedRouter`` by name -- that class is an implementation
    detail this app does not own, and the attribute it exposes is the only
    thing this walk needs.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _walk_api_routes(original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def _all_routes():
    for route in _walk_api_routes(app.routes):
        if route.path not in UNAUTHENTICATED_ROUTE_PATHS:
            yield route


def _all_route_methods():
    for route in _all_routes():
        for method in _declared_methods(route):
            yield route.path, method


def _route_dependency_calls(route) -> set:
    """The callables FastAPI will actually invoke as dependencies for this
    route -- as opposed to what require_api_key does when called directly,
    this observes attachment."""
    return {dep.call for dep in route.dependant.dependencies}


ALL_ROUTE_METHODS = list(_all_route_methods())
GET_ROUTE_METHODS = [(p, m) for p, m in ALL_ROUTE_METHODS if m == "GET"]
NON_GET_ROUTE_METHODS = [(p, m) for p, m in ALL_ROUTE_METHODS if m != "GET"]
ALL_ROUTES = list(_all_routes())
_UNFILTERED_WALK_PATHS = {r.path for r in _walk_api_routes(app.routes)}

# Sanity check on the fixture itself: routes.py has both GET and write routes,
# so an empty list here would mean the composed app's route tree changed
# shape and the enumeration below is silently checking nothing.
assert GET_ROUTE_METHODS
assert NON_GET_ROUTE_METHODS
assert ALL_ROUTES

# Sanity check on the allowlist itself, in both directions: /health must be
# reachable by the walk at all (or excluding it below is vacuous and this
# fixture would pass even if _walk_api_routes stopped finding it), and must
# not appear in ALL_ROUTES (or the carve-out silently isn't applied and
# /health would be asserted to require an API key it deliberately lacks).
assert "/health" in _UNFILTERED_WALK_PATHS
assert "/health" not in {r.path for r in ALL_ROUTES}


def test_health_route_is_intentionally_unauthenticated():
    """Documents the one carve-out: main.py:68 serves GET /health without
    Depends(require_api_key) as a liveness probe, not a gap the ratchet
    should flag. If this ever starts failing, either /health gained a
    dependency (drop it from UNAUTHENTICATED_ROUTE_PATHS) or the app grew a
    second route at this path that isn't meant to be exempt."""
    (health_route,) = [
        r for r in _walk_api_routes(app.routes) if r.path == "/health"
    ]
    assert require_api_key not in _route_dependency_calls(health_route)


@pytest.mark.parametrize(
    "route", ALL_ROUTES, ids=lambda r: f"{sorted(r.methods or [])} {r.path}"
)
def test_every_route_declares_require_api_key(route):
    assert require_api_key in _route_dependency_calls(route)


@pytest.fixture(autouse=True)
def both_keys(monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("SUBSTRATE_READ_API_KEY", READ_KEY)


@pytest.mark.parametrize("path,method", GET_ROUTE_METHODS)
def test_read_key_accepted_on_every_get_route(path, method):
    require_api_key(_request(method), x_api_key=READ_KEY)  # must not raise


@pytest.mark.parametrize("path,method", NON_GET_ROUTE_METHODS)
def test_read_key_refused_on_every_non_get_route(path, method):
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(_request(method), x_api_key=READ_KEY)
    assert exc_info.value.status_code == 403
    assert "read-only key cannot write" in str(exc_info.value.detail)


@pytest.mark.parametrize("path,method", ALL_ROUTE_METHODS)
def test_write_key_still_works_on_every_route(path, method):
    require_api_key(_request(method), x_api_key=WRITE_KEY)  # must not raise


def test_missing_key_is_401():
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(_request("GET"), x_api_key=None)
    assert exc_info.value.status_code == 401


def test_wrong_key_is_401():
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(_request("POST"), x_api_key="not-a-real-key")
    assert exc_info.value.status_code == 401


def test_missing_substrate_api_key_is_503_even_with_read_key_configured(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(_request("GET"), x_api_key=READ_KEY)
    assert exc_info.value.status_code == 503


class TestUnsetReadKeyIsByteIdenticalToToday:
    """SUBSTRATE_READ_API_KEY absent must reproduce require_api_key's
    pre-existing behaviour exactly -- these mirror the write-key-only
    assertions made elsewhere in this suite (e.g. test_bead_link.py's
    ``test_links_require_api_key``), run here with the new variable absent."""

    @pytest.fixture(autouse=True)
    def _no_read_key(self, monkeypatch):
        monkeypatch.delenv("SUBSTRATE_READ_API_KEY", raising=False)

    @pytest.mark.parametrize("path,method", ALL_ROUTE_METHODS)
    def test_write_key_accepted_on_every_route(self, path, method):
        require_api_key(_request(method), x_api_key=WRITE_KEY)  # must not raise

    def test_missing_key_is_401(self):
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(_request("GET"), x_api_key=None)
        assert exc_info.value.status_code == 401

    def test_wrong_key_is_401(self):
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(_request("GET"), x_api_key="not-a-real-key")
        assert exc_info.value.status_code == 401

    def test_missing_substrate_api_key_is_503(self, monkeypatch):
        monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(_request("GET"), x_api_key=WRITE_KEY)
        assert exc_info.value.status_code == 503


class TestDegenerateReadKeyDoesNotDisableWrites:
    """F1: a read key that equals the write key, or is whitespace-only, is
    the likeliest provisioning slip (the two are provisioned side by side)
    and must not silently turn every write into a 403 'read-only key cannot
    write'. It is treated as unset instead."""

    @pytest.mark.parametrize("path,method", ALL_ROUTE_METHODS)
    def test_write_key_still_works_when_read_key_equals_write_key(
        self, monkeypatch, path, method
    ):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", WRITE_KEY)
        require_api_key(_request(method), x_api_key=WRITE_KEY)  # must not raise

    def test_whitespace_read_key_does_not_authenticate(self, monkeypatch):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", "   ")
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(_request("GET"), x_api_key="   ")
        assert exc_info.value.status_code == 401

    def test_whitespace_read_key_leaves_write_key_working(self, monkeypatch):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", "   ")
        require_api_key(_request("POST"), x_api_key=WRITE_KEY)  # must not raise


class TestNonAsciiReadKeyDoesNotCrashOrDisableWrites:
    """F-1: secrets.compare_digest raises TypeError on a non-ASCII operand.
    Without a guard, a read key containing one stray smart quote or accented
    character would 500 every authenticated route -- reads and writes alike
    -- while GET /health (no dependency) kept returning 200, so monitoring
    would read green through a total outage. A non-ASCII read key must be
    treated as unset, the same as blank or identical-to-write-key."""

    @pytest.mark.parametrize("path,method", ALL_ROUTE_METHODS)
    def test_write_key_still_works_when_read_key_is_non_ascii(
        self, monkeypatch, path, method
    ):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", "réad-key")
        require_api_key(_request(method), x_api_key=WRITE_KEY)  # must not raise

    def test_non_ascii_read_key_does_not_authenticate(self, monkeypatch):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", "réad-key")
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(_request("GET"), x_api_key="wrong-key")
        assert exc_info.value.status_code == 401


class TestDegenerateReadKeyWarns:
    """F-F: the log line at routes.py is the only operator-visible signal
    that a configured SUBSTRATE_READ_API_KEY is being silently ignored
    (equal to the write key, or whitespace-only) -- deleting it left the
    suite green. Each test resets the process-lifetime "warned once" latch
    so the outcome does not depend on whether some earlier test in the
    session already tripped it."""

    @pytest.fixture(autouse=True)
    def _reset_warned_once(self, monkeypatch):
        monkeypatch.setattr(routes, "_warned_degenerate_read_key", False)

    def test_degenerate_read_key_logs_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", WRITE_KEY)
        with caplog.at_level(logging.WARNING, logger=routes.__name__):
            require_api_key(_request("GET"), x_api_key=WRITE_KEY)
        assert any(
            "SUBSTRATE_READ_API_KEY" in record.getMessage()
            for record in caplog.records
        )

    def test_degenerate_read_key_warns_only_once_per_process(self, monkeypatch, caplog):
        monkeypatch.setenv("SUBSTRATE_READ_API_KEY", WRITE_KEY)
        with caplog.at_level(logging.WARNING, logger=routes.__name__):
            require_api_key(_request("GET"), x_api_key=WRITE_KEY)
            require_api_key(_request("GET"), x_api_key=WRITE_KEY)
        assert len(caplog.records) == 1
