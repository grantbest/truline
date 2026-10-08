"""dispatch.DEV_LANES must never drift from the intake contract's `lanes` list.

The console derives its lane picker from the same contract (see
apps/lifeops-console/src/lib/dev-board.ts / dev-board.test.ts) so a task the
dispatcher will accept is always offerable from the board, and vice versa.
Two independent lists of the same lanes means drift is possible, so this test
makes drift fail CI, the same pattern test_task_intake_contract_copy_parity.py
uses for the contract's two file copies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "task-intake-contract.json"
)


def test_dev_lanes_match_intake_contract_lanes():
    contract = json.loads(CONTRACT_PATH.read_text())
    assert tuple(contract["lanes"]) == dispatch.DEV_LANES, (
        f"contract lanes {contract['lanes']!r} != dispatch.DEV_LANES "
        f"{dispatch.DEV_LANES!r} -- update both together, in the same change."
    )


def test_contract_forbidden_always_is_pinned():
    """Pin the literal contents here too: this file reads the contract
    independently of file_task/scanner's derived ``FORBIDDEN_ALWAYS``
    constant, so a regression that reaches the contract but not those
    modules' import-time coercion is still caught.
    """
    contract = json.loads(CONTRACT_PATH.read_text())
    forbidden_always = contract["forbidden_always"]
    assert forbidden_always[0] == ".github/workflows/**"
    assert "docs/releases/**" in forbidden_always
