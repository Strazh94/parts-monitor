"""РўРѕРІР°СЂС‹: РїРѕРёСЃРє, С„РёР»СЊС‚СЂС‹ (РўР— Рї.18), РєР°СЂС‚РѕС‡РєР° СЃ РёСЃС‚РѕСЂРёРµР№ (РўР— Рї.14)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.templating import templates
from app.models import (
    Competitor,
    Offer,
    Product,
    ProductMetrics,
    Snapshot,
)

router = APIRouter()

# Р”РѕСЃС‚СѓРїРЅС‹Рµ С„РёР»СЊС‚СЂС‹: РєР»СЋС‡ -> РєРѕР»РѕРЅРєР° РјРµС‚СЂРёРє (РўР— Рї.18)
RATING_FILTERS = ("A", "B", "C", "D", "NEW")


@router.get("", response_class=HTMLResponse)
def product_list(
    request: Request,
    q: str = "",
    rating: str = "",
    min_competitors: int = 0,
    max_price: float | None = None,
    in_stock_only: bool = False,
    sort: str = "index",
    db: Session = Depends(get_db),
):
    """РЎРїРёСЃРѕРє С‚РѕРІР°СЂРѕРІ СЃ РїРѕРёСЃРєРѕРј РїРѕ Р°СЂС‚РёРєСѓР»Сѓ/OEM/РЅР°Р·РІР°РЅРёСЋ/Р±СЂРµРЅРґСѓ Рё С„РёР»СЊС‚СЂР°РјРё."""
    query = (
        select(Product, ProductMetrics)
        .join(ProductMetrics, isouter=True)
    )

    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Product.article.ilike(like),
                Product.oem.ilike(like),
                Product.name.ilike(like),
                Product.brand.ilike(like),
            )
        )
    if rating in RATING_FILTERS:
        query = query.where(ProductMetrics.rating == rating)
    if min_competitors > 0:
        query = query.where(ProductMetrics.competitors_count >= min_competitors)
    if max_price is not None:
        query = query.where(ProductMetrics.price_min <= max_price)
    if in_stock_only:
        query = (
            query.join(Offer, Offer.product_id == Product.id, isouter=True)
            .where(Offer.disappeared.is_(False), Offer.stock_status == "in_stock")
        )

    sort_map = {
        "index": ProductMetrics.demand_index.desc(),
        "sales30": ProductMetrics.sales_30d.desc(),
        "sales7": ProductMetrics.sales_7d.desc(),
        "demand": ProductMetrics.avg_daily_demand.desc(),
        "price_asc": ProductMetrics.price_min.asc(),
        "competitors": ProductMetrics.competitors_count.desc(),
        "name": Product.name.asc(),
    }
    query = query.order_by(sort_map.get(sort, ProductMetrics.demand_index.desc()))
    rows = db.execute(query.limit(200)).all()

    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "rows": rows,
            "q": q,
            "rating": rating,
            "min_competitors": min_competitors,
            "max_price": max_price,
            "in_stock_only": in_stock_only,
            "sort": sort,
            "ratings": RATING_FILTERS,
        },
    )


@router.get("/{product_id}", response_class=HTMLResponse)
def product_card(request: Request, product_id: int, db: Session = Depends(get_db)):
    """РљР°СЂС‚РѕС‡РєР° С‚РѕРІР°СЂР°: РіСЂР°С„РёРєРё РёСЃС‚РѕСЂРёРё + С‚Р°Р±Р»РёС†Р° (РўР— Рї.14)."""
    product = db.get(Product, product_id)
    if product is None:
        return HTMLResponse("РўРѕРІР°СЂ РЅРµ РЅР°Р№РґРµРЅ", status_code=404)

    metrics = db.get(ProductMetrics, product_id)

    # Р’СЃСЏ РёСЃС‚РѕСЂРёСЏ РїРѕ РІСЃРµРј РєРѕРЅРєСѓСЂРµРЅС‚Р°Рј, РѕС‚СЃРѕСЂС‚РёСЂРѕРІР°РЅРЅР°СЏ РїРѕ РґР°С‚Рµ
    snapshots = db.scalars(
        select(Snapshot)
        .where(Snapshot.product_id == product_id)
        .order_by(Snapshot.day.asc())
    ).all()

    # РџСЂРµРґР»РѕР¶РµРЅРёСЏ РєРѕРЅРєСѓСЂРµРЅС‚РѕРІ
    offers = db.scalars(
        select(Offer)
        .where(Offer.product_id == product_id)
        .options(joinedload(Offer.competitor))
    ).unique().all()

    # РЎРµСЂРёР°Р»РёР·Р°С†РёСЏ РґР»СЏ РіСЂР°С„РёРєРѕРІ: РїРѕ РєР°Р¶РґРѕРјСѓ РєРѕРЅРєСѓСЂРµРЅС‚Сѓ С‚РѕС‡РєРё (РґР°С‚Р°, С†РµРЅР°, РѕСЃС‚Р°С‚РѕРє)
    series: dict[str, dict] = {}
    for s in snapshots:
        name = next(
            (o.competitor.name for o in offers if o.id == s.offer_id), "вЂ”"
        )
        entry = series.setdefault(name, {"stock": [], "price": []})
        entry["stock"].append({"x": s.day.isoformat(), "y": s.stock_qty})
        entry["price"].append({"x": s.day.isoformat(), "y": float(s.price) if s.price else None})

    # РЎРІРѕРґРЅР°СЏ С‚Р°Р±Р»РёС†Р° РёСЃС‚РѕСЂРёРё (РґР°С‚Р°, С†РµРЅР° min, РѕСЃС‚Р°С‚РѕРє sum, РёР·РјРµРЅРµРЅРёРµ)
    by_day: dict = {}
    for s in snapshots:
        d = by_day.setdefault(s.day, {"prices": [], "stocks": []})
        if s.price is not None:
            d["prices"].append(float(s.price))
        if s.stock_qty is not None:
            d["stocks"].append(s.stock_qty)

    history = []
    prev_total = None
    for day in sorted(by_day):
        prices = by_day[day]["prices"]
        stocks = by_day[day]["stocks"]
        total = sum(stocks) if stocks else None
        delta = (
            total - prev_total
            if total is not None and prev_total is not None
            else None
        )
        history.append(
            {
                "day": day,
                "price": min(prices) if prices else None,
                "price_max": max(prices) if prices else None,
                "total": total,
                "delta": delta,
            }
        )
        if total is not None:
            prev_total = total

    import json

    return templates.TemplateResponse(
        request,
        "product_card.html",
        {
            "product": product,
            "metrics": metrics,
            "offers": offers,
            "history": history,
            "series_json": json.dumps(series, ensure_ascii=False),
        },
    )
