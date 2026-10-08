# Truline

**A true line from north star to running system.**

Truline is an enterprise-architecture platform where the service model and the software delivery
that changes it are one graph. Every capability, application, requirement, release, change and
incident is a typed record. Every task, finding and pull request is a record in the same graph,
linked back to the requirement it serves and the capability behind that. Temporal workflows move
the records through their lifecycles, and the build fails the moment the model, the code and the
running system disagree. Whether a person or an agent made the change, the line holds.

```
north star ─▶ canon ─▶ capability ─▶ application ─▶ requirement ─▶ release outcome
                                                                          │
   observation ◀─ running system ◀─ change record ◀─ pull request ◀─ task ◀┘
        │
        └── back onto the model: drift between what was intended, what was built and what is running is a finding
```

| | |
|---|---|
| **13** architecture conformance checks on every pull request | **21** engineering principles, each binding on the work the factory dispatches |
| **14** typed `arch.*` record types, from capability to observation | **1** gate: a fresh-context verdict before anything merges |

<!-- TODO before launch: a 90-second recording of a bead crossing the factory board, linked here. -->

## Try it in ten minutes

```bash
git clone https://github.com/grantbest/truline && cd truline
cp quickstart/env.example quickstart/.env       # set SUBSTRATE_ENCRYPTION_KEY (instructions inside)
docker compose -f quickstart/docker-compose.yml up --build
./quickstart/seed.sh                              # one task bead, through the gateway
open http://localhost:8080/factory                # the board
```

That brings up the store, the gateway and the console. It does not start the factory worker,
which needs Temporal and a repository to work on; `apps/factory-dispatcher/README.md` is the
next step.

## Which door

- **You run coding agents and want them governed.** Start with
  [`docs/architecture/agentic-operating-model.md`](docs/architecture/agentic-operating-model.md)
  and `apps/factory-dispatcher/`. The release gate and the Tidy-First rule are the point.
- **You do enterprise architecture or ITSM.** Start with
  [`apps/substrate/publish/csdm-on-beads/`](apps/substrate/publish/csdm-on-beads/): a
  dependency-free CSDM core with state machines and a conformance checker you can run against
  your own repository.
- **You want the primitive.** Start with `apps/substrate/`: typed, namespaced, lineaged beads with
  an append-only event log, behind one API.
- **You want to see a vertical built on it.** `apps/lifeops-console/` and `apps/finance-reporting/`
  are a personal-finance vertical, kept as the worked example of an extension. It is not the
  product.

## What is here

| Path | What it is |
|---|---|
| `apps/substrate/` | the bead store: FastAPI + Postgres, typed namespaces, governed state machines, an event log. `client/` is the one Python client; `publish/csdm-on-beads/` is the service model as a standalone, dependency-free package |
| `apps/factory-dispatcher/` | the SDLC vertical: turns a `dev.task` bead into a pull request through an isolated worker, a scanner, containment and a fresh-context release gate; Temporal-owned |
| `apps/mcp-hub/` | the capability gateway: REST/OpenAPI and MCP surfaces over the store, the factory and the LifeOps vertical, behind an identity contract |
| `apps/lifeops-console/` | client #1: the console (beads, factory board, releases, architecture, and the LifeOps views) |
| `apps/finance-reporting/` | a one-page reporting service for the LifeOps vertical |
| `scripts/` | the harness: the release gate, release charters and status, requirement registries and citations, repository invariants, the EA model tooling, and the separability check |
| `docs/architecture/` | the EA metamodel, principles, doctrine, operating model and bead inventory |
| `docs/requirements/` | the platform and LifeOps requirement registries with dated conformance verdicts |
| `docs/releases/` | release charters: one objective, outcomes with a work class, a declared balance |

## Where it came from

This repository was exported from a private monorepo on its first commit, with a history written
for the purpose. The architecture is binding in [`ARCHITECTURE.md`](ARCHITECTURE.md); the
operating model is in [`docs/architecture/agentic-operating-model.md`](docs/architecture/agentic-operating-model.md).
What ships is the framework: the code, the harness, the metamodel, the doctrine, the personas and
the skills, with example charters and registries in the shape the tooling validates. The exporting
operator's own record (their charters, their measurements, their amendment log, the incidents behind
their rules) stays with them, and the exporter's record scan fails the build if any of it leaks.
Hosts, domains and people are rewritten to neutral names (`cluster-a`, `host-b`, `example.org`).

## Running it for real

Each application has its own README. The deployment this tree was exported from runs on k3s
with ArgoCD, Cloudflare Access at the edge and a macOS launchd worker for the dispatcher; those
manifests stay with the operator. What a deployment must supply is configuration, never a
compiled-in name: `SUBSTRATE_URL`, `SUBSTRATE_API_KEY`, `SUBSTRATE_ENCRYPTION_KEY`,
`FACTORY_REPO`, `FACTORY_REMOTE`, `EA_CANONICAL_CLUSTER`, `CLOUDFLARE_ACCESS_TEAM_DOMAIN`,
`MCP_HUB_PUBLIC_URL`, `LIFEOPS_CONSOLE_PUBLIC_URL`. The code refuses to start without the ones it
needs rather than guessing.

## Separability

`scripts/private_identifiers.py` runs on every pull request. It scans every path against
`scripts/private-identifier-patterns.yaml`; the file shipped here is an example, and an operator
keeps their own naming the hosts, domain and people that must never land in a published tree.

## Contributing, security, conduct

[`CONTRIBUTING.md`](CONTRIBUTING.md) says what is open today and the rules CI enforces.
[`SECURITY.md`](SECURITY.md) says how to report a vulnerability privately.
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) is the Contributor Covenant.

## Licence

MIT. See [`LICENSE`](LICENSE).
