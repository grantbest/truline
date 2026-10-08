import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { ArchReleaseContent, ReleaseOutcome } from "@/lib/ea-model";
import type { Bead, DevTaskContent, DevTaskState } from "@/types/bead";
import { classifyOutcome, type ReleaseIndexRow } from "@/lib/release-index";
import {
  LinksUnavailableBanner,
  ReleaseIndexCard,
  releaseIndexHasLoadedData,
  releaseIndexInitialError,
  releaseIndexIsLoading,
  releaseIndexLinksUnavailable,
} from "@/routes/ReleaseIndex";

// The render test that would have caught the requeued defect: an unresolved
// or failed linksQuery must never render every outcome as "declared only" —
// a confident zero for a number this page could not compute. Follows
// ReleaseView.test.tsx's pattern of exercising exported presentational
// pieces directly with renderToStaticMarkup, no DOM, no query mocking.

function query<T>(overrides: Partial<{ data: T | undefined; isLoading: boolean; isError: boolean }> = {}) {
  return { data: undefined, isLoading: false, isError: false, ...overrides };
}

describe("release index load/error gating — linksQuery must never be left out", () => {
  it("stays loading once charters and tasks land but links have not resolved yet, when links are needed", () => {
    const charters = query<Bead[]>({ data: [{ id: "R26.09" } as Bead] });
    const tasks = query<Bead[]>({ data: [] });
    const links = query<unknown>(); // unresolved: no data, not loading, not errored (e.g. still enabling)
    const linksStillLoading = { ...links, isLoading: true };

    expect(releaseIndexHasLoadedData(charters, tasks, linksStillLoading, true)).toBe(false);
    expect(releaseIndexIsLoading(charters, tasks, linksStillLoading, true)).toBe(true);
  });

  it("does not wait on linksQuery when there are no open charters to look delivery up for", () => {
    const charters = query<Bead[]>({ data: [] });
    const tasks = query<Bead[]>({ data: [] });
    const links = query<unknown>(); // never enabled, will never settle

    expect(releaseIndexHasLoadedData(charters, tasks, links, false)).toBe(true);
    expect(releaseIndexIsLoading(charters, tasks, links, false)).toBe(false);
  });

  it("resolves out of loading once linksQuery succeeds", () => {
    const charters = query<Bead[]>({ data: [{ id: "R26.09" } as Bead] });
    const tasks = query<Bead[]>({ data: [] });
    const links = query<unknown>({ data: [] });

    expect(releaseIndexHasLoadedData(charters, tasks, links, true)).toBe(true);
    expect(releaseIndexIsLoading(charters, tasks, links, true)).toBe(false);
  });

  it("a links-only failure is NOT an initial error (charters and tasks are still known-good)", () => {
    const charters = query<Bead[]>({ data: [{ id: "R26.09" } as Bead] });
    const tasks = query<Bead[]>({ data: [] });
    const links = query<unknown>({ isError: true });

    expect(releaseIndexHasLoadedData(charters, tasks, links, true)).toBe(true);
    expect(releaseIndexInitialError(charters, tasks, links, true)).toBe(false);
    expect(releaseIndexLinksUnavailable(links, true)).toBe(true);
  });

  it("a charters or tasks failure is an initial error regardless of links", () => {
    const charters = query<Bead[]>({ isError: true });
    const tasks = query<Bead[]>({ data: [] });
    const links = query<unknown>();

    expect(releaseIndexInitialError(charters, tasks, links, false)).toBe(true);
  });

  it("linksUnavailable is false whenever links are not needed, even if the query object reports an error", () => {
    const links = query<unknown>({ isError: true });
    expect(releaseIndexLinksUnavailable(links, false)).toBe(false);
  });
});

