import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ArchReleaseContent } from "@/lib/ea-model";
import taskIntakeContract from "@/lib/task-intake-contract.json";
import type { Bead, DevTaskContent } from "@/types/bead";
import {
  FACTORY_BOARD_REFRESH_INTERVAL_MS,
  taskRunnabilityFrom,
  type TaskIntakeDraft,
  type TaskRunnability,
  type TaskRunnabilityResponse,
} from "@/lib/dev-board";
import type { QueueView } from "@/lib/factory-status";
import {
  FactoryStatusPanel,
  TaskCard,
  TaskIntakeForm,
  boardInitialLoadFailed,
  boardRefreshFailed,
  factoryBoardNotesQueryOptions,
  factoryBoardTasksQueryOptions,
  taskForOpenDetail,
} from "@/routes/FactoryBoard";

function task(overrides: Partial<Bead> & Pick<Bead, "id">): Bead {
  const content: DevTaskContent = {
    lane: "code-health",
    title: "Keep the board fresh",
    intent: "Show dispatcher state changes without a reload.",
    context_refs: [],
    acceptance: ["The board polls."],
    verification: { commands: ["npm test"] },
    scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
    risk_class: "behavioral",
    budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
    autonomy: "propose",
  };

  return {
    namespace: "dev",
    type: "task",
    state: "pending",
    parent_id: null,
    context: {},
    content: content as unknown as Record<string, unknown>,
    confidence: null,
    trust_tier: "system",
    provenance: {},
    created_by: "test",
    created_at: "2026-08-06T00:00:00Z",
    updated_at: "2026-08-06T00:00:00Z",
    ...overrides,
  };
}

describe("factory board polling", () => {
  it("configures task and note queries on the same named refresh interval", () => {
    expect(factoryBoardTasksQueryOptions().refetchInterval).toBe(
      FACTORY_BOARD_REFRESH_INTERVAL_MS,
    );
    expect(factoryBoardNotesQueryOptions().refetchInterval).toBe(
      FACTORY_BOARD_REFRESH_INTERVAL_MS,
    );
  });
});

describe("factory board stale data handling", () => {
  it("keeps last-known data displayable when a refresh fails", () => {
    const tasks = [task({ id: "claimed", state: "doing" })];
    const refreshError = new Error("substrate unavailable");
    const tasksQuery = {
      data: tasks,
      isError: false,
      isRefetchError: true,
      error: refreshError,
    };
    const notesQuery = {
      data: [],
      isError: false,
      isRefetchError: false,
      error: null,
    };

    expect(boardInitialLoadFailed(tasksQuery, notesQuery)).toBe(false);
    expect(boardRefreshFailed(tasksQuery, notesQuery)).toBe(true);
    expect(tasksQuery.data).toEqual(tasks);
  });

  it("marks retained note data stale when the note refresh fails", () => {
    const tasksQuery = {
      data: [task({ id: "claimed", state: "doing" })],
      isError: false,
      isRefetchError: false,
      error: null,
    };
    const notesQuery = {
      data: [],
      isError: false,
      isRefetchError: true,
      error: new Error("note refresh failed"),
    };

    expect(boardInitialLoadFailed(tasksQuery, notesQuery)).toBe(false);
    expect(boardRefreshFailed(tasksQuery, notesQuery)).toBe(true);
  });

  it("still reports an initial load failure when no cached board data exists", () => {
    const tasksQuery = {
      data: undefined,
      isError: true,
      isRefetchError: false,
      error: new Error("no task data"),
    };
    const notesQuery = {
      data: undefined,
      isError: false,
      isRefetchError: false,
      error: null,
    };

    expect(boardInitialLoadFailed(tasksQuery, notesQuery)).toBe(true);
  });
});

