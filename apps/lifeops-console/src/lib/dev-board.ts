import type {
  Bead,
  BeadLink,
  DevLane,
  DevNoteContent,
  DevTaskContent,
  DevTaskState,
} from "@/types/bead";
import { DEV_TASK_STATES } from "@/types/bead";
import taskIntakeContract from "@/lib/task-intake-contract.json";
import { releaseContent, type WorkClass } from "@/lib/ea-model";

// Pure projection helpers for the factory board. Kept out of the components so
// the interesting logic — which question is still open, where an off-convention
// state lands — is testable without a DOM.

export function taskContent(bead: Bead): DevTaskContent {
  return bead.content as unknown as DevTaskContent;
}

export function noteContent(bead: Bead): DevNoteContent {
  return bead.content as unknown as DevNoteContent;
}

export const TASK_INTAKE_REQUIRED = taskIntakeContract.required;
export const TASK_FORBIDDEN_ALWAYS = taskIntakeContract.forbidden_always;
export const FACTORY_BOARD_REFRESH_INTERVAL_MS = 30_000;

// Terminal states, per apps/substrate/src/routes.py::STATE_MACHINES
// [("dev", "task")] — closed work, not open work. "done" shipped;
// "superseded" was replaced before it landed; "archived" shipped and was
// later put away. Different answers to "what happened to this", so each
// keeps its own board column rather than merging into one closed count.
const CLOSED_STATE_SET = new Set<DevTaskState>(["done", "superseded", "archived"]);
export const OPEN_DEV_TASK_STATES = DEV_TASK_STATES.filter(
  (state) => !CLOSED_STATE_SET.has(state),
);
export const CLOSED_DEV_TASK_STATES = DEV_TASK_STATES.filter((state) =>
  CLOSED_STATE_SET.has(state),
);

export interface TaskIntakeDraft {
  lane: DevLane;
  title: string;
  intent: string;
  acceptance: string;
  scopePaths: string;
  forbiddenPaths: string;
  risk_class: string;
  verificationCommands: string;
  requirementRefs: string;
  nfrs: string;
  archImpact: string;
}

function lines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function hasRequiredValue(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "string") return value.trim().length > 0;
  if (value && typeof value === "object") return Object.keys(value).length > 0;
  return Boolean(value);
}

function parseOptionalJson(value: string, field: string): unknown | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    throw new Error(`${field} must be valid JSON`);
  }
}

function nonEmptyArchImpact(value: unknown): DevTaskContent["arch_impact"] | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("arch_impact must be a JSON object");
  }
  const impact = value as NonNullable<DevTaskContent["arch_impact"]>;
  const hasApplications = Array.isArray(impact.applications) && impact.applications.length > 0;
  const hasCapabilities = Array.isArray(impact.capabilities) && impact.capabilities.length > 0;
  const hasNotes = typeof impact.notes === "string" && impact.notes.trim().length > 0;
  return hasApplications || hasCapabilities || hasNotes ? impact : undefined;
}

function nonEmptyNfrs(value: unknown): DevTaskContent["nfrs"] | undefined {
  if (!Array.isArray(value)) throw new Error("nfrs must be a JSON array");
  return value.length > 0 ? (value as DevTaskContent["nfrs"]) : undefined;
}

/**
 * The JSON spec `POST /api/v1/factory/tasks` (mcp-hub's `file_dev_task`,
 * calling `file_task.file_spec`/`build_content` — the same intake the CLI
 * uses) accepts, built from the board's draft form. Deliberately NOT a bead
 * envelope (contrast the old `buildTaskCreatePayload`, which built a
 * `Partial<Bead>` for a direct `substrateClient.createBead` write): the
 * gateway capability owns turning a spec into a bead, including the
 * refusal checks (duplicate `spec_identity`, unresolvable `release_ref`,
 * `.github/workflows/**` in scope, ...) that a raw substrate write skipped
 * entirely. What stays client-side below is only the cheap, purely local
 * subset of those checks (required fields present, the forbidden-always
 * path), kept for fast form feedback before a round-trip — not a second
 * copy of the intake's authority, which now runs once, server-side.
 */
