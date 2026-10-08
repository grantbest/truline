"""The worker must refuse to start without the config it cannot work without.

2026-08-06: the worker was restarted without SUBSTRATE_URL or
SUBSTRATE_API_KEY. It connected to Temporal, registered the schedule, logged
'factory-dispatcher worker started', and then failed every firing with
`KeyError: 'SUBSTRATE_URL'`. Five consecutive firings died in seconds each and
eight tasks sat pending. The factory was dead for three hours and every signal
an operator had said it was healthy.

These tests require no Temporal server, no substrate and no network.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402
import worker_revision  # noqa: E402

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [worker_revision.GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


COMPLETE = {
    "SUBSTRATE_URL": "http://localhost:18000",
    "SUBSTRATE_API_KEY": "test-key",
    "TEMPORAL_URL": "localhost:7233",
    "FACTORY_REPO": "example/repo",
    "FACTORY_REMOTE": "git@github.com:example/repo.git",
    "FACTORY_DEPLOYED_REVISION_NAMESPACE": "example-ns",
    "FACTORY_DEPLOYED_REVISION_DEPLOYMENT": "example-app",
}


@pytest.fixture(autouse=True)
def _checkout_on_main(monkeypatch, tmp_path_factory):
    """Point the checkout guard at a throwaway repo that is on main.

    worker.main() calls worker_revision.ensure_checkout_is_on_main_ancestor()
    at startup, which asks git whether HEAD is an ancestor of `main`. Left
    unpatched that question is asked of whatever checkout the tests happen to
    run in, and CI's actions/checkout gives a detached HEAD with no local
    `main` ref at all: the guard then refuses, and three tests about CONFIG
    die on git topology having nothing to do with what they assert.

    The guard is satisfied rather than stubbed out. Two of these tests exist to
    prove ORDER -- that the config refusal comes before Temporal is contacted --
    and replacing the guard with a lambda would let it silently move ahead of
    the config check without failing anything. Here it still runs, on a repo
    where it legitimately passes. The feature-branch test below overrides
    REPO_ROOT with its own repo, which is what that test is about.
    """
    repo = tmp_path_factory.mktemp("checkout") / "repo"
    _git(repo.parent, "init", str(repo))
    _git(repo, "config", "user.email", "factory@example.test")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    monkeypatch.setattr(worker_revision, "REPO_ROOT", repo)
    return repo


class ConnectAttempted(Exception):
    """Raised by the fake client so a test can prove connect() was reached."""


def _never_connects(monkeypatch):
    """Make Client.connect explode, so reaching it is observable."""

    async def fake_connect(*args, **kwargs):
        raise ConnectAttempted("Client.connect was called")

    monkeypatch.setattr(worker.Client, "connect", fake_connect)


def test_worker_refuses_to_start_without_substrate_config_and_names_it():
    with pytest.raises(worker.MissingConfigError) as excinfo:
        worker.ensure_required_config({"TEMPORAL_URL": "localhost:7233"})

    message = str(excinfo.value)
    assert "SUBSTRATE_URL" in message
    assert "SUBSTRATE_API_KEY" in message


def test_refusal_names_every_missing_variable_not_just_the_first():
    with pytest.raises(worker.MissingConfigError) as excinfo:
        worker.ensure_required_config({})

    message = str(excinfo.value)
    for name in (
        "SUBSTRATE_URL",
        "SUBSTRATE_API_KEY",
        "TEMPORAL_URL",
        "FACTORY_REPO",
        "FACTORY_REMOTE",
        "FACTORY_DEPLOYED_REVISION_NAMESPACE",
        "FACTORY_DEPLOYED_REVISION_DEPLOYMENT",
    ):
        assert name in message


def test_an_empty_value_is_missing_not_present():
    # A launchd env file with `SUBSTRATE_API_KEY=` sets the name and no value.
    with pytest.raises(worker.MissingConfigError, match="SUBSTRATE_API_KEY"):
        worker.ensure_required_config({**COMPLETE, "SUBSTRATE_API_KEY": ""})


def test_refusal_is_distinguishable_from_a_transient_connectivity_failure():
    with pytest.raises(worker.MissingConfigError) as excinfo:
        worker.ensure_required_config({})

    message = str(excinfo.value)
    # Named variables, not a stack trace, and explicitly not a tunnel problem:
    # launchd KeepAlive restarting the process cannot fix this one.
    assert "configuration fault" in message
    assert "not a connectivity fault" in message
    assert "Traceback" not in message


def test_complete_config_does_not_refuse():
    # Without this, a guard that always refused would pass every test above.
    assert worker.ensure_required_config(COMPLETE) is None


def test_main_refuses_before_connecting_to_temporal(monkeypatch, tmp_path):
    for name in COMPLETE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEMPORAL_URL", "localhost:7233")
    # worker.main() records its revision before any guard runs (worker_revision.py);
    # keep that write inside tmp_path instead of the real $HOME during tests.
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(tmp_path))
    _never_connects(monkeypatch)

    # The refusal must come first. A worker that connects and registers a
    # poller before noticing is exactly what made the outage invisible.
    with pytest.raises(worker.MissingConfigError):
        asyncio.run(worker.main())


def _build_repo_on_feature_branch(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "factory@example.test")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    branch = "fix/drop-two-dead-imports"
    _git(repo, "checkout", "-b", branch)
    (repo / "note.txt").write_text("side quest\n")
    _git(repo, "add", "note.txt")
    _git(repo, "commit", "-m", "feature work")
    return repo, branch


def test_main_refuses_when_the_worker_loaded_a_feature_branch_not_main(
    monkeypatch, tmp_path
):
    # TDD for the 2026-08-25 fix: the launchd worker loads whatever branch the
    # operator's shared working tree has checked out at restart. This fails
    # against today's behaviour -- nothing stops worker.main() from starting
    # on any branch -- and the failure must name the branch.
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(tmp_path / "state"))
    repo, branch = _build_repo_on_feature_branch(tmp_path)
    monkeypatch.setattr(worker_revision, "REPO_ROOT", repo)
    _never_connects(monkeypatch)

    with pytest.raises(worker.WorkerCheckoutDriftError) as excinfo:
        asyncio.run(worker.main())

    assert branch in str(excinfo.value)


def test_worker_refuses_to_start_when_required_executable_is_missing(
    monkeypatch, tmp_path
):
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    # This test empties PATH, and the checkout guard shells out to git. On this
    # runner worker_revision.GIT resolves to an absolute path so the guard
    # survives an empty PATH; on CI's Linux runner it is the bare name "git",
    # which then cannot be found and raises WorkerCheckoutDriftError before the
    # MissingExecutableError this test is about. The guard is stubbed here --
    # and only here -- because the subject is a missing WORKER executable, not
    # git: the two tests above keep the real guard in the code path.
    monkeypatch.setattr(
        worker_revision,
        "ensure_checkout_is_on_main_ancestor",
        lambda **kwargs: "stubbed-revision",
    )
    import dispatch
    monkeypatch.setattr(dispatch, "required_worker_executables", lambda: ("needed-tool",))
    _never_connects(monkeypatch)

    with pytest.raises(worker.MissingExecutableError) as excinfo:
        asyncio.run(worker.main())

    message = str(excinfo.value)
    assert "needed-tool" in message
    assert f"PATH searched: {tmp_path / 'empty-bin'}" in message


def _read_real_default_or_skip(real_default: Path) -> bytes | None:
    """The bytes at ``real_default``, ``None`` if it is absent, or a skip if
    it cannot even be READ.

    Distinguishes "absent" from "unreadable" with an explicit
    ``PermissionError`` probe rather than ``Path.exists()``: on Python 3.14,
    ``exists()`` swallows ``PermissionError`` and returns ``False``, which
    would make this test pass vacuously -- without checking anything -- when
    run inside the declared-verification sandbox (dev.finding 6c19f60f),
    whose profile denies READING ``Path.home() / ".factory-dispatcher"``
    (``containment.state_dir_read_denies``). Under that deny this test has
    nothing left to check: the sandbox's write deny already prevents the
    write this test guards against, so a skip -- not a silent pass -- is the
    honest outcome.
    """
    try:
        return real_default.read_bytes()
    except FileNotFoundError:
        return None
    except PermissionError:
        pytest.skip(
            f"cannot read {real_default}: denied by the verification sandbox's "
            "state-directory read deny (containment.state_dir_read_denies) -- "
            "its write deny already covers what this test guards against"
        )


def test_worker_main_without_state_dir_override_never_touches_the_real_default_path(
    monkeypatch, tmp_path
):
    """Mechanical proof that the suite cannot clobber the real worker record.

    2026-08-27: this is the exact call the test above makes --
    asyncio.run(worker.main()) with FACTORY_DISPATCHER_STATE_DIR unset -- the
    one that overwrote a real launchd worker's revision record twice in
    production because record_worker_start() (worker.main()'s first,
    unconditional statement) fell back to the real $HOME. conftest.py's
    autouse fixture now points HOME at a per-test tmp_path before this test
    body even starts; this asserts that isolation actually holds, by reading
    the real default path independently of $HOME (via pwd, which conftest.py
    cannot redirect) and proving it is untouched. Without conftest.py's
    fixture, this fails: the write lands at the real path below.

    Runs inside the declared-verification sandbox itself once dev.finding
    6c19f60f's containment wraps preflight -- see _read_real_default_or_skip.
    """
    monkeypatch.delenv(worker_revision.STATE_DIR_ENV, raising=False)
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr(
        worker_revision,
        "ensure_checkout_is_on_main_ancestor",
        lambda **kwargs: "stubbed-revision",
    )
    import dispatch
    monkeypatch.setattr(dispatch, "required_worker_executables", lambda: ("needed-tool",))
    _never_connects(monkeypatch)

    real_default = (
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ".factory-dispatcher" / "worker-revision.json"
    )
    before = _read_real_default_or_skip(real_default)

    with pytest.raises(worker.MissingExecutableError):
        asyncio.run(worker.main())

    after = _read_real_default_or_skip(real_default)
    assert after == before, (
        f"worker.main() wrote to the real default state path {real_default} -- "
        "test isolation is not holding"
    )


@pytest.mark.skipif(
    not (worker_revision.REPO_ROOT / ".git").exists(),
    reason=(
        "requires a .git directory so record_worker_start's real "
        "`git rev-parse HEAD` can succeed; absent in a tree produced by "
        "`git archive` (e.g. a gate-review export)"
    ),
)
def test_main_proceeds_past_the_guard_when_config_is_complete(monkeypatch, tmp_path):
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FACTORY_DISPATCHER_STATE_DIR", str(tmp_path))
    import dispatch
    monkeypatch.setattr(dispatch, "required_worker_executables", lambda: ())
    _never_connects(monkeypatch)

    # Reaching Client.connect is the proof: the guard let a correctly
    # configured worker through rather than refusing unconditionally.
    with pytest.raises(ConnectAttempted):
        asyncio.run(worker.main())

    # record_worker_start() ran before the guard, from the real checkout: a
    # worker that reaches Client.connect must have already left a revision
    # record, not just have been allowed to proceed.
    import worker_revision

    record = worker_revision.read_worker_revision_record(tmp_path / "worker-revision.json")
    assert record is not None
    assert record.revision
