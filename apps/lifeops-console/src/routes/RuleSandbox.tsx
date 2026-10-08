import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  substrateClient,
  type RuleSampleDiff,
  type RuleSpec,
  type RuleOperator,
} from "@/providers/substrate-client";
import { mcpClient, type RuleCommitResult } from "@/providers/mcp-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/Card";
import { Input, Select } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";

// SDD Phase 4 — Rule Definition & Backtesting Sandbox. Left pane drafts an
// auto-categorization rule; the right pane backtests it live against recent
// transactions via Substrate's Tier 2 cache (POST /substrate/rules/dry-run,
// read-only). "Apply Rule & Retroact" calls mcp-hub's /finance/rules/commit,
// which persists a finance.rule bead and starts the retroaction workflow.
//
// NOTE: only currently-Uncategorized transactions are re-categorized on commit
// (never clobber a human/LLM category). The preview shows every match and flags
// which ones will actually change.

// Fields a rule can match. "vendor" aliases server-side to normalized_merchant
// (transactions have no vendor field — that's a bill field), kept here for the
// spec's canonical example.
const FIELD_OPTIONS: { value: string; label: string }[] = [
  { value: "normalized_merchant", label: "Merchant (normalized)" },
  { value: "merchant_name", label: "Merchant (raw)" },
  { value: "description", label: "Description" },
  { value: "our_category", label: "Current category" },
  { value: "amount", label: "Amount" },
];

const OPERATOR_OPTIONS: { value: RuleOperator; label: string }[] = [
  { value: "contains", label: "contains" },
  { value: "equals", label: "equals" },
  { value: "starts_with", label: "starts with" },
  { value: "regex", label: "matches regex" },
];

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

const columnHelper = createColumnHelper<RuleSampleDiff>();

