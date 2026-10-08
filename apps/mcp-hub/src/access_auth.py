import asyncio
import contextvars
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException
import httpx
from jose import jwt


logger = logging.getLogger(__name__)


class AccessAuthError(Exception):
    """Raised when a Cloudflare Access assertion cannot be trusted."""


class VerifierUnavailableError(AccessAuthError):
    """The resolver's JWKS fetch failed or is in its post-failure cooldown.

    Distinct from AccessAuthError's other uses (a bad/expired/malformed
    assertion) -- this one means verification could not be attempted at
    all, which resolve_request_identity treats as `verifier-unavailable`,
    not `invalid-assertion`.
    """


class _KidNotFoundError(AccessAuthError):
    """The token's ``kid`` header is well-formed but absent from the
    currently-cached JWKS -- the ONLY case that should trigger AC-6's
    rate-limited refetch. A malformed JWT header or a token with no ``kid``
    at all is never a JWKS-staleness problem (refetching cannot fix it), so
    it must raise the plain :class:`AccessAuthError` above instead, which
    the refetch logic below does not catch.
    """


@dataclass(frozen=True)
class AccessIdentity:
    client: str
    client_type: str
    email: str = ""
    scopes: str = ""


# --- MCP_HUB_IDENTITY_MODE -------------------------------------------------
#
# Read fresh on every request (never cached at import), so tests can flip it
# with monkeypatch and a manifest change never needs a redeploy of anything
# but the env var itself.

IDENTITY_MODE_ENV = "MCP_HUB_IDENTITY_MODE"

MODE_LOG_ONLY = "log-only"
MODE_ENFORCE = "enforce"
MODE_TRUST_HEADERS = "trust-headers"

_VALID_MODES = frozenset({MODE_LOG_ONLY, MODE_ENFORCE, MODE_TRUST_HEADERS})

# A rollback typo must not enforce -- the underscore spellings are accepted
# as the same values as their hyphenated counterparts.
_MODE_ALIASES = {
    "log_only": MODE_LOG_ONLY,
    "trust_headers": MODE_TRUST_HEADERS,
}


def current_identity_mode() -> str:
    raw = (os.environ.get(IDENTITY_MODE_ENV) or "").strip()
    if not raw:
        return MODE_LOG_ONLY
    normalized = _MODE_ALIASES.get(raw, raw)
    if normalized in _VALID_MODES:
        return normalized
    # Never echo a header here -- only the (already-trusted) config value.
    logger.error(
        "%s has an unrecognized value %r; treating this request as %s",
        IDENTITY_MODE_ENV,
        raw,
        MODE_ENFORCE,
    )
    return MODE_ENFORCE


_JWKS_CACHE: Dict[str, Any] = {"expires_at": 0.0, "keys": []}
_JWKS_TTL_SECONDS = 300


def normalize_team_domain(team_domain: str) -> str:
    team_domain = team_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not team_domain:
        raise AccessAuthError("Cloudflare Access team domain is not configured")
    if "." not in team_domain:
        team_domain = f"{team_domain}.cloudflareaccess.com"
    return team_domain


def access_issuer(team_domain: str) -> str:
    return f"https://{normalize_team_domain(team_domain)}"


def access_certs_url(team_domain: str) -> str:
    return f"{access_issuer(team_domain)}/cdn-cgi/access/certs"


async def fetch_access_jwks(team_domain: str, *, force: bool = False) -> Dict[str, Any]:
    """Fetch (or return the cached) Cloudflare Access JWKS.

    ``force=True`` skips the cache *read* -- used by the resolver's kid-miss
    refetch (AC-6) to force a real network round trip despite a live 300s
    cache entry -- but the cache is still only ever *written* below, after a
    successful fetch, exactly as when ``force`` is false. A failed forced
    fetch therefore never touches ``_JWKS_CACHE``, so a transient failure on
    this path can never invalidate what Traefik's ForwardAuth (every public
    hostname, via ``/auth/cloudflare``) already has cached.
    """
    now = time.time()
    if not force and _JWKS_CACHE["keys"] and _JWKS_CACHE["expires_at"] > now:
        return {"keys": _JWKS_CACHE["keys"]}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(access_certs_url(team_domain))
        resp.raise_for_status()
        payload = resp.json()

    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise AccessAuthError("Cloudflare Access JWKS response did not contain keys")

    _JWKS_CACHE["keys"] = keys
    _JWKS_CACHE["expires_at"] = now + _JWKS_TTL_SECONDS
    return {"keys": keys}


