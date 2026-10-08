import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { JsonView } from "@/components/ui/JsonView";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtDateTime } from "@/lib/format";

// Producer contract documented in README. The Vision Inbox lists beads
// with namespace="vision", state="pending". Approve/Reject transition state.
// Downstream materialization (creating todo/event/expense beads from the
// extraction payload) is the producer's job, triggered by the state change.

export function VisionInboxRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  const queryClient = useQueryClient();

  const pendingQuery = useQuery({
    queryKey: ["vision-pending"],
    queryFn: () =>
      substrateClient.listBeads({ namespace: "vision", state: "pending", limit: 100 }),
  });

  const transition = useMutation({
    mutationFn: async ({ id, target }: { id: string; target: "approved" | "rejected" }) => {
      return substrateClient.updateBead(id, {
        state: target,
        created_by: "lifeops-console/vision-inbox",
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["vision-pending"] });
    },
  });

  const items = pendingQuery.data ?? [];

  return (
    <>
      <PageHeader
        title="Vision Inbox"
        subtitle="Whiteboard / receipt / handwritten extractions awaiting review"
        right={<span className="text-2xs text-fg-subtle num">{items.length} pending</span>}
      />

      <div className="p-6 space-y-4">
        {pendingQuery.isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Loading…</CardBody>
          </Card>
        ) : pendingQuery.error ? (
          <Card>
            <CardBody className="text-center text-neg py-8">
              {(pendingQuery.error as Error).message}
            </CardBody>
          </Card>
        ) : items.length === 0 ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              <div className="text-sm">No pending extractions.</div>
              <div className="text-2xs text-fg-subtle mt-2 max-w-prose mx-auto">
                When the vision tool persists extractions as beads with{" "}
                <code className="font-mono">namespace=&quot;vision&quot;</code> and{" "}
                <code className="font-mono">state=&quot;pending&quot;</code>, they show up here for
                approval.
              </div>
            </CardBody>
          </Card>
        ) : (
          items.map((bead) => {
            const extraction = bead.content as {
              summary?: string;
              todos?: string[];
              events?: { title?: string; date?: string }[];
              expenses?: { amount?: number; vendor?: string; date?: string }[];
            };
            return (
              <Card key={bead.id}>
                <CardHeader
                  title={extraction.summary || `${bead.type} extraction`}
                  hint={`${bead.created_by} · ${fmtDateTime(bead.created_at)}`}
                  right={
                    <div className="hidden md:flex items-center gap-2">
                      <Badge tone="warn">{bead.state}</Badge>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setSelected(bead)}
                      >
                        Inspect
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={transition.isPending}
                        onClick={() => transition.mutate({ id: bead.id, target: "rejected" })}
                      >
                        Reject
                      </Button>
                      <Button
                        size="sm"
                        disabled={transition.isPending}
                        onClick={() => transition.mutate({ id: bead.id, target: "approved" })}
                      >
                        Approve
                      </Button>
                    </div>
                  }
                />
                <CardBody className="space-y-3">
                  {/* Mobile badge row */}
                  <div className="md:hidden flex items-center mb-1">
                    <Badge tone="warn">{bead.state}</Badge>
                  </div>

                  {extraction.todos && extraction.todos.length > 0 ? (
                    <ExtractionSection title="Todos">
                      <ul className="text-sm space-y-1 list-disc list-inside text-fg">
                        {extraction.todos.map((t, i) => (
                          <li key={i}>{t}</li>
                        ))}
                      </ul>
                    </ExtractionSection>
                  ) : null}

                  {extraction.events && extraction.events.length > 0 ? (
                    <ExtractionSection title="Events">
                      <ul className="text-sm space-y-1">
                        {extraction.events.map((e, i) => (
                          <li key={i}>
                            <span className="text-fg">{e.title ?? "(untitled)"}</span>
                            {e.date ? (
                              <span className="text-fg-subtle num"> — {e.date}</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    </ExtractionSection>
                  ) : null}

                  {extraction.expenses && extraction.expenses.length > 0 ? (
                    <ExtractionSection title="Expenses">
                      <ul className="text-sm space-y-1">
                        {extraction.expenses.map((x, i) => (
                          <li key={i}>
                            <span className="text-fg">{x.vendor ?? "(unknown vendor)"}</span>
                            {x.amount !== undefined ? (
                              <span className="text-neg num"> · ${x.amount}</span>
                            ) : null}
                            {x.date ? (
                              <span className="text-fg-subtle num"> — {x.date}</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    </ExtractionSection>
                  ) : null}

                  {!extraction.todos?.length &&
                  !extraction.events?.length &&
                  !extraction.expenses?.length ? (
                    <ExtractionSection title="Raw content">
                      <JsonView data={bead.content} />
                    </ExtractionSection>
                  ) : null}

                  {/* Mobile action button layout */}
                  <div className="md:hidden grid grid-cols-3 gap-2 border-t border-border/30 pt-2.5 mt-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setSelected(bead)}
                      className="w-full text-center min-h-[38px]"
                    >
                      Inspect
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={transition.isPending}
                      onClick={() => transition.mutate({ id: bead.id, target: "rejected" })}
                      className="w-full text-center min-h-[38px] text-[11px] px-1 truncate"
                    >
                      Reject
                    </Button>
                    <Button
                      size="sm"
                      disabled={transition.isPending}
                      onClick={() => transition.mutate({ id: bead.id, target: "approved" })}
                      className="w-full text-center min-h-[38px] text-[11px] px-1 truncate"
                    >
                      Approve
                    </Button>
                  </div>
                </CardBody>
              </Card>
            );
          })
        )}
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

function ExtractionSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="panel-title mb-1.5">{title}</div>
      {children}
    </div>
  );
}
