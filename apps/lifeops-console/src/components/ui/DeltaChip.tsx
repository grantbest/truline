import { cn } from "@/lib/cn";
import { fmtCurrency } from "@/lib/format";

// A compact "vs previous" indicator: ▲/▼ + % change (and optional absolute
// delta). This is the bridge from a bare number to an insight — every Stat
// that has a prior-period value should carry one.
//
// Tone semantics: by default a rise is positive (net worth, cash). For metrics
// where down is good (spend, debt), pass `invert` so a decrease shows green.
interface DeltaChipProps {
  current: number;
  previous: number | null | undefined;
  // Flip tone: a decrease is "good" (spend, liabilities).
  invert?: boolean;
  // Show the absolute delta (e.g. +$1,240) alongside the percentage.
  showAbsolute?: boolean;
  className?: string;
}

export function DeltaChip({ current, previous, invert = false, showAbsolute = false, className }: DeltaChipProps) {
  if (previous === null || previous === undefined || Number.isNaN(previous)) {
    return <span className={cn("text-2xs text-fg-subtle", className)}>—</span>;
  }

  const delta = current - previous;
  const base = Math.abs(previous);
  const pct = base === 0 ? null : (delta / base) * 100;

  // Treat sub-cent moves as flat to avoid noisy ▲0.0% chips.
  const isFlat = Math.abs(delta) < 0.005;
  const isUp = delta > 0;
  const good = isFlat ? null : invert ? !isUp : isUp;
  const tone = good === null ? "text-fg-subtle" : good ? "text-pos" : "text-neg";
  const arrow = isFlat ? "→" : isUp ? "▲" : "▼";

  return (
    <span className={cn("inline-flex items-center gap-1 text-2xs num font-medium", tone, className)}>
      <span aria-hidden>{arrow}</span>
      {pct === null ? "—" : `${Math.abs(pct).toFixed(1)}%`}
      {showAbsolute ? (
        <span className="text-fg-subtle">
          ({delta >= 0 ? "+" : "−"}
          {fmtCurrency(Math.abs(delta))})
        </span>
      ) : null}
    </span>
  );
}
