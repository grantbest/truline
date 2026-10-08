#!/usr/bin/env python3
"""Advance the factory worker's own controlled checkout to a new revision.

2026-08-25: the launchd worker runs `cd <operator's shared working tree>;
exec python worker.py`. Whatever branch is checked out there when the
process starts IS the factory's code -- a PR branch mid-review, a half-done
rebase, a conflicted tree. `worker_revision.ensure_checkout_is_on_main_ancestor`
now refuses to let the worker start unless its loaded checkout is an ancestor
of main; this module is the other half -- the one deliberate, documented way
an operator moves what that checkout contains, so "what code is the factory
running" has an answer that does not depend on where anyone's HEAD happens to
point.

Design: `advance()` refuses -- before writing anything to `checkout_root` --
when the target revision is not an ancestor of `main_ref` as resolved in
`source_repo_root`. It then clones or fetches strictly the `main_ref` branch
by name (never whatever branch happens to be checked out in the source
working tree, which is exactly the bug this exists to fix) and detaches HEAD
at the target revision. A marker file records that this command, and nothing
else, produced the checkout, so `launchd_agent.py` can tell a factory-managed
checkout from a directory that merely looks like one.

Everything here is local-only: cloning and fetching happen from a filesystem
path, never a remote URL, so advancing the checkout requires no network.

2026-09-02: `advance()` also refuses when `source_repo_root` resolves to
`checkout_root` itself -- the self-referential invocation an operator hit by
running this command from inside the worker checkout, where
`--source-repo-root`'s `Path(__file__)`-derived default is the checkout being
advanced. That invocation used to fetch main from itself and report success
having changed nothing. `worker_checkout_drift_response.py` is the other new
half: it turns a *detected* drift (`worker_revision.describe_worker_revision_drift`)
into an actual `advance()` call plus a worker restart, or a declared halt when
advancing is not safe, instead of drift being detected on a schedule and acted
on by nobody.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from dispatch import DEFAULT_BASE_REF, GIT, run

#: Marks a checkout as one `advance()` itself produced. An operator's own
#: clone left at the same path must not be silently adopted as the worker's.
MARKER_NAME = ".factory-dispatcher-worker-checkout"

#: Relocates *where the controlled checkout lives*, never what it contains.
CHECKOUT_DIR_ENV = "FACTORY_DISPATCHER_WORKER_CHECKOUT"

#: Same trunk the rest of the dispatcher treats as canonical.
DEFAULT_MAIN_REF = DEFAULT_BASE_REF

#: The remote name a plain `git clone <github-url>` sets up -- the same name the
#: 2026-09-04 incident's manual fix (`git fetch origin main:main`, run by hand in the
#: mirror) already used. `sync_source_mirror_main` matches that command exactly rather
#: than resolving a configured upstream, since the mirror's `main` is not necessarily
#: ever checked out (an upstream is only configured for a branch you have checked out
#: at some point) -- the one thing this needs is the remote the mirror was cloned from.
DEFAULT_MIRROR_REMOTE = "origin"

GitRunner = Callable[..., Any]


class WorkerCheckoutError(RuntimeError):
    """Refused to advance the worker checkout to an unsafe revision."""


class SourceMirrorLocalCommitsError(RuntimeError):
    """The source mirror's main carries commits its canonical remote does not have.

    2026-09-04: a person committing straight to `main` in the mirror's interactive
    working copy is the one case `sync_source_mirror_main` refuses outright -- a
    fast-forward-only fetch would otherwise either silently discard that history or
    (if forced) overwrite it, and the mirror's history is not this module's to lose.
    This genuinely needs a person; the caller
    (`dispatch.ensure_base_ref_current`/`_sync_source_mirror_main`) raises the same
    declared base-ref alert OPS-56 already raises for the inner hop's equivalent case,
    and, since dev.finding ac4e8569 (2026-09-24), also refuses the drain itself
    (`dispatch.SourceMirrorBaseRefWrongError`) rather than announcing and proceeding --
    this is the one outcome of this function GitHub was actually reached to confirm,
    as opposed to an unreachable remote or a checked-out `main`, so it is the one
    outcome #649's "must never fail the drain" no longer covers.
    """

    def __init__(
        self,
        main_ref: str,
        local_rev: str,
        remote_ref: str,
        remote_rev: str,
        local_only: int,
    ):
        self.main_ref = main_ref
        self.local_rev = local_rev
        self.remote_ref = remote_ref
        self.remote_rev = remote_rev
        self.local_only = local_only
        super().__init__(
            f"refusing to update the source mirror's {main_ref} ({local_rev}) from "
            f"{remote_ref} ({remote_rev}): {local_only} local-only commit(s) not on "
            f"{remote_ref}; a fast-forward would lose them."
        )


@dataclass(frozen=True)
class SourceMirrorSyncResult:
    """What `sync_source_mirror_main` did. Never raised for a degraded outcome --
    only `SourceMirrorLocalCommitsError` (a distinct, always-raised case) is."""

    updated: bool
    revision: str | None
    #: Set when the fetch could not be trusted (remote unreachable, main checked out
    #: in the mirror, ...) -- `revision` is then the mirror's last-known `main`, not a
    #: freshly confirmed one. Never set together with `updated=True`.
    error: str | None = None


def default_checkout_root(environ: Mapping[str, str] | None = None) -> Path:
    """Where the factory's controlled checkout lives, in this environment."""
    values = os.environ if environ is None else environ
    base = values.get(CHECKOUT_DIR_ENV)
    if base:
        return Path(base)
    return Path.home() / ".factory-dispatcher" / "worker-checkout"


