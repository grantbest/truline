import { describe, expect, it } from "vitest";
import type { ArchReleaseContent } from "@/lib/ea-model";
import taskIntakeContract from "@/lib/task-intake-contract.json";
import type { Bead, BeadLink, DevNoteContent, DevNoteKind, DevTaskContent } from "@/types/bead";
import { DEV_LANES, DEV_TASK_STATES } from "@/types/bead";
import {
  answeredWithoutRelease,
  blockingQuestions,
  buildAnswerNoteContent,
  buildTaskFilingSpec,
  CLOSED_DEV_TASK_STATES,
  deliversMapFromLinks,
  dependentsByPredecessorId,
  groupTasksByState,
  notesByParent,
  OPEN_DEV_TASK_STATES,
  openQuestions,
  releaseBadgeFor,
  sortThread,
  TASK_FORBIDDEN_ALWAYS,
  TASK_INTAKE_REQUIRED,
  taskRunnabilityFrom,
  type TaskIntakeDraft,
  type TaskRunnabilityResponse,
} from "@/lib/dev-board";

// Mirrors apps/substrate/src/routes.py::STATE_MACHINES[("dev", "task")] keys.
// Substrate is the source of truth and is out of this app's reach (Python,
// different repo path), so this is a hand-kept copy, not an import — the
// point of this test is that the copy is asserted against DEV_TASK_STATES,
// so the next state either machine gains without the other is a failing
// test here rather than a silent OFF-CONVENTION card on the board.
const SUBSTRATE_DEV_TASK_STATES = [
  "pending",
  "doing",
  "review",
  "done",
  "failed",
  "archived",
  "superseded",
];

// Minimal bead factories — only the fields the projection actually reads.
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

function note(
  id: string,
  kind: DevNoteKind,
  extra: Partial<DevNoteContent> = {},
  rest: Partial<Bead> = {},
): Bead {
  const content: DevNoteContent = { kind, body: `${kind} body`, ...extra };
  return bead({
    id,
    type: "note",
    state: "active",
    parent_id: "task-1",
    content: content as unknown as Record<string, unknown>,
    ...rest,
  });
}

function draft(overrides: Partial<TaskIntakeDraft> = {}): TaskIntakeDraft {
  return {
    lane: "code-health",
    title: "Add board intake",
    intent: "Create dev.task beads from the factory board.",
    acceptance: "WHEN the form submits, THEN the task appears as pending.",
    scopePaths: "apps/lifeops-console/src/",
    forbiddenPaths: "",
    risk_class: "behavioral",
    verificationCommands: "cd apps/lifeops-console && npm run typecheck && npm test",
    requirementRefs: "",
    nfrs: "",
    archImpact: "",
    ...overrides,
  };
}

