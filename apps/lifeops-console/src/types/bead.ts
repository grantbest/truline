// Mirrors apps/substrate/src/schemas.py::BeadRead. Keep in sync.
export interface Bead {
  id: string;
  namespace: string;
  type: string;
  state: string;
  parent_id: string | null;
  context: Record<string, unknown>;
  content: Record<string, unknown>;
  confidence: number | null;
  trust_tier: string;
  provenance: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface BeadEvent {
  id: string;
  bead_id: string;
  event_type: string;
  from_state: string | null;
  to_state: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  created_by: string;
}

export interface BeadSearchHit {
  bead: Bead;
  score: number;
}

export interface BeadLink {
  id: string;
  source_id: string;
  target_id: string;
  link_type: string;
  content: Record<string, unknown>;
  created_at: string;
  created_by: string;
}

// ---------------------------------------------------------------------------
// dev.* — the factory's own work items. Mirrors apps/substrate/src/schemas.py
// (DevTaskContent / DevNoteContent). Keep in sync; the API rejects with 422 on
// drift, so a mismatch here surfaces as a failed write, not silent corruption.
// ---------------------------------------------------------------------------

// Mirrors apps/factory-dispatcher/dispatch.py::DEV_LANES, and the `lanes`
// array in the task-intake contract (both copies — see
// task-intake-contract.json and its test_task_intake_contract_copy_parity.py
// guard). Declared here as a literal `as const` tuple, not derived from the
// JSON import: resolveJsonModule widens a JSON array to `string[]`, which
// would silently collapse DevLane to `string` with no visible symptom.
// dev-board.test.ts binds this tuple to the contract's `lanes` at runtime so
// the two can never drift.
export const DEV_LANES = ["code-health", "drift", "bug-triage", "feature"] as const;
export type DevLane = (typeof DEV_LANES)[number];

// The board's columns. Mirrors apps/substrate/src/routes.py::STATE_MACHINES
// [("dev", "task")] — every state that machine declares, not just the ones
// docs/plans/dev-collaboration-primitives.md §4 named first. "superseded"
// and "archived" are both terminal/closed states there (see dev-board.ts's
// CLOSED_DEV_TASK_STATES); keep in sync or a live state falls into the
// board's off-convention column.
export const DEV_TASK_STATES = [
  "pending",
  "doing",
  "review",
  "done",
  "failed",
  "superseded",
  "archived",
] as const;
export type DevTaskState = (typeof DEV_TASK_STATES)[number];

export interface DevTaskContent {
  lane: DevLane;
  title: string;
  intent: string;
  context_refs: string[];
  source_bead_ids?: string[];
  // Ordering edge this task is held on. Mirrors
  // apps/factory-dispatcher/guards.py::predecessor_bead_ids — the dispatcher
  // will not claim this task while any id here names a bead that has not
  // reached "done". Whether that holds this task back is read from the
  // served runnability answer (dev-board.ts::taskRunnabilityFrom), not
  // computed from this field; dev-board.ts::dependentsByPredecessorId reads
  // it directly only for the reverse "who does this block" display.
  predecessor_bead_ids?: string[];
  acceptance: string[];
  verification: {
    commands: string[];
    must_report_unverified?: boolean;
  };
  scope: {
    paths: string[];
    // Always contains ".github/workflows/**" — the factory may not write the
    // gates that judge it. Enforced by a field_validator server-side.
    forbidden_paths: string[];
  };
  risk_class: "structural" | "behavioral";
  budget: {
    max_agent_minutes: number;
    max_usd: number;
    max_tokens: number;
  };
  worker_hint?: "codex" | "claude" | "gemini" | "local" | null;
  // WHICH orchestrator actually ran this task — a fact the dispatcher stamps
  // when it completes a run, distinct from `worker_hint` above (which states
  // what was intended, not what happened). "unrecognized[:name]" means a
  // writer declared an identity outside the known roster; see
  // apps/factory-dispatcher/dispatch.py's KNOWN_ORCHESTRATORS/resolve_ran_by.
  ran_by?: string | null;
  autonomy?: "propose" | "auto-merge-eligible";
  attempts?: number;
  max_attempts?: number;
  requirement_refs?: string[];
  nfrs?: {
    category: "latency" | "availability" | "durability" | "security" | "cost" | "observability" | "usability";
    statement: string;
    threshold: string;
    verification: string;
  }[];
  arch_impact?: {
    applications?: string[];
    capabilities?: string[];
    notes?: string | null;
  } | null;
  pr_refs?: string[];
  // Additive: the dispatcher stamps the PR once it opens one.
  pr_url?: string | null;
  // Release traceability (2026-08-25). The release itself lives in the
  // `delivers` bead_link, never here — `outcome_ref` is a bare id (`O-2`)
  // meaningless without that edge, and `release_ref_waived` is set only when
  // no `delivers` edge was written at all. See apps/substrate/src/schemas.py
  // DevTaskContent for the authoritative shape.
  outcome_ref?: string | null;
  release_ref_waived?: string | null;
  // Emergency marker (R26.12/B15). Intent recorded on the bead, not execution
  // state: nothing reads these yet. The server refuses class_of_service
  // other than 'emergency', expedite_until that isn't an ISO-8601 datetime,
  // and any of the three present without the other two — see
  // apps/substrate/src/schemas.py DevTaskContent for the authoritative shape.
  class_of_service?: "emergency";
  expedite_reason?: string;
  expedite_until?: string;
}

export type DevNoteKind =
  | "comment"
  | "question"
  | "answer"
  | "status"
  | "attachment"
  | "review";

// Per-kind fields are conditionally validated server-side: setting one on the
// wrong kind is a 422, not an ignored field. Only ever send the field that
// belongs to the kind being written.
export interface DevNoteContent {
  kind: DevNoteKind;
  body: string;
  blocking?: boolean; // question only
  answers_ref?: string; // answer only — the question bead id
  // answer only — whether this answer closes its `answers_ref` question.
  // Mirrors apps/factory-dispatcher/guards.py::RELEASES_WORK_FIELD: an
  // answer existing is not consent, only `releases_work: true` is. Missing
  // or false both leave the question open — the dispatcher and the board
  // must read this identically.
  releases_work?: boolean;
  url?: string; // attachment only
  verdict?: "approve" | "request-changes"; // review only
}

// finance.account content (extra fields allowed by Pydantic — keep the
// shape additive). Mirrors workflows/bank_sync.py::_build_content.
export interface FinanceAccountContent {
  institution: string;
  plaid_account_id: string;
  name: string;
  type: string;
  subtype?: string | null;
  mask?: string | null;
  current_balance?: number;
  available_balance?: number | null;
  iso_currency_code?: string;
  first_synced?: string;
  last_synced?: string;
  liabilities?: {
    apr?: number | null;
    min_payment?: number | null;
    principal?: number | null;
    due_date?: string | null;
    next_payment_date?: string | null;
  };
}

export interface FinanceTransactionContent {
  amount: number;
  iso_currency_code?: string;
  merchant_name: string;
  normalized_merchant: string;
  posted_date: string;
  authorized_date?: string | null;
  plaid_transaction_id: string;
  is_transfer?: boolean;
  account_id: string;
  // Additive fields bank_sync persists (Pydantic extra="allow").
  institution?: string;
  description?: string;
  // LLM categorization — stored under our_category (see write_transaction_bead_activity).
  our_category?: string | null;
  transfer_pair_id?: string | null;
}

// finance.audit_discrepancy content — emitted by the ledger reconciliation
// workflow when an account's balance drifts from the tracked ledger.
// Mirrors workflows/finance_reconciliation.py::emit_discrepancies_activity.
export interface FinanceDiscrepancyContent {
  account_id: string;
  account_name?: string | null;
  expected_balance: number;
  actual_balance: number;
  drift: number;
  prev_snapshot_at?: string | null;
  window_end?: string | null;
}

// finance.transfer content — links two transaction legs of one internal move.
// Mirrors workflows/transfer_pairing.py::persist_transfer_pair_activity; the
// console's manual matcher writes the identical shape.
export interface FinanceTransferContent {
  transfer_pair_id: string;
  from_tx_id: string;
  to_tx_id: string;
  from_account_id?: string | null;
  to_account_id?: string | null;
  amount: number;
  iso_currency_code?: string;
  posted_date?: string | null;
}

// finance.balance_snapshot content — nightly per-account balance anchor used
// to reconcile drift. Mirrors finance_reconciliation.py::_create_snapshot.
export interface FinanceBalanceSnapshotContent {
  account_id: string;
  balance: number;
  iso_currency_code?: string | null;
  as_of: string;
}

// finance.review_anomaly content — emitted by the Phase 2 anomaly detector
// when a duplicate charge or a price hike is detected. Mirrors
// workflows/finance_anomaly_detector.py::emit_anomalies_activity.
export interface FinanceAnomalyContent {
  reason: "duplicate_charge" | "price_hike";
  transaction_ids: string[];
  account_id?: string | null;
  vendor?: string | null;
  amount: number;
  posted_date?: string | null;
  fingerprint: string;
  detected_at?: string | null;
  // duplicate_charge
  count?: number;
  // price_hike
  baseline_amount?: number;
  change_pct?: number;
  sample_size?: number;
}

// finance.action_required content — emitted by the Phase 2 trial sentinel.
// `kind` discriminates the action card. Mirrors
// workflows/subscription_sentinel.py::emit_trial_alerts_activity.
export interface FinanceActionRequiredContent {
  kind: "trial_ending";
  subscription_id?: string | null;
  vendor?: string | null;
  amount_at_risk?: number | null;
  frequency?: string | null;
  end_date: string;
  days_until?: number;
  fingerprint: string;
  detected_at?: string | null;
}

// finance.insight_allocation content — emitted by the Phase 3 yield optimizer
// when idle checking cash should be paid against a high-APR liability. Mirrors
// workflows/finance_yield_optimizer.py::emit_allocation_insights_activity.
export interface FinanceInsightAllocationContent {
  action: "paydown";
  source_account_id?: string | null;
  source_account_name?: string | null;
  target_account_id?: string | null;
  target_account_name?: string | null;
  target_apr?: number | null;
  amount: number;
  projected_annual_savings: number;
  idle_cash?: number;
  buffer_retained?: number;
  fingerprint: string;
  detected_at?: string | null;
}

// finance.budget_recommendation content — emitted by the Phase 3 budget
// analyzer when a category's 90-day average drifts >15% from its cap. Mirrors
// workflows/finance_budget_analyzer.py::emit_budget_recommendations_activity.
export interface FinanceBudgetRecommendationContent {
  category: string;
  budget_id?: string | null;
  current_cap: number;
  suggested_cap: number;
  monthly_average: number;
  deviation_pct: number;
  direction: "increase" | "decrease";
  window_days: number;
  sample_size: number;
  reason: string;
  fingerprint: string;
  detected_at?: string | null;
}

// finance.projection_scenario content — produced on-demand by the Phase 3
// scenario engine. Mirrors workflows/finance_scenario_runner.py::build_projection.
export interface FinanceProjectionScenarioContent {
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
  computed_at?: string;
}

// finance.rule content — persisted by the Phase 4 Rule Sandbox commit
// (tools/rules.py::commit_rule). last_apply is stamped by the retroaction
// workflow once it finishes (workflows/finance_rule_apply.py).
export interface FinanceRuleContent {
  field: string;
  operator: "contains" | "equals" | "starts_with" | "regex";
  value: string;
  target_category: string;
  created_at?: string;
  last_apply?: {
    rule_bead_id?: string | null;
    scanned: number;
    matched: number;
    patched: number;
    errors: number;
  };
}

export interface FinanceBillContent {
  vendor: string;
  amount: number;
  due_date: string;
  category: string;
  source: "liability" | "vision" | "manual";
  owner?: string | null;
  external_id?: string | null;
  account_id?: string | null;
  min_payment?: number | null;
  recurring?: boolean;
  frequency?: "monthly" | "quarterly" | "annual" | null;
}
