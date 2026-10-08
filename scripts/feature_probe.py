"""Answers "does this invocation work HERE" by running it, never by parsing
a version string.

dev.finding, PR #941 (measured 2026-09-18): a GitHub Actions runner reported
``git version 2.34.1`` while a developer machine reported 2.50.1.
``git merge-tree --write-tree`` arrived in git 2.38, so the flag did not
exist on the runner; a test written against the developer's git went red in
CI with no distinguishing signal -- both directions of the guarded
assertion failed identically (``assert None is True`` and
``assert None is False``), the shape a command that never ran produces,
not the shape a classification bug produces. The fix probed the exact
invocation instead of parsing the version string, because a version number
is a PROXY for capability and proxies lie: a vendor build or a backport can
change what a version implies without changing the number, and a committed
manifest of "known-good" versions goes stale silently -- the same failure
mode this repository has already hit with a darwin-only test that only
ever proved its own claim in one place. A probe answers the question a
test actually needs answered ("can I run this, here, now"); a version
parse answers a different, merely correlated, question.

Twenty-one specs under apps/factory-dispatcher/tasks/ hand-copied a
paragraph warning engineers about this class of failure, because nothing
recorded it as code. This module exists so the next one can call something
instead of writing prose.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    """``available`` is the only field a caller should branch on. ``detail``
    is diagnostic content (the probe's stderr/stdout, or the exception
    string for a missing executable or a timeout) meant for a skip reason,
    never for another branch of logic."""

    available: bool
    detail: str


def probe_invocation(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | None = None,
    input: str | None = None,
    timeout: float = 10.0,
) -> ProbeResult:
    """Runs ``argv`` for real and reports whether the exact invocation a
    caller needs succeeds in this environment.

    This SHALL NOT parse ``git --version`` or any other version string as
    its primary mechanism. A version number is a proxy for capability, and
    proxies lie: a vendor build or a backport can change what a version
    implies without changing the number, and a committed manifest of
    runner versions goes stale silently -- exactly the failure this
    repository has documented before. A live probe answers the question a
    caller is actually asking ("does this invocation work here"); a
    version parse answers a merely correlated one. See the module
    docstring (dev.finding, PR #941) for the incident this was built to
    stop repeating.

    A nonzero exit, a missing executable, and a timeout are all reported
    the same way -- unavailable -- because a caller deciding whether it can
    run something does not need to distinguish those; ``detail`` carries
    that distinction for a skip reason instead.
    """
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(available=False, detail=str(exc))
    detail = (result.stderr or result.stdout or "").strip()
    return ProbeResult(available=result.returncode == 0, detail=detail)


def missing_capability_reason(
    *,
    requirement: str,
    capability: str,
    substitute_test: str,
    detail: str = "",
) -> str:
    """Builds a skip reason that names three things, because a bare
    ``skipif`` teaches a future reader nothing: WHAT is unavailable
    (``requirement``), WHAT version or capability would provide it
    (``capability``), and WHICH test covers the degraded contract instead
    (``substitute_test``) -- the shape PR #941's fix used. That third part
    is the other half of the answer: skipping a test is not itself the
    fix, and a reader who only sees "skipped" needs to know the contract
    is still exercised somewhere, not merely dropped -- the darwin-only
    trap this repository has hit before was a test that stopped running
    anywhere once its one guard failed.
    """
    reason = f"{requirement} unavailable in this environment (needs {capability})"
    if detail:
        reason = f"{reason}: {detail}"
    return f"{reason}; degraded contract covered by {substitute_test}"


def skip_unless(
    argv: list[str] | tuple[str, ...],
    *,
    requirement: str,
    capability: str,
    substitute_test: str,
    cwd: str | None = None,
    input: str | None = None,
    timeout: float = 10.0,
):
    """The ready-to-apply form of ``probe_invocation`` +
    ``missing_capability_reason``: a ``pytest.mark.skipif`` built from a
    live probe of ``argv``, not a version string.

    Skipping is never the whole answer -- see ``missing_capability_reason``.
    Every call site SHOULD have a companion test, named in
    ``substitute_test``, that covers the contract which still holds when
    this one skips; a guarded test with no substitute is the darwin-only
    trap this repository has hit before, just spelled with a different
    capability.

    Imports ``pytest`` lazily so this module stays importable by callers
    that have not taken a pytest dependency; only building the marker needs
    it.
    """
    import pytest

    result = probe_invocation(argv, cwd=cwd, input=input, timeout=timeout)
    reason = missing_capability_reason(
        requirement=requirement,
        capability=capability,
        substitute_test=substitute_test,
        detail=result.detail,
    )
    return pytest.mark.skipif(not result.available, reason=reason)
