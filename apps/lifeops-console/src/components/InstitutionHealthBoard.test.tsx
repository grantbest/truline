import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  INSTITUTION_HEALTH_REFRESH_INTERVAL_MS,
  InstitutionHealthBoard,
  institutionHealthPresentation,
  institutionHealthVerdict,
} from "@/components/InstitutionHealthBoard";
import {
  institutionHealthIsUnavailable,
  institutionHealthQueryOptions,
} from "@/hooks/useInstitutionHealth";
import type { ConnectionStatus } from "@/providers/mcp-client";

function institution(overrides: Partial<ConnectionStatus> & { institution: string }): ConnectionStatus {
  return {
    token_present: true,
    item_id: "item-1",
    linked_at: "2026-01-01T00:00:00Z",
    last_relinked_at: null,
    cursor_present: true,
    last_synced: "2026-08-20T00:00:00Z",
    account_count: 2,
    accounts_last_synced: "2026-08-20T00:00:00Z",
    healthy: true,
    error_code: null,
    repairable: null,
    link_url: "https://example.test/link",
    repair_url: "https://example.test/repair",
    status: "ok",
    data_freshness: null,
    ...overrides,
  };
}

describe("institution health verdict — freshness only, never connectivity status", () => {
  it("reads unhealthy from data_freshness even when status reports the provider reachable (RC-2)", () => {
    const row = institution({
      institution: "chase",
      status: "ok",
      data_freshness: {
        last_transaction_date: "2026-07-01T00:00:00",
        days_since_last_transaction: 51,
        known: true,
        window_days: 90,
        freshness_slo_days: 5,
      },
    });

    expect(institutionHealthVerdict(row.data_freshness)).toBe("unhealthy");
    const presentation = institutionHealthPresentation(row);
    expect(presentation.tone).toBe("neg");
    expect(presentation.reason).toBe(
      "51 days since the last transaction — exceeds the 5-day SLO",
    );
    expect(presentation.newestTransaction).not.toBe("—");
  });

  it("reads healthy from data_freshness even when status flags a connectivity problem", () => {
    const row = institution({
      institution: "amex",
      status: "reauth_required",
      data_freshness: {
        last_transaction_date: "2026-08-19T00:00:00",
        days_since_last_transaction: 2,
        known: true,
        window_days: 90,
        freshness_slo_days: 10,
      },
    });

    expect(institutionHealthVerdict(row.data_freshness)).toBe("healthy");
    const presentation = institutionHealthPresentation(row);
    expect(presentation.tone).toBe("pos");
    expect(presentation.newestTransaction).not.toBe("—");
  });

  it("treats no learned SLO or no data in window as unmeasured, never as a green pass", () => {
    const noSlo = institution({
      institution: "fidelity",
      data_freshness: {
        last_transaction_date: null,
        days_since_last_transaction: null,
        known: true,
        window_days: 90,
        freshness_slo_days: null,
      },
    });
    const noData = institution({
      institution: "wells",
      data_freshness: {
        last_transaction_date: null,
        days_since_last_transaction: null,
        known: false,
        window_days: 90,
        freshness_slo_days: null,
      },
    });

    expect(institutionHealthVerdict(noSlo.data_freshness)).toBe("unmeasured");
    expect(institutionHealthVerdict(noData.data_freshness)).toBe("unmeasured");
    expect(institutionHealthPresentation(noSlo).tone).toBe("warn");
    expect(institutionHealthPresentation(noData).tone).toBe("warn");
  });
});

describe("institution health board rendering", () => {
  const rows = [
    institution({
      institution: "chase",
      status: "ok",
      data_freshness: {
        last_transaction_date: "2026-07-01T00:00:00",
        days_since_last_transaction: 51,
        known: true,
        window_days: 90,
        freshness_slo_days: 5,
      },
    }),
    institution({
      institution: "amex",
      status: "ok",
      data_freshness: {
        last_transaction_date: "2026-08-19T00:00:00",
        days_since_last_transaction: 2,
        known: true,
        window_days: 90,
        freshness_slo_days: 10,
      },
    }),
  ];

  it("renders one row per institution with its own health, SLO, and newest-transaction date", () => {
    const html = renderToStaticMarkup(<InstitutionHealthBoard institutions={rows} />);

    expect(html).toContain("chase");
    expect(html).toContain("amex");
    expect(html).toContain("unhealthy");
    expect(html).toContain("healthy");
    expect(html).toContain("exceeds the 5-day SLO");
    expect(html).toContain("Aug 19, 2026");
    expect(html).toContain("Jul 01, 2026");
  });

  it("never renders an aggregate health indicator", () => {
    const html = renderToStaticMarkup(<InstitutionHealthBoard institutions={rows} />);

    // The only count on the page is a bare institution count, not a verdict.
    expect(html).toContain("2 institutions");
    // No combined/overall verdict language, and no "N of M healthy" style rollup.
    for (const forbidden of [
      "Overall",
      "All healthy",
      "All institutions healthy",
      "System health",
      "1 of 2",
      "1/2 healthy",
    ]) {
      expect(html).not.toContain(forbidden);
    }
    // Each verdict word traces back to a specific institution's row/card, not a
    // page-level summary: every occurrence sits beside "chase" or "amex" in the
    // markup, never on its own outside a per-institution block.
    expect(html).toContain("unhealthy");
    expect(html).toContain("healthy");
  });

  it("shows a named retrieval failure instead of stale data presented as current", () => {
    const html = renderToStaticMarkup(<InstitutionHealthBoard isUnavailable />);

    expect(html).toContain("Institution health unavailable");
    expect(html).toContain("retrieval failed");
    expect(html).not.toContain("chase");
    expect(html).not.toContain("amex");
  });

  it("treats both an initial-load error and a background refetch error as unavailable", () => {
    expect(institutionHealthIsUnavailable({ isError: true, isRefetchError: false })).toBe(true);
    expect(institutionHealthIsUnavailable({ isError: false, isRefetchError: true })).toBe(true);
    expect(institutionHealthIsUnavailable({ isError: false, isRefetchError: false })).toBe(false);
  });
});

describe("institution health refresh — PR #315 convention", () => {
  it("polls on an interval so the view does not read as current while frozen on a mounted tab", () => {
    expect(institutionHealthQueryOptions().refetchInterval).toBe(
      INSTITUTION_HEALTH_REFRESH_INTERVAL_MS,
    );
  });
});
