import type { Bead, BeadLink } from "@/types/bead";

// Shaping for the Architecture route. Kept out of the component for the same
// reason dev-board.ts is: the grouping rules are the part worth testing, and a
// component that owns them can only be tested by rendering it.
//
// The model is loaded into the substrate by scripts/ea-load.py, which strips
// the `supports` / `realizes` / `depends_on` ref arrays out of bead content and
// writes them as bead_link rows. Capability grouping uses `parent_id`; graph
// views use loaded bead_link rows so the UI has one answer for each edge.

export type Maturity = "absent" | "emerging" | "operating" | "optimised";
export type Disposition = "invest" | "tolerate" | "migrate" | "eliminate";
export type Layer = "demand" | "supply";
export type Health = "healthy" | "degraded" | "at_risk";
export type Value = "high" | "medium" | "low";

// content.source_class says which of three ways a record came to exist —
// authored (a judgment call), derived (read mechanically off another
// record), or observed (read mechanically off the running estate). Same
// vocabulary docs/architecture/model/services.yaml already uses for
// services; capabilities and applications carry it too, optionally, because
// pre-S34-B3 records were written before the field existed. Absence is not
// "authored" by default — an absent field renders as `unclassified` so a
// record's currency is never overstated by a guess.
export type SourceClass = "authored" | "derived" | "observed";
export type SourceClassification = SourceClass | "unclassified";

/** Reconfirmation cadence, in days, for source classes a writer is expected
 *  to keep current. Authored records are a judgment call with no mechanical
 *  cadence, so they carry none — staleness is a currency question, and
 *  currency only applies where a writer is on the hook to keep confirming. */
export const CONFIRMATION_CADENCE_DAYS: Partial<Record<SourceClass, number>> = {
  derived: 30,
  observed: 7,
};

export interface RoadmapAction {
  opened_at: string;
  target_date: string;
  action: string;
}

export interface CapabilityContent {
  ref: string;
  name: string;
  description: string;
  layer: Layer;
  maturity: Maturity;
  owner: string;
  evidence: string[];
  note?: string;
  assessed_at: string;
  roadmap?: RoadmapAction;
  source_class?: SourceClass;
}

export interface WorkloadObject {
  cluster: string;
  namespace: string;
  kind: string;
  name: string;
  manifest: string | null;
  managed_by: string;
}

export interface ApplicationContent {
  ref: string;
  name: string;
  description: string;
  layer: Layer;
  owner: string;
  build: string;
  business_value: Value;
  technical_health: Health;
  time_disposition: Disposition;
  workload?: { runtime: string; objects?: WorkloadObject[]; note?: string };
  loc?: number;
  debt?: string[];
  evidence: string[];
  note?: string;
  assessed_at: string;
  roadmap?: RoadmapAction;
  source_class?: SourceClass;
}

export type WorkClass = "feature" | "enabling" | "blocking" | "risk" | "security";

export interface ReleaseOutcome {
  id: string;
  statement: string;
  work_class: WorkClass;
  requirement_refs?: string[];
}

// arch.release content — the charter, not the gate-run verdict. Mirrors
// apps/substrate/src/schemas.py::ArchReleaseContent. Mirrored into the
// substrate from docs/releases/*.json by scripts/release-load.py; the
// `dev.release` bead type is a different object (a gate-run verdict) and
// has no bearing on this shape.
export interface ArchReleaseContent {
  ref: string;
  name: string;
  objective: string;
  sprints: string[];
  outcomes: ReleaseOutcome[];
  declared_balance: Partial<Record<WorkClass, number>>;
  opened_at: string;
  target_at?: string | null;
  source_class?: SourceClass;
}

export interface ObservationContent {
  ref?: string;
  observed_at: string;
  workload: {
    cluster: string;
    namespace: string;
    kind: string;
    name: string;
  };
  image_ref?: string | null;
  replicas?: number | null;
  ready_replicas?: number | null;
  argocd_sync_status?: string | null;
  argocd_health_status?: string | null;
  last_synced_revision?: string | null;
}

export type AssessmentSource = "asserted" | "measured";
export type RoadmapKind = "capability_gap" | "application_migration" | "application_elimination";

export interface ApplicationAssessment {
  application: Bead;
  source: AssessmentSource;
  observations: Bead[];
}

export interface CapabilityCoverage {
  capability: Bead;
  realizedBy: Bead[];
  supports: Bead[];
  supportedBy: Bead[];
}

export interface ApplicationDependency {
  application: Bead;
  dependsOn: Bead[];
  dependedOnBy: Bead[];
}

