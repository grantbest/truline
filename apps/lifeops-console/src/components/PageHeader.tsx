import type { ReactNode } from "react";

interface Props {
  title: string;
  subtitle?: string;
  right?: ReactNode;
}

export function PageHeader({ title, subtitle, right }: Props) {
  return (
    <div className="border-b border-border bg-bg-panel/50 px-6 py-4 flex items-center justify-between">
      <div>
        <h1 className="text-lg font-semibold text-fg">{title}</h1>
        {subtitle ? <p className="text-xs text-fg-muted mt-0.5">{subtitle}</p> : null}
      </div>
      {right}
    </div>
  );
}
