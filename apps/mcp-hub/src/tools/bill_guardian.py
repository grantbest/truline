"""Bill Guardian logic — Phase 8.5.

Handles auditing pending bills, transitioning them to overdue when missed,
and identifying upcoming bills for proactive alerting.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.tools.finance import (
    patch_bead_state,
    query_beads,
)

logger = logging.getLogger(__name__)


async def get_bill_guardian_summary(upcoming_days: int = 3) -> Dict[str, Any]:
    """Find upcoming and overdue bills.

    Transitions missed ``pending`` bills to ``overdue`` state in Substrate.
    Identifies bills due within ``upcoming_days`` for alerting.
    """
    # 1. Fetch all pending bills.
    params = {"type": "bill", "state": "pending", "limit": 1000}
    bills = await query_beads(params)

    today = datetime.now().date()
    upcoming: List[Dict[str, Any]] = []
    overdue: List[Dict[str, Any]] = []

    for b in bills:
        content = b.get("content", {})
        due_date_str = content.get("due_date")
        if not due_date_str:
            continue

        try:
            # Support both ISO date strings and full ISO datetimes
            due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                due_date = datetime.strptime(due_date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                logger.warning("Skipping bill %s: invalid due_date %s", b["id"], due_date_str)
                continue

        bill_summary = {
            "id": b["id"],
            "vendor": content.get("vendor", "Unknown"),
            "amount": content.get("amount", 0),
            "due_date": due_date.isoformat(),
        }

        if due_date < today:
            # 2. Missed bills: flip to overdue state.
            await patch_bead_state(
                b["id"],
                state="overdue",
                created_by="bill-guardian/audit",
            )
            overdue.append(bill_summary)
        elif today <= due_date <= today + timedelta(days=upcoming_days):
            # 3. Upcoming bills: mark for alert.
            upcoming.append(bill_summary)

    return {
        "upcoming": upcoming,
        "overdue": overdue,
    }
