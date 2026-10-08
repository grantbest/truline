# The agentic operating model

**Status:** ADOPTED

Who does what, in which loop, and what has to be true at each handoff. This document is canonical
for the *process*; [`ARCHITECTURE.md`](../../ARCHITECTURE.md) remains canonical for the platform.
When they disagree about the platform, `ARCHITECTURE.md` wins.

Three loops, eight personas, one model family. The design constraint that shapes everything below:
**every transition is a file in this repository or a bead.** There is no manual handoff: the outer
loop dispatches the release gate as a fresh-context agent, and the attended floor is graduated
authority over structural change, not a paste (ARCHITECTURE.md §7).

```
  OUTER LOOP                   INNER DEV — the worker       INNER RELEASE — the gate agent
 ┌───────────────────────┐     ┌──────────────────────┐     ┌──────────────────────────┐
 │ Product Owner         │ bead│ Architect / SME      │ bead│ Release Manager          │
 │ Site Reliability Eng. │────>│ Polecat developer(s) │ + PR│ QA Engineer              │
 │                       │     │                      │────>│ Configuration Management │
 │  <── bugs, CMDB deltas, portfolio metrics ──────────────  │ Enterprise Architect     │
 └───────────────────────┘     └──────────────────────┘     └──────────────────────────┘
                                                                 ▲
                                                                 └── dispatched fresh-context
```

**The bead travels with the PR, always.** A pull request is evidence that work happened; the bead is
what the work was *for* — the requirement, the NFRs, the architectural impact, and the history of
what has broken here before. A reviewer given only the diff can judge whether code is correct. Only
the bead lets them judge whether it is the right code, and which suites it obliges them to run.

So the carrier on that edge is both, by construction rather than by diligence: the dispatcher
stamps the originating `dev.task` id into every PR body it opens, and the release manifest
(see [`agentic-sdlc-beads.md`](agentic-sdlc-beads.md)) exports the surrounding subgraph beside it. A
PR that arrives at the gate without its bead is a defect in the handoff, not a PR to review anyway.

---

## Outer loop

Owns intent and operational truth. Everything entering the factory is shaped here, and everything
coming back out is absorbed here.