function bead(overrides: Partial<Bead> & Pick<Bead, "id">): Bead {
  return {
    namespace: "dev",
    type: "task",
    state: "pending",
    parent_id: null,
    context: {},
    content: {},
    confidence: null,
    trust_tier: "system",
    provenance: {},
    created_by: "test",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

function releaseCharterBead(id: string, state: string, contentOverrides: Partial<ArchReleaseContent> = {}): Bead {
  const content: ArchReleaseContent = {
    ref: id,
    name: `Charter ${id}`,
    objective: "Objective.",
    sprints: [],
    outcomes: [],
    declared_balance: {},
    opened_at: "2026-08-25T00:00:00Z",
    ...contentOverrides,
  };
  return bead({ id, namespace: "arch", type: "release", state, content: content as unknown as Record<string, unknown> });
}

function taskBead(id: string, state: DevTaskState, contentOverrides: Partial<DevTaskContent> = {}): Bead {
  const content: DevTaskContent = {
    lane: "feature",
    title: `Task ${id}`,
    intent: "Some intent.",
    context_refs: [],
    acceptance: ["Some acceptance."],
    verification: { commands: ["npm test"] },
    scope: { paths: ["apps/lifeops-console/src/"], forbidden_paths: [".github/workflows/**"] },
    risk_class: "behavioral",
    budget: { max_agent_minutes: 30, max_usd: 2, max_tokens: 250000 },
    autonomy: "propose",
    ...contentOverrides,
  };
  return bead({ id, state, content: content as unknown as Record<string, unknown> });
}

function outcome(id: string, statement = "Statement."): ReleaseOutcome {
  return { id, statement, work_class: "feature" };
}

function rowFixture(): ReleaseIndexRow {
  const charter = releaseCharterBead("R26.09", "in_flight", {
    outcomes: [outcome("O-1"), outcome("O-2"), outcome("O-3")],
  });
  const doneTask = taskBead("t-done", "done", { outcome_ref: "O-1" });
  const doingTask = taskBead("t-doing", "doing", { outcome_ref: "O-2" });
  const outcomes = [
    classifyOutcome(outcome("O-1"), [doneTask]),
    classifyOutcome(outcome("O-2"), [doingTask]),
    classifyOutcome(outcome("O-3"), []),
  ];
  const counts = { complete: 1, in_progress: 1, declared_only: 1 };
  return {
    charter,
    content: {
      ref: "R26.09",
      name: "Charter R26.09",
      objective: "Objective.",
      sprints: [],
      outcomes: [outcome("O-1"), outcome("O-2"), outcome("O-3")],
      declared_balance: {},
      opened_at: "2026-08-25T00:00:00Z",
    },
    outcomes,
    counts,
  };
}

// ReleaseIndexCard's title links to /releases/:ref, so it needs a router
// context to render server-side — mirrors how any react-router-dom <Link>
// must be rendered under a Router in a DOM-free renderToStaticMarkup test.
function renderCard(row: ReleaseIndexRow, linksUnavailable: boolean) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ReleaseIndexCard row={row} linksUnavailable={linksUnavailable} />
    </MemoryRouter>,
  );
}

describe("ReleaseIndexCard / LinksUnavailableBanner — a failed links read must never render as a claim", () => {
  it("renders real counts and per-outcome groups when links are available", () => {
    const html = renderCard(rowFixture(), false);
    expect(html).toContain("1 complete");
    expect(html).toContain("1 in progress");
    expect(html).toContain("1 declared only");
    expect(html).not.toContain("delivery unknown");
  });

  it("never renders '0 complete / 0 in progress / N declared only' when links are unavailable", () => {
    const html = renderCard(rowFixture(), true);
    expect(html).not.toContain("complete");
    expect(html).not.toContain("in progress");
    expect(html).not.toContain("declared only");
    expect(html).toContain("delivery unknown");
    expect(html).toContain("unknown");
  });

  it("produces different rendered output for the available and unavailable states", () => {
    const row = rowFixture();
    const available = renderCard(row, false);
    const unavailable = renderCard(row, true);
    expect(available).not.toBe(unavailable);
  });

  it("LinksUnavailableBanner renders nothing when links are available", () => {
    expect(renderToStaticMarkup(<LinksUnavailableBanner unavailable={false} />)).toBe("");
  });

  it("LinksUnavailableBanner names the problem and warns the counts are unknown, not zero", () => {
    const html = renderToStaticMarkup(<LinksUnavailableBanner unavailable />);
    expect(html).toContain("unknown, not zero");
  });
});
