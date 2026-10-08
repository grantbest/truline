import { describe, expect, it } from "vitest";
import type { Bead } from "@/types/bead";
import type { ArchReleaseContent } from "@/lib/ea-model";
import {
  outcomeDeliveryState,
  releaseCharterFrom,
  releaseDeliveryFrom,
  releaseViewAvailability,
  type ReleaseDeliveryOutcome,
  type ReleaseDeliveryResponse,
} from "@/lib/release-view";

function charterBead(overrides: Partial<ArchReleaseContent> = {}): Bead {
  const content: ArchReleaseContent = {
    ref: "R26.02",
    name: "The work says what it is for",
    objective: "You can open a release and see what it is for.",
    sprints: ["36", "37", "38"],
    outcomes: [],
    declared_balance: {},
    opened_at: "2026-09-03",
    ...overrides,
  };
  return {
    id: "release-bead-1",
    namespace: "arch",
    type: "release",
    state: "in_flight",
    parent_id: null,
    context: {},
    content: content as unknown as Record<string, unknown>,
    confidence: null,
    trust_tier: "user",
    provenance: {},
    created_by: "release-load",
    created_at: "2026-09-03T00:00:00Z",
    updated_at: "2026-09-03T00:00:00Z",
  };
}

describe("releaseDeliveryFrom — the served computation, interpreted", () => {
  it("reports ok with the passed-through fields when the server answers found", () => {
    const response: ReleaseDeliveryResponse = {
      status: "ok",
      found: true,
      ref: "R26.02",
      name: "The work says what it is for",
      outcomes: [],
      unclassified_delivering: [],
      balance: [],
      criteria: [],
    };
    const delivery = releaseDeliveryFrom(response, null);
    expect(delivery).toEqual({
      kind: "ok",
      ref: "R26.02",
      name: "The work says what it is for",
      outcomes: [],
      unclassifiedDelivering: [],
      balance: [],
      criteria: [],
    });
  });

  it("reports unknown, carrying the server's detail, when status is not ok — never as an empty result", () => {
    const response: ReleaseDeliveryResponse = {
      status: "unknown",
      detail: "factory-dispatcher checkout not found",
    };
    const delivery = releaseDeliveryFrom(response, null);
    expect(delivery).toEqual({ kind: "unknown", reason: "factory-dispatcher checkout not found" });
  });

  it("reports unknown when the request itself failed, distinct from a served unknown", () => {
    const delivery = releaseDeliveryFrom(undefined, new Error("mcp-hub 500 on /factory/release_delivery"));
    expect(delivery).toEqual({ kind: "unknown", reason: "mcp-hub 500 on /factory/release_delivery" });
  });

  it("reports not_found distinctly when the server found no charter for this ref", () => {
    const response: ReleaseDeliveryResponse = { status: "ok", found: false };
    expect(releaseDeliveryFrom(response, null)).toEqual({ kind: "not_found" });
  });

  it("reports not_served_here, distinct from unknown, when the server says the checkout was never configured (OPS-110)", () => {
    const response: ReleaseDeliveryResponse = {
      status: "unknown",
      code: "not_configured",
      detail: "scripts checkout not configured (set FACTORY_STATUS_REPO_ROOT)",
    };
    expect(releaseDeliveryFrom(response, null)).toEqual({
      kind: "not_served_here",
      reason: "scripts checkout not configured (set FACTORY_STATUS_REPO_ROOT)",
    });
  });

  it("still reports plain unknown when the server says the checkout is configured but unavailable", () => {
    const response: ReleaseDeliveryResponse = {
      status: "unknown",
      code: "unavailable",
      detail: "release charter directory not found: /some/path",
    };
    expect(releaseDeliveryFrom(response, null)).toEqual({
      kind: "unknown",
      reason: "release charter directory not found: /some/path",
    });
  });
});

describe("releaseCharterFrom — the declared prose, read once, never re-derived", () => {
  it("is unavailable, not empty, when the arch.release query errored", () => {
    const charter = releaseCharterFrom({
      ref: "R26.02",
      isLoading: false,
      isError: true,
      error: new Error("substrate unreachable"),
      releases: [],
    });
    expect(charter).toEqual({ kind: "unavailable", reason: "substrate unreachable" });
  });

  it("is not_found when the substrate answered but no bead matches this ref", () => {
    const charter = releaseCharterFrom({
      ref: "R26.02",
      isLoading: false,
      isError: false,
      error: null,
      releases: [charterBead({ ref: "R26.01" })],
    });
    expect(charter).toEqual({ kind: "not_found" });
  });

  it("is ok with the matching charter's content when a bead matches", () => {
    const bead = charterBead();
    const charter = releaseCharterFrom({
      ref: "R26.02",
      isLoading: false,
      isError: false,
      error: null,
      releases: [charterBead({ ref: "R26.01" }), bead],
    });
    expect(charter.kind).toBe("ok");
    if (charter.kind === "ok") expect(charter.charter.ref).toBe("R26.02");
  });
});

