import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ReleaseCoverageCard } from "@/components/ReleaseCoverageCard";
import type { Bead } from "@/types/bead";

const FIXTURE_STRINGS = ["Gemini", "PR-256", "fixture-release-r2608", "merge-with-changes"];

function releaseCharter(overrides: Partial<Bead> & Pick<Bead, "id">): Bead {
  return {
    namespace: "arch",
    type: "release",
    state: "planned",
    parent_id: null,
    context: {},
    content: {
      ref: "R26.01",
      name: "Trust earns its amendment",
      objective: "Make the safety claims measurable rather than stated.",
      sprints: ["33", "34", "35"],
      outcomes: [{ id: "O-1", statement: "Every filed task names its release.", work_class: "enabling" }],
      declared_balance: {},
      opened_at: "2026-08-25",
    },
    confidence: null,
    trust_tier: "user",
    provenance: {},
    created_by: "release-load",
    created_at: "2026-08-25T00:00:00Z",
    updated_at: "2026-08-25T00:00:00Z",
    ...overrides,
  };
}

describe("ReleaseCoverageCard — unreachable substrate vs. empty result must never look alike", () => {
  it("renders an explicit unavailable state when the arch.release query errored", () => {
    const html = renderToStaticMarkup(<ReleaseCoverageCard isUnavailable releases={[]} />);
    expect(html).toMatch(/unavailable/i);
    expect(html).toMatch(/could not be reached|unknown/i);
    for (const forbidden of FIXTURE_STRINGS) expect(html).not.toContain(forbidden);
  });

  it("renders an explicit empty state — not fixture content — when the query genuinely returns zero rows", () => {
    const html = renderToStaticMarkup(<ReleaseCoverageCard isUnavailable={false} releases={[]} />);
    expect(html).toMatch(/0 returned|returned 0/i);
    for (const forbidden of FIXTURE_STRINGS) expect(html).not.toContain(forbidden);
  });

  it("renders unavailable and empty as distinguishable output", () => {
    const unavailableHtml = renderToStaticMarkup(<ReleaseCoverageCard isUnavailable releases={[]} />);
    const emptyHtml = renderToStaticMarkup(<ReleaseCoverageCard isUnavailable={false} releases={[]} />);
    expect(unavailableHtml).not.toBe(emptyHtml);
  });

  it("renders real arch.release charters with stated coverage when the substrate answers", () => {
    const html = renderToStaticMarkup(
      <ReleaseCoverageCard isUnavailable={false} releases={[releaseCharter({ id: "release-r2601" })]} />,
    );
    expect(html).toContain("R26.01");
    expect(html).toContain("Trust earns its amendment");
    expect(html).toMatch(/1 of 1/);
    for (const forbidden of FIXTURE_STRINGS) expect(html).not.toContain(forbidden);
  });

  it("shows a loading state that asserts neither emptiness nor unavailability", () => {
    const html = renderToStaticMarkup(<ReleaseCoverageCard isLoading />);
    expect(html).toMatch(/loading/i);
    expect(html).not.toMatch(/0 returned|unavailable/i);
  });
});
