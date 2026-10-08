# Enterprise Architecture Metamodel — CSDM on Beads

**Status:** ADOPTED, working document
**Framework:** ServiceNow Common Service Data Model (CSDM) — Foundation + Design domains
**Storage:** the Bead Substrate. Architecture objects are beads, exactly like `dev.task` and a
vertical's domain beads.

---

## 0. Why this exists

A platform can have an excellent *target* architecture (`/ARCHITECTURE.md`) and a careful
current-state record and still have no **portfolio**: no single object per thing-that-exists,
carrying its own lifecycle, its own owner, its own disposition, and its own relationships to the
outcome it serves.

Without that, three questions cannot be answered mechanically:

1. **What does this application exist to do?** — capability traceability
2. **What breaks if this dies?** — impact analysis
3. **Should this still exist?** — rationalisation

This metamodel is the contract for answering all three. It is deliberately small. CSDM's
crawl/walk/run ordering puts **Foundation** and **Design** first for exactly this reason: they
are the two domains that make Application Portfolio Management possible, and they are the two
that a single-operator estate can actually populate and keep true.

**Non-goal for Stage 1:** Manage Technical Services as a hand-authored domain (CI-level modelling
of pods, nodes, and databases) and Sell/Consume (service offerings, entitlements, subscriptions).
Those domains are reserved, not rejected — see §7.

---

## 1. The dual-layer framing

The model has two enterprises stacked on each other. This is not decoration; it is the thing
that makes "does this add value?" answerable.

| | Demand side | Supply side |
|---|---|---|
| **Enterprise** | The operator's domain | The agentic platform |
| **Capabilities** | What the domain needs done | What the platform can do |
| **CSDM analogue** | Business Capability (Foundation) | Business Capability, but IT-internal |
| **Success measure** | Outcome in the domain | Enablement of a demand-side capability |

The demand side is the people and outcomes the platform serves — for the LifeOps example: money,
home, knowledge, connectivity. The supply side is what the platform can do: persist work, execute
durably, expose capability, deliver software.

Every supply-side capability **must** trace to at least one demand-side capability, directly or
through another supply-side capability. A platform capability with no path to the demand side is
either infrastructure-of-infrastructure (legitimate, but must say what it underpins) or it is a
rationalisation candidate. That single rule is the engine of the whole exercise.

---

## 2. Object types (Stage 1 scope)

CSDM object names on the left; the bead type that instantiates them on the right. All live in
the **`arch`** namespace (see §8.1).

| CSDM object | Domain | Bead type | Stage |
|---|---|---|---|
| Business Capability | Foundation | `arch.capability` | **1** |
| Business Application | Design | `arch.application` | 2 |
| Application Service | Design | `arch.service` | 2 |
| Information Object | Design | `arch.information_object` | 2 |
| Observation | Design support | `arch.observation` | 2 |
| Requirement Conformance | Design support | `arch.requirement_conformance` | 2 |
| Person / Contact | Foundation | `arch.person` | 3 |
| Vendor | Foundation | `arch.vendor` | 3 |
| Location | Foundation | `arch.location` | deferred |
| Technical Service Offering | Manage Tech | `arch.technical_service` | reserved |
| Service Offering | Sell/Consume | `arch.offering` | reserved |

> **Location is deferred deliberately.** CSDM Foundation includes it, but a single-site estate
> has nothing for it to distinguish. Modelling Location before it distinguishes anything is
> ceremony. It enters when a second site does.

> **`arch.requirement_conformance`.** A dated verdict against one `arch.requirement` acceptance
> criterion — written only by `scripts/requirements-load.py`, mirroring
> `docs/requirements/*.json`'s per-criterion `conformance`/`verdict`/`measured_at`.
> `arch.observation` cannot hold this: it is closed (`extra="forbid"`) on `observed_at` and
> `workload`, neither of which a requirement measurement has. This is the destination both
> `ArchRequirementContent`'s docstring and `_RELEASE_MEASUREMENT_KEYS` in
> `apps/substrate/src/schemas.py` name for a dated conformance measurement. `source_class` is
> locked to `derived`, following `arch.ci`'s precedent (§7): the record is mechanically mirrored
> from a reviewed, git-authored registry, never hand-authored in the substrate. See
> `apps/substrate/src/schemas.py:ArchRequirementConformanceContent`.

