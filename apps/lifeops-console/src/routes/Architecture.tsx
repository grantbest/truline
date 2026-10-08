import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { factoryStatusClient } from "@/providers/factory-status-client";
import type { Bead, BeadLink } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardHeader, CardBody } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import {
  HEALTH_COLUMNS,
  HEALTH_LABEL,
  VALUE_ROWS,
  applicationAssessments,
  applicationDependencies,
  appContent,
  byMaturity,
  capabilityCoverage,
  capContent,
  countDebt,
  findingsStatus,
  gaps,
  gitopsOrphans,
  groupByLayer,
  isStale,
  observationContent,
  observationFindings,
  observationObservedAt,
  roadmapBacklog,
  sourceClassOf,
  timeGrid,
  undoneTeardowns,
  type ApplicationAssessment,
  type ApplicationContent,
  type ApplicationDependency,
  type CapabilityContent,
  type CapabilityCoverage,
  type Disposition,
  type FindingsStatus,
  type Maturity,
  type ObservationFinding,
  type RoadmapBacklogItem,
  type RoadmapKind,
  type SourceClassification,
} from "@/lib/ea-model";
import {
  blastRadius,
  ciContent,
  ciLabel,
  graphCoverage,
  serviceContent,
  type BlastRadiusEntities,
  type GraphCoverage as NodeCoverage,
  type ServiceContent,
} from "@/lib/blast-radius";
import {
  coverageRatioLabel,
  impactPanelStateFrom,
  impactViewFrom,
  type ImpactKind,
  type ImpactRef,
  type ImpactResponse,
} from "@/lib/impact";

// The EA model, rendered from the substrate rather than from the YAML.
//
// docs/architecture/model/*.yaml is authoritative for structure; scripts/
// ea-load.py reconciles it into the arch namespace; this reads what landed.
// So the page cannot drift from the model the way a hand-written status doc
// does — if it disagrees with git, the loader has not run, and that is a
// visible fact rather than a silent one.
//
// Edges live in bead_link. This route loads links for the EA beads it renders,
// bounded by the model size, then keeps graph interpretation in ea-model.ts.

const QK = ["architecture"];
const FETCH_LIMIT = 500;

const MATURITY_TONE: Record<Maturity, string> = {
  absent: "bg-neg",
  emerging: "bg-warn",
  operating: "bg-pos",
  optimised: "bg-accent",
};

const DISPOSITION_TONE: Record<Disposition, string> = {
  invest: "bg-pos",
  tolerate: "bg-border",
  migrate: "bg-warn",
  eliminate: "bg-neg",
};

const SOURCE_CLASS_TONE: Record<SourceClassification, "neutral" | "accent" | "pos"> = {
  authored: "neutral",
  derived: "accent",
  observed: "pos",
  unclassified: "neutral",
};

