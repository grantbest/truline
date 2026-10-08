"""``tools.factory_probe`` must run apps/factory-dispatcher/probe_console_surface.py's
own ``run_and_record`` -- never a re-derived orchestration (PRIN-005) -- and must
degrade to a declared ``status: "unknown"`` (PRIN-015) when the checkout, the
substrate config, or Temporal cannot be reached. No browser, no cluster, no
real Temporal client, no real HTTP: every dependency below is either an
in-process fake or a monkeypatched seam.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER_DIR = REPO_ROOT / "apps" / "factory-dispatcher"

# openapi_app (and everything it transitively imports, including
# routers.v1.finance's `from workflows.finance_reconciliation import ...`)
# is imported FIRST, while sys.path holds only mcp-hub's own "src" --
# finance.py's `workflows` is a namespace package there (no __init__.py). The
# dispatcher checkout's own apps/factory-dispatcher/workflows/ IS a regular
# package (it has __init__.py), and a regular package anywhere on sys.path
# always wins name resolution over a namespace package, regardless of
# position -- so DISPATCHER_DIR must never reach sys.path before `workflows`
# has already been imported and cached, or every route module that touches
# finance breaks at collection time.
import access_auth  # noqa: E402
import openapi_app  # noqa: E402
import routers.v1.factory as factory_router  # noqa: E402
from tools import factory_probe  # noqa: E402

if str(DISPATCHER_DIR) not in sys.path:
    sys.path.append(str(DISPATCHER_DIR))

import probe_console_surface as probe  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _fake_result(outcome="demonstrated", divergences=None) -> probe.ProbeResult:
    return probe.ProbeResult(
        probe_at="2026-09-23T00:00:00Z",
        ran_by="tester",
        probed_revision="deadbeef",
        operations=["GET /api/v1/factory/schedule_status"],
        release_refs=["R26.09"],
        schedule=probe.GroupResult("schedule", "demonstrated"),
        releases={"R26.09": probe.GroupResult("release[R26.09]", "demonstrated")},
        outcome=outcome,
        divergences=divergences or [],
    )


# ---------------------------------------------------------------------------
# run_console_surface_probe -- declared-unknown degradation paths (PRIN-015)
# ---------------------------------------------------------------------------


def test_unknown_when_dispatcher_root_is_unset(monkeypatch):
    monkeypatch.delenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, raising=False)
    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "unknown"
    assert factory_probe.FACTORY_DISPATCHER_ROOT_ENV in result["detail"]


def test_unknown_when_dispatcher_checkout_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(tmp_path))
    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "unknown"
    assert "not found" in result["detail"]


def test_unknown_when_substrate_env_is_not_configured(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "unknown"
    assert "SUBSTRATE_URL" in result["detail"]


def test_unknown_when_temporal_is_unreachable(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")

    async def _raise():
        raise RuntimeError("Temporal is unreachable")

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _raise)

    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "unknown"
    assert "Temporal is unreachable" in result["detail"]


# ---------------------------------------------------------------------------
# run_console_surface_probe -- happy path. probe_console_surface.run_and_record
# itself is faked (the real checkout module is imported and reused for
# everything else) so this test never builds a real httpx or Temporal client.
# ---------------------------------------------------------------------------


def test_happy_path_reuses_the_real_probe_module_and_shapes_the_response(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.delenv("GIT_SHA", raising=False)

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "deadbeef")

    captured: dict = {}

    async def _fake_run_and_record(**kwargs):
        captured.update(kwargs)
        return {"result": _fake_result(), "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    result = _run(
        factory_probe.run_console_surface_probe(release_refs=["R26.09"], client_identity="tester")
    )

    assert result == {
        "status": "ok",
        "outcome": "demonstrated",
        "probe_at": "2026-09-23T00:00:00Z",
        "probed_revision": "deadbeef",
        "checked_releases": ["R26.09"],
        "operations": ["GET /api/v1/factory/schedule_status"],
        "groups": {"schedule": "demonstrated", "releases": {"R26.09": "demonstrated"}},
        "not_probed": list(probe.NOT_PROBED),
        "divergences": [],
        "detail": "",
        "observation_bead_id": "bead-1",
    }
    assert captured["release_refs"] == ["R26.09"]
    assert captured["ran_by"] == "tester"
    assert captured["probed_revision"] == "deadbeef"
    # One HTTP port, pointed at the gateway itself -- never the substrate
    # proxy a browser also reaches.
    assert isinstance(captured["gateway"], probe.HttpxJsonClient)
    assert isinstance(captured["schedule_authority"], probe.TemporalScheduleAuthority)


# ---------------------------------------------------------------------------
# AC-2: probed_revision must be real in the gateway -- the image's own
# build-time GIT_SHA (Dockerfile:75-76, set from GITHUB_SHA in CI) when it
# names a real build, else the dispatcher checkout's own git revision.
# ---------------------------------------------------------------------------


def test_probed_revision_prefers_the_images_git_sha(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("GIT_SHA", "abc123")

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)

    def _must_not_be_called(repo_dir):
        raise AssertionError("git_revision must not run when GIT_SHA names a real build")

    monkeypatch.setattr(probe, "git_revision", _must_not_be_called)

    captured: dict = {}

    async def _fake_run_and_record(**kwargs):
        captured.update(kwargs)
        result = _fake_result()
        result = dataclasses.replace(result, probed_revision=kwargs["probed_revision"])
        return {"result": result, "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    result = _run(
        factory_probe.run_console_surface_probe(release_refs=["R26.09"], client_identity="tester")
    )
    assert result["probed_revision"] == "abc123"
    assert captured["probed_revision"] == "abc123"


def test_probed_revision_falls_back_to_git_when_git_sha_is_the_dockerfiles_own_default(monkeypatch):
    """"unknown" is Dockerfile:75's own ARG default for a build with no
    --build-arg -- not a real revision, so it must not be reported as one."""
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setenv("GIT_SHA", "unknown")

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "checkout-revision")

    captured: dict = {}

    async def _fake_run_and_record(**kwargs):
        captured.update(kwargs)
        return {"result": _fake_result(), "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    _run(factory_probe.run_console_surface_probe(release_refs=["R26.09"], client_identity="tester"))
    assert captured["probed_revision"] == "checkout-revision"


def test_probed_revision_falls_back_to_git_when_git_sha_is_unset(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.delenv("GIT_SHA", raising=False)

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "checkout-revision")

    captured: dict = {}

    async def _fake_run_and_record(**kwargs):
        captured.update(kwargs)
        return {"result": _fake_result(), "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    _run(factory_probe.run_console_surface_probe(release_refs=["R26.09"], client_identity="tester"))
    assert captured["probed_revision"] == "checkout-revision"


# ---------------------------------------------------------------------------
# AC-3: an exception that escapes run_and_record itself (beyond the
# refused-write / unreachable-store cases run_and_record already reports as
# failed:record) must still not 500 this route.
# ---------------------------------------------------------------------------


def test_unknown_when_run_and_record_raises_unexpectedly(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.delenv("GIT_SHA", raising=False)

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "deadbeef")

    async def _raise(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(probe, "run_and_record", _raise)

    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "unknown"
    assert "unexpected" in result["detail"]


def test_divergence_reports_never_read_status_ok_as_demonstrated(monkeypatch):
    """A divergence-carrying result still answers status: ok (the HTTP call
    itself succeeded) with outcome != "demonstrated" -- PRIN-015's fail-closed
    posture lives in ``outcome``, not in the transport-level ``status``."""
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.delenv("GIT_SHA", raising=False)

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "deadbeef")

    async def _fake_run_and_record(**kwargs):
        result = probe.ProbeResult(
            probe_at="2026-09-23T00:00:00Z",
            ran_by="tester",
            probed_revision="deadbeef",
            operations=[],
            release_refs=["R26.09"],
            schedule=probe.GroupResult("schedule", "failed:compare", [probe.Divergence("schedule", "schedule.paused", False, True)]),
            releases={},
            outcome="failed:schedule",
            divergences=[probe.Divergence("schedule", "schedule.paused", False, True)],
        )
        return {"result": result, "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    result = _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))
    assert result["status"] == "ok"
    assert result["outcome"] == "failed:schedule"
    assert result["divergences"] == [
        {"group": "schedule", "field": "schedule.paused", "claim": False, "authority": True}
    ]


def test_refused_write_reports_failed_record_with_no_bead_id_never_a_500(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.delenv("GIT_SHA", raising=False)

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "deadbeef")

    async def _fake_run_and_record(**kwargs):
        # Mirrors what probe.run_and_record itself returns for a refused
        # write -- this test is at the tools.factory_probe boundary, so
        # run_and_record's own catch-and-report behaviour is covered by
        # apps/factory-dispatcher/tests/test_probe_console_surface.py.
        result = _fake_result(outcome="failed:record")
        return {"result": result, "bead": None}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    result = _run(factory_probe.run_console_surface_probe(release_refs=["R26.09"], client_identity="tester"))
    assert result["status"] == "ok"
    assert result["outcome"] == "failed:record"
    assert result["observation_bead_id"] is None


# ---------------------------------------------------------------------------
# #1052 gate required change 6 (AC-7): the probe's default gateway headers
# must carry the process-local internal-call credential, so the route keeps
# working once MCP_HUB_IDENTITY_MODE=enforce is flipped.
# ---------------------------------------------------------------------------


def test_default_gateway_headers_carry_the_process_internal_call_token(monkeypatch):
    monkeypatch.setenv(factory_probe.FACTORY_DISPATCHER_ROOT_ENV, str(REPO_ROOT))
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")

    async def _fake_get_client():
        return SimpleNamespace()

    monkeypatch.setattr(factory_probe.factory_merge, "get_factory_dispatcher_client", _fake_get_client)
    monkeypatch.setattr(probe, "git_revision", lambda repo_dir: "deadbeef")

    captured_headers: dict = {}
    real_client_cls = probe.HttpxJsonClient

    class _CapturingClient(real_client_cls):
        def __init__(self, base_url, headers):
            captured_headers.update(headers)
            super().__init__(base_url, headers)

    monkeypatch.setattr(probe, "HttpxJsonClient", _CapturingClient)

    async def _fake_run_and_record(**kwargs):
        return {"result": _fake_result(), "bead": {"id": "bead-1"}}

    monkeypatch.setattr(probe, "run_and_record", _fake_run_and_record)

    _run(factory_probe.run_console_surface_probe(release_refs=None, client_identity="tester"))

    assert captured_headers.get("X-Truline-Internal-Call") == access_auth.PROCESS_INTERNAL_CALL_TOKEN


# ---------------------------------------------------------------------------
# The gateway route: auth shape (mirrors test_factory_schedule_status.py)
# ---------------------------------------------------------------------------

PROBE_PATH = "/api/v1/factory/probe"

HUMAN = {"X-Truline-Client": "operator@example.org", "X-Truline-Client-Type": "human", "X-Truline-Scopes": "*"}
SERVICE_WITH_SCOPE = {
    "X-Truline-Client": "agent-dev",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "factory.read",
}
SERVICE_NO_SCOPE = {
    "X-Truline-Client": "pipeline-probe",
    "X-Truline-Client-Type": "service",
    "X-Truline-Scopes": "probe.read",
}


@pytest.fixture
def stub_run_console_surface_probe(monkeypatch):
    """Not autouse -- the tool-level tests above must exercise the real
    function, mirroring test_factory_schedule_status.py's identical rationale."""

    async def _fake(*, release_refs, client_identity):
        return {
            "status": "ok",
            "outcome": "demonstrated",
            "probe_at": "2026-09-23T00:00:00Z",
            "probed_revision": "deadbeef",
            "checked_releases": release_refs or ["R26.09"],
            "operations": [],
            "groups": {"schedule": "demonstrated", "releases": {}},
            "not_probed": list(probe.NOT_PROBED),
            "divergences": [],
            "detail": "",
            "observation_bead_id": "bead-1",
        }

    monkeypatch.setattr(factory_router.factory_probe, "run_console_surface_probe", _fake)


