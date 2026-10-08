# Truline — Architecture (Binding)

**A true line from north star to running system.**

This is the architectural source of truth for Truline: an ITSM, SDLC and EA platform on a typed
bead graph, with Temporal as durable execution, a factory that turns work records into reviewed
pull requests, and a capability gateway that exposes the whole thing to people, scripts and agents
under one identity contract. The process that operates it is in
[`docs/architecture/agentic-operating-model.md`](docs/architecture/agentic-operating-model.md);
the service model it carries is in
[`docs/architecture/ea-metamodel.md`](docs/architecture/ea-metamodel.md). When those documents
disagree with this one about the platform, this one wins.

---

## 0. Governance — read this first

### No-drift rule

**No implementer, human or agent, deviates from this document while implementing.** If a task
implies a change to the pillars in §2, the components in §3, a vertical boundary, a namespace
scheme or the task-queue topology, the implementer MUST stop, surface the conflict, and hand it to
the operator. An approved change lands here as an amendment (§7) before any code that depends on it
is merged. The reviewer of an amendment is the operator or the outer loop, never the loop that
proposed the code.

Allowed without an amendment:

- Implementation detail inside a component this document already names, where the detail changes
  no pillar and no boundary; naming and refactoring within a component.
- Operational detail (logging, retries, caches, backups) that introduces no new component.
- Adding a bead type, an edge type or a state machine to a namespace the document already
  recognises, through the registries in §3.1.

Requires an amendment:

- Adding a top-level component: anything that would appear in §3.
- Changing a vertical boundary, the namespace scheme, or the Temporal task-queue topology.
- Replacing or substituting a named technology (Postgres, Temporal, FastAPI, the MCP protocol).
- Changing a design pillar in §2.
- Changing the release gate: what it may read, write or refuse is the platform's risk model.
- Changing a security property: the identity contract, the encryption model, the worker
  containment profile, or the separability check.

A `PROPOSED` amendment binds nothing. A control an amendment proposes to retire is in force until
that amendment is `RATIFIED`. An amendment left `PROPOSED` for longer than four weeks is withdrawn
by the outer loop with a note.

### Document conventions

- **MUST** / **MUST NOT** — binding. No drift.
- **SHOULD** / **SHOULD NOT** — strong default; a deviation carries a written rationale in the PR.
- **MAY** — implementer judgment.
- "The operator" is whoever runs a Truline deployment and owns its intent. "The outer loop" is
  the operator's attended session acting as product owner and site reliability engineer. "The
  worker" is the dispatched inner-loop agent. "The gate" is the fresh-context release reviewer.
  None of these is a person's name, and this document never uses one.

---

## 1. The problem

An organisation that builds software holds two models of itself that are almost never the same
object. One is the **service model**: what capabilities exist, which applications realise them,
which services those depend on, what information they hold, and what has happened to each of them
(changes, incidents, risks). The other is the **delivery model**: the tasks, designs, findings,
pull requests and releases through which the software changes. The first lives in a CMDB or an
architecture tool; the second lives in a tracker and a git host. Each is maintained by people who
rarely open the other.

The cost shows up at the seams. A change record exists only if someone writes one after the merge.
An incident cites an application whose dependencies are a diagram from a year ago. A coding agent
opens a pull request that is correct and nobody can say which capability it serves or which suites
it obliges. Every one of these is the same defect: the record of what is intended, what is built
and what is running are three stores, and nothing fails when they disagree.

Truline makes them one graph. A capability, an application, a requirement, a release, a change and
an incident are typed records in one store. A task, a design, a finding and a release verdict are
typed records in the same store, linked by a closed edge vocabulary to the requirement they serve
and the application they touch. Durable workflows move the records through governed lifecycles,
and the build fails the moment the model, the code and the running system contradict each other.
A coding agent is then one more writer of records, under the same gate as a person.

The platform is deliberately the graph plus the machinery that keeps it true, and nothing else. A
domain built on it — a personal-finance system, a home platform, a product team's SDLC — is a
vertical: a namespace, its types, its workflows and its capabilities, with no change to the core.

---

## 2. Design pillars

These are non-negotiable. Every PR is judged against them.

1. **Substrate over orchestrator.** No piece of code owns the system. The bead store is the
   system; everything else is a producer or a consumer of beads. What the store carries is an
   ITSM/SDLC/EA model: capabilities, applications, services and observations; tasks, designs and
   findings; changes, incidents, risks and releases. Any domain is a vertical on that model.
2. **Beads are typed, namespaced and lineaged.** A bead is a durable, structured unit of work or
   knowledge with provenance, state, a parent, typed edges, a confidence and a trust tier. Views
   over beads (a board, a dashboard, a chat surface) are projections; the bead exists whether or
   not any projection is up.
3. **Temporal is the durable execution layer, platform-wide.** Anything that must survive a
   process restart, retry on failure, or run on a schedule is a Temporal workflow over beads. The
   platform MUST NOT grow a second scheduler or a second retry mechanism.
4. **Verticals are plug-ins, not forks.** A new domain is a new namespace, new content schemas,
   new workflows and new gateway capabilities. It requires no change to the core, and the core
   MUST NOT import a vertical's module to function.
