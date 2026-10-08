"""dev.finding 69016c99: a claude CLI session-limit exhaustion must classify
as capacity backpressure, not an ordinary work failure.

Six retry attempts were burned across two beads (dev.task f07f6b01, e1cf3515)
because `detect_worker_capacity_failure` never saw the CLI's "session limit"
prose: for a JSON-graded worker (`grade_json_result=True`,
`dispatch._grade_json_worker_result`), that prose lands in `WorkerResult.stdout`
while `WorkerResult.stderr` is always a grading message the classifier's old
`stderr is None` fallback could never reach. Every positive case below drives
`_grade_json_worker_result` with a real `subprocess.CompletedProcess`, exactly
as `run_worker` does, rather than hand-building a `WorkerResult` or passing a
bare string to `detect_capacity_failure` -- the two disagree for this worker,
and only `detect_worker_capacity_failure` is the production path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402

# The two separators observed in the wild -- AC-2 measured them identical.
SESSION_LIMIT_CENTER_DOT = (
    "You've hit your session limit · resets 7:30pm (America/Chicago)"
)
SESSION_LIMIT_HYPHEN = "You've hit your session limit - resets 7:30pm (America/Chicago)"


def _no_parseable_json_result(stdout: str) -> dispatch.WorkerResult:
    """Build the first real failure shape: the CLI printed plain prose, not
    JSON, to stdout. `_grade_json_worker_result` grades this as exit 1 with a
    grading message -- not the CLI's words -- in stderr."""
    proc = subprocess.CompletedProcess(
        args=["claude", "exec"], returncode=1, stdout=stdout, stderr=""
    )
    return dispatch._grade_json_worker_result(proc, 4.0)


def _error_during_execution_result(stdout_message: str) -> dispatch.WorkerResult:
    """Build the second real failure shape: the CLI's JSON result document
    parses with `is_error: true`, its `result` field carries the CLI's prose,
    and `_grade_json_worker_result` puts the document's `subtype` -- not the
    prose -- in stderr."""
    doc = {
        "is_error": True,
        "subtype": "error_during_execution",
        "result": stdout_message,
    }
    proc = subprocess.CompletedProcess(
        args=["claude", "exec"],
        returncode=0,
        stdout=json.dumps(doc),
        stderr="",
    )
    return dispatch._grade_json_worker_result(proc, 4.0)


# ---------------------------------------------------------------------------
# AC-1 / AC-2: both real shapes classify, with the retry hint captured.
# ---------------------------------------------------------------------------


def test_no_parseable_json_shape_classifies_as_capacity():
    result = _no_parseable_json_result(SESSION_LIMIT_CENTER_DOT)
    # Sanity on the shape itself: the CLI's words are in stdout, and stderr is
    # the grading message, not the prose -- this is exactly what makes the
    # old `stderr is None` fallback unreachable for this worker.
    assert result.stderr == "worker emitted no parseable result JSON"
    assert SESSION_LIMIT_CENTER_DOT in result.stdout

    capacity = dispatch.detect_worker_capacity_failure(result)
    assert capacity is not None
    assert capacity.cause == "usage limit exhaustion"
    assert capacity.retry_at == "7:30pm (America/Chicago)"


def test_error_during_execution_shape_classifies_as_capacity():
    result = _error_during_execution_result(SESSION_LIMIT_CENTER_DOT)
    assert result.exit_code == 1
    assert result.stderr == "error_during_execution"
    assert SESSION_LIMIT_CENTER_DOT in result.stdout

    capacity = dispatch.detect_worker_capacity_failure(result)
    assert capacity is not None
    assert capacity.cause == "usage limit exhaustion"
    assert capacity.retry_at == "7:30pm (America/Chicago)"


def test_hyphen_separator_classifies_identically_to_center_dot():
    result = _error_during_execution_result(SESSION_LIMIT_HYPHEN)
    capacity = dispatch.detect_worker_capacity_failure(result)
    assert capacity is not None
    assert capacity.retry_at == "7:30pm (America/Chicago)"


def test_bare_usage_limit_wording_still_classifies():
    """The pre-existing "usage limit" wording must keep working -- this is a
    widening, not a replacement."""
    result = _error_during_execution_result(
        "You've hit your usage limit. Please try again at Aug 7th 10:43 PM."
    )
    capacity = dispatch.detect_worker_capacity_failure(result)
    assert capacity is not None
    assert capacity.retry_at == "Aug 7th 10:43 PM"


# ---------------------------------------------------------------------------
# AC-3: negative controls, through the same production entry point.
# ---------------------------------------------------------------------------


def test_mid_line_quote_of_the_full_message_does_not_classify():
    """The control that can actually fail: a single line that quotes the CLI
    message IN FULL -- including its "resets" clause -- inside other text.
    A widening that shared RETRY_AT_PATTERN/_is_retry_line with "resets"
    would make this classify (measured); this asserts it does not."""
    assertion_line = (
        f'AssertionError: expected "{SESSION_LIMIT_CENTER_DOT}" in captured output'
    )
    result = dispatch.WorkerResult(
        exit_code=1,
        stdout=assertion_line,
        duration_s=1.0,
        timed_out=False,
        stderr="worker emitted no parseable result JSON",
    )
    assert dispatch.detect_worker_capacity_failure(result) is None


def test_mid_line_pytest_failed_line_does_not_classify():
    failed_line = (
        f"FAILED tests/test_x.py::test_y - AssertionError: {SESSION_LIMIT_HYPHEN}"
    )
    result = dispatch.WorkerResult(
        exit_code=1,
        stdout=failed_line,
        duration_s=1.0,
        timed_out=False,
        stderr="worker emitted no parseable result JSON",
    )
    assert dispatch.detect_worker_capacity_failure(result) is None


def test_multiline_transcript_quoting_the_wording_does_not_classify():
    transcript = "\n".join(
        [
            "Investigating rate-limit handling.",
            "A fixture contains this provider sentence:",
            f"+{SESSION_LIMIT_CENTER_DOT}",
            "pytest failed: assertion error",
        ]
    )
    result = dispatch.WorkerResult(
        exit_code=1,
        stdout=transcript,
        duration_s=1.0,
        timed_out=False,
        stderr="worker emitted no parseable result JSON",
    )
    assert dispatch.detect_worker_capacity_failure(result) is None


def test_ordinary_failure_with_unrelated_output_stays_ordinary():
    result = dispatch.WorkerResult(
        exit_code=1,
        stdout="pytest failed: assertion error",
        duration_s=1.0,
        timed_out=False,
        stderr="worker emitted no parseable result JSON",
    )
    assert dispatch.detect_worker_capacity_failure(result) is None


# ---------------------------------------------------------------------------
# AC-2: retry-hint extraction must not widen line eligibility.
# ---------------------------------------------------------------------------


def test_resets_extraction_does_not_touch_shared_retry_line_pattern():
    """RETRY_AT_PATTERN and _is_retry_line are untouched by the "resets"
    widening -- only _matches_capacity_pattern gained a second lookup."""
    assert dispatch.RETRY_AT_PATTERN.search("resets 7:30pm (America/Chicago)") is None
    assert not dispatch._is_retry_line("resets 7:30pm (America/Chicago)")
