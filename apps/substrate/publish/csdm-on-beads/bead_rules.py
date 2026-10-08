"""The bead state machines and edge vocabulary — no FastAPI, no database.

Extracted from this platform's substrate (``apps/substrate/src/bead_rules.py``),
minus the one section that stayed behind: the writer-enrollment map for
*who* may claim a given ``source_class`` on a bead. That map names this
platform's own operational writer identities (its dispatcher's automated
agents) and is a policy about a specific deployment, not part of the edge
vocabulary or the state machines themselves — a stranger standing up this
model against their own repository has no use for identities that only
exist in ours, and would need to declare their own regardless.
"""

# ---------------------------------------------------------------------------
# State machines — the legal edges a bead of a given (namespace, type) may
# walk.
#
# Declared per (namespace, type). A pair with no entry here stays permissive
# — an undeclared machine must not start rejecting writes, so a caller may
# add new types without touching this registry until that type's lifecycle
# actually needs enforcing.
# ---------------------------------------------------------------------------

STATE_MACHINES: dict[tuple[str, str], dict[str, frozenset]] = {
    # A work item's lifecycle: filed, worked, reviewed, and either landed or
    # sent back. "superseded" is a second terminal state alongside
    # "archived" for a re-filed item whose predecessor never shipped.
    ("dev", "task"): {
        "pending": frozenset({"doing", "superseded"}),
        "doing": frozenset({"review", "pending", "failed"}),
        "review": frozenset({"done", "pending", "failed"}),
        "done": frozenset({"archived"}),
        "failed": frozenset({"pending", "superseded"}),
        "archived": frozenset(),
        "superseded": frozenset(),
    },
    # A release is the objective a body of work is aimed at. "closing" is a
    # distinct state from "released" because generating the notes is where
    # an outcome is found undelivered — the way back to "in_flight" is the
    # honest response to that, rather than shipping notes that overclaim.
    ("arch", "release"): {
        "planned": frozenset({"in_flight", "abandoned"}),
        "in_flight": frozenset({"closing", "abandoned"}),
        "closing": frozenset({"released", "in_flight"}),
        "released": frozenset(),
        "abandoned": frozenset(),
    },
    # A production incident. "closed" is reachable directly from "detected"
    # (spurious/duplicate) and from "resolved" (the normal close), but never
    # directly from "mitigating" — mitigation ends by declaring the incident
    # resolved, never by skipping straight to closed mid-fix. "detected" is
    # reachable again from "mitigating" or "resolved" as the reopen edge on
    # recurrence within a declared window.
    ("arch", "incident"): {
        "detected": frozenset({"mitigating", "closed"}),
        "mitigating": frozenset({"resolved", "detected"}),
        "resolved": frozenset({"closed", "detected"}),
        "closed": frozenset(),
    },
    # A defect, enhancement or security issue raised at a review gate. Four
    # terminal states, one edge each from "pending", no intermediate triage
    # state: "backlogged" and "ruled" are the two ways a finding absorbs
    # into future work (a backlog item, or a rule that prevents recurrence);
    # "already_fixed" and "not_a_defect" are the two ways it closes without
    # absorbing into either.
    ("dev", "finding"): {
        "pending": frozenset(
            {"backlogged", "ruled", "already_fixed", "not_a_defect"}
        ),
        "backlogged": frozenset(),
        "ruled": frozenset(),
        "already_fixed": frozenset(),
        "not_a_defect": frozenset(),
    },
    # An accepted risk. Entered "accepted" only -- a register of risks
    # already accepted, not a proposal queue. "under_review" -> "accepted"
    # is the renewal edge, filed with a fresh review date; "under_review" ->
    # "retired" closes a risk that no longer applies. "retired" is terminal.
    ("arch", "risk"): {
        "accepted": frozenset({"under_review"}),
        "under_review": frozenset({"retired", "accepted"}),
        "retired": frozenset(),
    },
    # A release verdict — the record of what admitted a merged change. It is
    # born "verdict_recorded" and that state has no edges out: a verdict is a
    # fact about a merge that already happened, not a lifecycle.
    ("dev", "release"): {
        "verdict_recorded": frozenset(),
    },
    # A release's governed health posture. Entered "unmeasured" only --
    # nothing may create one already carrying a posture before the first
    # measurement ran. "accepted" is reachable only from "drifting" and
    # "breached" -- acceptance is a disposition over an existing problem,
    # not a starting posture. "closed" is terminal: a closed release's
    # last-known health is not revised in place.
    ("arch", "release_health"): {
        "unmeasured": frozenset({"on_track", "drifting", "breached", "closed"}),
        "on_track": frozenset({"drifting", "breached", "unmeasured", "closed"}),
        "drifting": frozenset({"on_track", "breached", "accepted", "unmeasured", "closed"}),
        "breached": frozenset({"on_track", "drifting", "accepted", "unmeasured", "closed"}),
        "accepted": frozenset({"on_track", "drifting", "breached", "unmeasured", "closed"}),
        "closed": frozenset(),
    },
}

# States a bead of a declared machine may be *created* in. Creation has no
# from_state to check an edge against, so entry states are declared
# separately from the edges above.
STATE_MACHINE_ENTRY_STATES: dict[tuple[str, str], frozenset[str]] = {
    ("dev", "task"): frozenset({"pending"}),
    # A release is born planned. Creating one already "released" would let a
    # loader assert the outcome of work that has not happened.
    ("arch", "release"): frozenset({"planned"}),
    # An incident is born detected only — nothing may create one already
    # mitigating, resolved, or closed, which would assert a response
    # happened before the record of what triggered it exists.
    ("arch", "incident"): frozenset({"detected"}),
    # A finding is born pending only.
    ("dev", "finding"): frozenset({"pending"}),
    # A risk is born accepted only.
    ("arch", "risk"): frozenset({"accepted"}),
    # A release verdict is born recorded and stays there.
    ("dev", "release"): frozenset({"verdict_recorded"}),
    # A release-health record is born unmeasured only.
    ("arch", "release_health"): frozenset({"unmeasured"}),
}


# The closed edge vocabulary every link between two beads is admitted
# against. `link_type` is otherwise a free string with a uniqueness
# constraint on (source, target, link_type) — nothing else stops a typo
# forking the vocabulary.
BEAD_LINK_TYPES = frozenset(
    {
        "designs",
        "supersedes",
        "gates",
        "regresses",
        "found_by",
        "affects",
        "measures",
        "supports",
        "realizes",
        "depends_on",
        "consumes",
        "applies",
        "derived_from",
        "enforced_by",
        "caused_by",
        "resolved_by",
        "delivers",
        "threatens",
        "accepted_by",
    }
)


__all__ = [
    "STATE_MACHINES",
    "STATE_MACHINE_ENTRY_STATES",
    "BEAD_LINK_TYPES",
]
