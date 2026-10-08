import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead, FinanceDiscrepancyContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Badge, stateTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtCurrency, fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";

// The nightly LedgerReconciliationWorkflow emits finance.audit_discrepancy
// beads (state=pending) when an account's institution balance diverges from
// the tracked ledger. This view lets a human investigate and resolve them —
// the resolution is a state PATCH pending -> resolved (the actual missing /
// duplicate transaction is fixed via the bank sync or the ledger).

const RECON_QUERY_KEY = ["finance-audit-discrepancies"];
const SNAPSHOT_QUERY_KEY = ["finance-balance-snapshots"];

// The reconciliation writes a fresh balance_snapshot per account on EVERY run,
// unconditionally, and computes drift against the previous one. Two things
// follow, and the empty state used to admit neither:
//
//   1. An unexplained gap found on Monday is absorbed into Monday's new
//      baseline and is gone by Tuesday. "No pending discrepancies" therefore
//      means "last night's delta reconciled", not "the ledger is correct".
//   2. If the workflow has not run, there are also no discrepancies — the same
//      empty list, for the opposite reason. A green that means "absent".
//
// So the empty state is derived from when reconciliation last ran, and it never
// claims more than a delta check can support. Cumulative integrity is
// LO-REC-002 (Sprint 5); until that exists this page must not imply it.
const RECON_STALE_AFTER_HOURS = 36;

function hoursSince(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 3_600_000;
}

