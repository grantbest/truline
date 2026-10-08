"""R2603-5: the platform core reads this household's repository from
configuration rather than a compiled-in default, and refuses -- naming what
is missing -- when that configuration is absent.

No Temporal server, no substrate, no network: dispatch.Config.from_env()
only ever reads os.environ.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402


def test_config_from_env_uses_the_households_own_values_supplied_as_configuration(
    monkeypatch,
):
    monkeypatch.setenv("FACTORY_REPO", "a-household/their-repo")
    monkeypatch.setenv("FACTORY_REMOTE", "git@github.com:a-household/their-repo.git")
    monkeypatch.setenv("FACTORY_BASE_REF", "trunk")

    cfg = dispatch.Config.from_env()

    assert cfg.repo == "a-household/their-repo"
    assert cfg.remote == "git@github.com:a-household/their-repo.git"
    assert cfg.base_ref == "trunk"


def test_config_from_env_refuses_when_factory_repo_is_missing(monkeypatch):
    monkeypatch.delenv("FACTORY_REPO", raising=False)
    monkeypatch.setenv("FACTORY_REMOTE", "git@github.com:a-household/their-repo.git")

    with pytest.raises(dispatch.MissingConfigError, match="FACTORY_REPO"):
        dispatch.Config.from_env()


def test_config_from_env_refuses_when_factory_remote_is_missing(monkeypatch):
    monkeypatch.setenv("FACTORY_REPO", "a-household/their-repo")
    monkeypatch.delenv("FACTORY_REMOTE", raising=False)

    with pytest.raises(dispatch.MissingConfigError, match="FACTORY_REMOTE"):
        dispatch.Config.from_env()


def test_config_from_env_refusal_names_both_when_both_are_missing(monkeypatch):
    monkeypatch.delenv("FACTORY_REPO", raising=False)
    monkeypatch.delenv("FACTORY_REMOTE", raising=False)

    with pytest.raises(dispatch.MissingConfigError) as excinfo:
        dispatch.Config.from_env()

    message = str(excinfo.value)
    assert "FACTORY_REPO" in message
    assert "FACTORY_REMOTE" in message
    assert "configuration fault" in message
