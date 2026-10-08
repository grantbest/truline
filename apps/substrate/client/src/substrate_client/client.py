#!/usr/bin/env python3
"""The substrate's BeadStore client.

Ported from ``apps/factory-dispatcher/substrate.py`` (the widest-used of the
eight bead-store client implementations the 2026-09-12 architecture review
found -- see ``docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md``
§4). Request-identical to that module: same headers, same paths, same paging
behaviour -- the recorder proof in ``tests/test_recorder_parity.py`` is what
establishes that, not this docstring.

Fourteen of its methods are exactly the ``BeadStore`` protocol's surface
(``apps/factory-dispatcher/beadstore.py``) -- :meth:`Substrate.create_bead`
and :meth:`Substrate.patch_context` joined the protocol itself (OPS-190), so
they are no longer counted separately from it. One more, :meth:`Substrate.search`,
exists because the protocol has no generic search, so every consumer outside
the dev.task lane (mcp-hub's finance/incident/cost beads, none of them
dev/task/pending) was building its own HTTP calls instead of widening this
one client -- see
``docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md``
§4/§6 and the finding this bead closes. ``create_task`` stays narrow on
purpose: it is the only method here that may hardcode namespace/type/state,
because ``pending`` is the only entry state the dev.task machine declares.
Fifteen methods total, pinned in ``tests/test_public_surface.py`` so the
next addition is a visible diff, not drift.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

HTTP_TIMEOUT_S = 30.0
# Bound on a paged walk. 500 pages at the default 200 covers 100k beads of one
# type -- ~300x the live dev.task population -- so reaching it means the server
# stopped honouring offset, not that the estate grew. Loud, per PRIN-015.
MAX_PAGES = 500

DEV_NAMESPACE = "dev"


class SubstrateError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"substrate {status}: {body[:400]}")
        self.status = status
        self.body = body


class Substrate:
    """The dispatcher's view of the store, over HTTP with ``X-API-Key`` auth."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ["SUBSTRATE_URL"]).rstrip("/")
        key = api_key or os.environ.get("SUBSTRATE_API_KEY")
        if not key:
            raise RuntimeError("SUBSTRATE_API_KEY is not set")
        self._headers = {"X-API-Key": key, "Content-Type": "application/json"}

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        url = f"{self.base_url}{path}"
        merged_headers = {**self._headers, **(headers or {})}
        resp = httpx.request(
            method, url, headers=merged_headers, timeout=HTTP_TIMEOUT_S, **kwargs
        )
        if resp.status_code >= 300:
            raise SubstrateError(resp.status_code, resp.text)
        return resp.json()

    # -- reads ---------------------------------------------------------------

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict | None:
        """The one bead in ``namespace``/``type`` whose ``content["ref"]`` equals
        ``content_ref`` (e.g. a ``PRIN-NNN`` principle id), or None."""
        results = self._request(
            "GET",
            "/beads",
            params={
                "namespace": namespace,
                "type": type,
                "content_ref": content_ref,
                "limit": 1,
            },
        )
        return results[0] if results else None

    def list_beads(
        self, namespace: str, type: str, state: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Every bead in ``namespace``/``type``, optionally filtered to one state.

        ``limit`` is a page size, not a cap: this keeps asking for the next
        page, by ``offset``, until the server returns fewer rows than
        requested, and only then returns the union of every page fetched. The
        walk is bounded at ``MAX_PAGES`` -- see ``SubstrateError`` below --
        so a server that ignores ``offset`` fails loudly rather than looping
        or silently truncating.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive to page a population; got {limit!r}")
        params: dict[str, Any] = {"namespace": namespace, "type": type, "limit": limit}
        if state:
            params["state"] = state
        results: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if offset:
                page_params["offset"] = offset
            page = self._request("GET", "/beads", params=page_params)
            results.extend(page)
            if len(page) < limit:
                return results
            offset += limit
        raise SubstrateError(
            0,
            f"paging {namespace}.{type} did not exhaust after {MAX_PAGES} pages of "
            f"{limit} ({len(results)} rows so far). Either the population is larger "
            "than this walk is designed for, or the server is not honouring offset. "
            "Refusing to return a population that may be silently incomplete.",
        )

    def list_tasks(self, state: str | None = None, limit: int = 200) -> list[dict]:
        return self.list_beads(DEV_NAMESPACE, "task", state=state, limit=limit)

    def list_notes(self, parent_id: str, limit: int = 500) -> list[dict]:
        return self._request(
            "GET",
            "/beads",
            params={
                "namespace": DEV_NAMESPACE,
                "type": "note",
                "parent_id": parent_id,
                "limit": limit,
            },
        )

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict]:
        params: dict[str, Any] = {"direction": direction}
        if link_type is not None:
            params["link_type"] = link_type
        return self._request("GET", f"/beads/{bead_id}/links", params=params)

    def list_events(self, bead_id: str) -> list[dict]:
        """Every event recorded against ``bead_id``, as the substrate returns
        them -- no ``limit``, the route takes none."""
        return self._request("GET", f"/beads/{bead_id}/events")

    # -- writes --------------------------------------------------------------

    def create_task(self, content: dict, created_by: str, *, trust_tier: str = "user") -> dict:
        """File a new dev.task bead -- the intake path.

        Always dev/task/pending: pending is the only entry state
        STATE_MACHINE_ENTRY_STATES declares for dev.task, so there is nothing
        else a caller could legally ask for here.
        """
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": DEV_NAMESPACE,
                "type": "task",
                "state": "pending",
                "trust_tier": trust_tier,
                "created_by": created_by,
                "content": content,
            },
        )

    def set_state(self, bead_id: str, state: str, created_by: str) -> dict:
        return self._request(
            "PATCH", f"/beads/{bead_id}", json={"state": state, "created_by": created_by}
        )

    def transition_state(
        self, bead_id: str, from_state: str, to_state: str, created_by: str
    ) -> dict:
        """Atomic compare-and-set state transition.

        Raises SubstrateError(409) if the bead is not currently in from_state.
        """
        return self._request(
            "POST",
            f"/beads/{bead_id}/transition",
            json={"from_state": from_state, "to_state": to_state, "created_by": created_by},
        )

    def patch_content(self, bead_id: str, content: dict, created_by: str) -> dict:
        """Whole-content replace -- the API has no field-level merge.

        Callers must read, mutate, and write back the full content dict or
        they will silently drop fields.
        """
        return self._request(
            "PATCH", f"/beads/{bead_id}", json={"content": content, "created_by": created_by}
        )

    def add_note(
        self,
        parent_id: str,
        kind: str,
        body: str,
        created_by: str,
        trust_tier: str = "system",
        provenance: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict:
        content: dict[str, Any] = {"kind": kind, "body": body}
        content.update({k: v for k, v in extra.items() if v is not None})
        payload: dict[str, Any] = {
            "namespace": DEV_NAMESPACE,
            "type": "note",
            "state": "active",
            "trust_tier": trust_tier,
            "parent_id": parent_id,
            "created_by": created_by,
            "content": content,
        }
        if provenance is not None:
            payload["provenance"] = provenance
        return self._request("POST", "/beads", json=payload)

    def add_link(
        self, source_id: str, target_id: str, link_type: str, created_by: str
    ) -> dict:
        """POST /beads/{source_id}/links. ``created_by`` travels as the
        ``X-Created-By`` header, per the route's contract -- unlike every
        other write here, this endpoint does not read it from the JSON body."""
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            headers={"X-Created-By": created_by},
            json={"target_id": target_id, "link_type": link_type},
        )

    def create_bead(
        self,
        namespace: str,
        type: str,
        state: str,
        content: dict,
        created_by: str,
        *,
        trust_tier: str = "user",
        context: dict | None = None,
        provenance: dict | None = None,
    ) -> dict:
        """Create a bead in any namespace, of any type, in any entry state
        the store accepts -- the generic counterpart ``create_task`` cannot
        be, because widening ``create_task`` itself to take these same
        arguments would drop the one guarantee it makes (always
        dev/task/pending). Callers that only ever want that should keep
        using ``create_task``; everyone else (a finance bead, an incident
        bead, a cost bead) uses this one instead of composing their own
        POST /beads.

        ``context`` and ``provenance`` ride as their own top-level keys --
        ``BeadCreate.context`` / ``BeadCreate.provenance``
        (apps/substrate/src/schemas.py) -- never folded into ``content``.
        Omitted (``None``), the request body carries neither key at all,
        byte-identical to a call before this widening."""
        payload: dict[str, Any] = {
            "namespace": namespace,
            "type": type,
            "state": state,
            "trust_tier": trust_tier,
            "created_by": created_by,
            "content": content,
        }
        if context is not None:
            payload["context"] = context
        if provenance is not None:
            payload["provenance"] = provenance
        return self._request("POST", "/beads", json=payload)

    def patch_context(self, bead_id: str, context: dict, created_by: str) -> dict:
        """Whole-context replace -- ``patch_content``'s counterpart for
        ``context`` instead of ``content``. The API has no field-level
        merge: a PATCH carrying only context leaves content untouched."""
        return self._request(
            "PATCH", f"/beads/{bead_id}", json={"context": context, "created_by": created_by}
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        namespace: str | None = None,
        type: str | None = None,
    ) -> list[dict]:
        """POST /beads/search -- the store's vector-similarity search
        (``BeadSearchRequest``/``BeadSearchHit`` in
        ``apps/substrate/src/schemas.py``). Returns the hit list verbatim,
        each item shaped ``{"bead": {...}, "score": float}``.

        This does not itself enforce ``BeadSearchRequest``'s bounds (query
        1..4000 chars, limit 1..50): duplicating that check here would be a
        second place for it to drift from the store's. An out-of-bounds
        call reaches the store unmodified and comes back as the same
        ``SubstrateError`` any other rejected request would raise.
        """
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if namespace is not None:
            payload["namespace"] = namespace
        if type is not None:
            payload["type"] = type
        return self._request("POST", "/beads/search", json=payload)