5. **Interactive and autonomous agency are siblings.** An interactive session serves a person who
   is waiting; an autonomous workflow serves a fact, an event or a schedule. They share one
   substrate and one set of bead contracts, and either MAY hand work to the other through it.
6. **Reversibility is first-class.** Every bead transition is appended to an event log and is
   auditable and replayable. Trust at the system level comes from undo at the action level, and a
   change that cannot be reverted is a risk record, not a routine one.
7. **The service model is binding, not advisory.** The EA model in git is validated on every PR
   and reconciled into the store on a schedule. A claim in the model that the repository or the
   running system contradicts fails the build.
8. **Tidy before extending.** Structural and behavioural changes never share a PR, and a pile of
   half-finished code is culled before anything is added to it.
9. **Avoid distributed systems not yet needed.** The simplest substrate that satisfies a pillar at
   actual usage wins: Postgres `NOTIFY` before a message bus, a single node before a cluster, a
   schedule before a subscriber.
10. **Intent and execution are separate stores.** A bead records what is wanted and what resulted.
    Temporal records what ran, how many times, and with what outcome. Attempt counts, lease
    holders, timeouts and retry bookkeeping MUST NOT be written into bead content; a store that
    holds both ends up with an advisory state machine and a control flow that is the real one.
    `scripts/ea-conformance.py` enforces this.

---

## 3. Target architecture

```
            ┌──────────────────────────────────────────────────┐
            │ IDENTITY AT THE EDGE: access assertion or token  │
            └───────────────────────┬──────────────────────────┘
   interactive sessions,            │
   console (client #1),             ▼
   scripts, phone, MCP ──▶ ┌─────────────────────────┐
                           │  CAPABILITY GATEWAY     │  X-Truline-* identity contract
                           │  apps/mcp-hub           │  REST/OpenAPI + MCP
                           └────┬───────────────┬────┘
                                │               │
                 ┌──────────────▼───┐    ┌──────▼─────────────────────┐
                 │ BEAD SUBSTRATE   │◀──▶│ TEMPORAL                   │
                 │ apps/substrate   │    │ one cluster; task queues   │
                 │ typed, lineaged, │    │ per vertical + environment │
                 │ event-logged     │    └──────┬─────────────────────┘
                 └────────▲─────────┘           │
                          │              ┌──────▼─────────────────────┐
                          │              │ FACTORY DISPATCHER         │
                          │              │ apps/factory-dispatcher    │
                          │              │ intake → isolate → run →   │
                          │              │ contain → verify → PR →    │
                          │              │ gate → merge on verdict    │
                          │              └────────────────────────────┘
            ┌─────────────┴───────────────────────────────────────────┐
            │ EA MODEL LAYER: docs/architecture/ + arch.* beads       │
            │ validated per PR, applied on schedule, observed nightly │
            └─────────────────────────────────────────────────────────┘
```

### 3.1 Bead substrate (`apps/substrate/`)

The spine. One FastAPI service over Postgres.

**The record.** A bead carries `id`, `namespace`, `type`, `state`, an optional `parent_id`,
`context` and `content` (JSON), `confidence` (0 to 1), `trust_tier` (`sensor`, `derived`,
`inferred`, `hypothesis`), `provenance`, `created_by` and timestamps. `bead_event` is the
append-only transition log; `bead_link` is the typed edge table (`source_id`, `target_id`,
`link_type`, unique on the triple, no self-edges). The models are in `apps/substrate/src/models.py`.

**Namespaces.** A namespace is a vertical's boundary inside the store. The core recognises `dev`
(artifacts passing through the SDLC: *how a change came to be*) and `arch` (the service model and
the ITSM records that move it: *what exists and what happened to it*). A vertical registers its
own namespace, routers and integrity-error mappers through
`apps/substrate/src/namespace_registry.py`; the core defines no vertical of its own.

**Typed content.** Every `(namespace, type)` pair MAY register a pydantic schema in
`apps/substrate/src/schemas.py` (`NAMESPACE_TYPE_SCHEMAS`). A write whose content fails the schema
is refused with field-level errors. Unregistered pairs pass through, which is how a vertical adds
types without a core change. The registered `dev.*` types are the factory's operational contract:

- `dev.task` — a work specification: `lane` (`code-health`, `drift`, `bug-triage`, `feature`),
  `title`, `intent`, `acceptance[]` (at least one), `verification.commands[]`, `scope.paths[]`,
  `scope.forbidden_paths[]` (MUST include `.github/workflows/**`: the factory may not write the
  gates that judge it), `risk_class` (`structural` or `behavioral`), a `budget` in minutes,
  dollars and tokens, traceability (`requirement_refs[]`, `nfrs[]`, `arch_impact`), release
  binding (`outcome_ref`, or a written `release_ref_waived`), `worker_hint`, `autonomy`
  (`propose` by default) and `pr_refs[]`.
- `dev.note` — `kind` is `comment`, `question`, `answer`, `status`, `attachment` or `review`;
  a `review` note carries a `verdict`, a `question` may be `blocking`.
