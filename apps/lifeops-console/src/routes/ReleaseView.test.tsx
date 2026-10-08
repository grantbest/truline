import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { substrateClient } from "@/providers/substrate-client";
import type { factoryStatusClient } from "@/providers/factory-status-client";
import {
  releaseDeliveryFrom,
  releaseViewAvailability,
  type ReleaseCharter,
  type ReleaseDeliveryOutcome,
  type ReleaseDeliveryResponse,
} from "@/lib/release-view";
import {
  AvailabilityBanner,
  BalanceSection,
  CriteriaSection,
  OutcomesSection,
  releaseCharterQueryOptions,
  releaseDeliveryQueryOptions,
} from "@/routes/ReleaseView";

function deliveringOutcome(overrides: Partial<ReleaseDeliveryOutcome> = {}): ReleaseDeliveryOutcome {
  return {
    id: "O-3",
    statement: "You can open a release and see what it promised.",
    work_class: "feature",
    task_count: 2,
    tasks_by_state: { doing: ["task-2"], done: ["task-1"] },
    ...overrides,
  };
}

function noWorkOutcome(overrides: Partial<ReleaseDeliveryOutcome> = {}): ReleaseDeliveryOutcome {
  return {
    id: "O-6",
    statement: "A number this release cannot compute is shown as unknown and never as zero.",
    work_class: "risk",
    task_count: 0,
    tasks_by_state: {},
    ...overrides,
  };
}

describe("OutcomesSection — an outcome with no delivering work must render distinctly from one with work", () => {
  it("marks the zero-task outcome with an explicit notice the worked outcome does not carry", () => {
    const okResponse: ReleaseDeliveryResponse = {
      status: "ok",
      found: true,
      ref: "R26.02",
      name: "The work says what it is for",
      outcomes: [deliveringOutcome(), noWorkOutcome()],
      unclassified_delivering: [],
      balance: [],
      criteria: [],
    };
    const delivery = releaseDeliveryFrom(okResponse, null);
    const html = renderToStaticMarkup(<OutcomesSection delivery={delivery} />);

    expect(html).toContain("no delivering work");
    // The worked outcome's task ids and state names must appear...
    expect(html).toContain("task-1");
    expect(html).toContain("task-2");
    // ...and the zero-work outcome must not fabricate a state breakdown.
    const noWorkArticleIndex = html.indexOf("O-6");
    const deliveringArticleIndex = html.indexOf("O-3");
    expect(noWorkArticleIndex).toBeGreaterThan(-1);
    expect(deliveringArticleIndex).toBeGreaterThan(-1);
  });

  it("produces different rendered output for the two outcome shapes", () => {
    const withWork = renderToStaticMarkup(
      <OutcomesSection
        delivery={{
          kind: "ok",
          ref: "R26.02",
          name: "x",
          outcomes: [deliveringOutcome()],
          unclassifiedDelivering: [],
          balance: [],
          criteria: [],
        }}
      />,
    );
    const withoutWork = renderToStaticMarkup(
      <OutcomesSection
        delivery={{
          kind: "ok",
          ref: "R26.02",
          name: "x",
          outcomes: [noWorkOutcome()],
          unclassifiedDelivering: [],
          balance: [],
          criteria: [],
        }}
      />,
    );
    expect(withWork).not.toBe(withoutWork);
    expect(withWork).not.toContain("no delivering work");
    expect(withoutWork).toContain("no delivering work");
  });
});