def _accepted_audiences(raw_audiences: str) -> set[str]:
    return {aud.strip() for aud in raw_audiences.split(",") if aud.strip()}


def _claim_audiences(claims: Dict[str, Any]) -> set[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return {aud}
    if isinstance(aud, list):
        return {str(item) for item in aud}
    return set()


def _key_for_token(token: str, jwks: Dict[str, Any]) -> Dict[str, Any]:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception as exc:
        raise AccessAuthError("Cloudflare Access JWT header is malformed") from exc
    if not kid:
        raise AccessAuthError("Cloudflare Access JWT header is missing kid")

    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    raise _KidNotFoundError("Cloudflare Access JWT kid is not in current JWKS")


def validate_access_claims(claims: Dict[str, Any], accepted_audiences: str) -> None:
    accepted = _accepted_audiences(accepted_audiences)
    if not accepted:
        return

    token_audiences = _claim_audiences(claims)
    if not token_audiences.intersection(accepted):
        raise AccessAuthError("Cloudflare Access JWT audience is not accepted")


async def verify_access_jwt(
    token: str,
    *,
    team_domain: str,
    accepted_audiences: str = "",
) -> Dict[str, Any]:
    jwks = await fetch_access_jwks(team_domain)
    key = _key_for_token(token, jwks)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=access_issuer(team_domain),
            options={"verify_aud": False},
        )
    except Exception as exc:
        raise AccessAuthError("Cloudflare Access JWT signature or claims are invalid") from exc

    validate_access_claims(claims, accepted_audiences)
    return claims


def identity_from_claims(
    claims: Dict[str, Any],
    *,
    fallback_email: Optional[str] = None,
    service_token_names: Optional[Dict[str, str]] = None,
    service_token_scopes: Optional[Dict[str, str]] = None,
) -> AccessIdentity:
    common_name = str(claims.get("common_name") or "")
    email = str(claims.get("email") or fallback_email or "")

    service_token_names = service_token_names or {}
    service_token_scopes = service_token_scopes or {}

    if common_name:
        client = service_token_names.get(common_name, common_name)
        return AccessIdentity(
            client=client,
            client_type="service",
            scopes=service_token_scopes.get(client, service_token_scopes.get(common_name, "")),
        )

    if email:
        return AccessIdentity(client=email, client_type="human", email=email)

    subject = str(claims.get("sub") or "unknown-access-client")
    return AccessIdentity(client=subject, client_type="unknown")


def parse_mapping(raw_mapping: str) -> Dict[str, str]:
    """Parse "key=value" env mappings.

    Pairs are ";"-separated when a ";" is present, so values can hold
    comma-separated scope lists ("a=finance.read,automations.trigger;b=x").
    Without a ";", commas separate pairs only when every comma segment is
    itself a "key=value" pair (legacy "a=1,b=2"); otherwise the whole string
    is one pair whose value keeps its commas.
    """
    if ";" in raw_mapping:
        items = raw_mapping.split(";")
    else:
        comma_parts = [p for p in raw_mapping.split(",") if p.strip()]
        if comma_parts and all("=" in p for p in comma_parts):
            items = comma_parts
        else:
            items = [raw_mapping]

    mapping: Dict[str, str] = {}
    for item in items:
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if sep and key.strip() and value.strip():
            mapping[key.strip()] = value.strip()
    return mapping


