"""
Entry point: builds the options trading stack and starts APScheduler jobs.

Scheduler jobs:
  run_position_management — every 2 minutes: position monitor, exit rules, EOD close
  run_scan_and_trade      — every 5 minutes: universe → IV data → decisions → orders

CLI flags:
  --dry-run   Log decisions without submitting any broker orders.
  --force     Bypass market-hours gates (use with --dry-run for testing).
"""

import argparse
import signal
import sys
import time
from datetime import datetime

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

import config
from bootstrap import build_trading_stack
from core.database import log


def graceful_exit(sig, frame):
    """Handle SIGINT or SIGTERM: log and exit.

    Args:
        sig:   Signal number.
        frame: Current stack frame (unused).
    """
    log.info("Shutdown signal received — stopping options bot")
    sys.exit(0)


def main():
    """
    Parse CLI flags, wire the trading stack, start the scheduler, and block.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Autonomous options trading bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log decisions without placing orders")
    parser.add_argument("--force",   action="store_true",
                        help="Bypass market-hours gates (use with --dry-run)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    log.info("=" * 65)
    suffix = ("  [DRY-RUN]" if args.dry_run else "") + ("  [FORCE]" if args.force else "")
    log.info("Autonomous Options Trading Bot — starting up%s", suffix)
    log.info("Account: $%.0f | Max premium/trade: $%.0f | Max positions: %d",
             config.ACCOUNT_SIZE,
             config.MAX_PREMIUM_PER_TRADE,
             config.MAX_OPEN_OPTIONS_POSITIONS)
    log.info("IV high threshold: %d | IV low threshold: %d | Min VRP: %.1f pts",
             config.IV_RANK_HIGH_THRESHOLD,
             config.IV_RANK_LOW_THRESHOLD,
             config.MIN_VRP_TO_SELL)
    log.info("Exit rules: 50%% profit | 200%% stop | credit DTE≤%d | debit DTE≤%d",
             config.CREDIT_CLOSE_DTE_DAYS,
             config.DEBIT_CLOSE_DTE_DAYS)
    log.info("=" * 65)

    orchestrator = build_trading_stack(dry_run=args.dry_run)
    if args.force:
        orchestrator.set_force_run(True)

    executors = {"default": ThreadPoolExecutor(max_workers=2)}
    scheduler = BackgroundScheduler(executors=executors, timezone=config.ET)

    # ── Recurring jobs ────────────────────────────────────────────────────────
    scheduler.add_job(
        orchestrator.run_position_management,
        "interval",
        minutes=2,
        id="position_management",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        orchestrator.run_scan_and_trade,
        "interval",
        minutes=5,
        id="scan_and_trade",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    # ── Immediate startup triggers ────────────────────────────────────────────
    scheduler.add_job(
        orchestrator.run_position_management,
        "date",
        run_date=datetime.now(config.ET),
        id="immediate_position",
    )
    scheduler.add_job(
        orchestrator.run_scan_and_trade,
        "date",
        run_date=datetime.now(config.ET),
        id="immediate_scan",
    )

    log.info("Scheduler started — position management every 2 min, scan every 5 min")
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info("Options bot stopped by user")
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
