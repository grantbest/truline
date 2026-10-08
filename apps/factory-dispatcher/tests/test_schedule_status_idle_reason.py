"""PC-EXE-007/AC-6: schedule_status.describe_idle_reason(IdleInputs) answers why the
factory is not running from six named inputs, never synthesising a healthy mark for
an input nobody served.

dev.findings 6706292f / 7e208eea: four surfaces already say "claimable" and none says
why nothing runs. These tests pin the pure function (cases (a)-(e) plus the additional
reason classes, and the precedence/NOT_SERVED rules AC-5's mutation list targets) and
the CLI wiring that supplies all six inputs and renders the answer.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schedule_status  # noqa: E402
from schedule_runtime import CAPACITY_PAUSE_NOTE_PREFIX, FactoryScheduleStatus  # noqa: E402
from schedule_status import (  # noqa: E402
    IDLE_INPUT_FIELDS,
    NOT_SERVED,
    BaseRefLiveStatus,
    IdleInputs,
    SchedulePauseInput,
    TunnelKeeperIdleInput,
    TunnelKeeperLiveStatus,
    WaitingQueueStatus,
    describe_idle_reason,
)
from worker_revision import WorkerRevisionStatus  # noqa: E402

SCHEDULE_ID = "factory-dispatcher-dev"


def healthy_schedule() -> SchedulePauseInput:
    return SchedulePauseInput(paused=False, note="")


def healthy_worker_revision() -> WorkerRevisionStatus:
    return WorkerRevisionStatus(
        worker_revision="abc123",
        worker_started_at="2026-01-01T00:00:00Z",
        main_ref="main",
        main_revision="abc123",
        is_ancestor=True,
        commits_behind=0,
    )


def healthy_base_ref() -> BaseRefLiveStatus:
    return BaseRefLiveStatus(
        base_ref="main",
        local_rev="abc",
        upstream_ref="origin/main",
        upstream_rev="abc",
        commits_behind=0,
        local_only=0,
        checked=True,
    )


def healthy_tunnel_keeper() -> TunnelKeeperIdleInput:
    return TunnelKeeperIdleInput(down=False, detail="")


def healthy_queue(selectable_count: int = 1, latched_count: int = 0) -> WaitingQueueStatus:
    return WaitingQueueStatus(
        claimable_count=selectable_count,
        oldest_waiting_age=None,
        non_claimable_pending_count=0,
        selectable_count=selectable_count,
        latched_count=latched_count,
    )


def healthy_inputs(**overrides) -> IdleInputs:
    base = dict(
        schedule=healthy_schedule(),
        worker_revision=healthy_worker_revision(),
        base_ref=healthy_base_ref(),
        tunnel_keeper=healthy_tunnel_keeper(),
        queue=healthy_queue(),
        in_flight=0,
    )
    base.update(overrides)
    return IdleInputs(**base)


# --- AC-1: the input shape ---------------------------------------------------


def test_idle_inputs_has_exactly_the_six_named_fields_in_order():
    assert IDLE_INPUT_FIELDS == (
        "schedule",
        "worker_revision",
        "base_ref",
        "tunnel_keeper",
        "queue",
        "in_flight",
    )
    field_names = tuple(f.name for f in dataclasses.fields(IdleInputs))
    assert field_names == IDLE_INPUT_FIELDS


def test_idle_inputs_has_no_defaults_on_any_field():
    """AC-5 mutation: a default on any field would let a caller omit it --
    silently serving a healthy mark for an input nobody actually read."""
    for field in dataclasses.fields(IdleInputs):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name

    with pytest.raises(TypeError):
        IdleInputs(
            schedule=healthy_schedule(),
            worker_revision=healthy_worker_revision(),
            base_ref=healthy_base_ref(),
            tunnel_keeper=healthy_tunnel_keeper(),
            queue=healthy_queue(),
            # in_flight omitted
        )


def test_idle_inputs_is_frozen():
    inputs = healthy_inputs()
    with pytest.raises(FrozenInstanceError):
        inputs.schedule = NOT_SERVED  # type: ignore[misc]


# --- (a) schedule paused with the capacity note ------------------------------


def test_case_a_capacity_pause_reports_not_running_with_the_note():
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    inputs = healthy_inputs(schedule=SchedulePauseInput(paused=True, note=note))

    result = describe_idle_reason(inputs)

    assert result.running is False
    assert result.reasons == [["capacity_pause", note]]
    assert result.inputs_not_served == []


def test_case_a_capacity_pause_with_in_flight_work_says_both():
    """'the reasons are still listed, so a capacity pause with work in flight
    says both': running is driven by in_flight, independently of reasons."""
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    inputs = healthy_inputs(
        schedule=SchedulePauseInput(paused=True, note=note),
        in_flight=3,
    )

    result = describe_idle_reason(inputs)

    assert result.running is True
    assert result.reasons == [["capacity_pause", note]]