- `dev.design` — `decision`, `rationale`, `alternatives_rejected[]`, derived NFRs, `arch_impact`.
- `dev.release` — what the gate required, ran and concluded: `reviewer`, `verdict` (`merge`,
  `merge-with-changes`, `do-not-merge`), `pr_refs[]`, `required_suites[]`, `results[]` with
  category, outcome and evidence, `merge_order[]`, `unverified_claims[]`, `not_reviewed[]`. A
  `merge` verdict rejects required suites that are missing, `not-run` or `fail`.
- `dev.finding` — `kind` (`bug`, `enhancement`, `security`), `disposition`, `severity`,
  `summary`; a `blocking` finding MUST carry `evidence`.

The `arch.*` types are in §3.5.

**Provenance.** A bead written by a recognised automated writer MUST carry a complete
`BeadProvenance` (`worker`, `model`, `prompt_ref`, `tokens`, `cost_usd`, `duration_s`, no extra
keys) or it is refused; a partial record is not provenance. `source_class` (`authored`, `derived`,
`observed`) declares how a model record came to be, and `SOURCE_CLASS_WRITERS` in
`apps/substrate/src/bead_rules.py` says which writers may claim each class: a reflector writes
`observed`, a loader writes `derived`, and only a non-automated writer may write `authored`.

**Governed state machines.** `apps/substrate/src/bead_rules.py` declares, per `(namespace, type)`,
the legal transitions and the entry state. A transition the machine does not declare is refused by
`POST /beads/{id}/transition`:

| Type | Entry | Other states |
|---|---|---|
| `dev.task` | `pending` | `doing`, `review`, `done`, `failed`, `archived`, `superseded` |
| `dev.finding` | `pending` | `backlogged`, `ruled`, `already_fixed`, `not_a_defect` (terminal) |
| `dev.release` | `verdict_recorded` | none: a verdict is a fact, not a workflow |
| `arch.release` | `planned` | `in_flight`, `closing`, `released`, `abandoned` |
| `arch.incident` | `detected` | `mitigating`, `resolved`, `closed` |
| `arch.risk` | `accepted` | `under_review`, `retired` |
| `arch.release_health` | `unmeasured` | `on_track`, `drifting`, `breached`, `accepted`, `closed` |

This module imports neither FastAPI nor the database, so the dispatcher, the scripts and any
client depend on the same rules the HTTP surface enforces rather than on a copy.

**Edge vocabulary.** `link_type` is a free column with a uniqueness constraint; `BEAD_LINK_TYPES`
closes it to nineteen admitted types, and a test keeps the set equal to the canonical table in
[`docs/architecture/bead-object-inventory.md`](docs/architecture/bead-object-inventory.md).
Within the SDLC: `dev.design --designs--> dev.task`, `dev.task --supersedes--> dev.task`,
`dev.release --gates--> dev.task`, `dev.finding --regresses--> dev.task`,
`dev.finding --found_by--> dev.release`, `dev.task --derived_from--> dev.finding`.
From the SDLC into the model: `dev.task --delivers--> arch.release`,
`dev.design --applies--> arch.principle`, `arch.principle --derived_from--> dev.task`,
`arch.principle --enforced_by--> dev.task`, `arch.incident --caused_by--> dev.task`.
Within the model: `arch.change --affects--> arch.application`,
`arch.incident --resolved_by--> arch.change`, `arch.risk --threatens--> arch.application`,
`arch.risk --accepted_by--> arch.change`, `arch.observation --measures--> arch.application`,
`arch.capability --supports--> arch.capability`, `arch.application --realizes--> arch.capability`,
`arch.application --depends_on--> arch.application | arch.ci`,
`arch.application --consumes--> arch.service`. An edge with no writer is speculative, not
admitted; adding one is a PR against both the inventory and `BEAD_LINK_TYPES`.

**Event log.** Every create, patch and transition appends a `bead_event`; `GET /beads/{id}/events`
reads them. Postgres `NOTIFY` on bead writes feeds exactly one consumer, the vector listener
(`apps/substrate/src/vector_listener.py`) that keeps semantic search current. Agent-facing eventing
is Temporal schedules and signals, not database subscribers; a channel topology enters only by an
amendment that names its consumer.

**Encryption.** Bead `content` and `context` are encrypted at the application layer
(`apps/substrate/src/crypto.py`): leaf values are Fernet-encrypted under `SUBSTRATE_ENCRYPTION_KEY`
before they reach Postgres, the JSON structure stays in plaintext so path lookups work, and
identifier-shaped keys a namespace declares as `plaintext_keys` stay queryable. The core encrypts
no namespace of its own; a vertical opts in through `register_encrypted_namespace`, and in doing so
gives up server-side filtering on that namespace's content. The semantic columns of `bead_link` are
plaintext by design, which is why relationships live in the edge table and not in content. The
service refuses to start without a valid key.

**API.** REST, in `apps/substrate/src/routes.py` and `apps/substrate/openapi.json`: create, patch,
transition, list, get, events, links, search, delete. Writes require `X-API-Key`; an additive
read-only key serves clients that only read. The substrate API is internal to the platform: public
clients consume the gateway (§3.4), not the store. `apps/substrate/client/` is the one Python
client, and every platform process that talks to the store uses it; a second client is drift.
`apps/substrate/publish/csdm-on-beads/` is the service-model layer (schemas, rules, conformance
checker) as a dependency-free package.

