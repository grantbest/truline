from __future__ import annotations

import pathlib
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import private_identifiers as pi  # noqa: E402


_PATTERNS_YAML = """
patterns:
  - name: test-household-id
    category: household_identifier
    pattern: 'canyonvale'
  - name: test-financial-field
    category: financial_field
    pattern: 'account_number["'']?\\s*[:=]\\s*["'']?[0-9]{3,}'
  - name: test-credential
    category: credential
    pattern: 'AKIA[0-9A-Z]{16}'
"""

_SCOPE_YAML = """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical decision records."
"""


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _seed(root: pathlib.Path) -> None:
    _write(root / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(root / pi.SCOPE_FILE, _SCOPE_YAML)


def test_household_identifier_in_publishable_path_fails(tmp_path):
    _seed(tmp_path)
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "canyonvale"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any("canyonvale" in v.message for v in result.violations)
    assert any("household_identifier" in v.message for v in result.violations)


def test_household_identifier_in_historical_record_does_not_fail(tmp_path):
    _seed(tmp_path)
    _write(
        tmp_path / "docs" / "decisions" / "2026-01-01-example.md",
        "We migrated off canyonvale in 2026.\n",
    )

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_financial_field_in_publishable_path_fails(tmp_path):
    _seed(tmp_path)
    _write(
        tmp_path / "apps" / "widget" / "seed.py",
        'account_number = "123456789"\n',
    )

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any("financial_field" in v.message for v in result.violations)


def test_credential_shaped_value_in_publishable_path_fails(tmp_path):
    _seed(tmp_path)
    # Concatenated so this source file carries no contiguous credential-shaped
    # token for the secret-scan gate; the written fixture content is unchanged.
    _write(tmp_path / "apps" / "widget" / "leak.py", 'KEY = "AKIA' + 'ABCDEFGHIJKLMNOP"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any("credential" in v.message for v in result.violations)


def test_clean_publishable_tree_passes(tmp_path):
    _seed(tmp_path)
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "generic"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations
    assert "1 publishable path(s) checked" in result.summary


def test_grandfathered_path_is_excluded_rather_than_flagged(tmp_path):
    _seed(tmp_path)
    _write(tmp_path / "legacy.py", 'CLUSTER = "canyonvale"\n')
    _write(tmp_path / pi.GRANDFATHER_FILE, "legacy.py\n")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations
    assert "1 grandfathered" in result.summary


def test_missing_declared_patterns_is_tolerated_not_a_violation(tmp_path):
    """Mirrors house precedent (check_test_map_covers_apps,
    check_job_classes_complete): a synthetic tree built for an unrelated
    rule's test carries no scripts/ contract files, and that absence must
    not be read as a violation of this rule."""
    _write(tmp_path / pi.SCOPE_FILE, _SCOPE_YAML)
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "canyonvale"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed
    assert "skipped" in result.summary


def test_missing_declared_scope_is_tolerated_not_a_violation(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "canyonvale"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed
    assert "skipped" in result.summary


def test_malformed_pattern_entry_fails_closed_rather_than_passes(tmp_path):
    """WHERE the check cannot determine whether something is publishable, it
    must fail rather than pass. An unreadable/malformed policy is that case:
    a present-but-broken patterns file must not be treated the same as an
    absent one."""
    _write(tmp_path / pi.PATTERNS_FILE, "patterns:\n  - name: broken\n")
    _write(tmp_path / pi.SCOPE_FILE, _SCOPE_YAML)

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed


def test_malformed_scope_entry_fails_closed_rather_than_passes(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(tmp_path / pi.SCOPE_FILE, "excluded_paths:\n  - path: 'x/**'\n")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed


def test_declared_files_do_not_trip_their_own_patterns(tmp_path):
    """scripts/private-identifier-patterns.yaml necessarily carries this
    household's real identifiers as pattern text; it must not fail against
    itself."""
    _seed(tmp_path)

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_live_repo_carries_no_private_identifier_in_a_publishable_path():
    """Proves the check live rather than merely quiet, in the direction
    that matters for a required PR gate: the actual repository, as it
    stands, must pass."""
    assert (REPO / pi.PATTERNS_FILE).exists()
    assert (REPO / pi.SCOPE_FILE).exists()

    result = pi.check_publishable_paths_carry_no_private_identifiers(REPO)

    assert result.passed, result.violations
    assert "publishable path(s) checked" in result.summary


def test_an_undeclared_unreadable_file_is_a_violation_not_a_skip(tmp_path):
    # Release-gate finding on #643: a security scanner that silently passes a
    # file it could not read reports coverage it does not have. Fail closed.
    _seed(tmp_path)
    blob = tmp_path / "apps" / "widget" / "blob.data"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\xff\xfe\x00garbage")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any(
        "blob.data" in v.message and "fails" in v.message for v in result.violations
    ), result.violations


def test_a_declared_binary_suffix_still_skips(tmp_path):
    _seed(tmp_path)
    logo = tmp_path / "apps" / "widget" / "logo.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


# -- publication_targets outrank excluded_paths / grandfather -------------

_TARGET_YAML = """
excluded_paths:
  - path: "vendored/**"
    reason: "vendored third-party code, not ours."

publication_targets:
  - name: "widget"
    outcome: "R99.01/O-1"
    paths:
      - "vendored/widget.py"
"""

_TARGET_WITH_DEBT_YAML = """
excluded_paths:
  - path: "vendored/**"
    reason: "vendored third-party code, not ours."

publication_targets:
  - name: "widget"
    outcome: "R99.01/O-1"
    paths:
      - "vendored/widget.py"

accepted_shadow_debt:
  - path: "vendored/widget.py"
    reason: "pending extraction, still carries the household id."
    clears: ["R9901-1"]
"""


def _seed_targets(root: pathlib.Path, scope_yaml: str) -> None:
    _write(root / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(root / pi.SCOPE_FILE, scope_yaml)


def test_publication_target_scans_a_path_excluded_paths_would_have_skipped(tmp_path):
    _seed_targets(tmp_path, _TARGET_YAML)
    _write(tmp_path / "vendored" / "widget.py", 'CLUSTER = "canyonvale"\n')
    _write(tmp_path / "vendored" / "other.py", "# nothing here\n")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    messages = " ".join(v.message for v in result.violations)
    assert "widget (R99.01/O-1)" in messages
    assert "excluded_paths entry 'vendored/**'" in messages
    assert "undeclared" in messages


def test_publication_target_scans_a_path_grandfather_would_have_skipped(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical decision records."

publication_targets:
  - name: "widget"
    outcome: "R99.01/O-1"
    paths:
      - "legacy.py"
""",
    )
    _write(tmp_path / "legacy.py", 'CLUSTER = "canyonvale"\n')
    _write(tmp_path / pi.GRANDFATHER_FILE, "legacy.py\n")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    messages = " ".join(v.message for v in result.violations)
    assert "widget (R99.01/O-1)" in messages
    assert "grandfathered entry" in messages
    assert "undeclared" in messages


def test_declared_shadow_debt_permits_the_existing_hit(tmp_path):
    _seed_targets(tmp_path, _TARGET_WITH_DEBT_YAML)
    _write(tmp_path / "vendored" / "widget.py", 'CLUSTER = "canyonvale"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations
    assert "widget (R99.01/O-1): 1 scanned" in result.summary


def test_stale_accepted_shadow_debt_entry_is_a_violation(tmp_path):
    _seed_targets(tmp_path, _TARGET_WITH_DEBT_YAML)
    _write(tmp_path / "vendored" / "widget.py", 'CLUSTER = "generic"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any("stale entry" in v.message for v in result.violations), result.violations


def test_publication_target_matching_zero_paths_is_a_violation(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical decision records."

publication_targets:
  - name: "ghost"
    outcome: "R99.01/O-1"
    paths:
      - "nothing/here/**"
""",
    )
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "generic"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any(
        "ghost" in v.message and "matched zero paths" in v.message for v in result.violations
    ), result.violations


def test_malformed_publication_targets_fails_closed(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical."
publication_targets:
  - name: "widget"
    outcome: "O-1"
    paths:
      - "vendored/widget.py"
""",
    )

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed


def test_malformed_accepted_shadow_debt_fails_closed(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical."
accepted_shadow_debt:
  - path: "vendored/widget.py"
    reason: "missing clears field"
""",
    )

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed


def test_publication_targets_absent_is_tolerated_not_a_violation(tmp_path):
    """Existing scope files written before publication_targets existed must
    keep working: absence is "no targets", not an error."""
    _seed(tmp_path)
    _write(tmp_path / "apps" / "widget" / "config.py", 'CLUSTER = "generic"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations
    assert "publication targets" not in result.summary


# -- the four live shadow instances named in this bead's intent, each on its
# -- own so a regression in one does not hide behind the others -----------

_LIVE_INSTANCE_SCOPE_YAML = """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical decision records."

publication_targets:
  - name: "csdm-model-layer"
    outcome: "R99.01/O-4"
    paths:
      - "scripts/ea-conformance.py"
      - "apps/substrate/tests/test_arch_schema.py"
      - "apps/substrate/tests/test_arch_ci.py"
  - name: "store"
    outcome: "R99.01/O-5"
    paths:
      - "apps/substrate/src/main.py"
      - "apps/substrate/tests/test_arch_schema.py"
      - "apps/substrate/tests/test_arch_ci.py"

accepted_shadow_debt:
  - path: "scripts/ea-conformance.py"
    reason: "the EA conformance check R99.01/O-4 publishes by name."
    clears: ["R9901-6"]
  - path: "apps/substrate/src/main.py"
    reason: "the substrate entrypoint R99.01/O-5 publishes."
    clears: ["R9901-5"]
  - path: "apps/substrate/tests/test_arch_schema.py"
    reason: "arch-schema fixtures for both the model layer and the store."
    clears: ["R9901-5", "R9901-6"]
  - path: "apps/substrate/tests/test_arch_ci.py"
    reason: "arch-CI fixtures for both the model layer and the store."
    clears: ["R9901-5", "R9901-6"]
"""


_LIVE_INSTANCE_PATHS = [
    "scripts/ea-conformance.py",
    "apps/substrate/src/main.py",
    "apps/substrate/tests/test_arch_schema.py",
    "apps/substrate/tests/test_arch_ci.py",
]


def _seed_live_instances(root: pathlib.Path, carries_identifier: str) -> None:
    """Seed the scope/grandfather declarations plus all four live-instance
    paths, matching the real repository's current shape: every one of them
    genuinely carries the household id today. `carries_identifier` names the
    one path this particular test is exercising, so a failure there can't
    hide behind the other three's passing content."""
    _write(root / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(root / pi.SCOPE_FILE, _LIVE_INSTANCE_SCOPE_YAML)
    _write(root / pi.GRANDFATHER_FILE, "\n".join(_LIVE_INSTANCE_PATHS) + "\n")
    assert carries_identifier in _LIVE_INSTANCE_PATHS
    for rel in _LIVE_INSTANCE_PATHS:
        _write(root / rel, 'HOST = "canyonvale"\n')


def test_live_instance_ea_conformance_is_declared_and_passes(tmp_path):
    _seed_live_instances(tmp_path, carries_identifier="scripts/ea-conformance.py")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_live_instance_substrate_main_is_declared_and_passes(tmp_path):
    _seed_live_instances(tmp_path, carries_identifier="apps/substrate/src/main.py")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_live_instance_test_arch_schema_is_declared_and_passes(tmp_path):
    _seed_live_instances(tmp_path, carries_identifier="apps/substrate/tests/test_arch_schema.py")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_live_instance_test_arch_ci_is_declared_and_passes(tmp_path):
    _seed_live_instances(tmp_path, carries_identifier="apps/substrate/tests/test_arch_ci.py")

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert result.passed, result.violations


def test_live_instance_undeclared_debt_fails_closed(tmp_path):
    """Same shadow shape as the four live instances, minus the
    accepted_shadow_debt entry: proves the mechanism actually fails closed
    rather than the fixture happening to pass for an unrelated reason."""
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "docs/decisions/**"
    reason: "historical decision records."

publication_targets:
  - name: "csdm-model-layer"
    outcome: "R99.01/O-4"
    paths:
      - "scripts/ea-conformance.py"
""",
    )
    _write(tmp_path / pi.GRANDFATHER_FILE, "scripts/ea-conformance.py\n")
    _write(tmp_path / "scripts" / "ea-conformance.py", '# host: "canyonvale"\n')

    result = pi.check_publishable_paths_carry_no_private_identifiers(tmp_path)

    assert not result.passed
    assert any("undeclared" in v.message for v in result.violations), result.violations


# -- publishable-scope.yaml reason strings cite real, release-prefixed outcomes

def _write_charter(root: pathlib.Path, release: str, outcome_ids: list[str]) -> None:
    import json

    outcomes = [{"id": oid} for oid in outcome_ids]
    _write(
        root / pi.RELEASES_DIR / f"{release}.json",
        json.dumps({"ref": release, "outcomes": outcomes}),
    )


def test_bare_outcome_reference_in_reason_is_a_violation(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "apps/harness/**"
    reason: "the harness (O-6)."
""",
    )
    _write_charter(tmp_path, "R99.01", ["O-6"])

    result = pi.check_publishable_scope_outcomes_are_release_prefixed_and_resolve(tmp_path)

    assert not result.passed
    assert any("no release prefix" in v.message for v in result.violations), result.violations


def test_release_prefixed_outcome_that_does_not_resolve_is_a_violation(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "apps/harness/**"
    reason: "the harness (R99.01/O-5)."
""",
    )
    _write_charter(tmp_path, "R99.01", ["O-6"])

    result = pi.check_publishable_scope_outcomes_are_release_prefixed_and_resolve(tmp_path)

    assert not result.passed
    assert any("does not declare" in v.message for v in result.violations), result.violations


def test_release_prefixed_outcome_that_resolves_passes(tmp_path):
    _write(tmp_path / pi.PATTERNS_FILE, _PATTERNS_YAML)
    _write(
        tmp_path / pi.SCOPE_FILE,
        """
excluded_paths:
  - path: "apps/harness/**"
    reason: "the harness (R99.01/O-6)."
""",
    )
    _write_charter(tmp_path, "R99.01", ["O-6"])

    result = pi.check_publishable_scope_outcomes_are_release_prefixed_and_resolve(tmp_path)

    assert result.passed, result.violations


def test_outcome_reference_check_live_repo_passes():
    result = pi.check_publishable_scope_outcomes_are_release_prefixed_and_resolve(REPO)

    assert result.passed, result.violations


# --- live-repo pins -------------------------------------------------------
#
# Gate finding on #749: every other test in this module builds a synthetic
# scope file under an R99.01 release, so the WHOLE publication_targets and
# accepted_shadow_debt block could be deleted from the real
# scripts/publishable-scope.yaml and the suite stayed green. That is the
# fixture-masks-live drift this repository has already paid for twice in its
# gate scripts. These two pin the live declaration itself.

#: The paths measured on 2026-09-10 as carrying a household identifier AND
#: sitting inside what R26.05 publishes (three then; two since 2026-09-13, when
#: #816 renamed the substrate's API title and main.py scanned clean). Each must stay claimed by a
#: publication target and covered by a dated debt entry until the bead named
#: in `clears` cleans it -- at which point the orphan check requires the entry
#: be removed, and this list shrinks with it.
LIVE_SHADOWED_PUBLISHABLE_PATHS = (
    "apps/substrate/tests/test_arch_schema.py",
    "apps/substrate/tests/test_arch_ci.py",
)


def test_live_scope_file_claims_and_declares_every_known_shadowed_path():
    """The real scope file, not a fixture: each known shadowed path is claimed
    by a publication target and carries an accepted_shadow_debt entry naming a
    bead. Deleting either block from publishable-scope.yaml must fail here."""
    targets = pi.load_publication_targets(REPO)
    debt = pi.load_accepted_shadow_debt(REPO)

    assert targets, "publishable-scope.yaml declares no publication_targets"
    assert debt, "publishable-scope.yaml declares no accepted_shadow_debt"

    ci_changes = pi._load_ci_changes()
    for path in LIVE_SHADOWED_PUBLISHABLE_PATHS:
        claimed_by = [
            t.name
            for t in targets
            if any(ci_changes.glob_to_regex(g).match(path) for g in t.globs)
        ]
        assert claimed_by, f"{path} is claimed by no publication target"
        assert path in debt, f"{path} is shadowed but has no accepted_shadow_debt entry"
        assert debt[path].clears, f"{path}'s debt entry names no bead that clears it"


def test_live_repo_publishable_check_passes():
    """The real check against the real tree. Guards the whole arrangement:
    an undeclared shadow, a stale entry, an orphaned debt entry or a
    zero-match target each fail this."""
    result = pi.check_publishable_paths_carry_no_private_identifiers(REPO)

    assert result.passed, result.violations
