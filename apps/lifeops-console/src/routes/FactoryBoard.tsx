import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { factoryStatusClient, refusalMessage } from "@/providers/factory-status-client";
import type { Bead, DevTaskState } from "@/types/bead";
import { DEV_LANES } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody } from "@/components/ui/Card";
import { Badge, stateTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input, Select, Textarea } from "@/components/ui/Input";
import { TaskThreadPanel } from "@/components/TaskThreadPanel";
import {
  CLOSED_DEV_TASK_STATES,
  COLUMN_LABEL,
  FACTORY_BOARD_REFRESH_INTERVAL_MS,
  OPEN_DEV_TASK_STATES,
  blockingQuestions,
  buildTaskFilingSpec,
  deliversMapFromLinks,
  dependentsByPredecessorId,
  groupTasksByState,
  noteContent,
  notesByParent,
  releaseBadgeFor,
  taskContent,
  taskRunnabilityFrom,
  type ReleaseBadge,
  type TaskIntakeDraft,
  type TaskRunnability,
} from "@/lib/dev-board";
import {
  WORKER_REVISION_DRIFT_REF,
  factoryScheduleViewFrom,
  queueCountsFrom,
  queueViewFrom,
  workerRevisionViewFrom,
  type FactoryScheduleView,
  type QueueView,
  type WorkerRevisionView,
} from "@/lib/factory-status";
import { fmtAge } from "@/lib/format";
import { cn } from "@/lib/cn";

// The factory board — dev.task beads as kanban columns, dev.note threads as
// the collaboration record. Replaces the parked Vikunja projection: the board
// is a read over the substrate, not a second data store (ARCHITECTURE §3.1).
//
// Two queries, not one-per-card: pulling every dev.note once and grouping by
// parent_id client-side keeps the board at O(2) requests regardless of how
// many tasks are on it. Substrate caches list reads for 5 minutes but
// invalidates on write, so a posted answer shows up immediately.

const QK = ["dev-board"];
const FETCH_LIMIT = 500;
const TASKS_QK = [...QK, "tasks"] as const;
const NOTES_QK = [...QK, "notes"] as const;

interface BoardQueryState<T> {
  data: T | undefined;
  isError: boolean;
  isRefetchError?: boolean;
  error: unknown;
}

export function factoryBoardTasksQueryOptions() {
  return {
    queryKey: TASKS_QK,
    queryFn: () =>
      substrateClient.listBeads({ namespace: "dev", type: "task", limit: FETCH_LIMIT }),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  };
}

export function factoryBoardNotesQueryOptions() {
  return {
    queryKey: NOTES_QK,
    queryFn: () =>
      substrateClient.listBeads({ namespace: "dev", type: "note", limit: FETCH_LIMIT }),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  };
}

// arch.release charters, the same read ReleaseCoverageCard/ProjectAlpha
// already make — reused, not duplicated. Which task delivers which charter
// is resolved below by fetching each charter's *incoming* `delivers` links,
// mirroring release-status.py:gather_live_data: the fan-out is bounded by
// the number of open releases (a handful), never by the number of tasks.
const RELEASE_CHARTERS_QK = [...QK, "release-charters"] as const;

export function factoryBoardReleaseChartersQueryOptions() {
  return {
    queryKey: RELEASE_CHARTERS_QK,
    queryFn: () =>
      substrateClient.listBeads({ namespace: "arch", type: "release", limit: FETCH_LIMIT }),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  };
}

// Whether the factory is running (S54-B / R26.09 O-10, read half only) —
// the dispatch schedule's own pause/note/recent-run state, read through the
// gateway's schedule_status route rather than a terminal session on the
// dispatcher host.
const SCHEDULE_STATUS_QK = [...QK, "schedule-status"] as const;

export function factoryBoardScheduleStatusQueryOptions() {
  return {
    queryKey: SCHEDULE_STATUS_QK,
    queryFn: () => factoryStatusClient.scheduleStatus(),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  };
}

