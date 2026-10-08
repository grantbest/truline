"""dev.finding a0166920's central claim, measured directly: a value the launcher
loads into os.environ AFTER exec is absent from the process's EXEC-TIME environment
(what KERN_PROCARGS2 / /proc/<pid>/environ expose to any same-uid reader), even
though the process's own os.environ holds it.

The probe here runs IN THE LAUNCHED PYTHON PROCESS ITSELF -- not a further exec'd
child -- because a platform binary such as /bin/sleep exposes no environment through
KERN_PROCARGS2 even unsandboxed (nuance N2). No fixture value appears in the probe's
argv, script path, or any path passed to it: KERN_PROCARGS2 and /proc/<pid>/cmdline
include argv, so a `python -c '<source containing a fixture>'` probe would read as a
false positive (this is what PR 1038's release gate reproduced). The probe is instead
a script FILE that takes only a ready-file path and a variable NAME on argv.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _interpreter_env import minimal_exec_environment  # noqa: E402
from exec_environ_probe import read_exec_environment  # noqa: E402

FACTORY_DISPATCHER_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = FACTORY_DISPATCHER_DIR / "process_env.py"

PROBE_SOURCE = """\
import json
import os
import sys
import time

ready_file, name = sys.argv[1], sys.argv[2]
with open(ready_file, "w") as fh:
    json.dump({"held": name in os.environ}, fh)
while True:
    time.sleep(3600)
"""

FIXTURE_KEY = "fixture-key-value-" + "k" * 20
CONTROL_MARKER = "control-marker-value-" + "m" * 20


def _write_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE_SOURCE, encoding="utf-8")
    return probe


def _wait_for_ready(ready_file: Path, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready_file.exists():
            try:
                return json.loads(ready_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    raise AssertionError(f"probe never became ready: {ready_file}")


def _exec_time_environment_or_skip(pid: int, control_marker: str) -> str:
    """The process's exec-time environment joined into one string, or a test skip
    if it cannot be read or the control marker itself isn't visible in it (which
    would make the read method untrustworthy rather than the property false)."""
    exec_env = read_exec_environment(pid)
    if exec_env is None:
        pytest.skip("could not read the process's exec-time environment on this platform")
    joined = "\n".join(exec_env)
    control_present = control_marker in joined
    if not control_present:
        pytest.skip(
            "exec-time environment read did not even show the control marker; "
            "the read method itself is not trustworthy here"
        )
    return joined


def test_env_file_value_absent_from_exec_time_environment_but_present_in_os_environ(
    tmp_path,
):
    exec_environ = minimal_exec_environment(CONTROL_MARKER=CONTROL_MARKER)
    env_file = tmp_path / "worker.env"
    env_file.write_text(f"SUBSTRATE_API_KEY={FIXTURE_KEY}\n", encoding="utf-8")
    probe = _write_probe(tmp_path)
    ready_file = tmp_path / "ready.json"

    proc = subprocess.Popen(
        [
            sys.executable,
            str(LAUNCHER),
            "--env-file",
            str(env_file),
            str(probe),
            str(ready_file),
            "SUBSTRATE_API_KEY",
        ],
        cwd=str(FACTORY_DISPATCHER_DIR),
        env=exec_environ,
    )
    try:
        payload = _wait_for_ready(ready_file)
        joined = _exec_time_environment_or_skip(proc.pid, CONTROL_MARKER)

        assert FIXTURE_KEY not in joined
        assert payload["held"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_nuance_n1_a_name_already_in_the_exec_environment_keeps_its_original_value(
    tmp_path,
):
    """A name ALREADY PRESENT in the exec environment keeps its ORIGINAL value visible
    after os.environ[name] = ... -- libc does not overwrite exec-time strings in place.
    This is expected behaviour (it is why AC-10 requires `env -i` for hand restarts),
    not a failure of the design."""
    fixture_a = "fixture-A-already-in-exec-" + "a" * 20
    fixture_b = "fixture-B-from-env-file-" + "b" * 20
    exec_environ = minimal_exec_environment(
        CONTROL_MARKER=CONTROL_MARKER,
        SUBSTRATE_API_KEY=fixture_a,
    )
    env_file = tmp_path / "worker.env"
    env_file.write_text(f"SUBSTRATE_API_KEY={fixture_b}\n", encoding="utf-8")
    probe = _write_probe(tmp_path)
    ready_file = tmp_path / "ready.json"

    proc = subprocess.Popen(
        [
            sys.executable,
            str(LAUNCHER),
            "--env-file",
            str(env_file),
            str(probe),
            str(ready_file),
            "SUBSTRATE_API_KEY",
        ],
        cwd=str(FACTORY_DISPATCHER_DIR),
        env=exec_environ,
    )
    try:
        _wait_for_ready(ready_file)
        joined = _exec_time_environment_or_skip(proc.pid, CONTROL_MARKER)

        assert fixture_a in joined
    finally:
        proc.terminate()
        proc.wait(timeout=5)
