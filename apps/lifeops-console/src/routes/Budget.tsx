import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { mcpClient } from "@/providers/mcp-client";
import type { CategoryStats } from "@/providers/mcp-client";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead, FinanceBudgetRecommendationContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { fmtCurrency } from "@/lib/format";
import { cn } from "@/lib/cn";

// Canonical list — must match Python FINANCE_CATEGORIES
const ALL_CATEGORIES = [
  "groceries",
  "dining",
  "utilities",
  "kids",
  "auto",
  "housing",
  "entertainment",
  "health",
  "interest_fees",
  "misc",
] as const;

type FinanceCategory = (typeof ALL_CATEGORIES)[number];

const RECS_QK = ["finance-budget-recommendations"];

// ── helpers ─────────────────────────────────────────────────────────────────

function utilizationTone(pct: number): "pos" | "warn" | "neg" {
  if (pct >= 100) return "neg";
  if (pct >= 80) return "warn";
  return "pos";
}

function trendLabel(trend: CategoryStats["trend"]): string {
  return { up: "↑ Rising", down: "↓ Falling", stable: "→ Stable", new: "✦ New" }[trend];
}

function trendTone(trend: CategoryStats["trend"]): "neg" | "pos" | "neutral" | "accent" {
  return { up: "neg", down: "pos", stable: "neutral", new: "accent" }[trend] as never;
}

/** Round up to the nearest $25 — makes recommended amounts feel intentional */
function roundUp25(n: number) {
  return Math.ceil(n / 25) * 25;
}

// ── mini bar chart ───────────────────────────────────────────────────────────

function MiniBarChart({
  series,
  monthKeys,
  budget,
}: {
  series: number[];
  monthKeys: string[];
  budget?: number;
}) {
  const max = Math.max(...series, budget ?? 0, 1);
  return (
    <div className="flex items-end gap-1 h-20">
      {series.map((val, i) => {
        const pct = (val / max) * 100;
        const budgetPct = budget ? (budget / max) * 100 : undefined;
        const over = budget && val > budget;
        const label = monthKeys[i]?.slice(5); // "MM"
        return (
          <div key={monthKeys[i]} className="flex-1 flex flex-col items-center gap-0.5 min-w-0">
            <div className="relative w-full flex flex-col items-center justify-end" style={{ height: "64px" }}>
              {/* Budget line marker */}
              {budgetPct !== undefined && i === series.length - 1 && (
                <div
                  className="absolute w-full border-t border-dashed border-accent/60 pointer-events-none"
                  style={{ bottom: `${budgetPct}%` }}
                />
              )}
              <div
                className={cn(
                  "w-full rounded-t transition-all",
                  over ? "bg-neg/70" : val > 0 ? "bg-accent/50 hover:bg-accent/70" : "bg-bg-subtle/30"
                )}
                style={{ height: `${Math.max(pct, val > 0 ? 4 : 1)}%` }}
                title={`${monthKeys[i]}: ${fmtCurrency(val)}`}
              />
            </div>
            <span className="text-[10px] text-fg-subtle num">{label}</span>
          </div>
        );
      })}
    </div>
  );
}

// ── budget set panel ─────────────────────────────────────────────────────────

interface PanelProps {
  category: FinanceCategory;
  currentBudget: number;
  stats: CategoryStats | undefined;
  monthKeys: string[];
  monthlyTarget: number;
  totalHistoricalAvg: number;
  onClose: () => void;
  onSaved: () => void;
}

type Tier = "avg" | "comfortable" | "target";

