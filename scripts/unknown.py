#!/usr/bin/env python3
"""The one representation of "this could not be computed."

``release-status.py`` already draws one instance of this distinction by hand:
a work class declared at a non-zero share with no delivered work reports
``ABSENT``, never ``0%`` (``BalanceEntry.absent``). That is a real, verified
fact -- the population was checked and came up empty -- and it is not what
this module is for. This module is for the *other* fact: a value nobody
could establish at all, which must never render the same as a zero or an
empty collection.

Every consumer of a value that might be unknown -- a CLI's text formatter, an
HTTP JSON response, a test fixture -- must recognise the same thing, or the
distinction is only as strong as the least careful call site. So there is
exactly one type (:class:`Unknown`) and one JSON encoding for it, defined
here and nowhere else; a second module inventing its own "unknown" shape is
the divergence this module exists to prevent, not a reasonable variation on
it.

No substrate, no cluster, no network: this is a pure, dependency-free module
so it can be imported from a container image that ships only
``apps/mcp-hub/src`` (see ``apps/mcp-hub/src/tools/factory_status.py``) as
readily as from a full checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The one JSON key that means "this dict is an encoded Unknown, not data."
#: Nothing else in this codebase's JSON output uses this key; that is what
#: makes the round trip lossless rather than a heuristic.
UNKNOWN_TAG = "__unknown__"


@dataclass(frozen=True)
class Unknown:
    """A value that could not be computed, carrying why.

    Distinct from ``None`` (absent on purpose), ``0``/``0.0`` (computed as
    zero), and ``""``/``[]``/``{}`` (computed as empty): those are answers.
    ``Unknown`` is the absence of an answer.
    """

    reason: str

    def to_jsonable(self) -> dict[str, Any]:
        return {UNKNOWN_TAG: True, "reason": self.reason}


def is_unknown(value: Any) -> bool:
    """True for an :class:`Unknown` or its round-tripped JSON encoding.

    A caller checks this without knowing whether ``value`` just came out of
    ``build_release_report`` or out of a ``json.loads`` on an HTTP response
    body -- both forms answer the same way (AC4).
    """
    if isinstance(value, Unknown):
        return True
    return isinstance(value, dict) and value.get(UNKNOWN_TAG) is True


def to_jsonable(value: Any) -> Any:
    """Recursively replace any :class:`Unknown` with its JSON-safe encoding.

    Everything else passes through unchanged -- this does not attempt to be
    a general JSON encoder, only to make ``Unknown`` safe to hand to
    ``json.dumps`` (directly, or via a framework's own encoder).
    """
    if isinstance(value, Unknown):
        return value.to_jsonable()
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def from_jsonable(value: Any) -> Any:
    """Recursively restore :class:`Unknown` from its JSON-safe encoding.

    The inverse of :func:`to_jsonable`, applied after a value has actually
    made the round trip through ``json.dumps``/``json.loads`` (or an HTTP
    client's own JSON decoding) -- this is what proves the distinction
    survives transport rather than only the in-process object.
    """
    if isinstance(value, dict):
        if value.get(UNKNOWN_TAG) is True:
            return Unknown(reason=str(value.get("reason", "")))
        return {key: from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_jsonable(item) for item in value]
    return value
