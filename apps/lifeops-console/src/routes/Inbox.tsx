import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { fmtCurrency, fmtAge } from "@/lib/format";
import {
  useAttentionItems,
  SEVERITY_TONE,
  type AttentionItem,
  type Severity,
} from "@/hooks/useAttentionItems";

const INBOX_CREATED_BY = "lifeops-console/inbox";

// Friendly source label per bead type — the rich resolution for each lives on
// its owning route, which "Open →" deep-links to.
const SOURCE_LABEL: Record<string, string> = {
  audit_discrepancy: "Ledger",
  review_anomaly: "Anomaly",
  action_required: "Trial",
  insight_allocation: "Optimizer",
  budget_recommendation: "Budget",
};

const SEVERITY_DOT: Record<Severity, string> = {
  high: "bg-neg",
  medium: "bg-warn",
  opportunity: "bg-accent",
  low: "bg-fg-subtle",
};

const FILTERS: Array<{ key: Severity | "all"; label: string }> = [
  { key: "all", label: "All" },
  { key: "high", label: "High" },
  { key: "medium", label: "Medium" },
  { key: "opportunity", label: "Opportunity" },
  { key: "low", label: "Low" },
];

export function InboxRoute() {
  const attention = useAttentionItems();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Severity | "all">("all");

  // Triage-level dismiss: move the bead out of `pending` so it leaves the feed.
  // Intentionally generic — the type-specific "accept / cancel / pay" actions
  // stay on their owning routes to avoid forking that logic here.
  const dismiss = useMutation({
    mutationFn: (id: string) =>
      substrateClient.updateBead(id, { state: "dismissed", created_by: INBOX_CREATED_BY }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["attention"] }),
  });

  const items =
    filter === "all" ? attention.items : attention.items.filter((i) => i.severity === filter);

  return (
    <>
      <PageHeader
        title="Inbox"
        subtitle="Every pending insight across the system — triage in one place"
        right={
          attention.total > 0 ? (
            <Badge tone="warn">{attention.total} open</Badge>
          ) : (
            <Badge tone="pos">all clear</Badge>
          )
        }
      />

      <div className="space-y-4 p-6">
        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => {
            const count = f.key === "all" ? attention.total : attention.bySeverity[f.key];
            const active = filter === f.key;
            return (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-2xs transition-colors",
                  active
                    ? "border-accent bg-bg-hover text-fg"
                    : "border-border text-fg-muted hover:bg-bg-hover/50",
                )}
              >
                {f.key !== "all" ? (
                  <span className={cn("h-1.5 w-1.5 rounded-full", SEVERITY_DOT[f.key])} aria-hidden />
                ) : null}
                {f.label}
                <span className="num text-fg-subtle">{count}</span>
              </button>
            );
          })}
        </div>

        <Card>
          <CardBody className="space-y-2">
            {attention.isLoading ? (
              <div className="py-10 text-center text-sm text-fg-muted">Scanning for insights…</div>
            ) : items.length === 0 ? (
              <div className="py-10 text-center text-sm text-fg-muted">
                ✓ Nothing here — {filter === "all" ? "you're all caught up." : "no items at this level."}
              </div>
            ) : (
              items.map((item) => (
                <InboxRow
                  key={item.id}
                  item={item}
                  onDismiss={() => dismiss.mutate(item.id)}
                  dismissing={dismiss.isPending && dismiss.variables === item.id}
                />
              ))
            )}
          </CardBody>
        </Card>
      </div>
    </>
  );
}

function InboxRow({
  item,
  onDismiss,
  dismissing,
}: {
  item: AttentionItem;
  onDismiss: () => void;
  dismissing: boolean;
}) {
  return (
    <div className="flex items-start gap-3 rounded border border-border bg-bg-subtle/40 px-3 py-2.5">
      <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", SEVERITY_DOT[item.severity])} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-fg">{item.title}</span>
          <Badge tone={SEVERITY_TONE[item.severity]}>{item.severity}</Badge>
          <Badge tone="neutral">{SOURCE_LABEL[item.beadType] ?? item.beadType}</Badge>
          <span className="text-2xs text-fg-subtle">{fmtAge(item.createdAt)}</span>
        </div>
        <div className="mt-0.5 text-2xs text-fg-muted">{item.detail}</div>
      </div>
      {item.amount != null ? (
        <div className="shrink-0 text-right">
          <div className="num text-sm text-fg">{fmtCurrency(item.amount)}</div>
          {item.amountLabel ? (
            <div className="text-2xs uppercase tracking-wider text-fg-subtle">{item.amountLabel}</div>
          ) : null}
        </div>
      ) : null}
      <div className="flex shrink-0 items-center gap-2">
        <Link
          to={item.route}
          className="rounded border border-border px-2 py-1 text-2xs text-accent transition-colors hover:bg-bg-hover"
        >
          Open →
        </Link>
        <Button size="sm" variant="ghost" disabled={dismissing} onClick={onDismiss}>
          {dismissing ? "Dismissing…" : "Dismiss"}
        </Button>
      </div>
    </div>
  );
}
