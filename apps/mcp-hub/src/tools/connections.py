"""Per-institution connection status for the LifeOps Console.

Plaid plan PR 11: one call answers "what's linked, is it healthy, when
did it last sync, and what do I click to fix it" — the data the Console
Connections page (PR 12) renders and the place Discord repair alerts
deep-link to.

Sources, merged per institution:
  * env        — PLAID_ACCESS_TOKEN_* (which tokens this pod actually has)
  * plaid_item — Item registry beads (item_id, sync cursor, last_synced)
  * account    — account beads (count, balances freshness)
  * Plaid      — live /item/get probe (healthy / error_code / repairable),
                 skippable with probe=False for a fast beads-only render.

An institution appears if EITHER a token or a registry bead exists for
the current PLAID_ENV: token-without-bead means the registry self-heal
hasn't run yet; bead-without-token means the token was removed (or never
synced to this pod) and sync is silently dead — exactly the state the
page exists to surface.

Two things this page reports, and they are not the same thing
-------------------------------------------------------------
``status``/``healthy`` answer **"does Plaid answer for this Item?"** — a
liveness probe against ``/item/get``.

``data_freshness`` answers **"when did money data last arrive?"**

Before 2026-08-02 only the first existed, and ``last_synced`` — the timestamp
of the sync JOB, not of any data — sat next to it under a name that read like
the second. So a feed that returned nothing was indistinguishable from one
returning everything: on 2026-08-02 all five institutions reported ``ok`` with
``last_synced`` that morning, while Amex had produced no transaction since
07-27 and Citi and Discover none since 07-28.

``data_freshness`` used to be an OBSERVATION and nothing more, because a useful
staleness threshold has to be per-institution — Amex produced 18 transactions in
five months, so a global rule either cries wolf on it forever or is too loose to
catch a real outage on a daily feed.

That threshold now exists (LO-OBS-002). :func:`_freshness_slo_days` learns one
per institution from that institution's own gaps between posted dates, and
:func:`_derive_status` uses it, so ``ok`` asserts BOTH that Plaid answers and
that money data arrived inside the institution's own SLO. Where there is too
little cadence to measure, the SLO is withheld and the status is
``freshness_unknown`` — the module still refuses to imply something it does not
know, which is what the two answers above were always protecting.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from tools.finance import query_beads
from tools.plaid import (
    REAUTH_ERROR_CODES,
    TOKEN_REPAIR_ERROR_CODES,
    access_token_for,
    get_item_status,
    list_linked_institutions,
    plaid_error_code_from_exception,
)

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 15.0

# Lookback for the data-freshness observation. Long enough that a genuinely
# low-volume card (amex: 18 transactions in 5 months) still registers, short
# enough that the query stays cheap for a status page.
_FRESHNESS_WINDOW_DAYS = 60


def _portal_url(institution_slug: str, *, update: bool = False) -> str:
    base = os.environ.get("MCP_HUB_PUBLIC_URL", "https://mcp-hub.example.org").rstrip("/")
    url = f"{base}/finance/link/{institution_slug}"
    return f"{url}?mode=update" if update else url


async def _probe_item(institution_slug: str) -> Dict[str, Any]:
    """Live /item/get health check → {healthy, error_code, repairable}.

    Mirrors bank_sync.check_item_status_activity's classification, minus
    the Discord alerting — this is a read path, the nightly sync owns
    alerting.
    """
    token = access_token_for(institution_slug)
    if not token:
        return {"healthy": False, "error_code": "NO_TOKEN", "repairable": False}

    try:
        item_payload = await asyncio.wait_for(
            get_item_status(token), timeout=_PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return {"healthy": False, "error_code": "PROBE_TIMEOUT", "repairable": False}
    except Exception as exc:  # noqa: BLE001 — classify, never crash the page
        error_code = plaid_error_code_from_exception(exc)
        if error_code in TOKEN_REPAIR_ERROR_CODES:
            return {
                "healthy": False,
                "error_code": error_code,
                "repairable": error_code in REAUTH_ERROR_CODES,
            }
        logger.warning("connections probe failed for %s: %s", institution_slug, exc)
        return {
            "healthy": False,
            "error_code": error_code or "PLAID_API_ERROR",
            "repairable": False,
        }

    error = (item_payload.get("item") or {}).get("error") or {}
    error_code = error.get("error_code") if isinstance(error, dict) else None
    if error_code in TOKEN_REPAIR_ERROR_CODES:
        return {
            "healthy": False,
            "error_code": error_code,
            "repairable": error_code in REAUTH_ERROR_CODES,
        }
    # Non-reauth item.error (rate limit, institution outage) — degraded
    # but not actionable from the portal; surface the code, stay healthy
    # like the nightly sync does.
    return {"healthy": True, "error_code": error_code, "repairable": False}


# A freshness SLO is only meaningful once an institution has shown a cadence to
# measure. Two gaps is the minimum that describes a rhythm rather than a single
# interval, and below that the honest answer is that freshness is unmeasurable —
# NOT that the feed is fine.
_MIN_GAPS_FOR_SLO = 2

# How much slack a learned threshold gets over the institution's own worst
# ordinary gap. Without it, the widest normal gap alerts roughly half the time by
# construction. With it, a feed has to be meaningfully quieter than its own worst
# stretch before it is called stale.
_SLO_SLACK_MULTIPLIER = 2.0

# Floor for the learned threshold. amex has produced 18 transactions in 5 months;
# a card that is used twice a month must not be reported stale on a quiet week.
_SLO_FLOOR_DAYS = 7


def _freshness_slo_days(posted_dates: List[str], *, window_days: int) -> Optional[int]:
    """Days of silence this institution may have before its feed is stale.

    Learned from the institution's OWN gaps between posted dates, never from a
    global constant. The docstring on :func:`_freshness_from_transactions`
    records why: amex legitimately goes weeks between charges, so a fixed
    threshold either alerts on it constantly or is too loose to catch a real
    outage. Both failures were observed on 2026-08-02, when every institution
    reported ``ok`` while amex had produced nothing since 07-27.

    Returns ``None`` when there is not enough history to describe a cadence. That
    is a third answer, and the caller must not collapse it into either of the
    other two.
    """
    unique = sorted({d[:10] for d in posted_dates if d})
    if len(unique) < _MIN_GAPS_FOR_SLO + 1:
        return None

    days = [date.fromisoformat(d) for d in unique]
    gaps = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    if len(gaps) < _MIN_GAPS_FOR_SLO:
        return None

    slo = int(round(max(gaps) * _SLO_SLACK_MULTIPLIER))
    slo = max(slo, _SLO_FLOOR_DAYS)

    # An SLO at or beyond the lookback window cannot be evaluated with the data
    # the window holds — a feed silent that long looks identical to one with no
    # history at all. Refusing here is what keeps `stale` from being asserted on
    # evidence the query never had.
    if slo >= window_days:
        return None
    return slo


def _derive_status(entry: Dict[str, Any]) -> str:
    """Single UI-ready status the Console can switch on.

    ``ok`` now means BOTH that Plaid answers for this Item and that money data
    has arrived inside the institution's own freshness SLO. It used to mean only
    the first, which is how chase read as healthy while producing nothing:
    ``data_freshness`` was computed, rendered as a caption, and then the verdict
    was based on something else entirely.

    Actionability wins over staleness. An institution that is both stale and
    needs re-auth reports the re-auth, because that is the one a human can act
    on — and a dead feed is very often a symptom of it.
    """
    if not entry["token_present"]:
        return "no_token"
    if entry["healthy"] is None:
        return "unprobed"
    if not entry["healthy"]:
        if entry["error_code"] == "PROBE_TIMEOUT":
            return "unknown"
        return "reauth_required" if entry["repairable"] else "relink_required"

    # A non-reauth item.error (rate limit, institution outage) used to be
    # swallowed: the probe returned healthy=True with the code attached and
    # _derive_status reported a flat "ok", so the code reached the Console
    # and was never shown. Degraded is not actionable from the portal, but
    # it is not "ok" either.
    if entry.get("error_code"):
        return "degraded"

    freshness = entry.get("data_freshness") or {}
    slo = freshness.get("freshness_slo_days")
    days = freshness.get("days_since_last_transaction")

    if slo is None:
        # Not enough history to describe a cadence, or a cadence wider than the
        # window can see. Neither a pass nor a failure.
        return "freshness_unknown"
    if not freshness.get("known") or days is None:
        return "freshness_unknown"
    if days > slo:
        return "stale"
    return "ok"


def _freshness_verdict_message(
    institution: str, status: str, entry: Dict[str, Any]
) -> Optional[str]:
    """One-line prose for the two cases LO-OBS-002's acceptance criteria name.

    A structured ``stale``/``ok`` status plus a caption of numbers is not the
    same as a sentence a human reads and understands: the unhealthy case must
    name the SLO, the age of the newest transaction, and the institution; the
    healthy case must name the date of the most recent transaction it saw.
    Every other status (``freshness_unknown``, ``degraded``, re-auth, ...) is
    already named by the status vocabulary itself, so this returns ``None``
    rather than inventing prose the requirement never asked for.
    """
    freshness = entry.get("data_freshness") or {}
    if status == "stale":
        slo = freshness.get("freshness_slo_days")
        days = freshness.get("days_since_last_transaction")
        return (
            f"{institution}: no transactions in {days} days, "
            f"exceeding its {slo}-day freshness SLO."
        )
    if status == "ok":
        last = freshness.get("last_transaction_date")
        return f"{institution}: healthy — newest transaction {last}."
    return None


def _freshness_from_transactions(
    txs: List[Dict[str, Any]], slugs: List[str], *, window_days: int
) -> Dict[str, Dict[str, Any]]:
    """Newest ``posted_date`` per institution, over a bounded window.

    This is the number the Console needed and never had. ``last_synced`` records
    when the sync JOB ran; it says nothing about whether Plaid returned
    anything. On 2026-08-02 all five institutions reported ``ok`` with
    ``last_synced`` that morning while Amex had produced no transaction since
    07-27 — a dead feed and a quiet card were byte-identical.

    Amex has produced 18 transactions in 5 months, so any FIXED staleness
    threshold would either alert on it constantly or be too loose to catch a real
    outage. That is why the threshold is learned per institution here rather than
    declared globally — see :func:`_freshness_slo_days`, which closes LO-OBS-002.
    """
    newest: Dict[str, str] = {}
    posted_by_slug: Dict[str, List[str]] = {}
    for tx in txs:
        if tx.get("state") == "removed":
            continue
        content = tx.get("content") or {}
        slug = content.get("institution")
        posted = content.get("posted_date")
        if not slug or not posted:
            continue
        posted_by_slug.setdefault(slug, []).append(posted)
        if posted > newest.get(slug, ""):
            newest[slug] = posted

    today = date.today()
    out: Dict[str, Dict[str, Any]] = {}
    for slug in slugs:
        last = newest.get(slug)
        days: Optional[int] = None
        if last:
            try:
                days = (today - date.fromisoformat(last[:10])).days
            except ValueError:
                days = None
        slo = _freshness_slo_days(posted_by_slug.get(slug, []), window_days=window_days)
        out[slug] = {
            "last_transaction_date": last,
            "days_since_last_transaction": days,
            # False means "no transaction inside the lookback window", which is
            # different from "we know it has been N days". The UI must not turn
            # an unknown into a green.
            "known": last is not None,
            "window_days": window_days,
            # The threshold that produced the verdict, in whole days, so a human
            # can read it and disagree. None means this institution has not shown
            # enough cadence to measure — which is neither healthy nor stale.
            "freshness_slo_days": slo,
        }
    return out


async def get_connections_status(probe: bool = True) -> Dict[str, Any]:
    plaid_env = os.environ.get("PLAID_ENV", "sandbox")

    # Bounded window: an institution with nothing in 60 days is the signal, so
    # there is no reason to page the whole ledger to render a status page.
    freshness_after = (datetime.now() - timedelta(days=_FRESHNESS_WINDOW_DAYS)).isoformat()
    item_beads, account_beads, recent_txs = await asyncio.gather(
        query_beads({"type": "plaid_item", "limit": 100}),
        query_beads({"type": "account", "state": "active", "limit": 100}),
        query_beads(
            {"type": "transaction", "created_after": freshness_after, "limit": 2000}
        ),
    )

    items_by_slug = {
        (b.get("content") or {}).get("institution"): b
        for b in item_beads
        if (b.get("content") or {}).get("plaid_env") == plaid_env
        and (b.get("content") or {}).get("institution")
    }

    accounts_by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for b in account_beads:
        slug = (b.get("content") or {}).get("institution")
        if slug:
            accounts_by_slug.setdefault(slug, []).append(b)

    slugs = sorted(set(list_linked_institutions()) | set(items_by_slug))
    freshness = _freshness_from_transactions(
        recent_txs, slugs, window_days=_FRESHNESS_WINDOW_DAYS
    )

    probes: Dict[str, Dict[str, Any]] = {}
    if probe and slugs:
        results = await asyncio.gather(*(_probe_item(s) for s in slugs))
        probes = dict(zip(slugs, results))

    institutions: List[Dict[str, Any]] = []
    for slug in slugs:
        item_content = (items_by_slug.get(slug) or {}).get("content") or {}
        accounts = accounts_by_slug.get(slug, [])
        account_synced = [
            c.get("last_synced")
            for c in (a.get("content") or {} for a in accounts)
            if c.get("last_synced")
        ]
        probe_result = probes.get(slug) or {
            "healthy": None, "error_code": None, "repairable": None,
        }

        entry: Dict[str, Any] = {
            "institution": slug,
            "token_present": access_token_for(slug) is not None,
            "item_id": item_content.get("item_id"),
            "linked_at": item_content.get("linked_at"),
            "last_relinked_at": item_content.get("last_relinked_at"),
            "cursor_present": bool(item_content.get("transactions_cursor")),
            "last_synced": item_content.get("last_synced"),
            "account_count": len(accounts),
            "accounts_last_synced": max(account_synced) if account_synced else None,
            "healthy": probe_result["healthy"],
            "error_code": probe_result["error_code"],
            "repairable": probe_result["repairable"],
            "link_url": _portal_url(slug),
            "repair_url": _portal_url(slug, update=True),
            # Separate from `last_synced`, which is when the JOB ran. This is
            # when data last ARRIVED. They diverge silently, which is the whole
            # reason chase read as healthy while producing nothing.
            "data_freshness": freshness.get(slug),
        }
        entry["status"] = _derive_status(entry)
        entry["verdict"] = _freshness_verdict_message(slug, entry["status"], entry)
        institutions.append(entry)

    return {
        "plaid_env": plaid_env,
        "probed": bool(probe),
        "institutions": institutions,
    }