describe("buildTaskFilingSpec", () => {
  it("uses the shared CLI intake required set", () => {
    expect(TASK_INTAKE_REQUIRED).toEqual([
      "lane",
      "title",
      "intent",
      "acceptance",
      "scope",
      "risk_class",
    ]);
  });

  it("builds the JSON spec the gateway's file_dev_task capability accepts", () => {
    const spec = buildTaskFilingSpec(draft());
    const scope = spec.scope as { paths: string[]; forbidden_paths: string[] };

    // Not a bead envelope — no namespace/type/state/trust_tier. The gateway
    // capability (file_task.file_spec) owns turning this spec into a bead.
    expect(spec).not.toHaveProperty("namespace");
    expect(spec).not.toHaveProperty("state");
    expect(spec.lane).toBe("code-health");
    expect(spec.risk_class).toBe("behavioral");
    expect(scope.paths).toEqual(["apps/lifeops-console/src/"]);
    for (const always of TASK_FORBIDDEN_ALWAYS) {
      expect(scope.forbidden_paths).toContain(always);
    }
  });

  it("appends every forbidden_always entry, not only the first", () => {
    expect(TASK_FORBIDDEN_ALWAYS).toEqual([".github/workflows/**", "docs/releases/**"]);
    const spec = buildTaskFilingSpec(draft());
    const scope = spec.scope as { forbidden_paths: string[] };

    expect(scope.forbidden_paths).toContain(".github/workflows/**");
    expect(scope.forbidden_paths).toContain("docs/releases/**");
  });

  it("rejects a submission that scopes into docs/releases/**", () => {
    expect(() =>
      buildTaskFilingSpec(draft({ scopePaths: "docs/releases/**" })),
    ).toThrow("docs/releases/** may not be listed in scope.paths");
  });

  it("rejects a submission that can write GitHub workflows", () => {
    expect(() =>
      buildTaskFilingSpec(draft({ scopePaths: ".github/workflows/**" })),
    ).toThrow(".github/workflows/** may not be listed in scope.paths");
  });

  it("writes traceability fields only when non-empty", () => {
    const spec = buildTaskFilingSpec(draft());

    expect(spec).not.toHaveProperty("requirement_refs");
    expect(spec).not.toHaveProperty("nfrs");
    expect(spec).not.toHaveProperty("arch_impact");
  });

  it("keeps non-empty traceability fields", () => {
    const spec = buildTaskFilingSpec(
      draft({
        requirementRefs: "LO-CAT-004/AC-1",
        nfrs: JSON.stringify([
          {
            category: "usability",
            statement: "Task filing does not need the CLI.",
            threshold: "Visible on submit without refresh.",
            verification: "cd apps/lifeops-console && npm test",
          },
        ]),
        archImpact: JSON.stringify({ applications: ["app.lifeops-console"] }),
      }),
    );

    expect(spec.requirement_refs).toEqual(["LO-CAT-004/AC-1"]);
    expect(spec.nfrs).toEqual([
      {
        category: "usability",
        statement: "Task filing does not need the CLI.",
        threshold: "Visible on submit without refresh.",
        verification: "cd apps/lifeops-console && npm test",
      },
    ]);
    expect(spec.arch_impact).toEqual({ applications: ["app.lifeops-console"] });
  });
});

describe("groupTasksByState", () => {
  it("mirrors the substrate's declared dev.task state machine", () => {
    // The two sides are kept as separate literals (see SUBSTRATE_DEV_TASK_STATES
    // above) so a state added to either machine without the other fails here,
    // not as a card silently routed to OFF-CONVENTION.
    expect(new Set(DEV_TASK_STATES)).toEqual(new Set(SUBSTRATE_DEV_TASK_STATES));
  });

  it("separates open columns from closed work by convention", () => {
    expect(OPEN_DEV_TASK_STATES).toEqual(["pending", "doing", "review", "failed"]);
    expect(CLOSED_DEV_TASK_STATES).toEqual(["done", "superseded", "archived"]);
  });

  it("buckets tasks into the seven convention columns", () => {
    const { byState, unknown } = groupTasksByState([
      bead({ id: "a", state: "pending" }),
      bead({ id: "b", state: "doing" }),
      bead({ id: "c", state: "review" }),
      bead({ id: "d", state: "done" }),
      bead({ id: "e", state: "failed" }),
      bead({ id: "f", state: "superseded" }),
      bead({ id: "g", state: "archived" }),
    ]);

    expect(byState.pending.map((t) => t.id)).toEqual(["a"]);
    expect(byState.doing.map((t) => t.id)).toEqual(["b"]);
    expect(byState.review.map((t) => t.id)).toEqual(["c"]);
    expect(byState.done.map((t) => t.id)).toEqual(["d"]);
    expect(byState.failed.map((t) => t.id)).toEqual(["e"]);
    expect(byState.superseded.map((t) => t.id)).toEqual(["f"]);
    expect(byState.archived.map((t) => t.id)).toEqual(["g"]);
    expect(unknown).toEqual([]);
  });

  it("treats a superseded bead as closed, not off-convention", () => {
    // This is the regression this test suite exists to prevent: retiring a
    // bead into "superseded" must not make the board look worse by dumping
    // it in the OFF-CONVENTION column.
    const { byState, unknown } = groupTasksByState([bead({ id: "dead", state: "superseded" })]);

    expect(byState.superseded.map((t) => t.id)).toEqual(["dead"]);
    expect(unknown).toEqual([]);
  });

  it("surfaces off-convention states instead of dropping them", () => {
    // Substrate does not constrain `state`. A worker writing "in_progress"
    // must still appear somewhere — a silently lost work item is the worst
    // possible failure for a board.
    const { byState, unknown } = groupTasksByState([
      bead({ id: "a", state: "pending" }),
      bead({ id: "rogue", state: "in_progress" }),
    ]);

    expect(byState.pending.map((t) => t.id)).toEqual(["a"]);
    expect(unknown.map((t) => t.id)).toEqual(["rogue"]);
  });

  it("orders each column newest first", () => {
    const { byState } = groupTasksByState([
      bead({ id: "old", state: "doing", created_at: "2026-07-01T00:00:00Z" }),
      bead({ id: "new", state: "doing", created_at: "2026-07-28T00:00:00Z" }),
      bead({ id: "mid", state: "doing", created_at: "2026-07-14T00:00:00Z" }),
    ]);

    expect(byState.doing.map((t) => t.id)).toEqual(["new", "mid", "old"]);
  });

  it("returns all seven columns even when there are no tasks", () => {
    const { byState } = groupTasksByState([]);
    expect(Object.keys(byState).sort()).toEqual(
      ["archived", "doing", "done", "failed", "pending", "review", "superseded"].sort(),
    );
  });
});

