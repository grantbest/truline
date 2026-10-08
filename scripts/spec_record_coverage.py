"""Spec-record tracking coverage checker.

`apps/factory-dispatcher/tasks/` is the committed record of what the factory
was told to build. A spec file that exists only on one operator's disk is
invisible to the repository, and when the `dev.task` bead it drove has
already reached a TERMINAL state (``done``/``superseded``), the acceptance
criteria that shipped work was built against become readable only by
querying production -- the second-source-of-truth failure this check exists
to catch, in the direction where the FILE is the one that is missing.

The bead side of the comparison comes from a committed snapshot file ONLY
(CLAUDE.md decision record D7): this module holds no substrate client, no
HTTP call, and no credential lookup, so it can never read the live store even
by accident. ``scripts/tests/test_spec_record_coverage.py`` asserts this
statically (no import here names anything substrate-shaped) rather than
trusting a docstring to stay true.

Because the bead side is a file and not a live query, this module also
refuses to agree with a snapshot it cannot trust: ``load_snapshot`` requires
an explicit ``"snapshot_kind": "measured"`` declaration and a ``"dated"``
field, and raises ``UntrustedSnapshotError`` -- reported by the CLI as exit
code 2, distinct from OK (0) and from a terminal-untracked FAIL (1) -- for
anything else, including a snapshot that never says what it is.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASKS_DIR = Path("apps/factory-dispatcher/tasks")

#: Literal per this check's own acceptance criterion -- not derived from
#: dev_task_contract.DEV_TASK_STATE_MACHINE's zero-outgoing states, which
#: also includes "archived" (reached only from "done"). Widening this to
#: match that set is a decision for whoever owns AC-1 next, not this check.
TERMINAL_STATES = frozenset({"done", "superseded"})

#: Directory names under ``tasks_dir`` that hold JSON that is not a dev.task
#: spec record -- e.g. apps/factory-dispatcher/tasks/snapshots/*.json, which
#: has no ``title`` and would otherwise be miscounted as "unqueued work" the
#: moment such a file is untracked (it happens to be tracked today).
NON_SPEC_SUBDIRS = frozenset({"snapshots"})

#: The only value of a snapshot's ``snapshot_kind`` field this check trusts
#: as a real measurement of bead state. Anything else -- including the
#: field's absence -- is refused (see ``load_snapshot``).
TRUSTED_SNAPSHOT_KIND = "measured"

TRACKED = "tracked"
UNTRACKED_NO_BEAD = "untracked-no-bead"
UNTRACKED_OPEN_BEAD = "untracked-open-bead"
UNTRACKED_TERMINAL_BEAD = "untracked-terminal-bead"


class UntrustedSnapshotError(Exception):
    """The snapshot cannot be trusted as a real measurement of bead state.

    Raised instead of returning a result so the caller cannot mistake
    "I could not tell" for "OK" (AC-1) -- PRIN-015: an ambiguous case fails
    closed.
    """


@dataclasses.dataclass(frozen=True)
class SpecStatus:
    path: str
    title: str
    tracked: bool
    bead_states: tuple[str, ...]
    category: str


@dataclasses.dataclass(frozen=True)
class SnapshotData:
    by_title: dict[str, list[str]]
    dated: str


@dataclasses.dataclass(frozen=True)
class CheckResult:
    statuses: tuple[SpecStatus, ...]
    snapshot_dated: str

    def _by_category(self, category: str) -> tuple[SpecStatus, ...]:
        return tuple(s for s in self.statuses if s.category == category)

    @property
    def tracked(self) -> tuple[SpecStatus, ...]:
        return self._by_category(TRACKED)

    @property
    def untracked_no_bead(self) -> tuple[SpecStatus, ...]:
        return self._by_category(UNTRACKED_NO_BEAD)

    @property
    def untracked_open_bead(self) -> tuple[SpecStatus, ...]:
        return self._by_category(UNTRACKED_OPEN_BEAD)

    @property
    def untracked_terminal_bead(self) -> tuple[SpecStatus, ...]:
        return self._by_category(UNTRACKED_TERMINAL_BEAD)

    @property
    def ok(self) -> bool:
        return not self.untracked_terminal_bead


def load_snapshot(path: Path) -> SnapshotData:
    """Read and validate a committed dev.task titles-and-states snapshot.

    This is the ONLY place bead state enters this module, and it is a plain
    file read -- no store client, no network.

    A snapshot earns trust by declaring itself, not by its filename -- a
    placeholder file can be copied to whatever name a real snapshot would
    use, so filename matching would not catch it. It SHALL carry
    ``"snapshot_kind": "measured"``; anything else, including the field's
    absence, is refused (AC-1). It SHALL also carry a ``"dated"`` field
    (AC-3) so a reader can judge how current the measurement is -- staleness
    itself is not enforced here, only reported.
    """
    data = json.loads(Path(path).read_text())

    kind = data.get("snapshot_kind")
    if kind != TRUSTED_SNAPSHOT_KIND:
        got = "no 'snapshot_kind' field" if kind is None else f"snapshot_kind={kind!r}"
        raise UntrustedSnapshotError(
            f"{path}: snapshot is not usable ({got}, expected "
            f"{TRUSTED_SNAPSHOT_KIND!r}) -- refusing to treat unmeasured or "
            "placeholder data as bead state."
        )

    dated = data.get("dated")
    if not dated:
        raise UntrustedSnapshotError(
            f"{path}: snapshot is not usable (no 'dated' field) -- a "
            "snapshot is a dated measurement and this one does not say "
            "when it was taken."
        )

    by_title: dict[str, list[str]] = {}
    for task in data["tasks"]:
        by_title.setdefault(str(task["title"]), []).append(str(task["state"]))
    return SnapshotData(by_title=by_title, dated=str(dated))


def tracked_paths(repo: Path, tasks_dir: Path) -> set[str]:
    """Repo-relative posix paths git considers tracked under ``tasks_dir``."""
    result = subprocess.run(
        ["git", "ls-files", "--", tasks_dir.as_posix()],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def all_spec_files(repo: Path, tasks_dir: Path) -> list[Path]:
    """Spec record files under ``tasks_dir`` -- not every JSON file there.

    F2: a plain ``rglob("*.json")`` also picks up non-spec JSON such as
    ``tasks/snapshots/*.json`` (dated measurement files, not dev.task specs
    -- no ``title`` field, so they resolve to ``title=""`` and would be
    miscounted as untitled "unqueued work" the moment one is untracked).
    Excluding known non-spec subdirectories by name, rather than requiring a
    ``title`` field, keeps a spec that is missing its title (a real defect)
    visible instead of silently dropping it.
    """
    files = []
    base = repo / tasks_dir
    for path in sorted(base.rglob("*.json")):
        rel_dirs = path.relative_to(base).parts[:-1]
        if any(part in NON_SPEC_SUBDIRS for part in rel_dirs):
            continue
        files.append(path)
    return files


def classify(tracked: bool, bead_states: list[str]) -> str:
    if tracked:
        return TRACKED
    if not bead_states:
        return UNTRACKED_NO_BEAD
    if all(state in TERMINAL_STATES for state in bead_states):
        return UNTRACKED_TERMINAL_BEAD
    return UNTRACKED_OPEN_BEAD


def check(
    repo: Path,
    tasks_dir: Path,
    snapshot_path: Path,
    tracked_fn: Callable[[Path, Path], set[str]] = tracked_paths,
) -> CheckResult:
    snapshot = load_snapshot(snapshot_path)
    tracked_set = tracked_fn(repo, tasks_dir)

    statuses: list[SpecStatus] = []
    for spec_path in all_spec_files(repo, tasks_dir):
        rel = spec_path.relative_to(repo).as_posix()
        spec = json.loads(spec_path.read_text())
        title = str(spec.get("title") or "")
        is_tracked = rel in tracked_set
        bead_states = snapshot.by_title.get(title, [])
        statuses.append(
            SpecStatus(
                path=rel,
                title=title,
                tracked=is_tracked,
                bead_states=tuple(bead_states),
                category=classify(is_tracked, bead_states),
            )
        )
    return CheckResult(tuple(statuses), snapshot_dated=snapshot.dated)


def render_report(result: CheckResult) -> str:
    lines = [f"spec record coverage: {len(result.statuses)} spec file(s) checked"]
    lines.append(f"  snapshot dated: {result.snapshot_dated}")
    lines.append(f"  tracked: {len(result.tracked)}")

    lines.append(
        f"  untracked, no bead at all (unqueued work): {len(result.untracked_no_bead)}"
    )
    for status in result.untracked_no_bead:
        lines.append(f"    - {status.path}  title={status.title!r}")

    lines.append(
        "  untracked, open bead (queued, bead not yet terminal): "
        f"{len(result.untracked_open_bead)}"
    )
    for status in result.untracked_open_bead:
        states = ",".join(status.bead_states)
        lines.append(f"    - {status.path}  bead_state(s)={states}")

    lines.append(
        "  untracked, TERMINAL bead (shipped work with no spec in the repo): "
        f"{len(result.untracked_terminal_bead)}"
    )
    for status in result.untracked_terminal_bead:
        states = ",".join(status.bead_states)
        lines.append(f"    - {status.path}  bead_state(s)={states}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--tasks-dir", type=Path, default=TASKS_DIR)
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="committed dev.task titles-and-states snapshot (the only bead-side source)",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    try:
        result = check(repo, args.tasks_dir, args.snapshot)
    except UntrustedSnapshotError as exc:
        print(f"REFUSED: {exc}")
        return 2

    print(render_report(result))

    if not result.ok:
        print(
            f"FAIL: {len(result.untracked_terminal_bead)} spec file(s) untracked "
            "with a terminal (done/superseded) bead"
        )
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
