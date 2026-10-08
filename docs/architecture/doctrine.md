# The Doctrine Layer

**Status:** ADOPTED — the `arch.principle` substrate type this document specifies exists
(`apps/substrate/src/schemas.py`, `ArchPrincipleContent`), the registry is mirrored by
`scripts/principles_sync.py`, and dispatch-time injection runs (`apps/factory-dispatcher/doctrine.py`).

How the platform learns on purpose. This document defines where design principles live, how they
gain force, and how they reach the agent that is about to make a decision. It is canonical for the
*doctrine mechanism*; [`ARCHITECTURE.md`](../../ARCHITECTURE.md) remains canonical for the platform
and [`agentic-operating-model.md`](agentic-operating-model.md) for the process.

## The rule this implements

**The harness is managed with beads and skills; Temporal is the run-state.**

This extends the design pillar that separates intent from execution (ARCHITECTURE.md §2) from work
to knowledge:

- **Beads** hold what the platform believes and why — principles, their sources, their status, and
  the edges to what they govern. Knowledge is typed graph data, not prose in someone's context
  window.
- **Skills** are the injection points — the mechanism that puts adopted doctrine into an agent's
  context *at decision time*, on demand, without bloating the always-loaded instruction files.
- **Temporal** records what ran. Doctrine never lives in run-state, and run-state bookkeeping never
  lives in doctrine — the same separation the pillar already enforces for attempt counts.

## Why a mechanism, not a shelf

Stored knowledge the platform does not *read at decision time* does not operationally exist. The
exporting monorepo established this three times over: retry workers that never read back the
failure reasons written for them; a note on a bead that explained a counter reset and was never
opened, so a rule shipped that stranded wanted work; supersession carried as free text, so a
superseded bead stayed runnable for days. The operating rule in `CLAUDE.md` already states the law:
*a finding that produces neither a backlog item nor a rule was not absorbed.* This layer mechanizes
that rule for design knowledge.

The theory is Baldwin's (*Design Rules, Volume 2*): converting lateral dependencies — re-explaining
a principle in every conversation — into hierarchical design rules is what modularization *is*
(p. 33), and design rules only hold when they are embedded and enforced rather than conventional
(p. 152).

## The ladder: three tiers of binding force

Every learning is at exactly one tier. The system's job is to make promotion cheap and stagnation
visible.

| Tier | Force | Read by |
|---|---|---|
| 3 — Advisory | None. Reference. | Whoever goes looking |
| 2 — Adopted | Binding text at decision time | The deciding agent, by construction |
| 1 — Enforced | Mechanized; violation is an error | Nobody — it fires regardless |

Where each tier lives: Tier 3 in plan documents, audit notes and memory files; Tier 2 in
`CLAUDE.md`, `AGENTS.md`, the gate charter, `ARCHITECTURE.md`'s pillars and amendments, and the
principle registry via the skill; Tier 1 in `ea-conformance.py`, CI guards, substrate 422s/409s
and state machines.

A principle's `status` names its tier: `proposed` (Tier 3), `adopted` (Tier 2), `enforced`
(Tier 1). Enforcement coverage may be partial; the registry entry says what is and is not
mechanized. Demotion is allowed and must be recorded with a reason — a principle the platform has
stopped believing is retired in place, never deleted, for the same reason conformance verdicts are
dated snapshots.

## The type: `arch.principle`

Registered in the [bead object inventory](bead-object-inventory.md). Fields:

- **`statement`** — one sentence, imperative, testable-in-review ("the gate is the option
  operator; changes to it are risk-model changes").
- **`rationale`** — why: the failure it prevents or the value it protects, with the incident or
  source that taught it.
- **`source`** — provenance: book + page, incident id, audit — free text; incidents also get the
  typed edge.
- **`status`** — `proposed` / `adopted` / `enforced` / `retired`.
- **`status_history[]`** — dated transitions with reasons: promotions earn their way, demotions
  explain themselves.

Edges (in the inventory's vocabulary):

- `dev.design --applies--> arch.principle` — a decision record citing the principle it applies
- `arch.principle --derived_from--> dev.task` — the extraction task that landed it
- `arch.principle --enforced_by--> dev.task` — the change that mechanized it

## The registry file is a view

The registry is [`principles.md`](principles.md) in this directory — diffable, dated, reviewed,
exactly as the operating model's QA-memory section prescribes for knowledge that must not rot in
an agent's private memory. It is a *view* of the beads (the kanban-is-a-view pattern: beads exist
regardless), rendered and checked by `scripts/principles_sync.py render` / `check-view`, and the
skill queries the substrate first and falls back to the file. Ids are stable across both —
`PRIN-NNN` is the bead's external id — so a citation written against either resolves on both.

## Injection points — one per loop, all existing machinery

| Loop | Mechanism |
|---|---|
| Outer | The `platform-doctrine` skill (`.claude/skills/platform-doctrine/`) |
| Inner dev (workers) | The bead, via `dispatch.py`'s prompt builder |
| Inner release (the gate) | The gate charter's promoted-rules path |

- **Outer:** the skill is invoked when shaping beads, planning sprints, reviewing architecture, or
  drafting amendments; it loads the registry and requires principle citations in outputs.
- **Inner dev:** principles reach Polecats as bead/lane-template content — never as new prose
  files.
- **Inner release:** a principle that changes what the gate checks lands in the charter by PR from
  the proposed-rules list.

## Staleness: doctrine rot is measured, not suspected

The lane scanner produces dated `measured_at` verdicts. One rule extends it to doctrine: an
`adopted` principle with no `applies` citation and no `enforced_by` edge after a configurable window
is flagged — it governs nothing, and the system says so instead of anyone noticing. This is the
absorption rule made mechanical; `factory-doctrine-staleness-nightly` runs it.

## Citation checking

CI fails an amendment or `dev.design` that cites a `PRIN-` id absent from the registry — the same
shape as requirements-citation checking. A citation that resolves to nothing is a dangling edge in
prose, and prose edges are what this layer exists to replace.