export function LedgerAuditRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  const queryClient = useQueryClient();

  const discrepanciesQuery = useQuery({
    queryKey: RECON_QUERY_KEY,
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "audit_discrepancy",
        limit: 500,
      }),
  });

  const resolve = useMutation({
    mutationFn: (id: string) =>
      substrateClient.updateBead(id, {
        state: "resolved",
        created_by: "lifeops-console/ledger-audit",
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: RECON_QUERY_KEY }),
  });

  const snapshotsQuery = useQuery({
    queryKey: SNAPSHOT_QUERY_KEY,
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "finance",
        type: "balance_snapshot",
        limit: 200,
      }),
  });

  // Newest snapshot = when reconciliation last actually ran.
  const lastReconAt = useMemo(() => {
    let newest: string | null = null;
    for (const b of snapshotsQuery.data ?? []) {
      const asOf =
        ((b.content as Record<string, unknown>)?.as_of as string | undefined) ??
        b.created_at ??
        null;
      if (asOf && (newest === null || asOf > newest)) newest = asOf;
    }
    return newest;
  }, [snapshotsQuery.data]);

  const reconAgeHours = hoursSince(lastReconAt);
  const reconIsStale = reconAgeHours === null || reconAgeHours > RECON_STALE_AFTER_HOURS;

  // Never "all balances reconcile" — this check cannot establish that.
  const emptyStateText = reconIsStale
    ? lastReconAt
      ? `Reconciliation last ran ${fmtDateTime(lastReconAt)} — nothing has been checked since.`
      : "Reconciliation has not run. Nothing has been checked."
    : `No drift in the latest nightly delta check (${fmtDateTime(lastReconAt as string)}).`;

  const beads = discrepanciesQuery.data ?? [];
  const { pending, resolved } = useMemo(() => {
    const p: Bead[] = [];
    const r: Bead[] = [];
    for (const b of beads) {
      if (b.state === "pending") p.push(b);
      else r.push(b);
    }
    // Most-drifted first for the actionable queue; newest first for history.
    p.sort(
      (a, b) =>
        Math.abs(Number((b.content as unknown as FinanceDiscrepancyContent).drift ?? 0)) -
        Math.abs(Number((a.content as unknown as FinanceDiscrepancyContent).drift ?? 0)),
    );
    r.sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
    return { pending: p, resolved: r };
  }, [beads]);

  const renderRows = (rows: Bead[], actionable: boolean) =>
    rows.map((b) => {
      const c = b.content as unknown as FinanceDiscrepancyContent;
      return (
        <Tr key={b.id} className="cursor-pointer" onClick={() => setSelected(b)}>
          <Td className="text-sm">{c.account_name ?? c.account_id}</Td>
          <Td className="num text-right text-fg-muted">{fmtCurrency(c.expected_balance)}</Td>
          <Td className="num text-right">{fmtCurrency(c.actual_balance)}</Td>
          <Td className={`num text-right ${c.drift < 0 ? "text-neg" : "text-pos"}`}>
            {fmtCurrency(c.drift)}
          </Td>
          <Td className="num text-2xs text-fg-subtle">{fmtDateTime(c.window_end)}</Td>
          <Td className="text-right">
            {actionable ? (
              <Button
                variant="outline"
                size="sm"
                disabled={resolve.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  resolve.mutate(b.id);
                }}
              >
                Mark resolved
              </Button>
            ) : (
              <Badge tone={stateTone(b.state)}>{b.state}</Badge>
            )}
          </Td>
        </Tr>
      );
    });

  const renderMobileCards = (rows: Bead[], actionable: boolean) =>
    rows.map((b) => {
      const c = b.content as unknown as FinanceDiscrepancyContent;
      return (
        <div
          key={b.id}
          className="p-3 bg-bg-subtle/30 border border-border/60 rounded flex flex-col gap-2 cursor-pointer hover:bg-bg-hover/40 active:bg-bg-hover transition-colors"
          onClick={() => setSelected(b)}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <span className="text-sm font-semibold text-fg block truncate">{c.account_name ?? c.account_id}</span>
              <span className="text-2xs text-fg-subtle num block mt-0.5">Detected: {fmtDateTime(c.window_end)}</span>
            </div>
            <div className="text-right shrink-0">
              <span className={cn("num text-sm font-bold block", c.drift < 0 ? "text-neg" : "text-pos")}>
                {fmtCurrency(c.drift)}
              </span>
              <span className="text-2xs text-fg-subtle block mt-0.5">drift</span>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2 text-2xs border-t border-border/30 pt-2 mt-0.5">
            <div>
              <span className="panel-title block mb-0.5">Expected balance</span>
              <span className="num text-fg-muted">{fmtCurrency(c.expected_balance)}</span>
            </div>
            <div>
              <span className="panel-title block mb-0.5">Actual balance</span>
              <span className="num text-fg">{fmtCurrency(c.actual_balance)}</span>
            </div>
          </div>

          <div className="flex justify-between items-center mt-1 border-t border-border/30 pt-2">
            <span className="text-2xs text-fg-subtle font-mono">ID: {b.id.slice(0, 8)}…</span>
            {actionable ? (
              <Button
                variant="outline"
                size="sm"
                disabled={resolve.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  resolve.mutate(b.id);
                }}
                className="min-h-[38px] px-3.5"
              >
                Mark resolved
              </Button>
            ) : (
              <Badge tone={stateTone(b.state)}>{b.state}</Badge>
            )}
          </div>
        </div>
      );
    });

  return (
    <>
      <PageHeader
        title="Ledger Audit"
        subtitle="Nightly balance-delta check. Not a cumulative integrity measure — see below."
        right={
          <span className="text-2xs text-fg-subtle num">
            {pending.length} pending · {resolved.length} resolved
          </span>
        }
      />

      <div className="p-6 space-y-4">
        <Card className={pending.length > 0 ? "border-neg" : undefined}>
          <CardHeader
            title="Drift Discrepancies"
            hint={
              reconIsStale
                ? "⚠ Reconciliation is not current — this list may be empty because nothing ran."
                : "Compares one night's balance delta against that night's transactions. It cannot detect a gap that predates the last snapshot."
            }
          />
          {/* Desktop View */}
          <div className="hidden md:block overflow-x-auto">
            <Table>
              <THead>
                <Tr>
                  <Th>Account</Th>
                  <Th className="text-right">Expected</Th>
                  <Th className="text-right">Actual</Th>
                  <Th className="text-right">Drift</Th>
                  <Th>Detected</Th>
                  <Th className="text-right">Action</Th>
                </Tr>
              </THead>
              <TBody>
                {discrepanciesQuery.isLoading ? (
                  <Tr>
                    <Td colSpan={6} className="text-center text-fg-muted py-6">
                      Loading discrepancies…
                    </Td>
                  </Tr>
                ) : discrepanciesQuery.isError ? (
                  <Tr>
                    <Td colSpan={6} className="text-center text-neg py-6">
                      Failed to load discrepancies.
                    </Td>
                  </Tr>
                ) : pending.length === 0 ? (
                  <Tr>
                    <Td colSpan={6} className="text-center text-fg-muted py-6">
                      {emptyStateText}
                    </Td>
                  </Tr>
                ) : (
                  renderRows(pending, true)
                )}
              </TBody>
            </Table>
          </div>

          {/* Mobile View */}
          <div className="md:hidden p-3 space-y-3">
            {discrepanciesQuery.isLoading ? (
              <div className="text-center text-fg-muted py-6 text-sm">Loading discrepancies…</div>
            ) : discrepanciesQuery.isError ? (
              <div className="text-center text-neg py-6 text-sm">Failed to load discrepancies.</div>
            ) : pending.length === 0 ? (
              <div className="text-center text-fg-muted py-6 text-sm">{emptyStateText}</div>
            ) : (
              renderMobileCards(pending, true)
            )}
          </div>
        </Card>

        {resolved.length > 0 ? (
          <Card>
            <CardHeader
              title="Resolved history"
            />
            {/* Desktop View */}
            <div className="hidden md:block overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th>Account</Th>
                    <Th className="text-right">Expected</Th>
                    <Th className="text-right">Actual</Th>
                    <Th className="text-right">Drift</Th>
                    <Th>Detected</Th>
                    <Th className="text-right">State</Th>
                  </Tr>
                </THead>
                <TBody>{renderRows(resolved, false)}</TBody>
              </Table>
            </div>

            {/* Mobile View */}
            <div className="md:hidden p-3 space-y-3">
              {renderMobileCards(resolved, false)}
            </div>
          </Card>
        ) : null}
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}
