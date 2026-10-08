"""The boundary between a worker-produced patch and the dispatcher's own
control clone, W/repo (dev.finding 79db3113, design §5).

In split mode the only thing that reaches W/repo from the worker is the
patch bytes: every git invocation that touches W/repo (make_clone,
apply_preserved_baseline, guard_tree, apply, write-tree, the contain/scope
diffs, save_failure_patch, open_pull_request) runs as the dispatcher, on
W/repo only. guard_tree() and apply() are this module's two checkpoints.

guard_tree() refuses a tree that carries anything other than a plain file
(mode 100644 or 100755) or that has a path component which, case-folded,
equals '.git' or '.factory' -- both `git ls-files -s` and `git ls-tree -r
HEAD` run with `-z` and are split on NUL, never on newline, because without
`-z` git quotes a whole path the moment any one byte in it needs quoting
and a stray leading/trailing quote character can shift a '.git'/'.factory'
component out of an exact case-folded match. apply() runs `git apply
--numstat -z --summary --binary` on an in-memory patch first and refuses
before any write when any numstat record's own path, or a rename/copy
source or destination, or a created/mode-changed entry's path, fails the
component rule or carries a disallowed mode -- `--numstat` alone does not
refuse a `.git` path the way `git apply --index` itself does (git's own
verify_path), so this module's own component rule is what refuses `.factory`
and the rename/copy source side verify_path does not cover. Only after that
check, the 16 MiB bound, and the real `git apply --index --binary` does
guard_tree() run a second time, to catch what the pre-check structurally
cannot see -- a rename or copy of an already-present symlink or gitlink
carries no mode line in --summary at all, since its mode never "changed".
A refused patch is saved to dispatch.FAILURE_PATCH_DIR as data, never
applied.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import dispatch
import process_env

_VALID_MODES = frozenset({"100644", "100755"})
_REFUSED_COMPONENTS = frozenset({".git", ".factory"})

#: dev.finding 79db3113's own bound: large enough for any real worker diff,
#: small enough that a refused oversized patch is cheap to reject before any
#: git call.
MAX_PATCH_BYTES = 16 * 1024 * 1024

_CREATE_RE = re.compile(r"^create mode (?P<mode>\d{6}) (?P<path>.+)$")
_DELETE_RE = re.compile(r"^delete mode (?P<mode>\d{6}) (?P<path>.+)$")
_MODE_CHANGE_RE = re.compile(r"^mode change (?P<old>\d{6}) => (?P<new>\d{6})(?: (?P<path>.+))?$")
_RENAME_RE = re.compile(r"^rename (?P<spec>.+) \(\d+%\)$")
_COPY_RE = re.compile(r"^copy (?P<spec>.+) \(\d+%\)$")


class HandoffRefused(Exception):
    """Raised by guard_tree() or apply() naming the offending path and rule.

    ``saved_patch_path`` is set by ``apply()`` (never by ``guard_tree()``,
    which has no patch to save) before this is raised, naming where the
    refused patch bytes were preserved as data.
    """

    def __init__(self, message: str, *, saved_patch_path: Path | None = None) -> None:
        super().__init__(message)
        self.saved_patch_path = saved_patch_path


class HandoffResult(NamedTuple):
    """apply()'s only return value: the patch applied and the index updated."""

    repo: Path


def _git_argv(args: list[str]) -> list[str]:
    return [dispatch.GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]


def _run_git(
    repo: Path, args: list[str], *, input: bytes | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        _git_argv(args),
        cwd=repo,
        input=input,
        capture_output=True,
        env=process_env.child_env(),
    )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _has_refused_component(path: str) -> bool:
    return any(part.casefold() in _REFUSED_COMPONENTS for part in PurePosixPath(path).parts)


def _check_component(repo: Path, path: str, rule: str) -> None:
    if _has_refused_component(path):
        raise HandoffRefused(
            f"{path!r} in {repo}: {rule} has a path component that case-folds "
            "to '.git' or '.factory'"
        )


def guard_tree(repo: Path) -> None:
    """Refuse ``repo`` unless every tracked entry (index and HEAD's tree) is
    a plain file (100644/100755) with no path component that case-folds to
    '.git' or '.factory'.

    Both listings run with ``-z`` and are split on NUL, never on newline:
    without ``-z``, git quotes a whole path the moment any one byte in it
    needs quoting (a non-ASCII byte, a control character, a double quote or
    a backslash -- core.quotePath), wrapping the entire path in a pair of
    double-quote characters. The stray quote then becomes part of whichever
    component sits first or last, so a path whose first component is
    literally ``.GIT`` followed by a non-ASCII byte is rendered as
    ``".GIT/\\303\\251"`` and its first component, read naively, is
    ``".GIT`` -- which case-folds to ``".git``, not ``.git``, and the
    refused-component check silently passes it. ``-z`` disables this
    quoting entirely, so every record below carries the real bytes.
    """
    for args, label in (
        (["ls-files", "-s", "-z"], "git ls-files -s"),
        (["ls-tree", "-r", "-z", "HEAD"], "git ls-tree -r HEAD"),
    ):
        proc = _run_git(repo, args)
        if proc.returncode != 0:
            raise HandoffRefused(
                f"{label} failed in {repo}: {_decode(proc.stderr or proc.stdout).strip()}"
            )
        for record in proc.stdout.split(b"\0"):
            if not record:
                continue
            raw_line = _decode(record)
            fields, _tab, path = raw_line.partition("\t")
            mode = fields.split(" ", 1)[0]
            if mode not in _VALID_MODES:
                raise HandoffRefused(
                    f"{label}: {path!r} in {repo} has mode {mode!r}, not "
                    "100644 or 100755"
                )
            _check_component(repo, path, f"{label} entry")


