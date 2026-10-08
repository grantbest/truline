import { describe, expect, it } from "vitest";
import type { Bead, BeadLink } from "@/types/bead";
import {
  assessmentSources,
  applicationAssessments,
  applicationDependencies,
  findingsStatus,
  gaps,
  capabilityCoverage,
  gitopsOrphans,
  groupByLayer,
  isStale,
  observationFindings,
  roadmapBacklog,
  sourceClassOf,
  timeGrid,
  undoneTeardowns,
  countDebt,
  byMaturity,
} from "./ea-model";

// The properties worth testing are the ones whose failure would make the page
// lie quietly rather than break loudly: a capability silently missing from its
// group, an orphan not surfaced, a teardown that looks done.

function cap(
  id: string,
  over: Partial<Record<string, unknown>> = {},
  parent_id: string | null = null,
): Bead {
  return {
    id,
    namespace: "arch",
    type: "capability",
    state: "active",
    parent_id,
    content: {
      ref: id,
      name: id,
      description: "d",
      layer: "supply",
      maturity: "operating",
      owner: "grant",
      evidence: ["x"],
      assessed_at: "2026-07-31",
      ...over,
    },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
  } as unknown as Bead;
}

function app(id: string, over: Partial<Record<string, unknown>> = {}): Bead {
  return {
    id,
    namespace: "arch",
    type: "application",
    state: "operate",
    parent_id: null,
    content: {
      ref: id,
      name: id,
      description: "d",
      layer: "supply",
      owner: "grant",
      build: "oss",
      business_value: "high",
      technical_health: "healthy",
      time_disposition: "tolerate",
      workload: { runtime: "none", note: "runs nowhere" },
      evidence: ["x"],
      assessed_at: "2026-07-31",
      ...over,
    },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
  } as unknown as Bead;
}

function observation(id: string): Bead {
  return {
    id,
    namespace: "arch",
    type: "observation",
    state: "active",
    parent_id: null,
    content: {
      ref: id,
      observed_at: "2026-08-04T16:18:17Z",
      workload: {
        cluster: "cluster-a",
        namespace: "platform-core",
        kind: "Deployment",
        name: "lifeops-console",
      },
      replicas: 1,
      ready_replicas: 1,
    },
    context: {},
    trust_tier: "user",
    provenance: {},
    created_by: "ea-load",
    created_at: "2026-08-04T16:18:17Z",
    updated_at: "2026-08-04T16:18:17Z",
  } as unknown as Bead;
}

function link(source: Bead, target: Bead, link_type = "measures"): BeadLink {
  return {
    id: `${source.id}-${link_type}-${target.id}`,
    source_id: source.id,
    target_id: target.id,
    link_type,
    content: {},
    created_at: "2026-08-04T16:18:17Z",
    created_by: "ea-load",
  };
}

describe("groupByLayer", () => {
  it("returns only roots for the requested layer", () => {
    const caps = [
      cap("bc.a", { layer: "demand" }),
      cap("pc.a", { layer: "supply" }),
      cap("pc.a.x", { layer: "supply" }, "pc.a"),
    ];
    expect(groupByLayer(caps, "demand").map((g) => g.root.id)).toEqual(["bc.a"]);
    expect(groupByLayer(caps, "supply").map((g) => g.root.id)).toEqual(["pc.a"]);
  });

  it("attaches children by parent_id, not by name prefix", () => {
    const caps = [cap("pc.a"), cap("unrelated-id", {}, "pc.a")];
    const [group] = groupByLayer(caps, "supply");
    expect(group.children.map((c) => c.id)).toEqual(["unrelated-id"]);
  });

  it("a root with no children is still a group", () => {
    const [group] = groupByLayer([cap("pc.lonely")], "supply");
    expect(group.children).toEqual([]);
  });

  it("never loses a capability that has a parent in the other layer", () => {
    // Defensive: children are collected across the whole set, so a child does
    // not vanish because its own `layer` was authored differently.
    const caps = [cap("pc.a"), cap("pc.a.x", { layer: "demand" }, "pc.a")];
    expect(groupByLayer(caps, "supply")[0].children).toHaveLength(1);
  });
});

