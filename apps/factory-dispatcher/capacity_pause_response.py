"""Turn a probed capacity pause into action: resume-or-wait, never silent continuation.

Since #627, capacity backpressure exhausting the usage window pauses the dispatch schedule with
the `schedule_runtime.CAPACITY_PAUSE_NOTE_PREFIX` note and raises a declared alert
(`failure_diagnosis.announce_capacity_pause`). The resume half never existed: the live schedule
note as of this writing reads "Unpaused by outer loop at the Operator's direction 2026-09-03: probing
whether the usage window reset" -- a person guessing at a clock, on their own time, with no
mechanism helping them. Now that capacity is the factory's real governor (both loops share one
subscription), every hour between the window actually resetting and a human noticing is idle
capacity paid for.

This module is the missing action half, in the same shape as `worker_checkout_drift_response.py`:
given a schedule's current pause note and a way to probe whether the usage window has
demonstrably reset, `respond_to_capacity_pause` decides and executes exactly one of three
outcomes -- never a fourth "log it and move on":

  * NOT_OURS -- the schedule's note does not carry the capacity-pause prefix. This is either an
    operator's own manual pause or no pause at all, and this mechanism never touches either one.
    The OPS-52 lesson generalized: a schedule's pause identity is carried in its own note field,
    checked directly, never inferred from timing or any other side channel.
  * STILL_EXHAUSTED -- our pause, but the probe did not confirm the usage window has reset. This
    covers both "confirmed still exhausted" and "the probe itself could not evaluate the
    question" (it raised, timed out, or produced something unclassifiable) -- PRIN-015's fail-
    closed shape: a control that cannot evaluate its question must never resolve toward the
    action it cannot justify.
  * RESUMED -- our pause, and the probe demonstrated the window has reset (an actual attempt
    that did not hit the capacity failure again -- never a parsed guess at a provider's own
    free-text retry clock, which is exactly the guess this task exists to replace). The schedule
    is resumed with a note naming this mechanism and the probe's evidence, and the resume is
    announced through a declared alert.

Every collaborator this needs -- how to probe the usage window, how to resume the schedule, how
to announce the resume -- is injected, so the decision logic here is testable with no Temporal
server, no substrate, no network, and no real worker invocation. Production wiring (the real
Temporal read/update and the real live-worker probe) lives in
`activities/capacity_pause_response.py`, the one place that already runs on the schedule. See
`.factory/design.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import schedule_runtime

#: The only three outcomes `respond_to_capacity_pause` may return -- deliberately not an
#: open-ended string, so a caller (and a test) can assert "one of these three" rather than trust
#: free text never to grow a silent fourth meaning.
NOT_OURS = "not_ours"
STILL_EXHAUSTED = "still_exhausted"
RESUMED = "resumed"

#: Written into the schedule's note on RESUMED, naming both the mechanism and (appended by the
#: caller) the probe's own evidence -- an operator reading the note later must never have to take
#: "it just started running again" on faith.
RESUME_NOTE_PREFIX = "factory dispatcher resumed: capacity-pause resume responder"


@dataclass(frozen=True)
class CapacityProbeResult:
    """Whether a live probe demonstrated the usage window has reset.

    `reset` is a tri-state, not a bool: `True` means the probe confirmed the window has reset
    (a live attempt completed with no capacity failure detected), `False` means the probe
    confirmed it has not, and `None` means the probe could not evaluate the question at all
    (it raised, timed out, or produced something unclassifiable). `respond_to_capacity_pause`
    treats `None` exactly like `False` -- never as a guess toward `RESUMED` -- but `evidence`
    must still say which of the two it was, since "still exhausted" and "could not tell" are
    different facts an operator needs told apart.
    """

    reset: bool | None
    evidence: str


#: `probe()` performs whatever real check demonstrates (or fails to demonstrate) that the usage
#: window has reset. Takes no arguments and returns no partial state: this module never inspects
#: *how* the probe reached its answer, only the answer itself.
CapacityWindowProbe = Callable[[], CapacityProbeResult]
#: Resumes the paused schedule with the given note. Takes the note, not a bare "resume" signal,
#: so the caller decides what evidence to preserve without this module reaching back into it.
ResumeSchedule = Callable[[str], None]
#: Announces the resume through the declared alert. Takes the note actually written to the
#: schedule, so the alert and the schedule agree on what happened.
AnnounceResume = Callable[[str], None]


@dataclass(frozen=True)
class CapacityPauseResponse:
    outcome: str
    detail: str


def respond_to_capacity_pause(
    schedule_note: str,
    *,
    probe: CapacityWindowProbe,
    resume_schedule: ResumeSchedule,
    announce_resume: AnnounceResume,
) -> CapacityPauseResponse:
    """Resume-or-wait on a schedule's current pause note; never touch a pause this never made.

    `schedule_note` is read directly off the paused schedule -- see
    `activities/capacity_pause_response.py` for where that read happens. Only a note carrying
    `schedule_runtime.CAPACITY_PAUSE_NOTE_PREFIX` is ever eligible for the probe below; every
    other note (an operator's own pause, or no note at all) is `NOT_OURS`, unconditionally, with
    `probe`, `resume_schedule`, and `announce_resume` all left uncalled.
    """
    if not (schedule_note or "").startswith(schedule_runtime.CAPACITY_PAUSE_NOTE_PREFIX):
        return CapacityPauseResponse(
            NOT_OURS,
            "schedule note does not carry the capacity-pause prefix "
            f"({schedule_runtime.CAPACITY_PAUSE_NOTE_PREFIX!r}); this is an operator's own pause "
            f"(or no pause at all) and is never resumed by this mechanism. note={schedule_note!r}",
        )

    result = probe()
    if not result.reset:
        if result.reset is None:
            headline = "probe could not evaluate whether the usage window has reset"
        else:
            headline = "probe found the usage window has not reset"
        return CapacityPauseResponse(STILL_EXHAUSTED, f"{headline}: {result.evidence}")

    note = f"{RESUME_NOTE_PREFIX}; {result.evidence}"
    resume_schedule(note)
    announce_resume(note)
    return CapacityPauseResponse(RESUMED, note)
