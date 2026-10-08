import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { ReleaseCoverageCard } from "@/components/ReleaseCoverageCard";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead } from "@/types/bead";
import { cn } from "@/lib/cn";
import {
  ALPHA_FETCH_LIMIT_PER_TYPE,
  ALPHA_INITIAL_BEAD_BUDGET,
  EDGE_VIEWS,
  OPERATING_MODEL_PERSONAS,
  PRODUCT_PERSONAS,
  beadKind,
  beadLookup,
  buildAlphaModel,
  displayName,
  linksForEdge,
  type AlphaEdgeType,
} from "@/lib/project-alpha";

const QK = ["project-alpha"];
const types = {
  tasks: { namespace: "dev", type: "task" },
  designs: { namespace: "dev", type: "design" },
  releases: { namespace: "dev", type: "release" },
  releaseCharters: { namespace: "arch", type: "release" },
  findings: { namespace: "dev", type: "finding" },
  applications: { namespace: "arch", type: "application" },
  changes: { namespace: "arch", type: "change" },
  observations: { namespace: "arch", type: "observation" },
  capabilities: { namespace: "arch", type: "capability" },
} as const;

export function ProjectAlphaRoute() {
  const [activeEdge, setActiveEdge] = useState<AlphaEdgeType>("designs");

  const taskQuery = useAlphaBeads(types.tasks);
  const designQuery = useAlphaBeads(types.designs);
  const releaseQuery = useAlphaBeads(types.releases);
  // arch.release — the charter release-load.py mirrors from docs/releases/*.json.
  // Kept out of buildAlphaModel/the graph traversal on purpose: it answers a
  // different question ("what releases exist and what did they charter") than
  // the task/design/finding graph does, and is rendered by its own honest
  // loading/unavailable/empty/data states rather than folded into the
  // whole-graph fixture fallback.
  const releaseCharterQuery = useAlphaBeads(types.releaseCharters);
  const findingQuery = useAlphaBeads(types.findings);
  const appQuery = useAlphaBeads(types.applications);
  const changeQuery = useAlphaBeads(types.changes);
  const observationQuery = useAlphaBeads(types.observations);
  const capabilityQuery = useAlphaBeads(types.capabilities);

  const baseModel = useMemo(
    () =>
      buildAlphaModel({
        tasks: taskQuery.data ?? [],
        designs: designQuery.data ?? [],
        releases: releaseQuery.data ?? [],
        findings: findingQuery.data ?? [],
        applications: appQuery.data ?? [],
        changes: changeQuery.data ?? [],
        observations: observationQuery.data ?? [],
        capabilities: capabilityQuery.data ?? [],
        links: [],
      }),
    [
      appQuery.data,
      capabilityQuery.data,
      changeQuery.data,
      designQuery.data,
      findingQuery.data,
      observationQuery.data,
      releaseQuery.data,
      taskQuery.data,
    ],
  );

  const taskLinksQuery = useQuery({
    queryKey: [...QK, "links", baseModel.task.id],
    queryFn: () => substrateClient.listBeadLinks(baseModel.task.id),
    enabled: baseModel.source === "live",
  });
  const appLinksQuery = useQuery({
    queryKey: [...QK, "links", baseModel.application.id],
    queryFn: () => substrateClient.listBeadLinks(baseModel.application.id),
    enabled: baseModel.source === "live",
  });

  const model = useMemo(
    () =>
      buildAlphaModel({
        tasks: taskQuery.data ?? [],
        designs: designQuery.data ?? [],
        releases: releaseQuery.data ?? [],
        findings: findingQuery.data ?? [],
        applications: appQuery.data ?? [],
        changes: changeQuery.data ?? [],
        observations: observationQuery.data ?? [],
        capabilities: capabilityQuery.data ?? [],
        links:
          baseModel.source === "live"
            ? [...(taskLinksQuery.data ?? []), ...(appLinksQuery.data ?? [])]
            : baseModel.links,
      }),
    [
      appLinksQuery.data,
      appQuery.data,
      baseModel.links,
      baseModel.source,
      capabilityQuery.data,
      changeQuery.data,
      designQuery.data,
      findingQuery.data,
      observationQuery.data,
      releaseQuery.data,
      taskLinksQuery.data,
      taskQuery.data,
    ],
  );

  const loading =
    taskQuery.isLoading ||
    designQuery.isLoading ||
    releaseQuery.isLoading ||
    findingQuery.isLoading ||
    appQuery.isLoading ||
    changeQuery.isLoading ||
    observationQuery.isLoading ||
    capabilityQuery.isLoading;
  const substrateUnavailable =
    taskQuery.isError ||
    designQuery.isError ||
    releaseQuery.isError ||
    findingQuery.isError ||
    appQuery.isError ||
    changeQuery.isError ||
    observationQuery.isError ||
    capabilityQuery.isError;

  const lookup = useMemo(() => beadLookup(model), [model]);
  const activeView = EDGE_VIEWS.find((edge) => edge.type === activeEdge) ?? EDGE_VIEWS[0];
  const activeLinks = linksForEdge(model, activeView.type);
  const taskContent = model.task.content as Record<string, unknown>;
  const appContent = model.application.content as Record<string, unknown>;

  return (
    <>
      <PageHeader
        title="Project Alpha"
        subtitle="requirement-centered traversal over the SDLC/ITSM bead graph"
        right={
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            <Badge tone={model.source === "live" ? "pos" : "warn"}>
              {model.source === "live" ? "live trace" : "schema fixture"}
            </Badge>
            <Badge tone="neutral">≤ {ALPHA_INITIAL_BEAD_BUDGET} initial beads</Badge>
          </div>
        }
      />

      <div className="p-3 sm:p-6 space-y-3 sm:space-y-4">
        {loading ? (
          <Card>
            <CardBody className="py-8 text-center text-fg-muted">Loading Alpha graph slice…</CardBody>
          </Card>
        ) : (
          <>
            {substrateUnavailable ? (
              <Card>
                <CardBody className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <div className="text-sm font-medium text-fg">
                      Substrate proxy unavailable; showing the schema fixture.
                    </div>
                    <div className="mt-1 text-xs text-fg-muted">
                      Local dev expects mcp-hub at{" "}
                      <span className="font-mono">VITE_DEV_MCP_HUB</span> or{" "}
                      <span className="font-mono">http://localhost:8000</span>. Alpha stays
                      usable while the graph service is offline.
                    </div>
                  </div>
                  <Badge tone="warn">fixture mode</Badge>
                </CardBody>
              </Card>
            ) : null}

            <Card className="overflow-hidden">
              <CardBody className="p-0">
                <div className="grid lg:grid-cols-[1.05fr_0.95fr]">
                  <section className="p-4 sm:p-5 border-b lg:border-b-0 lg:border-r border-border bg-bg-panel">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="accent">entry point</Badge>
                      <span className="text-2xs text-fg-subtle font-mono">
                        requirement_ref · {model.requirementRef}
                      </span>
                    </div>
                    <h2 className="mt-3 text-xl sm:text-2xl font-semibold tracking-tight text-fg">
                      {displayName(model.task)}
                    </h2>
                    <p className="mt-2 text-sm text-fg-muted max-w-2xl">
                      {typeof taskContent.intent === "string"
                        ? taskContent.intent
                        : "No intent is present on this task yet. Alpha keeps the missing handoff visible."}
                    </p>
                    <div className="mt-4 grid grid-cols-2 gap-px bg-border border border-border">
                      <Fact label="state" value={model.task.state} />
                      <Fact label="risk" value={String(taskContent.risk_class ?? "missing")} />
                      <Fact
                        label="acceptance"
                        value={`${Array.isArray(taskContent.acceptance) ? taskContent.acceptance.length : 0}`}
                      />
                      <Fact
                        label="nfrs"
                        value={`${Array.isArray(taskContent.nfrs) ? taskContent.nfrs.length : 0}`}
                      />
                    </div>
                  </section>

                  <section className="p-4 sm:p-5 bg-bg-subtle/20">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="neutral">application portfolio</Badge>
                      <span className="text-2xs text-fg-subtle font-mono">
                        arch.application · {String(appContent.ref ?? model.application.id)}
                      </span>
                    </div>
                    <h3 className="mt-3 text-lg font-semibold text-fg">{displayName(model.application)}</h3>
                    <p className="mt-2 text-sm text-fg-muted">
                      {typeof appContent.description === "string"
                        ? appContent.description
                        : "No application description is available in this graph slice."}
                    </p>
                    <div className="mt-4 flex flex-wrap gap-2">
                      <Badge tone="accent">{String(appContent.time_disposition ?? "no disposition")}</Badge>
                      <Badge tone="warn">{String(appContent.technical_health ?? "no health")}</Badge>
                      <Badge tone="neutral">{String(appContent.business_value ?? "no value")} value</Badge>
                    </div>
                    <div className="mt-4 rounded border border-border bg-bg-panel px-3 py-2 text-2xs text-fg-muted">
                      Application Portfolio Owner travels up to capabilities and disposition.
                      Technology Owner travels down to services, changes, findings, and incidents.
                      The incident carrier is deliberately absent today.
                    </div>
                  </section>
                </div>
              </CardBody>
            </Card>

            <Stats model={model} />

            <ReleaseCoverageCard
              isLoading={releaseCharterQuery.isLoading}
              isUnavailable={releaseCharterQuery.isError}
              releases={releaseCharterQuery.data}
            />

            <section className="grid lg:grid-cols-[0.8fr_1.2fr] gap-3 sm:gap-4">
              <Card>
                <CardHeader
                  title="Directions of travel"
                  hint="edge vocabulary, not UI filters"
                />
                <CardBody className="space-y-2">
                  {EDGE_VIEWS.map((edge) => (
                    <button
                      key={edge.type}
                      type="button"
                      onClick={() => setActiveEdge(edge.type)}
                      className={cn(
                        "w-full rounded border px-3 py-2.5 text-left transition-colors",
                        activeEdge === edge.type
                          ? "border-accent bg-accent-muted/10"
                          : "border-border bg-bg-panel hover:bg-bg-hover",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-medium text-fg">{edge.label}</span>
                        <span className="text-2xs font-mono text-fg-subtle">{edge.type}</span>
                      </div>
                      <div className="mt-1 text-2xs text-fg-muted">{edge.direction}</div>
                    </button>
                  ))}
                </CardBody>
              </Card>

              <Card>
                <CardHeader
                  title={activeView.label}
                  hint={`${activeView.sourceType} -> ${activeView.targetType}`}
                  right={<Badge tone="neutral">{activeLinks.length} edge{activeLinks.length === 1 ? "" : "s"}</Badge>}
                />
                <CardBody className="space-y-2">
                  {activeLinks.length === 0 ? (
                    <EmptyEdge view={activeView} source={model.source} />
                  ) : (
                    activeLinks.map((edge) => {
                      const source = lookup.get(edge.source_id);
                      const target = lookup.get(edge.target_id);
                      return (
                        <article key={edge.id} className="rounded border border-border bg-bg-subtle/40 p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone="accent">{edge.link_type}</Badge>
                            <span className="text-2xs text-fg-subtle font-mono">
                              {beadKind(source)} {"->"} {beadKind(target)}
                            </span>
                          </div>
                          <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_1fr] sm:items-center">
                            <Node bead={source} label="source" />
                            <div className="hidden sm:block text-fg-subtle">→</div>
                            <Node bead={target} label="target" />
                          </div>
                        </article>
                      );
                    })
                  )}
                </CardBody>
              </Card>
            </section>

            <section className="grid xl:grid-cols-[1.15fr_0.85fr] gap-3 sm:gap-4">
              <Card>
                <CardHeader
                  title="Product persona views"
                  hint="users of Alpha, shaped as directions through the graph"
                />
                <CardBody className="grid gap-2 sm:grid-cols-2">
                  {PRODUCT_PERSONAS.map((persona) => (
                    <article key={persona.id} className="rounded border border-border bg-bg-panel p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-fg">{persona.role}</span>
                        <Badge tone={persona.mode === "flow" ? "warn" : "accent"}>
                          {persona.mode}
                        </Badge>
                      </div>
                      <p className="mt-2 text-xs text-fg-muted leading-relaxed">{persona.outcome}</p>
                      <div className="mt-3 text-2xs text-fg-subtle">{persona.direction}</div>
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {persona.mode === "flow" ? (
                          <Link
                            to="/factory"
                            className="rounded border border-warn/30 bg-warn/10 px-2 py-1 text-2xs font-mono uppercase tracking-wider text-warn"
                          >
                            open Factory Board
                          </Link>
                        ) : (
                          persona.edgeTypes.map((edge) => (
                            <button
                              key={edge}
                              type="button"
                              onClick={() => setActiveEdge(edge)}
                              className="rounded border border-border bg-bg-subtle px-2 py-1 text-2xs font-mono uppercase tracking-wider text-fg-muted"
                            >
                              {edge}
                            </button>
                          ))
                        )}
                      </div>
                    </article>
                  ))}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Honest graph state" hint="partial is normal today" />
                <CardBody className="space-y-3">
                  <div className="rounded border border-warn/30 bg-warn/5 p-3">
                    <div className="text-sm font-medium text-fg">Designed for sparse data</div>
                    <p className="mt-1 text-xs text-fg-muted leading-relaxed">
                      A node with no edges is not an error. The route reads current schemas and
                      uses fixtures only when live tasks have no requirement references yet.
                    </p>
                  </div>
                  <div className="rounded border border-border bg-bg-subtle/40 p-3">
                    <div className="text-sm font-medium text-fg">Product users are not loop roles</div>
                    <p className="mt-1 text-xs text-fg-muted leading-relaxed">
                      Scrum Master and Technology Owner are Alpha users, not loop personas.
                      Polecat developer is a loop persona, not a product user.
                    </p>
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {OPERATING_MODEL_PERSONAS.map((role) => (
                        <Badge key={role} tone="neutral">{role}</Badge>
                      ))}
                    </div>
                  </div>
                  <div className="rounded border border-neg/30 bg-neg/5 p-3">
                    <div className="text-sm font-medium text-fg">No carrier yet</div>
                    <p className="mt-1 text-xs text-fg-muted leading-relaxed">
                      Production incidents, accepted risk, spikes, and reviewer evaluation
                      metrics are not modeled as live beads in this pass. Alpha names the gap
                      instead of drawing fake edges.
                    </p>
                  </div>
                </CardBody>
              </Card>
            </section>
          </>
        )}
      </div>
    </>
  );
}

function useAlphaBeads(spec: { namespace: string; type: string }) {
  return useQuery({
    queryKey: [...QK, spec.namespace, spec.type],
    queryFn: () =>
      substrateClient.listBeads({
        namespace: spec.namespace,
        type: spec.type,
        limit: ALPHA_FETCH_LIMIT_PER_TYPE,
      }),
  });
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-bg-panel px-3 py-2.5">
      <div className="panel-title">{label}</div>
      <div className="mt-1 text-sm font-semibold text-fg truncate">{value}</div>
    </div>
  );
}

