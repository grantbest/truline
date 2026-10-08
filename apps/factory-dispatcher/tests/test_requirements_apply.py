"""The requirements-registry applier's contract: revision-gated, failure-recorded, loud.

Every test drives the shipped ``apply_requirement_registries`` with injected
fakes — no substrate, no git. The properties pinned are the ones the charter
requires: PRIN-014 (an unchanged registries revision issues zero reconcile
work), PRIN-008 (a failure is recorded on the standing status bead AND still
raises so the Temporal run fails), and recovery carries ``recovered_from``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import status_bead  # noqa: E402
from activities.requirements_apply import (  # noqa: E402
    CREATED_BY,
    STATUS_REF,
    RequirementsApplyError,
    apply_requirement_registries,
)

NOW = datetime(2026, 9, 7, 21, 0, 0, tzinfo=timezone.utc)


class FakeSub:
    """Rejects what the live substrate rejects: only the verbs the applier uses.

    OPS-87: ``create``/``patch`` used to drop the ``state`` field the caller passed --
    ``create`` never stored it on the bead it returned, and ``patch`` filtered it out of
    the fields it applied -- the exact FakeSubstrate.add_note accepts-more-than-the-live-
    contract shape. A real substrate persists ``state``; this fake now does too, so a
    regression in ``status_bead.write_status``'s stamped state is visible through this
    double, not just through ``test_status_bead.py``'s dedicated pin.
    """

    def __init__(self, status_context=None):
        self.beads = []
        self.patches = []
        if status_context is not None:
            self.beads.append(
                {
                    "id": "obs-1",
                    "state": "active",
                    "content": {"ref": STATUS_REF},
                    "context": status_context,
                }
            )

    def list_beads(self, bead_type):
        assert bead_type == "observation"
        return list(self.beads)

    def find_bead(self, namespace, type, content_ref):
        assert namespace == "arch"
        assert type == "observation"
        for bead in self.beads:
            if (bead.get("content") or {}).get("ref") == content_ref:
                return bead
        return None

    def create(self, bead_type, state, content, parent_id, *, created_by):
        # 4th positional is parent_id, matching StatusSubstrate's Protocol --
        # the prior fake received it under the name `context` and dropped it,
        # the exact silent-drop shape this file's fidelity tests exist to end.
        assert bead_type == "observation"
        bead = {
            "id": f"obs-{len(self.beads) + 1}",
            "state": state,
            "content": content,
            "context": {},
            "parent_id": parent_id,
            "created_by": created_by,
        }
        self.beads.append(bead)
        return bead

    def patch(self, bead_id, fields, *, created_by):
        self.patches.append((bead_id, fields))
        for bead in self.beads:
            if bead["id"] == bead_id:
                bead.update(fields)
                bead["created_by"] = created_by


def test_fake_sub_retains_every_field_write_status_sends_on_create():
    """OPS-87 class check: not just the one field this bead named -- no field
    ``write_status`` sends may be silently dropped by the fake. On the create path
    ``write_status`` calls ``create(..., state="active", ...)`` then
    ``patch(id, {"context": ...})``; both must land on the stored bead."""
    sub = FakeSub()

    status_bead.write_status(
        sub, None, {"ref": STATUS_REF}, {"status": "ok"}, created_by="test",
    )

    assert len(sub.beads) == 1
    bead = sub.beads[0]
    assert bead["state"] == "active"
    assert bead["content"] == {"ref": STATUS_REF}
    assert bead["context"] == {"status": "ok"}
    assert bead["parent_id"] is None


def test_fake_sub_retains_every_field_write_status_sends_on_update():
    """Same check on the update path: ``write_status`` patches
    ``{"content", "context", "state"}`` in one call; none of the three may vanish."""
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    existing = sub.beads[0]
    new_content = {"ref": STATUS_REF, "last_synced_revision": "sha-2"}

    status_bead.write_status(
        sub, existing, new_content, {"status": "ok", "applied_revision": "sha-2"},
        created_by="test",
    )

    bead = sub.beads[0]
    assert bead["state"] == "active"
    assert bead["content"] == new_content
    assert bead["context"] == {"status": "ok", "applied_revision": "sha-2"}


class FakePlan:
    def __init__(self, errors=()):
        self.requirement_creates = ["PC-X-001"]
        self.requirement_updates = []
        self.requirement_unchanged = ["PC-X-002"]
        self.conformance_creates = ["ref-1"]
        self.conformance_updates = []
        self.conformance_unchanged = ["ref-2"]
        self.errors = list(errors)


class FakeLoader:
    def __init__(self, plan=None):
        self.plan = plan or FakePlan()
        self.reconcile_calls = 0

    def load_registries(self):
        return (["req"], ["conf"])

    def reconcile(self, sub, requirements, conformances, apply):
        assert apply is True
        self.reconcile_calls += 1
        return self.plan


class FakeCfg:
    base_ref = "origin/main"
    repo_root = Path("/nowhere")


def test_unchanged_revision_issues_no_reconcile_work():
    """PRIN-014: the ~96 idle ticks a day must not re-list every bead or re-run the
    reconcile. The standing status record still refreshes (R2605-8 DEFECT 2, covered
    below) -- that write is the report that the run happened, not a registry write."""
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    loader = FakeLoader()

    result = apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-1",
    )

    assert result == {"status": "unchanged", "revision": "sha-1"}
    assert loader.reconcile_calls == 0
    assert len(sub.patches) == 1
    context = sub.patches[-1][1]["context"]
    assert context["status"] == "unchanged"
    assert context["applied_revision"] == "sha-1"


def test_unchanged_revision_still_refreshes_the_standing_status_records_observed_at():
    """R2605-8 DEFECT 2 (sibling reconciler to activities/ea_apply.py's own fix): a
    frozen observed_at on obs.requirements-apply-status must mean the reconciler
    stopped, not that it is idle."""
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    loader = FakeLoader()
    later = datetime(2026, 9, 19, 2, 0, 0, tzinfo=timezone.utc)

    result = apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: later, revision_fn=lambda cfg: "sha-1",
    )

    assert result == {"status": "unchanged", "revision": "sha-1"}
    bead = sub.beads[0]
    assert bead["content"]["observed_at"] == "2026-09-19T02:00:00Z"
    # Distinct from "ok" (applied): a reader must never have to guess whether
    # this run applied something or found nothing to do.
    assert bead["context"]["status"] == "unchanged"


def test_changed_revision_reconciles_and_records_applied_revision():
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    loader = FakeLoader()

    result = apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-2",
    )

    assert result["status"] == "applied"
    assert result["revision"] == "sha-2"
    assert result["summary"]["requirement_creates"] == 1
    assert loader.reconcile_calls == 1
    context = sub.patches[-1][1]["context"]
    assert context["applied_revision"] == "sha-2"
    assert context["status"] == "ok"


def test_a_loader_sys_exit_still_writes_the_standing_failure_record():
    """SystemExit derives from BaseException; without the explicit catch it
    bypassed the PRIN-008 record while the bead kept saying ok (gate F1)."""

    class ExitingLoader(FakeLoader):
        def load_registries(self):
            sys.exit("duplicate requirement id: PC-X-001")

    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})

    with pytest.raises(RequirementsApplyError, match="duplicate requirement id"):
        apply_requirement_registries(
            sub, cfg=FakeCfg(), requirements_load=ExitingLoader(),
            now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-2",
        )

    context = sub.patches[-1][1]["context"]
    assert context["status"] == "failed"
    assert "duplicate requirement id" in context["reason"]


def test_requirements_apply_a_409_duplicate_ref_refusal_creating_the_status_bead_is_recorded_by_status_and_ref():
    """OPS-181/OPS-182 sibling to activities/ea_apply.py's own pin: a create/create
    race `find_status` does not itself close still refuses with the store's own
    unique-ref constraint (migration 0006_unique_arch_ref); the recorded reason must
    carry the status code and ref, never the response's body (the finance-integrity
    mapper's plaid-duplicate mislabel, OPS-182)."""

    class RuntimeError409(RuntimeError):
        def __init__(self, body: str):
            super().__init__(f"substrate 409: {body}")
            self.status = 409

    class RacyFakeSub(FakeSub):
        def create(self, bead_type, state, content, parent_id, *, created_by):
            if bead_type == "observation" and not any(
                b["id"] == "obs-race" for b in self.beads
            ):
                self.beads.append({
                    "id": "obs-race", "state": "active", "content": dict(content),
                    "context": {}, "parent_id": None,
                    "created_by": "factory-dispatcher/requirements-apply-concurrent",
                })
                raise RuntimeError409("plaid: duplicate transaction reference detected")
            return super().create(
                bead_type, state, content, parent_id, created_by=created_by
            )

    sub = RacyFakeSub()
    loader = FakeLoader()

    with pytest.raises(RequirementsApplyError) as excinfo:
        apply_requirement_registries(
            sub, cfg=FakeCfg(), requirements_load=loader,
            now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-1",
        )

    message = str(excinfo.value)
    assert "409" in message
    assert STATUS_REF in message
    assert "plaid" not in message
    assert "duplicate transaction" not in message

    [bead] = [b for b in sub.beads if b["id"] == "obs-race"]
    assert bead["context"]["status"] == "failed"
    assert bead["context"]["duplicate_ref_refusal"] == {"status": 409, "ref": STATUS_REF}
    assert "plaid" not in bead["context"]["reason"]


def test_plan_errors_record_standing_failure_then_raise():
    """PRIN-008: recorded AND announced — never a green run over a failed reconcile."""
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    loader = FakeLoader(plan=FakePlan(errors=["422 on PC-X-001"]))

    with pytest.raises(RequirementsApplyError, match="422 on PC-X-001"):
        apply_requirement_registries(
            sub, cfg=FakeCfg(), requirements_load=loader,
            now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-2",
        )

    context = sub.patches[-1][1]["context"]
    assert context["status"] == "failed"
    assert context["registries_revision"] == "sha-2"
    assert context["applied_revision"] == "sha-1"
    assert "422 on PC-X-001" in context["reason"]
    assert context["consecutive_failures"] == 1


def test_repeated_failure_counts_and_marks_repeat():
    prior = {
        "status": "failed",
        "registries_revision": "sha-2",
        "applied_revision": "sha-1",
        "reason": "requirements-load reconcile recorded 1 error(s): 422 on PC-X-001",
        "failed_at": "2026-09-07T20:00:00+00:00",
        "consecutive_failures": 1,
    }
    sub = FakeSub(status_context=prior)
    loader = FakeLoader(plan=FakePlan(errors=["422 on PC-X-001"]))

    with pytest.raises(RequirementsApplyError):
        apply_requirement_registries(
            sub, cfg=FakeCfg(), requirements_load=loader,
            now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-2",
        )

    context = sub.patches[-1][1]["context"]
    assert context["repeat_of_previous_failure"] is True
    assert context["consecutive_failures"] == 2
    assert context["previous_failure"]["reason"] == prior["reason"]


def test_recovery_carries_recovered_from():
    prior = {
        "status": "failed",
        "registries_revision": "sha-2",
        "applied_revision": "sha-1",
        "reason": "boom",
        "failed_at": "2026-09-07T20:00:00+00:00",
        "consecutive_failures": 3,
    }
    sub = FakeSub(status_context=prior)
    loader = FakeLoader()

    result = apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-2",
    )

    assert result["status"] == "applied"
    context = sub.patches[-1][1]["context"]
    assert context["recovered_from"]["reason"] == "boom"


