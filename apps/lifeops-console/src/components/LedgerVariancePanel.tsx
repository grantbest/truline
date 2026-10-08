import type {
  LedgerVarianceAccount,
  LedgerVarianceResponse,
  LedgerVarianceTotal,
} from "@/providers/mcp-client";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { fmtCurrency, fmtDate } from "@/lib/format";
import { cn } from "@/lib/cn";

export const LEDGER_VARIANCE_REFRESH_INTERVAL_MS = 60_000;

type VarianceTone = "pos" | "neg" | "warn" | "neutral";

interface VariancePresentation {
  label: string;
  value: string;
  detail: string;
  tone: VarianceTone;
  measured: boolean;
  hasVariance: boolean;
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function ledgerVarianceAccountPresentation(
  row: LedgerVarianceAccount | null | undefined,
): VariancePresentation {
  if (!row) {
    return {
      label: "Unavailable",
      value: "Variance unavailable",
      detail: "No figure returned for this account",
      tone: "warn",
      measured: false,
      hasVariance: false,
    };
  }

  if (!row.anchor_date) {
    return {
      label: "Not yet measuring",
      value: "No anchor",
      detail: "No dollar figure until an anchor exists",
      tone: "warn",
      measured: false,
      hasVariance: false,
    };
  }

  const hasVariance = row.unexplained_variance !== 0;
  return {
    label: hasVariance ? "Unexplained variance" : "Measured clean",
    value: fmtCurrency(row.unexplained_variance),
    detail: `Since ${fmtDate(row.anchor_date)} · ${pluralize(row.open_discrepancies, "open gap")}`,
    tone: hasVariance ? "neg" : "pos",
    measured: true,
    hasVariance,
  };
}

export function ledgerVarianceTotalPresentation(
  total: LedgerVarianceTotal | null | undefined,
): VariancePresentation {
  if (!total) {
    return {
      label: "Unavailable",
      value: "Ledger variance unavailable",
      detail: "The API did not return a total",
      tone: "warn",
      measured: false,
      hasVariance: false,
    };
  }

  if (!total.anchor_date) {
    return {
      label: "Not yet measuring",
      value: "No anchor",
      detail: `${pluralize(total.accounts_not_measured, "account")} without an anchor`,
      tone: "warn",
      measured: false,
      hasVariance: false,
    };
  }

  const hasVariance = total.unexplained_variance !== 0;
  const coverage =
    total.accounts_not_measured > 0
      ? ` · ${pluralize(total.accounts_not_measured, "account")} not yet measuring`
      : "";
  return {
    label: hasVariance ? "Ledger untrustworthy" : "Measured clean",
    value: fmtCurrency(total.unexplained_variance),
    detail: `Since ${fmtDate(total.anchor_date)} · ${pluralize(
      total.open_discrepancies,
      "open gap",
    )} · ${pluralize(total.accounts_measured, "account")} measured${coverage}`,
    tone: hasVariance ? "neg" : total.accounts_not_measured > 0 ? "warn" : "pos",
    measured: true,
    hasVariance,
  };
}

export function ledgerVarianceByAccount(
  report: LedgerVarianceResponse | undefined,
): Map<string, LedgerVarianceAccount> {
  return new Map((report?.accounts ?? []).map((row) => [row.account_id, row]));
}

function badgeTone(tone: VarianceTone): "neutral" | "pos" | "neg" | "warn" {
  return tone === "pos" || tone === "neg" || tone === "warn" ? tone : "neutral";
}

function amountTone(tone: VarianceTone): string {
  if (tone === "pos") return "text-pos";
  if (tone === "neg") return "text-neg";
  if (tone === "warn") return "text-warn";
  return "text-fg-muted";
}

export function LedgerVarianceInline({
  row,
  unavailable = false,
}: {
  row?: LedgerVarianceAccount | null;
  unavailable?: boolean;
}) {
  const presentation = unavailable
    ? ledgerVarianceAccountPresentation(null)
    : ledgerVarianceAccountPresentation(row);

  return (
    <div className="flex flex-col items-end gap-0.5">
      <span className={cn("num text-xs font-semibold", amountTone(presentation.tone))}>
        {presentation.value}
      </span>
      <span className="text-2xs text-fg-subtle">{presentation.label}</span>
    </div>
  );
}

export function LedgerVarianceTotalInline({
  report,
  unavailable = false,
}: {
  report?: LedgerVarianceResponse;
  unavailable?: boolean;
}) {
  const presentation = unavailable
    ? ledgerVarianceTotalPresentation(null)
    : ledgerVarianceTotalPresentation(report?.total);

  return (
    <span className={cn("num", amountTone(presentation.tone))}>
      {presentation.label}: {presentation.value}
    </span>
  );
}

export function LedgerVariancePanel({
  report,
  isLoading = false,
  isUnavailable = false,
}: {
  report?: LedgerVarianceResponse;
  isLoading?: boolean;
  isUnavailable?: boolean;
}) {
  if (isLoading) {
    return (
      <Card>
        <CardHeader title="Ledger integrity" hint="Cumulative unexplained variance" />
        <CardBody>
          <div className="text-sm text-fg-muted">Loading ledger integrity...</div>
        </CardBody>
      </Card>
    );
  }

  if (isUnavailable) {
    return (
      <Card className="border-warn">
        <CardHeader
          title="Ledger integrity unavailable"
          hint="The variance API could not be reached"
          right={<Badge tone="warn">unavailable</Badge>}
        />
        <CardBody>
          <div className="text-sm text-warn">
            The cumulative unexplained variance is unavailable. No dollar figure is shown.
          </div>
        </CardBody>
      </Card>
    );
  }

  const total = ledgerVarianceTotalPresentation(report?.total);

  return (
    <Card className={total.hasVariance ? "border-neg" : total.tone === "warn" ? "border-warn" : undefined}>
      <CardHeader
        title="Ledger integrity"
        hint="Cumulative unexplained variance"
        right={<Badge tone={badgeTone(total.tone)}>{total.label}</Badge>}
      />
      <CardBody className="space-y-3">
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2">
          <div>
            <div className="panel-title">Total variance</div>
            <div className={cn("num text-2xl font-semibold", amountTone(total.tone))}>
              {total.value}
            </div>
          </div>
          <div className="text-2xs text-fg-subtle sm:text-right">{total.detail}</div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2">
          {(report?.accounts ?? []).map((row) => {
            const account = ledgerVarianceAccountPresentation(row);
            return (
              <div
                key={row.account_id}
                className={cn(
                  "border border-border rounded p-2 bg-bg-subtle/40 flex items-start justify-between gap-3",
                  account.hasVariance ? "border-neg/70" : account.tone === "warn" ? "border-warn/70" : "",
                )}
              >
                <div className="min-w-0">
                  <div className="text-sm text-fg truncate">
                    {row.account_name ?? row.account_id}
                  </div>
                  <div className="text-2xs text-fg-subtle">{account.detail}</div>
                </div>
                <div className="text-right shrink-0">
                  <div className={cn("num text-sm font-semibold", amountTone(account.tone))}>
                    {account.value}
                  </div>
                  <Badge tone={badgeTone(account.tone)}>{account.label}</Badge>
                </div>
              </div>
            );
          })}
        </div>
      </CardBody>
    </Card>
  );
}
