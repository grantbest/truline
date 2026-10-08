import { useState } from "react";
import { cn } from "@/lib/cn";

interface Props {
  data: unknown;
  className?: string;
  // Nodes at depth < initialExpandedDepth start expanded. Default 1:
  // the root container is open, nested containers start collapsed so a
  // deep bead doesn't dump everything at once.
  initialExpandedDepth?: number;
}

type JsonContainer = Record<string, unknown> | unknown[];

function isContainer(v: unknown): v is JsonContainer {
  return typeof v === "object" && v !== null;
}

export function JsonView({ data, className, initialExpandedDepth = 1 }: Props) {
  return (
    <div
      className={cn(
        "font-mono text-xs leading-relaxed text-fg bg-bg-subtle border border-border rounded p-3 overflow-auto",
        className,
      )}
    >
      <JsonNode value={data} depth={0} initialExpandedDepth={initialExpandedDepth} isLast />
    </div>
  );
}

interface NodeProps {
  value: unknown;
  depth: number;
  initialExpandedDepth: number;
  // Property key when rendering a member of an object.
  nodeKey?: string;
  // Suppress the trailing comma on the final entry of a container.
  isLast: boolean;
}

function JsonNode({ value, depth, initialExpandedDepth, nodeKey, isLast }: NodeProps) {
  const [open, setOpen] = useState(depth < initialExpandedDepth);

  const keyLabel =
    nodeKey !== undefined ? <span className="text-accent">{`"${nodeKey}"`}: </span> : null;
  const comma = isLast ? null : <span className="text-fg-subtle">,</span>;

  if (!isContainer(value)) {
    return (
      <div style={{ paddingLeft: depth * 12 }}>
        {keyLabel}
        <ScalarValue value={value} />
        {comma}
      </div>
    );
  }

  const isArray = Array.isArray(value);
  const entries: [string, unknown][] = isArray
    ? (value as unknown[]).map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);
  const openBracket = isArray ? "[" : "{";
  const closeBracket = isArray ? "]" : "}";

  if (entries.length === 0) {
    return (
      <div style={{ paddingLeft: depth * 12 }}>
        {keyLabel}
        <span className="text-fg-subtle">
          {openBracket}
          {closeBracket}
        </span>
        {comma}
      </div>
    );
  }

  return (
    <div style={{ paddingLeft: depth * 12 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-left hover:text-fg select-none"
      >
        <span className="inline-block w-3 text-fg-subtle">{open ? "▾" : "▸"}</span>
        {keyLabel}
        <span className="text-fg-subtle">{openBracket}</span>
        {open ? null : (
          <span className="text-fg-subtle">
            {" "}
            {isArray ? `${entries.length} items` : `${entries.length} keys`} {closeBracket}
          </span>
        )}
      </button>
      {open ? (
        <>
          {entries.map(([k, v], i) => (
            <JsonNode
              key={k}
              nodeKey={isArray ? undefined : k}
              value={v}
              depth={depth + 1}
              initialExpandedDepth={initialExpandedDepth}
              isLast={i === entries.length - 1}
            />
          ))}
          <div style={{ paddingLeft: depth * 12 }}>
            <span className="inline-block w-3" />
            <span className="text-fg-subtle">{closeBracket}</span>
            {comma}
          </div>
        </>
      ) : null}
    </div>
  );
}

function ScalarValue({ value }: { value: unknown }) {
  if (value === null) return <span className="text-fg-subtle">null</span>;
  if (typeof value === "string") return <span className="text-pos break-all">{`"${value}"`}</span>;
  if (typeof value === "number") return <span className="num text-accent">{value}</span>;
  if (typeof value === "boolean")
    return <span className="text-accent">{value ? "true" : "false"}</span>;
  return <span className="break-all">{String(value)}</span>;
}
