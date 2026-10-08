// Client for mcp-hub's /finance management endpoints (connections,
// schedules, sync-now). Same-origin: nginx proxies /finance/* to mcp-hub,
// exactly like the Plaid Link portal. No keys in the browser.

import type { Bead } from "@/types/bead";
import type { RuleSpec } from "./substrate-client";

class McpHubError extends Error {
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
    throw new McpHubError(resp.status, body, `mcp-hub ${resp.status} on ${versionedPath}`);
  }
  return (await resp.json()) as T;
}

export type ConnectionStatusKind =
  | "ok"
  | "degraded"
  | "reauth_required"
  | "relink_required"
  | "no_token"
  | "unprobed"
  | "unknown"
  // Data has not arrived inside this institution's own learned freshness SLO.
  // Distinct from `degraded`, which is about the provider answering.
  | "stale"
  // Too little cadence to derive an SLO, or a cadence wider than the lookback
  // window can see. Neither a pass nor a failure — never render it as green.
  | "freshness_unknown";

export interface ConnectionStatus {
  institution: string;
  token_present: boolean;
  item_id: string | null;
  linked_at: string | null;
  last_relinked_at: string | null;
  cursor_present: boolean;
  last_synced: string | null;
  account_count: number;
  accounts_last_synced: string | null;
  healthy: boolean | null;
  error_code: string | null;
  repairable: boolean | null;
  link_url: string;
  repair_url: string;
  status: ConnectionStatusKind;
  // When money data last ARRIVED, as opposed to last_synced, which is when
  // the sync JOB ran. Those diverge silently — that divergence is why chase
  // read as healthy while producing nothing. `known: false` means no
  // transaction inside the lookback window, which is NOT the same as
  // "we know it has been N days"; the UI must not render it as a green.
  data_freshness: {
    last_transaction_date: string | null;
    days_since_last_transaction: number | null;
    known: boolean;
    window_days: number;
    // The threshold that produced the verdict, in whole days, so a human can
    // read it and disagree. null means this institution has not shown enough
    // cadence to measure.
    freshness_slo_days: number | null;
  } | null;
}

export interface ConnectionsResponse {
  plaid_env: string;
  probed: boolean;
  institutions: ConnectionStatus[];
}

export interface ScheduleStatus {
  institution: string;
  schedule_id: string;
  exists: boolean;
  paused?: boolean;
  note?: string | null;
  num_actions?: number;
  last_run_at?: string | null;
  next_run_at?: string | null;
}

export interface SchedulesResponse {
  task_queue: string;
  schedules: ScheduleStatus[];
  missing: string[];
}

export interface SyncNowResult {
  institution: string;
  via: "schedule" | "workflow";
  id: string;
}

export interface SubscriptionPriceChange {
  detected?: boolean;
  previous_amount?: number | null;
  new_amount?: number | null;
  change_pct?: number | null;
  first_increased_at?: string | null;
}

export interface Subscription {
  bead_id: string;
  name: string | null;
  merchant_key: string | null;
  category: string | null;
  amount: number | null;
  frequency: string | null;
  monthly_equivalent: number;
  first_seen: string | null;
  last_seen: string | null;
  occurrences: number | null;
  confidence: number | null;
  is_price_hike: boolean;
  price_change: SubscriptionPriceChange;
  is_stale: boolean;
  status: "active" | "stale" | "price_hike";
}

export interface SubscriptionsResponse {
  total_monthly: number;
  active_count: number;
  stale_count: number;
  price_hike_count: number;
  subscriptions: Subscription[];
}

export interface BillProof {
  transaction_id: string;
  amount: number | null;
  posted_date: string | null;
  merchant: string | null;
}

export interface BillEntry {
  bead_id: string;
  vendor: string | null;
  amount: number | null;
  due_date: string | null;
  category: string | null;
  source: string | null;
  state: string;
  recurring: boolean | null;
  frequency: string | null;
  status: "overdue" | "due_soon" | "upcoming" | "paid" | string;
  proof?: BillProof | null;
}

export interface BillLedgerResponse {
  as_of: string;
  days_ahead: number;
  overdue_count: number;
  due_soon_count: number;
  overdue_total: number;
  due_soon_total: number;
  upcoming_total: number;
  groups: {
    overdue: BillEntry[];
    due_soon: BillEntry[];
    upcoming: BillEntry[];
    paid: BillEntry[];
  };
}

export interface RunRateResponse {
  months_analyzed: number;
  window_start: string;
  window_end: string;
  monthly_run_rate: number;
  fixed_monthly: number;
  variable_monthly: number;
  fixed_breakdown: { subscriptions: number; recurring_bills: number };
}