### 3.2 Temporal — durable execution

- **Topology.** One Temporal cluster. **Task queues are per vertical and environment, not per
  workflow**: the factory polls `factory-dispatcher-<env>`, the gateway's worker polls its own
  queue. A worker registers the workflows and activities of its vertical on its queue.
- **Workflows are pure functions over beads.** Input: bead ids. Output: bead transitions and new
  beads, written through activities. No workflow reads outside data directly.
- **Schedules are the event model.** The dispatcher registers its schedules at startup with
  `ScheduleOverlapPolicy.SKIP`, so a still-running execution causes the next trigger to be skipped
  rather than overlapped. The registry is `apps/factory-dispatcher/schedule_runtime.py`: dispatch
  on an interval; EA apply, release apply, requirements apply, change apply, cluster health,
  worker-revision drift and the capacity-resume probe every fifteen minutes; EA observation,
  release status, verdict staleness and doctrine staleness nightly. A schedule the registry
  declares and the architecture README does not name fails CI.
- **Pillar 10 applies here.** Retry bounds, heartbeats, timeouts and leases are Temporal's. A
  workflow that writes an attempt counter into bead content is a conformance violation.
- **Two senses of "namespace".** A Temporal namespace groups workflow registries; a bead
  namespace bounds a vertical in the store. A vertical that owns workflows SHOULD use the same name
  for both. A Temporal namespace is created only when it buys a distinct visibility surface, a
  distinct schedule owner and a distinct worker pool, all three.

### 3.3 The factory dispatcher (`apps/factory-dispatcher/`)

The SDLC vertical: a thin Temporal workflow and activity set that turns a `dev.task` bead into a
reviewed pull request. It is generic dispatch; SDLC roles are personas loaded at runtime, not
components. Its admission test is dependency-shaped: every module depends on the core, modules do
not hidden-couple through it, and anything embedded in it raises its change cost.

**Intake.** `file_task.py` files a `dev.task` from a JSON spec against the contract in
`contracts/task-intake-contract.json`: required fields, the always-forbidden paths
(`.github/workflows/**`, `docs/releases/**`), and the admitted lanes. Filing refuses a spec that
cites no resolvable requirement, and one that names neither a release outcome nor a written waiver.
`file_finding.py` files a `dev.finding` and transitions it to its disposition in one call, reporting
a partial write rather than hiding it. An operator-filed observation becomes a `dev.finding` first;
the task that absorbs it carries a `derived_from` edge back.

**Dispatch** (`workflows/dispatch_task.py`, `activities/dispatch_steps.py`, `dispatch.py`,
`guards.py`), one task per execution:

| Step | What it does |
|---|---|
| claim | `pending → doing` by compare-and-set, so two dispatchers cannot claim one task |
| isolate | clone into a temporary directory whose `.git` has no path back to the real tree |
| materialise | copy the persona definitions from `docs/agents/` into the clone, in-process |
| run | the worker named by `worker_hint` (or the registry default), bounded by the task budget |
| contain | verify the real working tree is byte-identical to before the run |
| scope | verify the diff stayed inside `scope.paths` and clear of `forbidden_paths` |
| smoke | byte-compile changed Python before spending time on declared checks |
| verify | bootstrap a clone-local environment and run the task's declared commands |
| propose | branch, push, open the PR with the bead id and `Change kind:` in its body; `doing → review` |

A failed run is a `Run failed:` status note on the bead. Temporal owns the retry bound; when it is
spent the task is `failed`. **The dispatcher never merges**: every run ends at a pull request
regardless of `autonomy`.

**Containment** (`containment.py`). The worker runs under an OS-level deny-write profile scoped to
the isolated clone; a write outside it fails at the kernel regardless of what the worker reports.
The before/after fingerprint of the real tree is defence in depth, checked before the diff is read:
an uncontained pass is not a pass. Scope is verified, not enforced: the allow-matcher fails closed
and the forbid-matcher over-matches, both toward refusal. Nothing trusts the worker's own report;
declared verification runs after the worker exits and before a PR opens. Every child the dispatcher
spawns receives an explicit, credential-free environment, and dispatcher git calls in a worker tree
run with hooks and repository config disabled. Workers are entries in `WORKER_REGISTRY` with
allowed lanes, per-worker allowances and a quarantine flag; an unknown, quarantined or
lane-forbidden worker is refused before claim or clone.

**Doctrine at dispatch** (`doctrine.py`). A lane declares the principle ids relevant to its work;
the dispatcher embeds the `adopted` and `enforced` statements from
[`docs/architecture/principles.md`](docs/architecture/principles.md) in the worker prompt, offline.
A `proposed` principle is a conjecture and is never injected.

