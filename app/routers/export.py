"""Excel export: 4 sheets (spec §21)."""
from io import BytesIO

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Offer, Product, ProductMetrics, Snapshot, StockStatus

router = APIRouter()

STOCK_STATUS_LABELS = {
    "in_stock": "In stock",
    "out_of_stock": "Out of stock",
    "on_order": "On order",
    "many": "Many",
    "unknown": "Unknown",
}


def _header(ws, columns: list[str]) -> None:
    from openpyxl.styles import Font

    for col, title in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"


@router.get("/excel")
def export_excel(db: Session = Depends(get_db)):
    """Export: Sheet 1 "Current data", 2 "History", 3 "Recommendations", 4 "Competitor prices"."""
    wb = Workbook()

    # --- Sheet 1: Current data ---
    ws = wb.active
    ws.title = "Current data"
    _header(
        ws,
        [
            "SKU", "OEM", "Brand", "Name", "Application",
            "Category", "Competitor", "Price", "Old price", "Discount %",
            "Stock", "Availability", "URL", "First seen date",
        ],
    )
    offers = db.scalars(
        select(Offer)
        .options(joinedload(Offer.product), joinedload(Offer.competitor))
        .where(Offer.disappeared.is_(False))
    ).unique().all()
    for row_idx, o in enumerate(offers, 2):
        p = o.product
        ws.append(
            [
                p.article, p.oem, p.brand, p.name, p.applicability, p.category,
                o.competitor.name,
                float(o.price) if o.price else None,
                float(o.old_price) if o.old_price else None,
                float(o.discount_pct) if o.discount_pct else None,
                o.stock_qty,
                STOCK_STATUS_LABELS.get(o.stock_status.value, o.stock_status.value),
                o.url,
                o.first_seen_at.isoformat() if o.first_seen_at else None,
            ]
        )
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 50)

    # --- Sheet 2: History (all daily snapshots) ---
    ws2 = wb.create_sheet("History")
    _header(ws2, ["Date", "SKU", "Name", "Competitor", "Price", "Stock", "Status"])
    snapshots = db.scalars(
        select(Snapshot)
        .join(Product)
        .order_by(Snapshot.day.desc())
        .limit(50000)
    ).all()
    product_cache: dict[int, Product] = {}
    competitor_cache: dict[int, str] = {}
    for s in snapshots:
        p = product_cache.setdefault(s.product_id, db.get(Product, s.product_id))
        cname = competitor_cache.get(s.competitor_id)
        if cname is None:
            from app.models import Competitor

            comp = db.get(Competitor, s.competitor_id)
            cname = comp.name if comp else "—"
            competitor_cache[s.competitor_id] = cname
        ws2.append(
            [
                s.day.isoformat(),
                p.article if p else None,
                p.name if p else None,
                cname,
                float(s.price) if s.price else None,
                s.stock_qty,
                STOCK_STATUS_LABELS.get(s.stock_status.value, s.stock_status.value),
            ]
        )

    # --- Sheet 3: Purchase recommendations ---
    ws3 = wb.create_sheet("Purchase recommendations")
    _header(
        ws3,
        [
            "Rating", "Demand index", "SKU", "Name", "Application",
            "Competitors", "Min price", "Max price", "Average price",
            "Sales 7 days", "Sales 30 days", "Average daily demand",
            "Sales frequency %",
        ],
    )
    recs = db.execute(
        select(Product, ProductMetrics)
        .join(ProductMetrics)
        .order_by(ProductMetrics.demand_index.desc())
        .limit(1000)
    ).all()
    for p, m in recs:
        ws3.append(
            [
                m.rating, float(m.demand_index), p.article, p.name, p.applicability,
                m.competitors_count,
                float(m.price_min) if m.price_min else None,
                float(m.price_max) if m.price_max else None,
                float(m.price_avg) if m.price_avg else None,
                m.sales_7d, m.sales_30d,
                float(m.avg_daily_demand),
                float(m.sales_frequency_pct),
            ]
        )

    # --- Sheet 4: Competitor prices ---
    ws4 = wb.create_sheet("Competitor prices")
    _header(ws4, ["SKU", "Name", "Competitor", "Price", "Availability", "URL"])
    for o in offers:
        if o.price is None:
            continue
        p = o.product
        ws4.append(
            [
                p.article, p.name, o.competitor.name, float(o.price),
                STOCK_STATUS_LABELS.get(o.stock_status.value, o.stock_status.value),
                o.url,
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    filename = "monitoring_export.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
