import type { Bead, BeadLink } from "@/types/bead";
import { appContent, capContent } from "./ea-model";

// "What breaks if this dies" (S35/PC-ASR-002), read as a traversal rather than
// a recollection. Kept out of Architecture.tsx for the same reason the rest
// of this file's neighbours are: the graph walk is the part worth testing on
// its own, without a rendered component in the way.
//
// The estate's dependency graph is two edge types read together:
//   depends_on  — application/service -> application/service/ci ("needs")
//   consumes    — application -> service ("needs", service-shaped)
// Both mean the same thing for blast-radius purposes: if the target dies, the
// source is affected. Walking *backward* along both — from target to source —
// is the "what depends on this" walk. `realizes` (application -> capability)
// is then walked forward, once, from whatever applications the backward walk
// turned up (plus the origin itself, when it is an application) to answer
// which capabilities break.

export type ServiceLayer = "demand" | "supply";
export type ServiceSourceClass = "authored" | "derived" | "observed";

export interface ServiceContent {
  ref: string;
  name: string;
  description: string;
  layer: ServiceLayer;
  owner: string;
  evidence: string[];
  assessed_at: string;
  note?: string;
  source_class?: ServiceSourceClass;
  roadmap?: { opened_at: string; target_date: string; action: string };
}

export type CiKind = "workload" | "namespace" | "database";

// Mirrors apps/substrate/src/schemas.py::ArchCiContent. `source_class` is
// locked to "observed" server-side — a CI record only ever comes from the
// observer, never from a human authoring one by hand.
export interface CiContent {
  ref: string;
  ci_kind: CiKind;
  cluster: string;
  namespace: string;
  kind: string;
  name: string;
  source_class: "observed";
}

export function serviceContent(bead: Bead): ServiceContent {
  return bead.content as unknown as ServiceContent;
}

export function ciContent(bead: Bead): CiContent {
  return bead.content as unknown as CiContent;
}

/** A CI has no `name` field worth a label on its own — it is a coordinate in
 *  the cluster, so its label is the coordinate. */
export function ciLabel(bead: Bead): string {
  const c = ciContent(bead);
  return `${c.cluster}/${c.namespace}/${c.kind}/${c.name}`;
}

const DEPENDENCY_EDGE_TYPES = new Set(["depends_on", "consumes"]);
const REALIZES_EDGE_TYPE = "realizes";

// Bounds the walk so a data error that creates a cycle, or a graph denser
// than this estate has ever been, cannot hang the tab. The queue-plus-visited
// walk below is already cycle-safe on its own; this is the second, blunter
// guard — a traversal that would still be running past this many nodes is a
// bug worth surfacing, not a state worth spinning in.
export const MAX_TRAVERSED_NODES = 2000;

export interface BlastRadiusEntities {
  applications: Bead[];
  services: Bead[];
  cis: Bead[];
  capabilities: Bead[];
}

// The declaration PC-ASR-002/AC-1 requires: how much of the estate this
// traversal is actually built on, as two numbers, not a percentage that can
// hide a near-empty graph behind a confident-looking "0%".
export interface GraphCoverage {
  linkedNodes: number;
  totalNodes: number;
}

// "measured" only means at least one depends_on/consumes/realizes edge is
// loaded anywhere in scope — it is not a claim about the chosen node
// specifically. A node with genuinely no dependents still reads "measured"
// with empty lists; "no_linkage_data" is reserved for the case a blank list
// would otherwise lie about — nothing has been loaded to check at all.
export type BlastRadiusStatus = "measured" | "no_linkage_data";

export interface BlastRadiusResult {
  originId: string;
  applications: Bead[];
  services: Bead[];
  capabilities: Bead[];
  coverage: GraphCoverage;
  status: BlastRadiusStatus;
  truncated: boolean;
}

/** Of the CI/service/application nodes in scope, how many carry at least one
 *  depends_on/consumes/realizes edge (either direction). The denominator a
 *  traversal answer is honest about, per PC-ASR-002/AC-1. */
