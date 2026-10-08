import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("card", className)} {...props} />;
}

export function CardHeader({
  title,
  hint,
  right,
  className,
}: {
  title: ReactNode;
  hint?: ReactNode;
  right?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-border px-3 py-2.5 sm:py-2", className)}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="panel-title">{title}</span>
        {hint ? <span className="text-2xs text-fg-subtle">{hint}</span> : null}
      </div>
      {right ? <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 self-start sm:self-auto">{right}</div> : null}
    </div>
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-3", className)} {...props} />;
}

export function Stat({
  label,
  value,
  sub,
  tone = "default",
  delta,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "default" | "pos" | "neg";
  // Optional vs-prior-period indicator (e.g. a <DeltaChip />) shown beside the value.
  delta?: ReactNode;
}) {
  const valueTone =
    tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "text-fg";
  return (
    <div className="flex flex-col gap-1">
      <span className="panel-title">{label}</span>
      <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className={cn("num text-xl sm:text-2xl font-semibold", valueTone)}>{value}</span>
        {delta}
      </span>
      {sub ? <span className="text-2xs text-fg-subtle num">{sub}</span> : null}
    </div>
  );
}
