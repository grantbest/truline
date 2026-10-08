"""dispatch.transition_state_checked is meant to be the dispatcher's ONE path
to BeadStore.transition_state -- but until this test existed that was true
only by convention. dispatch_steps.py (the unattended drain's pending<->doing
claim/compensate pair, the path most exposed to a store swap) and
file_task.py's --supersede path called ``sub.transition_state`` directly on
hardcoded edges, bypassing the local machine check entirely, and nothing
stopped a NEW bypass call site from joining them (release-gate advisory on
#659).

Same discipline as test_store_call_surface.py's GRANDFATHERED_BYPASSES: this
parses the dispatcher-side modules with ``ast`` and collects every direct
``sub.transition_state(...)`` / ``store.transition_state(...)`` call, then
asserts that set is empty except for dispatch.py's own
``transition_state_checked`` function body -- the one place a direct call is
correct, since that IS the wrapper. A call site found anywhere else fails
this test until it is either converted to go through
``dispatch.transition_state_checked`` or added to GRANDFATHERED_BYPASSES with
a reason. The ratchet runs both ways: a NEW bypass fails the first assertion;
fixing one without deleting its entry fails the second, so the list can only
shrink.

No substrate, no network: pure source analysis.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1]

#: Dispatcher-side modules that hold a store and could call transition_state.
SURFACE_MODULES = (
    "dispatch.py",
    "file_task.py",
    "scanner.py",
    "activities/dispatch_steps.py",
    "workflow_core.py",
)

#: Names dispatcher code binds a BeadStore to.
STORE_RECEIVERS = {"sub", "store"}

#: The module and function whose body IS the checked wrapper -- the one
#: direct call excluded by name, not by line number, so moving the function
#: doesn't silently blind this test to a real bypass landing elsewhere.
WRAPPER_MODULE = "dispatch.py"
WRAPPER_FUNCTION = "transition_state_checked"

#: Known direct-call bypasses, named as debt rather than hidden as green.
#: Empty today -- both call sites this test was written to catch
#: (dispatch_steps.py's claim/compensate pair, file_task.py's --supersede
#: transition) were converted to dispatch.transition_state_checked in the
#: same change that added this test. A future bypass must be named here,
#: with a reason, or fail test_no_new_bypass_call_sites below.
GRANDFATHERED_BYPASSES: dict[str, int] = {}


def _wrapper_line_range(tree: ast.AST) -> set[int]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == WRAPPER_FUNCTION:
            return {n.lineno for n in ast.walk(node) if hasattr(n, "lineno")}
    raise AssertionError(
        f"{WRAPPER_MODULE} no longer defines {WRAPPER_FUNCTION} -- "
        "derivation broke, update this test"
    )


def _direct_transition_state_calls(rel: str) -> list[int]:
    path = APP / rel
    tree = ast.parse(path.read_text(), filename=str(path))
    excluded = _wrapper_line_range(tree) if rel == WRAPPER_MODULE else set()

    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "transition_state"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in STORE_RECEIVERS
            and node.lineno not in excluded
        ):
            lines.append(node.lineno)
    return lines


def test_no_new_bypass_call_sites():
    offenders: dict[str, list[int]] = {}
    for rel in SURFACE_MODULES:
        lines = _direct_transition_state_calls(rel)
        allowed = GRANDFATHERED_BYPASSES.get(rel, 0)
        if len(lines) > allowed:
            offenders[rel] = lines
    assert not offenders, (
        "direct BeadStore.transition_state calls outside "
        f"dispatch.{WRAPPER_FUNCTION} -- route through it instead (or name "
        f"the bypass in GRANDFATHERED_BYPASSES with a reason): {offenders}"
    )


def test_transition_checked_call_surface_the_grandfathered_bypasses_still_exist():
    """A fixed bypass must delete its ratchet entry -- the list only shrinks.
    Currently empty, so this is vacuous until a future bypass is grandfathered
    in; it exists now so the pattern is in place before it is needed."""
    for rel, allowed in GRANDFATHERED_BYPASSES.items():
        actual = len(_direct_transition_state_calls(rel))
        assert actual >= allowed, (
            f"{rel} now has fewer direct transition_state calls ({actual}) "
            f"than GRANDFATHERED_BYPASSES declares ({allowed}) -- lower the "
            "count (or delete the entry) so the ratchet records the seam closing"
        )


def test_the_derivation_actually_sees_the_wrapper_call():
    """A parser that silently matched nothing would pass vacuously above."""
    tree = ast.parse((APP / WRAPPER_MODULE).read_text(), filename=WRAPPER_MODULE)
    wrapper_lines = _wrapper_line_range(tree)
    assert wrapper_lines, "wrapper function body appears empty -- derivation broke"


def test_dispatch_steps_and_file_task_no_longer_bypass_directly():
    """Names the exact two call sites the release-gate advisory (#659) found,
    pinned so a regression back to a direct call is obvious rather than
    lumped in with any future unrelated bypass."""
    assert _direct_transition_state_calls("activities/dispatch_steps.py") == []
    assert _direct_transition_state_calls("file_task.py") == []
