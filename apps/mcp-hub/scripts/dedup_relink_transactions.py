"""One-off cleanup for re-link duplicate transactions (2026-06-12).

The 2026-06-11 re-links replayed each institution's history under new
plaid_transaction_ids, duplicating ~150 transactions (different ptid,
identical account/amount/date/description). This script groups
transaction beads by re-link-stable fingerprint, keeps the best bead
per group, and deletes the re-imports.

Keeper choice within a duplicate group:
  1. a bead that is reconciled or carries parent_id (it holds links the
     others don't); if MORE than one such bead exists the group is
     flagged for manual review and skipped,
  2. otherwise the oldest created_at.

DRY-RUN by default — prints the plan. Pass --apply to delete.

Run from inside the mcp-hub pod (proxy injects the Substrate key):

    kubectl exec -n platform-mcp-prod deploy/mcp-hub -- \
        python scripts/dedup_relink_transactions.py [--apply]

Requires the substrate DELETE fix (bead_events cascade) deployed first.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "http://localhost:8000/substrate"


def fetch_transactions() -> list[dict]:
    params = urllib.parse.urlencode(
        {"namespace": "finance", "type": "transaction", "limit": 5000}
    )
    with urllib.request.urlopen(f"{BASE}/beads?{params}", timeout=60) as resp:
        return json.loads(resp.read())


def fingerprint(bead: dict) -> tuple:
    c = bead.get("content") or {}
    return (
        str(c.get("account_id")),
        f"{float(c.get('amount') or 0.0):.2f}",
        str(c.get("posted_date") or ""),
        (c.get("description") or c.get("merchant_name") or "").strip().lower(),
    )


def is_linked(bead: dict) -> bool:
    return bead.get("state") == "reconciled" or bead.get("parent_id") is not None


def plan(beads: list[dict]) -> tuple[list[dict], list[tuple]]:
    groups = defaultdict(list)
    for b in beads:
        if b.get("state") == "removed":
            continue
        groups[fingerprint(b)].append(b)

    losers: list[dict] = []
    flagged: list[tuple] = []
    for fp, group in groups.items():
        if len(group) < 2:
            continue
        ptids = {(b.get("content") or {}).get("plaid_transaction_id") for b in group}
        if len(ptids) < 2:
            # Same ptid twice would mean the DB unique index failed — out
            # of scope here, surface loudly.
            flagged.append((fp, "same plaid_transaction_id duplicated"))
            continue
        linked = [b for b in group if is_linked(b)]
        if len(linked) > 1:
            flagged.append((fp, f"{len(linked)} beads carry reconciliation links"))
            continue
        keeper = linked[0] if linked else min(group, key=lambda b: b["created_at"])
        losers.extend(b for b in group if b["id"] != keeper["id"])
    return losers, flagged


def delete_bead(bead_id: str) -> None:
    req = urllib.request.Request(f"{BASE}/beads/{bead_id}", method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
        assert body.get("status") == "deleted", body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete")
    args = parser.parse_args()

    beads = fetch_transactions()
    losers, flagged = plan(beads)

    by_inst: dict[str, int] = defaultdict(int)
    for b in losers:
        by_inst[(b.get("content") or {}).get("institution") or "?"] += 1

    print(f"transaction beads: {len(beads)}")
    print(f"duplicate beads to delete: {len(losers)}  {dict(by_inst)}")
    for fp, reason in flagged:
        print(f"FLAGGED (manual review, skipped): {fp} — {reason}")
    for b in sorted(losers, key=lambda b: fingerprint(b)):
        c = b.get("content") or {}
        print(
            f"  DELETE {b['id']}  {c.get('institution')}  {c.get('posted_date')}  "
            f"${c.get('amount')}  {c.get('merchant_name')}  created={b['created_at'][:19]}"
        )

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to delete.")
        return 0

    failed = 0
    for b in losers:
        try:
            delete_bead(b["id"])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAILED {b['id']}: {exc}", file=sys.stderr)
    print(f"deleted {len(losers) - failed}/{len(losers)} (failed: {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
