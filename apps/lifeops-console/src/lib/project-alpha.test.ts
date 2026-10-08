import { describe, expect, it } from "vitest";
import type { Bead } from "@/types/bead";
import {
  ALPHA_FETCH_LIMIT_PER_TYPE,
  ALPHA_INITIAL_BEAD_BUDGET,
  EDGE_VIEWS,
  OPERATING_MODEL_PERSONAS,
  PRODUCT_PERSONAS,
  buildAlphaModel,
  productPersonaById,
  releaseCoverage,
} from "@/lib/project-alpha";

function bead(overrides: Partial<Bead> & Pick<Bead, "id" | "namespace" | "type">): Bead {
  return {
    state: "active",
    parent_id: null,
    context: {},
    content: {},
    confidence: null,
    trust_tier: "test",
    provenance: {},
    created_by: "test",
    created_at: "2026-08-02T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
    ...overrides,
  };
}

const emptyData = {
  tasks: [],
  designs: [],
  releases: [],
  findings: [],
  applications: [],
  changes: [],
  observations: [],
  capabilities: [],
  links: [],
};

describe("Project Alpha model", () => {
  it("keeps the initial browser bead budget under the architecture limit", () => {
    expect(ALPHA_INITIAL_BEAD_BUDGET).toBeLessThanOrEqual(1000);
  });

  it("uses only the canonical edge vocabulary from bead-object-inventory", () => {
    expect(EDGE_VIEWS.map((edge) => edge.type)).toEqual([
      "designs",
      "gates",
      "regresses",
      "found_by",
      "affects",
      "measures",
      "supersedes",
    ]);
  });

  it("does not conflate product personas with operating-model loop roles", () => {
    expect(PRODUCT_PERSONAS.map((p) => p.role)).toContain("Scrum Master");
    expect(PRODUCT_PERSONAS.map((p) => p.role)).toContain("Technology Owner");
    expect(PRODUCT_PERSONAS.map((p) => p.role)).not.toContain("Polecat developer");
    expect(OPERATING_MODEL_PERSONAS).toContain("Polecat developer");
    expect(OPERATING_MODEL_PERSONAS).not.toContain("Scrum Master");
    expect(OPERATING_MODEL_PERSONAS).not.toContain("Technology Owner");
  });

  it("uses fixtures when live dev.task beads have no requirement_refs", () => {
    const model = buildAlphaModel({
      ...emptyData,
      tasks: [
        bead({
          id: "task-without-refs",
          namespace: "dev",
          type: "task",
          content: { title: "Current sparse task" },
        }),
      ],
    });

    expect(model.source).toBe("fixture");
    expect(model.requirementRef).toBe("LO-CAT-004/AC-1");
    expect(model.stats.tasks).toBe(1);
    expect(model.stats.tasksWithRequirementRefs).toBe(0);
  });

  it("selects a live requirement task and matching application when trace fields exist", () => {
    const task = bead({
      id: "task-with-refs",
      namespace: "dev",
      type: "task",
      content: {
        title: "Live trace task",
        requirement_refs: ["LO-CAT-004/AC-1"],
        arch_impact: { applications: ["app.identity"], capabilities: ["cap.auth"] },
      },
    });
    const app = bead({
      id: "app-1",
      namespace: "arch",
      type: "application",
      content: { ref: "app.identity", name: "Identity" },
    });

    const model = buildAlphaModel({ ...emptyData, tasks: [task], applications: [app] });

    expect(model.source).toBe("live");
    expect(model.task.id).toBe("task-with-refs");
    expect(model.application.id).toBe("app-1");
    expect(model.requirementRef).toBe("LO-CAT-004/AC-1");
  });

  it("flags Scrum Master as flow while portfolio and technology owners travel opposite directions", () => {
    expect(productPersonaById("scrum-master").mode).toBe("flow");
    expect(productPersonaById("application-portfolio-owner").direction).toMatch(/capabilities/);
    expect(productPersonaById("technology-owner").direction).toMatch(/services/);
  });
});

describe("release charter coverage — arch.release, never a fixture substitute", () => {
  function releaseCharter(overrides: Partial<Bead> & Pick<Bead, "id">): Bead {
    return bead({
      namespace: "arch",
      type: "release",
      content: {
        ref: "R26.01",
        name: "Trust earns its amendment",
        objective: "Make the safety claims measurable rather than stated.",
        sprints: ["33", "34", "35"],
        outcomes: [{ id: "O-1", statement: "Every filed task names its release.", work_class: "enabling" }],
        declared_balance: {},
        opened_at: "2026-08-25",
      },
      ...overrides,
    });
  }

  it("reports unavailable when the substrate could not be reached, regardless of any stale data", () => {
    const coverage = releaseCoverage({ isError: true, releases: [] });
    expect(coverage.status).toBe("unavailable");
    expect(coverage.charters).toEqual([]);
  });

  it("reports empty — not a fixture — when arch.release genuinely returns zero rows", () => {
    const coverage = releaseCoverage({ isError: false, releases: [] });
    expect(coverage.status).toBe("empty");
    expect(coverage.charters).toEqual([]);
    // No invented release content leaks into the empty-state message.
    expect(coverage.message).not.toMatch(/Gemini|PR-256|fixture-release/);
  });

  it("renders unavailable and empty with distinguishable messages — 'could not ask' is not 'nothing there'", () => {
    const unavailable = releaseCoverage({ isError: true, releases: [] });
    const empty = releaseCoverage({ isError: false, releases: [] });
    expect(unavailable.message).not.toBe(empty.message);
    expect(unavailable.status).not.toBe(empty.status);
  });

  it("reads real arch.release charters and states full coverage when under the fetch limit", () => {
    const charter = releaseCharter({ id: "release-r2601" });
    const coverage = releaseCoverage({ isError: false, releases: [charter] });
    expect(coverage.status).toBe("data");
    expect(coverage.charters).toEqual([charter]);
    expect(coverage.message).toBe("1 of 1 charter(s) returned by the substrate.");
  });

  it("names the fetch limit rather than implying completeness when the query is saturated", () => {
    const charters = Array.from({ length: ALPHA_FETCH_LIMIT_PER_TYPE }, (_, i) =>
      releaseCharter({ id: `release-${i}`, content: { ...releaseCharter({ id: "x" }).content, ref: `R26.${i}` } }),
    );
    const coverage = releaseCoverage({ isError: false, releases: charters });
    expect(coverage.status).toBe("data");
    expect(coverage.message).toContain(`fetch limit ${ALPHA_FETCH_LIMIT_PER_TYPE}`);
    expect(coverage.message).not.toMatch(/^\d+ of \d+ charter/);
  });
});
