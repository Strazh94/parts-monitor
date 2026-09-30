"""РђРЅР°Р»РёС‚РёРєР°: СЂРµР№С‚РёРЅРіРё, С†РµРЅС‹, СЂРµРєРѕРјРµРЅРґР°С†РёРё Рє Р·Р°РєСѓРїРєРµ (РўР— Рї.9-13, 10)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.templating import templates
from app.models import Product, ProductMetrics, StockStatus
from app.models import Offer

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def analytics_home(request: Request, db: Session = Depends(get_db)):
    """РЎРІРѕРґРЅР°СЏ Р°РЅР°Р»РёС‚РёРєР°: СЂР°СЃРїСЂРµРґРµР»РµРЅРёРµ СЂРµР№С‚РёРЅРіРѕРІ, РёРЅРґРµРєСЃ СЃРїСЂРѕСЃР°."""
    from sqlalchemy import func

    rating_dist = dict(
        db.execute(
            select(ProductMetrics.rating, func.count())
            .group_by(ProductMetrics.rating)
        ).all()
    )

    # РўРѕРІР°СЂС‹ СЃ СЂРѕСЃС‚РѕРј С†РµРЅ / РїР°РґРµРЅРёРµРј С†РµРЅ Р·Р° 30 РґРЅРµР№ вЂ” С‡РµСЂРµР· РјРµС‚СЂРёРєРё С†РµРЅ
    top_index = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.demand_index.desc())
        .limit(50)
    ).all()

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {"rating_dist": rating_dist, "top_index": top_index},
    )


@router.get("/top", response_class=HTMLResponse)
def top_sales(request: Request, db: Session = Depends(get_db)):
    """ТОП продаж: ранжирование по предполагаемым продажам (ТЗ п.8, 20)."""
    rows = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(
            ProductMetrics.sales_30d.desc(),
            ProductMetrics.sales_7d.desc(),
            ProductMetrics.avg_daily_demand.desc(),
        )
        .limit(100)
    ).all()
    return templates.TemplateResponse(request, "top.html", {"rows": rows})


@router.get("/prices", response_class=HTMLResponse)
def price_analysis(request: Request, q: str = "", db: Session = Depends(get_db)):
    """РђРЅР°Р»РёР· С†РµРЅ РїРѕ Р°СЂС‚РёРєСѓР»Р°Рј: РјРёРЅ/РјР°РєСЃ/СЃСЂРµРґ/РјРµРґРёР°РЅР°, С†РµРЅС‹ РєРѕРЅРєСѓСЂРµРЅС‚РѕРІ (РўР— Рї.13)."""
    query = (
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .where(ProductMetrics.price_min.isnot(None))
    )
    if q:
        like = f"%{q.strip()}%"
        from sqlalchemy import or_

        query = query.where(
            or_(Product.article.ilike(like), Product.name.ilike(like), Product.oem.ilike(like))
        )
    rows = db.execute(
        query.order_by(ProductMetrics.demand_index.desc()).limit(100)
    ).all()

    # Р”Р»СЏ С‚РѕРїРѕРІС‹С… С‚РѕРІР°СЂРѕРІ РїРѕРґС‚СЏРіРёРІР°РµРј С†РµРЅС‹ РєР°Р¶РґРѕРіРѕ РєРѕРЅРєСѓСЂРµРЅС‚Р°
    product_ids = [p.id for p, _ in rows]
    offers_by_product: dict[int, list] = {}
    if product_ids:
        offers = db.scalars(
            select(Offer)
            .where(
                Offer.product_id.in_(product_ids),
                Offer.disappeared.is_(False),
                Offer.stock_status != StockStatus.OUT_OF_STOCK,
            )
            .options(joinedload(Offer.competitor))
        ).unique().all()
        for o in offers:
            offers_by_product.setdefault(o.product_id, []).append(o)

    return templates.TemplateResponse(
        request,
        "prices.html",
        {"rows": rows, "q": q, "offers_by_product": offers_by_product},
    )


@router.get("/purchase", response_class=HTMLResponse)
def purchase_recommendations(request: Request, db: Session = Depends(get_db)):
    """Р Р°Р·РґРµР» В«Р РµРєРѕРјРµРЅРґСѓРµРјС‹Рµ РїРѕР·РёС†РёРё РґР»СЏ Р·Р°РєСѓРїРєРёВ» (РўР— Рї.10)."""
    rows = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.demand_index.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        request, "purchase.html", {"rows": rows}
    )
