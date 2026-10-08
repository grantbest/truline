#!/usr/bin/env python3
"""The get-only view of the store -- ported from ``scripts/substrate_client.py``'s
``SubstrateReader``/``reader()`` (the merge-gate read path, R26.06, bead
a3e235d5): read-only by construction, not by docstring. The object a gate
script holds has no create/patch/add_link/delete attribute to call.

Deliberately its own request, not a call through :meth:`Substrate._request`:
that choke point always attaches ``Content-Type`` (every one of the eleven
BeadStore methods it serves either sends a JSON body or is a GET the
dispatcher never distinguished from a write), where the client this ports
from sent ``Content-Type`` only when a body was present -- never on its
bodyless GET. Routing this through ``_request`` would add a header the old
client never sent, which the recorder proof in
``tests/test_recorder_parity.py`` exists to catch.
"""

from __future__ import annotations

from typing import Any

import httpx

from .client import HTTP_TIMEOUT_S, Substrate, SubstrateError


class SubstrateReader:
    def __init__(self, client: Substrate) -> None:
        self._client = client

    def get(self, path: str) -> Any:
        url = f"{self._client.base_url}{path}"
        api_key = self._client._headers["X-API-Key"]
        resp = httpx.request("GET", url, headers={"X-API-Key": api_key}, timeout=HTTP_TIMEOUT_S)
        if resp.status_code >= 300:
            raise SubstrateError(resp.status_code, resp.text)
        return resp.json()


def reader(base_url: str | None = None, api_key: str | None = None) -> SubstrateReader:
    """The read-only gate scripts' front door: same construction shape as
    :class:`Substrate`, wrapped so the returned object can only read."""
    return SubstrateReader(Substrate(base_url=base_url, api_key=api_key))