describe("TaskCard — the id a person is holding must be findable on the board", () => {
  const FIXTURE_ID = "751bf004-aaaa-bbbb-cccc-dddddddddddd";

  function blockingQuestionNote(): Bead {
    return task({
      id: "note-1",
      namespace: "dev",
      type: "note",
      content: { kind: "question", body: "Which environment?", blocking: true } as unknown as Record<
        string,
        unknown
      >,
    });
  }

  it("renders the bead's first 8 id characters — the form the platform already uses elsewhere", () => {
    const fixture = task({ id: FIXTURE_ID, created_at: "2026-08-06T00:00:00Z" });
    const html = renderToStaticMarkup(
      <TaskCard dependents={[]} task={fixture} notes={[]} showState={false} onSelect={() => {}} />,
    );

    expect(html).toContain(FIXTURE_ID.slice(0, 8));
  });

  it("keeps every field the card already showed rendering alongside the new identifier", () => {
    const fixture = task({
      id: FIXTURE_ID,
      state: "doing",
      created_at: "2026-08-06T00:00:00Z",
      content: {
        lane: "code-health",
        title: "Keep the board fresh",
        intent: "Show dispatcher state changes without a reload.",
        context_refs: [],
        acceptance: ["The board polls."],
        verification: { commands: ["npm test"] },
        scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
        risk_class: "behavioral",
        budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
        autonomy: "auto-merge-eligible",
        worker_hint: "claude",
        attempts: 2,
        max_attempts: 3,
      } as unknown as Record<string, unknown>,
    });

    // The served answer, stubbed — the card must render this and only this,
    // not re-derive blocking-ness from the note below.
    const runnability: TaskRunnability = {
      kind: "blocked",
      reason: "waiting on blocking question note-1: Which environment?",
    };

    const html = renderToStaticMarkup(
      <TaskCard
        dependents={[]}
        task={fixture}
        notes={[blockingQuestionNote()]}
        showState
        runnability={runnability}
        onSelect={() => {}}
      />,
    );

    // Pre-existing fields the card must not lose.
    expect(html).toContain("code-health"); // lane badge
    expect(html).toContain("doing"); // state badge
    expect(html).toContain("auto-merge"); // auto-merge badge
    expect(html).toContain("Keep the board fresh"); // title
    expect(html).toMatch(/Blocked.*waiting on blocking question/); // blocking-question notice
    expect(html).toContain("claude"); // worker_hint
    expect(html).toContain("1 notes"); // note count
    expect(html).toContain("2/3"); // attempt count
    expect(html).toMatch(/ago|just now/); // age

    // The new identifier, alongside everything above.
    expect(html).toContain(FIXTURE_ID.slice(0, 8));
  });
});

describe("TaskCard — which orchestrator ran the work, distinct from worker_hint", () => {
  function cardHtml(overrides: Partial<DevTaskContent>): string {
    const fixture = task({
      id: "ran-by-fixture",
      content: {
        lane: "code-health",
        title: "Some task",
        intent: "i",
        context_refs: [],
        acceptance: ["a"],
        verification: { commands: ["npm test"] },
        scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
        risk_class: "behavioral",
        budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
        worker_hint: "claude",
        ...overrides,
      } as unknown as Record<string, unknown>,
    });
    return renderToStaticMarkup(
      <TaskCard dependents={[]} task={fixture} notes={[]} showState={false} onSelect={() => {}} />,
    );
  }

  it("shows worker_hint as intent only, with no run recorded yet", () => {
    const html = cardHtml({});
    expect(html).toContain("hint:claude");
    expect(html).not.toContain("ran:");
  });

  it("shows the recorded ran_by fact alongside, but distinct from, worker_hint", () => {
    const html = cardHtml({ ran_by: "factory-dispatcher" } as unknown as Partial<DevTaskContent>);
    expect(html).toContain("hint:claude");
    expect(html).toContain("ran:factory-dispatcher");
  });

  it("shows a declared non-dispatcher lane as itself, not folded into the dispatcher", () => {
    const html = cardHtml({ ran_by: "gastown" } as unknown as Partial<DevTaskContent>);
    expect(html).toContain("ran:gastown");
    expect(html).not.toContain("ran:factory-dispatcher");
  });

  it("shows an unrecognized writer as unrecognized, never silently mapped to the dispatcher", () => {
    const html = cardHtml({
      ran_by: "unrecognized:some-mystery-writer",
    } as unknown as Partial<DevTaskContent>);
    expect(html).toContain("ran:unrecognized:some-mystery-writer");
    expect(html).not.toContain("ran:factory-dispatcher");
  });
});

