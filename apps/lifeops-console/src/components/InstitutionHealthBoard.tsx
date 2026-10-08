import type { ConnectionStatus } from "@/providers/mcp-client";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { fmtDate } from "@/lib/format";
import { cn } from "@/lib/cn";

// Per-institution freshness health (Track A, A1 — exposure half). S24-P1
// (predecessor) already computes `data_freshness` server-side, including a
// per-institution learned SLO. This board's only job is to render it
// honestly: one row per institution, never rolled up into a single verdict
// (docs/plans/2026-08-05-feature-tracks.md — "a green board that is wrong is
// worse than a red one").
//
// The verdict below reads ONLY `data_freshness`, never `ConnectionStatus.status`.
// `status` conflates connectivity (reauth/relink/no_token) with data arrival,
// which is precisely what A1 exists to stop doing — reachability is
// Connections' concern, not this board's.
export const INSTITUTION_HEALTH_REFRESH_INTERVAL_MS = 60_000;

export type InstitutionHealthVerdict = "healthy" | "unhealthy" | "unmeasured";

export interface InstitutionHealthPresentation {
  verdict: InstitutionHealthVerdict;
  label: string;
  tone: "pos" | "neg" | "warn";
  newestTransaction: string;
  reason: string;
}

export function institutionHealthVerdict(
  freshness: ConnectionStatus["data_freshness"],
): InstitutionHealthVerdict {
  if (!freshness || !freshness.known) return "unmeasured";
  const { freshness_slo_days, days_since_last_transaction } = freshness;
  if (freshness_slo_days == null || days_since_last_transaction == null) return "unmeasured";
  return days_since_last_transaction > freshness_slo_days ? "unhealthy" : "healthy";
}

export function institutionHealthPresentation(
  row: Pick<ConnectionStatus, "data_freshness">,
): InstitutionHealthPresentation {
  const f = row.data_freshness;
  const verdict = institutionHealthVerdict(f);
  const newestTransaction = f?.last_transaction_date ? fmtDate(f.last_transaction_date) : "—";

  if (verdict === "unhealthy") {
    // freshness_slo_days and days_since_last_transaction are guaranteed
    // non-null here — that is exactly what "unhealthy" means above.
    const days = f!.days_since_last_transaction!;
    const slo = f!.freshness_slo_days!;
    return {
      verdict,
      label: "unhealthy",
      tone: "neg",
      newestTransaction,
      reason: `${days} day${days === 1 ? "" : "s"} since the last transaction — exceeds the ${slo}-day SLO`,
    };
  }

  if (verdict === "healthy") {
    const slo = f!.freshness_slo_days!;
    return {
      verdict,
      label: "healthy",
      tone: "pos",
      newestTransaction,
      reason: `Newest transaction ${newestTransaction}, within the ${slo}-day SLO`,
    };
  }

  if (!f) {
    return {
      verdict: "unmeasured",
      label: "unmeasured",
      tone: "warn",
      newestTransaction: "—",
      reason: "No freshness data returned for this institution",
    };
  }
  if (!f.known) {
    return {
      verdict: "unmeasured",
      label: "unmeasured",
      tone: "warn",
      newestTransaction: "—",
      reason: `No transaction seen in the ${f.window_days}-day lookback window`,
    };
  }
  return {
    verdict: "unmeasured",
    label: "unmeasured",
    tone: "warn",
    newestTransaction,
    reason: "Not enough observed cadence yet to learn a freshness SLO",
  };
}

export function InstitutionHealthBoard({
  institutions,
  isLoading = false,
  isUnavailable = false,
}: {
  institutions?: ConnectionStatus[];
  isLoading?: boolean;
  isUnavailable?: boolean;
}) {
  if (isUnavailable) {
    return (
      <Card className="border-warn">
        <CardHeader
          title="Institution health unavailable"
          hint="The connections API could not be reached"
          right={<Badge tone="warn">retrieval failed</Badge>}
        />
        <CardBody>
          <div className="text-sm text-warn">
            Per-institution freshness could not be retrieved. No health data is shown — this is
            not the same as every institution being healthy.
          </div>
        </CardBody>
      </Card>
    );
  }

  if (isLoading) {
    return (
      <Card>
        <CardHeader title="Institution health" hint="Freshness, per institution" />
        <CardBody>
          <div className="text-sm text-fg-muted">Loading institution health...</div>
        </CardBody>
      </Card>
    );
  }

  const rows = institutions ?? [];

  return (
    <Card>
      <CardHeader title="Institution health" hint={`${rows.length} institution${rows.length === 1 ? "" : "s"}`} />
      <CardBody className="p-0">
        {rows.length === 0 ? (
          <div className="text-center text-fg-muted py-8">No institutions linked yet.</div>
        ) : (
          <>
            {/* Desktop */}
            <div className="hidden md:block overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th>Institution</Th>
                    <Th>Health</Th>
                    <Th>SLO</Th>
                    <Th>Newest transaction</Th>
                    <Th>Reason</Th>
                  </Tr>
                </THead>
                <TBody>
                  {rows.map((row) => {
                    const p = institutionHealthPresentation(row);
                    const slo = row.data_freshness?.freshness_slo_days;
                    return (
                      <Tr key={row.institution}>
                        <Td className="font-medium">{row.institution}</Td>
                        <Td>
                          <Badge tone={p.tone}>{p.label}</Badge>
                        </Td>
                        <Td className="num text-fg-muted">{slo != null ? `${slo}d` : "—"}</Td>
                        <Td className="text-fg-muted">{p.newestTransaction}</Td>
                        <Td
                          className={cn(
                            "text-2xs",
                            p.verdict === "unhealthy" ? "text-neg" : "text-fg-subtle",
                          )}
                        >
                          {p.reason}
                        </Td>
                      </Tr>
                    );
                  })}
                </TBody>
              </Table>
            </div>

            {/* Mobile */}
            <div className="md:hidden divide-y divide-border/60">
              {rows.map((row) => {
                const p = institutionHealthPresentation(row);
                const slo = row.data_freshness?.freshness_slo_days;
                return (
                  <div key={row.institution} className="py-3 px-4 flex flex-col gap-1.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-sm text-fg capitalize">
                        {row.institution}
                      </span>
                      <Badge tone={p.tone}>{p.label}</Badge>
                    </div>
                    <div className="text-2xs text-fg-subtle">
                      SLO {slo != null ? `${slo}d` : "unmeasured"} · newest {p.newestTransaction}
                    </div>
                    <div className={cn("text-2xs", p.verdict === "unhealthy" ? "text-neg" : "text-fg-subtle")}>
                      {p.reason}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
}
