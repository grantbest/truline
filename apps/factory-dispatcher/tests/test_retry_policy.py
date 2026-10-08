"""Pure unit tests for dispatch failure classification."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import retry_policy  # noqa: E402


def test_infrastructure_timeout_classifies_environmental_even_with_worker_result_present():
    """verify/propose/contain/scope/smoke run after `run` already put a
    worker_result on the state, so worker_result_present alone would call
    this a work failure. An infrastructure_timeout death (heartbeat_timeout
    on a heartbeating step, or start_to_close_timeout on one of the short
    non-heartbeating steps) is an infrastructure fact, not a worker verdict,
    regardless of what already happened earlier in the sequence."""
    classification = retry_policy.classify_dispatch_failure(
        "worker changed nothing",
        worker_result_present=True,
        infrastructure_timeout=True,
    )
    assert classification == retry_policy.ENVIRONMENT_FAILURE
    assert classification.consumes_retry is False


def test_infrastructure_timeout_overrides_every_other_message_based_rule():
    for reason in (
        "capacity backpressure: retry_at=Aug 7th 10:43 PM",
        f"{retry_policy.ALREADY_SATISFIED_WORK_MARKER} looks fixed already",
        "Could not start: pytest -q. Failed: (none)",
        "some ordinary failure text",
    ):
        assert (
            retry_policy.classify_dispatch_failure(
                reason, worker_result_present=True, infrastructure_timeout=True
            )
            == retry_policy.ENVIRONMENT_FAILURE
        )


def test_infrastructure_timeout_defaults_to_false_and_preserves_existing_classification():
    assert (
        retry_policy.classify_dispatch_failure(
            "worker changed nothing", worker_result_present=True
        )
        == retry_policy.WORK_FAILURE
    )
