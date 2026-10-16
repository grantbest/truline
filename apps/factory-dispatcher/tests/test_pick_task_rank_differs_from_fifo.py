"""R26.12 B8 AC-1/AC-8: in rank mode ``dispatch.pick_task`` returns the
rank's first pick, and that pick is not the oldest-created bead.

The fixtures the spec names give rank and FIFO the same first pick, so a
mutation returning the created_at pick in rank mode survived them (found at
the release gate, 2026-10-09). This constructs the two-bead case where the
two orders disagree: the bead created later became claimable earlier, so
rank (waiting_since from the injected claimable_since) puts it first while
FIFO (created_at) puts the other first. Same priority tier for both, so
only waiting_since decides. No substrate, no network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch  # noqa: E402
import queue_order  # noqa: E402

from test_claim_atomicity import FakeSubstrate, make_task  # noqa: E402

POLICY = queue_order.RankPolicy(urgency_band_medium=0.33, urgency_band_high=0.66, aging_days=14)


def _two_beads_where_rank_and_fifo_disagree():
    older_created = make_task("created-first", "2026-08-01T00:00:00Z")
    newer_created = make_task("created-second", "2026-08-02T00:00:00Z")
    claimable_since = {
        # The older bead was blocked for a long time; the newer one has been
        # claimable since before it. Same urgency and impact for both, so
        # the rank key reaches waiting_since (two days apart) whether or not
        # the wall clock has moved one of them across a 14-day aging step;
        # nothing here asserts on the clock-dependent effective_priority.
        "created-first": datetime(2026, 10, 5, tzinfo=timezone.utc),
        "created-second": datetime(2026, 10, 3, tzinfo=timezone.utc),
    }
    return [older_created, newer_created], claimable_since


def _pick(mode, tasks, claimable_since, order_out=None):
    sub = FakeSubstrate(tasks)
    return dispatch.pick_task(
        sub,
        None,
        tasks,
        claimable_since_by_task_id=claimable_since,
        time_box_by_release_ref={},
        policy=POLICY,
        mode=mode,
        order_out=order_out,
    )


def test_rank_mode_picks_the_rank_first_not_the_oldest_created():
    tasks, claimable_since = _two_beads_where_rank_and_fifo_disagree()
    order_out: list = []

    picked = _pick("rank", tasks, claimable_since, order_out)

    assert picked is not None
    assert picked["id"] == "created-second"
    assert order_out[0].mode == "rank"
    assert order_out[0].fallback_reason is None
    assert [p.task_id for p in order_out[0].ranked] == ["created-second", "created-first"]


def test_fifo_mode_picks_the_oldest_created_on_the_same_inputs():
    tasks, claimable_since = _two_beads_where_rank_and_fifo_disagree()
    order_out: list = []

    picked = _pick("fifo", tasks, claimable_since, order_out)

    assert picked is not None
    assert picked["id"] == "created-first"
    assert order_out[0].mode == "fifo"
    assert [p.task_id for p in order_out[0].ranked] == ["created-first", "created-second"]
