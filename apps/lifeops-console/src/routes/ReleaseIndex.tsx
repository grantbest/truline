import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { fmtDateTime } from "@/lib/format";
import {
  openReleaseCharters,
  releaseIndexRows,
  type OutcomeRollup,
  type OutcomeRollupGroup,
  type ReleaseIndexRow,
} from "@/lib/release-index";

// The releases index — the board's answer to "where does a release stand"
// (this route and its nav entry are the whole fix; the detail page and its
// projection already existed at /releases/:ref and ReleaseView.tsx).
//
// Reads only dev.task and arch.release beads and their `delivers` edges —
// this route deliberately never calls factory_status.release_delivery.
// That capability answers not_configured in every production deployment by
// a recorded decision (OPS-110 arm (c); dev.task 09029a3c) — an index built
// on it would render empty and green in every test and dark in prod. See
// release-index.ts's module comment for the full reasoning; do not "fix"
// this index by wiring that route in. (The /releases/:ref detail page this
// index links to still depends on it for its Outcomes/Balance/Criteria
// panels, and those stay dark in production — unchanged by this route.)

const QK = ["release-index"];
const FETCH_LIMIT = 500;
const RELEASE_INDEX_REFRESH_INTERVAL_MS = 30_000;

export function releaseIndexChartersQueryOptions() {
  return {
    queryKey: [...QK, "charters"] as const,
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "release", limit: FETCH_LIMIT }),
    refetchInterval: RELEASE_INDEX_REFRESH_INTERVAL_MS,
  };
}

export function releaseIndexTasksQueryOptions() {
  return {
    queryKey: [...QK, "tasks"] as const,
    queryFn: () => substrateClient.listBeads({ namespace: "dev", type: "task", limit: FETCH_LIMIT }),
    refetchInterval: RELEASE_INDEX_REFRESH_INTERVAL_MS,
  };
}

// Shape shared by the three reads this page gates on — deliberately just
// the fields the gating below reads, so a fixture can be a plain object
// instead of a real useQuery() result. Mirrors FactoryBoard.tsx's
// BoardQueryState<T> / boardInitialLoadFailed pattern.
interface IndexQueryState<T> {
  data: T | undefined;
  isLoading: boolean;
  isError: boolean;
}

/**
 * True once every read this page needs has settled — including linksQuery,
 * which starts disabled and only turns on after charters load, and which an
 * empty `??` fallback can silently stand in for. `linksNeeded` is false when
 * there are no open charters to look delivery up for, in which case
 * linksQuery is never enabled and must not be waited on.
 */
export function releaseIndexHasLoadedData(
  chartersQuery: IndexQueryState<Bead[]>,
  tasksQuery: IndexQueryState<Bead[]>,
  linksQuery: IndexQueryState<unknown>,
  linksNeeded: boolean,
): boolean {
  const linksSettled = !linksNeeded || linksQuery.data !== undefined || linksQuery.isError;
  return chartersQuery.data !== undefined && tasksQuery.data !== undefined && linksSettled;
}

export function releaseIndexIsLoading(
  chartersQuery: IndexQueryState<Bead[]>,
  tasksQuery: IndexQueryState<Bead[]>,
  linksQuery: IndexQueryState<unknown>,
  linksNeeded: boolean,
): boolean {
  const hasLoadedData = releaseIndexHasLoadedData(chartersQuery, tasksQuery, linksQuery, linksNeeded);
  return (
    !hasLoadedData &&
    (chartersQuery.isLoading || tasksQuery.isLoading || (linksNeeded && linksQuery.isLoading))
  );
}

/** A hard failure: even the charter/task reads this page cannot render
 *  anything without did not come back. Does not fire on a linksQuery
 *  failure alone — that renders as `linksUnavailable` instead (a partial
 *  page, not a blank one), since the charters and their objectives are
 *  still known-good. */
export function releaseIndexInitialError(
  chartersQuery: IndexQueryState<Bead[]>,
  tasksQuery: IndexQueryState<Bead[]>,
  linksQuery: IndexQueryState<unknown>,
  linksNeeded: boolean,
): boolean {
  const hasLoadedData = releaseIndexHasLoadedData(chartersQuery, tasksQuery, linksQuery, linksNeeded);
  return !hasLoadedData && (chartersQuery.isError || tasksQuery.isError);
}

/** The defect this exists to close: `releaseIndexRows` cannot tell "no
 *  bound tasks" apart from "could not read the bound tasks" — both come
 *  back `declared_only`. When linksQuery has failed, the caller must render
 *  a distinct state instead of trusting that classification. */
