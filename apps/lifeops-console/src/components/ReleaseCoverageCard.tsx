import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { releaseContent } from "@/lib/ea-model";
import { releaseCoverage } from "@/lib/project-alpha";
import type { Bead } from "@/types/bead";

// Reads arch.release — the charter release-load.py mirrors from
// docs/releases/*.json — and only what the substrate actually returned.
// Never substitutes fixture content: "could not ask" (isUnavailable) and
// "asked, got nothing" (empty releases) render distinguishably, and neither
// one is a release detail view — that is R26.02/O-3, out of scope here.
export function ReleaseCoverageCard({
  isLoading = false,
  isUnavailable = false,
  releases = [],
}: {
  isLoading?: boolean;
  isUnavailable?: boolean;
  releases?: Bead[];
}) {
  if (isLoading) {
    return (
      <Card>
        <CardHeader title="Release charters" hint="arch.release" />
        <CardBody className="text-sm text-fg-muted">Loading release charters…</CardBody>
      </Card>
    );
  }

  const coverage = releaseCoverage({ isError: isUnavailable, releases });

  if (coverage.status === "unavailable") {
    return (
      <Card className="border-warn">
        <CardHeader
          title="Release charters unavailable"
          hint="arch.release could not be reached"
          right={<Badge tone="warn">retrieval failed</Badge>}
        />
        <CardBody>
          <div className="text-sm text-warn">{coverage.message}</div>
        </CardBody>
      </Card>
    );
  }

  if (coverage.status === "empty") {
    return (
      <Card>
        <CardHeader
          title="Release charters"
          hint="arch.release"
          right={<Badge tone="neutral">0 returned</Badge>}
        />
        <CardBody>
          <div className="text-sm text-fg-muted">{coverage.message}</div>
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Release charters"
        hint="arch.release"
        right={<Badge tone="pos">{coverage.charters.length} live</Badge>}
      />
      <CardBody className="space-y-2">
        <div className="text-2xs text-fg-subtle">{coverage.message}</div>
        {coverage.charters.map((bead) => {
          const content = releaseContent(bead);
          return (
            <article key={bead.id} className="rounded border border-border bg-bg-panel p-3">
              <div className="flex flex-wrap items-center gap-2">
                {/* Plain <a>, not <Link> — nginx's SPA fallback resolves a
                    full navigation to /releases/:ref (see nginx.conf), and
                    a plain anchor renders without a Router context, so this
                    card stays testable with renderToStaticMarkup alone. */}
                <a href={`/releases/${encodeURIComponent(content.ref)}`}>
                  <Badge tone="accent">{content.ref}</Badge>
                </a>
                <span className="text-sm font-medium text-fg">{content.name}</span>
              </div>
              <p className="mt-1 text-xs text-fg-muted">{content.objective}</p>
              <div className="mt-2 text-2xs text-fg-subtle font-mono">
                {content.outcomes?.length ?? 0} outcome(s) · opened {content.opened_at}
              </div>
            </article>
          );
        })}
      </CardBody>
    </Card>
  );
}
