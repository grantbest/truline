# Bead object inventory

**Status:** ADOPTED
**Companion to:** [`agentic-sdlc-beads.md`](agentic-sdlc-beads.md) (how the graph moves between
loops) and [`ea-metamodel.md`](ea-metamodel.md) (the CSDM contract and its staging).

What is modelled as a bead, what is not yet, and what is deliberately out of scope. The point of
writing it down is that "we should probably have a type for that" is otherwise decided one incident
at a time, which is how a graph turns into a junk drawer.

## Namespaces

| Namespace | Holds | Test for belonging |
|---|---|---|
| `arch` | The service model and the ITSM records that move it | *What exists, and what happened to it* |
| `dev` | Artifacts passing through the SDLC | *How a change came to be* |
| `finance` | The LifeOps example vertical | Vertical data, not platform metadata |

`arch` carries `change` and `incident` rather than a separate ITSM namespace, because
[`ea-metamodel.md`](ea-metamodel.md) §2 already places `vendor`, `person`, `location` and
`offering` there. One boundary, already chosen, is worth more than a tidier one invented later.

## Inventory

**Live** — registered in `schemas.py` and validated.

| Type | Carries | Persona |
|---|---|---|
| `arch.capability` | Business/platform capability | EA |
| `arch.application` | Business application | EA, Config Mgmt |
| `arch.service` | Application service | EA |
| `arch.information_object` | Information object | EA |
| `arch.observation` | Measurement against a model object | EA |
| `arch.change` | ITIL change record | Config Mgmt |
| `arch.ci` | Technology-layer configuration item (workload, namespace, database) | Observer only |
| `arch.release` | The objective a body of work serves: outcomes, declared balance, lifecycle | PO / SRE |
| `arch.requirement` | A registry requirement, mirrored from `docs/requirements/*.json` | PO |
| `arch.requirement_conformance` | A dated verdict against one requirement criterion | loader only |
| `arch.principle` | A platform design principle and its binding tier | Architect / SME |
| `arch.incident` | Production incident: severity, detection, resolution, affected applications | SRE |
| `arch.risk` | Accepted risk: statement, owner, severity, review dates, decision | Architect, SRE |
| `arch.release_health` | One per open release: health, coverage, close readiness | PO / SRE (reads) |
| `dev.task` | Work specification, with requirement/NFR/impact refs | PO, Architect, Polecat |
| `dev.note` | Comment, question, answer, status, review | all |
| `dev.design` | Decision, rationale, alternatives rejected | Architect / SME |
| `dev.release` | What the gate required, ran and concluded | Release Manager |
| `dev.finding` | Bug, enhancement or security issue at the gate; a hand-filed observation | QA, SRE |

Three rows carry a writer restriction: `arch.ci`'s `source_class` is locked to `observed`;
`arch.requirement_conformance` is written only by `scripts/requirements-load.py`; and
`arch.release_health` (`health.<release_ref>`) is written only by
`factory-dispatcher/release-status` — its health is computed over a closed signal set with
hysteresis, and its six-state machine enters at `unmeasured`.

**Gaps** — no carrier today, ranked by what they cost.

| Type | Carries | Why it matters |
|---|---|---|
| `dev.spike` | A question, a time box, and what was learned | `dev.task` distorts it |
| `dev.evaluation` | Per-run reviewer precision, findings verified vs raised | Makes calibration queryable |

A spike's output is knowledge, and its success is that you now know, which `dev.task`'s
acceptance shape cannot express. `dev.evaluation` turns the calibration file from a
hand-maintained table into a queryable EA metric.

### `arch.incident`

The content schema is `ArchIncidentContent` (`apps/substrate/src/schemas.py`). Deliberately not an
`ArchContentBase` subclass, for the reason `ArchChangeContent` already gives: an incident is an
*event against* a CI, not a CI. Fields: `summary`, `severity` (the `ALERT_INVENTORY` closed set —
`urgent` / `actionable` / `informational`, one severity vocabulary expressed as a literal rather
than an mcp-hub import, with a parity test holding the two together), `detected_at`,
`resolved_at`, `source`, `applications`, `resolution`, `runbook_refs: list[str]` — paths under
`docs/runbooks/`; runbooks stay documents, per this inventory's own ruling below, so the
reachable-runbook promise is a content field rather than a new edge type.

The state machine is registered in `apps/substrate/src/bead_rules.py`'s `STATE_MACHINES`
registry: entry state `detected` only, `detected → {mitigating, closed}`,
`mitigating → {resolved, detected}`, `resolved → {closed, detected}`, `closed` terminal. The same
machined-type PATCH guard that holds for `dev.task` and `arch.release` holds here — no route code
is needed, because the generic `STATE_MACHINES.get((namespace, type))` lookup in `routes.py`
picks up every registered machine.

