#!/usr/bin/env bash
# repo-hygiene-guard.sh — Asserts repository hygiene invariants (Phase 10 T1.4)
set -euo pipefail

echo "=== Running Repo Hygiene Guard Invariant Checks ==="
FAILED=0

# Check 1: No git-tracked files in cache or dependency dirs
echo "--> Check 1: No tracked cache/dependency directories..."
TRACKED_CACHE=$(git ls-files | grep -E '__pycache__|\.venv/|\.ruff_cache|node_modules/' || true)
if [ -n "$TRACKED_CACHE" ]; then
  echo "❌ FAIL: Tracked files found in cache or dependency directories:" >&2
  echo "$TRACKED_CACHE" >&2
  FAILED=1
else
  echo "✅ PASS: No tracked cache/dependency files."
fi

# Check 2: No tracked files matching render-dump patterns
echo "--> Check 2: No tracked kustomize render dumps..."
# Render dump patterns: infrastructure/k8s/json, *.rendered.yaml, kustomize-build*.yaml
TRACKED_RENDERS=$(git ls-files | grep -E '^infrastructure/k8s/json/|.*\.rendered\.yaml|.*kustomize-build.*\.yaml' || true)
if [ -n "$TRACKED_RENDERS" ]; then
  echo "❌ FAIL: Tracked kustomize render artifacts found:" >&2
  echo "$TRACKED_RENDERS" >&2
  FAILED=1
else
  echo "✅ PASS: No tracked kustomize render artifacts."
fi

# Check 3: No orphan root-level markdown files outside the allowlist
echo "--> Check 3: Checking root-level markdown files against allowlist..."
# Allowlist: README.md, ARCHITECTURE.md, GEMINI.md, CLAUDE.md, AGENTS.md
ALLOWED_PATTERN="^(README|ARCHITECTURE|GEMINI|CLAUDE|AGENTS)\.md$"
ROOT_MDS=$(find . -maxdepth 1 -name "*.md" -exec basename {} \;)
for md in $ROOT_MDS; do
  if [[ ! "$md" =~ $ALLOWED_PATTERN ]]; then
    echo "❌ FAIL: Untracked or non-allowlisted root-level markdown file found: $md" >&2
    FAILED=1
  fi
done

if [ "$FAILED" -eq 1 ]; then
  echo "❌ Repo Hygiene Invariant Check FAILED" >&2
  exit 1
fi

echo "✅ Repo Hygiene Invariant Check PASSED"
exit 0
