// Client for mcp-hub's /factory status endpoints — the served answers to
// "can this dev.task start, and why not" and "how much of a release is
// delivered", produced by importing and calling the dispatcher's own
// guards.is_runnable / scripts/release-status.py rather than re-deriving
// them (see apps/mcp-hub/src/tools/factory_status.py). Same-origin: nginx
// proxies /api/ to mcp-hub, exactly like mcp-client.ts's /finance calls. No
// key in the browser.

import type { TaskRunnabilityResponse } from "@/lib/dev-board";
import type { FactoryScheduleStatusResponse } from "@/lib/factory-status";
import type { ImpactKind, ImpactResponse } from "@/lib/impact";
import type { ReleaseDeliveryResponse } from "@/lib/release-view";
import type { DevNoteContent } from "@/types/bead";

class FactoryStatusError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const versionedPath = path.startsWith("/api/v1") ? path : `/api/v1${path}`;
  const resp = await fetch(versionedPath, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!resp.ok) {
    let body: unknown = null;
    try {
      body = await resp.json();
    } catch {
      body = await resp.text();
    }
    throw new FactoryStatusError(resp.status, body, `mcp-hub ${resp.status} on ${versionedPath}`);
  }
  return (await resp.json()) as T;
}

// What POST /api/v1/factory/tasks returns on success — file_dev_task's own
// summary of the filed bead, not the full bead (the gateway process files
// through file_task.file_spec and hands back only what it computed; the
// board re-reads the full bead over its existing listBeads query rather than
// this route also shipping a second bead-shape contract to keep in sync).
export interface FiledTaskResult {
  status: "ok";
  id: string;
  lane: string | null;
  title: string | null;
  risk_class: string | null;
  state: string;
}

// What POST /api/v1/factory/note returns on success — factory_file_note's
// own summary of the filed bead (routers/v1/factory.py), not the full bead:
// TaskThreadPanel re-reads the full thread over its existing listBeads
// query rather than this route also shipping a second bead-shape contract
// to keep in sync.
export interface FiledNoteResult {
  status: "ok";
  id: string | null;
  parent_id: string | null;
  kind: string | null;
  state: string;
}

export const factoryStatusClient = {
  taskRunnable(taskId: string): Promise<TaskRunnabilityResponse> {
    return request<TaskRunnabilityResponse>("/factory/task_runnable", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    });
  },

  // "How much of a release's declared work is delivered" — read-only server
  // side (require_authenticated_scope("factory.read"); no bead is ever
  // written by this call). Returns release-status.py's build_release_report
  // for `releaseRef`, computed once behind mcp-hub and never re-derived here
  // — see apps/lifeops-console/src/lib/release-view.ts.
  releaseDelivery(releaseRef: string): Promise<ReleaseDeliveryResponse> {
    return request<ReleaseDeliveryResponse>("/factory/release_delivery", {
      method: "POST",
      body: JSON.stringify({ release_ref: releaseRef }),
    });
  },

  // Whether the dispatch schedule is paused, its pause note verbatim, and
  // the outcome/time of its most recent firings (S54-B / R26.09 O-10, read
  // half only) -- GET, read-only server side
  // (require_authenticated_scope("factory.read"), same as taskRunnable/
  // releaseDelivery above). Returns tools.factory_schedule_status
  // .dispatch_schedule_status's own reused-reader answer; never re-derived
  // here. See apps/lifeops-console/src/lib/factory-status.ts.
  scheduleStatus(): Promise<FactoryScheduleStatusResponse> {
    return request<FactoryScheduleStatusResponse>("/factory/schedule_status");
  },

  // What a change/application/CI affects over the EA graph's typed edges,
  // with the coverage the answer is computed on stated beside it (R26.09/O-4,
  // PC-ASR-002/AC-1) -- tools.impact.get_impact's own computation, served
  // read-only (require_authenticated_scope("factory.read")). Replaces the
  // Architecture view's client-side blast-radius.ts walk for application/ci
  // origins; see apps/lifeops-console/src/lib/impact.ts's module comment for
  // why "service" is not a valid kind here.
  impact(kind: ImpactKind, ref: string): Promise<ImpactResponse> {
    return request<ImpactResponse>("/factory/impact", {
      method: "POST",
      body: JSON.stringify({ kind, ref }),
    });
  },

  // Files a dev.task bead through the one intake (file_task.file_spec,
  // called both by the CLI and this route — PRIN-005) instead of a raw
  // substrate write. A refusal (duplicate spec identity, unresolvable
  // release_ref, forbidden scope path, ...) comes back as a 422 with the
  // intake's own message in `detail`; an unconfigured gateway comes back as
  // a 503. See refusalMessage below for surfacing either in the UI.
  fileTask(spec: Record<string, unknown>): Promise<FiledTaskResult> {
    return request<FiledTaskResult>("/factory/tasks", {
      method: "POST",
      body: JSON.stringify(spec),
    });
  },

  // Files a dev.note bead through the gateway's intake capability instead of
  // writing the substrate directly (this bead: TaskThreadPanel's half of the
  // console's dev.* intake gap). Server-side this checks
  // require_authenticated_scope("factory.write"), the same scope
  // POST /api/v1/factory/tasks uses; provenance and trust_tier are both
  // derived from the Access-derived client identity there, never carried by
  // this payload. `JSON.stringify` drops an `undefined` `releases_work`
  // entirely, so a caller that never sets it sends no such key at all — the
  // gateway must never invent one either (this bead's AC-4).
  fileNote(input: DevNoteContent & { parent_id: string }): Promise<FiledNoteResult> {
    return request<FiledNoteResult>("/factory/note", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
};

// Pulls the intake's own refusal text out of a FactoryStatusError's body
// (FastAPI's `{"detail": "..."}"`), so a 422/503 from fileTask reads the same
// in the console as it would from file_task.py's CLI (PC-FAC-001/AC-5).
// Falls back to the error's own message, then a caller-supplied default, for
// anything that isn't shaped like an HTTPException body (a network failure,
// a non-JSON 500).
export function refusalMessage(error: unknown, fallback: string): string {
  if (error instanceof FactoryStatusError) {
    const body = error.body;
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
    }
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export { FactoryStatusError };
