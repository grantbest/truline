from __future__ import annotations

import pathlib
import re
import sys
import tempfile


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import readme_conformance as rc  # noqa: E402


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _schemas_py(root: pathlib.Path, type_names: tuple[str, ...]) -> None:
    lines = [
        "from typing import Dict",
        "",
        "class C:",
        "    pass",
        "",
        "ARCH_TYPE_SCHEMAS: Dict[str, type] = {",
    ]
    lines += [f'    "{name}": C,' for name in type_names]
    lines.append("}")
    _write(root / rc.SCHEMAS_PATH, "\n".join(lines) + "\n")


def _ea_conformance_py(root: pathlib.Path, checks_count: int) -> None:
    body = "\n\n".join(f"def check_{i}(r):\n    pass" for i in range(checks_count))
    _write(root / rc.EA_CONFORMANCE_PATH, body + "\n")


def _model_yaml(root: pathlib.Path, rel: pathlib.Path, key: str, count: int) -> None:
    items = "\n".join(f"  - ref: x{i}\n    state: active" for i in range(count)) or "  []"
    _write(root / rel, f"{key}:\n{items}\n")


#: The fixture's default schedule_runtime.py declarations, matched exactly
#: by the default README machinery bullets below -- a family name (used as
#: the "<PREFIX>" in "<PREFIX>_SCHEDULE_ID") mapped to (schedule_id,
#: interval_seconds).
_DEFAULT_SCHEDULES: dict[str, tuple[str, int]] = {
    "EA_APPLY": ("factory-ea-apply-15m", 15 * 60),
    "EA_OBSERVATION": ("factory-ea-observation-nightly", 24 * 60 * 60),
}


def _schedule_runtime_py(
    root: pathlib.Path,
    schedules: dict[str, tuple[str, int]],
    *,
    annotated_prefixes: frozenset[str] = frozenset(),
) -> None:
    """Write a synthetic schedule_runtime.py. A prefix in ``annotated_prefixes``
    gets its pair declared with a type annotation (``NAME: str = ...`` /
    ``NAME: int = ...``) instead of a plain assignment -- the shape this task
    fixes the walker to read identically either way."""
    lines = []
    for prefix, (schedule_id, interval_seconds) in schedules.items():
        if prefix in annotated_prefixes:
            lines.append(f'{prefix}_SCHEDULE_ID: str = "{schedule_id}"')
            lines.append(f"{prefix}_SCHEDULE_INTERVAL_SECONDS: int = {interval_seconds}")
        else:
            lines.append(f'{prefix}_SCHEDULE_ID = "{schedule_id}"')
            lines.append(f"{prefix}_SCHEDULE_INTERVAL_SECONDS = {interval_seconds}")
    _write(root / rc.SCHEDULE_RUNTIME_PATH, "\n".join(lines) + "\n")


_DEFAULT_MACHINERY_SECTION = """\
- **Validated on every PR:** `scripts/ea-conformance.py` — {checks_count} checks — plus
  the checker test suite.
- **Applied every 15 minutes:** `scripts/ea-load.py` runs in-cluster on the Temporal schedule
  `factory-ea-apply-15m`.
- **Observed nightly:** `factory-ea-observation-nightly` reads the live cluster and ArgoCD."""


def _exemptions_block(items: dict[str, str]) -> str:
    """Build a README reasoned-exemption list using the checker's own header
    text (`rc._EXEMPT_HEADER`), so the fixture and the parser can never
    silently drift apart on the exact phrase."""
    lines = [rc._EXEMPT_HEADER]
    lines += [f"- `{schedule_id}` — {reason}" for schedule_id, reason in items.items()]
    return "\n".join(lines)


