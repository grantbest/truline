import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Tone = "neutral" | "accent" | "pos" | "neg" | "warn";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

const toneClasses: Record<Tone, string> = {
  neutral: "bg-bg-subtle text-fg-muted border-border",
  accent: "bg-accent-muted/15 text-accent border-accent-muted/30",
  pos: "bg-pos/10 text-pos border-pos/30",
  neg: "bg-neg/10 text-neg border-neg/30",
  warn: "bg-warn/10 text-warn border-warn/30",
};

export function Badge({ className, tone = "neutral", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-2xs font-mono uppercase tracking-wider",
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  );
}

// State → tone mapping. Common Substrate states; falls back to neutral.
export function stateTone(state: string | undefined): Tone {
  if (!state) return "neutral";
  if (["active", "approved", "completed", "resolved", "synced"].includes(state)) return "pos";
  if (["pending", "queued", "draft"].includes(state)) return "warn";
  if (["rejected", "failed", "error", "stale"].includes(state)) return "neg";
  if (["archived", "deleted", "superseded"].includes(state)) return "neutral";
  return "accent";
}
