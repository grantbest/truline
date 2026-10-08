"""The traceability contract's own precondition.

``schemas.py`` says the rollout "warns for one sprint before it refuses, because
the in-flight non-conforming population cannot currently be counted." That sprint
passed and the count was never built, so the refusal could not be scheduled.

The two properties that matter most here are the ones that decide whether the
refusal can ever be switched on: a reference that points nowhere is counted
separately from a task that honestly carries none, and registries are discovered
from the directory rather than named — so adding one needs no code change.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import traceability as tr  # noqa: E402


def _registry_file(directory: pathlib.Path, name: str, requirements: dict) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({
        "registry": {"id": name},
        "requirements": [
            {
                "id": rid,
                "acceptance_criteria": [{"id": cid} for cid in criteria],
            }
            for rid, criteria in requirements.items()
        ],
    }))
    return path


def _task(identifier, *, refs=None, nfrs=None, arch=None):
    content = {"title": f"task {identifier}"}
    if refs is not None:
        content["requirement_refs"] = refs
    if nfrs is not None:
        content["nfrs"] = nfrs
    if arch is not None:
        content["arch_impact"] = arch
    return {"id": identifier, "content": content}


# --- the three populations are distinct -------------------------------------


def test_the_three_populations_are_counted_separately(tmp_path):
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": ["AC-1"]})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([
        _task("conforming", refs=["LO-CAT-004"]),
        _task("bare"),
        _task("dangling", refs=["LO-CAT-999"]),
    ], registry)

    assert report.references_resolve == ["conforming"]
    assert report.no_references == ["bare"]
    assert [i for i, _ in report.references_dangling] == ["dangling"]


def test_a_reference_pointing_nowhere_is_not_the_same_as_none(tmp_path):
    """The validator's own warning: it reads as traceability while pointing nowhere."""
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": []})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([_task("dangling", refs=["LO-CAT-999"])], registry)

    assert report.no_references == []
    assert report.references_dangling == [("dangling", ["LO-CAT-999"])]


def test_a_task_is_dangling_if_any_single_reference_fails(tmp_path):
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": []})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks(
        [_task("mixed", refs=["LO-CAT-004", "LO-CAT-999"])], registry
    )

    assert report.references_resolve == []
    assert report.references_dangling == [("mixed", ["LO-CAT-999"])]


# --- both reference forms ---------------------------------------------------


def test_criterion_level_references_resolve(tmp_path):
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": ["AC-1", "AC-2"]})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([_task("crit", refs=["LO-CAT-004/AC-2"])], registry)

    assert report.references_resolve == ["crit"]


def test_a_criterion_that_does_not_exist_is_dangling(tmp_path):
    """The requirement existing is not enough — the criterion is the claim."""
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": ["AC-1"]})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([_task("crit", refs=["LO-CAT-004/AC-9"])], registry)

    assert report.references_dangling == [("crit", ["LO-CAT-004/AC-9"])]


def test_a_malformed_reference_is_dangling_rather_than_crashing(tmp_path):
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([_task("junk", refs=["not a reference"])], registry)

    assert report.references_dangling == [("junk", ["not a reference"])]


# --- registries are discovered, not named -----------------------------------


def test_a_reference_defined_only_in_a_second_registry_resolves(tmp_path):
    """The property that lets the refusal ever be switched on.

    Until REG-PLATFORM existed, every factory task was platform work with no
    requirement it could legally cite, because the only registry scoped the
    platform out. Discovering registries from the directory means the next one
    is counted with no code change.
    """
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": []})
    _registry_file(tmp_path, "platform.json", {"PC-SUB-001": ["AC-1"]})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([
        _task("product", refs=["LO-CAT-004"]),
        _task("platform", refs=["PC-SUB-001/AC-1"]),
    ], registry)

    assert sorted(report.references_resolve) == ["platform", "product"]
    assert registry.sources == ("lifeops.json", "platform.json")


def test_a_malformed_registry_does_not_take_down_the_count(tmp_path):
    _registry_file(tmp_path, "good.json", {"LO-CAT-004": []})
    (tmp_path / "broken.json").write_text("{not json")

    registry = tr.load_registries(tmp_path)

    assert registry.resolves("LO-CAT-004")
    assert registry.sources == ("good.json",)


def test_no_requirements_directory_is_survivable(tmp_path):
    registry = tr.load_registries(tmp_path / "absent")

    assert registry.sources == ()
    report = tr.audit_tasks([_task("bare")], registry)
    assert report.no_references == ["bare"]