def identity_headers(identity: AccessIdentity) -> Dict[str, str]:
    headers = {
        "X-Truline-Client": identity.client,
        "X-Truline-Client-Type": identity.client_type,
    }
    if identity.email:
        headers["X-Truline-Email"] = identity.email

    scopes = identity.scopes
    if identity.client_type == "human":
        scopes = "*"

    if scopes:
        headers["X-Truline-Scopes"] = scopes
    return headers


_IDENTITY_HEADER_NAMES = (
    "x-truline-client",
    "x-truline-client-type",
    "x-truline-scopes",
    "x-truline-email",
)


def identity_from_headers(headers: Any) -> AccessIdentity:
    """The identity _identity_logging_dispatch has always bound: the raw
    X-Truline-* headers as they arrive, defaulting to internal/unknown.

    This is the effective identity in log-only and trust-headers mode, and
    the fallback identity attached to a request enforce mode rejects before
    any route runs (never used to authorize anything in that case).
    """
    return AccessIdentity(
        client=headers.get("x-truline-client", "internal/unknown"),
        client_type=headers.get("x-truline-client-type", "internal"),
        scopes=headers.get("x-truline-scopes", ""),
        email=headers.get("x-truline-email", ""),
    )


def _has_any_identity_header(headers: Any) -> bool:
    return any(headers.get(name) for name in _IDENTITY_HEADER_NAMES)


_CF_ASSERTION_HEADER = "cf-access-jwt-assertion"
_CF_ASSERTION_COOKIE = "CF_Authorization"


def access_token_from_headers(headers: Any, cookies: Any) -> Optional[str]:
    """Read the Cloudflare Access assertion the way ForwardAuth's own route
    always has: header first, then the CF_Authorization cookie. The single
    helper both the /auth/cloudflare route and the resolver call, so what
    each one reads can never drift apart.
    """
    token = headers.get(_CF_ASSERTION_HEADER)
    if token:
        return token
    return cookies.get(_CF_ASSERTION_COOKIE)


# --- The probe's process-local loopback credential (AC-7) ------------------
#
# Generated once per process, never read from the environment and never
# written anywhere -- a token from a different process (a second worker, a
# restarted pod) is simply a different value, which is exactly the point:
# it is valid only within the process that generated it.
PROCESS_INTERNAL_CALL_TOKEN = secrets.token_urlsafe(32)


# --- Resolver-only JWKS caching (AC-6) --------------------------------------
#
# Everything below is invisible to fetch_access_jwks and verify_access_jwt
# themselves (both behave exactly as before this bead), and therefore to
# /auth/cloudflare -- Traefik's ForwardAuth for every public hostname. A
# negative cache in the shared fetch would turn one transient JWKS failure
# at a 300-second refresh into 30 seconds of 403 on every public site; this
# design keeps that cache local to the path that newly calls the verifier on
# every request (the resolver), not the path that already did (ForwardAuth).

_RESOLVER_FETCH_FAILURE_TTL_SECONDS = 30.0
_RESOLVER_KID_MISS_RATE_SECONDS = 60.0

_resolver_fetch_failure_until = 0.0
_resolver_kid_miss_state: Dict[str, float] = {"last_refetch": 0.0}
_resolver_fetch_lock = asyncio.Lock()
_resolver_inflight: Optional["asyncio.Future"] = None


def _reset_resolver_state_for_tests() -> None:
    """Test-only. Clears every piece of resolver-path state (the negative
    cache, the kid-miss rate limit, the single-flight future, and the
    shared JWKS cache fetch_access_jwks also reads) so cases don't leak
    into each other. Never called from production code."""
    global _resolver_fetch_failure_until, _resolver_inflight
    _resolver_fetch_failure_until = 0.0
    _resolver_inflight = None
    _resolver_kid_miss_state["last_refetch"] = 0.0
    _JWKS_CACHE["keys"] = []
    _JWKS_CACHE["expires_at"] = 0.0


