"""Demand metrics calculation (spec §8, 9, 11, 12, 13).

Runs after each parse: recalculates ProductMetrics
for all products from the accumulated snapshot history.
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

# Rating thresholds (spec §9) — configurable
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
    """Recalculate metrics for all products. Returns the number processed."""
    products = db.scalars(select(Product)).all()
    weights = _get_weights(db)
    today = date.today()

    # One query for all data over 90 days — instead of N queries per product
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
    """Days on which the product had at least one snapshot (for sales frequency)."""
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
    """Sales per period = sum of negative stock deltas (spec §8)."""
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
                replenish_7d += 0  # restock counter is below
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

    # Days observed — how many days exist in the history (at most 30 for frequency)
    metrics.days_observed = len(observed_days)

    # Sales frequency: % of days with a stock decrease (spec §8)
    if observed_days:
        window = [d for d in observed_days if (today - d).days <= 30]
        observed_30d = max(len(window), 1)
        days_in_window = [d for d in days_with_decrease if (today - d).days <= 30]
        metrics.sales_frequency_pct = round(
            100.0 * len(days_in_window) / observed_30d, 2
        )
    else:
        metrics.sales_frequency_pct = 0

    # Average daily demand = sales over 30 days / days observed (spec §8)
    denom = min(metrics.days_observed, 30) or 1
    metrics.avg_daily_demand = round(metrics.sales_30d / denom, 3)


def _fill_competitors(metrics: ProductMetrics, offers: list[Offer]) -> None:
    """How many competitors sell the same SKU at once (spec §11)."""
    metrics.competitors_count = len({o.competitor_id for o in offers})


def _fill_prices(metrics: ProductMetrics, offers: list[Offer]) -> None:
    """Min/max/average/median across active offers (spec §13)."""
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
    """A/B/C/D/NEW (spec §9)."""
    # NEW: little history — the product appeared recently
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
    """Overall demand index 0..100 with configurable weights (spec §12).

    Each factor is normalized to 0..1, then the weighted sum * 100.
    """
    factors = {
        # Sales volume over 30 days: 10 pcs or more = maximum
        "w_sales_volume": min(metrics.sales_30d / 10.0, 1.0),
        # Sales frequency in %
        "w_frequency": min(metrics.sales_frequency_pct / 100.0, 1.0),
        # Competitors: 5 or more = maximum
        "w_competitors": min(metrics.competitors_count / 5.0, 1.0),
        # Stock dynamics: restocks over 7 days as an activity signal
        "w_stock_dynamics": min(
            (metrics.sales_7d + metrics.replenishments_7d) / 10.0, 1.0
        ),
        # Price change over the period: counted by whether a price exists at all
        "w_price_change": 1.0 if metrics.price_min is not None else 0.0,
        # Days observed: 30+ days = maximum confidence
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
