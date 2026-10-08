"""SDD Phase 2 — Subscription & Free-Trial Sentinel.

Stop the bleed on unwanted recurring charges: warn exactly 3 days before a
free trial converts to a paid subscription, surfacing it in the console's
Anomaly Inbox for review.

Data source
-----------
The weekly subscription auditor persists ``finance.subscription`` beads. This
sentinel reads any that carry a ``content.trial_end_date`` (additive field —
populated today by Vision extraction of a signup confirmation, or manually;
the auditor itself doesn't infer trial dates). When that date is exactly
``LEAD_DAYS`` out from "today" we raise one ``finance.action_required`` bead
(``content.kind = "trial_ending"``, state=pending) for the Anomaly Inbox.

Why "exactly 3 days" and not "≤3 days"?
  A daily cron with a ``== LEAD_DAYS`` check fires the alert once, on the right
  morning. A ``<=`` check would re-raise every day until the trial ends; the
  pending-bead upsert (keyed on ``subscription_id``) already dedups, but the
  single-day trigger keeps the semantics obvious and matches the brief.

``action_required`` is a free-form type (not in
``schemas.FINANCE_TYPE_SCHEMAS``), single-token per the Phase 1 precedent
(``audit_discrepancy``), NOT the brief's dotted ``action_required.trial_ending``.
The trial/dispute/etc. discriminator lives in ``content.kind`` so one type can
back several action cards in the inbox.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    from src.tools.finance import create_bead, patch_bead, query_beads


# --- Tunables -------------------------------------------------------------

# Fire the alert this many days before the trial converts.
LEAD_DAYS = 3

_SUBSCRIPTION_TYPE = "subscription"
_ACTION_TYPE = "action_required"
_KIND = "trial_ending"


# --- Pure helpers (deterministic; safe to unit test directly) -------------

def _to_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def evaluate_trial_deadline(
    subscription: Dict[str, Any],
    *,
    today: date,
    lead_days: int = LEAD_DAYS,
) -> Optional[Dict[str, Any]]:
    """Return an alert dict if this subscription's trial ends exactly
    ``lead_days`` from ``today``, else None.

    Pure and deterministic — no clock, no I/O — so it's unit-testable. The
    subscription's ``content.trial_end_date`` must parse to a date; absent or
    unparseable dates yield None.
    """
    content = subscription.get("content") or {}
    trial_end = _to_date(content.get("trial_end_date"))
    if trial_end is None:
        return None
    days_until = (trial_end - today).days
    if days_until != lead_days:
        return None
    return {
        "kind": _KIND,
        "subscription_id": subscription.get("id"),
        "vendor": content.get("name") or content.get("merchant_key"),
        "amount_at_risk": content.get("amount"),
        "frequency": content.get("frequency"),
        "end_date": trial_end.isoformat(),
        "days_until": days_until,
        "fingerprint": f"trial:{subscription.get('id')}",
    }


def evaluate_trials(
    subscriptions: List[Dict[str, Any]],
    *,
    today: date,
    lead_days: int = LEAD_DAYS,
) -> List[Dict[str, Any]]:
    alerts = []
    for sub in subscriptions:
        alert = evaluate_trial_deadline(sub, today=today, lead_days=lead_days)
        if alert is not None:
            alerts.append(alert)
    return alerts


# --- Activities -----------------------------------------------------------

@activity.defn
async def identify_trial_alerts_activity() -> List[Dict[str, Any]]:
    """Fetch active subscriptions and return alerts for trials ending in
    exactly ``LEAD_DAYS`` days. Date math uses the activity clock."""
    subscriptions = await query_beads(
        {"type": _SUBSCRIPTION_TYPE, "state": "active", "limit": 1000}
    )
    today = datetime.now(timezone.utc).date()
    alerts = evaluate_trials(subscriptions, today=today)
    logger.info(
        "Trial sentinel: %d subscriptions scanned -> %d trial(s) ending in %d days.",
        len(subscriptions), len(alerts), LEAD_DAYS,
    )
    return alerts


@activity.defn
async def emit_trial_alerts_activity(alerts: List[Dict[str, Any]]) -> int:
    """Upsert one ``finance.action_required`` (kind=trial_ending) bead per
    alert, keyed on ``content.fingerprint`` (one per subscription) so re-runs
    refresh rather than duplicate. Returns the count written or refreshed."""
    if not alerts:
        return 0

    existing = await query_beads({"type": _ACTION_TYPE, "state": "pending", "limit": 1000})
    pending_by_fp: Dict[str, Dict[str, Any]] = {}
    for b in existing:
        fp = (b.get("content") or {}).get("fingerprint")
        if fp and fp not in pending_by_fp:
            pending_by_fp[fp] = b

    written = 0
    for alert in alerts:
        content = {**alert, "detected_at": datetime.now(timezone.utc).isoformat()}
        match = pending_by_fp.get(alert.get("fingerprint"))
        if match is not None:
            await patch_bead(
                match["id"], content=content, created_by="subscription-sentinel/refresh"
            )
        else:
            await create_bead(
                _ACTION_TYPE, content, state="pending", created_by="subscription-sentinel/emit"
            )
        written += 1

    logger.info("Emitted/refreshed %d action_required (trial_ending) beads.", written)
    return written


# --- Workflow -------------------------------------------------------------

@workflow.defn
class SubscriptionSentinelWorkflow:
    """Daily trial-deadline sentinel. Scheduled by
    ``temporal_worker.ensure_schedules`` for 08:00 America/Chicago."""

    @workflow.run
    async def run(self) -> Dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=3,
        )

        alerts = await workflow.execute_activity(
            identify_trial_alerts_activity,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        emitted = await workflow.execute_activity(
            emit_trial_alerts_activity,
            alerts,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry,
        )

        return {"trials_ending": len(alerts), "beads_emitted": emitted}