def _readme(
    *,
    type_count_word: str = "Two",
    type_names: tuple[str, ...] = ("capability", "application"),
    capability_count: int = 2,
    application_count: int = 1,
    service_count: int = 1,
    checks_count: int = 2,
    types_sentence: str | None = None,
    machinery_section: str | None = None,
    exemptions_block: str | None = None,
) -> str:
    if types_sentence is None:
        names = ", ".join(f"`{n}`" for n in type_names)
        types_sentence = (
            f"{type_count_word} `arch.*` types are registered "
            f"(`apps/substrate/src/schemas.py`, `ARCH_TYPE_SCHEMAS`):\n"
            f"{names}. Their liveness and gaps are inventoried honestly "
            f"in [`bead-object-inventory.md`](bead-object-inventory.md)."
        )
    if machinery_section is None:
        machinery_section = _DEFAULT_MACHINERY_SECTION.format(checks_count=checks_count)
    text = f"""# Enterprise Architecture

## The files

| File | What it is |
|---|---|
| [`model/business-layer.yaml`](model/business-layer.yaml) | {capability_count} capabilities, demand + supply layers |
| [`model/application-portfolio.yaml`](model/application-portfolio.yaml) | {application_count} applications with lifecycle |
| [`model/services.yaml`](model/services.yaml) | {service_count} application services |

## The machinery (what actually runs)

{machinery_section}

## The types

{types_sentence}

## This is the system of record
"""
    if exemptions_block is not None:
        text += f"\n## What is still not enforced\n\n{exemptions_block}\n"
    return text


#: The fixture's steady state: what the README claims and what the model
#: actually declares agree exactly. Each test below perturbs one side of
#: that equality and leaves the other alone.
_BASE = dict(
    type_count_word="Two",
    type_names=("capability", "application"),
    capability_count=2,
    application_count=1,
    service_count=1,
    checks_count=2,
)


def _fixture_repo(
    root: pathlib.Path,
    *,
    actual_type_names: tuple[str, ...] | None = None,
    actual_capability_count: int | None = None,
    actual_application_count: int | None = None,
    actual_service_count: int | None = None,
    actual_checks_count: int | None = None,
    schedules: dict[str, tuple[str, int]] | None = None,
    annotated_schedule_prefixes: frozenset[str] = frozenset(),
    **readme_overrides,
) -> None:
    readme_kwargs = {**_BASE, **readme_overrides}
    _write(root / rc.README_PATH, _readme(**readme_kwargs))
    _schemas_py(root, actual_type_names if actual_type_names is not None else readme_kwargs["type_names"])
    _ea_conformance_py(root, actual_checks_count if actual_checks_count is not None else readme_kwargs["checks_count"])
    _model_yaml(
        root,
        rc.BUSINESS_LAYER_PATH,
        "capabilities",
        actual_capability_count if actual_capability_count is not None else readme_kwargs["capability_count"],
    )
    _model_yaml(
        root,
        rc.APPLICATION_PORTFOLIO_PATH,
        "applications",
        actual_application_count if actual_application_count is not None else readme_kwargs["application_count"],
    )
    _model_yaml(
        root,
        rc.SERVICES_PATH,
        "services",
        actual_service_count if actual_service_count is not None else readme_kwargs["service_count"],
    )
    _schedule_runtime_py(
        root,
        schedules if schedules is not None else _DEFAULT_SCHEDULES,
        annotated_prefixes=annotated_schedule_prefixes,
    )


def _messages(result) -> str:
    return "\n".join(v.message for v in result.violations)


def test_passes_when_readme_matches_model(tmp_path):
    _fixture_repo(tmp_path)

    result = rc.check_readme_model_conformance(tmp_path)

    assert result.passed, _messages(result)


def test_fails_when_capability_count_drifts_from_model(tmp_path):
    """Acceptance: perturbing the model falsifies the README's count claim."""
    _fixture_repo(tmp_path, actual_capability_count=3)

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "claims 2 capabilities" in _messages(result)
    assert "declares 3" in _messages(result)


def test_fails_when_application_count_drifts_from_model(tmp_path):
    _fixture_repo(tmp_path, actual_application_count=5)

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "application-portfolio.yaml" in _messages(result)


def test_fails_when_services_count_drifts_from_model(tmp_path):
    _fixture_repo(tmp_path, actual_service_count=4)

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "services.yaml" in _messages(result)


def test_fails_when_a_type_is_added_to_the_model_but_not_the_readme(tmp_path):
    """The exact rot vector named in the task: a model change (a new arch.*
    type registered in schemas.py) falsifies the README's prose."""
    _fixture_repo(tmp_path, actual_type_names=("capability", "application", "incident"))

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    messages = _messages(result)
    assert "ARCH_TYPE_SCHEMAS carries 3" in messages
    assert "incident" in messages


