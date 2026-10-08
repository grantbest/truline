#!/usr/bin/env python3
"""Substrate HTTP client shared by every script that reaches the store.

ea-load.py, release-load.py and requirements-load.py each carried their own
urllib client for the same handful of lines: open a request, attach
`X-API-Key`, decode JSON, raise `SubstrateError` on a non-2xx. Three copies of
"how to reach the store" drift independently, and the one nobody is looking at
is the one that drifts. `get()` folds in the read-only path gate-prepass.py,
release-manifest.py, release-status.py, release-notes.py and principles_sync.py
used — `gate_substrate.fetch_json` was a fourth, GET-only client for the same
store, kept in step with this one by hand. One client, reads and writes both.

`validate()`/`find_schema_violations()` fold in the content/provenance schemas
themselves (apps/substrate/src/schemas.py), imported rather than copied — the
same pattern scripts/tests/test_arch_source_class_backfill.py already uses for
a fake substrate double. A caller can now ask "would this write be accepted"
before attempting it, and can re-check an already-stored bead against the live
schema without a second, drifting transcription of it.

Environment: SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, NamedTuple, Optional, Protocol

_SUBSTRATE_SRC = pathlib.Path(__file__).resolve().parents[1] / "apps" / "substrate" / "src"
if str(_SUBSTRATE_SRC) not in sys.path:
    sys.path.insert(0, str(_SUBSTRATE_SRC))
from schemas import BeadProvenance, validate_bead_content, validate_candidate_bead  # noqa: E402
from pydantic import ValidationError  # noqa: E402

NAMESPACE = "arch"
TRUST_TIER = "user"
URL_ENV = "SUBSTRATE_URL"
KEY_ENV = "SUBSTRATE_API_KEY"

#: Sentinel distinguishing "caller omitted base_url/key" (env-backed, fails
#: fast at construction — the write-side loaders' contract) from "caller
#: passed an explicit value, even a falsy one" (fails lazily at request time —
#: the read-only gate scripts' contract, ported from gate_substrate.fetch_json).
_UNSET: Any = object()


class SubstrateError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        # The response body rides in the message (bounded) as well as on
        # .body: the old gate_substrate.fetch_json printed it, and a merge
        # gate diagnosing a 401 or a validation refusal needs the server's
        # words, not just the status (release-gate advisory on the
        # consolidating PR).
        suffix = f": {body[:400]}" if body.strip() else ""
        super().__init__(f"substrate {status} on {path}{suffix}")
        self.status = status
        self.body = body
        self.path = path


class SchemaViolation(NamedTuple):
    """One already-stored bead the live schema would now reject.

    ``errors`` is ``str(pydantic.ValidationError)`` — kept as rendered text,
    not the exception object, so a caller can log or assert on it without
    importing pydantic itself.
    """

    bead_id: str
    errors: str


def find_schema_violations(beads: list[dict]) -> list[SchemaViolation]:
    """Re-validate already-stored beads against the live content/provenance
    schemas, without writing anything.

    Feed it whatever ``Substrate.list_beads``/``.get`` already returned. A
    store that accepted a bead its own declared schema would now reject used
    to be invisible until some downstream reader choked on the shape — this
    makes it a condition a script can assert on instead, using the exact
    ``validate_bead_content``/``BeadProvenance`` classes ``POST /beads``
    checks against (apps/substrate/src/schemas.py), not a re-description of
    them.
    """
    violations = []
    for bead in beads:
        try:
            validate_bead_content(bead["namespace"], bead["type"], bead.get("content") or {})
            provenance = bead.get("provenance") or {}
            if provenance:
                BeadProvenance.model_validate(provenance)
        except ValidationError as exc:
            violations.append(SchemaViolation(bead_id=str(bead.get("id")), errors=str(exc)))
    return violations


class SubstrateClient(Protocol):
    """The calls release-load.py and requirements-load.py reconcile against.

    `Substrate` below implements a wider surface too — `links`, `add_link`,
    `delete_link`, `delete_bead`, and a `parent_id`/`created_by`-aware
    `create`/`patch` — that only ea-load.py's edge reconcile needs.
    """

    def list_beads(self, bead_type: str, limit: int = 1000, offset: int = 0) -> list[dict]:
        ...

    def create(self, bead_type: str, state: str, content: dict) -> dict:
        ...

    def patch(self, bead_id: str, body: dict) -> dict:
        ...


class Substrate:
    """Talks to the substrate over HTTP with `X-API-Key` auth.

    `created_by` is fixed per instance — each loader tags its own writes
    (`release-load`, `ea-load`, `requirements-load`) — but any single `create`
    or `patch` call may override it.
    """

    def __init__(
        self,
        *,
        created_by: str = "",
        namespace: str = NAMESPACE,
        trust_tier: str = TRUST_TIER,
        base_url: Any = _UNSET,
        key: Any = _UNSET,
    ) -> None:
        # Omitting base_url/key (the write-side loaders' call shape) reads
        # the environment and fails fast, at construction, exactly as before.
        # Passing them explicitly (the read-only gate scripts' call shape)
        # defers a missing value to the first request instead, since those
        # callers construct a client before knowing whether a call will even
        # be attempted.
        explicit = base_url is not _UNSET or key is not _UNSET
        resolved_base = (os.environ.get(URL_ENV) or "") if base_url is _UNSET else (base_url or "")
        resolved_key = (os.environ.get(KEY_ENV) or "") if key is _UNSET else (key or "")
        if not explicit and (not resolved_base or not resolved_key):
            sys.exit(f"{URL_ENV} and {KEY_ENV} must be set")
        self.base = resolved_base.rstrip("/")
        self.key = resolved_key
        self.created_by = created_by
        self.namespace = namespace
        self.trust_tier = trust_tier

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        if not self.base or not self.key:
            raise RuntimeError(f"{URL_ENV} and {KEY_ENV} must be set")
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self.key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise SubstrateError(
                exc.code, exc.read().decode(errors="ignore"), path
            ) from None


    def get(self, path: str) -> Any:
        """GET an arbitrary path and decode the JSON body.

        The one way any caller reaches the store for a read it does not have
        a named method for — gate-prepass.py, release-manifest.py,
        release-notes.py, release-status.py and principles_sync.py all read
        through this rather than each carrying its own urllib client.
        """
        return self._request("GET", path)

    def find_bead(self, namespace: str, type: str, content_ref: str) -> Optional[dict]:
        """The one bead in ``namespace``/``type`` whose ``content["ref"]`` equals
        ``content_ref``, or None -- the store's own indexed lookup
        (``GET /beads?...content_ref=...&limit=1``), modelled on
        ``apps/substrate/client/src/substrate_client/client.py``'s ``find_bead``.

        For a bead type whose population grows without bound (dated per-night
        mints, newest first) and a caller that only ever wants one standing
        record by its unique ``content.ref``, this is the query that answers
        the question directly -- unlike ``list_beads``, whose fixed page size
        a large enough population outgrows regardless of how it is ordered.
        """
        query = urllib.parse.urlencode(
            {"namespace": namespace, "type": type, "content_ref": content_ref, "limit": 1}
        )
        results = self._request("GET", f"/beads?{query}") or []
        return results[0] if results else None

    def list_beads(self, bead_type: str, limit: int = 1000, offset: int = 0) -> list[dict]:
        """One page. `limit` is a page size, not a cap -- a caller that wants
        the whole population of a bead type must walk `offset` itself (as
        `scripts/ea-load.py`'s `_list_all` does); this method fetches exactly
        one page, the way it always has, so callers that only ever wanted
        page one (release-load.py, requirements-load.py) see no behaviour
        change from adding `offset`, which defaults to the same query this
        sent before it existed.
        """
        params: dict[str, Any] = {"namespace": self.namespace, "type": bead_type, "limit": limit}
        if offset:
            params["offset"] = offset
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"/beads?{query}") or []

    def validate(
        self,
        bead_type: str,
        content: dict,
        *,
        state: str = "pending",
        provenance: Optional[dict] = None,
        created_by: Optional[str] = None,
    ) -> None:
        """Raise ``pydantic.ValidationError`` when ``create(bead_type, ...)``
        with this content/provenance would be rejected by the store's schema
        validation — without sending it. Validates against this instance's
        ``namespace``, ``trust_tier`` and ``created_by`` (or an explicit
        override), the same values ``create()`` would attribute the write to.

        This is the schema half only, deliberately not an iff: the route also
        runs an entry-state check pre-DB (``_validate_entry_state_or_422``),
        so a legal-shaped candidate with a non-entry state passes here and
        still 422s at the store. The store stays authoritative; this refuses
        earlier where it can (release-gate advisory on the PR that landed
        this).
        """
        validate_candidate_bead(
            self.namespace,
            bead_type,
            content,
            provenance or {},
            created_by if created_by is not None else self.created_by,
            trust_tier=self.trust_tier,
            state=state,
        )

    def create(
        self,
        bead_type: str,
        state: str,
        content: dict,
        parent_id: Optional[str] = None,
        *,
        created_by: Optional[str] = None,
    ) -> dict:
        return self._request(
            "POST",
            "/beads",
            {
                "namespace": self.namespace,
                "type": bead_type,
                "state": state,
                "content": content,
                "trust_tier": self.trust_tier,
                "created_by": self._attributed(created_by),
                **({"parent_id": parent_id} if parent_id else {}),
            },
        )

    def patch(self, bead_id: str, body: dict, *, created_by: Optional[str] = None) -> dict:
        return self._request(
            "PATCH",
            f"/beads/{bead_id}",
            {**body, "created_by": self._attributed(created_by)},
        )

    def _attributed(self, override: Optional[str]) -> str:
        """No write leaves this client unattributed: a log that misattributes
        work is worse than a log with a gap, but a log with a silent '' author
        is both (release-gate finding on the consolidating PR — created_by
        had loosened from required to a '' default)."""
        who = override if override is not None else self.created_by
        if not who:
            raise RuntimeError(
                "refusing an unattributed write: pass created_by to the "
                "client constructor or to this call"
            )
        return who

    def links(self, bead_id: str, direction: str = "outgoing") -> list[dict]:
        return self._request("GET", f"/beads/{bead_id}/links?direction={direction}") or []

    def add_link(self, source_id: str, target_id: str, link_type: str) -> dict:
        """POST /beads/{source_id}/links. Unlike every other write here, this
        route does not read ``created_by`` from the JSON body — ``BeadLinkCreate``
        (apps/substrate/src/schemas.py) declares no such field, and a request
        carrying it there is silently accepted with the value dropped
        (pydantic's default ``extra="ignore"``), not rejected. The route reads
        attribution from the ``X-Created-By`` header instead
        (apps/substrate/src/routes.py), so that is where it travels here."""
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            {"target_id": target_id, "link_type": link_type},
            headers={"X-Created-By": self._attributed(None)},
        )

    def delete_link(self, link_id: str) -> None:
        self._request("DELETE", f"/links/{link_id}")

    def delete_bead(self, bead_id: str) -> None:
        self._request("DELETE", f"/beads/{bead_id}")
class SubstrateReader:
    """A get-only view of the store for the merge-gate read path (R26.06,
    bead a3e235d5): read-only by construction, not by docstring. The object
    a gate script holds has no create/patch/add_link/delete attribute to
    call — the property gate_substrate.fetch_json used to provide by having
    no write path at all, restored here as a facade after the release gate
    on the consolidating PR found it had regressed to prose.
    """

    def __init__(self, client: "Substrate") -> None:
        self._client = client

    def get(self, path: str) -> Any:
        return self._client.get(path)


def reader(*, base_url: Any = _UNSET, key: Any = _UNSET) -> SubstrateReader:
    """The read-only gate scripts' front door: same lazy-fail construction
    shape as passing base_url/key to Substrate, but the returned object can
    only read."""
    return SubstrateReader(Substrate(base_url=base_url, key=key))