describe("TaskCard — what the work is for", () => {
  function releaseCharter(overrides: Partial<ArchReleaseContent> = {}): Bead {
    const content: ArchReleaseContent = {
      ref: "R26.02",
      name: "Traceability closes the loop",
      objective: "Every task names its release.",
      sprints: [],
      outcomes: [
        { id: "O-2", statement: "The board shows what work is for.", work_class: "enabling" },
      ],
      declared_balance: {},
      opened_at: "2026-08-25T00:00:00Z",
      ...overrides,
    };
    return task({
      id: "release-1",
      namespace: "arch",
      type: "release",
      content: content as unknown as Record<string, unknown>,
    });
  }

  it("renders the release and outcome for work carrying a delivers edge", () => {
    const fixture = task({
      id: "task-1",
      content: {
        lane: "code-health",
        title: "Keep the board fresh",
        intent: "Show dispatcher state changes without a reload.",
        context_refs: [],
        acceptance: ["The board polls."],
        verification: { commands: ["npm test"] },
        scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
        risk_class: "behavioral",
        budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
        autonomy: "propose",
        outcome_ref: "O-2",
      } as unknown as Record<string, unknown>,
    });

    const html = renderToStaticMarkup(
      <TaskCard
        dependents={[]}
        task={fixture}
        notes={[]}
        showState={false}
        releaseCharter={releaseCharter()}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain("R26.02");
    expect(html).toContain("O-2");
    expect(html).toContain("The board shows what work is for.");
  });

  it("renders a waived card distinctly from a delivering card", () => {
    const waived = task({
      id: "task-2",
      content: {
        lane: "code-health",
        title: "A spike with no chartered outcome",
        intent: "Explore an approach.",
        context_refs: [],
        acceptance: ["A finding is recorded."],
        verification: { commands: ["npm test"] },
        scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
        risk_class: "behavioral",
        budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
        autonomy: "propose",
        release_ref_waived: "Spike; no chartered outcome applies yet.",
      } as unknown as Record<string, unknown>,
    });

    const deliveringHtml = renderToStaticMarkup(
      <TaskCard
        dependents={[]}
        task={task({ id: "task-1" })}
        notes={[]}
        showState={false}
        releaseCharter={releaseCharter()}
        onSelect={() => {}}
      />,
    );
    const waivedHtml = renderToStaticMarkup(
      <TaskCard
        dependents={[]} task={waived} notes={[]} showState={false} onSelect={() => {}} />,
    );

    expect(waivedHtml).toContain("waived");
    expect(waivedHtml).toContain("Spike; no chartered outcome applies yet.");
    expect(waivedHtml).not.toContain("R26.02");
    expect(deliveringHtml).not.toContain("waived");
  });

  it("renders an unresolvable release as unknown, never as waived", () => {
    const fixture = task({ id: "task-3" });

    const html = renderToStaticMarkup(
      <TaskCard
        dependents={[]} task={fixture} notes={[]} showState={false} onSelect={() => {}} />,
    );

    expect(html).toContain("release unknown");
    expect(html).not.toContain("waived");
  });
});

describe("TaskCard — dependentsByPredecessorId is visible on the board (the reverse edge, not a runnability decision)", () => {
  it("identifies a task other work waits on — the same edge read the other way", () => {
    const dependent = task({ id: "dependent" });
    const blocked = task({
      id: "blocked-1",
      content: {
        lane: "code-health",
        title: "Downstream work held on this one",
        intent: "…",
        context_refs: [],
        acceptance: ["…"],
        verification: { commands: [] },
        scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
        risk_class: "behavioral",
        budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
        autonomy: "propose",
      } as unknown as Record<string, unknown>,
    });

    const html = renderToStaticMarkup(
      <TaskCard task={dependent} notes={[]} dependents={[blocked]} showState={false} onSelect={() => {}} />,
    );

    expect(html).toMatch(/blocks 1/);
    expect(html).toContain("Downstream work held on this one");
  });
});

describe("TaskCard — runnability is read from the served answer, never computed locally", () => {
  it("reports a task as blocked when the served answer says blocked, driven by a stubbed read", () => {
    // "Driven by a stubbed read": this is what a fetch of
    // POST /api/v1/factory/task_runnable would return for a task held on an
    // unlanded predecessor — guards.py's own ordering_block_reason wording.
    const served: TaskRunnabilityResponse = {
      status: "ok",
      found: true,
      runnable: false,
      reason:
        "waiting for predecessor bead 86400de6-a7f0-4f26-a90e-cbc08b589b72 to land (state=doing)",
    };
    const runnability = taskRunnabilityFrom(served);
    const fixture = task({ id: "dependent" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={runnability}
        onSelect={() => {}}
      />,
    );

    expect(html).toMatch(/Blocked/);
    expect(html).toContain(
      "waiting for predecessor bead 86400de6-a7f0-4f26-a90e-cbc08b589b72 to land (state=doing)",
    );
  });

  it("renders any reason the authority sends, even one invented after this code was written", () => {
    // Proves the card does not pattern-match on reason text: a rule that did
    // not exist when this test was written still renders correctly, because
    // guards.py alone controls what shows here (the acceptance criterion
    // this test exists to demonstrate).
    const served: TaskRunnabilityResponse = {
      status: "ok",
      found: true,
      runnable: false,
      reason: "a brand-new guards.py rule invented after this UI shipped",
    };
    const fixture = task({ id: "dependent" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={taskRunnabilityFrom(served)}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain("a brand-new guards.py rule invented after this UI shipped");
  });

  it("renders the unknown case as unknown, not as movable and not as blocked", () => {
    const served: TaskRunnabilityResponse = {
      status: "unknown",
      detail: "factory-dispatcher checkout not found",
    };
    const fixture = task({ id: "dependent" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={taskRunnabilityFrom(served)}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain("Runnability unknown");
    expect(html).not.toMatch(/Blocked/);
    expect(html).not.toContain("border-warn");
  });

  it("renders no notice at all for closed/terminal work, which is never queried for runnability", () => {
    const fixture = task({ id: "done-task", state: "done" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState
        runnability={undefined}
        onSelect={() => {}}
      />,
    );

    expect(html).not.toMatch(/Blocked/);
    expect(html).not.toContain("Runnability unknown");
  });

  it("renders nothing when the served answer says runnable", () => {
    const fixture = task({ id: "dependent" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={{ kind: "runnable" }}
        onSelect={() => {}}
      />,
    );

    expect(html).not.toMatch(/Blocked/);
    expect(html).not.toContain("Runnability unknown");
  });
});

describe("TaskCard — ready and blocked are distinguishable without opening the card", () => {
  it("shows a ready marker on a pending, runnable card that a blocked card does not carry", () => {
    const readyTask = task({ id: "ready-1", state: "pending" });
    const blockedTask = task({ id: "blocked-1", state: "pending" });

    const readyHtml = renderToStaticMarkup(
      <TaskCard
        task={readyTask}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={{ kind: "runnable" }}
        onSelect={() => {}}
      />,
    );
    const blockedHtml = renderToStaticMarkup(
      <TaskCard
        task={blockedTask}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={{
          kind: "blocked",
          reason: "waiting for predecessor bead 86400de6 to land (state=doing)",
        }}
        onSelect={() => {}}
      />,
    );

    expect(readyHtml).toContain("Ready to start");
    expect(readyHtml).not.toMatch(/Blocked/);
    expect(blockedHtml).toMatch(/Blocked/);
    // Verbatim, not just the marker: a card that says "Blocked" without the
    // authority's own words is the defect this spec removes.
    expect(blockedHtml).toContain(
      "waiting for predecessor bead 86400de6 to land (state=doing)",
    );
    expect(blockedHtml).not.toContain("Ready to start");
  });

  it("renders a lone open blocking question even when a different reason holds the card", () => {
    // Release-gate finding on #625: with exactly one open question and a
    // predecessor reason in the authority slot, the question was silently
    // hidden — the silent-pick the spec's acceptance forbids.
    const blocked = task({ id: "blocked-2", state: "pending" });
    const question = task({
      id: "q-1",
      namespace: "dev",
      type: "note",
      content: { kind: "question", body: "Which lane should this run in?", blocking: true } as unknown as Record<
        string,
        unknown
      >,
    });

    const html = renderToStaticMarkup(
      <TaskCard
        task={blocked}
        notes={[question]}
        dependents={[]}
        showState={false}
        runnability={{
          kind: "blocked",
          reason: "waiting for predecessor bead 86400de6 to land (state=doing)",
        }}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain("waiting for predecessor bead 86400de6 to land (state=doing)");
    expect(html).toContain("1 open blocking question");
    expect(html).toContain("Which lane should this run in?");
  });

  it("shows no ready marker once the task has left the pending queue, even if still reported runnable", () => {
    const fixture = task({ id: "doing-1", state: "doing" });

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={[]}
        dependents={[]}
        showState={false}
        runnability={{ kind: "runnable" }}
        onSelect={() => {}}
      />,
    );

    expect(html).not.toContain("Ready to start");
  });
});

describe("TaskCard — several reasons can hold one bead at once", () => {
  function blockingNote(id: string, body: string): Bead {
    return task({
      id,
      namespace: "dev",
      type: "note",
      content: { kind: "question", body, blocking: true } as unknown as Record<string, unknown>,
    });
  }

  it("surfaces every open blocking question, not only the one the authority's reason names", () => {
    // guards.is_runnable's own reason string only ever names the first
    // blocking question (guards.py:767 `pending[0]`); a second, independent
    // blocking question filed on the same bead is real and must not vanish.
    const fixture = task({ id: "held" });
    const notes = [
      blockingNote("q1", "Does this need a data migration?"),
      blockingNote("q2", "Should this wait for the R26.06 charter to open?"),
    ];
    const runnability: TaskRunnability = {
      kind: "blocked",
      reason: "waiting on blocking question q1: Does this need a data migration?",
    };

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={notes}
        dependents={[]}
        showState={false}
        runnability={runnability}
        onSelect={() => {}}
      />,
    );

    expect(html).toContain(
      "waiting on blocking question q1: Does this need a data migration?",
    );
    expect(html).toContain("Should this wait for the R26.06 charter to open?");
  });

  it("adds no 'other reasons' notice when only one blocking question is open", () => {
    const fixture = task({ id: "held-1" });
    const notes = [blockingNote("q1", "Does this need a data migration?")];
    const runnability: TaskRunnability = {
      kind: "blocked",
      reason: "waiting on blocking question q1: Does this need a data migration?",
    };

    const html = renderToStaticMarkup(
      <TaskCard
        task={fixture}
        notes={notes}
        dependents={[]}
        showState={false}
        runnability={runnability}
        onSelect={() => {}}
      />,
    );

    expect(html).not.toContain("open blocking questions");
  });
});

describe("factory board open detail", () => {
  it("keeps an open task detail attached to refreshed content", () => {
    const selected = task({ id: "claimed", state: "pending" });
    const refreshed = task({ id: "claimed", state: "doing" });

    expect(taskForOpenDetail(selected, [refreshed])).toBe(refreshed);
  });

  it("does not close the detail while a refetch result omits the task", () => {
    const selected = task({ id: "claimed", state: "pending" });

    expect(taskForOpenDetail(selected, [])).toBe(selected);
  });
});

describe("TaskIntakeForm — the lane picker", () => {
  it("renders one option per contract lane, feature included, instead of a hand-typed subset", () => {
    const draft: TaskIntakeDraft = {
      lane: "code-health",
      title: "",
      intent: "",
      acceptance: "",
      scopePaths: "",
      forbiddenPaths: "",
      risk_class: "behavioral",
      verificationCommands: "",
      requirementRefs: "",
      nfrs: "",
      archImpact: "",
    };

    const html = renderToStaticMarkup(
      <TaskIntakeForm
        draft={draft}
        error={null}
        isSaving={false}
        onChange={() => {}}
        onCancel={() => {}}
        onSubmit={() => {}}
      />,
    );

    // Scoped to the Lane <select> specifically — the Risk <select> a few
    // lines down also renders <option value="X">X</option> pairs (behavioral,
    // structural), which a bare document-wide scan would conflate with lanes.
    const laneSelectStart = html.indexOf("<select", html.indexOf("Lane</span>"));
    const laneSelectHtml = html.slice(laneSelectStart, html.indexOf("</select>", laneSelectStart));
    // React SSR adds a `selected=""` attribute ahead of `value` on whichever
    // option matches the controlled value, so the attribute order isn't fixed.
    const renderedLanes = [...laneSelectHtml.matchAll(/<option[^>]*\svalue="([^"]+)"/g)].map(
      (m) => m[1],
    );
    expect(renderedLanes).toEqual(taskIntakeContract.lanes);
  });
});

describe("FactoryStatusPanel — is the factory running, per S54-B / R26.09 O-10", () => {
  const emptyQueue: QueueView = {
    kind: "ok",
    byState: { pending: 0, doing: 0, review: 0, done: 0, failed: 0, superseded: 0, archived: 0 },
    pendingHeld: 0,
    pendingReadyExcludingBreaker: 0,
    pendingUnknown: 0,
  };

  it("never shows a single combined health verdict for paused+in-flight (AC-4)", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{
          kind: "ok",
          scheduleId: "factory-dispatcher-dev",
          paused: true,
          note: "factory dispatcher paused: capacity backpressure",
          inFlightCount: 2,
          recent: [],
          lastSuccessAt: "",
          consecutiveFailures: 0,
          recentFailures: 0,
        }}
        queue={emptyQueue}
        worker={{ kind: "not_found" }}
      />,
    );
    expect(html).toContain("paused");
    expect(html).toContain("2 in flight");
    expect(html).not.toContain("healthy");
    expect(html).not.toContain("quiet");
    expect(html).toContain("capacity backpressure");
  });

  it("renders the cannot-reach-Temporal case as unknown, not an empty/blank panel", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "could not reach Temporal: connection refused" }}
        queue={emptyQueue}
        worker={{ kind: "not_found" }}
      />,
    );
    expect(html).toContain("Unknown");
    expect(html).toContain("could not reach Temporal");
  });

  it("labels the ready count as excluding the environmental-fault breaker (AC-3)", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "n/a" }}
        queue={{ ...emptyQueue, pendingHeld: 1, pendingReadyExcludingBreaker: 3, pendingUnknown: 1 }}
        worker={{ kind: "not_found" }}
      />,
    );
    expect(html).toContain("1 held");
    expect(html).toContain("3 ready");
    expect(html).toContain("excludes the environmental-fault breaker");
  });

  it("renders a resolved worker-revision observation as historical, never as the current revision (AC-4/AC-5)", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "n/a" }}
        queue={emptyQueue}
        worker={{
          kind: "resolved",
          lastKnownWorkerRevision: "1fc66426",
          lastKnownCommitsBehind: 16,
          asOfLastDriftedObservation: "2026-09-13T08:00:00Z",
        }}
      />,
    );
    expect(html).toContain("resolved");
    expect(html).toContain("1fc66426");
    expect(html).toContain("historical, not");
  });

  it("renders an active worker-revision observation's fields as current", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "n/a" }}
        queue={emptyQueue}
        worker={{
          kind: "active",
          condition: "drifted",
          workerRevision: "5a3606fd",
          commitsBehind: 3,
          lastObservedAt: "2026-09-23T12:00:00Z",
        }}
      />,
    );
    expect(html).toContain("5a3606fd");
    expect(html).toContain("3 commits behind main");
    expect(html).not.toContain("historical");
  });

  // The #991 release-gate defect, pinned at the panel level: the queue and
  // worker columns used to render a confident zero / "no record found" when
  // their backing reads were pending or had failed -- the schedule column
  // already got this right (see the "Unknown -- ..." test above). These two
  // must fail against 9de25a60 and pass here.
  it("renders the queue column as unknown, never a confident zero, when the tasks read could not answer (AC per #991 requeue)", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "n/a" }}
        queue={{ kind: "unknown", reason: "the task list has not loaded yet" }}
        worker={{ kind: "not_found" }}
      />,
    );
    expect(html).toContain("Unknown");
    expect(html).toContain("the task list has not loaded yet");
    expect(html).not.toContain("0 pending");
    expect(html).not.toContain("0 doing");
  });

  it("renders the worker column as unknown, never 'no record found', when the drift read could not answer (AC per #991 requeue)", () => {
    const html = renderToStaticMarkup(
      <FactoryStatusPanel
        schedule={{ kind: "unknown", reason: "n/a" }}
        queue={emptyQueue}
        worker={{ kind: "unknown", reason: "substrate unavailable" }}
      />,
    );
    expect(html).toContain("Unknown");
    expect(html).toContain("substrate unavailable");
    expect(html).not.toContain("No drift-check record found yet.");
  });
});
