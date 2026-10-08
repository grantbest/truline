import type { Bead, BeadLink } from "@/types/bead";
import { releaseContent, type ArchReleaseContent, type ReleaseOutcome } from "@/lib/ea-model";
import { deliversMapFromLinks, OPEN_DEV_TASK_STATES, taskContent } from "@/lib/dev-board";

// Pure projection helpers for the releases index — the board's answer to
// "where does a release stand", which until now had no way in (no /releases
// route, no nav entry; the only detail page lived at /releases/:ref and the
// only list was ReleaseCoverageCard's flat, state-blind dump inside /alpha).
// Kept out of the route component, mirroring release-view.ts's own shape, so
// the open/closed filter and the outcome rollup — the two things a stale or
// wrong read here would silently misreport — are testable without a DOM.
//
// This index computes everything client-side from beads already on the
// board (arch.release charters, dev.task beads, and the `delivers` edges
// between them) — it never calls factory_status.release_delivery (mcp-hub's
// wrapper around release-status.py). That route answers not_configured in
// every production deployment, by a recorded decision (OPS-110 arm (c);
// dev.task 09029a3c chose to keep the capability checkout-side and say so)
// — an index built on it would render empty and green in every test and
// dark in prod. Do not "fix" this index by wiring that route in; the
// existing /releases/:ref detail page already depends on it and stays dark
// there for the same reason (see ReleaseView.tsx's own module comment).

// Per apps/substrate/src/bead_rules.py::STATE_MACHINES[("arch","release")]:
// planned -> in_flight -> closing -> released, with abandoned reachable from
// planned/in_flight and closing able to fall back to in_flight. "released"
// and "abandoned" are the only terminal states — everything else is open
// work a reader can still act on. Never derive "open" from opened_at/
// target_at: a release's dates say nothing about whether it is still open
// (R26.10 is planned with an opened_at after R26.04's abandoned one).
const TERMINAL_RELEASE_STATES = new Set(["released", "abandoned"]);

export function isOpenReleaseState(state: string): boolean {
  return !TERMINAL_RELEASE_STATES.has(state);
}

/** Open release charters, sorted by ref — never by opened_at/target_at. A
 *  date sort would put an abandoned release above a planned one whenever
 *  the abandoned one happened to open later; ref is stable and carries no
 *  claim about how "open" is defined. */
export function openReleaseCharters(charters: Bead[]): Bead[] {
  return charters
    .filter((charter) => isOpenReleaseState(charter.state))
    .sort((a, b) => releaseContent(a).ref.localeCompare(releaseContent(b).ref));
}

export type OutcomeRollupGroup = "complete" | "in_progress" | "declared_only";

export interface OutcomeRollup {
  outcome: ReleaseOutcome;
  group: OutcomeRollupGroup;
  taskIds: string[];
}

const OPEN_DEV_TASK_STATE_SET = new Set<string>(OPEN_DEV_TASK_STATES);

/**
 * Classifies one outcome by the dev.task beads bound to it (a resolved
 * `delivers` edge to this outcome's charter, and `outcome_ref` naming this
 * outcome's id) — never by anything on the outcome itself. A charter
 * outcome has exactly four fields and no field for what happened to the
 * work, only what was promised — see the caller for how retirement (a fact
 * only the outcome's own statement can carry) stays visible alongside this.
 *
 * `complete` requires an actual `done` bead, not just the absence of an
 * open one: an outcome whose bound beads are all `superseded`/`archived`
 * with none `done` has nothing standing. It must not read as `complete`,
 * and it has no open bead either, so by elimination it reports the same as
 * "nothing bound" (`declared_only`) rather than fabricate a fourth group
 * this view does not have.
 */
export function classifyOutcome(outcome: ReleaseOutcome, boundTasks: Bead[]): OutcomeRollup {
  const hasOpen = boundTasks.some((task) => OPEN_DEV_TASK_STATE_SET.has(task.state));
  const hasDone = boundTasks.some((task) => task.state === "done");
  const group: OutcomeRollupGroup = hasDone && !hasOpen ? "complete" : hasOpen ? "in_progress" : "declared_only";
  return { outcome, group, taskIds: boundTasks.map((task) => task.id) };
}

export interface ReleaseIndexRow {
  charter: Bead;
  content: ArchReleaseContent;
  outcomes: OutcomeRollup[];
  counts: Record<OutcomeRollupGroup, number>;
}

/**
 * One row per open release, each outcome rolled up from bound dev.task
 * beads. Follows FactoryBoard.tsx's fan-out shape: `linksByCharterId` is
 * expected to hold one `listBeadLinks(charterId, {direction:"incoming",
 * link_type:"delivers"})` result per OPEN charter — bounded by the (small)
 * number of open releases, never by the number of tasks on the board.
 */
export function releaseIndexRows(
  charters: Bead[],
  tasks: Bead[],
  linksByCharterId: Map<string, BeadLink[]>,
): ReleaseIndexRow[] {
  const open = openReleaseCharters(charters);
  const releaseByTaskId = deliversMapFromLinks(open, linksByCharterId);

  const tasksByCharterId = new Map<string, Bead[]>();
  for (const task of tasks) {
    const charter = releaseByTaskId.get(task.id);
    if (!charter) continue;
    const bucket = tasksByCharterId.get(charter.id);
    if (bucket) bucket.push(task);
    else tasksByCharterId.set(charter.id, [task]);
  }

  return open.map((charter) => {
    const content = releaseContent(charter);
    const boundTasks = tasksByCharterId.get(charter.id) ?? [];
    const outcomes = content.outcomes.map((outcome) =>
      classifyOutcome(
        outcome,
        boundTasks.filter((task) => (taskContent(task).outcome_ref ?? undefined) === outcome.id),
      ),
    );
    const counts: Record<OutcomeRollupGroup, number> = { complete: 0, in_progress: 0, declared_only: 0 };
    for (const rollup of outcomes) counts[rollup.group] += 1;
    return { charter, content, outcomes, counts };
  });
}