def test_fails_when_readme_names_a_type_the_model_does_not_register(tmp_path):
    _fixture_repo(
        tmp_path,
        actual_type_names=("capability", "application"),
        type_count_word="Three",
        type_names=("capability", "application", "ghost"),
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "named in README but not in ARCH_TYPE_SCHEMAS: ghost" in _messages(result)


def test_fails_when_ea_conformance_check_count_drifts(tmp_path):
    _fixture_repo(tmp_path, actual_checks_count=5)

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "claims 2 checks" in _messages(result)
    assert "defines 5" in _messages(result)


def test_fails_when_a_files_table_entry_does_not_exist(tmp_path):
    _fixture_repo(tmp_path)
    (tmp_path / rc.SERVICES_PATH).unlink()

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "model/services.yaml" in _messages(result)
    assert "does not exist" in _messages(result)


def test_unparseable_types_sentence_is_a_failure_not_a_silent_skip(tmp_path):
    """PRIN-015: an assertion this checker cannot evaluate must fail, never
    pass silently because the prose no longer matches the pattern."""
    _fixture_repo(tmp_path, types_sentence="The type registry lives elsewhere now.")

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "could not find" in _messages(result)


def test_missing_readme_is_a_tolerated_skip_not_a_crash(tmp_path):
    """House precedent (Rule 7 in repo_invariants.py): a rule whose subject
    is entirely absent skips rather than accuses -- this only applies to a
    fixture/partial checkout, never to the live repository (see
    test_check_repo_invariants_cli.py's live-repo pin)."""
    result = rc.check_readme_model_conformance(tmp_path)

    assert result.passed
    assert "skipped" in result.summary


def test_fails_when_readme_names_a_renamed_or_retired_schedule(tmp_path):
    """Acceptance: renaming the schedule id in schedule_runtime.py falsifies
    the README's mention of the old id."""
    _fixture_repo(
        tmp_path,
        schedules={
            "EA_APPLY": ("factory-ea-apply-fast", 15 * 60),
            "EA_OBSERVATION": _DEFAULT_SCHEDULES["EA_OBSERVATION"],
        },
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    messages = _messages(result)
    assert "factory-ea-apply-15m" in messages
    assert "no such schedule" in messages


def test_fails_when_a_schedules_cadence_drifts_from_declared_interval(tmp_path):
    """Acceptance: changing the declared interval falsifies the README's
    cadence claim even though the schedule id itself still matches."""
    _fixture_repo(
        tmp_path,
        schedules={
            "EA_APPLY": ("factory-ea-apply-15m", 5 * 60),
            "EA_OBSERVATION": _DEFAULT_SCHEDULES["EA_OBSERVATION"],
        },
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    messages = _messages(result)
    assert "factory-ea-apply-15m" in messages
    assert "900s" in messages
    assert "300s" in messages


def test_fails_when_a_schedule_is_declared_but_absent_from_readme_and_unexempted(tmp_path):
    """AC #2: a schedule declared in schedule_runtime.py but named nowhere
    in the README (and not exempted) is a reported gap, not a silent one."""
    _fixture_repo(
        tmp_path,
        schedules={**_DEFAULT_SCHEDULES, "FAKE_EXTRA": ("factory-fake-extra-99", 60)},
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    messages = _messages(result)
    assert "factory-fake-extra-99" in messages
    assert "reasoned exemption" in messages


def test_declared_schedule_can_be_exempted_with_a_reason(tmp_path):
    """The exemption mechanism the AC asks for: a documented, reasoned gap
    passes instead of failing."""
    _fixture_repo(
        tmp_path,
        schedules={**_DEFAULT_SCHEDULES, "FAKE_EXTRA": ("factory-fake-extra-99", 60)},
        exemptions_block=_exemptions_block({"factory-fake-extra-99": "test-only exemption"}),
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert result.passed, _messages(result)


def test_exemption_with_no_reason_is_a_failure(tmp_path):
    _fixture_repo(
        tmp_path,
        schedules={**_DEFAULT_SCHEDULES, "FAKE_EXTRA": ("factory-fake-extra-99", 60)},
        exemptions_block=_exemptions_block({"factory-fake-extra-99": "   "}),
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "no reason" in _messages(result)


def test_stale_exemption_entry_is_a_failure(tmp_path):
    """The 'dead grandfather entry' failure mode (#713): an exemption for a
    schedule that no longer exists silently weakens the ratchet unless it is
    itself flagged."""
    _fixture_repo(
        tmp_path,
        exemptions_block=_exemptions_block({"factory-does-not-exist": "orphaned reason"}),
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    messages = _messages(result)
    assert "factory-does-not-exist" in messages
    assert "stale exemption" in messages


def test_missing_machinery_heading_is_a_failure_not_a_silent_skip(tmp_path):
    """PRIN-015: if the section this assertion parses disappears, that is a
    failure, not quietly nothing to check."""
    _fixture_repo(tmp_path)
    _write(
        tmp_path / rc.README_PATH,
        "# Enterprise Architecture\n\nNo machinery section here at all.\n",
    )

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "could not find the '## The machinery" in _messages(result)


def test_fails_when_schedule_runtime_missing(tmp_path):
    _fixture_repo(tmp_path)
    (tmp_path / rc.SCHEDULE_RUNTIME_PATH).unlink()

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "schedule_runtime.py" in _messages(result)
    assert "cannot verify" in _messages(result)


def test_declares_no_schedule_constants_is_a_failure_not_a_silent_skip(tmp_path):
    """PRIN-015 / preservation criterion: when schedule_runtime.py exists but
    declares zero recognizable '<PREFIX>_SCHEDULE_ID' constants in any shape,
    `_schedule_declarations` returning None and the caller's explicit
    'cannot verify' violation is untouched by this task's fix -- only the set
    of assignment *shapes* the walker recognizes changed, not what happens
    when nothing of any shape is found."""
    _fixture_repo(tmp_path)
    _write(tmp_path / rc.SCHEDULE_RUNTIME_PATH, "OTHER_CONSTANT = 1\n")

    assert rc._schedule_declarations(tmp_path) is None

    result = rc.check_readme_model_conformance(tmp_path)

    assert not result.passed
    assert "schedule_runtime.py" in _messages(result)
    assert "cannot verify" in _messages(result)


def test_new_annotated_schedule_declaration_is_caught_same_as_plain(tmp_path):
    """The measured fail-open (dev.finding dd6fb265, F1): a NEW schedule
    declared with a type annotation must fall into the 'declared but named
    nowhere in the README' check exactly like the plain form. Against
    unfixed code the plain form (known-bad control) yields 1 violation and
    the annotated form yields 0 -- that asymmetry is the defect this test
    pins closed. A known-good plain control is required alongside the
    annotated case: a test that only checks the annotated form can't tell a
    fixed walker from a check that has stopped running."""
    ghost = {"GHOST": ("factory-ghost-99", 60)}

    plain_root = tmp_path / "plain"
    _fixture_repo(plain_root, schedules={**_DEFAULT_SCHEDULES, **ghost})
    plain_result = rc.check_readme_model_conformance(plain_root)

    annotated_root = tmp_path / "annotated"
    _fixture_repo(
        annotated_root,
        schedules={**_DEFAULT_SCHEDULES, **ghost},
        annotated_schedule_prefixes=frozenset({"GHOST"}),
    )
    annotated_result = rc.check_readme_model_conformance(annotated_root)

    assert not plain_result.passed
    assert "factory-ghost-99" in _messages(plain_result)
    assert "reasoned exemption" in _messages(plain_result)

    assert not annotated_result.passed, (
        "a NEW annotated schedule declaration vanished from the walker "
        "instead of being caught -- the exact fail-open this bead fixes"
    )
    assert "factory-ghost-99" in _messages(annotated_result)
    assert "reasoned exemption" in _messages(annotated_result)


def test_annotating_an_existing_declaration_still_resolves_it(tmp_path):
    """AC #1's actual normative text ('SHALL resolve ... exactly as it
    resolves the unannotated forms') requires an annotated EXISTING
    declaration to keep resolving normally after this fix, not to start
    vanishing from ScheduleDeclarations the way a new one used to. Measured
    directly: on unfixed code, annotating both lines of one existing,
    exempted schedule (STALENESS) drops it from `all_ids` entirely, which
    trips the reasoned-exemption cross-check as a stale exemption (8
    resolved, 2 unresolved, 1 violation, measured on the live repository
    file). After this fix it must simply resolve like any other
    declaration."""
    _fixture_repo(
        tmp_path,
        annotated_schedule_prefixes=frozenset({"EA_OBSERVATION"}),
    )

    declarations = rc._schedule_declarations(tmp_path)
    assert declarations is not None
    assert declarations.unresolved_ids == ()
    assert declarations.intervals_by_id == {
        schedule_id: interval for schedule_id, interval in _DEFAULT_SCHEDULES.values()
    }

    result = rc.check_readme_model_conformance(tmp_path)
    assert result.passed, _messages(result)


def test_annotating_the_shared_interval_constant_preserves_the_alias_chain():
    """Pins the sharper cascade the round-4 gate measured (dev.finding
    dd6fb265): `EA_APPLY_SCHEDULE_INTERVAL_SECONDS` in the live
    schedule_runtime.py is aliased by six other declarations via a bare
    `Name` reference resolved against the namespace built earlier in the
    same walk. A fix that adds AnnAssign handling as a second pass instead
    of widening the one shared target-name guard (`_assign_target_name`)
    can satisfy every other test here and still break those six schedules:
    measured on unfixed code, annotating only that one shared constant drops
    it from `namespace`, so all seven aliasing/aliased ids -- not just one --
    lose their resolved interval (4 resolved, 7 unresolved, 1 violation)."""
    live_text = (REPO / rc.SCHEDULE_RUNTIME_PATH).read_text(encoding="utf-8")
    original_line = "EA_APPLY_SCHEDULE_INTERVAL_SECONDS = 15 * 60"
    annotated_line = "EA_APPLY_SCHEDULE_INTERVAL_SECONDS: int = 15 * 60"
    assert original_line in live_text, (
        "fixture assumption about the live schedule_runtime.py's shape is "
        "stale -- update this test's mutation to match"
    )

    baseline = rc._schedule_declarations(REPO)
    assert baseline is not None
    assert baseline.unresolved_ids == ()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        mutated_path = tmp_path / rc.SCHEDULE_RUNTIME_PATH
        mutated_path.parent.mkdir(parents=True, exist_ok=True)
        mutated_path.write_text(live_text.replace(original_line, annotated_line, 1))

        mutated = rc._schedule_declarations(tmp_path)

    assert mutated is not None
    assert mutated.unresolved_ids == (), (
        f"{len(mutated.unresolved_ids)} schedule(s) lost their resolved "
        "interval when only the shared EA_APPLY_SCHEDULE_INTERVAL_SECONDS "
        f"constant was annotated: {sorted(mutated.unresolved_ids)}"
    )
    assert mutated.intervals_by_id == baseline.intervals_by_id
    assert mutated.all_ids == baseline.all_ids


def test_annotating_every_live_declaration_still_resolves_all_eleven():
    """AC #1's 'exactly as it resolves the unannotated forms' is a claim
    about every declaration, not just the one shared constant the alias-chain
    test above mutates. Switch every '<PREFIX>_SCHEDULE_ID' and
    '<PREFIX>_SCHEDULE_INTERVAL_SECONDS' assignment in the live
    schedule_runtime.py to its annotated spelling and confirm all 11 schedule
    ids still resolve with zero unresolved -- the strongest form of the
    per-declaration guarantee, exercised against the real file rather than a
    fixture."""
    live_text = (REPO / rc.SCHEDULE_RUNTIME_PATH).read_text(encoding="utf-8")

    id_pattern = re.compile(r"^([A-Z_]+_SCHEDULE_ID)\s*=\s*", re.MULTILINE)
    interval_pattern = re.compile(r"^([A-Z_]+_SCHEDULE_INTERVAL_SECONDS)\s*=\s*", re.MULTILINE)
    annotated_text = id_pattern.sub(r"\1: str = ", live_text)
    annotated_text = interval_pattern.sub(r"\1: int = ", annotated_text)
    assert annotated_text != live_text, (
        "the annotation substitution matched nothing -- fixture assumption "
        "about the live schedule_runtime.py's shape is stale"
    )

    baseline = rc._schedule_declarations(REPO)
    assert baseline is not None
    assert baseline.unresolved_ids == ()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        mutated_path = tmp_path / rc.SCHEDULE_RUNTIME_PATH
        mutated_path.parent.mkdir(parents=True, exist_ok=True)
        mutated_path.write_text(annotated_text)

        mutated = rc._schedule_declarations(tmp_path)

    assert mutated is not None
    assert mutated.unresolved_ids == (), (
        f"{len(mutated.unresolved_ids)} schedule(s) lost their resolved "
        f"interval once every declaration was annotated: {sorted(mutated.unresolved_ids)}"
    )
    assert mutated.intervals_by_id == baseline.intervals_by_id
    assert mutated.all_ids == baseline.all_ids
