import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { mcpClient } from "@/providers/mcp-client";
import type { FinanceAccountContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Sparkline } from "@/components/ui/Sparkline";
import { DeltaChip } from "@/components/ui/DeltaChip";
import { fmtCurrency, fmtDate } from "@/lib/format";
import { useBalanceHistory } from "@/hooks/useBalanceHistory";
import { useAttentionItems, SEVERITY_TONE, type AttentionItem } from "@/hooks/useAttentionItems";

const LIABILITY_ACCOUNT_TYPES = new Set(["credit", "loan"]);
const DEPOSITORY_TYPES = new Set(["depository", "cash"]);

function isLiability(c: FinanceAccountContent): boolean {
  return LIABILITY_ACCOUNT_TYPES.has(c.type) || Boolean(c.liabilities);
}

function SeverityDot({ tone }: { tone: "neg" | "warn" | "accent" | "neutral" }) {
  const bg =
    tone === "neg" ? "bg-neg" : tone === "warn" ? "bg-warn" : tone === "accent" ? "bg-accent" : "bg-fg-subtle";
  return <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${bg}`} aria-hidden />;
}

function AttentionRow({ item }: { item: AttentionItem }) {
  return (
    <Link
      to={item.route}
      className="flex items-start gap-3 rounded border border-border bg-bg-subtle/40 px-3 py-2 transition-colors hover:bg-bg-hover"
    >
      <SeverityDot tone={SEVERITY_TONE[item.severity]} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-fg">{item.title}</span>
          <Badge tone={SEVERITY_TONE[item.severity]}>{item.severity}</Badge>
        </div>
        <div className="truncate text-2xs text-fg-muted">{item.detail}</div>
      </div>
      {item.amount != null ? (
        <div className="shrink-0 text-right">
          <div className="num text-sm text-fg">{fmtCurrency(item.amount)}</div>
          {item.amountLabel ? (
            <div className="text-2xs uppercase tracking-wider text-fg-subtle">{item.amountLabel}</div>
          ) : null}
        </div>
      ) : null}
    </Link>
  );
}

export function TodayRoute() {
  const accountsQuery = useQuery({
    queryKey: ["finance-accounts"],
    queryFn: () => substrateClient.listBeads({ namespace: "finance", type: "account", limit: 200 }),
  });
  const runRateQuery = useQuery({
    queryKey: ["finance-run-rate"],
    queryFn: () => mcpClient.getRunRate(3),
  });
  const budgetQuery = useQuery({
    queryKey: ["finance-budget-status"],
    queryFn: () => mcpClient.getBudgetStatus("current"),
  });

  const history = useBalanceHistory(120);
  const attention = useAttentionItems();

  const accounts = accountsQuery.data ?? [];
  const { assets, liabilities, netWorth, cashOnHand, cashAccounts } = useMemo(() => {
    let a = 0;
    let l = 0;
    let cash = 0;
    let cashN = 0;
    for (const acct of accounts) {
      const c = acct.content as unknown as FinanceAccountContent;
      const bal = c.current_balance ?? 0;
      if (isLiability(c)) {
        l += bal;
      } else {
        a += bal;
        if (DEPOSITORY_TYPES.has(c.type)) {
          cash += c.available_balance ?? c.current_balance ?? 0;
          cashN += 1;
        }
      }
    }
    return { assets: a, liabilities: l, netWorth: a - l, cashOnHand: cash, cashAccounts: cashN };
  }, [accounts]);

  const prevNetWorth = history.prior(30)?.netWorth ?? null;

  const monthSpend = budgetQuery.data?.total?.spent ?? null;
  const runRate = runRateQuery.data?.monthly_run_rate ?? null;
  const pctOfRunRate =
    monthSpend != null && runRate && runRate > 0 ? Math.round((monthSpend / runRate) * 100) : null;

  const topItems = attention.items.slice(0, 6);

  return (
    <>
      <PageHeader
        title="Today"
        subtitle="Your money at a glance — net worth, spend pace, and what needs you"
        right={
          // `total > 0 ? warn : pos` made the green badge the default for
          // every non-positive case — including "nothing has loaded yet" and
          // "every request failed". The badge is the first thing on the first
          // screen, so it is the last place an unknown should read as good.
          attention.isError ? (
            <Badge tone="warn">unknown</Badge>
          ) : !attention.isReady ? (
            <Badge tone="neutral">checking…</Badge>
          ) : attention.total > 0 ? (
            <Badge tone="warn">{attention.total} need attention</Badge>
          ) : (
            <Badge tone="pos">all clear</Badge>
          )
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        {/* Net worth headline + trend */}
        <Card>
          <CardBody className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 sm:gap-6">
            <div className="min-w-0">
              <span className="panel-title">Net worth</span>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className={`num text-3xl sm:text-4xl font-semibold ${netWorth >= 0 ? "text-fg" : "text-neg"}`}>
                  {fmtCurrency(netWorth)}
                </span>
                {history.latest ? (
                  <DeltaChip current={history.latest.netWorth} previous={prevNetWorth} showAbsolute />
                ) : null}
              </div>
              <div className="mt-1 text-2xs text-fg-subtle break-words">
                {history.first ? `since ${fmtDate(history.first.date)} · ` : ""}
                {assets >= 0 ? `${fmtCurrency(assets)} assets · ${fmtCurrency(liabilities)} debt` : null}
              </div>
            </div>
            <div className={`h-12 w-full sm:w-48 shrink-0 ${netWorth >= 0 ? "text-pos" : "text-neg"}`}>
              <Sparkline data={history.series} area width={192} height={48} className="h-full w-full" />
            </div>
          </CardBody>
        </Card>

        {/* Quick stats */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4">
          <Card>
            <CardBody>
              <Stat
                label="Cash on hand"
                value={fmtCurrency(cashOnHand)}
                sub={`${cashAccounts} liquid account${cashAccounts === 1 ? "" : "s"}`}
              />
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <div className="flex flex-col gap-1">
                <span className="panel-title">Spent this month</span>
                <span className="flex flex-wrap items-baseline gap-2">
                  <span className="num text-xl font-semibold text-fg">
                    {monthSpend != null ? fmtCurrency(monthSpend) : "—"}
                  </span>
                </span>
                <span className="text-2xs text-fg-subtle num">
                  {pctOfRunRate != null ? `${pctOfRunRate}% of ${fmtCurrency(runRate)}/mo run rate` : "run rate —"}
                </span>
              </div>
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <Stat
                label="Liabilities"
                value={fmtCurrency(liabilities)}
                tone={liabilities > 0 ? "neg" : "default"}
                sub="total debt"
              />
            </CardBody>
          </Card>
        </div>

        {/* Needs attention feed */}
        <Card>
          <CardHeader
            title="Needs attention"
            hint="Every pending insight, ranked by urgency"
            right={
              attention.total > topItems.length ? (
                <Link to="/inbox" className="text-2xs text-accent underline-offset-2 hover:underline">
                  view all {attention.total} →
                </Link>
              ) : null
            }
          />
          <CardBody className="space-y-2">
            {attention.isError ? (
              // Never "all clear" off a read that failed — an unreachable
              // substrate must not look like a quiet week.
              <div className="py-6 text-center text-sm text-warn">
                Could not load insights. This is not an all-clear.
              </div>
            ) : !attention.isReady ? (
              <div className="py-6 text-center text-sm text-fg-muted">Scanning for insights…</div>
            ) : topItems.length === 0 ? (
              <div className="py-6 text-center text-sm text-fg-muted">
                ✓ All clear — nothing needs you right now.
              </div>
            ) : (
              topItems.map((item) => <AttentionRow key={item.id} item={item} />)
            )}
          </CardBody>
        </Card>
      </div>
    </>
  );
}
