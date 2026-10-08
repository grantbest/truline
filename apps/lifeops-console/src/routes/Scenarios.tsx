import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { mcpClient, type ScenarioProjection } from "@/providers/mcp-client";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody, CardHeader, Stat } from "@/components/ui/Card";
import { Table, TBody, Td, Th, THead, Tr } from "@/components/ui/Table";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { fmtCurrency } from "@/lib/format";

// SDD Phase 3 — Cash Flow Counterfactuals. Submit a what-if; mcp-hub starts the
// FinanceScenarioRunnerWorkflow, waits, and returns a 12-month baseline-vs-
// scenario liquid-wealth projection (also persisted as a projection_scenario
// bead). We render it as a dual-line chart + month table — no charting dep.

export function ScenariosRoute() {
  const [name, setName] = useState("Buy New Car");
  const [upfront, setUpfront] = useState("-5000");
  const [monthly, setMonthly] = useState("-600");

  const run = useMutation<ScenarioProjection, Error, void>({
    mutationFn: () =>
      mcpClient.runScenario({
        scenario_name: name.trim() || "Scenario",
        upfront_impact: Number(upfront) || 0,
        monthly_impact: Number(monthly) || 0,
      }),
  });

  const projection = run.data;

  return (
    <>
      <PageHeader
        title="Scenarios"
        subtitle="Cash-flow what-if — project a decision's 12-month impact on liquid wealth"
      />

      <div className="p-6 space-y-4">
        <Card>
          <CardHeader
            title="Define a scenario"
            hint="Upfront = one-time cash hit at month 0. Monthly = recurring change. Use negative numbers for costs."
          />
          <CardBody>
            <form
              className="flex flex-wrap items-end gap-4"
              onSubmit={(e) => {
                e.preventDefault();
                run.mutate();
              }}
            >
              <label className="flex flex-col gap-1">
                <span className="text-2xs text-fg-subtle uppercase tracking-wide">Scenario</span>
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Buy New Car"
                  className="w-56"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-2xs text-fg-subtle uppercase tracking-wide">
                  Upfront impact ($)
                </span>
                <Input
                  type="number"
                  value={upfront}
                  onChange={(e) => setUpfront(e.target.value)}
                  className="w-36 num"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-2xs text-fg-subtle uppercase tracking-wide">
                  Monthly impact ($)
                </span>
                <Input
                  type="number"
                  value={monthly}
                  onChange={(e) => setMonthly(e.target.value)}
                  className="w-36 num"
                />
              </label>
              <Button type="submit" disabled={run.isPending}>
                {run.isPending ? "Projecting…" : "Run projection"}
              </Button>
            </form>
            {run.isError ? (
              <div className="mt-3 text-sm text-neg">
                Projection failed: {run.error?.message ?? "unknown error"}
              </div>
            ) : null}
          </CardBody>
        </Card>

        {projection ? <ProjectionResult projection={projection} /> : null}
      </div>
    </>
  );
}

function ProjectionResult({ projection: p }: { projection: ScenarioProjection }) {
  const betterOff = p.delta_ending >= 0;
  return (
    <>
      <Card>
        <CardHeader
          title={`Projection — ${p.name}`}
          hint={`${p.horizon_months}-month horizon · starting liquid ${fmtCurrency(p.starting_liquid)} · net flow ${fmtCurrency(p.net_monthly_flow)}/mo`}
        />
        <CardBody className="grid grid-cols-3 gap-6">
          <Stat label="Baseline (12mo)" value={fmtCurrency(p.ending_baseline)} sub="if you do nothing" />
          <Stat
            label="Scenario (12mo)"
            value={fmtCurrency(p.ending_scenario)}
            sub={`upfront ${fmtCurrency(p.upfront_impact)} · ${fmtCurrency(p.monthly_impact)}/mo`}
          />
          <Stat
            label="Net difference"
            value={fmtCurrency(p.delta_ending)}
            tone={betterOff ? "pos" : "neg"}
            sub={betterOff ? "ahead vs baseline" : "behind vs baseline"}
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Liquid wealth trajectory" hint="Baseline (muted) vs scenario (accent)" />
        <CardBody>
          <DualLineChart
            months={p.months}
            baseline={p.baseline_wealth}
            scenario={p.scenario_wealth}
          />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Month-by-month" />
        <div className="overflow-x-auto">
          <Table>
            <THead>
              <Tr>
                <Th>Month</Th>
                <Th className="text-right">Baseline</Th>
                <Th className="text-right">Scenario</Th>
                <Th className="text-right">Difference</Th>
              </Tr>
            </THead>
            <TBody>
              {p.months.map((m, i) => {
                const diff = (p.scenario_wealth[i] ?? 0) - (p.baseline_wealth[i] ?? 0);
                return (
                  <Tr key={m}>
                    <Td className="num">{m}</Td>
                    <Td className="num text-right text-fg-muted">
                      {fmtCurrency(p.baseline_wealth[i])}
                    </Td>
                    <Td className="num text-right">{fmtCurrency(p.scenario_wealth[i])}</Td>
                    <Td className={`num text-right ${diff < 0 ? "text-neg" : "text-pos"}`}>
                      {fmtCurrency(diff)}
                    </Td>
                  </Tr>
                );
              })}
            </TBody>
          </Table>
        </div>
      </Card>
    </>
  );
}

// Minimal hand-rolled dual-line chart — same "no dependency" ethos as the
// budget ProgressBar. viewBox coordinates; the SVG scales to its container.
function DualLineChart({
  months,
  baseline,
  scenario,
}: {
  months: string[];
  baseline: number[];
  scenario: number[];
}) {
  const W = 640;
  const H = 220;
  const PAD = 36;

  const { baselinePts, scenarioPts, zeroY, min, max } = useMemo(() => {
    const all = [...baseline, ...scenario, 0];
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const span = hi - lo || 1;
    const n = Math.max(months.length - 1, 1);
    const x = (i: number) => PAD + (i / n) * (W - 2 * PAD);
    const y = (v: number) => H - PAD - ((v - lo) / span) * (H - 2 * PAD);
    const toPts = (series: number[]) => series.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    return {
      baselinePts: toPts(baseline),
      scenarioPts: toPts(scenario),
      zeroY: y(0),
      min: lo,
      max: hi,
    };
  }, [months, baseline, scenario]);

  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Wealth projection">
        {/* zero line when the range crosses it */}
        {min < 0 && max > 0 ? (
          <line
            x1={PAD}
            x2={W - PAD}
            y1={zeroY}
            y2={zeroY}
            className="stroke-neg/40"
            strokeDasharray="3 3"
            strokeWidth={1}
          />
        ) : null}
        <polyline
          points={baselinePts}
          fill="none"
          className="stroke-fg-subtle"
          strokeWidth={1.5}
        />
        <polyline points={scenarioPts} fill="none" className="stroke-accent" strokeWidth={2} />
      </svg>
      <div className="flex items-center justify-between text-2xs text-fg-subtle num">
        <span>{months[0]}</span>
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1">
            <span className="inline-block h-0.5 w-4 bg-fg-subtle" /> baseline
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-0.5 w-4 bg-accent" /> scenario
          </span>
        </span>
        <span>{months[months.length - 1]}</span>
      </div>
    </div>
  );
}
