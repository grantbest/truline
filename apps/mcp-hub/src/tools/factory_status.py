"""Serves the factory dispatcher's own runnability and release-delivery
answers, so a screen can read them without re-deriving
``apps/factory-dispatcher/guards.py::is_runnable`` or
``scripts/release-status.py``'s ``build_release_report`` in a second
language (see OPS-40/OPS-47: the console's own re-derivation of
``guards.open_questions`` disagreed with the dispatcher in production for
several hours because a rule changed in one language and not the other).

Both answers below are produced by importing and calling the dispatcher's
and the CLI's own code from the checkout this service runs alongside — never
reimplemented here. That checkout (``apps/factory-dispatcher``, ``scripts``,
``docs/releases``) is not guaranteed to be present in every place this module
runs (this service's own container image ships only ``apps/mcp-hub/src``), so
every entry point here treats "the authority could not be loaded or read" as
its own outcome — ``status: unknown`` — rather than falling through to a
default that would read as a real answer.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
import sys
from typing import Any

# This service's own container image ships only apps/mcp-hub/src (see module
# docstring): /app/src/tools/factory_status.py has just four parents in that
# image (/app/src/tools, /app/src, /app, /), none of them a repo root, so
# there is no fixed parent-index that is a repo root in every place this
# module runs. #614 hard-coded parents[4], which is a repo root in a checkout
# (tools -> src -> mcp-hub -> apps -> repo root) and out of range in the
# image, raising IndexError at import time and taking the whole app down
# before uvicorn could bind. Guessing a shallower index would not fix that
# class of bug, only move it: it would silently resolve to whatever real
# directory happens to sit at that depth instead of failing to import.
#
# So the checkout location is explicit configuration, not derived from
# __file__: whoever runs this module alongside a real factory-dispatcher/
# scripts checkout (local dev, tests -- see tests/conftest.py) sets
# FACTORY_STATUS_REPO_ROOT. Unset is a real, permanent state in production
# (the image never ships the checkout) and is handled the same way as
# "checkout present but unreadable" already was: Unavailable, surfaced as
# status: unknown, never a crash and never a silently wrong path.
#
# OPS-110: that "unset" case is not just permanent, it is by design -- see
# apps/mcp-hub/Dockerfile (COPYs only pyproject.toml and src/) and
# .github/workflows/build-mcp-hub.yml (docker build's context is
# apps/mcp-hub/ itself, so a COPY here could not reach apps/factory-dispatcher
# or scripts/ even if added -- that would mean changing the build context, an
# infrastructure change outside this module's scope). Shipping the checkout
# into the image (build it to work) and moving these endpoints to run
# alongside a real checkout (serve them elsewhere) were both considered and
# both require changes outside apps/mcp-hub (the comparison is the paragraph
# above). What stays inside this module's scope, and what this
# change adds, is making the distinction below reach the wire: "the checkout
# was never configured" (NotConfigured, below) is a fact about how this
# service is deployed, not a fault -- and must read differently, to a caller,
# than "the checkout is configured but something about reaching it failed"
# (every other Unavailable). Callers get that as a `code` field alongside
# `status: "unknown"`: `"not_configured"` for the former, `"unavailable"` for
# the latter. The console's release-view.ts and dev-board.ts use it to stop
# rendering a permanent, by-design absence as an indefinite, transient-looking
# failure.
REPO_ROOT_ENV = "FACTORY_STATUS_REPO_ROOT"

_repo_root_value = os.environ.get(REPO_ROOT_ENV)
REPO_ROOT = pathlib.Path(_repo_root_value).resolve() if _repo_root_value else None
DISPATCHER_DIR = (REPO_ROOT / "apps" / "factory-dispatcher") if REPO_ROOT else None
SCRIPTS_DIR = (REPO_ROOT / "scripts") if REPO_ROOT else None


class Unavailable(RuntimeError):
    """The authority this module defers to could not be loaded or read."""


class NotConfigured(Unavailable):
    """FACTORY_STATUS_REPO_ROOT is unset.

    The permanent, by-design production state (see REPO_ROOT_ENV's module-
    level comment): this service's image never ships the checkout, so there
    is nothing to load, not something that failed to load. Kept as an
    `Unavailable` subclass so every existing `except Unavailable` catch still
    degrades to `status: "unknown"` unchanged; `_unknown` below is what reads
    the subtype back out to set `code`.
    """


def _unknown(exc: Unavailable) -> dict[str, Any]:
    """The one place an ``Unavailable`` becomes the wire's ``status: unknown``.

    ``code`` is what lets a caller -- ultimately the console -- tell
    OPS-110's two cases apart without parsing ``detail`` prose:
    ``not_configured`` is the permanent, by-design absence (the
    FACTORY_STATUS_REPO_ROOT env var unset); anything else is
    ``unavailable``, a checkout that is configured but not reachable or not
    readable right now, which is the shape a real incident takes.
    """
    code = "not_configured" if isinstance(exc, NotConfigured) else "unavailable"
    return {"status": "unknown", "code": code, "detail": str(exc)}


def _dispatcher_modules() -> tuple[Any, Any, Any]:
    """Import ``dispatch``, ``queue_order`` and ``substrate`` from the real
    checkout.

    Raises :class:`Unavailable` rather than letting ``ImportError`` propagate,
    so every caller has one exception type to catch for "the dispatcher's own
    code is not reachable from here" — whether that is because this process's
    image does not ship the checkout at all, or because importing it failed.
    """
    if DISPATCHER_DIR is None:
        raise NotConfigured(f"factory-dispatcher checkout not configured (set {REPO_ROOT_ENV})")
    if not DISPATCHER_DIR.is_dir():
        raise Unavailable(f"factory-dispatcher checkout not found at {DISPATCHER_DIR}")
    if str(DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(DISPATCHER_DIR))
    try:
        dispatch = importlib.import_module("dispatch")
        queue_order = importlib.import_module("queue_order")
        substrate = importlib.import_module("substrate")
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(f"factory-dispatcher modules could not be imported: {exc}") from exc
    return dispatch, queue_order, substrate


def _load_hyphenated(name: str, path: pathlib.Path) -> Any:
    """Load a ``-``-named script as a module, the way it loads itself.

    Mirrors ``release-status.py``'s own ``_load_release_load_module`` (the
    hyphen in the filename makes ``import release_status`` impossible). The
    module is registered in ``sys.modules`` under ``name`` before execution —
    without that, its ``@dataclass``-decorated classes fail to resolve their
    own module at class-creation time.
    """
    if not path.is_file():
        raise Unavailable(f"{path} not found")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Unavailable(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except SystemExit as exc:
        del sys.modules[name]
        raise Unavailable(f"{name} raised SystemExit while loading: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        del sys.modules[name]
        raise Unavailable(f"{name} could not be loaded: {exc}") from exc
    return module


def _unknown_module() -> Any:
    """``scripts/unknown.py`` -- the one shared "could not be computed"
    representation, loaded the same lazy way as ``release_status`` (see its
    module docstring: this service's image may not ship ``scripts/`` at
    all), so a missing checkout degrades to ``status: unknown`` rather than
    crashing or silently dropping the distinction it exists to carry.
    """
    cached = sys.modules.get("unknown")
    if cached is not None:
        return cached
    if SCRIPTS_DIR is None:
        raise NotConfigured(f"scripts checkout not configured (set {REPO_ROOT_ENV})")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module("unknown")
    except Exception as exc:  # noqa: BLE001 - collapsed into Unavailable for callers
        raise Unavailable(f"unknown module could not be imported: {exc}") from exc


def _release_status_module() -> Any:
    # Loaded once per process: re-running _load_hyphenated on every request
    # would re-read, re-compile and re-exec release-status.py per HTTP call,
    # and redefine its classes so isinstance checks against earlier loads
    # fail (release-gate finding on #614).
    cached = sys.modules.get("release_status")
    if cached is not None:
        return cached
    if SCRIPTS_DIR is None:
        raise NotConfigured(f"scripts checkout not configured (set {REPO_ROOT_ENV})")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return _load_hyphenated("release_status", SCRIPTS_DIR / "release-status.py")


def task_runnability(task_id: str, *, store: Any | None = None) -> dict[str, Any]:
    """``queue_order.selectable_verdict``'s live verdict for ``task_id``.

    Reuses ``substrate.default_store`` (when ``store`` is not supplied for a
    test), ``dispatch.resolve_task_release_states`` and
    ``queue_order.selectable_verdict`` verbatim — the exact objects
    ``dispatch.pick_task`` itself calls — so this can never independently
    drift from what the dispatcher would decide for the same bead.
    """
    try:
        dispatch, queue_order, substrate = _dispatcher_modules()
    except Unavailable as exc:
        return _unknown(exc)

    if store is None:
        try:
            store = substrate.default_store()
        except Exception as exc:  # noqa: BLE001
            return {"status": "unknown", "code": "unavailable", "detail": f"substrate is not configured: {exc}"}

    try:
        all_tasks = store.list_tasks()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "code": "unavailable", "detail": f"could not list dev.task beads: {exc}"}

    matches = [t for t in all_tasks if t.get("id") == task_id]
    if not matches:
        return {"status": "ok", "found": False}
    task = matches[0]

    try:
        notes = store.list_notes(task_id)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "code": "unavailable", "detail": f"could not list notes for {task_id}: {exc}"}

    try:
        release_by_task_id = dispatch.resolve_task_release_states(store, [task])
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "code": "unavailable", "detail": f"could not resolve release state: {exc}"}

    verdict = queue_order.selectable_verdict(task, notes, all_tasks, release_by_task_id)
    return {
        "status": "ok",
        "found": True,
        "runnable": verdict.selectable,
        "reason": verdict.hold_reasons[0] if verdict.hold_reasons else None,
        "selectable": verdict.selectable,
        "hold_reasons": list(verdict.hold_reasons),
        "latched": verdict.latched,
    }


def release_delivery(release_ref: str, *, reader: Any | None = None) -> dict[str, Any]:
    """``release-status.py``'s live ``build_release_report`` for ``release_ref``.

    Reuses ``release-status.py``'s own charter loader, ``GateSubstrateReader``,
    ``gather_live_data`` and ``build_release_report`` verbatim — the same
    functions its CLI runs — so this can never independently drift from what
    ``python scripts/release-status.py`` prints for the same release.
    """
    try:
        release_status = _release_status_module()
    except Unavailable as exc:
        return _unknown(exc)

    try:
        unknown = _unknown_module()
    except Unavailable as exc:
        return _unknown(exc)

    try:
        release_load = release_status._load_release_load_module()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "code": "unavailable", "detail": f"release-load.py could not be loaded: {exc}"}

    releases_dir = release_status.REPO / release_status.RELEASES_DIR
    requirements_dir = release_status.REPO / release_status.REQUIREMENTS_DIR
    if not releases_dir.is_dir():
        # Distinct from "no charter matches this ref": the whole population is
        # unreadable, which must not be reported as though it were empty.
        return {
            "status": "unknown",
            "code": "unavailable",
            "detail": f"release charter directory not found: {releases_dir}",
        }

    try:
        charter_items = release_load.load_charters(
            release_load.charter_paths(releases_dir), requirements_dir=requirements_dir
        )
    except SystemExit as exc:
        return {"status": "unknown", "code": "unavailable", "detail": f"release charters could not be loaded: {exc}"}

    matches = [item for item in charter_items if item.ref == release_ref]
    if not matches:
        return {"status": "ok", "found": False}
    charter_item = matches[0]

    if reader is None:
        reader = release_status.GateSubstrateReader(
            os.environ.get("SUBSTRATE_URL"), os.environ.get("SUBSTRATE_API_KEY")
        )

    try:
        tasks, delivers, conformances = release_status.gather_live_data(
            reader, [charter_item.content]
        )
    except RuntimeError as exc:
        return {"status": "unknown", "code": "unavailable", "detail": f"release data could not be read: {exc}"}

    report = release_status.build_release_report(charter_item.content, tasks, delivers, conformances)

    return {
        "status": "ok",
        "found": True,
        "ref": report.ref,
        "name": report.name,
        "outcomes": [
            {
                "id": outcome.id,
                "statement": outcome.statement,
                "work_class": outcome.work_class,
                "task_count": outcome.task_count,
                "tasks_by_state": outcome.tasks_by_state,
            }
            for outcome in report.outcomes
        ],
        "unclassified_delivering": list(report.unclassified_delivering),
        "balance": [
            {
                "work_class": entry.work_class,
                "declared_pct": entry.declared_pct,
                "actual_count": entry.actual_count,
                "actual_pct": entry.actual_pct,
                "absent": entry.absent,
                "sample_size": entry.sample_size,
                "insufficient_sample": entry.insufficient_sample,
            }
            for entry in report.balance
        ],
        "criteria": [
            {
                "ref": status.ref,
                "as_of_opened": status.as_of_opened.value if status.as_of_opened else None,
                "latest": status.latest.value if status.latest else None,
                "stale": status.stale,
                "changed": status.changed,
                # None means "measurable" -- distinct from an Unknown, which
                # is passed through unknown.to_jsonable() rather than left as
                # a bare Python object, so this dict is safe to hand straight
                # to FastAPI's JSON response encoder without collapsing the
                # distinction it carries (AC2: it must survive serialization).
                "unmeasurable": (
                    unknown.to_jsonable(status.unmeasurable)
                    if status.unmeasurable is not None
                    else None
                ),
            }
            for status in report.criteria
        ],
    }
