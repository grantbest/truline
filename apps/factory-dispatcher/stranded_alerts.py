"""Announce the two stranded shapes the drain already computes.

Before this, a `doing` bead whose owner cannot be shown live was only
visible to a human who ran `--report-stuck`, and a `review` bead with no
`pr_url` was only ever printed as `missing_pr_url` by `reconcile_review_tasks`
to a log nobody tails. Both shapes were already computed correctly; nothing
told anyone. Each stranded `doing` bead silently removes one task's worth of
factory throughput, and a `review` bead with no `pr_url` can never be
reconciled to `done` on its own.

Detection here is not reimplemented: the caller (`dispatch.reconcile_review_tasks`)
reuses `dispatch.stuck_doing_tasks` and `guards.has_claim_note` — the same two
checks `--report-stuck` already runs — and the same `pr_url` check
`reconcile_review_tasks` already prints `missing_pr_url` from. This module
only adds the missing announcement.

Dedup reuses failure_diagnosis.py's mechanism verbatim (per-`kind` AlertPolicy
state in a host-local JSON file) rather than a second alert-state backend —
that module's `dev_task_alert_policy` already keys state by an arbitrary
`kind` string, so a second alert family shares the file instead of forking
the suppression logic.

Nothing here releases, rebinds, requeues or dispatches anything. The operator
commands (`--release-stranded`, `--bind-pr`, `--requeue`) remain the only way
any of this state changes.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_HUB_SRC = str(_REPO_ROOT / "apps" / "mcp-hub" / "src")
if _MCP_HUB_SRC not in sys.path:
    sys.path.insert(0, _MCP_HUB_SRC)

from tools import notify  # noqa: E402

import failure_diagnosis  # noqa: E402

STRANDED_DOING_ALERT_ID = "factory_dispatcher.stranded_doing_task"
REVIEW_MISSING_PR_URL_ALERT_ID = "factory_dispatcher.review_missing_pr_url"


def stranded_alert_policy(post: "notify.PostAlert | None" = None) -> "notify.AlertPolicy":
    """The same dedup mechanism and state file failure_diagnosis.py established."""
    return failure_diagnosis.dev_task_alert_policy(post)


def _episode_key(task: dict[str, Any]) -> str:
    """A stable id for "this stint in the current state".

    `updated_at` changes whenever the bead is reclaimed into `doing` (or
    re-enters `review`), so a fingerprint built from it suppresses repeated
    observation of the same episode but re-alerts once the bead recovers and
    is later found stranded again under a fresh episode.
    """
    return str(task.get("updated_at") or "")


async def _announce_stranded_doing_async(
    bead_id: str,
    title: str,
    age_minutes: int,
    reason: str,
    episode_key: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"dev.task {bead_id} ({title!r}) has been in doing for {age_minutes} "
        f"minute(s) with no live owner shown ({reason})."
    )
    fingerprint = notify.alert_content_fingerprint(f"{bead_id}:{episode_key}")
    return await notify.send_alert(
        policy,
        STRANDED_DOING_ALERT_ID,
        fingerprint,
        content,
        template_values={"bead_id": bead_id, "title": title},
    )


def announce_stranded_doing_task(
    task: dict[str, Any],
    age_minutes: int,
    reason: str,
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a `doing` bead whose owner cannot be shown live.

    Best-effort, matching every other alert path in this app
    (failure_diagnosis.announce_failed_task, cluster_health.notify): a bad
    webhook or a corrupt local alert-state file must never turn a read-only
    detection pass into a dispatch failure.
    """
    bead_id = str(task.get("id") or "")
    title = str((task.get("content") or {}).get("title") or "(untitled)")
    episode_key = _episode_key(task)
    try:
        return asyncio.run(
            _announce_stranded_doing_async(
                bead_id,
                title,
                age_minutes,
                reason,
                episode_key,
                policy or stranded_alert_policy(),
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce stranded doing dev.task %s; detection is unaffected",
            bead_id,
        )
        return False


async def _announce_review_missing_pr_url_async(
    bead_id: str,
    title: str,
    episode_key: str,
    policy: "notify.AlertPolicy",
) -> bool:
    content = (
        f"dev.task {bead_id} ({title!r}) is in review with no pr_url recorded; "
        "reconcile cannot settle it to done until one is bound."
    )
    fingerprint = notify.alert_content_fingerprint(f"{bead_id}:{episode_key}")
    return await notify.send_alert(
        policy,
        REVIEW_MISSING_PR_URL_ALERT_ID,
        fingerprint,
        content,
        template_values={"bead_id": bead_id, "title": title},
    )


def announce_review_missing_pr_url(
    task: dict[str, Any],
    *,
    policy: "notify.AlertPolicy | None" = None,
) -> bool:
    """Raise the declared alert for a `review` bead with no `pr_url` recorded."""
    bead_id = str(task.get("id") or "")
    title = str((task.get("content") or {}).get("title") or "(untitled)")
    episode_key = _episode_key(task)
    try:
        return asyncio.run(
            _announce_review_missing_pr_url_async(
                bead_id, title, episode_key, policy or stranded_alert_policy()
            )
        )
    except Exception:  # noqa: BLE001 - alerting must never crash the dispatcher.
        logger.exception(
            "could not announce missing pr_url for dev.task %s; detection is unaffected",
            bead_id,
        )
        return False
