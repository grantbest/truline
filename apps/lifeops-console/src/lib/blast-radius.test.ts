import { describe, expect, it } from "vitest";
import type { Bead, BeadLink } from "@/types/bead";
import { blastRadius, graphCoverage, type BlastRadiusEntities } from "./blast-radius";

// The properties worth testing here are the ones a sparse or cyclic graph
// would get wrong quietly: a transitive dependent missed because it only
// shows up two hops out through a service, a cycle that hangs the walk, and
// an empty answer that looks the same whether nothing depends on a node or
// nothing was ever loaded to check.

function app(id: string, name = id): Bead {
  return {
    id,
    namespace: "arch",
    type: "application",
    state: "operate",
    parent_id: null,
    content: { ref: id, name, description: "d", layer: "supply", owner: "grant", build: "custom",
      business_value: "high", technical_health: "healthy", time_disposition: "invest",
      evidence: ["x"], assessed_at: "2026-08-23" },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
  } as unknown as Bead;
}

function svc(id: string, name = id): Bead {
  return {
    id,
    namespace: "arch",
    type: "service",
    state: "active",
    parent_id: null,
    content: { ref: id, name, description: "d", layer: "supply", owner: "grant",
      evidence: ["x"], assessed_at: "2026-08-23" },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
  } as unknown as Bead;
}

function ci(id: string): Bead {
  return {
    id,
    namespace: "arch",
    type: "ci",
    state: "active",
    parent_id: null,
    content: { ref: id, ci_kind: "workload", cluster: "cluster-a", namespace: "platform-core",
      kind: "Deployment", name: id, source_class: "observed" },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-observer",
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
  } as unknown as Bead;
}

function cap(id: string, name = id): Bead {
  return {
    id,
    namespace: "arch",
    type: "capability",
    state: "active",
    parent_id: null,
    content: { ref: id, name, description: "d", layer: "supply", maturity: "operating",
      owner: "grant", evidence: ["x"], assessed_at: "2026-08-23" },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-08-23T00:00:00Z",
    updated_at: "2026-08-23T00:00:00Z",
  } as unknown as Bead;
}

function link(source: Bead, target: Bead, link_type: string): BeadLink {
  return {
    id: `${source.id}-${link_type}-${target.id}`,
    source_id: source.id,
    target_id: target.id,
    link_type,
    content: {},
    created_at: "2026-08-23T00:00:00Z",
    created_by: "ea-load",
  };
}

