import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead, FinanceAccountContent, FinanceBalanceSnapshotContent } from "@/types/bead";

// Account types Plaid classifies as liabilities. Mirrors the same set used in
// WealthDashboard — kept local so this hook is self-contained.
const LIABILITY_ACCOUNT_TYPES = new Set(["credit", "loan"]);

function isLiability(c: FinanceAccountContent): boolean {
  return LIABILITY_ACCOUNT_TYPES.has(c.type) || Boolean(c.liabilities);
}

export interface NetWorthPoint {
  day: string; // YYYY-MM-DD (the snapshot day)
  date: string; // alias of `day`, kept for display call sites
  assets: number;
  liabilities: number;
  netWorth: number;
}

export interface BalanceHistory {
  points: NetWorthPoint[];
  series: number[]; // netWorth only, for <Sparkline data={...} />
  // Per-account balance series keyed by the account *bead id* (matches
  // balance_snapshot.account_id), for inline sparklines.
  seriesByAccount: Record<string, number[]>;
  latest: NetWorthPoint | null;
  first: NetWorthPoint | null;
  // The point closest to (but not after) `daysAgo` before the latest — for MoM
  // deltas that stay internally consistent even when snapshots are sparse.
  prior: (daysAgo: number) => NetWorthPoint | null;
  isLoading: boolean;
}

// Reconstructs net worth over time from finance.balance_snapshot beads, which
// are written nightly per-account but never visualized. Snapshots are sparse
// (an account only appears on days it was synced), so we forward-fill: walk
// days in order, keep each account's most recent balance, and recompute the
// signed total at every day that has at least one new snapshot. Liability sign
// comes from the accounts map (snapshots don't carry account type).
export function useBalanceHistory(days = 180): BalanceHistory {
  const accountsQuery = useQuery({
    queryKey: ["finance-accounts"],
    queryFn: () => substrateClient.listBeads({ namespace: "finance", type: "account", limit: 200 }),
  });

  const snapshotsQuery = useQuery({
    queryKey: ["finance-balance-snapshots", days],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "balance_snapshot",
        // Pull generously; we trim to the window after sorting by as_of.
        limit: 2000,
      }),
  });

  return useMemo<BalanceHistory>(() => {
    const accounts = accountsQuery.data ?? [];
    const snapshots = snapshotsQuery.data ?? [];

    // balance_snapshot.content.account_id is the substrate *account bead id*
    // (finance_reconciliation.write_balance_snapshots_activity passes a["id"]),
    // NOT the Plaid account id — so the join key is the bead id, not
    // plaid_account_id.
    const liabilityById = new Map<string, boolean>();
    for (const a of accounts) {
      const c = a.content as unknown as FinanceAccountContent;
      liabilityById.set(a.id, isLiability(c));
    }

    // Normalize + sort by day. Bucket every snapshot under its YYYY-MM-DD.
    type Snap = { day: string; accountId: string; balance: number };
    const snaps: Snap[] = [];
    for (const b of snapshots as Bead[]) {
      const c = b.content as unknown as FinanceBalanceSnapshotContent;
      if (!c.as_of || c.balance == null || !c.account_id) continue;
      const day = c.as_of.slice(0, 10);
      snaps.push({ day, accountId: c.account_id, balance: c.balance });
    }
    snaps.sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));

    // Forward-fill: running per-account balance, emit one point per distinct day.
    const running = new Map<string, number>();
    const seriesByAccount: Record<string, number[]> = {};
    const points: NetWorthPoint[] = [];
    let i = 0;
    while (i < snaps.length) {
      const day = snaps[i].day;
      while (i < snaps.length && snaps[i].day === day) {
        running.set(snaps[i].accountId, snaps[i].balance);
        (seriesByAccount[snaps[i].accountId] ??= []).push(snaps[i].balance);
        i += 1;
      }
      let assets = 0;
      let liabilities = 0;
      for (const [accountId, bal] of running) {
        if (liabilityById.get(accountId)) liabilities += bal;
        else assets += bal;
      }
      points.push({ day, date: day, assets, liabilities, netWorth: assets - liabilities });
    }

    // Trim to the requested window (last `days` from the most recent point).
    let windowed = points;
    if (points.length > 0 && days > 0) {
      const cutoff = new Date(points[points.length - 1].day);
      cutoff.setDate(cutoff.getDate() - days);
      const cutoffStr = cutoff.toISOString().slice(0, 10);
      windowed = points.filter((p) => p.day >= cutoffStr);
    }

    const prior = (daysAgo: number): NetWorthPoint | null => {
      if (windowed.length === 0) return null;
      const cut = new Date(windowed[windowed.length - 1].day);
      cut.setDate(cut.getDate() - daysAgo);
      const cutStr = cut.toISOString().slice(0, 10);
      let p: NetWorthPoint | null = null;
      for (const point of windowed) {
        if (point.day <= cutStr) p = point;
        else break;
      }
      return p ?? windowed[0];
    };

    return {
      points: windowed,
      series: windowed.map((p) => p.netWorth),
      seriesByAccount,
      latest: windowed.length ? windowed[windowed.length - 1] : null,
      first: windowed.length ? windowed[0] : null,
      prior,
      isLoading: accountsQuery.isLoading || snapshotsQuery.isLoading,
    };
  }, [accountsQuery.data, accountsQuery.isLoading, snapshotsQuery.data, snapshotsQuery.isLoading, days]);
}