function SetBudgetPanel({
  category,
  currentBudget,
  stats,
  monthKeys,
  monthlyTarget,
  totalHistoricalAvg,
  onClose,
  onSaved,
}: PanelProps) {
  const qc = useQueryClient();

  const avgRec = stats ? roundUp25(stats.avg) : 0;
  const comfortableRec = stats ? roundUp25(stats.avg * 1.15) : 0;
  const targetRec =
    monthlyTarget > 0 && totalHistoricalAvg > 0 && stats
      ? roundUp25((stats.avg / totalHistoricalAvg) * monthlyTarget)
      : 0;

  const [tier, setTier] = useState<Tier>("comfortable");
  const [rawInput, setRawInput] = useState<string>(() => {
    const initial = comfortableRec || currentBudget || 0;
    return initial > 0 ? String(initial) : "";
  });

  function pickTier(t: Tier) {
    setTier(t);
    const val = t === "avg" ? avgRec : t === "comfortable" ? comfortableRec : targetRec;
    if (val > 0) setRawInput(String(val));
  }

  const amount = parseFloat(rawInput) || 0;

  const saveMutation = useMutation({
    mutationFn: () => mcpClient.setBudget(category, amount),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["finance-budget-status"] });
      qc.invalidateQueries({ queryKey: ["finance-run-rate"] });
      onSaved();
    },
  });

  return (
    <>
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm z-30 animate-fade-in"
        onClick={onClose}
      />
      <div className="fixed inset-y-0 right-0 w-full sm:w-[480px] max-w-full bg-bg-panel border-l border-border z-40 flex flex-col shadow-2xl animate-slide-in">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <span className="text-sm font-semibold capitalize text-fg">{category}</span>
            {currentBudget > 0 && (
              <span className="ml-2 text-2xs text-fg-subtle num">
                currently {fmtCurrency(currentBudget)}/mo
              </span>
            )}
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} className="min-h-[44px]">
            ✕
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-5">
          {/* History chart */}
          <section>
            <div className="flex items-center justify-between mb-2">
              <span className="panel-title">Last {monthKeys.length} months</span>
              {stats && (
                <Badge tone={trendTone(stats.trend)}>{trendLabel(stats.trend)}</Badge>
              )}
            </div>
            {stats ? (
              <MiniBarChart
                series={stats.series}
                monthKeys={monthKeys}
                budget={currentBudget || undefined}
              />
            ) : (
              <div className="h-20 flex items-center justify-center text-2xs text-fg-subtle">
                No spending history yet
              </div>
            )}
          </section>

          {/* Stats row */}
          {stats && (
            <section className="grid grid-cols-3 gap-3">
              <div className="bg-bg-subtle/60 rounded p-2.5 text-center">
                <div className="panel-title mb-1">Avg / mo</div>
                <div className="num text-sm font-semibold text-fg">{fmtCurrency(stats.avg)}</div>
              </div>
              <div className="bg-bg-subtle/60 rounded p-2.5 text-center">
                <div className="panel-title mb-1">Median</div>
                <div className="num text-sm font-semibold text-fg">{fmtCurrency(stats.median)}</div>
              </div>
              <div className="bg-bg-subtle/60 rounded p-2.5 text-center">
                <div className="panel-title mb-1">Peak month</div>
                <div className="num text-sm font-semibold text-fg">{fmtCurrency(stats.max)}</div>
              </div>
            </section>
          )}

          {/* Recommendation tiers */}
          <section>
            <div className="panel-title mb-2">Recommendations</div>
            <div className="grid grid-cols-1 gap-2">
              <TierButton
                active={tier === "avg"}
                onClick={() => pickTier("avg")}
                label="Historical avg"
                sublabel={`${fmtCurrency(avgRec)}/mo — matches your typical spend`}
                value={avgRec}
                disabled={!stats}
              />
              <TierButton
                active={tier === "comfortable"}
                onClick={() => pickTier("comfortable")}
                label="Comfortable (+15%)"
                sublabel={`${fmtCurrency(comfortableRec)}/mo — buffer for busy months`}
                value={comfortableRec}
                disabled={!stats}
              />
              {targetRec > 0 && (
                <TierButton
                  active={tier === "target"}
                  onClick={() => pickTier("target")}
                  label="Target-proportional"
                  sublabel={`${fmtCurrency(targetRec)}/mo — your share of $${Math.round(monthlyTarget).toLocaleString()} target`}
                  value={targetRec}
                />
              )}
            </div>
          </section>

          {/* Manual input */}
          <section>
            <label className="panel-title block mb-1.5">Monthly budget</label>
            <div className="flex items-center gap-2">
              <span className="text-fg-subtle text-sm">$</span>
              <Input
                type="number"
                min={0}
                step={25}
                value={rawInput}
                onChange={(e) => {
                  setRawInput(e.target.value);
                  setTier("avg"); // clear preset highlight on manual edit
                }}
                className="flex-1"
                placeholder="0"
              />
              <span className="text-2xs text-fg-subtle">/ mo</span>
            </div>
            {amount > 0 && stats && amount < stats.avg && (
              <p className="mt-1.5 text-2xs text-warn">
                Below your {fmtCurrency(stats.avg)} avg — you may exceed this budget in a typical month.
              </p>
            )}
          </section>
        </div>

        {/* Footer */}
        <div className="border-t border-border px-4 py-3 flex gap-2">
          <Button
            className="flex-1"
            disabled={amount <= 0 || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            {saveMutation.isPending ? "Saving…" : `Set ${fmtCurrency(amount)}/mo`}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </>
  );
}

