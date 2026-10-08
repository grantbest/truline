"""Serves the dispatch schedule's pause/note/recent-run state to a browser
request, without a terminal session on the dispatcher host (S54-B / R26.09
O-10, read half only).

Reuses ``schedule_runtime.describe_factory_schedule_status`` from the real
``apps/factory-dispatcher`` checkout this service ships alongside — never
reimplemented here (OPS-40, OPS-47; see ``.factory/design.md``). Imported via
``FACTORY_DISPATCHER_ROOT`` (``tools/task_filing.py``'s variable — always set
in this image), not ``tools/factory_status.py``'s ``FACTORY_STATUS_REPO_ROOT``
(OPS-110: that one is unset in production by design). ``schedule_runtime.py``
imports only stdlib at module scope, so a plain ``import schedule_runtime``
after adding the dispatcher dir to ``sys.path`` is sufficient -- unlike
``file_task``, it has no import-cycle hazard that requires importing another
module first.

The Temporal client is ``tools.factory_merge.get_factory_dispatcher_client()``
-- the same cached client already opened against the dispatcher's own
namespace/task-queue for the merge-by-verdict surface. One connection, one
place its lifecycle is owned.

``schedule_runtime.FactoryScheduleStatus`` carries no pause note, and
``apps/factory-dispatcher/**`` is forbidden to this bead, so the note is read
by this module's own second ``describe()`` call
(``description.schedule.state.note``) rather than by extending the reused
dataclass -- see ``.factory/design.md`` for why this is not a
reimplementation of the reader (it still owns ``paused``, ``in_flight`` and
``recent``).

``apps/factory-dispatcher/schedule_status.py``'s ``main()`` (posts Discord
alerts, can announce beads) is never imported here -- this module imports
only ``schedule_runtime``.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
from typing import Any

from tools import factory_merge

#: Distinct from tools.factory_status.REPO_ROOT_ENV and shared with
#: tools.task_filing.FACTORY_DISPATCHER_ROOT_ENV -- see that module's
#: docstring for why this variable (unlike factory_status's) is always set in
#: this image, never a permanent by-design absence.
FACTORY_DISPATCHER_ROOT_ENV = "FACTORY_DISPATCHER_ROOT"

#: Matches apps/factory-dispatcher/config.py's Config.DISPATCH_SCHEDULE_ID
#: exactly -- the one durable schedule this route reports on.
DISPATCH_SCHEDULE_ID_ENV = "FACTORY_DISPATCH_SCHEDULE_ID"
DEFAULT_DISPATCH_SCHEDULE_ID = "factory-dispatcher-dev"


class Unavailable(RuntimeError):
    """The dispatcher checkout or Temporal itself could not be reached.

    Always a bug to fix in this image (FACTORY_DISPATCHER_ROOT is set
    unconditionally by the Dockerfile) or a real, transient Temporal
    incident -- never a permanent by-design absence the way
    tools.factory_status.NotConfigured is.
    """


def _dispatcher_dir() -> pathlib.Path:
    root = os.environ.get(FACTORY_DISPATCHER_ROOT_ENV)
    if not root:
        raise Unavailable(
            f"{FACTORY_DISPATCHER_ROOT_ENV} is not set -- the dispatcher tree "
            "this route reads schedule state through is not reachable. This "
            "must always be set in this service's image; an unset value here "
            "means the image was built without it, not a permanent absence."
        )
    return pathlib.Path(root) / "apps" / "factory-dispatcher"


def _schedule_runtime_module() -> Any:
    dispatcher_dir = _dispatcher_dir()
    if not dispatcher_dir.is_dir():
        raise Unavailable(f"factory-dispatcher checkout not found at {dispatcher_dir}")
    if str(dispatcher_dir) not in sys.path:
        sys.path.insert(0, str(dispatcher_dir))
    try:
        return importlib.import_module("schedule_runtime")
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(f"factory-dispatcher's schedule_runtime could not be imported: {exc}") from exc


def _schedule_id() -> str:
    return os.environ.get(DISPATCH_SCHEDULE_ID_ENV, DEFAULT_DISPATCH_SCHEDULE_ID)


def _drain_outcome_jsonable(outcome: Any) -> dict[str, Any]:
    return {
        "workflow_id": outcome.workflow_id,
        "scheduled_at": outcome.scheduled_at,
        "status": outcome.status,
        "failed": outcome.failed,
        "succeeded": outcome.succeeded,
    }


def _in_flight_jsonable(workflow: Any) -> dict[str, Any]:
    return {
        "workflow_id": workflow.workflow_id,
        "run_id": workflow.run_id,
        "scheduled_at": workflow.scheduled_at,
        "started_at": workflow.started_at,
    }


async def dispatch_schedule_status(*, client: Any | None = None) -> dict[str, Any]:
    """Whether the dispatch schedule is paused, its pause note verbatim, and
    the outcome/time of its most recent firings.

    ``client`` lets tests inject a double in place of a real Temporal
    connection (AC-6: no test may open a real Temporal client) -- mirrors
    ``tools.factory_status.task_runnability``'s own ``store`` parameter.
    """
    try:
        schedule_runtime = _schedule_runtime_module()
    except Unavailable as exc:
        return {"status": "unknown", "detail": str(exc)}

    try:
        real_client = client if client is not None else await factory_merge.get_factory_dispatcher_client()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"could not reach Temporal: {exc}"}

    schedule_id = _schedule_id()
    try:
        # Two describe() calls, deliberately: describe_factory_schedule_status
        # (paused/in_flight/recent) owns the reused reader's answer; the pause
        # note has no home on its FactoryScheduleStatus return, so it is read
        # here directly -- see this module's docstring.
        description = await real_client.get_schedule_handle(schedule_id).describe()
        status = await schedule_runtime.describe_factory_schedule_status(
            real_client, schedule_id=schedule_id
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "detail": f"could not describe schedule {schedule_id}: {exc}"}

    note = getattr(getattr(getattr(description, "schedule", None), "state", None), "note", None)

    return {
        "status": "ok",
        "schedule_id": status.schedule_id,
        "paused": status.paused,
        "note": note,
        "in_flight": [_in_flight_jsonable(w) for w in status.in_flight],
        "recent": [_drain_outcome_jsonable(o) for o in status.recent],
        "last_success_at": status.last_success_at,
        "consecutive_failures": status.consecutive_failures,
        "recent_failures": status.recent_failures,
    }
