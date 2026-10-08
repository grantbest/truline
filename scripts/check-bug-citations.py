#!/usr/bin/env python3
"""CLI wrapper for the resolved known-bugs test-citation checker."""

from __future__ import annotations

import sys

import bug_citations


if __name__ == "__main__":
    sys.exit(bug_citations.main())
