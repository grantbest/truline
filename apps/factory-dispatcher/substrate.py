#!/usr/bin/env python3
"""Thin bead client for the factory dispatcher.

Only the five calls the dispatcher makes. Deliberately not a general SDK — the
substrate API is the contract, and a fat client here would be a second place for
the schema to drift.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Any

import httpx

from beadstore import BeadStore

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

        The general form of ``list_tasks`` below — pulled out so a caller that
        needs a whole population of a non-``dev.task`` type (e.g. every
        ``arch.release`` bead, to resolve a ``release_ref``) does not need a
        second, hard-coded query builder that could drift from this one.

        A single page capped at ``limit`` cannot tell a caller "this is the
        whole population" from "there are more past here" — and a coverage
        union built from the former when it was really the latter drops
        whichever beads landed on the far side of the cut with nothing to
        signal it (the newest-first ordering makes a dropped OPEN bead the
        quiet case, not the loud one). So ``limit`` is a page size, not a cap:
        this keeps asking for the next page, by ``offset``, until the server
        returns fewer rows than requested, and only then returns the union of
        every page fetched. A short page is the exhaustion signal: the route's
        SQL ``LIMIT`` always returns ``min(limit, remaining)``, so fewer rows
        than asked for means the population ran out. That is a fact about the
        server this loop depends on, stated here because the dependency is
        otherwise invisible.

        The walk is bounded. Fetching every page trusts the server to honour
        ``offset``; a server that ignores it hands back a full page forever,
        which as an unbounded loop is a hot request loop and unbounded memory
        in a nightly — worse than the truncation this method exists to fix. So
        the loop raises :class:`SubstrateError` at ``MAX_PAGES``: a bound that
        fails loudly rather than one that silently returns a short answer, the
        same choice PRIN-015 makes everywhere else. ``limit`` must be positive
        for the same reason — ``limit=0`` would satisfy neither exit.
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
        them — no ``limit``, the route takes none."""
        return self._request("GET", f"/beads/{bead_id}/events")

    # -- writes --------------------------------------------------------------

    def create_task(self, content: dict, created_by: str, *, trust_tier: str = "user") -> dict:
        """File a new dev.task bead — the intake path.

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
        the store accepts.

        ``context`` and ``provenance`` ride as their own top-level keys,
        added to the POST body only when not None -- omitted, the body is
        byte-identical to a call before this widening.
        """
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
        """Whole-context replace -- the API has no field-level merge."""
        return self._request(
            "PATCH", f"/beads/{bead_id}", json={"context": context, "created_by": created_by}
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
        """Whole-content replace — the API has no field-level merge.

        Callers must read, mutate, and write back the full content dict or they
        will silently drop fields.
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
        ``X-Created-By`` header, per the route's contract — unlike every other
        write here, this endpoint does not read it from the JSON body."""
        return self._request(
            "POST",
            f"/beads/{source_id}/links",
            headers={"X-Created-By": created_by},
            json={"target_id": target_id, "link_type": link_type},
        )


_store_override: contextvars.ContextVar[BeadStore | None] = contextvars.ContextVar(
    "factory_dispatcher_store_override", default=None
)


def default_store() -> BeadStore:
    """Return the store the dispatcher runs against today.

    Honors an override installed by ``use_store`` for the lifetime of one
    dispatch. The CLI (``dispatch.dispatch_once``) and the scheduled Temporal
    drain now share the same activity step functions
    (``activities/dispatch_steps.py``), each of which resolves its own store
    via this call rather than taking one as a parameter — a Temporal
    activity's payload is a single plain dict, with no room for a live
    ``BeadStore`` object. ``dispatch_once`` installs the ``BeadStore`` its
    caller gave it here instead of forking every step's signature; the real
    Temporal worker never installs an override, so it keeps resolving a
    fresh ``Substrate()`` exactly as before.
    """
    override = _store_override.get()
    if override is not None:
        return override
    return Substrate()


@contextlib.contextmanager
def use_store(store: BeadStore):
    """Make ``default_store()`` return ``store`` for the duration of the block."""
    token = _store_override.set(store)
    try:
        yield
    finally:
        _store_override.reset(token)