describe("sortThread", () => {
  it("reads oldest first, unlike the card columns", () => {
    const sorted = sortThread([
      note("n2", "comment", {}, { created_at: "2026-07-28T02:00:00Z" }),
      note("n1", "status", {}, { created_at: "2026-07-28T01:00:00Z" }),
      note("n3", "review", { verdict: "approve" }, { created_at: "2026-07-28T03:00:00Z" }),
    ]);
    expect(sorted.map((n) => n.id)).toEqual(["n1", "n2", "n3"]);
  });

  it("does not mutate its input", () => {
    const input = [
      note("n2", "comment", {}, { created_at: "2026-07-28T02:00:00Z" }),
      note("n1", "status", {}, { created_at: "2026-07-28T01:00:00Z" }),
    ];
    sortThread(input);
    expect(input.map((n) => n.id)).toEqual(["n2", "n1"]);
  });
});

describe("openQuestions — drives TaskThreadPanel's reply affordance, not the board's movability", () => {
  it("treats a question with no answer as open", () => {
    const notes = [note("q1", "question", { blocking: true })];
    expect(openQuestions(notes).map((n) => n.id)).toEqual(["q1"]);
  });

  it("closes a question once an answer declares releases_work: true", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1", releases_work: true }),
    ];
    expect(openQuestions(notes)).toEqual([]);
  });

  it("leaves a question open when its only answer does not declare release", () => {
    // Mirrors apps/factory-dispatcher/guards.py::open_questions: an answer
    // existing is not consent. This is the OPS-40 regression — before it, an
    // answer with no releases_work field silently closed the question.
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1" }),
    ];
    expect(openQuestions(notes).map((n) => n.id)).toEqual(["q1"]);
  });

  it("leaves a question open when its answer explicitly sets releases_work: false", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1", releases_work: false }),
    ];
    expect(openQuestions(notes).map((n) => n.id)).toEqual(["q1"]);
  });

  it("resolves by answers_ref, not by ordering", () => {
    // Two questions, one releasing answer claiming the *first*. The second
    // stays open even though the answer is chronologically last.
    const notes = [
      note("q1", "question", { blocking: true }, { created_at: "2026-07-28T01:00:00Z" }),
      note("q2", "question", { blocking: true }, { created_at: "2026-07-28T02:00:00Z" }),
      note(
        "a1",
        "answer",
        { answers_ref: "q1", releases_work: true },
        { created_at: "2026-07-28T03:00:00Z" },
      ),
    ];
    expect(openQuestions(notes).map((n) => n.id)).toEqual(["q2"]);
  });

  it("ignores answers whose answers_ref matches nothing", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "some-other-task-question", releases_work: true }),
    ];
    expect(openQuestions(notes).map((n) => n.id)).toEqual(["q1"]);
  });

  it("ignores non-question kinds", () => {
    const notes = [
      note("s1", "status"),
      note("c1", "comment"),
      note("at1", "attachment", { url: "docs/plans/x.md" }),
      note("r1", "review", { verdict: "request-changes" }),
    ];
    expect(openQuestions(notes)).toEqual([]);
  });
});

