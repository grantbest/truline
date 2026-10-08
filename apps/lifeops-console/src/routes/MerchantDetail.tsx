import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { ListBeadsParams } from "@/providers/substrate-client";
import type { Bead, FinanceBillContent, FinanceTransactionContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader, Stat } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtCurrency, fmtDate } from "@/lib/format";
import { merchantMatches } from "@/lib/merchant";
import { cn } from "@/lib/cn";

// Substrate has no JSONB-content query, so we page through all finance beads
// and filter to the merchant client-side.
const PAGE_SIZE = 500;

interface MonthBucket {
  key: string; // YYYY-MM
  label: string; // e.g. "May '26"
  total: number;
}

interface MerchantData {
  transactions: Bead[];
  bills: Bead[];
  summary?: {
    avg_amount: number;
    total_ytd: number;
    occurrences: number;
    last_paid: string;
    frequency: string;
  };
}

async function listAllBeads(params: ListBeadsParams): Promise<Bead[]> {
  const out: Bead[] = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = await substrateClient.listBeads({ ...params, limit: PAGE_SIZE, offset });
    out.push(...page);
    if (page.length < PAGE_SIZE) return out;
  }
}

async function fetchMerchantData(merchantName: string): Promise<MerchantData> {
  // 1. Try Fast-Path: Fetch the latest finance.summary bead.
  // We fetch only the most recent summary.
  const summaries = await substrateClient.listBeads({
    namespace: "finance",
    type: "summary",
    limit: 1,
  });

  let fastPathSummary: MerchantData["summary"] | undefined;
  if (summaries.length > 0) {
    const insights = summaries[0].content.merchant_insights as Record<string, any>;
    // The key in the summary is the normalized_merchant.
    // We try to find a match.
    const key = merchantName.toLowerCase().replace(/[^a-z0-9 ]/g, " ").trim();
    if (insights && insights[key]) {
      fastPathSummary = insights[key];
    }
  }

  // 2. Fetch raw data as fallback/supplement.
  const [transactions, bills] = await Promise.all([
    listAllBeads({ namespace: "finance", type: "transaction" }),
    listAllBeads({ namespace: "finance", type: "bill" }),
  ]);

  return {
    summary: fastPathSummary,
    transactions: transactions.filter((t) => {
      // Plaid-retracted beads must not feed spend math or the timeline.
      if (t.state === "removed") return false;
      const c = t.content as unknown as FinanceTransactionContent;
      return (
        merchantMatches(merchantName, c.normalized_merchant) ||
        merchantMatches(merchantName, c.merchant_name)
      );
    }),
    bills: bills.filter((b) =>
      merchantMatches(merchantName, (b.content as unknown as FinanceBillContent).vendor),
    ),
  };
}

function labelMonth(key: string): string {
  const [year, month] = key.split("-");
  const date = new Date(Number(year), Number(month) - 1, 1);
  if (Number.isNaN(date.getTime())) return key;
  return date.toLocaleDateString("en-US", { month: "short" }) + " '" + year.slice(-2);
}

