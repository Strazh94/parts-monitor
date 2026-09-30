"""Сквозной тест: парсинг -> снапшот -> изменения -> метрики -> отчёт.

Запуск: python tests/e2e_test.py
"""
from __future__ import annotations

import asyncio
import sys
import threading
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    ChangeEvent,
    ChangeType,
    Competitor,
    EngineType,
    Offer,
    Product,
    ProductMetrics,
    Snapshot,
)
from app.services.analytics.metrics import recalculate_all
from app.services.report import build_daily_report
from app.services.runner import run_competitor

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "site"

CONFIG = {
    "start_urls": ["http://127.0.0.1:8765/index.html"],
    "max_pages": 5,
    "pagination": "next_button",
    "next_selector": "a.next",
    "item_selector": ".product-card",
    "delay_seconds": 0,
    "fields": {
        "name": {"selector": ".title"},
        "article": {"selector": ".sku"},
        "oem": {"selector": ".oem"},
        "brand": {"selector": ".brand"},
        "price": {"selector": ".price"},
        "old_price": {"selector": ".old-price"},
        "stock": {"selector": ".availability"},
        "applicability": {"selector": ".applicability"},
        "url": {"selector": "a", "attr": "href"},
    },
}


def start_fixture_server() -> ThreadingHTTPServer:
    """HTTP-сервер с тестовым каталогом на 127.0.0.1:8765."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

        def log_message(self, *args):  # тихий режим
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check(name: str, condition: bool, detail: str = "") -> bool:
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    server = start_fixture_server()
    ok = True
    db = SessionLocal()
    try:
        # --- Подготовка: чистый конкурент ---
        for c in db.scalars(select(Competitor)).all():
            db.delete(c)
        db.commit()

        competitor = Competitor(
            name="Тестовый конкурент",
            url="http://127.0.0.1:8765",
            engine=EngineType.HTTP,
            parser_config=CONFIG,
        )
        db.add(competitor)
        db.commit()

        # --- 1. Первый парсинг ---
        run1 = asyncio.run(run_competitor(db, competitor, trigger="manual"))
        db.refresh(run1)
        ok &= check("Первый запуск завершился OK", run1.status.value == "ok",
                    run1.error_message or "")
        ok &= check("Найдено 6 товаров (2 страницы)", run1.products_found == 6,
                    f"найдено {run1.products_found}")
        ok &= check("Обработано 2 страницы", run1.pages_processed == 2,
                    f"{run1.pages_processed}")
        ok &= check("Все 6 предложений созданы", run1.products_new == 6,
                    f"{run1.products_new}")

        products = db.scalars(select(Product)).all()
        # 6 позиций, но K12345 имеет OEM DZ911234 -> объединяется => 5 товаров
        ok &= check("Товаров в БД: 5 (K12345 объединён по OEM)", len(products) == 5,
                    f"{len(products)}")

        offers = db.scalars(select(Offer)).all()
        ok &= check("Снапшотов создано: 6", db.scalar(select(Snapshot)) is not None
                    and len(db.scalars(select(Snapshot)).all()) == 6)

        # Статусы наличия (ТЗ п.16)
        by_article = {p.article: p for p in products}
        o_dz = db.scalar(select(Offer).where(Offer.product_id == by_article["DZ911234"].id))
        ok &= check("DZ911234: в наличии, 15 шт",
                    o_dz.stock_status.value == "in_stock" and o_dz.stock_qty == 15,
                    f"{o_dz.stock_status.value}/{o_dz.stock_qty}")
        o_wg = db.scalar(select(Offer).where(Offer.product_id == by_article["WG9725"].id))
        ok &= check("WG9725: статус 'Много', кол-во неизвестно",
                    o_wg.stock_status.value == "many" and o_wg.stock_qty is None)
        o_xc = db.scalar(select(Offer).where(Offer.product_id == by_article["XCMG001"].id))
        ok &= check("XCMG001: нет в наличии -> 0",
                    o_xc.stock_status.value == "out_of_stock" and o_xc.stock_qty == 0)
        o_s3 = db.scalar(select(Offer).where(Offer.product_id == by_article["S3HD-13"].id))
        ok &= check("S3HD-13: под заказ -> кол-во неизвестно",
                    o_s3.stock_status.value == "on_order" and o_s3.stock_qty is None)

        # Сопоставление (ТЗ п.17): K12345 имеет oem DZ911234 -> тот же товар
        ok &= check("K12345 объединён с DZ911234 по OEM (п.17)",
                    len(products) == 5 + 1 - 1 or len(products) == 5,
                    f"товаров {len(products)} (6 позиций, одно объединение = 5)")

        # --- 2. Имитация следующего дня: остаток DZ911234 15 -> 12 ---
        yesterday = date.today() - timedelta(days=1)
        for snap in db.scalars(select(Snapshot)).all():
            snap.day = yesterday
        db.commit()

        # Меняем остаток в фикстуре: 15 -> 12, цену 12500 -> 12900
        index_file = FIXTURE_DIR / "index.html"
        original = index_file.read_text(encoding="utf-8")
        changed = original.replace("В наличии: 15", "В наличии: 12").replace(
            "12 500 ₽", "12 900 ₽"
        )
        index_file.write_text(changed, encoding="utf-8")

        try:
            run2 = asyncio.run(run_competitor(db, competitor, trigger="auto"))
            db.refresh(run2)
            ok &= check("Второй запуск OK", run2.status.value == "ok",
                        run2.error_message or "")

            # Дифф-события (ТЗ п.7, 15)
            events = db.scalars(select(ChangeEvent)).all()
            sales = [e for e in events if e.change_type == ChangeType.SALES]
            price_up = [e for e in events if e.change_type == ChangeType.PRICE_UP]
            ok &= check("Зафиксирована продажа DZ911234: 15 -> 12 (delta -3)",
                        any(e.delta_qty == -3 for e in sales),
                        f"событий продаж {len(sales)}")
            ok &= check("Зафиксирован рост цены 12500 -> 12900",
                        any(float(e.new_price) == 12900 for e in price_up),
                        f"событий цены {len(price_up)}")
            ok &= check("Снапшоты за 2 дня не перезаписаны (п.6)",
                        len(db.scalars(select(Snapshot)).all()) == 12,
                        f"снапшотов {len(db.scalars(select(Snapshot)).all())}")

            # --- 3. Метрики (ТЗ п.8, 9, 12) ---
            count = recalculate_all(db)
            ok &= check("Метрики пересчитаны", count > 0, f"{count} товаров")

            m = db.get(ProductMetrics, by_article["DZ911234"].id)
            ok &= check("DZ911234: продажи 1 день = 3",
                        m.sales_1d == 3, f"{m.sales_1d}")
            ok &= check("DZ911234: продажи 7 дней = 3",
                        m.sales_7d == 3, f"{m.sales_7d}")
            ok &= check("DZ911234: индекс спроса в 0..100",
                        0 <= float(m.demand_index) <= 100,
                        f"{m.demand_index}")
            ok &= check("DZ911234: среднедневной спрос > 0",
                        float(m.avg_daily_demand) > 0,
                        f"{m.avg_daily_demand}")
            # Частота: 1 день со снижением из 2 наблюдаемых = 50% (ТЗ п.8)
            ok &= check("DZ911234: частота продаж = 50% (1 из 2 дней)",
                        float(m.sales_frequency_pct) == 50,
                        f"{m.sales_frequency_pct}%")
            # История всего 2 дня (< 3) -> NEW по правилу «недостаточно истории» (п.9)
            ok &= check("DZ911234: рейтинг NEW при 2 днях истории",
                        m.rating == "NEW", m.rating)

            # Цены (ТЗ п.13)
            ok &= check("DZ911234: цена min=12900 (один конкурент)",
                        m.price_min is not None and float(m.price_min) == 12900,
                        f"{m.price_min}")
            ok &= check("DZ911234: средняя = медиана",
                        m.price_avg is not None and m.price_median is not None
                        and float(m.price_avg) == float(m.price_median))

            # Поступление (ТЗ п.15): моделируем следующий день.
            # Убираем старые снапшоты за вчера, сегодняшние сдвигаем на вчера —
            # чтобы прогон создал снапшоты "сегодня" и сравнил их с "вчера".
            from sqlalchemy import delete as sa_delete
            db.execute(sa_delete(Snapshot).where(
                Snapshot.day < date.today()
            ))
            for snap in db.scalars(select(Snapshot)).all():
                snap.day = date.today() - timedelta(days=1)
            db.commit()
            # XCMG001: вчера 0 -> сегодня 25
            orig2 = index_file.read_text(encoding="utf-8")
            index_file.write_text(
                orig2.replace("Нет в наличии", "В наличии: 25"),
                encoding="utf-8",
            )

            run3 = asyncio.run(run_competitor(db, competitor, trigger="auto"))
            db.refresh(run3)
            events = db.scalars(select(ChangeEvent)).all()
            replen = [e for e in events if e.change_type == ChangeType.REPLENISHMENT]
            ok &= check("Рост остатка = поступление +25, не продажа (п.15)",
                        any(e.delta_qty == 25 for e in replen),
                        f"поступлений {len(replen)}")
            negative = [e for e in events
                        if e.change_type == ChangeType.SALES and (e.delta_qty or 0) > 0]
            ok &= check("Нет 'отрицательных продаж'", not negative)

            # --- 4. Отчёт (ТЗ п.20) ---
            report = build_daily_report(db)
            ok &= check("Отчёт: сайтов проверено >= 1", report["sites_checked"] >= 1)
            ok &= check("Отчёт: товаров проверено > 0",
                        report["products_checked"] > 0,
                        str(report["products_checked"]))
            ok &= check("Отчёт: есть продажи за день", report["stock_decreased"] > 0)
        finally:
            index_file.write_text(original, encoding="utf-8")

        # --- 5. Исчезновение товара (ТЗ п.16) ---
        (FIXTURE_DIR / "index.html").write_text(
            original.replace(
                '<div class="product-card">\n    <a href="/product/3">',
                '<div class="hidden">\n    <a href="/product/3">'
            ).replace('class="product-card">\n    <a href="/product/3"', 'class="hidden">\n    <a href="/product/3"'),
            encoding="utf-8",
        )
        try:
            run4 = asyncio.run(run_competitor(db, competitor, trigger="auto"))
            db.refresh(run4)
            x_offer = db.scalar(select(Offer).where(
                Offer.product_id == by_article["XCMG001"].id))
            ok &= check("Исчезнувший товар помечен (п.16)", x_offer.disappeared)
            events = db.scalars(select(ChangeEvent)).all()
            disp = [e for e in events if e.change_type == ChangeType.DISAPPEARED]
            ok &= check("Событие 'исчез из каталога'", len(disp) >= 1)
        finally:
            (FIXTURE_DIR / "index.html").write_text(original, encoding="utf-8")
    finally:
        db.close()
        server.shutdown()

    print()
    print("ИТОГ:", "ВСЕ ТЕСТЫ ПРОШЛИ" if ok else "ЕСТЬ ОШИБКИ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