describe("blockingQuestions — every open blocking question, not only guards.is_runnable's first", () => {
  it("returns every open question marked blocking, not just one", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("q2", "question", { blocking: true }),
    ];
    expect(blockingQuestions(notes).map((n) => n.id)).toEqual(["q1", "q2"]);
  });

  it("excludes an open question not marked blocking", () => {
    const notes = [note("q1", "question", { blocking: false })];
    expect(blockingQuestions(notes)).toEqual([]);
  });

  it("excludes a blocking question whose answer released it", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1", releases_work: true }),
    ];
    expect(blockingQuestions(notes)).toEqual([]);
  });
});

describe("answeredWithoutRelease", () => {
  it("flags an open question whose only answer does not release the work", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1" }),
    ];
    expect(answeredWithoutRelease(notes)).toEqual(new Set(["q1"]));
  });

  it("does not flag a question with no answer at all", () => {
    const notes = [note("q1", "question", { blocking: true })];
    expect(answeredWithoutRelease(notes)).toEqual(new Set());
  });

  it("does not flag a question that was released", () => {
    const notes = [
      note("q1", "question", { blocking: true }),
      note("a1", "answer", { answers_ref: "q1", releases_work: true }),
    ];
    expect(answeredWithoutRelease(notes)).toEqual(new Set());
  });
});

describe("buildAnswerNoteContent", () => {
  it("writes the field the dispatcher requires when releasing", () => {
    const content = buildAnswerNoteContent("q1", "go ahead", true);
    expect(content).toEqual({
      kind: "answer",
      body: "go ahead",
      answers_ref: "q1",
      releases_work: true,
    });
  });

  it("writes releases_work: false, not an omitted field, on a deliberate hold", () => {
    const content = buildAnswerNoteContent("q1", "hold for now", false);
    expect(content).toEqual({
      kind: "answer",
      body: "hold for now",
      answers_ref: "q1",
      releases_work: false,
    });
  });
});

describe("notesByParent", () => {
  it("groups notes by the task they hang off", () => {
    const grouped = notesByParent([
      note("n1", "status", {}, { parent_id: "task-1" }),
      note("n2", "comment", {}, { parent_id: "task-2" }),
      note("n3", "comment", {}, { parent_id: "task-1" }),
    ]);

    expect(grouped.get("task-1")?.map((n) => n.id)).toEqual(["n1", "n3"]);
    expect(grouped.get("task-2")?.map((n) => n.id)).toEqual(["n2"]);
  });

  it("skips orphan notes rather than bucketing them under a null key", () => {
    const grouped = notesByParent([note("orphan", "comment", {}, { parent_id: null })]);
    expect(grouped.size).toBe(0);
  });

  it("returns an empty map for no notes", () => {
    expect(notesByParent([]).size).toBe(0);
  });
});