**Scanner** (`scanner.py`). Lane scanners report what they would file and file nothing; the
ability to write is absent from the scan path rather than defaulted off. Candidates are assessed
through the real filing path, so a reported candidate is one filing would accept; a candidate whose
every path is forbidden is reported as suppressed, not as fileable. Scanner-filed tasks take a
release waiver by default, so an automated filer surfaces a triage queue rather than filling one.

**Release gate.** The verdict on a PR is written by a **fresh-context gate agent**: spawned with no
conversational history, given the diff, the bead, the checkout and the house review lessons,
chartered adversarially, and denied write, merge and network actions. It runs as one pass with four
hats (release manager, QA, configuration management, enterprise architect). Binding on every
dispatch: the gate is read-only, with `scripts/gate-verify.py` routing each verification command
through a profile that denies writes outside scratch and denies network; the gate's environment
carries no credential it is not asked to judge; a claim the gate cannot verify is recorded as
unverified, never silently accepted. The verdict is a bare `Release-gate: MERGE`,
`Release-gate: MERGE-WITH-CHANGES` or `Release-gate: DO-NOT-MERGE` line on the PR, optionally
followed by `Release-gate-revision: <sha>`; `scripts/gate_markers.py` is the one recogniser, and
`scripts/gate-prepass.py` and `scripts/release-manifest.py` assemble the gate's inputs, including
the originating bead and its subgraph. A verdict in a dispatcher-shaped PR body is never read. The
residual risk — a gate and a worker sharing one model lineage can share a blind spot — is named and
mitigated structurally: the gate cannot merge and the merging loop cannot self-verdict.

**Merge on verdict.** The outer loop executes merges through `scripts/merge-pr.sh`, which refuses
without a recognised verdict or on `DO-NOT-MERGE`, never passes `--delete-branch`, and keeps the
PR body in the squash commit so the `Change kind:` declaration survives. Green CI is a
precondition, never a verdict. `MERGE-WITH-CHANGES` means change and re-verify, not merge and
follow up. The tested tree MUST be the merged tree: any rebase or force-push re-verifies.
`workflows/merge_on_verdict.py` is the same rule as a Temporal workflow started by the gateway,
which holds no git credential: one activity, no automatic retry, a queryable disposition. A merged
PR writes the `arch.change` record it is (`workflows/change_apply.py`) and mints a `dev.release`,
so the change record is a consequence of the merge, not a chore after it. `--reconcile-review`
moves a `review` task to `done` only when the PR is merged *and* its merge commit is an ancestor
of `main`; `--report-stuck` is read-only, and recovery is Temporal's.

### 3.4 The capability gateway (`apps/mcp-hub/`)

Every capability is defined once as a typed, versioned FastAPI router under
`src/routers/v1/<capability>` and exposed on two surfaces from the same definition: REST/OpenAPI
(`/api/v1/...`, `openapi.json` regenerated by CI) for code, and MCP (`/mcp`, `src/openapi_app.py`)
for agents. REST and MCP cannot drift into hand-written twins because there is one definition.
Clients include interactive sessions, the console, scripts and a phone.

**Identity contract** (`src/access_auth.py`). Every request resolves to an `AccessIdentity`
(`client`, `client_type`, `email`, `scopes`), and every capability declares the scope it checks.
`MCP_HUB_IDENTITY_MODE` sets how far verification goes:

- `enforce` — an access assertion (a Cloudflare Access JWT) is verified against the issuer's
  JWKS; a service token maps to a named client and its scopes; identity headers without an
  assertion are rejected. Production.
- `log-only` (default) — the same verification; a failure is logged and the request continues
  with the identity it carries; a request with no identity is still refused at the scope check.
- `trust-headers` — `X-Truline-Client`, `X-Truline-Client-Type`, `X-Truline-Scopes` and
  `X-Truline-Email` are taken as given, never verified. Only behind a trusted proxy, or in the
  quickstart.

A verifier outage is `verifier-unavailable`, distinct from `invalid-assertion`; a rollback typo
MUST NOT enforce. Every request logs one structured `mcp_hub_request` line with client, scopes,
capability, scope checked, verdict and outcome: that line is the audit. A boundary spec declares
its principal. Externally sourced text that reaches a model is wrapped by `src/untrusted.py`.

**Boundary.** The gateway is the only process that holds the substrate write key. The console's
route to data is `/substrate/{path}`, a passthrough for identities holding `substrate.proxy`; the
browser never holds a key. Public webhooks that bypass edge identity MUST verify a cryptographic
signature. The gateway starts factory workflows (filing, probing, merge-by-verdict) and holds no
git credential; the activity that needs one runs on the dispatcher's worker.
`src/temporal_worker.py` is the gateway's own Temporal worker for vertical workflows, a separate
process on its own queue.

### 3.5 The EA model layer (`docs/architecture/`, `arch.*`)

The platform models itself, in CSDM terms, as beads. The contract is
[`docs/architecture/ea-metamodel.md`](docs/architecture/ea-metamodel.md); the inventory of what is
live is [`docs/architecture/bead-object-inventory.md`](docs/architecture/bead-object-inventory.md).