function TierButton({
  active,
  onClick,
  label,
  sublabel,
  value,
  disabled,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  sublabel: string;
  value: number;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || value <= 0}
      className={cn(
        "w-full text-left rounded border px-3 py-2.5 transition-colors",
        active
          ? "border-accent bg-accent/10 text-fg"
          : "border-border bg-bg-subtle/30 text-fg-muted hover:bg-bg-hover/40 hover:text-fg",
        (disabled || value <= 0) && "opacity-40 pointer-events-none"
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        {active && <span className="text-2xs text-accent font-mono">✓ selected</span>}
      </div>
      <span className="text-2xs text-fg-subtle block mt-0.5">{sublabel}</span>
    </button>
  );
}

// ── progress bar ─────────────────────────────────────────────────────────────

function ProgressBar({ pct, tone }: { pct: number; tone: "pos" | "warn" | "neg" }) {
  const fill = tone === "neg" ? "bg-neg" : tone === "warn" ? "bg-warn" : "bg-pos";
  return (
    <div className="h-1.5 w-full rounded bg-bg-subtle overflow-hidden">
      <div className={`h-full ${fill} transition-all`} style={{ width: `${Math.min(pct, 100)}%` }} />
    </div>
  );
}

// ── main route ───────────────────────────────────────────────────────────────

export function BudgetRoute() {
  const [selectedCategory, setSelectedCategory] = useState<FinanceCategory | null>(null);
  const [monthlyTarget, setMonthlyTarget] = useState<number>(0);
  const [editingTarget, setEditingTarget] = useState(false);
  const [targetInput, setTargetInput] = useState("");
  const qc = useQueryClient();

  const runRateQuery = useQuery({
    queryKey: ["finance-run-rate"],
    queryFn: () => mcpClient.getRunRate(3),
  });
  const budgetQuery = useQuery({
    queryKey: ["finance-budget-status"],
    queryFn: () => mcpClient.getBudgetStatus("current"),
  });
  const historyQuery = useQuery({
    queryKey: ["finance-category-history"],
    queryFn: () => mcpClient.getCategoryHistory(6),
    staleTime: 5 * 60_000,
  });
  const recsQuery = useQuery({
    queryKey: RECS_QK,
    queryFn: () =>
      substrateClient.listBeads({ namespace: "finance", type: "budget_recommendation", state: "pending", limit: 100 }),
  });

  const recsByCategory = useMemo(() => {
    const map = new Map<string, Bead>();
    for (const b of recsQuery.data ?? []) {
      const c = b.content as unknown as FinanceBudgetRecommendationContent;
      if (c.category) map.set(c.category, b);
    }
    return map;
  }, [recsQuery.data]);

  const acceptRec = useMutation({
    mutationFn: async (bead: Bead) => {
      const c = bead.content as unknown as FinanceBudgetRecommendationContent;
      await mcpClient.setBudget(c.category, c.suggested_cap, "monthly");
      await substrateClient.updateBead(bead.id, { state: "resolved.accepted", created_by: "lifeops-console/budget-recommendation" });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: RECS_QK });
      qc.invalidateQueries({ queryKey: ["finance-budget-status"] });
    },
  });

  const runRate = runRateQuery.data;
  const history = historyQuery.data;

  // Use run rate as the default monthly target when user hasn't set one
  const effectiveTarget = monthlyTarget > 0 ? monthlyTarget : (runRate?.monthly_run_rate ?? 0);

  // Total of all historical avgs across categories (for proportional recommendations)
  const totalHistoricalAvg = useMemo(() => {
    if (!history) return 0;
    return Object.values(history.categories).reduce((sum, s) => sum + s.avg, 0);
  }, [history]);

  // Build unified rows for ALL categories
  const rows = useMemo(() => {
    const budgetData = budgetQuery.data ?? {};
    return ALL_CATEGORIES.map((cat) => {
      const b = budgetData[cat];
      const stats = history?.categories[cat];
      return {
        category: cat,
        budget: b?.budget ?? 0,
        spent: b?.spent ?? 0,
        remaining: b?.remaining ?? 0,
        pct: b && b.budget > 0 ? Math.round((b.spent / b.budget) * 100) : b?.spent > 0 ? 999 : 0,
        hasBudget: (b?.budget ?? 0) > 0,
        stats,
      };
    }).sort((a, b) => {
      // Budgeted categories first, then by pct desc
      if (a.hasBudget !== b.hasBudget) return a.hasBudget ? -1 : 1;
      return b.pct - a.pct;
    });
  }, [budgetQuery.data, history]);

  const total = budgetQuery.data?.total;
  const monthKeys = history?.month_keys ?? [];

  const selectedStats = selectedCategory ? history?.categories[selectedCategory] : undefined;
  const selectedBudget = selectedCategory
    ? (budgetQuery.data?.[selectedCategory]?.budget ?? 0)
    : 0;

  return (
    <>
      <PageHeader
        title="Budget"
        subtitle="Set category budgets from your spending history · month-to-date actuals from bank + manual"
      />

      <div className="p-4 sm:p-6 space-y-4">
        {/* Run rate + target */}
        <Card>
          <CardHeader
            title="Monthly snapshot"
            hint={
              runRate
                ? `${runRate.months_analyzed}-mo avg · ${runRate.window_start} → ${runRate.window_end}`
                : "recent burn"
            }
          />
          <CardBody className="grid grid-cols-2 sm:grid-cols-4 gap-4 sm:gap-6">
            <Stat label="Run rate" value={fmtCurrency(runRate?.monthly_run_rate ?? 0)} sub="Avg total / month" />
            <Stat label="Fixed" value={fmtCurrency(runRate?.fixed_monthly ?? 0)} sub="Subs + bills" />
            <Stat label="Variable" value={fmtCurrency(runRate?.variable_monthly ?? 0)} sub="Discretionary" />
            <div className="flex flex-col gap-1">
              <span className="panel-title">Monthly target</span>
              {editingTarget ? (
                <div className="flex items-center gap-1">
                  <span className="text-fg-subtle text-sm">$</span>
                  <Input
                    type="number"
                    value={targetInput}
                    onChange={(e) => setTargetInput(e.target.value)}
                    className="flex-1 w-24"
                    autoFocus
                    onBlur={() => {
                      const v = parseFloat(targetInput);
                      if (v > 0) setMonthlyTarget(v);
                      setEditingTarget(false);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        const v = parseFloat(targetInput);
                        if (v > 0) setMonthlyTarget(v);
                        setEditingTarget(false);
                      }
                      if (e.key === "Escape") setEditingTarget(false);
                    }}
                  />
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setTargetInput(monthlyTarget > 0 ? String(monthlyTarget) : "");
                    setEditingTarget(true);
                  }}
                  className="text-left group"
                >
                  <span className="num text-xl font-semibold text-fg group-hover:text-accent transition-colors">
                    {monthlyTarget > 0 ? fmtCurrency(monthlyTarget) : fmtCurrency(runRate?.monthly_run_rate ?? 0)}
                  </span>
                  <span className="text-2xs text-fg-subtle block mt-0.5">
                    {monthlyTarget > 0 ? "custom target · click to edit" : "= run rate · click to set"}
                  </span>
                </button>
              )}
            </div>
          </CardBody>
        </Card>

        {/* Budget vs actual — all categories */}
        <Card>
          <CardHeader
            title="Budget vs actual"
            hint="This month · click any row to set or adjust a budget"
            right={
              total ? (
                <span className="text-2xs text-fg-subtle num">
                  {fmtCurrency(total.spent)} spent · {fmtCurrency(total.budget)} budgeted
                </span>
              ) : null
            }
          />

          {/* Desktop table */}
          <div className="overflow-x-auto hidden md:block">
            <table className="w-full border-collapse text-sm">
              <thead className="text-2xs uppercase tracking-wider text-fg-muted border-b border-border">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">Category</th>
                  <th className="px-4 py-2 text-right font-medium">6-mo avg</th>
                  <th className="px-4 py-2 text-right font-medium">Budget</th>
                  <th className="px-4 py-2 text-right font-medium">Spent</th>
                  <th className="px-4 py-2 text-right font-medium">Remaining</th>
                  <th className="px-4 py-2 w-40 font-medium">Used</th>
                  <th className="px-4 py-2 w-44 font-medium">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {budgetQuery.isLoading || historyQuery.isLoading ? (
                  <tr>
                    <td colSpan={7} className="text-center text-fg-muted py-8">Loading…</td>
                  </tr>
                ) : (
                  rows.map((r) => {
                    const tone = utilizationTone(r.pct);
                    const recBead = recsByCategory.get(r.category);
                    const rec = recBead
                      ? (recBead.content as unknown as FinanceBudgetRecommendationContent)
                      : null;
                    return (
                      <tr
                        key={r.category}
                        onClick={() => setSelectedCategory(r.category)}
                        className="hover:bg-bg-hover/40 cursor-pointer transition-colors"
                      >
                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-2">
                            <span className="font-medium capitalize">{r.category}</span>
                            {r.stats && (
                              <Badge tone={trendTone(r.stats.trend)} className="hidden lg:inline-flex">
                                {trendLabel(r.stats.trend)}
                              </Badge>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-2.5 num text-right text-fg-muted">
                          {r.stats ? fmtCurrency(r.stats.avg) : "—"}
                        </td>
                        <td className="px-4 py-2.5 num text-right">
                          {r.hasBudget ? fmtCurrency(r.budget) : (
                            <span className="text-fg-subtle text-2xs">not set</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5 num text-right">{r.spent > 0 ? fmtCurrency(r.spent) : "—"}</td>
                        <td className={`px-4 py-2.5 num text-right ${r.remaining < 0 ? "text-neg font-medium" : ""}`}>
                          {r.hasBudget ? fmtCurrency(r.remaining) : "—"}
                        </td>
                        <td className="px-4 py-2.5">
                          {r.hasBudget ? (
                            <div className="flex items-center gap-2">
                              <ProgressBar pct={r.pct} tone={tone} />
                              <Badge tone={tone}>{r.pct}%</Badge>
                            </div>
                          ) : (
                            <span className="text-2xs text-fg-subtle">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                          {rec && recBead ? (
                            <div className="flex items-center gap-2">
                              <span className="text-2xs text-fg-subtle num leading-tight">
                                {fmtCurrency(rec.current_cap)} →{" "}
                                <span className={rec.direction === "increase" ? "text-neg" : "text-pos"}>
                                  {fmtCurrency(rec.suggested_cap)}
                                </span>
                              </span>
                              <Button
                                size="sm"
                                disabled={acceptRec.isPending}
                                onClick={() => acceptRec.mutate(recBead)}
                              >
                                Accept
                              </Button>
                            </div>
                          ) : (
                            <Button
                              size="sm"
                              variant={r.hasBudget ? "outline" : "ghost"}
                              onClick={() => setSelectedCategory(r.category)}
                              className="text-2xs"
                            >
                              {r.hasBudget ? "Edit" : "+ Set budget"}
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Mobile card list */}
          <div className="md:hidden divide-y divide-border">
            {budgetQuery.isLoading || historyQuery.isLoading ? (
              <div className="text-center text-fg-muted py-8">Loading…</div>
            ) : (
              rows.map((r) => {
                const tone = utilizationTone(r.pct);
                return (
                  <div
                    key={r.category}
                    onClick={() => setSelectedCategory(r.category)}
                    className="px-4 py-3 hover:bg-bg-hover/40 active:bg-bg-hover/60 transition-colors cursor-pointer flex flex-col gap-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="font-medium capitalize text-sm">{r.category}</span>
                        {r.stats && (
                          <Badge tone={trendTone(r.stats.trend)}>{trendLabel(r.stats.trend)}</Badge>
                        )}
                      </div>
                      <Button
                        size="sm"
                        variant={r.hasBudget ? "outline" : "ghost"}
                        onClick={(e) => { e.stopPropagation(); setSelectedCategory(r.category); }}
                        className="shrink-0"
                      >
                        {r.hasBudget ? "Edit" : "+ Set"}
                      </Button>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-2xs">
                      <div>
                        <span className="panel-title block">6-mo avg</span>
                        <span className="num text-fg-muted">{r.stats ? fmtCurrency(r.stats.avg) : "—"}</span>
                      </div>
                      <div>
                        <span className="panel-title block">Budget</span>
                        <span className="num">{r.hasBudget ? fmtCurrency(r.budget) : <span className="text-fg-subtle">not set</span>}</span>
                      </div>
                      <div>
                        <span className="panel-title block">Spent</span>
                        <span className="num">{r.spent > 0 ? fmtCurrency(r.spent) : "—"}</span>
                      </div>
                    </div>
                    {r.hasBudget && (
                      <div className="flex items-center gap-2">
                        <ProgressBar pct={r.pct} tone={tone} />
                        <Badge tone={tone}>{r.pct}%</Badge>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </Card>

        {/* Total allocation summary */}
        {total && total.budget > 0 && (
          <Card>
            <CardBody className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
              <div>
                <span className="panel-title block mb-1">Budget allocation coverage</span>
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="num text-2xl font-semibold text-fg">{fmtCurrency(total.budget)}</span>
                  <span className="text-2xs text-fg-subtle">
                    budgeted of {fmtCurrency(effectiveTarget)} target
                    {effectiveTarget > 0 ? ` · ${Math.round((total.budget / effectiveTarget) * 100)}% coverage` : ""}
                  </span>
                </div>
              </div>
              <div className="w-full sm:w-64">
                <ProgressBar
                  pct={effectiveTarget > 0 ? Math.round((total.budget / effectiveTarget) * 100) : 0}
                  tone={
                    total.budget >= effectiveTarget * 0.95
                      ? "pos"
                      : total.budget >= effectiveTarget * 0.7
                      ? "warn"
                      : "neg"
                  }
                />
                <span className="text-2xs text-fg-subtle mt-1 block">
                  {effectiveTarget > 0 && total.budget < effectiveTarget
                    ? `${fmtCurrency(effectiveTarget - total.budget)} unallocated — set budgets for more categories`
                    : total.budget > effectiveTarget
                    ? `${fmtCurrency(total.budget - effectiveTarget)} over target`
                    : "Fully allocated"}
                </span>
              </div>
            </CardBody>
          </Card>
        )}
      </div>

      {selectedCategory && (
        <SetBudgetPanel
          category={selectedCategory}
          currentBudget={selectedBudget}
          stats={selectedStats}
          monthKeys={monthKeys}
          monthlyTarget={effectiveTarget}
          totalHistoricalAvg={totalHistoricalAvg}
          onClose={() => setSelectedCategory(null)}
          onSaved={() => setSelectedCategory(null)}
        />
      )}
    </>
  );
}