describe("gaps", () => {
  it("is exactly the absent capabilities", () => {
    const caps = [cap("a", { maturity: "absent" }), cap("b", { maturity: "emerging" })];
    expect(gaps(caps).map((c) => c.id)).toEqual(["a"]);
  });
});

describe("timeGrid", () => {
  it("keys cells by value and health", () => {
    const grid = timeGrid([
      app("x", { business_value: "low", technical_health: "at_risk" }),
      app("y", { business_value: "low", technical_health: "at_risk" }),
      app("z", { business_value: "high", technical_health: "healthy" }),
    ]);
    expect(grid.get("low:at_risk")).toHaveLength(2);
    expect(grid.get("high:healthy")).toHaveLength(1);
    expect(grid.get("medium:degraded")).toBeUndefined();
  });
});

describe("gitopsOrphans", () => {
  const orphan = {
    cluster: "cluster-a",
    namespace: "platform-itsm",
    kind: "Deployment",
    name: "itop-app",
    manifest: null,
    managed_by: "none",
  };

  it("finds an object with no manifest and no manager", () => {
    const found = gitopsOrphans([
      app("app.itop", { workload: { runtime: "kubernetes", objects: [orphan] } }),
    ]);
    expect(found).toHaveLength(1);
    expect(found[0].object.name).toBe("itop-app");
  });

  it("a managed object is not an orphan even with no manifest", () => {
    // Helm-installed components legitimately have no manifest in this repo.
    const helm = { ...orphan, managed_by: "helm" };
    expect(gitopsOrphans([app("a", { workload: { runtime: "kubernetes", objects: [helm] } })]))
      .toHaveLength(0);
  });

  it("an unmanaged object with a manifest is not an orphan", () => {
    const cited = { ...orphan, manifest: "infrastructure/k8s/x.yaml" };
    expect(gitopsOrphans([app("a", { workload: { runtime: "kubernetes", objects: [cited] } })]))
      .toHaveLength(0);
  });

  it("applications with no workload objects are skipped", () => {
    expect(gitopsOrphans([app("a")])).toHaveLength(0);
  });
});

describe("undoneTeardowns", () => {
  it("is eliminate plus a still-declared workload", () => {
    const running = {
      cluster: "c", namespace: "home", kind: "Deployment",
      name: "vikunja", manifest: "infrastructure/k8s/home/vikunja.yaml", managed_by: "deploy-workflow",
    };
    const apps = [
      app("app.vikunja", {
        time_disposition: "eliminate",
        workload: { runtime: "kubernetes", objects: [running] },
      }),
      // Retired properly: eliminate, but nothing left running.
      app("app.sync", { time_disposition: "eliminate" }),
      app("app.keep", { time_disposition: "invest", workload: { runtime: "kubernetes", objects: [running] } }),
    ];
    expect(undoneTeardowns(apps).map((a) => a.id)).toEqual(["app.vikunja"]);
  });
});

describe("countDebt", () => {
  it("sums recorded debt across applications", () => {
    expect(countDebt([app("a", { debt: ["x", "y"] }), app("b", { debt: [] }), app("c")])).toBe(2);
  });
});

describe("roadmapBacklog", () => {
  const roadmap = (target_date: string) => ({
    opened_at: "2026-08-04",
    target_date,
    action: `target ${target_date}`,
  });

  it("turns absent capabilities and migrate/eliminate applications into backlog items", () => {
    const rows = roadmapBacklog(
      [
        cap("cap.absent", { name: "Absent", maturity: "absent", roadmap: roadmap("2026-09-01") }),
        cap("cap.operating", { maturity: "operating", roadmap: roadmap("2026-08-01") }),
      ],
      [
        app("app.migrate", { name: "Migrate", time_disposition: "migrate", roadmap: roadmap("2026-08-15") }),
        app("app.eliminate", {
          name: "Eliminate",
          time_disposition: "eliminate",
          roadmap: roadmap("2026-08-10"),
        }),
        app("app.tolerate", { time_disposition: "tolerate", roadmap: roadmap("2026-08-01") }),
      ],
    );

    expect(rows.map((row) => [row.kind, row.ref])).toEqual([
      ["application_elimination", "app.eliminate"],
      ["application_migration", "app.migrate"],
      ["capability_gap", "cap.absent"],
    ]);
  });

  it("does not invent backlog items when the model has no roadmap action", () => {
    expect(
      roadmapBacklog(
        [cap("cap.absent", { maturity: "absent" })],
        [app("app.migrate", { time_disposition: "migrate" })],
      ),
    ).toEqual([]);
  });
});

