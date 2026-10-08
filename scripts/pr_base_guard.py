"""Guard: a merged PR must have a path to trunk.

On 2026-08-02, seven PRs (#251, #253-#258) all reported MERGED and only one of
them reached ``main``. #251 merged ``lifeops/gemini-reviewer-orientation`` into
``main`` at 22:07:45Z. Thirty-two seconds later #253 merged into
``lifeops/gemini-reviewer-orientation`` — a branch that had just been consumed
and would never be merged again. Every PR stacked above it inherited the same
dead end, and ``main`` sat at #250 while the console, the operating model, the
role split and the bead schema accumulated on branches nobody was watching.

Nothing failed. GitHub reports "Merged" for a merge into any base, and the
factory's own signal — PR checks green, PR state MERGED — was green throughout.
That is the failure mode this guard exists for: not a broken build, but a
correct-looking merge into a branch with no route to trunk.

The invariant: **a PR whose base is not ``main`` is only safe while its base
branch still has an open PR of its own.** Stacked PRs are legitimate; stacking
onto something already merged or closed is not, because the stack is orphaned
the moment the base lands.

Pure logic lives in :func:`evaluate` so the meta-tests can exercise every verdict
without a network. See ``scripts/tests/test_pr_base_guard.py`` — a gate nobody
tests is indistinguishable from a gate that passes everything.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

TRUNK = "main"

API_ROOT = "https://api.github.com"

# GitHub rejects requests without a User-Agent. The Discord webhook incident
# (2026-07) was the same class of bug in a different service: a stdlib urllib
# POST with no UA, 403'd by the edge while a browser GET to the same host
# returned 200. Set it explicitly rather than relying on a default.
USER_AGENT = "factory-dispatcher-pr-base-guard"


class Result(NamedTuple):
    ok: bool
    message: str


def evaluate(
    base_ref: str,
    base_pr_state: Optional[str],
    base_pr_number: Optional[int] = None,
) -> Result:
    """Decide whether a PR based on ``base_ref`` can still reach trunk.

    ``base_pr_state`` is the state of the pull request whose *head* is
    ``base_ref`` — ``"OPEN"``, ``"MERGED"``, ``"CLOSED"``, or ``None`` when no
    such PR exists. Case-insensitive; GitHub's REST and GraphQL APIs disagree on
    casing and the caller should not have to care.
    """
    if base_ref == TRUNK:
        return Result(True, f"base is {TRUNK} — merging this advances trunk.")

    state = (base_pr_state or "").upper()
    ref = f"#{base_pr_number}" if base_pr_number else ""

    if state == "OPEN":
        return Result(
            True,
            f"base '{base_ref}' is a stacked branch whose own PR {ref or '(unnumbered)'} "
            f"is still open. Merge order matters: land {ref or 'the base PR'} into "
            f"{TRUNK} LAST, after this one, or this stack orphans itself.",
        )

    if state == "MERGED":
        return Result(
            False,
            f"base '{base_ref}' has ALREADY been merged (PR {ref or 'unknown'}). Merging "
            f"this PR writes into a branch with no remaining path to {TRUNK}: it will "
            f"report 'Merged' and change nothing on trunk. Retarget this PR at {TRUNK}.",
        )

    if state == "CLOSED":
        return Result(
            False,
            f"base '{base_ref}' belongs to a CLOSED, unmerged PR {ref or 'unknown'}. "
            f"Anything merged here is stranded. Retarget this PR at {TRUNK}.",
        )

    return Result(
        False,
        f"base '{base_ref}' is not {TRUNK} and has no pull request of its own, so "
        f"nothing will ever carry it to trunk. Open a PR for '{base_ref}' first, or "
        f"retarget this PR at {TRUNK}.",
    )


def _api(path: str, token: str) -> list:
    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def lookup_base_pr(repo: str, base_ref: str, token: str):
    """Return ``(state, number)`` for the PR whose head is ``base_ref``.

    Picks the most recently created match. ``(None, None)`` when there is none.
    """
    owner = repo.split("/")[0]
    prs = _api(
        f"/repos/{repo}/pulls?head={owner}:{base_ref}&state=all&per_page=100",
        token,
    )
    if not prs:
        return None, None
    newest = max(prs, key=lambda p: p.get("created_at") or "")
    if newest.get("merged_at"):
        return "MERGED", newest.get("number")
    return (newest.get("state") or "").upper(), newest.get("number")


def main() -> int:
    base_ref = os.environ.get("PR_BASE_REF", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")

    if not base_ref:
        print("PR_BASE_REF is empty — cannot evaluate.")
        return 1

    if base_ref == TRUNK:
        result = evaluate(base_ref, None)
    else:
        if not (repo and token):
            print("GITHUB_REPOSITORY and GITHUB_TOKEN are required for a non-main base.")
            return 1
        try:
            state, number = lookup_base_pr(repo, base_ref, token)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            # Fail closed. A guard that passes when it cannot see is the
            # liveness-signal mistake the runner canary was built to avoid.
            print(f"Could not query the base branch's PR state: {exc}")
            return 1
        result = evaluate(base_ref, state, number)

    print(("PASS: " if result.ok else "FAIL: ") + result.message)
    return 0 if result.ok else 1
