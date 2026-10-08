import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { mcpClient } from "@/providers/mcp-client";
import type { Bead, FinanceTransactionContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card } from "@/components/ui/Card";
import { Input, Select } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { BeadDetailPanel } from "@/components/BeadDetailPanel";
import { fmtCurrency, fmtDate, fmtDateTime } from "@/lib/format";
import { merchantPath } from "@/lib/merchant";
import { cn } from "@/lib/cn";

// The ledger asks mcp-hub for transactions already ordered by bank
// posted_date. Raw Substrate lists beads by created_at, which is not the
// same thing after backfills, retries, or re-links.
const FETCH_LIMIT = 1000;

interface LedgerRow {
  bead: Bead;
  id: string;
  posted_date: string;
  merchant: string;
  institution: string;
  category: string;
  amount: number;
  currency: string;
  is_transfer: boolean;
}

const UNCATEGORIZED = "uncategorized";

// Distinct on purpose from the `posted_date` column (bare date, via
// fmtDate): this is *when the page read the data*, not a transaction fact.
// Sourced straight from the query's own dataUpdatedAt/isFetching — no
// separately tracked timestamp to drift out of sync with it, which is what
// let the ledger show a frozen "latest transaction" with nothing on screen
// to say the data itself was frozen too.
export function LedgerFreshness({
  dataUpdatedAt,
  isFetching,
}: {
  dataUpdatedAt: number;
  isFetching: boolean;
}) {
  if (!dataUpdatedAt) return null;
  return (
    <div className="text-2xs text-fg-subtle whitespace-nowrap">
      <span>Updated {fmtDateTime(new Date(dataUpdatedAt).toISOString())}</span>
      {isFetching ? <span className="text-accent"> · refreshing…</span> : null}
    </div>
  );
}

function toRow(bead: Bead): LedgerRow {
  const c = bead.content as unknown as FinanceTransactionContent;
  return {
    bead,
    id: bead.id,
    posted_date: c.posted_date,
    merchant: c.normalized_merchant || c.merchant_name || c.description || "—",
    institution: c.institution || "—",
    category: c.our_category || UNCATEGORIZED,
    amount: c.amount,
    currency: c.iso_currency_code || "USD",
    is_transfer: Boolean(c.is_transfer),
  };
}

