"""The parity guard proves the two entry points agree on order, not on body.

test_dispatch_step_parity.py proves the CLI (dispatch.dispatch_once) and the
scheduled drain (workflows.dispatch_task.DispatchTaskWorkflow) run the same
step NAMES in the same order. It cannot see whether a step name is backed by
ONE implementation or TWO: the release gate's review of #636 found that the
"propose" step still had two bodies -- dispatch.py's inline implementation and
activities.dispatch_steps.propose_activity -- and a capability added to one
was invisible to the other, because both sequences still said "propose". The
sequence-only guard passed throughout.

This test closes that gap by asserting object identity, not name equality:
for every step name workflow_core.DISPATCH_STEPS says both entry points run,
the CLI's step table (activities.dispatch_steps.ACTIVITY_FUNCTIONS, which
dispatch.dispatch_once resolves every step through -- see dispatch.py's
`execute()` closure) and the activity layer's step table (activities.ACTIVITIES,
the exact list worker.py hands the real Temporal Worker) must resolve the
step to the SAME function object. A step whose two entries resolve to
different callables fails with both locations named.

These two tables are the right ones to compare because they are the real
bindings, not documentation of them: dispatch.py's `execute()` closure
resolves every step through ACTIVITY_FUNCTIONS, and worker.py registers
exactly ACTIVITIES with the Temporal Worker. They are identical by
construction today -- this test makes that fact assertable, so the next
duplicate (the #636 defect class) fails here instead of needing a human
reviewer to notice it again.

No substrate, no network, no Temporal server: activities.ACTIVITIES is a
plain in-process import, and every comparison below is a pure object-identity
check.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import activities  # noqa: E402
import workflow_core  # noqa: E402
from activities import dispatch_steps  # noqa: E402


def _unwrap(fn):
    """Peel back any wrapper to the callable actually invoked.

    `@activity.defn` (temporalio>=1.28,<2, the version this repo pins) mutates
    the decorated function in place and returns it unchanged -- verified
    against `temporalio.activity.defn`'s own source, which calls
    `_Definition._apply_to_callable(fn, ...)` and then `return fn`. So this is
    a no-op in practice. It stays defensive anyway: a future SDK version, or a
    local decorator, that *does* introduce a proxy must not silently defeat
    this test by comparing two distinct wrapper objects instead of the shared
    target underneath (the acceptance criteria's own "via __wrapped__ or the
    activity's fn attribute" language).
    """
    seen: set[int] = set()
    while id(fn) not in seen:
        seen.add(id(fn))
        inner = getattr(fn, "__wrapped__", None)
        if inner is None:
            inner = getattr(fn, "fn", None)
        if inner is None or not callable(inner):
            return fn
        fn = inner
    return fn


def _location(fn) -> str:
    module = getattr(fn, "__module__", "?")
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    return f"{module}.{qualname}"


def _temporal_registered_step_bodies() -> dict[str, object]:
    """name -> callable, for every dispatch step registered with the real
    Temporal Worker (worker.py: Worker(activities=activities.ACTIVITIES, ...)).

    Keyed by the same "{step}_activity" -> "{step}" derivation
    activities.dispatch_steps.ACTIVITY_FUNCTIONS already uses for its own
    dict -- not a second, hand-maintained name mapping.
    """
    return {fn.__name__.removesuffix("_activity"): fn for fn in activities.ACTIVITIES}


#: Steps legitimately exempt from the shared-body requirement. None known
#: today. Shrink-only, ratchet-style, like test_store_call_surface.py's
#: GRANDFATHERED_BYPASSES: an entry here must name why, and
#: test_the_step_body_identity_exemptions_are_still_needed fails the moment
#: that reason stops being true, so the list can only shrink.
STEP_BODY_IDENTITY_EXEMPTIONS: dict[str, str] = {}


def test_every_shared_step_resolves_to_one_body_in_both_entry_points():
    cli_table = dispatch_steps.ACTIVITY_FUNCTIONS
    activity_table = _temporal_registered_step_bodies()

    # Every DISPATCH_STEPS name must exist on both sides before identity is
    # even a question -- these two assertions keep their precise messages.
    for step in workflow_core.DISPATCH_STEPS:
        assert step in cli_table, (
            f"step {step!r} has no CLI-side body in "
            "dispatch_steps.ACTIVITY_FUNCTIONS -- dispatch.dispatch_once "
            "cannot run it"
        )
        assert step in activity_table, (
            f"step {step!r} is not registered with the Temporal worker "
            "(activities.ACTIVITIES) -- the scheduled drain cannot run it"
        )

    # Identity is asserted over every name BOTH tables carry -- derived from
    # the tables themselves, not from DISPATCH_STEPS. The release gate on the
    # PR that landed this proved the narrower loop blind by mutation: a
    # duplicate cleanup_activity sailed through while the same mutation on
    # propose was caught, because record_failure/cleanup/reconcile run
    # through the same shared driver (workflow_core) but outside
    # DISPATCH_STEPS. The intersection covers them and every future step
    # either table gains.
    offenders: dict[str, str] = {}
    for step in sorted(set(cli_table) & set(activity_table)):
        if step in STEP_BODY_IDENTITY_EXEMPTIONS:
            continue
        cli_fn = cli_table[step]
        activity_fn = activity_table[step]
        if _unwrap(cli_fn) is not _unwrap(activity_fn):
            offenders[step] = (
                f"CLI resolves {step!r} to {_location(cli_fn)}; the activity "
                f"layer resolves it to {_location(activity_fn)}"
            )

    assert not offenders, (
        "step(s) with two DIFFERENT implementations behind one name -- the "
        "propose-duplicate defect class (#636), reproduced:\n"
        + "\n".join(f"  {step}: {reason}" for step, reason in offenders.items())
    )


def test_the_step_body_identity_exemptions_are_still_needed():
    """A fixed exemption must delete its entry -- the list only shrinks."""
    cli_table = dispatch_steps.ACTIVITY_FUNCTIONS
    activity_table = _temporal_registered_step_bodies()
    healed = [
        step
        for step in STEP_BODY_IDENTITY_EXEMPTIONS
        if _unwrap(cli_table.get(step)) is _unwrap(activity_table.get(step))
    ]
    assert not healed, (
        f"{healed} now resolve to one shared body -- delete the entry from "
        "STEP_BODY_IDENTITY_EXEMPTIONS so the ratchet records the seam closing"
    )


def test_the_derivation_actually_sees_dispatch_steps():
    """A lookup that silently matched nothing would pass vacuously above."""
    activity_table = _temporal_registered_step_bodies()
    assert {"claim", "propose", "verify"} <= activity_table.keys(), (
        "derivation implausibly sparse -- registration convention drifted? "
        f"saw: {sorted(activity_table)}"
    )
    assert set(workflow_core.DISPATCH_STEPS) <= activity_table.keys(), (
        "workflow_core.DISPATCH_STEPS names a step the activity layer never "
        f"registers: {set(workflow_core.DISPATCH_STEPS) - activity_table.keys()}"
    )
    # Plausibility floor, not a parallel implementation list: the failure-path
    # steps the shared driver invokes outside DISPATCH_STEPS
    # (workflow_core.py: record_failure, cleanup, reconcile) must be inside
    # the derived intersection, or the identity loop above has silently
    # stopped guarding the very defect class (#636 on a failure path) the
    # release gate demonstrated against the narrower version of this test.
    shared = dispatch_steps.ACTIVITY_FUNCTIONS.keys() & activity_table.keys()
    assert {"record_failure", "cleanup", "reconcile"} <= shared, (
        "failure-path steps fell out of the shared table intersection -- "
        f"identity is no longer asserted for them; shared={sorted(shared)}"
    )
