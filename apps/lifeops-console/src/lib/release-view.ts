import type { Bead } from "@/types/bead";
import { releaseContent, type ArchReleaseContent, type WorkClass } from "@/lib/ea-model";

// Pure projection helpers for the release view (R26.02/O-3, PC-ASR-002).
// Kept out of the route component so the honesty-clause logic — what counts
// as "could not be established", and how that stays visible next to the
// numbers that did compute — is testable without a DOM.
//
// Two independent reads back this view, and each can fail on its own:
//   - the `arch.release` charter bead, fetched the same way
//     ReleaseCoverageCard/FactoryBoard/ProjectAlpha already do, for the
//     declared prose (objective, sprints, opened_at) — a declared-field
//     read, the same category dev-board.ts's releaseBadgeFor already
//     treats as safe to do locally.
//   - `POST /api/v1/factory/release_delivery` (mcp-hub's
//     tools.factory_status.release_delivery, which imports and calls
//     scripts/release-status.py's build_release_report verbatim — never
//     re-derived here) for every *computed* number: outcome/task-state
//     breakdown, declared-vs-actual balance, and per-criterion verdicts.
// Neither read's failure may be silently dropped in favor of the other's
// success — see releaseViewAvailability below.

// -----------------------------------------------------------------------
// Wire shape — mirrors apps/mcp-hub/src/tools/factory_status.py::
// release_delivery's return dict exactly. `status: "unknown"` means the
// authority itself could not be reached (no checkout, no substrate);
// `found: false` means release-status.py has no charter for this ref.
//
// `code` (OPS-110) distinguishes *why* it's unknown, because the two causes
// read differently to a viewer: `"not_configured"` is the permanent,
// by-design production state — this service's image never ships the
// factory-dispatcher/scripts checkout, so there is nothing wrong to retry —
// while `"unavailable"` (or its absence, from a pre-OPS-110 server) is a
// checkout that is configured but not reachable right now, which is the
// shape a real incident takes. See releaseDeliveryFrom below.
// -----------------------------------------------------------------------

export interface ReleaseDeliveryOutcome {
  id: string;
  statement: string;
  work_class: WorkClass;
  task_count: number;
  tasks_by_state: Record<string, string[]>;
}

export interface ReleaseDeliveryBalanceEntry {
  work_class: WorkClass;
  declared_pct: number;
  actual_count: number;
  // null means ABSENT: declared at a non-zero share, nothing delivered.
  // Distinct from 0.0, which is a real, verified zero share.
  actual_pct: number | null;
  absent: boolean;
}

// `scripts/unknown.py::Unknown.to_jsonable()`'s shape — "could not be
// computed," never collapsed into null/0/"" on the wire.
export interface UnknownMarker {
  __unknown__: true;
  reason: string;
}

export interface ReleaseDeliveryCriterion {
  ref: string;
  as_of_opened: string | null;
  latest: string | null;
  stale: boolean;
  changed: boolean;
  unmeasurable: UnknownMarker | null;
}

export interface ReleaseDeliveryResponse {
  status: "ok" | "unknown";
  code?: "not_configured" | "unavailable";
  found?: boolean;
  detail?: string;
  ref?: string;
  name?: string;
  outcomes?: ReleaseDeliveryOutcome[];
  unclassified_delivering?: string[];
  balance?: ReleaseDeliveryBalanceEntry[];
  criteria?: ReleaseDeliveryCriterion[];
}

// -----------------------------------------------------------------------
// The served computation, interpreted. This is the only place the view
// inspects `status`/`found` on the wire response — everything else (a
// component deciding how to render an outcome, a balance row, a
// criterion) reads the passed-through fields, never the wire shape
// directly, mirroring taskRunnabilityFrom's rule in dev-board.ts.
// -----------------------------------------------------------------------

export type ReleaseDelivery =
  | { kind: "unknown"; reason: string }
  // The checkout-only capability's permanent, by-design absence in this
  // deployment (server's code: "not_configured") — never a request that
  // failed, so it must never render with the same "could not reach it,
  // maybe retry" tone as `unknown` (OPS-110; see AvailabilityBanner).
  | { kind: "not_served_here"; reason: string }
  | { kind: "not_found" }
  | {
      kind: "ok";
      ref: string;
      name: string;
      outcomes: ReleaseDeliveryOutcome[];
      unclassifiedDelivering: string[];
      balance: ReleaseDeliveryBalanceEntry[];
      criteria: ReleaseDeliveryCriterion[];
    };

