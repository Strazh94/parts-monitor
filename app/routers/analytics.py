"""Analytics: ratings, prices, purchase recommendations (spec §9-13, 10)."""
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
    """Overview analytics: rating distribution, demand index."""
    from sqlalchemy import func

    rating_dist = dict(
        db.execute(
            select(ProductMetrics.rating, func.count())
            .group_by(ProductMetrics.rating)
        ).all()
    )

    # Products with rising / falling prices over 30 days — via price metrics
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
    """Top sales: ranking by estimated sales (spec §8, 20)."""
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
    """Price analysis by SKU: min/max/average/median, competitor prices (spec §13)."""
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

    # For the top products, pull the prices of every competitor
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
    """Section "Recommended items for purchase" (spec §10)."""
    rows = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.demand_index.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        request, "purchase.html", {"rows": rows}
    )
