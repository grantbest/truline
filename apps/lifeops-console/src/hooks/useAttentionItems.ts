import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type {
  Bead,
  FinanceActionRequiredContent,
  FinanceAnomalyContent,
  FinanceBudgetRecommendationContent,
  FinanceDiscrepancyContent,
  FinanceInsightAllocationContent,
} from "@/types/bead";
import { fmtCurrency, fmtPercent } from "@/lib/format";

// One normalized card for the cross-domain attention feed. Every pending
// insight bead — drift, anomalies, trial alerts, optimizer moves, budget
// rebases — collapses into this shape so the Command Center and nav badges
// can rank them together instead of each route owning its own silo.
export type Severity = "high" | "medium" | "opportunity" | "low";

export interface AttentionItem {
  id: string;
  beadType: string;
  severity: Severity;
  title: string;
  detail: string;
  amount: number | null; // representative dollar figure for sorting/display
  amountLabel: string | null; // e.g. "drift", "at risk", "saves/yr"
  route: string; // deep-link to the route that resolves it
  createdAt: string;
  bead: Bead;
}

export const SEVERITY_ORDER: Record<Severity, number> = {
  high: 0,
  medium: 1,
  opportunity: 2,
  low: 3,
};

export const SEVERITY_TONE: Record<Severity, "neg" | "warn" | "accent" | "neutral"> = {
  high: "neg",
  medium: "warn",
  opportunity: "accent",
  low: "neutral",
};

// Each pending insight type pulled with a stable query key so the Layout's
// nav badges and the Today route share one cache entry instead of double-fetching.
function usePending(type: string) {
  return useQuery({
    queryKey: ["attention", type],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "finance", type, state: "pending", limit: 100 }),
    staleTime: 30_000,
  });
}

export interface AttentionFeed {
  items: AttentionItem[];
  total: number;
  byRoute: Record<string, number>;
  bySeverity: Record<Severity, number>;
  isLoading: boolean;
  // A failed query resolves to no data, which is indistinguishable from a
  // genuinely empty feed unless someone asks. Without this the Command Center
  // renders "✓ All clear — nothing needs you right now" when substrate is
  // simply unreachable — the same "green means absent" defect as the /audit
  // route, on the one surface the Operator checks first.
  isError: boolean;
  // True only when EVERY source has actually returned. See the note below —
  // `!isLoading` is not the same thing, and the difference is a false all-clear.
  isReady: boolean;
}

