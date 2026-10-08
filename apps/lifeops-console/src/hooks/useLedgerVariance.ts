import { useQuery } from "@tanstack/react-query";
import { LEDGER_VARIANCE_REFRESH_INTERVAL_MS } from "@/components/LedgerVariancePanel";
import { mcpClient } from "@/providers/mcp-client";

export interface LedgerVarianceQueryState {
  isError: boolean;
  isRefetchError: boolean;
}

export function ledgerVarianceQueryOptions() {
  return {
    queryKey: ["finance-ledger-variance"] as const,
    queryFn: () => mcpClient.getLedgerVariance(),
    refetchInterval: LEDGER_VARIANCE_REFRESH_INTERVAL_MS,
  };
}

export function ledgerVarianceIsUnavailable(query: LedgerVarianceQueryState): boolean {
  return query.isError || query.isRefetchError;
}

export function useLedgerVariance() {
  return useQuery(ledgerVarianceQueryOptions());
}
