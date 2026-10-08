import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { factoryStatusClient } from "@/providers/factory-status-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { fmtDate } from "@/lib/format";
import {
  outcomeDeliveryState,
  releaseCharterFrom,
  releaseDeliveryFrom,
  releaseViewAvailability,
  type ReleaseCharter,
  type ReleaseDelivery,
  type ReleaseDeliveryBalanceEntry,
  type ReleaseDeliveryCriterion,
  type ReleaseDeliveryOutcome,
  type ReleaseViewAvailability,
} from "@/lib/release-view";

// The release view — R26.02/O-3, PC-ASR-002. Read-only: the only calls this
// route makes are substrateClient.listBeads (the charter's declared prose)
// and factoryStatusClient.releaseDelivery (the served computation). Neither
// is a write; neither is re-derived — see .factory/design.md.

const RELEASE_VIEW_REFRESH_INTERVAL_MS = 30_000;
const FETCH_LIMIT = 200;

type SubstrateReader = Pick<typeof substrateClient, "listBeads">;
type StatusReader = Pick<typeof factoryStatusClient, "releaseDelivery">;

export function releaseCharterQueryOptions(client: SubstrateReader = substrateClient) {
  return {
    queryKey: ["release-view", "charter"] as const,
    queryFn: () => client.listBeads({ namespace: "arch", type: "release", limit: FETCH_LIMIT }),
    refetchInterval: RELEASE_VIEW_REFRESH_INTERVAL_MS,
  };
}

export function releaseDeliveryQueryOptions(ref: string, client: StatusReader = factoryStatusClient) {
  return {
    queryKey: ["release-view", "delivery", ref] as const,
    queryFn: () => client.releaseDelivery(ref),
    enabled: ref.length > 0,
    refetchInterval: RELEASE_VIEW_REFRESH_INTERVAL_MS,
  };
}

export function ReleaseViewRoute() {
  const { ref: rawRef } = useParams<{ ref: string }>();
  const ref = decodeURIComponent(rawRef ?? "");

  const charterQuery = useQuery(releaseCharterQueryOptions());
  const deliveryQuery = useQuery(releaseDeliveryQueryOptions(ref));

  const charter = releaseCharterFrom({
    ref,
    isLoading: charterQuery.isLoading,
    isError: charterQuery.isError,
    error: charterQuery.error,
    releases: charterQuery.data ?? [],
  });
  const delivery = releaseDeliveryFrom(deliveryQuery.data, deliveryQuery.error);
  const availability = releaseViewAvailability(charter, delivery);

  const title = charter.kind === "ok" ? `${charter.charter.ref} — ${charter.charter.name}` : ref || "Release";

  return (
    <>
      <PageHeader
        title={title}
        subtitle="What this release promised, and which work is delivering it"
        right={
          <Link to="/alpha" className="text-xs text-fg-muted hover:text-fg underline-offset-2 hover:underline">
            ← Back
          </Link>
        }
      />

      <div className="p-6 space-y-4">
        <AvailabilityBanner availability={availability} />
        <ObjectiveSection charter={charter} />
        <OutcomesSection delivery={delivery} />
        <BalanceSection delivery={delivery} />
        <CriteriaSection delivery={delivery} />
      </div>
    </>
  );
}

// What this view was able to establish, stated in one place regardless of
// whether either read succeeded — a partial picture must never render as a
// complete one (PC-ASR-002/AC-2).
export function AvailabilityBanner({ availability }: { availability: ReleaseViewAvailability }) {
  if (availability.complete) {
    return (
      <Card className="border-pos/30">
        <CardBody className="text-xs text-pos">
          Complete: the release charter and the served delivery computation both loaded.
        </CardBody>
      </Card>
    );
  }

  return (
    <Card className="border-warn">
      <CardHeader title="Partial view" right={<Badge tone="warn">incomplete</Badge>} />
      <CardBody className="space-y-1 text-xs">
        {availability.charterProblem ? (
          <div className="text-warn">Charter: {availability.charterProblem}</div>
        ) : (
          <div className="text-fg-muted">Charter: read in full.</div>
        )}
        {availability.deliveryProblem ? (
          <div className={availability.deliveryNotServedHere ? "text-fg-muted" : "text-warn"}>
            Delivery: {availability.deliveryProblem}
          </div>
        ) : (
          <div className="text-fg-muted">Delivery: computed in full.</div>
        )}
      </CardBody>
    </Card>
  );
}

export function ObjectiveSection({ charter }: { charter: ReleaseCharter }) {
  if (charter.kind !== "ok") {
    return (
      <Card>
        <CardHeader title="Objective" hint="arch.release" />
        <CardBody className="text-sm text-fg-muted">
          {charter.kind === "loading"
            ? "Loading…"
            : charter.kind === "not_found"
              ? "No charter found for this ref."
              : `Could not be read: ${charter.reason}`}
        </CardBody>
      </Card>
    );
  }

  const c = charter.charter;
  return (
    <Card>
      <CardHeader
        title="Objective"
        hint="arch.release"
        right={
          <span className="text-2xs text-fg-subtle num">
            opened {fmtDate(c.opened_at)}
            {c.target_at ? ` · target ${fmtDate(c.target_at)}` : ""} · sprint(s) {c.sprints.join(", ")}
          </span>
        }
      />
      <CardBody className="text-sm text-fg whitespace-pre-wrap">{c.objective}</CardBody>
    </Card>
  );
}

