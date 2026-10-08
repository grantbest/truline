# CSDM-on-beads

A small, typed service model — capability, application, service, information
object, change, incident, technology-layer CI, release, requirement,
requirement conformance, principle, risk, release health and observation —
expressed as content schemas over a generic graph node ("bead"), plus the edge vocabulary, the
state machines, and a conformance checker that keeps a written model honest
against the repository it describes.

This is a common-service-data-model (CSDM) core, not a finished ITSM
product: it gives you the object types, the relationships between them, and
a CI check that fails the build when a claim in the model stops matching
reality. What graph store, API, or UI sits on top of it is yours to build —
nothing here assumes one.

## What's in this tree

```
LICENSE                  MIT.
schemas.py                The fourteen arch.* content models (pydantic v2), a
                           small type registry, and validate_bead_content(),
                           the one entry point that checks a piece of
                           content against its type's schema.
bead_rules.py              The edge vocabulary (BEAD_LINK_TYPES) and the
                           registered state machines (STATE_MACHINES /
                           STATE_MACHINE_ENTRY_STATES) — which transitions
                           an object of a given type may legally make.
scripts/ea-conformance.py  Reads a written model (YAML under
                           docs/architecture/model/) and fails the build
                           when it contradicts itself or the repository it
                           describes: dangling refs, inconsistent lifecycle
                           states, stale evidence, capabilities with no
                           supporting application, and more.
scripts/ea-derive.py       Derives Kubernetes-observable facts (workload
                           objects, manifest-provable dependencies) from a
                           repository's own manifests, so the model doesn't
                           hand-copy what the cluster can already prove.
                           Only exercised if you actually declare
                           kubernetes-runtime applications.
scripts/ea_reflect.py      Pure comparison logic between what the model
                           declares and what a live collector observed —
                           no network calls, no credentials, just data in
                           and findings out.
docs/architecture/model/   A minimal example model: one capability, one
                           application realizing it, and the two
                           "foundation" applications the conformance
                           checker's foundation-grouping rule expects to
                           see named. It exists to prove the checker runs
                           clean against a repository that isn't the one
                           this tree came from — run
                           `python3 scripts/ea-conformance.py --no-baseline`
                           from this directory and it reports OK.
```

None of these files import anything outside the standard library and
`pydantic`/`pyyaml`. There is no dependency on a particular web framework,
database, or deployment platform — bring your own store for the actual
bead rows; this tree only defines what a row of each type must look like
and how the graph between them may legally change shape.

## Using it

1. **Content models.** `from schemas import validate_bead_content` and call
   it with a namespace (`"arch"`), a bead type (e.g. `"capability"`), and a
   dict of content. It raises `pydantic.ValidationError` if the content
   doesn't match that type's schema, and is a silent pass-through for a
   namespace or type this module doesn't define — so you can register your
   own types alongside these without forking the file (see
   `NamespaceSchemaRegistry.register` in `schemas.py`).

2. **Edges and state machines.** `from bead_rules import BEAD_LINK_TYPES,
   STATE_MACHINES, STATE_MACHINE_ENTRY_STATES`. `BEAD_LINK_TYPES` is the
   closed set of legal `link_type` values between two objects.
   `STATE_MACHINES[(namespace, type)]` maps a current state to the set of
   states it may legally move to next; `STATE_MACHINE_ENTRY_STATES` names
   the state an object of that type is legally born into. A `(namespace,
   type)` pair with no entry is unrestricted — this is additive governance,
   not a closed list of every type that will ever exist.

3. **Conformance.** Write your own model as YAML under
   `docs/architecture/model/` (see the example there for the expected
   shape), then run `python3 scripts/ea-conformance.py`. Wire it into your
   own CI as a required check on every pull request — that is the whole
   value of this checker: it turns "the model describes the system" from an
   intention into an enforced, dated claim. `--no-baseline` skips the
   change-aware maturity-raise check, which otherwise compares the current
   model against `origin/main` via `git`; use it when running against a
   repository with no such history (a fresh checkout, or the example model
   in this tree). `EA_CANONICAL_CLUSTER` must be set to a name of your
   choosing before `ea-derive.py`'s workload/dependency derivation runs —
   it refuses to guess.

## What this is not

This tree carries no factory, no dispatcher, no LLM worker orchestration,
and no application-specific content (finance, personal infrastructure, or
otherwise). Those are the layers built on top of a CSDM core in the
platform this tree was extracted from; this is the part of that platform
general enough to stand on its own.

## Provenance

Extracted from a private platform's substrate and enterprise-architecture
tooling. The extraction is mechanically checked to import nothing from that
platform's household-specific applications and to carry no household
identifier, credential, or personal-financial field — see the parent
repository's `scripts/publishable-scope.yaml` and
`scripts/private_identifiers.py` for how, if you're reading this from
inside that repository rather than a fresh checkout of this tree alone.