export interface RoadmapBacklogItem {
  id: string;
  kind: RoadmapKind;
  bead: Bead;
  ref: string;
  label: string;
  openedAt: string;
  targetDate: string;
  action: string;
}

export const MATURITY_ORDER: Maturity[] = ["absent", "emerging", "operating", "optimised"];
export const HEALTH_COLUMNS: Health[] = ["at_risk", "degraded", "healthy"];
export const VALUE_ROWS: Value[] = ["high", "medium", "low"];

export const HEALTH_LABEL: Record<Health, string> = {
  at_risk: "at risk",
  degraded: "degraded",
  healthy: "healthy",
};

export function capContent(bead: Bead): CapabilityContent {
  return bead.content as unknown as CapabilityContent;
}

export function appContent(bead: Bead): ApplicationContent {
  return bead.content as unknown as ApplicationContent;
}

export function releaseContent(bead: Bead): ArchReleaseContent {
  return bead.content as unknown as ArchReleaseContent;
}

export function observationContent(bead: Bead): ObservationContent {
  return bead.content as unknown as ObservationContent;
}

/** A capability/application's source class, never guessed. A record written
 *  before S33-B2 has no `source_class` field at all — that is `unclassified`,
 *  not `authored` by default, so a pre-S34-B3 record can never be mistaken
 *  for a currently-confirmed one. */
export function sourceClassOf(content: { source_class?: SourceClass }): SourceClassification {
  return content.source_class ?? "unclassified";
}

/** Whether a derived/observed record's last confirmation is older than its
 *  cadence allows. Authored and unclassified records are never stale by this
 *  measure: authored is a judgment call with no mechanical cadence, and a
 *  record with no declared source class has no cadence to measure against —
 *  absence must render as unclassified, never as fresh. */
export function isStale(sourceClass: SourceClassification, confirmedAt: string, now: Date): boolean {
  const cadenceDays = sourceClass === "authored" || sourceClass === "unclassified"
    ? undefined
    : CONFIRMATION_CADENCE_DAYS[sourceClass];
  if (!cadenceDays) return false;
  const confirmed = new Date(confirmedAt);
  if (Number.isNaN(confirmed.getTime())) return false;
  const ageDays = (now.getTime() - confirmed.getTime()) / (1000 * 60 * 60 * 24);
  return ageDays > cadenceDays;
}

export interface CapabilityGroup {
  root: Bead;
  children: Bead[];
}

/** Top-level capabilities for one layer, each with its children.
 *
 * A root is a capability with no `parent_id`. Children come from `parent_id`
 * rather than from a ref array, because the loader writes decomposition as a
 * real tree — that is the one relationship the substrate models natively.
 */
export function groupByLayer(capabilities: Bead[], layer: Layer): CapabilityGroup[] {
  const inLayer = capabilities.filter((b) => capContent(b).layer === layer);
  const roots = inLayer.filter((b) => !b.parent_id);
  const byParent = new Map<string, Bead[]>();
  for (const bead of capabilities) {
    if (!bead.parent_id) continue;
    const list = byParent.get(bead.parent_id) ?? [];
    list.push(bead);
    byParent.set(bead.parent_id, list);
  }
  return roots.map((root) => ({ root, children: byParent.get(root.id) ?? [] }));
}

/** Capabilities scored `absent`, worst-first by definition — this is the gap list. */
export function gaps(capabilities: Bead[]): Bead[] {
  return capabilities.filter((b) => capContent(b).maturity === "absent");
}

/** value × health, the TIME grid. Ordinal on both axes, so a grid and not a
 *  scatter — there is no precision here the assessment does not have. */
export function timeGrid(applications: Bead[]): Map<string, Bead[]> {
  const cells = new Map<string, Bead[]>();
  for (const app of applications) {
    const c = appContent(app);
    const key = `${c.business_value}:${c.technical_health}`;
    const list = cells.get(key) ?? [];
    list.push(app);
    cells.set(key, list);
  }
  return cells;
}

/** Applications whose declared workload has no manifest and no manager.
 *
 * This is the B-103 detector rendered. `app.itop` was exactly this state for
 * 40 days while the model scored it `eol` — running, unmanaged, invisible to
 * every repository-level check because the *absence* of a manifest is what
 * made those checks pass.
 */
export function gitopsOrphans(applications: Bead[]): { app: Bead; object: WorkloadObject }[] {
  const out: { app: Bead; object: WorkloadObject }[] = [];
  for (const app of applications) {
    for (const object of appContent(app).workload?.objects ?? []) {
      if (!object.manifest && object.managed_by === "none") out.push({ app, object });
    }
  }
  return out;
}