export function buildTaskFilingSpec(draft: TaskIntakeDraft): Record<string, unknown> {
  const acceptance = lines(draft.acceptance);
  const scopePaths = lines(draft.scopePaths);
  const requiredCheck = {
    lane: draft.lane,
    title: draft.title,
    intent: draft.intent,
    acceptance,
    scope: scopePaths.length > 0 ? { paths: scopePaths } : {},
    risk_class: draft.risk_class,
  };
  const missing = TASK_INTAKE_REQUIRED.filter(
    (field) => !hasRequiredValue(requiredCheck[field as keyof typeof requiredCheck]),
  );
  if (missing.length > 0) {
    throw new Error(`missing required field(s): ${missing.join(", ")}`);
  }
  const conflicting = TASK_FORBIDDEN_ALWAYS.filter((always) => scopePaths.includes(always));
  if (conflicting.length > 0) {
    throw new Error(`${conflicting.join(", ")} may not be listed in scope.paths`);
  }

  const forbidden = lines(draft.forbiddenPaths);
  for (const always of TASK_FORBIDDEN_ALWAYS) {
    if (!forbidden.includes(always)) forbidden.push(always);
  }

  const spec: Record<string, unknown> = {
    lane: draft.lane,
    title: draft.title.trim(),
    intent: draft.intent.trim(),
    acceptance,
    verification: {
      commands: lines(draft.verificationCommands),
      must_report_unverified: true,
    },
    scope: {
      paths: scopePaths,
      forbidden_paths: forbidden,
    },
    risk_class: draft.risk_class,
  };

  const requirementRefs = lines(draft.requirementRefs);
  if (requirementRefs.length > 0) spec.requirement_refs = requirementRefs;

  const nfrs = parseOptionalJson(draft.nfrs, "nfrs");
  if (nfrs !== undefined) {
    const parsedNfrs = nonEmptyNfrs(nfrs);
    if (parsedNfrs) spec.nfrs = parsedNfrs;
  }

  const archImpact = parseOptionalJson(draft.archImpact, "arch_impact");
  if (archImpact !== undefined) {
    const parsedArchImpact = nonEmptyArchImpact(archImpact);
    if (parsedArchImpact) spec.arch_impact = parsedArchImpact;
  }

  return spec;
}

export interface BoardColumns {
  byState: Record<DevTaskState, Bead[]>;
  /**
   * Tasks whose state is not one of DEV_TASK_STATES. Substrate does not
   * constrain `state`, so a worker writing "in_progress" instead of "doing"
   * would otherwise vanish from the board entirely — a silently lost work
   * item is worse than an ugly extra column.
   */
  unknown: Bead[];
}

const isKnownState = (s: string): s is DevTaskState =>
  (DEV_TASK_STATES as readonly string[]).includes(s);

export function groupTasksByState(tasks: Bead[]): BoardColumns {
  const byState = Object.fromEntries(DEV_TASK_STATES.map((s) => [s, [] as Bead[]])) as Record<
    DevTaskState,
    Bead[]
  >;
  const unknown: Bead[] = [];

  for (const task of tasks) {
    if (isKnownState(task.state)) byState[task.state].push(task);
    else unknown.push(task);
  }

  // Newest first within a column, matching every other list in the console.
  const byCreatedDesc = (a: Bead, b: Bead) =>
    (b.created_at ?? "").localeCompare(a.created_at ?? "");
  for (const state of DEV_TASK_STATES) byState[state].sort(byCreatedDesc);
  unknown.sort(byCreatedDesc);

  return { byState, unknown };
}

/** Notes oldest-first — a thread reads top to bottom, unlike the card lists. */
export function sortThread(notes: Bead[]): Bead[] {
  return [...notes].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
}

/**
 * Question notes with no answer that explicitly declares release.
 *
 * Mirrors apps/factory-dispatcher/guards.py::open_questions exactly. Used
 * only by TaskThreadPanel, to decide whether a given note still needs a
 * reply widget — a UI affordance over the thread the user is already
 * looking at, not a "can this task start" determination (that comes solely
 * from taskRunnabilityFrom / the served answer; see FactoryBoard.tsx). There
 * is no served per-note equivalent to defer to for this, so this stays a
 * local read of the thread already loaded for display. Keep this in sync
 * with guards.py's own rule (PRIN-011): an answer referencing a question via
 * `answers_ref` does not by itself close it — only `releases_work: true`
 * does. An answer missing the field, or carrying `false`, leaves the
 * question open.
 */
export function openQuestions(notes: Bead[]): Bead[] {
  const released = new Set<string>();
  for (const note of notes) {
    const c = noteContent(note);
    if (c.kind === "answer" && c.answers_ref && c.releases_work === true) {
      released.add(c.answers_ref);
    }
  }
  return sortThread(notes).filter((n) => {
    const c = noteContent(n);
    return c.kind === "question" && !released.has(n.id);
  });
}

