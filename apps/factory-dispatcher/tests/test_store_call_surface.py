"""The dispatcher's store call surface is derived, not maintained.

Release-gate finding on #635: a hand-kept REQUIRED tuple covers the surface
only until the next `sub.something()` lands without a tuple update — the exact
silent half-seam Amendment 35's costing depends on not having. This test
parses the dispatcher-side modules with ``ast`` and collects every method
invoked on a store receiver, then asserts that set is a subset of the
``BeadStore`` protocol. A new store call on the dispatcher's surface fails
here until the protocol (and therefore every implementation, loudly including
BdStore's declared refusals) answers it.

Receiver convention, stated rather than guessed: dispatcher code reaches the
store through parameters and locals named ``sub`` or ``store`` (verified
against every current call site). A store call smuggled through another name
defeats this test — which is why the companion protocol tests keep the exact
surface pinned from the other side.

No substrate, no network: pure source analysis.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from beadstore import BeadStore  # noqa: E402

#: Dispatcher-side modules that hold a store and call through it.
SURFACE_MODULES = (
    "dispatch.py",
    "file_task.py",
    "file_finding.py",
    "scanner.py",
    "activities/dispatch_steps.py",
    "workflow_core.py",
    "schedule_status.py",
    "queue_order.py",
)

#: Names dispatcher code binds a BeadStore to.
STORE_RECEIVERS = {"sub", "store"}


def _store_calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in STORE_RECEIVERS
        ):
            calls.add(node.func.attr)
    return calls


def _protocol_surface() -> set[str]:
    return {
        n
        for n in dir(BeadStore)
        if not n.startswith("_") and callable(getattr(BeadStore, n, None))
    }


#: Known protocol bypasses, named as debt rather than hidden as green.
#: scanner's filing POST reaches Substrate's private ``_request`` instead of
#: the protocol's ``create_task`` — chartered for removal by R26.06/O-1
#: ("'everything' includes intake"). file_task.py's own create path closed
#: this seam under M8 (docs/audits/2026-09-12-architecture-review-modularity-and-contracts.md
#: §6): it now calls ``sub.create_task`` like every other protocol consumer,
#: so its entry is gone rather than left to rot as a stale allowance. The
#: ratchet runs both ways: a NEW bypass fails the first assertion; fixing one
#: of these without deleting its entry fails the second, so the list can only
#: shrink.
GRANDFATHERED_BYPASSES: dict[str, set[str]] = {
    "scanner.py": {"_request"},
}


def test_every_store_call_in_the_dispatcher_is_on_the_protocol():
    surface = _protocol_surface()
    offenders: dict[str, set[str]] = {}
    for rel in SURFACE_MODULES:
        calls = _store_calls(APP / rel)
        extra = calls - surface - GRANDFATHERED_BYPASSES.get(rel, set())
        if extra:
            offenders[rel] = extra
    assert not offenders, (
        "store calls outside the BeadStore protocol — widen the protocol "
        f"(and every implementation, refusals included) first: {offenders}"
    )


def test_store_call_surface_the_grandfathered_bypasses_still_exist():
    """A fixed bypass must delete its ratchet entry — the list only shrinks."""
    for rel, names in GRANDFATHERED_BYPASSES.items():
        calls = _store_calls(APP / rel)
        gone = names - calls
        assert not gone, (
            f"{rel} no longer calls {sorted(gone)} — delete the entry from "
            "GRANDFATHERED_BYPASSES so the ratchet records the seam closing"
        )


def test_the_derivation_actually_sees_the_call_sites():
    """A parser that silently matched nothing would pass vacuously above."""
    seen = set()
    for rel in SURFACE_MODULES:
        seen |= _store_calls(APP / rel)
    assert {"list_tasks", "list_notes", "list_links", "list_beads"} <= seen, (
        f"derivation implausibly sparse — receiver convention drifted? saw: {sorted(seen)}"
    )


def test_every_dispatcher_call_site_is_exercised_in_the_shared_suite():
    """Being on the protocol is not the same as being tested. ``list_beads``
    sat on this derived surface — the dispatcher's release-state gate has
    called it since #635 — while tests/test_store_contract.py (the shared
    suite PC-SUB-004/PC-SUB-003 stood up) never once invoked it: covered by
    inspection (the protocol declares it, both implementations define it) but
    not by the suite meant to hold both to the same contract for it.

    This walks the same call-site enumeration as
    ``test_every_store_call_in_the_dispatcher_is_on_the_protocol`` above, but
    asks a stricter question: not just "is this name on the protocol?" but
    "does the shared suite actually call it?" — established by parsing real
    call sites, not by asking whether someone remembered to add a test.
    """
    surface: set[str] = set()
    for rel in SURFACE_MODULES:
        surface |= _store_calls(APP / rel)
    bypassed = {name for names in GRANDFATHERED_BYPASSES.values() for name in names}
    called_methods = (surface & _protocol_surface()) - bypassed

    contract_source = (Path(__file__).parent / "test_store_contract.py").read_text()
    uncovered = {
        name for name in called_methods if f".{name}(" not in contract_source
    }
    assert not uncovered, (
        "the dispatcher calls these store methods but tests/test_store_contract.py "
        f"never invokes them: {sorted(uncovered)}"
    )