export function useAttentionItems(): AttentionFeed {
  const discrepancies = usePending("audit_discrepancy");
  const anomalies = usePending("review_anomaly");
  const actions = usePending("action_required");
  const allocations = usePending("insight_allocation");
  const budgetRecs = usePending("budget_recommendation");

  return useMemo<AttentionFeed>(() => {
    const items: AttentionItem[] = [];

    for (const b of discrepancies.data ?? []) {
      const c = b.content as unknown as FinanceDiscrepancyContent;
      items.push({
        id: b.id,
        beadType: b.type,
        severity: "high",
        title: "Ledger drift",
        detail: `${c.account_name ?? c.account_id} diverged from the bank's reported balance`,
        amount: Math.abs(c.drift ?? 0),
        amountLabel: "drift",
        route: "/audit",
        createdAt: b.created_at,
        bead: b,
      });
    }

    for (const b of anomalies.data ?? []) {
      const c = b.content as unknown as FinanceAnomalyContent;
      const dup = c.reason === "duplicate_charge";
      items.push({
        id: b.id,
        beadType: b.type,
        severity: dup ? "high" : "medium",
        title: dup ? "Duplicate charge" : "Price hike",
        detail: dup
          ? `${c.vendor ?? "merchant"} charged ${c.count ?? 2}× ${fmtCurrency(c.amount)}`
          : `${c.vendor ?? "merchant"} up ${fmtPercent(c.change_pct ?? 0, 0)} vs baseline`,
        amount: c.amount ?? null,
        amountLabel: dup ? "duplicated" : "now",
        route: "/anomalies",
        createdAt: b.created_at,
        bead: b,
      });
    }

    for (const b of actions.data ?? []) {
      const c = b.content as unknown as FinanceActionRequiredContent;
      const urgent = (c.days_until ?? 99) <= 3;
      items.push({
        id: b.id,
        beadType: b.type,
        severity: urgent ? "high" : "medium",
        title: "Trial ending",
        detail: `${c.vendor ?? "subscription"} converts in ${c.days_until ?? "?"}d — cancel to avoid the charge`,
        amount: c.amount_at_risk ?? null,
        amountLabel: "at risk",
        route: "/anomalies",
        createdAt: b.created_at,
        bead: b,
      });
    }

    for (const b of allocations.data ?? []) {
      const c = b.content as unknown as FinanceInsightAllocationContent;
      items.push({
        id: b.id,
        beadType: b.type,
        severity: "opportunity",
        title: "Idle cash → debt",
        detail: `Move ${fmtCurrency(c.amount)} to ${c.target_account_name ?? "a high-APR balance"}`,
        amount: c.projected_annual_savings ?? null,
        amountLabel: "saves/yr",
        route: "/wealth",
        createdAt: b.created_at,
        bead: b,
      });
    }

    for (const b of budgetRecs.data ?? []) {
      const c = b.content as unknown as FinanceBudgetRecommendationContent;
      items.push({
        id: b.id,
        beadType: b.type,
        severity: "low",
        title: "Budget off base",
        detail: `${c.category}: ${c.direction} cap to ${fmtCurrency(c.suggested_cap)} (${c.reason})`,
        amount: c.suggested_cap ?? null,
        amountLabel: "new cap",
        route: "/budget",
        createdAt: b.created_at,
        bead: b,
      });
    }

    // Rank: severity first, then largest dollars, then most recent.
    items.sort((a, b) => {
      const s = SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity];
      if (s !== 0) return s;
      const amt = (b.amount ?? 0) - (a.amount ?? 0);
      if (amt !== 0) return amt;
      return a.createdAt < b.createdAt ? 1 : -1;
    });

    const byRoute: Record<string, number> = {};
    const bySeverity: Record<Severity, number> = { high: 0, medium: 0, opportunity: 0, low: 0 };
    for (const it of items) {
      byRoute[it.route] = (byRoute[it.route] ?? 0) + 1;
      bySeverity[it.severity] += 1;
    }

    return {
      items,
      total: items.length,
      byRoute,
      bySeverity,
      isLoading:
        discrepancies.isLoading ||
        anomalies.isLoading ||
        actions.isLoading ||
        allocations.isLoading ||
        budgetRecs.isLoading,
      // ANY source failing makes the feed incomplete. Reporting "all clear"
      // off a partial read is the failure mode, so this is deliberately an
      // OR and not a count of how many succeeded.
      isError:
        discrepancies.isError ||
        anomalies.isError ||
        actions.isError ||
        allocations.isError ||
        budgetRecs.isError,
      // Gate the all-clear on every source having actually SUCCEEDED.
      //
      // `isLoading` is not that gate. In react-query v5 isLoading is
      // `isPending && isFetching`, so a query that is pending but not
      // currently in flight — between retries, or paused — reports
      // isLoading=false AND isError=false. Verified against the running
      // console on 2026-08-02: all five sources sat at status="pending"
      // while the page rendered "✓ All clear — nothing needs you right now"
      // with every request returning 500.
      //
      // That is the same defect as the /audit green and the connections
      // "healthy": an unknown rendered as good news, on the surface the Operator
      // checks first.
      isReady:
        discrepancies.status === "success" &&
        anomalies.status === "success" &&
        actions.status === "success" &&
        allocations.status === "success" &&
        budgetRecs.status === "success",
    };
  }, [
    discrepancies.data,
    discrepancies.isLoading,
    anomalies.data,
    anomalies.isLoading,
    actions.data,
    actions.isLoading,
    allocations.data,
    allocations.isLoading,
    budgetRecs.data,
    budgetRecs.isLoading,
    discrepancies.status,
    anomalies.status,
    actions.status,
    allocations.status,
    budgetRecs.status,
    discrepancies.isError,
    anomalies.isError,
    actions.isError,
    allocations.isError,
    budgetRecs.isError,
  ]);
}
