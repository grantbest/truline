from datetime import date, timedelta

from workflows.historical_backfill import (
    _BACKFILL_CHUNK_DAYS,
    _INTER_CHUNK_SLEEP_SECONDS,
    _resume_cursor_from_last_completed,
)


def test_resume_cursor_restarts_at_next_chunk_boundary():
    today = date(2026, 5, 27)
    last_completed = "2025-05-28"

    cursor = _resume_cursor_from_last_completed(today, last_completed)
    next_chunk_since = cursor - timedelta(days=_BACKFILL_CHUNK_DAYS)

    assert cursor.isoformat() == last_completed
    assert next_chunk_since.isoformat() == "2025-02-27"


def test_backfill_keeps_five_second_rate_limit_guard():
    assert _INTER_CHUNK_SLEEP_SECONDS >= 5
