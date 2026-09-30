"""Р“Р»Р°РІРЅР°СЏ СЃС‚СЂР°РЅРёС†Р°: РїРѕРєР°Р·Р°С‚РµР»Рё РґРЅСЏ, РўРћРџ-20 (РўР— Рї.19)."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models import (
    ChangeEvent,
    ChangeType,
    Competitor,
    Offer,
    ParseRun,
    Product,
    ProductMetrics,
    RunStatus,
    StockStatus,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()

    competitors_count = db.scalar(
        select(func.count()).select_from(Competitor).where(Competitor.enabled)
    ) or 0

    products_total = db.scalar(select(func.count()).select_from(Product)) or 0

    # РўРѕРІР°СЂС‹ РІ РЅР°Р»РёС‡РёРё: РµСЃС‚СЊ Р°РєС‚РёРІРЅРѕРµ РїСЂРµРґР»РѕР¶РµРЅРёРµ СЃРѕ СЃС‚Р°С‚СѓСЃРѕРј "РІ РЅР°Р»РёС‡РёРё"
    in_stock = db.scalar(
        select(func.count(func.distinct(Offer.product_id)))
        .select_from(Offer)
        .where(
            Offer.disappeared.is_(False),
            Offer.stock_status == StockStatus.IN_STOCK,
        )
    ) or 0

    # РР·РјРµРЅРµРЅРёСЏ Р·Р° СЃРµРіРѕРґРЅСЏ
    changes_today = db.execute(
        select(ChangeEvent.change_type, func.count())
        .where(ChangeEvent.day == today)
        .group_by(ChangeEvent.change_type)
    ).all()
    changes_by_type = dict(changes_today)

    stock_changes = (
        changes_by_type.get(ChangeType.SALES, 0)
        + changes_by_type.get(ChangeType.REPLENISHMENT, 0)
    )
    price_changes = changes_by_type.get(ChangeType.PRICE_DOWN, 0) + changes_by_type.get(
        ChangeType.PRICE_UP, 0
    )
    new_products = changes_by_type.get(ChangeType.APPEARED, 0)
    disappeared = changes_by_type.get(ChangeType.DISAPPEARED, 0)

    last_run = db.scalar(
        select(ParseRun).order_by(ParseRun.started_at.desc()).limit(1)
    )
    processed_today = last_run.products_found if last_run else 0

    # РўРћРџ-20 РїРѕ РґРёРЅР°РјРёРєРµ СЃРїСЂРѕСЃР° (СЃСЂРµРґРЅРµРґРЅРµРІРЅРѕР№ СЃРїСЂРѕСЃ)
    top_demand = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.avg_daily_demand.desc(), ProductMetrics.sales_30d.desc())
        .limit(20)
    ).all()

    # РўРћРџ-20 РґР»СЏ Р·Р°РєСѓРїРєРё (РёРЅРґРµРєСЃ СЃРїСЂРѕСЃР°)
    top_purchase = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.demand_index.desc())
        .limit(20)
    ).all()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "competitors_count": competitors_count,
            "products_total": products_total,
            "in_stock": in_stock,
            "stock_changes": stock_changes,
            "price_changes": price_changes,
            "new_products": new_products,
            "disappeared": disappeared,
            "processed_today": processed_today,
            "top_demand": top_demand,
            "top_purchase": top_purchase,
            "today": today,
        },
    )
