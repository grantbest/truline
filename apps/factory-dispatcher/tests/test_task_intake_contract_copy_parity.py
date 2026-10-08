"""task-intake-contract.json must never drift between its owner and its copy.

The dispatcher owns the intake contract at apps/factory-dispatcher/contracts/
and reads it locally (file_task.py). The console's Docker build context is
apps/lifeops-console/ and its Dockerfile copies only src/ and the config
files, so a relative import resolving outside src/ would pass the clone but
fail `docker build` -- the console keeps its own committed copy at
apps/lifeops-console/src/lib/task-intake-contract.json instead. Two files
means drift is possible, so this test makes drift fail CI, the same pattern
used to guard other duplicated-by-necessity files.
"""

from __future__ import annotations

import json
from pathlib import Path

DISPATCHER_CONTRACT = (
    Path(__file__).resolve().parents[1] / "contracts" / "task-intake-contract.json"
)
CONSOLE_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "lifeops-console"
    / "src"
    / "lib"
    / "task-intake-contract.json"
)


def test_console_copy_is_byte_identical_to_dispatcher_contract():
    assert DISPATCHER_CONTRACT.read_bytes() == CONSOLE_CONTRACT.read_bytes(), (
        "apps/lifeops-console/src/lib/task-intake-contract.json has drifted from "
        "apps/factory-dispatcher/contracts/task-intake-contract.json -- the "
        "dispatcher's copy is the owner; update both together, in the same change."
    )


def test_forbidden_always_is_pinned_in_both_copies():
    """Byte-identity alone misses both copies converging on the same wrong
    list (e.g. both dropping ``.github/workflows/**``) -- pin the literal
    contents directly, in the file that owns this comparison.
    """
    for contract_path in (DISPATCHER_CONTRACT, CONSOLE_CONTRACT):
        forbidden_always = json.loads(contract_path.read_text())["forbidden_always"]
        assert forbidden_always[0] == ".github/workflows/**", contract_path
        assert "docs/releases/**" in forbidden_always, contract_path