function Stats({ model }: { model: ReturnType<typeof buildAlphaModel> }) {
  const stats = [
    ["dev.task", model.stats.tasks],
    ["with requirement_refs", model.stats.tasksWithRequirementRefs],
    ["with arch_impact", model.stats.tasksWithArchImpact],
    ["arch.application", model.stats.applications],
    ["edge rows", model.stats.links],
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-5 gap-px bg-border border border-border">
      {stats.map(([label, value]) => (
        <div key={label} className="bg-bg-panel p-3">
          <div className="panel-title">{label}</div>
          <div className="mt-1 text-xl font-semibold num text-fg">{value}</div>
        </div>
      ))}
    </div>
  );
}

function EmptyEdge({ view, source }: { view: (typeof EDGE_VIEWS)[number]; source: "live" | "fixture" }) {
  return (
    <div className="rounded border border-dashed border-border bg-bg-subtle/30 p-4 text-center">
      <div className="text-sm font-medium text-fg">{view.empty}</div>
      <p className="mt-2 text-xs text-fg-muted max-w-prose mx-auto">
        {source === "live"
          ? "This is expected on the current dev substrate while writers for the newer bead types and edge rows come online."
          : "The fixture graph demonstrates the intended traversal without pretending the live substrate is populated."}
      </p>
    </div>
  );
}

function Node({ bead, label }: { bead: Bead | undefined; label: string }) {
  return (
    <div className="rounded border border-border bg-bg-panel px-3 py-2">
      <div className="panel-title">{label}</div>
      <div className="mt-1 text-sm font-medium text-fg">{displayName(bead)}</div>
      <div className="mt-1 text-2xs text-fg-subtle font-mono">{beadKind(bead)}</div>
    </div>
  );
}
