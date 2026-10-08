import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead, FinanceTransactionContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { fmtCurrency, fmtDate } from "@/lib/format";
import { cn } from "@/lib/cn";

// Phase 6.5's TransferPairingWorkflow auto-pairs internal-transfer legs nightly
// (writes a finance.transfer bead + stamps transfer_pair_id on both legs). Its
// matcher is deliberately conservative: same |amount| ±$0.01, ±3 days, cross
// account. Anything it can't confidently pair (cross-institution settling >3d
// apart, fees/FX) stays orphaned. This view is the manual escape hatch for
// those residual orphans — and it writes the EXACT same representation as the
// workflow so manual and auto matches behave identically downstream.

const ORPHANS_KEY = ["finance-orphan-transfers"];
const TXN_PAGE_SIZE = 1000;
const MAX_TXN_PAGES = 100;
const AMOUNT_TOLERANCE = 0.01;
const DATE_WINDOW_DAYS = 5;

interface Leg {
  bead: Bead;
  id: string;
  accountId: string;
  amount: number;
  postedDate: string;
  currency: string;
  merchant: string;
}

function toLeg(bead: Bead): Leg {
  const c = bead.content as unknown as FinanceTransactionContent;
  return {
    bead,
    id: bead.id,
    accountId: c.account_id,
    amount: c.amount,
    postedDate: c.posted_date,
    currency: c.iso_currency_code || "USD",
    merchant: c.normalized_merchant || c.merchant_name || c.description || "—",
  };
}

function daysApart(a: string, b: string): number {
  const ta = new Date(a).getTime();
  const tb = new Date(b).getTime();
  if (Number.isNaN(ta) || Number.isNaN(tb)) return Number.POSITIVE_INFINITY;
  return Math.abs(ta - tb) / 86_400_000;
}

async function fetchAllTransactions(): Promise<Bead[]> {
  const all: Bead[] = [];
  for (let page = 0; page < MAX_TXN_PAGES; page += 1) {
    const chunk = await substrateClient.listBeads({
      namespace: "finance",
      type: "transaction",
      limit: TXN_PAGE_SIZE,
      offset: page * TXN_PAGE_SIZE,
    });
    all.push(...chunk);
    if (chunk.length < TXN_PAGE_SIZE) break;
  }
  return all;
}