---

## 3. Common attributes

Every architecture bead carries these, over and above the substrate's own columns.

| Attribute | Where | Notes |
|---|---|---|
| `ref` | `content.ref` | **Stable business key**, see below |
| `name` | `content.name` | Short display name |
| `description` | `content.description` | One sentence. What it is, not how it works. |
| `layer` | `content.layer` | `demand` \| `supply` |
| `owner` | `content.owner` | Accountable party, see below |
| `evidence` | `content.evidence` | **Required.** What substantiates the stated state, see below |
| `assessed_at` | `content.assessed_at` | ISO date of the last human/agent verification |

`ref` is human-readable, immutable, and unique within a type (e.g. `bc.finance.reconciliation`).
It is what relationships point at, not UUIDs — so the model is diffable in git and survives a
substrate rebuild. `owner` is one identity throughout a single-operator estate; it is kept
explicit because that stops being trivially true the moment a second person or an autonomous
agent owns something. `evidence` is a list of repo paths, PR numbers, or doc anchors that
substantiate the object's stated state: an architecture record with no evidence is an intention,
and intentions rot.

`bead.created_by`, `bead.provenance`, and `bead.updated_at` come free from the substrate and are
not duplicated into content.

---

## 4. Lifecycle — `bead.state` per type

This is the "digital record for the lifecycle of object types". The substrate does not constrain
`state` (it is a free-text column), so these are conventions enforced by the content validator
and by the console, in the same way `DEV_TASK_STATES` is a convention rather than a DB check.

### 4.1 `arch.capability`

```
proposed ──► active ──► deprecated ──► retired
    │                        ▲
    └────────────────────────┘   (abandoned before going active)
```

| State | Means |
|---|---|
| `proposed` | Named as needed; nothing realises it yet |
| `active` | At least one application realises it and it is in use |
| `deprecated` | Still realised, but we intend to stop |
| `retired` | No longer needed; kept for history |

A capability in `proposed` with a realising application in `operate` is a **modelling error** —
the capability is active. A capability in `active` with **no** realising application is the more
interesting case: it is an unmet need, and it is the primary input to the roadmap.

### 4.2 `arch.application`

ServiceNow APM lifecycle stages, unmodified:

```
plan ──► build ──► operate ──► retire ──► eol
```

Plus the APM **TIME** disposition, carried in content, reassessed on a cadence:

| `content.time_disposition` | Means | Action |
|---|---|---|
| `invest` | High value, healthy | Fund it |
| `tolerate` | Healthy but low value, or valuable but constrained | Leave alone; do not extend |
| `migrate` | Valuable, unhealthy | Re-platform or rewrite |
| `eliminate` | Low value and unhealthy | Kill it |

Supporting attributes: `business_criticality` (`critical`/`high`/`medium`/`low`),
`technical_health` (`healthy`/`degraded`/`at_risk`), `business_value` (`high`/`medium`/`low`).
TIME is *derived* from value × health — recording the inputs alongside the verdict is what makes
the verdict arguable rather than decreed.

Runtime footprint is carried in `content.workload`. The field is required because "no declared
workload" is unassessed, not evidence that nothing runs.

| Runtime | Required shape | Meaning |
|---|---|---|
| `kubernetes` | `objects[]`, see below | Represented by Kubernetes API objects |
| `external` | `binding` or `note`, see below | Runs outside Kubernetes |
| `none` | `note` | Assessed and found to run nowhere |

A `kubernetes` object carries `cluster`, `namespace`, `kind`, `name`, `manifest` and
`managed_by`; `manifest` is a repo path when the object is repo-managed, and `managed_by` names
the mechanism. An `external` runtime carries a `binding` when it is structured and locally
checkable, or a `note` when it has no repo-checkable binding; the reflector must not infer health
from absent Kubernetes objects.

The first structured external binding is Docker Compose:

```yaml
workload:
  runtime: external
  binding:
    host: edge-host
    compose_path: deploy/compose/edge-host/docker-compose.dns.yml
    service: dns
```

`host` is the machine on which the external runtime is declared to run. `compose_path` is a repo
path and is mechanically checked for existence. `service` is the Compose service name. This is a
declaration, not live verification: the reflector reports it distinctly as
`external-declared` / declared-but-not-live-verified and does not attempt SSH, Docker, or remote
execution from the machine that can run `kubectl`.