export function releaseIndexLinksUnavailable(
  linksQuery: IndexQueryState<unknown>,
  linksNeeded: boolean,
): boolean {
  return linksNeeded && linksQuery.isError;
}

export function ReleaseIndexRoute() {
  const chartersQuery = useQuery(releaseIndexChartersQueryOptions());
  const tasksQuery = useQuery(releaseIndexTasksQueryOptions());
  const charters = useMemo(() => chartersQuery.data ?? [], [chartersQuery.data]);
  const openCharters = useMemo(() => openReleaseCharters(charters), [charters]);
  const linksNeeded = openCharters.length > 0;

  // Link lookups fan out over the OPEN charters (a handful), never over
  // tasks — mirrors FactoryBoard.tsx's release-charter fan-out, bounded
  // further here since a closed release's delivery no longer needs a read.
  const linksQuery = useQuery({
    queryKey: [...QK, "delivers-links", openCharters.map((c) => c.id).sort().join("|")],
    enabled: linksNeeded,
    refetchInterval: RELEASE_INDEX_REFRESH_INTERVAL_MS,
    queryFn: async () => {
      const groups = await Promise.all(
        openCharters.map((charter) =>
          substrateClient.listBeadLinks(charter.id, { direction: "incoming", link_type: "delivers" }),
        ),
      );
      return openCharters.map((charter, i) => [charter.id, groups[i]] as const);
    },
  });

  // linksQuery can never resolve before openCharters is known (it is only
  // `enabled` once charters have loaded), and an outcome with no bound-task
  // read comes back `declared_only` from releaseIndexRows — indistinguishable
  // from an outcome that genuinely has no bound work. So a stuck or failed
  // linksQuery must never be allowed to render as that claim: the load/error
  // gates below fold linksQuery in, and linksUnavailable drives a distinct
  // render (never silently falls back to "0 complete / 0 in progress").
  const isLoading = releaseIndexIsLoading(chartersQuery, tasksQuery, linksQuery, linksNeeded);
  const isInitialError = releaseIndexInitialError(chartersQuery, tasksQuery, linksQuery, linksNeeded);
  const linksUnavailable = releaseIndexLinksUnavailable(linksQuery, linksNeeded);

  const rows = useMemo(
    () => releaseIndexRows(charters, tasksQuery.data ?? [], new Map(linksQuery.data ?? [])),
    [charters, tasksQuery.data, linksQuery.data],
  );

  // The CLIENT's fetch time, not the age of the data itself — the substrate
  // caches list reads for up to 5 minutes (apps/substrate/src/cache.py), so
  // a fresh-looking "fetched" timestamp here can still describe a snapshot
  // that is up to 5 minutes stale (AC-5). Reported as the oldest of the
  // reads feeding this rollup, since that is the more honest bound; a
  // 30-second refetch interval does not shrink that 5-minute ceiling, and
  // this view does not attempt to work around it.
  const fetchedAtCandidates = [
    chartersQuery.dataUpdatedAt,
    tasksQuery.dataUpdatedAt,
    linksQuery.dataUpdatedAt,
  ].filter((t) => t > 0);
  const fetchedAt = fetchedAtCandidates.length > 0 ? Math.min(...fetchedAtCandidates) : 0;
  const isFetching = chartersQuery.isFetching || tasksQuery.isFetching || linksQuery.isFetching;

  return (
    <>
      <PageHeader
        title="Releases"
        subtitle="Open releases and what their outcomes have delivered, from dev.task work already on the board"
        right={<FetchedNotice fetchedAt={fetchedAt} isFetching={isFetching} />}
      />
      <div className="p-6 space-y-3">
        {isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Loading…</CardBody>
          </Card>
        ) : isInitialError ? (
          <Card>
            <CardBody className="text-center text-neg py-8">Failed to load releases.</CardBody>
          </Card>
        ) : rows.length === 0 ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              <div className="text-sm">No open releases.</div>
              <div className="text-2xs text-fg-subtle mt-2">
                Every arch.release charter is released or abandoned right now.
              </div>
            </CardBody>
          </Card>
        ) : (
          <>
            <LinksUnavailableBanner unavailable={linksUnavailable} />
            {rows.map((row) => (
              <ReleaseIndexCard key={row.charter.id} row={row} linksUnavailable={linksUnavailable} />
            ))}
          </>
        )}
      </div>
    </>
  );
}

