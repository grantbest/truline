"""Temporal workflow for the nightly doctrine principle staleness report (F-DCE-5) and,
riding the same nightly, the OPS-75 principles-registry check-view mechanization
(PRIN-016/PRIN-015), the spec-record reconcile schedule (PRIN-005: reuses
scanner.scan_spec_record/scan_ahead_of_gate_filings, the OPS-14 class -- a reconciler
that already exists and runs from nowhere), the OPS-109 deployed-revision-drift check
(activities/deployed_revision_drift.py), and the S53-4 EA coverage report
(activities/ea_coverage_apply.py, PC-ASR-002): all five activities need exactly the same
substrate credentials, so this workflow runs them in sequence rather than each needing
its own schedule and worker wiring -- the same reasoning that put the third leg here
rather than in a dedicated schedule, applied once more rather than reinvented."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(minutes=10)
NO_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class DoctrineStalenessReportWorkflow:
    """Durable owner for one read-only doctrine staleness report pass, the
    read-only principles-registry check-view pass, the read-only spec-record
    reconcile check, the read-only deployed-revision-drift check, and the
    read-only EA coverage report -- all five ride the same nightly."""

    @workflow.run
    async def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        # Every leg runs -- raising its own declared alerts -- even when an
        # earlier leg fails (OPS-75 AC2, now general to five legs). A
        # substrate outage failing this workflow on its first await would
        # otherwise silence the later legs' unreachable alerts in exactly the
        # outage they exist to report. The first failure stays visible: it is
        # re-raised once every leg has had its turn.
        results: dict[str, Any] = {}
        first_error: ActivityError | None = None

        try:
            results["doctrine_staleness"] = await workflow.execute_activity(
                "report_doctrine_staleness",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = exc

        try:
            results["principles_registry_view"] = await workflow.execute_activity(
                "check_principles_registry_view",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = first_error or exc

        try:
            results["spec_record"] = await workflow.execute_activity(
                "check_spec_record",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = first_error or exc

        try:
            results["deployed_revision_drift"] = await workflow.execute_activity(
                "check_deployed_revision_drift",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = first_error or exc

        try:
            results["ea_coverage"] = await workflow.execute_activity(
                "report_ea_coverage",
                request or {},
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=NO_ACTIVITY_RETRY,
            )
        except ActivityError as exc:
            first_error = first_error or exc

        if first_error is not None:
            raise first_error
        return results