**Who may open one.** Only an `AlertDefinition` in mcp-hub's `ALERT_INVENTORY` that opts in via
`opens_incident` (default `False`, severity-thresholded) may open an incident. The definition's
existing per-subject dedup key becomes the incident's identity — one open incident per (alert,
subject); a re-fire attaches evidence to the open incident rather than minting a second. The raw
Alertmanager/Prometheus rules stay read-only and out of scope until a rule earns a declared
`AlertDefinition` first — they carry no per-subject identity contract and would flood the type.
Opening a bead here is record-keeping, rung 0 of the graduated actuation ladder, not actuation.

The case for the type: the release gate has a feedback loop — `dev.finding --regresses--> dev.task`
means a defect found at review carries back to the change that caused it. Production needs the
same: an incident that lands in a memory file or an audit note is disconnected from the change
that caused it. Three real incidents, three different places, no edges, is what
`arch.incident --caused_by--> dev.task` exists to close.

### `dev.finding` has a state machine

`bead_rules.py`'s `STATE_MACHINES` registers `("dev", "finding")`: entry state `pending` only,
fanning out to four terminal states in one edge — `backlogged` and `ruled` (`CLAUDE.md`'s two
absorption outcomes: a backlog item, or a rule that prevents recurrence), and `already_fixed` /
`not_a_defect` (the two ways a finding closes without absorbing into either). A finding with no
declared machine sits in `pending` forever, because nothing can move it through a governed
transition; the machine is what makes "dispositioned" a checkable fact.

### `arch.risk`

A content model (`ArchRiskContent`, `apps/substrate/src/schemas.py`) and a state machine in
`bead_rules.py`'s `STATE_MACHINES` — entry state `accepted` only (a register of risks already
accepted, not a proposal queue), `accepted → under_review`, `under_review → {retired, accepted}`
(the renewal edge, filed with a fresh `review_by`), `retired` terminal. Content carries
`statement`, `owner`, `accepted_at`, `review_by`, `severity` (`critical`/`high`/`medium`/`low`,
reusing `arch.application`'s `business_criticality` vocabulary rather than `arch.incident`'s
alerting-severity one) and `decision_ref` — a citation to the amendment or decision record that
accepted the risk, since not every historical acceptance has a bead of its own. Two edges come
with it (see Edge vocabulary below): `threatens` to the application(s) a risk puts at stake, and
`accepted_by` to the `arch.change` that formally accepted it, for the risks that gain one.

The first risks to file are the ones whose acceptance lives only in prose: an amendment that
acknowledges a single-node control-plane SPOF, a mutable deploy path that cannot be rolled back,
a factory that is a single process on a single host. Until they are filed, `risk` stays
measurable as ABSENT against a declared release balance — the gap the register exists to close.

## Provenance is a contract, not a type

`bead` has a `provenance` JSONB column, alongside `confidence`, `trust_tier` and `created_by`,
holding the chain of agents, prompts, and models that produced it. Its shape is `BeadProvenance`
in `apps/substrate/src/schemas.py`: six required keys, with nullable `tokens`/`cost_usd` so an
unmeasured run says so instead of asserting zero. Populating it at write time is what makes the
Enterprise Architect persona's remit — *which models are producing which value at what health
and cost* — answerable at all:

```python
{
  "worker":     "<worker lane>" | "gate" | "human",
  "model":      "<model id>",
  "prompt_ref": "AGENTS.md#polecat-developer@<sha>",   # versioned, not just named
  "tokens":     {"in": 41200, "out": 3100},
  "cost_usd":   0.87,
  "duration_s": 194,
}
```

A sparse provenance population leaves that question not hard to answer but **unanswerable**,
because the data is not recorded anywhere.

## Out of scope, deliberately

Not oversights. Each has a reason and, where relevant, a stage.

- **`arch.vendor`, `arch.person`, `arch.location`, `arch.offering`, `arch.technical_service`** —
  already on the CSDM roadmap in the metamodel §2. Planned, not missing. `vendor` becomes real
  when supply chain does.
- **ITIL Problem** — premature until incidents exist and recur. A problem record with one incident
  is an incident.
- **Runbooks** — procedures humans read. Files serve that better than beads; the useful part is a
  reference from an incident to the runbook that resolved it.
- **Test cases as objects** — the suite is the record. A test case bead that can drift from the
  test it describes is a second source of truth.

## The one open fork: requirements as beads

`dev.task.requirement_refs` is a validated **string** — `LO-CAT-004/AC-1`. `arch.requirement`
mirrors the registry into beads, but the task-to-requirement relationship is still a string, not an
edge: queryable, integrity-enforced, and cascade-protected against pointing at something deleted.