// The delivery counts below come from a `delivers`-link read that can fail
// on its own even when charters and tasks both loaded (Promise.all rejects
// if any one charter's listBeadLinks does). Without this, the per-outcome
// badges below fall back to an empty links map and every outcome reads
// "declared only" — a confident zero for a number this page could not
// compute. Named here so that claim can never render un-flagged.
export function LinksUnavailableBanner({ unavailable }: { unavailable: boolean }) {
  if (!unavailable) return null;
  return (
    <Card className="border-warn">
      <CardBody className="text-xs text-warn">
        Could not read which dev.task beads deliver these releases. The counts and groups below are
        unknown, not zero — treat every outcome on this page as unmeasured until the next successful
        refresh.
      </CardBody>
    </Card>
  );
}

// States when this page's own fetch happened, and says in words that this
// is not the same as how fresh the underlying data is — the substrate can
// serve a list read from a cache up to 5 minutes old (AC-5). Distinct on
// purpose from TransactionLedger.tsx's LedgerFreshness, which has the same
// "client fetch time" caveat but no server-side cache behind it worth
// calling out.
function FetchedNotice({ fetchedAt, isFetching }: { fetchedAt: number; isFetching: boolean }) {
  if (!fetchedAt) return null;
  return (
    <div className="text-2xs text-fg-subtle text-right">
      <div className="whitespace-nowrap">
        Fetched by this page {fmtDateTime(new Date(fetchedAt).toISOString())}
        {isFetching ? <span className="text-accent"> · refreshing…</span> : null}
      </div>
      <div className="text-3xs whitespace-nowrap">
        not the data's age — the substrate may serve a read up to 5m older than this
      </div>
    </div>
  );
}

const STATE_TONE: Record<string, "accent" | "warn" | "pos" | "neutral"> = {
  planned: "accent",
  in_flight: "warn",
  closing: "warn",
};

const GROUP_LABEL: Record<OutcomeRollupGroup, string> = {
  complete: "Complete",
  in_progress: "In progress",
  declared_only: "Declared only",
};

const GROUP_TONE: Record<OutcomeRollupGroup, "pos" | "warn" | "neutral"> = {
  complete: "pos",
  in_progress: "warn",
  declared_only: "neutral",
};

export function ReleaseIndexCard({
  row,
  linksUnavailable = false,
}: {
  row: ReleaseIndexRow;
  linksUnavailable?: boolean;
}) {
  const { charter, content, outcomes, counts } = row;
  return (
    <Card>
      <CardHeader
        title={
          <Link
            to={`/releases/${encodeURIComponent(content.ref)}`}
            className="hover:underline underline-offset-2"
          >
            {content.ref} — {content.name}
          </Link>
        }
        hint="arch.release"
        right={
          <>
            <Badge tone={STATE_TONE[charter.state] ?? "neutral"}>{charter.state}</Badge>
            {linksUnavailable ? (
              <Badge tone="warn">delivery unknown</Badge>
            ) : (
              <>
                <Badge tone="pos">{counts.complete} complete</Badge>
                <Badge tone="warn">{counts.in_progress} in progress</Badge>
                <Badge tone="neutral">{counts.declared_only} declared only</Badge>
              </>
            )}
          </>
        }
      />
      <CardBody className="space-y-2">
        <p className="text-xs text-fg-muted">{content.objective}</p>
        <div className="space-y-1.5">
          {outcomes.map((rollup) => (
            <OutcomeRollupRow key={rollup.outcome.id} rollup={rollup} linksUnavailable={linksUnavailable} />
          ))}
        </div>
      </CardBody>
    </Card>
  );
}

export function OutcomeRollupRow({
  rollup,
  linksUnavailable = false,
}: {
  rollup: OutcomeRollup;
  linksUnavailable?: boolean;
}) {
  return (
    <div className="rounded border border-border bg-bg-panel p-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">{rollup.outcome.id}</Badge>
        {linksUnavailable ? (
          <Badge tone="warn">unknown</Badge>
        ) : (
          <Badge tone={GROUP_TONE[rollup.group]}>{GROUP_LABEL[rollup.group]}</Badge>
        )}
        <span className="text-2xs text-fg-subtle num">
          {linksUnavailable ? "bound task count unknown" : `${rollup.taskIds.length} bound task(s)`}
        </span>
      </div>
      {/* A charter outcome has no retirement marker — only its own statement
          says whether the work it names still stands (AC-3). Always shown,
          never truncated by logic, so a rewritten statement (e.g. "RETIRED,
          undelivered and not to be built") stays visible next to the counts
          a bead-state-only rule cannot tell apart from a delivered one. */}
      <p className="mt-1 text-xs text-fg-muted">{rollup.outcome.statement}</p>
    </div>
  );
}
