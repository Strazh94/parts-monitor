"""Оркестратор запуска парсинга одного конкурента.

Цепочка: скачать -> распарсить -> сопоставить -> сохранить снапшот ->
зафиксировать изменения -> обновить статус запуска (ТЗ п.24).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChangeEvent,
    ChangeType,
    Competitor,
    EngineType,
    Offer,
    ParseRun,
    Product,
    RunStatus,
    RunTrigger,
    Snapshot,
    StockStatus,
)
from app.services.matching import ParsedItem, ProductMatcher
from app.services.parser.http_adapter import HttpAdapter, ParseError

logger = logging.getLogger(__name__)


async def run_competitor(
    db: Session, competitor: Competitor, trigger: str = "auto"
) -> ParseRun:
    """Полный проход по одному сайту. Создаёт запись ParseRun."""
    run = ParseRun(
        competitor_id=competitor.id,
        trigger=RunTrigger(trigger),
        status=RunStatus.RUNNING,
    )
    db.add(run)
    db.commit()

    try:
        items = await _fetch_items(competitor)
        run.products_found = len(items)
        run.pages_processed = getattr(_last_adapter, "value", 0)
        summary = _process_items(db, competitor, items)
        run.products_new = summary["new"]
        run.status = RunStatus.OK
        run.finished_at = datetime.utcnow()
        db.commit()
    except ParseError as exc:
        run.status = RunStatus.ERROR
        run.error_message = str(exc)
        run.errors_count = 1
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.warning("Парсинг %s: %s", competitor.name, exc)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ParseRun, run.id) or run
        run.status = RunStatus.ERROR
        run.error_message = f"Непредвиденная ошибка: {exc}"
        run.errors_count = 1
        run.finished_at = datetime.utcnow()
        db.add(run)
        db.commit()
        logger.exception("Парсинг %s упал", competitor.name)
    return run


# Последний адаптер — для передачи числа страниц в ParseRun
class _Last:
    value = 0


_last_adapter = _Last()


async def _fetch_items(competitor: Competitor) -> list[ParsedItem]:
    """Загрузка товаров: HTTP-движок (для JS-сайтов — Playwright, п.2)."""
    config = competitor.parser_config or {}

    async def on_progress(pages: int, _count: int) -> None:
        _last_adapter.value = pages

    _last_adapter.value = 0

    if competitor.engine == EngineType.PLAYWRIGHT:
        from app.services.parser.playwright_adapter import PlaywrightAdapter

        adapter = PlaywrightAdapter(competitor.url, config)
    else:
        adapter = HttpAdapter(competitor.url, config)

    return await adapter.fetch_items(on_progress=on_progress)


def _process_items(
    db: Session, competitor: Competitor, items: list[ParsedItem]
) -> dict:
    """Сопоставление, обновление офферов, снапшот и дифф-события."""
    today = date.today()
    matcher = ProductMatcher(db)
    new_count = 0

    # Индекс существующих офферов конкурента: url -> offer
    existing_offers = {
        o.url: o
        for o in db.scalars(
            select(Offer).where(Offer.competitor_id == competitor.id)
        ).all()
    }

    for item in items:
        product = matcher.match(item)
        offer = existing_offers.get(item.url)
        is_new_offer = offer is None
        if is_new_offer:
            offer = Offer(
                product_id=product.id,
                competitor_id=competitor.id,
                url=item.url,
                first_seen_at=today,
            )
            db.add(offer)
            new_count += 1
        else:
            if offer.product_id != product.id:
                offer.product_id = product.id

        # Обновляем текущее состояние оффера
        stock_status, stock_qty = _stock(item)
        offer.price = item.price
        offer.old_price = item.old_price
        offer.stock_status = stock_status
        offer.stock_qty = stock_qty
        offer.warehouse = item.warehouse
        offer.delivery_time = item.delivery_time
        offer.disappeared = False
        offer.last_seen_at = today
        if is_new_offer or offer.first_seen_at is None:
            offer.first_seen_at = today
        db.flush()

        # Ежедневный снапшот (append-only, ТЗ п.6)
        snapshot = db.scalar(
            select(Snapshot).where(
                Snapshot.offer_id == offer.id, Snapshot.day == today
            )
        )
        if snapshot is None:
            snapshot = Snapshot(
                offer_id=offer.id,
                product_id=product.id,
                competitor_id=competitor.id,
                day=today,
                price=item.price,
                stock_qty=stock_qty,
                stock_status=stock_status,
            )
            db.add(snapshot)
            db.flush()
            _record_changes(db, offer, product, snapshot, is_new_offer)

    # Товары, которые были на сайте, но не встретились в этом проходе
    _mark_disappeared(db, competitor, set(existing_offers), items, today)
    db.flush()
    return {"new": new_count}


def _stock(item: ParsedItem) -> tuple[StockStatus, int | None]:
    from app.services.parser.normalize import parse_stock

    return parse_stock(item.stock_raw)


def _record_changes(
    db: Session,
    offer: Offer,
    product: Product,
    snapshot: Snapshot,
    is_new_offer: bool,
) -> None:
    """Сравнение со вчерашним днём (ТЗ п.7, 15)."""
    today = snapshot.day
    prev = db.scalar(
        select(Snapshot).where(
            Snapshot.offer_id == offer.id,
            Snapshot.day < today,
        ).order_by(Snapshot.day.desc()).limit(1)
    )

    if is_new_offer and prev is None:
        db.add(
            ChangeEvent(
                product_id=product.id,
                offer_id=offer.id,
                competitor_id=offer.competitor_id,
                day=today,
                change_type=ChangeType.APPEARED,
            )
        )
        return

    if prev is None:
        return

    # Изменение остатка
    if (
        prev.stock_qty is not None
        and snapshot.stock_qty is not None
        and prev.stock_qty != snapshot.stock_qty
    ):
        delta = snapshot.stock_qty - prev.stock_qty
        if delta < 0:
            change = ChangeType.SALES  # предполагаемая продажа
        else:
            change = ChangeType.REPLENISHMENT  # поступление
        db.add(
            ChangeEvent(
                product_id=product.id,
                offer_id=offer.id,
                competitor_id=offer.competitor_id,
                day=today,
                change_type=change,
                delta_qty=delta,
            )
        )

    # Изменение цены
    if prev.price is not None and snapshot.price is not None and prev.price != snapshot.price:
        db.add(
            ChangeEvent(
                product_id=product.id,
                offer_id=offer.id,
                competitor_id=offer.competitor_id,
                day=today,
                change_type=(
                    ChangeType.PRICE_UP
                    if snapshot.price > prev.price
                    else ChangeType.PRICE_DOWN
                ),
                old_price=prev.price,
                new_price=snapshot.price,
            )
        )


def _mark_disappeared(
    db: Session,
    competitor: Competitor,
    seen_urls: set[str],
    items: list[ParsedItem],
    today: date,
) -> None:
    """«Товар исчез из каталога» (ТЗ п.16) — не считаем продажей."""
    parsed_urls = {i.url for i in items}
    offers = db.scalars(
        select(Offer).where(Offer.competitor_id == competitor.id)
    ).all()
    for offer in offers:
        if offer.url in parsed_urls or offer.disappeared:
            continue
        offer.disappeared = True
        offer.last_seen_at = today
        db.add(
            ChangeEvent(
                product_id=offer.product_id,
                offer_id=offer.id,
                competitor_id=competitor.id,
                day=today,
                change_type=ChangeType.DISAPPEARED,
            )
        )
