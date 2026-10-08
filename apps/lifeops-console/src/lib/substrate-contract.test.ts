import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";

// The console must depend on the bead store through one contract: the named
// operations on providers/substrate-client.ts. Two ways that discipline can
// erode without anyone noticing until the browser breaks:
//   1. a new file re-deriving the proxy prefix instead of importing the one
//      resolved value from lib/env.ts;
//   2. a new file composing a bead-store resource path (or calling fetch
//      directly) instead of calling a named substrateClient operation.
// Both are checked here on every `npm run test`, so a regression fails the
// build instead of surfacing as "dev moved and the console broke".
const SRC_ROOT = fileURLToPath(new URL("..", import.meta.url));
const THIS_FILE = fileURLToPath(import.meta.url);

const PROXY_OWNER = join(SRC_ROOT, "lib", "env.ts");
const PROXY_OWNER_TEST = join(SRC_ROOT, "lib", "env.test.ts");
const PROXY_TYPE_DECL = join(SRC_ROOT, "vite-env.d.ts");

// Matches the env var name, or the literal proxy prefix used as a string
// (as opposed to `SUBSTRATE_PROXY`, the already-resolved constant, which is
// fine to reference for display purposes outside the client module).
const PROXY_DEFINITION_PATTERN = /VITE_SUBSTRATE_PROXY|["'`]\/substrate(["'`]|\/)/;

// Bead-store resource paths as composed in providers/substrate-client.ts.
const REST_PATH_PATTERN = /["'`]\/beads(["'`]|\/|\$)|["'`]\/rules\/dry-run["'`]/;

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...listSourceFiles(full));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

function isTestFile(file: string): boolean {
  return /\.test\.tsx?$/.test(file);
}

