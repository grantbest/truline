import type { Bead, DevTaskState } from "@/types/bead";
import { DEV_TASK_STATES } from "@/types/bead";
import type { TaskRunnability } from "@/lib/dev-board";

// Pure projection helpers for the Factory Board's "is the factory running"
// panel (S54-B / R26.09 O-10, read half only). Kept out of the route
// component, same reason release-view.ts's logic is: the honesty rules here
// — never render a conjunction as health, never render a resolved
// observation's frozen context as current — are exactly the kind of thing
// that needs to be testable without a DOM, against fixtures, never the live
// store (AC-6).

// -----------------------------------------------------------------------
// Schedule half — GET /api/v1/factory/schedule_status. Mirrors
// tools.factory_schedule_status.dispatch_schedule_status's return dict
// exactly, which itself is schedule_runtime.describe_factory_schedule_status
// (the dispatcher's own reader) plus the pause note read alongside it. See
// apps/mcp-hub/src/tools/factory_schedule_status.py.
// -----------------------------------------------------------------------

export interface FactoryInFlightWorkflow {
  workflow_id: string;
  run_id: string;
  scheduled_at: string;
  started_at: string;
}

export interface FactoryDrainOutcome {
  workflow_id: string;
  scheduled_at: string;
  status: string;
  failed: boolean;
  succeeded: boolean;
}

export interface FactoryScheduleStatusResponse {
  status: "ok" | "unknown";
  detail?: string;
  schedule_id?: string;
  paused?: boolean;
  note?: string | null;
  in_flight?: FactoryInFlightWorkflow[];
  recent?: FactoryDrainOutcome[];
  last_success_at?: string;
  consecutive_failures?: number;
  recent_failures?: number;
}

// What the panel renders: `paused` and `in_flight` are always two separate
// facts (AC-4) — this type has no combined "healthy"/"quiet" member on
// purpose, so a caller cannot accidentally collapse them the way
// `FactoryScheduleStatus.genuinely_quiet` does server-side. `unknown` covers
// both "the request failed" and the server's own declared cannot-evaluate
// shape (PRIN-015) — there is no separate "not_configured" here because,
// unlike factory_status.py's checkout-dependent answers, this route's only
// failure mode is Temporal/the dispatcher checkout being unreachable right
// now, not a permanent by-design absence.
export type FactoryScheduleView =
  | { kind: "unknown"; reason: string }
  | {
      kind: "ok";
      scheduleId: string;
      paused: boolean;
      note: string | null;
      inFlightCount: number;
      recent: FactoryDrainOutcome[];
      lastSuccessAt: string;
      consecutiveFailures: number;
      recentFailures: number;
    };

export function factoryScheduleViewFrom(
  response: FactoryScheduleStatusResponse | undefined,
  fetchError: unknown,
): FactoryScheduleView {
  if (fetchError) {
    return {
      kind: "unknown",
      reason: fetchError instanceof Error ? fetchError.message : "schedule status request failed",
    };
  }
  if (!response || response.status !== "ok") {
    return { kind: "unknown", reason: response?.detail?.trim() || "schedule status is unknown" };
  }
  return {
    kind: "ok",
    scheduleId: response.schedule_id ?? "",
    paused: response.paused ?? false,
    note: response.note ?? null,
    inFlightCount: response.in_flight?.length ?? 0,
    recent: response.recent ?? [],
    lastSuccessAt: response.last_success_at ?? "",
    consecutiveFailures: response.consecutive_failures ?? 0,
    recentFailures: response.recent_failures ?? 0,
  };
}

// -----------------------------------------------------------------------
// Queue half — dev.task counts by state, and the pending subset split into
// held/ready/unknown. Computed entirely from data the board already fetches
// (AC-2): the task list, and the per-task runnability answers FactoryBoard
// already queries via factoryStatusClient.taskRunnable (guards.is_runnable,
// never re-derived here).
// -----------------------------------------------------------------------

export interface QueueCounts {
  byState: Record<DevTaskState, number>;
  /** Pending tasks the served answer calls blocked. */
  pendingHeld: number;
  /**
   * Pending tasks the served answer calls runnable. NOT "what the
   * dispatcher will pick next" — guards.is_runnable (what that served
   * answer calls) does not model dispatch.pick_task's
   * consecutive-environmental-fault breaker
   * (trailing_environmental_fault_streak /
   * CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT), so this can overcount what the
   * dispatcher would actually claim next (AC-3). Render this labeled
   * "excludes the environmental-fault breaker," never as a plain "ready"
   * count with no caveat.
   */
  pendingReadyExcludingBreaker: number;
  /** Pending tasks whose served runnability answer could not be read. */
  pendingUnknown: number;
}

export function queueCountsFrom(
  tasks: Bead[],
  runnabilityByTaskId: Map<string, TaskRunnability>,
): QueueCounts {
  const byState = Object.fromEntries(DEV_TASK_STATES.map((s) => [s, 0])) as Record<
    DevTaskState,
    number
  >;
  let pendingHeld = 0;
  let pendingReadyExcludingBreaker = 0;
  let pendingUnknown = 0;

  for (const task of tasks) {
    const state = task.state as DevTaskState;
    if (state in byState) byState[state] += 1;
    if (state !== "pending") continue;

    const runnability = runnabilityByTaskId.get(task.id);
    if (runnability?.kind === "blocked") pendingHeld += 1;
    else if (runnability?.kind === "runnable") pendingReadyExcludingBreaker += 1;
    else pendingUnknown += 1;
  }

  return { byState, pendingHeld, pendingReadyExcludingBreaker, pendingUnknown };
}

