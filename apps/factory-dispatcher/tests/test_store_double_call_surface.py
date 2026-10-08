"""A hand-rolled Substrate/BeadStore test double is a second, hand-kept copy
of "what the store can do" -- and nothing asserts it covers what the
production code it stands in for actually calls. dev.finding 24970f0e: PR
#864 taught ``file_task.main()`` to call ``sub.create_task(...)`` and updated
every ``FakeSubstrate`` that existed at its base; PR #853, based on an older
commit, added a fourth ``FakeSubstrate`` in a brand-new file. Each PR was
individually green. Merged, either order, the tree was red:
``AttributeError: 'FakeSubstrate' object has no attribute 'create_task'``.
Twenty-two more doubles across nineteen files could drift the same way, and
nothing said so.

This is deliberately NOT "does every double implement the whole BeadStore
protocol" -- ``test_store_call_surface.py`` already established that the
protocol (eleven methods) is far more than any one caller needs, and the
census behind this file found the *median* hand-rolled double implements
four of them, correctly, not as debt. Instead: for each hand-rolled double,
derive -- from the production source, never from a hand-kept list -- which
store methods are *unconditionally* reachable from whichever production
entry points the double's own test file actually calls (skipping branches
inside ``if``/``for``/``while``/``except``, since a specific test may never
drive those), and assert the double's own-or-locally-inherited methods cover
that set. A double whose test never reaches a conditional branch has no need
of what only that branch calls -- so this does not, and must not, flag the
census's small doubles as broken.

No substrate, no network: pure source analysis, exactly like its sibling.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO_ROOT = APP.parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from beadstore import BeadStore  # noqa: E402
from test_store_call_surface import SURFACE_MODULES, STORE_RECEIVERS  # noqa: E402

#: The BeadStore protocol's own surface -- what a double could conceivably
#: need to cover, never more.
PROTOCOL_METHODS = {
    n for n in dir(BeadStore) if not n.startswith("_") and callable(getattr(BeadStore, n, None))
}

#: These test the BeadStore protocol / the shared contract-suite harness
#: itself -- deliberately-incomplete fixtures probing protocol conformance
#: (e.g. "does an implementation missing create_task still satisfy
#: isinstance"), not doubles standing in for a store under dispatcher
#: business-logic tests. Named here, visibly, rather than silently filtered.
NOT_DOUBLES_HERE = {"test_beadstore.py", "test_store_contract.py"}

#: Every tree whose git-tracked test files the ceiling test and the
#: call-surface test below scan for hand-rolled Substrate/BeadStore doubles.
#: One declaration -- ``_tracked_test_files()`` is the only place either test
#: reads it -- so the two checks cannot drift apart from each other the way a
#: single hard-coded ``"apps/factory-dispatcher/tests"`` once let them: that
#: hard-coding left 8 doubles in apps/mcp-hub/tests and scripts/tests
#: unguarded by either check while this file's own docstring and dated notes
#: below kept reading as though the control were repo-wide.
DOUBLE_SCAN_TREES = (
    "apps/factory-dispatcher/tests",
    "apps/mcp-hub/tests",
    "scripts/tests",
)

#: Dated observation, 2026-09-16 -- not a target. Measured by this file's own
#: methodology (a class in a git-tracked tests/ file, excluding the pair
#: above, whose own-or-locally-inherited methods overlap at least two
#: BeadStore protocol names; a class subclassed locally within the same file
#: counts once, under its root, since a subclass only specialises its base
#: rather than independently drifting from it). A companion census on
#: dev.finding 24970f0e, walking the same git-tracked tree by a different
#: method, measured 22 doubles across 19 files; this file's own count is 21
#: across 19 -- close enough to corroborate, not identical, because "what
#: counts as a double" is itself a judgment call and the two methods make it
#: differently. Either way: this SHALL NOT grow silently. A new double is
#: then a deliberate, visible line in a diff, not a name that quietly
#: widened the count.
#: RAISED 21 -> 22 on 2026-09-17, deliberately and in the same diff that adds
#: the double, which is what this ratchet's own failure message asks for. The
#: new double is `FakeSubstrate` in tests/test_file_task_pr_prerequisite.py,
#: added by PR #853 -- the very PR whose collision with #864 is
#: dev.finding 24970f0e and this file's worked example. #853 was written before
#: this guard existed; the guard caught it on the merged tree exactly as
#: designed, which is the outcome it was built for rather than a surprise.
#:
#: RAISED 22 -> 23 ON 2026-09-17, deliberately and in the diff that adds the
#: double, which is what the assertion message asks for. The new one is the
#: store double in tests/test_held_pr_terminal_states.py, added by this same
#: pull request for a surface that did not exist before. Left AT PARITY:
#: 23 permitted, 23 actual, headroom 0 -- a ratchet raised with slack stops
#: ratcheting. (22 was this PR's original figure, against a main that still
#: held 21; #853's double landed first, so the rebased tree carries both.)
#:
#: (A merge-order note that stood here on 2026-09-17 has been removed at the
#: #915 gate. It claimed that merging this PR and #909 -- which raises the same
#: constant -- would silently redden main, and cited dev.finding-class bead
#: 54bbfc73. BOTH HALVES WERE WRONG. `git merge-tree --write-tree` reports a
#: CONFLICT on this very line, because both PRs replace it; git refuses the
#: second merge loudly rather than letting an over-ceiling count through.
#: And 54bbfc73 is the OPPOSITE shape -- its own intent records that
#: every PR in that incident had 'a git merge-tree CLEAN result against every
#: other open PR'. Citing it here taught the reader that this class is silent,
#: when this instance is the one kind that is loud. A transient, dated
#: merge-order constraint also does not belong in permanent source: it is stale
#: the moment both PRs land, naming two dead PR numbers. It lives in the pull
#: request body and the gate verdict instead.)
#:
#: RAISED 23 -> 24 ON 2026-09-17, deliberately and in the diff that adds the
#: double, which is exactly what the assertion message asks for. The new one is
#: `FakeSubstrate` in
#: tests/test_http_filed_task_reaches_the_environmental_bound.py, added by this
#: same pull request to cover a surface that did not exist before: a task filed
#: over HTTP reaching the dispatcher's consecutive-environmental-fault bound.
#: It was checked against the live contract rather than waved through -- its
#: `add_note` spreads `**extra` into the note content exactly as
#: `substrate.Substrate.add_note` does, so it is NOT the 2026-08 shape that
#: accepted more than the real dependency and shipped `--bind-pr` with no
#: provenance. One divergence is recorded rather than hidden: the double omits
#: `trust_tier`, which the real method takes and places in the PAYLOAD; a caller
#: passing it here would have it absorbed by `**extra` and land in CONTENT
#: instead. That does not affect this bead's tests, and narrowing the double is
#: left to the gate's judgement rather than changed silently here.
#:
#: RAISED 24 -> 32 ON 2026-09-19, widening ``DOUBLE_SCAN_TREES`` above from the
#: single hard-coded ``apps/factory-dispatcher/tests`` to also cover
#: ``apps/mcp-hub/tests`` and ``scripts/tests`` -- both checks scanned exactly
#: one tree while the same store contract drifted unguarded in the other two
#: (dev.finding 24970f0e's own incident, ``FakeSubstrate.add_note`` accepting
#: ``**kwargs``, is exactly the class of double this control exists to catch,
#: and nothing stopped a copy of it from landing outside
#: apps/factory-dispatcher/tests). Re-derived by running this file's own
#: ``_hand_rolled_doubles_from_source`` over every file ``_tracked_test_files()``
#: now returns, not by arithmetic on the old ceiling: 24 in
#: apps/factory-dispatcher/tests (unchanged), 3 in apps/mcp-hub/tests
#: (``FakeStore`` and ``FakeReleaseReader`` in test_factory_status.py,
#: ``FakeStore`` in test_task_filing.py), 5 in scripts/tests across four files
#: (``FakeSubstrate`` in test_ea_derive_dependencies.py and test_ea_load.py,
#: ``_FakeReader`` and ``_BrokenReader`` in test_release_balance_report.py,
#: ``_FakeReader`` in test_release_cross_store_join.py) -- 32 total. All 8
#: newly-counted doubles are admitted here, not exempted: each stands in for a
#: real reader/substrate dependency and drives a real production entry point
#: (release-status.py's ``main``, ea-derive.py's ``reconcile_dependencies``,
#: ea-load.py's ``build_plan``, factory_status.py's ``task_runnability`` and
#: ``release_delivery``, task_filing's own filer) rather than being a
#: protocol-conformance fixture like ``NOT_DOUBLES_HERE``'s pair. Left at
#: parity: 32 permitted, 32 actual, headroom 0 -- a ratchet raised with slack
#: stops ratcheting.
#:
#: RAISED 32 -> 33 ON 2026-09-19 (dev.finding 91dedb1a, gate finding F2 on
#: PR #953), for exactly one new double: `FakeSubstrate` in the new
#: tests/test_base_verification_check_wiring.py, added in the same diff to
#: cover a surface that did not exist before (record_failure_activity's
#: wiring to the new base-revision check). Composed census re-derived, not
#: computed by arithmetic on the prior ceiling alone: 25 in
#: apps/factory-dispatcher/tests (24 + this one), 3 in apps/mcp-hub/tests
#: (unchanged), 5 in scripts/tests (unchanged) -- 33 total. Left at parity:
#: 33 permitted, 33 actual, headroom 0.
#:
#: RAISED 33 -> 36 ON 2026-09-19 (OPS-181, PR #962; rebased over PR #961's
#: 32 -> 33 above), deliberately and in the same diff that adds
#: the double(s), which is exactly what this ratchet's own failure message
#: asks for. OPS-181/OPS-182 gave ``activities/status_bead.py``'s
#: ``StatusSubstrate`` protocol a ``find_bead`` method (the standing-status
#: lookup that replaced a page scan); every double that already implemented
#: ``list_beads`` for that protocol gained ``find_bead`` too, per the bead's
#: own acceptance criteria. ``find_bead`` also happens to be a
#: ``BeadStore`` protocol method name (a coincidence of naming, the same
#: shape ``NOT_THIS_SUBSTRATE_HERE`` in test_store_double_signature_surface.py
#: already documents for ``list_beads``/``add_link``), so a double that
#: previously overlapped ``BeadStore`` on one name now overlaps on two and
#: is newly counted here: ``FakeSub`` in test_requirements_apply.py,
#: ``_RecordingSubstrate`` in test_status_bead.py, and the new
#: ``FixtureSubstrate`` in test_status_bead_lookup.py (+3). None of the three
#: stand in for the dispatcher's own ``BeadStore`` -- they mirror the
#: ``scripts/substrate_client.py``/``status_bead.py`` arch-side contract, the
#: same category ``NOT_THIS_SUBSTRATE_HERE`` names, and are added there too.
#: Composed census re-derived on the rebased tree: 28 in
#: apps/factory-dispatcher/tests (25 + these three), 3 in apps/mcp-hub/tests,
#: 5 in scripts/tests -- 36 total. Left at parity: 36 permitted, 36 actual,
#: headroom 0.
#:
#: LOWERED 36 -> 35 ON 2026-09-20 (the bd/Dolt archival: Amendment 33
#: rescinded the bd migration and Amendment 35 was withdrawn 2026-09-07).
#: Archiving scripts/tests/test_release_cross_store_join.py to
#: docs/archive/2026-09-bd-dolt-evaluation/ removes its ``_FakeReader`` from
#: the census -- re-derived by running this file's own
#: ``_hand_rolled_doubles_from_source`` over every file
#: ``_tracked_test_files()`` now returns on the changed tree, not by
#: arithmetic on the old ceiling: 28 in apps/factory-dispatcher/tests
#: (unchanged), 3 in apps/mcp-hub/tests (unchanged), 4 in scripts/tests
#: (5 - the one removed) -- 35 total. Left at parity: 35 permitted, 35
#: actual, headroom 0 -- a ratchet re-measured down, not left with slack.
MAX_HAND_ROLLED_DOUBLES = 35


def _git_tracked_py_files(root: Path, subdir: str) -> list[Path]:
    """Every ``.py`` file ``root``'s git index actually tracks under
    ``subdir`` -- via ``git ls-files``, never a filesystem walk. A walk
    descends into ``.claude/worktrees/**``, each a full nested checkout, and
    inflates a count of 22 into an artefact of 62 (the exact trap this
    bead's gate prompt names). ``git ls-files`` never sees into a nested
    worktree: it is either untracked disk content at this path, or a
    separate repository entirely, and either way is not part of ``root``'s
    own tracked tree here."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", subdir],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / line for line in out.splitlines() if line.endswith(".py")]


def _calls_in_simple_stmt(stmt: ast.stmt, functions: dict[str, ast.AST]) -> tuple[set[str], set[str]]:
    """Store-attribute calls, and store-forwarding calls to another function
    in ``functions``, found anywhere in one statement. Safe to ``ast.walk``
    here without over-collecting: a "simple" statement (``Assign``, ``Expr``,
    ``Return``, ...) cannot itself contain a nested block statement."""
    direct: set[str] = set()
    forwarded: set[str] = set()
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in STORE_RECEIVERS
        ):
            direct.add(node.func.attr)
        callee = None
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        if callee and callee in functions:
            passed_store = any(
                isinstance(a, ast.Name) and a.id in STORE_RECEIVERS for a in node.args
            ) or any(
                isinstance(kw.value, ast.Name) and kw.value.id in STORE_RECEIVERS
                for kw in node.keywords
            )
            if passed_store:
                forwarded.add(callee)
    return direct, forwarded


def _unconditional_calls(
    body: list[ast.stmt], functions: dict[str, ast.AST]
) -> tuple[set[str], set[str]]:
    """Store calls (direct, or forwarded to another function in the same
    module) that execute on every call of the enclosing function -- the
    top-level body, ``Try.body``/``finalbody``, and ``With.body``, but
    explicitly *not* ``If``/``For``/``While`` bodies or exception handlers,
    which depend on runtime branching one specific test may never drive."""
    direct: set[str] = set()
    forwarded: set[str] = set()
    for stmt in body:
        if isinstance(stmt, ast.If):
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            continue
        if isinstance(stmt, ast.Try):
            d, f = _unconditional_calls(stmt.body, functions)
            direct |= d
            forwarded |= f
            d, f = _unconditional_calls(stmt.finalbody, functions)
            direct |= d
            forwarded |= f
            continue
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            d, f = _unconditional_calls(stmt.body, functions)
            direct |= d
            forwarded |= f
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        d, f = _calls_in_simple_stmt(stmt, functions)
        direct |= d
        forwarded |= f
    return direct, forwarded


def _module_function_surfaces_from_source(source: str, filename: str) -> dict[str, set[str]]:
    """For every top-level function in ``source``, the BeadStore protocol
    methods unconditionally reachable from it -- directly, or via a chain of
    calls to other top-level functions the store is passed along to."""
    tree = ast.parse(source, filename=filename)
    functions = {
        n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_calls: dict[str, set[str]] = {}
    forwards: dict[str, set[str]] = {}
    for name, node in functions.items():
        d, f = _unconditional_calls(node.body, functions)
        direct_calls[name] = d
        forwards[name] = f

    def reachable(name: str, seen: set[str] | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        if name in seen:
            return set()
        seen.add(name)
        result = set(direct_calls.get(name, set()))
        for nxt in forwards.get(name, set()):
            result |= reachable(nxt, seen)
        return result

    return {name: reachable(name) & PROTOCOL_METHODS for name in functions}


def _module_function_surfaces(path: Path) -> dict[str, set[str]]:
    return _module_function_surfaces_from_source(path.read_text(), str(path))


def _dispatcher_function_surfaces() -> dict[str, set[str]]:
    """The union, across every dispatcher-side module that holds a store
    (the same ``SURFACE_MODULES`` the sibling call-surface test derives
    from), of each top-level function's name to what it unconditionally
    calls on a store."""
    merged: dict[str, set[str]] = {}
    for rel in SURFACE_MODULES:
        path = APP / rel
        if not path.exists():
            continue
        for name, calls in _module_function_surfaces(path).items():
            merged.setdefault(name, set()).update(calls)
    return merged


#: Computed once at collection time from the real dispatcher source -- not a
#: hand-kept list, so it moves when the production call sites do.
FUNCTION_TO_SURFACE = _dispatcher_function_surfaces()


def _required_surface_for_test_source(
    source: str, function_to_surface: dict[str, set[str]]
) -> tuple[set[str], set[str]]:
    """Which store methods a test file's double(s) need to cover: the union
    of ``function_to_surface[name]`` for every production entry point
    literally called anywhere in ``source``."""
    tree = ast.parse(source)
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
    matched = called_names & function_to_surface.keys()
    required: set[str] = set()
    for name in matched:
        required |= function_to_surface[name]
    return required, matched


def _hand_rolled_double_nodes_from_source(
    source: str,
) -> list[tuple[str, ast.ClassDef, set[str]]]:
    """Every class in ``source`` that counts as a hand-rolled Substrate
    double: not a ``Test*`` collection class, with own-or-locally-inherited
    methods overlapping at least two BeadStore protocol names. A class
    locally subclassed in the same file (e.g. ``class Broken(FakeSubstrate)``
    nested in one test, injecting a failure) is folded into its root rather
    than counted a second time -- it only specialises the base, so checking
    the base covers it.

    Returns the class's own :class:`ast.ClassDef` alongside its name and
    effective method-name set, so a caller that needs the actual method
    bodies (e.g. to inspect a signature) does not have to re-parse and
    re-match by name -- a second lookup that could resolve to the wrong node
    if two same-named classes exist in one file, exactly the kind of drift
    this file's own doubles guard against.
    """
    tree = ast.parse(source)
    classes: dict[str, list[ast.ClassDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.setdefault(node.name, []).append(node)

    def own_methods(node: ast.ClassDef) -> set[str]:
        return {
            n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def local_base_name(node: ast.ClassDef) -> str | None:
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                return base.id
        return None

    def effective(node: ast.ClassDef, seen: set[int] | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        if id(node) in seen:
            return set()
        seen.add(id(node))
        methods = own_methods(node)
        base_name = local_base_name(node)
        if base_name:
            for candidate in classes[base_name]:
                methods |= effective(candidate, seen)
        return methods

    results: list[tuple[str, ast.ClassDef, set[str]]] = []
    for name, nodes in classes.items():
        if name.startswith("Test"):
            continue
        for node in nodes:
            if local_base_name(node) is not None:
                continue  # folded into its root above
            eff = effective(node)
            if len(eff & PROTOCOL_METHODS) >= 2:
                results.append((name, node, eff))
    return results


def _hand_rolled_doubles_from_source(source: str) -> list[tuple[str, set[str]]]:
    """Name and effective method-name set for every hand-rolled double in
    ``source`` -- see :func:`_hand_rolled_double_nodes_from_source`, which
    this is a thin projection of."""
    return [(name, eff) for name, _node, eff in _hand_rolled_double_nodes_from_source(source)]


def _offenders_for(
    function_to_surface: dict[str, set[str]], filename: str, source: str
) -> list[tuple[str, str, list[str]]]:
    """The check itself: hand-rolled doubles in ``source`` that don't cover
    what the production entry points ``source`` actually calls unconditionally
    need. Shared by the real-repo test and the constructed fixtures below, so
    a fixture exercises the exact same logic the real check runs -- not a
    reimplementation that could quietly drift from it."""
    doubles = _hand_rolled_doubles_from_source(source)
    if not doubles:
        return []
    required, _ = _required_surface_for_test_source(source, function_to_surface)
    offenders = []
    for name, effective_methods in doubles:
        missing = required - effective_methods
        if missing:
            offenders.append((filename, name, sorted(missing)))
    return offenders


def _tracked_test_files() -> list[Path]:
    files: list[Path] = []
    for tree in DOUBLE_SCAN_TREES:
        files.extend(_git_tracked_py_files(REPO_ROOT, tree))
    return files


def test_every_hand_rolled_double_covers_what_its_own_file_calls():
    offenders: list[tuple[str, str, list[str]]] = []
    for path in _tracked_test_files():
        if path.name in NOT_DOUBLES_HERE:
            continue
        offenders += _offenders_for(
            FUNCTION_TO_SURFACE, str(path.relative_to(REPO_ROOT)), path.read_text()
        )
    assert not offenders, (
        "hand-rolled Substrate doubles missing a method the production code "
        f"their own test file actually calls unconditionally invokes: {offenders}"
    )


def test_the_derivation_actually_sees_doubles_and_surface():
    """A parser that silently matched nothing would pass the assertion above
    vacuously."""
    assert FUNCTION_TO_SURFACE, "derived production call surface is empty — receiver convention drifted?"
    total = sum(
        len(_hand_rolled_doubles_from_source(p.read_text()))
        for p in _tracked_test_files()
        if p.name not in NOT_DOUBLES_HERE
    )
    assert total > 10, f"implausibly few hand-rolled doubles found ({total}) — detection drifted?"


def test_tracked_test_files_spans_every_declared_tree():
    """A typo in ``DOUBLE_SCAN_TREES``, or a tree ``git ls-files`` silently
    returns nothing for, would leave the ceiling and the coverage check
    scanning fewer trees than they claim to -- exactly the "one tree
    hard-coded, two others unguarded" defect this widening exists to close,
    reintroduced silently instead of loudly. Assert each declared tree
    actually contributes a tracked file, not just that the aggregate total
    below looks plausible."""
    found = {p.relative_to(REPO_ROOT).as_posix() for p in _tracked_test_files()}
    for tree in DOUBLE_SCAN_TREES:
        assert any(p.startswith(f"{tree}/") for p in found), (
            f"no git-tracked .py files found under {tree!r} -- "
            "DOUBLE_SCAN_TREES entry wrong, or the tree scan silently matched nothing"
        )


def test_the_hand_rolled_double_count_has_not_grown():
    total = sum(
        len(_hand_rolled_doubles_from_source(p.read_text()))
        for p in _tracked_test_files()
        if p.name not in NOT_DOUBLES_HERE
    )
    assert total <= MAX_HAND_ROLLED_DOUBLES, (
        f"{total} hand-rolled Substrate doubles found, exceeding the pinned "
        f"ceiling of {MAX_HAND_ROLLED_DOUBLES} (dev.finding 24970f0e) — if this "
        "growth is deliberate, raise the constant in the same diff that adds "
        "the double"
    )


def test_fires_on_the_853_864_collision_reduced_to_one_file():
    """The real historical case (dev.finding 24970f0e), reduced to one
    production function and one double: PR #864 taught ``file_task.main()``
    to call ``sub.create_task(...)`` unconditionally; PR #853, based on an
    older commit, added a fourth ``FakeSubstrate`` that only ever needed
    ``list_tasks``. Merged, the tree crashed with ``AttributeError:
    'FakeSubstrate' object has no attribute 'create_task'``. This check must
    name the double (file and class) and the missing method."""
    production_source = (
        "def main(argv=None):\n"
        "    sub = Substrate()\n"
        "    tasks = sub.list_tasks()\n"
        "    bead = sub.create_task({'title': 'x'}, 'factory-dispatcher')\n"
        "    return bead\n"
    )
    function_to_surface = _module_function_surfaces_from_source(production_source, "file_task.py")
    assert function_to_surface["main"] == {"list_tasks", "create_task"}

    test_source = (
        "class FakeSubstrate:\n"
        "    def __init__(self):\n"
        "        self.tasks = []\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return list(self.tasks)\n"
        "    def add_note(self, parent_id, kind, body, created_by, **extra):\n"
        "        pass\n"
        "\n"
        "def test_files_a_task():\n"
        "    main(FakeSubstrate())\n"
    )

    offenders = _offenders_for(function_to_surface, "test_new_double.py", test_source)

    assert len(offenders) == 1
    filename, class_name, missing = offenders[0]
    assert filename == "test_new_double.py"
    assert class_name == "FakeSubstrate"
    assert missing == ["create_task"]


def test_does_not_fire_on_a_double_that_covers_a_conditional_branch_its_test_never_takes():
    """A double must not be required to cover a branch only some other test
    drives. ``add_link`` below is real production shape (``file_task.main``'s
    own ``if release_binding is not None: sub.add_link(...)``): gated behind
    a condition, not on the double's required path, so a double whose test
    never sets up a release binding has no need of it."""
    production_source = (
        "def main(argv=None, release_binding=None):\n"
        "    sub = Substrate()\n"
        "    tasks = sub.list_tasks()\n"
        "    bead = sub.create_task({'title': 'x'}, 'factory-dispatcher')\n"
        "    if release_binding is not None:\n"
        "        sub.add_link(bead['id'], release_binding, 'delivers', 'factory-dispatcher')\n"
        "    return bead\n"
    )
    function_to_surface = _module_function_surfaces_from_source(production_source, "file_task.py")
    assert function_to_surface["main"] == {"list_tasks", "create_task"}  # add_link excluded: conditional

    test_source = (
        "class FakeSubstrate:\n"
        "    def __init__(self):\n"
        "        self.tasks = []\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return list(self.tasks)\n"
        "    def create_task(self, content, created_by, *, trust_tier='user'):\n"
        "        return {'id': 'dev-task-1'}\n"
        "\n"
        "def test_files_a_task_with_no_release_binding():\n"
        "    main()\n"
    )

    assert _offenders_for(function_to_surface, "test_small_double.py", test_source) == []


def test_does_not_fire_on_a_real_small_double_whose_test_never_drives_the_rest():
    """Control from the census itself, not a constructed fixture: this real
    double implements 4 of the protocol's 11 methods -- the census's stated
    median, not debt -- and must stay green."""
    path = TESTS_DIR / "test_dispatch_steps_capacity.py"
    source = path.read_text()
    doubles = dict(_hand_rolled_doubles_from_source(source))
    assert "FakeSubstrate" in doubles
    assert len(doubles["FakeSubstrate"] & PROTOCOL_METHODS) == 4

    offenders = _offenders_for(FUNCTION_TO_SURFACE, str(path.relative_to(REPO_ROOT)), source)
    assert offenders == []


def test_enumeration_excludes_nested_worktree_content(tmp_path):
    """The trap this bead's gate prompt names explicitly: a filesystem walk
    from the repo root descends into ``.claude/worktrees/**``, each a whole
    nested checkout, and counts its test doubles as this repo's own (62
    phantom doubles instead of 22, on this exact bead). Built here rather
    than relying on a worktree happening to exist."""
    repo = tmp_path / "repo"
    tests_dir = repo / "apps" / "factory-dispatcher" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_real.py").write_text(
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def list_notes(self, parent_id, limit=500):\n"
        "        return []\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=test@example.com", "-c", "user.name=test",
            "commit", "-q", "-m", "seed",
        ],
        check=True,
    )

    # A whole nested checkout under .claude/worktrees/, as a real worktree
    # leaves on disk -- untracked from this repo's own perspective at this
    # path, exactly like the artefact that once inflated 22 into 62.
    phantom_dir = repo / ".claude" / "worktrees" / "phantom" / "apps" / "factory-dispatcher" / "tests"
    phantom_dir.mkdir(parents=True)
    (phantom_dir / "test_phantom.py").write_text(
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def list_notes(self, parent_id, limit=500):\n"
        "        return []\n"
    )

    found = _git_tracked_py_files(repo, "apps/factory-dispatcher/tests")
    names = {p.name for p in found}
    assert names == {"test_real.py"}
    assert "test_phantom.py" not in names