---

## 5. Relationships

### 5.1 Two shapes, one graph

An EA model is a **graph**. The substrate's `bead.parent_id` is a single nullable self-FK
([`models.py`](../../apps/substrate/src/models.py)) — a tree — and `bead_link` is the typed edge
table beside it. Relationships are therefore modelled in two places: decomposition where the tree
is genuinely good at it, and everything else as typed links.

### 5.2 Decomposition uses `parent_id` — this is a real tree

Capability decomposition (L1 → L2 → L3) is strictly hierarchical: a sub-capability has exactly
one parent. This maps onto `parent_id` natively, and `GET /beads?type=capability&parent_id=X`
returns the children in one call. **Use it.** No interim, no compromise.

### 5.3 Authoring format: `ref` arrays in content

Typed edges are *authored* as arrays of `ref` strings on the source object, because that is what
reviews well in a PR:

```yaml
content:
  ref: app.substrate
  realizes: [pc.substrate.persistence, pc.substrate.provenance]   # application → capability
  depends_on: [app.temporal-postgres]                             # application → application
  supports: [bc.finance.visibility]                               # supply capability → demand capability
```

| Edge | CSDM verb | Source type | Target type |
|---|---|---|---|
| `realizes` | *Business Application supports Business Capability* | `application` | `capability` |
| `supports` | *capability enables capability* | `capability` | `capability` |
| `consumes` | *Business Application consumes Application Service* | `application` | `service` |
| `depends_on` | *Depends on :: Used by* | `application`/`service` | `application`/`service` |
| `produces` / `reads` | *CI ↔ Information Object* | `application` | `information_object` |
| `measures` | *Metric supports portfolio assessment* | `observation` | `application` |

The loader resolves those arrays into real edges. The resolved form is a queryable table with
referential integrity, never a copy of the array left in `content`: application-level encryption
encrypts every leaf value in `content`, so a `ref` stored there is **ciphertext at rest** and could
never become a server-side query. The link table's semantic columns are plaintext by design.

