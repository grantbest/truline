import { useQuery } from "@tanstack/react-query";
import { mcpClient } from "@/providers/mcp-client";
import type { Subscription } from "@/providers/mcp-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader, Stat } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { fmtCurrency, fmtDate } from "@/lib/format";
import { cn } from "@/lib/cn";

function StatusCell({ sub }: { sub: Subscription }) {
  if (sub.status === "price_hike") {
    const pc = sub.price_change ?? {};
    return (
      <div className="flex flex-col gap-0.5">
        <Badge tone="warn">↑ price hike</Badge>
        {pc.previous_amount != null && pc.new_amount != null ? (
          <span className="text-2xs text-fg-subtle num">
            {fmtCurrency(pc.previous_amount)} → {fmtCurrency(pc.new_amount)}
            {pc.change_pct != null ? ` (+${pc.change_pct}%)` : ""}
          </span>
        ) : null}
      </div>
    );
  }
  if (sub.status === "stale") {
    return <Badge tone="neg">cancel?</Badge>;
  }
  return <Badge tone="pos">active</Badge>;
}

export function SubscriptionsRoute() {
  const subsQuery = useQuery({
    queryKey: ["finance-subscriptions"],
    queryFn: () => mcpClient.getSubscriptions(true),
  });

  const data = subsQuery.data;
  const subscriptions = data?.subscriptions ?? [];

  return (
    <>
      <PageHeader
        title="Subscriptions"
        subtitle="Recurring commitments — monthly burn, price hikes & cancel candidates"
      />

      <div className="p-6 space-y-4">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
          <Stat
            label="Monthly total"
            value={fmtCurrency(data?.total_monthly ?? 0)}
            sub="Live subscriptions, monthly-equiv"
          />
          <Stat label="Active" value={(data?.active_count ?? 0).toString()} sub="Currently charging" />
          <Stat
            label="Price hikes"
            value={(data?.price_hike_count ?? 0).toString()}
            tone={(data?.price_hike_count ?? 0) > 0 ? "neg" : "default"}
            sub="Caught by the weekly auditor"
          />
          <Stat
            label="Cancel candidates"
            value={(data?.stale_count ?? 0).toString()}
            tone={(data?.stale_count ?? 0) > 0 ? "neg" : "default"}
            sub="No charge in 1.5x cadence"
          />
        </div>

        <Card>
          <CardHeader
            title="Detected Subscriptions"
            hint="From the weekly subscription auditor — sorted by monthly cost"
          />
          {/* Desktop Table View */}
          <div className="hidden md:block overflow-x-auto">
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Cadence</Th>
                  <Th className="text-right">Per charge</Th>
                  <Th className="text-right">Monthly</Th>
                  <Th>Last seen</Th>
                  <Th>Status</Th>
                </Tr>
              </THead>
              <TBody>
                {subsQuery.isLoading ? (
                  <Tr>
                    <Td colSpan={6} className="text-center text-fg-muted py-6">
                      Loading subscriptions…
                    </Td>
                  </Tr>
                ) : subscriptions.length === 0 ? (
                  <Tr>
                    <Td colSpan={6} className="text-center text-fg-muted py-6">
                      No subscriptions detected yet. The auditor runs weekly (Sun 09:00 CT).
                    </Td>
                  </Tr>
                ) : (
                  subscriptions.map((s) => (
                    <Tr key={s.bead_id} className={s.is_stale ? "opacity-60" : ""}>
                      <Td>
                        <div className="font-medium">{s.name ?? s.merchant_key ?? "—"}</div>
                        {s.category ? (
                          <div className="text-2xs text-fg-subtle capitalize">{s.category}</div>
                        ) : null}
                      </Td>
                      <Td className="capitalize">{s.frequency ?? "unknown"}</Td>
                      <Td className="num text-right">{fmtCurrency(s.amount)}</Td>
                      <Td className="num text-right font-medium">
                        {fmtCurrency(s.monthly_equivalent)}
                        <span className="text-2xs text-fg-subtle">/mo</span>
                      </Td>
                      <Td className="num">{fmtDate(s.last_seen)}</Td>
                      <Td>
                        <StatusCell sub={s} />
                      </Td>
                    </Tr>
                  ))
                )}
              </TBody>
            </Table>
          </div>

          {/* Mobile Card List View */}
          <div className="md:hidden divide-y divide-border/60">
            {subsQuery.isLoading ? (
              <div className="text-center text-fg-muted py-8 text-sm">Loading subscriptions…</div>
            ) : subscriptions.length === 0 ? (
              <div className="text-center text-fg-muted py-8 text-sm">No subscriptions detected yet.</div>
            ) : (
              subscriptions.map((s) => (
                <div key={s.bead_id} className={cn("py-3 px-3 flex flex-col gap-2", s.is_stale ? "opacity-60" : "")}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-sm text-fg truncate">{s.name ?? s.merchant_key ?? "—"}</div>
                      {s.category ? (
                        <div className="text-2xs text-fg-subtle capitalize mt-0.5">{s.category}</div>
                      ) : null}
                    </div>
                    <div className="text-right shrink-0">
                      <div className="num text-sm font-semibold text-fg">
                        {fmtCurrency(s.monthly_equivalent)}
                        <span className="text-2xs text-fg-subtle">/mo</span>
                      </div>
                      <div className="text-2xs text-fg-subtle mt-0.5">
                        {fmtCurrency(s.amount)} · <span className="capitalize">{s.frequency}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-2 border-t border-border/30 pt-2 mt-0.5">
                    <div className="text-2xs text-fg-subtle">
                      Last seen: <span className="num">{fmtDate(s.last_seen)}</span>
                    </div>
                    <StatusCell sub={s} />
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
