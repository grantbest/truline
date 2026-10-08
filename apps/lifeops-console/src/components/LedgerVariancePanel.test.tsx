import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  LEDGER_VARIANCE_REFRESH_INTERVAL_MS,
  LedgerVarianceInline,
  LedgerVariancePanel,
  ledgerVarianceAccountPresentation,
} from "@/components/LedgerVariancePanel";
import {
  ledgerVarianceIsUnavailable,
  ledgerVarianceQueryOptions,
} from "@/hooks/useLedgerVariance";
import type { LedgerVarianceResponse } from "@/providers/mcp-client";

const report: LedgerVarianceResponse = {
  total: {
    unexplained_variance: 42.5,
    accounts_measured: 2,
    accounts_not_measured: 1,
    anchor_date: "2026-08-14T00:00:00",
    open_discrepancies: 1,
  },
  accounts: [
    {
      account_id: "unmeasured",
      account_name: "New checking",
      unexplained_variance: null,
      open_discrepancies: 0,
      anchor_date: null,
    },
    {
      account_id: "clean",
      account_name: "Measured checking",
      unexplained_variance: 0,
      open_discrepancies: 0,
      anchor_date: "2026-08-14T00:00:00",
    },
    {
      account_id: "dirty",
      account_name: "Card",
      unexplained_variance: 42.5,
      open_discrepancies: 1,
      anchor_date: "2026-08-14T00:00:00",
    },
  ],
};

describe("ledger variance presentation", () => {
  it("renders an unmeasured account differently from a measured zero-variance account", () => {
    const unmeasured = ledgerVarianceAccountPresentation(report.accounts[0]);
    const clean = ledgerVarianceAccountPresentation(report.accounts[1]);
    const html = renderToStaticMarkup(<LedgerVariancePanel report={report} />);

    expect(unmeasured.value).not.toBe(clean.value);
    expect(unmeasured.value).not.toBe("$0.00");
    expect(clean.value).toBe("$0.00");
    expect(html).toContain("Not yet measuring");
    expect(html).toContain("No anchor");
    expect(html).toContain("Measured clean");
    expect(html).toContain("$0.00");
  });

  it("renders an explicit unavailable state when the API cannot be reached", () => {
    expect(ledgerVarianceIsUnavailable({ isError: true, isRefetchError: false })).toBe(true);
    expect(ledgerVarianceIsUnavailable({ isError: false, isRefetchError: true })).toBe(true);

    const panelHtml = renderToStaticMarkup(<LedgerVariancePanel isUnavailable />);
    const inlineHtml = renderToStaticMarkup(<LedgerVarianceInline unavailable />);

    expect(panelHtml).toContain("Ledger integrity unavailable");
    expect(panelHtml).toContain("No dollar figure is shown");
    expect(inlineHtml).toContain("Variance unavailable");
    expect(panelHtml + inlineHtml).not.toContain("$0.00");
  });

  it("sets a refresh interval for the ledger variance view", () => {
    expect(ledgerVarianceQueryOptions().refetchInterval).toBe(
      LEDGER_VARIANCE_REFRESH_INTERVAL_MS,
    );
  });
});