export function graphCoverage(entities: BlastRadiusEntities, links: BeadLink[]): GraphCoverage {
  const nodes = [...entities.applications, ...entities.services, ...entities.cis];
  const linkedIds = new Set<string>();
  for (const link of links) {
    if (!DEPENDENCY_EDGE_TYPES.has(link.link_type) && link.link_type !== REALIZES_EDGE_TYPE) continue;
    linkedIds.add(link.source_id);
    linkedIds.add(link.target_id);
  }
  return {
    linkedNodes: nodes.filter((node) => linkedIds.has(node.id)).length,
    totalNodes: nodes.length,
  };
}

/** From `origin` (a CI, service or application bead), the applications and
 *  services that transitively depend on it, and the capabilities that break
 *  with them — plus the coverage the answer is built on, per PC-ASR-002.
 *
 * `maxNodes` exists for tests; production callers should leave it at the
 * module default.
 */
export function blastRadius(
  origin: Bead,
  entities: BlastRadiusEntities,
  links: BeadLink[],
  maxNodes: number = MAX_TRAVERSED_NODES,
): BlastRadiusResult {
  const byId = new Map(
    [...entities.applications, ...entities.services, ...entities.cis].map((bead) => [bead.id, bead]),
  );

  // Reverse index of depends_on/consumes: target -> the sources that need it.
  const dependents = new Map<string, string[]>();
  for (const link of links) {
    if (!DEPENDENCY_EDGE_TYPES.has(link.link_type)) continue;
    const list = dependents.get(link.target_id) ?? [];
    list.push(link.source_id);
    dependents.set(link.target_id, list);
  }

  const visited = new Set<string>([origin.id]);
  const collectedIds: string[] = [];
  const queue: string[] = [origin.id];
  let truncated = false;

  while (queue.length > 0) {
    if (visited.size >= maxNodes) {
      truncated = true;
      break;
    }
    const current = queue.shift() as string;
    for (const sourceId of dependents.get(current) ?? []) {
      if (visited.has(sourceId)) continue; // cycle guard: never re-queue a seen node
      visited.add(sourceId);
      if (byId.has(sourceId)) collectedIds.push(sourceId);
      queue.push(sourceId);
    }
  }

  const dependentBeads = collectedIds
    .map((id) => byId.get(id))
    .filter((bead): bead is Bead => Boolean(bead));
  const applications = dependentBeads.filter((bead) => bead.type === "application").sort(byNodeName);
  const services = dependentBeads.filter((bead) => bead.type === "service").sort(byNodeName);

  // Capabilities broken: realized by the dependent applications, plus by the
  // origin itself when it is an application — its own capability breaks
  // before any dependent's does, and "they" in the requirement's "the
  // applications and services ... and the capabilities they realize" reads
  // thin without it.
  const realizingIds = new Set(applications.map((bead) => bead.id));
  if (origin.type === "application") realizingIds.add(origin.id);
  const capabilityById = new Map(entities.capabilities.map((bead) => [bead.id, bead]));
  const capabilityIds = new Set<string>();
  for (const link of links) {
    if (link.link_type !== REALIZES_EDGE_TYPE) continue;
    if (!realizingIds.has(link.source_id)) continue;
    if (capabilityById.has(link.target_id)) capabilityIds.add(link.target_id);
  }
  const capabilities = [...capabilityIds]
    .map((id) => capabilityById.get(id) as Bead)
    .sort((a, b) => capContent(a).name.localeCompare(capContent(b).name));

  const relevantLinkCount = links.filter(
    (link) => DEPENDENCY_EDGE_TYPES.has(link.link_type) || link.link_type === REALIZES_EDGE_TYPE,
  ).length;

  return {
    originId: origin.id,
    applications,
    services,
    capabilities,
    coverage: graphCoverage(entities, links),
    status: relevantLinkCount === 0 ? "no_linkage_data" : "measured",
    truncated,
  };
}

function byNodeName(a: Bead, b: Bead): number {
  return nodeName(a).localeCompare(nodeName(b));
}

function nodeName(bead: Bead): string {
  if (bead.type === "application") return appContent(bead).name;
  if (bead.type === "service") return serviceContent(bead).name;
  return ciLabel(bead);
}
