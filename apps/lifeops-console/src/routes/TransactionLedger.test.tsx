import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { fmtDateTime } from "@/lib/format";
import { LedgerFreshness } from "@/routes/TransactionLedger";

describe("LedgerFreshness — the ledger must say when it last read, not just what it read", () => {
  it("renders the query's own dataUpdatedAt, not a separately tracked timestamp", () => {
    const dataUpdatedAt = new Date("2026-08-06T14:32:00Z").getTime();
    const html = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={dataUpdatedAt} isFetching={false} />,
    );

    expect(html).toContain(fmtDateTime(new Date(dataUpdatedAt).toISOString()));
  });

  it("updates to the new fetch time once a refetch completes", () => {
    const first = new Date("2026-08-06T14:32:00Z").getTime();
    const second = new Date("2026-08-06T20:05:00Z").getTime();

    const before = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={first} isFetching={false} />,
    );
    const after = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={second} isFetching={false} />,
    );

    expect(before).toContain(fmtDateTime(new Date(first).toISOString()));
    expect(after).toContain(fmtDateTime(new Date(second).toISOString()));
    expect(after).not.toContain(fmtDateTime(new Date(first).toISOString()));
  });

  it("indicates a background refresh in progress without hiding the last-known fetch time", () => {
    const dataUpdatedAt = new Date("2026-08-06T14:32:00Z").getTime();
    const html = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={dataUpdatedAt} isFetching />,
    );

    expect(html).toContain(fmtDateTime(new Date(dataUpdatedAt).toISOString()));
    expect(html).toMatch(/refreshing/);
  });

  it("shows no refreshing notice once the background fetch settles", () => {
    const dataUpdatedAt = new Date("2026-08-06T14:32:00Z").getTime();
    const html = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={dataUpdatedAt} isFetching={false} />,
    );

    expect(html).not.toMatch(/refreshing/);
  });

  it("renders nothing before the first successful fetch, rather than a bogus epoch time", () => {
    const html = renderToStaticMarkup(<LedgerFreshness dataUpdatedAt={0} isFetching />);

    expect(html).toBe("");
  });

  it("renders a time-of-day component absent from the posted_date column's bare-date format", () => {
    // fmtDate (posted_date) is date-only; fmtDateTime (freshness) carries
    // hour:minute. Distinguishability is the acceptance criterion, not a
    // stylistic nicety — this asserts the two never coincide as strings.
    const dataUpdatedAt = new Date("2026-08-06T14:32:00Z").getTime();
    const html = renderToStaticMarkup(
      <LedgerFreshness dataUpdatedAt={dataUpdatedAt} isFetching={false} />,
    );

    expect(html).toMatch(/\d{1,2}:\d{2}/);
  });
});
