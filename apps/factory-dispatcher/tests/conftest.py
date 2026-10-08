"""Test-suite-wide isolation for on-disk defaults under the operator's $HOME.

2026-08-27: worker.main() calls worker_revision.record_worker_start() as its
first statement, unconditionally, before any guard -- deliberately, so a
worker dying at a later guard still leaves a record of the revision it died
running (worker_revision.py). record_worker_start() writes to
worker_revision.default_state_path(), which falls back to
`Path.home() / ".factory-dispatcher"` whenever FACTORY_DISPATCHER_STATE_DIR
is unset. test_worker_config_guard.py's sibling tests each set that
variable; test_worker_refuses_to_start_when_required_executable_is_missing
did not, so running the suite from any checkout overwrote a real launchd
worker's revision record with that checkout's HEAD -- twice, in production,
in one day.

Leaving this to each test author to remember is exactly the arrangement that
produced the incident. This autouse fixture makes it structural instead: no
test in this suite ever sees the real $HOME, so
worker_revision.default_state_path() (and the analogous fallbacks in
worker_checkout.py, dispatch.py and launchd_agent.py) can only ever resolve
under a throwaway directory, regardless of what any individual test does or
forgets to do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# conftest.py loads before any test module's own sys.path.insert (several
# test files do this themselves, e.g. test_worker_registration.py), so the
# import below needs this or it is the first thing in the whole session to
# try reaching a top-level app module and fails.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activities import dispatch_steps  # noqa: E402


@pytest.fixture(autouse=True)
def _no_test_writes_to_the_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _every_test_commit_has_an_identity(monkeypatch):
    """A test that commits needs a git author/committer identity, and the
    fixture above already means it can never fall back to a real $HOME's
    ~/.gitconfig to get one. A self-hosted CI runner has no identity of its
    own either, so a test that relied on ambient config passed on any
    developer machine with a global git identity and exited 128 ('Author
    identity unknown') in CI -- a test passing in the factory clone and
    failing in CI on an environment difference, for the third time on this
    platform. Setting GIT_AUTHOR_*/GIT_COMMITTER_* here, once, for the whole
    suite, means no individual test's ``git commit`` can ever depend on
    ambient identity again, the same structural fix as the fixture above.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory@example.test")


@pytest.fixture(autouse=True)
def _dispatch_run_lock_never_leaks_across_tests():
    """dispatch_steps.claim_activity/cleanup_activity share the run lock's
    handle through a module-level global, not through anything a test
    controls -- most claim_activity tests call it once and never reach
    cleanup_activity (the normal release point) in the same test, so the
    handle from a "claimed" result would otherwise sit open for the rest of
    the test session. `_no_test_writes_to_the_real_home` already gives each
    test its own default lock *path* (it derives from $HOME), which is
    enough to stop one test's held lock from making another test's claim
    fail -- but it does nothing about the leaked file descriptor. Releasing
    before AND after every test is the structural fix for both: before,
    in case a previous test somehow left the module in a held state despite
    the path isolation; after, so the descriptor from whatever this test
    itself claimed and never released is always closed.
    """
    dispatch_steps._release_dispatch_run_lock()
    yield
    dispatch_steps._release_dispatch_run_lock()


@pytest.fixture(autouse=True)
def _default_household_repo_config(monkeypatch):
    """R2603-5: FACTORY_REPO / FACTORY_REMOTE are required configuration
    (config.REQUIRED_CONFIG) that dispatch.Config.from_env() now refuses to
    start without, replacing a compiled-in household default. Setting a
    generic placeholder here, for every test, is the same "structural, not
    left to each test author" fix as the $HOME fixture above -- the great
    majority of this suite's tests have nothing to do with which repo is
    configured and would otherwise all need to remember to set it themselves.
    A test that specifically exercises the missing-config refusal
    (test_worker_config_guard.py) deletes these itself.
    """
    monkeypatch.setenv("FACTORY_REPO", "example/repo")
    monkeypatch.setenv("FACTORY_REMOTE", "git@github.com:example/repo.git")
    monkeypatch.setenv("FACTORY_DEPLOYED_REVISION_NAMESPACE", "example-ns")
    monkeypatch.setenv("FACTORY_DEPLOYED_REVISION_DEPLOYMENT", "example-app")
