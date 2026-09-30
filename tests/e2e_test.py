"""End-to-end test: parsing -> snapshot -> changes -> metrics -> report.

Run: python tests/e2e_test.py
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
    """HTTP server with the test catalog on 127.0.0.1:8765."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

        def log_message(self, *args):  # quiet mode
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
        # --- Setup: a clean competitor ---
        for c in db.scalars(select(Competitor)).all():
            db.delete(c)
        db.commit()

        competitor = Competitor(
            name="Test competitor",
            url="http://127.0.0.1:8765",
            engine=EngineType.HTTP,
            parser_config=CONFIG,
        )
        db.add(competitor)
        db.commit()

        # --- 1. First parse ---
        run1 = asyncio.run(run_competitor(db, competitor, trigger="manual"))
        db.refresh(run1)
        ok &= check("First run finished OK", run1.status.value == "ok",
                    run1.error_message or "")
        ok &= check("Found 6 products (2 pages)", run1.products_found == 6,
                    f"found {run1.products_found}")
        ok &= check("Processed 2 pages", run1.pages_processed == 2,
                    f"{run1.pages_processed}")
        ok &= check("All 6 offers created", run1.products_new == 6,
                    f"{run1.products_new}")

        products = db.scalars(select(Product)).all()
        # 6 items, but K12345 has OEM DZ911234 -> merged => 5 products
        ok &= check("Products in DB: 5 (K12345 merged by OEM)", len(products) == 5,
                    f"{len(products)}")

        offers = db.scalars(select(Offer)).all()
        ok &= check("Snapshots created: 6", db.scalar(select(Snapshot)) is not None
                    and len(db.scalars(select(Snapshot)).all()) == 6)

        # Availability statuses (spec §16)
        by_article = {p.article: p for p in products}
        o_dz = db.scalar(select(Offer).where(Offer.product_id == by_article["DZ911234"].id))
        ok &= check("DZ911234: in stock, 15 pcs",
                    o_dz.stock_status.value == "in_stock" and o_dz.stock_qty == 15,
                    f"{o_dz.stock_status.value}/{o_dz.stock_qty}")
        o_wg = db.scalar(select(Offer).where(Offer.product_id == by_article["WG9725"].id))
        ok &= check("WG9725: status 'Many', quantity unknown",
                    o_wg.stock_status.value == "many" and o_wg.stock_qty is None)
        o_xc = db.scalar(select(Offer).where(Offer.product_id == by_article["XCMG001"].id))
        ok &= check("XCMG001: out of stock -> 0",
                    o_xc.stock_status.value == "out_of_stock" and o_xc.stock_qty == 0)
        o_s3 = db.scalar(select(Offer).where(Offer.product_id == by_article["S3HD-13"].id))
        ok &= check("S3HD-13: on order -> quantity unknown",
                    o_s3.stock_status.value == "on_order" and o_s3.stock_qty is None)

        # Matching (spec §17): K12345 has oem DZ911234 -> the same product
        ok &= check("K12345 merged with DZ911234 by OEM (§17)",
                    len(products) == 5 + 1 - 1 or len(products) == 5,
                    f"products {len(products)} (6 items, one merge = 5)")

        # --- 2. Simulate the next day: DZ911234 stock 15 -> 12 ---
        yesterday = date.today() - timedelta(days=1)
        for snap in db.scalars(select(Snapshot)).all():
            snap.day = yesterday
        db.commit()

        # Change the stock in the fixture: 15 -> 12, price 12500 -> 12900
        index_file = FIXTURE_DIR / "index.html"
        original = index_file.read_text(encoding="utf-8")
        changed = original.replace("В наличии: 15", "В наличии: 12").replace(
            "12 500 ₽", "12 900 ₽"
        )
        index_file.write_text(changed, encoding="utf-8")

        try:
            run2 = asyncio.run(run_competitor(db, competitor, trigger="auto"))
            db.refresh(run2)
            ok &= check("Second run OK", run2.status.value == "ok",
                        run2.error_message or "")

            # Diff events (spec §7, 15)
            events = db.scalars(select(ChangeEvent)).all()
            sales = [e for e in events if e.change_type == ChangeType.SALES]
            price_up = [e for e in events if e.change_type == ChangeType.PRICE_UP]
            ok &= check("Sale recorded for DZ911234: 15 -> 12 (delta -3)",
                        any(e.delta_qty == -3 for e in sales),
                        f"sales events {len(sales)}")
            ok &= check("Price increase recorded 12500 -> 12900",
                        any(float(e.new_price) == 12900 for e in price_up),
                        f"price events {len(price_up)}")
            ok &= check("Snapshots for 2 days not overwritten (§6)",
                        len(db.scalars(select(Snapshot)).all()) == 12,
                        f"snapshots {len(db.scalars(select(Snapshot)).all())}")

            # --- 3. Metrics (spec §8, 9, 12) ---
            count = recalculate_all(db)
            ok &= check("Metrics recalculated", count > 0, f"{count} products")

            m = db.get(ProductMetrics, by_article["DZ911234"].id)
            ok &= check("DZ911234: sales 1 day = 3",
                        m.sales_1d == 3, f"{m.sales_1d}")
            ok &= check("DZ911234: sales 7 days = 3",
                        m.sales_7d == 3, f"{m.sales_7d}")
            ok &= check("DZ911234: demand index within 0..100",
                        0 <= float(m.demand_index) <= 100,
                        f"{m.demand_index}")
            ok &= check("DZ911234: average daily demand > 0",
                        float(m.avg_daily_demand) > 0,
                        f"{m.avg_daily_demand}")
            # Frequency: 1 day with a decrease out of 2 observed = 50% (spec §8)
            ok &= check("DZ911234: sales frequency = 50% (1 of 2 days)",
                        float(m.sales_frequency_pct) == 50,
                        f"{m.sales_frequency_pct}%")
            # History is only 2 days (< 3) -> NEW by the "not enough history" rule (§9)
            ok &= check("DZ911234: rating NEW with 2 days of history",
                        m.rating == "NEW", m.rating)

            # Prices (spec §13)
            ok &= check("DZ911234: min price=12900 (one competitor)",
                        m.price_min is not None and float(m.price_min) == 12900,
                        f"{m.price_min}")
            ok &= check("DZ911234: average = median",
                        m.price_avg is not None and m.price_median is not None
                        and float(m.price_avg) == float(m.price_median))

            # Restock (spec §15): simulate the next day.
            # Remove old snapshots for yesterday, shift today's ones to yesterday —
            # so the run creates "today" snapshots and compares them with "yesterday".
            from sqlalchemy import delete as sa_delete
            db.execute(sa_delete(Snapshot).where(
                Snapshot.day < date.today()
            ))
            for snap in db.scalars(select(Snapshot)).all():
                snap.day = date.today() - timedelta(days=1)
            db.commit()
            # XCMG001: yesterday 0 -> today 25
            orig2 = index_file.read_text(encoding="utf-8")
            index_file.write_text(
                orig2.replace("Нет в наличии", "В наличии: 25"),
                encoding="utf-8",
            )

            run3 = asyncio.run(run_competitor(db, competitor, trigger="auto"))
            db.refresh(run3)
            events = db.scalars(select(ChangeEvent)).all()
            replen = [e for e in events if e.change_type == ChangeType.REPLENISHMENT]
            ok &= check("Stock increase = restock +25, not a sale (§15)",
                        any(e.delta_qty == 25 for e in replen),
                        f"restocks {len(replen)}")
            negative = [e for e in events
                        if e.change_type == ChangeType.SALES and (e.delta_qty or 0) > 0]
            ok &= check("No 'negative sales'", not negative)

            # --- 4. Report (spec §20) ---
            report = build_daily_report(db)
            ok &= check("Report: sites checked >= 1", report["sites_checked"] >= 1)
            ok &= check("Report: products checked > 0",
                        report["products_checked"] > 0,
                        str(report["products_checked"]))
            ok &= check("Report: there are sales for the day", report["stock_decreased"] > 0)
        finally:
            index_file.write_text(original, encoding="utf-8")

        # --- 5. Product disappearance (spec §16) ---
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
            ok &= check("Disappeared product marked (§16)", x_offer.disappeared)
            events = db.scalars(select(ChangeEvent)).all()
            disp = [e for e in events if e.change_type == ChangeType.DISAPPEARED]
            ok &= check("Event 'disappeared from catalog'", len(disp) >= 1)
        finally:
            (FIXTURE_DIR / "index.html").write_text(original, encoding="utf-8")
    finally:
        db.close()
        server.shutdown()

    print()
    print("RESULT:", "ALL TESTS PASSED" if ok else "THERE ARE ERRORS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
