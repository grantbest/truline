import { describe, expect, it } from "vitest";
import type { ArchReleaseContent, ReleaseOutcome } from "@/lib/ea-model";
import type { Bead, BeadLink, DevTaskContent, DevTaskState } from "@/types/bead";
import {
  classifyOutcome,
  isOpenReleaseState,
  openReleaseCharters,
  releaseIndexRows,
  type OutcomeRollupGroup,
} from "@/lib/release-index";

// Minimal bead factories — only the fields the projection actually reads.
// Mirrors dev-board.test.ts's shape, kept local since this is a new module.
function bead(overrides: Partial<Bead> & Pick<Bead, "id">): Bead {
  return {
    namespace: "dev",
    type: "task",
    state: "pending",
    parent_id: null,
    context: {},
    content: {},
    confidence: null,
    trust_tier: "system",
    provenance: {},
    created_by: "test",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

function releaseCharterBead(
  id: string,
  state: string,
  contentOverrides: Partial<ArchReleaseContent> = {},
): Bead {
  const content: ArchReleaseContent = {
    ref: id,
    name: `Charter ${id}`,
    objective: "Objective.",
    sprints: [],
    outcomes: [],
    declared_balance: {},
    opened_at: "2026-08-25T00:00:00Z",
    ...contentOverrides,
  };
  return bead({
    id,
    namespace: "arch",
    type: "release",
    state,
    content: content as unknown as Record<string, unknown>,
  });
}

function taskBead(id: string, state: DevTaskState, contentOverrides: Partial<DevTaskContent> = {}): Bead {
  const content: DevTaskContent = {
    lane: "feature",
    title: `Task ${id}`,
    intent: "Some intent.",
    context_refs: [],
    acceptance: ["Some acceptance."],
    verification: { commands: ["npm test"] },
    scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
    risk_class: "behavioral",
    budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
    autonomy: "propose",
    ...contentOverrides,
  };
  return bead({ id, state, content: content as unknown as Record<string, unknown> });
}

function deliversLink(source_id: string, target_id: string): BeadLink {
  return {
    id: `${source_id}->${target_id}`,
    source_id,
    target_id,
    link_type: "delivers",
    content: {},
    created_at: "2026-08-25T00:00:00Z",
    created_by: "test",
  };
}

function outcome(id: string, statement = "Statement."): ReleaseOutcome {
  return { id, statement, work_class: "feature" };
}

describe("isOpenReleaseState / openReleaseCharters — open is a state, never a date", () => {
  it("treats planned, in_flight and closing as open; released and abandoned as terminal", () => {
    expect(isOpenReleaseState("planned")).toBe(true);
    expect(isOpenReleaseState("in_flight")).toBe(true);
    expect(isOpenReleaseState("closing")).toBe(true);
    expect(isOpenReleaseState("released")).toBe(false);
    expect(isOpenReleaseState("abandoned")).toBe(false);
  });

  it("excludes released and abandoned charters even when they opened after an open one", () => {
    // R26.10 (planned) opens 2026-11-01; R26.04 (abandoned) opens 2026-11-17
    // — later by the calendar, but not open. A date-based filter would keep
    // the abandoned one and could even rank it first; this must not.
    const planned = releaseCharterBead("R26.10", "planned", { opened_at: "2026-11-01" });
    const abandoned = releaseCharterBead("R26.04", "abandoned", { opened_at: "2026-11-17" });
    const open = openReleaseCharters([abandoned, planned]);
    expect(open.map((c) => c.id)).toEqual(["R26.10"]);
  });

  it("sorts by ref, not by opened_at", () => {
    const b = releaseCharterBead("R26.11", "in_flight", { opened_at: "2026-09-01" });
    const a = releaseCharterBead("R26.09", "in_flight", { opened_at: "2026-10-01" });
    expect(openReleaseCharters([b, a]).map((c) => c.id)).toEqual(["R26.09", "R26.11"]);
  });
});

describe("classifyOutcome — complete / in_progress / declared_only, computed from bead states", () => {
  it("is declared_only with zero bound tasks", () => {
    expect(classifyOutcome(outcome("O-1"), []).group).toBe("declared_only");
  });

  it("is in_progress when any bound task is open, regardless of the rest", () => {
    const rollup = classifyOutcome(outcome("O-1"), [
      taskBead("t-1", "doing"),
      taskBead("t-2", "done"),
    ]);
    expect(rollup.group).toBe("in_progress");
    expect(rollup.taskIds).toEqual(["t-1", "t-2"]);
  });

  it("is complete only when at least one bead is done AND none is open", () => {
    const rollup = classifyOutcome(outcome("O-1"), [taskBead("t-1", "done")]);
    expect(rollup.group).toBe("complete");
  });

  it("does NOT read as complete when every bound bead is closed but none is done — nothing stands", () => {
    // The corner case the spec calls out by name: all superseded/archived,
    // no done bead. Must not be `complete`; by elimination it reports the
    // same as "nothing bound" rather than invent a fourth group.
    const allSuperseded = classifyOutcome(outcome("O-1"), [
      taskBead("t-1", "superseded"),
      taskBead("t-2", "archived"),
    ]);
    expect(allSuperseded.group).not.toBe("complete");
    expect(allSuperseded.group).toBe("declared_only");
  });

  it("a single done bead among only-done beads is enough for complete", () => {
    expect(
      classifyOutcome(outcome("O-1"), [taskBead("t-1", "done"), taskBead("t-2", "done")]).group,
    ).toBe("complete");
  });
});

describe("releaseIndexRows — the index's whole rollup, fan-out bounded by open charters", () => {
  it("excludes closed releases from the rows entirely", () => {
    const open = releaseCharterBead("R26.09", "in_flight", { outcomes: [outcome("O-1")] });
    const closed = releaseCharterBead("R26.04", "abandoned", { outcomes: [outcome("O-1")] });
    const rows = releaseIndexRows([open, closed], [], new Map());
    expect(rows.map((r) => r.charter.id)).toEqual(["R26.09"]);
  });

  it("never looks up links for a charter that was not passed in linksByCharterId (fan-out stays bounded)", () => {
    const charter = releaseCharterBead("R26.09", "in_flight", { outcomes: [outcome("O-1")] });
    const task = taskBead("t-1", "done", { outcome_ref: "O-1" });
    // linksByCharterId has an entry for a DIFFERENT charter id — this must
    // not spuriously bind t-1 to R26.09.
    const rows = releaseIndexRows(
      [charter],
      [task],
      new Map([["some-other-charter", [deliversLink("t-1", "some-other-charter")]]]),
    );
    expect(rows[0].outcomes[0].group).toBe("declared_only");
  });

  it("reproduces the R26.09 shape from PR #971 (2 complete / 6 in_progress / 2 declared_only)", () => {
    const outcomes = [
      outcome("O-1", "RETIRED, undelivered and not to be built"), // complete by bead state, retired in prose
      outcome("O-2"),
      outcome("O-3"),
      outcome("O-4"),
      outcome("O-5"),
      outcome("O-6"),
      outcome("O-7"),
      outcome("O-8"),
      outcome("O-9"), // added by #971, no bound beads
      outcome("O-10"), // added by #971, no bound beads
    ];
    const charter = releaseCharterBead("R26.09", "in_flight", { outcomes });
    const tasks: Bead[] = [
      taskBead("o1-done", "done", { outcome_ref: "O-1" }),
      taskBead("o2-done", "done", { outcome_ref: "O-2" }),
      taskBead("o3-doing", "doing", { outcome_ref: "O-3" }),
      taskBead("o4-doing", "doing", { outcome_ref: "O-4" }),
      taskBead("o5-doing", "doing", { outcome_ref: "O-5" }),
      taskBead("o6-doing", "doing", { outcome_ref: "O-6" }),
      taskBead("o7-doing", "doing", { outcome_ref: "O-7" }),
      taskBead("o8-doing", "doing", { outcome_ref: "O-8" }),
    ];
    const links = new Map<string, BeadLink[]>([
      ["R26.09", tasks.map((t) => deliversLink(t.id, "R26.09"))],
    ]);

    const rows = releaseIndexRows([charter], tasks, links);
    expect(rows).toHaveLength(1);
    expect(rows[0].counts).toEqual({ complete: 2, in_progress: 6, declared_only: 2 });

    // Retirement has no bead-state signal — the statement is what carries
    // it, and it must still render for this "complete" outcome.
    const o1 = rows[0].outcomes.find((r) => r.outcome.id === "O-1");
    expect(o1?.group).toBe("complete");
    expect(o1?.outcome.statement).toContain("RETIRED");
  });

  it("counts sum to the outcome count for every row", () => {
    const charter = releaseCharterBead("R26.11", "in_flight", {
      outcomes: [outcome("O-1"), outcome("O-2"), outcome("O-3")],
    });
    const tasks = [taskBead("t-1", "done", { outcome_ref: "O-1" })];
    const links = new Map<string, BeadLink[]>([["R26.11", [deliversLink("t-1", "R26.11")]]]);
    const rows = releaseIndexRows([charter], tasks, links);
    const sum = (Object.values(rows[0].counts) as number[]).reduce((a, b) => a + b, 0);
    expect(sum).toBe(3);
  });
});

describe("group type stays a closed, three-member union", () => {
  it("every group produced by classifyOutcome is one of the three named groups", () => {
    const groups: OutcomeRollupGroup[] = ["complete", "in_progress", "declared_only"];
    const cases: Bead[][] = [
      [],
      [taskBead("a", "done")],
      [taskBead("a", "doing")],
      [taskBead("a", "superseded")],
      [taskBead("a", "archived"), taskBead("b", "superseded")],
    ];
    for (const boundTasks of cases) {
      expect(groups).toContain(classifyOutcome(outcome("O-1"), boundTasks).group);
    }
  });
});