def _split_rename_spec(spec: str) -> tuple[str, str]:
    """Invert git's pprint_rename abbreviation: either a plain ``a => b``, or
    a common-prefix/suffix form ``pfx{mid-a => mid-b}sfx`` (git only takes
    this shorter form when the paths need no C-style quoting, so a quoted
    spec here is intentionally left unparsed, below)."""
    brace_start = spec.find("{")
    brace_end = spec.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        prefix = spec[:brace_start]
        middle = spec[brace_start + 1 : brace_end]
        suffix = spec[brace_end + 1 :]
        if " => " in middle:
            mid_a, _, mid_b = middle.partition(" => ")
            return prefix + mid_a + suffix, prefix + mid_b + suffix
    if " => " in spec:
        old, _, new = spec.partition(" => ")
        return old, new
    raise HandoffRefused(f"could not parse git apply --summary rename/copy spec {spec!r}")


def _check_numstat_paths(repo: Path, numstat_output: bytes) -> None:
    """Check the component rule against every path named by a
    ``--numstat -z`` record itself (one NUL-terminated ``added\\tdeleted\\t
    path`` triple per changed file -- for a rename/copy this is the
    destination; the source only ever appears in the ``--summary`` text, so
    ``_check_summary`` below covers it). This is the direct, unparsed-text
    check the component rule needs: ``-z`` means these paths are never
    quoted, unlike plain ``git ls-files``/``git ls-tree`` output (see
    guard_tree's docstring)."""
    *records, _summary = numstat_output.split(b"\0")
    for record in records:
        if not record:
            continue
        _added, _tab1, rest = _decode(record).partition("\t")
        _deleted, _tab2, path = rest.partition("\t")
        if path:
            _check_component(repo, path, "git apply --numstat entry")


def _check_summary(repo: Path, numstat_output: bytes) -> None:
    """Parse ``git apply --numstat -z --summary --binary``'s stdout: the
    summary text is everything after the last NUL-terminated numstat entry.
    """
    summary_text = _decode(numstat_output.split(b"\0")[-1])
    for raw_line in summary_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = _CREATE_RE.match(line)
        if match:
            _check_component(repo, match.group("path"), "create mode entry")
            if match.group("mode") not in _VALID_MODES:
                raise HandoffRefused(
                    f"{match.group('path')!r} in {repo} created with mode "
                    f"{match.group('mode')!r}, not 100644 or 100755"
                )
            continue
        if _DELETE_RE.match(line):
            continue
        match = _MODE_CHANGE_RE.match(line)
        if match:
            path = match.group("path")
            if path:
                _check_component(repo, path, "mode change entry")
            if match.group("new") not in _VALID_MODES:
                raise HandoffRefused(
                    f"{path or '(see the rename/copy line above)'!r} in {repo} "
                    f"changed mode to {match.group('new')!r}, not 100644 or 100755"
                )
            continue
        match = _RENAME_RE.match(line) or _COPY_RE.match(line)
        if match:
            old, new = _split_rename_spec(match.group("spec"))
            _check_component(repo, old, "rename/copy source")
            _check_component(repo, new, "rename/copy destination")
            continue
        raise HandoffRefused(f"unrecognised `git apply --summary` line in {repo}: {line!r}")


def _save_refused_patch(patch: bytes, task_id: str) -> Path:
    dest_dir = dispatch.FAILURE_PATCH_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = dest_dir / f"{task_id}-{stamp}.refused.patch"
    dest.write_bytes(patch)
    return dest


def apply(W: Path, patch: bytes, task_id: str) -> HandoffResult:
    """Apply ``patch`` to ``W/repo``, in order, or not at all.

    Refuses before any git call when ``patch`` is over MAX_PATCH_BYTES;
    otherwise runs ``git apply --numstat -z --summary --binary`` and refuses
    on any disallowed component in a numstat record's own path or a
    rename/copy source or destination, or any disallowed created/mode-changed
    mode; only then runs the real ``git apply --index --binary`` and
    guard_tree() again. On any refusal the patch bytes are saved to
    dispatch.FAILURE_PATCH_DIR as data (never applied), and the raised
    HandoffRefused names that path.
    """
    repo = W / "repo"
    try:
        if len(patch) > MAX_PATCH_BYTES:
            raise HandoffRefused(
                f"patch is {len(patch)} bytes, over the {MAX_PATCH_BYTES}-byte bound"
            )
        numstat = _run_git(
            repo, ["apply", "--numstat", "-z", "--summary", "--binary"], input=patch
        )
        if numstat.returncode != 0:
            raise HandoffRefused(
                "git apply --numstat could not parse the patch: "
                f"{_decode(numstat.stderr or numstat.stdout).strip()}"
            )
        _check_numstat_paths(repo, numstat.stdout)
        _check_summary(repo, numstat.stdout)
        applied = _run_git(repo, ["apply", "--index", "--binary"], input=patch)
        if applied.returncode != 0:
            raise HandoffRefused(
                "git apply --index failed: "
                f"{_decode(applied.stderr or applied.stdout).strip()}"
            )
        guard_tree(repo)
    except HandoffRefused as exc:
        exc.saved_patch_path = _save_refused_patch(patch, task_id)
        raise
    return HandoffResult(repo=repo)