**Objects.** Fourteen `arch.*` types are registered (`ARCH_TYPE_SCHEMAS`): `capability`,
`application`, `service`, `information_object`, `observation`, `change`, `ci`, `release`,
`requirement`, `requirement_conformance`, `principle`, `incident`, `risk`, `release_health`. A
configuration item carries `ref` (the stable business key relationships point at), `name`,
`description`, `layer` (`demand` or `supply`), `owner`, `evidence[]` (required: a record with no
evidence is an intention) and `assessed_at`. An application adds its workload, build, health,
value and a time disposition (`invest`, `tolerate`, `migrate`, `eliminate`). Capability
decomposition is a real tree on `parent_id`; every other relationship is an edge (§3.1).

**Two layers.** Every supply-side capability MUST trace to a demand-side capability, directly or
through another. A platform capability with no path to demand is infrastructure-of-infrastructure
that says what it underpins, or a rationalisation candidate. That one rule is the engine.

**Git is authoritative for structure; the substrate is authoritative for lifecycle.** The model is
authored as YAML (an operator-owned instance, §6), reviewed by PR, and reconciled into `arch.*`
beads idempotently on `content.ref` by `scripts/ea-load.py` on the fifteen-minute schedule:
structure is overwritten from git, `state` is left alone unless the YAML asserts it. Nothing in
git may declare a release `released`; that is reached by transition. `scripts/ea-derive.py`
derives workload objects and dependency edges from the repository's own manifests; a hand-authored
copy of a derivable fact is a conformance violation. The nightly observer
(`activities/ea_observation.py`, `scripts/ea_reflect.py`) reads the running system, lands one
standing `arch.observation` per divergence from the declared portfolio, and writes the technology
layer as observed-only `arch.ci` records with `depends_on` edges.

**Conformance.** `scripts/ea-conformance.py` runs on every PR: structure and dangling refs,
observations, consistency, roadmap, maturity monotonicity, supports chains, evidence, derived
dependencies, workload, reality against the cluster, Pillar 10, the foundation service, and
freshness. `scripts/ea-coverage.py` ratchets coverage against a committed baseline, and
`scripts/readme_conformance.py` ties the counts the architecture README states to the model files.

**Requirements and releases.** `docs/requirements/*.json` are the registries; a `dev.task`
cites `<REG>-<CAT>-<NNN>` or `<REG>-<CAT>-<NNN>/AC-<n>`, and `scripts/requirements-load.py`
mirrors them to `arch.requirement` and each dated verdict to `arch.requirement_conformance`. A
conformance verdict is a dated snapshot: it is burned down by work, never corrected to match
today's code. `docs/releases/<ref>.json` is a charter: `ref`, `name`, `objective`, the sprints in
the arc, `outcomes[]` (each a statement for the person the release is for, a `work_class` among
`feature`, `enabling`, `blocking`, `risk`, `security`, and the criteria that prove it) and a
`declared_balance`. `scripts/release-load.py` mirrors charters to `arch.release`;
`scripts/release-status.py` reports declared against actual balance and the work bound to no
release; `scripts/release-notes.py` generates the notes from what merged and what is measured.
`arch.release_health` is computed per open release from a closed signal set under the policy in
`docs/releases/policy/health-policy.json`, with the operator's disposition on the bead.

**Doctrine.** Principles are `arch.principle` beads mirrored from
[`docs/architecture/principles.md`](docs/architecture/principles.md) by
`scripts/principles_sync.py`, with a status (`proposed`, `adopted`, `enforced`), a source, and
edges to what enforces them. The mechanism is
[`docs/architecture/doctrine.md`](docs/architecture/doctrine.md); the injection points are the
dispatcher (§3.3) and the `platform-doctrine` skill (§6). Stored knowledge that is not read at
decision time does not operationally exist.

### 3.6 The console (`apps/lifeops-console/`)

Client #1: a React application that serves the bead explorer, the factory board, the release
views, the architecture views and the worked vertical's views. It is a projection and a client of
the gateway, not a privileged path; all its data flows through `/substrate/{path}` with identity
resolved at the edge. A view the console serves is derived from beads and can be rebuilt from them.

### 3.7 Verticals as namespaces

A vertical is a namespace in the store, its content schemas, its encryption opt-in, its Temporal
workflows on its own task queue, its gateway capabilities, and the capabilities it declares in the
EA model. It changes nothing in the core. **LifeOps** is the worked example shipped in this tree:
the `finance` namespace (`apps/substrate/src/finance_schemas.py`, `finance_encryption.py`,
`finance_integrity.py`, registered through the namespace registry), its workflows and tools under
`apps/mcp-hub/src/workflows/` and `apps/mcp-hub/src/tools/`, its routers under
`apps/mcp-hub/src/routers/v1/`, a one-page reporting service in `apps/finance-reporting/`, and its
requirement registry in `docs/requirements/lifeops-requirements.json`. It is an extension, not the
product; removing it leaves the platform whole.

### 3.8 Repository layout

