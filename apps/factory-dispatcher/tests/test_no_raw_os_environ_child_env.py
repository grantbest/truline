"""AC-1 (SEC-a0166920-2): no non-test module other than process_env.py builds a
child environment from `os.environ` directly.

A regex scan rather than an AST walk, deliberately -- the rule is about never
writing `dict(os.environ`, `{**os.environ` or `os.environ.copy()` at all in these
modules, not about whether a particular instance of one happens to reach a spawn
call. `launchd_agent.effective_environment` is the one named exception (AC-1):
it builds a *reported* configuration environment, validates it, and spawns
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]

_RAW_OS_ENVIRON_COPY = re.compile(r"dict\(os\.environ|\{\*\*os\.environ|os\.environ\.copy\(\)")

_EXEMPT_FILE_LINE = {
    ("launchd_agent.py", "values = dict(os.environ if environ is None else environ)"),
}


def _non_test_source_files():
    for path in sorted(_DISPATCHER_ROOT.rglob("*.py")):
        rel = path.relative_to(_DISPATCHER_ROOT)
        if rel.parts[0] == "tests":
            continue
        if path.name == "process_env.py":
            continue
        if path.name.startswith("test_"):
            continue
        yield path, rel.as_posix()


def test_no_module_other_than_process_env_copies_os_environ_directly():
    offenders = []
    for path, rel in _non_test_source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not _RAW_OS_ENVIRON_COPY.search(line):
                continue
            if (rel, line.strip()) in _EXEMPT_FILE_LINE:
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "raw os.environ copy outside process_env.py:\n" + "\n".join(offenders)
