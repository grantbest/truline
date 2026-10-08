import { useId } from "react";
import { cn } from "@/lib/cn";

// Dependency-free SVG trend line. The console deliberately ships no charting
// library (recharts/visx would dwarf the bundle), so this draws a normalized
// polyline in a fixed viewBox and lets CSS scale it. Color is driven entirely
// by `currentColor` — set tone with a Tailwind text-* class on the parent so
// it inherits the existing theme tokens (text-pos / text-neg / text-accent).
interface SparklineProps {
  data: number[];
  // viewBox units — the SVG scales to its container via width/height 100%.
  width?: number;
  height?: number;
  strokeWidth?: number;
  // Fill the area under the line with a faint currentColor wash.
  area?: boolean;
  className?: string;
}

export function Sparkline({
  data,
  width = 120,
  height = 32,
  strokeWidth = 1.5,
  area = false,
  className,
}: SparklineProps) {
  // Need at least two points to draw a segment. Degrade to a flat baseline so
  // callers never have to special-case sparse history.
  if (!data || data.length < 2) {
    return (
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className={cn("text-fg-subtle", className)}
        aria-hidden
      >
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeDasharray="2 3"
          opacity={0.5}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    );
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1; // avoid /0 on a flat series
  // Pad vertically so the stroke isn't clipped at the extremes.
  const pad = strokeWidth + 1;
  const usableH = height - pad * 2;
  const stepX = width / (data.length - 1);

  const points = data.map((v, i) => {
    const x = i * stepX;
    const y = pad + (1 - (v - min) / span) * usableH;
    return [x, y] as const;
  });

  const linePath = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
  const areaPath = `${linePath} L${width} ${height} L0 ${height} Z`;
  const gradId = useId();

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={cn("overflow-visible", className)}
      aria-hidden
    >
      {area ? (
        <>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="currentColor" stopOpacity={0.22} />
              <stop offset="100%" stopColor="currentColor" stopOpacity={0} />
            </linearGradient>
          </defs>
          <path d={areaPath} fill={`url(#${gradId})`} stroke="none" />
        </>
      ) : null}
      <path
        d={linePath}
        fill="none"
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
