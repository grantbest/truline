"""Tests for process_env.py's launcher: `process_env.py --env-file PATH SCRIPT [ARGS...]`.

Every test here runs the launcher as a real subprocess of sys.executable, with an
explicit minimal exec environment (just PATH and HOME) and cwd = apps/factory-dispatcher,
so these tests exercise the actual import-order and argument-handling behaviour rather
than calling process_env.main() in-process (which would not reproduce the second-copy
or import-order traps the module docstring describes).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _interpreter_env import minimal_exec_environment  # noqa: E402

FACTORY_DISPATCHER_DIR = Path(__file__).resolve().parents[1]

MINIMAL_ENV = minimal_exec_environment()


def run_launcher(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FACTORY_DISPATCHER_DIR / "process_env.py"), *args],
        cwd=str(FACTORY_DISPATCHER_DIR),
        env=MINIMAL_ENV if env is None else env,
        capture_output=True,
        text=True,
    )


def write_env_file(path: Path, values: dict[str, str]) -> Path:
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return path


FIXTURE_KEY = "sk_" + "a" * 20


def test_import_order_env_file_loads_before_config_and_dispatch_are_imported(tmp_path):
    env_file = write_env_file(
        tmp_path / "worker.env",
        {
            "TEMPORAL_NAMESPACE": "fixture-ns-0001",
            "FACTORY_PATCH_DIR": str(tmp_path / "patches"),
            "SUBSTRATE_API_KEY": FIXTURE_KEY,
        },
    )
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(FACTORY_DISPATCHER_DIR)!r})\n"
        "import config\n"
        "import dispatch\n"
        "print(json.dumps({\n"
        "    'namespace': config.Config.TEMPORAL_NAMESPACE,\n"
        "    'patch_dir': str(dispatch.FAILURE_PATCH_DIR),\n"
        "    'has_key': 'SUBSTRATE_API_KEY' in os.environ,\n"
        "}))\n",
        encoding="utf-8",
    )

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["namespace"] == "fixture-ns-0001"
    assert payload["patch_dir"] == str(tmp_path / "patches")
    assert payload["has_key"] is True


def test_loaded_names_are_visible_on_the_importable_module_copy(tmp_path):
    values = {
        "TEMPORAL_NAMESPACE": "fixture-ns-0002",
        "SUBSTRATE_API_KEY": FIXTURE_KEY,
        "SOME_OTHER_NAME": "some-other-value",
    }
    env_file = write_env_file(tmp_path / "worker.env", values)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "import process_env\n"
        "print(json.dumps({\n"
        "    name: os.environ.get(name)\n"
        "    for name in process_env.LOADED_ENV_NAMES\n"
        "}))\n",
        encoding="utf-8",
    )

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    for name, value in values.items():
        assert seen[name] == value


def test_arguments_after_script_belong_to_the_script_unchanged(tmp_path):
    env_file = write_env_file(tmp_path / "worker.env", {"A": "1"})
    probe = tmp_path / "probe.py"
    probe.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")

    result = run_launcher(
        ["--env-file", str(env_file), str(probe), "check-heartbeat", "--max-age-seconds", "7", "--flag"]
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["check-heartbeat", "--max-age-seconds", "7", "--flag"]


def test_script_system_exit_status_becomes_the_launchers_exit_status(tmp_path):
    env_file = write_env_file(tmp_path / "worker.env", {"A": "1"})
    probe = tmp_path / "probe.py"
    probe.write_text("raise SystemExit(3)\n", encoding="utf-8")

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 3


def test_missing_env_file_exits_2_without_running_script_or_leaking_a_value(tmp_path):
    probe = tmp_path / "probe.py"
    marker = tmp_path / "marker"
    probe.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")

    result = run_launcher(["--env-file", str(tmp_path / "does-not-exist"), str(probe)])

    assert result.returncode == 2
    assert not marker.exists()
    assert len(result.stderr.strip().splitlines()) == 1
    assert str(tmp_path / "does-not-exist") in result.stderr


def test_unparseable_env_file_exits_2_without_running_script_or_leaking_a_value(tmp_path):
    env_file = write_env_file(tmp_path / "worker.env", {"A": "x;y"})
    probe = tmp_path / "probe.py"
    marker = tmp_path / "marker"
    probe.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 2
    assert not marker.exists()
    assert len(result.stderr.strip().splitlines()) == 1
    assert "x;y" not in result.stderr


def test_non_utf8_env_file_exits_2_without_a_traceback_or_running_script(tmp_path):
    env_file = tmp_path / "worker.env"
    env_file.write_bytes(b"A=1\nB=\xff\xfe not utf-8\n")
    probe = tmp_path / "probe.py"
    marker = tmp_path / "marker"
    probe.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 2
    assert not marker.exists()
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.stderr
    assert str(env_file) in result.stderr


def test_unsafe_env_file_line_exits_2_without_running_script_or_leaking_a_value(tmp_path):
    env_file = write_env_file(tmp_path / "worker.env", {"SUBSTRATE_API_KEY": FIXTURE_KEY + "$evil"})
    probe = tmp_path / "probe.py"
    marker = tmp_path / "marker"
    probe.write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")

    result = run_launcher(["--env-file", str(env_file), str(probe)])

    assert result.returncode == 2
    assert not marker.exists()
    assert FIXTURE_KEY not in result.stderr
