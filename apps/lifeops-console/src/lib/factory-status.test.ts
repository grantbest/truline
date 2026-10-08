import { describe, expect, it } from "vitest";
import type { Bead } from "@/types/bead";
import type { TaskRunnability } from "@/lib/dev-board";
import {
  factoryScheduleViewFrom,
  queueCountsFrom,
  queueViewFrom,
  workerRevisionViewFrom,
  WORKER_REVISION_DRIFT_REF,
  type FactoryScheduleStatusResponse,
  type QueueCounts,
  type WorkerRevisionDriftContext,
} from "@/lib/factory-status";

// AC-6: every fixture below is built in-process. No test here opens a real
// Temporal client, reads the live store, or pauses/unpauses/triggers/
// terminates anything.

function task(id: string, state: string): Bead {
  return {
    id,
    namespace: "dev",
    type: "task",
    state,
    parent_id: null,
    context: {},
    content: { lane: "code-health", title: id },
    confidence: null,
    trust_tier: "system",
    provenance: {},
    created_by: "test",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
  };
}

function observationBead(state: string, context: WorkerRevisionDriftContext): Bead {
  return {
    id: "obs-worker-revision-drift-1",
    namespace: "arch",
    type: "observation",
    state,
    parent_id: null,
    context: context as unknown as Record<string, unknown>,
    content: { ref: WORKER_REVISION_DRIFT_REF },
    confidence: null,
    trust_tier: "system",
    provenance: {},
    created_by: "factory-dispatcher/worker-revision-drift",
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
  };
}

// ---------------------------------------------------------------------------
// Schedule half
// ---------------------------------------------------------------------------

describe("factoryScheduleViewFrom", () => {
  it("renders the paused case with the note verbatim and no combined health flag", () => {
    const response: FactoryScheduleStatusResponse = {
      status: "ok",
      schedule_id: "factory-dispatcher-dev",
      paused: true,
      note: "factory dispatcher paused: capacity backpressure; nightly window",
      in_flight: [],
      recent: [{ workflow_id: "wf-1", scheduled_at: "2026-09-20T10:00:00Z", status: "COMPLETED", failed: false, succeeded: true }],
      last_success_at: "2026-09-20T10:00:00Z",
      consecutive_failures: 0,
      recent_failures: 0,
    };
    const view = factoryScheduleViewFrom(response, null);
    expect(view.kind).toBe("ok");
    if (view.kind !== "ok") throw new Error("unreachable");
    expect(view.paused).toBe(true);
    expect(view.note).toBe("factory dispatcher paused: capacity backpressure; nightly window");
    expect(view.inFlightCount).toBe(0);
    // No `genuinely_quiet`/`healthy` field exists on the type at all --
    // TypeScript itself would reject `view.healthy`. Runtime pin: the two
    // facts a caller can read are `paused` and `inFlightCount`, nothing
    // combines them.
    expect(Object.keys(view)).not.toContain("healthy");
    expect(Object.keys(view)).not.toContain("genuinelyQuiet");
  });

  it("renders the unpaused case with in-flight and recent both visible as separate facts", () => {
    const response: FactoryScheduleStatusResponse = {
      status: "ok",
      schedule_id: "factory-dispatcher-dev",
      paused: false,
      note: "factory dispatcher unattended drain",
      in_flight: [{ workflow_id: "wf-running", run_id: "run-1", scheduled_at: "2026-09-23T09:00:00Z", started_at: "2026-09-23T09:00:05Z" }],
      recent: [{ workflow_id: "wf-0", scheduled_at: "2026-09-23T08:45:00Z", status: "FAILED", failed: true, succeeded: false }],
      last_success_at: "",
      consecutive_failures: 1,
      recent_failures: 1,
    };
    const view = factoryScheduleViewFrom(response, null);
    expect(view.kind).toBe("ok");
    if (view.kind !== "ok") throw new Error("unreachable");
    expect(view.paused).toBe(false);
    expect(view.inFlightCount).toBe(1);
    expect(view.consecutiveFailures).toBe(1);
    expect(view.lastSuccessAt).toBe("");
  });

  it("renders the cannot-reach-Temporal case as a declared unknown, never an empty success", () => {
    const response: FactoryScheduleStatusResponse = {
      status: "unknown",
      detail: "could not reach Temporal: connection refused",
    };
    const view = factoryScheduleViewFrom(response, null);
    expect(view).toEqual({ kind: "unknown", reason: "could not reach Temporal: connection refused" });
  });

  it("renders a fetch-level failure as unknown too", () => {
    const view = factoryScheduleViewFrom(undefined, new Error("network error"));
    expect(view).toEqual({ kind: "unknown", reason: "network error" });
  });
});

