"""The bead state machines and edge vocabulary — no FastAPI, no database.

These are rules the platform depends on, not implementation details of the
substrate web app: the factory dispatcher, its operators, and any other
caller need the same seven-state ``dev.task`` machine and the same
nineteen-type closed edge vocabulary that ``routes.py`` and ``schemas.py``
enforce. Importing this module must never pull in FastAPI or the database
layer, so anything that only needs the rules — not the HTTP surface built on
top of them — can depend on this module alone.
"""

# ---------------------------------------------------------------------------
# State machines — the legal edges named in ARCHITECTURE.md §3.1.
#
# Declared per (namespace, type). A pair with no entry here stays permissive:
# this endpoint is additive and finance.* has its own state vocabulary that
# predates it, so an undeclared machine must not start rejecting writes.
#
# dev.task's edges are the full lifecycle the factory dispatcher and its
# operators actually walk, including the failure and recovery paths observed
# live on 2026-08-20: the dispatcher settles pending->doing->failed, and an
# operator recovers failed->pending via /transition. review carries the same
# two exits (review->pending to send work back, review->failed to give up)
# so a stalled review is never a dead end.
#
# "superseded" is a second terminal state alongside "archived", added because
# a re-filed task otherwise had no way to say a predecessor bead is dead
# without shipping — the fact lived only in a dev.note, while the board and
# every open-population reader kept reading state, which still said "pending"
# forever. Reachable from "pending" and "failed" only: those are the two
# states a dead-by-supersession bead is actually observed in (a bead already
# "doing" or in "review" is live work, not dead work, and gets no edge here).
# No edges out: once a bead is known dead, nothing un-supersedes it in place —
# a fresh filing is a new bead.
# ---------------------------------------------------------------------------