# --- (b) worker_revision unknown ---------------------------------------------


def test_case_b_worker_revision_unknown_makes_running_unknown():
    inputs = healthy_inputs(worker_revision="unknown")

    result = describe_idle_reason(inputs)

    assert result.running == "unknown"
    assert result.reasons == [["worker_revision_unknown", "worker_revision could not be read"]]
    assert result.inputs_not_served == []


# --- (c) everything healthy but the queue is empty by the breaker -----------


def test_case_c_nothing_selectable_names_the_latched_count():
    inputs = healthy_inputs(queue=healthy_queue(selectable_count=0, latched_count=4))

    result = describe_idle_reason(inputs)

    assert result.running is False
    assert result.reasons == [["nothing_selectable", "latched 4"]]


def test_case_c_latched_zero_is_still_named_explicitly():
    """AC-5 mutation: dropping the latched count from the detail must turn
    this red -- even a count of zero is named, not omitted."""
    inputs = healthy_inputs(queue=healthy_queue(selectable_count=0, latched_count=0))

    result = describe_idle_reason(inputs)

    assert result.reasons == [["nothing_selectable", "latched 0"]]


# --- (d) the queue read failing -----------------------------------------------


def test_case_d_queue_unknown_never_reads_as_running():
    inputs = healthy_inputs(queue="unknown")

    result = describe_idle_reason(inputs)

    assert result.running == "unknown"
    assert result.reasons == [["queue_unknown", "queue could not be read"]]


def test_multiple_unknown_inputs_are_all_named_in_field_order():
    inputs = healthy_inputs(base_ref="unknown", queue="unknown")

    result = describe_idle_reason(inputs)

    assert result.running == "unknown"
    assert result.reasons == [
        ["base_ref_unknown", "base_ref could not be read"],
        ["queue_unknown", "queue could not be read"],
    ]


# --- (e) NOT_SERVED inputs never synthesise a reason or a healthy mark ------


def test_case_e_not_served_inputs_are_named_but_never_reasoned_about():
    inputs = healthy_inputs(
        worker_revision=NOT_SERVED,
        base_ref=NOT_SERVED,
        tunnel_keeper=NOT_SERVED,
        queue=healthy_queue(selectable_count=2),
    )

    result = describe_idle_reason(inputs)

    assert result.running is True
    assert result.reasons == []
    assert result.inputs_not_served == ["worker_revision", "base_ref", "tunnel_keeper"]


def test_not_served_queue_synthesises_no_nothing_selectable_reason():
    """AC-5 mutation: a healthy mark (or any reason) must never be synthesised
    for a NOT_SERVED input, even one that would otherwise drive a reason."""
    inputs = healthy_inputs(queue=NOT_SERVED)

    result = describe_idle_reason(inputs)

    assert result.reasons == []
    assert result.inputs_not_served == ["queue"]


# --- additional classes: schedule_paused, base_ref_stale, tunnel_keeper_down -


def test_schedule_paused_for_a_non_capacity_reason():
    inputs = healthy_inputs(
        schedule=SchedulePauseInput(paused=True, note="manual pause for maintenance")
    )

    result = describe_idle_reason(inputs)

    assert result.running is False
    assert result.reasons == [["schedule_paused", "manual pause for maintenance"]]


def test_base_ref_stale_is_reported_with_detail():
    stale_base_ref = BaseRefLiveStatus(
        base_ref="main",
        local_rev="aaa",
        upstream_ref="origin/main",
        upstream_rev="bbb",
        commits_behind=4,
        local_only=0,
        checked=True,
    )
    inputs = healthy_inputs(base_ref=stale_base_ref)

    result = describe_idle_reason(inputs)

    assert result.running is False
    assert result.reasons == [
        ["base_ref_stale", schedule_status._base_ref_stale_detail(stale_base_ref)]
    ]