const WORK_CLASS_TONE = {
  feature: "accent",
  enabling: "pos",
  blocking: "neg",
  risk: "warn",
  security: "neutral",
} as const;

export function OutcomesSection({ delivery }: { delivery: ReleaseDelivery }) {
  if (delivery.kind !== "ok") {
    return (
      <Card>
        <CardHeader title="Outcomes" hint="release-status.py" />
        <CardBody className="text-sm text-fg-muted">
          {delivery.kind === "not_found"
            ? "release-status.py has no charter for this ref."
            : delivery.kind === "not_served_here"
              ? `Not served in this environment: ${delivery.reason}`
              : `Could not be computed: ${delivery.reason}`}
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Outcomes"
        hint="release-status.py — who delivers what"
        right={<span className="text-2xs text-fg-subtle num">{delivery.outcomes.length} outcome(s)</span>}
      />
      <CardBody className="space-y-3">
        {delivery.outcomes.map((outcome) => (
          <OutcomeRow key={outcome.id} outcome={outcome} />
        ))}
        {delivery.unclassifiedDelivering.length > 0 ? (
          <div className="text-2xs text-warn">
            Delivers this release but names no valid outcome:{" "}
            {delivery.unclassifiedDelivering.join(", ")}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function OutcomeRow({ outcome }: { outcome: ReleaseDeliveryOutcome }) {
  const state = outcomeDeliveryState(outcome);
  return (
    <article className="rounded border border-border bg-bg-panel p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{outcome.id}</Badge>
        <Badge tone={WORK_CLASS_TONE[outcome.work_class]}>{outcome.work_class}</Badge>
        {state.kind === "no_work" ? (
          <Badge tone="warn">no delivering work</Badge>
        ) : (
          <span className="text-2xs text-fg-subtle num">{outcome.task_count} task(s)</span>
        )}
      </div>
      <p className="mt-1 text-xs text-fg-muted">{outcome.statement}</p>
      {state.kind === "delivering" ? (
        <ul className="mt-2 space-y-0.5">
          {state.byState.map(([taskState, taskIds]) => (
            <li key={taskState} className="text-2xs text-fg-subtle num">
              <span className="uppercase tracking-wide">{taskState}</span>: {taskIds.join(", ")}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

export function BalanceSection({ delivery }: { delivery: ReleaseDelivery }) {
  if (delivery.kind !== "ok") return null;

  return (
    <Card>
      <CardHeader title="Declared vs. actual balance" hint="release-status.py" />
      <CardBody className="space-y-1">
        {delivery.balance.map((entry) => (
          <BalanceRow key={entry.work_class} entry={entry} />
        ))}
      </CardBody>
    </Card>
  );
}

function BalanceRow({ entry }: { entry: ReleaseDeliveryBalanceEntry }) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="capitalize text-fg">{entry.work_class}</span>
      <span className="num text-fg-subtle">
        declared {entry.declared_pct}% · actual{" "}
        {entry.absent ? <span className="text-warn font-semibold">ABSENT</span> : `${entry.actual_pct}%`} ·{" "}
        {entry.actual_count} task(s)
      </span>
    </div>
  );
}

export function CriteriaSection({ delivery }: { delivery: ReleaseDelivery }) {
  if (delivery.kind !== "ok" || delivery.criteria.length === 0) return null;

  return (
    <Card>
      <CardHeader title="Cited criteria" hint="verdict as at opened_at vs. latest" />
      <CardBody className="space-y-1.5">
        {delivery.criteria.map((criterion) => (
          <CriterionRow key={criterion.ref} criterion={criterion} />
        ))}
      </CardBody>
    </Card>
  );
}

function CriterionRow({ criterion }: { criterion: ReleaseDeliveryCriterion }) {
  if (criterion.unmeasurable) {
    return (
      <div className="text-2xs">
        <span className="font-mono text-fg">{criterion.ref}</span>{" "}
        <Badge tone="warn">unknown</Badge> <span className="text-fg-muted">{criterion.unmeasurable.reason}</span>
      </div>
    );
  }

  const movement = criterion.stale ? "not re-measured since opened" : criterion.changed ? "changed" : "unchanged";
  return (
    <div className="text-2xs text-fg-subtle">
      <span className="font-mono text-fg">{criterion.ref}</span> — as of opened:{" "}
      {criterion.as_of_opened ?? "no observation recorded"}; latest: {criterion.latest ?? "no observation recorded"}{" "}
      [{movement}]
    </div>
  );
}