async def _resolver_fetch_jwks(team_domain: str) -> Dict[str, Any]:
    global _resolver_fetch_failure_until, _resolver_inflight

    now = time.time()
    if _resolver_fetch_failure_until > now:
        raise VerifierUnavailableError(
            "Cloudflare Access JWKS fetch is in cooldown after a recent failure"
        )

    async with _resolver_fetch_lock:
        now = time.time()
        if _resolver_fetch_failure_until > now:
            raise VerifierUnavailableError(
                "Cloudflare Access JWKS fetch is in cooldown after a recent failure"
            )
        # Single-flight: concurrent callers reuse one in-flight fetch rather
        # than each dialing out, since verification now runs on every
        # assertion-bearing request instead of only on ForwardAuth's own.
        if _resolver_inflight is None or _resolver_inflight.done():
            _resolver_inflight = asyncio.ensure_future(fetch_access_jwks(team_domain))
        inflight = _resolver_inflight

    try:
        # asyncio.shield, not a bare `await inflight`: `inflight` is a Future
        # shared by every coalesced caller. Awaiting it directly lets a
        # cancelled caller's Task.cancel() cancel `inflight` itself (asyncio
        # cancels whatever a task's `_fut_waiter` currently is), which would
        # cancel the JWKS fetch out from under every OTHER concurrent caller
        # still waiting on it. shield() gives each awaiter its own wrapper
        # future, so cancelling one caller never reaches the shared fetch.
        return await asyncio.shield(inflight)
    except Exception as exc:
        _resolver_fetch_failure_until = time.time() + _RESOLVER_FETCH_FAILURE_TTL_SECONDS
        raise VerifierUnavailableError("Cloudflare Access JWKS fetch failed") from exc


async def _resolver_force_fetch_jwks(team_domain: str) -> Dict[str, Any]:
    """The kid-miss refetch's own entry point (AC-6 item 2): bypasses
    fetch_access_jwks's cache via force=True, outside the single-flight
    machinery above (the kid-miss rate limit already caps this to one call
    per 60s, so no separate coalescing is needed here). Like
    fetch_access_jwks itself, this writes nothing on failure -- a failed
    refetch never touches _JWKS_CACHE, so it can never invalidate what
    ForwardAuth already has cached (probe P3).
    """
    global _resolver_fetch_failure_until
    try:
        return await fetch_access_jwks(team_domain, force=True)
    except Exception as exc:
        _resolver_fetch_failure_until = time.time() + _RESOLVER_FETCH_FAILURE_TTL_SECONDS
        raise VerifierUnavailableError("Cloudflare Access JWKS refetch failed") from exc


async def _resolver_verify_access_jwt(
    token: str,
    *,
    team_domain: str,
    accepted_audiences: str = "",
) -> Dict[str, Any]:
    """The resolver's default verifier: verify_access_jwt's own validation
    steps (reused via _key_for_token/validate_access_claims), fetching
    through _resolver_fetch_jwks instead of fetch_access_jwks directly so
    the negative cache and kid-miss refetch above apply -- see AC-6.
    """
    jwks = await _resolver_fetch_jwks(team_domain)
    try:
        key = _key_for_token(token, jwks)
    except _KidNotFoundError:
        now = time.time()
        if now - _resolver_kid_miss_state["last_refetch"] < _RESOLVER_KID_MISS_RATE_SECONDS:
            raise
        _resolver_kid_miss_state["last_refetch"] = now
        # Force a real refetch despite fetch_access_jwks's own 300s cache --
        # this absorbs Cloudflare key rotation, which would otherwise 401
        # this path for up to that long while ForwardAuth's own cache
        # (unaffected by this -- see _resolver_force_fetch_jwks) keeps
        # succeeding. A malformed or kid-less assertion raises the plain
        # AccessAuthError above, not _KidNotFoundError, so it never reaches
        # here and never triggers a refetch.
        jwks = await _resolver_force_fetch_jwks(team_domain)
        key = _key_for_token(token, jwks)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=access_issuer(team_domain),
            options={"verify_aud": False},
        )
    except Exception as exc:
        raise AccessAuthError("Cloudflare Access JWT signature or claims are invalid") from exc

    validate_access_claims(claims, accepted_audiences)
    return claims


# --- The resolver (AC-2/AC-3/AC-11) -----------------------------------------