```
/
├── ARCHITECTURE.md              (this file — binding)
├── CLAUDE.md                    (the outer loop's operating file; operator-owned)
├── apps/
│   ├── substrate/               (§3.1 — the bead store; client/; publish/csdm-on-beads/)
│   ├── factory-dispatcher/      (§3.3 — Temporal-owned dispatch, gate tooling, personas)
│   ├── mcp-hub/                 (§3.4 — the capability gateway)
│   ├── lifeops-console/         (§3.6 — client #1)
│   └── finance-reporting/       (§3.7 — the worked vertical's reporting service)
├── scripts/                     (the harness: gate, release, requirements, invariants, EA)
├── docs/
│   ├── architecture/            (§3.5 — metamodel, inventory, doctrine, operating model)
│   ├── requirements/            (registries with dated conformance verdicts)
│   ├── releases/                (charters, notes, the health policy)
│   └── agents/                  (persona definitions materialised into the worker)
└── .claude/skills/              (decision-time injection points)
```

A directory enters this layout when its first PR ships, not before. Deployment manifests are not
part of the platform: what a deployment supplies is configuration (`SUBSTRATE_URL`,
`SUBSTRATE_API_KEY`, `SUBSTRATE_ENCRYPTION_KEY`, `FACTORY_REPO`, `FACTORY_REMOTE`,
`MCP_HUB_IDENTITY_MODE`, the public URLs), and the code refuses to start without what it needs.

---

## 4. Interactive and autonomous agency

| Dimension | Interactive session | Temporal workflow |
|---|---|---|
| Mode | synchronous, conversational | asynchronous, durable |
| Trigger | a person's message | an event, a schedule, a bead transition |
| Failure model | tell the person, ask | retry, escalate, branch |
| Best for | exploration, "do this for me now" | routines, monitors, reconciliation, long-running work |

**Decision rule (binding).** A person waiting on the response → an interactive session. A fact,
an event or a schedule triggered the work → a Temporal workflow. Either MAY hand off to the other
through the substrate, and only through it: a handoff is a bead and an edge, never a message.

The interactive session is a client of the gateway under the same contracts as any other; it is
not a platform component and holds no privilege the gateway does not grant. The outer loop is an
interactive session with a specific charter (`CLAUDE.md`): it shapes intent into beads, dispatches
the gate, and executes merges on the gate's verdict. It never writes a verdict for its own work.

**Anti-pattern.** Rebuilding a fixed hierarchy of SDLC roles inside the dispatcher. Dispatch is
generic; personas are files under `docs/agents/`, selected at runtime by the task's `risk_class`
and traceability fields, and the artefact a persona produces is a bead, or the separation of
duties did not happen.

---

## 5. Cross-cutting requirements

- **Observability.** Every gateway call emits one structured audit line. Every bead transition is
  an event. Every dispatcher schedule lands its failures on a standing status bead, so an idle or
  broken schedule is visible from the store, not only from logs. Every LLM call MUST record its
  cost in provenance (`cost_usd`) so spend is attributable per bead and per namespace.
- **Identity.** No surface serves without a resolved identity. The store accepts a key; the
  gateway resolves people and machines to named clients with scopes and is the only holder of the
  store's write key; a browser never holds one. A machine identity holds the narrowest scope that
  serves it, and a worker that only reads holds only the read key.
- **Reversibility.** Transitions are event-sourced and replayable. A merge is reversible by a
  measured, rehearsed revert, and a deployment pins images by digest so a rollback is a known
  revision, not a rebuild. A change that cannot be reverted is filed as an `arch.risk`.
- **Secrets as configuration.** No secret is compiled in, committed, or printed. Services read
  keys from the environment and refuse to start without them. A prompt that needs a secret hands
  over the variable name, never the value; verification is by hash prefix, never by echo. A
  worker's execution environment is credential-free by construction.
