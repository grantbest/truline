# Contributing

Thank you for looking. This repository is the public tree of a platform that is built, gated and
merged by the process it describes. That shapes how contributions work here, so please read this
before opening anything.

## What is welcome today

- **Issues**: bug reports with a reproduction, questions about the architecture, and proposals.
  An issue that names the requirement or principle it concerns (`docs/requirements/`,
  `docs/architecture/principles.md`) gets a faster answer.
- **Discussions**: design questions, "how would I run this on X", and experience reports.
- **Pull requests for documentation and the quickstart.** Small, self-contained, one change kind.

## What is not yet open

Feature work on the platform itself goes through the factory described in
[`docs/architecture/agentic-operating-model.md`](docs/architecture/agentic-operating-model.md):
a `dev.task` bead, an isolated worker, a fresh-context release gate, and a merge executed by the
outer loop. External pull requests for features will be read and may be turned into a task, but
they will not be merged directly. This is a limitation of where the project is, not a judgement
of the contribution; the path for outside work is being chartered.

## Rules every pull request follows

These are enforced by CI, not by convention. Read them before you push.

1. **Tidy-First.** A pull request is either `structural` (behaviour unchanged) or `behavioral`
   (behaviour changed), never both. The body carries exactly one bare line:
   `Change kind: structural` or `Change kind: behavioral`.
2. **Verification evidence in the body.** Say what you ran and what it printed. "Tests pass" with
   no output is not evidence.
3. **No private identifiers.** `scripts/private_identifiers.py` scans every path on every PR
   against `scripts/private-identifier-patterns.yaml`. Hosts, domains, people and credential
   shapes do not land here.
4. **The model and the code agree.** In the operator's repository `scripts/ea-conformance.py`
   fails the build when the architecture model and the code disagree; the model instance stays
   there, so this tree's CI cannot run it yet. If your change moves a boundary, say so in the PR.
5. **Cite what you serve.** Work that implements a requirement cites its id from
   `docs/requirements/`. Work that fixes a bug cites the finding.

## Running the checks locally

What CI runs is in `.github/workflows/ci.yml`. Locally, the two that matter most:

```bash
python3 scripts/private_identifiers.py                       # separability scan, the same one CI runs
cd apps/substrate && python -m pytest -q tests/ client/tests/   # needs DATABASE_URL and SUBSTRATE_ENCRYPTION_KEY
```

`scripts/check-repo-invariants.py` and `scripts/ea-conformance.py` are the exporting operator's
whole-repository checks; they read files that stay in the operator's tree and do not run at this
root today. The CSDM checker runs on its own example model:
`cd apps/substrate/publish/csdm-on-beads && EA_CANONICAL_CLUSTER=example python3 scripts/ea-conformance.py --no-baseline`.

## Licence

By contributing you agree that your contribution is licensed under the MIT licence in
[`LICENSE`](LICENSE).