/**
 * Open questions marked `blocking: true` — mirrors
 * apps/factory-dispatcher/guards.py::blocking_questions exactly.
 * `guards.is_runnable` only ever names the *first* of these (truncated to
 * 120 chars) in its served `reason` string, so a task held on more than one
 * at once would otherwise lose the rest. Still not a runnability decision —
 * see `openQuestions` above for why this local mirror is tolerated (kept in
 * sync with guards.py's own rule, PRIN-011): whether the card reads as
 * blocked at all comes solely from `taskRunnabilityFrom` / the served
 * answer, never from this list's length.
 */
export function blockingQuestions(notes: Bead[]): Bead[] {
  return openQuestions(notes).filter((n) => noteContent(n).blocking === true);
}

/**
 * The content payload a board reply writes as a `dev.note` answer.
 * `releases` is written explicitly either way — never omitted — so a
 * deliberate hold (`releases_work: false`) is recorded distinctly from a
 * question nobody has answered at all, and a deliberate release
 * (`releases_work: true`) is the only thing `guards.open_questions` accepts.
 */
export function buildAnswerNoteContent(
  questionId: string,
  body: string,
  releases: boolean,
): DevNoteContent {
  return { kind: "answer", body, answers_ref: questionId, releases_work: releases };
}

/**
 * Ids of open questions that already carry at least one answer, none of
 * which released the work. Used only by TaskThreadPanel (see openQuestions
 * above for why this stays local) — without it, a non-releasing answer looks
 * identical to no answer at all in the reply UI.
 */
export function answeredWithoutRelease(notes: Bead[]): Set<string> {
  const open = new Set(openQuestions(notes).map((n) => n.id));
  const answered = new Set<string>();
  for (const note of notes) {
    const c = noteContent(note);
    if (c.kind === "answer" && c.answers_ref && open.has(c.answers_ref)) {
      answered.add(c.answers_ref);
    }
  }
  return answered;
}

/**
 * Raw `predecessor_bead_ids` off task content, for the reverse "who does
 * this block" index below. This is a declared-field read for display, the
 * same category as `releaseBadgeFor` reading `outcome_ref` off content — it
 * does not decide whether *this* task can start. That decision comes only
 * from `taskRunnabilityFrom` (see below), never from this field locally.
 */
function predecessorBeadIds(task: Bead): string[] {
  const raw = taskContent(task).predecessor_bead_ids ?? [];
  return raw.map((id) => String(id).trim()).filter(Boolean);
}

/**
 * Which tasks name `bead_id` as a predecessor — the same ordering edge read
 * in reverse. Mirrors apps/factory-dispatcher/guards.py::dependents_of, but
 * indexes every task in one pass instead of rescanning per card — the board
 * already pays O(2) requests total for tasks/notes (see the module comment
 * above); this keeps the read side O(n) rather than O(n^2) as the board
 * grows. Unlike whether-a-task-can-start (see taskRunnabilityFrom), there is
 * no served surface for "who depends on me" to defer to, so reading the
 * declared field directly is the only implementation there is.
 */
export function dependentsByPredecessorId(tasks: Bead[]): Map<string, Bead[]> {
  const map = new Map<string, Bead[]>();
  for (const task of tasks) {
    for (const predecessorId of predecessorBeadIds(task)) {
      const bucket = map.get(predecessorId);
      if (bucket) bucket.push(task);
      else map.set(predecessorId, [task]);
    }
  }
  return map;
}

/**
 * The wire shape `POST /api/v1/factory/task_runnable` returns (mcp-hub's
 * `tools.factory_status.task_runnability`, which calls
 * `apps/factory-dispatcher/guards.py::is_runnable` verbatim — never
 * re-derived here). `status: "unknown"` means the authority itself could
 * not be reached (no checkout, no substrate); `found: false` means this
 * board has a task id the dispatcher's own task list does not.
 *
 * `code` (OPS-110) distinguishes *why* it's unknown: `"not_configured"` is
 * this service's permanent, by-design production state (its image never
 * ships the factory-dispatcher checkout) — a fact about the deployment, not
 * a fault — while `"unavailable"` (or its absence, from a pre-OPS-110
 * server) is a checkout that is configured but not reachable right now.
 */
export interface TaskRunnabilityResponse {
  status: "ok" | "unknown";
  code?: "not_configured" | "unavailable";
  found?: boolean;
  runnable?: boolean;
  reason?: string;
  detail?: string;
}