function ordinal(n: number): string {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

export function MerchantDetailRoute() {
  const { name: rawName } = useParams<{ name: string }>();
  const merchantName = decodeURIComponent(rawName ?? "");
  const [selected, setSelected] = useState<Bead | null>(null);

  const merchantQuery = useQuery({
    queryKey: ["merchant", merchantName],
    queryFn: () => fetchMerchantData(merchantName),
    enabled: merchantName.length > 0,
  });

  // Matched transactions, newest first.
  const transactions = useMemo(() => {
    const data = merchantQuery.data?.transactions ?? [];
    return [...data]
      .sort((a, b) => {
        const da = (a.content as unknown as FinanceTransactionContent).posted_date || "";
        const db = (b.content as unknown as FinanceTransactionContent).posted_date || "";
        return db.localeCompare(da);
      });
  }, [merchantQuery.data]);

  const bills = useMemo(() => {
    const data = merchantQuery.data?.bills ?? [];
    return [...data]
      .sort((a, b) => {
        const da = (a.content as unknown as FinanceBillContent).due_date || "";
        const db = (b.content as unknown as FinanceBillContent).due_date || "";
        return db.localeCompare(da);
      });
  }, [merchantQuery.data]);

  // Spend math runs on outflows only (Plaid convention: outflows positive).
  const analysis = useMemo(() => {
    const summary = merchantQuery.data?.summary;
    const months = new Map<string, MonthBucket>();
    let spendTotal = summary?.total_ytd ?? 0;
    let spendCount = summary?.occurrences ?? 0;
    const dayCounts = new Map<number, number>();
    let firstSeen = "";
    let lastSeen = summary?.last_paid ?? "";

    // If we have a summary, we use its totals as a starting point.
    // However, we still iterate transactions to build the chart and day stats.
    let rawTotal = 0;
    let rawCount = 0;

    for (const t of transactions) {
      const c = t.content as unknown as FinanceTransactionContent;
      const date = c.posted_date;
      if (date) {
        if (!firstSeen || date < firstSeen) firstSeen = date;
        if (!lastSeen || date > lastSeen) lastSeen = date;
      }
      if (c.is_transfer || c.amount <= 0) continue;
      rawTotal += c.amount;
      rawCount += 1;

      if (date) {
        const key = date.slice(0, 7);
        const bucket = months.get(key) ?? { key, label: labelMonth(key), total: 0 };
        bucket.total += c.amount;
        months.set(key, bucket);
        const day = new Date(date).getUTCDate();
        if (!Number.isNaN(day)) dayCounts.set(day, (dayCounts.get(day) ?? 0) + 1);
      }
    }

    // If summary is missing, fall back to raw counts.
    if (!summary) {
      spendTotal = rawTotal;
      spendCount = rawCount;
    }

    let typicalDay: number | null = null;
    let best = -1;
    for (const [day, count] of dayCounts) {
      if (count > best) {
        best = count;
        typicalDay = day;
      }
    }

    const monthBuckets = [...months.values()].sort((a, b) => a.key.localeCompare(b.key));
    const maxMonth = monthBuckets.reduce((m, b) => Math.max(m, b.total), 0);

    return {
      months: monthBuckets,
      maxMonth,
      spendTotal,
      spendCount,
      avgAmount: summary?.avg_amount ?? (spendCount > 0 ? spendTotal / spendCount : 0),
      typicalDay,
      firstSeen,
      lastSeen,
      frequency: summary?.frequency ?? "unknown",
    };
  }, [transactions, merchantQuery.data]);

  const isLoading = merchantQuery.isLoading;

  return (
    <>
      <PageHeader
        title={merchantName || "Merchant"}
        subtitle={`${transactions.length} transactions · ${bills.length} linked bill${bills.length === 1 ? "" : "s"} · matched client-side from full history`}
        right={
          <Link to="/ledger" className="text-xs text-fg-muted hover:text-fg underline-offset-2 hover:underline">
            ← Back to ledger
          </Link>
        }
      />

      <div className="p-6 space-y-4">
        {/* 1. Header stats */}
        <Card>
          <div className="p-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 sm:gap-6">
            <Stat label="Total spend" value={fmtCurrency(analysis.spendTotal)} tone="neg" sub={`${analysis.spendCount} charges`} />
            <Stat label="Avg amount" value={fmtCurrency(analysis.avgAmount)} sub="per charge" />
            <Stat
              label="Typical day"
              value={analysis.typicalDay !== null ? ordinal(analysis.typicalDay) : "—"}
              sub="of the month"
            />
            <Stat label="Frequency" value={<span className="capitalize">{analysis.frequency}</span>} tone={analysis.frequency !== "unknown" ? "pos" : "default"} />
            <Stat label="First seen" value={fmtDate(analysis.firstSeen)} />
            <Stat label="Last seen" value={fmtDate(analysis.lastSeen)} />
          </div>
        </Card>

        {/* 2. Spending variance */}
        <Card>
          <CardHeader title="Spending variance" hint="Total spend per month · grouped by YYYY-MM" />
          <div className="p-4">
            {analysis.maxMonth === 0 ? (
              <div className="text-center text-fg-muted py-6 text-sm">No spend found for this merchant.</div>
            ) : (
              <div className="overflow-x-auto pb-2 scrollbar-thin">
                <div className="flex items-end gap-2 h-40" style={{ minWidth: `${Math.max(analysis.months.length * 52, 320)}px` }}>
                  {analysis.months.map((m) => {
                    const pct = analysis.maxMonth > 0 ? (m.total / analysis.maxMonth) * 100 : 0;
                    return (
                      <div key={m.key} className="flex-1 flex flex-col items-center gap-1 min-w-0">
                        <div className="text-2xs num text-fg-subtle whitespace-nowrap">
                          {m.total > 0 ? fmtCurrency(m.total) : ""}
                        </div>
                        <div className="w-full flex items-end justify-center" style={{ height: "100%" }}>
                          <div
                            className="w-full rounded-t bg-accent-muted/40 hover:bg-accent transition-colors"
                            style={{ height: `${Math.max(pct, m.total > 0 ? 4 : 0)}%` }}
                            title={`${m.label}: ${fmtCurrency(m.total)}`}
                          />
                        </div>
                        <div className="text-2xs text-fg-subtle whitespace-nowrap">{m.label}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </Card>

        {/* 4. Linked bills */}
        <Card>
          <CardHeader title="Linked bills" hint="finance.bill beads matching this vendor" />
          <div className="p-3">
            {isLoading ? (
              <div className="text-center text-fg-muted py-4 text-sm">Loading bills…</div>
            ) : bills.length === 0 ? (
              <div className="text-center text-fg-muted py-4 text-sm">No bills linked to this merchant.</div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {bills.map((b) => {
                  const c = b.content as unknown as FinanceBillContent;
                  const today = new Date().toISOString().split("T")[0];
                  const overdue = b.state === "pending" && c.due_date < today;
                  return (
                    <button
                      key={b.id}
                      type="button"
                      onClick={() => setSelected(b)}
                      className="text-left border border-border rounded p-3 hover:bg-bg-hover/60 transition-colors"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="num text-sm font-medium">{fmtCurrency(c.amount)}</span>
                        <Badge tone={b.state === "paid" ? "pos" : overdue ? "neg" : "neutral"}>
                          {overdue ? "overdue" : b.state}
                        </Badge>
                      </div>
                      <div className="text-2xs text-fg-subtle num mt-1">due {fmtDate(c.due_date)}</div>
                      <div className="text-2xs text-fg-subtle mt-0.5 capitalize">
                        {c.source}
                        {c.recurring ? ` · ${c.frequency ?? "recurring"}` : ""}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </Card>

        {/* 5. Transaction feed */}
        <Card>
          <CardHeader title="Transactions" hint="Raw ledger for this merchant" />
          <div className="overflow-x-auto hidden md:block">
            <Table>
              <THead>
                <Tr>
                  <Th>Date</Th>
                  <Th>Description</Th>
                  <Th>Institution</Th>
                  <Th>Category</Th>
                  <Th className="text-right">Amount</Th>
                </Tr>
              </THead>
              <TBody>
                {isLoading ? (
                  <Tr>
                    <Td colSpan={5} className="text-center text-fg-muted py-6">
                      Loading transactions…
                    </Td>
                  </Tr>
                ) : transactions.length === 0 ? (
                  <Tr>
                    <Td colSpan={5} className="text-center text-fg-muted py-6">
                      No transactions found for this merchant.
                    </Td>
                  </Tr>
                ) : (
                  transactions.map((t) => {
                    const c = t.content as unknown as FinanceTransactionContent;
                    const isOutflow = c.amount > 0;
                    return (
                      <Tr key={t.id} className="cursor-pointer" onClick={() => setSelected(t)}>
                        <Td className="num text-2xs text-fg-subtle whitespace-nowrap">{fmtDate(c.posted_date)}</Td>
                        <Td className="text-sm">{c.merchant_name || c.description || "—"}</Td>
                        <Td className="text-xs text-fg-muted">{c.institution || "—"}</Td>
                        <Td>
                          {c.is_transfer ? (
                            <Badge tone="neutral">transfer</Badge>
                          ) : (
                            <Badge tone={c.our_category ? "accent" : "warn"}>{c.our_category || "uncategorized"}</Badge>
                          )}
                        </Td>
                        <Td className={cn("num text-right whitespace-nowrap", isOutflow ? "text-fg" : "text-pos")}>
                          {fmtCurrency(c.amount, c.iso_currency_code)}
                        </Td>
                      </Tr>
                    );
                  })
                )}
              </TBody>
            </Table>
          </div>

          {/* Mobile View for Transactions */}
          <div className="md:hidden divide-y divide-border">
            {isLoading ? (
              <div className="text-center text-fg-muted py-6 text-sm">Loading transactions…</div>
            ) : transactions.length === 0 ? (
              <div className="text-center text-fg-muted py-6 text-sm">No transactions found for this merchant.</div>
            ) : (
              transactions.map((t) => {
                const c = t.content as unknown as FinanceTransactionContent;
                const isOutflow = c.amount > 0;
                return (
                  <div
                    key={t.id}
                    onClick={() => setSelected(t)}
                    className="px-4 py-3 bg-bg-subtle/20 hover:bg-bg-hover/40 active:bg-bg-hover/60 transition-colors flex flex-col gap-1 cursor-pointer"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <span className="text-sm font-semibold text-fg block truncate">
                          {c.merchant_name || c.description || "—"}
                        </span>
                      </div>
                      <span className={cn("num text-sm font-semibold shrink-0", isOutflow ? "text-fg" : "text-pos")}>
                        {fmtCurrency(c.amount, c.iso_currency_code)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2 text-2xs text-fg-subtle">
                      <div className="flex items-center gap-2">
                        <span className="num">{fmtDate(c.posted_date)}</span>
                        <span>·</span>
                        <span>{c.institution || "—"}</span>
                      </div>
                      {c.is_transfer ? (
                        <Badge tone="neutral">transfer</Badge>
                      ) : (
                        <Badge tone={c.our_category ? "accent" : "warn"}>
                          {c.our_category || "uncategorized"}
                        </Badge>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}
