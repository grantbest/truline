import type { Bead, BeadLink, DevTaskContent } from "@/types/bead";
import type { ApplicationContent, CapabilityContent } from "@/lib/ea-model";

export const ALPHA_FETCH_LIMIT_PER_TYPE = 100;
export const ALPHA_INITIAL_QUERY_COUNT = 8;
export const ALPHA_INITIAL_BEAD_BUDGET = ALPHA_FETCH_LIMIT_PER_TYPE * ALPHA_INITIAL_QUERY_COUNT;

export type AlphaEdgeType =
  | "designs"
  | "gates"
  | "regresses"
  | "found_by"
  | "affects"
  | "measures"
  | "supersedes";

export interface EdgeView {
  type: AlphaEdgeType;
  label: string;
  direction: string;
  sourceType: string;
  targetType: string;
  empty: string;
}

export const EDGE_VIEWS: EdgeView[] = [
  {
    type: "designs",
    label: "Designs",
    direction: "Architectural decision -> task",
    sourceType: "dev.design",
    targetType: "dev.task",
    empty: "No design decision bead has been linked to this task yet.",
  },
  {
    type: "gates",
    label: "Gates",
    direction: "Gate run -> task",
    sourceType: "dev.release",
    targetType: "dev.task",
    empty: "No release gate bead admits this task yet.",
  },
  {
    type: "regresses",
    label: "Regresses",
    direction: "Finding -> task",
    sourceType: "dev.finding",
    targetType: "dev.task",
    empty: "No gate finding currently traces back to this task.",
  },
  {
    type: "found_by",
    label: "Found by",
    direction: "Finding -> gate run",
    sourceType: "dev.finding",
    targetType: "dev.release",
    empty: "No finding is linked to a gate run for this slice.",
  },
  {
    type: "affects",
    label: "Affects",
    direction: "Change -> application",
    sourceType: "arch.change",
    targetType: "arch.application",
    empty: "No ITIL change record is linked to this application.",
  },
  {
    type: "measures",
    label: "Measures",
    direction: "Observation -> application",
    sourceType: "arch.observation",
    targetType: "arch.application",
    empty: "No portfolio metric bead measures this application yet.",
  },
  {
    type: "supersedes",
    label: "Supersedes",
    direction: "Task -> prior task",
    sourceType: "dev.task",
    targetType: "dev.task",
    empty: "No rework lineage is linked for this task.",
  },
];

export type ProductPersonaId =
  | "product-owner"
  | "sre-manager"
  | "cmdb-owner"
  | "technology-owner"
  | "release-manager"
  | "scrum-master"
  | "enterprise-architect"
  | "application-portfolio-owner";

export interface ProductPersona {
  id: ProductPersonaId;
  role: string;
  outcome: string;
  direction: string;
  tools: string[];
  edgeTypes: AlphaEdgeType[];
  mode: "graph" | "flow";
}

