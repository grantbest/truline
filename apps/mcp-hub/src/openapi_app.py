import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional, Dict

from fastapi import Depends, FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from jose import jwt
import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap
from fastapi.openapi.utils import get_openapi
from starlette.middleware.base import BaseHTTPMiddleware

from access_auth import (
    AccessAuthError,
    MODE_ENFORCE,
    VERDICT_VERIFIER_UNAVAILABLE,
    access_token_from_headers,
    current_checked_scope,
    current_client_identity,
    current_identity_mode,
    current_identity_verdict,
    identity_from_claims,
    identity_from_headers,
    identity_headers,
    parse_mapping,
    rejection_for,
    require_authenticated_scope,
    resolve_request_identity,
    verify_access_jwt,
)
from tools.finance import substrate_base_url, substrate_headers

from routers.v1.types import FinanceCategory  # noqa: F401
from routers.v1.automations import router as automations_router
from routers.v1.context import router as context_router
from routers.v1.factory import router as factory_router
from routers.v1.homelab import router as homelab_router
from routers.v1.calendar import router as calendar_router
from routers.v1.finance import router as finance_router
from routers.v1.ha import router as ha_router
from routers.v1.vision import router as vision_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Plaid hosts/secrets for webhook validation
_PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


def _plaid_host() -> str:
    return _PLAID_HOSTS.get(os.environ.get("PLAID_ENV", "sandbox"), _PLAID_HOSTS["sandbox"])


def _plaid_secret() -> Optional[str]:
    env = os.environ.get("PLAID_ENV", "sandbox")
    return os.environ.get("PLAID_SANDBOX_SECRET") if env == "sandbox" else os.environ.get("PLAID_SECRET")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the FastMCP HTTP app lifespan
    async with mcp_http.lifespan(app):
        yield


app = FastAPI(
    title="Truline MCP Hub",
    description="Personal OS tool server. Exposes homelab, calendar, and finance tools as OpenAPI for Open WebUI.",
    version="0.1.1",
    lifespan=lifespan,
)

