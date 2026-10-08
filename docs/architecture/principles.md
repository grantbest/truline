# Principle Registry

The registry of `arch.principle` beads — see [`doctrine.md`](doctrine.md) for the mechanism and
lifecycle. Ids are stable (`PRIN-NNN` is the bead's external id). Statuses are honest to the
platform's reality: `adopted` means binding text already carries it, `enforced` means a mechanism
fires on violation, `proposed` means it exists only here. Sources cite Baldwin, *Design Rules,
Volume 2* by page; where a source is an operating incident in the exporting monorepo, the entry
says so without narrating it.

Seeded 2000-01-01 — the registry's fixed seed date. The bead schema requires a dated transition,
so every History line carries this one date and no other. Change by PR only.

---

### PRIN-001 — The gate is the option operator

- **Statement:** Every dispatch is a cheap, rejectable option *only because* the release gate can
  reject it; changes to the gate are changes to the platform's risk model and are amendment-class.
- **Source:** DRv2 pp. 153–158; an operating incident in the exporting monorepo in which several
  PRs were merged on green CI with no gate at all.
- **Status:** `adopted` — `CLAUDE.md`'s merging rules are binding text.
- **Enforcement gap:** no mechanism blocks a verdict-less merge; process only.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-002 — Invest at the constraint, measure the constraint

- **Statement:** The factory runs at the capacity of its slowest stage; per-stage capacity is
  measured, the current constraint is named in every portfolio report, and investment (including
  model tier) goes to the constraint first.
- **Source:** DRv2 pp. 86, 104–111; the observation that one automated end is the ceiling.
- **Status:** `proposed` — becomes adopted with a constraint-first metric and model tiers.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-003 — A real dependency is a typed edge or it is a defect

- **Statement:** Dependencies between work items, and between loops, are carried as typed graph
  edges and structured bead fields — never as prose that a reader must happen to open.
- **Source:** DRv2 pp. 24, 36–37; an operating incident in the exporting monorepo in which
  supersession carried as free text left a superseded bead runnable for days.
- **Status:** `adopted` — the operating model's "the bead is the contract" is binding.
- **Enforcement:** partial — supersession guards reverse-index `source_bead_ids`; the three
  traceability fields (`requirement_refs[]`, `nfrs[]`, `arch_impact`) remain the named gap.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-004 — Frozen contracts vs. internal flux, declared

- **Statement:** Every interface is explicitly one of two kinds: a frozen contract (versioned,
  validated in CI, amendment-class to change) or internal flux (free to change, but the changer
  fixes every in-repo consumer in the same PR).
- **Source:** DRv2 pp. 152, 342, 422; a dispatcher state-machine bypass and a stale-OpenAPI
  verification pass, both operating incidents in the exporting monorepo.
- **Status:** `adopted` — binding: deviating from it needs a written rationale in the output, like
  a SHOULD in ARCHITECTURE.md §0.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-005 — Modularity survives only through enforcement

- **Statement:** Interface and citation discipline is mechanized (conformance scripts, CI checks),
  because unenforced formality is the documented failure mode of every modular codebase.
- **Source:** DRv2 p. 369.
- **Status:** `adopted`; **enforcement** partial — `ea-conformance.py` covers the architecture
  model; `docs/requirements/` citations are unchecked (the operating model's not-built list).
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-006 — Stable core, fast periphery

- **Statement:** A thing belongs in the frozen core iff every module depends on it, modules do not
  hidden-couple through it, and embedding amplifies its change cost; everything else churns freely
  without amendment traffic.
- **Source:** DRv2 pp. 136, 164.
- **Status:** `adopted` — the design pillars assert it; this entry adds the admission test.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-007 — Unattended flow is entered through a buffer

- **Statement:** Any persona, prompt, or model change that participates in the unattended flow
  ships through an attended/staged buffer first (attended runs, staged rollout), never directly.
- **Source:** DRv2 pp. 338–340, 468; the attended containment spike that admitted the worker lane
  is the pattern.
- **Status:** `adopted` — the graduated-actuation ladder is the standing rule, and a second
  demonstrated instance exists: a drift detector ran announce-only from registration until its
  disposition half was added under attended conditions.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-008 — Surface the failure; never let a retry forget it

- **Statement:** Problems halt and announce themselves with their cause attached; a retry carries
  the reason its predecessor failed, and attempt history is never silently erased.
- **Source:** DRv2 pp. 111, 142; the requeue/attempt-counter rule.
- **Status:** `enforced` — the amendment is ratified and the mechanism shipped.
- **History:** 2000-01-01 `enforced` — seeded

### PRIN-009 — Feedback artifacts, not authority, steer design work

- **Statement:** Workers are corrected through richer specs, written verdicts, promoted rules, and
  calibration files — never through step-level command of how they work.
- **Source:** DRv2 pp. 370–373, 381.
- **Status:** `adopted` — the operating model's persona design and QA-memory section carry it.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-010 — System-level claims expire without demonstration

- **Statement:** Capability claims carry a probe and a date; a scheduled end-to-end demonstration
  runs each release cycle, because a stream of individually green PRs can still compose a failing
  system.
- **Source:** DRv2 pp. 8, 96, 379; "poller registered ≠ the factory works", an operating incident
  in the exporting monorepo.
- **Status:** `proposed` — conformance verdicts and the scorecard are the seed, not the cadence.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-011 — Demonstrated, or it is a conjecture

- **Statement:** No success is reported without verification, and disagreeing representations of
  one fact are read at their divergence point before a cause is written down.
- **Source:** DRv2 p. 8; `CLAUDE.md` operating rules (verify before asserting; never report
  unverified success).
- **Status:** `adopted` — binding text in `CLAUDE.md`.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-012 — Two inseparable options are one bead

- **Statement:** At intake, a bead that has no stand-alone value if its siblings are rejected is
  merged with them or linked with an explicit ordering edge the dispatcher respects.
- **Source:** DRv2 p. 150; the observation that FIFO dispatch ignores ordering dependencies.
- **Status:** `proposed` — becomes adopted when written into the PO → Architect handoff row.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-013 — Checks select themselves by the change they check

- **Statement:** A validation that runs regardless of what changed must either be cheap enough to
  be universal or justify itself in writing; shared scarce compute never runs unselected work.
- **Source:** proposed from the CI changes-gate, the first mechanized instance; the citation
  checker surfaced the unregistered proposal on its first live run.
- **Status:** `proposed` — one instance exists (the changes-gate); not yet a standing rule for
  suites beyond CI.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-014 — An unattended intake is idempotent

- **Statement:** Filing the same intent twice is a defect of the intake, never additional
  throughput; an unattended intake refuses a duplicate at the mouth, and a deliberate re-file is
  an explicit, recorded supersession.
- **Source:** proposed from a duplicate-filing incident in the exporting monorepo: two workers'
  full budgets spent producing PRs the gate then had to kill — every unattended loop downstream
  multiplied what the intake accepted twice.
- **Status:** `adopted` — filed `proposed` with promotion conditioned on its mechanization
  merging, and promoted when it did (filing refuses a duplicate live bead; a deliberate re-file is
  an explicit `--supersede`). **Enforcement note:** the first refusal was check-then-act with no
  serialization, so it only held for sequential filers — two concurrent filers each found no live
  duplicate and both filed. The mouth now refuses a duplicate under concurrency too: the store
  carries a partial unique index on live (pending/doing/review) `dev.task`
  `content.spec_identity`, so a second concurrent create is rejected by the database itself, not
  by another advisory re-check.
- **History:** 2000-01-01 `adopted` — seeded

### PRIN-015 — A control that cannot evaluate its question fails closed

- **Statement:** A check reports healthy only when it has actually evaluated the question it
  exists to answer; an input it cannot read, a measurement it cannot compute, or a proxy that no
  longer tracks the property are each a failure, never a pass.
- **Source:** proposed from four controls in the exporting monorepo that reported healthy in the
  exact situation each was built for. An image-policy rule asked whether an image was
  digest-*shaped* and so passed an all-zeros placeholder that left pods in `ImagePullBackOff`. A
  wedged-workflow check could not compute the age of a workflow that had been in flight for hours
  and printed `running_for=unknown wedged=false`. A drift check asked whether the worker's
  revision was an *ancestor* of main, which every stale-but-clean worker is, so a worker two
  merged commits behind reported no drift. A supersession backfill resolved a replacement id from
  the *oldest* claim while its docstring promised the newest, because `list_notes` returns
  newest-first and the reader assumed the opposite.
- **Status:** `proposed` — four repairs exist and no standing rule; the mechanization question is
  whether "cannot evaluate" can be made structurally distinct from "evaluated as healthy" in the
  checks the platform already runs.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-016 — A merged change is complete as merged

- **Statement:** A change lands with the thing that makes it act, or it does not land; the
  consumer of a capability never merges ahead of its producer, and a comment promising later
  wiring is not a control in a repository where merge is deploy.
- **Source:** proposed from five changes in the exporting monorepo that each shipped one half of a
  contract and read as done. Manifests were pinned to registry digests while every build workflow
  still gated its push on a variable that had never been set, so the registry had never received
  an image and those digests could not have resolved. A verification field shipped into the
  substrate schema with nothing in the dispatcher reading it. A filing waiver shipped that the
  dispatcher ignored, minting beads that file and can never run. A cluster health checker shipped
  with no schedule, so the alarm fired only when a human ran it. And a registry `check-view`
  command — which exits 1 correctly — was run by no CI job and no invariant, so the registry
  drifted from the beads it is a view of with nothing reporting it.
- **Status:** `proposed` — the intake-side twin, PRIN-012, already carries this shape for sibling
  beads; this extends it to merge time, where four of the five instances occurred. Mechanization
  is unsolved: CI can check that a named capability flag is on before its consumer merges, but
  cannot in general prove a change is whole.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-017 — A detection ends in a recorded disposition

- **Statement:** A condition worth detecting is worth a record of what was decided about it: every
  detector's outcomes are a closed set, one member is recorded per firing, and silent continuation
  is never a member of the set.
- **Source:** proposed from a worker-revision-drift schedule that detected the checkout many merged
  PRs behind main every fifteen minutes while nothing else happened — "detection without action
  is not an outcome" (`worker_checkout_drift_response.py`). The statement says *disposition*
  rather than *action* on purpose: DEFERRED and NONE are legitimate recorded outcomes, so the
  obligation is a recorded decision, not motion. `failure_diagnosis.py`'s
  proto-incident-with-no-bead is the same lesson from the announce side.
- **Status:** `proposed` — two instances exist (`worker_checkout_drift_response.py`'s closed
  three-outcome set; the declared-alert discipline covers the announce half); becomes adopted when
  detectors record dispositions against the `arch.incident` carrier.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-018 — Implementable work defaults to a bead

- **Statement:** Work that could be chartered is chartered: the board sees every change before it
  ships, and an outer-loop implementation is the justified exception — its PR body records why
  the bead path was not taken, so a working day the board cannot see is a defect, never a pace.
- **Source:** proposed from a working day in the exporting monorepo in which several outer-loop
  PRs merged under full gate, marker and merge discipline while the board never moved and the
  release balance saw none of it; the operator's question at day's end — "are we following
  discipline?" — was answerable for the gate and not for the beads.
- **Status:** `proposed` — the remedy pattern exists (retroactive filing walked to done with dated
  notes citing PR and verdict) and no standing rule does; becomes adopted when the outer-loop
  justification line is a recorded convention the gate checks for, mechanized or procedural.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-019 — A copied contract carries a parity test, or the copy is a defect

- **Statement:** A contract copied across a module boundary — a state machine, a vocabulary, a rule a
  second tree re-derives — carries a parity test that re-executes the source and asserts equality; a
  copy without one is the third state PRIN-004 does not name, frozen on one side and flux on the other.
- **Source:** proposed by an architecture review and confirmed by a later audit in four more
  places: an incident severity literal, the console's board and blast-radius mirrors, a test's copy
  of the agent-provenance rule, and an alert inventory imported across an app boundary.
  `dev_task_contract.py` + `test_dev_task_contract_parity.py` is the pattern; one silent copy is
  what it cost before that pattern existed.
- **Status:** `proposed` — becomes adopted when parity tests for the alert inventory and the
  retired-name invariant ship.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-020 — A ratified amendment is executed by beads the ratifying PR files

- **Statement:** A pull request that ratifies an amendment files the `dev.task`(s) for each executable
  clause of its diff, or cites the PR that already carried it, and the ledger records execution beside
  ratification; a `RATIFIED` amendment with an unexecuted clause and no execution line is a defect the
  repository reports.
- **Source:** proposed from an audit's dominant finding: several ratified amendments each carried an
  unexecuted executable clause with nothing in the amendment ledger saying so, and a tripwire clause
  that nothing read. PRIN-018 says implementable work defaults to a bead; this is that rule applied
  to the document that binds everything else.
- **Status:** `proposed` — becomes adopted when the governance clause that names it is ratified and
  the ledger invariant it names is in CI.
- **History:** 2000-01-01 `proposed` — seeded

### PRIN-021 — Every registered schedule's last run is a measured fact

- **Statement:** For every schedule a worker registers, the outcome of its most recent run is evaluated
  on a cadence and either reads Completed or raises a declared alert naming the schedule and the failure;
  a run that cannot be evaluated is reported as such, never as healthy.
- **Source:** proposed from silent schedule failures found by hand in one audit: a reconciler that
  had failed hundreds of consecutive ticks, several scheduled reports that had failed every run in
  retention — and the only schedule alerts that existed covered the dispatch drain. PRIN-015 says a
  control that cannot evaluate its question fails closed; PRIN-017 says a detection ends in a
  recorded disposition; this names the population both apply to.
- **Status:** `proposed` — becomes adopted when the check ships across every namespace the factory
  runs schedules in.
- **History:** 2000-01-01 `proposed` — seeded