- **Separability.** The platform MUST be separable from any one deployment of it.
  `scripts/private_identifiers.py` runs on every PR through `scripts/check-repo-invariants.py`,
  scanning every publishable path against `scripts/private-identifier-patterns.yaml` (the
  operator's hosts, domain, people and financial field names), with
  `scripts/publishable-scope.yaml` declaring what is publishable (default: everything; an exclusion
  carries a reviewed reason) and `scripts/private-identifier-grandfathered.txt` as a shrinking
  ratchet. An unreadable policy fails closed. `scripts/build-platform-tree.py` exports the
  publishable tree from the manifest in `scripts/platform-tree.yaml`, applies the declared
  rewrites, and runs the same check over the result.
- **Repository invariants.** `scripts/check-repo-invariants.py` carries the rest: the test map
  covers every app, images are pinned, a `dev.task` spec's requirement references resolve, the
  principles view matches the beads, and the `Change kind:` declaration survives into history. A
  ratchet file that admits debt names exact files and shrinks; it never admits a directory.
- **Tidy-First.** Structural and behavioural changes never share a PR. Every PR body declares
  exactly one `Change kind:` and carries verification evidence; a factory PR carries its bead id,
  and an attended outer-loop PR carries a bare `Outer-loop: true` line.

---

## 6. Extension points

**Add a vertical.** Choose a namespace. Register its content schemas in the substrate's schema
registry and, if its data is sensitive, opt the namespace into encryption with its plaintext keys.
Register its routers and integrity mappers through `apps/substrate/src/namespace_registry.py`.
Give it a Temporal task queue and a worker. Define its capabilities once as routers under
`apps/mcp-hub/src/routers/v1/` so they appear on REST and MCP together, each declaring the scope
it checks. Declare its demand-side capabilities in the EA model and trace its applications to
them. Add a requirement registry under `docs/requirements/`. The LifeOps vertical (§3.7) is the
template.

**Add a persona.** A persona is a Markdown file with front matter under `docs/agents/`, tracked
and reviewed; `scripts/materialize_agents.py` and the dispatcher copy it into the worker's agent
directory per run. A persona that exists only in an ignored directory is memory, not a persona,
and a persona's output MUST be a bead or a note on one.

**Add a principle.** Append a `PRIN-NNN` entry to `docs/architecture/principles.md` with a
statement, a source, a status and the enforcement gap; `scripts/principles_sync.py` mirrors it to
an `arch.principle` bead and `check-view` keeps the file and the beads equal. Map it to the lanes
it governs so the dispatcher injects it; it becomes `enforced` when a mechanism fires on violation.

**Add a conformance check.** Add a `check_*` function to `scripts/ea-conformance.py`, a test for
the unsafe direction, and update the count the architecture README states; `readme_conformance.py`
fails the build until the two agree.

**Add a bead type or an edge.** Register the content schema; declare the state machine and entry
state in `bead_rules.py` if the type has a lifecycle; add the edge to `BEAD_LINK_TYPES` and to the
inventory table together, with the persona that writes it.

**Operator-owned files.** These ship as examples and are replaced by the operator's own:

| File | What the operator owns there |
|---|---|
| `scripts/private-identifier-patterns.yaml` | hosts, domain, people and field names that never publish |
| `scripts/publishable-scope.yaml` | which paths are publishable, each exclusion with a reason |
| the EA model instance (the YAML the loader reads) | capabilities, applications, services, observations |
| `docs/releases/*.json` | release charters: objective, outcomes, declared balance |
| `docs/requirements/*.json` | requirement registries with dated conformance verdicts |
| `docs/agents/*.md` | the personas the worker runs as |
| `.claude/skills/platform-doctrine/SKILL.md` | the decision-time doctrine injection |
| `CLAUDE.md` | the outer loop's operating file: role, merge rules, the rules its incidents produce |

`CLAUDE.md` is the one file this document expects to grow: the outer loop absorbs every finding
into either a backlog item with acceptance criteria or a rule that prevents recurrence, and the
rules live there. A finding that produces neither is not absorbed.

---

## 7. Amendments

Amendments to this document are versioned and recorded inline below. An amendment is a
`### Amendment N` heading followed by a `**Status:** \`PROPOSED\`|\`RATIFIED\`|\`WITHDRAWN\``
line, then **Summary**, **Rationale** and **Diff**. A `PROPOSED` amendment binds nothing. A
`RATIFIED` amendment's diff is applied to the sections it names in the same PR that ratifies it,
and each executable clause is carried by a `dev.task` that the entry names. A `PROPOSED`
amendment undecided after four weeks is `WITHDRAWN` by the outer loop with a note. Numbers are
never reused.

### Amendment 1

**Status:** `RATIFIED`

**Summary:** This document is established, at the first public commit, from the binding
architecture of the private monorepo this tree was exported from. Sections 0 to 6 state the
platform as it exists in the exported tree; the exporting operator's own amendment log, roadmap,
locked decisions and deployment description stay with that operator and are not part of this
document.

**Rationale:** A binding document must describe the platform it binds. The exporting
architecture binds one deployment and its history; the public platform is the framework alone,
with the operator-specific parts named as extension points (§6) rather than carried as record.

**Diff:** The whole document, as published.

---

## 8. References

- The operating model (loops, personas, release as the unit of intent, handoff contracts):
  [`docs/architecture/agentic-operating-model.md`](docs/architecture/agentic-operating-model.md)
- The EA metamodel (CSDM objects, attributes, lifecycles, git-versus-substrate split):
  [`docs/architecture/ea-metamodel.md`](docs/architecture/ea-metamodel.md)
- The bead inventory and the canonical edge table:
  [`docs/architecture/bead-object-inventory.md`](docs/architecture/bead-object-inventory.md)
- The SDLC as a bead graph (persona to bead-type carrier map):
  [`docs/architecture/agentic-sdlc-beads.md`](docs/architecture/agentic-sdlc-beads.md)
- The doctrine mechanism and the principle registry:
  [`docs/architecture/doctrine.md`](docs/architecture/doctrine.md),
  [`docs/architecture/principles.md`](docs/architecture/principles.md)
- The architecture directory's README, with the machinery that runs:
  [`docs/architecture/README.md`](docs/architecture/README.md)
- The requirement registries: [`docs/requirements/README.md`](docs/requirements/README.md)
- The code that enforces each section is cited inline in §3 and §5: the substrate's
  `schemas.py`, `bead_rules.py` and `crypto.py`; the dispatcher's `dispatch.py`, `guards.py`,
  `containment.py`, `scanner.py` and `schedule_runtime.py`; the gateway's `access_auth.py`; and
  under `scripts/`, the gate, release, requirement, EA, separability and invariant tooling.
