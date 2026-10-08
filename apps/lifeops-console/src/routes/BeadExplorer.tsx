import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  type ColumnDef,
  type ColumnFiltersState,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { substrateClient } from "@/providers/substrate-client";
import type { Bead } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Badge, stateTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Input";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";

// Substrate has no JSONB-content query yet, so we pull a generous page and
// let TanStack Table own sorting + namespace/type filtering client-side —
// same trade-off TransactionLedger makes. Semantic search swaps the data
// source but the table (sort/filter) layers on top unchanged.
const FETCH_LIMIT = 500;

export function BeadExplorerRoute() {
  const [semanticQ, setSemanticQ] = useState("");
  const [activeSemanticQ, setActiveSemanticQ] = useState("");
  const [selected, setSelected] = useState<Bead | null>(null);

  const [sorting, setSorting] = useState<SortingState>([{ id: "created_at", desc: true }]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [showMobileFilters, setShowMobileFilters] = useState(false);

  const isSemantic = activeSemanticQ.trim().length > 0;

  const listQuery = useQuery({
    queryKey: ["beads", FETCH_LIMIT],
    queryFn: () => substrateClient.listBeads({ limit: FETCH_LIMIT }),
    enabled: !isSemantic,
  });

  const searchQuery = useQuery({
    queryKey: ["beads-search", activeSemanticQ],
    queryFn: () => substrateClient.semanticSearch(activeSemanticQ, { limit: 50 }),
    enabled: isSemantic,
  });

  const data = useMemo<Bead[]>(() => {
    if (isSemantic) return (searchQuery.data ?? []).map((h) => h.bead);
    return listQuery.data ?? [];
  }, [isSemantic, listQuery.data, searchQuery.data]);

  const loading = isSemantic ? searchQuery.isLoading : listQuery.isLoading;
  const error = isSemantic ? searchQuery.error : listQuery.error;

  // Namespace options come from the data so the one-click filter only ever
  // offers values that actually exist in the current result set.
  const namespaceOptions = useMemo(
    () => [...new Set(data.map((b) => b.namespace))].sort(),
    [data],
  );

  const columns = useMemo<ColumnDef<Bead>[]>(
    () => [
      {
        accessorKey: "namespace",
        header: "Namespace",
        filterFn: "equalsString",
        cell: (ctx) => <Badge tone="accent">{ctx.getValue<string>()}</Badge>,
      },
      {
        accessorKey: "type",
        header: "Type",
        filterFn: "includesString",
        cell: (ctx) => <span className="font-mono text-xs">{ctx.getValue<string>()}</span>,
      },
      {
        accessorKey: "state",
        header: "State",
        enableColumnFilter: false,
        cell: (ctx) => {
          const v = ctx.getValue<string>();
          return <Badge tone={stateTone(v)}>{v}</Badge>;
        },
      },
      {
        accessorKey: "trust_tier",
        header: "Trust",
        enableColumnFilter: false,
        cell: (ctx) => <span className="text-xs text-fg-muted">{ctx.getValue<string>()}</span>,
      },
      {
        accessorKey: "created_at",
        header: "Created",
        enableColumnFilter: false,
        cell: (ctx) => (
          <span className="num text-xs text-fg-muted whitespace-nowrap">
            {fmtDateTime(ctx.getValue<string>())}
          </span>
        ),
      },
      {
        accessorKey: "created_by",
        header: "Created by",
        enableSorting: false,
        enableColumnFilter: false,
        cell: (ctx) => (
          <span className="font-mono text-xs text-fg-muted">{ctx.getValue<string>()}</span>
        ),
      },
      {
        id: "preview",
        header: "Preview",
        enableSorting: false,
        enableColumnFilter: false,
        accessorFn: (b) => previewContent(b.content),
        cell: (ctx) => (
          <span className="text-xs text-fg-muted max-w-[28rem] truncate block">
            {ctx.getValue<string>()}
          </span>
        ),
      },
    ],
    [],
  );

  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnFilters },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  const visibleRows = table.getRowModel().rows;
  const colCount = table.getAllLeafColumns().length;

  const resetFilters = () => {
    setColumnFilters([]);
    setSorting([{ id: "created_at", desc: true }]);
    setSemanticQ("");
    setActiveSemanticQ("");
  };

  const namespaceFilter = (table.getColumn("namespace")?.getFilterValue() as string) ?? "";
  const typeFilter = (table.getColumn("type")?.getFilterValue() as string) ?? "";
  const hasActiveFilters = namespaceFilter || typeFilter || isSemantic;

  return (
    <>
      <PageHeader
        title="Bead Explorer"
        subtitle="Unified ledger across all Substrate namespaces"
        right={
          <div className="flex items-center gap-2">
            <span className="text-2xs text-fg-subtle num">
              {visibleRows.length} / {data.length} rows
            </span>
            <Button variant="ghost" size="sm" onClick={resetFilters}>
              Reset
            </Button>
          </div>
        }
      />

      <div className="p-4 sm:p-6 space-y-4">
        {/* Mobile Filters Trigger */}
        <div className="md:hidden flex items-center justify-between bg-bg-panel border border-border rounded p-3">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setShowMobileFilters(true)}
            className="flex items-center gap-1.5"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
            </svg>
            Filters {hasActiveFilters ? `(Active)` : ""}
          </Button>
          {hasActiveFilters && (
            <Button size="sm" variant="ghost" onClick={resetFilters}>
              Clear
            </Button>
          )}
        </div>

        {/* Filters Panel (Desktop Inline, Mobile Drawer) */}
        <div className={cn(
          "md:block",
          showMobileFilters 
            ? "fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-end sm:items-center sm:justify-center p-0 sm:p-4" 
            : "hidden"
        )}>
          {showMobileFilters && (
            <div className="fixed inset-0" onClick={() => setShowMobileFilters(false)} />
          )}
          <Card className={cn(
            "w-full relative z-10",
            showMobileFilters ? "rounded-t-xl sm:rounded-md max-w-lg bg-bg-panel border-t sm:border border-border p-4 max-h-[85vh] overflow-y-auto animate-slide-up" : ""
          )}>
            {showMobileFilters && (
              <div className="flex items-center justify-between border-b border-border pb-2 mb-3">
                <span className="font-mono text-xs font-semibold uppercase tracking-wider text-fg-muted">Filters</span>
                <Button size="sm" variant="ghost" onClick={() => setShowMobileFilters(false)}>
                  ✕
                </Button>
              </div>
            )}
            <CardBody className={cn(
              "flex flex-col md:flex-row flex-wrap gap-3.5 md:gap-2 items-stretch md:items-end",
              showMobileFilters ? "p-0" : ""
            )}>
              <div className="flex flex-col gap-1 flex-1 min-w-[120px]">
                <label className="panel-title">Namespace</label>
                <Select
                  value={namespaceFilter}
                  onChange={(e) =>
                    table.getColumn("namespace")?.setFilterValue(e.target.value || undefined)
                  }
                  className="w-full"
                >
                  <option value="">all</option>
                  {namespaceOptions.map((ns) => (
                    <option key={ns} value={ns}>
                      {ns}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex flex-col gap-1 flex-1 min-w-[150px]">
                <label className="panel-title">Type</label>
                <Input
                  value={typeFilter}
                  onChange={(e) =>
                    table.getColumn("type")?.setFilterValue(e.target.value || undefined)
                  }
                  placeholder="e.g. transaction"
                  className="w-full"
                />
              </div>
              <div className="flex-1 min-w-[240px] flex flex-col gap-1">
                <label className="panel-title">Semantic search</label>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    setActiveSemanticQ(semanticQ);
                    setShowMobileFilters(false);
                  }}
                  className="flex gap-1"
                >
                  <Input
                    value={semanticQ}
                    onChange={(e) => setSemanticQ(e.target.value)}
                    placeholder="Search Qdrant…"
                    className="flex-1"
                  />
                  <Button type="submit" size="sm">
                    Search
                  </Button>
                  {isSemantic ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        setSemanticQ("");
                        setActiveSemanticQ("");
                      }}
                    >
                      Clear
                    </Button>
                  ) : null}
                </form>
              </div>

              {showMobileFilters && (
                <div className="mt-4 pt-3 border-t border-border flex gap-2 w-full">
                  <Button className="flex-1" onClick={() => setShowMobileFilters(false)}>
                    Apply
                  </Button>
                  {hasActiveFilters && (
                    <Button variant="outline" onClick={() => { resetFilters(); setShowMobileFilters(false); }}>
                      Reset
                    </Button>
                  )}
                </div>
              )}
            </CardBody>
          </Card>
        </div>

        <Card>
          <CardHeader
            title={isSemantic ? `Search · "${activeSemanticQ}"` : "Beads"}
            hint={isSemantic ? "Ranked by vector similarity" : `Most recent ${FETCH_LIMIT}`}
          />
          {/* Desktop Table View */}
          <div className="overflow-x-auto hidden md:block">
            <table className="w-full border-collapse text-sm">
              <thead className="text-2xs uppercase tracking-wider text-fg-muted">
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => {
                      const sortDir = header.column.getIsSorted();
                      const canSort = header.column.getCanSort();
                      return (
                        <th
                          key={header.id}
                          onClick={header.column.getToggleSortingHandler()}
                          className={cn(
                            "text-left font-medium px-3 py-2 border-b border-border select-none",
                            canSort ? "cursor-pointer hover:text-fg" : "",
                          )}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {sortDir ? (sortDir === "desc" ? " ↓" : " ↑") : ""}
                        </th>
                      );
                    })}
                  </tr>
                ))}
              </thead>
              <tbody className="divide-y divide-border">
                {loading ? (
                  <tr>
                    <td colSpan={colCount} className="text-center text-fg-muted py-6">
                      Loading…
                    </td>
                  </tr>
                ) : error ? (
                  <tr>
                    <td colSpan={colCount} className="text-center text-neg py-6">
                      {(error as Error).message}
                    </td>
                  </tr>
                ) : visibleRows.length === 0 ? (
                  <tr>
                    <td colSpan={colCount} className="text-center text-fg-muted py-6">
                      No beads match.
                    </td>
                  </tr>
                ) : (
                  visibleRows.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => setSelected(row.original)}
                      className="hover:bg-bg-hover/60 cursor-pointer"
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id} className="px-3 py-2 align-top">
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Mobile Card List View */}
          <div className="md:hidden divide-y divide-border">
            {loading ? (
              <div className="text-center text-fg-muted py-6">Loading…</div>
            ) : error ? (
              <div className="text-center text-neg py-6">{(error as Error).message}</div>
            ) : visibleRows.length === 0 ? (
              <div className="text-center text-fg-muted py-6">No beads match.</div>
            ) : (
              visibleRows.map((row) => {
                const b = row.original;
                return (
                  <div
                    key={row.id}
                    onClick={() => setSelected(b)}
                    className="px-4 py-3 bg-bg-subtle/20 hover:bg-bg-hover/40 active:bg-bg-hover/60 transition-colors flex flex-col gap-1.5 cursor-pointer"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <Badge tone="accent">{b.namespace}</Badge>
                        <span className="font-mono text-xs text-fg truncate font-bold">{b.type}</span>
                      </div>
                      <Badge tone={stateTone(b.state)}>{b.state}</Badge>
                    </div>
                    <div className="flex items-center justify-between gap-2 text-2xs text-fg-subtle">
                      <span className="font-mono truncate">by {b.created_by}</span>
                      <span className="num whitespace-nowrap">{fmtDateTime(b.created_at)}</span>
                    </div>
                    <div className="text-2xs text-fg-muted font-mono truncate bg-bg-subtle/40 px-1.5 py-0.5 rounded border border-border/20">
                      {previewContent(b.content)}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </Card>
      </div>

      {selected ? <BeadDetailPanel bead={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}

function previewContent(c: Record<string, unknown>): string {
  const keys = Object.keys(c).slice(0, 3);
  return keys
    .map((k) => {
      const v = c[k];
      const s = typeof v === "object" ? JSON.stringify(v) : String(v);
      return `${k}=${s.length > 30 ? s.slice(0, 30) + "…" : s}`;
    })
    .join(" · ");
}