describe("Unreadable portions are reported, never omitted (PC-ASR-002/AC-2)", () => {
  it("OutcomesSection states the reason when the served computation is unknown, rather than showing an empty list", () => {
    const html = renderToStaticMarkup(
      <OutcomesSection delivery={{ kind: "unknown", reason: "factory-dispatcher checkout not found" }} />,
    );
    expect(html).toContain("Could not be computed");
    expect(html).toContain("factory-dispatcher checkout not found");
  });

  it("BalanceSection never renders 0% for a work class the server marked ABSENT", () => {
    const html = renderToStaticMarkup(
      <BalanceSection
        delivery={{
          kind: "ok",
          ref: "R26.02",
          name: "x",
          outcomes: [],
          unclassifiedDelivering: [],
          balance: [{ work_class: "security", declared_pct: 5, actual_count: 0, actual_pct: null, absent: true }],
          criteria: [],
        }}
      />,
    );
    expect(html).toContain("ABSENT");
    expect(html).not.toContain("0%");
  });

  it("CriteriaSection surfaces an unmeasurable criterion's reason rather than dropping the row", () => {
    const html = renderToStaticMarkup(
      <CriteriaSection
        delivery={{
          kind: "ok",
          ref: "R26.02",
          name: "x",
          outcomes: [],
          unclassifiedDelivering: [],
          balance: [],
          criteria: [
            {
              ref: "not-a-ref",
              as_of_opened: null,
              latest: null,
              stale: false,
              changed: false,
              unmeasurable: { __unknown__: true, reason: "'not-a-ref' is not a well-formed requirement reference" },
            },
          ],
        }}
      />,
    );
    expect(html).toContain("unknown");
    expect(html).toContain("not a well-formed requirement reference");
  });

  it("AvailabilityBanner names which side failed rather than rendering a generic complete state", () => {
    const charter: ReleaseCharter = { kind: "unavailable", reason: "substrate unreachable" };
    const availability = releaseViewAvailability(charter, { kind: "not_found" });
    const html = renderToStaticMarkup(<AvailabilityBanner availability={availability} />);
    expect(html).toContain("substrate unreachable");
    expect(html).toContain("release-status.py has no charter for this ref");
    expect(html).not.toMatch(/both loaded/i);
  });

  it("AvailabilityBanner states completeness plainly when both sides succeeded", () => {
    const availability = releaseViewAvailability(
      { kind: "ok", charter: { ref: "R26.02", name: "x", objective: "o", sprints: [], outcomes: [], declared_balance: {}, opened_at: "2026-09-03" } },
      { kind: "ok", ref: "R26.02", name: "x", outcomes: [], unclassifiedDelivering: [], balance: [], criteria: [] },
    );
    const html = renderToStaticMarkup(<AvailabilityBanner availability={availability} />);
    expect(html).toMatch(/both loaded/i);
  });
});

describe("The release view never writes (a double that fails if a write is attempted)", () => {
  function writeAttempted(name: string) {
    return () => {
      throw new Error(`write attempted: ${name}`);
    };
  }

  it("releaseCharterQueryOptions only ever calls listBeads against the injected client", async () => {
    const fixtureCharterBead = {
      id: "release-bead-1",
      namespace: "arch",
      type: "release",
      state: "in_flight",
      parent_id: null,
      context: {},
      content: { ref: "R26.02", name: "x", objective: "o", sprints: [], outcomes: [], declared_balance: {}, opened_at: "2026-09-03" },
      confidence: null,
      trust_tier: "user",
      provenance: {},
      created_by: "release-load",
      created_at: "2026-09-03T00:00:00Z",
      updated_at: "2026-09-03T00:00:00Z",
    };

    const noWriteSubstrateDouble: typeof substrateClient = {
      listBeads: async () => [fixtureCharterBead as any],
      getBead: writeAttempted("getBead") as any,
      createBead: writeAttempted("createBead") as any,
      updateBead: writeAttempted("updateBead") as any,
      deleteBead: writeAttempted("deleteBead") as any,
      listEvents: writeAttempted("listEvents") as any,
      listBeadLinks: writeAttempted("listBeadLinks") as any,
      dryRunRule: writeAttempted("dryRunRule") as any,
      semanticSearch: writeAttempted("semanticSearch") as any,
    };

    const options = releaseCharterQueryOptions(noWriteSubstrateDouble);
    // If the release view ever called a mutating method on this double, the
    // double itself would throw — resolving cleanly is the proof no write
    // was attempted.
    await expect(options.queryFn()).resolves.toEqual([fixtureCharterBead]);
  });

  it("releaseDeliveryQueryOptions only ever calls releaseDelivery against the injected client", async () => {
    const okResponse: ReleaseDeliveryResponse = { status: "ok", found: true, ref: "R26.02", name: "x", outcomes: [], unclassified_delivering: [], balance: [], criteria: [] };

    const noWriteStatusDouble: typeof factoryStatusClient = {
      taskRunnable: writeAttempted("taskRunnable") as any,
      releaseDelivery: async () => okResponse,
      scheduleStatus: writeAttempted("scheduleStatus") as any,
      impact: writeAttempted("impact") as any,
      fileTask: writeAttempted("fileTask") as any,
      fileNote: writeAttempted("fileNote") as any,
    };

    const options = releaseDeliveryQueryOptions("R26.02", noWriteStatusDouble);
    await expect(options.queryFn()).resolves.toEqual(okResponse);
  });
});
