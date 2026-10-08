# Enterprise Architecture

An EA practice for the Truline platform, modelled on CSDM and stored in the substrate.

`/ARCHITECTURE.md` says what the platform intends to build. This directory says *what should
exist, what does exist, and how the two are reconciled* — structure authored in git, mirrored to
`arch.*` beads, validated on every PR, applied on a schedule, and observed against the live cluster
nightly.

This page is machine-checked. `scripts/check-repo-invariants.py` runs
`scripts/readme_conformance.py`, which ties the page's named counts and claims — the `arch.*`
type count and names, the capability/application/service counts, the ea-conformance check count,
the file inventory table, and the machinery section's named schedule IDs and their stated
cadences — to the model files and code they describe, and fails CI the moment one drifts. A
schedule `apps/factory-dispatcher/schedule_runtime.py` declares that this page names neither in
the machinery section nor in the reasoned-exemption list below fails CI too. Judgments (a `debt`
rating) remain uncheckable by nature. See "What is still not enforced".

## The files

The model instance under `model/` is operator-owned: it describes one operator's estate and is
not part of the framework. The example model that ships with the framework is at
`apps/substrate/publish/csdm-on-beads/` (`docs/architecture/model/` inside that package). The
rows below describe the shape an operator's model takes; the counts live with the operator.

| File | What it is |
|---|---|
| [`ea-metamodel.md`](ea-metamodel.md) | The CSDM contract: objects, attributes, lifecycles |
| [`bead-object-inventory.md`](bead-object-inventory.md) | Type inventory and the closed edge vocabulary |
| [`doctrine.md`](doctrine.md) | The doctrine mechanism: how principles gain force and reach agents |
| [`principles.md`](principles.md) | The PRIN-001..021 registry, mirrored to `arch.principle` beads |
| [`agentic-operating-model.md`](agentic-operating-model.md) | Loops, personas, releases, handoff contracts |
| [`agentic-sdlc-beads.md`](agentic-sdlc-beads.md) | Persona→bead-type carrier map and the return path |
| [`dependency-posture.md`](dependency-posture.md) | The one definition of dependency posture |
| `model/business-layer.yaml` (operator-owned, not shipped) | capabilities, demand + supply layers |
| `model/application-portfolio.yaml` (operator-owned, not shipped) | applications, TIME and debt |
| `model/services.yaml` (operator-owned, not shipped) | application services; most derived from the deployment |
| `model/observations.yaml` (operator-owned, not shipped) | seed observations; the live set is written nightly |

## The machinery (what actually runs)

- **Validated on every PR:** `scripts/ea-conformance.py` — 13 checks — plus the checker test
  suite, in the `EA model conformance` CI job (`.github/workflows/lint.yml`). Coverage is
  ratcheted by `scripts/ea-coverage.py` against a committed baseline.
- **Applied every 15 minutes:** `scripts/ea-load.py` runs in-cluster on the Temporal schedule
  `factory-ea-apply-15m` (`apps/factory-dispatcher/activities/ea_apply.py`) — idempotent on
  `content.ref`, zero writes on an unchanged model revision, failures land on a standing
  `arch.observation` status bead.
- **Observed nightly:** `factory-ea-observation-nightly`
  (`apps/factory-dispatcher/activities/ea_observation.py`) reads the live cluster and ArgoCD,
  compares against the declared portfolio (`scripts/ea_reflect.py`), lands one standing
  `arch.observation` per divergence, and writes the technology layer as observed-only `arch.ci`
  records with `depends_on` edges.
- **Derived, not hand-copied:** `scripts/ea-derive.py` derives `depends_on` edges and workload
  objects from the manifests; a hand-authored copy of a derivable fact is a conformance
  *violation*, not a convenience.

## The types

Fourteen `arch.*` types are registered (`apps/substrate/src/schemas.py`, `ARCH_TYPE_SCHEMAS`):
`capability`, `application`, `service`, `information_object`, `observation`, `change`, `ci`,
`release`, `requirement`, `requirement_conformance`, `principle`, `incident`, `risk`,
`release_health`. Their liveness and
gaps are inventoried honestly in [`bead-object-inventory.md`](bead-object-inventory.md):
`information_object` has no writer. `arch.release` carries one of the platform's governed state
machines and gates dispatcher admission — the pattern the ITSM types copy.

## This is the system of record

Not a documentation exercise. A hand-maintained current-state document cannot hold this job: it
drifts into anti-truth however carefully its editing rule tells humans not to let it. The
difference is not that this model is better written; it is that the model half is **enforced**.

## Rules

1. **Evidence or it does not exist.** Every object carries `content.evidence` pointing at a repo
   path, PR, or dated verification.
2. **Structure in git, lifecycle in the substrate.** Adding a capability is a reviewed PR.
   A capability going `active` is a bead transition. See metamodel §6.
3. **Every supply capability traces to the demand side.** A platform capability with no path to a
   demand-side outcome is a rationalisation candidate, not an achievement.
4. **State and maturity are different questions.** `active` means something realises it.
   `operating` means it works. The gap between them is the backlog.
5. **Derivable facts are derived.** If machinery can read it from the manifests or the cluster,
   hand-authoring it is the violation (`ea-derive.py`, `check_derived_dependencies`).

## What is still not enforced

1. **`debt`, `technical_health`, and `business_value` are judgments, and nothing checks them.**
   Conformance proves every *citation* resolves; it cannot prove an assessment is still true.
   A `debt` entry can go stale within days of being written.
2. **This page's own prose beyond what `readme_conformance.py` names.** The counts, the file
   inventory, and the machinery section's named schedule IDs and cadences are gated — a schedule
   rename, retirement, or cadence change in `apps/factory-dispatcher/schedule_runtime.py` fails
   CI, and so does a schedule declared there but named in neither the machinery section nor the
   list below.

   Schedules `schedule_runtime.py` declares that this page does not name above, and why:
   - `factory-verdict-staleness-nightly` — dispatcher's own operational report, not EA-model machinery
   - `factory-release-apply-15m` — release-charter machinery, not this EA-model page
   - `factory-requirements-apply-15m` — requirements-registry machinery, not this EA-model page
   - `factory-release-status-nightly` — dispatcher's own operational report, not EA-model machinery
   - `factory-worker-revision-drift-15m` — dispatcher operational health check, not EA-model machinery
   - `factory-doctrine-staleness-nightly` — doctrine staleness report, not EA-model machinery
   - `factory-capacity-resume-probe-15m` — dispatcher capacity-pause operations, not EA-model machinery
   - `factory-cluster-health-15m` — dispatcher cluster health check, not EA-model machinery
   - `factory-change-apply-15m` — the `arch.change` reconciler, not this page's machinery section

   Everything else in this page's prose — the free-text description of what each schedule does —
   stays a judgment call, corrected by hand, same as `debt` above.
