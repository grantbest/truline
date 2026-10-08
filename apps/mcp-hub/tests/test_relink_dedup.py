"""Re-link duplicate defense (fingerprint multiset) — workflows/bank_sync."""

import json

import pytest

from src.workflows.bank_sync import (
    fetch_existing_fingerprint_counts_activity,
    transaction_fingerprint,
)


def test_fingerprint_is_stable_across_relink_artifacts():
    # Same charge, different plaid ids / merchant-name casing → same print.
    a = transaction_fingerprint("acct-1", 157.86, "2026-05-31", "FAT ROSIE'S TACO  ")
    b = transaction_fingerprint("acct-1", 157.860, "2026-05-31", "fat rosie's taco")
    assert a == b


def test_fingerprint_distinguishes_real_differences():
    base = ("acct-1", 157.86, "2026-05-31", "fat rosie's")
    assert transaction_fingerprint(*base) != transaction_fingerprint(
        "acct-2", 157.86, "2026-05-31", "fat rosie's"
    )
    assert transaction_fingerprint(*base) != transaction_fingerprint(
        "acct-1", 157.87, "2026-05-31", "fat rosie's"
    )
    assert transaction_fingerprint(*base) != transaction_fingerprint(
        "acct-1", 157.86, "2026-06-01", "fat rosie's"
    )


def _bead(institution, account, amount, date, desc, state="posted"):
    return {
        "state": state,
        "content": {
            "institution": institution,
            "account_id": account,
            "amount": amount,
            "posted_date": date,
            "description": desc,
        },
    }


@pytest.mark.asyncio
async def test_fingerprint_counts_multiset_and_filters(httpx_mock, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    httpx_mock.add_response(
        json=[
            _bead("citi", "acct-1", 157.86, "2026-05-31", "FAT ROSIE'S"),
            _bead("citi", "acct-1", 157.86, "2026-05-31", "FAT ROSIE'S"),  # real double
            _bead("citi", "acct-1", 12.00, "2026-06-01", "Coffee"),
            _bead("citi", "acct-1", 9.99, "2026-06-01", "Gone", state="removed"),
            _bead("chase", "acct-9", 50.0, "2026-06-01", "Other institution"),
        ]
    )

    counts = await fetch_existing_fingerprint_counts_activity("citi")

    double = transaction_fingerprint("acct-1", 157.86, "2026-05-31", "FAT ROSIE'S")
    single = transaction_fingerprint("acct-1", 12.00, "2026-06-01", "Coffee")
    removed = transaction_fingerprint("acct-1", 9.99, "2026-06-01", "Gone")
    other = transaction_fingerprint("acct-9", 50.0, "2026-06-01", "Other institution")

    assert counts[double] == 2  # multiset: both real charges occupy a slot
    assert counts[single] == 1
    assert removed not in counts  # removed beads don't reserve slots
    assert other not in counts  # other institutions excluded
    assert json.dumps(counts)  # JSON-safe for Temporal payloads