export function ArchitectureRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  // Fixed at mount rather than recomputed per render, so staleness reads
  // consistently within one page view instead of flipping mid-scroll.
  const [now] = useState(() => new Date());

  const capQuery = useQuery({
    queryKey: [...QK, "capabilities"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "capability", limit: FETCH_LIMIT }),
  });
  const appQuery = useQuery({
    queryKey: [...QK, "applications"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "application", limit: FETCH_LIMIT }),
  });
  const observationQuery = useQuery({
    queryKey: [...QK, "observations"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "observation", limit: FETCH_LIMIT }),
  });
  // Neither type was fetched here before this task — the blast-radius reader
  // needs both rendered so a CI or service can be a traversal origin, not
  // just an application.
  const serviceQuery = useQuery({
    queryKey: [...QK, "services"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "service", limit: FETCH_LIMIT }),
  });
  const ciQuery = useQuery({
    queryKey: [...QK, "cis"],
    queryFn: () => substrateClient.listBeads({ namespace: "arch", type: "ci", limit: FETCH_LIMIT }),
  });

  const capabilities = useMemo(() => capQuery.data ?? [], [capQuery.data]);
  const applications = useMemo(() => appQuery.data ?? [], [appQuery.data]);
  const observations = useMemo(() => observationQuery.data ?? [], [observationQuery.data]);
  const services = useMemo(() => serviceQuery.data ?? [], [serviceQuery.data]);
  const cis = useMemo(() => ciQuery.data ?? [], [ciQuery.data]);
  const linkBeads = useMemo(
    () => [...capabilities, ...applications, ...observations, ...services, ...cis],
    [applications, capabilities, observations, services, cis],
  );
  const linksQuery = useQuery({
    queryKey: [...QK, "links", linkBeads.map((bead) => bead.id).sort().join("|")],
    enabled: linkBeads.length > 0,
    queryFn: async () => {
      const groups = await Promise.all(
        linkBeads.map((bead) => substrateClient.listBeadLinks(bead.id)),
      );
      return dedupeLinks(groups.flat());
    },
  });
  const links = useMemo(() => linksQuery.data ?? [], [linksQuery.data]);

  const demand = useMemo(() => groupByLayer(capabilities, "demand"), [capabilities]);
  const supply = useMemo(() => groupByLayer(capabilities, "supply"), [capabilities]);
  const gapList = useMemo(() => gaps(capabilities), [capabilities]);
  const orphans = useMemo(() => gitopsOrphans(applications), [applications]);
  const teardowns = useMemo(() => undoneTeardowns(applications), [applications]);
  const findings = useMemo(() => observationFindings(applications), [applications]);
  const findingsState = useMemo(
    () => findingsStatus(findings, observations),
    [findings, observations],
  );
  const grid = useMemo(() => timeGrid(applications), [applications]);
  const debt = useMemo(() => countDebt(applications), [applications]);
  const assessments = useMemo(
    () => applicationAssessments(applications, observations, links),
    [applications, links, observations],
  );
  const assessmentByApp = useMemo(
    () => new Map(assessments.map((assessment) => [assessment.application.id, assessment])),
    [assessments],
  );
  const coverage = useMemo(
    () => capabilityCoverage(capabilities, applications, links),
    [applications, capabilities, links],
  );
  const coverageByCapability = useMemo(
    () => new Map(coverage.map((row) => [row.capability.id, row])),
    [coverage],
  );
  const dependencies = useMemo(() => applicationDependencies(applications, links), [applications, links]);
  const roadmap = useMemo(() => roadmapBacklog(capabilities, applications), [applications, capabilities]);
  const measuredCount = assessments.filter((assessment) => assessment.source === "measured").length;
  const classifiableRecords = useMemo(
    () => [...capabilities.map(capContent), ...applications.map(appContent)],
    [applications, capabilities],
  );
  const unclassifiedCount = useMemo(
    () => classifiableRecords.filter((content) => sourceClassOf(content) === "unclassified").length,
    [classifiableRecords],
  );
  const staleCount = useMemo(
    () =>
      classifiableRecords.filter((content) => isStale(sourceClassOf(content), content.assessed_at, now))
        .length,
    [classifiableRecords, now],
  );
  const isLinkLoading = linkBeads.length > 0 && linksQuery.isLoading;

  const isLoading =
    capQuery.isLoading ||
    appQuery.isLoading ||
    observationQuery.isLoading ||
    serviceQuery.isLoading ||
    ciQuery.isLoading ||
    isLinkLoading;
  const isError =
    capQuery.isError ||
    appQuery.isError ||
    observationQuery.isError ||
    serviceQuery.isError ||
    ciQuery.isError ||
    linksQuery.isError;
  const empty = !isLoading && !isError && capabilities.length === 0;

  // Blast-radius traversal (PC-ASR-002/S35): CI/service/application nodes and
  // the graph coverage the traversal is built on, declared once here and
  // reused by whichever node the reader selects.
  const blastEntities = useMemo<BlastRadiusEntities>(
    () => ({ applications, services, cis, capabilities }),
    [applications, services, cis, capabilities],
  );
  const nodeCoverage = useMemo(() => graphCoverage(blastEntities, links), [blastEntities, links]);

  return (
    <>
      <PageHeader
        title="Architecture"
        subtitle="the CSDM model, read live from the arch namespace"
        right={
          <div className="flex items-center gap-2">
            {orphans.length > 0 ? <Badge tone="neg">{orphans.length} GitOps orphan</Badge> : null}
            {gapList.length > 0 ? <Badge tone="warn">{gapList.length} absent</Badge> : null}
            <Badge
              tone={nodeCoverage.linkedNodes === 0 && nodeCoverage.totalNodes > 0 ? "warn" : "neutral"}
              title="CI/service/application nodes carrying at least one depends_on, consumes or realizes edge, out of all such nodes loaded — the coverage this page's blast-radius traversal is built on (PC-ASR-002)"
            >
              graph coverage {nodeCoverage.linkedNodes}/{nodeCoverage.totalNodes}
            </Badge>
            <span className="text-2xs text-fg-subtle num">
              {capabilities.length} capabilities · {applications.length} applications ·{" "}
              {services.length} services · {cis.length} CIs
            </span>
          </div>
        }
      />

      <div className="p-6 space-y-4">
        {isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Loading…</CardBody>
          </Card>
        ) : isError ? (
          <Card>
            <CardBody className="text-center text-neg py-8">
              Failed to load the architecture model.
            </CardBody>
          </Card>
        ) : empty ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              <div className="text-sm">No arch beads in this substrate.</div>
              <div className="text-2xs text-fg-subtle mt-2 max-w-prose mx-auto">
                The model lives in{" "}
                <span className="font-mono">docs/architecture/model/*.yaml</span> and is
                reconciled by <span className="font-mono">scripts/ea-load.py</span>. An empty
                page means the loader has not run against the substrate this console reads —
                not that the model is empty.
              </div>
            </CardBody>
          </Card>
        ) : (
          <>
            <StatStrip
              capabilities={capabilities}
              applications={applications}
              measured={measuredCount}
              asserted={applications.length - measuredCount}
              gaps={gapList.length}
              orphans={orphans.length}
              teardowns={teardowns.length}
              debt={debt}
              roadmap={roadmap.length}
              unclassified={unclassifiedCount}
              stale={staleCount}
            />

            <FindingsList findings={findings} status={findingsState} onSelect={setSelected} />

            <Card>
              <CardHeader
                title="Capability Map"
                hint="capability tree plus loaded graph coverage"
                right={<EdgeCounts links={links} />}
              />
              <CardBody className="space-y-4">
                <LayerBlock
                  label="Demand — the household"
                  groups={demand}
                  coverage={coverageByCapability}
                  now={now}
                  onSelect={setSelected}
                />
                <div className="flex items-center gap-3">
                  <div className="h-px flex-1 bg-border" />
                  <span className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
                    supply supports demand
                  </span>
                  <div className="h-px flex-1 bg-border" />
                </div>
                <LayerBlock
                  label="Supply — the platform"
                  groups={supply}
                  coverage={coverageByCapability}
                  now={now}
                  onSelect={setSelected}
                />
              </CardBody>
            </Card>

            <MeasurementCoverage
              assessments={assessments}
              observations={observations}
              now={now}
              onSelect={setSelected}
            />

            <GraphCoverage coverage={coverage} now={now} onSelect={setSelected} />

            <DependencyView dependencies={dependencies} onSelect={setSelected} />

            <TechnologyLayer
              services={services}
              cis={cis}
              coverage={nodeCoverage}
              onSelect={setSelected}
            />

            <RoadmapPressure items={roadmap} onSelect={setSelected} />

            <Card>
              <CardHeader title="Portfolio — TIME" hint="business value × technical health" />
              <CardBody>
                <div className="overflow-x-auto">
                  <div className="grid min-w-[640px] gap-px bg-border" style={{ gridTemplateColumns: "auto repeat(3, minmax(0, 1fr))" }}>
                    <div className="bg-bg-panel" />
                    {HEALTH_COLUMNS.map((h) => (
                      <div key={h} className="bg-bg-panel px-2 py-1.5 text-2xs font-mono uppercase tracking-wider text-fg-subtle">
                        health · {HEALTH_LABEL[h]}
                      </div>
                    ))}
                    {VALUE_ROWS.map((v) => (
                      <Row key={v} value={v} grid={grid} now={now} onSelect={setSelected} />
                    ))}
                  </div>
                </div>
              </CardBody>
            </Card>

            {gapList.length > 0 && (
              <Card>
                <CardHeader title="Gap register" hint="capabilities scored absent" />
                <CardBody className="grid gap-2 sm:grid-cols-2">
                  {gapList.map((c) => (
                    <button
                      key={c.id}
                      onClick={() => setSelected(c)}
                      className="text-left rounded border border-border bg-bg-subtle/40 px-3 py-2.5 hover:bg-bg-subtle"
                    >
                      <div className="text-2xs font-mono text-fg-subtle">
                        {capContent(c).ref} · {c.state}
                      </div>
                      <div className="text-xs font-medium mt-0.5">{capContent(c).name}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        <SourceMeta content={capContent(c)} now={now} />
                      </div>
                      <div className="text-2xs text-fg-muted mt-1">{capContent(c).description}</div>
                    </button>
                  ))}
                </CardBody>
              </Card>
            )}
          </>
        )}
      </div>

      {selected ? (
        <DetailPanel
          bead={selected}
          assessment={assessmentByApp.get(selected.id)}
          blastEntities={blastEntities}
          links={links}
          now={now}
          onClose={() => setSelected(null)}
          onSelect={setSelected}
        />
      ) : null}
    </>
  );
}

function StatStrip(props: {
  capabilities: Bead[];
  applications: Bead[];
  measured: number;
  asserted: number;
  gaps: number;
  orphans: number;
  teardowns: number;
  debt: number;
  roadmap: number;
  unclassified: number;
  stale: number;
}) {
  const stats: [string, number, "neutral" | "warn" | "neg"][] = [
    ["capabilities", props.capabilities.length, "neutral"],
    ["applications", props.applications.length, "neutral"],
    ["measured apps", props.measured, "neutral"],
    ["asserted apps", props.asserted, props.asserted > 0 ? "warn" : "neutral"],
    ["roadmap", props.roadmap, props.roadmap > 0 ? "warn" : "neutral"],
    ["absent", props.gaps, "neg"],
    ["gitops orphans", props.orphans, "neg"],
    ["teardowns due", props.teardowns, "warn"],
    ["debt items", props.debt, "warn"],
    ["unclassified source", props.unclassified, props.unclassified > 0 ? "warn" : "neutral"],
    ["stale confirmations", props.stale, props.stale > 0 ? "neg" : "neutral"],
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-11 gap-px bg-border border border-border">
      {stats.map(([label, n, tone]) => (
        <div key={label} className="bg-bg-panel px-3 py-2.5">
          <div
            className={cn(
              "text-xl font-semibold num",
              n > 0 && tone === "neg" && "text-neg",
              n > 0 && tone === "warn" && "text-warn",
            )}
          >
            {n}
          </div>
          <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">{label}</div>
        </div>
      ))}
    </div>
  );
}

function LayerBlock({
  label,
  groups,
  coverage,
  now,
  onSelect,
}: {
  label: string;
  groups: { root: Bead; children: Bead[] }[];
  coverage: Map<string, CapabilityCoverage>;
  now: Date;
  onSelect: (b: Bead) => void;
}) {
  return (
    <div>
      <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle mb-2">{label}</div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {groups.map(({ root, children }) => (
          <div key={root.id} className="rounded border border-border bg-bg-subtle/30 p-2">
            <button onClick={() => onSelect(root)} className="text-left w-full">
              <div className="text-xs font-medium leading-tight">{capContent(root).name}</div>
              <div className="text-2xs font-mono text-fg-subtle">{capContent(root).ref}</div>
              <div className="text-2xs text-fg-subtle mt-1">
                {coverage.get(root.id)?.realizedBy.length ?? 0} apps ·{" "}
                {coverage.get(root.id)?.supportedBy.length ?? 0} supports in
              </div>
              <div className="mt-1 flex flex-wrap gap-1">
                <SourceMeta content={capContent(root)} now={now} />
              </div>
            </button>
            <div className="flex gap-px h-0.5 my-1.5">
              {[...children].sort(byMaturity).map((c) => (
                <div key={c.id} className={cn("flex-1", MATURITY_TONE[capContent(c).maturity])} />
              ))}
            </div>
            <div className="flex flex-col gap-1">
              {[...children].sort(byMaturity).map((c) => {
                const sourceClass = sourceClassOf(capContent(c));
                const stale = isStale(sourceClass, capContent(c).assessed_at, now);
                return (
                  <button
                    key={c.id}
                    onClick={() => onSelect(c)}
                    className={cn(
                      "flex items-baseline justify-between gap-2 rounded-sm border-l-2 bg-bg-panel px-1.5 py-1 text-left hover:bg-bg-subtle",
                      capContent(c).maturity === "absent" && "border-l-neg",
                      capContent(c).maturity === "emerging" && "border-l-warn",
                      capContent(c).maturity === "operating" && "border-l-pos",
                      capContent(c).maturity === "optimised" && "border-l-accent",
                      c.state === "proposed" && "border border-dashed border-border",
                    )}
                  >
                    <span className="text-2xs leading-tight">{capContent(c).name}</span>
                    <span
                      className={cn(
                        "text-2xs font-mono text-fg-subtle shrink-0",
                        stale && "text-neg",
                      )}
                      title={`source: ${sourceClass}${stale ? " · stale" : ""}`}
                    >
                      {capContent(c).maturity.slice(0, 3)} · {sourceClass.slice(0, 4)}
                      {stale ? "!" : ""} ·{" "}
                      {coverage.get(c.id)?.realizedBy.length ?? 0} app
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function MeasurementCoverage({
  assessments,
  observations,
  now,
  onSelect,
}: {
  assessments: ApplicationAssessment[];
  observations: Bead[];
  now: Date;
  onSelect: (b: Bead) => void;
}) {
  const measured = assessments.filter((assessment) => assessment.source === "measured").length;
  return (
    <Card>
      <CardHeader
        title="Measurement Coverage"
        hint="portfolio assertions versus dated observations"
        right={
          <div className="flex flex-wrap gap-1.5">
            <Badge tone="pos">{measured} measured</Badge>
            <Badge tone="warn">{assessments.length - measured} asserted</Badge>
            <Badge>{observations.length} observations</Badge>
          </div>
        }
      />
      <CardBody className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {assessments.map((assessment) => {
          const latest = assessment.observations[0];
          return (
            <button
              key={assessment.application.id}
              type="button"
              onClick={() => onSelect(assessment.application)}
              className="text-left rounded border border-border bg-bg-subtle/40 px-2.5 py-2 hover:bg-bg-subtle"
            >
              <div className="flex items-center justify-between gap-2">
                <Badge tone={assessment.source === "measured" ? "pos" : "warn"}>
                  {assessment.source}
                </Badge>
                <span className="text-2xs font-mono text-fg-subtle">
                  {appContent(assessment.application).time_disposition}
                </span>
              </div>
              <div className="text-xs font-medium mt-1 leading-tight">
                {appContent(assessment.application).name}
              </div>
              <div className="mt-1 flex flex-wrap gap-1">
                <SourceMeta content={appContent(assessment.application)} now={now} />
              </div>
              <div className="text-2xs text-fg-muted mt-1">
                {latest ? latestObservationLabel(latest) : "reviewed model assertion"}
              </div>
            </button>
          );
        })}
      </CardBody>
    </Card>
  );
}

function GraphCoverage({
  coverage,
  now,
  onSelect,
}: {
  coverage: CapabilityCoverage[];
  now: Date;
  onSelect: (b: Bead) => void;
}) {
  return (
    <Card>
      <CardHeader title="Capability Coverage" hint="realizes and supports edges" />
      <CardBody className="grid gap-2 lg:grid-cols-2">
        {coverage.map((row) => (
          <button
            key={row.capability.id}
            type="button"
            onClick={() => onSelect(row.capability)}
            className="text-left rounded border border-border bg-bg-subtle/40 px-2.5 py-2 hover:bg-bg-subtle"
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={capContent(row.capability).maturity === "absent" ? "neg" : "neutral"}>
                {capContent(row.capability).maturity}
              </Badge>
              <span className="text-xs font-medium">{capContent(row.capability).name}</span>
              <span className="text-2xs font-mono text-fg-subtle">{capContent(row.capability).ref}</span>
              <SourceMeta content={capContent(row.capability)} now={now} />
            </div>
            <div className="mt-1 grid gap-1 sm:grid-cols-3 text-2xs text-fg-muted">
              <span>{row.realizedBy.length} realizing app{row.realizedBy.length === 1 ? "" : "s"}</span>
              <span>{row.supportedBy.length} supports in</span>
              <span>{row.supports.length} supports out</span>
            </div>
            <div className="mt-1 text-2xs text-fg-subtle truncate">
              {row.realizedBy.map((application) => appContent(application).name).join(", ") || "No app edge"}
            </div>
          </button>
        ))}
      </CardBody>
    </Card>
  );
}

function DependencyView({
  dependencies,
  onSelect,
}: {
  dependencies: ApplicationDependency[];
  onSelect: (b: Bead) => void;
}) {
  return (
    <Card>
      <CardHeader title="Dependency View" hint="application depends_on edges" />
      <CardBody className="space-y-2">
        {dependencies.length === 0 ? (
          <div className="text-xs text-fg-muted">No application dependency edges loaded.</div>
        ) : (
          dependencies.map((row) => (
            <button
              key={row.application.id}
              type="button"
              onClick={() => onSelect(row.application)}
              className="w-full text-left rounded border border-border bg-bg-subtle/40 px-3 py-2 hover:bg-bg-subtle"
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-xs font-medium">{appContent(row.application).name}</span>
                <span className="text-2xs font-mono text-fg-subtle">{appContent(row.application).ref}</span>
              </div>
              <div className="mt-1 grid gap-1 md:grid-cols-2 text-2xs text-fg-muted">
                <span>depends on: {applicationList(row.dependsOn)}</span>
                <span>used by: {applicationList(row.dependedOnBy)}</span>
              </div>
            </button>
          ))
        )}
      </CardBody>
    </Card>
  );
}

function RoadmapPressure({
  items,
  onSelect,
}: {
  items: RoadmapBacklogItem[];
  onSelect: (b: Bead) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Roadmap Pressure"
        hint="dated backlog from absent capabilities and migrate/eliminate apps"
        right={<Badge tone={items.length > 0 ? "warn" : "neutral"}>{items.length} items</Badge>}
      />
      <CardBody className="space-y-2">
        {items.length === 0 ? (
          <div className="text-xs text-fg-muted">No roadmap pressure in the current model.</div>
        ) : (
          items.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelect(item.bead)}
              className="w-full text-left rounded border border-border bg-bg-subtle/40 px-3 py-2 hover:bg-bg-subtle"
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <Badge tone={roadmapTone(item.kind)}>{roadmapLabel(item.kind)}</Badge>
                <span className="text-xs font-medium">{item.label}</span>
                <span className="text-2xs font-mono text-fg-subtle">{item.ref}</span>
                <span className="ml-auto text-2xs font-mono text-fg-subtle">
                  target {item.targetDate}
                </span>
              </div>
              <div className="mt-1 text-2xs text-fg-muted">{item.action}</div>
              <div className="mt-1 text-2xs font-mono text-fg-subtle">
                opened {item.openedAt}
              </div>
            </button>
          ))
        )}
      </CardBody>
    </Card>
  );
}

function Row({
  value,
  grid,
  now,
  onSelect,
}: {
  value: string;
  grid: Map<string, Bead[]>;
  now: Date;
  onSelect: (b: Bead) => void;
}) {
  return (
    <>
      <div className="bg-bg-panel px-2 py-2 text-2xs font-mono uppercase tracking-wider text-fg-subtle whitespace-nowrap">
        value · {value}
      </div>
      {HEALTH_COLUMNS.map((h) => {
        const cell = grid.get(`${value}:${h}`) ?? [];
        const hot = value === "low" && h === "at_risk";
        return (
          <div
            key={h}
            className={cn("bg-bg-panel p-1.5 space-y-1 min-h-[3.5rem]", hot && "bg-neg/5")}
          >
            {cell.length === 0 ? (
              <span className="text-2xs text-fg-subtle">—</span>
            ) : (
              cell.map((a) => {
                const sourceClass = sourceClassOf(appContent(a));
                const stale = isStale(sourceClass, appContent(a).assessed_at, now);
                return (
                  <button
                    key={a.id}
                    onClick={() => onSelect(a)}
                    className="w-full flex items-baseline justify-between gap-2 rounded-sm border-l-2 bg-bg-subtle/50 px-1.5 py-1 text-left hover:bg-bg-subtle"
                    style={{ borderLeftColor: "currentColor" }}
                  >
                    <span
                      className={cn(
                        "w-1 h-3 rounded-sm shrink-0",
                        DISPOSITION_TONE[appContent(a).time_disposition],
                      )}
                    />
                    <span className="text-2xs leading-tight flex-1">{appContent(a).name}</span>
                    <span
                      className={cn("text-2xs font-mono text-fg-subtle shrink-0", stale && "text-neg")}
                      title={`source: ${sourceClass}${stale ? " · stale" : ""}`}
                    >
                      {appContent(a).time_disposition.slice(0, 3)} · {sourceClass.slice(0, 4)}
                      {stale ? "!" : ""}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        );
      })}
    </>
  );
}

function DetailPanel({
  bead,
  assessment,
  blastEntities,
  links,
  now,
  onClose,
  onSelect,
}: {
  bead: Bead;
  assessment?: ApplicationAssessment;
  blastEntities: BlastRadiusEntities;
  links: BeadLink[];
  now: Date;
  onClose: () => void;
  onSelect: (b: Bead) => void;
}) {
  const isApp = bead.type === "application";
  const isService = bead.type === "service";
  const isCi = bead.type === "ci";
  const showBlastRadius = isApp || isService || isCi;

  // CIs carry a wholly different, observation-only shape (no description,
  // evidence, note or roadmap) — a dedicated branch rather than forcing them
  // through the authored-content layout below.
  if (isCi) {
    const cc = ciContent(bead);
    return (
      <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-label="Object detail">
        <div className="absolute inset-0 bg-black/40" onClick={onClose} />
        <div className="relative w-full max-w-md h-full overflow-y-auto border-l border-border bg-bg-panel">
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <div className="text-2xs font-mono text-fg-subtle">{cc.ref}</div>
              <h2 className="text-sm font-semibold mt-0.5">{ciLabel(bead)}</h2>
            </div>
            <button onClick={onClose} className="text-2xs border border-border rounded px-2 py-1 hover:text-fg">
              Close
            </button>
          </div>
          <div className="p-4 space-y-4 text-xs">
            <div className="flex flex-wrap gap-1.5">
              <Badge>{bead.state}</Badge>
              <Badge>{cc.ci_kind}</Badge>
              <Badge tone="pos">observed</Badge>
            </div>
            <Section title="Technology layer">
              <ul className="space-y-0.5 font-mono text-2xs text-fg-muted">
                <li>cluster: {cc.cluster}</li>
                <li>namespace: {cc.namespace}</li>
                <li>kind: {cc.kind}</li>
                <li>name: {cc.name}</li>
              </ul>
            </Section>
            <BlastRadiusPanel origin={bead} entities={blastEntities} links={links} onSelect={onSelect} />
          </div>
        </div>
      </div>
    );
  }

  const c = isApp ? appContent(bead) : isService ? serviceContent(bead) : capContent(bead);
  const a = isApp ? appContent(bead) : null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-label="Object detail">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-md h-full overflow-y-auto border-l border-border bg-bg-panel">
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <div className="text-2xs font-mono text-fg-subtle">{c.ref}</div>
            <h2 className="text-sm font-semibold mt-0.5">{c.name}</h2>
          </div>
          <button onClick={onClose} className="text-2xs border border-border rounded px-2 py-1 hover:text-fg">
            Close
          </button>
        </div>
        <div className="p-4 space-y-4 text-xs">
          <div className="flex flex-wrap gap-1.5">
            <Badge>{bead.state}</Badge>
            {a ? (
              <>
                <Badge tone={a.time_disposition === "eliminate" ? "neg" : a.time_disposition === "invest" ? "pos" : "neutral"}>
                  {a.time_disposition}
                </Badge>
                <Badge tone={a.technical_health === "healthy" ? "pos" : a.technical_health === "at_risk" ? "neg" : "warn"}>
                  {HEALTH_LABEL[a.technical_health]}
                </Badge>
                <Badge>value {a.business_value}</Badge>
                <Badge tone={assessment?.source === "measured" ? "pos" : "warn"}>
                  {assessment?.source ?? "asserted"}
                </Badge>
              </>
            ) : isService ? (
              <Badge>service</Badge>
            ) : (
              <Badge tone={capContent(bead).maturity === "absent" ? "neg" : "neutral"}>
                {capContent(bead).maturity}
              </Badge>
            )}
            <SourceMeta content={c} now={now} />
          </div>

          <Section title="Description">
            <p className="text-fg-muted">{c.description}</p>
          </Section>

          {a?.workload ? (
            <Section title="Workload">
              {a.workload.objects?.length ? (
                <ul className="space-y-1">
                  {a.workload.objects.map((o) => (
                    <li key={`${o.namespace}/${o.name}`} className="font-mono text-2xs text-fg-muted">
                      {o.namespace}/{o.kind}/{o.name}
                      <span className={cn("ml-1", o.managed_by === "none" && "text-neg")}>
                        [{o.managed_by}]
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-fg-muted">
                  <span className="font-mono text-2xs">runtime: {a.workload.runtime}</span>
                  {a.workload.note ? ` — ${a.workload.note}` : null}
                </p>
              )}
            </Section>
          ) : null}

          {a ? (
            <Section title="Measurement">
              {assessment?.observations.length ? (
                <ul className="space-y-1 font-mono text-2xs text-fg-muted">
                  {assessment.observations.map((observation) => (
                    <li key={observation.id}>{latestObservationLabel(observation)}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-fg-muted">asserted by reviewed model fields</p>
              )}
            </Section>
          ) : null}

          {c.note ? (
            <Section title="Assessment">
              <p className="text-fg-muted whitespace-pre-wrap">{c.note}</p>
            </Section>
          ) : null}

          {c.roadmap ? (
            <Section title="Roadmap">
              <div className="rounded border border-border bg-bg-subtle/40 px-2.5 py-2">
                <div className="flex flex-wrap gap-1.5">
                  <Badge tone="warn">opened {c.roadmap.opened_at}</Badge>
                  <Badge>target {c.roadmap.target_date}</Badge>
                </div>
                <p className="text-fg-muted mt-2">{c.roadmap.action}</p>
              </div>
            </Section>
          ) : null}

          {a?.debt?.length ? (
            <Section title={`Known debt (${a.debt.length})`}>
              <ul className="list-disc pl-4 space-y-1 text-fg-muted">
                {a.debt.map((d) => <li key={d}>{d}</li>)}
              </ul>
            </Section>
          ) : null}

          <Section title="Evidence">
            <ul className="space-y-0.5 font-mono text-2xs text-fg-muted break-words">
              {c.evidence.map((e) => <li key={e}>{e}</li>)}
            </ul>
          </Section>

          <Section title="Assessed">
            <p className="text-fg-muted num">{c.assessed_at}</p>
          </Section>

          {showBlastRadius ? (
            <BlastRadiusPanel origin={bead} entities={blastEntities} links={links} onSelect={onSelect} />
          ) : null}
        </div>
      </div>
    </div>
  );
}

function BlastRadiusPanel({
  origin,
  entities,
  links,
  onSelect,
}: {
  origin: Bead;
  entities: BlastRadiusEntities;
  links: BeadLink[];
  onSelect: (b: Bead) => void;
}) {
  // The served answer (PC-ASR-002/AC-1) covers application/ci origins;
  // "service" has no served kind (lib/impact.ts's module comment explains
  // why), so that origin keeps the client-side walk. The useMemo hook itself
  // is still called unconditionally so hook order never depends on
  // origin.type, but blastRadius(...) is only ever invoked when servedKind
  // is null -- an application/ci origin must never re-derive the answer
  // client-side (substrate-contract.test.ts's own rule for this panel).
  const servedKind: ImpactKind | null =
    origin.type === "application" || origin.type === "ci" ? (origin.type as ImpactKind) : null;
  const originRef = (origin.content as { ref?: string } | undefined)?.ref ?? null;

  const localResult = useMemo(
    () => (servedKind === null ? blastRadius(origin, entities, links) : null),
    [origin, entities, links, servedKind],
  );
  const impactQuery = useQuery({
    queryKey: ["architecture", "impact", servedKind, originRef],
    queryFn: () => factoryStatusClient.impact(servedKind as ImpactKind, originRef as string),
    enabled: servedKind !== null && Boolean(originRef),
  });

  if (servedKind === null) {
    const result = localResult!;
    const dependentCount = result.applications.length + result.services.length;

    return (
      <Section title="Blast radius — what depends on this, transitively">
        <div className="flex flex-wrap items-center gap-1.5 mb-2">
          <Badge tone={result.status === "no_linkage_data" ? "neg" : "neutral"}>
            coverage {result.coverage.linkedNodes}/{result.coverage.totalNodes} nodes linked
          </Badge>
          {result.truncated ? (
            <Badge tone="warn">traversal truncated at the node cap — answer may be incomplete</Badge>
          ) : null}
        </div>

        {result.status === "no_linkage_data" ? (
          <p className="text-fg-muted">
            No depends_on, consumes or realizes edges are loaded for this graph. That is not
            evidence the blast radius is empty — it means it has not been measured yet.
          </p>
        ) : dependentCount === 0 ? (
          <p className="text-fg-muted">Nothing in the loaded graph transitively depends on this.</p>
        ) : (
          <div className="space-y-2.5">
            {result.services.length > 0 ? (
              <div>
                <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
                  services ({result.services.length})
                </div>
                <ul className="mt-1 space-y-0.5">
                  {result.services.map((service) => (
                    <li key={service.id}>
                      <button
                        type="button"
                        onClick={() => onSelect(service)}
                        className="text-left text-xs hover:underline"
                      >
                        {serviceContent(service).name}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {result.applications.length > 0 ? (
              <div>
                <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
                  applications ({result.applications.length})
                </div>
                <ul className="mt-1 space-y-0.5">
                  {result.applications.map((application) => (
                    <li key={application.id}>
                      <button
                        type="button"
                        onClick={() => onSelect(application)}
                        className="text-left text-xs hover:underline"
                      >
                        {appContent(application).name}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}

        <div className="mt-2.5">
          <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
            capabilities realized ({result.capabilities.length})
          </div>
          {result.capabilities.length === 0 ? (
            <p className="text-2xs text-fg-muted mt-1">None realized by the affected set.</p>
          ) : (
            <ul className="mt-1 space-y-0.5">
              {result.capabilities.map((capability) => (
                <li key={capability.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(capability)}
                    className="text-left text-xs hover:underline"
                  >
                    {capContent(capability).name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Section>
    );
  }

  // application/ci origin: the served answer (tools.impact.get_impact)
  // replaces the client-side walk for this panel — POST /api/v1/factory/impact
  // via factoryStatusClient.impact, never re-derived here.
  const view = impactViewFrom(impactQuery.data, impactQuery.error);

  return (
    <Section title="Impact — what this affects, over the graph's typed edges">
      {view.kind === "unknown" ? (
        <p className="text-fg-muted">Served impact answer unavailable: {view.reason}</p>
      ) : (
        <ImpactSummary
          response={view.response}
          originKind={servedKind as "application" | "ci"}
          entities={entities}
          onSelect={onSelect}
        />
      )}
    </Section>
  );
}

/** Purely presentational: renders one already-served /factory/impact answer.
 * Split out from BlastRadiusPanel so it can be unit-tested with
 * renderToStaticMarkup against synthetic ImpactResponse fixtures, without a
 * QueryClient (Architecture.test.tsx). */
export function ImpactSummary({
  response,
  originKind,
  entities,
  onSelect,
}: {
  response: ImpactResponse;
  originKind: "application" | "ci";
  entities: BlastRadiusEntities;
  onSelect: (b: Bead) => void;
}) {
  const { affected, coverage, context } = response;
  const resolve = (ref: ImpactRef, pool: Bead[]) => pool.find((bead) => bead.id === ref.id);
  const hasRunsOnContext = context.runs_on.cis.length > 0 || context.runs_on.services.length > 0;

  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        <Badge tone={coverage.failed_types.includes("application") ? "neg" : coverage.partial ? "warn" : "neutral"}>
          apps {coverageRatioLabel(coverage.applications_assessed, coverage.applications_total, "application", coverage.failed_types)} assessed
        </Badge>
        <Badge tone={coverage.failed_types.includes("ci") ? "neg" : "neutral"}>
          CIs {coverageRatioLabel(coverage.cis_owned, coverage.cis_total, "ci", coverage.failed_types)} owned
        </Badge>
        <Badge tone={coverage.failed_types.includes("change") ? "neg" : "neutral"}>
          changes {coverageRatioLabel(coverage.changes_with_affects, coverage.changes_total, "change", coverage.failed_types)} with affects
        </Badge>
        {coverage.partial ? (
          <Badge tone="warn" title={coverage.partial_reads.join("; ")}>
            partial read — some collections could not be fetched
          </Badge>
        ) : null}
        {affected.lower_bound ? (
          <Badge tone="warn">
            lower bound — unassessed applications may extend this further
          </Badge>
        ) : null}
      </div>

      {(() => {
        const panelState = impactPanelStateFrom(response, originKind);
        if (panelState.kind === "populated") {
          return (
            <div className="space-y-2.5">
              {affected.applications.length > 0 ? (
                <ImpactRefList
                  label="applications"
                  refs={affected.applications}
                  pool={entities.applications}
                  resolve={resolve}
                  onSelect={onSelect}
                />
              ) : null}
              {affected.services.length > 0 ? (
                <ImpactRefList
                  label="services"
                  refs={affected.services}
                  pool={entities.services}
                  resolve={resolve}
                  onSelect={onSelect}
                />
              ) : null}
              {affected.cis.length > 0 ? (
                <ImpactRefList
                  label="CIs"
                  refs={affected.cis}
                  pool={entities.cis}
                  resolve={resolve}
                  onSelect={onSelect}
                />
              ) : null}
            </div>
          );
        }

        if (panelState.kind === "empty_partial") {
          return (
            <p className="text-fg-muted">
              Coverage is incomplete for this read ({panelState.reasons.join("; ")}) — this
              may under-report what is affected, not a confirmed empty result.
            </p>
          );
        }

        if (panelState.kind === "empty_unowned_ci") {
          return (
            <p className="text-fg-muted">
              This CI has no recorded owning application in the graph — that is unknown
              impact, never verified as unaffected.
            </p>
          );
        }

        return <p className="text-fg-muted">Nothing in the graph is affected by this.</p>;
      })()}

      {hasRunsOnContext ? (
        <div className="mt-2.5">
          <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
            runs on (context, not affected)
          </div>
          <p className="text-2xs text-fg-muted mt-1">
            What the affected applications themselves depend on — shown for context, never
            counted as affected: a change to an application does not affect what that
            application itself depends on.
          </p>
          <div className="space-y-2.5 mt-1">
            {context.runs_on.services.length > 0 ? (
              <ImpactRefList
                label="services"
                refs={context.runs_on.services}
                pool={entities.services}
                resolve={resolve}
                onSelect={onSelect}
              />
            ) : null}
            {context.runs_on.cis.length > 0 ? (
              <ImpactRefList
                label="CIs"
                refs={context.runs_on.cis}
                pool={entities.cis}
                resolve={resolve}
                onSelect={onSelect}
              />
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="mt-2.5">
        <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
          unknown depends_on posture ({affected.unknown_applications.length})
        </div>
        {affected.unknown_applications.length === 0 ? (
          <p className="text-2xs text-fg-muted mt-1">
            None — every affected application's dependency posture is assessed.
          </p>
        ) : (
          <>
            <p className="text-2xs text-fg-muted mt-1">
              Only their own downstream depends_on posture is unassessed, so impact reached
              THROUGH them may extend further than shown (R26.09/O-4: unknown, never counted as
              unaffected). Their own CI/service edges, if any, appear under "runs on (context,
              not affected)" above, never as affected.
            </p>
            <ImpactRefList
              label="unknown"
              refs={affected.unknown_applications}
              pool={entities.applications}
              resolve={resolve}
              onSelect={onSelect}
              hideLabel
            />
          </>
        )}
      </div>
    </>
  );
}

function ImpactRefList({
  label,
  refs,
  pool,
  resolve,
  onSelect,
  hideLabel,
}: {
  label: string;
  refs: ImpactRef[];
  pool: Bead[];
  resolve: (ref: ImpactRef, pool: Bead[]) => Bead | undefined;
  onSelect: (b: Bead) => void;
  hideLabel?: boolean;
}) {
  return (
    <div>
      {hideLabel ? null : (
        <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">
          {label} ({refs.length})
        </div>
      )}
      <ul className="mt-1 space-y-0.5">
        {refs.map((ref) => {
          const bead = resolve(ref, pool);
          const text = ref.name ?? ref.ref ?? ref.id;
          return (
            <li key={ref.id}>
              {bead ? (
                <button
                  type="button"
                  onClick={() => onSelect(bead)}
                  className="text-left text-xs hover:underline"
                >
                  {text}
                </button>
              ) : (
                <span className="text-xs text-fg-muted">{text}</span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function TechnologyLayer({
  services,
  cis,
  coverage,
  onSelect,
}: {
  services: Bead[];
  cis: Bead[];
  coverage: NodeCoverage;
  onSelect: (b: Bead) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Technology Layer"
        hint="application services and observed CIs — the depends_on/consumes sources the blast-radius traversal reads"
        right={
          <Badge tone={coverage.totalNodes > 0 && coverage.linkedNodes === 0 ? "warn" : "neutral"}>
            {coverage.linkedNodes}/{coverage.totalNodes} nodes linked
          </Badge>
        }
      />
      <CardBody className="space-y-3">
        <div>
          <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle mb-1">
            services ({services.length})
          </div>
          {services.length === 0 ? (
            <div className="text-xs text-fg-muted">No arch.service beads loaded.</div>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {services.map((service) => (
                <button
                  key={service.id}
                  type="button"
                  onClick={() => onSelect(service)}
                  className="text-left rounded border border-border bg-bg-subtle/40 px-2.5 py-2 hover:bg-bg-subtle"
                >
                  <div className="text-xs font-medium leading-tight">{serviceContent(service).name}</div>
                  <div className="text-2xs font-mono text-fg-subtle">{serviceContent(service).ref}</div>
                </button>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle mb-1">
            CIs ({cis.length})
          </div>
          {cis.length === 0 ? (
            <div className="text-xs text-fg-muted">No arch.ci beads loaded.</div>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {cis.map((ciBead) => (
                <button
                  key={ciBead.id}
                  type="button"
                  onClick={() => onSelect(ciBead)}
                  className="text-left rounded border border-border bg-bg-subtle/40 px-2.5 py-2 hover:bg-bg-subtle"
                >
                  <div className="text-xs font-medium leading-tight truncate">{ciLabel(ciBead)}</div>
                  <div className="text-2xs font-mono text-fg-subtle">{ciContent(ciBead).ci_kind}</div>
                </button>
              ))}
            </div>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

function EdgeCounts({ links }: { links: BeadLink[] }) {
  const count = (type: string) => links.filter((link) => link.link_type === type).length;
  return (
    <div className="flex flex-wrap gap-1.5">
      <Badge>{count("realizes")} realizes</Badge>
      <Badge>{count("supports")} supports</Badge>
      <Badge>{count("depends_on")} depends_on</Badge>
    </div>
  );
}

function dedupeLinks(links: BeadLink[]): BeadLink[] {
  return [...new Map(links.map((link) => [link.id, link])).values()];
}

function latestObservationLabel(observation: Bead): string {
  const content = observationContent(observation);
  const ready =
    typeof content.ready_replicas === "number" && typeof content.replicas === "number"
      ? ` · ${content.ready_replicas}/${content.replicas} ready`
      : "";
  return `${observationObservedAt(observation)} · ${content.workload.namespace}/${content.workload.kind}/${content.workload.name}${ready}`;
}

function applicationList(applications: Bead[]): string {
  return applications.map((application) => appContent(application).name).join(", ") || "none";
}

function roadmapLabel(kind: RoadmapKind): string {
  if (kind === "capability_gap") return "gap";
  if (kind === "application_migration") return "migrate";
  return "eliminate";
}

function roadmapTone(kind: RoadmapKind): "neg" | "warn" {
  return kind === "application_elimination" ? "neg" : "warn";
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-2xs font-mono uppercase tracking-wider text-fg-subtle">{title}</div>
      {children}
    </div>
  );
}

// Source class chip plus, for derived/observed records, when the writer last
// confirmed it. Authored and unclassified records get only the chip — an
// unclassified record must never carry a "confirmed" claim it has no cadence
// to back, and an authored one has no mechanical cadence to be stale against.
function SourceMeta({
  content,
  now,
}: {
  content: CapabilityContent | ApplicationContent | ServiceContent;
  now: Date;
}) {
  const sourceClass = sourceClassOf(content);
  const stale = isStale(sourceClass, content.assessed_at, now);
  return (
    <>
      <Badge
        tone={SOURCE_CLASS_TONE[sourceClass]}
        className={sourceClass === "unclassified" ? "border-dashed" : undefined}
      >
        {sourceClass}
      </Badge>
      {sourceClass === "derived" || sourceClass === "observed" ? (
        <Badge tone={stale ? "neg" : "neutral"}>
          {stale ? "stale since " : "confirmed "}
          {content.assessed_at}
        </Badge>
      ) : null}
    </>
  );
}

function FindingsList({
  findings,
  status,
  onSelect,
}: {
  findings: ObservationFinding[];
  status: FindingsStatus;
  onSelect: (b: Bead) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Findings"
        hint="standing observation findings — the model disagreeing with the estate"
        right={<Badge tone={findings.length > 0 ? "neg" : "neutral"}>{findings.length} standing</Badge>}
      />
      <CardBody className="space-y-2">
        {status === "not_measuring" ? (
          <div className="text-xs text-fg-muted">
            No observations loaded yet — the estate has not been measured, so this is not
            evidence of a clean estate.
          </div>
        ) : status === "measured_clean" ? (
          <div className="text-xs text-fg-muted">Measured — no standing findings.</div>
        ) : (
          findings.map((finding) => (
            <button
              key={finding.id}
              onClick={() => onSelect(finding.application)}
              className={cn(
                "w-full text-left flex flex-wrap items-baseline gap-2 rounded border px-2.5 py-2",
                finding.kind === "orphan"
                  ? "border-neg/30 bg-neg/5 hover:bg-neg/10"
                  : "border-warn/30 bg-warn/5 hover:bg-warn/10",
              )}
            >
              <Badge tone={finding.kind === "orphan" ? "neg" : "warn"}>{finding.kind}</Badge>
              <span className="text-xs font-medium">{appContent(finding.application).name}</span>
              <span className="text-2xs text-fg-muted">{finding.detail}</span>
              <span className="ml-auto text-2xs font-mono text-fg-subtle">{finding.foundAt}</span>
            </button>
          ))
        )}
      </CardBody>
    </Card>
  );
}
