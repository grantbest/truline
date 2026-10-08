import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ImpactSummary } from "@/routes/Architecture";
import type { ImpactResponse } from "@/lib/impact";
import type { BlastRadiusEntities } from "@/lib/blast-radius";

// ImpactSummary is the purely-presentational half of BlastRadiusPanel's
// served-answer branch, split out precisely so these three console-honesty
// fixes (round-2 #1024 gate: lower_bound notice, the unknown-applications
// caption, and never rendering a failed collection as "0/0") can be
// exercised directly against synthetic ImpactResponse fixtures, with no
// QueryClient or network involved.

const EMPTY_ENTITIES: BlastRadiusEntities = { applications: [], services: [], cis: [], capabilities: [] };

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

describe("ImpactSummary — lower-bound notice", () => {
  it("renders a lower-bound notice when affected.lower_bound is true", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response({
          affected: {
            applications: [],
            cis: [],
            services: [],
            unknown_applications: [{ id: "app-1", ref: "app.unknown", name: "Unknown App" }],
            lower_bound: true,
          },
        })}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    expect(html).toMatch(/lower bound/i);
  });

  it("renders no lower-bound notice when affected.lower_bound is false", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response({
          affected: {
            applications: [{ id: "app-1", ref: "app.known", name: "Known App" }],
            cis: [],
            services: [],
            unknown_applications: [],
            lower_bound: false,
          },
        })}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    expect(html).not.toMatch(/lower bound/i);
  });
});

describe("ImpactSummary — unknown-applications caption", () => {
  it("never claims the unknown application's own edges are included in affected", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response({
          affected: {
            applications: [],
            cis: [],
            services: [],
            unknown_applications: [{ id: "app-1", ref: "app.unknown", name: "Unknown App" }],
            lower_bound: true,
          },
        })}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    // PR #1043 gate (required change 2): the old caption said these
    // applications' "own CI/service edges are already included above" --
    // true when a reached application's forward edges still counted toward
    // affected, false now that they are context only. The corrected caption
    // must not repeat that claim, and must still say the unassessed posture
    // is a lower bound, never a confirmed absence of further impact.
    expect(html).not.toMatch(/already included above/i);
    expect(html).toMatch(/never counted as unaffected/i);
  });
});

describe("ImpactSummary — context.runs_on is never rendered as affected", () => {
  it("renders a response whose only non-empty lists are context lists as not-affected", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response({
          affected: { applications: [], cis: [], services: [], unknown_applications: [], lower_bound: false },
          context: {
            runs_on: {
              cis: [{ id: "ci-1", ref: "ci.owned", name: "Owned CI" }],
              services: [{ id: "svc-1", ref: "svc.one", name: "Service One" }],
            },
          },
        })}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    // Required change 2 (PR #1043 gate): impactPanelStateFrom must not count
    // context.runs_on toward "populated" -- nothing was reached in affected,
    // so this must still read as verified-empty, never as affected.
    expect(html).toMatch(/nothing in the graph is affected/i);
    // The context entries are still shown, but labeled as context, not affected.
    expect(html).toMatch(/runs on \(context, not affected\)/i);
    expect(html).toMatch(/Owned CI/);
    expect(html).toMatch(/Service One/);
  });
});

describe("ImpactSummary — failed-collection totals never render as 0/0", () => {
  it("renders a failed collection's ratio as a read-failed marker, not 0/0", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response({
          coverage: {
            applications_assessed: 0,
            applications_total: 0,
            cis_owned: 3,
            cis_total: 5,
            changes_with_affects: 0,
            changes_total: 0,
            partial: true,
            partial_reads: ["arch.application: boom"],
            failed_types: ["application"],
          },
        })}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    expect(html).toMatch(/apps.*read failed/i);
    expect(html).not.toMatch(/apps 0\/0/i);
    // The CI ratio did not fail and must still render as a plain number.
    expect(html).toMatch(/CIs 3\/5/);
  });

  it("renders a genuinely-empty (not failed) collection as a real 0/0", () => {
    const html = renderToStaticMarkup(
      <ImpactSummary
        response={response()}
        originKind="application"
        entities={EMPTY_ENTITIES}
        onSelect={() => {}}
      />,
    );
    expect(html).toMatch(/apps 0\/0/);
    expect(html).not.toMatch(/read failed/i);
  });
});
