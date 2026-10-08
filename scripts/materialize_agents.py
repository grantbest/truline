#!/usr/bin/env python3
"""Copy tracked persona definitions into a worktree's .claude/agents/.

Amendment 30 diff item 4: persona definitions are repository artefacts under
docs/agents/ — tracked, diffable, reviewed. `.gitignore` ignores `.claude/`
wholesale, so the copies this script writes can never be committed and drift
into a second source of truth. A persona that exists only in the ignored
directory is not a persona, it is memory.

Usage:
    python3 scripts/materialize_agents.py /path/to/worktree

Writes nowhere by default: the destination is required, must exist, and must
be a directory. The dispatcher calls this before invoking a `claude` worker
(Amendment 30 implementation plan, PR-7).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "docs" / "agents"

# The personas the dispatcher materialises. The repo invariant asserts each
# exists; adding a persona means adding it here AND under docs/agents/.
PERSONAS = ("polecat-developer.md", "architect-sme.md")


def materialize(destination: Path, agents_dir: Path = AGENTS_DIR) -> list[Path]:
    if not destination.is_dir():
        raise SystemExit(f"destination is not an existing directory: {destination}")
    target = destination / ".claude" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in PERSONAS:
        source = agents_dir / name
        if not source.is_file():
            raise SystemExit(f"persona missing from repository: {source}")
        out = target / name
        shutil.copyfile(source, out)
        written.append(out)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="worktree to materialise into")
    args = parser.parse_args(argv)
    for path in materialize(args.destination):
        print(f"materialised {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
