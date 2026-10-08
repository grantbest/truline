"""The verification venv must not be built from an interpreter it cannot install into.

2026-08-02: the host venv was 3.14.4, so sys.executable was 3.14, greenlet had
no wheel for it, and every declared verification command reported
could-not-start. The work was correct; the interpreter was not.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import dispatch  # noqa: E402


def _versions(mapping):
    """version_of stub: path -> (major, minor), unknown paths are unusable."""
    return lambda executable: mapping.get(executable)


def _which(mapping):
    return lambda name: mapping.get(name)


def test_supported_window_excludes_314():
    """The exact version that broke the factory must not be accepted."""
    assert not dispatch.python_is_supported((3, 14))
    assert not dispatch.python_is_supported((3, 15))


def test_supported_window_accepts_312_and_313():
    assert dispatch.python_is_supported((3, 12))
    assert dispatch.python_is_supported((3, 13))
    assert dispatch.python_is_supported((3, 11))


def test_too_old_is_rejected():
    assert not dispatch.python_is_supported((3, 10))
    assert not dispatch.python_is_supported((2, 7))


def test_unusable_interpreter_is_not_supported():
    assert not dispatch.python_is_supported(None)


def test_launching_interpreter_used_when_supported():
    got = dispatch.resolve_verification_python(
        launching="/usr/bin/python3.12",
        version_of=_versions({"/usr/bin/python3.12": (3, 12)}),
        which=_which({}),
    )
    assert got == "/usr/bin/python3.12"


def test_falls_back_when_launcher_is_314():
    """The 2026-08-02 case: launched from 3.14, a 3.12 is on PATH."""
    got = dispatch.resolve_verification_python(
        launching="/repo/venv/bin/python",
        candidates=("python3.13", "python3.12"),
        version_of=_versions({
            "/repo/venv/bin/python": (3, 14),
            "/opt/homebrew/bin/python3.12": (3, 12),
        }),
        which=_which({"python3.12": "/opt/homebrew/bin/python3.12"}),
    )
    assert got == "/opt/homebrew/bin/python3.12"


def test_raises_when_nothing_suitable_exists():
    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch.resolve_verification_python(
            launching="/repo/venv/bin/python",
            candidates=("python3.12",),
            version_of=_versions({"/repo/venv/bin/python": (3, 14)}),
            which=_which({}),
        )
    message = str(excinfo.value)
    # The diagnosis, not a wheel-build log.
    assert "3.14" in message
    assert "greenlet" in message
    assert "FACTORY_PYTHON" in message


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("FACTORY_PYTHON", "python3.12")
    got = dispatch.resolve_verification_python(
        launching="/repo/venv/bin/python",
        version_of=_versions({
            "/repo/venv/bin/python": (3, 14),
            "/usr/local/bin/python3.12": (3, 12),
        }),
        which=_which({"python3.12": "/usr/local/bin/python3.12"}),
    )
    assert got == "/usr/local/bin/python3.12"


def test_explicit_override_is_not_silently_replaced(monkeypatch):
    """An operator naming an unsupported interpreter is told, not overridden."""
    monkeypatch.setenv("FACTORY_PYTHON", "python3.14")
    with pytest.raises(dispatch.DispatchError) as excinfo:
        dispatch.resolve_verification_python(
            launching="/usr/bin/python3.12",
            candidates=("python3.12",),
            version_of=_versions({
                "python3.14": (3, 14),
                "/usr/bin/python3.12": (3, 12),
            }),
            which=_which({"python3.12": "/usr/bin/python3.12"}),
        )
    assert "FACTORY_PYTHON" in str(excinfo.value)
    assert "3.14" in str(excinfo.value)


def test_interpreter_version_reads_a_real_interpreter():
    """The probe must work against an actual binary, not just stubs."""
    assert dispatch.interpreter_version(sys.executable) == sys.version_info[:2]


def test_interpreter_version_returns_none_for_nonsense():
    assert dispatch.interpreter_version("/nonexistent/python-does-not-exist") is None
