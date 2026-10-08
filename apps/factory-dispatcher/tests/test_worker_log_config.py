"""The worker log must carry a timestamp and drop httpx's per-request noise.

An audit of the worker's log found 1,894 Temporal tunnel-loss lines with no
timestamp on any of them -- `logging.basicConfig(level=logging.INFO)` took the
stdlib default format ('LEVEL:name:message') and handed httpx an INFO line per
HTTP request into the same stream, which was two thirds of the volume. Neither
"when did this happen" nor "how often does it recur" could be answered from
the log alone, and reconstructing time from file mtimes is not an answer.

These tests assert on the logging system's ACTUAL configured state after
importing `worker`, never on a formatter the test builds for itself. The first
attempt at this bead did the latter: every assertion passed while the
production configuration was inert, because `logging.basicConfig` does nothing
-- silently, no error -- when the root logger already has a handler, and
`worker.py`'s own `from activities import ACTIVITIES` reaches
`cluster_health.py`, which calls `basicConfig` at import. A test that builds
its own formatter cannot see that, and three of four passed with every line of
production configuration deleted.

No Temporal server, no substrate, no network.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402

#: The Z designator is the point: a bare local time is not unambiguous.
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


@pytest.fixture
def reconfigured_worker():
    """Re-run the worker's module-level logging setup under the production
    hazard -- a root handler already installed by an earlier import -- and put
    the test runner's own logging back afterwards.

    `basicConfig(force=True)` removes existing handlers, pytest's included, so
    this fixture is what keeps the reload from taking the rest of the suite's
    log capture with it.
    """
    saved_handlers = list(logging.root.handlers)
    saved_level = logging.root.level
    saved_converter = logging.Formatter.converter
    saved_httpx = logging.getLogger("httpx").level
    saved_httpcore = logging.getLogger("httpcore").level

    # The decoy: exactly what cluster_health.py's import-time basicConfig leaves
    # behind. Without force=True the worker's configuration silently loses to it.
    logging.root.handlers = [logging.StreamHandler()]
    logging.root.handlers[0].setFormatter(
        logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    )

    # Call the real production configuration -- NOT importlib.reload(worker),
    # which re-runs the workflow decorators and breaks Temporal's registration
    # identity for every other test holding a reference to those classes.
    worker.configure_logging()
    yield worker

    logging.root.handlers = saved_handlers
    logging.root.setLevel(saved_level)
    logging.Formatter.converter = saved_converter
    logging.getLogger("httpx").setLevel(saved_httpx)
    logging.getLogger("httpcore").setLevel(saved_httpcore)


def _render(message: str = "Temporal tunnel lost") -> str:
    """Format a record through whatever handler the root logger ACTUALLY has."""
    handler = logging.root.handlers[0]
    record = logging.LogRecord(
        name="worker", level=logging.INFO, pathname="worker.py", lineno=1,
        msg=message, args=None, exc_info=None,
    )
    return handler.format(record)


def test_the_configured_root_handler_stamps_utc_even_when_a_handler_already_existed(
    reconfigured_worker,
):
    """AC1, against the real handler. The decoy handler installed by the fixture
    is the production condition; if `force=True` were dropped, basicConfig would
    no-op and this renders 'INFO:worker:Temporal tunnel lost' with no date --
    which is exactly what the worker emitted before this fix."""
    rendered = _render()

    assert UTC_TIMESTAMP.match(rendered), rendered
    assert rendered.endswith("INFO worker: Temporal tunnel lost")
    # A log stamped in local time cannot be diffed against the bead timestamps
    # the same incident is reconstructed from, so UTC is not a preference here.
    assert logging.root.handlers[0].formatter.converter is time.gmtime


def test_the_worker_configuration_wins_over_an_earlier_basicconfig(reconfigured_worker):
    """Pins force=True specifically. The fixture leaves the stdlib default format
    in place first; the assertion is that the worker's format replaced it rather
    than being silently discarded."""
    handler = logging.root.handlers[0]

    assert handler.formatter._fmt == worker.LOG_FORMAT
    assert handler.formatter.datefmt == worker.LOG_DATE_FORMAT
    assert "%(asctime)s" in handler.formatter._fmt
    # force=True replaces rather than appends, so a reconfiguration cannot start
    # double-printing every line. Counted by formatter rather than by total
    # handler count: pytest re-installs its own capture handlers after
    # basicConfig clears them, and asserting on the total would pin the test
    # runner's behaviour instead of the worker's.
    worker_handlers = [
        h
        for h in logging.root.handlers
        if getattr(h.formatter, "_fmt", None) == worker.LOG_FORMAT
    ]
    assert len(worker_handlers) == 1


def test_http_client_chatter_is_quieted_without_raising_the_root(reconfigured_worker):
    """AC2 and AC3 together, read off the live logger tree rather than constants.
    Raising the root level would also silence the worker's own INFO lines, which
    is the fix the bead explicitly forbids."""
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    # Children inherit rather than needing to be listed one by one.
    assert logging.getLogger("httpcore.http11").getEffectiveLevel() == logging.WARNING

    assert logging.root.level == logging.INFO
    assert logging.getLogger("worker").getEffectiveLevel() == logging.INFO