**Product Owner.** Roadmap, vision, backlog. Scopes not just the next sprint but the next three
releases, balancing enabling, blocking, risk and security work. Owns the requirements registry
(`docs/requirements/`), the release charters under `docs/releases/`, and the sprint plans. A
requirement without acceptance criteria is not ready to leave this loop, and a sprint that serves
no chartered outcome is not ready to be planned — see
[The release is the unit of intent](#the-release-is-the-unit-of-intent) below.

**Site Reliability Engineer.** The operational state — releases, merges, incidents, runbooks, and
what the system is actually doing right now. Focus is experience and resilience. This persona is
the reason the outer loop can answer "what changed and what broke" without asking the other loops.

**This persona executes every merge into `main`.** The release gate writes the verdict; the SRE
acts on it. The reasoning is that a merge is the moment work becomes operational truth, and the
failure modes that follow one — a red `main`, a stale branch that carries someone else's commit, a
guard that fires on history it cannot change — are all handled faster by the loop that already owns
the pager than by routing them through the operator.

The gate is not weakened by this and must not be skipped: the SRE may merge only against a written
verdict, never to unblock itself, and never on green CI alone. `CLAUDE.md` carries the operating
rules; the gate charter carries the reciprocal statement that its report is written to be acted on
literally.

## Inner loop, development — the factory worker

Owns the change while it is being made. The personas below are materialised into whichever worker
the dispatcher runs; they are the loop's, not any one vendor's.

**Architect / SME.** First to see work once a sprint locks, and the only role that judges
architectural impact before code exists. Maximises the Temporal and bead capabilities rather than
routing around them. Solves for resilience, NFRs, and how information flows. The EA model
(`docs/architecture/`) and CSDM are binding here, not advisory — `scripts/ea-conformance.py` fails
the build when the model and the code disagree.

**Polecat developer.** Receives a bead carrying everything to its left — the requirement, its
success criteria, and the NFRs the architect derived. Works TDD. Follows the engineering doctrine
in the substrate (Beck on tidying and test-first, Majors on observability and operating what you
build). Full traceability is the point: a Polecat should never need to ask what a task is for.

## Inner loop, release — the gate (a fresh-context agent)

Owns the gate. Runs as **one pass with four hats**, not four sessions — one pass per dispatch is a
deliberate constraint, and splitting it would multiply it. The gate charter defines the four hats;
who wears them may change, what they are does not.

**Release Manager.** Reviews a sprint's worth of staged PRs as one release. Determines which test
cases and regression suites are *required* based on what was touched, using the traceability in the
beads moving through. Negative testing, regression, linting, and the rest of the gate.

**QA Engineer.** Executes what the Release Manager identified. Deep knowledge of the specific
technologies is what makes this persona good at the job. Writes bugs and reports them back to the
outer loop. Builds a flywheel of what "good" looks like — see *QA memory* below, because the naive
version of this does not work here.

**Configuration Management.** Accountable for change records and CMDB accuracy in the CSDM model.
ITIL discipline: capabilities, business applications, and underlying services stay mapped and
current. Every change passes this gate, so nothing should ever be missed — a CMDB that drifts is a
CMDB that gets ignored.

**Enterprise Architect.** Accountable for the application portfolio, and the translation of outer-loop
strategy into it. Focus is AI-first engineering metrics: which models are producing which value at
what health and cost, so the outer loop can set technology strategy on data.

---

## The release is the unit of intent

A sprint is a time box. It answers *when*, and it cannot answer *what for*. A loop with nothing
above the sprint that a machine can read answers "what is this release going to deliver" from
whichever plan document is most recent — and when the factory outpaces the planning cadence,
merges land under no plan at all.

**A release is one three-sprint arc**, chartered before the sprints inside it are planned.

### The charter

`docs/releases/<ref>.json`, authored by the PO and reviewed by PR. It carries a `ref`
(`R<YY>.<NN>`), a name, a one-paragraph `objective`, the sprint numbers in the arc, and its
**outcomes** — each a statement of what is tangibly different afterwards, a `work_class`, and the
requirement criteria that prove it. It also declares a **balance**: the intended split across
`feature`, `enabling`, `blocking`, `risk` and `security`.

The outcome statements are written for the person the release is for. They are the lines that end
up in the release notes, so a charter whose outcomes only a builder can read has not been written
yet.

`scripts/release-load.py` mirrors charters into `arch.release` beads. Git stays authoritative for
structure; the substrate owns the lifecycle — `planned → in_flight → closing → released`, governed
by a declared state machine. **Nothing in git may declare a release released**: that claim is only
true once the work landed, so it is reached by transition and never by a loader.

### What ties work to it

| Fact | Carrier |
|---|---|
| Which release a task delivers | `dev.task --delivers--> arch.release` |
| Which outcome within it | `dev.task.content.outcome_ref` — the bare `O-2` |
| Its work class | the outcome, never the task |
| That no release applies | `dev.task.content.release_ref_waived`, in writing |

Filing refuses a spec carrying neither a resolvable `release_ref` nor a written waiver — the same
shape `requirement_refs` enforces, one field over. A waiver is a visible choice, recorded on the
bead and counted as its own population, not silence. The scanner's auto-filed beads take a waiver
by default, so an automated filer surfaces a triage queue for the PO rather than quietly filling a
release.

The split matters and is deliberate: the release lives only in the edge, because "everything in
this release" must be one indexed query; the outcome lives only in content, and is meaningless
without the edge, so the two cannot drift apart.

### What comes out of it

- **During the arc:** `release-status.py` reports declared against actual balance, and counts the
  work bound to no release at all. Crowding-out becomes a number at the moment it can still be
  changed, which is sprint planning.
- **At the close:** `release-notes.py` generates `docs/releases/<ref>-notes.md` from what merged
  and what was measured — outcome by outcome, with the conformance verdicts that moved, their dates
  and their revisions. An outcome with no merged task renders NOT DELIVERED; a criterion whose
  newest observation predates the release opening renders *not re-measured*. Notes that overclaim
  are worse than no notes, because they are what a later reader will trust.

The procedure is [`docs/runbooks/release-close.md`](../runbooks/release-close.md).

### How this changes the sprint plan

Not much, deliberately. The plan keeps its shape — §0 ground truth, one section per sprint,
expectations, doctrine. It gains a header line naming its release, and every item cites the outcome
it serves (`R<YY>.<NN>/O-2`). The release holds the objective; the sprint holds the time box.

---

## The bead is the contract

Traceability between loops is carried by the bead, not by prose in a PR. The dispatcher builds the
Polecat's prompt and the PR body from bead fields
([`apps/factory-dispatcher/dispatch.py`](../../apps/factory-dispatcher/dispatch.py)).

**What a `dev.task` bead carries:**

`title` · `lane` · `risk_class` · `intent` · `acceptance[]` · `verification.commands[]` ·
`scope.paths[]` · `scope.forbidden_paths[]` · `worker_hint` · `release_ref` / `outcome_ref` ·
`requirement_refs[]`

**What this model needs it to carry for full traceability:**

| Field | Produced by | Consumed by |
|---|---|---|
| `requirement_refs[]` | Product Owner | Polecat, Release Manager |
| `nfrs[]` | Architect / SME | Polecat, QA |
| `arch_impact` | Architect / SME | Config Management, EA |

- `requirement_refs[]` links the task to `docs/requirements/` so a PR can be traced to the
  criterion it satisfies. Accepts `LO-CAT-004` or `LO-CAT-004/AC-1`.
- `nfrs[]` carries the non-functional constraints that would otherwise be derived and then lost,
  never reaching the developer or the tester. Structured
  `{category, statement, threshold, verification}`, so QA can derive tests mechanically.
- `arch_impact` names which CSDM applications and capabilities this touches, declared by id.
  Without it, CMDB updates are reconstructed after the fact from diffs.

Where those fields are absent, the Release Manager cannot derive required regression suites from
traceability — it can only infer them from the diff, which is guesswork on exactly the changes
where guessing is most expensive.

Those three fields cover the *forward* path only. The return arrow in the diagram above — bugs,
CMDB deltas, portfolio metrics — needs carriers of its own.
[`agentic-sdlc-beads.md`](agentic-sdlc-beads.md) maps every persona's output to a bead type and the
edges that connect them.

**A caution on lineage.** Bead history is what Postgres and the Substrate API provide, and that is
the end state as well as the present one. Design against it; do not design for a hypothetical move
to another store.

---

## QA memory, and why the obvious version fails

The QA persona should accumulate knowledge of what good looks like. It cannot do this the natural
way. The gate agent has no memory across runs — structurally true of a fresh-context dispatch — and
a gate that writes to its own instruction file produces a self-written mandate that outlives the
architecture it describes, in plain text, because nothing ever diffs it against the repository.

So the flywheel is made of repository artifacts, which are diffable, dated and reviewed:

- **Test-case knowledge** accumulates in the test suites themselves. A regression that was found
  once and is not encoded as a test was not learned.
- **Review calibration** accumulates in a calibration file in the repository — per-run precision,
  finding by finding.
- **Rules** accumulate in the gate charter, promoted by PR from the proposed-rules list each run
  ends with.

Slower than remembering. It is the only version that stays true.

---

## Handoff contracts

| Handoff | Carrier |
|---|---|
| Charter → PO | `arch.release` bead |
| PO → Architect | `dev.task` bead |
| Architect → Polecat | same bead, enriched |
| Polecat → Release | PR **and** its bead, via the release manifest |
| Release → Outer loop | Bugs, CMDB deltas, portfolio metrics |

Ready means:

- **Charter → PO:** objective stated, outcomes testable and classed, balance declared, cited
  criteria resolvable.
- **PO → Architect:** requirement referenced, acceptance criteria testable, risk class declared,
  **and a release named or waived in writing**.
- **Architect → Polecat:** NFRs attached, architectural impact recorded, scope bounded.
- **Polecat → Release:** one declared `Change kind:`, verification evidence in the body, CI green,
  and the originating `dev.task` id stamped in the PR body.
- **Release → Outer loop:** findings verified per the gate charter, precision recorded in the
  calibration file.

---

## What is not built yet

Named here so the model is not mistaken for a description of running machinery:

1. **The `nfrs[]` and `arch_impact` fields.** Without them, inter-loop traceability is partial.
2. **Registry citation checking.** `scripts/ea-conformance.py` validates `docs/architecture/` but
   nothing validates `docs/requirements/` citations, so the requirements baseline rots silently.
3. **AI-first engineering metrics.** The EA persona needs a data source. Per-model spend is
   exposed and is the obvious seed; there is no portfolio-level view yet.
4. **EA consultation services for the gate.** Expected to mature over time. Today the EA persona
   reads the operator's model files directly.