def test_tunnel_keeper_down_is_reported_with_detail():
    inputs = healthy_inputs(
        tunnel_keeper=TunnelKeeperIdleInput(down=True, detail="pid=123 pid_running=False")
    )

    result = describe_idle_reason(inputs)

    assert result.running is False
    assert result.reasons == [["tunnel_keeper_down", "pid=123 pid_running=False"]]


def test_every_applicable_class_is_listed_in_the_fixed_order():
    stale_base_ref = BaseRefLiveStatus(
        base_ref="main",
        local_rev="aaa",
        upstream_ref="origin/main",
        upstream_rev="bbb",
        commits_behind=2,
        local_only=0,
        checked=True,
    )
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    inputs = healthy_inputs(
        schedule=SchedulePauseInput(paused=True, note=note),
        base_ref=stale_base_ref,
        tunnel_keeper=TunnelKeeperIdleInput(down=True, detail="keeper dead"),
        queue=healthy_queue(selectable_count=0, latched_count=1),
    )

    result = describe_idle_reason(inputs)

    assert [reason[0] for reason in result.reasons] == [
        "capacity_pause",
        "base_ref_stale",
        "tunnel_keeper_down",
        "nothing_selectable",
    ]


# --- precedence: unknown wins before any reason is derived -------------------


def test_unknown_precedence_suppresses_reasons_from_other_inputs():
    """AC-5 mutation: deriving reasons before checking for unknown must turn
    this red -- a capacity-pause schedule alongside an unknown base_ref must
    report only the unknown, not capacity_pause too."""
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    inputs = healthy_inputs(
        schedule=SchedulePauseInput(paused=True, note=note),
        base_ref="unknown",
    )

    result = describe_idle_reason(inputs)

    assert result.running == "unknown"
    assert result.reasons == [["base_ref_unknown", "base_ref could not be read"]]


def test_unknown_in_flight_never_reads_as_running():
    """AC-5 mutation: 'running true when the queue read failed' generalises --
    an unknown in_flight must not make running True either."""
    inputs = healthy_inputs(in_flight="unknown")

    result = describe_idle_reason(inputs)

    assert result.running == "unknown"
    assert result.reasons == [["in_flight_unknown", "in_flight could not be read"]]


# --- CLI wiring (AC-3) --------------------------------------------------------


def _install_fake_temporal_client(monkeypatch, status: FactoryScheduleStatus, note: str) -> None:
    class _FakeClient:
        @staticmethod
        async def connect(_address, namespace=None):
            return object()

    monkeypatch.setitem(sys.modules, "temporalio.client", types.SimpleNamespace(Client=_FakeClient))

    async def fake_describe(_client, *, schedule_id):
        return status

    async def fake_pause_state(_client, *, schedule_id):
        return status.paused, note

    monkeypatch.setattr(schedule_status, "describe_factory_schedule_status", fake_describe)
    monkeypatch.setattr(schedule_status, "dispatch_schedule_pause_state", fake_pause_state)


def _stub_local_describers(
    monkeypatch,
    *,
    worker_revision=None,
    base_ref=None,
    tunnel_keeper=None,
    queue=None,
) -> None:
    monkeypatch.setattr(
        schedule_status,
        "describe_worker_revision_drift",
        lambda *a, **k: worker_revision
        or WorkerRevisionStatus(
            worker_revision="abc123",
            worker_started_at="2026-01-01T00:00:00Z",
            main_ref="main",
            main_revision="abc123",
            is_ancestor=True,
            commits_behind=0,
        ),
    )
    monkeypatch.setattr(
        schedule_status,
        "describe_base_ref_status",
        lambda *a, **k: base_ref
        or BaseRefLiveStatus(
            base_ref="main",
            local_rev="abc",
            upstream_ref="origin/main",
            upstream_rev="abc",
            commits_behind=0,
            local_only=0,
            checked=True,
        ),
    )
    monkeypatch.setattr(
        schedule_status,
        "describe_tunnel_keeper_status",
        lambda *a, **k: tunnel_keeper
        or TunnelKeeperLiveStatus(heartbeat_at="2026-01-01T00:00:00Z", pid=1, pid_running=True, links={}),
    )
    monkeypatch.setattr(schedule_status, "_tunnel_keeper_stale", lambda status, now: (False, timedelta(0)))
    monkeypatch.setattr(
        schedule_status,
        "describe_waiting_queue",
        lambda *a, **k: queue
        or WaitingQueueStatus(
            claimable_count=1,
            oldest_waiting_age=None,
            non_claimable_pending_count=0,
            selectable_count=1,
            latched_count=0,
        ),
    )