// ---------------------------------------------------------------------------
// Queue half
// ---------------------------------------------------------------------------

describe("queueCountsFrom", () => {
  it("counts by state and splits pending into held/ready/unknown without reimplementing the breaker", () => {
    const tasks = [
      task("held-1", "pending"),
      task("ready-1", "pending"),
      task("ready-2", "pending"),
      task("unknown-1", "pending"),
      task("doing-1", "doing"),
      task("done-1", "done"),
    ];
    const runnability = new Map<string, TaskRunnability>([
      ["held-1", { kind: "blocked", reason: "predecessor not done" }],
      ["ready-1", { kind: "runnable" }],
      ["ready-2", { kind: "runnable" }],
      ["unknown-1", { kind: "unknown" }],
      // doing-1 intentionally has no entry -- non-pending tasks are not
      // bucketed into held/ready/unknown at all.
    ]);

    const counts = queueCountsFrom(tasks, runnability);

    expect(counts.byState.pending).toBe(4);
    expect(counts.byState.doing).toBe(1);
    expect(counts.byState.done).toBe(1);
    expect(counts.pendingHeld).toBe(1);
    expect(counts.pendingReadyExcludingBreaker).toBe(2);
    expect(counts.pendingUnknown).toBe(1);
  });

  it("treats a pending task with no served runnability answer at all as unknown, not ready", () => {
    const tasks = [task("no-answer", "pending")];
    const counts = queueCountsFrom(tasks, new Map());
    expect(counts.pendingHeld).toBe(0);
    expect(counts.pendingReadyExcludingBreaker).toBe(0);
    expect(counts.pendingUnknown).toBe(1);
  });
});

// The #991 release-gate defect: FactoryStatusPanel was fed
// `tasks = tasksQuery.data ?? []`, so a pending or failed tasks query
// rendered as a confident "0 pending / 0 doing / ... / 0 held, 0 ready, 0
// unknown" instead of a declared unknown -- the same shape #982 was
// returned for. These pin `queueViewFrom` never doing that: it must fail
// against 9de25a60 (queueViewFrom did not exist there; queue was rendered
// straight from queueCountsFrom's all-zero output) and pass here.
describe("queueViewFrom", () => {
  const zeroCounts: QueueCounts = {
    byState: { pending: 0, doing: 0, review: 0, done: 0, failed: 0, superseded: 0, archived: 0 },
    pendingHeld: 0,
    pendingReadyExcludingBreaker: 0,
    pendingUnknown: 0,
  };

  it("renders unknown, never a confident zero, while the board's tasks query has not loaded yet", () => {
    const view = queueViewFrom(false, false, zeroCounts);
    expect(view.kind).toBe("unknown");
    if (view.kind !== "unknown") throw new Error("unreachable");
    expect(view.reason).toContain("not loaded yet");
  });

  it("renders unknown, never a confident zero, when the board's initial load failed", () => {
    const view = queueViewFrom(false, true, zeroCounts);
    expect(view.kind).toBe("unknown");
    if (view.kind !== "unknown") throw new Error("unreachable");
    expect(view.reason).toContain("failed to load");
  });

  it("renders the real counts once the board has genuinely loaded data", () => {
    const counts: QueueCounts = {
      byState: { pending: 4, doing: 1, review: 0, done: 1, failed: 0, superseded: 0, archived: 0 },
      pendingHeld: 1,
      pendingReadyExcludingBreaker: 2,
      pendingUnknown: 1,
    };
    const view = queueViewFrom(true, false, counts);
    expect(view).toEqual({ kind: "ok", ...counts });
  });
});