// The one standing obs.worker-revision-drift bead, fetched by its unique
// content.ref rather than paging every arch.observation bead the way
// Architecture.tsx does for the generic case — see substrate-client.ts's
// content_ref param.
const WORKER_REVISION_DRIFT_QK = [...QK, "worker-revision-drift"] as const;

export function factoryBoardWorkerRevisionDriftQueryOptions() {
  return {
    queryKey: WORKER_REVISION_DRIFT_QK,
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "arch",
        type: "observation",
        content_ref: WORKER_REVISION_DRIFT_REF,
        limit: 1,
      }),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  };
}

export function boardHasLoadedData(
  tasksQuery: BoardQueryState<Bead[]>,
  notesQuery: BoardQueryState<Bead[]>,
): boolean {
  return tasksQuery.data !== undefined && notesQuery.data !== undefined;
}

export function boardInitialLoadFailed(
  tasksQuery: BoardQueryState<Bead[]>,
  notesQuery: BoardQueryState<Bead[]>,
): boolean {
  return (
    !boardHasLoadedData(tasksQuery, notesQuery) &&
    ((tasksQuery.data === undefined && tasksQuery.isError) ||
      (notesQuery.data === undefined && notesQuery.isError))
  );
}

export function boardRefreshFailed(
  tasksQuery: BoardQueryState<Bead[]>,
  notesQuery: BoardQueryState<Bead[]>,
): boolean {
  const failedWithTasksData =
    tasksQuery.data !== undefined &&
    (tasksQuery.isError || tasksQuery.isRefetchError === true || tasksQuery.error != null);
  const failedWithNotesData =
    notesQuery.data !== undefined &&
    (notesQuery.isError || notesQuery.isRefetchError === true || notesQuery.error != null);
  return failedWithTasksData || failedWithNotesData;
}

export function taskForOpenDetail(selected: Bead | null, tasks: Bead[]): Bead | null {
  if (!selected) return null;
  return tasks.find((t) => t.id === selected.id) ?? selected;
}

const emptyDraft = (): TaskIntakeDraft => ({
  lane: "code-health",
  title: "",
  intent: "",
  acceptance: "",
  scopePaths: "",
  forbiddenPaths: "",
  risk_class: "behavioral",
  verificationCommands: "",
  requirementRefs: "",
  nfrs: "",
  archImpact: "",
});

