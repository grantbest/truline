"""Shared test helper for dev.finding 79db3113 part c1/c2.

``check_clone_git_control`` stays a no-op through part c1, but part c2's
whole change is a one-line rewire to the real check
(``dispatch.verify_clone_git_control``), which fails closed when a clone
carries no recorded baseline. A fixture clone built by this suite's own
``git(..., "clone", ...)`` helper -- not through ``dispatch.make_clone``,
which already records one as its last statement -- never gets that record
unless a test asks for it. ``recorded()`` is that ask: call it once, right
after a fixture clone is created, so c2's rewire needs to touch no test in
this file.
"""

from __future__ import annotations

from pathlib import Path

import dispatch


def recorded(clone: Path) -> Path:
    """Record ``clone``'s git-control baseline and return it unchanged.

    For a fixture clone built directly (bypassing ``dispatch.make_clone``).
    Not for a path that was never actually cloned (no ``.git`` to
    fingerprint) -- those tests instead stub ``dispatch.check_clone_git_control``
    itself.
    """
    dispatch.record_clone_git_control(clone)
    return clone
