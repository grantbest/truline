"""Files a `dev.note` bead by calling the factory-dispatcher's own thin
Substrate client (``substrate.Substrate.add_note``) -- never a re-derived
write path (PRIN-005, see ``.factory/design.md``).

Reuses the same ``FACTORY_DISPATCHER_ROOT`` checkout ``tools.task_filing``
depends on, but imports ``substrate`` directly rather than ``file_task``:
dev.note filing has no duplicate-check, traceability-resolution or
forbidden-path business rules to borrow the way dev.task filing borrows
``file_task.file_spec``, so there is nothing here that needs ``file_task``'s
tangled ``file_task <-> dispatch`` import cycle (see ``task_filing``'s own
module docstring). ``apps/factory-dispatcher/substrate.py`` imports neither
``dispatch`` nor ``file_task``, so the "import dispatch first" workaround
does not apply to it.

The closed dev.note shape (kind vocabulary, `answer` needs `answers_ref`,
`review` needs `verdict`, ...) is validated by the caller
(``routers.v1.factory.NoteCreateRequest``), mirroring
``apps/substrate/src/schemas.py::DevNoteContent`` at the Pydantic-model
level -- this module is a thin, unopinionated forwarder onto
``Substrate.add_note``, the same way ``add_note``'s own ``**extra`` is an
unopinionated forward onto ``content``.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
from typing import Any


#: Same variable tools.task_filing.py uses -- both routes depend on the same
#: dispatcher checkout being mounted at the same place.
FACTORY_DISPATCHER_ROOT_ENV = "FACTORY_DISPATCHER_ROOT"


class Unavailable(RuntimeError):
    """FACTORY_DISPATCHER_ROOT is unset or the dispatcher tree under it can't
    be imported. Mirrors tools.task_filing.Unavailable -- see that module's
    docstring for why this is always a bug to fix in production, never a
    permanent by-design state."""


class FilingRefused(RuntimeError):
    """``Substrate.add_note`` reached the store and the store refused the
    write -- a business-rule refusal (e.g. a shape ``NoteCreateRequest``
    didn't catch, or a state the store itself rejects), not a transport or
    configuration failure. Carries the store's own status and body so the
    HTTP surface can refuse with the store's own reason, the same discipline
    tools.task_filing.FilingRefused gives the tasks route via file_spec's
    SystemExit text."""

    def __init__(self, status: int, body: str):
        super().__init__(f"substrate refused: {status} {body}")
        self.status = status
        self.body = body


def _dispatcher_dir() -> pathlib.Path:
    root = os.environ.get(FACTORY_DISPATCHER_ROOT_ENV)
    if not root:
        raise Unavailable(
            f"{FACTORY_DISPATCHER_ROOT_ENV} is not set -- the dispatcher tree "
            "this route files through is not reachable. This must always be "
            "set in this service's image (see apps/mcp-hub/Dockerfile); an "
            "unset value here means the image was built without it, not a "
            "permanent absence."
        )
    return pathlib.Path(root) / "apps" / "factory-dispatcher"


def _load_substrate() -> Any:
    """Import and return the factory-dispatcher's ``substrate`` module."""
    dispatcher_dir = _dispatcher_dir()
    if not dispatcher_dir.is_dir():
        raise Unavailable(f"factory-dispatcher checkout not found at {dispatcher_dir}")
    if str(dispatcher_dir) not in sys.path:
        sys.path.insert(0, str(dispatcher_dir))
    try:
        return importlib.import_module("substrate")
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(
            f"factory-dispatcher's substrate client could not be imported: {exc}"
        ) from exc


def file_dev_note(
    parent_id: str,
    kind: str,
    body: str,
    created_by: str,
    client_type: str,
    *,
    fields: dict[str, Any] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """File a ``dev.note`` bead against ``parent_id`` over HTTP.

    ``fields`` carries only the kind-specific extras the caller's request
    actually set (``answers_ref``, ``verdict``, ``url``, ``blocking``,
    ``releases_work``) -- forwarded verbatim as ``Substrate.add_note``'s
    ``**extra``, never defaulted here. A key genuinely absent from ``fields``
    never reaches ``content``; that distinction is the caller's to make
    (``NoteCreateRequest.model_fields_set``), not this function's, because by
    the time a plain value arrives here "unset" and "explicitly false" are
    already indistinguishable.

    ``client_type`` is the Access-derived ``identity.client_type`` ("human" |
    "service" | "unknown") -- ``trust_tier`` is derived from it ("user" for a
    human, "system" otherwise, matching ``Substrate.add_note``'s own default
    for everything that isn't a human at a keyboard) rather than hard-coded,
    since this route is reachable by any client holding ``factory.write``,
    not only the console's human traffic.

    ``store`` lets tests inject a fake in place of a real ``Substrate()`` --
    mirrors ``tools.task_filing.file_dev_task``'s own ``store`` parameter.
    """
    sub = store if store is not None else _load_substrate().Substrate()
    trust_tier = "user" if client_type == "human" else "system"
    try:
        return sub.add_note(
            parent_id, kind, body, created_by, trust_tier=trust_tier, **(fields or {})
        )
    except Exception as exc:
        # Duck-typed on shape, not isinstance against
        # apps/factory-dispatcher/substrate.py::SubstrateError's class: that
        # class lives behind the same dynamic import _load_substrate() does,
        # which a store-injected test must not need just to assert this
        # mapping. SubstrateError's whole shape is these two attributes.
        status = getattr(exc, "status", None)
        body_text = getattr(exc, "body", None)
        if isinstance(status, int) and body_text is not None:
            raise FilingRefused(status, body_text) from exc
        raise
