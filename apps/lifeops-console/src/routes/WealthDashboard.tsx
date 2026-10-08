import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { mcpClient } from "@/providers/mcp-client";
import type {
  Bead,
  FinanceAccountContent,
  FinanceDiscrepancyContent,
  FinanceInsightAllocationContent,
  FinanceTransactionContent,
} from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import {
  LedgerVarianceInline,
  LedgerVariancePanel,
  LedgerVarianceTotalInline,
  ledgerVarianceByAccount,
} from "@/components/LedgerVariancePanel";
import { Sparkline } from "@/components/ui/Sparkline";
import { DeltaChip } from "@/components/ui/DeltaChip";
import { useBalanceHistory } from "@/hooks/useBalanceHistory";
import { ledgerVarianceIsUnavailable, useLedgerVariance } from "@/hooks/useLedgerVariance";
import { fmtCurrency, fmtDate, fmtPercent } from "@/lib/format";
import { merchantPath } from "@/lib/merchant";
import { cn } from "@/lib/cn";

const OPTIMIZER_CREATED_BY = "lifeops-console/optimizer-panel";

// Account types Plaid classifies as liabilities. Anything else counts as
// an asset for net-worth math. Keep this list aligned with what bank_sync
// actually persists.
const LIABILITY_ACCOUNT_TYPES = new Set(["credit", "loan"]);

function isLiability(content: FinanceAccountContent): boolean {
  return LIABILITY_ACCOUNT_TYPES.has(content.type) || Boolean(content.liabilities);
}

