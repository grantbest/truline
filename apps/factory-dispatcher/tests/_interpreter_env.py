"""Shared helper: the minimal subprocess exec environment these process_env tests use.

PATH and HOME come from the test process. LD_LIBRARY_PATH, DYLD_LIBRARY_PATH and
SYSTEMROOT are passed through only when the test process itself has them set --
this is the interpreter's own loader input, not a credential, and never widens what
the test proves. Without it, CI's setup-python interpreter (which finds its shared
library only through LD_LIBRARY_PATH) fails every subprocess with "error while
loading shared libraries" before the test's own assertions run (see the identical
`_INTERPRETER_PASSTHROUGH` pattern in scripts/tests/test_csdm_publish_*.py, #794).
Never add a credential-named variable here.
"""

from __future__ import annotations

import os

_INTERPRETER_PASSTHROUGH = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "SYSTEMROOT")


def minimal_exec_environment(**extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ["HOME"],
        **extra,
    }
    for name in _INTERPRETER_PASSTHROUGH:
        if name in os.environ:
            env[name] = os.environ[name]
    return env
