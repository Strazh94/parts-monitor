"""Расчёт метрик спроса (ТЗ п.8, 9, 11, 12, 13).

Запускается после каждого парсинга: пересчитывает ProductMetrics
для всех товаров по накопленной истории снапшотов.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    ChangeEvent,
    ChangeType,
    Offer,
    Product,
    ProductMetrics,
    Snapshot,
    StockStatus,
)

PERIODS = (1, 7, 14, 30, 60, 90)

# Пороговые значения рейтинга (ТЗ п.9) — настраиваемые
RATING_RULES = {
    "A": {"min_frequency_pct": 40, "min_sales_30d": 5},
    "B": {"min_frequency_pct": 15, "min_sales_30d": 2},
    "C": {"min_frequency_pct": 0, "min_sales_30d": 1},
}

DEFAULT_WEIGHTS = {
    "w_sales_volume": 30,
    "w_frequency": 20,
    "w_competitors": 20,
    "w_stock_dynamics": 15,
    "w_price_change": 10,
    "w_days_observed": 5,
}


def recalculate_all(db: Session) -> int:
    """Пересчёт метрик для всех товаров. Возвращает число обработанных."""
    products = db.scalars(select(Product)).all()
    weights = _get_weights(db)
    today = date.today()

    # Один запрос на все данные за 90 дней — а не N запросов на товар
    since = today - timedelta(days=max(PERIODS))
    events = _load_events(db, since, today)
    offers_by_product = _load_offers(db)
    snapshots_meta = _load_snapshot_days(db, since, today)

    for product in products:
        metrics = db.get(ProductMetrics, product.id)
        if metrics is None:
            metrics = ProductMetrics(product_id=product.id)
            db.add(metrics)

        _fill_sales(metrics, events.get(product.id, []), snapshots_meta.get(product.id, []), today)
        offers = offers_by_product.get(product.id, [])
        _fill_competitors(metrics, offers)
        _fill_prices(metrics, offers)
        _fill_rating(metrics, product)
        _fill_index(metrics, weights)

    db.commit()
    return len(products)


def _load_events(db: Session, since: date, today: date) -> dict[int, list[ChangeEvent]]:
    rows = db.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.day >= since, ChangeEvent.day <= today)
        .order_by(ChangeEvent.day.asc())
    ).all()
    by_product: dict[int, list[ChangeEvent]] = {}
    for ev in rows:
        by_product.setdefault(ev.product_id, []).append(ev)
    return by_product


def _load_offers(db: Session) -> dict[int, list[Offer]]:
    rows = db.scalars(
        select(Offer).where(Offer.disappeared.is_(False))
    ).all()
    by_product: dict[int, list[Offer]] = {}
    for o in rows:
        by_product.setdefault(o.product_id, []).append(o)
    return by_product


def _load_snapshot_days(db: Session, since: date, today: date) -> dict[int, list[date]]:
    """Дни, в которые был хоть один снапшот товара (для частоты продаж)."""
    rows = db.execute(
        select(Snapshot.product_id, Snapshot.day)
        .where(Snapshot.day >= since, Snapshot.day <= today)
        .distinct()
    ).all()
    by_product: dict[int, list[date]] = {}
    for pid, day in rows:
        by_product.setdefault(pid, []).append(day)
    return by_product


def _fill_sales(
    metrics: ProductMetrics,
    events: list[ChangeEvent],
    observed_days: list[date],
    today: date,
) -> None:
    """Продажи по периодам = сумма отрицательных дельт остатка (ТЗ п.8)."""
    sales_by_period = {p: 0 for p in PERIODS}
    replenish_7d = 0
    days_with_decrease: set[date] = set()

    for ev in events:
        if ev.change_type == ChangeType.SALES and ev.delta_qty:
            days_ago = (today - ev.day).days
            sold = abs(ev.delta_qty)
            for period in PERIODS:
                if days_ago < period:
                    sales_by_period[period] += sold
            if days_ago < 7:
                replenish_7d += 0  # счётчик поступлений ниже
            days_with_decrease.add(ev.day)
        elif ev.change_type == ChangeType.REPLENISHMENT and ev.delta_qty:
            if (today - ev.day).days < 7:
                replenish_7d += 1

    metrics.sales_1d = sales_by_period[1]
    metrics.sales_7d = sales_by_period[7]
    metrics.sales_14d = sales_by_period[14]
    metrics.sales_30d = sales_by_period[30]
    metrics.sales_60d = sales_by_period[60]
    metrics.sales_90d = sales_by_period[90]
    metrics.replenishments_7d = replenish_7d

    # Дней наблюдения — сколько дней есть в истории (не больше 30 для частоты)
    metrics.days_observed = len(observed_days)

    # Частота продаж: % дней со снижением остатка (ТЗ п.8)
    if observed_days:
        window = [d for d in observed_days if (today - d).days <= 30]
        observed_30d = max(len(window), 1)
        days_in_window = [d for d in days_with_decrease if (today - d).days <= 30]
        metrics.sales_frequency_pct = round(
            100.0 * len(days_in_window) / observed_30d, 2
        )
    else:
        metrics.sales_frequency_pct = 0

    # Среднедневной спрос = продажи за 30 дней / дни наблюдения (ТЗ п.8)
    denom = min(metrics.days_observed, 30) or 1
    metrics.avg_daily_demand = round(metrics.sales_30d / denom, 3)


def _fill_competitors(metrics: ProductMetrics, offers: list[Offer]) -> None:
    """Сколько конкурентов одновременно продают артикул (ТЗ п.11)."""
    metrics.competitors_count = len({o.competitor_id for o in offers})


def _fill_prices(metrics: ProductMetrics, offers: list[Offer]) -> None:
    """Мин/макс/сред/медиана по активным предложениям (ТЗ п.13)."""
    prices = sorted(
        float(o.price) for o in offers if o.price is not None
    )
    if not prices:
        metrics.price_min = None
        metrics.price_max = None
        metrics.price_avg = None
        metrics.price_median = None
        return
    metrics.price_min = prices[0]
    metrics.price_max = prices[-1]
    metrics.price_avg = round(sum(prices) / len(prices), 2)
    n = len(prices)
    metrics.price_median = (
        prices[n // 2] if n % 2 == 1
        else round((prices[n // 2 - 1] + prices[n // 2]) / 2, 2)
    )


def _fill_rating(metrics: ProductMetrics, product: Product) -> None:
    """A/B/C/D/NEW (ТЗ п.9)."""
    # NEW: мало истории — товар появился недавно
    if metrics.days_observed < 3:
        metrics.rating = "NEW"
        return
    if (
        metrics.sales_frequency_pct >= RATING_RULES["A"]["min_frequency_pct"]
        and metrics.sales_30d >= RATING_RULES["A"]["min_sales_30d"]
    ):
        metrics.rating = "A"
    elif (
        metrics.sales_frequency_pct >= RATING_RULES["B"]["min_frequency_pct"]
        and metrics.sales_30d >= RATING_RULES["B"]["min_sales_30d"]
    ):
        metrics.rating = "B"
    elif metrics.sales_30d >= RATING_RULES["C"]["min_sales_30d"]:
        metrics.rating = "C"
    else:
        metrics.rating = "D"


def _fill_index(metrics: ProductMetrics, weights: dict) -> None:
    """Сводный индекс спроса 0..100 с настраиваемыми весами (ТЗ п.12).

    Каждый фактор нормируется в 0..1, затем взвешенная сумма * 100.
    """
    factors = {
        # Объем продаж за 30 дней: 10 шт. и более = максимум
        "w_sales_volume": min(metrics.sales_30d / 10.0, 1.0),
        # Частота продаж в %
        "w_frequency": min(metrics.sales_frequency_pct / 100.0, 1.0),
        # Конкурентов: 5 и больше = максимум
        "w_competitors": min(metrics.competitors_count / 5.0, 1.0),
        # Динамика остатков: поступления за 7 дней как сигнал активности
        "w_stock_dynamics": min(
            (metrics.sales_7d + metrics.replenishments_7d) / 10.0, 1.0
        ),
        # Изменение цены за период: считаем по наличию цены вообще
        "w_price_change": 1.0 if metrics.price_min is not None else 0.0,
        # Дни наблюдения: 30+ дней = максимум доверия
        "w_days_observed": min(metrics.days_observed / 30.0, 1.0),
    }

    total_weight = sum(weights.get(k, 0) for k in factors) or 1
    score = sum(
        factors[k] * weights.get(k, 0) for k in factors
    )
    metrics.demand_index = round(100.0 * score / total_weight, 1)


def _get_weights(db: Session) -> dict:
    row = db.get(AppSetting, "demand_index_weights")
    return row.value if row else DEFAULT_WEIGHTS
