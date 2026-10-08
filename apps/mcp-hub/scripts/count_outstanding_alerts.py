"""One-off measurement: how many delivered alerts are outstanding right now.

Written to answer R26.04/O-2's "eighteen unwatched backup pages" claim with an
actual query rather than a number carried forward on say-so — see
docs/plans (2026-08-30 decision record) and the OPS-43 task for why this
does not wait for R26.04.

Requires SUBSTRATE_URL / SUBSTRATE_API_KEY in the environment. Run from the
repo root:

    python apps/mcp-hub/scripts/count_outstanding_alerts.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.notify import load_finance_alert_states, outstanding_alerts  # noqa: E402


async def main() -> None:
    states = await load_finance_alert_states()
    outstanding = outstanding_alerts(states)

    print(f"delivered alert records: {len(states)}")
    print(f"outstanding (delivered, not acknowledged): {len(outstanding)}")
    for alert in sorted(outstanding, key=lambda a: str(a.get("last_posted_at"))):
        print(
            f"  - {alert.get('kind')} delivered {alert.get('last_posted_at')} "
            f"(id={alert.get('id')})"
        )


if __name__ == "__main__":
    asyncio.run(main())
