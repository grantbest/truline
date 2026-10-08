import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { substrateClient } from "@/providers/substrate-client";
import type { ListBeadsParams } from "@/providers/substrate-client";
import type { Bead, FinanceAccountContent } from "@/types/bead";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { fmtCurrency } from "@/lib/format";

// SDD Phase 2 — Component 3: Merchant Risk Dependency Graph.
//
// Maps recurring commitments (finance.subscription beads) to the funding
// account they're charged against, so a single card expiry / cancellation
// can't silently take down critical infrastructure (mortgage, AWS, utilities).
//
// Funding-account resolution: subscription beads carry no account_id, but the
// auditor stamps the evidence transaction ids in provenance.parent_ids. We
// resolve each subscription to the modal account among its evidence txns.
//
// Data gap (intentional, documented): Plaid account beads carry no card
// expiration date, so the brief's "expiring within 30 days" warning has no
// source yet. We surface the dependency map + a `critical` tag and flag credit
// cards funding critical merchants for manual expiry review. Wire the real
// warning once an `expiration_date` lands on finance.account content.

const PAGE_SIZE = 500;
const CRITICAL_DEFAULT_VENDORS = ["mortgage", "aws", "amazon web services", "insurance", "utility", "electric", "gas company", "water"];

interface SubFunding {
  beadId: string;
  name: string;
  amount?: number;
  frequency?: string;
  critical: boolean;
  accountId: string | null;
}

interface AccountGroup {
  account: { id: string; content: FinanceAccountContent } | null;
  subs: SubFunding[];
}

async function listAll(params: ListBeadsParams): Promise<Bead[]> {
  const out: Bead[] = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = await substrateClient.listBeads({ ...params, limit: PAGE_SIZE, offset });
    out.push(...page);
    if (page.length < PAGE_SIZE) return out;
  }
}

function isCritical(content: Record<string, unknown>): boolean {
  if (content.critical === true) return true;
  const name = String(content.name ?? content.merchant_key ?? "").toLowerCase();
  return CRITICAL_DEFAULT_VENDORS.some((v) => name.includes(v));
}

