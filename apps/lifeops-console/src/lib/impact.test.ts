import { describe, expect, it } from "vitest";
import { coverageRatioLabel, impactPanelStateFrom, impactViewFrom, type ImpactResponse } from "@/lib/impact";

function response(overrides: Partial<ImpactResponse> = {}): ImpactResponse {
  return {
    affected: { applications: [], cis: [], services: [], unknown_applications: [], lower_bound: false },
    context: { runs_on: { cis: [], services: [] } },
    coverage: {
      applications_assessed: 0,
      applications_total: 0,
      cis_owned: 0,
      cis_total: 0,
      changes_with_affects: 0,
      changes_total: 0,
      partial: false,
      partial_reads: [],
      failed_types: [],
    },
    computed_at: "2026-09-19T00:00:00+00:00",
    revision_hint: "test",
    ...overrides,
  };
}

describe("impactViewFrom", () => {
  it("reports unknown on a fetch error, never a stale/empty response", () => {
    const view = impactViewFrom(response(), new Error("network down"));
    expect(view).toEqual({ kind: "unknown", reason: "network down" });
  });

  it("reports unknown while the query has not resolved yet", () => {
    const view = impactViewFrom(undefined, undefined);
    expect(view.kind).toBe("unknown");
  });

  it("passes the served response through unchanged on success", () => {
    const served = response({ revision_hint: "abc123" });
    const view = impactViewFrom(served, undefined);
    expect(view).toEqual({ kind: "ok", response: served });
  });
});

describe("impactPanelStateFrom", () => {
  it("reports populated when anything was reached, even with nothing but an unknown application", () => {
    const withUnknown = response({
      affected: {
        applications: [],
        cis: [],
        services: [],
        unknown_applications: [{ id: "app-1", ref: "app.unknown", name: null }],
        lower_bound: true,
      },
    });
    expect(impactPanelStateFrom(withUnknown, "application").kind).toBe("populated");

    const withApp = response({
      affected: {
        applications: [{ id: "app-1", ref: "app.known", name: null }],
        cis: [],
        services: [],
        unknown_applications: [],
        lower_bound: false,
      },
    });
    expect(impactPanelStateFrom(withApp, "application").kind).toBe("populated");
  });

  it("never reports the verified-empty state when the read is partial, even with nothing reached", () => {
    const partial = response({
      coverage: {
        applications_assessed: 0,
        applications_total: 0,
        cis_owned: 0,
        cis_total: 0,
        changes_with_affects: 0,
        changes_total: 0,
        partial: true,
        partial_reads: ["arch.service: boom"],
        failed_types: ["service"],
      },
    });

    const state = impactPanelStateFrom(partial, "application");
    expect(state.kind).toBe("empty_partial");
    expect(state.kind === "empty_partial" && state.reasons).toEqual(["arch.service: boom"]);
    // Also true for a CI origin -- a partial read outranks the no-owner case.
    expect(impactPanelStateFrom(partial, "ci").kind).toBe("empty_partial");
  });

  it("never reports the verified-empty state for a CI origin with nothing reached — that is an unowned CI, not a confirmed empty result", () => {
    const empty = response();
    expect(impactPanelStateFrom(empty, "ci").kind).toBe("empty_unowned_ci");
  });

  it("reports verified-empty only for a non-CI origin with a complete read and nothing reached", () => {
    const empty = response();
    expect(impactPanelStateFrom(empty, "application").kind).toBe("empty_verified");
  });

  it("never counts context.runs_on toward populated -- a response with only context lists is still verified-empty", () => {
    // Required change 2, PR #1043 gate: what an application runs on is
    // context, not affected, so a response whose only non-empty lists are
    // context.runs_on must render exactly like a fully-empty response.
    const onlyContext = response({
      context: {
        runs_on: {
          cis: [{ id: "ci-1", ref: "ci.owned", name: null }],
          services: [{ id: "svc-1", ref: "svc.one", name: null }],
        },
      },
    });
    expect(impactPanelStateFrom(onlyContext, "application").kind).toBe("empty_verified");
  });
});

describe("coverageRatioLabel", () => {
  it("renders the plain ratio when this collection's read did not fail", () => {
    expect(coverageRatioLabel(13, 32, "application", [])).toBe("13/32");
    expect(coverageRatioLabel(13, 32, "application", ["ci"])).toBe("13/32");
  });

  it("never renders a failed collection's ratio as 0/0 — it is unknown, not verified empty", () => {
    expect(coverageRatioLabel(0, 0, "ci", ["ci"])).toBe("—/— (read failed)");
  });

  it("renders a failed collection as unknown even when it happens to carry non-zero numbers", () => {
    // A collection can fail partway through pagination and still return a
    // partial numerator/denominator -- failed_types names the read as
    // untrustworthy regardless of what numbers came back.
    expect(coverageRatioLabel(2, 5, "change", ["change"])).toBe("—/— (read failed)");
  });
});
