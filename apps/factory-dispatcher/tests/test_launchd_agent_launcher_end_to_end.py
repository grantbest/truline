"""AC-6(d): the RENDERED worker shell command, run for real under `/bin/sh -c`, never
puts the env file's credential into the worker's exec-time environment.

Builds a fake checkout under tmp holding a copy of process_env.py and a stub
worker.py, renders launchd_agent.shell_command() for that checkout, runs it with
`/bin/sh -c` under a minimal exec environment (PATH, HOME, a CONTROL_MARKER), and
applies the same exec-time-environment read AC-5 uses to the stub's pid -- `exec` in
the rendered command makes the stub's python the shell's own pid, so reading the
shell's pid IS reading the worker's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launchd_agent  # noqa: E402
from _interpreter_env import minimal_exec_environment  # noqa: E402
from exec_environ_probe import read_exec_environment  # noqa: E402

FACTORY_DISPATCHER_DIR = Path(__file__).resolve().parents[1]

STUB_WORKER_SOURCE = """\
import json
import os
import sys
import time

ready_file = sys.argv[1] if len(sys.argv) > 1 else "ready.json"
with open(ready_file, "w") as fh:
    json.dump({"held": "SUBSTRATE_API_KEY" in os.environ, "pid": os.getpid()}, fh)
while True:
    time.sleep(3600)
"""

FIXTURE_KEY = "fixture-write-key-" + "k" * 20
CONTROL_MARKER = "fixture-control-marker-" + "m" * 20


def _wait_for_ready(ready_file: Path, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready_file.exists():
            try:
                return json.loads(ready_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"stub worker never became ready: {ready_file}")


def test_rendered_worker_command_keeps_the_credential_out_of_the_exec_environment(
    tmp_path,
):
    fake_repo = tmp_path / "checkout"
    (fake_repo / "apps" / "factory-dispatcher").mkdir(parents=True)
    shutil.copyfile(
        FACTORY_DISPATCHER_DIR / "process_env.py",
        fake_repo / "apps" / "factory-dispatcher" / "process_env.py",
    )
    ready_file = tmp_path / "ready.json"
    (fake_repo / "apps" / "factory-dispatcher" / "worker.py").write_text(
        STUB_WORKER_SOURCE, encoding="utf-8"
    )

    env_file = tmp_path / "worker.env"
    env_file.write_text(f"SUBSTRATE_API_KEY={FIXTURE_KEY}\n", encoding="utf-8")

    config = launchd_agent.LaunchdConfig(
        repo_root=fake_repo,
        env_file=env_file,
        python=Path(sys.executable),
    )
    command = launchd_agent.shell_command(config)
    assert "process_env.py" in command
    assert "set -a" not in command

    # worker.py normally has no argv, but the rendered command form is
    # `... apps/factory-dispatcher/worker.py`; append the ready-file path so the
    # stub can report back without any fixture value on its own argv or path.
    full_command = f"{command} {ready_file}"

    exec_environ = minimal_exec_environment(CONTROL_MARKER=CONTROL_MARKER)
    proc = subprocess.Popen(["/bin/sh", "-c", full_command], env=exec_environ)
    try:
        payload = _wait_for_ready(ready_file)
        stub_pid = payload["pid"]

        exec_env = read_exec_environment(stub_pid)
        if exec_env is None:
            import pytest

            pytest.skip("could not read the stub worker's exec-time environment")
        joined = "\n".join(exec_env)
        if CONTROL_MARKER not in joined:
            import pytest

            pytest.skip("exec-time environment read did not show the control marker")

        assert FIXTURE_KEY not in joined
        assert payload["held"] is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