// Minimal task/release/link factories for the release-badge tests below.
function taskBead(
  id: string,
  contentOverrides: Partial<DevTaskContent> = {},
  rest: Partial<Bead> = {},
): Bead {
  const content: DevTaskContent = {
    lane: "code-health",
    title: "Some work",
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
  return bead({ id, content: content as unknown as Record<string, unknown>, ...rest });
}

function releaseCharterBead(id: string, contentOverrides: Partial<ArchReleaseContent> = {}): Bead {
  const content: ArchReleaseContent = {
    ref: "R26.02",
    name: "Traceability closes the loop",
    objective: "Every task names its release.",
    sprints: [],
    outcomes: [{ id: "O-2", statement: "The board shows what work is for.", work_class: "enabling" }],
    declared_balance: {},
    opened_at: "2026-08-25T00:00:00Z",
    ...contentOverrides,
  };
  return bead({
    id,
    namespace: "arch",
    type: "release",
    content: content as unknown as Record<string, unknown>,
  });
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

describe("deliversMapFromLinks", () => {
  it("keys the map by the source task, not the release", () => {
    const charter = releaseCharterBead("release-1");
    const map = deliversMapFromLinks(
      [charter],
      new Map([["release-1", [deliversLink("task-1", "release-1")]]]),
    );
    expect(map.get("task-1")).toBe(charter);
  });

  it("ignores link rows of a different type on the same charter", () => {
    const charter = releaseCharterBead("release-1");
    const gatesLink: BeadLink = { ...deliversLink("task-1", "release-1"), link_type: "gates" };
    const map = deliversMapFromLinks([charter], new Map([["release-1", [gatesLink]]]));
    expect(map.has("task-1")).toBe(false);
  });

  it("stays bounded by the charters passed in, not by any task population", () => {
    // No charter -> no lookup happens at all, even if a links map somehow
    // carried entries — the fan-out this function drives is per-charter.
    const map = deliversMapFromLinks([], new Map([["release-1", [deliversLink("task-1", "release-1")]]]));
    expect(map.size).toBe(0);
  });
});

describe("releaseBadgeFor — what a card says about the work it serves", () => {
  it("resolves release and outcome from a delivers edge, not from task content", () => {
    const charter = releaseCharterBead("release-1");
    const task = taskBead("task-1", { outcome_ref: "O-2" });

    const badge = releaseBadgeFor(task, charter);

    expect(badge.status).toBe("delivering");
    expect(badge.releaseRef).toBe("R26.02");
    expect(badge.outcomeId).toBe("O-2");
    expect(badge.outcomeStatement).toBe("The board shows what work is for.");
    expect(badge.workClass).toBe("enabling");
  });

  it("never reads a delivering release off task content — a resolved edge is required", () => {
    // A stray release-ref-shaped string in content must not be mistaken for
    // a resolved `delivers` edge (R26.02/O-1's whole point).
    const task = taskBead("task-1");
    (task.content as Record<string, unknown>).release_ref = "R26.02";

    const badge = releaseBadgeFor(task, undefined);

    expect(badge.status).toBe("unknown");
  });

  it("renders a written waiver distinctly from delivering work", () => {
    const task = taskBead("task-1", {
      release_ref_waived: "Spike; no chartered outcome applies yet.",
    });

    const badge = releaseBadgeFor(task, undefined);

    expect(badge.status).toBe("waived");
    expect(badge.waiverReason).toBe("Spike; no chartered outcome applies yet.");
  });

  it("prefers a resolved delivers edge over a stale waiver string", () => {
    const charter = releaseCharterBead("release-1");
    const task = taskBead("task-1", {
      outcome_ref: "O-2",
      release_ref_waived: "no longer true",
    });

    expect(releaseBadgeFor(task, charter).status).toBe("delivering");
  });

  it("renders unknown, not waived, when neither an edge nor a waiver is present", () => {
    const task = taskBead("task-1");

    const badge = releaseBadgeFor(task, undefined);

    expect(badge.status).toBe("unknown");
    expect(badge.waiverReason).toBeUndefined();
  });

  it("treats a blank waiver string the same as no waiver — unknown, not waived", () => {
    const task = taskBead("task-1", { release_ref_waived: "   " });

    expect(releaseBadgeFor(task, undefined).status).toBe("unknown");
  });
});


describe("dependentsByPredecessorId — the same edge read the other way", () => {
  it("indexes tasks by the predecessor they name, matching guards.py::dependents_of", () => {
    const blocker = taskBead("p1", { title: "blocker" });
    const dependentA = taskBead("dA", { predecessor_bead_ids: ["p1"] });
    const dependentB = taskBead("dB", { predecessor_bead_ids: ["p1", "p2"] });

    const index = dependentsByPredecessorId([blocker, dependentA, dependentB]);

    expect(index.get("p1")?.map((t) => t.id)).toEqual(["dA", "dB"]);
    expect(index.get("p2")?.map((t) => t.id)).toEqual(["dB"]);
  });

  it("returns an empty index when nothing declares a predecessor", () => {
    expect(dependentsByPredecessorId([taskBead("a"), taskBead("b")]).size).toBe(0);
  });
});

describe("taskRunnabilityFrom — the board's only interpretation of the served guards.is_runnable answer", () => {
  function servedResponse(overrides: Partial<TaskRunnabilityResponse> = {}): TaskRunnabilityResponse {
    return { status: "ok", found: true, runnable: true, ...overrides };
  }

  it("reports runnable when the served answer says runnable", () => {
    const response = servedResponse({ runnable: true });
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "runnable" });
  });

  it("reports blocked with the authority's own reason, verbatim, when the served answer says blocked", () => {
    const response = servedResponse({
      runnable: false,
      reason: "waiting on blocking question note-q1: does this need a migration?",
    });
    expect(taskRunnabilityFrom(response)).toEqual({
      kind: "blocked",
      reason: "waiting on blocking question note-q1: does this need a migration?",
    });
  });

  it("passes through a reason it has never seen before — no local reason-text matching", () => {
    // This is the point of the redesign: a brand-new guards.py rule that
    // never existed when this code was written still renders correctly,
    // because nothing here branches on what the reason string says.
    const response = servedResponse({
      runnable: false,
      reason: "a rule that does not exist yet triggered a brand-new block",
    });
    expect(taskRunnabilityFrom(response)).toEqual({
      kind: "blocked",
      reason: "a rule that does not exist yet triggered a brand-new block",
    });
  });

  it("renders unknown, not runnable or blocked, when the authority could not be reached", () => {
    const response: TaskRunnabilityResponse = {
      status: "unknown",
      detail: "factory-dispatcher checkout not found",
    };
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "unknown" });
  });

  it("renders unknown when the served task list does not include this task", () => {
    const response = servedResponse({ found: false, runnable: undefined });
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "unknown" });
  });

  it("renders unknown, never a default that reads as a real answer, when there is no response at all", () => {
    // The pre-fetch / failed-fetch case: no local fallback computation runs.
    expect(taskRunnabilityFrom(undefined)).toEqual({ kind: "unknown" });
  });

  it("falls back to a non-empty reason if the authority somehow sends a blank one", () => {
    const response = servedResponse({ runnable: false, reason: "   " });
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "blocked", reason: "blocked" });
  });

  it("reports not_served_here, distinct from unknown, when the server says the checkout was never configured (OPS-110)", () => {
    const response: TaskRunnabilityResponse = {
      status: "unknown",
      code: "not_configured",
      detail: "factory-dispatcher checkout not configured (set FACTORY_STATUS_REPO_ROOT)",
    };
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "not_served_here" });
  });

  it("still reports plain unknown when the server says the checkout is configured but unavailable", () => {
    const response: TaskRunnabilityResponse = {
      status: "unknown",
      code: "unavailable",
      detail: "factory-dispatcher checkout not found at /some/path",
    };
    expect(taskRunnabilityFrom(response)).toEqual({ kind: "unknown" });
  });
});

describe("DEV_LANES", () => {
  it("matches the intake contract's lanes, so the picker can never offer a lane the dispatcher would refuse (or vice versa)", () => {
    expect(DEV_LANES).toEqual(taskIntakeContract.lanes);
  });
});
