import { useQuery } from "@tanstack/react-query";
import { INSTITUTION_HEALTH_REFRESH_INTERVAL_MS } from "@/components/InstitutionHealthBoard";
import { mcpClient } from "@/providers/mcp-client";

export interface InstitutionHealthQueryState {
  isError: boolean;
  isRefetchError: boolean;
}

// Probed (probe=true): the freshness board is a truth-telling surface, so it
// needs the live per-institution data_freshness read, not the beads-only fast
// path Connections uses for its instant first paint.
export function institutionHealthQueryOptions() {
  return {
    queryKey: ["institution-health"] as const,
    queryFn: () => mcpClient.getConnections(true),
    refetchInterval: INSTITUTION_HEALTH_REFRESH_INTERVAL_MS,
  };
}

export function institutionHealthIsUnavailable(query: InstitutionHealthQueryState): boolean {
  return query.isError || query.isRefetchError;
}

export function useInstitutionHealth() {
  return useQuery(institutionHealthQueryOptions());
}
