"""Tests for the release charter loader.

The real substrate is deliberately not involved. What these cover is the part of
the loader that can corrupt the mirror quietly: the git/substrate split over
release *state*, resolution of the requirement references a charter cites, and
idempotence against the business key.

The single most important assertion here is
``test_apply_never_writes_state_after_the_create``. A charter is a file, and a
file cannot know whether the work shipped. If the loader ever patched state, a
`git revert` of a charter edit could walk a released release backwards, or a
typo could declare one shipped.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "release_load", REPO / "scripts" / "release-load.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


loader = _load()


def charter(ref="R26.01", **overrides):
    base = {
        "ref": ref,
        "name": "Trust earns its amendment",
        "objective": "Safety claims become measurements.",
        "sprints": ["33", "34", "35"],
        "opened_at": "2026-08-25",
        "declared_balance": {"enabling": 60, "risk": 40},
        "outcomes": [
            {
                "id": "O-1",
                "statement": "A bad deploy is undone by one command.",
                "work_class": "risk",
                "requirement_refs": ["PC-TRU-001/AC-1"],
            },
            {
                "id": "O-2",
                "statement": "Filed work names the release it serves.",
                "work_class": "enabling",
                "requirement_refs": [],
            },
        ],
    }
    base.update(overrides)
    return base


def write_charter(tmp_path, data, name=None):
    path = tmp_path / (name or f"{data['ref']}.json")
    path.write_text(json.dumps(data))
    return path


class FakeSubstrate:
    def __init__(self, reject_refs=None):
        self.beads = []
        self.creates = []
        self.patches = []
        self.reject_refs = set(reject_refs or [])
        self.next_id = 1

    def list_beads(self, bead_type, limit=1000):
        return list(self.beads)

    def create(self, bead_type, state, content):
        self.creates.append((bead_type, state, content))
        if content.get("ref") in self.reject_refs:
            raise loader.SubstrateError(422, "body containing secret-token", "/beads")
        bead = {
            "id": f"bead-{self.next_id}",
            "type": bead_type,
            "state": state,
            "content": content,
        }
        self.next_id += 1
        self.beads.append(bead)
        return bead

    def patch(self, bead_id, body):
        self.patches.append((bead_id, body))
        for bead in self.beads:
            if bead["id"] == bead_id:
                bead.update(body)
                return bead
        raise AssertionError(f"unknown bead id {bead_id}")


# --- discovery ---------------------------------------------------------------


def test_charters_are_discovered_by_enumerating_the_directory(tmp_path):
    """The rule PC-FAC-001/AC-5 puts on the requirement registries.

    The next charter is picked up with no code change and no second place to
    update.
    """
    write_charter(tmp_path, charter("R26.01"))
    write_charter(tmp_path, charter("R26.02"))

    assert [p.name for p in loader.charter_paths(tmp_path)] == [
        "R26.01.json",
        "R26.02.json",
    ]


def test_notes_files_are_not_charters(tmp_path):
    """Generated notes live in the same directory. Only *.json is a charter."""
    write_charter(tmp_path, charter("R26.01"))
    (tmp_path / "R26.01-notes.md").write_text("# notes")

    assert [p.name for p in loader.charter_paths(tmp_path)] == ["R26.01.json"]


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert loader.charter_paths(tmp_path / "nope") == []


# --- validation before any write ---------------------------------------------


def test_dangling_requirement_ref_stops_the_load(tmp_path):
    """A citation that resolves to nothing would render as a measurement.

    The release notes read these refs to report conformance movement; an
    unresolvable one would print a requirement that does not exist.
    """
    path = write_charter(
        tmp_path,
        charter(outcomes=[{
            "id": "O-1", "statement": "x", "work_class": "risk",
            "requirement_refs": ["PC-SUB-999/AC-1"],
        }]),
    )

    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([path])
    assert "PC-SUB-999/AC-1" in str(exc_info.value)


def test_a_resolvable_ref_loads(tmp_path):
    path = write_charter(tmp_path, charter())

    items = loader.load_charters([path])

    assert [item.ref for item in items] == ["R26.01"]


def _write_registry(directory: pathlib.Path, requirements: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "registry.json").write_text(json.dumps({
        "requirements": [
            {"id": rid, "acceptance_criteria": [{"id": cid} for cid in criteria]}
            for rid, criteria in requirements.items()
        ]
    }))


def test_a_missing_registry_directory_refuses_and_names_the_environment_not_the_citation(
    tmp_path,
):
    """Control: a present registry loads the charter normally, same as
    ``test_a_resolvable_ref_loads``. A missing registry directory must still
    refuse (fail closed) -- but naming the unreachable directory, not
    misreporting the charter's real, resolvable citation as dangling.
    """
    path = write_charter(tmp_path, charter())
    real_dir = tmp_path / "requirements"
    _write_registry(real_dir, {"PC-TRU-001": ["AC-1"]})

    items = loader.load_charters([path], requirements_dir=real_dir)
    assert [item.ref for item in items] == ["R26.01"]

    missing_dir = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([path], requirements_dir=missing_dir)

    message = str(exc_info.value)
    assert "not found" in message
    assert str(missing_dir) in message
    assert "PC-TRU-001/AC-1" not in message


def test_a_charter_may_not_carry_measurements(tmp_path):
    """Same line ArchRequirementContent draws. Refused before the network call,
    with the file name attached, so the author is told which file to fix."""
    path = write_charter(tmp_path, charter(actual_balance={"risk": 40}))

    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([path])
    assert "actual_balance" in str(exc_info.value)


def test_duplicate_refs_across_files_stop_the_load(tmp_path):
    first = write_charter(tmp_path, charter("R26.01"), name="a.json")
    second = write_charter(tmp_path, charter("R26.01"), name="b.json")

    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([first, second])
    assert "duplicate release ref" in str(exc_info.value)


def test_a_charter_with_no_ref_stops_the_load(tmp_path):
    data = charter()
    del data["ref"]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(data))

    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([path])
    assert "declares no ref" in str(exc_info.value)


def test_malformed_json_names_the_file(tmp_path):
    path = tmp_path / "R26.01.json"
    path.write_text("{ not json")

    with pytest.raises(SystemExit) as exc_info:
        loader.load_charters([path])
    assert "R26.01.json" in str(exc_info.value)


# --- the git / substrate split over state ------------------------------------


def test_creates_land_at_the_entry_state(tmp_path):
    path = write_charter(tmp_path, charter())
    sub = FakeSubstrate()

    loader.reconcile(sub, loader.load_charters([path]), apply=True)

    assert [(state) for _, state, _ in sub.creates] == ["planned"]


def test_apply_never_writes_state_after_the_create(tmp_path):
    """The load-bearing assertion of this module.

    A charter is a file and cannot know whether the work shipped. If the loader
    patched state, reverting a charter edit could walk a released release
    backwards, and a typo could declare one shipped. Structure comes from git;
    lifecycle is the substrate's alone (PQ-4, the Operator 2026-08-22).
    """
    path = write_charter(tmp_path, charter())
    sub = FakeSubstrate()
    loader.reconcile(sub, loader.load_charters([path]), apply=True)

    # The release moves on without the charter changing.
    sub.beads[0]["state"] = "released"

    edited = charter(objective="Safety claims become measurements, restated.")
    edited_path = write_charter(tmp_path, edited)
    loader.reconcile(sub, loader.load_charters([edited_path]), apply=True)

    assert sub.patches, "the content edit should have been mirrored"
    for _, body in sub.patches:
        assert "state" not in body, f"the loader wrote state: {body}"
    assert sub.beads[0]["state"] == "released"


def test_a_content_edit_is_mirrored(tmp_path):
    path = write_charter(tmp_path, charter())
    sub = FakeSubstrate()
    loader.reconcile(sub, loader.load_charters([path]), apply=True)

    edited_path = write_charter(tmp_path, charter(name="Trust, renamed"))
    plan = loader.reconcile(sub, loader.load_charters([edited_path]), apply=True)

    assert plan.updates == ["R26.01"]
    assert sub.beads[0]["content"]["name"] == "Trust, renamed"


# --- idempotence and failure handling ----------------------------------------


def test_dry_run_plans_creates_and_writes_nothing(tmp_path):
    path = write_charter(tmp_path, charter())
    sub = FakeSubstrate()

    plan = loader.reconcile(sub, loader.load_charters([path]))

    assert plan.creates == ["R26.01"]
    assert plan.dry_run
    assert sub.creates == [] and sub.patches == []


def test_apply_twice_is_idempotent(tmp_path):
    """PRIN-014: the same revision twice writes nothing.

    This loader runs on a 15-minute Temporal schedule; a non-idempotent one
    would rewrite every charter ninety-six times a day and make `updated_at`
    meaningless.
    """
    path = write_charter(tmp_path, charter())
    sub = FakeSubstrate()

    loader.reconcile(sub, loader.load_charters([path]), apply=True)
    second = loader.reconcile(sub, loader.load_charters([path]), apply=True)

    assert second.unchanged == ["R26.01"]
    assert second.creates == [] and second.updates == []
    assert len(sub.creates) == 1 and sub.patches == []


def test_source_class_is_authored(tmp_path):
    """So an automated writer overwriting a charter gets a 409, not a silent win.

    A release objective is written by a person; nothing derived may restate it.
    """
    path = write_charter(tmp_path, charter())

    items = loader.load_charters([path])

    assert items[0].content["source_class"] == "authored"


def test_rejected_payload_reports_the_ref_and_leaks_no_body(capsys):
    """A 422 body can carry whatever was posted. The report prints the ref and
    the status, never the response."""
    sub = FakeSubstrate(reject_refs={"R26.01"})
    item = loader.ReleaseItem(
        ref="R26.01", source="R26.01.json", content=charter()
    )

    plan = loader.reconcile(sub, [item], apply=True)
    loader.report(plan)

    out = capsys.readouterr().out
    assert "R26.01" in out
    assert "secret-token" not in out
    assert plan.errors


def test_one_rejection_does_not_stop_the_rest(tmp_path):
    sub = FakeSubstrate(reject_refs={"R26.01"})
    items = [
        loader.ReleaseItem(ref="R26.01", source="a.json", content=charter("R26.01")),
        loader.ReleaseItem(ref="R26.02", source="b.json", content=charter("R26.02")),
    ]

    plan = loader.reconcile(sub, items, apply=True)

    assert len(plan.errors) == 1
    assert [bead["content"]["ref"] for bead in sub.beads] == ["R26.02"]


# --- the charter actually committed ------------------------------------------


def test_the_committed_charters_load_and_validate():
    """The real docs/releases/ content, against the real registries and the
    real substrate content model.

    Catches a charter that was hand-edited into an unloadable state — the file
    is authored by a person and reviewed by PR, so nothing else would. The
    schema check is not cosmetic: OPS-measured on 2026-09-12, R26.09.json
    merged with ``opened_at: null`` and this test's two assertions alone
    (``ref``, ``outcomes``) said nothing about it — see
    ``test_a_null_opened_at_is_caught_the_way_r26_09_was_not`` below for the
    charter that exposed the gap.
    """
    items = loader.load_charters()

    for item in items:
        assert item.content["ref"], item.source
        assert item.content["outcomes"], item.source
        error = loader.release_content_validation_error(item.content)
        assert error is None, f"{item.source}: {error}"


def test_a_null_opened_at_is_caught_the_way_r26_09_was_not(tmp_path):
    """The fixture is R26.09 as it merged in #771: a real charter, opened_at
    null. ArchReleaseContent requires a real date, so the substrate would have
    rejected this at ``POST /beads`` — this proves ``--dry-run`` now catches
    it first, reporting the ref as an error and never as ``create R26.09``.
    """
    broken = charter(ref="R26.09", opened_at=None)
    path = write_charter(tmp_path, broken)

    plan = loader.reconcile(FakeSubstrate(), loader.load_charters([path]))

    assert plan.creates == []
    assert len(plan.errors) == 1
    assert "R26.09" in plan.errors[0]
    assert "opened_at: Input should be a valid date" in plan.errors[0]

    fixed_path = write_charter(
        tmp_path, charter(ref="R26.09", opened_at="2026-12-30"), name="R26.09.json"
    )
    plan = loader.reconcile(FakeSubstrate(), loader.load_charters([fixed_path]))

    assert plan.creates == ["R26.09"]
    assert plan.errors == []


# --- --check mode (PC-ASR-008/AC-6) -------------------------------------------


def _valid_policy(**overrides):
    base = {
        "urgency_bands": {"medium": 0.33, "high": 0.66},
        "aging_days": 14,
        "max_aging_steps": 3,
        "balance_tolerance_pct": 25,
        "absent_after_elapsed_pct": 0.33,
        "no_landed_work_after_elapsed_pct": 0.5,
        "criterion_stale_days": 30,
        "min_classified": 5,
        "rework_rate_pct": 40,
        "unmeasured_actionable_after_hours": 48,
        "re_alert_interval_hours": 168,
        "reconciler_interval_seconds": 3600,
        "dispatch_interval_seconds": 900,
    }
    base.update(overrides)
    return base


def _write_policy(path, data):
    path.write_text(json.dumps(data))
    return path


def _releases_dir(tmp_path):
    """A charter directory holding one valid charter, kept separate from
    wherever the test writes its policy file -- ``charter_paths`` globs
    every ``*.json`` in the directory, and a policy file dropped in the same
    directory as the charters would otherwise be (wrongly) read as one."""
    directory = tmp_path / "releases"
    directory.mkdir()
    write_charter(directory, charter())
    return directory


def _raising_substrate_class(*_args, **_kwargs):
    raise AssertionError("--check must never construct a Substrate client")


def _git_blob_sha(data: bytes) -> str:
    """The git blob sha of ``data``, computed independently of
    ``health_policy._git_blob_sha`` so this test does not become circular
    with the module it is pinning the output of."""
    import hashlib

    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def test_health_policy_validation_check_passes_a_valid_policy_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)
    releases_dir = _releases_dir(tmp_path)
    policy_data = _valid_policy()
    policy_path = _write_policy(tmp_path / "policy.json", policy_data)

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 0
    out = capsys.readouterr().out.splitlines()
    charter_path = loader.charter_paths(releases_dir)[0]
    sha = _git_blob_sha(policy_path.read_bytes())
    assert f"ok {charter_path}" in out
    assert out[-1] == f"ok {policy_path} (revision {sha})"


def test_health_policy_validation_check_refuses_an_invalid_charter(tmp_path, capsys):
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    charter_path = write_charter(releases_dir, charter(ref="R26.09", opened_at=None))
    policy_path = _write_policy(tmp_path / "policy.json", _valid_policy())

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 1
    out, err = capsys.readouterr()
    assert str(charter_path) in err
    assert "opened_at: Input should be a valid date" in err
    assert f"ok {charter_path}" not in out


def test_health_policy_validation_check_refuses_a_charter_with_no_ref(tmp_path):
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    data = charter()
    del data["ref"]
    (releases_dir / "broken.json").write_text(json.dumps(data))
    policy_path = _write_policy(tmp_path / "policy.json", _valid_policy())

    with pytest.raises(SystemExit) as exc_info:
        loader.main(
            ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
        )
    assert "declares no ref" in str(exc_info.value)


def test_health_policy_validation_check_names_the_file_and_the_key_on_invalid_value(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)
    releases_dir = _releases_dir(tmp_path)
    policy_path = _write_policy(
        tmp_path / "policy.json", _valid_policy(balance_tolerance_pct=150)
    )

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err == (
        f"health_policy_validation_error: {policy_path}: balance_tolerance_pct: "
        "Input should be less than or equal to 100\n"
    )


def test_health_policy_validation_check_names_an_unknown_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)
    releases_dir = _releases_dir(tmp_path)
    policy_path = _write_policy(
        tmp_path / "policy.json", _valid_policy(unexpected_key="x")
    )

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "unexpected_key" in err
    assert str(policy_path) in err


def test_health_policy_validation_check_names_the_path_on_malformed_json(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)
    releases_dir = _releases_dir(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{ not json")

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert str(policy_path) in err
    assert "not valid JSON" in err


def test_health_policy_validation_check_names_the_path_on_a_missing_file(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)
    releases_dir = _releases_dir(tmp_path)
    policy_path = tmp_path / "does-not-exist.json"

    exit_code = loader.main(
        ["--check", "--releases-dir", str(releases_dir), "--policy", str(policy_path)]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert str(policy_path) in err
    assert "missing" in err


def test_check_with_apply_is_refused_by_argparse():
    with pytest.raises(SystemExit) as exc_info:
        loader.main(["--check", "--apply"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("mode_flags", [["--dry-run"], []])
def test_policy_and_releases_dir_flags_are_refused_without_check(mode_flags, tmp_path):
    policy_path = tmp_path / "policy.json"
    with pytest.raises(SystemExit) as exc_info:
        loader.main([*mode_flags, "--policy", str(policy_path)])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        loader.main([*mode_flags, "--releases-dir", str(tmp_path)])
    assert exc_info.value.code == 2


def test_dry_run_never_reads_the_health_policy(monkeypatch, capsys):
    """--apply and --dry-run reconcile charters only. A patched
    ``HEALTH_POLICY_PATH`` alone would be invisible to a
    ``from health_policy import load_health_policy`` binding, so both the
    loader's own reference and the ``health_policy`` module's are replaced
    with a recorder that fails the test if called.
    """
    import health_policy as health_policy_module

    def _recorder(*_args, **_kwargs):
        raise AssertionError("--dry-run must never read the health policy")

    monkeypatch.setattr(loader, "load_health_policy", _recorder)
    monkeypatch.setattr(health_policy_module, "load_health_policy", _recorder)
    monkeypatch.setattr(loader, "Substrate", FakeSubstrate)

    exit_code = loader.main(["--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "release-load: DRY RUN" in out


def test_the_committed_health_policy_passes_check(monkeypatch):
    """The real docs/releases/policy/health-policy.json, read for real, with
    no store involved."""
    monkeypatch.setattr(loader, "Substrate", _raising_substrate_class)

    assert loader.main(["--check"]) == 0