[`ea-metamodel.md`](ea-metamodel.md) §5 fought this exact argument for the EA model and its verdict
transfers: ref arrays in content are acceptable at tens of objects, not at hundreds.

**So strings are correct now, and this is a known exit rather than a permanent choice.** Trigger it
on whichever comes first:

1. The registry passes ~100 requirements, or
2. A second consumer needs to query by requirement (the release manifest counts as the first), or
3. A dangling `requirement_ref` reaches a release — at which point the string form has already cost
   more than the migration.

The exit is cheaper decided than discovered. Deciding it at 300 requirements means migrating 300.

## Edge vocabulary

**This is the canonical edge table.** [`agentic-sdlc-beads.md`](agentic-sdlc-beads.md#edge-vocabulary)
points here rather than repeating it — a table copied into two documents drifts, and
`BEAD_LINK_TYPES` (`apps/substrate/src/schemas.py`) must be built against the table the loader and
the knowledge engine actually write.

`link_type` is a free string with a uniqueness constraint on `(source, target, link_type)`. Nothing
in the database prevents a typo forking the vocabulary into `regressed_by`; `BEAD_LINK_TYPES` is
what closes that, and
`apps/substrate/tests/test_edge_vocabulary.py::test_schema_link_types_match_canonical_edge_table`
keeps the two from drifting apart.

### Admitted — nineteen types

| Edge | Writer |
|---|---|
| `dev.design --designs--> dev.task` | Architect/SME, filing the design record |
| `dev.task --supersedes--> dev.task` | PO / dispatcher, filing rework |
| `dev.release --gates--> dev.task` | Release Manager |
| `dev.finding --regresses--> dev.task` | QA |
| `dev.finding --found_by--> dev.release` | QA |
| `arch.change --affects--> arch.application` | Config Mgmt |
| `arch.observation --measures--> arch.application` | `scripts/ea-load.py` (`EDGES`) |
| `arch.capability --supports--> arch.capability` | `scripts/ea-load.py` (`EDGES`) |
| `arch.application --realizes--> arch.capability` | `scripts/ea-load.py` (`EDGES`) |
| `arch.application --depends_on--> arch.application` | `scripts/ea-load.py` (`EDGES`) |
| `arch.application --depends_on--> arch.ci` | the observer (`activities/ea_observation.py`), see note 1 |
| `arch.application --consumes--> arch.service` | `scripts/ea-load.py` (`EDGES`) |
| `dev.design --applies--> arch.principle` | Architect/SME, citing the principle a decision applies |
| `arch.principle --derived_from--> dev.task` | knowledge ingestion, see note 2 |
| `dev.task --derived_from--> dev.finding` | PO / dispatcher at filing (`file_task.py`), see note 3 |
| `arch.principle --enforced_by--> dev.task` | Architect/SME, when a principle is mechanized |
| `arch.incident --caused_by--> dev.task` | SRE |
| `arch.incident --resolved_by--> arch.change` | SRE |
| `dev.task --delivers--> arch.release` | PO / dispatcher at filing, or `--bind-release`, see note 4 |
| `arch.risk --threatens--> arch.application` | Architect/SME, SRE |
| `arch.risk --accepted_by--> arch.change` | Architect/SME, SRE, see note 5 |

Notes:

1. The observer reuses the admitted `depends_on` verb rather than minting a new type; direction
   reads *the application depends on the CI to run*, backward *the CI is used by the application*.
2. `apps/factory-dispatcher/activities/knowledge_ingestion.py` links a landed principle back to
   the extraction task that produced it — not to `arch.incident`; see that module's docstring.
3. `apps/factory-dispatcher/file_task.py` writes it from the `derived_from_finding_ids` spec
   field — the edge back to the finding that `CLAUDE.md`'s finding rule requires; the task is the
   source, the finding the target.
4. Written at intake (`apps/factory-dispatcher/file_task.py`) so the release a task serves is
   decided before the work starts, not reconstructed after it.
5. `arch.change` is the existing decision-record-shaped carrier in this vocabulary, not a new
   type.

### Excluded, deliberately

A vocabulary is not a wish list — an edge with no writer is speculative, not closed.

| Edge | Why excluded | Admit when |
|---|---|---|
| `arch.application --produces--> arch.information_object` | No writer, see below | A writer exists |
| `arch.information_object --reads--> arch.application` | Same as `produces` | Same as `produces` |

No writer exists anywhere in the estate for the CI ↔ Information Object edge
([`ea-metamodel.md`](ea-metamodel.md) §5.3); both edges are admitted the day one does.