describe("outcomeDeliveryState — no delivering work vs. work in progress must never look alike", () => {
  function outcome(overrides: Partial<ReleaseDeliveryOutcome> = {}): ReleaseDeliveryOutcome {
    return {
      id: "O-3",
      statement: "You can open a release and see what it promised.",
      work_class: "feature",
      task_count: 0,
      tasks_by_state: {},
      ...overrides,
    };
  }

  it("classifies a zero-task outcome as no_work", () => {
    expect(outcomeDeliveryState(outcome())).toEqual({ kind: "no_work" });
  });

  it("classifies an outcome with tasks as delivering, carrying the state breakdown sorted by state name", () => {
    const state = outcomeDeliveryState(
      outcome({
        task_count: 2,
        tasks_by_state: { doing: ["t-2"], done: ["t-1"] },
      }),
    );
    expect(state).toEqual({
      kind: "delivering",
      byState: [
        ["doing", ["t-2"]],
        ["done", ["t-1"]],
      ],
    });
  });

  it("produces a different discriminant for no_work vs. delivering — this is the AC's own distinction", () => {
    const noWork = outcomeDeliveryState(outcome());
    const delivering = outcomeDeliveryState(outcome({ task_count: 1, tasks_by_state: { done: ["t-1"] } }));
    expect(noWork.kind).not.toBe(delivering.kind);
  });
});

describe("releaseViewAvailability — states how much of the estate was established", () => {
  it("is complete only when both the charter and the delivery computation succeeded", () => {
    const availability = releaseViewAvailability(
      { kind: "ok", charter: charterBead().content as unknown as ArchReleaseContent },
      { kind: "ok", ref: "R26.02", name: "x", outcomes: [], unclassifiedDelivering: [], balance: [], criteria: [] },
    );
    expect(availability).toEqual({
      complete: true,
      charterProblem: null,
      deliveryProblem: null,
      deliveryNotServedHere: false,
    });
  });

  it("names the charter failure specifically when only the charter read failed", () => {
    const availability = releaseViewAvailability(
      { kind: "unavailable", reason: "substrate unreachable" },
      { kind: "ok", ref: "R26.02", name: "x", outcomes: [], unclassifiedDelivering: [], balance: [], criteria: [] },
    );
    expect(availability.complete).toBe(false);
    expect(availability.charterProblem).toContain("substrate unreachable");
    expect(availability.deliveryProblem).toBeNull();
    expect(availability.deliveryNotServedHere).toBe(false);
  });

  it("names the delivery failure specifically when only the served computation failed", () => {
    const availability = releaseViewAvailability(
      { kind: "ok", charter: charterBead().content as unknown as ArchReleaseContent },
      { kind: "unknown", reason: "release charters could not be loaded" },
    );
    expect(availability.complete).toBe(false);
    expect(availability.charterProblem).toBeNull();
    expect(availability.deliveryProblem).toContain("release charters could not be loaded");
    expect(availability.deliveryNotServedHere).toBe(false);
  });

  it("never reports complete when both sides failed", () => {
    const availability = releaseViewAvailability(
      { kind: "not_found" },
      { kind: "not_found" },
    );
    expect(availability.complete).toBe(false);
    expect(availability.charterProblem).not.toBeNull();
    expect(availability.deliveryProblem).not.toBeNull();
  });

  it("flags deliveryNotServedHere, and still reports incomplete, when delivery is a permanent by-design absence (OPS-110)", () => {
    const availability = releaseViewAvailability(
      { kind: "ok", charter: charterBead().content as unknown as ArchReleaseContent },
      { kind: "not_served_here", reason: "scripts checkout not configured (set FACTORY_STATUS_REPO_ROOT)" },
    );
    expect(availability.complete).toBe(false);
    expect(availability.charterProblem).toBeNull();
    expect(availability.deliveryNotServedHere).toBe(true);
    expect(availability.deliveryProblem).toContain("not served in this environment");
    expect(availability.deliveryProblem).toContain("FACTORY_STATUS_REPO_ROOT");
  });
});