/**
 * What a card renders: `guards.is_runnable`'s verdict, or `unknown` when the
 * served answer could not be obtained. This is the *only* place the board
 * interprets the served payload, and it deliberately never inspects the
 * *content* of `reason` — only whether the call succeeded and what
 * `runnable` said. That is what keeps a `guards.py` rule change alone
 * sufficient to change what the board shows: there is no reason-text
 * matching here to go stale the way the old note/predecessor re-derivation
 * did (OPS-40).
 *
 * `not_served_here` is split out from `unknown` for the same reason
 * release-view.ts's `ReleaseDelivery` splits it (OPS-110): this deployment
 * never shipping the checkout is a permanent, by-design absence, not a
 * failed request that might succeed on retry.
 */
export type TaskRunnability =
  | { kind: "runnable" }
  | { kind: "blocked"; reason: string }
  | { kind: "unknown" }
  | { kind: "not_served_here" };

export function taskRunnabilityFrom(
  response: TaskRunnabilityResponse | undefined,
): TaskRunnability {
  if (response?.status === "unknown" && response.code === "not_configured") {
    return { kind: "not_served_here" };
  }
  if (!response || response.status !== "ok" || response.found !== true) {
    return { kind: "unknown" };
  }
  if (response.runnable) {
    return { kind: "runnable" };
  }
  return { kind: "blocked", reason: response.reason?.trim() || "blocked" };
}

/** Notes grouped by the task they hang off, for a one-pass board render. */
export function notesByParent(notes: Bead[]): Map<string, Bead[]> {
  const map = new Map<string, Bead[]>();
  for (const note of notes) {
    if (!note.parent_id) continue;
    const bucket = map.get(note.parent_id);
    if (bucket) bucket.push(note);
    else map.set(note.parent_id, [note]);
  }
  return map;
}

/**
 * Which arch.release charter a task delivers, resolved from `delivers`
 * bead_link rows — never from a copy on the task itself. Reuses the same
 * shape `release-status.py:gather_live_data` builds: one entry per task with
 * a resolved edge, keyed by the *charter's* incoming links so the fan-out is
 * bounded by the (small) number of open releases, not by the number of
 * tasks on the board.
 */
export function deliversMapFromLinks(
  charters: Bead[],
  linksByCharterId: Map<string, BeadLink[]>,
): Map<string, Bead> {
  const map = new Map<string, Bead>();
  for (const charter of charters) {
    for (const link of linksByCharterId.get(charter.id) ?? []) {
      if (link.link_type !== "delivers") continue;
      map.set(link.source_id, charter);
    }
  }
  return map;
}

export type ReleaseBadgeStatus = "delivering" | "waived" | "unknown";

/**
 * What a card should say about the release/outcome its work serves.
 *
 * `delivering` only ever happens when a `delivers` edge resolved to a loaded
 * arch.release charter — `outcome_ref` on the task is looked up against that
 * charter's own `outcomes[]`, never trusted alone, because it is a bare id
 * (`O-2`) the schema itself treats as meaningless without the edge.
 *
 * `waived` and `unknown` are deliberately distinct: a written
 * `release_ref_waived` is a design decision to revisit, while `unknown`
 * covers a task with neither an edge nor a waiver — the pre-2026-08-25
 * grandfathered population, or an edge that failed to resolve. Collapsing
 * either into the other would hide the thing the card exists to show.
 */
export interface ReleaseBadge {
  status: ReleaseBadgeStatus;
  releaseRef?: string;
  releaseName?: string;
  outcomeId?: string;
  outcomeStatement?: string;
  workClass?: WorkClass;
  waiverReason?: string;
}

export function releaseBadgeFor(task: Bead, releaseCharter: Bead | undefined): ReleaseBadge {
  if (releaseCharter) {
    const charterContent = releaseContent(releaseCharter);
    const outcomeRef = taskContent(task).outcome_ref ?? undefined;
    const outcome = charterContent.outcomes?.find((o) => o.id === outcomeRef);
    return {
      status: "delivering",
      releaseRef: charterContent.ref,
      releaseName: charterContent.name,
      outcomeId: outcome?.id,
      outcomeStatement: outcome?.statement,
      workClass: outcome?.work_class,
    };
  }

  const waiverReason = (taskContent(task).release_ref_waived ?? "").trim();
  if (waiverReason) {
    return { status: "waived", waiverReason };
  }

  return { status: "unknown" };
}

export const COLUMN_LABEL: Record<DevTaskState, string> = {
  pending: "Pending",
  doing: "Doing",
  review: "Review",
  done: "Done",
  failed: "Failed",
  superseded: "Superseded",
  archived: "Archived",
};

export const NOTE_KIND_TONE: Record<string, "neutral" | "accent" | "pos" | "neg" | "warn"> = {
  comment: "neutral",
  question: "warn",
  answer: "pos",
  status: "accent",
  attachment: "neutral",
  review: "accent",
};
