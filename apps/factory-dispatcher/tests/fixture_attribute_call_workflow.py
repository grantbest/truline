"""Fixture-only workflow for test_schedule_wiring.py's attribute-call coverage.

Lives in its own module, mirroring every real `workflows/*.py` module having
exactly one workflow class, because `literal_activity_names` scans the whole
module's source for `execute_activity` call sites rather than scoping to a
single class -- sharing a module with test_schedule_wiring.py's other fixture
workflows would fold their activity names into this one's result.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow

import activities.cluster_health as cluster_health_activities


@workflow.defn
class FixtureAttributeCallWorkflow:
    """Invokes its activity by attribute reference (`module.activity_fn`)
    rather than by string name -- OPS-84's named parser-bypass case. A scan
    that only recognizes string literals reports zero activity dependencies
    for this workflow, which would make `report_cluster_health` look
    unreachable even though this workflow reaches it every run."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return await workflow.execute_activity(
            cluster_health_activities.report_cluster_health_activity,
            request or {},
        )
