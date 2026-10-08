"""Temporal activities for the factory dispatcher."""

from activities.capacity_pause_response import ACTIVITIES as CAPACITY_PAUSE_RESPONSE_ACTIVITIES
from activities.change_apply import ACTIVITIES as CHANGE_APPLY_ACTIVITIES
from activities.cluster_health import ACTIVITIES as CLUSTER_HEALTH_ACTIVITIES
from activities.dispatch_steps import ACTIVITIES as DISPATCH_ACTIVITIES
from activities.doctrine_registry_view import ACTIVITIES as DOCTRINE_REGISTRY_VIEW_ACTIVITIES
from activities.doctrine_staleness import report_doctrine_staleness_activity
from activities.ea_apply import ACTIVITIES as EA_APPLY_ACTIVITIES
from activities.ea_coverage_apply import ACTIVITIES as EA_COVERAGE_APPLY_ACTIVITIES
from activities.ea_observation import observe_ea_model_activity
from activities.knowledge_ingestion import ACTIVITIES as KNOWLEDGE_INGESTION_ACTIVITIES
from activities.merge_on_verdict import ACTIVITIES as MERGE_ON_VERDICT_ACTIVITIES
from activities.release_apply import ACTIVITIES as RELEASE_APPLY_ACTIVITIES
from activities.requirements_apply import ACTIVITIES as REQUIREMENTS_APPLY_ACTIVITIES
from activities.release_status import ACTIVITIES as RELEASE_STATUS_ACTIVITIES
from activities.spec_record_reconcile import ACTIVITIES as SPEC_RECORD_RECONCILE_ACTIVITIES
from activities.staleness_report import report_verdict_staleness_activity
from activities.worker_revision_drift import ACTIVITIES as WORKER_REVISION_DRIFT_ACTIVITIES
from activities.workflow_run_health import ACTIVITIES as WORKFLOW_RUN_HEALTH_ACTIVITIES
# Imported last, deliberately: deployed_revision_drift.py imports failure_diagnosis ->
# worker_revision -> dispatch -> (mid-module) activities.dispatch_steps. DISPATCH_ACTIVITIES
# above already forces that whole chain fully loaded before this line runs; importing this
# earlier in the list risks the circular import deployed_revision.py's own module docstring
# explains (.factory/design.md) whenever this activity module is the first thing imported.
from activities.deployed_revision_drift import ACTIVITIES as DEPLOYED_REVISION_DRIFT_ACTIVITIES

ACTIVITIES = [
    *DISPATCH_ACTIVITIES,
    report_verdict_staleness_activity,
    report_doctrine_staleness_activity,
    *DOCTRINE_REGISTRY_VIEW_ACTIVITIES,
    *SPEC_RECORD_RECONCILE_ACTIVITIES,
    observe_ea_model_activity,
    *KNOWLEDGE_INGESTION_ACTIVITIES,
    *EA_APPLY_ACTIVITIES,
    *EA_COVERAGE_APPLY_ACTIVITIES,
    *RELEASE_APPLY_ACTIVITIES,
    *REQUIREMENTS_APPLY_ACTIVITIES,
    *RELEASE_STATUS_ACTIVITIES,
    *WORKER_REVISION_DRIFT_ACTIVITIES,
    *CAPACITY_PAUSE_RESPONSE_ACTIVITIES,
    *CLUSTER_HEALTH_ACTIVITIES,
    *CHANGE_APPLY_ACTIVITIES,
    *DEPLOYED_REVISION_DRIFT_ACTIVITIES,
    *WORKFLOW_RUN_HEALTH_ACTIVITIES,
    *MERGE_ON_VERDICT_ACTIVITIES,
]

__all__ = ["ACTIVITIES"]