describe("bead-store contract confinement", () => {
  it("confines the proxy prefix definition to lib/env.ts", () => {
    const offenders: string[] = [];
    for (const file of listSourceFiles(SRC_ROOT)) {
      if (file === PROXY_OWNER || file === PROXY_OWNER_TEST || file === PROXY_TYPE_DECL || file === THIS_FILE) continue;
      const text = readFileSync(file, "utf8");
      if (PROXY_DEFINITION_PATTERN.test(text)) offenders.push(relative(SRC_ROOT, file));
    }
    expect(offenders).toEqual([]);
  });

  // Every source dir except providers/ (the one client module, exempt by
  // the spec) — the release gate proved by mutation that a components/ file
  // calling fetch("http://substrate:8000/beads") escaped the lib+routes
  // scope this ratchet originally had.
  const RATCHETED_DIRS = ["lib", "routes", "components", "hooks"].filter((d) => {
    try {
      return statSync(join(SRC_ROOT, d)).isDirectory();
    } catch {
      return false;
    }
  });

  // A router prop like `to: "/beads"` is a BROWSER route, not a store
  // path — strip those before scanning so the ratchet reads store reach,
  // not navigation (Layout.tsx's nav links are the known case). A real
  // store call in the same file still trips the pattern.
  function withoutRouterPaths(text: string): string {
    return text.replace(/\b(?:to|path):\s*["'`][^"'`]*["'`]/g, "");
  }

  it("keeps ratcheted dirs free of hand-composed bead-store resource paths", () => {
    const offenders: string[] = [];
    for (const dir of RATCHETED_DIRS) {
      for (const file of listSourceFiles(join(SRC_ROOT, dir))) {
        if (isTestFile(file)) continue;
        const text = withoutRouterPaths(readFileSync(file, "utf8"));
        if (REST_PATH_PATTERN.test(text)) offenders.push(relative(SRC_ROOT, file));
      }
    }
    expect(offenders).toEqual([]);
  });

  it("never calls fetch directly outside the client module", () => {
    const offenders: string[] = [];
    for (const dir of RATCHETED_DIRS) {
      for (const file of listSourceFiles(join(SRC_ROOT, dir))) {
        if (isTestFile(file)) continue;
        const text = readFileSync(file, "utf8");
        if (/\bfetch\s*\(/.test(text)) offenders.push(relative(SRC_ROOT, file));
      }
    }
    expect(offenders).toEqual([]);
  });

  // An absolute URL pointing at the store bypasses the proxy prefix entirely
  // — the exact "store swap becomes a console rewrite" the charter closes.
  // Display prose may name a URL; code may not. Shrink-only allowlist.
  const ABSOLUTE_STORE_URL = /https?:\/\/[^\s"'`]*(?:substrate|:8000|:18001)/;
  const ABSOLUTE_URL_ALLOWLIST = new Set([
    // Informational UI copy describing the legacy direct endpoint; not code.
    "routes/ProjectAlpha.tsx",
  ]);

  it("never hardcodes an absolute store URL anywhere in src/", () => {
    const offenders: string[] = [];
    for (const file of listSourceFiles(SRC_ROOT)) {
      if (isTestFile(file) || file === THIS_FILE) continue;
      const rel = relative(SRC_ROOT, file);
      if (rel.startsWith("providers/") || ABSOLUTE_URL_ALLOWLIST.has(rel)) continue;
      const text = readFileSync(file, "utf8");
      if (ABSOLUTE_STORE_URL.test(text)) offenders.push(rel);
    }
    expect(offenders).toEqual([]);
  });

  // The second intake: a dev.* bead filed by a raw substrateClient.createBead
  // skips its gateway capability's own refusal checks entirely (dev.task:
  // file_task.py's duplicate spec_identity, unresolvable release_ref,
  // forbidden scope paths, ...; dev.note: the closed kind vocabulary and its
  // per-kind required fields — routers/v1/factory.py's NoteCreateRequest),
  // so a filing the gateway would refuse becomes a bead the factory has to
  // refuse later, at claim time, or strand (docs/audits/
  // 2026-09-12-architecture-review-modularity-and-contracts.md §5.3). Now
  // that both gateway capabilities exist (POST /api/v1/factory/tasks, M9/
  // #909; POST /api/v1/factory/note, this bead), routes/ and components/
  // must file any dev.* bead through one of them, never through a direct
  // createBead call. Detected by the co-occurrence of the TaskIntakeDraft
  // shape (the one dev.task-intake form in this tree) and a createBead call
  // in the same file — exactly what FactoryBoard.tsx had before #920's rule
  // existed — OR an inline dev.* literal and a createBead call in the same
  // file — exactly what TaskThreadPanel.tsx had before this bead.
  const TASK_INTAKE_DRAFT_PATTERN = /\bTaskIntakeDraft\b/;
  const CREATE_BEAD_CALL_PATTERN = /\bcreateBead\s*\(/;
  // Gate finding F1 (#920). Keying only on the identifier `TaskIntakeDraft`
  // detected THE OFFENDER THAT EXISTED, not the rule this bead exists to
  // install: a second intake reintroduced tomorrow as
  // `createBead({ namespace: "dev", type: "task", ... })`, without ever
  // naming that type, passed this check GREEN. Proven at the gate by adding
  // exactly that call to a routes/ file and watching the rule stay green.
  // So the literal shape is matched too. Both halves are kept rather than
  // one replacing the other: the identifier catches a payload built
  // elsewhere and passed in, and the literal catches an inline object that
  // never mentions the type.
  //
  // Widened from `type: "task"` to any dev.* type (this bead's AC-2): the
  // gap this rule closes isn't specific to dev.task, and a rule that only
  // ever matched "task" would stay blind to `createBead({ namespace: "dev",
  // type: "note", ... })` — the exact shape TaskThreadPanel.tsx had.
  const DEV_NAMESPACE_LITERAL_PATTERN = /namespace:\s*["']dev["']/;
  const DEV_TYPE_LITERAL_PATTERN = /type:\s*["'][a-zA-Z_]+["']/;

  it("never files a dev.* bead via a direct createBead call from routes/ or components/", () => {
    const offenders: string[] = [];
    for (const dir of ["routes", "components"]) {
      const dirPath = join(SRC_ROOT, dir);
      let exists = true;
      try {
        statSync(dirPath);
      } catch {
        exists = false;
      }
      if (!exists) continue;
      for (const file of listSourceFiles(dirPath)) {
        if (isTestFile(file)) continue;
        const text = readFileSync(file, "utf8");
        const namesTheDraftType = TASK_INTAKE_DRAFT_PATTERN.test(text);
        const buildsADevBeadInline =
          DEV_NAMESPACE_LITERAL_PATTERN.test(text) && DEV_TYPE_LITERAL_PATTERN.test(text);
        if (CREATE_BEAD_CALL_PATTERN.test(text) && (namesTheDraftType || buildsADevBeadInline)) {
          offenders.push(relative(SRC_ROOT, file));
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  // PC-ASR-002/AC-1 (impact analysis served as one gateway capability):
  // Architecture.tsx's impact panel must consume POST /api/v1/factory/impact
  // through factoryStatusClient.impact(...) for application/ci origins,
  // never re-derive the answer client-side over raw listBeadLinks reads the
  // way it did before that route existed. A regression that goes back to
  // computing the whole panel from blast-radius.ts's `blastRadius(...)` walk
  // without ever calling the served endpoint trips this.
  it("Architecture's impact panel consumes the served /factory/impact answer", () => {
    const architectureFile = join(SRC_ROOT, "routes", "Architecture.tsx");
    const text = readFileSync(architectureFile, "utf8");
    expect(text).toMatch(/factoryStatusClient\.impact\(/);
  });

  // Round-2 #1020 gate finding: the check above alone is too weak -- it only
  // asks whether the string appears ANYWHERE in the file, so it stays green
  // even if an application/ci origin ALSO reaches blast-radius.ts's
  // client-side `blastRadius(...)` walk (which is exactly what
  // BlastRadiusPanel did before this fix: `blastRadius(...)` was called
  // unconditionally on every render, regardless of `servedKind`, just with
  // its result only ever displayed for the null-servedKind branch). This
  // walks BlastRadiusPanel's own source and requires every `blastRadius(`
  // call to be textually guarded by a `servedKind === null` check on the
  // same line -- the shape the real fix uses (`servedKind === null ?
  // blastRadius(...) : null` inside the panel's one `useMemo`) -- so a
  // regression that calls it unconditionally again fails this rule instead
  // of only the substring check above.
  // Round-2 #1024 gate finding: this used to find the FIRST "{" after
  // "function BlastRadiusPanel(", which is the destructured PARAMETER
  // list's own opening brace (the signature destructures its props:
  // `function BlastRadiusPanel({ origin, entities, links, onSelect }: {...})`),
  // not the function body. Every check below ran against the extracted text
  // `{ origin, entities, links, onSelect, }` -- four identifiers that can
  // never contain "blastRadius(" -- so the rule could never fail regardless
  // of what the real body did. Fixed by balance-matching the parameter
  // list's own parentheses first, then finding the body's "{" only after
  // they close.
  function extractFunctionBody(text: string, functionName: string): string {
    const startMatch = new RegExp(`function ${functionName}\\(`).exec(text);
    if (!startMatch) throw new Error(`${functionName} not found in source`);
    const parenStart = text.indexOf("(", startMatch.index);
    let parenDepth = 0;
    let parenEnd = -1;
    for (let i = parenStart; i < text.length; i++) {
      if (text[i] === "(") parenDepth++;
      else if (text[i] === ")") {
        parenDepth--;
        if (parenDepth === 0) {
          parenEnd = i;
          break;
        }
      }
    }
    if (parenEnd === -1) throw new Error(`${functionName}'s parameter list never closes`);
    const braceStart = text.indexOf("{", parenEnd);
    let depth = 0;
    for (let i = braceStart; i < text.length; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}") {
        depth--;
        if (depth === 0) return text.slice(braceStart, i + 1);
      }
    }
    throw new Error(`${functionName}'s braces never close`);
  }

  // Strip comments before scanning: with extractFunctionBody now returning
  // the REAL body (see the fix above), a docstring comment merely
  // mentioning "blastRadius(...)" in prose -- exactly what this file's own
  // BlastRadiusPanel carries, explaining the guard -- would otherwise trip
  // the reachability check as a false positive.
  function stripComments(source: string): string {
    return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  }

  function blastRadiusReachableForServedOrigin(functionSource: string): boolean {
    return stripComments(functionSource)
      .split("\n")
      .some((line) => /\bblastRadius\(/.test(line) && !/servedKind === null/.test(line));
  }

  // The guard-line check above only catches an UNGUARDED call; it stays
  // green if `servedKind` is instead forced to a constant (e.g. `const
  // servedKind: ImpactKind | null = null;`), which silently disables the
  // served path for every origin without ever un-guarding the call
  // textually. This requires the statement assigning `servedKind` to
  // actually reference `origin.type`, not a bare literal.
  function servedKindDerivesFromOriginType(functionSource: string): boolean {
    const match = /const\s+servedKind\s*:[^=]*=([\s\S]*?);/.exec(stripComments(functionSource));
    if (!match) return false;
    return /origin\.type/.test(match[1]);
  }

  it("never lets an application or CI origin reach the client-side blastRadius(...) walk", () => {
    const architectureFile = join(SRC_ROOT, "routes", "Architecture.tsx");
    const text = readFileSync(architectureFile, "utf8");
    const panelSource = extractFunctionBody(text, "BlastRadiusPanel");
    expect(blastRadiusReachableForServedOrigin(panelSource)).toBe(false);
  });

  it("requires servedKind to be derived from origin.type, never assigned a constant", () => {
    const architectureFile = join(SRC_ROOT, "routes", "Architecture.tsx");
    const text = readFileSync(architectureFile, "utf8");
    const panelSource = extractFunctionBody(text, "BlastRadiusPanel");
    expect(servedKindDerivesFromOriginType(panelSource)).toBe(true);
  });

  // Required change 1: each of these mutations to the REAL Architecture.tsx
  // source (not a synthetic snippet) must turn the rule red. Proves
  // extractFunctionBody now finds the actual body -- against the pre-fix
  // extraction (the props destructuring alone), neither mutation below could
  // ever have been detected, because the extracted "body" never contained
  // `blastRadius(` or `servedKind` in the first place.
  const SERVED_KIND_ASSIGNMENT =
    'const servedKind: ImpactKind | null =\n' +
    '    origin.type === "application" || origin.type === "ci" ? (origin.type as ImpactKind) : null;';
  const GUARDED_BLAST_RADIUS_CALL =
    '() => (servedKind === null ? blastRadius(origin, entities, links) : null)';

  it("turns red on the real source when servedKind is forced to a constant null", () => {
    const architectureFile = join(SRC_ROOT, "routes", "Architecture.tsx");
    const text = readFileSync(architectureFile, "utf8");
    expect(text).toContain(SERVED_KIND_ASSIGNMENT);
    const mutated = text.replace(SERVED_KIND_ASSIGNMENT, "const servedKind: ImpactKind | null = null;");
    expect(mutated).not.toEqual(text);

    const panelSource = extractFunctionBody(mutated, "BlastRadiusPanel");
    expect(servedKindDerivesFromOriginType(panelSource)).toBe(false);
  });

  it("turns red on the real source when the guarded blastRadius(...) call is replaced with an unguarded one", () => {
    const architectureFile = join(SRC_ROOT, "routes", "Architecture.tsx");
    const text = readFileSync(architectureFile, "utf8");
    expect(text).toContain(GUARDED_BLAST_RADIUS_CALL);
    const mutated = text.replace(GUARDED_BLAST_RADIUS_CALL, "() => blastRadius(origin, entities, links)");
    expect(mutated).not.toEqual(text);

    const panelSource = extractFunctionBody(mutated, "BlastRadiusPanel");
    expect(blastRadiusReachableForServedOrigin(panelSource)).toBe(true);
  });

  it("the reachability check above turns red if servedKind is ever forced to null", () => {
    // The exact shape of the pre-fix regression: blastRadius(...) called
    // unconditionally, with only its RESULT gated on servedKind -- proving
    // this check would have caught it, not just the substring check.
    const forcedNullShape = `
      function BlastRadiusPanel({ origin, entities, links }) {
        const servedKind = null;
        const localResult = useMemo(() => blastRadius(origin, entities, links), [origin, entities, links]);
        if (servedKind === null) {
          const result = localResult;
          return result;
        }
        return null;
      }
    `;
    expect(blastRadiusReachableForServedOrigin(forcedNullShape)).toBe(true);
  });
});
