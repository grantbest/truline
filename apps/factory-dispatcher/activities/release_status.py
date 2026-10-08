"""Nightly release status observations (read-only measurement over release charters).

Direct clone of activities/staleness_report.py's shape: reads scripts/release-status.py's own
report machinery (reusing its `gather_live_data`/`build_release_report` unchanged -- the same
functions scripts/release-status.py itself runs) and writes one arch.observation bead per release
not in state released.

Read-only with respect to release lifecycle. This module has no call available to it that patches
an arch.release bead's state -- it reads releases and tasks (`SubstrateReader`, reused from
scripts/release-status.py) and creates observations (`ObservationStore`, reused from
activities/staleness_report.py). Lifecycle is an operator act: `scripts/release-load.py`'s own
docstring states the split (git authoritative for structure, the substrate's state machine
authoritative for lifecycle), and this report measures against that split without ever writing
into it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

from activities.staleness_report import SubstrateObservationStore  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]
RELEASE_STATUS_PATH = REPO_ROOT / "scripts" / "release-status.py"
CREATED_BY = "factory-dispatcher/release-status"
# "factory-dispatcher/release-status" is enrolled in
# apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119,
# #822) -- declaring "derived" here depends on that enrolment being DEPLOYED,
# not just merged (unenrolled, this would be a 409). See .factory/design.md.
SOURCE_CLASS = "derived"
OBSERVATION_KIND = "release_status"
RELEASED_STATE = "released"


class ObservationStore(Protocol):
    def observation_exists(self, ref: str) -> bool: ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _load_release_status_module():
    """Import scripts/release-status.py by path -- the filename is not a valid module name.

    Registered in `sys.modules` before `exec_module` runs: its dataclasses use
    `from __future__ import annotations`, and resolving those string annotations
    looks the defining module up by name in `sys.modules` -- unregistered, that
    lookup returns `None` and dataclass field processing raises.
    """
    spec = importlib.util.spec_from_file_location("release_status", RELEASE_STATUS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release_states(reader: Any) -> dict[str, str]:
    """ref -> state for every arch.release bead the substrate already holds.

    A charter with no mirrored bead yet (release-load has not run, or raced this report) is
    simply absent from this map -- callers must treat a missing ref as not-released, since
    "released" is a state that must be observed, never assumed.
    """
    states: dict[str, str] = {}
    for bead in reader.list_beads("arch", "release", limit=1000):
        ref = (bead.get("content") or {}).get("ref")
        if ref:
            states[ref] = bead.get("state") or ""
    return states


def run_release_status_report(
    reader: Any,
    store: ObservationStore,
    *,
    release_load: Any,
    release_status: Any,
    releases_dir: Path | None = None,
    requirements_dir: Path | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Report every release not in state released; write at most one observation per release."""
    observed_at = _iso(now_fn())
    observed_day = observed_at[:10]

    charter_items = release_load.load_charters(
        release_load.charter_paths(releases_dir or release_load.RELEASES_DIR),
        requirements_dir=requirements_dir or release_load.REQUIREMENTS_DIR,
    )
    tasks, delivers, conformances = release_status.gather_live_data(
        reader, [item.content for item in charter_items]
    )
    states = _release_states(reader)

    checked = 0
    reported = 0
    skipped_released = 0
    created = 0
    skipped_existing = 0
    for item in charter_items:
        checked += 1
        state = states.get(item.ref, "")
        if state == RELEASED_STATE:
            skipped_released += 1
            continue

        report = release_status.build_release_report(item.content, tasks, delivers, conformances)
        payload = _observation_payload(report, state, observed_at, observed_day)
        emitted = _emit_once(store, payload)
        created += int(emitted)
        skipped_existing += int(not emitted)
        reported += 1

    return {
        "status": "reported",
        "checked": checked,
        "reported": reported,
        "skipped_released": skipped_released,
        "observations_created": created,
        "observations_skipped_existing": skipped_existing,
    }


@activity.defn(name="report_release_status")
def report_release_status_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    release_status = _load_release_status_module()
    release_load = release_status._load_release_load_module()
    reader = release_status.GateSubstrateReader(
        os.environ.get("SUBSTRATE_URL"), os.environ.get("SUBSTRATE_API_KEY")
    )
    return run_release_status_report(
        reader,
        SubstrateObservationStore(),
        release_load=release_load,
        release_status=release_status,
    )


def _observation_payload(
    report: Any, state: str, observed_at: str, observed_day: str
) -> dict[str, Any]:
    ref = ".".join(
        (
            "obs",
            "release-status",
            _ref_part(observed_day),
            _ref_part(report.ref),
        )
    )
    balance = [
        {
            "work_class": entry.work_class,
            "declared_pct": entry.declared_pct,
            "actual_count": entry.actual_count,
            "actual_pct": entry.actual_pct,
            "absent": entry.absent,
        }
        for entry in report.balance
    ]
    outcomes = [
        {
            "id": outcome.id,
            "work_class": outcome.work_class,
            "task_count": outcome.task_count,
            "tasks_by_state": outcome.tasks_by_state,
        }
        for outcome in report.outcomes
    ]
    criteria = [
        {"ref": status.ref, "stale": status.stale, "changed": status.changed}
        for status in report.criteria
    ]
    return {
        "namespace": "arch",
        "type": "observation",
        "state": "active",
        "trust_tier": "system",
        "created_by": CREATED_BY,
        "content": {
            "ref": ref,
            "source_class": SOURCE_CLASS,
            "observed_at": observed_at,
            "workload": {
                "cluster": "repository",
                "namespace": "releases",
                "kind": "ArchRelease",
                "name": report.ref,
            },
        },
        "context": {
            "observation_kind": OBSERVATION_KIND,
            "release_ref": report.ref,
            "release_state": state,
            "unclassified_delivering": list(report.unclassified_delivering),
            "balance": balance,
            "outcomes": outcomes,
            "criteria": criteria,
        },
    }


def _emit_once(store: ObservationStore, payload: dict[str, Any]) -> bool:
    ref = str((payload.get("content") or {}).get("ref") or "")
    if store.observation_exists(ref):
        return False
    store.create_observation(payload)
    return True


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"


ACTIVITIES = [report_release_status_activity]
