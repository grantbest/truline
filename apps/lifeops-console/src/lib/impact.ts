// Types and pure projection for the served impact answer -- POST
// /api/v1/factory/impact (apps/mcp-hub/src/tools/impact.py, R26.09/O-4,
// PC-ASR-002/AC-1). Mirrors that module's response dict field-for-field, the
// same "never re-derive the server's shape" discipline factory-status.ts
// already keeps for the schedule/queue/worker panels.
//
// This is deliberately a different question from blast-radius.ts's
// `blastRadius()`: that is reverse reachability ("what depends on this, and
// breaks if it dies"); this is the literal forward chain the metamodel's
// edge vocabulary names -- change --affects--> application --depends_on-->
// ci, application --consumes--> service. See apps/mcp-hub/src/tools/
// impact.py's own docstring and .factory/design.md for the full design
// record. `kind` has no "service" member for the same reason: the metamodel
// gives services no outgoing edge to walk forward from, so the Architecture
// panel keeps blast-radius.ts's client-side walk for a service origin.

export type ImpactKind = "change" | "application" | "ci";

export interface ImpactRef {
  id: string;
  ref: string | null;
  name: string | null;
}

export interface ImpactCoverage {
  applications_assessed: number;
  applications_total: number;
  cis_owned: number;
  cis_total: number;
  changes_with_affects: number;
  changes_total: number;
  partial: boolean;
  partial_reads: string[];
  /** Which of this request's own bead-type collections (application/ci/
   * change/service) failed or was refused outright -- distinct from
   * `partial_reads` (a human-readable log line meant for a tooltip): this is
   * what decides whether a coverage ratio is a real "0/0" or a failed read
   * that must never be rendered as one (round-2 #1024 gate finding). */
  failed_types: string[];
}

export interface ImpactAffected {
  applications: ImpactRef[];
  /** Always empty under the current edge vocabulary -- no origin kind
   * directly targets a CI/service the way an `affects` edge directly targets
   * an application. What a reached application itself runs on is context,
   * not affected; see `ImpactContext.runs_on` (PR #1043 gate,
   * apps/mcp-hub/src/tools/impact.py's own docstring). Kept in the response
   * shape for a future edge type that would populate it directly. */
  cis: ImpactRef[];
  services: ImpactRef[];
  /** Reached via an assessed edge (an outgoing `affects`, or a `depends_on`
   * edge from another affected application) but whose OWN depends_on posture
   * is unknown -- real, proven-affected information that must never be
   * folded into `applications` (its downstream footprint cannot be
   * expanded) nor dropped from the response (R26.09/O-4: "an unassessed
   * application reads as unknown, never as unaffected"). */
  unknown_applications: ImpactRef[];
  /** True whenever an unknown-posture application contributed to the
   * application closure above: its own dependency posture is not assessed,
   * so the true set of affected applications may extend further than what
   * has been recorded for it so far. About the application walk only --
   * `context.runs_on` carries no completeness claim of its own (direct,
   * one-hop, never-chained context, not a walked closure). */
  lower_bound: boolean;
}

/** What a reached application itself runs on -- its own outgoing
 * `depends_on`/`consumes` edges to CIs/services. Real, one-hop information,
 * but NOT part of the blast radius: what X depends on is not affected by a
 * change to X (PR #1043 gate, 2026-09-25T20:30Z outer-loop answer). Rendered
 * separately from `affected`, never counted toward it. */
export interface ImpactContext {
  runs_on: {
    cis: ImpactRef[];
    services: ImpactRef[];
  };
}

export interface ImpactResponse {
  affected: ImpactAffected;
  context: ImpactContext;
  coverage: ImpactCoverage;
  computed_at: string;
  revision_hint: string;
}

export type ImpactView =
  | { kind: "unknown"; reason: string }
  | { kind: "ok"; response: ImpactResponse };

/** What the Architecture panel should render for an empty-looking answer.
 * `totalAffected === 0` alone is NOT "verified nothing is affected" -- it is
 * also what a failed/refused read looks like, and what a CI with no
 * recorded owning application looks like (R26.09/O-4's "unknown, never
 * unaffected" extended to CI ownership: an owner-less CI may mean the
 * ownership edge was never captured, not that nothing depends on it). The
 * panel must never collapse those into the same "Nothing in the graph is
 * affected by this." claim -- this function names which case applies so the
 * component only has to render it. */
export type ImpactPanelState =
  | { kind: "populated" }
  | { kind: "empty_partial"; reasons: string[] }
  | { kind: "empty_unowned_ci" }
  | { kind: "empty_verified" };

export function impactPanelStateFrom(
  response: ImpactResponse,
  originKind: "application" | "ci",
): ImpactPanelState {
  const { affected, coverage } = response;
  const totalAffected =
    affected.applications.length + affected.cis.length + affected.services.length;
  const nothingReached = totalAffected === 0 && affected.unknown_applications.length === 0;

  if (!nothingReached) return { kind: "populated" };
  if (coverage.partial) return { kind: "empty_partial", reasons: coverage.partial_reads };
  if (originKind === "ci") return { kind: "empty_unowned_ci" };
  return { kind: "empty_verified" };
}

export type CoverageCollectionType = "application" | "ci" | "change";

/** A coverage ratio's display text — "N/M" normally, or an explicit
 * read-failed marker when this ratio's own collection is named in
 * `failed_types`. A collection that failed to read is not "0" of anything;
 * rendering "0/0" for it would read as a verified-empty measurement instead
 * of a read that never happened (round-2 #1024 gate finding). */
export function coverageRatioLabel(
  numerator: number,
  denominator: number,
  type: CoverageCollectionType,
  failedTypes: readonly string[],
): string {
  if (failedTypes.includes(type)) return "—/— (read failed)";
  return `${numerator}/${denominator}`;
}

export function impactViewFrom(
  response: ImpactResponse | undefined,
  fetchError: unknown,
): ImpactView {
  if (fetchError) {
    return {
      kind: "unknown",
      reason: fetchError instanceof Error ? fetchError.message : "impact request failed",
    };
  }
  if (!response) {
    return { kind: "unknown", reason: "impact has not loaded yet" };
  }
  return { kind: "ok", response };
}
