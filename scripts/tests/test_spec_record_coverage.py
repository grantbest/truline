"""Tests for scripts/spec_record_coverage.py.

`apps/factory-dispatcher/tasks/` can hold spec files that never made it into
git -- and when the bead that spec drove has already reached a TERMINAL
state, the acceptance criteria the shipped work was built against no longer
exist anywhere in the repository. These tests exercise that check both ways
(AC-4): an untracked spec whose bead is done fails the check by name, and the
same fixture with the file tracked passes -- against snapshot fixtures built
right here, never against a live store (AC-2).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import spec_record_coverage as src

_GIT_ENV_ARGS = ["-c", "init.defaultBranch=main"]

#: AC-7: no test may pick up the operator's real git identity or config --
#: every git invocation in this file runs against a neutralised, fixed
#: environment instead of the ambient one.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_GIT_ENV_ARGS, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def _write_spec(repo: Path, tasks_dir: Path, name: str, title: str) -> Path:
    full_dir = repo / tasks_dir
    full_dir.mkdir(parents=True, exist_ok=True)
    spec_path = full_dir / name
    spec_path.write_text(json.dumps({"title": title}))
    return spec_path


def _write_snapshot(
    repo: Path,
    tasks: list[dict[str, str]],
    *,
    snapshot_kind: str | None = "measured",
    dated: str | None = "2026-09-18",
) -> Path:
    """Build a snapshot fixture in ``tmp_path``.

    Defaults produce a snapshot the guard accepts (``snapshot_kind``
    "measured", a "dated" field) -- these are synthetic, fixture-shaped rows
    for exercising the check's own logic, not a claim about real bead state.
    Pass ``snapshot_kind=None`` / ``dated=None`` to omit the field entirely,
    or any other string to test the guard's negative path.
    """
    body: dict[str, object] = {"tasks": tasks}
    if snapshot_kind is not None:
        body["snapshot_kind"] = snapshot_kind
    if dated is not None:
        body["dated"] = dated
    snapshot_path = repo / "snapshot.json"
    snapshot_path.write_text(json.dumps(body))
    return snapshot_path


TASKS_DIR = Path("apps/factory-dispatcher/tasks")


# ---------------------------------------------------------------------------
# AC-2: no substrate client, no HTTP call, no credential lookup, anywhere.
# ---------------------------------------------------------------------------


def test_module_imports_no_substrate_client():
    source = Path(src.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_substrings = ("substrate", "credential")
    forbidden_names = {"requests", "httpx", "urllib3", "http.client", "aiohttp"}

    for name in imported:
        assert not any(bad in name.lower() for bad in forbidden_substrings), name
    assert not (imported & forbidden_names)


# ---------------------------------------------------------------------------
# classify() -- the pure decision table, no filesystem or git involved.
# ---------------------------------------------------------------------------


def test_classify_tracked_is_always_fine_regardless_of_bead_state():
    assert src.classify(tracked=True, bead_states=[]) == src.TRACKED
    assert src.classify(tracked=True, bead_states=["done"]) == src.TRACKED


def test_classify_untracked_no_bead_is_its_own_class():
    assert src.classify(tracked=False, bead_states=[]) == src.UNTRACKED_NO_BEAD


def test_classify_untracked_terminal_bead_states():
    assert src.classify(tracked=False, bead_states=["done"]) == src.UNTRACKED_TERMINAL_BEAD
    assert (
        src.classify(tracked=False, bead_states=["superseded"]) == src.UNTRACKED_TERMINAL_BEAD
    )


def test_classify_untracked_open_bead_states():
    assert src.classify(tracked=False, bead_states=["pending"]) == src.UNTRACKED_OPEN_BEAD
    assert src.classify(tracked=False, bead_states=["failed"]) == src.UNTRACKED_OPEN_BEAD


def test_classify_a_reopened_title_is_open_not_terminal():
    """A superseded bead sharing a title with a live re-filed one is exactly
    the 'correctly queued again' shape scanner.py's scan_spec_record already
    treats as non-drift -- one closed match must not, by itself, condemn a
    title that also has an open match."""
    assert (
        src.classify(tracked=False, bead_states=["superseded", "pending"])
        == src.UNTRACKED_OPEN_BEAD
    )


# ---------------------------------------------------------------------------
# check() -- fired both ways against real git repos (AC-4).
# ---------------------------------------------------------------------------


def test_exits_nonzero_and_names_the_file_when_an_untracked_specs_bead_is_done(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = _write_spec(repo, TASKS_DIR, "shipped-with-no-spec.json", "Shipped work")
    snapshot = _write_snapshot(repo, [{"title": "Shipped work", "state": "done"}])

    result = src.check(repo, TASKS_DIR, snapshot)

    assert result.ok is False
    named = {status.path for status in result.untracked_terminal_bead}
    assert spec_path.relative_to(repo).as_posix() in named


def test_exits_zero_once_the_same_spec_is_tracked(tmp_path):
    repo = _init_repo(tmp_path)
    _write_spec(repo, TASKS_DIR, "shipped-with-no-spec.json", "Shipped work")
    snapshot = _write_snapshot(repo, [{"title": "Shipped work", "state": "done"}])
    _git(repo, "add", str(TASKS_DIR / "shipped-with-no-spec.json"))

    result = src.check(repo, TASKS_DIR, snapshot)

    assert result.ok is True
    assert result.untracked_terminal_bead == ()


def test_untracked_spec_with_no_matching_bead_is_reported_but_does_not_fail(tmp_path):
    """AC-3: unfiled work is a different failure from a missing record, and
    must not be folded into the terminal-bead class that fails the check."""
    repo = _init_repo(tmp_path)
    spec_path = _write_spec(repo, TASKS_DIR, "never-filed.json", "Nobody queued this")
    snapshot = _write_snapshot(repo, [])

    result = src.check(repo, TASKS_DIR, snapshot)

    assert result.ok is True
    named = {status.path for status in result.untracked_no_bead}
    assert spec_path.relative_to(repo).as_posix() in named
    assert result.untracked_terminal_bead == ()


def test_untracked_spec_with_a_pending_bead_is_reported_but_does_not_fail(tmp_path):
    repo = _init_repo(tmp_path)
    spec_path = _write_spec(repo, TASKS_DIR, "still-queued.json", "Still queued work")
    snapshot = _write_snapshot(repo, [{"title": "Still queued work", "state": "pending"}])

    result = src.check(repo, TASKS_DIR, snapshot)

    assert result.ok is True
    named = {status.path for status in result.untracked_open_bead}
    assert spec_path.relative_to(repo).as_posix() in named


# ---------------------------------------------------------------------------
# The CLI entry point, exercised as a real subprocess -- proof at the level
# the release gate actually runs this at (a process exit code).
# ---------------------------------------------------------------------------


def _run_cli(repo: Path, snapshot: Path) -> subprocess.CompletedProcess:
    script = Path(src.__file__).resolve().parent / "check-spec-record-coverage.py"
    return subprocess.run(
        [
            "python3",
            str(script),
            "--repo",
            str(repo),
            "--tasks-dir",
            str(TASKS_DIR),
            "--snapshot",
            str(snapshot),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def test_cli_exits_nonzero_for_an_untracked_done_spec_and_names_it(tmp_path):
    repo = _init_repo(tmp_path)
    _write_spec(repo, TASKS_DIR, "shipped-with-no-spec.json", "Shipped work")
    snapshot = _write_snapshot(repo, [{"title": "Shipped work", "state": "done"}])

    completed = _run_cli(repo, snapshot)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "shipped-with-no-spec.json" in completed.stdout


# ---------------------------------------------------------------------------
# The committed snapshot fixture (AC-2: "commit a snapshot ... under the
# dispatcher's test tree"). This worker had no substrate credentials and no
# route to the live dev.task population, so this is a labeled schema example,
# not a production dump -- but it is real committed input this loader must
# keep parsing, not a file nothing ever reads.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

DISPATCHER_TEST_TREE_SNAPSHOT = (
    REPO_ROOT
    / "apps/factory-dispatcher/tests/fixtures/spec_record_coverage"
    / "dev-task-titles-and-states.example.json"
)


def test_committed_example_snapshot_is_refused_not_agreed_with():
    """#931 F1: the shipped fixture holds three honestly-labelled PLACEHOLDER
    rows, and the shipped check agreed with it anyway (44 untracked, "OK",
    EXIT 0, against a tree missing 26+ shipped specs). The fixture now
    declares "snapshot_kind": "example", and the loader SHALL refuse it
    rather than silently treating placeholder rows as real bead state."""
    with pytest.raises(src.UntrustedSnapshotError, match="snapshot_kind"):
        src.load_snapshot(DISPATCHER_TEST_TREE_SNAPSHOT)


def test_cli_refuses_the_committed_example_snapshot_against_the_real_tree():
    """The exact F1 reproduction: run the shipped CLI, against the real repo
    tree, with the shipped example fixture. It must no longer print OK."""
    completed = _run_cli(REPO_ROOT, DISPATCHER_TEST_TREE_SNAPSHOT)

    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "REFUSED" in completed.stdout
    assert "OK" not in completed.stdout.splitlines()


def test_cli_exits_zero_once_that_spec_is_tracked(tmp_path):
    repo = _init_repo(tmp_path)
    _write_spec(repo, TASKS_DIR, "shipped-with-no-spec.json", "Shipped work")
    snapshot = _write_snapshot(repo, [{"title": "Shipped work", "state": "done"}])
    _git(repo, "add", str(TASKS_DIR / "shipped-with-no-spec.json"))

    completed = _run_cli(repo, snapshot)

    assert completed.returncode == 0, completed.stdout + completed.stderr


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3: the snapshot-trust guard, both ways.
# ---------------------------------------------------------------------------


def test_snapshot_missing_snapshot_kind_is_refused_ambiguous_fails_closed(tmp_path):
    """PRIN-015: absence of a trust declaration is not evidence of trust."""
    repo = _init_repo(tmp_path)
    snapshot = _write_snapshot(repo, [], snapshot_kind=None)

    with pytest.raises(src.UntrustedSnapshotError, match="no 'snapshot_kind' field"):
        src.load_snapshot(snapshot)


def test_snapshot_declared_example_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    snapshot = _write_snapshot(repo, [], snapshot_kind="example")

    with pytest.raises(src.UntrustedSnapshotError, match="snapshot_kind='example'"):
        src.load_snapshot(snapshot)


def test_snapshot_missing_dated_is_refused(tmp_path):
    repo = _init_repo(tmp_path)
    snapshot = _write_snapshot(repo, [], dated=None)

    with pytest.raises(src.UntrustedSnapshotError, match="no 'dated' field"):
        src.load_snapshot(snapshot)


def test_measured_snapshot_with_dated_field_is_accepted_and_its_date_reported(tmp_path):
    """AC-2: a guard that refused everything would satisfy AC-1 and destroy
    the tool -- a properly-labelled, dated snapshot must still pass. AC-3:
    the date it carries is reported, not silently dropped."""
    repo = _init_repo(tmp_path)
    spec_path = _write_spec(repo, TASKS_DIR, "still-queued.json", "Still queued work")
    snapshot = _write_snapshot(
        repo,
        [{"title": "Still queued work", "state": "pending"}],
        snapshot_kind="measured",
        dated="2026-09-17",
    )

    result = src.check(repo, TASKS_DIR, snapshot)

    assert result.snapshot_dated == "2026-09-17"
    named = {status.path for status in result.untracked_open_bead}
    assert spec_path.relative_to(repo).as_posix() in named

    report = src.render_report(result)
    assert "2026-09-17" in report


def test_cli_reports_the_snapshot_date_on_a_trusted_run(tmp_path):
    repo = _init_repo(tmp_path)
    snapshot = _write_snapshot(repo, [], dated="2026-01-05")

    completed = _run_cli(repo, snapshot)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "2026-01-05" in completed.stdout


def test_cli_refused_exit_code_is_distinct_from_ok_and_from_fail(tmp_path):
    """AC-1: an operator must be able to tell 'nothing is wrong' (0) apart
    from 'a spec shipped with no record' (1) apart from 'I could not tell'."""
    repo = _init_repo(tmp_path)
    untrusted_snapshot = _write_snapshot(repo, [], snapshot_kind=None)

    completed = _run_cli(repo, untrusted_snapshot)

    assert completed.returncode not in (0, 1), completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert "OK" not in completed.stdout
    assert "FAIL" not in completed.stdout


# ---------------------------------------------------------------------------
# AC-4 (F2): enumeration counts spec records, not every JSON under tasks/.
# ---------------------------------------------------------------------------


def test_untracked_json_under_tasks_snapshots_is_not_counted_as_a_spec(tmp_path):
    """The wrong-today-but-invisible case: tasks/snapshots/*.json has no
    'title' and would resolve to title="" -- if such a file were untracked
    (they happen to be tracked today) it would be miscounted as untitled
    'unqueued work'. It must not be reported at all: it is not a spec."""
    repo = _init_repo(tmp_path)
    snapshots_dir = repo / TASKS_DIR / "snapshots"
    snapshots_dir.mkdir(parents=True)
    non_spec = snapshots_dir / "2026-09-18-something.json"
    non_spec.write_text(json.dumps({"dated": "2026-09-18", "measured_by": "someone"}))
    spec_path = _write_spec(repo, TASKS_DIR, "real-spec.json", "A real spec")
    snapshot = _write_snapshot(repo, [])

    result = src.check(repo, TASKS_DIR, snapshot)

    all_paths = {status.path for status in result.statuses}
    assert non_spec.relative_to(repo).as_posix() not in all_paths
    assert spec_path.relative_to(repo).as_posix() in all_paths
    assert len(result.statuses) == 1


def test_all_spec_files_excludes_the_snapshots_subdirectory(tmp_path):
    repo = _init_repo(tmp_path)
    snapshots_dir = repo / TASKS_DIR / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "measurement.json").write_text("{}")
    _write_spec(repo, TASKS_DIR, "real-spec.json", "A real spec")

    files = src.all_spec_files(repo, TASKS_DIR)

    names = {p.name for p in files}
    assert names == {"real-spec.json"}
