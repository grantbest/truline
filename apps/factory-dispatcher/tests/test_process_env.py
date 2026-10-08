"""Tests for process_env.py: the stdlib-only credential-name predicate, the env-file
parser that refuses what `sh` sourcing could read differently, and child_env(), the
builder every spawn site should route through (SEC-a0166920-2 is the migration that
does that; this module ships the builder and the count it must drive to zero)."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import process_env  # noqa: E402


# ---------------------------------------------------------------------------
# AC-1: stdlib-only module, credential names, the sensitive-name pattern.
# ---------------------------------------------------------------------------


def test_process_env_imports_only_the_standard_library():
    """A fresh subprocess importing only process_env must not pull in any module that
    reads configuration at import time (dev.finding a0166920's import-order trap)."""
    probe = (
        "import process_env, sys; "
        'print(sorted(m for m in ("config", "dispatch", "launchd_agent", "worker", '
        '"worker_checkout", "substrate") if m in sys.modules))'
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_credential_names_are_exactly_the_declared_four():
    assert process_env.CREDENTIAL_NAMES == (
        "SUBSTRATE_API_KEY",
        "SUBSTRATE_READ_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DISCORD_WEBHOOK_URL",
    )


def test_sensitive_name_pattern_matches_dispatchs_pattern_byte_for_byte():
    import dispatch

    assert process_env.SENSITIVE_NAME_PATTERN.pattern == dispatch.SENSITIVE_ENV_NAME_PATTERN.pattern
    assert process_env.SENSITIVE_NAME_PATTERN.flags == dispatch.SENSITIVE_ENV_NAME_PATTERN.flags


@pytest.mark.parametrize(
    "name",
    [
        "SUBSTRATE_API_KEY",
        "SUBSTRATE_READ_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "GH_TOKEN",
    ],
)
def test_is_credential_name_true_for_declared_and_pattern_matched_names(name):
    assert process_env.is_credential_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["PATH", "KUBECONFIG", "TEMPORAL_URL", "SUBSTRATE_URL", "FACTORY_REPO"],
)
def test_is_credential_name_false_for_ordinary_names(name):
    assert process_env.is_credential_name(name) is False


# ---------------------------------------------------------------------------
# AC-2: the parser refuses what sh could read differently from a plain parse.
# ---------------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


REFUSED_LINE_CASES = {
    "hash_mid_line": "A=abc#def",
    "semicolon": "A=x;y",
    "ampersand": "A=x&y",
    "pipe": "A=x|y",
    "less_than": "A=x<y",
    "greater_than": "A=x>y",
    "parentheses": "A=x(y)",
    "backslash": "A=x\\y",
    "single_quote": "A=x'y",
    "double_quote": 'A=x"y',
    "backtick": "A=x`y`",
    "dollar": "A=x$y",
    "leading_tilde": "A=~root",
    "colon_tilde": "A=x:~y",
}


@pytest.mark.parametrize("line", REFUSED_LINE_CASES.values(), ids=REFUSED_LINE_CASES.keys())
def test_parse_env_file_refuses_each_unsafe_character_class(tmp_path, line):
    env_file = write(tmp_path / "env", line + "\n")

    with pytest.raises(process_env.ProcessEnvError) as excinfo:
        process_env.parse_env_file(env_file)

    message = str(excinfo.value)
    assert "A" in message
    fixture_value = line.split("=", 1)[1]
    assert fixture_value not in message


@pytest.mark.parametrize(
    "line",
    ["A=https://x/#frag", "A=abc#def", "A=x;y"],
    ids=["url_fragment", "hash_mid_word", "semicolon_command"],
)
def test_parse_env_file_refuses_rather_than_silently_truncates(tmp_path, line):
    env_file = write(tmp_path / "env", line + "\n")

    with pytest.raises(process_env.ProcessEnvError):
        process_env.parse_env_file(env_file)


def test_parse_env_file_matches_sh_sourcing_for_accepted_lines(tmp_path):
    lines = [
        "SUBSTRATE_URL=http://127.0.0.1:18001",
        "TEMPORAL_NAMESPACE=prod",
        "FACTORY_REPO=example/repo",
        "THE_PORT=8000",
        "KUBECONFIG_PATH=/Users/example/.kube/config",
        "export EXPORTED_NAME=exported-value",
    ]
    names = ["SUBSTRATE_URL", "TEMPORAL_NAMESPACE", "FACTORY_REPO", "THE_PORT",
             "KUBECONFIG_PATH", "EXPORTED_NAME"]
    env_file = write(tmp_path / "accepted.env", "\n".join(lines) + "\n")

    parsed = process_env.parse_env_file(env_file)

    sh_script = f"set -a; . {shlex.quote(str(env_file))}; set +a; env -0"
    proc = subprocess.run(
        ["/bin/sh", "-c", sh_script],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=True,
    )
    sh_env = dict(
        entry.split("=", 1)
        for entry in proc.stdout.decode("utf-8").split("\0")
        if entry
    )

    for name in names:
        assert parsed[name] == sh_env[name]


def test_parse_env_file_missing_file_raises_process_env_error(tmp_path):
    with pytest.raises(process_env.ProcessEnvError, match="does not exist"):
        process_env.parse_env_file(tmp_path / "missing")


def test_parse_env_file_refuses_non_utf8_bytes_instead_of_raising_unicode_decode_error(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_bytes(b"A=1\nB=\xff\xfe not utf-8\n")

    with pytest.raises(process_env.ProcessEnvError) as excinfo:
        process_env.parse_env_file(env_file)

    message = str(excinfo.value)
    assert str(env_file) in message
    assert len(message.splitlines()) == 1


# ---------------------------------------------------------------------------
# AC-3: load_env_file() and child_env().
# ---------------------------------------------------------------------------


def test_load_env_file_sets_names_into_the_given_mapping_and_returns_names(tmp_path):
    env_file = write(tmp_path / "env", "A=1\nB=2\n")
    environ = {"A": "stale", "UNRELATED": "kept"}

    names = process_env.load_env_file(env_file, environ)

    assert environ == {"A": "1", "B": "2", "UNRELATED": "kept"}
    assert sorted(names) == ["A", "B"]


WRITE_KEY_FIXTURE = "wk_" + "x" * 20  # >= 16 chars, well over the substring threshold


def test_child_env_drops_declared_and_pattern_matched_credential_names():
    environ = {
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "SUBSTRATE_READ_API_KEY": "rk_" + "y" * 20,
        "CLAUDE_CODE_OAUTH_TOKEN": "ct_" + "z" * 20,
        "DISCORD_WEBHOOK_URL": "https://discord.example/webhook/" + "w" * 20,
        "ACCESS_TOKEN": "at_" + "q" * 20,
        "PATH": "/usr/bin:/bin",
    }

    result = process_env.child_env(environ=environ)

    for name in (
        "SUBSTRATE_API_KEY",
        "SUBSTRATE_READ_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "ACCESS_TOKEN",
    ):
        assert name not in result
    assert result["PATH"] == "/usr/bin:/bin"


def test_child_env_drops_aliases_that_carry_the_credential_value():
    environ = {
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "ALIAS": WRITE_KEY_FIXTURE,
        "PADDED_ALIAS": f"  {WRITE_KEY_FIXTURE}  ",
        "AUTH_HEADER": f"Bearer {WRITE_KEY_FIXTURE}",
        "PATH": "/usr/bin:/bin",
    }

    result = process_env.child_env(environ=environ)

    assert "ALIAS" not in result
    assert "PADDED_ALIAS" not in result
    assert "AUTH_HEADER" not in result
    for value in result.values():
        assert WRITE_KEY_FIXTURE not in value


def test_child_env_passes_through_ordinary_names_unchanged():
    environ = {
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/example",
        "KUBECONFIG": "/Users/example/.kube/config",
        "TEMPORAL_URL": "127.0.0.1:7233",
    }

    result = process_env.child_env(environ=environ)

    assert result["PATH"] == "/usr/bin:/bin"
    assert result["HOME"] == "/Users/example"
    assert result["KUBECONFIG"] == "/Users/example/.kube/config"
    assert result["TEMPORAL_URL"] == "127.0.0.1:7233"


def test_child_env_carries_only_the_declared_need():
    environ = {
        "SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE,
        "CLAUDE_CODE_OAUTH_TOKEN": "ct_" + "z" * 20,
        "PATH": "/usr/bin:/bin",
    }

    result = process_env.child_env(needs=("CLAUDE_CODE_OAUTH_TOKEN",), environ=environ)

    assert result["CLAUDE_CODE_OAUTH_TOKEN"] == "ct_" + "z" * 20
    assert "SUBSTRATE_API_KEY" not in result


def test_child_env_never_mutates_its_input_mapping():
    environ = {"SUBSTRATE_API_KEY": WRITE_KEY_FIXTURE, "PATH": "/usr/bin:/bin"}
    before = dict(environ)

    process_env.child_env(needs=("SUBSTRATE_API_KEY",), environ=environ)

    assert environ == before
