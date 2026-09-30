"""Daily report built after parsing finishes (spec §20)."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ChangeEvent,
    ChangeType,
    ParseRun,
    Product,
    ProductMetrics,
)


def build_daily_report(db: Session, day: date | None = None) -> dict:
    """Builds the daily report: totals, TOP-50 sales, demand growth, new items."""
    day = day or date.today()

    runs = db.scalars(
        select(ParseRun).where(func.date(ParseRun.started_at) == day)
    ).all()
    sites_checked = len({r.competitor_id for r in runs})

    # Products checked — total across successful runs
    products_checked = sum(r.products_found for r in runs if r.status.value == "ok")

    changes = db.execute(
        select(ChangeEvent.change_type, func.count())
        .where(ChangeEvent.day == day)
        .group_by(ChangeEvent.change_type)
    ).all()
    by_type = dict(changes)

    sales_events = db.execute(
        select(
            ChangeEvent.product_id,
            func.sum(func.abs(ChangeEvent.delta_qty)).label("sold"),
        )
        .where(
            ChangeEvent.day == day,
            ChangeEvent.change_type == ChangeType.SALES,
        )
        .group_by(ChangeEvent.product_id)
        .order_by(func.sum(func.abs(ChangeEvent.delta_qty)).desc())
        .limit(50)
    ).all()

    top_sales = [
        (db.get(Product, pid), sold) for pid, sold in sales_events
    ]

    # TOP with growing demand — by index and average daily demand
    top_growing = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.avg_daily_demand.desc())
        .limit(20)
    ).all()

    # New items for the day
    new_pids = db.scalars(
        select(ChangeEvent.product_id)
        .where(ChangeEvent.day == day, ChangeEvent.change_type == ChangeType.APPEARED)
        .distinct()
    ).all()
    new_products = [db.get(Product, pid) for pid in new_pids][:50]

    # Most significant price changes
    price_changes = db.scalars(
        select(ChangeEvent)
        .where(
            ChangeEvent.day == day,
            ChangeEvent.change_type.in_([ChangeType.PRICE_UP, ChangeType.PRICE_DOWN]),
        )
        .limit(50)
    ).all()

    # Appeared at several competitors at once
    multi = db.execute(
        select(
            ChangeEvent.product_id,
            func.count(func.distinct(ChangeEvent.competitor_id)).label("cnt"),
        )
        .where(ChangeEvent.day == day, ChangeEvent.change_type == ChangeType.APPEARED)
        .group_by(ChangeEvent.product_id)
        .having(func.count(func.distinct(ChangeEvent.competitor_id)) >= 2)
        .limit(50)
    ).all()

    return {
        "day": day,
        "sites_checked": sites_checked,
        "products_checked": products_checked,
        "new_products": len(new_pids),
        "price_changes": by_type.get(ChangeType.PRICE_DOWN, 0)
        + by_type.get(ChangeType.PRICE_UP, 0),
        "stock_decreased": by_type.get(ChangeType.SALES, 0),
        "replenishments": by_type.get(ChangeType.REPLENISHMENT, 0),
        "disappeared": by_type.get(ChangeType.DISAPPEARED, 0),
        "top_sales": top_sales,
        "top_growing": top_growing,
        "new_items": new_products,
        "price_events": price_changes,
        "multi_competitor": [
            (db.get(Product, pid), cnt) for pid, cnt in multi
        ],
        "errors": [r for r in runs if r.status.value == "error"],
    }
