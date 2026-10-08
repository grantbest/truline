#!/usr/bin/env python3
"""CLI wrapper for the spec-record tracking coverage checker."""

from __future__ import annotations

import sys

import spec_record_coverage


if __name__ == "__main__":
    sys.exit(spec_record_coverage.main())