export function FactoryBoardRoute() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Bead | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<TaskIntakeDraft>(() => emptyDraft());
  const [formError, setFormError] = useState<string | null>(null);
  const [showClosed, setShowClosed] = useState(false);

  const tasksQuery = useQuery(factoryBoardTasksQueryOptions());

  const notesQuery = useQuery(factoryBoardNotesQueryOptions());

  // Computed here, ahead of the queue half below, so queueViewFrom can be
  // driven by the same load/error facts the board's own loading/error
  // branches use further down — not a second computation of the same thing.
  const hasLoadedData = boardHasLoadedData(tasksQuery, notesQuery);
  const isLoading = !hasLoadedData && (tasksQuery.isLoading || notesQuery.isLoading);
  const isInitialError = boardInitialLoadFailed(tasksQuery, notesQuery);
  const isPossiblyStale = hasLoadedData && boardRefreshFailed(tasksQuery, notesQuery);

  const releaseCharterQuery = useQuery(factoryBoardReleaseChartersQueryOptions());
  const releaseCharters = useMemo(
    () => releaseCharterQuery.data ?? [],
    [releaseCharterQuery.data],
  );

  const deliversLinksQuery = useQuery({
    queryKey: [...QK, "delivers-links", releaseCharters.map((c) => c.id).sort().join("|")],
    enabled: releaseCharters.length > 0,
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
    queryFn: async () => {
      const groups = await Promise.all(
        releaseCharters.map((charter) =>
          substrateClient.listBeadLinks(charter.id, {
            direction: "incoming",
            link_type: "delivers",
          }),
        ),
      );
      return releaseCharters.map((charter, i) => [charter.id, groups[i]] as const);
    },
  });

  const releaseByTaskId = useMemo(
    () => deliversMapFromLinks(releaseCharters, new Map(deliversLinksQuery.data ?? [])),
    [releaseCharters, deliversLinksQuery.data],
  );

  // Files through the M9 gateway capability (factoryStatusClient.fileTask,
  // POST /api/v1/factory/tasks) instead of a raw substrate write — see
  // .factory/design.md. The route returns file_dev_task's summary, not a
  // full bead, so success re-reads the board's own tasks query rather than
  // seeding the cache from a partial shape.
  const createTask = useMutation({
    mutationFn: (nextDraft: TaskIntakeDraft) =>
      factoryStatusClient.fileTask(buildTaskFilingSpec(nextDraft)),
    onSuccess: async (filed) => {
      await queryClient.invalidateQueries({ queryKey: TASKS_QK });
      const current = queryClient.getQueryData<Bead[]>(TASKS_QK) ?? [];
      const filedTask = current.find((task) => task.id === filed.id) ?? null;
      setDraft(emptyDraft());
      setFormError(null);
      setFormOpen(false);
      setSelected(filedTask);
    },
  });

  const tasks = useMemo(() => tasksQuery.data ?? [], [tasksQuery.data]);
  const columns = useMemo(() => groupTasksByState(tasks), [tasks]);
  const threads = useMemo(() => notesByParent(notesQuery.data ?? []), [notesQuery.data]);
  // The reverse predecessor_bead_ids edge ("who does this block"), indexed
  // once so every card resolves it in O(1), not by rescanning the whole
  // board per card.
  const dependentsIndex = useMemo(() => dependentsByPredecessorId(tasks), [tasks]);

  // Whether each task can start is read from the dispatcher's own authority
  // (guards.is_runnable), never re-derived here — see .factory/design.md.
  // Closed work (done/superseded/archived) cannot be "movable", so it is
  // excluded from the fan-out rather than queried for no purpose.
  const closedStateSet = useMemo(() => new Set<string>(CLOSED_DEV_TASK_STATES), []);
  const runnabilityTasks = useMemo(
    () => tasks.filter((t) => !closedStateSet.has(t.state)),
    [tasks, closedStateSet],
  );
  const runnabilityQueries = useQueries({
    queries: runnabilityTasks.map((t) => ({
      queryKey: [...QK, "runnable", t.id] as const,
      queryFn: () => factoryStatusClient.taskRunnable(t.id),
      refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
    })),
  });
  const runnabilityByTaskId = useMemo(() => {
    const map = new Map<string, TaskRunnability>();
    runnabilityTasks.forEach((t, i) => {
      map.set(t.id, taskRunnabilityFrom(runnabilityQueries[i]?.data));
    });
    return map;
  }, [runnabilityTasks, runnabilityQueries]);

  const blockedCount = useMemo(
    () => Array.from(runnabilityByTaskId.values()).filter((r) => r.kind === "blocked").length,
    [runnabilityByTaskId],
  );
  const closedCount = CLOSED_DEV_TASK_STATES.reduce(
    (sum, state) => sum + columns.byState[state].length,
    0,
  );

  // Is the factory running (S54-B / R26.09 O-10, read half only) — the
  // schedule half (Temporal, via the gateway), the queue half (this board's
  // own already-fetched tasks + served runnability), and the worker half
  // (the one standing obs.worker-revision-drift bead). See
  // .factory/design.md for why each is sourced the way it is, and why
  // `paused`/`in-flight` and `state`/`context` are never collapsed into a
  // single health flag.
  const scheduleStatusQuery = useQuery(factoryBoardScheduleStatusQueryOptions());
  const scheduleView = useMemo(
    () => factoryScheduleViewFrom(scheduleStatusQuery.data, scheduleStatusQuery.error),
    [scheduleStatusQuery.data, scheduleStatusQuery.error],
  );

  const workerRevisionDriftQuery = useQuery(factoryBoardWorkerRevisionDriftQueryOptions());
  const workerView = useMemo(
    () => workerRevisionViewFrom(workerRevisionDriftQuery.data, workerRevisionDriftQuery.error),
    [workerRevisionDriftQuery.data, workerRevisionDriftQuery.error],
  );

  const queueCounts = useMemo(
    () => queueCountsFrom(tasks, runnabilityByTaskId),
    [tasks, runnabilityByTaskId],
  );
  const queueView = useMemo(
    () => queueViewFrom(hasLoadedData, isInitialError, queueCounts),
    [hasLoadedData, isInitialError, queueCounts],
  );

  const updateDraft = (field: keyof TaskIntakeDraft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  const submitTask = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    try {
      buildTaskFilingSpec(draft);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Task intake was refused.");
      return;
    }
    createTask.mutate(draft, {
      onError: (error) => {
        setFormError(refusalMessage(error, "Task intake failed."));
      },
    });
  };

  // Keep the open panel's task object fresh across refetches, so a state
  // change lands in the panel header without closing it.
  const selectedTask = taskForOpenDetail(selected, tasks);

  return (
    <>
      <PageHeader
        title="Factory Board"
        subtitle="dev.task work items and their collaboration threads"
        right={
          <div className="flex items-center gap-2">
            {blockedCount > 0 ? (
              <Badge tone="warn">
                {blockedCount} blocked
              </Badge>
            ) : null}
            {isPossiblyStale ? (
              <Badge tone="warn">Refresh failed; showing cached board</Badge>
            ) : null}
            <span className="text-2xs text-fg-subtle num">{tasks.length} tasks</span>
            {closedCount > 0 ? (
              <Button size="sm" variant="ghost" onClick={() => setShowClosed((shown) => !shown)}>
                {showClosed ? "Hide closed" : `Show closed (${closedCount})`}
              </Button>
            ) : null}
            <Button size="sm" onClick={() => setFormOpen((open) => !open)}>
              New task
            </Button>
          </div>
        }
      />

      <div className="px-6 pt-6">
        <FactoryStatusPanel schedule={scheduleView} queue={queueView} worker={workerView} />
      </div>

      <div className="p-6 space-y-4">
        {formOpen ? (
          <TaskIntakeForm
            draft={draft}
            error={formError}
            isSaving={createTask.isPending}
            onChange={updateDraft}
            onCancel={() => {
              setFormOpen(false);
              setFormError(null);
            }}
            onSubmit={submitTask}
          />
        ) : null}

        {isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Loading…</CardBody>
          </Card>
        ) : isInitialError ? (
          <Card>
            <CardBody className="text-center text-neg py-8">
              Failed to load the board.
            </CardBody>
          </Card>
        ) : tasks.length === 0 ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              <div className="text-sm">No dev.task beads yet.</div>
              <div className="text-2xs text-fg-subtle mt-2 max-w-prose mx-auto">
                The board projects <span className="font-mono">dev.task</span> beads grouped
                by state; file one from the intake form to see it here.
              </div>
            </CardBody>
          </Card>
        ) : (
          <div className="space-y-4">
            <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
              {OPEN_DEV_TASK_STATES.map((state) => (
                <Column
                  key={state}
                  label={COLUMN_LABEL[state]}
                  state={state}
                  tasks={columns.byState[state]}
                  threads={threads}
                  releaseByTaskId={releaseByTaskId}
                  runnabilityByTaskId={runnabilityByTaskId}
                  dependentsIndex={dependentsIndex}
                  onSelect={setSelected}
                />
              ))}
              {columns.unknown.length > 0 ? (
                <Column
                  label="Off-convention"
                  state={null}
                  tasks={columns.unknown}
                  threads={threads}
                  releaseByTaskId={releaseByTaskId}
                  runnabilityByTaskId={runnabilityByTaskId}
                  dependentsIndex={dependentsIndex}
                  onSelect={setSelected}
                />
              ) : null}
            </div>

            {showClosed && closedCount > 0 ? (
              <div className="border-t border-border pt-3">
                <div className="text-2xs uppercase text-fg-subtle tracking-wide mb-2">
                  Closed
                </div>
                <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
                  {CLOSED_DEV_TASK_STATES.map((state) => (
                    <Column
                      key={state}
                      label={COLUMN_LABEL[state]}
                      state={state}
                      tasks={columns.byState[state]}
                      threads={threads}
                      releaseByTaskId={releaseByTaskId}
                      runnabilityByTaskId={runnabilityByTaskId}
                      dependentsIndex={dependentsIndex}
                      onSelect={setSelected}
                    />
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        )}
      </div>

      {selectedTask ? (
        <TaskThreadPanel task={selectedTask} onClose={() => setSelected(null)} />
      ) : null}
    </>
  );
}

// "Is the factory running" — S54-B / R26.09 O-10. Three independent facts,
// each rendered as what it is rather than folded into one verdict:
// schedule (paused, and separately in-flight — never `genuinely_quiet`'s
// conjunction), queue (held vs ready-excluding-breaker vs unknown, each
// labeled for what it does and doesn't mean), and worker (the one standing
// drift observation, state read alongside context so a resolved record
// never renders as current). See .factory/design.md.
export function FactoryStatusPanel({
  schedule,
  queue,
  worker,
}: {
  schedule: FactoryScheduleView;
  queue: QueueView;
  worker: WorkerRevisionView;
}) {
  return (
    <Card>
      <CardBody className="grid gap-4 md:grid-cols-3 text-xs">
        <div>
          <div className="panel-title mb-1.5">Dispatch schedule</div>
          {schedule.kind === "unknown" ? (
            <div className="text-fg-subtle">Unknown — {schedule.reason}</div>
          ) : (
            <div className="space-y-1">
              <div className="flex items-center gap-1.5">
                <Badge tone={schedule.paused ? "warn" : "pos"}>
                  {schedule.paused ? "paused" : "not paused"}
                </Badge>
                <Badge tone={schedule.inFlightCount > 0 ? "accent" : "neutral"}>
                  {schedule.inFlightCount} in flight
                </Badge>
              </div>
              {schedule.note ? (
                <div className="text-2xs text-fg-subtle">{schedule.note}</div>
              ) : null}
              <div className="text-2xs text-fg-subtle">
                {schedule.recentFailures > 0
                  ? `${schedule.recentFailures} of ${schedule.recent.length} recent run(s) failed` +
                    (schedule.consecutiveFailures > 0
                      ? ` (${schedule.consecutiveFailures} in a row)`
                      : "")
                  : schedule.recent.length > 0
                    ? `${schedule.recent.length} recent run(s), all succeeded`
                    : "No recent runs recorded"}
              </div>
            </div>
          )}
        </div>

        <div>
          <div className="panel-title mb-1.5">Queue</div>
          {queue.kind === "unknown" ? (
            <div className="text-fg-subtle">Unknown — {queue.reason}</div>
          ) : (
            <div className="space-y-0.5 text-2xs">
              <div>{queue.byState.pending} pending</div>
              <div>{queue.byState.doing} doing</div>
              <div>{queue.byState.review} in review</div>
              <div>{queue.byState.failed} failed</div>
              <div className="pt-1 text-fg-subtle">
                of pending: {queue.pendingHeld} held, {queue.pendingReadyExcludingBreaker} ready
                (excludes the environmental-fault breaker), {queue.pendingUnknown} unknown
              </div>
            </div>
          )}
        </div>

        <div>
          <div className="panel-title mb-1.5">Worker revision</div>
          {worker.kind === "unknown" ? (
            <div className="text-fg-subtle">Unknown — {worker.reason}</div>
          ) : worker.kind === "not_found" ? (
            <div className="text-fg-subtle">No drift-check record found yet.</div>
          ) : worker.kind === "active" ? (
            <div className="space-y-0.5 text-2xs">
              <div>
                <Badge tone={worker.condition === "drifted" ? "warn" : "neutral"}>
                  {worker.condition}
                </Badge>
              </div>
              <div className="font-mono">{worker.workerRevision ?? "unknown"}</div>
              {worker.commitsBehind !== null ? (
                <div>{worker.commitsBehind} commits behind main</div>
              ) : null}
              <div className="text-fg-subtle">as of {worker.lastObservedAt || "unknown"}</div>
            </div>
          ) : (
            <div className="space-y-0.5 text-2xs">
              <Badge tone="pos">resolved</Badge>
              <div className="text-fg-subtle">
                Last known revision {worker.lastKnownWorkerRevision ?? "unknown"}
                {worker.lastKnownCommitsBehind !== null
                  ? ` (${worker.lastKnownCommitsBehind} behind)`
                  : ""}{" "}
                as of {worker.asOfLastDriftedObservation || "unknown"} — historical, not
                confirmation of the current revision.
              </div>
            </div>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

export function TaskIntakeForm({
  draft,
  error,
  isSaving,
  onChange,
  onCancel,
  onSubmit,
}: {
  draft: TaskIntakeDraft;
  error: string | null;
  isSaving: boolean;
  onChange: (field: keyof TaskIntakeDraft, value: string) => void;
  onCancel: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <Card>
      <CardBody>
        <form className="space-y-3" onSubmit={onSubmit}>
          <div className="grid gap-3 md:grid-cols-[1fr_160px_160px]">
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Title</span>
              <Input
                value={draft.title}
                onChange={(event) => onChange("title", event.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Lane</span>
              <Select
                value={draft.lane}
                onChange={(event) => onChange("lane", event.target.value)}
              >
                {DEV_LANES.map((lane) => (
                  <option key={lane} value={lane}>
                    {lane}
                  </option>
                ))}
              </Select>
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Risk</span>
              <Select
                value={draft.risk_class}
                onChange={(event) => onChange("risk_class", event.target.value)}
              >
                <option value="behavioral">behavioral</option>
                <option value="structural">structural</option>
              </Select>
            </label>
          </div>

          <label className="block space-y-1">
            <span className="text-2xs text-fg-muted">Intent</span>
            <Textarea
              rows={3}
              value={draft.intent}
              onChange={(event) => onChange("intent", event.target.value)}
            />
          </label>

          <div className="grid gap-3 md:grid-cols-2">
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Acceptance</span>
              <Textarea
                rows={4}
                value={draft.acceptance}
                onChange={(event) => onChange("acceptance", event.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Verification</span>
              <Textarea
                rows={4}
                value={draft.verificationCommands}
                onChange={(event) => onChange("verificationCommands", event.target.value)}
              />
            </label>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Scope paths</span>
              <Textarea
                rows={4}
                value={draft.scopePaths}
                onChange={(event) => onChange("scopePaths", event.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Forbidden paths</span>
              <Textarea
                rows={4}
                value={draft.forbiddenPaths}
                onChange={(event) => onChange("forbiddenPaths", event.target.value)}
              />
            </label>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Requirement refs</span>
              <Textarea
                rows={3}
                value={draft.requirementRefs}
                onChange={(event) => onChange("requirementRefs", event.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">NFRs</span>
              <Textarea
                rows={3}
                value={draft.nfrs}
                onChange={(event) => onChange("nfrs", event.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="text-2xs text-fg-muted">Arch impact</span>
              <Textarea
                rows={3}
                value={draft.archImpact}
                onChange={(event) => onChange("archImpact", event.target.value)}
              />
            </label>
          </div>

          {error ? <div className="text-xs text-neg">{error}</div> : null}

          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onCancel} disabled={isSaving}>
              Cancel
            </Button>
            <Button type="submit" disabled={isSaving}>
              {isSaving ? "Filing..." : "File task"}
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  );
}

function Column({
  label,
  state,
  tasks,
  threads,
  releaseByTaskId,
  runnabilityByTaskId,
  dependentsIndex,
  onSelect,
}: {
  label: string;
  state: DevTaskState | null;
  tasks: Bead[];
  threads: Map<string, Bead[]>;
  releaseByTaskId: Map<string, Bead>;
  runnabilityByTaskId: Map<string, TaskRunnability>;
  dependentsIndex: Map<string, Bead[]>;
  onSelect: (b: Bead) => void;
}) {
  return (
    <div className="flex-shrink-0 w-[280px] sm:w-[300px]">
      <div className="flex items-center justify-between px-1 pb-2">
        <span className="panel-title">{label}</span>
        <span className="text-2xs text-fg-subtle num">{tasks.length}</span>
      </div>
      <div
        className={cn(
          "space-y-2 rounded border border-dashed p-2 min-h-[120px] bg-bg-subtle/30",
          state === null ? "border-neg/40" : "border-border/60",
        )}
      >
        {tasks.length === 0 ? (
          <div className="text-2xs text-fg-subtle text-center py-6">Empty</div>
        ) : (
          tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              notes={threads.get(task.id) ?? []}
              dependents={dependentsIndex.get(task.id) ?? []}
              showState={state === null}
              releaseCharter={releaseByTaskId.get(task.id)}
              runnability={runnabilityByTaskId.get(task.id)}
              onSelect={() => onSelect(task)}
            />
          ))
        )}
      </div>
    </div>
  );
}

export function TaskCard({
  task,
  notes,
  dependents,
  showState,
  releaseCharter,
  runnability,
  onSelect,
}: {
  task: Bead;
  notes: Bead[];
  dependents: Bead[];
  showState: boolean;
  releaseCharter?: Bead;
  // Undefined means this card's state is closed and terminal (done,
  // superseded, archived) — closed work is never queried for runnability
  // (see FactoryBoardRoute's runnabilityTasks) and shows no notice at all,
  // rather than a misleading "unknown".
  runnability?: TaskRunnability;
  onSelect: () => void;
}) {
  const c = taskContent(task);
  const attempts = c.attempts ?? 0;
  const maxAttempts = c.max_attempts ?? 3;
  const release = releaseBadgeFor(task, releaseCharter);
  // Additional open blocking questions beyond whichever one the authority's
  // single `reason` string names — only computed (and only rendered) when
  // the served answer already says this card is blocked; see
  // .factory/design.md.
  const extraBlockingNotes = runnability?.kind === "blocked" ? blockingQuestions(notes) : [];

  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full text-left card p-2.5 transition-colors hover:bg-bg-hover",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent",
        runnability?.kind === "blocked" ? "border-warn" : null,
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5 mb-1.5">
        <Badge tone="accent">{c.lane}</Badge>
        {showState ? <Badge tone={stateTone(task.state)}>{task.state}</Badge> : null}
        {c.autonomy === "auto-merge-eligible" ? <Badge tone="warn">auto-merge</Badge> : null}
        {dependents.length > 0 ? (
          <Badge
            tone="warn"
            title={`Blocks: ${dependents.map((d) => taskContent(d).title).join(", ")}`}
          >
            blocks {dependents.length}
          </Badge>
        ) : null}
        <span
          className="ml-auto font-mono text-2xs text-fg-subtle select-text"
          title={task.id}
          onClick={(event) => event.stopPropagation()}
        >
          {task.id.slice(0, 8)}
        </span>
      </div>

      <div className="text-xs text-fg leading-snug">{c.title}</div>

      <ReleaseLine release={release} />

      <RunnabilityLine
        taskState={task.state}
        runnability={runnability}
        extraBlockingNotes={extraBlockingNotes}
      />

      <div className="flex items-center justify-between gap-2 mt-2 text-2xs text-fg-subtle">
        <span className="font-mono truncate">
          {`hint:${c.worker_hint ?? "unassigned"}`}
          {c.ran_by
            ? ` · ran:${c.ran_by}`
            : ["review", "done", "failed"].includes(task.state)
              ? " · ran:unrecorded" // pre-stamp work — unknown, never blank
              : null}
        </span>
        <span className="flex items-center gap-2 num flex-shrink-0">
          {notes.length > 0 ? <span>{notes.length} notes</span> : null}
          {attempts > 0 ? (
            <span className={cn(attempts >= maxAttempts ? "text-neg" : null)}>
              {attempts}/{maxAttempts}
            </span>
          ) : null}
          <span>{fmtAge(task.created_at)}</span>
        </span>
      </div>
    </button>
  );
}

// What this card's work is for — resolved by the caller from the `delivers`
// edge (releaseBadgeFor), never read off task content directly. Waived and
// unresolvable both say so in words, not by omission, and render distinctly
// from each other and from a delivering card (R26.02/O-2).
function ReleaseLine({ release }: { release: ReleaseBadge }) {
  if (release.status === "delivering") {
    const label = release.outcomeId
      ? `${release.releaseRef} · ${release.outcomeId}`
      : (release.releaseRef ?? "release");
    return (
      <div
        className="mt-1.5 flex items-center gap-1"
        title={release.outcomeStatement ?? release.releaseName}
      >
        <Badge tone="pos">{label}</Badge>
      </div>
    );
  }

  if (release.status === "waived") {
    return (
      <div className="mt-1.5 flex items-center gap-1.5" title={release.waiverReason}>
        <Badge tone="warn">waived</Badge>
        <span className="text-2xs text-fg-subtle truncate">{release.waiverReason}</span>
      </div>
    );
  }

  return (
    <div className="mt-1.5">
      <Badge tone="neg">release unknown</Badge>
    </div>
  );
}

// Whether this card can start, in the dispatcher's own words — read from
// guards.is_runnable via the served answer, never re-derived here (see
// .factory/design.md). `undefined` (closed/terminal work) renders nothing.
// `unknown` (the served answer could not be fetched) renders distinctly
// from both `blocked` and `runnable` — an unreadable population is neither
// a confirmed block nor safe to show as ready. `not_served_here` (OPS-110)
// renders distinctly again from `unknown`: this mcp-hub deployment never
// shipping the factory-dispatcher checkout is a permanent, by-design fact,
// not a failed request that a refresh might fix.
function RunnabilityLine({
  taskState,
  runnability,
  extraBlockingNotes,
}: {
  taskState: string;
  runnability: TaskRunnability | undefined;
  extraBlockingNotes: Bead[];
}) {
  if (!runnability) return null;

  if (runnability.kind === "runnable") {
    // "Ready to start" only means something for the pending queue — a task
    // already doing/review/failed was not evaluated by the dispatcher's
    // pending-pool pick, so it gets no ready marker either way.
    if (taskState !== "pending") return null;
    return <div className="text-2xs text-pos mt-1.5">✓ Ready to start</div>;
  }

  if (runnability.kind === "blocked") {
    // Every open question not already named by the authority's single reason
    // renders — even one. Hiding a lone question because a different reason
    // (say, a predecessor) took the reason slot is the silent-pick the spec
    // forbids (release-gate finding on #625).
    const renderableNotes = extraBlockingNotes.filter(
      (note) =>
        !runnability.reason.includes(note.id) &&
        !runnability.reason.includes(noteContent(note).body),
    );
    return (
      <div className="mt-1.5">
        <div className="text-2xs text-warn">⏸ Blocked — {runnability.reason}</div>
        {renderableNotes.length > 0 ? (
          <div className="text-2xs text-warn/80 mt-1 space-y-0.5">
            <div>
              {renderableNotes.length} open blocking question
              {renderableNotes.length === 1 ? "" : "s"}:
            </div>
            {renderableNotes.map((note) => (
              <div key={note.id}>• {noteContent(note).body}</div>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  if (runnability.kind === "not_served_here") {
    return (
      <div className="text-2xs text-fg-subtle mt-1.5">
        — Runnability not served in this environment
      </div>
    );
  }

  return (
    <div className="text-2xs text-fg-subtle mt-1.5">
      ❔ Runnability unknown
    </div>
  );
}