def _quiet_notifications(monkeypatch) -> None:
    monkeypatch.setattr(schedule_status, "factory_health_notifications", lambda *a, **k: [])
    monkeypatch.setattr(schedule_status.cluster_health, "notify", lambda *a, **k: None)
    monkeypatch.setattr(schedule_status.cluster_health, "poster_from_env", lambda *a, **k: None)
    monkeypatch.setattr(schedule_status, "announce_wedged_dispatch_workflows", lambda *a, **k: [])


def test_cli_renders_the_idle_block_for_case_a(monkeypatch, capsys):
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    status = FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=True, in_flight=(), recent=())
    _install_fake_temporal_client(monkeypatch, status, note)
    _stub_local_describers(monkeypatch)
    _quiet_notifications(monkeypatch)
    monkeypatch.setattr(schedule_status, "render_schedule_status", lambda *a, **k: "BASE REPORT")

    exit_code = asyncio.run(
        schedule_status._main(address="localhost:7233", namespace="default", schedule_id=SCHEDULE_ID)
    )

    out = capsys.readouterr().out
    assert "IDLE:" in out
    assert "running: false" in out
    assert f"capacity_pause: {note}" in out
    assert "inputs_not_served: none" in out
    assert exit_code == 0  # Temporal read succeeded; paused with no in-flight is genuinely quiet


def test_cli_renders_the_idle_json_for_case_a(monkeypatch, capsys):
    note = f"{CAPACITY_PAUSE_NOTE_PREFIX}; worker capacity exhausted"
    status = FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=True, in_flight=(), recent=())
    _install_fake_temporal_client(monkeypatch, status, note)
    _stub_local_describers(monkeypatch)
    _quiet_notifications(monkeypatch)

    asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
            json_output=True,
        )
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload == {
        "idle_reason": {
            "running": False,
            "reasons": [["capacity_pause", note]],
            "inputs_not_served": [],
            "errors": {},
        }
    }


def test_cli_renders_the_idle_block_and_json_for_case_d(monkeypatch, capsys):
    status = FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=True, in_flight=(), recent=())
    _install_fake_temporal_client(monkeypatch, status, "")
    _stub_local_describers(
        monkeypatch,
        queue=WaitingQueueStatus.could_not_determine("substrate unavailable"),
    )
    _quiet_notifications(monkeypatch)
    monkeypatch.setattr(schedule_status, "render_schedule_status", lambda *a, **k: "BASE REPORT")

    exit_code = asyncio.run(
        schedule_status._main(address="localhost:7233", namespace="default", schedule_id=SCHEDULE_ID)
    )
    out = capsys.readouterr().out
    assert "running: unknown" in out
    assert "queue_unknown: queue could not be read" in out
    assert "idle_queue_error: substrate unavailable" in out
    assert exit_code == 0  # Temporal read succeeded and is genuinely quiet; unaffected by idle_reason

    monkeypatch.setattr(schedule_status, "render_schedule_status", lambda *a, **k: "BASE REPORT")
    asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
            json_output=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["idle_reason"]["running"] == "unknown"
    assert payload["idle_reason"]["reasons"] == [["queue_unknown", "queue could not be read"]]
    assert payload["idle_reason"]["errors"] == {"queue": "substrate unavailable"}


def _install_raising_temporal_client(monkeypatch, trigger: str) -> None:
    """RC-1: install a fake `temporalio.client` module whose `Client.connect`
    either raises directly (trigger="connect") or succeeds and lets the
    patched `describe_factory_schedule_status` raise instead (trigger=
    "describe") -- the two failure points AC-3 names. Never deletes the real
    module from `sys.modules`, so this is hermetic whether or not the real
    `temporalio` package happens to be installed (decision record 2026-09-13
    D7: no test may reach a live Temporal)."""

    class _Raising:
        @staticmethod
        async def connect(_address, namespace=None):
            raise ConnectionError("double: connection refused")

    class _Succeeding:
        @staticmethod
        async def connect(_address, namespace=None):
            return object()

    if trigger == "connect":
        monkeypatch.setitem(
            sys.modules, "temporalio.client", types.SimpleNamespace(Client=_Raising)
        )
    else:
        monkeypatch.setitem(
            sys.modules, "temporalio.client", types.SimpleNamespace(Client=_Succeeding)
        )

        async def failing_describe(_client, *, schedule_id):
            raise ConnectionError("double: connection refused")

        monkeypatch.setattr(schedule_status, "describe_factory_schedule_status", failing_describe)


