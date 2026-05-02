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
    log.info("Shutdown signal received — stopping bot")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Autonomous stock trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log AI decisions without placing orders",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    log.info("=" * 60)
    log.info(
        "Autonomous Stock Trading Bot — starting up%s",
        "  [DRY-RUN]" if args.dry_run else "",
    )
    log.info(
        "Account target: $%.0f | Max daily deploy: $%.0f",
        config.ACCOUNT_SIZE,
        config.MAX_DAILY_CAPITAL,
    )
    log.info(
        "Max risk/trade: $%.0f | Max positions: %d",
        config.MAX_RISK_PER_TRADE,
        config.MAX_CONCURRENT_POSITIONS,
    )
    log.info("=" * 60)

    orchestrator, backtester = build_trading_stack(dry_run=args.dry_run)

    executors = {"default": ThreadPoolExecutor(max_workers=2)}
    scheduler = BackgroundScheduler(executors=executors, timezone=config.ET)

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
        minutes=10,
        id="scan_and_trade",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
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
    scheduler.add_job(
        backtester.run_backtest,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        id="weekly_backtest",
        max_instances=1,
    )

    log.info("Scheduler started — position management every 2 min, scan every 10 min")
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by user")
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
