import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import { factoryStatusClient, refusalMessage } from "@/providers/factory-status-client";
import type { Bead, DevNoteContent } from "@/types/bead";
import { Badge, stateTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Textarea } from "@/components/ui/Input";
import {
  NOTE_KIND_TONE,
  FACTORY_BOARD_REFRESH_INTERVAL_MS,
  answeredWithoutRelease,
  buildAnswerNoteContent,
  noteContent,
  openQuestions,
  sortThread,
  taskContent,
} from "@/lib/dev-board";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";

// The collaboration surface for one dev.task. Reads the note thread via
// parent_id and writes dev.note beads back — comments, and answers that
// unblock a stopped worker.
//
// Deliberately does NOT mutate task state. The worker owns the task lifecycle;
// a human moving a card while a run is in flight is a write race with no
// resolution rule. Humans contribute notes, workers advance state.

interface Props {
  task: Bead;
  onClose: () => void;
}

export function TaskThreadPanel({ task, onClose }: Props) {
  const queryClient = useQueryClient();
  const [commentDraft, setCommentDraft] = useState("");
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({});

  const threadKey = ["dev-task-thread", task.id];

  const threadQuery = useQuery({
    queryKey: threadKey,
    queryFn: () =>
      substrateClient.listBeads({
        namespace: "dev",
        type: "note",
        parent_id: task.id,
        limit: 500,
      }),
    refetchInterval: FACTORY_BOARD_REFRESH_INTERVAL_MS,
  });

  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  const notes = useMemo(() => sortThread(threadQuery.data ?? []), [threadQuery.data]);
  const unanswered = useMemo(() => new Set(openQuestions(notes).map((n) => n.id)), [notes]);
  const notReleased = useMemo(() => answeredWithoutRelease(notes), [notes]);

  const addNote = useMutation({
    mutationFn: (content: DevNoteContent) =>
      // Through the gateway's dev.note intake capability, not a direct
      // substrate write: trust_tier ("user", distinguishing a human note
      // from a worker-authored one of the same kind) and created_by
      // (the Access-derived client identity) are the route's to set, not
      // this payload's.
      factoryStatusClient.fileNote({ ...content, parent_id: task.id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: threadKey });
      queryClient.invalidateQueries({ queryKey: ["dev-board"] });
    },
  });

  const submitComment = () => {
    const body = commentDraft.trim();
    if (!body) return;
    addNote.mutate({ kind: "comment", body }, { onSuccess: () => setCommentDraft("") });
  };

  const submitAnswer = (questionId: string, releases: boolean) => {
    const body = (answerDrafts[questionId] ?? "").trim();
    if (!body) return;
    addNote.mutate(buildAnswerNoteContent(questionId, body, releases), {
      onSuccess: () =>
        setAnswerDrafts((prev) => {
          const next = { ...prev };
          delete next[questionId];
          return next;
        }),
    });
  };

  const c = taskContent(task);

  return (
    <>
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm z-30 transition-opacity animate-fade-in"
        onClick={onClose}
      />
      <div className="fixed inset-y-0 right-0 w-full sm:w-[720px] max-w-full sm:max-w-[92vw] bg-bg-panel border-l border-border z-40 flex flex-col shadow-2xl animate-slide-in">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="accent">{c.lane}</Badge>
            <Badge tone={stateTone(task.state)}>{task.state}</Badge>
            {c.autonomy === "auto-merge-eligible" ? (
              <Badge tone="warn">auto-merge</Badge>
            ) : null}
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} className="min-h-[44px]">
            Close ✕
          </Button>
        </div>

        <div className="overflow-auto flex-1 p-4 space-y-5">
          <section>
            <h2 className="text-sm font-semibold text-fg leading-snug">{c.title}</h2>
            <p className="text-xs text-fg-muted mt-1 whitespace-pre-wrap">{c.intent}</p>
            <div className="text-2xs text-fg-subtle mt-2 font-mono select-text">
              {task.id} · {task.created_by} · {fmtDateTime(task.created_at)}
            </div>
          </section>

          {c.pr_url ? (
            <section>
              <div className="panel-title mb-1">Pull request</div>
              {/* rel=noreferrer: the PR host should not see the console URL. */}
              <a
                href={c.pr_url}
                target="_blank"
                rel="noreferrer noopener"
                className="text-xs text-accent hover:underline break-all"
              >
                {c.pr_url}
              </a>
            </section>
          ) : null}

          <section>
            <div className="panel-title mb-1">Acceptance</div>
            <ul className="space-y-1">
              {c.acceptance.map((a, i) => (
                <li key={i} className="text-xs text-fg-muted flex gap-2">
                  <span className="text-fg-subtle select-none">·</span>
                  <span>{a}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="grid grid-cols-2 gap-x-4 gap-y-1 text-2xs">
            <div className="text-fg-muted">risk</div>
            <div className="font-mono">{c.risk_class}</div>
            <div className="text-fg-muted">worker</div>
            <div className="font-mono">{c.worker_hint ?? "unassigned"}</div>
            <div className="text-fg-muted">ran by</div>
            <div className="font-mono">
              {c.ran_by ??
                (["review", "done", "failed"].includes(task.state)
                  ? "unrecorded"  // ran before lane stamping existed — unknown, never blank
                  : "not yet run")}
            </div>
            <div className="text-fg-muted">attempts</div>
            <div className="num">
              {c.attempts ?? 0} / {c.max_attempts ?? 3}
            </div>
            <div className="text-fg-muted">budget</div>
            <div className="num">
              {c.budget.max_agent_minutes}m · ${c.budget.max_usd} · {c.budget.max_tokens}tok
            </div>
            <div className="text-fg-muted">scope</div>
            <div className="font-mono break-all">{c.scope.paths.join(", ")}</div>
          </section>

          <section>
            <div className="panel-title mb-2">
              Thread{" "}
              <span className="text-fg-subtle num font-normal">
                {threadQuery.isLoading ? "" : `(${notes.length})`}
              </span>
            </div>

            {threadQuery.isLoading ? (
              <div className="text-xs text-fg-muted">Loading…</div>
            ) : threadQuery.error ? (
              <div className="text-xs text-neg">Failed to load the thread.</div>
            ) : notes.length === 0 ? (
              <div className="text-xs text-fg-muted">
                No notes yet. Workers post status, questions and review verdicts here.
              </div>
            ) : (
              <ol className="space-y-2">
                {notes.map((note) => (
                  <NoteRow
                    key={note.id}
                    note={note}
                    isOpenQuestion={unanswered.has(note.id)}
                    answeredNotReleased={notReleased.has(note.id)}
                    draft={answerDrafts[note.id] ?? ""}
                    onDraft={(v) =>
                      setAnswerDrafts((prev) => ({ ...prev, [note.id]: v }))
                    }
                    onHold={() => submitAnswer(note.id, false)}
                    onRelease={() => submitAnswer(note.id, true)}
                    pending={addNote.isPending}
                  />
                ))}
              </ol>
            )}
          </section>

          <section>
            <div className="panel-title mb-2">Add a comment</div>
            <Textarea
              rows={3}
              className="w-full"
              placeholder="Context for the worker…"
              value={commentDraft}
              onChange={(e) => setCommentDraft(e.target.value)}
            />
            <div className="flex items-center justify-end gap-2 mt-2">
              <Button
                size="sm"
                disabled={addNote.isPending || !commentDraft.trim()}
                onClick={submitComment}
              >
                {addNote.isPending ? "Posting…" : "Post comment"}
              </Button>
            </div>
            {addNote.error ? (
              <div className="text-2xs text-neg mt-2">
                Write failed — {refusalMessage(addNote.error, "Note failed.")}
              </div>
            ) : null}
          </section>
        </div>
      </div>
    </>
  );
}

function NoteRow({
  note,
  isOpenQuestion,
  answeredNotReleased,
  draft,
  onDraft,
  onHold,
  onRelease,
  pending,
}: {
  note: Bead;
  isOpenQuestion: boolean;
  answeredNotReleased: boolean;
  draft: string;
  onDraft: (v: string) => void;
  onHold: () => void;
  onRelease: () => void;
  pending: boolean;
}) {
  const c = noteContent(note);
  const blocking = isOpenQuestion && c.blocking === true;

  return (
    <li
      className={cn(
        "border rounded p-2.5 bg-bg-subtle",
        blocking ? "border-warn" : "border-border",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Badge tone={NOTE_KIND_TONE[c.kind] ?? "neutral"}>{c.kind}</Badge>
          {blocking ? <Badge tone="warn">blocking</Badge> : null}
          {c.verdict ? (
            <Badge tone={c.verdict === "approve" ? "pos" : "neg"}>{c.verdict}</Badge>
          ) : null}
        </div>
        <span className="num text-2xs text-fg-subtle">{fmtDateTime(note.created_at)}</span>
      </div>

      <div className="text-xs text-fg mt-1.5 whitespace-pre-wrap">{c.body}</div>

      {c.url ? (
        <div className="text-2xs font-mono text-accent mt-1 break-all">{c.url}</div>
      ) : null}

      <div className="text-2xs text-fg-subtle mt-1.5 font-mono">by {note.created_by}</div>

      {isOpenQuestion ? (
        <div className="mt-2.5 border-t border-border/40 pt-2.5">
          {answeredNotReleased ? (
            <div className="text-2xs text-warn font-medium mb-2">
              Answered, but not released — this question still blocks the dispatcher.
            </div>
          ) : null}
          <Textarea
            rows={2}
            className="w-full"
            placeholder="Reply — choose below whether this releases the held work…"
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
          />
          <div className="flex justify-end gap-2 mt-2">
            <Button
              variant="outline"
              size="sm"
              disabled={pending || !draft.trim()}
              onClick={onHold}
            >
              {pending ? "Saving…" : "Reply, keep held"}
            </Button>
            <Button size="sm" disabled={pending || !draft.trim()} onClick={onRelease}>
              {pending ? "Releasing…" : "Release work"}
            </Button>
          </div>
        </div>
      ) : null}
    </li>
  );
}
