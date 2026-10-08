import type { DataProvider } from "@refinedev/core";
import { substrateClient } from "./substrate-client";
import { SUBSTRATE_PROXY } from "@/lib/env";
import type { Bead } from "@/types/bead";

// Refine routes resources through `resource` → we use it to pre-filter by
// namespace where the route name maps 1:1 to a Substrate namespace.
// "beads" passes through unfiltered (Bead Explorer); "finance" / "vision"
// filter at the data layer so the resource components stay dumb.
const RESOURCE_NAMESPACE: Record<string, string | undefined> = {
  beads: undefined,
  finance: "finance",
  vision: "vision",
  personal: "personal",
  platform: "platform",
};

interface SubstrateFilter {
  field: string;
  operator: string;
  value: unknown;
}

function filtersToParams(filters: readonly SubstrateFilter[] | undefined): Record<string, string | undefined> {
  const out: Record<string, string | undefined> = {};
  if (!filters) return out;
  for (const f of filters) {
    // Substrate only supports equality on namespace/type/state/trust_tier,
    // plus created_after. Anything richer is filtered client-side.
    if (f.operator !== "eq") continue;
    if (f.value === undefined || f.value === null || f.value === "") continue;
    if (["namespace", "type", "state", "trust_tier", "created_after"].includes(f.field)) {
      out[f.field] = String(f.value);
    }
  }
  return out;
}

export const substrateDataProvider: DataProvider = {
  getApiUrl: () => SUBSTRATE_PROXY,

  async getList({ resource, pagination, filters }) {
    const ns = RESOURCE_NAMESPACE[resource];
    const filterParams = filtersToParams(filters as SubstrateFilter[] | undefined);

    const pageSize = pagination?.pageSize ?? 50;
    const current = pagination?.current ?? 1;
    const offset = (current - 1) * pageSize;

    // No total count from Substrate. Ask for one extra row to detect hasMore.
    const limit = pageSize + 1;
    const rows = await substrateClient.listBeads({
      namespace: ns ?? filterParams.namespace,
      type: filterParams.type,
      state: filterParams.state,
      trust_tier: filterParams.trust_tier,
      created_after: filterParams.created_after,
      limit,
      offset,
    });

    const hasMore = rows.length > pageSize;
    const data = hasMore ? rows.slice(0, pageSize) : rows;

    // Refine wants a numeric total; we approximate with offset + page
    // (+1 if there's another page). Pagination UI must use hasMore, not total.
    const total = offset + data.length + (hasMore ? 1 : 0);

    return { data: data as unknown as Bead[] as never, total };
  },

  async getOne({ id }) {
    const bead = await substrateClient.getBead(String(id));
    return { data: bead as unknown as never };
  },

  async create({ variables }) {
    const bead = await substrateClient.createBead(variables as Partial<Bead>);
    return { data: bead as unknown as never };
  },

  async update({ id, variables }) {
    const bead = await substrateClient.updateBead(String(id), variables as Parameters<typeof substrateClient.updateBead>[1]);
    return { data: bead as unknown as never };
  },

  async deleteOne({ id }) {
    await substrateClient.deleteBead(String(id));
    return { data: { id } as unknown as never };
  },

  // Custom: semantic search + event log. Surfaced via useCustom or direct calls.
  async custom({ url, method, payload }) {
    if (url === "search" && method === "post") {
      const p = payload as { query: string; limit?: number; namespace?: string; type?: string };
      const hits = await substrateClient.semanticSearch(p.query, {
        limit: p.limit,
        namespace: p.namespace,
        type: p.type,
      });
      return { data: hits as unknown as never };
    }
    if (url.startsWith("events/") && method === "get") {
      const id = url.slice("events/".length);
      const events = await substrateClient.listEvents(id);
      return { data: events as unknown as never };
    }
    throw new Error(`Unsupported custom call: ${method} ${url}`);
  },

  getMany: async ({ ids }) => {
    const beads = await Promise.all(ids.map((id) => substrateClient.getBead(String(id))));
    return { data: beads as unknown as never };
  },
};
