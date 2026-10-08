---
name: platform-doctrine
description: >-
  Load the platform principle registry when shaping beads, planning sprints, reviewing
  architecture, or drafting amendments. Use whenever a decision touches platform structure, the
  factory pipeline, gate policy, or interface contracts — the output must cite the principle ids
  it applies or consciously overrides.
---

# Platform Doctrine

You are about to make or review a decision that platform doctrine governs. Doctrine lives in the
bead graph (the registry file below is its view); this skill is the injection point that puts it
in context at decision time. Temporal is run-state and carries no doctrine.

## Do this

1. **Query the substrate first.** Run `scripts/principles_sync.py fetch` (SUBSTRATE_URL /
   SUBSTRATE_API_KEY) to read the live `arch.principle` beads — the store the factory itself
   writes. Optionally run `scripts/principles_sync.py check-view` first to confirm
   [`docs/architecture/principles.md`](../../../docs/architecture/principles.md) still matches
   the beads before trusting it as a substitute.
   **Fall back to the file when offline** — no substrate credentials, or the substrate is
   unreachable: read `principles.md` directly. Ids are stable across both sources (the
   id-churn rule), so a citation written against the file remains valid against the beads and
   vice versa. Either source, the mechanism, lifecycle, and tier definitions are in
   [`docs/architecture/doctrine.md`](../../../docs/architecture/doctrine.md).
2. **Apply by status.** `adopted` and `enforced` principles are binding on the work at hand —
   deviating requires a written rationale in the output, exactly like a SHOULD in
   `ARCHITECTURE.md` §0. `proposed` principles are advisory: weigh them, don't obey them.
3. **Cite ids in the output.** A sprint plan, `dev.task`, `dev.design`, amendment draft, or
   architecture review produced with this skill names the `PRIN-` ids it applies (and any it
   consciously overrides, with the reason). This is what grows the `applies` edges the staleness
   rule measures.
4. **Propose promotions and demotions with the helper.** If the work just mechanized a principle,
   or a principle blocked good work or no longer matches reality, run
   `scripts/principles_sync.py propose-promotion <PRIN-id> <target-status>` — it drafts the dated
   `status_history` transition and a unified diff of the registry entry, ready to paste into a PR.
   It writes nothing. Either way: registry changes go by PR, never inline.
5. **Flag new doctrine.** If the decision surfaced a lesson no principle covers, end the output
   with a proposed `PRIN-` entry (statement, source, rationale) for the Product Owner to take
   through review — a finding that produces neither a backlog item nor a rule was not absorbed.

## Do not

- Do not treat `proposed` principles as binding, and do not present them as platform policy.
- Do not edit `principles.md`, `doctrine.md`, or the beads directly from a planning conversation —
  propose, don't write; changes ride PRs to the gate. `propose-promotion`'s output is a draft, not
  an applied change: it writes nothing, on either side.
- Do not restate principle text into other documents — cite the id. Copies drift.

## The file and the beads

The substrate registers `arch.principle`; `principles.md` is a generated view of the beads
(`scripts/principles_sync.py render`/`check-view`), not a second store. Ids are stable across both
sources — the id-churn rule — so a citation written against the file resolves against the beads,
and a citation written against the beads resolves against the file.