VERDICT_VERIFIED = "verified"
VERDICT_MISMATCH = "mismatch"
VERDICT_INVALID_ASSERTION = "invalid-assertion"
VERDICT_VERIFIER_UNAVAILABLE = "verifier-unavailable"
VERDICT_UNVERIFIED_HEADERS = "unverified-headers"
VERDICT_NO_IDENTITY = "no-identity"
VERDICT_INTERNAL_CALL = "internal-call"
VERDICT_INTERNAL_CALL_REJECTED = "internal-call-rejected"

VERDICTS = frozenset(
    {
        VERDICT_VERIFIED,
        VERDICT_MISMATCH,
        VERDICT_INVALID_ASSERTION,
        VERDICT_VERIFIER_UNAVAILABLE,
        VERDICT_UNVERIFIED_HEADERS,
        VERDICT_NO_IDENTITY,
        VERDICT_INTERNAL_CALL,
        VERDICT_INTERNAL_CALL_REJECTED,
    }
)


@dataclass(frozen=True)
class ResolvedIdentity:
    identity: AccessIdentity
    verdict: str
    rejection: Optional[Tuple[int, str]] = None


_REJECTION_DETAIL = {
    VERDICT_UNVERIFIED_HEADERS: "identity headers were present without a verified Cloudflare Access assertion",
    VERDICT_INVALID_ASSERTION: "the Cloudflare Access assertion could not be verified",
    VERDICT_VERIFIER_UNAVAILABLE: "identity verification is temporarily unavailable",
    VERDICT_INTERNAL_CALL_REJECTED: "the internal-call credential was invalid for this request",
}

_REJECTION_STATUS = {
    VERDICT_UNVERIFIED_HEADERS: 401,
    VERDICT_INVALID_ASSERTION: 401,
    VERDICT_VERIFIER_UNAVAILABLE: 503,
    VERDICT_INTERNAL_CALL_REJECTED: 401,
}


def rejection_for(verdict: str) -> Tuple[int, str]:
    # Names the verdict, never a header or assertion value (AC-3/AC-5).
    # Public (not _-prefixed): openapi_app's middleware also calls this, as
    # defense-in-depth, when identity resolution itself raises unexpectedly.
    return _REJECTION_STATUS[verdict], f"identity_verdict={verdict}: {_REJECTION_DETAIL[verdict]}"