# CORS configuration
_mcp_cors_env = os.environ.get(
    "MCP_HUB_CORS_ORIGINS",
    "http://localhost:5173,https://console.example.org",
)
_mcp_cors_origins = [o.strip() for o in _mcp_cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_mcp_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


async def _identity_logging_dispatch(request: Request, call_next):
    """Bind the request's caller identity to the request context and log it.

    Shared by the FastAPI app and the mounted MCP ASGI app so the two surfaces
    can never drift on identity handling.

    /auth/cloudflare is excluded from resolution: that route verifies the
    assertion itself for ForwardAuth, and resolving it here too would double
    the verification per public request and turn its own 403 into a 401.
    Its behaviour is unchanged in every mode (AC-4).
    """
    start = time.monotonic()
    mode = current_identity_mode()

    rejection: Optional[tuple] = None
    try:
        if request.url.path == "/auth/cloudflare":
            identity = identity_from_headers(request.headers)
            verdict: Optional[str] = None
        else:
            client_host = request.client.host if request.client else None
            resolved = await resolve_request_identity(
                headers=request.headers,
                cookies=request.cookies,
                client_host=client_host,
                mode=mode,
            )
            identity = resolved.identity
            verdict = resolved.verdict
            rejection = resolved.rejection
    except Exception:
        # Defense-in-depth: resolve_request_identity itself converts every
        # exception the *verifier* raises into a verdict (AC-6), but a bug
        # elsewhere in resolution (its own bookkeeping, not the injected
        # verifier) must still not skip the audit record below entirely --
        # that would repeat the exact failure this except guards against one
        # layer up (probe P1: a 500 with no mcp_hub_request record at all).
        logger.exception(
            "identity resolution raised unexpectedly; treating this request as %s",
            VERDICT_VERIFIER_UNAVAILABLE,
        )
        identity = identity_from_headers(request.headers)
        verdict = VERDICT_VERIFIER_UNAVAILABLE
        rejection = rejection_for(VERDICT_VERIFIER_UNAVAILABLE) if mode == MODE_ENFORCE else None

    # Amendment 23 / R26.09 O-6: check_scope/require_authenticated_scope
    # write the scope they checked into this container (access_auth.py) so
    # it's still readable here after call_next returns -- see that module's
    # current_checked_scope docstring for why a plain ContextVar wouldn't
    # survive the call_next task boundary.
    checked_scope_record: Dict[str, Optional[str]] = {"scope": None}

    identity_token = current_client_identity.set(identity)
    scope_token = current_checked_scope.set(checked_scope_record)
    verdict_token = current_identity_verdict.set(verdict)
    response: Optional[Response] = None
    caught_exc: Optional[BaseException] = None
    try:
        if rejection is not None:
            status_code, detail = rejection
            response = JSONResponse(status_code=status_code, content={"detail": detail})
        else:
            response = await call_next(request)
    except Exception as exc:  # noqa: BLE001 -- re-raised below, after logging
        caught_exc = exc
    finally:
        current_client_identity.reset(identity_token)
        current_checked_scope.reset(scope_token)
        current_identity_verdict.reset(verdict_token)

    # Amendment 23 / R26.09 O-6, F1 on the #823 gate: this used to sit after
    # the try/finally, so an exception that escaped call_next (an unhandled
    # error in a route handler, not an HTTPException -- those are already
    # converted to a response by Starlette's ExceptionMiddleware before they
    # reach this middleware) skipped this log line entirely. That's the one
    # outcome class -- an unhandled 5xx -- an auditor most wants a record
    # of, so it must be logged unconditionally before the exception (if any)
    # propagates to Starlette's ServerErrorMiddleware for the actual 500
    # response.
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    route = request.scope.get("route")
    capability = getattr(route, "operation_id", None) or getattr(route, "name", None) or request.url.path
    checked_scope = checked_scope_record["scope"]
    status_code = response.status_code if response is not None else 500
    outcome = f"{status_code // 100}xx"
    client_host_for_log = request.client.host if request.client else "-"
    logger.info(
        "mcp_hub_request client=%s client_type=%s scopes=%s capability=%s checked_scope=%s "
        "method=%s path=%s status=%s outcome=%s duration_ms=%s user_agent=%s "
        "identity_verdict=%s client_host=%s",
        identity.client,
        identity.client_type,
        identity.scopes,
        capability,
        checked_scope,
        request.method,
        request.url.path,
        status_code,
        outcome,
        duration_ms,
        request.headers.get("user-agent", "-"),
        verdict,
        client_host_for_log,
        extra={
            "audit_identity": identity.client,
            "audit_capability": capability,
            "audit_scope": checked_scope,
            "audit_outcome": outcome,
            "audit_identity_verdict": verdict,
            "audit_client_host": client_host_for_log,
        },
    )
    if caught_exc is not None:
        raise caught_exc
    return response


@app.middleware("http")
async def log_client_identity(request: Request, call_next):
    return await _identity_logging_dispatch(request, call_next)


@app.get("/health")
async def health() -> Dict[str, str]:
    # git_sha is baked in at build time (Dockerfile ARG/ENV GIT_SHA) so the
    # commit a running pod actually serves is observable from outside the
    # cluster -- curl this and compare against main's HEAD -- without
    # trusting `kubectl rollout status`, which reports Available on a
    # successful re-run of a STALE build the same way it reports Available
    # on a fresh one (build-mcp-hub.yml failed on every commit 2026-09-03 to
    # 2026-09-11 while a nine-day-old image kept serving as healthy).
    # "unknown" is the honest default for any build that did not pass the
    # build arg, never a guess.
    return {"status": "ok", "git_sha": os.environ.get("GIT_SHA", "unknown")}


def _access_token_from_request(request: Request) -> Optional[str]:
    return access_token_from_headers(request.headers, request.cookies)


@app.api_route("/auth/cloudflare", methods=["GET", "POST", "HEAD"], include_in_schema=False)
async def cloudflare_access_forward_auth(request: Request) -> Response:
    token = _access_token_from_request(request)
    if not token:
        logger.warning("cloudflare access auth rejected: missing assertion")
        raise HTTPException(status_code=403, detail="Missing Cloudflare Access assertion")

    team_domain = os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "truline")
    accepted_audiences = os.environ.get("CLOUDFLARE_ACCESS_AUDS", "")
    try:
        claims = await verify_access_jwt(
            token,
            team_domain=team_domain,
            accepted_audiences=accepted_audiences,
        )
    except AccessAuthError as exc:
        logger.warning("cloudflare access auth rejected: %s", exc)
        raise HTTPException(status_code=403, detail="Invalid Cloudflare Access assertion") from exc

    identity = identity_from_claims(
        claims,
        fallback_email=request.headers.get("cf-access-authenticated-user-email"),
        service_token_names=parse_mapping(os.environ.get("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "")),
        service_token_scopes=parse_mapping(os.environ.get("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "")),
    )
    logger.info(
        "cloudflare access auth accepted: client=%s client_type=%s scopes=%s aud=%s",
        identity.client,
        identity.client_type,
        identity.scopes,
        claims.get("aud"),
    )
    return Response(status_code=204, headers=identity_headers(identity))


_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_SUBSTRATE_PROXY_TIMEOUT = 30.0


@app.api_route(
    "/api/v1/substrate/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=True,
    tags=["Substrate"],
)
@app.api_route(
    "/substrate/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def substrate_proxy(path: str, request: Request) -> Response:
    # Raw substrate passthrough injects the real X-API-Key — humans (scopes "*")
    # keep it for the console, but no machine token is granted substrate.proxy.
    #
    # require_authenticated_scope (not check_scope): this is the one door
    # onto the whole substrate store from mcp-hub's HTTP surface, so it must
    # be reachable by exactly the identities that reach the console today,
    # and by nobody who reaches mcp-hub directly without Traefik's
    # ForwardAuth — see access_auth.require_authenticated_scope's docstring
    # and release-gate finding on #661.
    require_authenticated_scope("substrate.proxy")
    target = f"{substrate_base_url()}/{path}"

    fwd_headers: Dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP_HEADERS:
            continue
        if lk in ("x-api-key", "x-truline-internal-call"):
            continue
        fwd_headers[k] = v
    fwd_headers.update(substrate_headers())

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=_SUBSTRATE_PROXY_TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                target,
                content=body if body else None,
                params=request.query_params,
                headers=fwd_headers,
            )
    except httpx.RequestError as exc:
        logger.exception("substrate proxy upstream error for %s %s", request.method, path)
        raise HTTPException(status_code=502, detail=f"Substrate upstream error: {exc}") from exc

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def verify_plaid_webhook(request: Request, plaid_verification: Optional[str] = Header(None)):
    if not plaid_verification:
        logger.warning("plaid webhook rejected: missing Plaid-Verification header")
        raise HTTPException(status_code=401, detail="Missing Plaid-Verification header")

    try:
        kid = jwt.get_unverified_header(plaid_verification).get("kid")
    except Exception as e:
        logger.warning("plaid webhook rejected: malformed JWT header (%s)", e)
        raise HTTPException(status_code=401, detail="Invalid Plaid-Verification header")
    if not kid:
        raise HTTPException(status_code=401, detail="JWT missing kid")

    secret = _plaid_secret()
    if not secret or not os.environ.get("PLAID_CLIENT_ID"):
        logger.error("plaid webhook: client_id/secret env vars not set")
        raise HTTPException(status_code=503, detail="Plaid credentials not configured")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_plaid_host()}/webhook_verification_key/get",
            json={
                "client_id": os.environ.get("PLAID_CLIENT_ID"),
                "secret": secret,
                "key_id": kid,
            },
        )
        if resp.status_code != 200:
            logger.warning("plaid key fetch failed: status=%s", resp.status_code)
            raise HTTPException(status_code=502, detail="Could not fetch Plaid public keys")
        key_data = resp.json()["key"]

    body = await request.body()
    try:
        claims = jwt.decode(plaid_verification, key_data, algorithms=["ES256"])
    except Exception as e:
        logger.warning("plaid webhook rejected: JWT signature invalid (%s)", e)
        raise HTTPException(status_code=401, detail="Invalid Plaid-Verification header")

    body_sha = hashlib.sha256(body).hexdigest()
    if claims.get("request_body_sha256") != body_sha:
        logger.warning("plaid webhook rejected: body SHA256 mismatch (body_bytes=%d)", len(body))
        raise HTTPException(status_code=401, detail="Webhook body hash mismatch")


@app.post("/webhooks/plaid", summary="Plaid transaction webhooks", dependencies=[Depends(verify_plaid_webhook)])
async def plaid_webhook(request: Request) -> Dict[str, str]:
    body = await request.json()
    logger.info(
        "plaid webhook accepted: type=%s code=%s item_id=%s",
        body.get("webhook_type"), body.get("webhook_code"), body.get("item_id"),
    )
    return {"status": "received"}


# Include new versioned capability routers
app.include_router(context_router, prefix="/api/v1/context", tags=["Context"])
app.include_router(factory_router, prefix="/api/v1/factory", tags=["Factory"])
app.include_router(homelab_router, prefix="/api/v1/homelab", tags=["Homelab"])
app.include_router(calendar_router, prefix="/api/v1/calendar", tags=["Calendar"])
app.include_router(finance_router, prefix="/api/v1/finance", tags=["Finance"])
app.include_router(ha_router, prefix="/api/v1/ha", tags=["Home Assistant"])
app.include_router(vision_router, prefix="/api/v1/vision", tags=["Vision"])
app.include_router(automations_router, prefix="/api/v1/automations", tags=["Automations"])

# Include unversioned legacy paths for backward compatibility (hidden from schema)
app.include_router(context_router, prefix="/context", include_in_schema=False)
app.include_router(factory_router, prefix="/factory", include_in_schema=False)
app.include_router(homelab_router, prefix="/homelab", include_in_schema=False)
app.include_router(calendar_router, prefix="/calendar", include_in_schema=False)
app.include_router(finance_router, prefix="/finance", include_in_schema=False)
app.include_router(ha_router, prefix="/ha", include_in_schema=False)
app.include_router(vision_router, prefix="/vision", include_in_schema=False)

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # Ensure stable ordering of path methods to prevent non-deterministic diffs in CI
    new_paths = {}
    for path, methods in openapi_schema.get("paths", {}).items():
        sorted_methods = {}
        for method in sorted(methods.keys()):
            spec = methods[method]
            if "operationId" in spec and "substrate_proxy" in spec["operationId"]:
                spec["operationId"] = f"substrate_proxy_{method}"
            sorted_methods[method] = spec
        new_paths[path] = sorted_methods
    openapi_schema["paths"] = new_paths
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# Derive FastMCP tools from versioned routers. The substrate passthrough is
# excluded: as an MCP tool it would hand any authenticated agent the raw
# substrate API (key injected server-side) — remote agents get the curated
# capability tools only. /factory/note is excluded for a narrower reason: a
# dev.note with kind=answer, answers_ref=<id>, releases_work=true is the one
# thing that releases a held dev.task blocking question (guards.py's
# open_questions), so as an auto-discoverable MCP tool it would let a remote
# agent answer and release its own blocking question. The route stays
# reachable over plain HTTP, where factory.write grants are the Operator's call —
# see .factory/design.md. Non-matching routes fall through to TOOL (default).
mcp = FastMCP.from_fastapi(
    app,
    name="Truline MCP Hub",
    route_maps=[
        RouteMap(pattern=r"^/(?:api/v1/)?substrate/", mcp_type=MCPType.EXCLUDE),
        RouteMap(pattern=r"^/(?:api/v1/)?factory/note$", mcp_type=MCPType.EXCLUDE),
    ],
)
# Internal path "/" so the mounted endpoint is exactly /mcp (fastmcp's default
# internal path would stack with the mount prefix into /mcp/mcp).
mcp_http = mcp.http_app(path="/", transport="streamable-http")

mcp_http.add_middleware(BaseHTTPMiddleware, dispatch=_identity_logging_dispatch)

app.mount("/mcp", mcp_http)


class _McpPathRewrite:
    """Serve bare /mcp directly instead of 307-redirecting to /mcp/.

    Starlette's Mount redirects the slashless prefix, but the claude.ai MCP
    client (Claude-User) does not follow redirects on POST — the connector
    fails with "server returned an error" (observed live 2026-07-09). Rewrite
    the path before routing so both spellings hit the endpoint identically.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


app.add_middleware(_McpPathRewrite)
