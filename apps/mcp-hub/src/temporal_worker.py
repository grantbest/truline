import asyncio
import os
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    ScheduleCalendarSpec,
    ScheduleRange,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.common import RetryPolicy
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

# Import Workflow and Activities
from src.workflows.morning_brief import (
    MorningBriefWorkflow,
    gather_data_activity,
    synthesize_brief_activity,
    send_to_discord_activity,
    save_to_substrate_activity
)
from src.workflows.bank_sync import (
    SCHEDULE_RETRY_MAX_ATTEMPTS,
    BankSyncWorkflow,
    list_accounts_activity,
    pull_transactions_activity,
    categorize_transaction_activity,
    categorize_transactions_bulk_activity,
    write_account_bead_activity,
    write_transaction_bead_activity,
    fetch_existing_transaction_ids_activity,
    fetch_existing_fingerprint_counts_activity,
    # Phase 5: item-health, reconciliation, anomaly alerting.
    check_item_status_activity,
    notify_sync_failure_activity,
    resolve_sync_failure_activity,
    reconcile_beads_activity,
    detect_anomalies_activity,
    # LO-OBS-002: per-institution freshness-SLO verdict, recorded not just alerted.
    evaluate_freshness_activity,
    # Phase 6: liabilities merged into account beads.
    fetch_liabilities_activity,
    # Phase 8.5: bill registration.
    register_liability_bills_activity,
    # /transactions/sync migration: cursor-based nightly sync.
    ensure_item_registry_activity,
    sync_transactions_page_activity,
    store_sync_cursor_activity,
    apply_modified_transactions_activity,
    apply_removed_transactions_activity,
)
from src.workflows.historical_backfill import (
    HistoricalBackfillWorkflow,
    fetch_backfill_status_activity,
    upsert_backfill_status_activity,
)
from src.workflows.financial_insights import (
    FinancialInsightsWorkflow,
    aggregate_finance_data_activity,
    synthesize_insight_activity,
    persist_insight_activity,
    summarize_merchant_insights_activity,
)
from src.workflows.budget_pulse import (
    DailyBudgetPulseWorkflow,
    analyze_budget_status_activity,
    persist_budget_alerts_activity,
    notify_budget_alerts_activity,
    audit_bills_activity,
)
from src.workflows.subscription_auditor import (
    WeeklySubscriptionAuditWorkflow,
    cluster_transactions_activity,
    analyze_subscription_activity,
    persist_subscription_activity,
    notify_subscriptions_activity,
)
from src.workflows.transfer_pairing import (
    TransferPairingWorkflow,
    fetch_unpaired_transfers_activity,
    persist_transfer_pair_activity,
)
from src.workflows.finance_reconciliation import (
    LedgerReconciliationWorkflow,
    reconcile_balances_activity,
    emit_discrepancies_activity,
    write_balance_snapshots_activity,
)
from src.workflows.finance_anomaly_detector import (
    FinanceAnomalyDetectorWorkflow,
    fetch_anomaly_inputs_activity,
    emit_anomalies_activity,
)
from src.workflows.subscription_sentinel import (
    SubscriptionSentinelWorkflow,
    identify_trial_alerts_activity,
    emit_trial_alerts_activity,
)
from src.workflows.finance_yield_optimizer import (
    FinanceYieldOptimizerWorkflow,
    fetch_optimizer_inputs_activity,
    emit_allocation_insights_activity,
)
from src.workflows.finance_budget_analyzer import (
    FinanceBudgetAnalyzerWorkflow,
    fetch_budget_analysis_inputs_activity,
    emit_budget_recommendations_activity,
)
from src.workflows.finance_scenario_runner import (
    FinanceScenarioRunnerWorkflow,
    run_scenario_activity,
)
from src.workflows.finance_rule_apply import (
    FinanceRuleApplyWorkflow,
    apply_rule_activity,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def temporal_task_queue() -> str:
    return os.environ.get("TEMPORAL_TASK_QUEUE", "morning-brief")


def temporal_namespace() -> str:
    return os.environ.get("TEMPORAL_NAMESPACE", "default")


def temporal_schedules_enabled() -> bool:
    return os.environ.get("ENABLE_TEMPORAL_SCHEDULES", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def bank_sync_workflow_retry_policy() -> RetryPolicy:
    """Retry the whole bank-sync workflow after transient downstream limits.

    Activity retries cover short LiteLLM/Plaid/Substrate blips. The July 2026
    outage showed that a per-minute LiteLLM 429 can still exhaust activity
    retries and fail the workflow; a workflow retry replays safely because
    transaction writes are idempotent and cursors advance only after writes.
    """
    return RetryPolicy(
        initial_interval=timedelta(minutes=10),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(hours=2),
        # Shared with the workflow so it knows which attempt is final
        # (only the final attempt posts the Discord failure alert).
        maximum_attempts=SCHEDULE_RETRY_MAX_ATTEMPTS,
    )


async def reconcile_schedule(client: Client, schedule_id: str, schedule: Schedule):
    """Create or update a Temporal schedule so startup reconciles drift.

    The update callback replaces the schedule's action/spec/policy with the
    desired definition every time — a manually edited spec is still
    corrected, convergence-style. But it preserves whatever pause state and
    note the schedule currently carries, read at update time via
    `update_input.description.schedule.state` (mirrors
    factory-dispatcher/schedule_runtime.py's read-then-rebuild pattern).
    Without this, any worker restart — including the ones the stakater
    reloader triggers on every secret rotation — silently un-pauses a
    schedule an operator paused from the Temporal UI.
    """
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        await client.create_schedule(schedule_id, schedule)
        logger.info(f"Schedule {schedule_id} created.")
        return
    except Exception as e:
        if "not found" not in str(e).lower():
            raise
        await client.create_schedule(schedule_id, schedule)
        logger.info(f"Schedule {schedule_id} created.")
        return

    def updater(update_input: Any) -> ScheduleUpdate:
        current_state = update_input.description.schedule.state
        preserved_state = ScheduleState(
            paused=current_state.paused,
            note=current_state.note,
        )
        updated = replace(schedule, state=preserved_state)
        return ScheduleUpdate(updated)

    await handle.update(updater)
    logger.info(f"Schedule {schedule_id} updated.")

async def ensure_schedules(client: Client):
    """Create or update Temporal schedules for recurring platform workflows."""
    task_queue = temporal_task_queue()
    
    # Office Schedule: 30 10 * * 1-3 (10:30 UTC Mon-Wed)
    office_id = "daily-brief-office"
    await reconcile_schedule(
        client,
        office_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                MorningBriefWorkflow.run,
                "office",
                id=office_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=10)],
                        minute=[ScheduleRange(start=30)],
                        day_of_week=[ScheduleRange(start=1, end=3)], # Mon-Wed
                    )
                ]
            ),
        ),
    )

    # Home Schedule: 30 12 * * 0,4-6 (12:30 UTC Sun, Thu-Sat)
    home_id = "daily-brief-home"
    await reconcile_schedule(
        client,
        home_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                MorningBriefWorkflow.run,
                "home",
                id=home_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=12)],
                        minute=[ScheduleRange(start=30)],
                        day_of_week=[
                            ScheduleRange(start=0), # Sun
                            ScheduleRange(start=4, end=6) # Thu-Sat
                        ],
                    )
                ]
            ),
        ),
    )

    # --- Bank Sync Schedules ---
    # One BankSyncWorkflow schedule per institution, discovered from env.
    # Convention (Phase 3): each Plaid access token lives in
    # `PLAID_ACCESS_TOKEN_<INSTITUTION_SLUG>`. The suffix (lowercased) is
    # the institution slug we pass to the workflow.
    #
    # Note: reconcile_schedule only creates/updates — schedules for tokens
    # that have been removed from env persist in Temporal until manually
    # deleted. Same trade-off as the morning-brief schedules above.
    # Sync cadence is a property of what an institution HOLDS, not of how many
    # institutions there are. Credit cards and savings are read for balances and
    # due dates, which move on a monthly cycle; the primary checking account is
    # read for money that has actually moved. One blanket four-runs-a-day spec
    # charged the same for both and cost $50 in a month of consumption billing
    # (2026-08-29) against roughly 26 runs that were actually wanted.
    #
    # 0=Sunday .. 6=Saturday, matching every other calendar spec in this file.
    SUNDAY, TUESDAY, THURSDAY, SATURDAY = 0, 2, 4, 6
    BANK_SYNC_DAYS: dict[str, tuple[int, ...]] = {
        # credit
        "amex": (SUNDAY,),
        "citi": (SUNDAY,),
        "discover": (SUNDAY,),
        # depository, secondary
        "sofi": (SUNDAY,),
        # depository, primary checking — the only feed read mid-week
        "chase": (TUESDAY, THURSDAY, SATURDAY),
    }

    PLAID_PREFIX = "PLAID_ACCESS_TOKEN_"
    institution_slugs = sorted({
        key[len(PLAID_PREFIX):].lower()
        for key, value in os.environ.items()
        if key.startswith(PLAID_PREFIX) and key != PLAID_PREFIX and value
    })

    if not institution_slugs:
        logger.warning(
            "No %s* env vars found — no BankSyncWorkflow schedules registered. "
            "Add an access token to Infisical to enable nightly sync.",
            PLAID_PREFIX,
        )

    for idx, slug in enumerate(institution_slugs):
        days = BANK_SYNC_DAYS.get(slug)
        if days is None:
            # Refuse rather than default. A default cadence is how five
            # institutions all ended up on four runs a day; an unclassified
            # institution with no schedule is reported by
            # tools.connections.get_bank_sync_schedules under `missing`,
            # which is a question someone answers, not a bill someone pays.
            logger.error(
                "institution=%s has no entry in BANK_SYNC_DAYS; no schedule "
                "registered. Classify it by what it holds and redeploy.",
                slug,
            )
            continue
        schedule_id = f"bank-sync-{slug}"
        await reconcile_schedule(
            client,
            schedule_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    BankSyncWorkflow.run,
                    slug,
                    id=schedule_id,
                    task_queue=task_queue,
                    execution_timeout=timedelta(hours=2),
                    retry_policy=bank_sync_workflow_retry_policy(),
                ),
                spec=ScheduleSpec(
                    calendars=[
                        ScheduleCalendarSpec(
                            day_of_week=[ScheduleRange(start=d) for d in days],
                            # Evening, and only evening. The four-runs-a-day
                            # spec this replaces existed because a single
                            # 03:00 run fires before banks finish posting the
                            # previous day and before Plaid's daily refresh of
                            # the Item, so it read an empty sync page and the
                            # ledger trailed (2026-07-13/14: every 03:00 run
                            # wrote 0 while 16 chase transactions sat in Plaid
                            # by evening). The fix for that was more runs; the
                            # fix that survives is the RIGHT run. 21:00 is the
                            # one of the four that was always going to find
                            # the day's transactions.
                            hour=[ScheduleRange(start=21)],
                            # Stagger institutions 2 min apart. All five at
                            # :00 meant five identical cache-miss
                            # /beads?limit=5000 queries hitting Substrate in
                            # the same second; the concurrent decrypt herd
                            # blew past the 30s client timeout and failed
                            # every sync (2026-07-18). Still load-bearing:
                            # four of the five share Sunday.
                            minute=[ScheduleRange(start=(idx * 2) % 60)],
                        )
                    ],
                    time_zone_name="America/Chicago",
                ),
            ),
        )
        logger.info(
            "Registered bank-sync schedule for institution=%s days=%s hour=21",
            slug,
            days,
        )

    # --- Financial Insights (weekly CFO digest) ---
    # Sunday 23:00 America/Chicago. Using time_zone_name on the
    # ScheduleSpec lets Temporal handle CST/CDT correctly across DST.
    insights_id = "financial-insights-weekly"
    await reconcile_schedule(
        client,
        insights_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                FinancialInsightsWorkflow.run,
                id=insights_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        day_of_week=[ScheduleRange(start=0)],  # Sunday
                        hour=[ScheduleRange(start=23)],
                        minute=[ScheduleRange(start=0)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Weekly Subscription Auditor (Phase 4.4) ---
    # Sunday 09:00 America/Chicago — runs 14 hours before the Sunday-23:00
    # financial-insights digest so the auditor's new finance.subscription
    # beads are available to the CFO digest's context window if/when that
    # workflow grows a subscription-aware section.
    subscription_audit_id = "subscription-audit-weekly"
    await reconcile_schedule(
        client,
        subscription_audit_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                WeeklySubscriptionAuditWorkflow.run,
                id=subscription_audit_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        day_of_week=[ScheduleRange(start=0)],  # Sunday
                        hour=[ScheduleRange(start=9)],
                        minute=[ScheduleRange(start=0)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Daily Budget Pulse (Phase 4.2 — Proactive Budgeting) ---
    # Weekdays 08:30 America/Chicago. time_zone_name lets Temporal
    # handle CST/CDT correctly across DST.
    pulse_id = "daily-budget-pulse"
    await reconcile_schedule(
        client,
        pulse_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                DailyBudgetPulseWorkflow.run,
                id=pulse_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        day_of_week=[ScheduleRange(start=1, end=5)],  # Mon-Fri
                        hour=[ScheduleRange(start=8)],
                        minute=[ScheduleRange(start=30)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Transfer Pairing (Phase 6.5) ---
    # Daily 03:30 America/Chicago — runs 30 min after the 03:00 bank syncs
    # so the day's fresh transfer legs are present before we try to pair them.
    transfer_pairing_id = "transfer-pairing-daily"
    await reconcile_schedule(
        client,
        transfer_pairing_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                TransferPairingWorkflow.run,
                id=transfer_pairing_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=3)],
                        minute=[ScheduleRange(start=30)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Ledger Reconciliation (SDD Phase 1) ---
    # Daily 03:45 America/Chicago — runs after the 03:00 bank syncs and the
    # 03:30 transfer pairing so balances + pairings are fresh before we
    # snapshot tonight's balances and reconcile drift against last night's.
    reconciliation_id = "ledger-reconciliation-daily"
    await reconcile_schedule(
        client,
        reconciliation_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                LedgerReconciliationWorkflow.run,
                id=reconciliation_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=3)],
                        minute=[ScheduleRange(start=45)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Anomaly Detector (SDD Phase 2 — Proactive Defense, Component 1) ---
    # Daily 04:00 America/Chicago — runs after the 03:00 bank syncs, 03:30
    # transfer pairing and 03:45 reconciliation, so it scans the freshest
    # 48h of transactions for duplicate charges and price hikes.
    anomaly_id = "finance-anomaly-detector-daily"
    await reconcile_schedule(
        client,
        anomaly_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                FinanceAnomalyDetectorWorkflow.run,
                id=anomaly_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=4)],
                        minute=[ScheduleRange(start=0)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Subscription / Trial Sentinel (SDD Phase 2, Component 2) ---
    # Daily 08:00 America/Chicago — morning visibility, ahead of the 08:30
    # budget pulse, so a trial ending in 3 days surfaces with the day's other
    # finance alerts.
    sentinel_id = "subscription-sentinel-daily"
    await reconcile_schedule(
        client,
        sentinel_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                SubscriptionSentinelWorkflow.run,
                id=sentinel_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        hour=[ScheduleRange(start=8)],
                        minute=[ScheduleRange(start=0)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Yield / Cash-Drag Optimizer (SDD Phase 3, Component 1) ---
    # Monday 06:00 America/Chicago — weekly capital-efficiency scan. Runs after
    # the weekend's syncs so balances/liabilities are fresh; surfaces idle
    # checking cash that should be paid against high-APR debt.
    yield_optimizer_id = "finance-yield-optimizer-weekly"
    await reconcile_schedule(
        client,
        yield_optimizer_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                FinanceYieldOptimizerWorkflow.run,
                id=yield_optimizer_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        day_of_week=[ScheduleRange(start=1)],  # Monday
                        hour=[ScheduleRange(start=6)],
                        minute=[ScheduleRange(start=0)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # --- Intelligent Budget Auto-Adjuster (SDD Phase 3, Component 2) ---
    # 1st of the month, 06:30 America/Chicago — compares each category's 90-day
    # rolling average to its cap and recommends a re-base when it has drifted
    # >15%. Monthly cadence matches the budgeting cycle.
    budget_analyzer_id = "finance-budget-analyzer-monthly"
    await reconcile_schedule(
        client,
        budget_analyzer_id,
        Schedule(
            action=ScheduleActionStartWorkflow(
                FinanceBudgetAnalyzerWorkflow.run,
                id=budget_analyzer_id,
                task_queue=task_queue,
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        day_of_month=[ScheduleRange(start=1)],
                        hour=[ScheduleRange(start=6)],
                        minute=[ScheduleRange(start=30)],
                    )
                ],
                time_zone_name="America/Chicago",
            ),
        ),
    )

    # Note: the SDD Phase 3 scenario engine (FinanceScenarioRunnerWorkflow) is
    # on-demand only — started by the POST /finance/scenario endpoint — so it
    # has no schedule here, just a worker registration below.

async def main():
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "temporal.platform-core.svc.cluster.local:7233")
    namespace = temporal_namespace()
    task_queue = temporal_task_queue()
    
    # Connect to Temporal
    logger.info("Connecting to Temporal at %s namespace=%s task_queue=%s...", temporal_address, namespace, task_queue)
    client = await Client.connect(temporal_address, namespace=namespace)

    # Idempotently register schedules only in environments that are allowed to
    # initiate recurring side effects. Dev/stage workers can still poll queues
    # for on-demand workflows without generating duplicate Discord messages.
    if temporal_schedules_enabled():
        await ensure_schedules(client)
    else:
        logger.info("ENABLE_TEMPORAL_SCHEDULES=false; skipping schedule registration.")

    # Start Worker
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            MorningBriefWorkflow,
            BankSyncWorkflow,
            FinancialInsightsWorkflow,
            DailyBudgetPulseWorkflow,
            WeeklySubscriptionAuditWorkflow,
            # Phase 6: on-demand 2-year historical backfill per institution.
            HistoricalBackfillWorkflow,
            # Phase 6.5: pair internal-transfer transaction legs.
            TransferPairingWorkflow,
            # SDD Phase 1: nightly balance-delta ledger reconciliation.
            LedgerReconciliationWorkflow,
            # SDD Phase 2: proactive defense.
            FinanceAnomalyDetectorWorkflow,
            SubscriptionSentinelWorkflow,
            # SDD Phase 3: advanced intelligence & yield.
            FinanceYieldOptimizerWorkflow,
            FinanceBudgetAnalyzerWorkflow,
            FinanceScenarioRunnerWorkflow,
            # SDD Phase 4: rule retroaction.
            FinanceRuleApplyWorkflow,
        ],
        activities=[
            gather_data_activity,
            synthesize_brief_activity,
            send_to_discord_activity,
            save_to_substrate_activity,
            list_accounts_activity,
            pull_transactions_activity,
            categorize_transaction_activity,
            categorize_transactions_bulk_activity,
            write_account_bead_activity,
            write_transaction_bead_activity,
            fetch_existing_transaction_ids_activity,
            fetch_existing_fingerprint_counts_activity,
            check_item_status_activity,
            notify_sync_failure_activity,
            resolve_sync_failure_activity,
            reconcile_beads_activity,
            detect_anomalies_activity,
            evaluate_freshness_activity,
            aggregate_finance_data_activity,
            synthesize_insight_activity,
            persist_insight_activity,
            summarize_merchant_insights_activity,
            analyze_budget_status_activity,
            persist_budget_alerts_activity,
            notify_budget_alerts_activity,
            audit_bills_activity,
            cluster_transactions_activity,
            analyze_subscription_activity,
            persist_subscription_activity,
            notify_subscriptions_activity,
            # Phase 6.
            fetch_liabilities_activity,
            register_liability_bills_activity,
            # /transactions/sync migration.
            ensure_item_registry_activity,
            sync_transactions_page_activity,
            store_sync_cursor_activity,
            apply_modified_transactions_activity,
            apply_removed_transactions_activity,
            fetch_backfill_status_activity,
            upsert_backfill_status_activity,
            # Phase 6.5: transfer pairing.
            fetch_unpaired_transfers_activity,
            persist_transfer_pair_activity,
            # SDD Phase 1: ledger reconciliation.
            reconcile_balances_activity,
            emit_discrepancies_activity,
            write_balance_snapshots_activity,
            # SDD Phase 2: anomaly detection, trial sentinel.
            fetch_anomaly_inputs_activity,
            emit_anomalies_activity,
            identify_trial_alerts_activity,
            emit_trial_alerts_activity,
            # SDD Phase 3: yield optimizer, budget analyzer, scenario engine.
            fetch_optimizer_inputs_activity,
            emit_allocation_insights_activity,
            fetch_budget_analysis_inputs_activity,
            emit_budget_recommendations_activity,
            run_scenario_activity,
            # SDD Phase 4: rule retroaction.
            apply_rule_activity,
        ],
    )
    
    logger.info("Worker started. Polling %s task queue in namespace=%s...", task_queue, namespace)
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
