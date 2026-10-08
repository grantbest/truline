#!/usr/bin/env python3
"""Operator-facing, read-only verbs over the declared queue order.

R26.12 S55 rail: the order can be read and switched off before it drives a
pick. ``explain-order`` builds queue_order.rank_or_fifo's inputs from the
same BeadStore GET methods the dispatcher's tick already uses (pending
tasks, their notes, arch.release beads, the delivers edges that resolve a
task's release, and each pending task's event log for claimable_since), and
prints the order, the held list and every position's explanation. It writes
nothing. pick_task does not call anything in this file yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

APP = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import guards  # noqa: E402
import queue_order  # noqa: E402
import schedule_status  # noqa: E402
from substrate import default_store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "docs" / "releases" / "policy" / "health-policy.json"

#: The BeadStore GET methods explain-order may call. Used only to document
#: intent beside _read_inputs below -- FakeStore in the test suite has no
#: write method at all, which is what actually proves this.
_READ_METHODS = ("list_tasks", "list_notes", "list_beads", "list_links", "list_events")


def _parse_release_date(value: Any) -> datetime | None:
    """A release's ``opened_at``/``target_at``: date-only ('2026-10-01') or
    a full ISO-8601 timestamp, same convention as the committed fixtures."""
    if value is None:
        return None
    text = str(value)
    if "T" not in text:
        text = text + "T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _parse_iso_now(value: str) -> datetime:
    text = value
    if "T" not in text:
        text = text + "T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    rendered = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        rendered += f".{utc.microsecond:06d}"
    return rendered + "Z"


def _git_blob_sha(data: bytes) -> str:
    """The git blob sha of ``data``, computed in process (no ``git``
    subprocess) -- what ``git hash-object`` would print for this content."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def load_policy(policy_path: Path) -> queue_order.RankPolicy:
    """Raises OSError or ValueError on anything short of a valid policy --
    callers must not substitute a default (PRIN-015)."""
    data = policy_path.read_bytes()
    mapping = json.loads(data)
    if not isinstance(mapping, dict):
        raise ValueError(f"policy file must contain a JSON object, got {type(mapping).__name__}")
    return queue_order.RankPolicy.from_mapping(mapping, policy_revision=_git_blob_sha(data))


def _read_inputs(store: Any) -> dict[str, Any]:
    """``rank_or_fifo``'s keyword inputs, read from ``store`` through GET
    calls only -- the same calls dispatch.resolve_task_release_states and
    the tick's note reads already make, plus one list_events per pending
    bead for claimable_since (schedule_status._became_claimable_at)."""
    all_tasks = list(store.list_tasks())
    pending = [task for task in all_tasks if task.get("state") == "pending"]

    notes_by_id = {task["id"]: store.list_notes(task["id"]) for task in pending}

    releases = store.list_beads("arch", "release")
    releases_by_id = {release["id"]: release for release in releases}

    time_box_by_release_ref: dict[str, queue_order.TimeBox] = {}
    for release in releases:
        content = release.get("content") or {}
        ref = str(content.get("ref") or release["id"])
        time_box_by_release_ref[ref] = queue_order.TimeBox(
            opened_at=_parse_release_date(content.get("opened_at")),
            target_at=_parse_release_date(content.get("target_at")),
        )

    release_by_task_id: dict[str, guards.ReleaseState] = {}
    for task in pending:
        task_id = task["id"]
        for link in store.list_links(task_id, direction="outgoing", link_type="delivers"):
            release = releases_by_id.get(link.get("target_id"))
            if release is None:
                continue
            release_content = release.get("content") or {}
            release_by_task_id[task_id] = guards.ReleaseState(
                ref=str(release_content.get("ref") or release["id"]),
                state=str(release.get("state") or ""),
            )
            break

    claimable_since_by_task_id = {
        task["id"]: schedule_status._became_claimable_at(store.list_events(task["id"])) for task in pending
    }

    return dict(
        tasks=all_tasks,
        notes_by_id=notes_by_id,
        release_by_task_id=release_by_task_id,
        time_box_by_release_ref=time_box_by_release_ref,
        claimable_since_by_task_id=claimable_since_by_task_id,
        claimable_since_source="events",
    )


def compute_queue_order(
    store: Any,
    *,
    now: datetime,
    environ: Mapping[str, str],
    policy: queue_order.RankPolicy,
) -> queue_order.QueueOrder:
    """Read-only: builds rank_or_fifo's inputs from ``store`` and the
    FACTORY_QUEUE_ORDER kill switch read from ``environ``."""
    inputs = _read_inputs(store)
    mode = queue_order.order_mode(environ)
    return queue_order.rank_or_fifo(mode, inputs["tasks"], policy=policy, now=now, **{
        key: value for key, value in inputs.items() if key != "tasks"
    })


def render_table(order: queue_order.QueueOrder) -> str:
    headers = (
        "position",
        "task_id",
        "priority",
        "effective_priority",
        "urgency",
        "release_ref",
        "target_at",
        "class_source",
        "waiting_since",
        "age_days",
        "aged_steps",
    )
    rows = [
        (
            str(position.position),
            position.task_id,
            str(position.priority),
            str(position.effective_priority),
            position.urgency,
            position.release_ref or "-",
            _iso_z(position.release_target_at) if position.release_target_at is not None else "-",
            position.class_source,
            _iso_z(position.waiting_since),
            str(position.age_days),
            str(position.aged_steps),
        )
        for position in order.ranked
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [" | ".join(header.ljust(width) for header, width in zip(headers, widths))]
    for row in rows:
        lines.append(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return "\n".join(lines)


def render_held(order: queue_order.QueueOrder) -> str:
    lines = ["Held:"]
    for position in order.held:
        lines.append(f"  {position.task_id}: {'; '.join(position.hold_reasons)}")
    return "\n".join(lines)


def render_explanations(order: queue_order.QueueOrder) -> str:
    lines = ["Explanations:"]
    for position in (*order.ranked, *order.held):
        lines.append(f"  {position.task_id}: {position.explanation}")
    return "\n".join(lines)


def render_explain_order(order: queue_order.QueueOrder) -> str:
    return "\n\n".join((render_table(order), render_held(order), render_explanations(order)))


def cmd_explain_order(args: argparse.Namespace, *, store: Any = None, policy_path: Path = POLICY_PATH) -> int:
    try:
        policy = load_policy(policy_path)
    except (OSError, ValueError):
        print("unknown: policy_unreadable")
        return 2

    now = _parse_iso_now(args.now) if args.now else datetime.now(timezone.utc)
    active_store = store if store is not None else default_store()
    order = compute_queue_order(active_store, now=now, environ=os.environ, policy=policy)

    if args.json:
        print(order.to_json())
        return 0

    print(render_explain_order(order))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="operator_verbs.py")
    verbs = parser.add_subparsers(dest="verb", required=True)

    explain_order = verbs.add_parser("explain-order", help="print the declared queue order, read-only")
    explain_order.add_argument("--json", action="store_true", help="print QueueOrder.to_json() instead of the table")
    explain_order.add_argument("--now", default=None, help="ISO-8601 timestamp to compute the order as of")

    return parser


def main(argv: list[str] | None = None, *, store: Any = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.verb == "explain-order":
        return cmd_explain_order(args, store=store)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
