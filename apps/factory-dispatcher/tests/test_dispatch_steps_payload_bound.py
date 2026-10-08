"""Worker output must not be able to exceed Temporal's transport limit.

On 2026-08-07 a dispatch activity returned 4,445,196 bytes of worker output.
Temporal refuses a gRPC message over 4MiB, so the activity could not complete at
the transport layer, the bead never transitioned out of `doing`, and — because
the schedule uses SKIP overlap — the whole queue stalled behind a workflow that
could never finish.

These tests hold the bound and, just as importantly, hold that the bound does
not quietly change what a normal run reports.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
from activities import dispatch_steps  # noqa: E402


# Temporal's hard gRPC ceiling. The bound exists to stay under this with room to
# spare, so the test states the real number rather than trusting the constant.
TEMPORAL_GRPC_LIMIT_BYTES = 4 * 1024 * 1024


def _result(stdout: str, stderr: str | None = None) -> dispatch.WorkerResult:
    return dispatch.WorkerResult(
        exit_code=0,
        stdout=stdout,
        duration_s=1.0,
        timed_out=False,
        stderr=stderr,
    )


def test_output_under_the_bound_is_passed_through_untouched():
    """The common path must be byte-identical, or this fix changes every run."""
    stdout = "a normal run\nwith a few lines\n"

    state = dispatch_steps._worker_result_to_state(_result(stdout))

    assert state["stdout"] == stdout
    assert "truncated" not in state["stdout"]


def test_oversized_stdout_is_bounded_and_says_so():
    stdout = "x" * (dispatch_steps.WORKER_STREAM_STATE_LIMIT * 3)

    state = dispatch_steps._worker_result_to_state(_result(stdout))

    assert len(state["stdout"]) < len(stdout)
    # Announced, not silent: a reader must be able to tell the dispatcher cut it.
    assert "truncated by the dispatcher" in state["stdout"]
    assert str(len(stdout)) in state["stdout"]


def test_the_tail_is_kept_because_that_is_where_the_failure_is():
    stdout = ("noise\n" * 50_000) + "FINAL LINE: the assertion that failed"

    state = dispatch_steps._worker_result_to_state(_result(stdout))

    assert state["stdout"].endswith("FINAL LINE: the assertion that failed")


def test_stderr_is_bounded_on_the_same_terms():
    stderr = "e" * (dispatch_steps.WORKER_STREAM_STATE_LIMIT * 3)

    state = dispatch_steps._worker_result_to_state(_result("ok", stderr=stderr))

    assert "truncated by the dispatcher" in state["stderr"]
    assert state["stderr"].endswith("e")


def test_absent_stderr_stays_absent():
    """A missing stream is a different claim from an empty one."""
    state = dispatch_steps._worker_result_to_state(_result("ok", stderr=None))

    assert "stderr" not in state


def test_the_2026_08_07_payload_would_now_fit():
    """The regression, stated in the size that actually broke it."""
    stdout = "x" * 4_445_196

    state = dispatch_steps._worker_result_to_state(_result(stdout, stderr=stdout))

    encoded = len(state["stdout"].encode()) + len(state["stderr"].encode())
    assert encoded < TEMPORAL_GRPC_LIMIT_BYTES
    # Two orders of magnitude of headroom, so a later note-limit rise is safe.
    assert encoded < TEMPORAL_GRPC_LIMIT_BYTES // 10


def test_round_trip_preserves_the_non_stream_fields():
    """Bounding output must not disturb the fields the workflow branches on."""
    state = dispatch_steps._worker_result_to_state(
        dispatch.WorkerResult(
            exit_code=1, stdout="x" * 200_000, duration_s=12.5, timed_out=True, stderr="boom"
        )
    )
    restored = dispatch_steps._worker_result_from_state(state)

    assert restored.exit_code == 1
    assert restored.duration_s == pytest.approx(12.5)
    assert restored.timed_out is True
    assert restored.stderr == "boom"