export const PRODUCT_PERSONAS: ProductPersona[] = [
  {
    id: "product-owner",
    role: "Product Owner",
    outcome: "Keeps business intent, acceptance criteria, and rework history visible.",
    direction: "Requirement -> dev.task -> superseded or regressed work",
    tools: ["Requirement entry", "Acceptance evidence", "Rework lineage"],
    edgeTypes: ["supersedes", "regresses"],
    mode: "graph",
  },
  {
    id: "sre-manager",
    role: "SRE Manager",
    outcome: "Sees release and finding signals that change operational risk.",
    direction: "dev.release -> dev.task <- dev.finding",
    tools: ["Gate evidence", "Regression memory", "Operational gaps"],
    edgeTypes: ["gates", "regresses", "found_by"],
    mode: "graph",
  },
  {
    id: "cmdb-owner",
    role: "CMDB Owner",
    outcome: "Finds the CMDB delta tied to the application portfolio object.",
    direction: "arch.change -> arch.application",
    tools: ["Change evidence", "Application impact", "Carrier gap callouts"],
    edgeTypes: ["affects"],
    mode: "graph",
  },
  {
    id: "technology-owner",
    role: "Technology Owner",
    outcome: "Travels down from an application to the services and operational evidence beneath it.",
    direction: "arch.application -> services, changes, findings",
    tools: ["Service impact", "Technical health", "Incident carrier gap"],
    edgeTypes: ["affects", "regresses", "measures"],
    mode: "graph",
  },
  {
    id: "release-manager",
    role: "Release Manager",
    outcome: "Determines what a release admitted and what suites were required.",
    direction: "dev.release -> dev.task -> prior regressions",
    tools: ["Gate run", "Suite evidence", "Merge order"],
    edgeTypes: ["gates", "regresses", "found_by"],
    mode: "graph",
  },
  {
    id: "scrum-master",
    role: "Scrum Master",
    outcome: "Watches flow and stuck work; this is task state, not graph traversal.",
    direction: "dev.task state across pending, doing, review, done, failed",
    tools: ["Factory Board", "Review queue", "Blocking questions"],
    edgeTypes: [],
    mode: "flow",
  },
  {
    id: "enterprise-architect",
    role: "Enterprise Architect",
    outcome: "Travels up to capability, standards, roadmap, and portfolio metrics.",
    direction: "arch.observation -> arch.application -> capabilities",
    tools: ["Portfolio metrics", "Capability alignment", "Provenance gap"],
    edgeTypes: ["measures", "affects"],
    mode: "graph",
  },
  {
    id: "application-portfolio-owner",
    role: "Application Portfolio Owner",
    outcome: "Travels up from an application to capabilities, disposition, and investment choices.",
    direction: "arch.application -> capabilities and TIME disposition",
    tools: ["Portfolio lens", "Lifecycle disposition", "Business value"],
    edgeTypes: ["measures", "affects"],
    mode: "graph",
  },
];

export const OPERATING_MODEL_PERSONAS = [
  "Product Owner",
  "SRE",
  "Architect/SME",
  "Polecat developer",
  "Release Manager",
  "QA",
  "Configuration Management",
  "Enterprise Architect",
];

export interface AlphaDatasets {
  tasks: Bead[];
  designs: Bead[];
  releases: Bead[];
  findings: Bead[];
  applications: Bead[];
  changes: Bead[];
  observations: Bead[];
  capabilities: Bead[];
  links?: BeadLink[];
}

export interface AlphaModel {
  source: "live" | "fixture";
  requirementRef: string;
  task: Bead;
  application: Bead;
  capabilities: Bead[];
  beads: Bead[];
  links: BeadLink[];
  stats: {
    tasks: number;
    tasksWithRequirementRefs: number;
    tasksWithArchImpact: number;
    applications: number;
    links: number;
  };
}

const now = "2026-08-02T00:00:00Z";