async def resolve_request_identity(
    *,
    headers: Any,
    cookies: Any,
    client_host: Optional[str],
    mode: str,
    verifier: Optional[Callable[..., Any]] = None,
) -> ResolvedIdentity:
    """The one place a request's caller identity is decided.

    ``headers`` and ``cookies`` need only support ``.get`` (a Starlette
    Headers/cookie mapping in production; a plain dict, or a
    case-insensitive Headers built from one, in tests). ``verifier``
    defaults to the resolver's own cached/rate-limited verifier (AC-6),
    itself built from verify_access_jwt's own validation steps; inject a
    fake to avoid the network entirely in tests.
    """
    header_identity = identity_from_headers(headers)

    # The probe's own loopback call (AC-7) -- checked first and
    # independently of A/H, in every mode, because it is the one caller that
    # legitimately sends X-Truline-* headers with no Cloudflare assertion
    # behind them.
    internal_call_header = headers.get("x-truline-internal-call")
    if internal_call_header is not None:
        # Compare as bytes, not str: hmac.compare_digest raises TypeError on
        # a non-ASCII str (an unauthenticated header value an adversary
        # controls), and bytes comparison has no such restriction.
        if client_host in ("127.0.0.1", "::1") and hmac.compare_digest(
            internal_call_header.encode("utf-8", errors="surrogateescape"),
            PROCESS_INTERNAL_CALL_TOKEN.encode("utf-8"),
        ):
            return ResolvedIdentity(identity=header_identity, verdict=VERDICT_INTERNAL_CALL)
        if mode == MODE_ENFORCE:
            return ResolvedIdentity(
                identity=header_identity,
                verdict=VERDICT_INTERNAL_CALL_REJECTED,
                rejection=rejection_for(VERDICT_INTERNAL_CALL_REJECTED),
            )
        return ResolvedIdentity(identity=header_identity, verdict=VERDICT_INTERNAL_CALL_REJECTED)

    has_headers = _has_any_identity_header(headers)

    if mode == MODE_TRUST_HEADERS:
        # Never calls the verifier -- today's pre-change behaviour, kept as
        # a rollback lever that needs no redeploy.
        verdict = VERDICT_UNVERIFIED_HEADERS if has_headers else VERDICT_NO_IDENTITY
        return ResolvedIdentity(identity=header_identity, verdict=verdict)

    assertion = access_token_from_headers(headers, cookies)

    if not assertion:
        if has_headers:
            # A request that claims an identity and cannot prove it is
            # either forged or a broken hop -- both loud, never a silent
            # downgrade.
            if mode == MODE_ENFORCE:
                return ResolvedIdentity(
                    identity=header_identity,
                    verdict=VERDICT_UNVERIFIED_HEADERS,
                    rejection=rejection_for(VERDICT_UNVERIFIED_HEADERS),
                )
            return ResolvedIdentity(identity=header_identity, verdict=VERDICT_UNVERIFIED_HEADERS)
        return ResolvedIdentity(identity=header_identity, verdict=VERDICT_NO_IDENTITY)

    team_domain = os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "truline")
    accepted_audiences_raw = os.environ.get("CLOUDFLARE_ACCESS_AUDS", "")
    verify = verifier if verifier is not None else _resolver_verify_access_jwt

    try:
        claims = await verify(
            assertion, team_domain=team_domain, accepted_audiences=accepted_audiences_raw
        )
    except VerifierUnavailableError:
        if mode == MODE_ENFORCE:
            return ResolvedIdentity(
                identity=header_identity,
                verdict=VERDICT_VERIFIER_UNAVAILABLE,
                rejection=rejection_for(VERDICT_VERIFIER_UNAVAILABLE),
            )
        return ResolvedIdentity(identity=header_identity, verdict=VERDICT_VERIFIER_UNAVAILABLE)
    except AccessAuthError:
        if mode == MODE_ENFORCE:
            return ResolvedIdentity(
                identity=header_identity,
                verdict=VERDICT_INVALID_ASSERTION,
                rejection=rejection_for(VERDICT_INVALID_ASSERTION),
            )
        return ResolvedIdentity(identity=header_identity, verdict=VERDICT_INVALID_ASSERTION)
    except Exception:
        # An unanticipated exception from the injected/default verifier --
        # not one of the two exception types this module defines itself.
        # AC-6: log-only must never degrade a request over this, so it's
        # folded into the same verifier-unavailable handling as an actual
        # JWKS outage, logged here (cause attached, PRIN-008) since this
        # exception is otherwise swallowed rather than propagated.
        logger.exception(
            "identity resolver: the verifier raised an unexpected exception; "
            "treating this request as %s",
            VERDICT_VERIFIER_UNAVAILABLE,
        )
        if mode == MODE_ENFORCE:
            return ResolvedIdentity(
                identity=header_identity,
                verdict=VERDICT_VERIFIER_UNAVAILABLE,
                rejection=rejection_for(VERDICT_VERIFIER_UNAVAILABLE),
            )
        return ResolvedIdentity(identity=header_identity, verdict=VERDICT_VERIFIER_UNAVAILABLE)

    if mode == MODE_ENFORCE and not _accepted_audiences(accepted_audiences_raw):
        # Fail closed: validate_access_claims itself accepts any audience
        # when the configured list is empty (today's behaviour, unchanged),
        # but enforce mode must not admit an assertion verified against no
        # configured audience at all.
        return ResolvedIdentity(
            identity=header_identity,
            verdict=VERDICT_INVALID_ASSERTION,
            rejection=rejection_for(VERDICT_INVALID_ASSERTION),
        )

    implied = identity_from_claims(
        claims,
        fallback_email=headers.get("cf-access-authenticated-user-email"),
        service_token_names=parse_mapping(os.environ.get("CLOUDFLARE_ACCESS_SERVICE_TOKEN_NAMES", "")),
        service_token_scopes=parse_mapping(os.environ.get("CLOUDFLARE_ACCESS_CLIENT_SCOPES", "")),
    )
    implied_headers = identity_headers(implied)
    implied_identity = AccessIdentity(
        client=implied_headers.get("X-Truline-Client", ""),
        client_type=implied_headers.get("X-Truline-Client-Type", ""),
        scopes=implied_headers.get("X-Truline-Scopes", ""),
        email=implied_headers.get("X-Truline-Email", ""),
    )

    matches = (
        (headers.get("x-truline-client") or "") == implied_identity.client
        and (headers.get("x-truline-client-type") or "") == implied_identity.client_type
        and (headers.get("x-truline-scopes") or "") == implied_identity.scopes
        and (headers.get("x-truline-email") or "") == implied_identity.email
    )
    verdict = VERDICT_VERIFIED if matches else VERDICT_MISMATCH
    effective_identity = implied_identity if mode == MODE_ENFORCE else header_identity
    return ResolvedIdentity(identity=effective_identity, verdict=verdict)


