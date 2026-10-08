"""stranded_bead_states.py -- the class, not just d861f65e-74ff-437e-a068-05e16b49c99a.

Finding 1's own shape is the first fixture below: an arch.incident bead in
state 'pending', which arch.incident's real machine (imported from
bead_rules.py, not copied) never declares as a key. The report-not-gate
contract is checked the same way traceability.py's own tests would: `main()`
returns 0 even when the population it found is non-empty, and non-zero only
when the check itself could not run.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import stranded_bead_states as sut  # noqa: E402

D861F65E_SHAPED_BEAD = {
    "id": "d861f65e-74ff-437e-a068-05e16b49c99a",
    "namespace": "arch",
    "type": "incident",
    "state": "pending",
}

TEST_MACHINES = {
    ("arch", "incident"): {
        "detected": frozenset({"mitigating", "closed"}),
        "mitigating": frozenset({"resolved", "detected"}),
        "resolved": frozenset({"closed", "detected"}),
        "closed": frozenset(),
    },
    ("dev", "task"): {
        "pending": frozenset({"doing"}),
        "doing": frozenset({"pending"}),
    },
}


# --- find_stranded_states -----------------------------------------------


def test_the_real_bead_rules_machine_flags_d861f65es_shape():
    """Imports the REAL bead_rules.STATE_MACHINES (the default), not a copy --
    if arch.incident's machine is ever widened to declare 'pending', this
    stops being a regression test for anything and should be revisited."""
    stranded = sut.find_stranded_states([D861F65E_SHAPED_BEAD])
    assert len(stranded) == 1
    assert stranded[0].bead_id == D861F65E_SHAPED_BEAD["id"]
    assert stranded[0].state == "pending"
    assert "detected" in stranded[0].declared_states
    assert "pending" not in stranded[0].declared_states


def test_a_bead_in_a_declared_state_is_not_flagged():
    bead = {**D861F65E_SHAPED_BEAD, "state": "detected"}
    assert sut.find_stranded_states([bead], TEST_MACHINES) == []


def test_a_bead_in_an_undeclared_state_is_flagged():
    bead = {**D861F65E_SHAPED_BEAD, "state": "pending"}
    stranded = sut.find_stranded_states([bead], TEST_MACHINES)
    assert len(stranded) == 1
    assert stranded[0].namespace == "arch"
    assert stranded[0].type == "incident"


def test_an_undeclared_machine_pair_is_never_flagged():
    """finance.* (and any other pair with no declared machine) must stay
    permissive here too, matching routes.py's STATE_MACHINES.get(...) -> None
    semantics -- an undeclared machine is not a violated one."""
    bead = {"id": "x", "namespace": "finance", "type": "transaction", "state": "anything"}
    assert sut.find_stranded_states([bead], TEST_MACHINES) == []


def test_multiple_beads_report_each_stranded_one_independently():
    beads = [
        D861F65E_SHAPED_BEAD,
        {"id": "clean-1", "namespace": "arch", "type": "incident", "state": "detected"},
        {"id": "dev-stranded", "namespace": "dev", "type": "task", "state": "wibble"},
    ]
    stranded = sut.find_stranded_states(beads, TEST_MACHINES)
    ids = {item.bead_id for item in stranded}
    assert ids == {D861F65E_SHAPED_BEAD["id"], "dev-stranded"}


# --- report formatting --------------------------------------------------


def test_format_report_names_every_stranded_bead():
    stranded = sut.find_stranded_states([D861F65E_SHAPED_BEAD], TEST_MACHINES)
    report = sut.format_report(stranded, beads_checked=1)
    assert D861F65E_SHAPED_BEAD["id"] in report
    assert "Stranded beads:   1" in report


def test_format_report_on_a_clean_population_names_none():
    report = sut.format_report([], beads_checked=5)
    assert "Stranded beads:   0" in report
    assert "d861f65e" not in report


# --- CLI: report, never gate ---------------------------------------------


def test_main_returns_zero_even_when_beads_are_stranded(tmp_path, capsys):
    path = tmp_path / "beads.json"
    path.write_text(json.dumps([D861F65E_SHAPED_BEAD]))

    exit_code = sut.main([str(path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Stranded beads:   1" in out


def test_main_returns_zero_on_a_clean_population(tmp_path, capsys):
    path = tmp_path / "beads.json"
    path.write_text(json.dumps([{**D861F65E_SHAPED_BEAD, "state": "detected"}]))

    exit_code = sut.main([str(path)])

    assert exit_code == 0
    assert "Stranded beads:   0" in capsys.readouterr().out


def test_main_returns_nonzero_when_it_cannot_run(tmp_path):
    path = tmp_path / "not-json.txt"
    path.write_text("{this is not valid json")

    assert sut.main([str(path)]) == 2


def test_main_accepts_a_beads_wrapper_dict(tmp_path):
    path = tmp_path / "beads.json"
    path.write_text(json.dumps({"beads": [D861F65E_SHAPED_BEAD]}))

    assert sut.main([str(path)]) == 0


def test_main_reads_stdin_when_no_file_given(monkeypatch, capsys):
    import io

    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps([D861F65E_SHAPED_BEAD]))
    )

    exit_code = sut.main([])

    assert exit_code == 0
    assert "Stranded beads:   1" in capsys.readouterr().out
