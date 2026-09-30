"""Выгрузка в Excel: 4 листа (ТЗ п.21)."""
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
    "in_stock": "В наличии",
    "out_of_stock": "Нет в наличии",
    "on_order": "Под заказ",
    "many": "Много",
    "unknown": "Неизвестно",
}


def _header(ws, columns: list[str]) -> None:
    from openpyxl.styles import Font

    for col, title in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"


@router.get("/excel")
def export_excel(db: Session = Depends(get_db)):
    """Выгрузка: Лист 1 «Текущие данные», 2 «История», 3 «Рекомендации», 4 «Цены конкурентов»."""
    wb = Workbook()

    # --- Лист 1: Текущие данные ---
    ws = wb.active
    ws.title = "Текущие данные"
    _header(
        ws,
        [
            "Артикул", "OEM", "Бренд", "Наименование", "Применяемость",
            "Категория", "Конкурент", "Цена", "Старая цена", "Скидка %",
            "Остаток", "Наличие", "URL", "Дата первого обнаружения",
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

    # --- Лист 2: История (все ежедневные снимки) ---
    ws2 = wb.create_sheet("История")
    _header(ws2, ["Дата", "Артикул", "Наименование", "Конкурент", "Цена", "Остаток", "Статус"])
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

    # --- Лист 3: Рекомендации к закупке ---
    ws3 = wb.create_sheet("Рекомендации к закупке")
    _header(
        ws3,
        [
            "Рейтинг", "Индекс спроса", "Артикул", "Наименование", "Применяемость",
            "Конкурентов", "Мин. цена", "Макс. цена", "Средняя цена",
            "Продажи 7 дней", "Продажи 30 дней", "Среднедневной спрос",
            "Частота продаж %",
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

    # --- Лист 4: Цены конкурентов ---
    ws4 = wb.create_sheet("Цены конкурентов")
    _header(ws4, ["Артикул", "Наименование", "Конкурент", "Цена", "Наличие", "URL"])
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