current_client_identity = contextvars.ContextVar[Optional[AccessIdentity]](
    "current_client_identity", default=None
)


# Amendment 23 / R26.09 O-6: the gateway's audit record must carry the scope
# a route required, not just the scopes the caller was granted -- but
# check_scope/require_authenticated_scope run inside the route handler,
# which starlette.middleware.base.BaseHTTPMiddleware executes in a separate
# task from the middleware that logs the request. A plain ContextVar.set()
# there would not be visible to the middleware after call_next returns (a
# child task's context mutations don't propagate to the parent). Holding a
# *mutable container* in the contextvar sidesteps that: the middleware sets
# a dict into this var before call_next, and mutating that same dict object
# from inside the child task is visible everywhere it's referenced,
# including back in the middleware. See openapi_app._identity_logging_dispatch.
current_checked_scope = contextvars.ContextVar[Optional[Dict[str, Optional[str]]]](
    "current_checked_scope", default=None
)


# The identity_verdict resolve_request_identity computed for this request,
# for check_scope to read (AC-11). Unlike current_checked_scope above, this
# flows middleware -> route (a parent ContextVar.set() before call_next IS
# visible to the child task call_next spawns -- context propagates forward
# into a child task; only a child's own mutations fail to propagate back),
# so a plain ContextVar is enough here, no mutable-container trick needed.
current_identity_verdict = contextvars.ContextVar[Optional[str]](
    "current_identity_verdict", default=None
)


def _record_checked_scope(required_scope: str) -> None:
    """Record the scope a route required, for the caller's audit record.

    Called unconditionally, before any allow/deny branch: the audit record
    is about what the route required, independent of whether the call was
    admitted.
    """
    record = current_checked_scope.get()
    if record is not None:
        record["scope"] = required_scope


# The full vocabulary of scope literals passed to check_scope/require_authenticated_scope
# calls under apps/mcp-hub/src today. tests/test_scope_registry.py greps the source for
# every such call and asserts its argument is a member of this set -- so a new call site
# with a typo'd or undeclared scope fails the suite instead of silently checking nothing.
#
# This set is the checked side only. It is not the same as what infrastructure/terraform/
# access.tf grants -- that file is read-only from this app's perspective, and the same test
# asserts every scope it grants is a member here too. Where the two sides disagree today
# (a granted scope nothing checks, or a checked scope nothing grants) is recorded in the
# test and in this task's notes, not resolved here: resolving it is a scope-grant decision
# (D4, 2026-09-12 review) that belongs to the Operator, not to this structural change.
SCOPES = frozenset(
    {
        "automations.trigger",
        "calendar.read",
        "context.read",
        "factory.merge",
        "factory.read",
        "factory.write",
        "finance.read",
        "finance.write",
        "ha.read",
        "homelab.read",
        "substrate.proxy",
        "vision.write",
    }
)