function bead(overrides: Partial<Bead> & Pick<Bead, "id" | "namespace" | "type">): Bead {
  return {
    state: "active",
    parent_id: null,
    context: {},
    content: {},
    confidence: 0.82,
    trust_tier: "derived",
    provenance: { fixture: "project-alpha-ui" },
    created_by: "project-alpha-fixture",
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

const fixtureCapability = bead({
  id: "fixture-capability-auth-resilience",
  namespace: "arch",
  type: "capability",
  content: {
    ref: "cap.auth-resilience",
    name: "Authentication resilience",
    description: "Keep customer access paths reliable through degraded identity-provider modes.",
    layer: "demand",
    maturity: "emerging",
    owner: "Digital Channels",
    evidence: ["docs/architecture/model/capabilities.yaml"],
    assessed_at: "2026-08-02",
  } satisfies CapabilityContent,
});

const fixtureApplication = bead({
  id: "fixture-application-customer-identity",
  namespace: "arch",
  type: "application",
  state: "operate",
  content: {
    ref: "app.customer-identity",
    name: "Customer Identity Suite",
    description: "Portfolio entry for customer login, token brokering, and passkey fallback.",
    layer: "supply",
    owner: "Application Portfolio Owner",
    build: "custom",
    business_value: "high",
    technical_health: "degraded",
    time_disposition: "invest",
    workload: {
      runtime: "kubernetes",
      objects: [
        {
          cluster: "cluster-a",
          namespace: "dev",
          kind: "Deployment",
          name: "identity-console",
          manifest: "infrastructure/k8s/dev/identity-console.yaml",
          managed_by: "argocd",
        },
      ],
    },
    evidence: ["seed fixture: graph is currently sparse"],
    assessed_at: "2026-08-02",
  } satisfies ApplicationContent,
});

const fixtureTask = bead({
  id: "fixture-task-passkey-fallback",
  namespace: "dev",
  type: "task",
  state: "review",
  content: {
    lane: "bug-triage",
    title: "Passkey fallback resilience",
    intent: "Make degraded passkey auth explicit, testable, and visible through release.",
    context_refs: ["docs/requirements/LO-CAT-004.md"],
    acceptance: [
      "Fallback path is visible to release and SRE review.",
      "Application impact is declared against the portfolio entry.",
    ],
    verification: {
      commands: ["npm run typecheck", "npm test"],
      must_report_unverified: true,
    },
    scope: {
      paths: ["apps/lifeops-console/**"],
      forbidden_paths: [".github/workflows/**"],
    },
    risk_class: "behavioral",
    budget: { max_agent_minutes: 45, max_usd: 5, max_tokens: 50000 },
    requirement_refs: ["LO-CAT-004/AC-1"],
    nfrs: [
      {
        category: "availability",
        statement: "Fallback guidance must remain visible when graph edges are absent.",
        threshold: "node renders with zero links",
        verification: "fixture-backed empty-state render",
      },
    ],
    arch_impact: {
      applications: ["app.customer-identity"],
      capabilities: ["cap.auth-resilience"],
      notes: "Requirement enters at application portfolio context.",
    },
    pr_refs: [],
    worker_hint: "codex",
    autonomy: "propose",
    attempts: 1,
    max_attempts: 3,
  } satisfies DevTaskContent,
});

const fixturePriorTask = bead({
  id: "fixture-task-prior-fallback",
  namespace: "dev",
  type: "task",
  state: "done",
  content: {
    ...fixtureTask.content,
    title: "Prior passkey outage repair",
    requirement_refs: ["LO-CAT-004/AC-1"],
  },
});

const fixtureDesign = bead({
  id: "fixture-design-passkey",
  namespace: "dev",
  type: "design",
  content: {
    decision: "Make degraded identity-provider handling a first-class release check.",
    rationale: "The release gate cannot infer resilience requirements from code diffs.",
    alternatives_rejected: ["Treat fallback as a runbook-only procedure."],
    nfrs_derived: (fixtureTask.content as unknown as DevTaskContent).nfrs,
    arch_impact: (fixtureTask.content as unknown as DevTaskContent).arch_impact,
  },
});

const fixtureRelease = bead({
  id: "fixture-release-r2608",
  namespace: "dev",
  type: "release",
  state: "review",
  content: {
    reviewer: "Gemini",
    verdict: "merge-with-changes",
    pr_refs: ["PR-256"],
    task_refs: [fixtureTask.id],
    required_suites: ["console typecheck", "alpha projection tests"],
    results: [
      {
        category: "unit",
        suite: "alpha projection tests",
        outcome: "pass",
        evidence: "npm test",
      },
    ],
    merge_order: ["PR-256"],
    unverified_claims: ["No production writes exist for dev.release yet."],
    not_reviewed: [],
  },
});

const fixtureFinding = bead({
  id: "fixture-finding-empty-edges",
  namespace: "dev",
  type: "finding",
  state: "pending",
  content: {
    kind: "enhancement",
    disposition: "backlog",
    severity: "medium",
    summary: "Most graph edges are empty until writers are added.",
    evidence: "dev substrate data reality, 2026-08-02",
  },
});

const fixtureChange = bead({
  id: "fixture-change-identity",
  namespace: "arch",
  type: "change",
  state: "planned",
  content: {
    change_type: "normal",
    summary: "Expose identity fallback readiness in the SDLC console.",
    applications: ["app.customer-identity"],
    capabilities: ["cap.auth-resilience"],
    release_ref: "R26.08",
    evidence: ["fixture data: seed record for the demo graph; live records are written by the arch.change reconciler (R26.05/O-1)"],
  },
});

const fixtureObservation = bead({
  id: "fixture-observation-identity",
  namespace: "arch",
  type: "observation",
  content: {
    observed_at: now,
    workload: {
      cluster: "cluster-a",
      namespace: "dev",
      kind: "Deployment",
      name: "identity-console",
    },
    replicas: 2,
    ready_replicas: 1,
    argocd_sync_status: "Synced",
    argocd_health_status: "Degraded",
  },
});

function link(
  id: string,
  source: Bead,
  target: Bead,
  link_type: AlphaEdgeType,
  content: Record<string, unknown> = {},
): BeadLink {
  return {
    id,
    source_id: source.id,
    target_id: target.id,
    link_type,
    content,
    created_at: now,
    created_by: "project-alpha-fixture",
  };
}

export const ALPHA_FIXTURE_BEADS = [
  fixtureCapability,
  fixtureApplication,
  fixtureTask,
  fixturePriorTask,
  fixtureDesign,
  fixtureRelease,
  fixtureFinding,
  fixtureChange,
  fixtureObservation,
];

export const ALPHA_FIXTURE_LINKS: BeadLink[] = [
  link("fixture-link-designs", fixtureDesign, fixtureTask, "designs"),
  link("fixture-link-gates", fixtureRelease, fixtureTask, "gates"),
  link("fixture-link-regresses", fixtureFinding, fixtureTask, "regresses"),
  link("fixture-link-found-by", fixtureFinding, fixtureRelease, "found_by"),
  link("fixture-link-affects", fixtureChange, fixtureApplication, "affects"),
  link("fixture-link-measures", fixtureObservation, fixtureApplication, "measures"),
  link("fixture-link-supersedes", fixtureTask, fixturePriorTask, "supersedes"),
];

function taskContent(bead: Bead): DevTaskContent {
  return bead.content as unknown as DevTaskContent;
}

export function displayName(bead: Bead | undefined): string {
  if (!bead) return "Unloaded bead";
  const c = bead.content as Record<string, unknown>;
  if (typeof c.title === "string") return c.title;
  if (typeof c.name === "string") return c.name;
  if (typeof c.summary === "string") return c.summary;
  if (typeof c.decision === "string") return c.decision;
  return `${bead.namespace}.${bead.type}`;
}

export function beadKind(bead: Bead | undefined): string {
  return bead ? `${bead.namespace}.${bead.type}` : "unknown";
}

export function buildAlphaModel(data: AlphaDatasets): AlphaModel {
  const tasksWithRequirementRefs = data.tasks.filter(
    (task) => (taskContent(task).requirement_refs?.length ?? 0) > 0,
  );
  const tasksWithArchImpact = data.tasks.filter(
    (task) => (taskContent(task).arch_impact?.applications?.length ?? 0) > 0,
  );
  const candidateTask =
    tasksWithRequirementRefs.find((task) => (taskContent(task).arch_impact?.applications?.length ?? 0) > 0) ??
    tasksWithRequirementRefs[0];

  if (!candidateTask) {
    return {
      source: "fixture",
      requirementRef: "LO-CAT-004/AC-1",
      task: fixtureTask,
      application: fixtureApplication,
      capabilities: [fixtureCapability],
      beads: ALPHA_FIXTURE_BEADS,
      links: ALPHA_FIXTURE_LINKS,
      stats: {
        tasks: data.tasks.length,
        tasksWithRequirementRefs: 0,
        tasksWithArchImpact: tasksWithArchImpact.length,
        applications: data.applications.length,
        links: data.links?.length ?? 0,
      },
    };
  }

  const content = taskContent(candidateTask);
  const appRefs = new Set(content.arch_impact?.applications ?? []);
  const capRefs = new Set(content.arch_impact?.capabilities ?? []);
  const application =
    data.applications.find((app) => appRefs.has(app.id) || appRefs.has(String(app.content.ref ?? ""))) ??
    data.applications[0] ??
    fixtureApplication;
  const capabilities = data.capabilities.filter(
    (cap) => capRefs.has(cap.id) || capRefs.has(String(cap.content.ref ?? "")),
  );
  const beads = [
    ...data.tasks,
    ...data.designs,
    ...data.releases,
    ...data.findings,
    ...data.applications,
    ...data.changes,
    ...data.observations,
    ...data.capabilities,
  ];

  return {
    source: "live",
    requirementRef: content.requirement_refs?.[0] ?? "requirement ref missing",
    task: candidateTask,
    application,
    capabilities,
    beads,
    links: data.links ?? [],
    stats: {
      tasks: data.tasks.length,
      tasksWithRequirementRefs: tasksWithRequirementRefs.length,
      tasksWithArchImpact: tasksWithArchImpact.length,
      applications: data.applications.length,
      links: data.links?.length ?? 0,
    },
  };
}

export function linksForEdge(model: AlphaModel, edgeType: AlphaEdgeType): BeadLink[] {
  const relevantIds = new Set([model.task.id, model.application.id]);
  for (const cap of model.capabilities) relevantIds.add(cap.id);
  return model.links.filter(
    (edge) =>
      edge.link_type === edgeType &&
      (relevantIds.has(edge.source_id) || relevantIds.has(edge.target_id) || model.source === "fixture"),
  );
}

export function beadLookup(model: AlphaModel): Map<string, Bead> {
  return new Map(model.beads.map((bead) => [bead.id, bead]));
}

export function productPersonaById(id: ProductPersonaId): ProductPersona {
  return PRODUCT_PERSONAS.find((persona) => persona.id === id) ?? PRODUCT_PERSONAS[0];
}

// Release charter coverage — reads arch.release, the type release-load.py
// actually writes. "Substrate unreachable" and "substrate answered with zero
// rows" are opposite claims (R26.01/O-10) and must never collapse into one
// rendered state, the same distinction ea-model.ts's findingsStatus draws for
// observations. Never falls back to fixture content: an empty or unavailable
// result says so in its own words instead.
export type ReleaseCoverageStatus = "unavailable" | "empty" | "data";

export interface ReleaseCoverage {
  status: ReleaseCoverageStatus;
  charters: Bead[];
  message: string;
}

export function releaseCoverage(params: { isError: boolean; releases: Bead[] }): ReleaseCoverage {
  if (params.isError) {
    return {
      status: "unavailable",
      charters: [],
      message:
        "arch.release could not be reached. Release charter coverage is unknown — this is not the same as zero charters existing.",
    };
  }

  if (params.releases.length === 0) {
    return {
      status: "empty",
      charters: [],
      message:
        "arch.release returned 0 charters from the substrate. (dev.release is a different type — the gate-run verdict — and has no bearing on this panel.)",
    };
  }

  const n = params.releases.length;
  const message =
    n >= ALPHA_FETCH_LIMIT_PER_TYPE
      ? `${n} charter(s) returned (fetch limit ${ALPHA_FETCH_LIMIT_PER_TYPE} per type) — more may exist beyond this page.`
      : `${n} of ${n} charter(s) returned by the substrate.`;

  return { status: "data", charters: params.releases, message };
}
