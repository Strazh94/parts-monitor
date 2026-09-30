"""Планировщик: ежедневный парсинг в 20:00–23:00 МСК (ТЗ п.3).

Запускается отдельным процессом: python -m worker.scheduler
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
    """Время запуска из настроек (по умолчанию 21:00 МСК = в окне 20:00–23:00)."""
    with SessionLocal() as db:
        row = db.get(AppSetting, "parse_schedule")
        if row and isinstance(row.value, dict):
            return int(row.value.get("hour", 21)), int(row.value.get("minute", 0))
    return settings.parse_hour_msk, settings.parse_minute_msk


async def daily_parse_job() -> None:
    """Полный ежедневный цикл: все конкуренты -> метрики -> отчёт."""
    logger.info("Ежедневный запуск сбора данных начат")
    with SessionLocal() as db:
        competitors = db.scalars(
            select(Competitor).where(Competitor.enabled.is_(True))
        ).all()

        for competitor in competitors:
            try:
                await run_competitor(db, competitor, trigger=RunTrigger.AUTO.value)
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка парсинга %s", competitor.name)

        # Пересчёт метрик и отчёт после всех сайтов
        count = recalculate_all(db)
        logger.info("Метрики пересчитаны для %d товаров", count)

        report = build_daily_report(db)
    logger.info(
        "Отчёт за %s: сайтов=%d, товаров=%d, новых=%d, изменений цен=%d, снижений остатка=%d",
        report["day"],
        report["sites_checked"],
        report["products_checked"],
        report["new_products"],
        report["price_changes"],
        report["stock_decreased"],
    )
    if report["errors"]:
        logger.warning("Завершились с ошибкой: %d запусков", len(report["errors"]))
    logger.info("Ежедневный сбор данных завершён")


async def amain() -> None:
    """Запуск планировщика внутри event loop (AsyncIOScheduler требует running loop)."""
    hour, minute = _schedule_hour()
    scheduler = AsyncIOScheduler(timezone=MSK)
    scheduler.add_job(
        daily_parse_job,
        CronTrigger(hour=hour, minute=minute, timezone=MSK),
        id="daily_parse",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Планировщик запущен: ежедневный сбор в %02d:%02d МСК", hour, minute)
    try:
        # Держим процесс живым до Ctrl+C / остановки контейнера
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