def test_first_run_creates_the_standing_status_bead():
    sub = FakeSub()
    loader = FakeLoader()

    apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-1",
    )

    assert len(sub.beads) == 1
    assert sub.beads[0]["content"]["ref"] == STATUS_REF


def test_status_bead_declares_source_class_derived_and_created_by_on_create():
    """"factory-dispatcher/requirements-apply" is enrolled in
    apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119, #822) --
    the first landing (create) path must declare it."""
    sub = FakeSub()
    loader = FakeLoader()

    apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-1",
    )

    assert sub.beads[0]["content"]["source_class"] == "derived"
    assert sub.beads[0]["created_by"] == CREATED_BY


def test_status_bead_declares_source_class_derived_and_created_by_on_update():
    """Same identity, existing-status (update/patch) path -- the unchanged-revision
    branch still refreshes the standing record (R2605-8 DEFECT 2)."""
    sub = FakeSub(status_context={"status": "ok", "applied_revision": "sha-1"})
    loader = FakeLoader()

    apply_requirement_registries(
        sub, cfg=FakeCfg(), requirements_load=loader,
        now_fn=lambda: NOW, revision_fn=lambda cfg: "sha-1",
    )

    assert sub.beads[0]["content"]["source_class"] == "derived"
    assert sub.beads[0]["created_by"] == CREATED_BY


def test_requirements_apply_workflow_is_registered_on_the_worker():
    """The L2 defect class must not recur here: the schedule's target workflow
    is in the worker's declared list from day one."""
    import inspect

    import worker as worker_module

    source = inspect.getsource(worker_module.build_worker)
    assert "RequirementsApplyWorkflow" in source
