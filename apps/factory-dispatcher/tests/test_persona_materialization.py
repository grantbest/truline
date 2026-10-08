"""dev.finding bd4b2a9a: personas are materialized in-process from the
clone's own docs/agents/, and no clone-supplied code ever runs to do it.

A preserved baseline is a prior worker's UNGATED diff. Running
<clone>/scripts/materialize_agents.py as a subprocess -- the old behaviour --
meant a failed attempt could plant that script and the next dispatch would
execute it as the dispatcher's own user. These tests establish that
dispatch.materialize_personas/containment.materialize_personas copy bytes
in-process instead, and refuse (rather than follow) anything symlink-shaped
at either the source or the destination.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import containment
import dispatch

POLECAT_TEXT = "---\nrole: polecat\n---\nPOLECAT BODY\n"
ARCHITECT_TEXT = "---\nrole: architect\n---\nARCHITECT BODY\n"
OUTSIDE_POLECAT_TEXT = "---\nrole: polecat\n---\nOUTSIDE POLECAT MARKER\n"
OUTSIDE_ARCHITECT_TEXT = "---\nrole: architect\n---\nOUTSIDE ARCHITECT MARKER\n"

PLANTED_MATERIALIZER = """\
import pathlib
here = pathlib.Path(__file__).resolve().parents[1]
(here.parent / "marker.txt").write_text("planted-ran")
target = here / ".claude" / "agents"
target.mkdir(parents=True, exist_ok=True)
(target / "polecat-developer.md").write_text("BOGUS")
"""


def _make_clone(root: pathlib.Path) -> pathlib.Path:
    clone = root / "clone"
    agents = clone / "docs" / "agents"
    agents.mkdir(parents=True)
    (agents / dispatch.POLECAT_PERSONA).write_text(POLECAT_TEXT)
    (agents / dispatch.ARCHITECT_PERSONA).write_text(ARCHITECT_TEXT)
    return clone


def _plant_materializer(clone: pathlib.Path) -> None:
    scripts = clone / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "materialize_agents.py").write_text(PLANTED_MATERIALIZER)


def test_planted_materializer_never_runs(tmp_path):
    clone = _make_clone(tmp_path)
    _plant_materializer(clone)
    marker = tmp_path / "marker.txt"

    dispatch.materialize_personas(clone)

    assert not marker.exists()
    for name in (dispatch.POLECAT_PERSONA, dispatch.ARCHITECT_PERSONA):
        dest = clone / ".claude" / "agents" / name
        src = clone / "docs" / "agents" / name
        assert dest.read_bytes() == src.read_bytes()


def test_edit_to_source_reaches_materialized_copy(tmp_path):
    clone = _make_clone(tmp_path)
    dispatch.materialize_personas(clone)

    edited = "---\nrole: polecat\n---\nEDITED BODY\n"
    (clone / "docs" / "agents" / dispatch.POLECAT_PERSONA).write_text(edited)
    dispatch.materialize_personas(clone)

    dest = clone / ".claude" / "agents" / dispatch.POLECAT_PERSONA
    assert dest.read_text() == edited


def test_no_subprocess_invoked(tmp_path, monkeypatch):
    clone = _make_clone(tmp_path)
    _plant_materializer(clone)

    def _boom(*_args, **_kwargs):
        raise AssertionError("subprocess must not be invoked")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    dispatch.materialize_personas(clone)

    assert not (tmp_path / "marker.txt").exists()


def test_symlinked_source_refused_and_marker_never_leaks(tmp_path):
    clone = _make_clone(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker_string = "TOP-SECRET-MARKER-9f3c"
    secret = outside / "secret.txt"
    secret.write_text(marker_string)

    arch_path = clone / "docs" / "agents" / dispatch.ARCHITECT_PERSONA
    arch_path.unlink()
    arch_path.symlink_to(secret)

    with pytest.raises(dispatch.DispatchEnvironmentError) as exc1:
        dispatch.materialize_personas(clone)
    assert "docs/agents/architect-sme.md" in str(exc1.value)
    assert marker_string not in str(exc1.value)

    with pytest.raises(dispatch.DispatchEnvironmentError) as exc2:
        dispatch.assemble_personas_prompt(clone, {"risk_class": "behavioral"})
    assert "docs/agents/architect-sme.md" in str(exc2.value)
    assert marker_string not in str(exc2.value)

    claude_dir = clone / ".claude"
    for path in claude_dir.rglob("*") if claude_dir.exists() else ():
        if path.is_file():
            assert marker_string not in path.read_text(errors="ignore")
    assert secret.read_text() == marker_string


def test_symlinked_destination_directory_refused(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".claude").mkdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (clone / ".claude" / "agents").symlink_to(outside_dir)

    with pytest.raises(dispatch.DispatchEnvironmentError):
        dispatch.materialize_personas(clone)

    assert list(outside_dir.iterdir()) == []


def test_destination_persona_path_is_directory_refused(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".claude" / "agents" / dispatch.POLECAT_PERSONA).mkdir(parents=True)

    with pytest.raises(dispatch.DispatchEnvironmentError):
        dispatch.materialize_personas(clone)


def test_claude_dir_is_regular_file_refused(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".claude").write_text("not a directory")

    with pytest.raises(dispatch.DispatchEnvironmentError):
        dispatch.materialize_personas(clone)


def test_claude_dir_created_when_absent(tmp_path):
    clone = _make_clone(tmp_path)
    assert not (clone / ".claude").exists()

    dispatch.materialize_personas(clone)

    assert (clone / ".claude" / "agents" / dispatch.POLECAT_PERSONA).is_file()


def _fault_symlinked_source(clone: pathlib.Path, root: pathlib.Path) -> None:
    arch_path = clone / "docs" / "agents" / dispatch.ARCHITECT_PERSONA
    arch_path.unlink()
    outside = root / "elsewhere.txt"
    outside.write_text("x")
    arch_path.symlink_to(outside)


def _fault_symlinked_claude_agents(clone: pathlib.Path, root: pathlib.Path) -> None:
    (clone / ".claude").mkdir()
    outside_dir = root / "outside-dir"
    outside_dir.mkdir()
    (clone / ".claude" / "agents").symlink_to(outside_dir)


def _fault_destination_is_directory(clone: pathlib.Path, root: pathlib.Path) -> None:
    (clone / ".claude" / "agents" / dispatch.POLECAT_PERSONA).mkdir(parents=True)


def _fault_claude_is_regular_file(clone: pathlib.Path, root: pathlib.Path) -> None:
    (clone / ".claude").write_text("nope")


def _fault_claude_agents_is_regular_file(clone: pathlib.Path, root: pathlib.Path) -> None:
    (clone / ".claude").mkdir()
    (clone / ".claude" / "agents").write_text("nope")


def _plant_outside_agents(outside_agents: pathlib.Path) -> None:
    outside_agents.mkdir(parents=True)
    (outside_agents / dispatch.POLECAT_PERSONA).write_text(OUTSIDE_POLECAT_TEXT)
    (outside_agents / dispatch.ARCHITECT_PERSONA).write_text(OUTSIDE_ARCHITECT_TEXT)


def _fault_docs_agents_symlinked_outside(clone: pathlib.Path, root: pathlib.Path) -> None:
    agents_dir = clone / "docs" / "agents"
    shutil.rmtree(agents_dir)
    outside_agents = root / "outside-agents"
    _plant_outside_agents(outside_agents)
    agents_dir.symlink_to(outside_agents)


def _fault_docs_symlinked_outside(clone: pathlib.Path, root: pathlib.Path) -> None:
    docs_dir = clone / "docs"
    shutil.rmtree(docs_dir)
    outside_docs = root / "outside-docs"
    _plant_outside_agents(outside_docs / "agents")
    docs_dir.symlink_to(outside_docs)


@pytest.mark.parametrize(
    "fault",
    [
        _fault_symlinked_source,
        _fault_symlinked_claude_agents,
        _fault_destination_is_directory,
        _fault_claude_is_regular_file,
        _fault_claude_agents_is_regular_file,
        _fault_docs_agents_symlinked_outside,
        _fault_docs_symlinked_outside,
    ],
)
def test_refusal_reason_stable_across_clones_and_path_free(tmp_path, fault):
    root_a = tmp_path / "clone-root-one"
    root_b = tmp_path / "an-entirely-differently-named-root"
    root_a.mkdir()
    root_b.mkdir()

    clone_a = _make_clone(root_a)
    fault(clone_a, root_a)
    reason_a = containment.materialize_personas(
        clone_a, (dispatch.POLECAT_PERSONA, dispatch.ARCHITECT_PERSONA)
    )

    clone_b = _make_clone(root_b)
    fault(clone_b, root_b)
    reason_b = containment.materialize_personas(
        clone_b, (dispatch.POLECAT_PERSONA, dispatch.ARCHITECT_PERSONA)
    )

    assert reason_a is not None and reason_b is not None
    assert reason_a == reason_b
    assert str(tmp_path) not in reason_a
    assert str(root_a) not in reason_a
    assert str(root_b) not in reason_b


def test_symlinked_destination_file_is_replaced_without_touching_target(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".claude" / "agents").mkdir(parents=True)
    outside_file = tmp_path / "outside-file.md"
    outside_content = "ORIGINAL OUTSIDE CONTENT"
    outside_file.write_text(outside_content)
    dest = clone / ".claude" / "agents" / dispatch.POLECAT_PERSONA
    dest.symlink_to(outside_file)

    dispatch.materialize_personas(clone)

    assert outside_file.read_text() == outside_content
    assert not dest.is_symlink()
    assert dest.read_bytes() == (
        clone / "docs" / "agents" / dispatch.POLECAT_PERSONA
    ).read_bytes()


@pytest.mark.parametrize(
    "fault",
    [_fault_docs_agents_symlinked_outside, _fault_docs_symlinked_outside],
)
def test_docs_or_docs_agents_symlinked_outside_clone_refused(tmp_path, fault):
    clone = _make_clone(tmp_path)
    fault(clone, tmp_path)

    with pytest.raises(dispatch.DispatchEnvironmentError) as exc1:
        dispatch.materialize_personas(clone)
    assert "docs/agents/polecat-developer.md: outside the clone" in str(exc1.value)
    assert "OUTSIDE POLECAT MARKER" not in str(exc1.value)

    with pytest.raises(dispatch.DispatchEnvironmentError) as exc2:
        dispatch.assemble_personas_prompt(clone, {"risk_class": "behavioral"})
    assert "docs/agents/polecat-developer.md: outside the clone" in str(exc2.value)
    assert "OUTSIDE POLECAT MARKER" not in str(exc2.value)

    claude_dir = clone / ".claude"
    for path in claude_dir.rglob("*") if claude_dir.exists() else ():
        if path.is_file():
            content = path.read_text(errors="ignore")
            assert "OUTSIDE POLECAT MARKER" not in content
            assert "OUTSIDE ARCHITECT MARKER" not in content


def test_claude_agents_is_regular_file_refused(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / ".claude").mkdir()
    (clone / ".claude" / "agents").write_text("not a directory")

    with pytest.raises(dispatch.DispatchEnvironmentError):
        dispatch.materialize_personas(clone)


def test_missing_source_raises(tmp_path):
    clone = _make_clone(tmp_path)
    (clone / "docs" / "agents" / dispatch.ARCHITECT_PERSONA).unlink()

    with pytest.raises(dispatch.DispatchEnvironmentError):
        dispatch.materialize_personas(clone)


def test_persona_constants_match_materialize_agents_script():
    script_path = (
        pathlib.Path(dispatch.__file__).resolve().parents[2]
        / "scripts"
        / "materialize_agents.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_materialize_agents_drift_check", script_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {dispatch.POLECAT_PERSONA, dispatch.ARCHITECT_PERSONA} == set(module.PERSONAS)