# R26.09/O-2: a scope that the human wildcard ('*', granted to every
# authenticated person by identity_headers above) must NOT satisfy. Every
# other scope in SCOPES is reachable by '*' -- that is what makes
# "factory.write ships dark" mean "dark to service tokens" while a person's
# own browser session still reaches it. "factory.merge ships dark" has to
# mean dark to the Operator too, or the capability is one click away from anyone
# already inside Cloudflare Access. See .factory/design.md.
EXACT_MATCH_SCOPES = frozenset({"factory.merge"})


def check_scope(required_scope: str) -> None:
    """Authorize access based on the current request context's scopes.

    Bypassed in stdio/internal mode (no active headers/AccessJWT)
    to support local development and existing test suites seamlessly.
    """
    _record_checked_scope(required_scope)
    identity = current_client_identity.get()
    if identity is None:
        return
    if identity.client == "internal/unknown" and identity.client_type == "internal":
        # The bypass a genuine stdio/direct call relies on (no request
        # context at all -- identity is None, handled above) stays
        # unconditional. This is the OTHER thing that collapses to the same
        # identity: an HTTP request whose resolver verdict is `no-identity`.
        # In enforce mode that request proved nothing, so this route (one
        # that calls check_scope, not require_authenticated_scope) must
        # refuse it -- dev.finding 4208f26f, the no-header bypass. log-only
        # and trust-headers keep today's outcome unconditionally.
        if (
            current_identity_verdict.get() == VERDICT_NO_IDENTITY
            and current_identity_mode() == MODE_ENFORCE
        ):
            raise HTTPException(
                status_code=401,
                detail=(
                    f"'{required_scope}' requires a verified caller identity; "
                    "none was established for this request (identity_verdict=no-identity)"
                ),
            )
        return

    scopes_list = [s.strip() for s in identity.scopes.split(",") if s.strip()]
    if required_scope not in EXACT_MATCH_SCOPES and ("*" in scopes_list or "admin" in scopes_list):
        return

    if required_scope not in scopes_list:
        raise HTTPException(
            status_code=403,
            detail=f"Client '{identity.client}' lacks required scope '{required_scope}'",
        )


def require_authenticated_scope(required_scope: str) -> None:
    """Like :func:`check_scope`, but never grants the internal/unknown bypass.

    ``check_scope`` treats two situations as trusted: no ASGI request context
    at all (a genuine stdio/direct call, where ``current_client_identity`` is
    still its ``None`` default), and an HTTP request whose Cloudflare Access
    headers are absent, which ``_identity_logging_dispatch`` defaults to the
    same ``internal/unknown`` identity a stdio call would have. Over the
    network those two are indistinguishable from inside this process, but
    only the first is actually local — the second is an HTTP request that
    reached mcp-hub without passing through Traefik's ForwardAuth, i.e.
    without the authentication the console itself requires.

    Routes that must be reachable by exactly the identities that can reach
    the console — and by nobody who reaches mcp-hub's HTTP surface directly —
    call this instead of ``check_scope``. It leaves the ``None`` case
    untouched (stdio/local-dev calls keep working) and refuses the
    HTTP-with-no-identity case that ``check_scope`` would otherwise bypass.
    """
    _record_checked_scope(required_scope)
    identity = current_client_identity.get()
    if identity is None:
        # Fail closed. Every HTTP request passes _identity_logging_dispatch,
        # which always sets an identity — so None here means the middleware
        # did not run (mis-mounted route, context propagation fault), not a
        # local stdio call: nothing outside the HTTP routers calls this
        # function. Release-gate finding on #620.
        raise HTTPException(
            status_code=401,
            detail=(
                f"'{required_scope}' requires the same authentication the console uses; "
                "no identity context was established for this request"
            ),
        )
    if identity.client == "internal/unknown" and identity.client_type == "internal":
        raise HTTPException(
            status_code=401,
            detail=(
                f"'{required_scope}' requires the same authentication the console uses; "
                "this request carried no Truline identity"
            ),
        )
    check_scope(required_scope)
