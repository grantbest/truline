"""A provenance created_by must never fall back to a fabricated actor,
enforced (release-gate finding on #938/#935: the same fallback shape was
copied to a second call site in a day and nothing in the repository noticed).

No cluster, no network, no substrate -- these tests only ever write to a
synthetic tmp_path tree and call the checker function directly.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_repo_invariants_cli",
        REPO / "scripts" / "check-repo-invariants.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_fails_on_the_none_guarded_conditional_fallback(tmp_path):
    """AC-3, direction one: the exact live shape must fail, naming file:line."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = identity.client if identity is not None else "unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert len(result.violations) == 1
    message = result.violations[0].message
    assert "example.py:1" in message
    assert "'unknown'" in message


def test_passes_on_the_corrected_explicit_raise_form(tmp_path):
    """AC-3, direction two: #938's fix -- fail loudly instead of fabricating --
    must not be flagged."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "if identity is None:\n"
        '    raise ValueError("no identity")\n'
        "created_by = identity.client\n",
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_a_plain_literal_created_by_is_not_flagged(tmp_path):
    """AC-2, the headline control: an automated writer naming itself is
    correct and must never be flagged, however many call sites do it."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "workflows" / "notify.py",
        'created_by="notify/alert_state"\n'
        'created_by = "bank-sync/ensure_item_registry"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_an_optional_plain_value_defaulting_is_not_flagged(tmp_path):
    """A bare optional value (e.g. an HTTP header) defaulting to a placeholder
    is a different, legitimate shape -- not an identity lookup failing.
    Measured on main: apps/substrate/src/routes.py's create_bead_link takes
    x_created_by from an optional header and falls back the same way; this
    rule must not flag it, or AC-2's "zero on an unmodified tree" is
    unreachable without grandfathering a second, non-identity call site."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "links.py",
        "def make(x_created_by):\n"
        "    return BeadLink(created_by=x_created_by or \"unknown\")\n",
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_fails_on_the_or_equivalent_through_a_lookup(tmp_path):
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = identity.client or "unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:1" in result.violations[0].message


def test_fails_on_the_getattr_equivalent(tmp_path):
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = getattr(identity, "client", "unknown")\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:1" in result.violations[0].message


def test_fails_on_a_fallback_passed_as_a_call_keyword(tmp_path):
    """The defect is not tied to a bare `created_by = ...` assignment -- an
    inline keyword argument carrying the same fallback shape is the same
    defect."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "bead = file_dev_task(\n"
        '    spec, created_by=identity.client if identity is not None else "unknown"\n'
        ")\n",
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:2" in result.violations[0].message


def test_grandfathered_entry_is_not_reported_as_a_violation(tmp_path, monkeypatch):
    """The one pre-existing, out-of-scope-to-fix instance stays green."""
    checker = _load_checker()
    # A fixture entry, not production's. CREATED_BY_FALLBACK_GRANDFATHERED is
    # empty at HEAD and that is the intended end state -- #938 removed the last
    # live fallback. This test covers the exemption MECHANISM, which must keep
    # working whether or not production currently has an offender to exempt.
    entry = "apps/example-service/src/routes.py:12"
    monkeypatch.setattr(checker, "CREATED_BY_FALLBACK_GRANDFATHERED", (entry,))
    rel_path, lineno = entry.rsplit(":", 1)
    lineno = int(lineno)
    body = "pass\n" * (lineno - 1) + 'created_by = identity.client if identity is not None else "unknown"\n'
    _write(tmp_path / rel_path, body)

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations
    assert "1 grandfathered" in result.summary


def test_a_stale_grandfather_entry_is_itself_flagged(tmp_path, monkeypatch):
    """If the grandfathered line is ever fixed (or drifts) without the entry
    being removed, that is dead weight nothing else would ever report --
    fail on it by name, mirroring change-kind-grandfathered.txt's own
    staleness check."""
    checker = _load_checker()
    entry = "apps/example-service/src/routes.py:12"
    monkeypatch.setattr(checker, "CREATED_BY_FALLBACK_GRANDFATHERED", (entry,))
    rel_path, _ = entry.rsplit(":", 1)
    _write(tmp_path / rel_path, "created_by = identity.client\n")

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert any(entry in v.message for v in result.violations)


def test_a_second_fallback_in_the_grandfathered_file_still_fires(tmp_path, monkeypatch):
    """AC-5's reason for exempting by exact line rather than by path: the
    recurrence this rule exists to catch is a second, undeclared fallback
    landing in the SAME file next to an already-grandfathered one (measured
    composing #938 with #935). A path-wide exemption would hide exactly
    that."""
    checker = _load_checker()
    entry = "apps/example-service/src/routes.py:12"
    monkeypatch.setattr(checker, "CREATED_BY_FALLBACK_GRANDFATHERED", (entry,))
    rel_path, lineno = entry.rsplit(":", 1)
    lineno = int(lineno)
    grandfathered_line = 'created_by = identity.client if identity is not None else "unknown"\n'
    body = "pass\n" * (lineno - 1) + grandfathered_line + grandfathered_line
    _write(tmp_path / rel_path, body)

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert len(result.violations) == 1
    assert f"{rel_path}:{lineno + 1}" in result.violations[0].message


def test_ac1_fails_on_a_fallback_inside_a_dict_literal(tmp_path):
    """AC-1, direction one: the fallback idiom copy-pasted into a dict
    literal -- the most common created_by-writing shape in the repo -- must
    be flagged the same as the keyword form, naming the VALUE's line."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "prov = {\n"
        '    "created_by": identity.client if identity is not None else "unknown",\n'
        "}\n",
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:2" in result.violations[0].message
    assert "'unknown'" in result.violations[0].message


def test_ac1_passes_on_a_dict_literal_with_a_genuinely_computed_value(tmp_path):
    """AC-1, direction two: a dict literal carrying a real lookup with no
    fabricated fallback must not be flagged."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "if identity is None:\n"
        '    raise ValueError("no identity")\n'
        'prov = {"created_by": identity.client}\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_ac2_fails_on_a_truthiness_check_in_place_of_is_not_none(tmp_path):
    """AC-2, direction one: `if identity` is one token from the already-caught
    `if identity is not None` and must be treated identically."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = identity.client if identity else "unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:1" in result.violations[0].message
    assert "'unknown'" in result.violations[0].message


def test_ac2_passes_on_a_truthiness_check_with_a_genuinely_computed_fallback(tmp_path):
    """AC-2, direction two: the fallback branch is itself a lookup, not a
    fabricated literal, so this must not be flagged."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "created_by = identity.client if identity else identity.legacy_client\n",
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_ac3_fails_on_a_fallback_written_through_a_subscript_target(tmp_path):
    """AC-3, direction one: `prov["created_by"] = ...` is the third writing
    form seen in this repo and must be flagged like the others."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'prov["created_by"] = identity.client if identity is not None else "unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:1" in result.violations[0].message
    assert "'unknown'" in result.violations[0].message


def test_ac3_passes_on_a_subscript_target_with_an_explicit_raise(tmp_path):
    """AC-3, direction two: the corrected form -- fail loudly, then write the
    real value through the subscript -- must not be flagged."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        "if identity is None:\n"
        '    raise ValueError("no identity")\n'
        'prov["created_by"] = identity.client\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_ac4_fails_on_an_f_string_fallback_with_no_placeholders(tmp_path):
    """AC-4, direction one: `f"unknown"` is a literal by any reading and must
    be flagged exactly like the plain-string form."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = identity.client if identity is not None else f"unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert not result.passed
    assert "example.py:1" in result.violations[0].message
    assert "'unknown'" in result.violations[0].message


def test_ac4_passes_on_an_f_string_fallback_that_interpolates_something(tmp_path):
    """AC-4, direction two: an f-string that interpolates a value is a
    computed value, not a fabricated one, and must not be flagged."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "routers" / "example.py",
        'created_by = identity.client if identity is not None else f"unknown-{suffix}"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_ac7_literal_service_identities_stay_unflagged_through_dict_and_subscript(tmp_path):
    """AC-7: widening the rule to dict literals and subscript targets must
    not catch a plain literal written through either new form -- a service
    naming itself is correct regardless of which of the three writing forms
    it uses."""
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "src" / "workflows" / "notify.py",
        'prov = {"created_by": "notify/alert_state"}\n'
        'prov["created_by"] = "bank-sync/ensure_item_registry"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations


def test_test_directories_and_migrations_are_not_scanned(tmp_path):
    checker = _load_checker()
    _write(
        tmp_path / "apps" / "example-service" / "tests" / "test_example.py",
        'created_by = identity.client if identity is not None else "unknown"\n',
    )
    _write(
        tmp_path / "apps" / "example-service" / "migrations" / "versions" / "0001_x.py",
        'created_by = identity.client if identity is not None else "unknown"\n',
    )

    result = checker.check_created_by_never_fabricates_identity(tmp_path)

    assert result.passed, result.violations
    assert "0 production file" in result.summary
