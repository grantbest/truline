"""Compare a *deployed* app's reported revision against main -- the missing half of
``worker_revision.py``'s check, extended from "did the worker load current code" to "is the
image actually running derived from what a human reading main would expect."

2026-09 measured instance: mcp-hub's build failed on every commit from 09-03 to 09-12 while
platform-mcp-prod/mcp-hub reported 1/1 Running, zero restarts, Available throughout, serving an
image built from a tree missing two modules that landed on main nine days earlier. Nothing
compared what was DEPLOYED against what was on main. This module is that comparison, generalized
just enough to read one pod's ``/health`` and answer "is this current," "is this drifted," or "I
cannot tell" -- never the third read as the second.

Deliberately independent of ``worker_revision.py`` and ``dispatch.py`` at import time, even
though the comparison algorithm below is the same shape (ancestor check + restricted commit
count). ``worker_revision.py`` does ``from dispatch import DEFAULT_BASE_REF, GIT, run`` before it
defines its own ``REPO_ROOT``, and ``dispatch.py`` itself, partway through executing, imports
``activities.dispatch_steps`` -- which runs ``activities/__init__.py`` top to bottom. A *new* leaf
module that reaches into ``worker_revision`` for a symbol at import time and is itself the first
thing a fresh interpreter imports (exactly what a fresh-interpreter import test does) hits a
genuine circular import: ``worker_revision`` is stuck mid-import, ``dispatch`` stuck underneath
it, and by the time ``activities/__init__.py`` tries to import the activity built on top of this
module, this module is *also* still mid-import. See ``.factory/design.md`` for the full chain.
The fix is this module having zero first-party import dependencies: its own ``REPO_ROOT``
(``Path(__file__)``, not borrowed), its own minimal git runner matching ``dispatch.run``'s
signature and behavior, duplicated rather than shared.

Reads the pod's own ``/health`` via ``kubectl exec`` -- never the public
Cloudflare-Access-fronted hostname, which answers 401 to an
unauthenticated caller (the #783 gate, F1). Minting a Cloudflare Access service token is a new
client-identity scope grant reserved for the Operator, not something this check may create.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import process_env

#: Computed locally, not imported from worker_revision.REPO_ROOT -- see the module
#: docstring for why that import would be a circular one for the first module in the chain.
REPO_ROOT = Path(__file__).resolve().parents[2]

GIT = "git"

#: build-mcp-hub.yml only builds on pushes touching these paths -- so "N commits behind main"
#: is the ordinary, harmless state after any unrelated merge. Drift is restricted to commits
#: that would actually change what the next build produces. Not a hostname or household
#: identifier (F4 is about the health *target*, not this): a fixed CI-trigger pathspec.
TRIGGER_PATHSPEC: tuple[str, ...] = ("apps/mcp-hub", ".github/workflows/build-mcp-hub.yml")

DEFAULT_BASE_REF = "main"
#: The ref name every comparison below runs against -- always the just-fetched tracking ref,
#: never a local branch (dispatch.py:3811's own comparison target).
TRACKING_REF = "FETCH_HEAD"

KUBECTL_TIMEOUT_S = 30.0
#: Matches dispatch.py:3808's own bound for fetching before a merge-commit ancestor check.
FETCH_TIMEOUT_S = 300.0

#: The literal value every image built before the Dockerfile's ARG/ENV GIT_SHA is wired with a
#: real build-arg reports (apps/mcp-hub/Dockerfile, openapi_app.health). Never comparable to a
#: revision -- treated as cannot-determine, not as "behind" and not as "current."
UNKNOWN_GIT_SHA = "unknown"

GitRunner = Callable[..., Any]
KubectlHealthProbe = Callable[..., tuple[str | None, str]]


def _default_git_runner(
    cmd: list[str], cwd: Path | None = None, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Matches dispatch.run's exact signature and behavior, without importing it."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=process_env.child_env(),
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd[:3])}...` exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    return proc


#: The probe script run inside the pod via `kubectl exec`. Talks to the pod's own loopback --
#: never the cluster network, never the public, Cloudflare-fronted hostname.
_HEALTH_PROBE_PYTHON = (
    "import json,urllib.request;"
    "print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5))"
    "['git_sha'])"
)


def default_kubectl_health_probe(
    namespace: str, deployment: str, *, timeout: float = KUBECTL_TIMEOUT_S
) -> tuple[str | None, str]:
    """``(git_sha, "")`` on success, ``(None, reason)`` on any failure to read it.

    The reason always carries kubectl's own stderr (or the exception text) -- never
    collapsed to a bare ``None`` with no explanation (PRIN-008 / the #784 gate's F5).
    """
    try:
        proc = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                f"deploy/{deployment}",
                "--",
                "python",
                "-c",
                _HEALTH_PROBE_PYTHON,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=process_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"kubectl exec into {namespace}/{deployment} failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        return None, (
            f"kubectl exec into {namespace}/{deployment} exited {proc.returncode}: {detail}"
        )
    sha = proc.stdout.strip()
    if not sha:
        return None, f"kubectl exec into {namespace}/{deployment} returned no output"
    return sha, ""


@dataclass(frozen=True)
class DeployedRevisionStatus:
    """Whether a deployed workload's reported revision has drifted from main.

    ``cannot_determine_reason`` is the typed distinction the acceptance criteria require:
    ``None`` when a real comparison happened, ``"unreachable"`` when the check itself could
    not run (kubectl, fetch, or git compare failed), ``"sha_unknown"`` when the pod answered
    but reported the placeholder ``git_sha`` every pre-build-arg image carries. The two
    cannot-determine reasons are never folded into one: an unreachable check is this
    platform's normal "something is broken, alert" shape; a pod honestly reporting
    "unknown" is the expected, universal, temporary state until the build workflow is wired
    with a real build-arg, and alerting on it nightly would be the same "alarm on the
    system's own normal state" mistake this bead's drift predicate itself was corrected to
    avoid.
    """

    namespace: str
    deployment: str
    deployed_sha: str | None
    tracking_ref: str
    tracking_revision: str | None
    is_ancestor: bool | None
    pathspec_commits: int | None = None
    error: str = ""
    cannot_determine_reason: str | None = None

    @property
    def could_not_determine(self) -> bool:
        return self.cannot_determine_reason is not None

    @property
    def drifted(self) -> bool:
        if self.could_not_determine:
            return False
        if not self.is_ancestor:
            return True
        return bool(self.pathspec_commits)

    @classmethod
    def unreachable(
        cls, *, namespace: str, deployment: str, error: str, deployed_sha: str | None = None
    ) -> "DeployedRevisionStatus":
        return cls(
            namespace=namespace,
            deployment=deployment,
            deployed_sha=deployed_sha,
            tracking_ref=TRACKING_REF,
            tracking_revision=None,
            is_ancestor=None,
            error=error,
            cannot_determine_reason="unreachable",
        )

    @classmethod
    def sha_unknown(cls, *, namespace: str, deployment: str) -> "DeployedRevisionStatus":
        return cls(
            namespace=namespace,
            deployment=deployment,
            deployed_sha=UNKNOWN_GIT_SHA,
            tracking_ref=TRACKING_REF,
            tracking_revision=None,
            is_ancestor=None,
            error=(
                f"{namespace}/{deployment}'s /health reports git_sha "
                f"{UNKNOWN_GIT_SHA!r} -- the image predates a wired --build-arg GIT_SHA "
                "build; cannot determine its revision"
            ),
            cannot_determine_reason="sha_unknown",
        )


def describe_deployed_revision_drift(
    *,
    namespace: str,
    deployment: str,
    repo_root: Path = REPO_ROOT,
    remote: str,
    base_ref: str = DEFAULT_BASE_REF,
    trigger_pathspec: Sequence[str] = TRIGGER_PATHSPEC,
    kubectl_probe: KubectlHealthProbe = default_kubectl_health_probe,
    git_runner: GitRunner = _default_git_runner,
    kubectl_timeout: float = KUBECTL_TIMEOUT_S,
    fetch_timeout: float = FETCH_TIMEOUT_S,
) -> DeployedRevisionStatus:
    """Read ``namespace``/``deployment``'s deployed sha and compare it against ``remote``.

    ``remote`` is fetched (bounded, ``fetch_timeout``) and every comparison below runs
    against the resulting ``FETCH_HEAD`` -- never a local branch, which an operator advances
    by hand and which lags after every merge (dispatch.py's own ``_refresh_stale_tracking_ref``
    / ``merge_commit_is_ancestor_of_main`` shape). Drift is restricted to commits touching
    ``trigger_pathspec``: a bare commits-behind count is not drift when the build that would
    have picked up those commits never runs on them.
    """
    sha, probe_error = kubectl_probe(namespace, deployment, timeout=kubectl_timeout)
    if probe_error:
        return DeployedRevisionStatus.unreachable(
            namespace=namespace, deployment=deployment, error=probe_error
        )
    if sha == UNKNOWN_GIT_SHA:
        return DeployedRevisionStatus.sha_unknown(namespace=namespace, deployment=deployment)

    try:
        git_runner([GIT, "fetch", "--quiet", remote, base_ref], cwd=repo_root, timeout=fetch_timeout)
        ancestor_proc = git_runner(
            [GIT, "merge-base", "--is-ancestor", sha, TRACKING_REF],
            cwd=repo_root,
            check=False,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - status must say unreachable, not "current".
        return DeployedRevisionStatus.unreachable(
            namespace=namespace, deployment=deployment, error=str(exc), deployed_sha=sha
        )

    if ancestor_proc.returncode not in (0, 1):
        detail = (getattr(ancestor_proc, "stderr", "") or getattr(ancestor_proc, "stdout", "") or "").strip()[:500]
        return DeployedRevisionStatus.unreachable(
            namespace=namespace,
            deployment=deployment,
            error=f"could not test whether {sha} is an ancestor of {TRACKING_REF}: {detail}",
            deployed_sha=sha,
        )

    is_ancestor = ancestor_proc.returncode == 0
    try:
        tracking_revision = git_runner(
            [GIT, "rev-parse", TRACKING_REF], cwd=repo_root, timeout=30
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - status must say unreachable, not "current".
        return DeployedRevisionStatus.unreachable(
            namespace=namespace, deployment=deployment, error=str(exc), deployed_sha=sha
        )

    pathspec_commits: int | None = None
    if is_ancestor and sha != tracking_revision:
        count_proc = git_runner(
            [GIT, "rev-list", "--count", f"{sha}..{TRACKING_REF}", "--", *trigger_pathspec],
            cwd=repo_root,
            check=False,
            timeout=30,
        )
        if count_proc.returncode != 0:
            detail = (
                getattr(count_proc, "stderr", "") or getattr(count_proc, "stdout", "") or ""
            ).strip()[:500]
            return DeployedRevisionStatus.unreachable(
                namespace=namespace,
                deployment=deployment,
                error=f"could not count commits between {sha} and {TRACKING_REF}: {detail}",
                deployed_sha=sha,
            )
        try:
            pathspec_commits = int(count_proc.stdout.strip())
        except ValueError:
            return DeployedRevisionStatus.unreachable(
                namespace=namespace,
                deployment=deployment,
                error=f"unexpected `git rev-list --count` output: {count_proc.stdout!r}",
                deployed_sha=sha,
            )
    elif is_ancestor:
        pathspec_commits = 0

    return DeployedRevisionStatus(
        namespace=namespace,
        deployment=deployment,
        deployed_sha=sha,
        tracking_ref=TRACKING_REF,
        tracking_revision=tracking_revision,
        is_ancestor=is_ancestor,
        pathspec_commits=pathspec_commits,
    )