export function RuleSandboxRoute() {
  const [field, setField] = useState("normalized_merchant");
  const [operator, setOperator] = useState<RuleOperator>("contains");
  const [value, setValue] = useState("Amazon");
  const [targetCategory, setTargetCategory] = useState("Shopping");

  const rule: RuleSpec = useMemo(
    () => ({ field, operator, value: value.trim(), target_category: targetCategory.trim() }),
    [field, operator, value, targetCategory],
  );
  const debouncedRule = useDebounced(rule, 350);
  const ruleReady = debouncedRule.value.length > 0 && debouncedRule.target_category.length > 0;

  const dryRun = useQuery({
    queryKey: ["rule-dry-run", debouncedRule],
    queryFn: () => substrateClient.dryRunRule(debouncedRule),
    enabled: ruleReady,
    // Keep the previous diffs on screen while the next backtest is in flight,
    // so the table doesn't flicker to empty on every keystroke.
    placeholderData: (prev) => prev,
  });

  const commit = useMutation<RuleCommitResult, Error, void>({
    mutationFn: () => mcpClient.commitRule(rule),
  });

  const result = dryRun.data;
  const willChange = result?.beads_affected ?? 0;

  const columns = useMemo(
    () => [
      columnHelper.accessor("vendor", { header: "Merchant" }),
      columnHelper.accessor("old", {
        header: "Current",
        cell: (c) => <span className="text-fg-muted">{c.getValue()}</span>,
      }),
      columnHelper.accessor("new", {
        header: "→ New",
        cell: (c) => <span className="text-accent">{c.getValue()}</span>,
      }),
      columnHelper.accessor("will_change", {
        header: "Effect",
        cell: (c) =>
          c.getValue() ? (
            <Badge tone="pos">will change</Badge>
          ) : (
            <Badge tone="neutral">skipped (already categorized)</Badge>
          ),
      }),
    ],
    [],
  );

  const table = useReactTable({
    data: result?.sample_diffs ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <>
      <PageHeader
        title="Rule Sandbox"
        subtitle="Draft an auto-categorization rule and backtest it against history before it touches the ledger"
      />

      <div className="p-6 grid grid-cols-1 lg:grid-cols-[360px_1fr] gap-4 items-start">
        {/* Left pane — rule builder */}
        <Card>
          <CardHeader title="Rule" hint="Backtested live against recent transactions" />
          <CardBody className="space-y-3">
            <label className="flex flex-col gap-1">
              <span className="text-2xs uppercase tracking-wider text-fg-subtle">Field</span>
              <Select value={field} onChange={(e) => setField(e.target.value)}>
                {FIELD_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-2xs uppercase tracking-wider text-fg-subtle">Operator</span>
              <Select
                value={operator}
                onChange={(e) => setOperator(e.target.value as RuleOperator)}
              >
                {OPERATOR_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-2xs uppercase tracking-wider text-fg-subtle">Value</span>
              <Input
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder="e.g. Amazon"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-2xs uppercase tracking-wider text-fg-subtle">
                Target category
              </span>
              <Input
                value={targetCategory}
                onChange={(e) => setTargetCategory(e.target.value)}
                placeholder="e.g. Shopping"
              />
            </label>

            <div className="pt-2 border-t border-border">
              <Button
                className="w-full"
                disabled={!ruleReady || willChange === 0 || commit.isPending}
                onClick={() => commit.mutate()}
              >
                {commit.isPending
                  ? "Applying…"
                  : `Apply Rule & Retroact${willChange ? ` (${willChange})` : ""}`}
              </Button>
              {commit.isSuccess ? (
                <div className="mt-2 text-xs text-pos">
                  Rule committed · retroaction started ({commit.data.status}).
                </div>
              ) : null}
              {commit.isError ? (
                <div className="mt-2 text-xs text-neg">
                  Commit failed: {commit.error?.message ?? "unknown error"}
                </div>
              ) : null}
            </div>
          </CardBody>
        </Card>

        {/* Right pane — backtest results */}
        <div className="space-y-4">
          <Card>
            <CardBody className="grid grid-cols-3 gap-6">
              <Stat label="Matched" value={result ? String(result.matched) : "—"} sub="txns matching the rule" />
              <Stat
                label="Will change"
                value={result ? String(result.beads_affected) : "—"}
                tone={willChange > 0 ? "pos" : undefined}
                sub="Uncategorized only"
              />
              <Stat label="Scanned" value={result ? String(result.scanned) : "—"} sub="recent transactions" />
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Backtest preview"
              hint={result ? `Sample of up to 100 of ${result.matched} matches` : undefined}
            />
            {/* Desktop Table View */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead className="text-2xs uppercase tracking-wider text-fg-muted">
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id}>
                      {hg.headers.map((header) => (
                        <th
                          key={header.id}
                          className="text-left font-medium px-3 py-2 border-b border-border"
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody className="divide-y divide-border">
                  {!ruleReady ? (
                    <tr>
                      <td colSpan={columns.length} className="text-center text-fg-muted py-8">
                        Enter a value and target category to backtest.
                      </td>
                    </tr>
                  ) : dryRun.isLoading ? (
                    <tr>
                      <td colSpan={columns.length} className="text-center text-fg-muted py-8">
                        Backtesting…
                      </td>
                    </tr>
                  ) : dryRun.isError ? (
                    <tr>
                      <td colSpan={columns.length} className="text-center text-neg py-8">
                        Backtest failed: {(dryRun.error as Error)?.message ?? "unknown error"}
                      </td>
                    </tr>
                  ) : table.getRowModel().rows.length === 0 ? (
                    <tr>
                      <td colSpan={columns.length} className="text-center text-fg-muted py-8">
                        No transactions match this rule.
                      </td>
                    </tr>
                  ) : (
                    table.getRowModel().rows.map((row) => (
                      <tr key={row.id} className="hover:bg-bg-hover/60">
                        {row.getVisibleCells().map((cell) => (
                          <td key={cell.id} className="px-3 py-2 align-top">
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
                          </td>
                        ))}
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* Mobile View */}
            <div className="md:hidden divide-y divide-border/60">
              {!ruleReady ? (
                <div className="text-center text-fg-muted py-8 text-sm">
                  Enter a value and target category to backtest.
                </div>
              ) : dryRun.isLoading ? (
                <div className="text-center text-fg-muted py-8 text-sm">
                  Backtesting…
                </div>
              ) : dryRun.isError ? (
                <div className="text-center text-neg py-8 text-sm">
                  Backtest failed.
                </div>
              ) : (result?.sample_diffs ?? []).length === 0 ? (
                <div className="text-center text-fg-muted py-8 text-sm">
                  No transactions match this rule.
                </div>
              ) : (
                (result?.sample_diffs ?? []).map((diff, index) => (
                  <div key={index} className="py-3 px-3 flex flex-col gap-2">
                    <div className="flex items-start justify-between gap-4">
                      <span className="text-sm font-medium text-fg truncate">{diff.vendor || "—"}</span>
                      {diff.will_change ? (
                        <Badge tone="pos">will change</Badge>
                      ) : (
                        <Badge tone="neutral">skipped</Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-2xs">
                      <span className="text-fg-muted">{diff.old || "uncategorized"}</span>
                      <span className="text-fg-subtle">→</span>
                      <span className="text-accent font-semibold">{diff.new}</span>
                    </div>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
