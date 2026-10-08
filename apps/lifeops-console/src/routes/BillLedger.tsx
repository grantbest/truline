import { useQuery } from "@tanstack/react-query";
import { mcpClient } from "@/providers/mcp-client";
import type { BillEntry } from "@/providers/mcp-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader, Stat } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { fmtCurrency, fmtDate } from "@/lib/format";

function BillTable({
  bills,
  showProof = false,
  emptyLabel,
}: {
  bills: BillEntry[];
  showProof?: boolean;
  emptyLabel: string;
}) {
  return (
    <>
      {/* Desktop Table View */}
      <div className="hidden md:block overflow-x-auto">
        <Table>
          <THead>
            <Tr>
              <Th>Vendor</Th>
              <Th>{showProof ? "Paid" : "Due"}</Th>
              <Th className="text-right">Amount</Th>
              <Th>Category</Th>
              <Th>{showProof ? "Proof of payment" : "Status"}</Th>
            </Tr>
          </THead>
          <TBody>
            {bills.length === 0 ? (
              <Tr>
                <Td colSpan={5} className="text-center text-fg-muted py-6">
                  {emptyLabel}
                </Td>
              </Tr>
            ) : (
              bills.map((b) => (
                <Tr key={b.bead_id}>
                  <Td>
                    <div className="font-medium">{b.vendor ?? "—"}</div>
                    <span className="text-2xs text-fg-subtle capitalize">{b.source}</span>
                  </Td>
                  <Td className="num">
                    <span className={b.status === "overdue" ? "text-neg font-bold" : ""}>
                      {fmtDate(showProof ? b.proof?.posted_date ?? b.due_date : b.due_date)}
                    </span>
                  </Td>
                  <Td className="num text-right font-medium">{fmtCurrency(b.amount)}</Td>
                  <Td>
                    <Badge tone="neutral">{b.category}</Badge>
                  </Td>
                  <Td>
                    {showProof ? (
                      b.proof ? (
                        <span className="text-2xs text-pos">
                          ✓ {b.proof.merchant ?? "matched"} · {fmtCurrency(b.proof.amount)}
                        </span>
                      ) : (
                        <span className="text-2xs text-fg-subtle">marked paid (manual)</span>
                      )
                    ) : (
                      <Badge
                        tone={
                          b.status === "overdue"
                            ? "neg"
                            : b.status === "due_soon"
                            ? "warn"
                            : "neutral"
                        }
                      >
                        {b.status === "due_soon" ? "due soon" : b.status}
                      </Badge>
                    )}
                  </Td>
                </Tr>
              ))
            )}
          </TBody>
        </Table>
      </div>

      {/* Mobile Card List View */}
      <div className="md:hidden divide-y divide-border/60">
        {bills.length === 0 ? (
          <div className="text-center text-fg-muted py-8 text-sm">{emptyLabel}</div>
        ) : (
          bills.map((b) => (
            <div key={b.bead_id} className="py-3 px-3 flex flex-col gap-2">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm text-fg truncate">{b.vendor ?? "—"}</div>
                  <div className="text-2xs text-fg-subtle capitalize mt-0.5">{b.source}</div>
                </div>
                <div className="text-right shrink-0">
                  <div className="num text-sm font-semibold text-fg">{fmtCurrency(b.amount)}</div>
                  <div className="text-2xs text-fg-subtle mt-0.5">
                    {showProof ? "Paid" : "Due"}:{" "}
                    <span className={!showProof && b.status === "overdue" ? "text-neg font-bold" : "num"}>
                      {fmtDate(showProof ? b.proof?.posted_date ?? b.due_date : b.due_date)}
                    </span>
                  </div>
                </div>
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-border/30 pt-2 mt-0.5">
                <Badge tone="neutral">{b.category}</Badge>
                {showProof ? (
                  b.proof ? (
                    <span className="text-2xs text-pos num">
                      ✓ {b.proof.merchant ?? "matched"} ({fmtCurrency(b.proof.amount)})
                    </span>
                  ) : (
                    <span className="text-2xs text-fg-subtle">marked paid (manual)</span>
                  )
                ) : (
                  <Badge
                    tone={
                      b.status === "overdue"
                        ? "neg"
                        : b.status === "due_soon"
                        ? "warn"
                        : "neutral"
                    }
                  >
                    {b.status === "due_soon" ? "due soon" : b.status}
                  </Badge>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </>
  );
}

export function BillLedgerRoute() {
  const ledgerQuery = useQuery({
    queryKey: ["finance-bill-ledger"],
    queryFn: () => mcpClient.getBillLedger(14),
  });

  const data = ledgerQuery.data;
  const groups = data?.groups ?? { overdue: [], due_soon: [], upcoming: [], paid: [] };

  return (
    <>
      <PageHeader
        title="Bill Guardian"
        subtitle="Auto-verified against bank transactions — what's overdue, due soon, and proven paid"
      />

      <div className="p-6 space-y-4">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
          <Stat
            label="Overdue"
            value={(data?.overdue_count ?? 0).toString()}
            tone={(data?.overdue_count ?? 0) > 0 ? "neg" : "default"}
            sub={fmtCurrency(data?.overdue_total ?? 0)}
          />
          <Stat
            label="Due soon (14d)"
            value={(data?.due_soon_count ?? 0).toString()}
            sub={fmtCurrency(data?.due_soon_total ?? 0)}
          />
          <Stat label="Upcoming" value={fmtCurrency(data?.upcoming_total ?? 0)} sub="Beyond 14 days" />
          <Stat
            label="Paid (recent)"
            value={groups.paid.length.toString()}
            tone="pos"
            sub="Verified outflows"
          />
        </div>

        {groups.overdue.length > 0 && (
          <Card className="border-neg">
            <CardHeader title="⚠ Action needed — Overdue" hint="Due date passed, no matching payment detected" />
            <BillTable bills={groups.overdue} emptyLabel="" />
          </Card>
        )}

        <Card>
          <CardHeader title="Due soon" hint="Pending bills due in the next 14 days" />
          <BillTable
            bills={groups.due_soon}
            emptyLabel={ledgerQuery.isLoading ? "Loading bills…" : "Nothing due in the next 14 days."}
          />
        </Card>

        {groups.upcoming.length > 0 && (
          <Card>
            <CardHeader title="Upcoming" hint="Pending bills beyond the 14-day horizon" />
            <BillTable bills={groups.upcoming} emptyLabel="" />
          </Card>
        )}

        <Card>
          <CardHeader title="Recently paid" hint="Proof: the bank transaction that satisfied each bill" />
          <BillTable
            bills={groups.paid}
            showProof
            emptyLabel={ledgerQuery.isLoading ? "Loading…" : "No paid bills yet."}
          />
        </Card>
      </div>
    </>
  );
}