describe("assessmentSources", () => {
  it("marks applications with linked observations as measured", () => {
    const measured = app("app.measured");
    const asserted = app("app.asserted");
    const obs = observation("obs.measured");

    const sources = assessmentSources([measured, asserted], [obs], [link(obs, measured)]);

    expect(sources.get(measured.id)).toBe("measured");
    expect(sources.get(asserted.id)).toBe("asserted");
  });

  it("does not treat an unlinked observation as measurement", () => {
    const application = app("app.only-asserted");

    const sources = assessmentSources([application], [observation("obs.unlinked")], []);

    expect(sources.get(application.id)).toBe("asserted");
  });

  it("ignores non-measures links", () => {
    const application = app("app.changed");
    const obs = observation("obs.changed");

    const sources = assessmentSources([application], [obs], [link(obs, application, "affects")]);

    expect(sources.get(application.id)).toBe("asserted");
  });
});

describe("applicationAssessments", () => {
  it("returns measured rows with newest observations first", () => {
    const application = app("app.measured");
    const oldObservation = observation("obs.old");
    const newObservation = observation("obs.new");
    oldObservation.content.observed_at = "2026-08-04T10:00:00Z";
    newObservation.content.observed_at = "2026-08-04T12:00:00Z";

    const rows = applicationAssessments(
      [application],
      [oldObservation, newObservation],
      [link(oldObservation, application), link(newObservation, application)],
    );

    expect(rows[0].source).toBe("measured");
    expect(rows[0].observations.map((item) => item.id)).toEqual(["obs.new", "obs.old"]);
  });

  it("keeps applications without measures links asserted", () => {
    const application = app("app.asserted");

    const rows = applicationAssessments([application], [observation("obs")], []);

    expect(rows[0].source).toBe("asserted");
    expect(rows[0].observations).toEqual([]);
  });
});

describe("capabilityCoverage", () => {
  it("maps realized applications and support direction from bead links", () => {
    const appBead = app("app.console", { name: "Console" });
    const source = cap("cap.source", { name: "Source" });
    const target = cap("cap.target", { name: "Target" });

    const rows = capabilityCoverage(
      [target, source],
      [appBead],
      [link(appBead, target, "realizes"), link(source, target, "supports")],
    );
    const targetRow = rows.find((row) => row.capability.id === target.id);
    const sourceRow = rows.find((row) => row.capability.id === source.id);

    expect(targetRow?.realizedBy.map((item) => item.id)).toEqual(["app.console"]);
    expect(targetRow?.supportedBy.map((item) => item.id)).toEqual(["cap.source"]);
    expect(sourceRow?.supports.map((item) => item.id)).toEqual(["cap.target"]);
  });
});

describe("applicationDependencies", () => {
  it("maps outgoing and incoming application dependencies", () => {
    const consoleApp = app("app.console", { name: "Console" });
    const substrateApp = app("app.substrate", { name: "Substrate" });

    const rows = applicationDependencies(
      [consoleApp, substrateApp],
      [link(consoleApp, substrateApp, "depends_on")],
    );

    expect(rows).toHaveLength(2);
    expect(rows.find((row) => row.application.id === consoleApp.id)?.dependsOn[0].id)
      .toBe(substrateApp.id);
    expect(rows.find((row) => row.application.id === substrateApp.id)?.dependedOnBy[0].id)
      .toBe(consoleApp.id);
  });

  it("omits applications with no dependency edges", () => {
    expect(applicationDependencies([app("app.alone")], [])).toEqual([]);
  });
});

describe("byMaturity", () => {
  it("sorts worst first so a group's problems lead", () => {
    const sorted = [
      cap("op", { maturity: "operating" }),
      cap("ab", { maturity: "absent" }),
      cap("em", { maturity: "emerging" }),
    ].sort(byMaturity);
    expect(sorted.map((c) => c.id)).toEqual(["ab", "em", "op"]);
  });
});

