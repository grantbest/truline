import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type {
  Bead,
  FinanceAnomalyContent,
  FinanceActionRequiredContent,
} from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtCurrency, fmtDate, fmtDateTime } from "@/lib/format";

// SDD Phase 2 — Proactive Defense inbox. One queue for two producers:
//   - finance.review_anomaly  (duplicate charges, price hikes)
//   - finance.action_required (trial ending, kind="trial_ending")
// Both are emitted by nightly Temporal workflows in state="pending". Actions
// here are pure state PATCHes.

const QK = ["finance-proactive-inbox"];
const CREATED_BY = "lifeops-console/anomaly-inbox";

export function AnomalyInboxRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  const queryClient = useQueryClient();

  const anomaliesQuery = useQuery({
    queryKey: [...QK, "anomalies"],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "review_anomaly",
        state: "pending",
        limit: 200,
      }),
  });

  const actionsQuery = useQuery({
    queryKey: [...QK, "actions"],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "action_required",
        state: "pending",
        limit: 200,
      }),
  });

  const transition = useMutation({
    mutationFn: ({ id, state }: { id: string; state: string }) =>
      substrateClient.updateBead(id, { state, created_by: CREATED_BY }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QK }),
  });

  const anomalies = anomaliesQuery.data ?? [];
  const actions = actionsQuery.data ?? [];

  // Most-recent first across both producers.
  const sortedAnomalies = useMemo(
    () =>
      [...anomalies].sort((a, b) =>
        (b.created_at ?? "").localeCompare(a.created_at ?? ""),
      ),
    [anomalies],
  );
  const sortedActions = useMemo(
    () =>
      [...actions].sort((a, b) => {
        const da = (a.content as unknown as FinanceActionRequiredContent).end_date ?? "";
        const db = (b.content as unknown as FinanceActionRequiredContent).end_date ?? "";
        return da.localeCompare(db); // soonest deadline first
      }),
    [actions],
  );

  const total = anomalies.length + actions.length;
  const isLoading = anomaliesQuery.isLoading || actionsQuery.isLoading;
  const isError = anomaliesQuery.isError || actionsQuery.isError;

  return (
    <>
      <PageHeader
        title="Anomaly Inbox"
        subtitle="Proactive defense — duplicate charges, price hikes & expiring trials"
        right={<span className="text-2xs text-fg-subtle num">{total} pending</span>}
      />

      <div className="p-6 space-y-4">
        {isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Loading…</CardBody>
          </Card>
        ) : isError ? (
          <Card>
            <CardBody className="text-center text-neg py-8">
              Failed to load the inbox.
            </CardBody>
          </Card>
        ) : total === 0 ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              <div className="text-sm">✓ Nothing needs review.</div>
              <div className="text-2xs text-fg-subtle mt-2 max-w-prose mx-auto">
                The anomaly detector (nightly 04:00 CT) flags duplicate charges and
                price hikes; the trial sentinel (daily 08:00 CT) warns 3 days before a
                free trial converts. Detections show up here for review.
              </div>
            </CardBody>
          </Card>
        ) : (
          <>
            {sortedActions.map((bead) => (
              <TrialCard
                key={bead.id}
                bead={bead}
                disabled={transition.isPending}
                onInspect={() => setSelected(bead)}
                onKeep={() => transition.mutate({ id: bead.id, state: "resolved.kept" })}
              />
            ))}
            {sortedAnomalies.map((bead) => (
              <AnomalyCard
                key={bead.id}
                bead={bead}
                disabled={transition.isPending}
                onInspect={() => setSelected(bead)}
                onAcknowledge={() =>
                  transition.mutate({ id: bead.id, state: "resolved.safe" })
                }
                onDispute={() =>
                  transition.mutate({ id: bead.id, state: "action_required.dispute" })
                }
              />
            ))}
          </>
        )}
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

