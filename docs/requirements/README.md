# Requirements registries

Two example registries in the shape the tooling validates. Replace their contents with your own;
keep the shape.

| File | Id | Owns |
|---|---|---|
| [`platform-requirements.json`](platform-requirements.json) | `REG-PLATFORM` | What the machine that builds and runs the system must do: substrate, factory, execution, trust, delivery, assurance |
| [`lifeops-requirements.json`](lifeops-requirements.json) | `REG-LIFEOPS` | What the example vertical must do |

A requirement belongs in `REG-PLATFORM` if it would still be true were the vertical replaced by a
different product.

Anything reading these files enumerates the directory rather than hard-coding a filename.

## Shape

Each registry carries a `registry` block (id, scope, legends, verdict discipline), a `capabilities`
list, and a `requirements` list. A requirement has an id, a capability, a title, a status, a priority,
a source, a rationale (or a user story, which the loader mirrors into the rationale), the paths that
implement it, and acceptance criteria written as given / when / then.

A criterion gains `conformance`, `measured_at` and `measured_revision` only when someone measures
it. Verdicts are dated snapshots of the system as observed; sprints burn them down. A verdict is never
edited to match today's code. A new observation is recorded instead, because a silently corrected
verdict destroys the measurement it exists to provide.

## How the tooling uses them

- `scripts/requirements-load.py` mirrors every requirement into an `arch.requirement` bead and
  every dated measurement into an `arch.requirement_conformance` bead.
- `scripts/check-repo-invariants.py` resolves every `requirement_refs` citation on a task spec
  against these files and fails on a dangling one.
- `scripts/release-status.py` reads the criteria a release's outcomes cite and reports what has
  been measured.