// queueCountsFrom above is a pure counter: given a `tasks` array, it always
// produces counts, including all-zero counts for an empty array. It cannot
// by itself tell "the board asked the store and got zero dev.task beads"
// apart from "the board's tasks query hasn't returned yet (or failed), so
// `tasks` is `[]` only because nothing has loaded." That distinction lives
// in the caller (FactoryBoardRoute already computes it via
// boardHasLoadedData/boardInitialLoadFailed), so this wrapper takes it as
// two booleans rather than re-deriving it from query internals — the same
// division `factoryScheduleViewFrom` and `workerRevisionViewFrom` draw
// between "pure projection" and "declared cannot-evaluate" (PRIN-015).
export type QueueView = { kind: "unknown"; reason: string } | ({ kind: "ok" } & QueueCounts);

export function queueViewFrom(
  hasLoadedData: boolean,
  loadFailed: boolean,
  counts: QueueCounts,
): QueueView {
  if (loadFailed) {
    return { kind: "unknown", reason: "the task list failed to load" };
  }
  if (!hasLoadedData) {
    return { kind: "unknown", reason: "the task list has not loaded yet" };
  }
  return { kind: "ok", ...counts };
}

// -----------------------------------------------------------------------
// Worker half — the obs.worker-revision-drift arch.observation bead's state
// read alongside its context (AC-4/AC-5). land_worker_revision_drift
// (apps/factory-dispatcher/activities/worker_revision_drift.py) patches
// only `state` (never `content`/`context`) the moment drift clears, so a
// `resolved` bead's context is frozen at whatever it was the instant it last
// cleared — potentially forever. Rendering that as current would be exactly
// the lie this bead exists to stop: `commits_behind: 16` shown next to a
// green "resolved" state reads as "16 behind and fine," when the truth is
// "was 16 behind once; no current number is available from this record."
// -----------------------------------------------------------------------

export const WORKER_REVISION_DRIFT_REF = "obs.worker-revision-drift";

export interface WorkerRevisionDriftContext {
  observation_kind?: string;
  condition?: "drifted" | "unknown" | "clear" | string;
  worker_revision?: string | null;
  // Optional even while active: only meaningful when the recorded revision
  // is an ancestor of main (worker_revision.py's own DriftStatus docstring)
  // — absent, not 0, when it isn't or couldn't be computed.
  commits_behind?: number | null;
  is_ancestor?: boolean | null;
  worker_started_at?: string | null;
  running_for_seconds?: number | null;
  main_ref?: string | null;
  main_revision?: string | null;
  error?: string | null;
  first_observed_at?: string;
  last_observed_at?: string;
  consecutive_deferrals?: number;
}

// `unknown` covers both "the substrate query hasn't resolved yet" (`data`
// is `undefined` — React Query's own signal for "no successful fetch yet")
// and "the query failed" — neither may render the same words as a genuine
// empty read. Only `data` actually being an empty array with no error means
// "asked the store, no such bead" — that, and only that, is `not_found`.
export type WorkerRevisionView =
  | { kind: "unknown"; reason: string }
  | { kind: "not_found" }
  | {
      kind: "active";
      condition: string;
      workerRevision: string | null;
      commitsBehind: number | null;
      lastObservedAt: string;
    }
  | {
      kind: "resolved";
      lastKnownWorkerRevision: string | null;
      lastKnownCommitsBehind: number | null;
      /** When the condition last cleared to resolved is not recorded by the
       * writer (only the last drifted observation's own timestamp is) — see
       * module comment. This is the last DRIFTED timestamp, explicitly
       * labeled as such by the caller, never presented as "now." */
      asOfLastDriftedObservation: string;
    };

export function workerRevisionViewFrom(
  data: Bead[] | undefined,
  fetchError: unknown,
): WorkerRevisionView {
  if (fetchError) {
    return {
      kind: "unknown",
      reason:
        fetchError instanceof Error ? fetchError.message : "worker revision drift query failed",
    };
  }
  if (data === undefined) {
    return { kind: "unknown", reason: "worker revision drift has not loaded yet" };
  }
  const bead = data[0] ?? null;
  if (!bead) return { kind: "not_found" };
  const context = (bead.context ?? {}) as WorkerRevisionDriftContext;

  if (bead.state === "resolved") {
    return {
      kind: "resolved",
      lastKnownWorkerRevision: context.worker_revision ?? null,
      lastKnownCommitsBehind: context.commits_behind ?? null,
      asOfLastDriftedObservation: context.last_observed_at ?? "",
    };
  }

  return {
    kind: "active",
    condition: context.condition ?? "unknown",
    workerRevision: context.worker_revision ?? null,
    commitsBehind: context.commits_behind ?? null,
    lastObservedAt: context.last_observed_at ?? "",
  };
}