function AnomalyCard({
  bead,
  disabled,
  onInspect,
  onAcknowledge,
  onDispute,
}: {
  bead: Bead;
  disabled: boolean;
  onInspect: () => void;
  onAcknowledge: () => void;
  onDispute: () => void;
}) {
  const c = bead.content as unknown as FinanceAnomalyContent;
  const isDup = c.reason === "duplicate_charge";
  const title = isDup
    ? `Duplicate charge — ${c.vendor ?? "unknown vendor"}`
    : `Price hike — ${c.vendor ?? "unknown vendor"}`;
  return (
    <Card>
      <CardHeader
        title={title}
        hint={`${bead.created_by} · ${fmtDateTime(bead.created_at)}`}
        right={
          <div className="hidden md:flex items-center gap-2">
            <Badge tone={isDup ? "neg" : "warn"}>{c.reason.replace("_", " ")}</Badge>
            <Button size="sm" variant="ghost" onClick={onInspect}>
              Inspect
            </Button>
            <Button size="sm" variant="outline" disabled={disabled} onClick={onDispute}>
              Flag for dispute
            </Button>
            <Button size="sm" disabled={disabled} onClick={onAcknowledge}>
              Acknowledge (safe)
            </Button>
          </div>
        }
      />
      <CardBody className="text-sm space-y-3">
        {/* Mobile-only badge row */}
        <div className="md:hidden flex items-center mb-1">
          <Badge tone={isDup ? "neg" : "warn"}>{c.reason.replace("_", " ")}</Badge>
        </div>

        {isDup ? (
          <div>
            <span className="num font-medium text-neg">{fmtCurrency(c.amount)}</span> charged{" "}
            <span className="num">{c.count ?? c.transaction_ids.length}×</span> on{" "}
            <span className="num">{fmtDate(c.posted_date)}</span>
          </div>
        ) : (
          <div>
            <span className="num font-medium">{fmtCurrency(c.amount)}</span> vs baseline{" "}
            <span className="num text-fg-muted">{fmtCurrency(c.baseline_amount)}</span>
            {c.change_pct != null ? (
              <span className="text-neg"> (+{c.change_pct}%)</span>
            ) : null}
            {c.sample_size != null ? (
              <span className="text-2xs text-fg-subtle"> · {c.sample_size} prior charges</span>
            ) : null}
          </div>
        )}
        <div className="text-2xs text-fg-subtle num">
          {c.transaction_ids.length} transaction{c.transaction_ids.length === 1 ? "" : "s"}
        </div>

        {/* Mobile action button layout */}
        <div className="md:hidden grid grid-cols-3 gap-2 border-t border-border/30 pt-2.5 mt-2">
          <Button size="sm" variant="ghost" onClick={onInspect} className="w-full text-center min-h-[38px]">
            Inspect
          </Button>
          <Button size="sm" variant="outline" disabled={disabled} onClick={onDispute} className="w-full text-center min-h-[38px] text-[11px] px-1 truncate">
            Dispute
          </Button>
          <Button size="sm" disabled={disabled} onClick={onAcknowledge} className="w-full text-center min-h-[38px] text-[11px] px-1 truncate">
            Acknowledge
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

function TrialCard({
  bead,
  disabled,
  onInspect,
  onKeep,
}: {
  bead: Bead;
  disabled: boolean;
  onInspect: () => void;
  onKeep: () => void;
}) {
  const c = bead.content as unknown as FinanceActionRequiredContent;
  return (
    <Card className="border-warn">
      <CardHeader
        title={`Free trial ending — ${c.vendor ?? "unknown"}`}
        hint={`${bead.created_by} · ${fmtDateTime(bead.created_at)}`}
        right={
          <div className="hidden md:flex items-center gap-2">
            <Badge tone="warn">trial ending</Badge>
            <Button size="sm" variant="ghost" onClick={onInspect}>
              Inspect
            </Button>
            <Button size="sm" variant="outline" disabled={disabled} onClick={onKeep}>
              Keep it
            </Button>
          </div>
        }
      />
      <CardBody className="text-sm space-y-3">
        {/* Mobile-only badge row */}
        <div className="md:hidden flex items-center mb-1">
          <Badge tone="warn">trial ending</Badge>
        </div>

        <div>
          Converts to{" "}
          <span className="num font-medium text-neg">{fmtCurrency(c.amount_at_risk)}</span>
          {c.frequency ? <span className="text-fg-muted">/{c.frequency}</span> : null} on{" "}
          <span className="num">{fmtDate(c.end_date)}</span>
          {c.days_until != null ? (
            <span className="text-2xs text-fg-subtle"> · in {c.days_until} days</span>
          ) : null}
        </div>

        {/* Mobile action button layout */}
        <div className="md:hidden grid grid-cols-2 gap-2 border-t border-border/30 pt-2.5 mt-2">
          <Button size="sm" variant="ghost" onClick={onInspect} className="w-full text-center min-h-[38px]">
            Inspect
          </Button>
          <Button size="sm" variant="outline" disabled={disabled} onClick={onKeep} className="w-full text-center min-h-[38px] text-[11px] px-1 truncate">
            Keep
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
