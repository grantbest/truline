"""`scripts/health_policy.py` loaded by path, the way
`activities/release_status.py:53` loads `scripts/release-status.py` -- the
filename is not a valid module name, so a same-directory consumer
(`scripts/release-load.py --check`, B12b) imports it as `health_policy` via
`importlib.util.spec_from_file_location`. This file proves that loading path
works and that the dispatcher's own ranker, `queue_order.RankPolicy`, agrees
with `HealthPolicy` on every case the ranker cares about -- so a policy file
`release-load.py --check` accepts can never be one `queue_order` then
refuses on its next tick.

No store, cluster or network: everything here reads
``docs/releases/policy/health-policy.json`` straight off disk, or a copy of
its content mutated in memory.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))

import queue_order  # noqa: E402


def _load_health_policy_module():
    cached = sys.modules.get("health_policy")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "health_policy", _REPO_ROOT / "scripts" / "health_policy.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


health_policy = _load_health_policy_module()

COMMITTED_CONTENT = json.loads(health_policy.HEALTH_POLICY_PATH.read_bytes())


def _base() -> dict:
    return copy.deepcopy(COMMITTED_CONTENT)


def test_module_loads_by_path_under_the_shared_name():
    assert sys.modules["health_policy"] is health_policy


def test_committed_file_validates_by_path():
    policy, revision = health_policy.load_health_policy(health_policy.HEALTH_POLICY_PATH)
    assert isinstance(policy, health_policy.HealthPolicy)
    assert revision


def test_committed_file_accepted_by_rank_policy_too():
    rank_policy = queue_order.RankPolicy.from_mapping(COMMITTED_CONTENT)
    assert rank_policy.aging_days == COMMITTED_CONTENT["aging_days"]
    health_policy.HealthPolicy.model_validate(COMMITTED_CONTENT)


# Every case below is one `RankPolicy.from_mapping`-relevant override. Both
# `RankPolicy.from_mapping` and `HealthPolicy` must refuse it, or accept it --
# a disagreement in either direction is exactly what this test exists to
# catch (a policy `release-load.py --check` passes that the ranker then
# refuses on its next tick).
REFUSAL_CASES = [
    ("high==1.01", {"urgency_bands": {"medium": 0.33, "high": 1.01}}),
    ("medium==high", {"urgency_bands": {"medium": 0.5, "high": 0.5}}),
    ("medium==0", {"urgency_bands": {"medium": 0.0, "high": 0.66}}),
    ("max_aging_steps==4", {"max_aging_steps": 4}),
    ("max_aging_steps==-1", {"max_aging_steps": -1}),
    ("aging_days==0", {"aging_days": 0}),
    ("aging_days=='14'", {"aging_days": "14"}),
    ("aging_days==14.0", {"aging_days": 14.0}),
    ("aging_days==True", {"aging_days": True}),
]


@pytest.mark.parametrize("label,override", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES])
def test_rank_policy_and_health_policy_agree_on_refusals(label, override):
    content = _base()
    content.update(override)

    with pytest.raises(ValueError):
        queue_order.RankPolicy.from_mapping(content)

    with pytest.raises(ValidationError):
        health_policy.HealthPolicy.model_validate(content)


ACCEPT_CASES = [
    ("high==1.0", {"urgency_bands": {"medium": 0.33, "high": 1.0}}),
    ("max_aging_steps==0", {"max_aging_steps": 0}),
    ("max_aging_steps==3", {"max_aging_steps": 3}),
]


@pytest.mark.parametrize("label,override", ACCEPT_CASES, ids=[c[0] for c in ACCEPT_CASES])
def test_rank_policy_and_health_policy_agree_on_acceptances(label, override):
    content = _base()
    content.update(override)

    queue_order.RankPolicy.from_mapping(content)
    health_policy.HealthPolicy.model_validate(content)
