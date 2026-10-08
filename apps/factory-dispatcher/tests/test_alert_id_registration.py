"""Tree-wide ratchet: every *_ALERT_ID constant under apps/factory-dispatcher/
(not just failure_diagnosis.py) must resolve against the real ALERT_INVENTORY.

test_failure_diagnosis.py's own ratchet (test_every_declared_alert_id_constant_
resolves_in_the_real_inventory) only ever enumerated failure_diagnosis.py's 6
constants via `vars(failure_diagnosis)`. Four sibling modules --
stranded_alerts.py and the three activities/*.py Temporal activities -- declare
7 more *_ALERT_ID constants of their own, and nothing enumerated those: a future
announce_* added to any of them with a new *_ALERT_ID but no ALERT_INVENTORY
entry would raise KeyError at call time, get swallowed by the caller's
`except Exception: return False`, and be dead on arrival with no test failing --
exactly how BASE_REF_NEEDS_PERSON_ALERT_ID and
CONCURRENT_CLONE_DEFER_WEDGED_ALERT_ID stayed dead (dev.finding 606a8e8d) until
someone noticed by hand.

Discovery here parses source with `ast` rather than importing each module: the
three activities/*.py files are Temporal activities with import-time
dependencies (temporalio) this test suite has no other reason to require, and
parsing is also the only way to see a constant's literal value without running
the module's own sys.path setup. Keying is on (module path, constant name), not
name alone, because UNREACHABLE_ALERT_ID and DRIFT_ALERT_ID are each declared in
three different modules with three different values -- a name-keyed collection
would silently collapse those and under-count.

FACT AT WRITING (measured against this tree, not the filing's arithmetic): 14
*_ALERT_ID constants exist outside tests/ -- 6 in failure_diagnosis.py, 2 each
in stranded_alerts.py, activities/deployed_revision_drift.py,
activities/doctrine_registry_view.py and activities/spec_record_reconcile.py --
and all 14 resolve against the real inventory today. No live defect is hiding;
this closes a forward-looking coverage gap.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failure_diagnosis  # noqa: E402 - also wires tools.notify onto sys.path

notify = failure_diagnosis.notify

_ALERT_ID_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_ALERT_ID$")
_SKIP_DIRNAMES = frozenset({"tests", "__pycache__"})


def _init_git_tree(root: Path) -> None:
    """Turn a synthetic tmp_path tree into a git index `ls-files` can read.

    `git add -A` stages every file into the index; no commit is needed,
    since `git ls-files` reads the index, not history.
    """
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)


def _tracked_python_files(root: Path) -> list[Path]:
    """List git-tracked *.py files under `root`, relative to `root`.

    Goes through `git ls-files` rather than a filesystem walk (e.g.
    `Path.rglob`): a filesystem walk of this repo reaches
    `.claude/worktrees/**`, which holds whole nested checkouts, and would
    silently multiply every match by however many worktrees happen to exist
    at run time.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(Path(name) for name in result.stdout.split("\0") if name)


def discover_alert_id_constants(
    root: Path, *, skip_dirnames: frozenset[str] = _SKIP_DIRNAMES
) -> list[tuple[str, str, str]]:
    """AST-walk every git-tracked .py file under `root` for
    `*_ALERT_ID = "<literal>"` or `*_ALERT_ID: <ann> = "<literal>"`.

    Returns a sorted list of (relative_path, constant_name, alert_id_value)
    triples, one per assignment found anywhere in the module (not just at
    module top level, so a constant declared inside a class or an
    `if TYPE_CHECKING:`-style block is still caught). Source is parsed, never
    imported: this must work without any of the discovered modules' own
    runtime dependencies installed.
    """
    found: list[tuple[str, str, str]] = []
    for relpath in _tracked_python_files(root):
        relparts = relpath.parts
        if any(part in skip_dirnames for part in relparts[:-1]):
            continue
        path = root / relpath
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                if node.value is None:
                    # `NAME: str` with no assigned value -- a valid
                    # AnnAssign, but literal_eval(None) raises. Nothing to
                    # discover here.
                    continue
                targets = [node.target]
                value_node = node.value
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if not _ALERT_ID_NAME_PATTERN.match(target.id):
                    continue
                try:
                    value = ast.literal_eval(value_node)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, str):
                    found.append((str(relpath), target.id, value))
    return sorted(found)