def test_a_missing_directory_is_distinguishable_from_an_empty_one(tmp_path):
    """A missing directory and a real, empty one must not be the same fact.

    Both leave sources=(), 0 requirements, and resolves() False for every
    reference — that much is unavoidable, since there is nothing to read
    either way. But a caller (file_task.py's refusal) must be able to tell
    "the environment did not provide a registry" apart from "the registry was
    there and genuinely empty", and it can only do that if the Registry itself
    carries the distinction as data.
    """
    missing = tmp_path / "does-not-exist"
    empty = tmp_path / "empty-but-real"
    empty.mkdir()

    missing_registry = tr.load_registries(missing)
    empty_registry = tr.load_registries(empty)

    assert missing_registry.directory_missing is True
    assert empty_registry.directory_missing is False

    # Everything else about them is, and must remain, identical — that
    # equality is the bug this field exists to let a caller see past.
    assert missing_registry.sources == empty_registry.sources == ()
    assert missing_registry.requirements == empty_registry.requirements == frozenset()
    assert missing_registry.resolves("PC-FAC-001/AC-5") is False
    assert empty_registry.resolves("PC-FAC-001/AC-5") is False


# --- the other two fields are counted separately ----------------------------


def test_nfrs_and_arch_impact_are_counted_apart_from_references(tmp_path):
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": []})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([
        _task("refs-only", refs=["LO-CAT-004"]),
        _task("full", refs=["LO-CAT-004"], nfrs=[{"category": "cost"}], arch={"applications": []}),
    ], registry)

    assert report.fields["requirement_refs"].present == ["refs-only", "full"]
    assert report.fields["nfrs"].present == ["full"]
    assert report.fields["nfrs"].absent == ["refs-only"]
    assert report.fields["arch_impact"].present == ["full"]


def test_an_empty_list_counts_as_absent_not_carried(tmp_path):
    """Writing an empty list is not the same claim as carrying the field."""
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([_task("empty", refs=[], nfrs=[])], registry)

    assert report.fields["nfrs"].absent == ["empty"]
    assert report.no_references == ["empty"]


# --- it reports; it does not gate -------------------------------------------


def test_the_exit_code_does_not_depend_on_the_population(tmp_path, capsys):
    """Non-conforming tasks must not fail the run. That flip is a later decision."""
    (tmp_path / "docs" / "requirements").mkdir(parents=True)
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([_task("bare"), _task("dangling", refs=["ZZ-ZZZ-001"])]))

    code = tr.main([str(tasks), "--repo", str(tmp_path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "bare" in out and "dangling" in out


def test_a_missing_registry_directory_warns_rather_than_blaming_the_citation(tmp_path, capsys):
    """Control: a present (if empty) registry reports a dangling reference as
    dangling, plainly -- see ``test_the_exit_code_does_not_depend_on_the_population``.
    A missing registry directory must not let that same reference read as an
    equally-plain dangling citation: the report must say the registry itself
    was unreachable, and the exit code (this tool only ever reports) stays 0
    either way.
    """
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([_task("dangling", refs=["PC-TRU-001/AC-1"])]))

    present_repo = tmp_path / "present"
    (present_repo / "docs" / "requirements").mkdir(parents=True)
    code = tr.main([str(tasks), "--repo", str(present_repo)])
    assert code == 0
    control_out = capsys.readouterr().out
    assert "WARNING" not in control_out

    missing_repo = tmp_path / "missing"
    missing_repo.mkdir()
    code = tr.main([str(tasks), "--repo", str(missing_repo)])

    assert code == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "unreachable" in out


def test_a_missing_task_file_is_a_tool_failure(tmp_path, capsys):
    code = tr.main([str(tmp_path / "nope.json"), "--repo", str(tmp_path)])

    assert code == 2
    assert "could not run" in capsys.readouterr().err


def test_records_are_accepted_as_beads_or_bare_content(tmp_path):
    _registry_file(tmp_path, "lifeops.json", {"LO-CAT-004": []})
    registry = tr.load_registries(tmp_path)

    report = tr.audit_tasks([
        {"id": "bead", "content": {"requirement_refs": ["LO-CAT-004"]}},
        {"title": "bare content", "requirement_refs": ["LO-CAT-004"]},
    ], registry)

    assert len(report.references_resolve) == 2
    assert "bare content" in report.references_resolve
