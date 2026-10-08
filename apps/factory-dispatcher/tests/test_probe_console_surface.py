"""R26.09/O-7: the console operator surface is proven by a dated probe, not a
demo. This bead adds no hand-rolled Substrate double of its own -- the
authority side is the REAL ``Substrate`` client (substrate.py) bound to the
shared ``FakeSubstrateBackend`` from ``test_store_contract.py``, the same
double every other dispatcher test enforcing the live content/admission
contract already uses. ``FakeGateway``/``FakeScheduleAuthority`` below are
not Substrate doubles -- neither overlaps the BeadStore protocol -- they
stand in for the gateway's own two HTTP routes and for Temporal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
APP_DIR = TESTS_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import probe_console_surface as probe  # noqa: E402
from substrate import Substrate, SubstrateError  # noqa: E402
from test_store_contract import FakeSubstrateBackend  # noqa: E402

import schemas  # noqa: E402 - apps/substrate/src is already on sys.path (substrate.py -> beadstore.py)

FIXTURE_PATH = TESTS_DIR / "fixtures" / "probe-observation.example.json"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store_and_backend(monkeypatch) -> tuple[Substrate, FakeSubstrateBackend]:
    """The real Substrate client bound to a fresh in-memory backend that
    enforces the live arch-content schema and source_class admission --
    mirrors test_store_contract.py's own ``substrate_store`` fixture rather
    than adding a second one."""
    backend = FakeSubstrateBackend()
    monkeypatch.setenv("SUBSTRATE_URL", "https://substrate.example.test")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "test-key")
    monkeypatch.setattr(
        Substrate,
        "_request",
        lambda self, method, path, headers=None, **kwargs: backend(
            method, path, headers=headers, **kwargs
        ),
    )
    return Substrate(), backend


def _seed_release(
    store: Substrate,
    *,
    ref: str = "R26.09",
    state: str = "in_flight",
    outcome_ids: tuple[str, ...] = ("O-1", "O-2", "O-3"),
) -> dict:
    content = {
        "ref": ref,
        "name": "Console surface",
        "objective": "prove the console shows what the store says",
        "outcomes": [{"id": oid, "statement": f"outcome {oid}", "work_class": "feature"} for oid in outcome_ids],
        "opened_at": "2026-09-01",
    }
    return store.create_bead("arch", "release", state, content, "release-load")


def _seed_task(store: Substrate, *, outcome_ref: str | None, state: str = "pending") -> str:
    bead = store.create_task({"outcome_ref": outcome_ref}, "agent")
    if state != "pending":
        store.set_state(bead["id"], state, "agent")
    return bead["id"]


def _release_delivery_ok(outcomes: list[dict], unclassified: list[str] | None = None) -> dict:
    return {"status": "ok", "found": True, "outcomes": outcomes, "unclassified_delivering": unclassified or []}


class FakeGateway:
    """Stands in for the gateway's own two routes -- never the substrate
    proxy. Not a Substrate double: it implements neither ``find_bead`` nor
    any other BeadStore protocol name."""

    def __init__(self, *, schedule_response: dict, release_responses: dict[str, dict]):
        self._schedule_response = schedule_response
        self._release_responses = release_responses
        self.calls: list[tuple[str, str, object]] = []

    async def get_json(self, path, params=None):
        self.calls.append(("GET", path, params))
        assert path == "/api/v1/factory/schedule_status"
        return self._schedule_response

    async def post_json(self, path, json=None):
        self.calls.append(("POST", path, json))
        assert path == "/api/v1/factory/release_delivery"
        return self._release_responses[json["release_ref"]]


class FakeScheduleAuthority:
    """Stands in for Temporal's own schedule description -- ``describe`` is
    not a BeadStore protocol name."""

    def __init__(self, *, paused=False, note=None, in_flight=(), raises: Exception | None = None):
        self._paused = paused
        self._note = note
        self._in_flight = list(in_flight)
        self._raises = raises
        self.calls: list[str] = []

    async def describe(self, schedule_id):
        self.calls.append(schedule_id)
        if self._raises is not None:
            raise self._raises
        return self._paused, self._note, list(self._in_flight)


def _matched_world(store: Substrate, *, paused=False, note="unattended drain", in_flight=()):
    """One consistent world: the gateway's claim and the store/Temporal
    authorities agree on everything for release R26.09."""
    release = _seed_release(store)
    t_done = _seed_task(store, outcome_ref="O-1", state="done")
    t_doing = _seed_task(store, outcome_ref="O-2", state="doing")
    store.add_link(t_done, release["id"], "delivers", "agent")
    store.add_link(t_doing, release["id"], "delivers", "agent")

    claim_outcomes = [
        {"id": "O-1", "statement": "s", "work_class": "feature", "task_count": 1, "tasks_by_state": {"done": [t_done]}},
        {"id": "O-2", "statement": "s", "work_class": "feature", "task_count": 1, "tasks_by_state": {"doing": [t_doing]}},
        {"id": "O-3", "statement": "s", "work_class": "feature", "task_count": 0, "tasks_by_state": {}},
    ]
    gateway = FakeGateway(
        schedule_response={
            "status": "ok",
            "paused": paused,
            "note": note,
            "in_flight": [{"workflow_id": wf} for wf in in_flight],
        },
        release_responses={"R26.09": _release_delivery_ok(claim_outcomes)},
    )
    schedule_authority = FakeScheduleAuthority(paused=paused, note=note, in_flight=in_flight)
    return gateway, schedule_authority


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_is_open_release_state():
    assert probe.is_open_release_state("planned")
    assert probe.is_open_release_state("in_flight")
    assert probe.is_open_release_state("closing")
    assert not probe.is_open_release_state("released")
    assert not probe.is_open_release_state("abandoned")


def test_aggregate_outcome_is_demonstrated_only_when_every_group_is():
    schedule = probe.GroupResult("schedule", "demonstrated")
    releases = {"R26.09": probe.GroupResult("release[R26.09]", "demonstrated")}
    assert probe._aggregate_outcome(schedule, releases) == "demonstrated"


def test_aggregate_outcome_prefers_failed_over_cannot_evaluate():
    schedule = probe.GroupResult("schedule", "failed:compare")
    releases = {"R26.09": probe.GroupResult("release[R26.09]", "cannot-evaluate:not_configured")}
    outcome = probe._aggregate_outcome(schedule, releases)
    assert outcome.startswith("failed:")
    assert "schedule" in outcome


def test_aggregate_outcome_reports_cannot_evaluate_when_nothing_failed():
    schedule = probe.GroupResult("schedule", "demonstrated")
    releases = {"R26.09": probe.GroupResult("release[R26.09]", "cannot-evaluate:not_configured")}
    outcome = probe._aggregate_outcome(schedule, releases)
    assert outcome == "cannot-evaluate:release[R26.09]"


# ---------------------------------------------------------------------------
# Schedule group
# ---------------------------------------------------------------------------


def test_probe_schedule_demonstrated_when_claim_and_authority_agree(store_and_backend):
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store, paused=True, note="paused", in_flight=["wf-1"])
    ops: list[str] = []
    result = _run(probe._probe_schedule(gateway, schedule_authority, "factory-dispatcher-dev", ops))
    assert result.outcome == "demonstrated"
    assert result.divergences == []
    assert "GET /api/v1/factory/schedule_status" in ops


def test_probe_schedule_reports_failed_compare_on_a_paused_divergence(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store, paused=False)
    lying_authority = FakeScheduleAuthority(paused=True, note="unattended drain")
    result = _run(probe._probe_schedule(gateway, lying_authority, "factory-dispatcher-dev", []))
    assert result.outcome == "failed:compare"
    assert result.divergences == [probe.Divergence("schedule", "schedule.paused", False, True)]


def test_probe_schedule_compares_in_flight_workflow_ids(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store, in_flight=["wf-claim"])
    authority = FakeScheduleAuthority(paused=False, note="unattended drain", in_flight=["wf-authority"])
    result = _run(probe._probe_schedule(gateway, authority, "factory-dispatcher-dev", []))
    assert result.outcome == "failed:compare"
    assert result.divergences == [probe.Divergence("schedule", "schedule.in_flight", ["wf-claim"], ["wf-authority"])]


def test_probe_schedule_reports_failed_read_claims_when_gateway_is_not_ok():
    gateway = FakeGateway(
        schedule_response={"status": "unknown", "detail": "Temporal unreachable"}, release_responses={}
    )
    authority = FakeScheduleAuthority()
    result = _run(probe._probe_schedule(gateway, authority, "factory-dispatcher-dev", []))
    assert result.outcome == "failed:read_claims"


def test_probe_schedule_reports_failed_read_authorities_when_temporal_is_unreachable(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)
    failing = FakeScheduleAuthority(raises=RuntimeError("Temporal is unreachable"))
    result = _run(probe._probe_schedule(gateway, failing, "factory-dispatcher-dev", []))
    assert result.outcome == "failed:read_authorities"


# ---------------------------------------------------------------------------
# Release group
# ---------------------------------------------------------------------------


def test_probe_release_demonstrated_when_claim_and_authority_agree(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "demonstrated"
    assert result.divergences == []


def test_probe_release_reports_cannot_evaluate_not_configured_for_ops_110_arm_c(store_and_backend):
    store, _backend = store_and_backend
    _seed_release(store)
    gateway = FakeGateway(
        schedule_response={},
        release_responses={"R26.09": {"status": "unknown", "code": "not_configured", "detail": "FACTORY_STATUS_REPO_ROOT is unset"}},
    )
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "cannot-evaluate:not_configured"


def test_probe_release_never_demonstrated_or_zero_when_not_configured_flips_to_reachable(store_and_backend):
    """AC-1: the same code path must evaluate unchanged the day OPS-110 arm
    (c) reverses -- i.e. once release_delivery answers status ok, this
    function compares for real rather than special-casing "was previously
    unconfigured"."""
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome not in ("cannot-evaluate:not_configured",)
    assert result.outcome == "demonstrated"


