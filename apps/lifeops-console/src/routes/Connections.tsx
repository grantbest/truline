import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  mcpClient,
  type ConnectionStatus,
  type ConnectionStatusKind,
} from "@/providers/mcp-client";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { fmtDateTime } from "@/lib/format";

// Connections page (Plaid plan PR 12). Replaces the static Connect page:
// the table is fed by /finance/connections (PR 11), "Sync now" by the
// schedule-trigger endpoint (PR 9), and Repair/Re-link reuse the existing
// Link portal — opened via the SAME-ORIGIN /finance/link proxy path, not
// the absolute mcp-hub URL the API returns (that one is for Discord,
// where the browser has no console session).

// "connected" not "healthy": this badge reflects a Plaid /item/get liveness
// probe and nothing about whether data is arriving. Freshness is rendered
// separately — conflating them is what let chase read as healthy for days
// while producing no transactions.
const STATUS_LABEL: Record<ConnectionStatusKind, string> = {
  ok: "connected",
  degraded: "degraded",
  reauth_required: "re-auth needed",
  relink_required: "re-link needed",
  no_token: "no token",
  unprobed: "checking…",
  unknown: "unknown",
  stale: "no data arriving",
  freshness_unknown: "data age unmeasurable",
};

const STATUS_TONE: Record<ConnectionStatusKind, "pos" | "warn" | "neg" | "neutral"> = {
  ok: "pos",
  degraded: "warn",
  reauth_required: "warn",
  relink_required: "neg",
  no_token: "neg",
  unprobed: "neutral",
  unknown: "warn",
  // A dead feed is the defect this whole surface exists to catch, and it is
  // actionable — it means go and look. It is not a neutral observation.
  stale: "neg",
  // Not a pass and not a failure. Neutral is the only honest tone for it.
  freshness_unknown: "neutral",
};

// "Last Sync" is when the JOB ran. This is when DATA arrived. Rendering only
// the first is what let every institution read as healthy on 2026-08-02 while
// amex had produced nothing since 07-27.
//
// This used to state the observation and refuse a verdict, because deciding what
// counts as too long needs a per-institution baseline. That baseline now exists:
// the API learns a freshness SLO from each institution's own gaps between posted
// dates and reports it as `freshness_slo_days`, so `stale` is a real status
// rather than a guess. amex still goes weeks between charges and is still not
// stale — its own cadence says so.
function freshnessLabel(row: ConnectionStatus): string {
  const f = row.data_freshness;
  if (!f) return "data age unknown";
  if (!f.known) return `no data in ${f.window_days}d`;
  const days = f.days_since_last_transaction;
  if (days === null) return "data age unknown";
  if (days === 0) return "data today";
  const slo = f.freshness_slo_days;
  // Show the threshold beside the age, so a verdict a human disagrees with can
  // be argued with rather than just distrusted.
  if (slo !== null && slo !== undefined) return `data ${days}d ago (SLO ${slo}d)`;
  return `data ${days}d ago`;
}

function portalUrl(slug: string, update = false): string {
  return `/api/v1/finance/link/${encodeURIComponent(slug)}${update ? "?mode=update" : ""}`;
}