describe("sourceClassOf", () => {
  it("reads an authored, derived, or observed record as itself", () => {
    expect(sourceClassOf({ source_class: "authored" })).toBe("authored");
    expect(sourceClassOf({ source_class: "derived" })).toBe("derived");
    expect(sourceClassOf({ source_class: "observed" })).toBe("observed");
  });

  it("a record with no source_class field is unclassified, not a guess", () => {
    // This is the pre-S34-B3 shape: today's model has no source_class field
    // at all on capability/application content, so this must be the case
    // that fails against current behaviour until the field is wired through.
    expect(sourceClassOf({})).toBe("unclassified");
  });
});

describe("isStale", () => {
  const now = new Date("2026-08-24T00:00:00Z");

  it("a derived record confirmed within its cadence is not stale", () => {
    expect(isStale("derived", "2026-08-10T00:00:00Z", now)).toBe(false);
  });

  it("a derived record unconfirmed beyond its cadence is stale", () => {
    // 30-day cadence for derived: 2026-07-01 is 54 days before "now".
    expect(isStale("derived", "2026-07-01T00:00:00Z", now)).toBe(true);
  });

  it("an observed record uses its own, tighter cadence", () => {
    // 7-day cadence for observed: 10 days old is stale, 3 days old is not.
    expect(isStale("observed", "2026-08-14T00:00:00Z", now)).toBe(true);
    expect(isStale("observed", "2026-08-21T00:00:00Z", now)).toBe(false);
  });

  it("an authored record is never stale, no matter its age", () => {
    expect(isStale("authored", "2020-01-01T00:00:00Z", now)).toBe(false);
  });

  it("an unclassified record is never stale — absence must not render as fresh either", () => {
    expect(isStale("unclassified", "2020-01-01T00:00:00Z", now)).toBe(false);
  });
});

describe("observationFindings", () => {
  const orphanObject = {
    cluster: "cluster-a",
    namespace: "platform-itsm",
    kind: "Deployment",
    name: "itop-app",
    manifest: null,
    managed_by: "none",
  };
  const runningObject = {
    cluster: "c",
    namespace: "home",
    kind: "Deployment",
    name: "vikunja",
    manifest: "infrastructure/k8s/home/vikunja.yaml",
    managed_by: "deploy-workflow",
  };

  it("carries a dated entry per orphan and per undone teardown", () => {
    const findings = observationFindings([
      app("app.itop", {
        assessed_at: "2026-08-01",
        workload: { runtime: "kubernetes", objects: [orphanObject] },
      }),
      app("app.vikunja", {
        assessed_at: "2026-08-10",
        time_disposition: "eliminate",
        workload: { runtime: "kubernetes", objects: [runningObject] },
      }),
    ]);

    expect(findings.map((f) => f.kind).sort()).toEqual(["orphan", "teardown"]);
    expect(findings.every((f) => typeof f.foundAt === "string" && f.foundAt.length > 0)).toBe(true);
  });

  it("is empty when the estate has no orphans or undone teardowns", () => {
    expect(observationFindings([app("app.clean")])).toEqual([]);
  });
});

describe("findingsStatus", () => {
  it("reads as findings when the list is non-empty", () => {
    const findings = observationFindings([
      app("app.itop", {
        workload: {
          runtime: "kubernetes",
          objects: [{
            cluster: "c", namespace: "n", kind: "Deployment", name: "x",
            manifest: null, managed_by: "none",
          }],
        },
      }),
    ]);
    expect(findingsStatus(findings, [observation("obs.a")])).toBe("findings");
  });

  it("reads as measured-clean when the list is empty but observations exist", () => {
    // This is the empty state that must not look like an unmeasured estate:
    // the model was checked and nothing was found.
    expect(findingsStatus([], [observation("obs.a")])).toBe("measured_clean");
  });

  it("reads as not-measuring when there are no observations at all", () => {
    // Distinct from measured-clean: nobody has looked yet.
    expect(findingsStatus([], [])).toBe("not_measuring");
  });
});