def test_probe_release_reports_cannot_evaluate_no_such_release_when_charter_not_found(store_and_backend):
    store, _backend = store_and_backend
    gateway = FakeGateway(schedule_response={}, release_responses={"R99.99": {"status": "ok", "found": False}})
    result = _run(probe._probe_release(gateway, store, "R99.99", []))
    assert result.outcome == "cannot-evaluate:no-such-release"


def test_probe_release_reports_cannot_evaluate_no_release_bead_when_charter_has_no_mirror(store_and_backend):
    store, _backend = store_and_backend
    # The claim's own authority (git charter files) resolves the ref, but no
    # arch.release bead mirrors it yet -- this probe's own authority cannot
    # be read at all.
    gateway = FakeGateway(
        schedule_response={},
        release_responses={"R26.09": _release_delivery_ok([{"id": "O-1", "statement": "s", "work_class": "feature", "task_count": 0, "tasks_by_state": {}}])},
    )
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "cannot-evaluate:no-release-bead"


def test_probe_release_reports_failed_compare_on_a_task_count_divergence(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)
    # Corrupt the claim's count for O-1 (authority says 1, claim says 2).
    gateway._release_responses["R26.09"]["outcomes"][0]["task_count"] = 2
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "failed:compare"
    fields = {d.field for d in result.divergences}
    assert "release[R26.09].outcomes[O-1].task_count" in fields