@pytest.mark.parametrize("trigger", ["connect", "describe"])
def test_a_failed_temporal_read_exits_nonzero_with_no_in_flight_line_and_no_notifications(
    monkeypatch, capsys, trigger
):
    """AC-3: a failed schedule/in-flight Temporal read must never let a line
    matching ^in_flight_workflows: reach stdout -- scripts/factory-redeploy.py's
    check_in_flight reads that line's absence as unsafe to restart, and a line
    asserted from a failed read would let a redeploy kickstart over live work.

    RC-1: hermetic against both environments -- real `temporalio` absent (this
    sandbox) and real `temporalio` installed (CI/prod). When the real package
    is importable, its own `Client.connect` is monkeypatched to raise an
    AssertionError first, and only then is the fake module installed over it;
    this test passing proves the fake, never the real client, is what the
    production code under test actually reached.
    """
    try:
        import temporalio.client as _real_temporalio_client

        def _assert_real_client_unused(*_args, **_kwargs):
            raise AssertionError("real Temporal client used")

        monkeypatch.setattr(
            _real_temporalio_client.Client,
            "connect",
            staticmethod(_assert_real_client_unused),
        )
    except ImportError:
        pass

    _install_raising_temporal_client(monkeypatch, trigger)
    _stub_local_describers(monkeypatch)

    notify_calls = []
    announce_calls = []
    monkeypatch.setattr(schedule_status, "factory_health_notifications", lambda *a, **k: notify_calls.append(1) or [])
    monkeypatch.setattr(schedule_status.cluster_health, "notify", lambda *a, **k: None)
    monkeypatch.setattr(schedule_status.cluster_health, "poster_from_env", lambda *a, **k: None)
    monkeypatch.setattr(
        schedule_status,
        "announce_wedged_dispatch_workflows",
        lambda *a, **k: announce_calls.append(1) or [],
    )

    exit_code = asyncio.run(
        schedule_status._main(address="localhost:7233", namespace="default", schedule_id=SCHEDULE_ID)
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert not any(line.startswith("in_flight_workflows:") for line in out.splitlines())
    assert "running: unknown" in out
    assert notify_calls == []
    assert announce_calls == []


# --- RC-2: pin the CLI's mapping of every local reader's failure to 'unknown' -


@pytest.mark.parametrize("name", ["worker_revision", "base_ref", "tunnel_keeper"])
def test_cli_maps_a_failed_local_reader_to_unknown_in_json(monkeypatch, capsys, name):
    """RC-2. Each of worker_revision, base_ref and tunnel_keeper must map a
    failed local read to the idle-reason "unknown" case, naming the input and
    carrying its error text through -- the queue case is already covered by
    test_cli_renders_the_idle_block_and_json_for_case_d above.

    Proving mutations, each must turn this red: (a) `_worker_revision_idle_input`
    returning `status` unconditionally; (b) `_base_ref_idle_input` returning
    `status` unconditionally; (c) `_tunnel_keeper_idle_input` returning
    `TunnelKeeperIdleInput(down=False)` when `status.error` is set.
    """
    error_text = f"double: {name} failed"
    status = FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=False, in_flight=(), recent=())
    _install_fake_temporal_client(monkeypatch, status, "")

    failing_status: dict[str, object] = {}
    if name == "worker_revision":
        failing_status["worker_revision"] = WorkerRevisionStatus.could_not_determine(
            "main", error_text
        )
    elif name == "base_ref":
        failing_status["base_ref"] = BaseRefLiveStatus.could_not_determine("main", error_text)
    else:
        failing_status["tunnel_keeper"] = TunnelKeeperLiveStatus.could_not_determine(error_text)

    _stub_local_describers(monkeypatch, **failing_status)
    _quiet_notifications(monkeypatch)

    asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
            json_output=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["idle_reason"]["running"] == "unknown"
    assert payload["idle_reason"]["reasons"] == [
        [f"{name}_unknown", f"{name} could not be read"]
    ]
    assert payload["idle_reason"]["errors"] == {name: error_text}