export function WealthDashboardRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  const queryClient = useQueryClient();

  // SDD Phase 2 (dashboard UX) — net-worth trend + MoM deltas from the nightly
  // finance.balance_snapshot beads.
  const history = useBalanceHistory(180);
  const prior30 = history.prior(30);

  const accountsQuery = useQuery({
    queryKey: ["finance-accounts"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "finance", type: "account", limit: 200 }),
  });

  const txnsQuery = useQuery({
    queryKey: ["finance-transactions-recent"],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "transaction",
        limit: 200,
      }),
  });

  const billsQuery = useQuery({
    queryKey: ["finance-bills-due-summary"],
    queryFn: () => mcpClient.getBillLedger(14),
  });

  const ledgerVarianceQuery = useLedgerVariance();

  const discrepanciesQuery = useQuery({
    queryKey: ["finance-audit-discrepancies-pending"],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "audit_discrepancy",
        state: "pending",
        limit: 100,
      }),
  });

  // SDD Phase 3 — Capital optimizer insights (idle cash → high-APR paydown).
  const insightsQuery = useQuery({
    queryKey: ["finance-insight-allocation-pending"],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "insight_allocation",
        state: "pending",
        limit: 100,
      }),
  });

  const acknowledgeInsight = useMutation({
    mutationFn: (id: string) =>
      substrateClient.updateBead(id, { state: "resolved", created_by: OPTIMIZER_CREATED_BY }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["finance-insight-allocation-pending"] }),
  });

  const accounts = accountsQuery.data ?? [];
  const discrepancies = discrepanciesQuery.data ?? [];
  const insights = insightsQuery.data ?? [];
  const totalAnnualSavings = useMemo(
    () =>
      insights.reduce((sum, b) => {
        const c = b.content as unknown as FinanceInsightAllocationContent;
        return sum + (c.projected_annual_savings ?? 0);
      }, 0),
    [insights],
  );
  // Plaid-retracted beads (state=removed) are excluded from recent activity.
  const transactions = (txnsQuery.data ?? []).filter((t) => t.state !== "removed");
  const billsDue = billsQuery.data;
  const varianceByAccount = useMemo(
    () => ledgerVarianceByAccount(ledgerVarianceQuery.data),
    [ledgerVarianceQuery.data],
  );
  const ledgerVarianceUnavailable = ledgerVarianceIsUnavailable(ledgerVarianceQuery);

  const { assets, liabilities, netWorth, assetCount, liabilityCount } = useMemo(() => {
    let assetSum = 0;
    let liabilitySum = 0;
    let assetN = 0;
    let liabN = 0;
    for (const a of accounts) {
      const c = a.content as unknown as FinanceAccountContent;
      const balance = c.current_balance ?? 0;
      if (isLiability(c)) {
        // Plaid reports liability balances as positive numbers; subtract them.
        liabilitySum += balance;
        liabN += 1;
      } else {
        assetSum += balance;
        assetN += 1;
      }
    }
    return {
      assets: assetSum,
      liabilities: liabilitySum,
      netWorth: assetSum - liabilitySum,
      assetCount: assetN,
      liabilityCount: liabN,
    };
  }, [accounts]);

  const byMerchant = useMemo(() => {
    const map = new Map<string, { total: number; count: number }>();
    for (const t of transactions) {
      const c = t.content as unknown as FinanceTransactionContent;
      if (c.is_transfer) continue;
      // Outflows positive (Plaid convention). Group only spend, not income.
      if (c.amount <= 0) continue;
      const key = c.normalized_merchant || c.merchant_name || "unknown";
      const existing = map.get(key) ?? { total: 0, count: 0 };
      existing.total += c.amount;
      existing.count += 1;
      map.set(key, existing);
    }
    return [...map.entries()]
      .map(([merchant, v]) => ({ merchant, ...v }))
      .sort((a, b) => b.total - a.total)
      .slice(0, 15);
  }, [transactions]);

  const liabilityAccounts = accounts.filter((a) =>
    isLiability(a.content as unknown as FinanceAccountContent),
  );

  return (
    <>
      <PageHeader
        title="Wealth Dashboard"
        subtitle={`finance.account × ${accounts.length} · finance.transaction × ${transactions.length} (last 200)`}
      />

      <div className="p-6 space-y-4">
        {discrepancies.length > 0 ? (
          <Card className="border-neg">
            <CardHeader
              title="⚠️ Ledger Drift Detected"
              hint="Tracked balances diverge from the institution's reported balance"
              right={
                <Link to="/audit" className="text-2xs text-accent hover:underline underline-offset-2">
                  Ledger Audit →
                </Link>
              }
            />
            <CardBody className="space-y-1">
              {discrepancies.slice(0, 5).map((d) => {
                const c = d.content as unknown as FinanceDiscrepancyContent;
                return (
                  <div key={d.id} className="text-sm text-neg">
                    {c.account_name ?? c.account_id} is off by{" "}
                    <span className="num font-semibold">{fmtCurrency(c.drift)}</span>
                  </div>
                );
              })}
              {discrepancies.length > 5 ? (
                <div className="text-2xs text-fg-subtle">
                  +{discrepancies.length - 5} more — see Ledger Audit
                </div>
              ) : null}
            </CardBody>
          </Card>
        ) : null}

        {insights.length > 0 ? (
          <Card className="border-accent">
            <CardHeader
              title="💡 Optimizer Insights"
              hint="Idle checking cash that could pay down high-APR debt"
              right={
                <span className="text-2xs text-pos num">
                  {fmtCurrency(totalAnnualSavings)}/yr potential savings
                </span>
              }
            />
            <CardBody className="space-y-2">
              {insights.map((b) => {
                const c = b.content as unknown as FinanceInsightAllocationContent;
                return (
                  <div
                    key={b.id}
                    className="flex items-center justify-between gap-4 rounded border border-border bg-bg-subtle/40 px-3 py-2"
                  >
                    <div className="text-sm">
                      Move{" "}
                      <span className="num font-medium">{fmtCurrency(c.amount)}</span> from{" "}
                      <span className="font-medium">{c.source_account_name ?? "checking"}</span> →{" "}
                      <span className="font-medium">{c.target_account_name ?? "liability"}</span>
                      {c.target_apr != null ? (
                        <span className="text-2xs text-fg-subtle"> @ {fmtPercent(c.target_apr)} APR</span>
                      ) : null}
                      <div className="text-2xs text-pos num">
                        saves ~{fmtCurrency(c.projected_annual_savings)}/yr in interest
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Button size="sm" variant="ghost" onClick={() => setSelected(b)}>
                        Inspect
                      </Button>
                      <Button
                        size="sm"
                        disabled={acknowledgeInsight.isPending}
                        onClick={() => acknowledgeInsight.mutate(b.id)}
                      >
                        Acknowledge
                      </Button>
                    </div>
                  </div>
                );
              })}
            </CardBody>
          </Card>
        ) : null}

        <Card>
          <CardBody className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
            <Stat
              label="Net worth"
              value={fmtCurrency(netWorth)}
              tone={netWorth >= 0 ? "pos" : "neg"}
              sub={
                <span className="flex flex-col gap-0.5">
                  <span>{assetCount + liabilityCount} accounts</span>
                  <LedgerVarianceTotalInline
                    report={ledgerVarianceQuery.data}
                    unavailable={ledgerVarianceUnavailable}
                  />
                </span>
              }
              delta={<DeltaChip current={netWorth} previous={prior30?.netWorth} showAbsolute />}
            />
            <Stat
              label="Assets"
              value={fmtCurrency(assets)}
              sub={`${assetCount} accounts`}
              delta={<DeltaChip current={assets} previous={prior30?.assets} />}
            />
            <Stat
              label="Liabilities"
              value={fmtCurrency(liabilities)}
              tone={liabilities > 0 ? "neg" : "default"}
              sub={`${liabilityCount} accounts`}
              delta={<DeltaChip current={liabilities} previous={prior30?.liabilities} invert />}
            />
          </CardBody>
        </Card>

        <LedgerVariancePanel
          report={ledgerVarianceQuery.data}
          isLoading={ledgerVarianceQuery.isLoading}
          isUnavailable={ledgerVarianceUnavailable}
        />

        {history.points.length >= 2 ? (
          <Card>
            <CardHeader
              title="Net worth trend"
              hint={`${history.points.length} daily snapshots · ${fmtDate(history.first?.date)} → ${fmtDate(history.latest?.date)}`}
              right={
                history.latest && history.first ? (
                  <DeltaChip current={history.latest.netWorth} previous={history.first.netWorth} showAbsolute />
                ) : null
              }
            />
            <CardBody>
              <div className={`h-32 w-full ${netWorth >= 0 ? "text-pos" : "text-neg"}`}>
                <Sparkline data={history.series} area width={640} height={128} className="h-full w-full" />
              </div>
              <div className="mt-2 flex justify-between text-2xs text-fg-subtle num">
                <span>{fmtCurrency(history.first?.netWorth)}</span>
                <span>{fmtCurrency(history.latest?.netWorth)}</span>
              </div>
            </CardBody>
          </Card>
        ) : null}

        {billsDue && (billsDue.overdue_count > 0 || billsDue.due_soon_count > 0) && (
          <Card className={billsDue.overdue_count > 0 ? "border-neg" : "border-warn"}>
            <CardHeader
              title="Bills due"
              hint="Next 14 days — auto-verified against bank transactions"
              right={
                <Link to="/bills" className="text-2xs text-accent hover:underline underline-offset-2">
                  Bill Guardian →
                </Link>
              }
            />
            <CardBody className="grid grid-cols-1 sm:grid-cols-2 gap-4 sm:gap-6">
              <Stat
                label="Overdue"
                value={billsDue.overdue_count.toString()}
                tone={billsDue.overdue_count > 0 ? "neg" : "default"}
                sub={fmtCurrency(billsDue.overdue_total)}
              />
              <Stat
                label="Due soon"
                value={billsDue.due_soon_count.toString()}
                sub={fmtCurrency(billsDue.due_soon_total)}
              />
            </CardBody>
          </Card>
        )}

        <Card>
          <CardHeader
            title="Accounts"
            hint="Pulled from finance.account beads · current_balance is what Plaid last reported"
          />
          <div className="overflow-x-auto hidden md:block">
            <Table>
              <THead>
                <Tr>
                  <Th>Institution</Th>
                  <Th>Account</Th>
                  <Th>Type</Th>
                  <Th className="text-right">Balance</Th>
                  <Th className="text-right">Integrity</Th>
                  <Th className="text-right">Available</Th>
                  <Th className="w-28">30d</Th>
                  <Th>Last sync</Th>
                </Tr>
              </THead>
              <TBody>
                {accountsQuery.isLoading ? (
                  <Tr>
                    <Td colSpan={8} className="text-center text-fg-muted py-6">
                      Loading accounts…
                    </Td>
                  </Tr>
                ) : accounts.length === 0 ? (
                  <Tr>
                    <Td colSpan={8} className="text-center text-fg-muted py-6">
                      No finance.account beads yet.
                    </Td>
                  </Tr>
                ) : (
                  accounts.map((a) => {
                    const c = a.content as unknown as FinanceAccountContent;
                    return (
                      <Tr key={a.id} className="cursor-pointer" onClick={() => setSelected(a)}>
                        <Td className="text-sm">{c.institution}</Td>
                        <Td>
                          <div className="text-sm">{c.name}</div>
                          {c.mask ? (
                            <div className="text-2xs text-fg-subtle font-mono">···{c.mask}</div>
                          ) : null}
                        </Td>
                        <Td>
                          <Badge tone={isLiability(c) ? "neg" : "accent"}>
                            {c.type}
                            {c.subtype ? `/${c.subtype}` : ""}
                          </Badge>
                        </Td>
                        <Td
                          className={`num text-right ${
                            isLiability(c) ? "text-neg" : "text-fg"
                          }`}
                        >
                          {fmtCurrency(c.current_balance, c.iso_currency_code)}
                        </Td>
                        <Td>
                          <LedgerVarianceInline
                            row={varianceByAccount.get(a.id)}
                            unavailable={ledgerVarianceUnavailable}
                          />
                        </Td>
                        <Td className="num text-right text-fg-muted">
                          {fmtCurrency(c.available_balance, c.iso_currency_code)}
                        </Td>
                        <Td>
                          <div className={`h-6 w-24 ${isLiability(c) ? "text-neg" : "text-pos"}`}>
                            <Sparkline
                              data={history.seriesByAccount[a.id] ?? []}
                              width={96}
                              height={24}
                              className="h-full w-full"
                            />
                          </div>
                        </Td>
                        <Td className="num text-2xs text-fg-subtle">{fmtDate(c.last_synced)}</Td>
                      </Tr>
                    );
                  })
                )}
              </TBody>
            </Table>
          </div>

          {/* Mobile View for Accounts */}
          <div className="md:hidden grid grid-cols-1 gap-2.5 p-3">
            {accountsQuery.isLoading ? (
              <div className="text-center text-fg-muted py-6">Loading accounts…</div>
            ) : accounts.length === 0 ? (
              <div className="text-center text-fg-muted py-6">No finance.account beads yet.</div>
            ) : (
              accounts.map((a) => {
                const c = a.content as unknown as FinanceAccountContent;
                return (
                  <div
                    key={a.id}
                    onClick={() => setSelected(a)}
                    className="border border-border rounded p-3 bg-bg-subtle/40 hover:bg-bg-hover/50 transition-colors flex flex-col gap-2 cursor-pointer active:bg-bg-hover"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <span className="text-2xs text-fg-subtle uppercase tracking-wider block font-mono">{c.institution}</span>
                        <span className="text-sm font-medium text-fg block truncate">{c.name} {c.mask ? `(···${c.mask})` : ""}</span>
                      </div>
                      <div className="text-right shrink-0">
                        <span className={cn("num text-sm font-semibold block", isLiability(c) ? "text-neg" : "text-fg")}>
                          {fmtCurrency(c.current_balance, c.iso_currency_code)}
                        </span>
                        <LedgerVarianceInline
                          row={varianceByAccount.get(a.id)}
                          unavailable={ledgerVarianceUnavailable}
                        />
                        {c.available_balance !== undefined && c.available_balance !== c.current_balance ? (
                          <span className="num text-2xs text-fg-muted block">
                            Avail: {fmtCurrency(c.available_balance, c.iso_currency_code)}
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex items-center justify-between gap-2 mt-0.5 border-t border-border/30 pt-2">
                      <Badge tone={isLiability(c) ? "neg" : "accent"}>
                        {c.type}{c.subtype ? `/${c.subtype}` : ""}
                      </Badge>
                      <div className="flex items-center gap-3">
                        <div className={`h-5 w-20 ${isLiability(c) ? "text-neg" : "text-pos"}`}>
                          <Sparkline
                            data={history.seriesByAccount[a.id] ?? []}
                            width={80}
                            height={20}
                            className="h-full w-full"
                          />
                        </div>
                        <span className="num text-2xs text-fg-subtle">
                          {fmtDate(c.last_synced)}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card>
            <CardHeader
              title="Liabilities"
              hint="APR, principal, next payment — from Plaid /liabilities/get"
            />
            <div className="overflow-x-auto hidden md:block">
              <Table>
                <THead>
                  <Tr>
                    <Th>Account</Th>
                    <Th className="text-right">APR</Th>
                    <Th className="text-right">Principal</Th>
                    <Th className="text-right">Min payment</Th>
                    <Th>Due</Th>
                  </Tr>
                </THead>
                <TBody>
                  {liabilityAccounts.length === 0 ? (
                    <Tr>
                      <Td colSpan={5} className="text-center text-fg-muted py-6">
                        No liability accounts.
                      </Td>
                    </Tr>
                  ) : (
                    liabilityAccounts.map((a) => {
                      const c = a.content as unknown as FinanceAccountContent;
                      const l = c.liabilities ?? {};
                      return (
                        <Tr key={a.id} className="cursor-pointer" onClick={() => setSelected(a)}>
                          <Td>
                            <div className="text-sm">{c.name}</div>
                            <div className="text-2xs text-fg-subtle">{c.institution}</div>
                          </Td>
                          <Td className="num text-right">{fmtPercent(l.apr)}</Td>
                          <Td className="num text-right text-neg">
                            {fmtCurrency(l.principal, c.iso_currency_code)}
                          </Td>
                          <Td className="num text-right">
                            {fmtCurrency(l.min_payment, c.iso_currency_code)}
                          </Td>
                          <Td className="num text-xs">{fmtDate(l.next_payment_date ?? l.due_date)}</Td>
                        </Tr>
                      );
                    })
                  )}
                </TBody>
              </Table>
            </div>

            {/* Mobile View for Liabilities */}
            <div className="md:hidden grid grid-cols-1 gap-2.5 p-3">
              {liabilityAccounts.length === 0 ? (
                <div className="text-center text-fg-muted py-6">No liability accounts.</div>
              ) : (
                liabilityAccounts.map((a) => {
                  const c = a.content as unknown as FinanceAccountContent;
                  const l = c.liabilities ?? {};
                  return (
                    <div
                      key={a.id}
                      onClick={() => setSelected(a)}
                      className="border border-border rounded p-3 bg-bg-subtle/40 hover:bg-bg-hover/50 transition-colors flex flex-col gap-1 cursor-pointer active:bg-bg-hover"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <span className="text-sm font-semibold text-fg">{c.name}</span>
                          <span className="text-2xs text-fg-subtle block">{c.institution}</span>
                        </div>
                        <div className="text-right">
                          <span className="num text-sm font-semibold text-neg">
                            {fmtCurrency(l.principal, c.iso_currency_code)}
                          </span>
                          <span className="num text-2xs text-fg-muted block">
                            APR: {fmtPercent(l.apr)}
                          </span>
                        </div>
                      </div>
                      <div className="flex justify-between items-center mt-2 border-t border-border/30 pt-2 text-2xs text-fg-subtle">
                        <span>Min Pay: <span className="num font-semibold text-fg">{fmtCurrency(l.min_payment, c.iso_currency_code)}</span></span>
                        <span>Due: <span className="num font-semibold text-fg">{fmtDate(l.next_payment_date ?? l.due_date)}</span></span>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Top spend by merchant"
              hint="Last 200 transactions · transfers excluded"
            />
            <div className="overflow-x-auto hidden md:block">
              <Table>
                <THead>
                  <Tr>
                    <Th>Merchant</Th>
                    <Th className="text-right">Count</Th>
                    <Th className="text-right">Total</Th>
                  </Tr>
                </THead>
                <TBody>
                  {byMerchant.length === 0 ? (
                    <Tr>
                      <Td colSpan={3} className="text-center text-fg-muted py-6">
                        No transactions yet.
                      </Td>
                    </Tr>
                  ) : (
                    byMerchant.map((row) => (
                      <Tr key={row.merchant}>
                        <Td className="text-sm">
                          <Link
                            to={merchantPath(row.merchant)}
                            className="text-accent hover:underline underline-offset-2"
                          >
                            {row.merchant}
                          </Link>
                        </Td>
                        <Td className="num text-right text-fg-muted">{row.count}</Td>
                        <Td className="num text-right text-neg">{fmtCurrency(row.total)}</Td>
                      </Tr>
                    ))
                  )}
                </TBody>
              </Table>
            </div>

            {/* Mobile View for Top Spend */}
            <div className="md:hidden grid grid-cols-1 gap-2.5 p-3">
              {byMerchant.length === 0 ? (
                <div className="text-center text-fg-muted py-6">No transactions yet.</div>
              ) : (
                byMerchant.map((row) => (
                  <div
                    key={row.merchant}
                    className="border border-border rounded p-3 bg-bg-subtle/40 flex items-center justify-between gap-4"
                  >
                    <div className="min-w-0 flex-1">
                      <Link
                        to={merchantPath(row.merchant)}
                        className="text-sm font-medium text-accent hover:underline underline-offset-2 block truncate"
                      >
                        {row.merchant}
                      </Link>
                      <span className="num text-2xs text-fg-subtle">{row.count} spend events</span>
                    </div>
                    <span className="num text-sm font-semibold text-neg shrink-0">
                      {fmtCurrency(row.total)}
                    </span>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}