// This is not synthetic. It is app.substrate's blast radius exactly as
// authored in docs/architecture/model/application-portfolio.yaml and
// services.yaml on 2026-08-23 — the `depends_on`, `consumes` and `realizes`
// arrays there, transcribed as fixture edges. app.mcp-hub and app.automations
// reach app.substrate only through svc.foundation (they `consumes:
// [svc.foundation]`, not `depends_on: [app.substrate]` directly), which is
// exactly the two-hop, cross-type case this traversal exists for.
function substrateFoundationFixture(): { entities: BlastRadiusEntities; links: BeadLink[]; origin: Bead } {
  const appSubstrate = app("app.substrate", "Bead Substrate");
  const appTemporal = app("app.temporal", "Temporal");
  const appFactoryDispatcher = app("app.factory-dispatcher", "Factory Dispatcher");
  const appMcpHub = app("app.mcp-hub", "MCP Hub (Capability Gateway)");
  const appAutomations = app("app.automations", "Automations Worker");
  const appFinanceReporting = app("app.finance-reporting", "Finance Reporting (Spend Pulse)");
  const appUnrelated = app("app.pihole", "Pi-hole");

  const svcFoundation = svc("svc.foundation", "Platform Foundation");

  const capSubstratePersistence = cap("pc.substrate.persistence");
  const capSubstrateProvenance = cap("pc.substrate.provenance");
  const capSubstrateRetrieval = cap("pc.substrate.retrieval");
  const capSubstrateEvents = cap("pc.substrate.events");
  const capTrustDataprotection = cap("pc.trust.dataprotection");
  const capFactoryDispatch = cap("pc.factory.dispatch");
  const capFactoryIntake = cap("pc.factory.intake");
  const capFinanceBudget = cap("bc.finance.budget");
  const capHomeLogistics = cap("bc.home.logistics");
  const capExposureRest = cap("pc.exposure.rest");
  const capNetwork = cap("bc.foundation.network"); // realized by app.pihole — must not appear

  const entities: BlastRadiusEntities = {
    applications: [
      appSubstrate, appTemporal, appFactoryDispatcher, appMcpHub, appAutomations,
      appFinanceReporting, appUnrelated,
    ],
    services: [svcFoundation],
    cis: [],
    capabilities: [
      capSubstratePersistence, capSubstrateProvenance, capSubstrateRetrieval, capSubstrateEvents,
      capTrustDataprotection, capFactoryDispatch, capFactoryIntake, capFinanceBudget,
      capHomeLogistics, capExposureRest, capNetwork,
    ],
  };

  const links: BeadLink[] = [
    // svc.foundation depends_on: [app.substrate, app.temporal]
    link(svcFoundation, appSubstrate, "depends_on"),
    link(svcFoundation, appTemporal, "depends_on"),
    // app.factory-dispatcher depends_on: [app.substrate, app.temporal]; consumes: [svc.foundation]
    link(appFactoryDispatcher, appSubstrate, "depends_on"),
    link(appFactoryDispatcher, appTemporal, "depends_on"),
    link(appFactoryDispatcher, svcFoundation, "consumes"),
    // app.mcp-hub consumes: [svc.foundation] — reaches app.substrate only via svc.foundation
    link(appMcpHub, svcFoundation, "consumes"),
    // app.automations consumes: [svc.foundation] — same two-hop shape
    link(appAutomations, svcFoundation, "consumes"),
    // app.finance-reporting depends_on: [app.substrate]
    link(appFinanceReporting, appSubstrate, "depends_on"),
    // app.pihole has no edge to app.substrate at all — must not appear
    // realizes
    link(appSubstrate, capSubstratePersistence, "realizes"),
    link(appSubstrate, capSubstrateProvenance, "realizes"),
    link(appSubstrate, capSubstrateRetrieval, "realizes"),
    link(appSubstrate, capSubstrateEvents, "realizes"),
    link(appSubstrate, capTrustDataprotection, "realizes"),
    link(appFactoryDispatcher, capFactoryDispatch, "realizes"),
    link(appFactoryDispatcher, capFactoryIntake, "realizes"),
    link(appFinanceReporting, capFinanceBudget, "realizes"),
    link(appAutomations, capHomeLogistics, "realizes"),
    link(appMcpHub, capExposureRest, "realizes"),
    link(appUnrelated, capNetwork, "realizes"),
  ];

  return { entities, links, origin: appSubstrate };
}

describe("blastRadius — real app.substrate scenario (S34-B1/S35-B1/S35-B2 fixture)", () => {
  it("fails against today's behaviour: no traversal exists yet", () => {
    // This assertion is the acceptance criterion itself: a known multi-hop
    // blast radius, computed by the function under test. Before this task,
    // no such function existed in src/lib — this test could not have passed.
    const { entities, links, origin } = substrateFoundationFixture();
    const result = blastRadius(origin, entities, links);
    expect(result.applications.map((a) => a.id)).toEqual([
      "app.automations",
      "app.factory-dispatcher",
      "app.finance-reporting",
      "app.mcp-hub",
    ]);
  });

  it("reaches app.mcp-hub and app.automations transitively through svc.foundation, not directly", () => {
    const { entities, links, origin } = substrateFoundationFixture();
    const result = blastRadius(origin, entities, links);
    expect(result.services.map((s) => s.id)).toEqual(["svc.foundation"]);
    expect(result.applications.map((a) => a.id)).toContain("app.mcp-hub");
    expect(result.applications.map((a) => a.id)).toContain("app.automations");
  });

  it("never includes an application with no path to the origin", () => {
    const { entities, links, origin } = substrateFoundationFixture();
    const result = blastRadius(origin, entities, links);
    expect(result.applications.map((a) => a.id)).not.toContain("app.pihole");
  });

  it("collects the capabilities realized by the dependents plus the origin's own", () => {
    const { entities, links, origin } = substrateFoundationFixture();
    const result = blastRadius(origin, entities, links);
    const ids = result.capabilities.map((c) => c.id).sort();
    expect(ids).toEqual(
      [
        "bc.finance.budget",
        "bc.home.logistics",
        "pc.exposure.rest",
        "pc.factory.dispatch",
        "pc.factory.intake",
        "pc.substrate.events",
        "pc.substrate.persistence",
        "pc.substrate.provenance",
        "pc.substrate.retrieval",
        "pc.trust.dataprotection",
      ].sort(),
    );
    // app.pihole's capability must not leak in through an unrelated realizes edge.
    expect(ids).not.toContain("bc.foundation.network");
  });

  it("declares coverage as two numbers, not a blank or a bare percentage", () => {
    const { entities, links, origin } = substrateFoundationFixture();
    const result = blastRadius(origin, entities, links);
    expect(result.status).toBe("measured");
    expect(result.coverage.totalNodes).toBe(entities.applications.length + entities.services.length);
    expect(result.coverage.linkedNodes).toBeGreaterThan(0);
    expect(result.coverage.linkedNodes).toBeLessThanOrEqual(result.coverage.totalNodes);
  });
});