For the current edge vocabulary — which has grown past this section's authoring list — see the
canonical table in [`bead-object-inventory.md`](bead-object-inventory.md#edge-vocabulary) rather
than restating it here.

### 5.4 The link table

`bead_link` (`source_id`, `target_id`, `link_type`, `content`) plus `POST /beads/{id}/links`,
`GET /beads/{id}/links?direction=` and `DELETE /links/{id}`. This **implements** the `link`
method `/ARCHITECTURE.md` §3 lists among the canonical substrate API methods rather than amending
it, so it needed no architectural amendment — only a migration and a router.

The business and application layers are tens of objects, where a content array would be fine and
a link table premature. The technology layer is hundreds of CIs with dense fan-out, where impact
analysis is the entire point and client-side joins over `list_beads` stop being viable. Paying for
the link table at that boundary keeps the cheap layers cheap and the expensive layer right the
first time.

---

## 6. Where the model lives, and which copy wins

```
<operator model>/*.yaml             ← source of truth, reviewed in PRs
              │
              │  loader
              ▼
    substrate    arch.*    beads   ← queryable, lifecycle-tracked, console-visible
              │
              ▼
    Console / EA views
```

**Git is authoritative for structure; the substrate is authoritative for lifecycle.**

That split is deliberate. Adding a capability or rewiring a relationship is an architectural
change and belongs in a reviewed PR — the same no-drift discipline `/ARCHITECTURE.md` §0 applies
to itself. But *state* changes (a capability going `active` because something finally realises
it; an application moving `build → operate`) happen continuously and are exactly what a bead's
event log is for. Reconciling YAML into the substrate is idempotent on `content.ref`: structure is
overwritten from git, `state` is left alone unless the YAML explicitly asserts it.

This also keeps the model honest under agent authorship. A worker that decides an application is
now `eliminate` has to open a PR to say so.

---

## 7. Reserved, and why

- **Manage Technical Services** (CIs, technical service offerings) as hand-authored objects —
  hundreds of objects with no discovery source; hand-maintained CI data is wrong within a month.
  Enters when something can *populate* it automatically (kubectl/ArgoCD introspection) and
  `bead_link` exists.
- **Sell/Consume** (service offerings, entitlements, SLAs) — one consumer, no chargeback, no
  contracts; the domain models a commercial relationship that does not exist here. Enters when
  multi-tenancy gives distinct consumers with distinct entitlements.
- **Location** — one site. Enters when a second site is load-bearing; an off-site runner dead-man
  already exists, so this is closer than it looks.
- **Vendor** — real and small: the edge/DNS provider, the bank-data aggregator, the model
  providers, the secrets manager, the forge. Enters at Stage 3; it is the natural home for spend
  and for third-party concentration risk.

> **Manage Technical Services is open, populated by observation only.** The trigger the row
> names — something that can populate the domain automatically — is met. CI-level state is
> recorded, but only as `arch.ci` and `arch.observation` beads written by an automated reflector
> (kubectl/ArgoCD introspection); no hand-authored or PR-reviewed CI object is in scope.
> `arch.technical_service` itself (§2) stays `reserved` until something writes it structurally,
> not merely observes against it.

---

## 8. Recorded decisions

### 8.1 Namespace — `arch`, not `platform`

The EA model lives in its own `arch` namespace with a dedicated entry in
`NAMESPACE_TYPE_SCHEMAS`. `platform.*` stays reserved for runtime self-modelling
(`platform.mcp.capability`, `platform.workflow.registered` per `/ARCHITECTURE.md` §3).

*Rationale:* `platform.capability` (a business capability, hand-authored, quarterly cadence) and
`platform.mcp.capability` (a registered MCP tool, machine-authored, per-deploy cadence) are two
different ideas one glance apart in every query, filter, and console view. Separate namespaces
also give the two models independent content-validation registries, which matters because one is
reviewed in PRs and the other is written by running code.

This is a deviation from the literal wording of the pillar that says the platform's own
architecture lives as beads, but not from its intent — the architecture is still beads, in a
namespace of its own.

### 8.2 Beads are the CMDB

The substrate is the single configuration/architecture record, queryable by agents and rendered
by the console the platform already owns. No external CMDB product sits beside it.

*Rationale:* two CMDBs is a rationalisation finding, and picking the external one would put the
architecture record outside the substrate — directly against the pillar that no piece of code
owns the system; the bead store is the system.

The standing lesson for any teardown this implies: de-managing a workload without tearing it down
creates a GitOps orphan that is worse than either keeping or killing it.

### 8.3 `bead_link` lands before the technology layer

Confirmed as §5.4 describes. The link table and `/beads/{id}/links` land before any CI-level
modelling begins.

### 8.4 The foundation is a governance grouping, not a Business Service

The platform foundation — `app.substrate` and `app.temporal` — is governed as **one unit**. The
obvious CSDM expression is a Business Service, and that is deliberately *not* what this model
does.

Business Service lives in **Sell/Consume**, which §7 reserves with a specific reason: "One
consumer, no chargeback, no contracts. The domain models a commercial relationship that does not
exist here." Creating one Business Service to express a governance boundary would open that
domain for a single object and re-import the ceremony §7 exists to keep out — the same trade the
model already refused for Location.

So the grouping is carried as a `FOUNDATION` clause in both applications' `note`, and enforced
where it actually bites: in `scripts/ea-conformance.py` and in the governing amendment itself.
The rule is about *how changes are reviewed*, and a YAML object would not have enforced that.

**This is a deliberate under-modelling with a known exit.** When `arch.service` (Application
Service — already sanctioned in §2, unlike Business Service) carries the foundation as a real
object with `app.substrate` and `app.temporal` as its components, these notes collapse into it.
Until then, the note is the honest representation: a decision recorded where it is enforced
rather than an object that implies a domain the model has not opened.

### 8.5 `source_class` is a governed epistemic class

Every `arch.*` bead carries a `source_class` — `authored` (hand-written in a reviewed file),
`derived` (mechanically mirrored or computed by an enrolled writer), or `observed` (read from a
live system by the observer). The class a writer may assert is declared per writer in
`apps/substrate/src/bead_rules.py`; a writer asserting a class it is not enrolled for is rejected.
A loader that mirrors human-authored, source-controlled files is deliberately *not* enrolled as an
automated writer (`apps/substrate/src/schemas.py`, `is_automated_writer`): it carries authored
facts, not a reconciler's derivation. A retroactive relabelling of production rows in a governed
class is an operator decision, taken on a committed snapshot, never a side effect of a loader run.