export function TransferLineageRoute() {
  const queryClient = useQueryClient();
  const [matching, setMatching] = useState<Leg | null>(null);

  const txnsQuery = useQuery({
    queryKey: ORPHANS_KEY,
    queryFn: fetchAllTransactions,
  });

  const accountsQuery = useQuery({
    queryKey: ["finance-accounts"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "finance", type: "account", limit: 200 }),
  });

  const accountName = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of accountsQuery.data ?? []) {
      const name = (a.content as { name?: string }).name;
      if (name) map.set(a.id, name);
    }
    return (id: string) => map.get(id) ?? id;
  }, [accountsQuery.data]);

  // Orphaned transfers: flagged is_transfer, not yet paired, not retracted.
  const orphans = useMemo(() => {
    return (txnsQuery.data ?? [])
      .filter((b) => {
        if (b.state === "removed") return false;
        const c = b.content as unknown as FinanceTransactionContent;
        return Boolean(c.is_transfer) && !c.transfer_pair_id;
      })
      .map(toLeg)
      .sort((a, b) => (b.postedDate ?? "").localeCompare(a.postedDate ?? ""));
  }, [txnsQuery.data]);

  // Candidates for the leg being matched: opposite-signed, near-equal absolute
  // amount, different account, within the date window. Mirrors the backend
  // matcher's criteria, with a wider date window for late-settling transfers.
  const candidates = useMemo(() => {
    if (!matching) return [] as Leg[];
    return orphans
      .filter(
        (o) =>
          o.id !== matching.id &&
          o.accountId !== matching.accountId &&
          Math.sign(o.amount) !== Math.sign(matching.amount) &&
          Math.abs(Math.abs(o.amount) - Math.abs(matching.amount)) <= AMOUNT_TOLERANCE &&
          daysApart(o.postedDate, matching.postedDate) <= DATE_WINDOW_DAYS,
      )
      .sort(
        (a, b) =>
          daysApart(a.postedDate, matching.postedDate) -
          daysApart(b.postedDate, matching.postedDate),
      );
  }, [matching, orphans]);

  const link = useMutation({
    mutationFn: async ({ a, b }: { a: Leg; b: Leg }) => {
      // Orient legs: outflow is the positive amount (Plaid convention).
      const [outflow, inflow] = a.amount > 0 ? [a, b] : [b, a];
      const pairId = crypto.randomUUID();
      const patchedLegs: Leg[] = [];

      // 1. The finance.transfer bead — identical shape to the workflow's
      //    persist_transfer_pair_activity.
      const transfer = await substrateClient.createBead({
        namespace: "finance",
        type: "transfer",
        state: "paired",
        trust_tier: "system",
        created_by: "lifeops-console/manual-transfer-link",
        content: {
          transfer_pair_id: pairId,
          from_tx_id: outflow.id,
          to_tx_id: inflow.id,
          from_account_id: outflow.accountId,
          to_account_id: inflow.accountId,
          amount: Math.abs(outflow.amount),
          iso_currency_code: outflow.currency,
          posted_date: outflow.postedDate,
        },
      });

      try {
        // 2. Stamp transfer_pair_id on both legs. PATCH replaces content
        //    wholesale, so send the full existing content + the new key.
        for (const leg of [outflow, inflow]) {
          await substrateClient.updateBead(leg.id, {
            content: { ...leg.bead.content, transfer_pair_id: pairId },
            created_by: "lifeops-console/manual-transfer-link",
          });
          patchedLegs.push(leg);
        }
      } catch (error) {
        await Promise.allSettled(
          patchedLegs.map((leg) =>
            substrateClient.updateBead(leg.id, {
              content: leg.bead.content,
              created_by: "lifeops-console/manual-transfer-link-rollback",
            }),
          ),
        );
        await substrateClient.deleteBead(transfer.id).catch(() => undefined);
        throw error;
      }
    },
    onSuccess: () => {
      setMatching(null);
      queryClient.invalidateQueries({ queryKey: ORPHANS_KEY });
    },
  });

  return (
    <>
      <PageHeader
        title="Transfer Audit"
        subtitle="Orphaned internal transfers awaiting a counterpart"
        right={<span className="text-2xs text-fg-subtle num">{orphans.length} orphaned</span>}
      />

      <div className="p-6 space-y-4">
        <Card>
          <CardHeader
            title="Orphaned transfers"
            hint="is_transfer · not yet paired — the nightly auto-pairer couldn't match these"
          />
          {/* Desktop view */}
          <div className="hidden md:block overflow-x-auto">
            <Table>
              <THead>
                <Tr>
                  <Th>Date</Th>
                  <Th>Account</Th>
                  <Th>Merchant</Th>
                  <Th className="text-right">Amount</Th>
                  <Th className="text-right">Action</Th>
                </Tr>
              </THead>
              <TBody>
                {txnsQuery.isLoading ? (
                  <Tr>
                    <Td colSpan={5} className="text-center text-fg-muted py-6">
                      Loading transfers…
                    </Td>
                  </Tr>
                ) : txnsQuery.isError ? (
                  <Tr>
                    <Td colSpan={5} className="text-center text-neg py-6">
                      Failed to load transactions.
                    </Td>
                  </Tr>
                ) : orphans.length === 0 ? (
                  <Tr>
                    <Td colSpan={5} className="text-center text-fg-muted py-6">
                      ✓ No orphaned transfers. Every internal move is paired.
                    </Td>
                  </Tr>
                ) : (
                  orphans.map((o) => (
                    <Tr key={o.id}>
                      <Td className="num text-2xs text-fg-subtle whitespace-nowrap">
                        {fmtDate(o.postedDate)}
                      </Td>
                      <Td className="text-sm">{accountName(o.accountId)}</Td>
                      <Td className="text-sm text-fg-muted">{o.merchant}</Td>
                      <Td className={`num text-right ${o.amount > 0 ? "text-fg" : "text-pos"}`}>
                        {fmtCurrency(o.amount, o.currency)}
                      </Td>
                      <Td className="text-right">
                        <Button variant="outline" size="sm" onClick={() => setMatching(o)}>
                          Match transfer
                        </Button>
                      </Td>
                    </Tr>
                  ))
                )}
              </TBody>
            </Table>
          </div>

          {/* Mobile view */}
          <div className="md:hidden divide-y divide-border/60">
            {txnsQuery.isLoading ? (
              <div className="text-center text-fg-muted py-8 text-sm">Loading transfers…</div>
            ) : txnsQuery.isError ? (
              <div className="text-center text-neg py-8 text-sm">Failed to load transactions.</div>
            ) : orphans.length === 0 ? (
              <div className="text-center text-fg-muted py-8 text-sm">✓ No orphaned transfers.</div>
            ) : (
              orphans.map((o) => (
                <div key={o.id} className="py-3 px-3 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-semibold text-fg truncate">{o.merchant}</div>
                      <div className="text-2xs text-fg-subtle mt-0.5">{accountName(o.accountId)}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className={cn("num text-sm font-semibold", o.amount > 0 ? "text-fg" : "text-pos")}>
                        {fmtCurrency(o.amount, o.currency)}
                      </div>
                      <div className="text-2xs text-fg-subtle num mt-0.5">{fmtDate(o.postedDate)}</div>
                    </div>
                  </div>
                  <div className="flex justify-end mt-1 border-t border-border/30 pt-2">
                    <Button variant="outline" size="sm" onClick={() => setMatching(o)} className="w-full text-center">
                      Match transfer
                    </Button>
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>

      {matching ? (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setMatching(null)}
        >
          <div
            className="card w-full max-w-2xl max-h-[80vh] overflow-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <CardHeader
              title="Match transfer"
              hint={`${fmtCurrency(matching.amount, matching.currency)} · ${accountName(
                matching.accountId,
              )} · ${fmtDate(matching.postedDate)}`}
              right={
                <Button variant="ghost" size="sm" onClick={() => setMatching(null)}>
                  Close
                </Button>
              }
            />
            <div className="p-3">
              <div className="text-2xs uppercase tracking-wider text-fg-subtle mb-2">
                Candidates · opposite leg, ±{AMOUNT_TOLERANCE.toFixed(2)} amount, ±
                {DATE_WINDOW_DAYS}d
              </div>
              {candidates.length === 0 ? (
                <div className="text-center text-fg-muted py-6 text-sm">
                  No candidate counterpart found among orphaned transfers.
                </div>
              ) : (
                <>
                  {/* Desktop Candidates Table */}
                  <div className="hidden md:block overflow-x-auto">
                    <Table>
                      <THead>
                        <Tr>
                          <Th>Date</Th>
                          <Th>Account</Th>
                          <Th>Merchant</Th>
                          <Th className="text-right">Amount</Th>
                          <Th className="text-right">Link</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {candidates.map((c) => (
                          <Tr key={c.id}>
                            <Td className="num text-2xs text-fg-subtle whitespace-nowrap">
                              {fmtDate(c.postedDate)}
                              {daysApart(c.postedDate, matching.postedDate) > 0 ? (
                                <Badge tone="neutral" className="ml-1">
                                  {Math.round(daysApart(c.postedDate, matching.postedDate))}d
                                </Badge>
                              ) : null}
                            </Td>
                            <Td className="text-sm">{accountName(c.accountId)}</Td>
                            <Td className="text-sm text-fg-muted">{c.merchant}</Td>
                            <Td className={`num text-right ${c.amount > 0 ? "text-fg" : "text-pos"}`}>
                              {fmtCurrency(c.amount, c.currency)}
                            </Td>
                            <Td className="text-right">
                              <Button
                                size="sm"
                                disabled={link.isPending}
                                onClick={() => link.mutate({ a: matching, b: c })}
                              >
                                Link
                              </Button>
                            </Td>
                          </Tr>
                        ))}
                      </TBody>
                    </Table>
                  </div>

                  {/* Mobile Candidates List */}
                  <div className="md:hidden divide-y divide-border/60 max-h-[60vh] overflow-y-auto">
                    {candidates.map((c) => (
                      <div key={c.id} className="py-2.5 px-3 flex flex-col gap-2">
                        <div className="flex items-start justify-between gap-4">
                          <div className="min-w-0 flex-1">
                            <div className="text-sm font-medium text-fg truncate">{c.merchant}</div>
                            <div className="text-2xs text-fg-subtle mt-0.5">{accountName(c.accountId)}</div>
                          </div>
                          <div className="text-right shrink-0">
                            <div className={cn("num text-sm font-semibold", c.amount > 0 ? "text-fg" : "text-pos")}>
                              {fmtCurrency(c.amount, c.currency)}
                            </div>
                            <div className="flex items-center justify-end gap-1.5 mt-0.5">
                              <span className="text-2xs text-fg-subtle num">{fmtDate(c.postedDate)}</span>
                              {daysApart(c.postedDate, matching.postedDate) > 0 ? (
                                <Badge tone="neutral">
                                  {Math.round(daysApart(c.postedDate, matching.postedDate))}d
                                </Badge>
                              ) : null}
                            </div>
                          </div>
                        </div>
                        <div className="flex justify-end mt-1 border-t border-border/30 pt-2">
                          <Button
                            size="sm"
                            disabled={link.isPending}
                            onClick={() => link.mutate({ a: matching, b: c })}
                            className="w-full text-center"
                          >
                            Link Partner
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
              {link.isError ? (
                <div className="text-neg text-xs mt-3">
                  Link failed: {(link.error as Error).message}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