_FACTORY_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_DISCOVERED_ALERT_IDS = discover_alert_id_constants(_FACTORY_DISPATCHER_ROOT)
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: R26.12 B5's seven structural constants: they enter failure_diagnosis.py with
#: inventory entries but no emitter. B6, B13e and B18 each narrow this set by one
#: name as they add the real `announce_*` call that references it.
_R2612_B5_UNEMITTED_ALERT_ID_NAMES = frozenset({
    "QUEUE_NOTHING_SELECTABLE_ALERT_ID",
    "EXPEDITE_PROVENANCE_REJECTED_ALERT_ID",
    "RELEASE_HEALTH_DRIFTING_ALERT_ID",
    "RELEASE_HEALTH_BREACHED_ALERT_ID",
    "RELEASE_HEALTH_RECOVERED_ALERT_ID",
    "RELEASE_HEALTH_UNMEASURED_ALERT_ID",
    "RELEASE_HEALTH_UNMEASURED_PERSISTENT_ALERT_ID",
})


def _tracked_apps_python_files_excluding_tests(root: Path) -> list[Path]:
    """Git-tracked apps/**/*.py files, any `tests` dirname excluded, relative to `root`."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "apps/**/*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = sorted(Path(name) for name in result.stdout.split("\0") if name)
    return [path for path in paths if "tests" not in path.parts]


# ---------------------------------------------------------------------------
# guard: the walk itself must not silently under-deliver
# ---------------------------------------------------------------------------


def test_discovery_is_not_empty_and_covers_every_known_module():
    """A discovery that silently returns fewer ids passes vacuously.

    14 is the measured count on this tree at the time this test was written
    (6 in failure_diagnosis.py + 2 each in stranded_alerts.py and the three
    activities/*.py modules). The floor must never fall below that without an
    explicit, reviewed reason -- a regression here means the walk stopped
    seeing a module it used to see.

    RE-MEASURED 2026-09-17 at the #903 gate: 20. The original 14 was written at
    PARITY with the population, leaving zero slack; six constants have since
    accumulated, so a floor still sitting at 14 tolerated a 30% silent shrink.
    That mattered because the exact pin in
    `test_annassign_widening_matches_nothing_new_on_the_real_tree` was removed
    in the same PR, and this floor was cited as the remaining cover for a
    SHRINKING discovery. It was not: a simulated walker regression from 20 to 16
    passed the whole module. Raised to parity again here. A floor is monotone --
    it never fails on growth, so the next alert id taking the count to 21 keeps
    passing -- which is exactly why the removed exact pin could not stay.

    The earlier observation is kept rather than overwritten: it is a dated
    measurement, and correcting one to match today's tree is how a baseline is
    destroyed.
    """
    assert len(_DISCOVERED_ALERT_IDS) >= 20, _DISCOVERED_ALERT_IDS

    modules_seen = {relpath for relpath, _name, _value in _DISCOVERED_ALERT_IDS}
    assert any(m.endswith("failure_diagnosis.py") for m in modules_seen)
    assert any(m.endswith("stranded_alerts.py") for m in modules_seen)
    assert any(m.endswith("deployed_revision_drift.py") for m in modules_seen)
    assert any(m.endswith("doctrine_registry_view.py") for m in modules_seen)
    assert any(m.endswith("spec_record_reconcile.py") for m in modules_seen)


def test_discovery_keys_on_module_and_name_not_name_alone():
    """UNREACHABLE_ALERT_ID and DRIFT_ALERT_ID each appear in 3 modules with
    distinct values. A name-only key would collapse those to 1 entry each and
    under-count -- exactly the vacuous-pass hazard this bead exists to close.
    """
    names = [name for _relpath, name, _value in _DISCOVERED_ALERT_IDS]
    keys = [(relpath, name) for relpath, name, _value in _DISCOVERED_ALERT_IDS]

    assert len(set(names)) < len(_DISCOVERED_ALERT_IDS), (
        "expected at least one constant NAME to repeat across modules"
    )
    assert len(set(keys)) == len(_DISCOVERED_ALERT_IDS), (
        "(module, name) pairs must be unique: " + repr(_DISCOVERED_ALERT_IDS)
    )

    values_by_name: dict[str, set[str]] = {}
    for _relpath, name, value in _DISCOVERED_ALERT_IDS:
        values_by_name.setdefault(name, set()).add(value)
    assert any(len(values) > 1 for values in values_by_name.values()), (
        "expected at least one repeated NAME to carry different alert id "
        "values across modules, proving a name-only key would have collapsed "
        "distinct alerts: " + repr(values_by_name)
    )


# ---------------------------------------------------------------------------
# the ratchet itself: every discovered id resolves in the real inventory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relpath,name,alert_id",
    _DISCOVERED_ALERT_IDS,
    ids=[f"{relpath}::{name}" for relpath, name, _value in _DISCOVERED_ALERT_IDS],
)
def test_every_discovered_alert_id_resolves_in_the_real_inventory(relpath, name, alert_id):
    definition = notify.get_alert_definition(alert_id)
    assert not definition.is_removed, (
        f"{relpath}:{name} ({alert_id!r}) is registered but removed"
    )


# ---------------------------------------------------------------------------
# direction (a): removing one inventory entry reddens exactly that case
# ---------------------------------------------------------------------------


def test_removing_one_inventory_entry_reddens_only_that_id(monkeypatch):
    """Simulates the failure mode this ratchet exists to catch, in reverse:
    deleting a real ALERT_INVENTORY entry must break resolution for that id
    alone, not for a sibling id, and not silently.
    """
    assert len(_DISCOVERED_ALERT_IDS) >= 2, "need at least two ids to prove isolation"
    (_relpath_a, _name_a, victim_id), (_relpath_b, _name_b, survivor_id) = (
        _DISCOVERED_ALERT_IDS[0],
        _DISCOVERED_ALERT_IDS[1],
    )

    patched_inventory = dict(notify.ALERT_INVENTORY)
    del patched_inventory[victim_id]
    monkeypatch.setattr(notify, "ALERT_INVENTORY", patched_inventory)

    with pytest.raises(KeyError):
        notify.get_alert_definition(victim_id)

    # the sibling id, untouched, still resolves -- the redden is scoped to
    # the one id whose entry was removed, not a blanket failure.
    assert not notify.get_alert_definition(survivor_id).is_removed


# ---------------------------------------------------------------------------
# direction (b): a new *_ALERT_ID in a non-failure_diagnosis module, with no
# inventory entry, is discovered AND fails resolution -- the whole point of
# this bead. Demonstrated against a synthetic tree so nothing real is
# registered and no raising condition changes.
# ---------------------------------------------------------------------------


def test_new_alert_id_in_a_sibling_module_with_no_inventory_entry_reddens(tmp_path):
    (tmp_path / "activities").mkdir()
    (tmp_path / "activities" / "__init__.py").write_text("")
    (tmp_path / "activities" / "some_new_activity.py").write_text(
        'NEW_THING_ALERT_ID = "factory_dispatcher.test_only_unregistered_marker"\n'
    )
    # a tests/ sibling must be skipped, same as the real tree.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_some_new_activity.py").write_text(
        'IGNORED_ALERT_ID = "factory_dispatcher.should_never_be_discovered"\n'
    )
    _init_git_tree(tmp_path)

    discovered = discover_alert_id_constants(tmp_path)

    assert discovered == [
        (
            str(Path("activities") / "some_new_activity.py"),
            "NEW_THING_ALERT_ID",
            "factory_dispatcher.test_only_unregistered_marker",
        )
    ]

    _relpath, _name, new_alert_id = discovered[0]
    with pytest.raises(KeyError):
        notify.get_alert_definition(new_alert_id)


# ---------------------------------------------------------------------------
# the AnnAssign blind spot: an `isinstance(node, ast.Assign)`-only walker
# never sees `NAME: str = "value"`, so an annotated alert id constant would
# be invisible to the ratchet above. dev.finding bbad4f8d.
# ---------------------------------------------------------------------------


def test_annotated_declaration_is_discovered_same_as_plain(tmp_path):
    """`NAME: str = "value"` must be found with the same name and value as
    the unannotated `NAME = "value"` form -- the widening changes which node
    types are examined, not the match predicate or the literal extraction.
    """
    (tmp_path / "plain_module.py").write_text(
        'SOME_THING_ALERT_ID = "factory_dispatcher.same_marker"\n'
    )
    (tmp_path / "annotated_module.py").write_text(
        'SOME_THING_ALERT_ID: str = "factory_dispatcher.same_marker"\n'
    )
    _init_git_tree(tmp_path)

    discovered = discover_alert_id_constants(tmp_path)

    assert discovered == [
        (
            str(Path("annotated_module.py")),
            "SOME_THING_ALERT_ID",
            "factory_dispatcher.same_marker",
        ),
        (
            str(Path("plain_module.py")),
            "SOME_THING_ALERT_ID",
            "factory_dispatcher.same_marker",
        ),
    ]


def test_bare_annotation_with_no_value_is_skipped_not_raised(tmp_path):
    """`NAME: str` alone is a valid AnnAssign whose `.value` is None.
    `ast.literal_eval(None)` raises -- the specific way a naive isinstance
    widening breaks. The walker must skip it, not crash.
    """
    (tmp_path / "bare_annotation_module.py").write_text(
        'BARE_THING_ALERT_ID: str\n'
        'REAL_THING_ALERT_ID = "factory_dispatcher.real_marker"\n'
    )
    _init_git_tree(tmp_path)

    discovered = discover_alert_id_constants(tmp_path)  # must not raise

    assert discovered == [
        (
            str(Path("bare_annotation_module.py")),
            "REAL_THING_ALERT_ID",
            "factory_dispatcher.real_marker",
        )
    ]


def test_annotated_alert_id_missing_from_inventory_reddens_and_registering_it_greens(
    tmp_path, monkeypatch
):
    """The case this bead exists for: an alert id declared with a type
    annotation, absent from ALERT_INVENTORY. It must be discovered AND fail
    resolution -- and once registered, resolve cleanly. Both directions, or
    the test proves nothing.
    """
    (tmp_path / "activities").mkdir()
    (tmp_path / "activities" / "annotated_activity.py").write_text(
        'ANNOTATED_NEW_THING_ALERT_ID: str = '
        '"factory_dispatcher.test_only_annotated_marker"\n'
    )
    _init_git_tree(tmp_path)

    discovered = discover_alert_id_constants(tmp_path)

    assert discovered == [
        (
            str(Path("activities") / "annotated_activity.py"),
            "ANNOTATED_NEW_THING_ALERT_ID",
            "factory_dispatcher.test_only_annotated_marker",
        )
    ]
    _relpath, _name, new_alert_id = discovered[0]

    # direction (a): not registered -> resolution fails.
    with pytest.raises(KeyError):
        notify.get_alert_definition(new_alert_id)

    # direction (b): registered -> resolution succeeds.
    patched_inventory = dict(notify.ALERT_INVENTORY)
    patched_inventory[new_alert_id] = next(iter(notify.ALERT_INVENTORY.values()))
    monkeypatch.setattr(notify, "ALERT_INVENTORY", patched_inventory)

    assert not notify.get_alert_definition(new_alert_id).is_removed


# ---------------------------------------------------------------------------
# pin: widening to AnnAssign must not change discovery over the real tree,
# where zero annotated *_ALERT_ID constants exist today.
# ---------------------------------------------------------------------------


def _annotated_alert_id_constants(root: Path) -> list[tuple[str, str, str]]:
    """The `*_ALERT_ID: <ann> = "<literal>"` constants only -- the AnnAssign
    half of what :func:`discover_alert_id_constants` matches.

    Separate walker rather than a flag on the main one: the test below must be
    able to say "the widening matches nothing new on the real tree" without
    depending on the widened walker it is checking.
    """
    found: list[tuple[str, str, str]] = []
    for relpath in _tracked_python_files(root):
        if any(part in _SKIP_DIRNAMES for part in relpath.parts[:-1]):
            continue
        path = root / relpath
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.AnnAssign) or node.value is None:
                continue
            target = node.target
            if not isinstance(target, ast.Name):
                continue
            if not _ALERT_ID_NAME_PATTERN.match(target.id):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if isinstance(value, str):
                found.append((str(relpath), target.id, value))
    return sorted(found)


# ---------------------------------------------------------------------------
# R26.12 B5: the seven new constants are structural -- no emitter exists yet.
# B6, B13e and B18 are the beads that each add one `announce_*` call and so
# each narrow this set by the one name their emitter references.
# ---------------------------------------------------------------------------


_FAILURE_DIAGNOSIS_RELPATH = str(Path("apps") / "factory-dispatcher" / "failure_diagnosis.py")


def test_r2612_b5_seven_new_constant_names_have_no_emitter_outside_failure_diagnosis():
    """ast-scans every git-tracked apps/**/*.py file (tests/ excluded) for an
    `ast.Name` or `ast.Attribute` node matching one of the seven R26.12 B5 constant
    names. The ONLY reference any of the seven may have anywhere in the tree is
    its own module-level `ast.Store` assignment target in
    apps/factory-dispatcher/failure_diagnosis.py, where the seven are declared --
    every other occurrence, including a Load, an Attribute access, or a non-
    module-level assignment inside failure_diagnosis.py itself, is an emitter and
    a violation, since this bead adds no emitter. B6 (queue_nothing_selectable),
    B13e (release_health_unmeasured_persistent, plus B13 for the other three
    release_health_* alerts) and B18 (expedite_provenance_rejected) each add a
    real `announce_*` call that references one of these names from a module
    other than failure_diagnosis.py, which narrows this set by that name when
    they land.
    """
    violations: dict[str, set[str]] = {}
    allowed_store_name_hits: list[str] = []

    for relpath in _tracked_apps_python_files_excluding_tests(_REPO_ROOT):
        is_failure_diagnosis = str(relpath) == _FAILURE_DIAGNOSIS_RELPATH
        tree = ast.parse((_REPO_ROOT / relpath).read_text(), filename=str(relpath))

        allowed_node_ids: set[int] = set()
        if is_failure_diagnosis:
            for stmt in tree.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Name)
                        and isinstance(target.ctx, ast.Store)
                        and target.id in _R2612_B5_UNEMITTED_ALERT_ID_NAMES
                    ):
                        allowed_node_ids.add(id(target))
                        allowed_store_name_hits.append(target.id)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                if is_failure_diagnosis and id(node) in allowed_node_ids:
                    continue
                name = node.id
            else:
                continue
            if name in _R2612_B5_UNEMITTED_ALERT_ID_NAMES:
                violations.setdefault(str(relpath), set()).add(name)

    assert violations == {}, violations
    assert sorted(allowed_store_name_hits) == sorted(_R2612_B5_UNEMITTED_ALERT_ID_NAMES), (
        "each of the seven names must have exactly one allowed module-level "
        "Store assignment in failure_diagnosis.py: " + repr(allowed_store_name_hits)
    )


def test_annassign_widening_matches_nothing_new_on_the_real_tree():
    """Widening the walker to also see AnnAssign must not change what is
    discovered on the real tree, because no annotated alert id exists there.

    ORIGINALLY WRITTEN as `len(_DISCOVERED_ALERT_IDS) == 18` -- the measured
    count at the time. That pinned a GROWING population to a constant: every
    later change that legitimately adds an alert id broke this test, and on
    2026-09-17 a batch of merges that were each green alone turned main red
    together, at `assert 20 == 18`. No textual merge check can see that; it is
    a semantic collision on a global count.

    The count was never the invariant. The invariant is that the AnnAssign
    widening matches NOTHING NEW on the real tree, which is stated directly
    here and is immune to the population growing. The floor against silent
    under-delivery is a separate concern and already lives in
    `test_discovery_is_not_empty_and_covers_every_known_module`.
    """
    annotated = _annotated_alert_id_constants(_FACTORY_DISPATCHER_ROOT)
    assert annotated == [], (
        "an annotated *_ALERT_ID constant now exists on the real tree, so the "
        "AnnAssign widening is no longer a no-op there: " + repr(annotated)
    )
    assert all(
        isinstance(value, str) for _relpath, _name, value in _DISCOVERED_ALERT_IDS
    )