def test_denied_without_any_identity(stub_run_console_surface_probe):
    with TestClient(openapi_app.app) as client:
        response = client.get(PROBE_PATH)
    assert response.status_code == 401
    assert "Truline identity" in response.text


def test_denied_for_authenticated_client_without_scope(stub_run_console_surface_probe):
    with TestClient(openapi_app.app) as client:
        response = client.get(PROBE_PATH, headers=SERVICE_NO_SCOPE)
    assert response.status_code == 403
    assert "factory.read" in response.text


def test_allowed_for_human_wildcard_like_the_console(stub_run_console_surface_probe):
    with TestClient(openapi_app.app) as client:
        response = client.get(PROBE_PATH, headers=HUMAN)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["outcome"] == "demonstrated"


def test_allowed_for_service_with_scope(stub_run_console_surface_probe):
    with TestClient(openapi_app.app) as client:
        response = client.get(PROBE_PATH, headers=SERVICE_WITH_SCOPE)
    assert response.status_code == 200


def test_release_ref_query_params_are_forwarded(monkeypatch):
    captured: dict = {}

    async def _fake(*, release_refs, client_identity):
        captured["release_refs"] = release_refs
        captured["client_identity"] = client_identity
        return {
            "status": "ok",
            "outcome": "demonstrated",
            "probe_at": "2026-09-23T00:00:00Z",
            "probed_revision": "deadbeef",
            "checked_releases": release_refs or [],
            "operations": [],
            "groups": {"schedule": "demonstrated", "releases": {}},
            "not_probed": list(probe.NOT_PROBED),
            "divergences": [],
            "detail": "",
            "observation_bead_id": "bead-1",
        }

    monkeypatch.setattr(factory_router.factory_probe, "run_console_surface_probe", _fake)

    with TestClient(openapi_app.app) as client:
        response = client.get(f"{PROBE_PATH}?release_ref=R26.09&release_ref=R26.01", headers=HUMAN)
    assert response.status_code == 200
    assert captured["release_refs"] == ["R26.09", "R26.01"]
    assert captured["client_identity"] == "operator@example.org"
