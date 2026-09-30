"""Scheduler: daily parsing at 20:00–23:00 MSK (spec §3).

Runs as a separate process: python -m worker.scheduler
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting, Competitor, RunTrigger
from app.services.analytics.metrics import recalculate_all
from app.services.report import build_daily_report
from app.services.runner import run_competitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")

MSK = timezone(timedelta(hours=3))


def _schedule_hour() -> tuple[int, int]:
    """Run time from settings (default 21:00 MSK = within the 20:00–23:00 window)."""
    with SessionLocal() as db:
        row = db.get(AppSetting, "parse_schedule")
        if row and isinstance(row.value, dict):
            return int(row.value.get("hour", 21)), int(row.value.get("minute", 0))
    return settings.parse_hour_msk, settings.parse_minute_msk


async def daily_parse_job() -> None:
    """Full daily cycle: all competitors -> metrics -> report."""
    logger.info("Daily data collection started")
    with SessionLocal() as db:
        competitors = db.scalars(
            select(Competitor).where(Competitor.enabled.is_(True))
        ).all()

        for competitor in competitors:
            try:
                await run_competitor(db, competitor, trigger=RunTrigger.AUTO.value)
            except Exception:  # noqa: BLE001
                logger.exception("Parsing error for %s", competitor.name)

        # Recalculate metrics and the report after all sites
        count = recalculate_all(db)
        logger.info("Metrics recalculated for %d products", count)

        report = build_daily_report(db)
    logger.info(
        "Report for %s: sites=%d, products=%d, new=%d, price changes=%d, stock decreases=%d",
        report["day"],
        report["sites_checked"],
        report["products_checked"],
        report["new_products"],
        report["price_changes"],
        report["stock_decreased"],
    )
    if report["errors"]:
        logger.warning("Finished with errors: %d runs", len(report["errors"]))
    logger.info("Daily data collection finished")


async def amain() -> None:
    """Start the scheduler inside an event loop (AsyncIOScheduler requires a running loop)."""
    hour, minute = _schedule_hour()
    scheduler = AsyncIOScheduler(timezone=MSK)
    scheduler.add_job(
        daily_parse_job,
        CronTrigger(hour=hour, minute=minute, timezone=MSK),
        id="daily_parse",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Scheduler started: daily collection at %02d:%02d MSK", hour, minute)
    try:
        # Keep the process alive until Ctrl+C / container stop
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    try:
        asyncio.run(amain())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
