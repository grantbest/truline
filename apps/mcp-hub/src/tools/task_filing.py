"""Files a `dev.task` bead by calling ``file_task.file_spec`` from the real
dispatcher checkout this service ships alongside — never a re-derived filing
path (PRIN-005, see ``.factory/design.md``).

The ``FACTORY_DISPATCHER_ROOT`` lookup and the ``import dispatch``-before-
``import file_task`` circular-import fix are shared with
``tools.finding_filing`` and live in ``tools.dispatcher_loader`` — see that
module's docstring for the full explanation of both (``FACTORY_DISPATCHER_ROOT``
vs. ``tools/factory_status.py``'s distinct, optional-by-design
``FACTORY_STATUS_REPO_ROOT``; and why a bare ``import file_task`` reproduces
``ImportError: cannot import name 'CLOSED_TASK_STATES' from partially
initialized module 'file_task'`` while importing ``dispatch`` first does
not). ``FACTORY_DISPATCHER_ROOT_ENV`` and ``Unavailable`` are re-exported
here unchanged so existing callers/tests that reach them as
``task_filing.FACTORY_DISPATCHER_ROOT_ENV`` / ``task_filing.Unavailable``
keep resolving.
"""

from __future__ import annotations

from typing import Any

from tools.dispatcher_loader import (
    FACTORY_DISPATCHER_ROOT_ENV,  # noqa: F401 - re-exported for callers/tests
    Unavailable,  # noqa: F401 - re-exported for callers/tests
    dispatcher_dir as _dispatcher_dir,  # noqa: F401 - re-exported for callers/tests
    load_dispatcher_module,
)


class FilingRefused(RuntimeError):
    """file_task.file_spec refused the spec (SystemExit) — a business-rule
    refusal (duplicate spec_identity, unresolvable release_ref, no
    requirement_refs and no waiver, unresolvable predecessor, etc.), not a
    transport or configuration failure. Carries file_task's own message text
    unchanged, so the HTTP surface can refuse with the SAME reason CLI filing
    would (PC-FAC-001/AC-5, reached from this second surface)."""


def _load_file_task() -> Any:
    """Import and return the real ``file_task`` module. See
    ``tools.dispatcher_loader.load_dispatcher_module`` for the load-bearing
    import order this relies on."""
    return load_dispatcher_module("file_task")


def file_dev_task(
    spec: dict[str, Any], created_by: str, *, store: Any | None = None
) -> dict[str, Any]:
    """File ``spec`` as a dev.task bead over HTTP.

    Calls ``file_task.file_spec`` with ``run_pristine_verification=False``
    (the Operator's Option A, 2026-09-17): this process must never clone or execute
    a client-supplied command — the dispatcher's own claim-time preflight
    (``activities.dispatch_steps.preflight_activity``) runs the identical
    check later, where a clone legitimately exists. See
    ``.factory/design.md`` for the full containment argument.

    ``store`` lets tests inject a fake in place of a real ``Substrate()`` —
    mirrors ``tools.factory_status.task_runnability``'s own ``store``
    parameter.
    """
    file_task = _load_file_task()
    sub = store if store is not None else file_task.Substrate()
    try:
        filed = file_task.file_spec(
            spec,
            created_by,
            spec_identity=None,
            supersede=None,
            run_pristine_verification=False,
            sub=sub,
        )
    except SystemExit as exc:
        raise FilingRefused(str(exc.code) if exc.code is not None else str(exc)) from exc
    return filed.bead
