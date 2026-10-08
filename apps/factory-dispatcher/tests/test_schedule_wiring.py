"""OPS-71 generalized, then OPS-73/OPS-84 closed the reverse direction: a
schedule-to-worker-to-ACTIVITIES wiring gap in either direction is invisible
until its report never arrives.

OPS-71/#690 was the forward instance: a Temporal Schedule named
`DoctrineStalenessReportWorkflow`, but `worker.build_worker`'s `workflows=[...]`
list did not include it, so the schedule could fire forever without ever
starting a run. Nothing failed loudly; a citations sweep found it, not a
control. OPS-73 shipped the forward check but its title promised both
directions while its acceptance only covered schedule-to-worker; OPS-84 named
the gap: an activity registered in `ACTIVITIES` whose only calling workflow is
later deleted becomes a silent orphan -- dead code Temporal will happily keep
advertising as runnable -- and nothing catches it.

This module makes the whole class of gap a test failure instead of an audit
finding, in both directions:

- Every workflow a `register_*_schedule(...)` call in `worker.main` names must be in
  `build_worker`'s registered workflow set (schedule -> worker).
- Every such workflow's activity dependencies must be in `ACTIVITIES` (worker -> ACTIVITIES,
  forward completeness).
- Every worker-registered workflow that is not scheduled must appear in the declared
  `EXEMPTIONS` list below, with a non-blank reason.
- Every activity registered in `ACTIVITIES` must be reachable from some
  worker-registered workflow's activity dependencies, or appear in the declared
  `ACTIVITY_EXEMPTIONS` list below with a non-blank reason (ACTIVITIES -> worker,
  reverse completeness -- OPS-84's line item).
- The activity-dependency scan resolves both literal string names
  (`execute_activity("name", ...)`) and attribute-call references
  (`execute_activity(some_module.some_activity_fn, ...)`), so a workflow that
  calls its activity by attribute rather than by string is not misread as
  having no dependencies at all (OPS-84's named parser-bypass line item; see
  `fixture_attribute_call_workflow.py`).

The ground truth for "what does the schedule reconciliation register" is read
statically off `worker.main`'s real source (`ast`), not a second hand-maintained
table -- a shadow table only proves agreement with itself, which is exactly how the
schedule and the registration drifted apart in OPS-71. See .factory/design.md.

No Temporal server, no substrate, no network: `scheduled_workflow_classes` parses
source, `worker_registered_workflow_classes` builds a real `Worker` against a faked
bridge/client (the same technique test_worker_registration.py uses), and
`check_schedule_wiring` is a pure function exercised directly by the fixture tests.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import temporalio.bridge.worker as bridge_worker  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.service import ConnectConfig, _BridgeServiceClient  # noqa: E402
from temporalio import workflow as temporal_workflow  # noqa: E402

import worker  # noqa: E402
from activities import ACTIVITIES  # noqa: E402
from activities.dispatch_steps import ACTIVITY_FUNCTIONS as DISPATCH_STEP_FUNCTIONS  # noqa: E402
from workflows.dispatch_task import DispatchTaskWorkflow  # noqa: E402
from workflows.knowledge_ingestion import KnowledgeIngestionWorkflow  # noqa: E402
from workflows.merge_on_verdict import MergeOnVerdictWorkflow  # noqa: E402
from fixture_attribute_call_workflow import FixtureAttributeCallWorkflow  # noqa: E402


# ---------------------------------------------------------------------------
# Declared exemptions: worker-registered workflows deliberately not scheduled.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exemption:
    workflow: type
    reason: str


EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption(
        workflow=KnowledgeIngestionWorkflow,
        reason=(
            "Deliberately unscheduled, not an oversight: F-DCE-4 files an "
            "extraction task per registered knowledge source on demand, not on "
            "a recurring cadence -- "
            "docs/plans/2026-08-17-sprints-23-26-doctrine-context-engine.md "
            "says 'the feature is done when the demonstration ran, not when "
            "the workflow registers' and names a schedule for it as "
            "namespace-topology-adjacent, requiring an EA amendment before it "
            "lands, not code first."
        ),
    ),
    Exemption(
        workflow=MergeOnVerdictWorkflow,
        reason=(
            "Deliberately unscheduled: R26.09/O-2's merge-by-verdict is an "
            "attended, request-triggered capability -- a person on the phone "
            "asks for one merge decision on one PR -- not a recurring "
            "reconciler. It ships dark (access_auth.EXACT_MATCH_SCOPES's "
            "factory.merge, granted to nobody, plus "
            "FACTORY_MERGE_CAPABILITY_ENABLED) until a decision record grants "
            "it; see .factory/design.md."
        ),
    ),
)


@dataclass(frozen=True)
class ActivityExemption:
    name: str
    reason: str


# Activities registered in ACTIVITIES that are deliberately reachable from no
# worker-registered workflow. Empty at HEAD: OPS-84's acceptance criterion is
# that the current ACTIVITIES set is whole, so nothing needs an exemption yet
# -- this exists as the escape hatch the reverse check requires, not as a
# resting place for orphans it would otherwise catch.
ACTIVITY_EXEMPTIONS: tuple[ActivityExemption, ...] = ()

# Workflows whose activity dependency is resolved dynamically at runtime rather
# than as a literal `workflow.execute_activity("name", ...)` in the workflow
# module. DispatchTaskWorkflow proxies one activity name per dispatch step
# through workflow_core.run_dispatch_sequence; the set of names it can call is
# exactly activities.dispatch_steps.ACTIVITY_FUNCTIONS -- the same table
# activities/__init__.py builds ACTIVITIES from, and which
# test_dispatch_step_parity.py already exercises end-to-end live.
DYNAMIC_ACTIVITY_DEPENDENCIES: dict[type, frozenset[str]] = {
    DispatchTaskWorkflow: frozenset(DISPATCH_STEP_FUNCTIONS),
}


# ---------------------------------------------------------------------------
# Static extraction: what does worker.main actually register/schedule?
# ---------------------------------------------------------------------------


def scheduled_workflow_classes(module) -> dict[str, type]:
    """Read every `register_*_schedule(client, temporal, Workflow.run, ...)`
    call in `module.main`'s real source, resolved against `module`'s own
    namespace.

    Raises AssertionError if a `register_*_schedule` call does not match the
    shape this parses -- a check that silently skipped an unrecognized call
    would report a whole registration set without having looked (PRIN-008).
    """
    tree = ast.parse(inspect.getsource(module.main))
    found: dict[str, type] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if not (name.startswith("register_") and name.endswith("_schedule")):
            continue
        if len(node.args) < 3:
            raise AssertionError(
                f"{name}(...) in {module.__name__}.main does not pass a third "
                "positional argument; scheduled_workflow_classes expects "
                "(client, temporal_types, WorkflowClass.run, ...) and cannot "
                "verify this call site without it."
            )
        run_ref = node.args[2]
        if not (
            isinstance(run_ref, ast.Attribute)
            and run_ref.attr == "run"
            and isinstance(run_ref.value, ast.Name)
        ):
            raise AssertionError(
                f"{name}(...)'s third argument in {module.__name__}.main is not "
                f"a plain `WorkflowClass.run` reference this check can resolve "
                f"statically: {ast.dump(run_ref)}"
            )
        class_name = run_ref.value.id
        if not hasattr(module, class_name):
            raise AssertionError(
                f"{name}(...) in {module.__name__}.main references "
                f"{class_name}.run, but {module.__name__} has no such name "
                "imported at module scope."
            )
        found[name] = getattr(module, class_name)
    return found


class _FakeBridgeWorker:
    def initiate_shutdown(self):
        pass

    async def finalize_shutdown(self):
        pass


def worker_registered_workflow_classes(monkeypatch) -> list[type]:
    """Build the real `worker.build_worker` Worker against a faked bridge/client
    (test_worker_registration.py's technique) and read back the workflow classes
    it actually registers -- the real receiving end of the schedules, not a
    restated list of them.
    """
    monkeypatch.setattr(bridge_worker.Worker, "create", lambda *args: _FakeBridgeWorker())

    service_client = _BridgeServiceClient(ConnectConfig("temporal-test.invalid:7233"))
    service_client._bridge_client = object()
    client = Client(service_client, namespace="dev")

    async def construct_worker():
        activity_executor = worker.new_activity_executor()
        try:
            return worker.build_worker(client, activity_executor)
        finally:
            activity_executor.shutdown(wait=True, cancel_futures=True)

    temporal_worker = asyncio.run(construct_worker())
    return list(temporal_worker._config["workflows"])


_UNRESOLVED = object()


def _resolve_module_attribute_path(node: ast.expr, module) -> Any:
    """Statically resolve a `name` or `attr.chain.of.names` expression against
    `module`'s own namespace (its imports and module-level bindings), the same
    way `scheduled_workflow_classes` resolves a bare class name -- never by
    executing the expression. Returns `_UNRESOLVED` if any segment is not a
    real attribute on the object resolved so far, so a genuinely dynamic
    reference (a local variable, a computed value) falls through rather than
    being guessed at.
    """
    if isinstance(node, ast.Name):
        return getattr(module, node.id, _UNRESOLVED)
    if isinstance(node, ast.Attribute):
        base = _resolve_module_attribute_path(node.value, module)
        if base is _UNRESOLVED:
            return _UNRESOLVED
        return getattr(base, node.attr, _UNRESOLVED)
    return _UNRESOLVED


def literal_activity_names(workflow_class: type) -> frozenset[str]:
    """Read every `workflow.execute_activity(...)` call's activity argument in
    the module that defines `workflow_class`, whether it names the activity by
    string literal (`"report_x"`) or by attribute-call reference to the
    decorated activity function itself (`some_module.report_x_activity`) --
    OPS-84's named line item: a scan that only recognized string literals
    would read a workflow that calls its activity by attribute as having no
    dependency at all, making that activity look unreachable from every
    workflow that in fact reaches it.

    Raises AssertionError if a call resolves its activity name some other,
    genuinely dynamic way (neither a string literal nor a statically-resolvable
    attribute path) and the class has no entry in DYNAMIC_ACTIVITY_DEPENDENCIES
    -- silently reporting an empty dependency set for a workflow that calls an
    activity would defeat the whole check.
    """
    module = sys.modules[workflow_class.__module__]
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute_activity"
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
            continue
        if isinstance(first, (ast.Name, ast.Attribute)):
            resolved = _resolve_module_attribute_path(first, module)
            definition = getattr(resolved, "__temporal_activity_definition", None)
            if definition is not None:
                names.add(definition.name)
                continue
        if workflow_class not in DYNAMIC_ACTIVITY_DEPENDENCIES:
            raise AssertionError(
                f"{workflow_class.__name__} calls execute_activity with a "
                "non-literal, non-statically-resolvable activity reference and "
                "has no entry in DYNAMIC_ACTIVITY_DEPENDENCIES to declare its "
                "real dependency set."
            )
    return frozenset(names)


def activity_dependencies(workflow_class: type) -> frozenset[str]:
    if workflow_class in DYNAMIC_ACTIVITY_DEPENDENCIES:
        return DYNAMIC_ACTIVITY_DEPENDENCIES[workflow_class]
    return literal_activity_names(workflow_class)


def registered_activity_names() -> frozenset[str]:
    return frozenset(
        fn.__temporal_activity_definition.name for fn in ACTIVITIES
    )


# ---------------------------------------------------------------------------
# The pure invariant check.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiringViolation:
    kind: str
    detail: str


def check_schedule_wiring(
    *,
    scheduled: dict[str, type],
    worker_workflows: list[type],
    exemptions: tuple[Exemption, ...],
    activity_deps: dict[type, frozenset[str]],
    registered_activities: frozenset[str],
    activity_exemptions: tuple[ActivityExemption, ...] = (),
) -> list[WiringViolation]:
    """Judge the wiring whole in both directions: every scheduled workflow
    reaches the worker and every activity dependency it declares is
    registered (schedule -> worker -> ACTIVITIES, OPS-71/#690's class), and
    every registered activity is reachable from some worker-registered
    workflow's declared dependencies, or carries a reasoned exemption
    (ACTIVITIES -> worker, OPS-84's line item).
    """
    violations: list[WiringViolation] = []
    worker_set = set(worker_workflows)

    for registration_name, wf in scheduled.items():
        if wf not in worker_set:
            violations.append(
                WiringViolation(
                    "unregistered-scheduled-workflow",
                    f"{registration_name} schedules {wf.__name__}, which is "
                    "not in build_worker's registered workflows list -- the "
                    "schedule can fire and can never start a run.",
                )
            )
        deps = activity_deps.get(wf, frozenset())
        missing = deps - registered_activities
        if missing:
            violations.append(
                WiringViolation(
                    "unregistered-activity-dependency",
                    f"{wf.__name__} depends on activities {sorted(missing)}, "
                    "which are not in ACTIVITIES.",
                )
            )

    valid_exemptions = [e for e in exemptions if e.reason.strip()]
    for exemption in exemptions:
        if not exemption.reason.strip():
            violations.append(
                WiringViolation(
                    "empty-exemption-reason",
                    f"{exemption.workflow.__name__} is exempted with a blank "
                    "reason.",
                )
            )

    scheduled_workflows = set(scheduled.values())
    exempted_workflows = {e.workflow for e in valid_exemptions}
    unaccounted = worker_set - scheduled_workflows - exempted_workflows
    if unaccounted:
        violations.append(
            WiringViolation(
                "unaccounted-worker-workflow",
                "registered on the worker but neither scheduled nor validly "
                f"exempted: {sorted(w.__name__ for w in unaccounted)}",
            )
        )

    # Reverse direction (OPS-84): every registered activity must be reachable
    # from some worker-registered workflow's declared activity dependencies,
    # or carry a reasoned exemption. An activity whose only calling workflow
    # was deleted from worker_workflows is exactly the #690 class run backward
    # -- nothing fails loudly, it just never gets invoked again.
    valid_activity_exemptions = [e for e in activity_exemptions if e.reason.strip()]
    for activity_exemption in activity_exemptions:
        if not activity_exemption.reason.strip():
            violations.append(
                WiringViolation(
                    "empty-activity-exemption-reason",
                    f"{activity_exemption.name!r} is exempted from the "
                    "reachability check with a blank reason.",
                )
            )

    reachable_activities: set[str] = set()
    for wf in worker_workflows:
        reachable_activities |= activity_deps.get(wf, frozenset())

    exempted_activity_names = {e.name for e in valid_activity_exemptions}
    unreachable = set(registered_activities) - reachable_activities - exempted_activity_names
    if unreachable:
        violations.append(
            WiringViolation(
                "unreachable-registered-activity",
                "registered in ACTIVITIES but not invoked -- by literal name "
                "or by attribute-call reference -- from any worker-registered "
                "workflow's declared dependencies, and not validly exempted: "
                f"{sorted(unreachable)}",
            )
        )

    # Staleness cuts both ways (the OPS-76 dead-grandfather lesson): an
    # exemption that outlives the condition that justified it is a standing
    # hole nothing else would ever report closed. An activity exemption is
    # dead when it names no registered activity, or when its activity became
    # reachable; a workflow exemption is dead when its workflow left the
    # worker list, or got a schedule after all.
    for activity_exemption in valid_activity_exemptions:
        if activity_exemption.name not in registered_activities:
            violations.append(
                WiringViolation(
                    "stale-activity-exemption",
                    f"{activity_exemption.name!r} is exempted but registered "
                    "in ACTIVITIES under no such name; delete the entry.",
                )
            )
        elif activity_exemption.name in reachable_activities:
            violations.append(
                WiringViolation(
                    "stale-activity-exemption",
                    f"{activity_exemption.name!r} is exempted but reachable "
                    "from a worker-registered workflow; the exemption is dead "
                    "weight -- delete the entry.",
                )
            )
    for exemption in valid_exemptions:
        if exemption.workflow not in worker_set:
            violations.append(
                WiringViolation(
                    "stale-workflow-exemption",
                    f"{exemption.workflow.__name__} is exempted but no longer "
                    "registered on the worker; delete the entry.",
                )
            )
        elif exemption.workflow in scheduled_workflows:
            violations.append(
                WiringViolation(
                    "stale-workflow-exemption",
                    f"{exemption.workflow.__name__} is exempted as unscheduled "
                    "but a schedule names it; the exemption is dead weight -- "
                    "delete the entry.",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# HEAD proof: the current registration set is whole.
# ---------------------------------------------------------------------------


def test_head_schedule_wiring_is_whole(monkeypatch):
    scheduled = scheduled_workflow_classes(worker)
    assert scheduled, "expected at least one register_*_schedule call in worker.main"

    worker_workflows = worker_registered_workflow_classes(monkeypatch)
    registered_activities = registered_activity_names()
    # Computed over every worker-registered workflow, not just the scheduled
    # ones -- a workflow exempted from scheduling (KnowledgeIngestionWorkflow)
    # still reaches real activities, and the reverse check below must count
    # those as reachable too.
    activity_deps = {wf: activity_dependencies(wf) for wf in worker_workflows}

    violations = check_schedule_wiring(
        scheduled=scheduled,
        worker_workflows=worker_workflows,
        exemptions=EXEMPTIONS,
        activity_deps=activity_deps,
        registered_activities=registered_activities,
        activity_exemptions=ACTIVITY_EXEMPTIONS,
    )
    assert violations == []


def test_scheduled_workflow_classes_resolves_known_call_site():
    # Sanity check on the parser itself: OPS-71's exact regression -- the
    # doctrine staleness schedule names DoctrineStalenessReportWorkflow.
    scheduled = scheduled_workflow_classes(worker)
    assert scheduled["register_doctrine_staleness_schedule"] is (
        worker.DoctrineStalenessReportWorkflow
    )


def test_declared_exemptions_have_non_blank_reasons():
    for exemption in EXEMPTIONS:
        assert exemption.reason.strip(), (
            f"{exemption.workflow.__name__}'s EXEMPTIONS entry has a blank "
            "reason"
        )


def test_declared_activity_exemptions_have_non_blank_reasons():
    for activity_exemption in ACTIVITY_EXEMPTIONS:
        assert activity_exemption.reason.strip(), (
            f"{activity_exemption.name!r}'s ACTIVITY_EXEMPTIONS entry has a "
            "blank reason"
        )


def test_literal_activity_names_resolves_attribute_call_form():
    # OPS-84's named parser-bypass line item: FixtureAttributeCallWorkflow
    # invokes its activity as `cluster_health_activities.report_cluster_health_activity`
    # (an attribute-call reference), not as the string "report_cluster_health".
    # A scan that only recognized string literals would return an empty set
    # here, which would make report_cluster_health look unreachable in the
    # reverse check even though this workflow reaches it every run.
    assert literal_activity_names(FixtureAttributeCallWorkflow) == frozenset(
        {"report_cluster_health"}
    )


# ---------------------------------------------------------------------------
# Fixtures: prove the check actually fires, not just that HEAD is clean.
# ---------------------------------------------------------------------------


@temporal_workflow.defn
class _FixtureScheduledButUnregisteredWorkflow:
    @temporal_workflow.run
    async def run(self, request=None):
        return await temporal_workflow.execute_activity("does_not_matter")


@temporal_workflow.defn
class _FixtureDependsOnMissingActivityWorkflow:
    @temporal_workflow.run
    async def run(self, request=None):
        return await temporal_workflow.execute_activity("activity_nobody_registers")


@temporal_workflow.defn
class _FixtureUnaccountedWorkflow:
    @temporal_workflow.run
    async def run(self, request=None):
        return await temporal_workflow.execute_activity("does_not_matter")


def test_schedule_naming_unregistered_workflow_fails():
    violations = check_schedule_wiring(
        scheduled={"register_fixture_schedule": _FixtureScheduledButUnregisteredWorkflow},
        worker_workflows=[],
        exemptions=(),
        activity_deps={
            _FixtureScheduledButUnregisteredWorkflow: frozenset({"does_not_matter"})
        },
        registered_activities=frozenset({"does_not_matter"}),
    )
    kinds = [v.kind for v in violations]
    assert "unregistered-scheduled-workflow" in kinds


def test_workflow_depending_on_unregistered_activity_fails():
    violations = check_schedule_wiring(
        scheduled={"register_fixture_schedule": _FixtureDependsOnMissingActivityWorkflow},
        worker_workflows=[_FixtureDependsOnMissingActivityWorkflow],
        exemptions=(),
        activity_deps={
            _FixtureDependsOnMissingActivityWorkflow: frozenset(
                {"activity_nobody_registers"}
            )
        },
        registered_activities=frozenset({"some_other_activity"}),
    )
    kinds = [v.kind for v in violations]
    assert "unregistered-activity-dependency" in kinds


def test_empty_exemption_reason_fails():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[_FixtureUnaccountedWorkflow],
        exemptions=(Exemption(workflow=_FixtureUnaccountedWorkflow, reason="   "),),
        activity_deps={},
        registered_activities=frozenset(),
    )
    kinds = [v.kind for v in violations]
    assert "empty-exemption-reason" in kinds
    # A blank reason does not count as an exemption either: the workflow must
    # still surface as unaccounted-for, or a blank reason would silently
    # suppress the very check it failed to justify.
    assert "unaccounted-worker-workflow" in kinds


def test_worker_registered_workflow_with_no_schedule_and_no_exemption_fails():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[_FixtureUnaccountedWorkflow],
        exemptions=(),
        activity_deps={},
        registered_activities=frozenset(),
    )
    kinds = [v.kind for v in violations]
    assert "unaccounted-worker-workflow" in kinds


def test_valid_exemption_suppresses_unaccounted_violation():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[_FixtureUnaccountedWorkflow],
        exemptions=(
            Exemption(
                workflow=_FixtureUnaccountedWorkflow,
                reason="fixture: exercises the valid-exemption path",
            ),
        ),
        activity_deps={},
        registered_activities=frozenset(),
    )
    assert violations == []


# ---------------------------------------------------------------------------
# Fixtures: the reverse direction (OPS-84) -- ACTIVITIES -> worker.
# ---------------------------------------------------------------------------


@temporal_workflow.defn
class _FixtureOnlyCallerOfAnActivityWorkflow:
    @temporal_workflow.run
    async def run(self, request=None):
        return await temporal_workflow.execute_activity("only_this_workflow_calls_me")


def test_removing_a_workflow_orphans_its_activity():
    # Simulates exactly OPS-84's scenario: a workflow that used to be the sole
    # caller of an activity is removed from worker_workflows (as if deleted
    # from build_worker's workflows=[...] list), but the activity it alone
    # invoked is still registered in ACTIVITIES. This is the #690 class run
    # backward -- nothing fails loudly, the activity just never runs again.
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[],  # the workflow that used to call this activity is gone
        exemptions=(),
        activity_deps={
            _FixtureOnlyCallerOfAnActivityWorkflow: frozenset(
                {"only_this_workflow_calls_me"}
            )
        },
        registered_activities=frozenset({"only_this_workflow_calls_me"}),
    )
    kinds = [v.kind for v in violations]
    assert "unreachable-registered-activity" in kinds


def test_activity_exemption_with_reason_suppresses_unreachable_violation():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[],
        exemptions=(),
        activity_deps={},
        registered_activities=frozenset({"deliberately_unreachable_activity"}),
        activity_exemptions=(
            ActivityExemption(
                name="deliberately_unreachable_activity",
                reason="fixture: exercises the valid-activity-exemption path",
            ),
        ),
    )
    assert violations == []


def test_empty_activity_exemption_reason_fails():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[],
        exemptions=(),
        activity_deps={},
        registered_activities=frozenset({"orphan_activity"}),
        activity_exemptions=(ActivityExemption(name="orphan_activity", reason="   "),),
    )
    kinds = [v.kind for v in violations]
    assert "empty-activity-exemption-reason" in kinds
    # A blank reason does not count as an exemption either: the activity must
    # still surface as unreachable, or a blank reason would silently suppress
    # the very check it failed to justify.
    assert "unreachable-registered-activity" in kinds


def test_stale_activity_exemption_naming_no_registered_activity_fails():
    """The OPS-76 dead-grandfather lesson: an exemption for an activity that
    no longer exists is a standing hole nothing else would report closed."""
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[],
        exemptions=(),
        activity_deps={},
        registered_activities=frozenset(),
        activity_exemptions=(
            ActivityExemption(
                name="activity_deleted_long_ago",
                reason="was needed once",
            ),
        ),
    )
    kinds = [v.kind for v in violations]
    assert "stale-activity-exemption" in kinds


def test_stale_activity_exemption_for_a_reachable_activity_fails():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[_FixtureOnlyCallerOfAnActivityWorkflow],
        exemptions=(),
        activity_deps={
            _FixtureOnlyCallerOfAnActivityWorkflow: frozenset(
                {"only_this_workflow_calls_me"}
            )
        },
        registered_activities=frozenset({"only_this_workflow_calls_me"}),
        activity_exemptions=(
            ActivityExemption(
                name="only_this_workflow_calls_me",
                reason="was unreachable before its workflow landed",
            ),
        ),
    )
    kinds = [v.kind for v in violations]
    assert "stale-activity-exemption" in kinds


def test_stale_workflow_exemption_for_an_unregistered_workflow_fails():
    violations = check_schedule_wiring(
        scheduled={},
        worker_workflows=[],
        exemptions=(
            Exemption(
                workflow=_FixtureOnlyCallerOfAnActivityWorkflow,
                reason="was registered-but-unscheduled once",
            ),
        ),
        activity_deps={},
        registered_activities=frozenset(),
    )
    kinds = [v.kind for v in violations]
    assert "stale-workflow-exemption" in kinds


def test_stale_workflow_exemption_for_a_scheduled_workflow_fails():
    violations = check_schedule_wiring(
        scheduled={"fixture-schedule": _FixtureOnlyCallerOfAnActivityWorkflow},
        worker_workflows=[_FixtureOnlyCallerOfAnActivityWorkflow],
        exemptions=(
            Exemption(
                workflow=_FixtureOnlyCallerOfAnActivityWorkflow,
                reason="unscheduled by design (no longer true)",
            ),
        ),
        activity_deps={},
        registered_activities=frozenset(),
    )
    kinds = [v.kind for v in violations]
    assert "stale-workflow-exemption" in kinds
