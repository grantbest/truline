"""In-cluster release-charter applier.

A release charter with no scheduled re-measurer rots the way the portfolio YAML rotted for 18
days while being the system of record (the milestone plan's own framing): managed data with no
scheduled re-measurer and consumer is the Amendment 29 defect shape, an artifact with no reader.
`scripts/release-load.py` has always been able to mirror `docs/releases/*.json` into
`arch.release` beads; nothing has ever called it on a schedule. This activity is that caller,
running beside the dispatcher on the same worker so it reuses the `SUBSTRATE_API_KEY` write
credential already there.

This is a thin wrapper, not a reimplementation. `scripts/release-load.py`'s own `reconcile()`
already carries the PRIN-014 property this activity is required to have: a release bead's content
is patched only when it differs from the charter on disk
(`current.get("content") == item.content` -- release-load.py), so calling `reconcile(..., apply=
True)` twice against an unchanged charter set issues zero `create`/`patch` calls on the second
call. `activities/ea_apply.py` needed its own git-revision short-circuit because `ea-load.py`'s
`build_plan` carries no such gate of its own; `release-load.py` has no equivalent gap, so no
second, redundant revision check is added here.

PRIN-014 (the same revision twice is zero writes) is not PRIN-015 at the other end: a charter set
that cannot be confirmed to be main's must not be applied. dev.finding 5170b3f9: a worker checkout
two merges behind GitHub reverted the store's charters to its own stale content, every fifteen
minutes, until advanced. `apply_release_charters_activity` now refuses before it loads or touches
anything when `worker_revision.check_checkout_freshness` cannot confirm HEAD equals `TRACKING_REF`
(`origin/main` -- the checkout's actual upstream, kept current by `worker_checkout.advance`'s
clone and the drift tick, not local `main`, which a hand advance moves without a fetch); see
`charter_tree_refusal`.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

import dispatch  # noqa: E402
import worker_revision  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT = _DISPATCHER_ROOT.parents[1]
RELEASE_LOAD_PATH = REPO_ROOT / "scripts" / "release-load.py"

#: The checkout's remote-tracking ref, not local `main`: `worker_checkout.advance`'s clone sets
#: `origin` to the source mirror, and a hand advance moves local `main` without a fetch, so
#: comparing against local `main` would read a just-advanced-but-unfetched checkout as current.
#: `origin/main` is what the tick actually needs confirmed.
TRACKING_REF = f"origin/{dispatch.DEFAULT_BASE_REF}"


class ReleaseApplyError(RuntimeError):
    """The reconcile plan recorded per-charter errors; propagate so the workflow shows failed."""


def charter_tree_refusal(freshness: "worker_revision.CheckoutFreshness") -> str | None:
    """None when `freshness` confirms HEAD equals `TRACKING_REF`; otherwise a one-sentence reason.

    Pure function of the freshness result -- see module docstring on why the gate is checked
    before anything else runs.
    """
    if freshness.error:
        return (
            f"could not confirm checkout {freshness.revision!r} against {freshness.main_ref}: "
            f"{freshness.error}"
        )
    if not freshness.is_ancestor:
        return (
            f"checkout {freshness.revision!r} is ahead of or diverged from {freshness.main_ref}"
        )
    if freshness.commits_behind:
        return (
            f"checkout {freshness.revision!r} is behind {freshness.main_ref} "
            f"(behind by {freshness.commits_behind})"
        )
    return None


def _load_release_load_module():
    """Import scripts/release-load.py by path -- the filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("release_load", RELEASE_LOAD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_release_charters(
    sub: Any,
    *,
    releases_dir: Path | None = None,
    requirements_dir: Path | None = None,
    release_load: Any = None,
) -> dict[str, Any]:
    """Reconcile docs/releases/*.json into arch.release beads via release-load.py's own plan.

    `release_load` is injectable for tests; defaults to importing scripts/release-load.py by
    path. Raises :class:`ReleaseApplyError` if the plan recorded any per-charter error, so a
    reconcile failure makes the Temporal workflow run itself show failed too.
    """
    release_load = release_load or _load_release_load_module()
    paths = release_load.charter_paths(releases_dir or release_load.RELEASES_DIR)
    items = release_load.load_charters(
        paths, requirements_dir=requirements_dir or release_load.REQUIREMENTS_DIR
    )
    plan = release_load.reconcile(sub, items, apply=True)

    result = {
        "status": "applied" if not plan.errors else "failed",
        "creates": list(plan.creates),
        "updates": list(plan.updates),
        "unchanged": list(plan.unchanged),
        "errors": list(plan.errors),
    }
    if plan.errors:
        raise ReleaseApplyError(
            f"release-load reconcile failed for {len(plan.errors)} charter(s): "
            + "; ".join(plan.errors)
        )
    return result


@activity.defn(name="apply_release_charters")
def apply_release_charters_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    freshness = worker_revision.check_checkout_freshness(
        repo_root=REPO_ROOT, main_ref=TRACKING_REF
    )
    reason = charter_tree_refusal(freshness)
    if reason is not None:
        logger.warning("Skipping release-charter apply: %s", reason)
        return {
            "status": "skipped",
            "reason": reason,
            "revision": freshness.revision,
            "tracking_ref": TRACKING_REF,
            "creates": [],
            "updates": [],
            "unchanged": [],
            "errors": [],
        }
    release_load = _load_release_load_module()
    return apply_release_charters(release_load.Substrate(), release_load=release_load)


ACTIVITIES = [apply_release_charters_activity]