export interface BudgetCategoryStatus {
  budget: number;
  spent: number;
  remaining: number;
}

// Keyed by category, plus a "total" rollup row. Mirrors get_budget_status.
export type BudgetStatusResponse = Record<string, BudgetCategoryStatus>;

export interface LedgerVarianceAccount {
  account_id: string;
  account_name?: string | null;
  unexplained_variance: number | null;
  open_discrepancies: number;
  anchor_date: string | null;
}

export interface LedgerVarianceTotal {
  unexplained_variance: number;
  accounts_measured: number;
  accounts_not_measured: number;
  anchor_date: string | null;
  open_discrepancies: number;
}

export interface LedgerVarianceResponse {
  accounts: LedgerVarianceAccount[];
  total: LedgerVarianceTotal;
}

export interface ScenarioRequestBody {
  scenario_name: string;
  monthly_impact: number;
  upfront_impact: number;
}

// Mirrors workflows/finance_scenario_runner.py::build_projection (+ bead_id).
export interface ScenarioProjection {
  name: string;
  horizon_months: number;
  starting_liquid: number;
  net_monthly_flow: number;
  monthly_burn: number;
  monthly_impact: number;
  upfront_impact: number;
  months: string[];
  baseline_wealth: number[];
  scenario_wealth: number[];
  ending_baseline: number;
  ending_scenario: number;
  delta_ending: number;
  bead_id?: string;
}

export const mcpClient = {
  getConnections(probe: boolean): Promise<ConnectionsResponse> {
    return request<ConnectionsResponse>(`/finance/connections?probe=${probe}`);
  },
  getSchedules(): Promise<SchedulesResponse> {
    return request<SchedulesResponse>("/finance/schedules");
  },
  syncNow(institutionSlug: string): Promise<SyncNowResult> {
    return request<SyncNowResult>(
      `/finance/connections/${encodeURIComponent(institutionSlug)}/sync`,
      { method: "POST" },
    );
  },
  getSubscriptions(includeStale = true): Promise<SubscriptionsResponse> {
    return request<SubscriptionsResponse>(
      `/finance/subscriptions?include_stale=${includeStale}`,
    );
  },
  getBillLedger(daysAhead = 14): Promise<BillLedgerResponse> {
    return request<BillLedgerResponse>(`/finance/bills?days_ahead=${daysAhead}`);
  },
  getRunRate(months = 3): Promise<RunRateResponse> {
    return request<RunRateResponse>(`/finance/run_rate?months=${months}`);
  },
  getBudgetStatus(month = "current"): Promise<BudgetStatusResponse> {
    return request<BudgetStatusResponse>("/finance/budget_status", {
      method: "POST",
      body: JSON.stringify({ month }),
    });
  },
  getLedgerVariance(): Promise<LedgerVarianceResponse> {
    return request<LedgerVarianceResponse>("/finance/ledger_variance");
  },
  getRecentTransactions(limit = 1000, includeTransfers = true): Promise<Bead[]> {
    return request<Bead[]>(
      `/finance/transactions/recent?limit=${limit}&include_transfers=${includeTransfers}`,
    );
  },
  setBudget(
    category: string,
    amount: number,
    period: "monthly" | "quarterly" | "annual" = "monthly",
  ): Promise<unknown> {
    return request<unknown>("/finance/set_budget", {
      method: "POST",
      body: JSON.stringify({ category, amount, period }),
    });
  },
  getCategoryHistory(months = 6): Promise<CategoryHistoryResponse> {
    return request<CategoryHistoryResponse>(`/finance/category_history?months=${months}`);
  },
  runScenario(body: ScenarioRequestBody): Promise<ScenarioProjection> {
    return request<ScenarioProjection>("/finance/scenario", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  commitRule(rule: RuleSpec): Promise<RuleCommitResult> {
    return request<RuleCommitResult>("/finance/rules/commit", {
      method: "POST",
      body: JSON.stringify(rule),
    });
  },
};

// SDD Phase 4 — Rule Sandbox commit. mcp-hub saves a finance.rule bead and
// starts the retroaction workflow (fire-and-forget), returning ids to track.
export interface RuleCommitResult {
  rule_bead_id: string | null;
  workflow_id: string;
  status: string;
}

export interface CategoryStats {
  series: number[];       // one value per month in month_keys order
  avg: number;
  median: number;
  max: number;
  trend: "up" | "down" | "stable" | "new";
  months_with_data: number;
}

export interface CategoryHistoryResponse {
  months: number;
  month_keys: string[];   // ["2024-12", "2025-01", …]
  window_start: string;
  categories: Record<string, CategoryStats>;
}
