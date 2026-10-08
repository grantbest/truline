import { createRoutesFromElements, matchRoutes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { appRoutes } from "@/App";
import { navGroups } from "@/components/Layout";

// This is the test that would have caught the bug this bead exists to fix:
// ReleaseView.tsx and its route lived at /releases/:ref, but nothing
// registered a bare /releases and nothing in Layout.tsx's nav pointed at
// it, so App.tsx's catch-all silently swallowed the index. Neither half of
// that failure is visible from either file alone — a route with no nav
// entry is merely unreachable by menu, and a nav entry with no matching
// route resolves to the "*" fallback with no error. This file checks both
// halves together, and stays DOM-free: `createRoutesFromElements` walks the
// real JSX route tree App.tsx renders (no restructuring of it beyond
// exporting `appRoutes`), and `matchRoutes` is react-router's own matching
// logic — no render, no jsdom, matching this repo's existing test style
// (release-view.test.ts, dev-board.test.ts).
const routeObjects = createRoutesFromElements(appRoutes);

function deepestMatchedPath(pathname: string): string | undefined {
  const matches = matchRoutes(routeObjects, pathname);
  return matches?.[matches.length - 1]?.route.path;
}

describe("every nav entry resolves to the route it names, not the catch-all", () => {
  const navEntries = navGroups.flatMap((group) => group.items);

  it.each(navEntries)("$to ($label) resolves to itself, not /today or the \"*\" fallback", ({ to }) => {
    expect(deepestMatchedPath(to)).toBe(to);
  });
});

describe("/releases — the index route this bead adds", () => {
  it("is registered as its own route, distinct from /releases/:ref and the catch-all", () => {
    expect(deepestMatchedPath("/releases")).toBe("/releases");
    expect(deepestMatchedPath("/releases/R26.09")).toBe("/releases/:ref");
  });

  it("has a nav entry reachable from BOTH mobile surfaces navGroups drives — the desktop", () => {
    // Layout.tsx renders navGroups twice: once for the desktop <aside>
    // sidebar, once for the mobile "More" full-screen overlay drawer. Both
    // map over this same array directly (see Layout.tsx), so membership
    // here is reachability on both surfaces at once. The separate
    // `primaryMobileNav` bottom-bar shortlist is NOT checked — it is a
    // curated top-4 list this bead does not touch; the overlay is the
    // surface that carries /releases on a phone.
    const navPaths = navGroups.flatMap((group) => group.items.map((item) => item.to));
    expect(navPaths).toContain("/releases");
  });

  it("is registered before the \"*\" catch-all in App.tsx's route order", () => {
    const paths = routeObjects[0]?.children?.map((route) => route.path) ?? [];
    const releasesIndex = paths.indexOf("/releases");
    const catchAllIndex = paths.indexOf("*");
    expect(releasesIndex).toBeGreaterThanOrEqual(0);
    expect(catchAllIndex).toBeGreaterThanOrEqual(0);
    expect(releasesIndex).toBeLessThan(catchAllIndex);
  });
});