# --- RC-3: a failed Temporal read names its error, in both forms ------------


def test_a_failed_temporal_read_names_its_error_in_text_and_json(monkeypatch, capsys):
    """RC-3. Extends the RC-1 test: pins the exact text line and the exact
    JSON payload a failed connect-or-describe read produces.

    Proving mutations, each must turn this red: (a) deleting the
    `errors["schedule"]`/`errors["in_flight"]` assignments in `_idle_errors`;
    (b) making `_in_flight_idle_input` return `0` on error.
    """
    _install_raising_temporal_client(monkeypatch, "connect")
    _stub_local_describers(monkeypatch)
    _quiet_notifications(monkeypatch)

    exit_code = asyncio.run(
        schedule_status._main(address="localhost:7233", namespace="default", schedule_id=SCHEDULE_ID)
    )
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "idle_schedule_error: double: connection refused" in out
    assert "running: unknown" in out

    exit_code = asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
            json_output=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload == {
        "idle_reason": {
            "running": "unknown",
            "reasons": [
                ["schedule_unknown", "schedule could not be read"],
                ["in_flight_unknown", "in_flight could not be read"],
            ],
            "inputs_not_served": [],
            "errors": {
                "schedule": "double: connection refused",
                "in_flight": "double: connection refused",
            },
        }
    }


# --- RC-4: a failed schedule-note read becomes 'unknown', not an empty note -


def test_a_failed_schedule_note_read_becomes_unknown_not_an_empty_note(monkeypatch, capsys):
    """RC-4. AC-3 says every read, local or Temporal, that fails becomes
    'unknown' with the error text -- today a failed
    `dispatch_schedule_pause_state` call degraded to `note = ""`, so a
    capacity pause reported as `schedule_paused: ` (empty detail) with no
    error recorded anywhere. The schedule/in-flight Temporal read itself
    (connect + describe_factory_schedule_status) still succeeds here, so the
    report, the in_flight_workflows line, the notifications and the exit
    code stay exactly what a fully successful read gives.

    Proving mutations, each must turn this red: deleting the `note_error`
    threading so a capacity-paused schedule with a failed note read reports
    `running: false` with `[["schedule_paused", ""]]`` instead of
    `running: unknown` with `errors == {"schedule": ...}`.
    """
    status = FactoryScheduleStatus(schedule_id=SCHEDULE_ID, paused=True, in_flight=(), recent=())

    class _FakeClient:
        @staticmethod
        async def connect(_address, namespace=None):
            return object()

    monkeypatch.setitem(sys.modules, "temporalio.client", types.SimpleNamespace(Client=_FakeClient))

    async def fake_describe(_client, *, schedule_id):
        return status

    async def failing_pause_state(_client, *, schedule_id):
        raise RuntimeError("double: note read failed")

    monkeypatch.setattr(schedule_status, "describe_factory_schedule_status", fake_describe)
    monkeypatch.setattr(schedule_status, "dispatch_schedule_pause_state", failing_pause_state)
    _stub_local_describers(monkeypatch)
    _quiet_notifications(monkeypatch)

    render_calls = []
    real_render = schedule_status.render_schedule_status

    def spying_render(*args, **kwargs):
        render_calls.append(1)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(schedule_status, "render_schedule_status", spying_render)

    exit_code = asyncio.run(
        schedule_status._main(address="localhost:7233", namespace="default", schedule_id=SCHEDULE_ID)
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert render_calls == [1]
    assert "in_flight_workflows: 0" in out

    monkeypatch.setattr(schedule_status, "render_schedule_status", spying_render)
    exit_code = asyncio.run(
        schedule_status._main(
            address="localhost:7233",
            namespace="default",
            schedule_id=SCHEDULE_ID,
            json_output=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["idle_reason"]["running"] == "unknown"
    assert payload["idle_reason"]["reasons"] == [
        ["schedule_unknown", "schedule could not be read"]
    ]
    assert payload["idle_reason"]["errors"] == {"schedule": "double: note read failed"}