function modalAccount(parentIds: string[], txAccount: Map<string, string>): string | null {
  const counts = new Map<string, number>();
  for (const id of parentIds) {
    const acct = txAccount.get(id);
    if (acct) counts.set(acct, (counts.get(acct) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestN = 0;
  for (const [acct, n] of counts) {
    if (n > bestN) {
      bestN = n;
      best = acct;
    }
  }
  return best;
}

async function fetchRiskData(): Promise<AccountGroup[]> {
  const [accounts, subscriptions, transactions] = await Promise.all([
    substrateClient.listBeads({ namespace: "finance", type: "account", state: "active", limit: 200 }),
    substrateClient.listBeads({ namespace: "finance", type: "subscription", state: "active", limit: 1000 }),
    listAll({ namespace: "finance", type: "transaction" }),
  ]);

  // tx id -> account_id, for resolving subscription funding.
  const txAccount = new Map<string, string>();
  for (const t of transactions) {
    const acct = (t.content as Record<string, unknown>).account_id;
    if (typeof acct === "string") txAccount.set(t.id, acct);
  }

  // Dedup subscriptions to the most-recent bead per merchant (auditor appends).
  const latest = new Map<string, Bead>();
  for (const b of subscriptions) {
    const c = b.content as Record<string, unknown>;
    const key = String(c.merchant_key ?? c.name ?? b.id);
    const prev = latest.get(key);
    if (!prev || (b.created_at ?? "") > (prev.created_at ?? "")) latest.set(key, b);
  }

  const accountById = new Map(accounts.map((a) => [a.id, a]));
  const groups = new Map<string, AccountGroup>();
  const groupFor = (accountId: string | null): AccountGroup => {
    const key = accountId ?? "__unmapped__";
    let g = groups.get(key);
    if (!g) {
      const acct = accountId ? accountById.get(accountId) : undefined;
      g = {
        account: acct ? { id: acct.id, content: acct.content as unknown as FinanceAccountContent } : null,
        subs: [],
      };
      groups.set(key, g);
    }
    return g;
  };

  for (const b of latest.values()) {
    const c = b.content as Record<string, unknown>;
    const provenance = (b.provenance ?? {}) as Record<string, unknown>;
    const parentIds = (provenance.parent_ids as string[] | undefined) ?? [];
    const accountId = modalAccount(parentIds, txAccount);
    groupFor(accountId).subs.push({
      beadId: b.id,
      name: String(c.name ?? c.merchant_key ?? "—"),
      amount: typeof c.amount === "number" ? c.amount : undefined,
      frequency: typeof c.frequency === "string" ? c.frequency : undefined,
      critical: isCritical(c),
      accountId,
    });
  }

  // Order: accounts with critical deps first, then by subscription count.
  return [...groups.values()].sort((a, b) => {
    const ac = a.subs.some((s) => s.critical) ? 1 : 0;
    const bc = b.subs.some((s) => s.critical) ? 1 : 0;
    if (ac !== bc) return bc - ac;
    return b.subs.length - a.subs.length;
  });
}

function isLiability(content: FinanceAccountContent | undefined): boolean {
  return ["credit", "loan"].includes(String(content?.type ?? "").toLowerCase());
}

export function MerchantRiskRoute() {
  const riskQuery = useQuery({ queryKey: ["merchant-risk"], queryFn: fetchRiskData });
  const groups = riskQuery.data ?? [];

  const criticalCount = useMemo(
    () => groups.reduce((n, g) => n + g.subs.filter((s) => s.critical).length, 0),
    [groups],
  );

  return (
    <>
      <PageHeader
        title="Merchant Risk"
        subtitle="Funding dependencies — which card/account each recurring commitment relies on"
        right={
          <span className="text-2xs text-fg-subtle num">
            {criticalCount} critical dependenc{criticalCount === 1 ? "y" : "ies"}
          </span>
        }
      />

      <div className="p-6 space-y-4">
        <Card>
          <CardBody className="text-2xs text-fg-subtle">
            Card-expiry data isn't synced from Plaid yet, so an automatic "card expiring
            in 30 days" alert has no source. Until then, this maps every recurring
            commitment to its funding account and flags{" "}
            <span className="text-warn">critical</span> merchants tied to a credit card
            for manual expiry review.
          </CardBody>
        </Card>

        {riskQuery.isLoading ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">Building dependency map…</CardBody>
          </Card>
        ) : riskQuery.isError ? (
          <Card>
            <CardBody className="text-center text-neg py-8">Failed to load risk data.</CardBody>
          </Card>
        ) : groups.length === 0 ? (
          <Card>
            <CardBody className="text-center text-fg-muted py-8">
              No recurring commitments detected yet.
            </CardBody>
          </Card>
        ) : (
          groups.map((g, i) => {
            const acct = g.account?.content;
            const liability = isLiability(acct);
            const hasCritical = g.subs.some((s) => s.critical);
            const label = acct
              ? `${acct.name}${acct.mask ? ` ••${acct.mask}` : ""}`
              : "Unmapped (no funding account resolved)";
            return (
              <Card key={g.account?.id ?? `unmapped-${i}`} className={hasCritical && liability ? "border-warn" : undefined}>
                <CardHeader
                  title={label}
                  hint={acct ? `${acct.institution ?? ""} · ${acct.type ?? ""}${acct.subtype ? ` / ${acct.subtype}` : ""}` : "subscriptions whose funding account couldn't be resolved"}
                  right={
                    <div className="flex items-center gap-2">
                      {liability ? <Badge tone="neg">credit</Badge> : acct ? <Badge tone="neutral">{acct.type}</Badge> : null}
                      {hasCritical && liability ? <Badge tone="warn">verify expiry</Badge> : null}
                      <span className="text-2xs text-fg-subtle num">{g.subs.length} commitments</span>
                    </div>
                  }
                />
                <CardBody className="flex flex-wrap gap-2">
                  {g.subs.map((s) => (
                    <Link
                      key={s.beadId}
                      to={`/merchant/${encodeURIComponent(s.name)}`}
                      className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-sm hover:bg-bg-hover/60 transition-colors"
                    >
                      {s.critical ? <span className="text-warn" title="critical dependency">★</span> : null}
                      <span>{s.name}</span>
                      {s.amount != null ? (
                        <span className="num text-2xs text-fg-subtle">{fmtCurrency(s.amount)}{s.frequency ? `/${s.frequency.slice(0, 2)}` : ""}</span>
                      ) : null}
                    </Link>
                  ))}
                </CardBody>
              </Card>
            );
          })
        )}
      </div>
    </>
  );
}