export function TransactionLedgerRoute() {
  const [selected, setSelected] = useState<Bead | null>(null);
  const [sorting, setSorting] = useState<SortingState>([{ id: "posted_date", desc: true }]);
  const [showMobileFilters, setShowMobileFilters] = useState(false);

  // Filters.
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [institution, setInstitution] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [hideTransfers, setHideTransfers] = useState(true);

  const txnsQuery = useQuery({
    queryKey: ["finance-transactions-ledger", FETCH_LIMIT],
    queryFn: () => mcpClient.getRecentTransactions(FETCH_LIMIT, true),
  });

  // state=removed beads are Plaid-retracted (or dedup-removed) — never
  // shown in the ledger.
  const rows = useMemo(
    () => (txnsQuery.data ?? []).filter((b) => b.state !== "removed").map(toRow),
    [txnsQuery.data],
  );

  // Distinct filter option lists, derived from the data.
  const { categories, institutions } = useMemo(() => {
    const cat = new Set<string>();
    const inst = new Set<string>();
    for (const r of rows) {
      cat.add(r.category);
      if (r.institution && r.institution !== "—") inst.add(r.institution);
    }
    return {
      categories: [...cat].sort(),
      institutions: [...inst].sort(),
    };
  }, [rows]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const fromTime = from ? new Date(from).getTime() : null;
    // `to` is inclusive — push to end of that day.
    const toTime = to ? new Date(to).getTime() + 86_399_999 : null;
    return rows.filter((r) => {
      if (hideTransfers && r.is_transfer) return false;
      if (category && r.category !== category) return false;
      if (institution && r.institution !== institution) return false;
      if (needle && !r.merchant.toLowerCase().includes(needle)) return false;
      if (fromTime !== null || toTime !== null) {
        const t = new Date(r.posted_date).getTime();
        if (Number.isNaN(t)) return false;
        if (fromTime !== null && t < fromTime) return false;
        if (toTime !== null && t > toTime) return false;
      }
      return true;
    });
  }, [rows, search, category, institution, from, to, hideTransfers]);

  const total = useMemo(
    () => filtered.reduce((sum, r) => sum + (r.amount > 0 ? r.amount : 0), 0),
    [filtered],
  );

  const columns = useMemo<ColumnDef<LedgerRow>[]>(
    () => [
      {
        accessorKey: "posted_date",
        header: "Date",
        cell: (ctx) => (
          <span className="num text-2xs text-fg-subtle whitespace-nowrap">
            {fmtDate(ctx.getValue<string>())}
          </span>
        ),
      },
      {
        accessorKey: "merchant",
        header: "Merchant",
        cell: (ctx) => {
          const merchant = ctx.getValue<string>();
          if (!merchant || merchant === "—") return <span className="text-sm">{merchant}</span>;
          return (
            <Link
              to={merchantPath(merchant)}
              onClick={(e) => e.stopPropagation()}
              className="text-sm text-accent hover:underline underline-offset-2"
            >
              {merchant}
            </Link>
          );
        },
      },
      {
        accessorKey: "institution",
        header: "Institution",
        cell: (ctx) => <span className="text-xs text-fg-muted">{ctx.getValue<string>()}</span>,
      },
      {
        accessorKey: "category",
        header: "Category",
        cell: (ctx) => {
          const v = ctx.getValue<string>();
          const row = ctx.row.original;
          if (row.is_transfer) return <Badge tone="neutral">transfer</Badge>;
          return (
            <Badge tone={v === UNCATEGORIZED ? "warn" : "accent"}>{v}</Badge>
          );
        },
      },
      {
        accessorKey: "amount",
        header: () => <div className="text-right">Amount</div>,
        cell: (ctx) => {
          const v = ctx.getValue<number>();
          // Plaid convention: outflows positive, inflows negative.
          const isOutflow = v > 0;
          return (
            <div
              className={cn(
                "num text-right whitespace-nowrap",
                isOutflow ? "text-fg" : "text-pos",
              )}
            >
              {fmtCurrency(v, ctx.row.original.currency)}
            </div>
          );
        },
      },
    ],
    [],
  );

  const table = useReactTable({
    data: filtered,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const resetFilters = () => {
    setSearch("");
    setCategory("");
    setInstitution("");
    setFrom("");
    setTo("");
  };

  const hasFilters = search || category || institution || from || to;

  return (
    <>
      <PageHeader
        title="Transaction Ledger"
        subtitle={`${filtered.length} of ${rows.length} transactions · spend ${fmtCurrency(total)} (latest ${FETCH_LIMIT} by posted date)`}
        right={
          <LedgerFreshness
            dataUpdatedAt={txnsQuery.dataUpdatedAt}
            isFetching={txnsQuery.isFetching}
          />
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
            Filters {hasFilters ? `(Active)` : ""}
          </Button>
          {hasFilters && (
            <Button size="sm" variant="ghost" onClick={resetFilters}>
              Clear
            </Button>
          )}
        </div>

        {/* Filter bar (Desktop inline, Mobile Modal/Drawer) */}
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
            "p-3 w-full relative z-10",
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
            <div className="flex flex-col md:flex-row flex-wrap items-stretch md:items-end gap-3.5 md:gap-3">
              <label className="flex flex-col gap-1 flex-1 min-w-[200px]">
                <span className="text-2xs uppercase tracking-wider text-fg-subtle">Search merchant</span>
                <Input
                  placeholder="e.g. Amazon, Starbucks…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="w-full"
                />
              </label>

              <label className="flex flex-col gap-1 flex-1 min-w-[150px]">
                <span className="text-2xs uppercase tracking-wider text-fg-subtle">Category</span>
                <Select value={category} onChange={(e) => setCategory(e.target.value)} className="w-full">
                  <option value="">All categories</option>
                  {categories.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              </label>

              <label className="flex flex-col gap-1 flex-1 min-w-[150px]">
                <span className="text-2xs uppercase tracking-wider text-fg-subtle">Institution</span>
                <Select
                  value={institution}
                  onChange={(e) => setInstitution(e.target.value)}
                  className="w-full"
                >
                  <option value="">All institutions</option>
                  {institutions.map((i) => (
                    <option key={i} value={i}>
                      {i}
                    </option>
                  ))}
                </Select>
              </label>

              <div className="grid grid-cols-2 gap-2 flex-1 min-w-[200px]">
                <label className="flex flex-col gap-1">
                  <span className="text-2xs uppercase tracking-wider text-fg-subtle">From</span>
                  <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="w-full" />
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-2xs uppercase tracking-wider text-fg-subtle">To</span>
                  <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="w-full" />
                </label>
              </div>

              <div className="flex items-center justify-between h-10 md:h-8 px-1 mt-2 md:mt-0 border-t border-border/30 md:border-0 pt-2 md:pt-0">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={hideTransfers}
                    onChange={(e) => setHideTransfers(e.target.checked)}
                    className="accent-accent h-4 w-4 rounded"
                  />
                  <span className="text-xs text-fg-muted">Hide transfers</span>
                </label>
              </div>

              {showMobileFilters && (
                <div className="mt-4 pt-3 border-t border-border flex gap-2 w-full">
                  <Button className="flex-1" onClick={() => setShowMobileFilters(false)}>
                    Apply
                  </Button>
                  {hasFilters && (
                    <Button variant="outline" onClick={() => { resetFilters(); setShowMobileFilters(false); }}>
                      Reset
                    </Button>
                  )}
                </div>
              )}

              {!showMobileFilters && hasFilters ? (
                <button
                  type="button"
                  onClick={resetFilters}
                  className="h-8 px-2 text-xs text-fg-muted hover:text-fg underline-offset-2 hover:underline self-end"
                >
                  Clear
                </button>
              ) : null}
            </div>
          </Card>
        </div>

        {/* Table/Cards */}
        <Card>
          {/* Desktop Table View */}
          <div className="overflow-x-auto hidden md:block">
            <table className="w-full border-collapse text-sm">
              <thead className="text-2xs uppercase tracking-wider text-fg-muted">
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => {
                      const sortDir = header.column.getIsSorted();
                      return (
                        <th
                          key={header.id}
                          onClick={header.column.getToggleSortingHandler()}
                          className={cn(
                            "text-left font-medium px-3 py-2 border-b border-border select-none",
                            header.column.getCanSort() ? "cursor-pointer hover:text-fg" : "",
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
                {txnsQuery.isLoading ? (
                  <tr>
                    <td colSpan={columns.length} className="text-center text-fg-muted py-8">
                      Loading transactions…
                    </td>
                  </tr>
                ) : txnsQuery.isError ? (
                  <tr>
                    <td colSpan={columns.length} className="text-center text-neg py-8">
                      Failed to load transactions.
                    </td>
                  </tr>
                ) : table.getRowModel().rows.length === 0 ? (
                  <tr>
                    <td colSpan={columns.length} className="text-center text-fg-muted py-8">
                      No transactions match these filters.
                    </td>
                  </tr>
                ) : (
                  table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => setSelected(row.original.bead)}
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
            {txnsQuery.isLoading ? (
              <div className="text-center text-fg-muted py-8">Loading transactions…</div>
            ) : txnsQuery.isError ? (
              <div className="text-center text-neg py-8">Failed to load transactions.</div>
            ) : table.getRowModel().rows.length === 0 ? (
              <div className="text-center text-fg-muted py-8">No transactions match these filters.</div>
            ) : (
              table.getRowModel().rows.map((row) => {
                const r = row.original;
                const c = r.bead.content as unknown as FinanceTransactionContent;
                const isOutflow = c.amount > 0;
                return (
                  <div
                    key={row.id}
                    onClick={() => setSelected(r.bead)}
                    className="px-4 py-3 bg-bg-subtle/20 hover:bg-bg-hover/40 active:bg-bg-hover/60 transition-colors flex flex-col gap-1 cursor-pointer"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <span className="text-sm font-semibold text-fg block truncate">
                          {c.normalized_merchant || c.merchant_name || c.description || "—"}
                        </span>
                      </div>
                      <span className={cn("num text-sm font-semibold shrink-0", isOutflow ? "text-fg" : "text-pos")}>
                        {fmtCurrency(c.amount, c.iso_currency_code)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2 text-2xs text-fg-subtle">
                      <div className="flex items-center gap-2">
                        <span className="num">{fmtDate(c.posted_date)}</span>
                        <span>·</span>
                        <span className="truncate max-w-[120px]">{c.institution || "—"}</span>
                      </div>
                      {c.is_transfer ? (
                        <Badge tone="neutral">transfer</Badge>
                      ) : (
                        <Badge tone={c.our_category ? "accent" : "warn"}>
                          {c.our_category || "uncategorized"}
                        </Badge>
                      )}
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
