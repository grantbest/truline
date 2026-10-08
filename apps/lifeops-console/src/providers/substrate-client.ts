import { SUBSTRATE_PROXY } from "@/lib/env";
import type { Bead, BeadEvent, BeadLink, BeadSearchHit } from "@/types/bead";

class SubstrateError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

// All Substrate calls flow through a same-origin proxy that injects the
// API key server-side. The browser never sees or sends X-API-Key.
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${SUBSTRATE_PROXY}${path}`, {
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
    throw new SubstrateError(resp.status, body, `Substrate ${resp.status} on ${path}`);
  }
  // DELETE returns {status:"deleted"}, others return JSON
  return (await resp.json()) as T;
}

// SDD Phase 4 — Rule Sandbox. The dry-run backtests a draft rule against
// recent transactions (read-only, Tier 2 cache) before commit mutates anything.
export type RuleOperator = "contains" | "equals" | "starts_with" | "regex";

export interface RuleSpec {
  field: string;
  operator: RuleOperator;
  value: string;
  target_category: string;
}

export interface RuleSampleDiff {
  id: string;
  vendor: string;
  old: string;
  new: string;
  will_change: boolean;
}

export interface RuleDryRunResult {
  matched: number;
  beads_affected: number;
  scanned: number;
  sample_diffs: RuleSampleDiff[];
}

export interface ListBeadsParams {
  namespace?: string;
  type?: string;
  state?: string;
  trust_tier?: string;
  // Server-side since PR #173. Fetching a dev.task's note thread is one
  // query rather than a full-table pull grouped client-side.
  parent_id?: string;
  // Server-side unique-ref lookup (apps/substrate/src/routes.py's
  // `content_ref`, filtering on `content->>'ref'`) -- already supported by
  // the backend; this client just hadn't exposed it. Lets a caller fetch
  // one standing arch.observation bead (e.g. "obs.worker-revision-drift")
  // by its idempotency key instead of paging the whole population and
  // filtering client-side, the way Architecture.tsx does for the generic
  // case (OPS-181/OPS-182: a page-size scan is exactly how that lookup
  // stopped finding its target in production once volume grew).
  content_ref?: string;
  created_after?: string;
  limit?: number;
  offset?: number;
}

function buildQuery(params: Record<string, unknown>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export const substrateClient = {
  async listBeads(params: ListBeadsParams = {}): Promise<Bead[]> {
    return request<Bead[]>(`/beads${buildQuery({ ...params })}`);
  },

  async getBead(id: string): Promise<Bead> {
    return request<Bead>(`/beads/${id}`);
  },

  async createBead(payload: Partial<Bead>): Promise<Bead> {
    return request<Bead>(`/beads`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async updateBead(
    id: string,
    payload: Partial<Pick<Bead, "state" | "parent_id" | "content" | "context" | "confidence"> & { created_by?: string }>,
  ): Promise<Bead> {
    return request<Bead>(`/beads/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async deleteBead(id: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/beads/${id}`, { method: "DELETE" });
  },

  async listEvents(beadId: string): Promise<BeadEvent[]> {
    return request<BeadEvent[]>(`/beads/${beadId}/events`);
  },

  async listBeadLinks(
    beadId: string,
    opts: { direction?: "incoming" | "outgoing" | "both"; link_type?: string } = {},
  ): Promise<BeadLink[]> {
    return request<BeadLink[]>(
      `/beads/${beadId}/links${buildQuery({
        direction: opts.direction ?? "both",
        link_type: opts.link_type,
      })}`,
    );
  },

  async dryRunRule(rule: RuleSpec): Promise<RuleDryRunResult> {
    return request<RuleDryRunResult>(`/rules/dry-run`, {
      method: "POST",
      body: JSON.stringify(rule),
    });
  },

  async semanticSearch(
    query: string,
    opts: { limit?: number; namespace?: string; type?: string } = {},
  ): Promise<BeadSearchHit[]> {
    return request<BeadSearchHit[]>(`/beads/search`, {
      method: "POST",
      body: JSON.stringify({
        query,
        limit: opts.limit ?? 20,
        namespace: opts.namespace,
        type: opts.type,
      }),
    });
  },
};

export { SubstrateError };
