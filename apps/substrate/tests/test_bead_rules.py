"""The lifted state-machine and edge-vocabulary rules — no FastAPI, no database.

DB-free: imports ``src.bead_rules`` only, so this runs without DATABASE_URL.
"""

from src.bead_rules import STATE_MACHINE_ENTRY_STATES, STATE_MACHINES


def test_dev_release_machine_registered():
    """dev.release (R26.12/B20, PC-DEL-004/AC-3): a verdict record is born
    recorded and never moves — exactly one state, with no edges out of it,
    and that state is the entry state.
    """
    machine = STATE_MACHINES[("dev", "release")]
    assert set(machine) == {"verdict_recorded"}
    assert machine["verdict_recorded"] == frozenset()
    assert STATE_MACHINE_ENTRY_STATES[("dev", "release")] == frozenset({"verdict_recorded"})


def test_release_health_machine_registered():
    """arch.release_health (R26.12/B11, PC-ASR-008/AC-1): a six-state
    machine entered "unmeasured" only, with "closed" terminal.
    """
    machine = STATE_MACHINES[("arch", "release_health")]
    assert machine == {
        "unmeasured": frozenset({"on_track", "drifting", "breached", "closed"}),
        "on_track": frozenset({"drifting", "breached", "unmeasured", "closed"}),
        "drifting": frozenset({"on_track", "breached", "accepted", "unmeasured", "closed"}),
        "breached": frozenset({"on_track", "drifting", "accepted", "unmeasured", "closed"}),
        "accepted": frozenset({"on_track", "drifting", "breached", "unmeasured", "closed"}),
        "closed": frozenset(),
    }
    assert machine["closed"] == frozenset()
    assert STATE_MACHINE_ENTRY_STATES[("arch", "release_health")] == frozenset({"unmeasured"})