// ---------------------------------------------------------------------------
// Worker half — the AC-4 resolved-observation trap, driven from an
// in-process fixture (never the live obs.worker-revision-drift bead, whose
// exact fields have changed shape between 2026-09-20 and 2026-09-23).
// ---------------------------------------------------------------------------

describe("workerRevisionViewFrom", () => {
  it("renders an active observation's revision/commits_behind as current", () => {
    const bead = observationBead("active", {
      condition: "drifted",
      worker_revision: "5a3606fd",
      commits_behind: 11,
      last_observed_at: "2026-09-23T12:00:00Z",
    });
    const view = workerRevisionViewFrom([bead], null);
    expect(view).toEqual({
      kind: "active",
      condition: "drifted",
      workerRevision: "5a3606fd",
      commitsBehind: 11,
      lastObservedAt: "2026-09-23T12:00:00Z",
    });
  });

  it("never renders a resolved observation's frozen context as current (AC-4)", () => {
    // The exact live trap: state resolved, context still carrying the last
    // drifted values -- land_worker_revision_drift patches only `state` on
    // the clear transition and never touches `content`/`context`.
    const bead = observationBead("resolved", {
      condition: "drifted",
      worker_revision: "1fc66426",
      commits_behind: 16,
      last_observed_at: "2026-09-13T08:00:00Z",
    });
    const view = workerRevisionViewFrom([bead], null);
    expect(view.kind).toBe("resolved");
    if (view.kind !== "resolved") throw new Error("unreachable");
    // The values are still surfaced (as history), but under resolved/
    // "last known" naming -- no field on this branch is spelled the same
    // as the active branch's "current" fields, so a renderer cannot
    // mistake one for the other by accident.
    expect(view.lastKnownWorkerRevision).toBe("1fc66426");
    expect(view.lastKnownCommitsBehind).toBe(16);
    expect(view.asOfLastDriftedObservation).toBe("2026-09-13T08:00:00Z");
    expect(Object.keys(view)).not.toContain("workerRevision");
    expect(Object.keys(view)).not.toContain("commitsBehind");
  });

  it("handles commits_behind legitimately absent (not an ancestor of main), not coerced to 0", () => {
    const bead = observationBead("active", {
      condition: "unknown",
      worker_revision: "deadbeef",
      commits_behind: null,
      last_observed_at: "2026-09-23T12:00:00Z",
    });
    const view = workerRevisionViewFrom([bead], null);
    expect(view.kind).toBe("active");
    if (view.kind !== "active") throw new Error("unreachable");
    expect(view.commitsBehind).toBeNull();
  });

  it("renders no record found only for a genuine empty read -- zero rows, no error", () => {
    expect(workerRevisionViewFrom([], null)).toEqual({ kind: "not_found" });
  });

  // The #991 release-gate defect: the call site collapsed
  // `workerRevisionDriftQuery.data?.[0] ?? null` before calling this
  // function, so a still-pending or failed query (`data === undefined`)
  // rendered the identical "No drift-check record found yet." text as a
  // genuine empty read. These pin the fix -- both must fail against 9de25a60
  // (the pre-fix signature took only a bead and could not express "the
  // query hasn't resolved" separately from "no such bead") and pass here.
  it("renders unknown, never 'no record found', while the query has not resolved yet", () => {
    const view = workerRevisionViewFrom(undefined, null);
    expect(view.kind).toBe("unknown");
    if (view.kind !== "unknown") throw new Error("unreachable");
    expect(view.reason).toContain("not loaded yet");
  });

  it("renders unknown, never 'no record found', when the query failed", () => {
    const view = workerRevisionViewFrom(undefined, new Error("substrate unavailable"));
    expect(view).toEqual({ kind: "unknown", reason: "substrate unavailable" });
  });
});