def test_probe_release_reports_failed_compare_on_unclassified_divergence(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)
    gateway._release_responses["R26.09"]["unclassified_delivering"] = ["task-ghost"]
    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "failed:compare"
    assert any(d.field == "release[R26.09].unclassified_delivering" for d in result.divergences)


def test_probe_release_reports_failed_read_authorities_when_list_links_refuses(store_and_backend, monkeypatch):
    """AC-3: an unreachable store or a store error inside the authority reads
    (find_bead/list_links/list_tasks) must never escape _probe_release as a
    raw exception -- it is a step failure like any other."""
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)

    def _refuse(self, *a, **kw):
        raise SubstrateError(503, "service unavailable")

    monkeypatch.setattr(Substrate, "list_links", _refuse)

    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "failed:read_authorities"
    assert result.divergences == []


def test_probe_release_reports_failed_read_authorities_when_the_store_is_unreachable(store_and_backend, monkeypatch):
    """The other shape of the same failure: httpx.ConnectError carries no
    .status at all (it is raised by the transport before any HTTP response
    exists), so a check narrowed to SubstrateError's own shape would miss it."""
    import httpx

    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store)

    def _unreachable(self, *a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(Substrate, "list_links", _unreachable)

    result = _run(probe._probe_release(gateway, store, "R26.09", []))
    assert result.outcome == "failed:read_authorities"
    assert result.divergences == []


# ---------------------------------------------------------------------------
# run_probe -- the full sequence and its aggregate outcome
# ---------------------------------------------------------------------------


def test_run_probe_reports_demonstrated_only_when_every_group_is(store_and_backend):
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)
    result = _run(
        probe.run_probe(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=["R26.09"],
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert result.outcome == "demonstrated"
    assert result.divergences == []
    assert result.release_refs == ["R26.09"]
    assert result.schedule.outcome == "demonstrated"
    assert result.releases["R26.09"].outcome == "demonstrated"


def test_run_probe_defaults_to_every_non_terminal_release_when_none_named(store_and_backend):
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)
    _seed_release(store, ref="R26.01", state="released", outcome_ids=("O-1",))

    result = _run(
        probe.run_probe(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=None,
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert result.release_refs == ["R26.09"]  # R26.01 excluded: released is terminal


def test_run_probe_aggregate_never_demonstrated_when_any_group_diverges(store_and_backend):
    store, _backend = store_and_backend
    gateway, _sched = _matched_world(store, paused=False)
    lying_authority = FakeScheduleAuthority(paused=True, note="unattended drain")
    result = _run(
        probe.run_probe(
            gateway=gateway,
            store=store,
            schedule_authority=lying_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=["R26.09"],
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert result.outcome == "failed:schedule"
    assert len(result.divergences) == 1


# ---------------------------------------------------------------------------
# record_observation -- closed content, everything else in context, two
# PATCHes on update, fixed identity, content_ref lookup
# ---------------------------------------------------------------------------


def _demonstrated_result() -> probe.ProbeResult:
    return probe.ProbeResult(
        probe_at="2026-09-23T00:00:00Z",
        ran_by="tester",
        probed_revision="deadbeef",
        operations=["GET /api/v1/factory/schedule_status"],
        release_refs=["R26.09"],
        schedule=probe.GroupResult("schedule", "demonstrated"),
        releases={"R26.09": probe.GroupResult("release[R26.09]", "demonstrated")},
        outcome="demonstrated",
        divergences=[],
    )


def test_record_observation_creates_the_bead_with_the_closed_content_shape(store_and_backend):
    store, _backend = store_and_backend
    bead = probe.record_observation(store, _demonstrated_result())

    assert bead["created_by"] == probe.CREATED_BY
    assert set(bead["content"]) == {"ref", "observed_at", "workload", "source_class"}
    assert bead["content"]["ref"] == probe.OBSERVATION_REF
    assert bead["content"]["source_class"] == "observed"
    assert bead["context"]["outcome"] == "demonstrated"
    assert bead["context"]["groups"] == {"schedule": "demonstrated", "releases": {"R26.09": "demonstrated"}}
    assert bead["context"]["not_probed"] == list(probe.NOT_PROBED)

    # The store's own validator accepts it -- not a fake that admits what the
    # store refuses.
    schemas.validate_bead_content("arch", "observation", bead["content"])


def test_record_observation_updates_the_same_bead_with_two_patches_on_a_second_run(store_and_backend, monkeypatch):
    store, _backend = store_and_backend
    first = probe.record_observation(store, _demonstrated_result())

    calls: list[str] = []
    real_patch_content = Substrate.patch_content
    real_patch_context = Substrate.patch_context
    monkeypatch.setattr(
        Substrate, "patch_content", lambda self, *a, **kw: (calls.append("content"), real_patch_content(self, *a, **kw))[1]
    )
    monkeypatch.setattr(
        Substrate, "patch_context", lambda self, *a, **kw: (calls.append("context"), real_patch_context(self, *a, **kw))[1]
    )

    second_result = probe.ProbeResult(
        probe_at="2026-09-24T00:00:00Z",
        ran_by="tester",
        probed_revision="cafebabe",
        operations=[],
        release_refs=["R26.09"],
        schedule=probe.GroupResult("schedule", "failed:compare", [probe.Divergence("schedule", "schedule.paused", False, True)]),
        releases={},
        outcome="failed:schedule",
        divergences=[probe.Divergence("schedule", "schedule.paused", False, True)],
    )
    second = probe.record_observation(store, second_result)

    assert calls == ["content", "context"]  # never optimised down to one PATCH
    assert second["id"] == first["id"]  # updated in place, never a second bead
    assert second["content"]["observed_at"] == "2026-09-24T00:00:00Z"
    assert second["context"]["outcome"] == "failed:schedule"
    schemas.validate_bead_content("arch", "observation", second["content"])


def test_run_and_record_writes_the_observation_and_returns_both(store_and_backend):
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)
    outcome = _run(
        probe.run_and_record(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=["R26.09"],
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert outcome["result"].outcome == "demonstrated"
    assert outcome["bead"]["content"]["ref"] == probe.OBSERVATION_REF


def test_run_and_record_reports_failed_record_never_a_500_on_a_refused_write(store_and_backend, monkeypatch):
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)

    def _refuse(self, *a, **kw):
        raise SubstrateError(409, "source_class_admission_violation")

    monkeypatch.setattr(Substrate, "create_bead", _refuse)

    outcome = _run(
        probe.run_and_record(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=["R26.09"],
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert outcome["bead"] is None
    assert outcome["result"].outcome == "failed:record"


def test_run_and_record_reports_failed_record_never_a_500_when_the_store_is_unreachable(
    store_and_backend, monkeypatch
):
    """AC-3: a record-step exception without .status (httpx.ConnectError,
    raised by the transport before any HTTP response exists) must also be
    reported as failed:record, not re-raised as a traceback -- the same
    posture as a refused write, for a different failure mode."""
    import httpx

    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)

    def _unreachable(self, *a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(Substrate, "create_bead", _unreachable)

    outcome = _run(
        probe.run_and_record(
            gateway=gateway,
            store=store,
            schedule_authority=schedule_authority,
            schedule_id="factory-dispatcher-dev",
            release_refs=["R26.09"],
            ran_by="tester",
            probed_revision="deadbeef",
        )
    )
    assert outcome["bead"] is None
    assert outcome["result"].outcome == "failed:record"


def test_run_and_record_reraises_an_unrelated_exception(store_and_backend, monkeypatch):
    """Only a refused write (one carrying .status) or an unreachable store
    (an httpx.HTTPError) is swallowed into failed:record -- anything else is
    a real bug and must not be hidden."""
    store, _backend = store_and_backend
    gateway, schedule_authority = _matched_world(store)

    def _blow_up(self, *a, **kw):
        raise ValueError("not a SubstrateError")

    monkeypatch.setattr(Substrate, "create_bead", _blow_up)

    with pytest.raises(ValueError):
        _run(
            probe.run_and_record(
                gateway=gateway,
                store=store,
                schedule_authority=schedule_authority,
                schedule_id="factory-dispatcher-dev",
                release_refs=["R26.09"],
                ran_by="tester",
                probed_revision="deadbeef",
            )
        )


# ---------------------------------------------------------------------------
# Fixture -- validated against the store's own validator, both examples
# ---------------------------------------------------------------------------


def test_fixture_content_validates_with_the_stores_own_validator():
    fixture = json.loads(FIXTURE_PATH.read_text())
    schemas.validate_bead_content("arch", "observation", fixture["content"])
    divergent = fixture["_example_of_a_divergent_and_unconfigured_run"]["content"]
    schemas.validate_bead_content("arch", "observation", divergent)


def test_fixture_context_keys_match_what_context_builds():
    fixture = json.loads(FIXTURE_PATH.read_text())
    result = _demonstrated_result()
    built_context = probe._context(result)
    assert set(fixture["context"]) == set(built_context)


def test_context_divergence_entries_carry_group_field_claim_authority():
    """AC-2: every divergence names the group it belongs to, not just the
    field -- a reader must not have to parse "release[R26.09].outcomes[O-1]"
    back apart to know which group diverged."""
    result = dataclasses.replace(
        _demonstrated_result(),
        schedule=probe.GroupResult(
            "schedule", "failed:compare", [probe.Divergence("schedule", "schedule.paused", False, True)]
        ),
        outcome="failed:schedule",
        divergences=[probe.Divergence("schedule", "schedule.paused", False, True)],
    )
    built = probe._context(result)
    assert built["divergences"] == [
        {"group": "schedule", "field": "schedule.paused", "claim": False, "authority": True}
    ]
    assert set(built["divergences"][0]) == {"group", "field", "claim", "authority"}


def test_fixture_divergence_entries_carry_group_field_claim_authority():
    fixture = json.loads(FIXTURE_PATH.read_text())
    divergence = fixture["_example_of_a_divergent_and_unconfigured_run"]["context"]["divergences"][0]
    assert set(divergence) == {"group", "field", "claim", "authority"}