/** Eliminate-disposition applications that still declare a running workload.
 *  The teardown backlog: decided, not yet done. */
export function undoneTeardowns(applications: Bead[]): Bead[] {
  return applications.filter((b) => {
    const c = appContent(b);
    return c.time_disposition === "eliminate" && (c.workload?.objects?.length ?? 0) > 0;
  });
}

export function countDebt(applications: Bead[]): number {
  return applications.reduce((n, b) => n + (appContent(b).debt?.length ?? 0), 0);
}

export type FindingKind = "orphan" | "teardown";

/** S34-B3's standing observation findings: the model disagreeing with the
 *  estate, as one dated list rather than two independently-shaped ones. */
export interface ObservationFinding {
  id: string;
  kind: FindingKind;
  application: Bead;
  detail: string;
  foundAt: string;
}

export function observationFindings(applications: Bead[]): ObservationFinding[] {
  const orphanFindings: ObservationFinding[] = gitopsOrphans(applications).map(({ app, object }) => ({
    id: `${app.id}:orphan:${object.name}`,
    kind: "orphan",
    application: app,
    detail: `${object.namespace}/${object.kind}/${object.name} — running, no manifest, nothing managing it (B-103)`,
    foundAt: appContent(app).assessed_at,
  }));
  const teardownFindings: ObservationFinding[] = undoneTeardowns(applications).map((app) => ({
    id: `${app.id}:teardown`,
    kind: "teardown",
    application: app,
    detail: "scored eliminate, still declares a workload",
    foundAt: appContent(app).assessed_at,
  }));
  return [...orphanFindings, ...teardownFindings].sort(
    (a, b) => b.foundAt.localeCompare(a.foundAt) || a.id.localeCompare(b.id),
  );
}

export type FindingsStatus = "not_measuring" | "measured_clean" | "findings";

/** "Measured, nothing found" and "nothing measured" must never look alike —
 *  the house rule from B-103, applied to the findings list itself. An empty
 *  findings list only reads as a clean estate when at least one observation
 *  bead backs it; with none loaded, the estate has not been measured at all. */
export function findingsStatus(findings: ObservationFinding[], observations: Bead[]): FindingsStatus {
  if (findings.length > 0) return "findings";
  return observations.length > 0 ? "measured_clean" : "not_measuring";
}

export function roadmapBacklog(capabilities: Bead[], applications: Bead[]): RoadmapBacklogItem[] {
  const items: RoadmapBacklogItem[] = [];

  for (const capability of capabilities) {
    const content = capContent(capability);
    if (content.maturity !== "absent" || !content.roadmap) continue;
    items.push({
      id: `${capability.id}:roadmap`,
      kind: "capability_gap",
      bead: capability,
      ref: content.ref,
      label: content.name,
      openedAt: content.roadmap.opened_at,
      targetDate: content.roadmap.target_date,
      action: content.roadmap.action,
    });
  }

  for (const application of applications) {
    const content = appContent(application);
    if (!["migrate", "eliminate"].includes(content.time_disposition) || !content.roadmap) {
      continue;
    }
    items.push({
      id: `${application.id}:roadmap`,
      kind: content.time_disposition === "migrate" ? "application_migration" : "application_elimination",
      bead: application,
      ref: content.ref,
      label: content.name,
      openedAt: content.roadmap.opened_at,
      targetDate: content.roadmap.target_date,
      action: content.roadmap.action,
    });
  }

  return items.sort((a, b) => (
    a.targetDate.localeCompare(b.targetDate) ||
    a.openedAt.localeCompare(b.openedAt) ||
    a.label.localeCompare(b.label)
  ));
}

/** Whether each application assessment has a linked observation behind it.
 *
 * Absence of an observation is not neutral. It means the portfolio fields are
 * asserted by the reviewed model rather than measured by a dated metric bead.
 */
export function assessmentSources(
  applications: Bead[],
  observations: Bead[],
  links: BeadLink[],
): Map<string, AssessmentSource> {
  const observationIds = new Set(observations.map((observation) => observation.id));
  const measuredApplicationIds = new Set(
    links
      .filter((link) => link.link_type === "measures" && observationIds.has(link.source_id))
      .map((link) => link.target_id),
  );

  return new Map(
    applications.map((application) => [
      application.id,
      measuredApplicationIds.has(application.id) ? "measured" : "asserted",
    ]),
  );
}

