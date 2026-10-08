"""Runs the console-surface probe (R26.09/O-7) from inside the gateway
process.

Imports and calls ``apps/factory-dispatcher/probe_console_surface.py``'s own
``run_and_record`` -- the exact function the CLI calls -- through
``FACTORY_DISPATCHER_ROOT`` (``tools/task_filing.py``'s variable, always set
in this image; distinct from ``tools/factory_status.py``'s optional-by-design
``FACTORY_STATUS_REPO_ROOT``, see that module's own docstring). Never a
second, re-derived orchestration; see that module's own docstring for why
the claim step reads only the gateway's own two routes over loopback, never
the substrate proxy a browser reaches.

The Temporal client is ``tools.factory_merge.get_factory_dispatcher_client()``
-- the same cached client already opened against the dispatcher's own
namespace/task-queue for the merge-by-verdict and schedule-status surfaces.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
from typing import Any

from access_auth import PROCESS_INTERNAL_CALL_TOKEN
from tools import factory_merge

#: Distinct from tools.factory_status.REPO_ROOT_ENV -- see tools/
#: factory_schedule_status.py's identical rationale for why this route needs
#: the "always set" variable, not the optional-by-design one.
FACTORY_DISPATCHER_ROOT_ENV = "FACTORY_DISPATCHER_ROOT"

GATEWAY_BASE_URL_ENV = "FACTORY_PROBE_GATEWAY_URL"
DEFAULT_GATEWAY_BASE_URL = "http://localhost:8000"

#: Set by the Dockerfile's own `ARG GIT_SHA=unknown` / `ENV GIT_SHA=$GIT_SHA`
#: (apps/mcp-hub/Dockerfile:75-76), populated from GITHUB_SHA at build time
#: (.github/workflows/build-mcp-hub.yml:52) in every image this route
#: actually runs in. "unknown" is the Dockerfile's own default for a build
#: with no --build-arg, e.g. a local `docker build` -- not a real revision.
GIT_SHA_ENV = "GIT_SHA"
UNCONFIGURED_GIT_SHA = "unknown"

DISPATCH_SCHEDULE_ID_ENV = "FACTORY_DISPATCH_SCHEDULE_ID"
DEFAULT_DISPATCH_SCHEDULE_ID = "factory-dispatcher-dev"


class Unavailable(RuntimeError):
    """FACTORY_DISPATCHER_ROOT is unset, the checkout it names can't be
    imported, or SUBSTRATE_URL/SUBSTRATE_API_KEY are not configured. Always a
    bug to fix in this image -- the Dockerfile sets FACTORY_DISPATCHER_ROOT
    unconditionally (see tools/task_filing.py's identical rationale)."""


def _dispatcher_root() -> pathlib.Path:
    root = os.environ.get(FACTORY_DISPATCHER_ROOT_ENV)
    if not root:
        raise Unavailable(
            f"{FACTORY_DISPATCHER_ROOT_ENV} is not set -- the dispatcher tree "
            "this route runs the probe through is not reachable."
        )
    return pathlib.Path(root)


def _dispatcher_dir() -> pathlib.Path:
    return _dispatcher_root() / "apps" / "factory-dispatcher"


def _probe_module() -> Any:
    dispatcher_dir = _dispatcher_dir()
    if not dispatcher_dir.is_dir():
        raise Unavailable(f"factory-dispatcher checkout not found at {dispatcher_dir}")
    if str(dispatcher_dir) not in sys.path:
        sys.path.insert(0, str(dispatcher_dir))
    try:
        # dispatch first -- sidesteps the same import-cycle hazard
        # tools/task_filing.py's docstring documents for file_task; probe_
        # console_surface has no such cycle itself, but importing dispatch
        # first costs nothing and keeps this loader identical in shape to
        # every other module in this file that reaches into the checkout.
        importlib.import_module("dispatch")
        return importlib.import_module("probe_console_surface")
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(
            f"factory-dispatcher's probe_console_surface could not be imported: {exc}"
        ) from exc


def _probed_revision(probe: Any, dispatcher_root: pathlib.Path) -> str:
    """The image's own build-time GIT_SHA when it names a real build, else
    the dispatcher checkout's own git revision (the CLI's only option, and
    this route's fallback for a build with no --build-arg)."""
    git_sha = os.environ.get(GIT_SHA_ENV)
    if git_sha and git_sha != UNCONFIGURED_GIT_SHA:
        return git_sha
    return probe.git_revision(dispatcher_root)


def _shared_substrate_client() -> Any:
    """The shared ``apps/substrate/client`` package, loaded the private way
    ``substrate_client_loader.py`` loads it -- reused here rather than
    reimplemented, now that ``_probe_module`` has already put the dispatcher
    dir on ``sys.path``."""
    dispatcher_dir = _dispatcher_dir()
    if str(dispatcher_dir) not in sys.path:
        sys.path.insert(0, str(dispatcher_dir))
    loader = importlib.import_module("substrate_client_loader")
    return loader.Substrate


async def run_console_surface_probe(
    *,
    release_refs: list[str] | None,
    client_identity: str,
    gateway_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The probe's gateway-side entry point -- ``GET /api/v1/factory/probe``.

    Degrades to a declared ``status: "unknown"`` (PRIN-015) rather than
    raising, the same posture every other checkout-dependent route in this
    package takes (tools/factory_status.py, tools/factory_schedule_status.py).
    A refused observation write is a different thing: ``run_and_record``
    itself catches that and reports ``outcome: "failed:record"`` inside a
    normal ``status: "ok"`` response, so this route never 500s on it.
    """
    try:
        probe = _probe_module()
    except Unavailable as exc:
        return {"status": "unknown", "detail": str(exc)}

    try:
        shared_substrate_cls = _shared_substrate_client()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"shared substrate client could not be loaded: {exc}"}

    substrate_url = os.environ.get("SUBSTRATE_URL")
    substrate_key = os.environ.get("SUBSTRATE_API_KEY")
    if not substrate_url or not substrate_key:
        return {"status": "unknown", "detail": "SUBSTRATE_URL/SUBSTRATE_API_KEY are not configured"}

    try:
        store = shared_substrate_cls(substrate_url, substrate_key)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"could not construct the shared substrate client: {exc}"}

    gateway_base_url = os.environ.get(GATEWAY_BASE_URL_ENV, DEFAULT_GATEWAY_BASE_URL)
    headers = gateway_headers or {
        "X-Truline-Client": client_identity,
        "X-Truline-Client-Type": "service",
        "X-Truline-Scopes": "factory.read",
        # This loopback call legitimately sends X-Truline-* headers with no
        # Cloudflare Access assertion behind them -- the process-local
        # credential access_auth.resolve_request_identity checks for
        # exactly that shape (AC-7), so this route keeps working once
        # MCP_HUB_IDENTITY_MODE=enforce is flipped.
        "X-Truline-Internal-Call": PROCESS_INTERNAL_CALL_TOKEN,
    }
    gateway = probe.HttpxJsonClient(gateway_base_url, headers)

    try:
        temporal_client = await factory_merge.get_factory_dispatcher_client()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"could not reach Temporal: {exc}"}
    schedule_authority = probe.TemporalScheduleAuthority(temporal_client)
    schedule_id = os.environ.get(DISPATCH_SCHEDULE_ID_ENV, DEFAULT_DISPATCH_SCHEDULE_ID)

    probed_revision = _probed_revision(probe, _dispatcher_root())

    try:
        outcome = await probe.run_and_record(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id=schedule_id,
            release_refs=release_refs,
            ran_by=client_identity,
            probed_revision=probed_revision,
            recorder=store,
        )
    except Exception as exc:  # noqa: BLE001 - run_and_record already reports refused writes and
        # unreachable-store failures as outcome: "failed:record" rather than
        # raising; anything that still escapes here is unanticipated, so this
        # route degrades the same way every earlier step in it does, rather
        # than 500ing.
        return {"status": "unknown", "detail": f"the probe raised unexpectedly: {exc}"}
    result = outcome["result"]
    bead = outcome["bead"]
    return {
        "status": "ok",
        "outcome": result.outcome,
        "probe_at": result.probe_at,
        "probed_revision": result.probed_revision,
        "checked_releases": result.release_refs,
        "operations": result.operations,
        "groups": {
            "schedule": result.schedule.outcome,
            "releases": {ref: g.outcome for ref, g in result.releases.items()},
        },
        "not_probed": list(probe.NOT_PROBED),
        "divergences": [
            {"group": d.group, "field": d.field, "claim": d.claim, "authority": d.authority}
            for d in result.divergences
        ],
        "detail": result.detail,
        "observation_bead_id": bead.get("id") if bead else None,
    }
