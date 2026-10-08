"""Pure logic for a clone's git-control fingerprint (dev.finding 79db3113
part c1/AC-5). Everything here is stdlib-only (hashlib/json/os/pathlib/stat)
and never imports ``dispatch`` -- ``dispatch.py`` imports this module, never
the reverse, so the fingerprint and its comparison can be read, tested and
reasoned about without the rest of the dispatcher's surface.

What is fingerprinted and why (dev.finding 79db3113 part c1 intent): a clone's
``.git`` must stay a directory (not a symlink, not a gitdir file -- either
would defeat every other check); ``.git/commondir`` must stay absent (its
presence redirects config/hooks/objects/refs elsewhere, bypassing every other
check); the SHA-256 of ``.git/config``; and a sorted manifest of
``.git/hooks`` and ``.git/info`` giving each entry's relative path, mode and
SHA-256, never following a symlink (a symlink is recorded as one). Not
``.git/objects``, ``index``, ``refs`` or ``logs``: ordinary git work writes
those.

The comparison here (``compare_git_control_fingerprint``) returns the first
difference as a message, or ``None`` -- it never raises, so that a dispatch
error type (which this module must not depend on) stays entirely
``dispatch.py``'s concern.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

# Beside the clone, in its PARENT -- never a suffix on the clone's own name
# (which would share a string PREFIX with paths under the clone without
# being under it; dev.finding 79db3113 part c1, AC-1).
GIT_CONTROL_DIR_NAME = ".git-control"


def record_path(clone: Path) -> Path:
    resolved = clone.resolve()
    return resolved.parent / GIT_CONTROL_DIR_NAME / f"{resolved.name}.json"


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_entries(root: Path, label: str) -> list[dict[str, Any]]:
    # Never follows a symlink -- one is recorded as such, never traversed.
    entries: list[dict[str, Any]] = []
    if not root.is_dir() or root.is_symlink():
        return entries

    def walk(directory: Path, prefix: str) -> None:
        for child in sorted(os.scandir(directory), key=lambda e: e.name):
            rel, path, st = f"{prefix}{child.name}", Path(child.path), os.lstat(child.path)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                sha256 = _hash_bytes(os.readlink(path).encode())
                entries.append({"path": f"{label}/{rel}", "kind": "symlink", "mode": mode, "sha256": sha256})
            elif stat.S_ISDIR(st.st_mode):
                entries.append({"path": f"{label}/{rel}", "kind": "dir", "mode": mode, "sha256": ""})
                walk(path, f"{rel}/")
            else:
                sha256 = _hash_bytes(path.read_bytes())
                entries.append({"path": f"{label}/{rel}", "kind": "file", "mode": mode, "sha256": sha256})

    walk(root, "")
    return entries


def compute_git_control_fingerprint(clone: Path) -> dict[str, Any]:
    git_dir = clone / ".git"
    try:
        git_st = git_dir.lstat()
    except FileNotFoundError:
        kind = "missing"
    else:
        kind = "symlink" if stat.S_ISLNK(git_st.st_mode) else "dir" if stat.S_ISDIR(git_st.st_mode) else "file"
    if kind != "dir":
        return {"git_entry_kind": kind, "commondir_present": False, "config": None, "manifest": []}
    config_path = git_dir / "config"
    try:
        config_st = config_path.lstat()
    except FileNotFoundError:
        config = None
    else:
        # Never read through a symlink -- its hash must never collide with
        # the real file's (AC-2's "config replaced by a symlink").
        config = (
            "symlink:" + _hash_bytes(os.readlink(config_path).encode())
            if stat.S_ISLNK(config_st.st_mode) else _hash_bytes(config_path.read_bytes())
        )
    manifest = sorted(
        _manifest_entries(git_dir / "hooks", "hooks") + _manifest_entries(git_dir / "info", "info"),
        key=lambda e: e["path"],
    )
    return {
        # lexists, not .exists(): a DANGLING symlink at .git/commondir must
        # still count as present -- .exists() follows the link and reports
        # False for exactly the tamper this field exists to catch.
        "git_entry_kind": kind, "commondir_present": os.path.lexists(git_dir / "commondir"),
        "config": config, "manifest": manifest,
    }


def record_exists(clone: Path) -> bool:
    return record_path(clone).exists()


def read_record(clone: Path) -> dict[str, Any]:
    return json.loads(record_path(clone).read_text())


def write_record(clone: Path, fingerprint: dict[str, Any]) -> None:
    path = record_path(clone)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fingerprint, indent=2, sort_keys=True))


def compare_git_control_fingerprint(
    baseline: dict[str, Any], current: dict[str, Any], clone: Path
) -> str | None:
    """Returns the first difference as a message naming the changed path (or
    ``".git entry replaced"`` / ``".git/commondir present"``), or ``None`` if
    unchanged. Never raises -- callers that want a dispatch-domain error wrap
    this return."""
    git_dir = clone / ".git"
    if current["git_entry_kind"] != "dir":
        return f"{clone}: .git entry replaced"
    if current["commondir_present"]:
        return f"{clone}: .git/commondir present"
    if current["config"] != baseline.get("config"):
        return f"{clone}: {git_dir / 'config'} changed"
    baseline_entries = {e["path"]: e for e in baseline.get("manifest", [])}
    current_entries = {e["path"]: e for e in current["manifest"]}
    for path in sorted(set(baseline_entries) | set(current_entries)):
        if baseline_entries.get(path) != current_entries.get(path):
            return f"{clone}: {git_dir / path} changed"
    return None
