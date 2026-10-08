"""Tests for the release-health policy model and its loader.

No store, cluster or network: everything here reads
``docs/releases/policy/health-policy.json`` straight off disk, or a copy of
its content mutated in memory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import health_policy  # noqa: E402

COMMITTED_BYTES = health_policy.HEALTH_POLICY_PATH.read_bytes()
COMMITTED_CONTENT = json.loads(COMMITTED_BYTES)


def _base() -> dict:
    return copy.deepcopy(COMMITTED_CONTENT)


def _blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def test_committed_file_validates():
    policy, revision = health_policy.load_health_policy(health_policy.HEALTH_POLICY_PATH)
    assert isinstance(policy, health_policy.HealthPolicy)
    assert revision == _blob_sha(COMMITTED_BYTES)


def test_revision_matches_git_hash_object():
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    result = subprocess.run(
        [git, "hash-object", str(health_policy.HEALTH_POLICY_PATH)],
        capture_output=True,
        text=True,
        check=True,
    )
    _, revision = health_policy.load_health_policy(health_policy.HEALTH_POLICY_PATH)
    assert revision == result.stdout.strip()


def test_missing_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        health_policy.load_health_policy(REPO / "docs" / "releases" / "policy" / "nope.json")


def test_malformed_json_raises_value_error(tmp_path):
    bad = tmp_path / "health-policy.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        health_policy.load_health_policy(bad)


def test_balance_tolerance_pct_150_is_refused_naming_the_key():
    content = _base()
    content["balance_tolerance_pct"] = 150
    message = health_policy.health_policy_validation_error(content)
    assert message is not None
    assert "balance_tolerance_pct" in message
    assert message.startswith("health-policy.json: ")


def test_unknown_key_is_refused():
    content = _base()
    content["unknown_key"] = 1
    assert health_policy.health_policy_validation_error(content) is not None
    with pytest.raises(ValidationError):
        health_policy.HealthPolicy.model_validate(content)


def test_medium_gte_high_is_refused():
    content = _base()
    content["urgency_bands"] = {"medium": 0.7, "high": 0.7}
    with pytest.raises(ValidationError):
        health_policy.HealthPolicy.model_validate(content)


def test_valid_name_passed_through_the_message():
    content = _base()
    content["balance_tolerance_pct"] = 150
    message = health_policy.health_policy_validation_error(content, name="/tmp/some-copy.json")
    assert message.startswith("/tmp/some-copy.json: ")


RANGE_REFUSALS = [
    ("urgency_bands.medium==0", {"urgency_bands": {"medium": 0.0, "high": 0.66}}),
    ("urgency_bands.high>1", {"urgency_bands": {"medium": 0.33, "high": 1.01}}),
    ("urgency_bands.medium==high", {"urgency_bands": {"medium": 0.5, "high": 0.5}}),
    ("aging_days<=0", {"aging_days": 0}),
    ("aging_days float", {"aging_days": 14.0}),
    ("aging_days string", {"aging_days": "14"}),
    ("aging_days bool", {"aging_days": True}),
    ("max_aging_steps<0", {"max_aging_steps": -1}),
    ("max_aging_steps>3", {"max_aging_steps": 4}),
    ("max_aging_steps float", {"max_aging_steps": 3.0}),
    ("max_aging_steps string", {"max_aging_steps": "3"}),
    ("max_aging_steps bool", {"max_aging_steps": True}),
    ("balance_tolerance_pct<0", {"balance_tolerance_pct": -1}),
    ("balance_tolerance_pct>100", {"balance_tolerance_pct": 101}),
    ("absent_after_elapsed_pct<=0", {"absent_after_elapsed_pct": 0}),
    ("absent_after_elapsed_pct>1", {"absent_after_elapsed_pct": 1.01}),
    ("no_landed_work_after_elapsed_pct<=0", {"no_landed_work_after_elapsed_pct": 0}),
    ("no_landed_work_after_elapsed_pct>1", {"no_landed_work_after_elapsed_pct": 1.01}),
    ("criterion_stale_days<=0", {"criterion_stale_days": 0}),
    ("min_classified<1", {"min_classified": 0}),
    ("rework_rate_pct<0", {"rework_rate_pct": -1}),
    ("rework_rate_pct>100", {"rework_rate_pct": 101}),
    ("unmeasured_actionable_after_hours<=0", {"unmeasured_actionable_after_hours": 0}),
    ("re_alert_interval_hours<=0", {"re_alert_interval_hours": 0}),
    ("reconciler_interval_seconds<=0", {"reconciler_interval_seconds": 0}),
    ("dispatch_interval_seconds<=0", {"dispatch_interval_seconds": 0}),
]


@pytest.mark.parametrize("label,override", RANGE_REFUSALS, ids=[r[0] for r in RANGE_REFUSALS])
def test_ranges(label, override):
    content = _base()
    content.update(override)
    with pytest.raises(ValidationError):
        health_policy.HealthPolicy.model_validate(content)


ACCEPTED_BOUNDARIES = [
    ("urgency_bands.high==1.0", {"urgency_bands": {"medium": 0.33, "high": 1.0}}),
    ("max_aging_steps==0", {"max_aging_steps": 0}),
    ("max_aging_steps==3", {"max_aging_steps": 3}),
    ("balance_tolerance_pct==0", {"balance_tolerance_pct": 0}),
    ("balance_tolerance_pct==100", {"balance_tolerance_pct": 100}),
    ("absent_after_elapsed_pct==1", {"absent_after_elapsed_pct": 1.0}),
    ("no_landed_work_after_elapsed_pct==1", {"no_landed_work_after_elapsed_pct": 1.0}),
    ("min_classified==1", {"min_classified": 1}),
    ("rework_rate_pct==0", {"rework_rate_pct": 0}),
    ("rework_rate_pct==100", {"rework_rate_pct": 100}),
]


@pytest.mark.parametrize(
    "label,override", ACCEPTED_BOUNDARIES, ids=[b[0] for b in ACCEPTED_BOUNDARIES]
)
def test_accepted_boundaries(label, override):
    content = _base()
    content.update(override)
    health_policy.HealthPolicy.model_validate(content)
