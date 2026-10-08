"""Shared dispatcher-checkout lookup and import-order fix for the gateway's
factory-filing routes.

``tools.task_filing`` and ``tools.finding_filing`` each file a bead by
importing the real module (``file_task``/``file_finding``) from the factory-
dispatcher checkout this service ships alongside -- never a re-derived
filing path (PRIN-005, see ``.factory/design.md``). Both need the identical
``FACTORY_DISPATCHER_ROOT`` lookup and the identical circular-import
workaround, so it is factored here once rather than copied twice.

The workaround: a bare ``import file_task`` (or ``import file_finding``)
closes a cycle onto itself through ``dispatch`` -> ``activities`` ->
``activities.doctrine_staleness`` -> ``scanner`` -> back to the target
module. The CLI never observes it because the running script is registered
as ``__main__``, a different ``sys.modules`` key than the module's own name.
A bare ``import`` from here observes it directly: the cycle reaches back for
the target module's name while only the first few lines of that very import
have run, and Python hands back the partially-initialized module --
``ImportError: cannot import name '...' from partially initialized module``.
Reproduced directly against this checkout (see ``tools.task_filing``'s
former module docstring, "Discovery 1", and ``tests/test_task_filing.py``).
Importing ``dispatch`` FIRST sidesteps it: by the time the deep chain
reaches back for the target module's name, that name isn't in
``sys.modules`` yet, so Python loads a full, independent, successful copy
under that name before the caller's own subsequent import ever runs -- by
then it's already cached. Getting this order backwards silently reintroduces
the ``ImportError`` above.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
from typing import Any

#: Deliberately distinct from tools.factory_status.REPO_ROOT_ENV -- that
#: variable is optional by design (OPS-110: this service's image never ships
#: that checkout, degrading to status: "unknown"). A *filing* route that
#: degraded the same way would be a second intake that files nothing (the
#: #781 gate finding task_filing.py exists to not repeat), so this variable
#: is never meant to be unset in production -- the Dockerfile sets it
#: unconditionally.
FACTORY_DISPATCHER_ROOT_ENV = "FACTORY_DISPATCHER_ROOT"


class Unavailable(RuntimeError):
    """FACTORY_DISPATCHER_ROOT is unset, or the dispatcher tree under it
    can't be imported. Always a bug to fix, never a permanent by-design
    state -- the Dockerfile sets this variable unconditionally, so a caller
    reaching this in production means the image was built without the
    dispatcher tree these routes depend on."""


def dispatcher_dir() -> pathlib.Path:
    root = os.environ.get(FACTORY_DISPATCHER_ROOT_ENV)
    if not root:
        raise Unavailable(
            f"{FACTORY_DISPATCHER_ROOT_ENV} is not set — the dispatcher tree "
            "this route files through is not reachable. This must always be "
            "set in this service's image (see apps/mcp-hub/Dockerfile); an "
            "unset value here means the image was built without it, not a "
            "permanent absence."
        )
    return pathlib.Path(root) / "apps" / "factory-dispatcher"


def load_dispatcher_module(name: str) -> Any:
    """Import and return ``name`` from the real dispatcher checkout.

    ``import dispatch`` before ``name`` is load-bearing -- see this module's
    docstring. Do not reorder these two imports.
    """
    d = dispatcher_dir()
    if not d.is_dir():
        raise Unavailable(f"factory-dispatcher checkout not found at {d}")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        importlib.import_module("dispatch")
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(f"factory-dispatcher's {name} could not be imported: {exc}") from exc