describe("blastRadius — cycle safety", () => {
  it("terminates on a cycle instead of hanging, and still finds the reachable dependent", () => {
    const a = app("a");
    const b = app("b");
    const entities: BlastRadiusEntities = { applications: [a, b], services: [], cis: [], capabilities: [] };
    // a depends_on b, b depends_on a — a two-node cycle.
    const links: BeadLink[] = [link(a, b, "depends_on"), link(b, a, "depends_on")];

    const result = blastRadius(a, entities, links);

    expect(result.applications.map((x) => x.id)).toEqual(["b"]);
    expect(result.truncated).toBe(false);
  });

  it("terminates on a self-loop", () => {
    const a = app("a");
    const entities: BlastRadiusEntities = { applications: [a], services: [], cis: [], capabilities: [] };
    const links: BeadLink[] = [link(a, a, "depends_on")];

    const result = blastRadius(a, entities, links);

    expect(result.applications).toEqual([]);
  });

  it("truncates a chain longer than the bound instead of walking it forever", () => {
    // A five-node chain: e -> d -> c -> b -> a (each depends_on the next),
    // walked backward from a. With maxNodes=3 the walk must stop early and
    // say so, rather than silently returning a partial answer as complete.
    const [a, b, c, d, e] = ["a", "b", "c", "d", "e"].map((id) => app(id));
    const entities: BlastRadiusEntities = {
      applications: [a, b, c, d, e], services: [], cis: [], capabilities: [],
    };
    const links: BeadLink[] = [
      link(b, a, "depends_on"),
      link(c, b, "depends_on"),
      link(d, c, "depends_on"),
      link(e, d, "depends_on"),
    ];

    const result = blastRadius(a, entities, links, 3);

    expect(result.truncated).toBe(true);
    expect(result.applications.length).toBeLessThan(4);
  });
});

describe("blastRadius — CI as origin", () => {
  it("finds the application that depends on a CI directly", () => {
    const theCi = ci("ci.postgres-0");
    const appOrders = app("app.orders");
    const entities: BlastRadiusEntities = {
      applications: [appOrders], services: [], cis: [theCi], capabilities: [],
    };
    const links: BeadLink[] = [link(appOrders, theCi, "depends_on")];

    const result = blastRadius(theCi, entities, links);

    expect(result.applications.map((a) => a.id)).toEqual(["app.orders"]);
  });
});

describe("blastRadius / graphCoverage — missing linkage is a failure state, not a blank", () => {
  it("reads no_linkage_data when nothing has been loaded, even though the origin exists", () => {
    const origin = app("app.lonely");
    const entities: BlastRadiusEntities = {
      applications: [origin, app("app.other")], services: [], cis: [], capabilities: [],
    };
    const result = blastRadius(origin, entities, []);

    expect(result.status).toBe("no_linkage_data");
    expect(result.applications).toEqual([]);
  });

  it("reads measured with an empty list when the graph is populated but this node truly has no dependents", () => {
    const origin = app("app.isolated");
    const other1 = app("app.other1");
    const other2 = app("app.other2");
    const entities: BlastRadiusEntities = {
      applications: [origin, other1, other2], services: [], cis: [], capabilities: [],
    };
    // Linkage exists in the graph, just not touching `origin`.
    const links: BeadLink[] = [link(other1, other2, "depends_on")];

    const result = blastRadius(origin, entities, links);

    expect(result.status).toBe("measured");
    expect(result.applications).toEqual([]);
  });

  it("graphCoverage reports linked vs total nodes as two numbers", () => {
    const a = app("a");
    const b = app("b");
    const c = app("c"); // unlinked
    const s = svc("s");
    const entities: BlastRadiusEntities = { applications: [a, b, c], services: [s], cis: [], capabilities: [] };
    const links: BeadLink[] = [link(a, s, "consumes"), link(b, a, "depends_on")];

    const coverage = graphCoverage(entities, links);

    expect(coverage).toEqual({ linkedNodes: 3, totalNodes: 4 });
  });

  it("graphCoverage is zero-of-N, not undefined, when the estate has no nodes linked at all", () => {
    const entities: BlastRadiusEntities = {
      applications: [app("a"), app("b")], services: [], cis: [], capabilities: [],
    };
    expect(graphCoverage(entities, [])).toEqual({ linkedNodes: 0, totalNodes: 2 });
  });
});