STATE_MACHINES: dict[tuple[str, str], dict[str, frozenset]] = {
    ("dev", "task"): {
        "pending": frozenset({"doing", "superseded"}),
        "doing": frozenset({"review", "pending", "failed"}),
        "review": frozenset({"done", "pending", "failed"}),
        "done": frozenset({"archived"}),
        "failed": frozenset({"pending", "superseded"}),
        "archived": frozenset(),
        "superseded": frozenset(),
    },
    # A release is the objective a body of work is aimed at, and its lifecycle
    # is the one thing about it the substrate owns outright: the charter under
    # docs/releases/ stays authoritative for structure (PQ-4, the Operator 2026-08-22),
    # but nothing in git may declare a release "released". That claim is only
    # true once the work landed, so it is reached by a governed transition and
    # never by a loader.
    #
    # "closing" exists as a distinct state, rather than going straight to
    # "released", because generating the notes is where an outcome is found
    # undelivered. The way back to "in_flight" is the honest response to that;
    # without it the only options would be to ship notes that overclaim or to
    # abandon a release that is merely late.
    ("arch", "release"): {
        "planned": frozenset({"in_flight", "abandoned"}),
        "in_flight": frozenset({"closing", "abandoned"}),
        "closing": frozenset({"released", "in_flight"}),
        "released": frozenset(),
        "abandoned": frozenset(),
    },
    # arch.incident — machine three in this lifted registry (R26.06/O-2), the
    # 2026-09-03 decision record's standing proposal
    # (docs/plans/2026-09-03-decision-record-state-driven-itsm.md D3,
    # docs/architecture/itsm-target-state.md §1). Registering it here is the
    # whole change: routes.py's STATE_MACHINES.get((namespace, type)) consults
    # are generic, so a third entry needs no route code.
    #
    # "closed" is reachable directly from "detected" (spurious/duplicate) and
    # from "resolved" (the normal close) and from "mitigating" is NOT allowed
    # directly to "closed" — mitigation ends by declaring the incident
    # resolved, never by skipping straight to closed while still mid-fix.
    # "detected" is reachable from "mitigating" and "resolved" as the reopen
    # edge on recurrence within a declared window; a later recurrence outside
    # that window is a new incident bead, not a reopen of this one.
    ("arch", "incident"): {
        "detected": frozenset({"mitigating", "closed"}),
        "mitigating": frozenset({"resolved", "detected"}),
        "resolved": frozenset({"closed", "detected"}),
        "closed": frozenset(),
    },
    # dev.finding — machine four (R26.05/O-10). Eight findings filed 2026-08-03
    # and 2026-08-06 sat in "pending" for 35 days because no declared machine
    # meant every writer, transition included, treated the pair as
    # permissive: with no entry here, both the compare-and-set
    # POST .../transition path and a bare PATCH accept any state string with
    # no legality check at all, so nothing could ever record a refused edge
    # or distinguish "moved correctly" from "moved because nothing was
    # watching".
    #
    # CLAUDE.md's absorption rule names exactly two outcomes for an absorbed
    # finding -- "either a backlog item with acceptance criteria or a rule
    # that prevents recurrence" -- plus two ways a finding closes without
    # absorbing into either: it was already fixed by the time it was picked
    # up, or investigation found it was not a defect at all. Four terminal
    # states, one edge each from "pending", no intermediate triage state:
    # nothing about the eight live findings or CLAUDE.md's rule distinguishes
    # "being triaged" from "filed, not yet dispositioned", which is what
    # "pending" already means for every other machine in this registry. A
    # triage state can be inserted later by a bead that actually needs one,
    # without touching these four terminals or invalidating anything already
    # resolved through them.
    ("dev", "finding"): {
        "pending": frozenset(
            {"backlogged", "ruled", "already_fixed", "not_a_defect"}
        ),
        "backlogged": frozenset(),
        "ruled": frozenset(),
        "already_fixed": frozenset(),
        "not_a_defect": frozenset(),
    },
    # arch.risk — machine five (R26.09/O-5), the risk register this platform's
    # accepted risks (the single-node SPOF, the mutable :latest deploy path,
    # the single-host factory) currently live in amendment prose with no
    # carrier. Entered "accepted" only: this is a register of risks already
    # accepted, not a proposal queue for risks under consideration.
    # "under_review" -> "accepted" is the renewal edge, filed with a fresh
    # review_by; "under_review" -> "retired" closes a risk that no longer
    # applies. "retired" is terminal — nothing un-retires a risk in place,
    # the same "a fresh filing is a new bead" rule dev.task's "superseded"
    # follows.
    ("arch", "risk"): {
        "accepted": frozenset({"under_review"}),
        "under_review": frozenset({"retired", "accepted"}),
        "retired": frozenset(),
    },
    # dev.release — machine six (R26.12/B20, O-8,
    # docs/plans/2026-09-23-design-r2612-steerable-and-legible.md). A verdict
    # record is a fact about a merge that already happened, not a lifecycle:
    # it is born "verdict_recorded" and that state has no edges out, so
    # nothing may move it once filed. Registering a zero-edge machine (rather
    # than leaving the pair unregistered, which stays permissive) is what
    # lets a later write be refused for trying to mutate a verdict in place.
    #
    # This is the first step toward closing dev.finding 36037d55 (F-A): the
    # release-health "merged_without_verdict" signal needs a dev.release
    # record to match a merged change's pr_url/pr_number against, and today
    # none exists for any change. B21 is the writer that mints one per merged
    # PR; this bead only gives that record a type and a machine to be born
    # into.
    ("dev", "release"): {
        "verdict_recorded": frozenset(),
    },
    # arch.release_health — machine seven (R26.12/B11, O-5,
    # docs/plans/2026-09-23-design-r2612-steerable-and-legible.md,
    # dev.finding 934989e6 F-B: the nightly reporter mints dated observations
    # nobody resolves; this object is what replaces them). Entered
    # "unmeasured" only: nothing may create one already carrying a posture
    # before the first measurement ran.
    #
    # "accepted" is reachable only from "drifting" and "breached" — never
    # from "unmeasured" or "on_track" — because acceptance is an operator's
    # disposition over an existing problem, not a starting posture. Every
    # non-terminal state can fall back to "unmeasured" (a stale or unreadable
    # measurement) and every non-terminal state can reach "closed" directly
    # (the release itself closed or was abandoned while health sat wherever
    # it was). "closed" is terminal: a closed release's last-known health is
    # not revised in place.
    #
    # This bead registers the type and the machine only (B11); B12 computes
    # the content, B13 (the existing factory-dispatcher/release-status
    # identity, already enrolled for "derived" above) is the writer that
    # moves beads through it.
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
# separately from the edges above — the only state dev.task beads are born
# into is pending; every other state is reached by a governed transition.
STATE_MACHINE_ENTRY_STATES: dict[tuple[str, str], frozenset[str]] = {
    ("dev", "task"): frozenset({"pending"}),
    # A release is born planned. Creating one already "released" would let a
    # loader assert the outcome of work that has not happened.
    ("arch", "release"): frozenset({"planned"}),
    # An incident is born detected only — nothing may create one already
    # mitigating, resolved, or closed, which would assert a response
    # happened before the record of what triggered it exists.
    ("arch", "incident"): frozenset({"detected"}),
    # A finding is born pending only -- the state the eight live findings
    # this machine was written for are already in, so registering the
    # machine requires no migration of them (entry states gate creation,
    # never existing rows).
    ("dev", "finding"): frozenset({"pending"}),
    # A risk is born accepted only -- this is a register of risks already
    # accepted, never a queue for ones still being weighed.
    ("arch", "risk"): frozenset({"accepted"}),
    # A release verdict is born recorded and stays there — see the machine
    # comment above.
    ("dev", "release"): frozenset({"verdict_recorded"}),
    # A release-health record is born unmeasured only -- see the machine
    # comment above (R26.12/B11, dev.finding 934989e6).
    ("arch", "release_health"): frozenset({"unmeasured"}),
}


# The closed edge vocabulary every ``bead_link`` row is admitted against —
# see ``schemas.BeadLinkCreate.normalize_link_type``. Sixteen were decided
# for B-110; ``delivers`` is the seventeenth, admitted 2026-08-25 with
# arch.release; ``threatens`` and ``accepted_by`` are the eighteenth and
# nineteenth, admitted 2026-09-13 with arch.risk (R26.09/O-5).
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


# ---------------------------------------------------------------------------
# Writer -> source_class enrollment (OPS-86, R26.06/O-2) — the fifth member
# of this lifted-rules family, beside the machines and edge vocabulary above.
#
# check_source_class_ownership (schemas.py) already governs who may
# OVERWRITE a fact that already carries a source_class: the exact identity
# that declared itself the deriver/observer, or (for "authored") anyone who
# isn't an automated writer. OPS-78 closed the gap where that rule fired on
# PATCH but not on POST for a ref an existing bead already owns. It left a
# second gap open: a POST naming a brand-new ref (or no ref at all) has no
# existing bead to check ownership against, so any writer could mint a fresh
# "observed" or "derived" fact by simply declaring the class in content — no
# rule said who may CLAIM a class in the first place, only who may keep
# overwriting one once claimed. This map is that rule.
#
# OPS-90 then closed the mirror of that gap: because ownership evaluates the
# class a bead ALREADY carries, a writer could mint a fresh "authored" fact
# (admitted by HUMAN_EXEMPTION) and then relabel it to "observed" in a second
# request, which ownership reads as a permitted authored overwrite. So a write
# that changes a fact's resolved class re-runs this admission check, from both
# the POST and PATCH paths — the same rule and the same map, reached through a
# third call site rather than copied.
#
# Placement is a decision, not a default: NOT config, because a table that
# decides who may mint a governed epistemic class must be reviewed by a
# human via PR, and config is exactly the surface edited without that gate.
# NOT keyed by trust_tier, because trust_tier says how much a writer is
# trusted platform-wide, not which class it is entitled to assert about a
# specific fact -- the two are independent, and raising a writer's trust tier
# for an unrelated reason must never silently widen what it may claim.
#
# "observed" and "derived" are closed enrollments: exact identity match
# against the writers actually running in production as of 2026-09-08 (see
# ea_observation.py, worker_revision_drift.py, change_apply.py,
# scripts/requirements-load.py). A class whose enrollment is empty refuses
# every claimant -- including that class's own real writer -- until a PR adds
# one; there is no writer admitted by default.
#
# "authored" carries the human exemption instead of an enumerated allowlist.
# check_source_class_ownership already lets any writer that is not an
# automated (factory-agent) writer overwrite an authored fact; admission
# mirrors that exact rule rather than naming every human and script that
# will ever author an arch bead. HUMAN_EXEMPTION is a sentinel the schemas-
# side gate recognizes and evaluates against its own is_automated_writer,
# not a literal writer set -- bead_rules.py has no dependency on that check.
# ---------------------------------------------------------------------------


class _HumanExemption:
    """Sentinel enrollment: any writer that is not a recognized automated
    agent may claim this class. See the module-level comment above.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "HUMAN_EXEMPTION"


HUMAN_EXEMPTION = _HumanExemption()

SOURCE_CLASS_WRITERS: dict[str, object] = {
    "authored": HUMAN_EXEMPTION,
    "derived": frozenset(
        {
            # change_apply's arch.change reconciler.
            "factory-dispatcher/change-apply",
            # requirements-load's arch.requirement_conformance mirror.
            "requirements-load",
            # doctrine_staleness's arch.observation reconciler
            # (CREATED_BY at activities/doctrine_staleness.py:33).
            "factory-dispatcher/doctrine-staleness",
            # requirements_apply's arch.observation reconciler
            # (CREATED_BY at activities/requirements_apply.py:52).
            "factory-dispatcher/requirements-apply",
            # spec_record_reconcile's arch.observation reconciler
            # (CREATED_BY at activities/spec_record_reconcile.py:51).
            "factory-dispatcher/spec-record-reconcile",
            # staleness_report's arch.observation reconciler
            # (CREATED_BY at activities/staleness_report.py:26).
            "factory-dispatcher/staleness-report",
            # release_status's arch.observation reconciler
            # (CREATED_BY at activities/release_status.py:37).
            "factory-dispatcher/release-status",
            # ea_apply's arch.observation reconciler
            # (CREATED_BY at activities/ea_apply.py:95).
            "factory-dispatcher/ea-apply",
            # knowledge_ingestion's land_knowledge_principles: mirrors a
            # prior, PR-reviewed extraction task's candidates.json straight
            # into arch.principle content, no synthesis in this activity
            # (CREATED_BY at activities/knowledge_ingestion.py:42). See
            # .factory/design.md for why "derived", not "authored" or
            # "observed".
            "factory-dispatcher/knowledge-ingestion",
        }
    ),
    "observed": frozenset(
        {
            # The EA observation path's arch.observation/arch.ci writes.
            "factory-dispatcher/ea-observer",
            # worker_revision_drift's fresh-ref arch.observation writes.
            "factory-dispatcher/worker-revision-drift",
            # doctrine_registry_view's standing obs.principles-check-view
            # write (doctrine_registry_view.py:62 at filing).
            "factory-dispatcher/doctrine-registry-view",
            # deployed_revision_drift's fresh-ref arch.observation write
            # (CREATED_BY at deployed_revision_drift.py:47 on main c729c15;
            # :75 on the preserved head 58eb4e0 the enrolment was filed from) --
            # OPS-109's next attempt declares source_class "observed" using
            # this identity.
            "factory-dispatcher/deployed-revision-drift",
            # probe_console_surface's standing obs.probe.console-surface
            # write (CREATED_BY at probe_console_surface.py, R26.09/O-7) --
            # a dated comparison of what the console claims against what the
            # store and Temporal say, not a derivation from either.
            "factory-dispatcher/probe-console-surface",
        }
    ),
}


__all__ = [
    "STATE_MACHINES",
    "STATE_MACHINE_ENTRY_STATES",
    "BEAD_LINK_TYPES",
    "HUMAN_EXEMPTION",
    "SOURCE_CLASS_WRITERS",
]