def is_factory_controlled_checkout(checkout_root: Path) -> bool:
    """True only if `advance()` itself produced this checkout."""
    return (checkout_root / MARKER_NAME).exists()


def declared_source_repo_root(checkout_root: Path, *, git_runner: GitRunner = run) -> Path:
    """The source `advance()` last cloned this checkout from, read back from `origin`.

    `advance()` takes `source_repo_root` as an explicit argument every call and never
    stores it anywhere else -- a fresh clone's `origin` remote is set to it by `git clone`
    (the standard-library behavior, not something this module writes) and is otherwise
    unused by `advance()`: every later fetch names `source_repo_root` again, explicitly,
    never `origin`. Reading it back here gives a caller that does not already know the
    declared source -- notably code running from inside `checkout_root` itself, where
    `Path(__file__)`-style derivation is exactly the self-referential invocation `advance()`
    now refuses -- a way to find it without guessing or introducing a second, parallel
    configuration surface for the same value.
    """
    url = git_runner([GIT, "remote", "get-url", "origin"], cwd=checkout_root).stdout.strip()
    return Path(url)


def sync_source_mirror_main(
    *,
    source_repo_root: Path,
    remote_name: str = DEFAULT_MIRROR_REMOTE,
    main_ref: str = DEFAULT_MAIN_REF,
    git_runner: GitRunner = run,
) -> SourceMirrorSyncResult:
    """Ref-only-fetch `source_repo_root`'s `main_ref` from its own canonical remote.

    `advance()` (above) automates worker-checkout <- source mirror, entirely
    local-only by design. This is the other hop -- source mirror <- canonical remote
    -- and it is not local-only: `source_repo_root` here is an operator's interactive
    working copy, not something this module produced, so this function is far more
    restrained than `advance()` is willing to be with a checkout it owns outright.

    Exactly two things ever touch `source_repo_root`: a plain `git fetch <remote_name>
    <main_ref>` (lands in `FETCH_HEAD` only -- no destination refspec, so nothing local
    moves yet) and, only once that fetch is compared against the current local
    `main_ref` and found to be a genuine fast-forward, `git branch -f main_ref
    <fetched-tip>` -- the same "safe because nothing checked out reads this ref"
    reasoning `dispatch._fast_forward_base_ref` already relies on one hop down. Never
    `checkout`, `merge`, or `reset`: this must never touch the working tree, and must
    never move whatever branch the operator currently has checked out, even if that
    happens to be `main_ref` itself (handled below by leaving it alone rather than
    attempting a `branch -f` git would refuse anyway).

    Raises `SourceMirrorLocalCommitsError` when `main_ref` carries commits the remote
    does not have -- the one outcome that must not be silently absorbed. Every other
    failure (remote unreachable, no such remote configured, `main_ref` not yet fetched
    to compare) is reported on the returned result (`error` set, `updated=False`)
    rather than raised: `main_ref` is left exactly as it was, for the caller to compare
    against as the last-known state.
    """
    local_proc = git_runner([GIT, "rev-parse", main_ref], cwd=source_repo_root, check=False)
    local_rev = local_proc.stdout.strip() if local_proc.returncode == 0 else None

    fetch = git_runner(
        [GIT, "fetch", remote_name, main_ref], cwd=source_repo_root, check=False
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout or "").strip()[:300]
        return SourceMirrorSyncResult(
            updated=False,
            revision=local_rev,
            error=(
                f"could not fetch {main_ref} from {remote_name!r} in "
                f"{source_repo_root}: {detail}"
            ),
        )

    remote_rev = git_runner([GIT, "rev-parse", "FETCH_HEAD"], cwd=source_repo_root).stdout.strip()
    remote_ref = f"{remote_name}/{main_ref}"

    if local_rev is None:
        # No local main_ref yet in the mirror -- nothing local to lose or compare.
        git_runner([GIT, "branch", "-f", main_ref, remote_rev], cwd=source_repo_root)
        return SourceMirrorSyncResult(updated=True, revision=remote_rev)

    if local_rev == remote_rev:
        return SourceMirrorSyncResult(updated=False, revision=local_rev)

    counts = git_runner(
        [GIT, "rev-list", "--left-right", "--count", f"{local_rev}...{remote_rev}"],
        cwd=source_repo_root,
    ).stdout.split()
    local_only = int(counts[0]) if len(counts) >= 1 else 0
    if local_only:
        raise SourceMirrorLocalCommitsError(main_ref, local_rev, remote_ref, remote_rev, local_only)

    current_branch = git_runner(
        [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=source_repo_root
    ).stdout.strip()
    if current_branch == main_ref:
        # main_ref is the operator's checked-out branch right now -- moving it is
        # exactly the working-tree touch this function must never make (`branch -f`
        # would refuse this too, but this is checked explicitly rather than relied on,
        # since relying on git's refusal here would still leave the intent undocumented).
        return SourceMirrorSyncResult(
            updated=False,
            revision=local_rev,
            error=f"{main_ref} is checked out in {source_repo_root}; left untouched",
        )

    git_runner([GIT, "branch", "-f", main_ref, remote_rev], cwd=source_repo_root)
    return SourceMirrorSyncResult(updated=True, revision=remote_rev)


def advance(
    revision: str | None = None,
    *,
    checkout_root: Path,
    source_repo_root: Path,
    main_ref: str = DEFAULT_MAIN_REF,
    git_runner: GitRunner = run,
) -> str:
    """Advance the factory's controlled checkout to `revision` (default: main's tip).

    Refuses before touching `checkout_root` when `revision` is not an
    ancestor of `main_ref` as resolved in `source_repo_root` -- the
    acceptance criterion this exists to satisfy. Returns the revision
    actually checked out.

    Also refuses -- before that ancestor check, before any git call at all -- when
    `source_repo_root` resolves to `checkout_root` itself. 2026-09-02: an operator ran this
    command from inside the worker checkout, so `--source-repo-root`'s `Path(__file__)`-derived
    default resolved to the checkout being advanced; the command fetched `main` from itself and
    reported success having changed nothing. `Path.resolve()` normalizes both sides without
    requiring either to exist yet, so this also catches the self-referential case on a checkout's
    very first (not-yet-cloned) advance.
    """
    if checkout_root.resolve() == source_repo_root.resolve():
        raise WorkerCheckoutError(
            f"refusing to advance {checkout_root}: --source-repo-root resolved to the same "
            "path as the checkout being advanced, so this would fetch main from itself and "
            "report success having changed nothing (2026-09-02: this is the self-referential "
            "invocation that happens when this command is run from inside the worker checkout "
            "itself). Pass --source-repo-root pointing at a separate source checkout, or run "
            "this command from there instead."
        )

    main_revision = git_runner(
        [GIT, "rev-parse", main_ref], cwd=source_repo_root
    ).stdout.strip()
    target = revision if revision is not None else main_revision

    ancestor = git_runner(
        [GIT, "merge-base", "--is-ancestor", target, main_ref],
        cwd=source_repo_root,
        check=False,
    )
    if ancestor.returncode != 0:
        raise WorkerCheckoutError(
            f"refusing to advance the worker checkout to {target}: it is not an "
            f"ancestor of {main_ref} ({main_revision}) in {source_repo_root}. Merge "
            f"it to {main_ref} first, or name a revision that is already on it."
        )

    if not (checkout_root / ".git").exists():
        checkout_root.parent.mkdir(parents=True, exist_ok=True)
        # `--branch main_ref` names the branch explicitly rather than
        # following whatever `source_repo_root`'s working tree currently has
        # checked out -- an ordinary local `git clone` otherwise follows the
        # source's HEAD, which is precisely the bug this module exists to fix.
        git_runner(
            [
                GIT,
                "clone",
                "--no-hardlinks",
                "--branch",
                main_ref,
                "--single-branch",
                str(source_repo_root),
                str(checkout_root),
            ]
        )
    else:
        git_runner([GIT, "fetch", str(source_repo_root), main_ref], cwd=checkout_root)

    # Detach before force-updating main_ref: git refuses `branch -f` against
    # whichever branch is currently checked out, which main_ref always is
    # immediately after a fresh clone.
    git_runner([GIT, "checkout", "--detach", target], cwd=checkout_root)
    git_runner([GIT, "branch", "-f", main_ref, main_revision], cwd=checkout_root)
    (checkout_root / MARKER_NAME).write_text(f"{target}\n", encoding="utf-8")
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Advance the factory worker's controlled checkout -- the one "
            "documented command for changing what code the unattended worker "
            "runs."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    advance_parser = subparsers.add_parser(
        "advance", help="Advance the controlled checkout to a revision on main."
    )
    advance_parser.add_argument(
        "revision",
        nargs="?",
        default=None,
        help=f"Revision to advance to; defaults to the tip of {DEFAULT_MAIN_REF}.",
    )
    advance_parser.add_argument(
        "--source-repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository the checkout is advanced from (default: this repo).",
    )
    advance_parser.add_argument(
        "--checkout-root",
        type=Path,
        default=None,
        help=(
            "Where the controlled checkout lives; defaults to "
            f"${CHECKOUT_DIR_ENV} or ~/.factory-dispatcher/worker-checkout."
        ),
    )
    advance_parser.add_argument(
        "--main-ref",
        default=DEFAULT_MAIN_REF,
        help=f"Ref advance refuses to move past (default: {DEFAULT_MAIN_REF}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checkout_root = (
        args.checkout_root if args.checkout_root is not None else default_checkout_root()
    ).expanduser()
    try:
        target = advance(
            args.revision,
            checkout_root=checkout_root,
            source_repo_root=args.source_repo_root.resolve(),
            main_ref=args.main_ref,
        )
    except WorkerCheckoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Advanced worker checkout at {checkout_root} to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