export function applicationAssessments(
  applications: Bead[],
  observations: Bead[],
  links: BeadLink[],
): ApplicationAssessment[] {
  const observationById = new Map(observations.map((observation) => [observation.id, observation]));
  const observationsByApplication = new Map<string, Bead[]>();

  for (const link of links) {
    if (link.link_type !== "measures") continue;
    const observation = observationById.get(link.source_id);
    if (!observation) continue;
    const bucket = observationsByApplication.get(link.target_id) ?? [];
    bucket.push(observation);
    observationsByApplication.set(link.target_id, bucket);
  }

  return [...applications]
    .sort((a, b) => appContent(a).name.localeCompare(appContent(b).name))
    .map((application) => {
      const linkedObservations = [...(observationsByApplication.get(application.id) ?? [])].sort(
        (a, b) => observationObservedAt(b).localeCompare(observationObservedAt(a)),
      );
      return {
        application,
        source: linkedObservations.length > 0 ? "measured" : "asserted",
        observations: linkedObservations,
      };
    });
}

export function capabilityCoverage(
  capabilities: Bead[],
  applications: Bead[],
  links: BeadLink[],
): CapabilityCoverage[] {
  const capabilityById = new Map(capabilities.map((capability) => [capability.id, capability]));
  const applicationById = new Map(applications.map((application) => [application.id, application]));
  const rows = new Map<string, CapabilityCoverage>(
    capabilities.map((capability) => [
      capability.id,
      { capability, realizedBy: [], supports: [], supportedBy: [] },
    ]),
  );

  for (const link of links) {
    if (link.link_type === "realizes") {
      const application = applicationById.get(link.source_id);
      const row = rows.get(link.target_id);
      if (application && row) row.realizedBy.push(application);
    }
    if (link.link_type === "supports") {
      const source = capabilityById.get(link.source_id);
      const target = capabilityById.get(link.target_id);
      const sourceRow = rows.get(link.source_id);
      const targetRow = rows.get(link.target_id);
      if (sourceRow && target) sourceRow.supports.push(target);
      if (targetRow && source) targetRow.supportedBy.push(source);
    }
  }

  return [...rows.values()]
    .map((row) => ({
      ...row,
      realizedBy: [...row.realizedBy].sort(byApplicationName),
      supports: [...row.supports].sort(byCapabilityName),
      supportedBy: [...row.supportedBy].sort(byCapabilityName),
    }))
    .sort((a, b) => {
      const maturity =
        MATURITY_ORDER.indexOf(capContent(a.capability).maturity) -
        MATURITY_ORDER.indexOf(capContent(b.capability).maturity);
      return maturity || capContent(a.capability).name.localeCompare(capContent(b.capability).name);
    });
}

export function applicationDependencies(
  applications: Bead[],
  links: BeadLink[],
): ApplicationDependency[] {
  const applicationById = new Map(applications.map((application) => [application.id, application]));
  const rows = new Map<string, ApplicationDependency>(
    applications.map((application) => [
      application.id,
      { application, dependsOn: [], dependedOnBy: [] },
    ]),
  );

  for (const link of links) {
    if (link.link_type !== "depends_on") continue;
    const source = applicationById.get(link.source_id);
    const target = applicationById.get(link.target_id);
    const sourceRow = rows.get(link.source_id);
    const targetRow = rows.get(link.target_id);
    if (sourceRow && target) sourceRow.dependsOn.push(target);
    if (targetRow && source) targetRow.dependedOnBy.push(source);
  }

  return [...rows.values()]
    .filter((row) => row.dependsOn.length > 0 || row.dependedOnBy.length > 0)
    .map((row) => ({
      ...row,
      dependsOn: [...row.dependsOn].sort(byApplicationName),
      dependedOnBy: [...row.dependedOnBy].sort(byApplicationName),
    }))
    .sort((a, b) => appContent(a.application).name.localeCompare(appContent(b.application).name));
}

export function observationObservedAt(observation: Bead): string {
  return observationContent(observation).observed_at ?? "";
}

/** Sort helper: worst maturity first, so a group's problems lead. */
export function byMaturity(a: Bead, b: Bead): number {
  return MATURITY_ORDER.indexOf(capContent(a).maturity) - MATURITY_ORDER.indexOf(capContent(b).maturity);
}

function byApplicationName(a: Bead, b: Bead): number {
  return appContent(a).name.localeCompare(appContent(b).name);
}

function byCapabilityName(a: Bead, b: Bead): number {
  return capContent(a).name.localeCompare(capContent(b).name);
}