export function releaseDeliveryFrom(
  response: ReleaseDeliveryResponse | undefined,
  fetchError: unknown,
): ReleaseDelivery {
  if (fetchError) {
    return {
      kind: "unknown",
      reason: fetchError instanceof Error ? fetchError.message : "release delivery request failed",
    };
  }
  if (!response) {
    return { kind: "unknown", reason: "no response received" };
  }
  if (response.status !== "ok") {
    const reason = response.detail?.trim() || "release delivery is unknown";
    if (response.code === "not_configured") {
      return { kind: "not_served_here", reason };
    }
    return { kind: "unknown", reason };
  }
  if (response.found !== true) {
    return { kind: "not_found" };
  }
  return {
    kind: "ok",
    ref: response.ref ?? "",
    name: response.name ?? "",
    outcomes: response.outcomes ?? [],
    unclassifiedDelivering: response.unclassified_delivering ?? [],
    balance: response.balance ?? [],
    criteria: response.criteria ?? [],
  };
}

// -----------------------------------------------------------------------
// The charter's declared prose — a plain read of the arch.release bead's
// content, never the source of a computed number (see module comment).
// -----------------------------------------------------------------------

export type ReleaseCharter =
  | { kind: "loading" }
  | { kind: "unavailable"; reason: string }
  | { kind: "not_found" }
  | { kind: "ok"; charter: ArchReleaseContent };

export function releaseCharterFrom(params: {
  ref: string;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  releases: Bead[];
}): ReleaseCharter {
  if (params.isLoading) return { kind: "loading" };
  if (params.isError) {
    return {
      kind: "unavailable",
      reason: params.error instanceof Error ? params.error.message : "arch.release could not be reached",
    };
  }
  const match = params.releases.find((bead) => releaseContent(bead).ref === params.ref);
  if (!match) return { kind: "not_found" };
  return { kind: "ok", charter: releaseContent(match) };
}

// -----------------------------------------------------------------------
// Outcome delivery — the "no work vs. work is done" distinction the AC
// names explicitly. A pure classification so the component never has to
// re-decide what "no delivering work" means.
// -----------------------------------------------------------------------

export type OutcomeDeliveryState =
  | { kind: "no_work" }
  | { kind: "delivering"; byState: [state: string, taskIds: string[]][] };

export function outcomeDeliveryState(outcome: ReleaseDeliveryOutcome): OutcomeDeliveryState {
  if (outcome.task_count === 0) return { kind: "no_work" };
  const byState = Object.entries(outcome.tasks_by_state).sort(([a], [b]) => a.localeCompare(b));
  return { kind: "delivering", byState };
}

// -----------------------------------------------------------------------
// Top-of-page availability — states how much of the estate this view was
// able to establish, in one place, so a page that got one source but not
// the other reads as partial rather than complete (PC-ASR-002/AC-2).
// -----------------------------------------------------------------------

export interface ReleaseViewAvailability {
  complete: boolean;
  charterProblem: string | null;
  deliveryProblem: string | null;
  // True only for the permanent, by-design absence (OPS-110) — distinct from
  // every other `deliveryProblem`, which is a real failure that could clear
  // on retry. Lets the banner render a fact rather than a fault.
  deliveryNotServedHere: boolean;
}

export function releaseViewAvailability(
  charter: ReleaseCharter,
  delivery: ReleaseDelivery,
): ReleaseViewAvailability {
  const charterProblem =
    charter.kind === "unavailable"
      ? `the release charter could not be read: ${charter.reason}`
      : charter.kind === "not_found"
        ? "no arch.release charter found for this ref"
        : charter.kind === "loading"
          ? "the release charter is still loading"
          : null;

  const deliveryNotServedHere = delivery.kind === "not_served_here";
  const deliveryProblem =
    delivery.kind === "unknown"
      ? `delivery could not be computed: ${delivery.reason}`
      : delivery.kind === "not_served_here"
        ? `not served in this environment (a checkout-only capability): ${delivery.reason}`
        : delivery.kind === "not_found"
          ? "release-status.py has no charter for this ref"
          : null;

  return {
    complete: charterProblem === null && deliveryProblem === null,
    charterProblem,
    deliveryProblem,
    deliveryNotServedHere,
  };
}