export function ConnectionsRoute() {
  const [customSlug, setCustomSlug] = useState("");
  const [syncNotes, setSyncNotes] = useState<Record<string, string>>({});
  const queryClient = useQueryClient();

  // Two-phase load: beads-only answer renders instantly; the probed
  // query refines each row with live Plaid health when it lands.
  const fastQuery = useQuery({
    queryKey: ["connections", "fast"],
    queryFn: () => mcpClient.getConnections(false),
  });
  const probedQuery = useQuery({
    queryKey: ["connections", "probed"],
    queryFn: () => mcpClient.getConnections(true),
    refetchInterval: 5 * 60 * 1000,
  });
  const schedulesQuery = useQuery({
    queryKey: ["bank-sync-schedules"],
    queryFn: () => mcpClient.getSchedules(),
  });

  const data = probedQuery.data ?? fastQuery.data;
  const schedulesBySlug = new Map(
    (schedulesQuery.data?.schedules ?? []).map((s) => [s.institution, s]),
  );

  const syncNow = useMutation({
    mutationFn: (slug: string) => mcpClient.syncNow(slug),
    onSuccess: (result) => {
      setSyncNotes((n) => ({
        ...n,
        [result.institution]:
          result.via === "schedule" ? "sync started" : "sync started (direct)",
      }));
      // The run takes minutes; refresh sync timestamps shortly after.
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["connections"] });
        queryClient.invalidateQueries({ queryKey: ["bank-sync-schedules"] });
      }, 30_000);
    },
    onError: (error, slug) => {
      setSyncNotes((n) => ({ ...n, [slug]: (error as Error).message }));
    },
  });

  const rows = data?.institutions ?? [];

  return (
    <>
      <PageHeader
        title="Connections"
        subtitle="Plaid institution health, sync state, and repair"
        right={
          <div className="flex items-center gap-2">
            {data ? (
              <Badge tone={data.plaid_env === "production" ? "neg" : "warn"}>
                {data.plaid_env}
              </Badge>
            ) : null}
            {probedQuery.isFetching ? (
              <span className="text-2xs text-fg-subtle">probing Plaid…</span>
            ) : null}
          </div>
        }
      />

      <div className="p-4 space-y-6">
        <Card>
          <CardHeader
            title="Linked institutions"
            hint={`${rows.length} linked`}
            right={
              schedulesQuery.data?.missing.length ? (
                <Badge tone="warn">
                  {schedulesQuery.data.missing.length} schedule(s) missing
                </Badge>
              ) : null
            }
          />
          <CardBody className="p-0">
            {fastQuery.isLoading && !data ? (
              <div className="text-center text-fg-muted py-8">Loading…</div>
            ) : fastQuery.error && !data ? (
              <div className="text-center text-neg py-8">
                {(fastQuery.error as Error).message}
              </div>
            ) : rows.length === 0 ? (
              <div className="text-center text-fg-muted py-8">
                No institutions linked yet — connect one below.
              </div>
            ) : (
              <>
                {/* Desktop View */}
                <div className="hidden md:block overflow-x-auto">
                  <Table>
                    <THead>
                      <Tr>
                        <Th>Institution</Th>
                        <Th>Status</Th>
                        <Th>Accounts</Th>
                        <Th>Last sync</Th>
                        <Th>Next sync</Th>
                        <Th className="text-right">Actions</Th>
                      </Tr>
                    </THead>
                    <TBody>
                      {rows.map((row) => (
                        <ConnectionRow
                          key={row.institution}
                          row={row}
                          nextRunAt={schedulesBySlug.get(row.institution)?.next_run_at}
                          schedulePaused={schedulesBySlug.get(row.institution)?.paused}
                          scheduleMissing={
                            schedulesQuery.data
                              ? !schedulesBySlug.get(row.institution)?.exists
                              : false
                          }
                          syncing={
                            syncNow.isPending && syncNow.variables === row.institution
                          }
                          syncNote={syncNotes[row.institution]}
                          onSyncNow={() => syncNow.mutate(row.institution)}
                        />
                      ))}
                    </TBody>
                  </Table>
                </div>

                {/* Mobile View */}
                <div className="md:hidden divide-y divide-border/60">
                  {rows.map((row) => {
                    const lastSync = row.last_synced ?? row.accounts_last_synced;
                    const nextRunAt = schedulesBySlug.get(row.institution)?.next_run_at;
                    const schedulePaused = schedulesBySlug.get(row.institution)?.paused;
                    const scheduleMissing = schedulesQuery.data ? !schedulesBySlug.get(row.institution)?.exists : false;
                    const syncing = syncNow.isPending && syncNow.variables === row.institution;
                    const syncNote = syncNotes[row.institution];

                    return (
                      <div key={row.institution} className="py-3 px-4 flex flex-col gap-2.5">
                        <div className="flex items-start justify-between gap-4">
                          <div className="min-w-0 flex-1">
                            <div className="font-semibold text-sm text-fg capitalize">{row.institution}</div>
                            {row.item_id ? (
                              <div className="text-2xs text-fg-subtle font-mono truncate mt-0.5">{row.item_id}</div>
                            ) : null}
                          </div>
                          <div className="text-right shrink-0">
                            <span className="num text-xs text-fg-muted font-medium">
                              {row.account_count} account{row.account_count === 1 ? "" : "s"}
                            </span>
                          </div>
                        </div>

                        <div className="flex flex-wrap gap-1.5 items-center">
                          <Badge tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</Badge>
                          {row.error_code && row.status !== "ok" ? (
                            <span className="text-2xs text-fg-subtle font-mono">{row.error_code}</span>
                          ) : null}
                          {schedulePaused ? <Badge tone="warn">paused</Badge> : null}
                          {scheduleMissing && row.token_present ? (
                            <Badge tone="warn">no schedule</Badge>
                          ) : null}
                        </div>

                        <div className="grid grid-cols-2 gap-2 text-2xs border-t border-border/30 pt-2 mt-0.5">
                          <div>
                            <span className="panel-title block mb-0.5">Last Sync</span>
                            <span className="num text-fg-muted">{lastSync ? fmtDateTime(lastSync) : "never"}</span>
                            <span className="text-2xs text-fg-subtle block mt-0.5">{freshnessLabel(row)}</span>
                          </div>
                          <div>
                            <span className="panel-title block mb-0.5">Next Sync</span>
                            <span className="num text-fg-muted">{nextRunAt ? fmtDateTime(nextRunAt) : "—"}</span>
                          </div>
                        </div>

                        <div className="flex items-center justify-end gap-2 border-t border-border/30 pt-2.5 mt-0.5">
                          {syncNote ? (
                            <span className="text-2xs text-fg-subtle mr-auto">{syncNote}</span>
                          ) : null}
                          {row.status === "reauth_required" ? (
                            <Button
                              size="sm"
                              onClick={() => window.open(portalUrl(row.institution, true), "_blank")}
                            >
                              Repair
                            </Button>
                          ) : null}
                          {row.status === "relink_required" || row.status === "no_token" ? (
                            <Button
                              size="sm"
                              variant="danger"
                              onClick={() => window.open(portalUrl(row.institution), "_blank")}
                            >
                              Re-link
                            </Button>
                          ) : null}
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={syncing || !row.token_present}
                            onClick={() => syncNow.mutate(row.institution)}
                            className="min-h-[38px] px-3"
                          >
                            {syncing ? "Starting…" : "Sync now"}
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Connect a new institution"
            hint="opens the Plaid Link portal in a new tab"
          />
          <CardBody>
            <div className="flex space-x-2 max-w-xl">
              <Input
                placeholder="Institution slug (e.g. fidelity)"
                value={customSlug}
                onChange={(e) => setCustomSlug(e.target.value)}
                className="flex-1"
              />
              <Button
                variant="outline"
                disabled={!customSlug.trim()}
                onClick={() =>
                  window.open(portalUrl(customSlug.trim().toLowerCase()), "_blank")
                }
              >
                Connect
              </Button>
            </div>
            <p className="text-2xs text-fg-subtle mt-3">
              The slug becomes <code className="font-mono">PLAID_ACCESS_TOKEN_[SLUG]</code>.
              Token storage and pod restarts are automatic; the first sync starts
              within a couple of minutes of finishing Plaid Link.
            </p>
          </CardBody>
        </Card>
      </div>
    </>
  );
}

function ConnectionRow({
  row,
  nextRunAt,
  schedulePaused,
  scheduleMissing,
  syncing,
  syncNote,
  onSyncNow,
}: {
  row: ConnectionStatus;
  nextRunAt: string | null | undefined;
  schedulePaused: boolean | undefined;
  scheduleMissing: boolean;
  syncing: boolean;
  syncNote: string | undefined;
  onSyncNow: () => void;
}) {
  const lastSync = row.last_synced ?? row.accounts_last_synced;
  return (
    <Tr>
      <Td>
        <div className="font-medium">{row.institution}</div>
        {row.item_id ? (
          <div className="text-2xs text-fg-subtle font-mono">{row.item_id}</div>
        ) : null}
      </Td>
      <Td>
        <div className="flex flex-col gap-1 items-start">
          <Badge tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</Badge>
          {row.error_code && row.status !== "ok" ? (
            <span className="text-2xs text-fg-subtle font-mono">{row.error_code}</span>
          ) : null}
          {schedulePaused ? <Badge tone="warn">schedule paused</Badge> : null}
          {scheduleMissing && row.token_present ? (
            <Badge tone="warn">no schedule</Badge>
          ) : null}
        </div>
      </Td>
      <Td className="num">{row.account_count}</Td>
      <Td className="text-fg-muted">
        {lastSync ? fmtDateTime(lastSync) : "never"}
        <span className="text-2xs text-fg-subtle block mt-0.5">{freshnessLabel(row)}</span>
      </Td>
      <Td className="text-fg-muted">{nextRunAt ? fmtDateTime(nextRunAt) : "—"}</Td>
      <Td>
        <div className="flex items-center justify-end gap-2">
          {syncNote ? (
            <span className="text-2xs text-fg-subtle">{syncNote}</span>
          ) : null}
          {row.status === "reauth_required" ? (
            <Button
              size="sm"
              onClick={() => window.open(portalUrl(row.institution, true), "_blank")}
            >
              Repair
            </Button>
          ) : null}
          {row.status === "relink_required" || row.status === "no_token" ? (
            <Button
              size="sm"
              variant="danger"
              onClick={() => window.open(portalUrl(row.institution), "_blank")}
            >
              Re-link
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            disabled={syncing || !row.token_present}
            onClick={onSyncNow}
          >
            {syncing ? "Starting…" : "Sync now"}
          </Button>
        </div>
      </Td>
    </Tr>
  );
}
