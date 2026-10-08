import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

// The browser is the second web surface this consolidation is about
// (apps/lifeops-console/src/providers/substrate-client.ts) -- it must never
// hold the credential mcp-hub injects server-side (see env.ts and
// substrate-client.ts's own comments). This walks the whole console source
// tree, read-only, so a new file that starts building its own substrate
// client -- with its own header, its own key -- fails here instead of
// shipping quietly. "Established by test, not by inspection."

const LIB_DIR = fileURLToPath(new URL(".", import.meta.url));
const SRC_ROOT = path.resolve(LIB_DIR, "..");

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules") continue;
    const full = path.join(dir, entry);
    const info = statSync(full);
    if (info.isDirectory()) {
      out.push(...listSourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

// Patterns that would mean a file is actually *setting* the credential or
// reading the raw secret env var, not just mentioning the header name in a
// comment (env.ts and substrate-client.ts both explain, in prose, that
// mcp-hub -- not the browser -- injects it).
const FORBIDDEN_PATTERNS: RegExp[] = [
  /SUBSTRATE_API_KEY/, // the raw secret env var name -- mcp-hub's alone to read
  // Any way of naming the header at all: object key, bracket assignment,
  // Headers#set/append -- the release gate proved by mutation that the
  // object-key-only form let `headers["x-api-key"] = ...` through.
  /["'`]x-api-key["'`]\s*[:\]=]/i,
  /headers\.(?:set|append)\(\s*["'`]x-api-key["'`]/i,
  // The browser sends NO auth header of any kind -- the proxy owns auth.
  // An Authorization header client-side is the same widening in different
  // clothes (second escaped mutation probe).
  /["'`]authorization["'`]\s*[:\]=]/i,
  /headers\.(?:set|append)\(\s*["'`]authorization["'`]/i,
];

const files = listSourceFiles(SRC_ROOT);

describe("browser surface never carries the raw substrate credential", () => {
  it("walked more than a handful of source files (sanity check on the walk itself)", () => {
    expect(files.length).toBeGreaterThan(10);
  });

  it.each(files.map((file) => [path.relative(SRC_ROOT, file), file] as const))(
    "%s never sets or reads the raw Substrate API key",
    (_label, file) => {
      const contents = readFileSync(file, "utf-8");
      for (const pattern of FORBIDDEN_PATTERNS) {
        expect(contents).not.toMatch(pattern);
      }
    },
  );
});
