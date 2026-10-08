import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead } from "@/types/bead";
import { Badge, stateTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { JsonView } from "@/components/ui/JsonView";
import { fmtDateTime } from "@/lib/format";

interface Props {
  bead: Bead;
  onClose: () => void;
}

export function BeadDetailPanel({ bead, onClose }: Props) {
  const events = useQuery({
    queryKey: ["bead-events", bead.id],
    queryFn: () => substrateClient.listEvents(bead.id),
  });

  // Lock body scroll when panel is open
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  return (
    <>
      {/* Backdrop backdrop-blur */}
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm z-30 transition-opacity animate-fade-in"
        onClick={onClose}
      />
      {/* Slide-over panel */}
      <div className="fixed inset-y-0 right-0 w-full sm:w-[640px] max-w-full sm:max-w-[90vw] bg-bg-panel border-l border-border z-40 flex flex-col shadow-2xl animate-slide-in">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Badge tone="accent">{bead.namespace}</Badge>
            <Badge>{bead.type}</Badge>
            <Badge tone={stateTone(bead.state)}>{bead.state}</Badge>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} className="min-h-[44px]">
            Close ✕
          </Button>
        </div>

      <div className="overflow-auto flex-1 p-4 space-y-4">
        <section>
          <div className="panel-title mb-2">Identity</div>
          <dl className="grid grid-cols-[120px_1fr] gap-y-1 text-xs">
            <dt className="text-fg-muted">id</dt>
            <dd className="font-mono break-all">{bead.id}</dd>
            <dt className="text-fg-muted">parent_id</dt>
            <dd className="font-mono break-all">{bead.parent_id ?? "—"}</dd>
            <dt className="text-fg-muted">trust_tier</dt>
            <dd>{bead.trust_tier}</dd>
            <dt className="text-fg-muted">confidence</dt>
            <dd className="num">{bead.confidence ?? "—"}</dd>
            <dt className="text-fg-muted">created_by</dt>
            <dd className="font-mono">{bead.created_by}</dd>
            <dt className="text-fg-muted">created_at</dt>
            <dd className="num">{fmtDateTime(bead.created_at)}</dd>
            <dt className="text-fg-muted">updated_at</dt>
            <dd className="num">{fmtDateTime(bead.updated_at)}</dd>
          </dl>
        </section>

        <section>
          <div className="panel-title mb-2">Content</div>
          <JsonView data={bead.content} />
        </section>

        {Object.keys(bead.context).length > 0 ? (
          <section>
            <div className="panel-title mb-2">Context</div>
            <JsonView data={bead.context} />
          </section>
        ) : null}

        {Object.keys(bead.provenance).length > 0 ? (
          <section>
            <div className="panel-title mb-2">Provenance</div>
            <JsonView data={bead.provenance} />
          </section>
        ) : null}

        <section>
          <div className="panel-title mb-2">Event log</div>
          {events.isLoading ? (
            <div className="text-xs text-fg-muted">Loading…</div>
          ) : events.error ? (
            <div className="text-xs text-neg">Failed to load events</div>
          ) : events.data && events.data.length > 0 ? (
            <ol className="space-y-2">
              {events.data.map((e) => (
                <li key={e.id} className="border border-border rounded p-2 bg-bg-subtle">
                  <div className="flex items-center justify-between text-xs">
                    <div className="flex items-center gap-2">
                      <Badge tone="accent">{e.event_type}</Badge>
                      {e.from_state || e.to_state ? (
                        <span className="font-mono text-fg-muted">
                          {e.from_state ?? "∅"} → {e.to_state ?? "∅"}
                        </span>
                      ) : null}
                    </div>
                    <span className="num text-2xs text-fg-subtle">{fmtDateTime(e.created_at)}</span>
                  </div>
                  <div className="text-2xs text-fg-subtle mt-1 font-mono">by {e.created_by}</div>
                </li>
              ))}
            </ol>
          ) : (
            <div className="text-xs text-fg-muted">No events.</div>
          )}
        </section>
      </div>
    </div>
  </>
  );
}
